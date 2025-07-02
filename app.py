import os
import sys
import datetime
import re
import json
import pytz
import mimetypes
import zipfile
from io import BytesIO
from collections import defaultdict
from dateutil.relativedelta import relativedelta
from functools import wraps

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, render_template, redirect, url_for, abort, send_from_directory, flash, jsonify, Response, session, g
from werkzeug.utils import secure_filename
from cachetools import TTLCache

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

import pandas as pd
import qrcode
import base64

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (MessageEvent, TextMessage, TextSendMessage)

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit

app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_dev_must_change')
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_ENV') == 'production'

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

CLIENT_SECRETS_FILE = 'client_secrets.json'
TOKEN_FILE = 'token.json'
SETTINGS_FILE = 'settings.json'
SCOPES = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/drive']
THAILAND_TZ = pytz.timezone('Asia/Bangkok')

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
line_bot_api = None
if LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET:
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(LINE_CHANNEL_SECRET)

#<editor-fold desc="Settings & Auth">
def load_app_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
        except (json.JSONDecodeError, IOError): pass
    return {}

def save_app_settings(settings_data):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings_data, f, ensure_ascii=False, indent=4)
        return True
    except IOError: return False

def get_google_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception: return None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(TOKEN_FILE, 'w') as token:
                    token.write(creds.to_json())
            except Exception:
                if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE)
                return None
        else: return None
    return creds

def get_google_service(api_name, api_version):
    creds = get_google_credentials()
    if not creds: return None
    try:
        return build(api_name, api_version, credentials=creds, cache_discovery=False)
    except Exception: return None

def google_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not g.user_logged_in:
            return redirect(url_for('auth_landing'))
        return f(*args, **kwargs)
    return decorated_function
#</editor-fold>

#<editor-fold desc="Utilities">
def parse_customer_info_from_notes(notes):
    info = {'name': '', 'phone': '', 'address': '', 'map_url': None}
    if not notes: return info
    name_match = re.search(r"ลูกค้า:\s*(.*)", notes, re.IGNORECASE)
    phone_match = re.search(r"เบอร์โทรศัพท์:\s*(.*)", notes, re.IGNORECASE)
    address_match = re.search(r"ที่อยู่:\s*(.*)", notes, re.IGNORECASE)
    map_url_match = re.search(r"https?://(?:www\.)?google\.com/maps/.*", notes)
    if name_match: info['name'] = name_match.group(1).strip()
    if phone_match: info['phone'] = phone_match.group(1).strip()
    if address_match: info['address'] = address_match.group(1).strip()
    if map_url_match: info['map_url'] = map_url_match.group(0).strip()
    return info

def parse_google_task_dates(task_item):
    parsed = task_item.copy()
    for key in ['due', 'completed', 'created', 'updated']:
        if parsed.get(key):
            try:
                dt_str = parsed[key].replace('Z', '+00:00')
                dt_utc = datetime.datetime.fromisoformat(dt_str)
                parsed[f'{key}_formatted'] = dt_utc.astimezone(THAILAND_TZ).strftime("%d/%m/%Y %H:%M")
                if key == 'due':
                    parsed['due_for_input'] = dt_utc.astimezone(THAILAND_TZ).strftime("%Y-%m-%dT%H:%M")
            except (ValueError, TypeError):
                parsed[f'{key}_formatted'], parsed['due_for_input'] = '', ''
    return parsed

@app.context_processor
def inject_globals():
    return {'thaizone': THAILAND_TZ, 'now': datetime.datetime.now(THAILAND_TZ)}

@app.before_request
def before_request_func():
    g.user_logged_in = os.path.exists(TOKEN_FILE)
#</editor-fold>

#<editor-fold desc="Core App Routes">
@app.route("/")
def index():
    if not os.path.exists(CLIENT_SECRETS_FILE):
        return "Error: client_secrets.json not found. Please follow setup instructions.", 500
    if not g.user_logged_in:
        return redirect(url_for('auth_landing'))
    return redirect(url_for('dashboard'))

@app.route("/dashboard")
@google_login_required
def dashboard():
    service = get_google_service('tasks', 'v1')
    if not service: return redirect(url_for('authorize'))
    app_settings = load_app_settings()
    task_list_id = app_settings.get('google_tasks_list_id')
    if not task_list_id:
        flash("กรุณาเลือก Google Tasks List ในหน้าตั้งค่าก่อน", "warning")
        return redirect(url_for('settings_page'))
    try:
        tasks_raw = service.tasks().list(tasklist=task_list_id, showCompleted=True, maxResults=100).execute().get('items', [])
    except HttpError as e:
        flash(f"เกิดข้อผิดพลาดในการดึงข้อมูลจาก Google Tasks: {e.reason}", "danger")
        return redirect(url_for('settings_page'))
    
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    current_time_utc = datetime.datetime.now(pytz.utc)
    final_tasks, stats = [], {'needsAction': 0, 'completed': 0, 'overdue': 0, 'total': len(tasks_raw)}

    for task in tasks_raw:
        task_status = task.get('status', 'needsAction')
        is_overdue = False
        if task_status == 'needsAction' and task.get('due'):
            try:
                due_dt_utc = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                if due_dt_utc < current_time_utc: is_overdue = True
            except (ValueError, TypeError): pass
        
        if task_status == 'completed': stats['completed'] += 1
        else:
            stats['needsAction'] += 1
            if is_overdue: stats['overdue'] += 1

        if (status_filter == 'all' or (status_filter == 'completed' and task_status == 'completed') or
            (status_filter == 'needsAction' and task_status == 'needsAction' and not is_overdue) or
            (status_filter == 'overdue' and is_overdue)):
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            searchable_text = f"{task.get('title', '')} {customer_info.get('name', '')} {customer_info.get('phone', '')}".lower()
            if not search_query or search_query in searchable_text:
                parsed_task = parse_google_task_dates(task)
                parsed_task['customer'], parsed_task['is_overdue'] = customer_info, is_overdue
                final_tasks.append(parsed_task)

    final_tasks.sort(key=lambda x: (x.get('status') != 'needsAction', x.get('due') is None, x.get('due', '')))
    return render_template("tasks_summary.html", tasks=final_tasks, summary=stats, search_query=search_query, status_filter=status_filter)

@app.route("/form", methods=['GET', 'POST'])
@google_login_required
def form_page():
    return "Create New Task Page"

@app.route('/task/<task_id>', methods=['GET', 'POST'])
@google_login_required
def task_details(task_id):
    return f"Details for task {task_id}"

@app.route('/delete_task/<task_id>', methods=['POST'])
@google_login_required
def delete_task(task_id):
    return redirect(url_for('dashboard'))

@app.route('/technician_report')
@google_login_required
def technician_report():
    return "Technician Report Page"
#</editor-fold>

#<editor-fold desc="OAuth 2.0 Routes">
@app.route('/auth_landing')
def auth_landing():
    return render_template('authorize.html')

@app.route('/authorize')
def authorize():
    flow = Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, scopes=SCOPES, redirect_uri=url_for('oauth2callback', _external=True))
    authorization_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true', prompt='consent')
    session['state'] = state
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    state = session.get('state')
    if not state or state != request.args.get('state'):
        return "Authorization failed: State mismatch.", 400
    flow = Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, scopes=SCOPES, state=state, redirect_uri=url_for('oauth2callback', _external=True))
    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception as e:
        flash(f"การยืนยันตัวตนล้มเหลว: {e}", "danger")
        return redirect(url_for('auth_landing'))
    credentials = flow.credentials
    with open(TOKEN_FILE, 'w') as token:
        token.write(credentials.to_json())
    flash("เชื่อมต่อกับบัญชี Google สำเร็จ!", "success")
    return redirect(url_for('settings_page'))

@app.route('/revoke')
@google_login_required
def revoke():
    if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE)
    save_app_settings({})
    flash("ยกเลิกการเชื่อมต่อกับบัญชี Google และล้างการตั้งค่าทั้งหมดเรียบร้อยแล้ว", "info")
    return redirect(url_for('auth_landing'))
#</editor-fold>

#<editor-fold desc="Data & Backup Management">
@app.route('/duplicates')
@google_login_required
def find_duplicates():
    return "Duplicate Management Page"

@app.route('/delete_duplicates', methods=['POST'])
@google_login_required
def delete_duplicates():
    return redirect(url_for('find_duplicates'))

@app.route('/backup_data')
@google_login_required
def backup_data():
    return "Backup Page"

@app.route('/import_settings', methods=['POST'])
@google_login_required
def import_settings():
    return redirect(url_for('settings_page'))

@app.route('/export_equipment_catalog')
@google_login_required
def export_equipment_catalog():
    return "Export Equipment Page"

@app.route('/import_equipment_catalog', methods=['POST'])
@google_login_required
def import_equipment_catalog():
    return redirect(url_for('settings_page'))

@app.route('/api/preview_tasks_import', methods=['POST'])
@google_login_required
def preview_tasks_import():
    return jsonify([])

@app.route('/api/import_tasks_from_backup', methods=['POST'])
@google_login_required
def import_tasks_from_backup():
    return jsonify({'status': 'success'})
#</editor-fold>

#<editor-fold desc="Settings Route">
@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    task_lists, app_settings = [], load_app_settings()
    if g.user_logged_in:
        service = get_google_service('tasks', 'v1')
        if service:
            try:
                task_lists = service.tasklists().list().execute().get('items', [])
            except Exception as e:
                flash(f"ไม่สามารถดึงรายการ Google Tasks ได้: {e}", "danger")

    if request.method == 'POST':
        current_settings = load_app_settings()
        current_settings['google_tasks_list_id'] = request.form.get('google_tasks_list_id')
        current_settings['shop_info'] = {'contact_phone': request.form.get('shop_contact_phone', ''), 'line_id': request.form.get('shop_line_id', '')}
        current_settings['technician_list'] = [name.strip() for name in request.form.get('technician_list', '').splitlines() if name.strip()]
        current_settings['line_recipients'] = {'admin_group_id': request.form.get('admin_group_id', ''), 'technician_group_id': request.form.get('technician_group_id', '')}
        current_settings['report_times'] = {'appointment_reminder_hour_thai': int(request.form.get('appointment_reminder_hour', 7)), 'customer_followup_hour_thai': int(request.form.get('customer_followup_hour', 9))}
        current_settings['sales_offers'] = {'post_feedback_offer_enabled': 'post_feedback_offer_enabled' in request.form, 'post_feedback_offer_message': request.form.get('post_feedback_offer_message', ''), 'report_promotion_enabled': 'report_promotion_enabled' in request.form, 'report_promotion_text': request.form.get('report_promotion_text', '')}
        current_settings['auto_backup'] = {'enabled': 'auto_backup_enabled' in request.form, 'hour_thai': int(request.form.get('auto_backup_hour', 2)), 'folder_id': app_settings.get('auto_backup', {}).get('folder_id')}

        if save_app_settings(current_settings):
            flash("บันทึกการตั้งค่าเรียบร้อยแล้ว", "success")
            run_scheduler()
        else:
            flash("เกิดข้อผิดพลาดในการบันทึกการตั้งค่า", "danger")
        return redirect(url_for('settings_page'))

    default_keys = {'shop_info': {}, 'technician_list': [], 'line_recipients': {}, 'report_times': {'appointment_reminder_hour_thai': 7, 'customer_followup_hour_thai': 9}, 'sales_offers': {}, 'auto_backup': {'enabled': False, 'hour_thai': 2}}
    for key, default_value in default_keys.items():
        app_settings.setdefault(key, default_value)

    return render_template('settings_page.html', task_lists=task_lists, settings=app_settings, selected_list_id=app_settings.get('google_tasks_list_id'))
#</editor-fold>

#<editor-fold desc="Scheduler & Auto Backup">
def scheduled_auto_backup_job():
    with app.app_context():
        app.logger.info("Running scheduled auto backup job...")
        # Placeholder for the actual backup logic
        
scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)

def run_scheduler():
    global scheduler
    if scheduler.running:
        scheduler.shutdown(wait=False)
    scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)
    app_settings = load_app_settings()
    auto_backup_settings = app_settings.get('auto_backup', {})
    if auto_backup_settings.get('enabled'):
        hour = auto_backup_settings.get('hour_thai', 2)
        scheduler.add_job(func=scheduled_auto_backup_job, trigger=CronTrigger(hour=hour, minute=5), id='auto_backup_job', name='Daily automatic backup', replace_existing=True)
        app.logger.info(f"Scheduled auto backup job to run daily at {hour:02d}:05.")
    if scheduler.get_jobs():
        scheduler.start()
        atexit.register(lambda: scheduler.shutdown())

with app.app_context():
    run_scheduler()
#</editor-fold>

if __name__ == '__main__':
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
