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
from datetime import timezone, date
import time
import tempfile
import uuid

from PIL import Image

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, render_template, redirect, url_for, abort, flash, jsonify, Response, session
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect
from cachetools import cached, TTLCache
from geopy.distance import geodesic

import qrcode
import base64

from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage,
    BubbleContainer, CarouselContainer, BoxComponent, TextComponent,
    ButtonComponent, SeparatorComponent, URIAction, PostbackAction, QuickReply, QuickReplyButton,
    ImageComponent, PostbackEvent
)

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

import pandas as pd
from dateutil.parser import parse as date_parse

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit

TEXT_SNIPPETS = {
    'task_details': [
        {'key': 'ล้างแอร์', 'value': 'ล้างทำความสะอาดเครื่องปรับอากาศ, ตรวจเช็คน้ำยา, วัดแรงดันไฟฟ้า และทำความสะอาดคอยล์ร้อน-เย็น'},
        {'key': 'ติดตั้งแอร์', 'value': 'ติดตั้งเครื่องปรับอากาศใหม่ ขนาด [ขนาด BTU] พร้อมเดินท่อน้ำยาและสายไฟ, ติดตั้งเบรกเกอร์'},
        {'key': 'ซ่อมตู้เย็น', 'value': 'ซ่อมตู้เย็น [ยี่ห้อ/รุ่น] อาการไม่เย็น, ตรวจสอบคอมเพรสเซอร์และน้ำยา'},
        {'key': 'ตรวจเช็ค', 'value': 'เข้าตรวจเช็คอาการเสียเบื้องต้นตามที่ลูกค้าแจ้ง'}
    ],
    'progress_reports': [
        {'key': 'ลูกค้าเลื่อนนัด', 'value': 'ลูกค้าขอเลื่อนนัดเป็นวันที่ [dd/mm/yyyy] เนื่องจากไม่สะดวก'},
        {'key': 'รออะไหล่', 'value': 'ตรวจสอบแล้วพบว่าต้องรออะไหล่ [ชื่ออะไหล่] จะแจ้งลูกค้าให้ทราบกำหนดการอีกครั้ง'},
        {'key': 'เข้าพื้นที่ไม่ได้', 'value': 'ไม่สามารถเข้าพื้นที่ได้เนื่องจาก [เหตุผล] ได้โทรแจ้งลูกค้าแล้ว'},
        {'key': 'เสร็จบางส่วน', 'value': 'ดำเนินการเสร็จสิ้นบางส่วน เหลือ [สิ่งที่ต้องทำต่อ] จะเข้ามาดำเนินการต่อในวันถัดไป'}
    ]
}

app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_development_only')
csrf = CSRFProtect(app)

UPLOAD_FOLDER = 'static/uploads'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'kmz', 'kml'}

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM')
LINE_LOGIN_CHANNEL_ID = os.environ.get('LINE_LOGIN_CHANNEL_ID')
GOOGLE_TASKS_LIST_ID = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')

SCOPES = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/drive']
THAILAND_TZ = pytz.timezone('Asia/Bangkok')
cache = TTLCache(maxsize=100, ttl=60)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)

SETTINGS_FILE = 'settings.json'
_DEFAULT_APP_SETTINGS_STORE = {
    'report_times': {
        'appointment_reminder_hour_thai': 7,
        'outstanding_report_hour_thai': 20,
        'customer_followup_hour_thai': 9
    },
    'line_recipients': {
        'admin_group_id': os.environ.get('LINE_ADMIN_GROUP_ID', ''),
        'technician_group_id': os.environ.get('LINE_TECHNICIAN_GROUP_ID', ''),
        'manager_user_id': ''
    },
    'equipment_catalog': [],
    'auto_backup': { 'enabled': False, 'hour_thai': 2, 'minute_thai': 0 },
    'shop_info': { 'contact_phone': '081-XXX-XXXX', 'line_id': '@ComphoneService' },
    'technician_list': [],
    'upload_settings': {
        'max_file_size_mb': 50
    }
}
app.jinja_env.filters['dateutil_parse'] = date_parse

#<editor-fold desc="Settings and Configuration Helpers">

def load_settings_from_file():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            app.logger.error(f"Error handling settings.json: {e}")
            if os.path.exists(SETTINGS_FILE) and os.path.getsize(SETTINGS_FILE) == 0:
                os.remove(SETTINGS_FILE)
                app.logger.warning(f"Empty settings.json deleted. Using default settings.")
    return None

def save_settings_to_file(settings_data):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f: json.dump(settings_data, f, ensure_ascii=False, indent=4)
        return True
    except IOError as e:
        app.logger.error(f"Error writing to settings.json: {e}")
        return False

def get_app_settings():
    app_settings = json.loads(json.dumps(_DEFAULT_APP_SETTINGS_STORE))
    loaded_settings = load_settings_from_file()
    
    if loaded_settings:
        for key, default_value in app_settings.items():
            if key in loaded_settings:
                if isinstance(default_value, dict) and isinstance(loaded_settings[key], dict):
                    app_settings[key].update(loaded_settings[key])
                else:
                    app_settings[key] = loaded_settings[key]
    else:
        save_settings_to_file(app_settings)
        
    equipment_catalog = app_settings.get('equipment_catalog', [])
    app_settings['common_equipment_items'] = sorted(list(set(item.get('item_name') for item in equipment_catalog if item.get('item_name'))))
    
    max_size = app_settings.get('upload_settings', {}).get('max_file_size_mb', 50)
    app.config['MAX_CONTENT_LENGTH'] = max_size * 1024 * 1024

    return app_settings

def save_app_settings(settings_data):
    current_settings = get_app_settings()
    
    for key, value in settings_data.items():
        if isinstance(value, dict) and key in current_settings and isinstance(current_settings[key], dict):
            current_settings[key].update(value)
        else:
            current_settings[key] = value
            
    return save_settings_to_file(current_settings)

def load_settings_from_drive_on_startup():
    settings_backup_folder_id = find_or_create_drive_folder("Settings_Backups", GOOGLE_DRIVE_FOLDER_ID)
    if not settings_backup_folder_id:
        app.logger.error("Could not find or create Settings_Backups folder. Skipping settings restore.")
        return False
        
    service = get_google_drive_service()
    if not service:
        app.logger.error("Could not get Drive service for settings restore.")
        return False

    try:
        query = f"name = 'settings_backup.json' and '{settings_backup_folder_id}' in parents and trashed = false"
        response = _execute_google_api_call_with_retry(service.files().list, q=query, spaces='drive', fields='files(id, name)', orderBy='modifiedTime desc', pageSize=1)
        files = response.get('files', [])

        if files:
            latest_backup_file_id = files[0]['id']
            app.logger.info(f"Found latest settings backup on Drive (ID: {latest_backup_file_id})")

            request = service.files().get_media(fileId=latest_backup_file_id)
            fh = BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
            fh.seek(0)

            downloaded_settings = json.loads(fh.read().decode('utf-8'))

            if save_settings_to_file(downloaded_settings):
                app.logger.info("Successfully restored settings from Google Drive backup.")
                return True
            else:
                app.logger.error("Failed to save restored settings to local file.")
                return False
        else:
            app.logger.info("No settings backup found on Google Drive for automatic restore.")
            return False
    except Exception as e:
        app.logger.error(f"An unexpected error occurred during settings restore from Drive: {e}")
        return False

#</editor-fold>

#<editor-fold desc="Google API Service Helpers">
def _execute_google_api_call_with_retry(api_call, *args, **kwargs):
    max_retries = 3
    base_delay = 1
    request_obj = api_call(*args, **kwargs)

    if not hasattr(request_obj, 'execute'):
        return request_obj

    for i in range(max_retries):
        try:
            return request_obj.execute()
        except HttpError as e:
            if e.resp.status in [500, 502, 503, 504, 429] and i < max_retries - 1:
                delay = base_delay * (2 ** i)
                app.logger.warning(f"Google API transient error (Status: {e.resp.status}). Retrying in {delay} seconds... (Attempt {i+1}/{max_retries})")
                time.sleep(delay)
            else:
                raise
        except Exception as e:
            app.logger.error(f"Unexpected error during Google API call execution: {e}")
            raise
    return None

def get_google_service(api_name, api_version):
    creds = None
    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')

    if google_token_json_str:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(google_token_json_str), SCOPES)
        except Exception as e:
            app.logger.warning(f"Could not load token from GOOGLE_TOKEN_JSON env var: {e}. Please check format.")
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            app.logger.info("="*80)
            app.logger.info("Google access token refreshed successfully!")
            app.logger.info("PLEASE UPDATE YOUR GOOGLE_TOKEN_JSON ENVIRONMENT VARIABLE ON RENDER.COM WITH THE FOLLOWING:")
            app.logger.info(f"NEW GOOGLE_TOKEN_JSON: {creds.to_json()}")
            app.logger.info("="*80)
        except Exception as e:
            app.logger.error(f"Error refreshing token: {e}")
            creds = None

    if creds and creds.valid:
        try:
            service = _execute_google_api_call_with_retry(build, serviceName=api_name, version=api_version, credentials=creds)
            return service
        except Exception as e:
            app.logger.error(f"Failed to build Google API service: {e}")
            return None
    else:
        app.logger.error("No valid Google credentials available. API service cannot be built.")
        return None

def get_google_tasks_service(): return get_google_service('tasks', 'v1')
def get_google_drive_service(): return get_google_service('drive', 'v3')

#</editor-fold>

#<editor-fold desc="Google Tasks and Drive Core Functions">

@cached(cache)
def find_or_create_drive_folder(name, parent_id):
    service = get_google_drive_service()
    if not service: return None
    
    query = f"name = '{name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    try:
        response = _execute_google_api_call_with_retry(service.files().list, q=query, spaces='drive', fields='files(id, name)', pageSize=1)
        files = response.get('files', [])
        if files:
            return files[0]['id']
        else:
            file_metadata = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
            folder = _execute_google_api_call_with_retry(service.files().create, body=file_metadata, fields='id')
            return folder.get('id')
    except HttpError as e:
        app.logger.error(f"Error finding or creating folder '{name}': {e}")
        return None

def _perform_drive_upload(media_body, file_name, folder_id):
    service = get_google_drive_service()
    if not service or not folder_id:
        app.logger.error(f"Drive service or Folder ID not configured for upload of '{file_name}'.")
        return None

    uploaded_file_id = None
    try:
        file_metadata = {'name': file_name, 'parents': [folder_id]}
        app.logger.info(f"Attempting to upload file '{file_name}' to Drive folder '{folder_id}'.")
        
        file_obj = _execute_google_api_call_with_retry(service.files().create, body=file_metadata, media_body=media_body, fields='id, webViewLink')

        if not file_obj or 'id' not in file_obj:
            app.logger.error(f"Drive upload failed for '{file_name}': File object or ID is missing.")
            return None

        uploaded_file_id = file_obj['id']
        app.logger.info(f"File '{file_name}' uploaded with ID: {uploaded_file_id}. Setting permissions.")

        permission_result = _execute_google_api_call_with_retry(service.permissions().create, fileId=uploaded_file_id, body={'role': 'reader', 'type': 'anyone'})
        
        if not permission_result or 'id' not in permission_result:
            app.logger.error(f"CRITICAL: Failed to set permissions for '{file_name}' (ID: {uploaded_file_id}). File will be inaccessible. Aborting and cleaning up.")
            try:
                _execute_google_api_call_with_retry(service.files().delete, fileId=uploaded_file_id)
                app.logger.info(f"Cleaned up file '{file_name}' (ID: {uploaded_file_id}) after permission failure.")
            except Exception as delete_error:
                app.logger.error(f"Could not clean up file '{uploaded_file_id}' after permission failure: {delete_error}")
            return None

        app.logger.info(f"Permissions set for '{file_name}' (ID: {uploaded_file_id}).")
        return file_obj

    except Exception as e:
        app.logger.error(f'Unexpected error during Drive upload for {file_name}: {e}', exc_info=True)
        if uploaded_file_id and service:
             app.logger.info(f"Attempting to clean up file {uploaded_file_id} after unexpected error.")
             try:
                _execute_google_api_call_with_retry(service.files().delete, fileId=uploaded_file_id)
                app.logger.info(f"Cleaned up file '{file_name}' (ID: {uploaded_file_id}) after unexpected error.")
             except Exception as cleanup_error:
                app.logger.error(f"Failed to cleanup file '{uploaded_file_id}' after error: {cleanup_error}")
        return None

def get_single_task(task_id):
    if not task_id: return None
    service = get_google_tasks_service()
    if not service: return None
    try:
        return _execute_google_api_call_with_retry(service.tasks().get, tasklist=GOOGLE_TASKS_LIST_ID, task=task_id)
    except HttpError as err:
        app.logger.error(f"Error getting single task {task_id}: {err}")
        return None

def update_google_task(task_id, **kwargs):
    service = get_google_tasks_service()
    if not service: return None
    try:
        task = get_single_task(task_id)
        if not task:
            app.logger.error(f"Task {task_id} not found for update.")
            return None
        
        task.update(kwargs)

        if kwargs.get('status') == 'completed':
            task['completed'] = datetime.datetime.now(pytz.utc).isoformat().replace('+00:00', 'Z')
            task['due'] = None
        elif 'status' in kwargs and kwargs['status'] != 'completed':
            task.pop('completed', None)

        return _execute_google_api_call_with_retry(service.tasks().update, tasklist=GOOGLE_TASKS_LIST_ID, task=task_id, body=task)
    except HttpError as e:
        app.logger.error(f"Failed to update task {task_id}: {e}")
        return None
        
def delete_google_task(task_id):
    service = get_google_tasks_service()
    if not service: return False
    try:
        _execute_google_api_call_with_retry(service.tasks().delete, tasklist=GOOGLE_TASKS_LIST_ID, task=task_id)
        return True
    except HttpError as err:
        app.logger.error(f"API Error deleting task {task_id}: {err}")
        return False
        
def create_google_task(title, notes=None, due=None):
    service = get_google_tasks_service()
    if not service: return None
    try:
        task_body = {'title': title, 'notes': notes, 'status': 'needsAction'}
        if due: task_body['due'] = due
        return _execute_google_api_call_with_retry(service.tasks().insert, tasklist=GOOGLE_TASKS_LIST_ID, body=task_body)
    except HttpError as e:
        app.logger.error(f"Error creating Google Task: {e}")
        return None

@cached(cache)
def get_google_tasks_for_report(show_completed=True):
    service = get_google_tasks_service()
    if not service: return None
    try:
        results = _execute_google_api_call_with_retry(service.tasks().list, tasklist=GOOGLE_TASKS_LIST_ID, showCompleted=show_completed, maxResults=100)
        return results.get('items', [])
    except HttpError as err:
        app.logger.error(f"API Error getting tasks: {err}")
        return None

#</editor-fold>

#<editor-fold desc="Note Parsing and Formatting Helpers">
def parse_customer_info_from_notes(notes):
    info = {'name': '', 'phone': '', 'address': '', 'map_url': None, 'organization': ''}
    if not notes: return info
    org_match = re.search(r"หน่วยงาน:\s*(.*)", notes, re.IGNORECASE)
    name_match = re.search(r"ลูกค้า:\s*(.*)", notes, re.IGNORECASE)
    phone_match = re.search(r"เบอร์โทรศัพท์:\s*(.*)", notes, re.IGNORECASE)
    address_match = re.search(r"ที่อยู่:\s*(.*)", notes, re.IGNORECASE)
    map_url_match = re.search(r"(https?:\/\/[^\s]+|(?:\-?\d+\.\d+,\s*\-?\d+\.\d+))", notes)
    if org_match: info['organization'] = org_match.group(1).strip()
    if name_match: info['name'] = name_match.group(1).strip()
    if phone_match: info['phone'] = phone_match.group(1).strip()
    if address_match: info['address'] = address_match.group(1).strip()
    if map_url_match:
        coords_or_url = map_url_match.group(1).strip()
        info['map_url'] = f"https://www.google.com/maps?q={coords_or_url}" if re.match(r"^\-?\d+\.\d+,\s*\-?\d+\.\d+$", coords_or_url) else coords_or_url
    return info

def parse_tech_report_from_notes(notes):
    if not notes: return [], ""
    report_blocks = re.findall(r"--- TECH_REPORT_START ---\s*\n(.*?)\n--- TECH_REPORT_END ---", notes, re.DOTALL)
    history = []
    for json_str in report_blocks:
        try:
            report_data = json.loads(json_str)
            if isinstance(report_data.get('equipment_used'), str):
                report_data['equipment_used_display'] = report_data['equipment_used'].replace('\n', '<br>')
            else:
                report_data['equipment_used_display'] = _format_equipment_list(report_data.get('equipment_used', []))
            history.append(report_data)
        except json.JSONDecodeError:
            app.logger.warning(f"Failed to decode tech report JSON: {json_str[:100]}...")
    
    base_notes = re.sub(r"--- (TECH_REPORT|CUSTOMER_FEEDBACK)_(START|END) ---.*?(--- (TECH_REPORT|CUSTOMER_FEEDBACK)_(START|END) ---)?", "", notes, flags=re.DOTALL).strip()
    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, base_notes

def parse_customer_feedback_from_notes(notes):
    feedback_data = {}
    if not notes: return feedback_data
    feedback_match = re.search(r"--- CUSTOMER_FEEDBACK_START ---\s*\n(.*?)\n--- CUSTOMER_FEEDBACK_END ---", notes, re.DOTALL)
    if feedback_match:
        try:
            feedback_data = json.loads(feedback_match.group(1))
        except json.JSONDecodeError:
            app.logger.warning("Failed to decode customer feedback JSON from notes.")
    return feedback_data

def build_final_notes(base_notes, history_reports, feedback_data):
    """Constructs the final notes string for a Google Task."""
    final_notes = base_notes.strip()
    if history_reports:
        history_reports.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
        tech_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history_reports])
        final_notes += tech_reports_text
    if feedback_data:
        final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
    return final_notes

def parse_google_task_dates(task_item):
    parsed = task_item.copy()
    for key in ['created', 'due', 'completed']:
        if parsed.get(key):
            try:
                dt_utc = date_parse(parsed[key])
                parsed[f'{key}_formatted'] = dt_utc.astimezone(THAILAND_TZ).strftime("%d/%m/%y %H:%M")
                if key == 'due':
                    parsed['due_for_input'] = dt_utc.astimezone(THAILAND_TZ).strftime("%Y-%m-%dT%H:%M")
            except (ValueError, TypeError):
                parsed[f'{key}_formatted'], parsed['due_for_input'] = '', ''
        else:
            parsed[f'{key}_formatted'], parsed['due_for_input'] = '', ''
    return parsed
    
#</editor-fold>

#<editor-fold desc="General Utility Functions">
def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name).strip() if name else "Unnamed"

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def _parse_equipment_string(text_input):
    if not text_input: return []
    return [
        {"item": parts[0].strip(), "quantity": float(parts[1].strip()) if len(parts) > 1 and parts[1].strip().replace('.', '', 1).isdigit() else (parts[1].strip() if len(parts) > 1 else 1)}
        for line in text_input.strip().split('\n') if line.strip() and (parts := line.split(',', 1))
    ]

def _format_equipment_list(equipment_data):
    if not equipment_data or not isinstance(equipment_data, list): return 'N/A'
    lines = [f"{item['item']} (x{item['quantity']:g})" if isinstance(item.get('quantity'), (int, float)) else f"{item['item']} ({item.get('quantity', '-')})" for item in equipment_data if isinstance(item, dict) and 'item' in item]
    return "<br>".join(lines) or 'N/A'

@app.context_processor
def inject_global_vars():
    return {'now': datetime.datetime.now(THAILAND_TZ), 'google_api_connected': check_google_api_status()}
#</editor-fold>

#<editor-fold desc="Background Scheduler and Jobs">
def run_scheduler():
    global scheduler
    settings = get_app_settings()
    if scheduler.running:
        scheduler.shutdown(wait=False)
    scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)
    
    ab = settings.get('auto_backup', {})
    if ab.get('enabled'):
        scheduler.add_job(scheduled_backup_job, CronTrigger(hour=ab.get('hour_thai', 2), minute=ab.get('minute_thai', 0)), id='auto_system_backup', replace_existing=True)
    
    rt = settings.get('report_times', {})
    scheduler.add_job(scheduled_appointment_reminder_job, CronTrigger(hour=rt.get('appointment_reminder_hour_thai', 7), minute=0), id='daily_appointment_reminder', replace_existing=True)
    scheduler.add_job(scheduled_customer_follow_up_job, CronTrigger(hour=rt.get('customer_followup_hour_thai', 9), minute=5), id='daily_customer_followup', replace_existing=True)
    
    scheduler.start()
    app.logger.info("APScheduler started/reconfigured.")

def scheduled_backup_job():
    # ... (This function is complete in your original file) ...
    pass

def scheduled_appointment_reminder_job():
    # ... (This function is complete in your original file) ...
    pass

def _create_customer_follow_up_flex_message(task_id, task_title, customer_name):
    # ... (This function is complete in your original file) ...
    pass

def scheduled_customer_follow_up_job():
    # ... (This function is complete in your original file) ...
    pass

def check_google_api_status():
    # ... (This function is complete in your original file) ...
    pass

def send_new_task_notification(task):
    # ... (This function is complete in your original file) ...
    pass

def send_completion_notification(task, technicians):
    # ... (This function is complete in your original file) ...
    pass

def send_update_notification(task, new_due_date_str, reason, technicians, is_today):
    # ... (This function is complete in your original file) ...
    pass
#</editor-fold>

# --- App Initialization and Error Handlers ---
with app.app_context():
    load_settings_from_drive_on_startup()
    run_scheduler()

atexit.register(lambda: scheduler.shutdown() if scheduler.running else None)

@app.errorhandler(404)
def page_not_found(e): return render_template('404.html'), 404
@app.errorhandler(500)
def internal_server_error(e):
    app.logger.error(f"Server Error: {e}", exc_info=True)
    return render_template('500.html'), 500

# --- Core Flask Routes ---
@app.route("/")
def root_redirect(): return redirect(url_for('summary'))

@app.route("/form", methods=['GET', 'POST'])
def form_page():
    if request.method == 'POST':
        task_title = str(request.form.get('task_title', '')).strip()
        customer_name = str(request.form.get('customer', '')).strip()
        organization_name = str(request.form.get('organization_name', '')).strip()

        if not task_title or not customer_name:
            flash('กรุณากรอกชื่อผู้ติดต่อและรายละเอียดงาน', 'danger')
            return redirect(url_for('form_page'))

        notes_lines = [
            f"หน่วยงาน: {organization_name}" if organization_name else None,
            f"ลูกค้า: {customer_name}",
            f"เบอร์โทรศัพท์: {str(request.form.get('phone', '')).strip()}",
            f"ที่อยู่: {str(request.form.get('address', '')).strip()}",
            str(request.form.get('latitude_longitude', '')).strip() or None
        ]
        notes = "\n".join(filter(None, notes_lines))

        due_date_gmt = None
        appointment_str = str(request.form.get('appointment', '')).strip()
        if appointment_str:
            try:
                dt_local = THAILAND_TZ.localize(date_parse(appointment_str))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
            except ValueError:
                flash('รูปแบบวันเวลานัดหมายไม่ถูกต้อง', 'warning')
                return render_template('form.html', form_data=request.form)

        new_task = create_google_task(task_title, notes=notes, due=due_date_gmt)
        if new_task:
            cache.clear()
            send_new_task_notification(new_task)
            
            uploaded_attachments = json.loads(request.form.get('uploaded_attachments_json', '[]'))

            if uploaded_attachments:
                # ... (Logic to move attachments and create initial report remains the same)
                pass

            flash('สร้างงานใหม่เรียบร้อยแล้ว!', 'success')
            return redirect(url_for('task_details', task_id=new_task['id']))
        else:
            flash('เกิดข้อผิดพลาดในการสร้างงาน', 'danger')
            return render_template('form.html', form_data=request.form)

    return render_template('form.html', task_detail_snippets=TEXT_SNIPPETS.get('task_details', []))


@app.route('/summary')
def summary():
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    today_thai = datetime.datetime.now(THAILAND_TZ).date()
    final_tasks = []
    stats = {'needsAction': 0, 'completed': 0, 'overdue': 0, 'total': len(tasks_raw), 'today': 0}

    for task in tasks_raw:
        task_status = task.get('status', 'needsAction')
        is_overdue = False
        is_today = False
        if task_status == 'needsAction' and task.get('due'):
            try:
                due_dt_utc = date_parse(task['due'])
                due_dt_local = due_dt_utc.astimezone(THAILAND_TZ)
                if due_dt_local.date() < today_thai:
                    is_overdue = True
                elif due_dt_local.date() == today_thai:
                    is_today = True
            except (ValueError, TypeError):
                pass
        
        if task_status == 'completed':
            stats['completed'] += 1
        else:
            stats['needsAction'] += 1
            if is_overdue:
                stats['overdue'] += 1
            if is_today:
                stats['today'] += 1

        task_passes_filter = (status_filter == 'all' or
                              (status_filter == 'completed' and task_status == 'completed') or
                              (status_filter == 'needsAction' and task_status == 'needsAction') or
                              (status_filter == 'today' and is_today))
        
        if task_passes_filter:
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            searchable_text = f"{task.get('title', '')} {customer_info.get('name', '')} {customer_info.get('organization', '')} {customer_info.get('phone', '')}".lower()

            if not search_query or search_query in searchable_text:
                parsed_task = parse_google_task_dates(task)
                parsed_task['customer'] = customer_info
                parsed_task['is_overdue'] = is_overdue
                parsed_task['is_today'] = is_today
                final_tasks.append(parsed_task)

    final_tasks.sort(key=lambda x: (x.get('status') == 'completed', x.get('due') is None, date_parse(x.get('due', '9999-12-31T23:59:59Z'))))
    
    completed_tasks_for_chart = [t for t in tasks_raw if t.get('status') == 'completed' and t.get('completed')]
    month_labels = []
    chart_values = []
    for i in range(12):
        target_d = datetime.datetime.now(THAILAND_TZ) - datetime.timedelta(days=30 * (11 - i))
        month_key = target_d.strftime('%Y-%m')
        month_labels.append(target_d.strftime('%b %y'))
        count = sum(1 for task in completed_tasks_for_chart if date_parse(task['completed']).astimezone(THAILAND_TZ).strftime('%Y-%m') == month_key)
        chart_values.append(count)
    chart_data = {'labels': month_labels, 'values': chart_values}

    return render_template("dashboard.html",
                           tasks=final_tasks, summary=stats,
                           search_query=search_query, status_filter=status_filter,
                           chart_data=chart_data)

# ... (ALL OTHER ROUTES FROM YOUR ORIGINAL FILE MUST BE PLACED HERE) ...
# For example:
# @app.route('/summary/print')
# def summary_print():
#     ... (full function)
#
# @app.route('/calendar')
# def calendar_view():
#     ... (full function)
#
# ... and so on for all remaining routes and handlers.

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=True)