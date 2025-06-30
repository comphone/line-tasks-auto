import os
import sys
import datetime
import re
import json
import pytz
import mimetypes 
import zipfile
from io import BytesIO

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, render_template, redirect, url_for, abort, send_from_directory, flash, jsonify, Response 
from werkzeug.utils import secure_filename
from cachetools import cached, TTLCache
from geopy.distance import geodesic

import qrcode
import base64

# --- Use line-bot-sdk version 2.4.2 ---
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
    ImageComponent
)
# ---------------------------------------------

from google.oauth2.credentials import Credentials 
from google.auth.transport.requests import Request 
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload 

import pandas as pd 

# --- Initialization & Configurations ---
app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_dev')
UPLOAD_FOLDER = 'static/uploads' 
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'} # Allowed extensions for logo

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# --- LINE & Google Configs ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    sys.exit("LINE Bot credentials are not set in environment variables.")

LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM') 
LINE_ADMIN_GROUP_ID = os.environ.get('LINE_ADMIN_GROUP_ID')
GOOGLE_TASKS_LIST_ID = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID') 

if not GOOGLE_DRIVE_FOLDER_ID:
    app.logger.warning("GOOGLE_DRIVE_FOLDER_ID environment variable is not set. Drive upload will not work.")

SCOPES = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/drive']
THAILAND_TZ = pytz.timezone('Asia/Bangkok')
cache = TTLCache(maxsize=100, ttl=60)

# Initialize LINE Bot SDK
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- Settings Management ---
SETTINGS_FILE = 'settings.json'
_DEFAULT_APP_SETTINGS_STORE = {
    'report_times': { 'appointment_reminder_hour_thai': 7, 'outstanding_report_hour_thai': 20 },
    'line_recipients': { 'admin_group_id': os.environ.get('LINE_ADMIN_GROUP_ID', ''), 'technician_group_id': os.environ.get('LINE_TECHNICIAN_GROUP_ID', '') },
    'qrcode_settings': { 'box_size': 8, 'border': 4, 'fill_color': '#28a745', 'back_color': '#FFFFFF', 'custom_url': '' },
    'equipment_catalog': []
}
_APP_SETTINGS_STORE = {}

#<editor-fold desc="Helper and Utility Functions">
def load_settings_from_file():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
        except (json.JSONDecodeError, IOError) as e: app.logger.error(f"Error handling settings.json: {e}")
    return None

def save_settings_to_file(settings_data):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f: json.dump(settings_data, f, ensure_ascii=False, indent=4)
        return True
    except IOError as e:
        app.logger.error(f"Error writing to settings.json: {e}")
        return False

def get_app_settings():
    global _APP_SETTINGS_STORE
    if not _APP_SETTINGS_STORE:
        loaded = load_settings_from_file()
        _APP_SETTINGS_STORE = json.loads(json.dumps(_DEFAULT_APP_SETTINGS_STORE))
        if loaded:
            for key, default_value in _APP_SETTINGS_STORE.items():
                if isinstance(default_value, dict): _APP_SETTINGS_STORE[key].update(loaded.get(key, {}))
                elif key in loaded: _APP_SETTINGS_STORE[key] = loaded[key]
        else:
            save_settings_to_file(_APP_SETTINGS_STORE)
    
    equipment_catalog = _APP_SETTINGS_STORE.get('equipment_catalog', [])
    _APP_SETTINGS_STORE['common_equipment_items'] = sorted(list(set(item.get('item_name') for item in equipment_catalog if item.get('item_name'))))
    return _APP_SETTINGS_STORE

def save_app_settings(settings_data):
    global _APP_SETTINGS_STORE
    current_settings = get_app_settings()
    for key, value in settings_data.items():
        if isinstance(value, dict) and key in current_settings: current_settings[key].update(value)
        else: current_settings[key] = value
    _APP_SETTINGS_STORE = current_settings
    return save_settings_to_file(_APP_SETTINGS_STORE)

_APP_SETTINGS_STORE = get_app_settings()

def get_google_service(api_name, api_version):
    creds = None
    token_path = 'token.json'
    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')
    if google_token_json_str:
        try: creds = Credentials.from_authorized_user_info(json.loads(google_token_json_str), SCOPES)
        except Exception as e: app.logger.warning(f"Could not load token from env var: {e}")
    elif os.path.exists(token_path):
        creds = Credentials.from_authorized_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try: creds.refresh(Request())
            except Exception as e:
                app.logger.error(f"Error refreshing token: {e}")
                creds = None
        if not creds and os.path.exists('credentials.json'):
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_console()
        if creds:
            with open(token_path, 'w') as token: token.write(creds.to_json())
            app.logger.info(f"Token saved to {token_path}. Please update GOOGLE_TOKEN_JSON on Render.")
    return build(api_name, api_version, credentials=creds) if creds else None

def get_google_tasks_service(): return get_google_service('tasks', 'v1')
def get_google_drive_service(): return get_google_service('drive', 'v3')

@cached(cache)
def get_google_tasks_for_report(show_completed=True):
    app.logger.info(f"Fetching tasks (show_completed={show_completed})")
    service = get_google_tasks_service()
    if not service: return None
    try:
        results = service.tasks().list(tasklist=GOOGLE_TASKS_LIST_ID, showCompleted=show_completed, maxResults=100).execute()
        return results.get('items', [])
    except HttpError as err:
        app.logger.error(f"API Error getting tasks: {err}")
        return None

def get_single_task(task_id):
    service = get_google_tasks_service()
    if not service: return None
    try:
        return service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
    except HttpError as err:
        app.logger.error(f"Error getting single task {task_id}: {err}")
        return None
        
def upload_file_to_google_drive(file_path, file_name, mime_type):
    service, folder_id = get_google_drive_service(), GOOGLE_DRIVE_FOLDER_ID
    if not service or not folder_id:
        app.logger.error("Drive service or folder ID is not configured.")
        return None
    try:
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        file_obj = service.files().create(body={'name': file_name, 'parents': [folder_id]}, media_body=media, fields='id, webViewLink').execute()
        service.permissions().create(fileId=file_obj['id'], body={'role': 'reader', 'type': 'anyone'}).execute()
        app.logger.info(f"Uploaded to Drive: {file_obj.get('webViewLink')}")
        return file_obj.get('webViewLink')
    except HttpError as e:
        app.logger.error(f'Drive upload error: {e}')
        return None

def create_google_task(title, notes=None, due=None):
    service = get_google_tasks_service()
    if not service: return None
    try:
        task_body = {'title': title, 'notes': notes, 'status': 'needsAction'}
        if due: task_body['due'] = due
        return service.tasks().insert(tasklist=GOOGLE_TASKS_LIST_ID, body=task_body).execute()
    except HttpError as e:
        app.logger.error(f"Error creating Google Task: {e}")
        return None
        
def delete_google_task(task_id):
    service = get_google_tasks_service()
    if not service: return False
    try:
        service.tasks().delete(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
        return True
    except HttpError as err:
        app.logger.error(f"API Error deleting task {task_id}: {err}")
        return False

def update_google_task(task_id, title=None, notes=None, status=None, due=None):
    service = get_google_tasks_service()
    if not service: return None
    try:
        task = service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
        if title is not None: task['title'] = title
        if notes is not None: task['notes'] = notes
        if status is not None:
            task['status'] = status
        
        if status == 'completed':
            task['completed'] = datetime.datetime.now(pytz.utc).isoformat()
            task.pop('due', None)
        else:
            task.pop('completed', None)
            if due: task['due'] = due
                
        return service.tasks().update(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id, body=task).execute()
    except HttpError as e:
        app.logger.error(f"Failed to update task {task_id}: {e}")
        return None

def parse_customer_info_from_notes(notes):
    info = {'name': '', 'phone': '', 'address': '', 'map_url': None}
    if not notes: return info
    
    info['name'] = (re.search(r"ลูกค้า:\s*(.*)", notes, re.IGNORECASE) or re.search(r"customer:\s*(.*)", notes, re.IGNORECASE)).group(1).strip() if (re.search(r"ลูกค้า:", notes) or re.search(r"customer:", notes)) else ''
    info['phone'] = (re.search(r"เบอร์โทรศัพท์:\s*(.*)", notes, re.IGNORECASE) or re.search(r"phone:\s*(.*)", notes, re.IGNORECASE)).group(1).strip() if (re.search(r"เบอร์โทรศัพท์:", notes) or re.search(r"phone:", notes)) else ''
    info['address'] = (re.search(r"ที่อยู่:\s*(.*)", notes, re.IGNORECASE) or re.search(r"address:\s*(.*)", notes, re.IGNORECASE)).group(1).strip() if (re.search(r"ที่อยู่:", notes) or re.search(r"address:", notes)) else ''
    
    map_url_match = re.search(r"(https?://(?:www\.)?google\.com/maps.*)", notes)
    if map_url_match:
        info['map_url'] = map_url_match.group(1).strip()
        
    if not any(info.values()):
        base_content = re.sub(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL).strip()
        lines = [line.strip() for line in base_content.split('\n') if line.strip()]
        if lines: info['name'] = lines.pop(0)
        if lines: info['phone'] = lines.pop(0)
        if lines: info['address'] = lines.pop(0)
        if lines and re.match(r"https?://(?:www\.)?google\.com/maps.*", lines[0]): info['map_url'] = lines.pop(0)

    return info

def parse_google_task_dates(task_item):
    parsed = task_item.copy()
    for key in ['created', 'due', 'completed']:
        if parsed.get(key):
            try:
                dt_utc = datetime.datetime.fromisoformat(parsed[key].replace('Z', '+00:00'))
                parsed[f'{key}_formatted'] = dt_utc.astimezone(THAILAND_TZ).strftime("%d/%m/%y %H:%M")
                if key == 'due':
                    parsed['due_for_input'] = dt_utc.astimezone(THAILAND_TZ).strftime("%Y-%m-%dT%H:%M")
            except (ValueError, TypeError): 
                parsed[f'{key}_formatted'] = ''
                if key == 'due': parsed['due_for_input'] = ''
        else: 
            parsed[f'{key}_formatted'] = ''
            if key == 'due': parsed['due_for_input'] = ''
    return parsed

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
        except json.JSONDecodeError: pass
    
    original_notes_text = re.sub(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL).strip()
    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, original_notes_text
    
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def _parse_equipment_string(text_input):
    equipment_list = []
    if not text_input: return equipment_list
    for line in text_input.strip().split('\n'):
        if not line.strip(): continue
        parts = line.split(',', 1)
        item_name = parts[0].strip()
        if item_name:
            equipment_list.append({"item": item_name, "quantity": parts[1].strip() if len(parts) > 1 else ''})
    return equipment_list

def _format_equipment_list(equipment_data):
    if not equipment_data: return 'N/A'
    if isinstance(equipment_data, str): return equipment_data
    lines = []
    if isinstance(equipment_data, list):
        for item in equipment_data:
            if isinstance(item, dict) and "item" in item:
                line = item['item']
                if item.get("quantity"): line += f", {item['quantity']}"
                lines.append(line)
            elif isinstance(item, str): lines.append(item)
    return "\n".join(lines) if lines else 'N/A'

@app.context_processor
def inject_now():
    return {'now': datetime.datetime.now(THAILAND_TZ)}
#</editor-fold>

@app.route("/")
def root_redirect():
    return redirect(url_for('summary'))
    
@app.route("/form", methods=['GET', 'POST'])
def form_page():
    if request.method == 'POST':
        task_title = str(request.form.get('task_title', '')).strip()
        customer_name = str(request.form.get('customer', '')).strip()

        if not task_title or not customer_name:
            flash('กรุณากรอกชื่อลูกค้าและรายละเอียดงาน', 'danger')
            return redirect(url_for('form_page'))
        
        customer_phone = str(request.form.get('phone', '')).strip()
        address = str(request.form.get('address', '')).strip()
        appointment_str = str(request.form.get('appointment', '')).strip()
        map_url_from_form = str(request.form.get('latitude_longitude', '')).strip()
        
        notes_lines = [
            f"ลูกค้า: {customer_name}",
            f"เบอร์โทรศัพท์: {customer_phone}",
            f"ที่อยู่: {address}",
        ]
        if map_url_from_form: notes_lines.append(map_url_from_form)
        notes = "\n".join(filter(None, notes_lines))
        
        due_date_gmt = None
        if appointment_str:
            try:
                dt_local = THAILAND_TZ.localize(datetime.datetime.strptime(appointment_str, "%Y-%m-%d %H:%M"))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat()
            except ValueError: app.logger.error(f"Invalid appointment format: {appointment_str}")

        created_task = create_google_task(task_title, notes=notes, due=due_date_gmt)
        
        if created_task:
            cache.clear()
            # LINE notification logic would go here if needed
            return redirect(url_for('summary'))
        else:
            flash('เกิดข้อผิดพลาดในการสร้างงาน', 'danger')
    return render_template('form.html')

@app.route('/summary')
def summary():
    service = get_google_tasks_service()
    if not service:
        flash('ไม่สามารถเชื่อมต่อกับ Google Service ได้ กรุณาตรวจสอบการตั้งค่า', 'danger')
        return render_template("tasks_summary.html", tasks=[], summary={})

    try:
        tasks_raw = get_google_tasks_for_report(show_completed=True)
        if tasks_raw is None: tasks_raw = []
    except HttpError as e:
        flash(f'เกิดข้อผิดพลาดในการดึงข้อมูลจาก Google Tasks: {e}', 'danger')
        tasks_raw = []

    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    
    current_time_utc = datetime.datetime.now(pytz.utc)
    final_filtered_tasks = []
    total_summary_stats = {'needsAction': 0, 'completed': 0, 'overdue': 0, 'total': len(tasks_raw)}

    for task in tasks_raw:
        task_status = task.get('status', 'needsAction')
        is_overdue = False
        if task_status == 'needsAction' and task.get('due'):
            try:
                due_dt_utc = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                if due_dt_utc < current_time_utc: is_overdue = True
            except (ValueError, TypeError): pass
        
        if task_status == 'completed': 
            total_summary_stats['completed'] += 1
        elif task_status == 'needsAction':
            total_summary_stats['needsAction'] += 1
            if is_overdue: 
                total_summary_stats['overdue'] += 1

        if (status_filter == 'all' or
            (status_filter == 'completed' and task_status == 'completed') or
            (status_filter == 'needsAction' and task_status == 'needsAction' and not is_overdue) or
            (status_filter == 'overdue' and is_overdue)):
            
            customer_info_for_search = parse_customer_info_from_notes(task.get('notes', ''))
            
            searchable_text = f"{task.get('title', '')} {customer_info_for_search.get('name', '')} {customer_info_for_search.get('phone', '')}".lower()
            
            if not search_query or search_query in searchable_text:
                parsed_task = parse_google_task_dates(task)
                parsed_task['customer'] = customer_info_for_search
                parsed_task['is_overdue'] = is_overdue
                final_filtered_tasks.append(parsed_task)

    final_filtered_tasks.sort(key=lambda x: x.get('created', ''), reverse=True)
    
    return render_template(
        "tasks_summary.html", 
        tasks=final_filtered_tasks, 
        summary=total_summary_stats, 
        search_query=search_query, 
        status_filter=status_filter
    )


# --- CONSOLIDATED TASK MANAGEMENT ROUTE ---
@app.route('/task/<task_id>', methods=['GET', 'POST'])
def task_details(task_id):
    service = get_google_tasks_service()
    if not service: abort(503)
    
    try:
        task_raw = service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
    except HttpError:
        abort(404)

    if request.method == 'POST':
        new_title = str(request.form.get('task_title', '')).strip()
        customer_name = str(request.form.get('customer_name', '')).strip()
        customer_phone = str(request.form.get('customer_phone', '')).strip()
        address = str(request.form.get('address', '')).strip()
        map_url = str(request.form.get('latitude_longitude', '')).strip()
        status = request.form.get('status')
        appointment_str = str(request.form.get('appointment_due', '')).strip()
        
        work_summary = str(request.form.get('work_summary', '')).strip()
        equipment_used = request.form.get('equipment_used', '')
        files = request.files.getlist('files[]')
        new_attachments_uploaded = any(f and f.filename for f in files)

        if not new_title:
            flash('กรุณากรอกรายละเอียดงาน', 'danger')
            return redirect(url_for('task_details', task_id=task_id))

        new_notes_lines = [f"ลูกค้า: {customer_name}", f"เบอร์โทรศัพท์: {customer_phone}", f"ที่อยู่: {address}"]
        if map_url: new_notes_lines.append(map_url)
        new_base_notes = "\n".join(filter(None, new_notes_lines))

        history, _ = parse_tech_report_from_notes(task_raw.get('notes', ''))
        if work_summary or new_attachments_uploaded:
            new_attachment_urls = []
            if new_attachments_uploaded:
                for file in files:
                    if file and allowed_file(file.filename):
                        filename = secure_filename(file.filename)
                        temp_filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                        file.save(temp_filepath)
                        mime_type = file.mimetype or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                        drive_url = upload_file_to_google_drive(temp_filepath, filename, mime_type)
                        if drive_url: new_attachment_urls.append(drive_url)
                        os.remove(temp_filepath)
            
            new_tech_report_data = {
                'summary_date': datetime.datetime.now(THAILAND_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                'work_summary': work_summary,
                'equipment_used': _parse_equipment_string(equipment_used),
                'attachment_urls': new_attachment_urls 
            }
            history.append(new_tech_report_data)

        all_reports_text = ""
        for report in sorted(history, key=lambda x: x.get('summary_date', '')):
            all_reports_text += f"\n\n--- TECH_REPORT_START ---\n{json.dumps(report, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"
        
        final_notes = new_base_notes + all_reports_text
        
        due_date_gmt = None
        if appointment_str:
            try:
                dt_local = THAILAND_TZ.localize(datetime.datetime.strptime(appointment_str, "%Y-%m-%dT%H:%M"))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat()
            except ValueError: 
                app.logger.error(f"Invalid reschedule format: {appointment_str}")
        
        updated_task = update_google_task(
            task_id,
            title=new_title,
            notes=final_notes,
            status=status,
            due=due_date_gmt
        )

        if updated_task:
            cache.clear()
            flash('บันทึกการเปลี่ยนแปลงทั้งหมดเรียบร้อยแล้ว!', 'success')
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกข้อมูล', 'danger')
        
        return redirect(url_for('task_details', task_id=task_id))

    # --- GET Request ---
    task = parse_google_task_dates(task_raw)
    task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
    task['tech_reports_history'], _ = parse_tech_report_from_notes(task.get('notes', ''))
    
    return render_template('update_task_details.html', task=task, common_equipment_items=get_app_settings().get('common_equipment_items', []))


@app.route('/delete_task/<task_id>', methods=['POST'])
def delete_task(task_id):
    if delete_google_task(task_id):
        flash('ลบงานเรียบร้อยแล้ว!', 'success')
        cache.clear()
    else:
        flash('เกิดข้อผิดพลาดในการลบงาน', 'danger')
    return redirect(url_for('summary'))


@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        # --- ADDED: Handle Logo Upload ---
        if 'logo_file' in request.files:
            logo_file = request.files['logo_file']
            if logo_file and logo_file.filename != '' and allowed_file(logo_file.filename):
                filename = 'logo.png' # Always overwrite with the same name
                filepath = os.path.join(app.root_path, 'static', filename)
                try:
                    logo_file.save(filepath)
                    flash('อัปเดตโลโก้เรียบร้อยแล้ว!', 'success')
                except Exception as e:
                    app.logger.error(f"Could not save logo: {e}")
                    flash('เกิดข้อผิดพลาดในการบันทึกโลโก้', 'danger')
                return redirect(url_for('settings_page'))

        # Handle other settings save
        save_app_settings({
            'report_times': { 'appointment_reminder_hour_thai': int(request.form.get('appointment_reminder_hour')), 'outstanding_report_hour_thai': int(request.form.get('outstanding_report_hour')) },
            'line_recipients': { 'admin_group_id': request.form.get('admin_group_id', '').strip(), 'technician_group_id': request.form.get('technician_group_id', '').strip() },
            'qrcode_settings': { 'box_size': int(request.form.get('qr_box_size', 8)), 'border': int(request.form.get('qr_border', 4)), 'fill_color': request.form.get('qr_fill_color', '#28a745'), 'back_color': request.form.get('qr_back_color', '#FFFFFF'), 'custom_url': request.form.get('qr_custom_url', '').strip() }
        })
        flash('บันทึกการตั้งค่าเรียบร้อยแล้ว!', 'success')
        cache.clear()
        return redirect(url_for('settings_page'))
    
    current_settings = get_app_settings()
    general_summary_url = url_for('summary', _external=True)
    qr_url_to_use = current_settings.get('qrcode_settings', {}).get('custom_url', '') or general_summary_url
    qr_settings = current_settings.get('qrcode_settings', {})
    
    qr_code_base64_general = ''
    def generate_qr_code_base64(data, box_size=8, border=4, fill_color='black', back_color='white'):
        try:
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=box_size,
                border=border,
            )
            qr.add_data(data)
            qr.make(fit=True)
            img = qr.make_image(fill_color=fill_color, back_color=back_color)
            buffered = BytesIO()
            img.save(buffered, format="PNG")
            return "data:image/png;base64," + base64.b64encode(buffered.getvalue()).decode("utf-8")
        except Exception as e:
            app.logger.error(f"Error generating QR code: {e}")
            return "" # Return empty string on error

    if 'generate_qr_code_base64' not in globals():
        globals()['generate_qr_code_base64'] = generate_qr_code_base64

    qr_code_base64_general = generate_qr_code_base64(
        qr_url_to_use, 
        box_size=qr_settings.get('box_size', 8), border=qr_settings.get('border', 4),
        fill_color=qr_settings.get('fill_color', '#28a745'), back_color=qr_settings.get('back_color', '#FFFFFF')
    )
    return render_template('settings_page.html', settings=current_settings, qr_code_base64_general=qr_code_base64_general, general_summary_url=general_summary_url)

@app.route('/test_notification', methods=['POST'])
def test_notification():
    settings = get_app_settings()
    recipient_id = settings.get('line_recipients', {}).get('admin_group_id', '')
    if not recipient_id:
        flash('กรุณากำหนด "LINE Admin Group ID" ในการตั้งค่าก่อน', 'danger')
        return redirect(url_for('settings_page'))
    try:
        test_message = TextSendMessage(text="[ทดสอบการแจ้งเตือน]\nสวัสดี! นี่คือข้อความทดสอบจากระบบจัดการงานของคุณ")
        line_bot_api.push_message(recipient_id, test_message)
        flash(f'ส่งข้อความทดสอบไปที่ ID: {recipient_id} เรียบร้อยแล้ว!', 'success')
    except Exception as e:
        app.logger.error(f"Failed to send test notification: {e}")
        flash(f'เกิดข้อผิดพลาดในการส่งข้อความทดสอบ: {e}', 'danger')

    return redirect(url_for('settings_page'))

@app.route('/backup_data')
def backup_data():
    try:
        all_tasks = get_google_tasks_for_report(show_completed=True)
        all_settings = get_app_settings()
        drive_service = get_google_drive_service()
        if all_tasks is None or all_settings is None or drive_service is None:
            flash('ไม่สามารถเชื่อมต่อบริการที่จำเป็น (Tasks, Drive) ได้', 'danger')
            return redirect(url_for('settings_page'))
        memory_file = BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.DEFLATED) as zf:
            zf.writestr('data/tasks_backup.json', json.dumps(all_tasks, indent=4, ensure_ascii=False))
            zf.writestr('data/settings_backup.json', json.dumps(all_settings, indent=4, ensure_ascii=False))
            # Include source code in backup
            project_root = os.path.dirname(os.path.abspath(__file__))
            for folder, _, files in os.walk(project_root):
                for file in files:
                    if file.endswith(('.py', '.html', '.css', '.js', '.json', '.env', 'Procfile', 'requirements.txt')):
                        file_path = os.path.join(folder, file)
                        archive_name = os.path.relpath(file_path, project_root)
                        zf.write(file_path, arcname=f'code/{archive_name}')
        memory_file.seek(0)
        backup_filename = f"full_system_backup_{datetime.date.today()}.zip"
        return Response(memory_file, mimetype='application/zip', headers={'Content-Disposition': f'attachment;filename={backup_filename}'})
    except Exception as e:
        app.logger.error(f"Error creating full system backup file: {e}")
        flash('เกิดข้อผิดพลาดในการสร้างไฟล์สำรองข้อมูล', 'danger')
        return redirect(url_for('settings_page'))


@app.route('/export_equipment_catalog', methods=['GET'])
def export_equipment_catalog():
    try:
        equipment_catalog = get_app_settings().get('equipment_catalog', [])
        df = pd.DataFrame(equipment_catalog)
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Equipment_Catalog')
        output.seek(0)
        return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment;filename=equipment_catalog.xlsx"})
    except Exception as e:
        app.logger.error(f"Error exporting equipment catalog: {e}")
        flash('เกิดข้อผิดพลาดในการส่งออกแคตตาล็อกอุปกรณ์', 'danger')
        return redirect(url_for('settings_page'))

@app.route('/import_equipment_catalog', methods=['POST'])
def import_equipment_catalog():
    if 'excel_file' not in request.files or not request.files['excel_file'].filename:
        flash('กรุณาเลือกไฟล์ Excel', 'danger')
        return redirect(url_for('settings_page'))
    file = request.files['excel_file']
    if file and file.filename.endswith(('.xls', '.xlsx')):
        try:
            df = pd.read_excel(file.stream)
            # Basic validation for required columns
            required_cols = ['item_name', 'unit', 'price']
            if not all(col in df.columns for col in required_cols):
                flash(f'ไฟล์ Excel ต้องมีคอลัมน์: {", ".join(required_cols)}', 'danger')
                return redirect(url_for('settings_page'))
                
            new_catalog = df.to_dict('records')
            current_settings = get_app_settings()
            current_settings['equipment_catalog'] = new_catalog
            save_app_settings(current_settings)
            flash('นำเข้าแคตตาล็อกอุปกรณ์เรียบร้อยแล้ว!', 'success')
        except Exception as e:
            app.logger.error(f"Error importing Excel: {e}")
            flash(f"เกิดข้อผิดพลาดในการนำเข้าไฟล์: {e}", 'danger')
    else:
        flash('รองรับเฉพาะไฟล์ Excel (.xls, .xlsx) เท่านั้น', 'danger')
    return redirect(url_for('settings_page'))

# --- LINE Bot Handlers ---
def create_task_list_message(title, tasks, limit=None):
    if not tasks:
        return TextSendMessage(text=f"ไม่พบรายการ{title}ในขณะนี้")
    
    message = f"📋 **{title}**\n\n"
    
    tasks.sort(key=lambda x: (x.get('due') is None, x.get('due', '')))

    if limit and len(tasks) > limit:
        tasks_to_show = tasks[:limit]
    else:
        tasks_to_show = tasks

    for i, task in enumerate(tasks_to_show):
        customer_info = parse_customer_info_from_notes(task.get('notes', ''))
        customer_name = customer_info.get('name', 'N/A')
        due_date = parse_google_task_dates(task).get('due_formatted', 'ไม่มีกำหนด')
        message += f"{i+1}. {task.get('title')}\n"
        message += f"   - ลูกค้า: {customer_name}\n"
        message += f"   - นัดหมาย: {due_date}\n\n"
    
    if limit and len(tasks) > limit:
        message += f"... และอีก {len(tasks) - limit} รายการ"

    return TextSendMessage(text=message)

def handle_outstanding_tasks_command(event):
    tasks_raw = get_google_tasks_for_report(show_completed=False) or []
    outstanding_tasks = [task for task in tasks_raw if task.get('status') == 'needsAction']
    reply_message = create_task_list_message("รายการงานที่ยังไม่เสร็จ", outstanding_tasks)
    line_bot_api.reply_message(event.reply_token, reply_message)

def handle_completed_tasks_command(event):
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    completed_tasks = [task for task in tasks_raw if task.get('status') == 'completed']
    completed_tasks.sort(key=lambda x: x.get('completed', ''), reverse=True)
    reply_message = create_task_list_message("งานที่เสร็จล่าสุด (5 รายการ)", completed_tasks, limit=5)
    line_bot_api.reply_message(event.reply_token, reply_message)

# --- NEW: Handle daily tasks command ---
def handle_daily_tasks_command(event, day_type):
    tasks_raw = get_google_tasks_for_report(show_completed=False) or []
    
    today = datetime.datetime.now(THAILAND_TZ).date()
    if day_type == 'today':
        target_date = today
        title = "งานวันนี้"
    elif day_type == 'tomorrow':
        target_date = today + datetime.timedelta(days=1)
        title = "งานพรุ่งนี้"
    else:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="คำสั่งไม่ถูกต้องครับ"))
        return

    filtered_tasks = []
    for task in tasks_raw:
        if task.get('due'):
            try:
                due_dt_utc = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                due_date_local = due_dt_utc.astimezone(THAILAND_TZ).date()
                if due_date_local == target_date and task.get('status') == 'needsAction':
                    filtered_tasks.append(task)
            except (ValueError, TypeError):
                continue
    
    reply_message = create_task_list_message(title, filtered_tasks)
    line_bot_api.reply_message(event.reply_token, reply_message)

# --- NEW: Handle create new task command ---
def handle_create_new_task_command(event):
    if not LIFF_ID_FORM:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ไม่สามารถสร้างงานได้: ไม่พบ LIFF ID สำหรับฟอร์ม"))
        return

    # สร้าง URL สำหรับ LIFF App
    liff_url = f"https://liff.line.me/{LIFF_ID_FORM}" 

    # สร้าง Quick Reply เพื่อให้ผู้ใช้กดเปิดฟอร์ม
    quick_reply_buttons = QuickReply(items=[
        QuickReplyButton(action=URIAction(label="เปิดฟอร์มสร้างงาน", uri=liff_url))
    ])

    line_bot_api.reply_message(
        event.reply_token, 
        TextSendMessage(
            text="คุณสามารถสร้างงานใหม่ได้ง่ายๆ ผ่านฟอร์มนี้ครับ 👇",
            quick_reply=quick_reply_buttons
        )
    )

def handle_view_task_by_name_command(event, customer_name):
    try:
        tasks_raw = get_google_tasks_for_report(show_completed=True) or []
        if not tasks_raw:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ไม่พบงานในระบบเลยครับ"))
            return

        matching_tasks = []
        for task in tasks_raw:
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            if customer_name.lower() in customer_info.get('name', '').lower():
                matching_tasks.append(task)
        
        if not matching_tasks:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"ไม่พบงานของลูกค้าชื่อ: {customer_name}"))
            return

        bubbles = [create_task_flex_message(task) for task in matching_tasks[:10]] # Limit to 10 bubbles
        
        carousel_contents = CarouselContainer(contents=bubbles)
        flex_message = FlexSendMessage(alt_text=f"ผลการค้นหางานของ: {customer_name}", contents=carousel_contents)
        
        line_bot_api.reply_message(event.reply_token, flex_message)

    except Exception as e:
        app.logger.error(f"Error in handle_view_task_by_name_command: {e}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ขออภัย, เกิดข้อผิดพลาดในการค้นหางานครับ"))

def create_task_flex_message(task):
    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    parsed_dates = parse_google_task_dates(task)
    update_url = url_for('task_details', task_id=task.get('id'), _external=True)
    
    bubble = BubbleContainer(
        direction='ltr',
        body=BoxComponent(layout='vertical', spacing='md', contents=[
            TextComponent(text=task.get('title', 'ไม่มีรายละเอียด'), weight='bold', size='lg', wrap=True),
            SeparatorComponent(margin='md'),
            BoxComponent(layout='vertical', margin='lg', spacing='sm', contents=[
                BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='ลูกค้า:', color='#AAAAAA', size='sm', flex=2), TextComponent(text=customer_info.get('name', '-'), wrap=True, color='#666666', size='sm', flex=5)]),
                BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='นัดหมาย:', color='#AAAAAA', size='sm', flex=2), TextComponent(text=parsed_dates.get('due_formatted', '-'), wrap=True, color='#666666', size='sm', flex=5)])
            ]),
        ]),
        footer=BoxComponent(layout='vertical', spacing='sm', contents=[
            ButtonComponent(style='primary', height='sm', action=URIAction(label='📝 เปิดในเว็บ', uri=update_url))
        ])
    )
    return bubble

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    text_lower = text.lower()
    
    command_map = {
        'งานค้าง': handle_outstanding_tasks_command,
        'งานเสร็จ': handle_completed_tasks_command,
        'งานวันนี้': lambda e: handle_daily_tasks_command(e, 'today'), # เพิ่มคำสั่งใหม่
        'งานพรุ่งนี้': lambda e: handle_daily_tasks_command(e, 'tomorrow'), # เพิ่มคำสั่งใหม่
        'สร้างงานใหม่': handle_create_new_task_command, # เพิ่มคำสั่งใหม่
        'สรุปรายงาน': lambda e: line_bot_api.reply_message(e.reply_token, TextSendMessage(text=f"ดูสรุปรายงานทั้งหมดได้ที่: {url_for('summary', _external=True)}")),
        'comphone': None 
    }
    
    if text_lower in command_map:
        if text_lower == 'comphone':
            help_text = (
                "สวัสดีครับ! พิมพ์คำสั่งที่ต้องการ:\n\n"
                "➡️ `งานค้าง`\nดูรายการงานที่ยังไม่เสร็จ\n\n"
                "➡️ `งานเสร็จ`\nดูงานที่ทำเสร็จล่าสุด\n\n"
                "➡️ `งานวันนี้`\nดูงานที่มีกำหนดเสร็จในวันนี้\n\n" # อัปเดตวิธีใช้
                "➡️ `งานพรุ่งนี้`\nดูงานที่มีกำหนดเสร็จในวันพรุ่งนี้\n\n" # อัปเดตวิธีใช้
                "➡️ `ดูงาน ชื่อลูกค้า`\nค้นหางานของลูกค้าคนนั้นๆ (เช่น: `ดูงาน สมศรี`)\n\n" # อัปเดตวิธีใช้
                "➡️ `สร้างงานใหม่`\nเปิดฟอร์มสำหรับสร้างงานใหม่\n\n" # อัปเดตวิธีใช้
                "➡️ `สรุปรายงาน`\nรับลิงก์เพื่อเปิดเว็บสรุปงาน\n\n"
                "หากคุณต้องการดูเมนูนี้อีกครั้ง พิมพ์ `comphone`"
            )
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text))
        else:
            command_map[text_lower](event)
        return
    
    if text_lower.startswith('ดูงาน '):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            handle_view_task_by_name_command(event, parts[1])
            return

    # หากข้อความไม่ใช่คำสั่งที่รู้จัก บอทจะยังคงเงียบ
    # ไม่มีการตอบกลับด้วยข้อความ "วิธีใช้" อัตโนมัติอีกต่อไป
    # ผู้ใช้ต้องพิมพ์ "comphone" เพื่อเรียกดูวิธีใช้
    # หากต้องการให้บอทแจ้งเตือนว่าไม่รู้จักคำสั่ง ให้เพิ่มบรรทัดด้านล่างนี้
    # line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ไม่เข้าใจคำสั่งของคุณครับ ลองพิมพ์ `comphone` เพื่อดูวิธีใช้"))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
