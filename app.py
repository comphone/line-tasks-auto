import os
import sys
import datetime
import re
import json
import pytz
import mimetypes 
import zipfile # เพิ่มการ import สำหรับสร้างไฟล์ .zip
from io import BytesIO # เพิ่มการ import สำหรับจัดการไฟล์ใน Memory

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, render_template, redirect, url_for, abort, send_from_directory, flash, jsonify, Response 
from werkzeug.utils import secure_filename
from cachetools import cached, TTLCache
from geopy.distance import geodesic

import qrcode
import base64

# --- ใช้ line-bot-sdk เวอร์ชัน 2.4.2 ---
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
    ImageMessage, FileMessage, PostbackEvent
)
# ---------------------------------------------

from google.oauth2.credentials import Credentials 
from google.auth.transport.requests import Request 
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload 

import pandas as pd 

# --- Initialization & Configurations ---
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_dev')
UPLOAD_FOLDER = 'static/uploads' 
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'}

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# --- LINE & Google Configs ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    sys.exit("LINE Bot credentials are not set in environment variables.")

LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM') 
LINE_ADMIN_GROUP_ID = os.environ.get('LINE_ADMIN_GROUP_ID')
LINE_HR_GROUP_ID = os.environ.get('LINE_HR_GROUP_ID') 
GOOGLE_TASKS_LIST_ID = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID') 

if not GOOGLE_DRIVE_FOLDER_ID:
    app.logger.warning("GOOGLE_DRIVE_FOLDER_ID environment variable is not set. Drive upload will not work.")

SCOPES = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/drive']
GOOGLE_CREDENTIALS_FILE_NAME = 'credentials.json'
THAILAND_TZ = pytz.timezone('Asia/Bangkok')
cache = TTLCache(maxsize=100, ttl=60)

# Initialize LINE Bot SDK v2
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

TECHNICIAN_LINE_IDS = {
    "ช่างเอ": "Uxxxxxxxxxxxxxxxxxxxxxxxxx1",
    "ช่างบี": "Uxxxxxxxxxxxxxxxxxxxxxxxxx2",
}

SETTINGS_FILE = 'settings.json'
_DEFAULT_APP_SETTINGS_STORE = {
    'report_times': { 'appointment_reminder_hour_thai': 7, 'outstanding_report_hour_thai': 20 },
    'line_recipients': { 'admin_group_id': os.environ.get('LINE_ADMIN_GROUP_ID', ''), 'manager_user_id': os.environ.get('LINE_MANAGER_USER_ID', ''), 'technician_group_id': os.environ.get('LINE_TECHNICIAN_GROUP_ID', '') },
    'qrcode_settings': { 'box_size': 8, 'border': 4, 'fill_color': '#28a745', 'back_color': '#FFFFFF', 'custom_url': '' },
    'equipment_catalog': [ 
        {'barcode': 'EQ001', 'item_name': 'สาย LAN', 'unit': 'เมตร', 'price': 50.0}, {'barcode': 'EQ002', 'item_name': 'หัว RJ45', 'unit': 'ชิ้น', 'price': 5.0},
        {'barcode': 'EQ003', 'item_name': 'คีมย้ำ', 'unit': 'อัน', 'price': 350.0}, {'barcode': 'EQ004', 'item_name': 'ไขควง', 'unit': 'อัน', 'price': 120.0},
        {'barcode': 'EQ005', 'item_name': 'มัลติมิเตอร์', 'unit': 'เครื่อง', 'price': 800.0}, {'barcode': 'EQ006', 'item_name': 'สายไฟ VAF 2.5', 'unit': 'เมตร', 'price': 30.0},
        {'barcode': 'EQ007', 'item_name': 'ปลั๊กไฟ', 'unit': 'ชุด', 'price': 80.0}, {'barcode': 'EQ008', 'item_name': 'เต้ารับ', 'unit': 'ตัว', 'price': 60.0},
        {'barcode': 'EQ009', 'item_name': 'เบรกเกอร์', 'unit': 'ลูก', 'price': 200.0}, {'barcode': 'EQ010', 'item_name': 'Adapter', 'unit': 'ชิ้น', 'price': 250.0},
        {'barcode': 'EQ011', 'item_name': 'ติดตั้งกล้อง', 'unit': 'จุด', 'price': 1500.0}
    ],
    'common_equipment_items': [] 
}
_APP_SETTINGS_STORE = {}

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
        if loaded:
            _APP_SETTINGS_STORE = _DEFAULT_APP_SETTINGS_STORE.copy()
            for key, default_value in _APP_SETTINGS_STORE.items():
                if isinstance(default_value, dict): _APP_SETTINGS_STORE[key].update(loaded.get(key, {}))
                elif key in loaded: _APP_SETTINGS_STORE[key] = loaded[key]
        else:
            _APP_SETTINGS_STORE = _DEFAULT_APP_SETTINGS_STORE
            save_settings_to_file(_APP_SETTINGS_STORE)
    _APP_SETTINGS_STORE['common_equipment_items'] = sorted(list(set(item['item_name'] for item in _APP_SETTINGS_STORE.get('equipment_catalog', []) if 'item_name' in item)))
    return _APP_SETTINGS_STORE

def save_app_settings(settings_data):
    global _APP_SETTINGS_STORE
    for key, value in settings_data.items():
        if isinstance(value, dict) and key in _APP_SETTINGS_STORE: _APP_SETTINGS_STORE[key].update(value)
        else: _APP_SETTINGS_STORE[key] = value
    _APP_SETTINGS_STORE['common_equipment_items'] = sorted(list(set(item['item_name'] for item in _APP_SETTINGS_STORE.get('equipment_catalog', []) if 'item_name' in item)))
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
        if not creds and os.path.exists(GOOGLE_CREDENTIALS_FILE_NAME):
            flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_FILE_NAME, SCOPES)
            creds = flow.run_console()
        if creds:
            with open(token_path, 'w') as token: token.write(creds.to_json())
            app.logger.info(f"Token saved to {token_path}. Please update GOOGLE_TOKEN_JSON on Render.")
    return build(api_name, api_version, credentials=creds) if creds else None

def get_google_tasks_service(): return get_google_service('tasks', 'v1')
def get_google_calendar_service(): return get_google_service('calendar', 'v3')
def get_google_drive_service(): return get_google_service('drive', 'v3')

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

def create_google_calendar_event(summary, location, description, start_time, end_time, timezone='Asia/Bangkok'):
    service = get_google_calendar_service()
    if not service:
        app.logger.error("Failed to get Google Calendar service.")
        return None
    try:
        event = {
            'summary': summary, 'location': location, 'description': description,
            'start': {'dateTime': start_time, 'timeZone': timezone},
            'end': {'dateTime': end_time, 'timeZone': timezone},
            'reminders': {'useDefault': True},
        }
        return service.events().insert(calendarId='primary', body=event).execute()
    except HttpError as e:
        app.logger.error(f"Error creating Google Calendar Event: {e}")
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

def get_upcoming_events(time_delta_hours=24):
    service = get_google_calendar_service()
    if not service: return []
    try:
        now_utc = datetime.datetime.utcnow().isoformat() + 'Z'
        time_max_utc = (datetime.datetime.utcnow() + datetime.timedelta(hours=time_delta_hours)).isoformat() + 'Z'
        events_result = service.events().list(
            calendarId='primary', timeMin=now_utc, timeMax=time_max_utc,
            maxResults=10, singleEvents=True, orderBy='startTime'
        ).execute()
        return events_result.get('items', [])
    except HttpError as e:
        app.logger.error(f"Error fetching upcoming events: {e}")
        return []

def extract_lat_lon_from_notes(notes):
    if not notes: return None, None
    match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", notes)
    if match: return (float(match.group(1)), float(match.group(2)))
    match = re.search(r"พิกัด:\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", notes)
    if match: return (float(match.group(1)), float(match.group(2)))
    map_url_regex = r"https?://(?:www\.)?(?:google\.com/maps/place/|maps\.app\.goo\.gl/)(?:[^/]+/@)?(-?\d+\.\d+),(-?\d+\.\d+)"
    map_url_match = re.search(map_url_regex, notes)
    if map_url_match:
        return (float(map_url_match.group(1)), float(map_url_match.group(2)))
    return None, None

def find_nearby_jobs(completed_task_id, radius_km=5):
    completed_task = get_single_task(completed_task_id)
    if not completed_task: return []
    origin_lat, origin_lon = extract_lat_lon_from_notes(completed_task.get('notes', ''))
    if origin_lat is None or origin_lon is None: return []
    origin_coords = (origin_lat, origin_lon)
    pending_tasks = get_google_tasks_for_report(show_completed=False)
    if not pending_tasks: return []
    nearby_jobs = []
    for task in pending_tasks:
        if task.get('id') == completed_task_id: continue
        task_lat, task_lon = extract_lat_lon_from_notes(task.get('notes', ''))
        if task_lat is not None and task_lon is not None:
            distance = geodesic(origin_coords, (task_lat, task_lon)).kilometers
            if distance <= radius_km:
                task['distance_km'] = round(distance, 1)
                nearby_jobs.append(task)
    nearby_jobs.sort(key=lambda x: x['distance_km'])
    return nearby_jobs

def parse_customer_info_from_notes(notes):
    info = {'name': '', 'phone': '', 'address': '', 'detail': '', 'map_url': None}
    if not notes: return info
    base_content = re.sub(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL).strip()
    lines = [line.strip() for line in base_content.split('\n') if line.strip()]
    if lines: info['name'] = lines.pop(0)
    if lines: info['phone'] = lines.pop(0)
    if lines: info['address'] = lines.pop(0)
    map_url_regex = r"https?://(?:www\.)?google\.com/maps.*"
    if lines and re.match(map_url_regex, lines[0]): info['map_url'] = lines.pop(0)
    info['detail'] = "\n".join(lines)
    return info

def parse_google_task_dates(task_item):
    parsed = task_item.copy()
    for key in ['created', 'due', 'completed']:
        if parsed.get(key):
            try:
                dt_utc = datetime.datetime.fromisoformat(parsed[key].replace('Z', '+00:00'))
                parsed[f'{key}_formatted'] = dt_utc.astimezone(THAILAND_TZ).strftime("%d/%m/%y %H:%M")
            except (ValueError, TypeError): parsed[f'{key}_formatted'] = ''
        else: parsed[f'{key}_formatted'] = ''
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

def generate_qr_code_base64(url, box_size=10, border=4, fill_color="black", back_color="white"):
    try:
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=box_size, border=border)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color=fill_color, back_color=back_color)
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('utf-8')}"
    except Exception as e:
        app.logger.error(f"Error generating QR code: {e}")
        return None

def _parse_equipment_string(text_input):
    equipment_list, common_items = [], set(get_app_settings().get('common_equipment_items', []))
    if not text_input: return equipment_list
    for line in text_input.strip().split('\n'):
        if not line.strip(): continue
        parts = line.split(',', 1)
        item_name = parts[0].strip()
        if item_name:
            equipment_list.append({"item": item_name, "quantity": parts[1].strip() if len(parts) > 1 else ''})
            common_items.add(item_name)
    save_app_settings({'common_equipment_items': sorted(list(common_items))})
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

def create_task_flex_message(task):
    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    parsed_dates = parse_google_task_dates(task)
    update_url = url_for('update_task_details', task_id=task.get('id'), _external=True)
    phone_action = URIAction(label=customer_info['phone'], uri=f"tel:{re.sub(r'[^0-9]','', customer_info['phone'])}") if customer_info.get('phone') else None
    map_action = URIAction(label="📍 เปิด Google Maps", uri=customer_info['map_url']) if customer_info.get('map_url') else None
    bubble = BubbleContainer(
        direction='ltr',
        header=BoxComponent(layout='vertical', contents=[TextComponent(text='📢 แจ้งเตือนงาน', weight='bold', color='#ffffff')], background_color='#007BFF', padding_all='12px'),
        body=BoxComponent(layout='vertical', contents=[
            TextComponent(text=task.get('title', 'ไม่มีหัวข้อ'), weight='bold', size='xl', wrap=True), SeparatorComponent(margin='md'),
            BoxComponent(layout='vertical', margin='lg', spacing='sm', contents=[
                BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='ลูกค้า:', color='#007BFF', size='sm', flex=2, weight='bold'), TextComponent(text=customer_info.get('name', '-'), wrap=True, color='#666666', size='sm', flex=5)]),
                BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='โทร:', color='#007BFF', size='sm', flex=2, weight='bold'), TextComponent(text=customer_info.get('phone', '-'), wrap=True, color='#1E90FF', size='sm', flex=5, action=phone_action, decoration='underline' if phone_action else 'none')]),
                BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='นัดหมาย:', color='#007BFF', size='sm', flex=2, weight='bold'), TextComponent(text=parsed_dates.get('due_formatted', '-'), wrap=True, color='#666666', size='sm', flex=5)])
            ]),
            SeparatorComponent(margin='md'),
            TextComponent(text='รายละเอียดงาน:', weight='bold', color='#007BFF', size='sm', margin='md'), TextComponent(text=customer_info.get('detail', '-'), wrap=True, margin='sm', color='#666666')
        ]),
        footer=BoxComponent(layout='vertical', spacing='sm', contents=([ButtonComponent(style='link', height='sm', action=map_action), SeparatorComponent(margin='md')] if map_action else []) + [ButtonComponent(style='link', height='sm', action=URIAction(label='📝 อัปเดต/สรุปงาน', uri=update_url))])
    )
    return FlexSendMessage(alt_text=f"แจ้งเตือนงาน: {task.get('title', '')}", contents=bubble)

@app.route("/", methods=['GET', 'POST'])
def form_page():
    if request.method == 'POST':
        customer_name = str(request.form.get('customer', '')).strip()
        detail = str(request.form.get('detail', '')).strip()
        if not customer_name or not detail:
            flash('กรุณากรอกชื่อลูกค้าและรายละเอียดงาน', 'danger')
            return redirect(url_for('form_page'))
        
        customer_phone = str(request.form.get('phone', '')).strip()
        address = str(request.form.get('address', '')).strip()
        appointment_str = str(request.form.get('appointment', '')).strip()
        map_url_from_form = str(request.form.get('latitude_longitude', '')).strip()
        
        title = f"งานลูกค้า: {customer_name} ({datetime.datetime.now(THAILAND_TZ).strftime('%d/%m/%y')})"
        notes_lines = [customer_name, customer_phone, address]
        if map_url_from_form: notes_lines.append(map_url_from_form)
        notes_lines.append(detail)
        notes = "\n".join(filter(None, notes_lines))
        
        due_date_gmt = None
        if appointment_str:
            try:
                dt_local = THAILAND_TZ.localize(datetime.datetime.strptime(appointment_str, "%Y-%m-%d %H:%M"))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat()
            except ValueError: app.logger.error(f"Invalid appointment format: {appointment_str}")

        created_task = create_google_task(title, notes=notes, due=due_date_gmt)
        if created_task:
            cache.clear()
            try:
                flex_message = create_task_flex_message(created_task)
                settings = get_app_settings()
                recipients = [id for id in [settings['line_recipients'].get('admin_group_id'), settings['line_recipients'].get('technician_group_id')] if id]
                if recipients:
                    line_bot_api.multicast(recipients, flex_message)
                    flash('สร้างงานและส่งแจ้งเตือน LINE เรียบร้อยแล้ว!', 'success')
                else:
                    flash('สร้างงานเรียบร้อยแล้ว (ไม่มีผู้รับแจ้งเตือน LINE).', 'info')
            except Exception as e:
                app.logger.error(f"Failed to create or push Flex Message: {e}")
                flash('สร้างงานเรียบร้อยแล้ว แต่ส่งข้อความแจ้งเตือนไม่ได้', 'warning')
            return redirect(url_for('summary'))
        else:
            flash('เกิดข้อผิดพลาดในการสร้างงาน', 'danger')
    return render_template('form.html')

@app.route("/lookup_customer", methods=['GET'])
def lookup_customer():
    customer_name_query = str(request.args.get('customer_name', '')).strip().lower() 
    if not customer_name_query: return jsonify({}) 
    tasks_raw = get_google_tasks_for_report(show_completed=False) 
    if tasks_raw is None: return jsonify({"error": "Failed to retrieve tasks"}), 500
    found_customer_info = {}
    for task_item in reversed(tasks_raw): 
        customer_info = parse_customer_info_from_notes(task_item.get('notes', ''))
        if customer_name_query in str(customer_info.get('name', '')).strip().lower(): 
            if customer_info.get('phone'): found_customer_info['phone'] = str(customer_info['phone']).strip() 
            if customer_info.get('address'): found_customer_info['address'] = str(customer_info['address']).strip() 
            if customer_info.get('detail'): found_customer_info['detail'] = str(customer_info['detail']).strip() 
            if customer_info.get('map_url'): found_customer_info['map_url'] = str(customer_info['map_url']).strip() 
            if all(key in found_customer_info and found_customer_info[key] for key in ['phone', 'address', 'detail']): break
    return jsonify(found_customer_info)

@app.route("/lookup_equipment", methods=['GET'])
def lookup_equipment():
    query = request.args.get('q', '').strip().lower()
    if not query: return jsonify([])
    equipment_catalog = get_app_settings().get('equipment_catalog', [])
    results = []
    for item in equipment_catalog:
        item_name_lower = str(item.get('item_name', '')).lower()
        barcode_lower = str(item.get('barcode', '')).lower()
        if query in item_name_lower or (barcode_lower and query in barcode_lower):
            results.append(item)
    results.sort(key=lambda x: (not str(x['item_name']).lower().startswith(query), str(x['item_name']).lower()))
    return jsonify(results[:10])

@app.route('/summary')
def summary():
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    tasks_raw = get_google_tasks_for_report(show_completed=True)
    if tasks_raw is None:
        flash('ไม่สามารถเชื่อมต่อกับ Google Tasks ได้', 'danger')
        return render_template("tasks_summary.html", tasks=[], summary={}, search_query=search_query, status_filter=status_filter)
    
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
        
        if task_status == 'completed': total_summary_stats['completed'] += 1
        elif task_status == 'needsAction':
            total_summary_stats['needsAction'] += 1
            if is_overdue: total_summary_stats['overdue'] += 1

        if (status_filter == 'all' or
            (status_filter == 'completed' and task_status == 'completed') or
            (status_filter == 'needsAction' and task_status == 'needsAction' and not is_overdue) or
            (status_filter == 'overdue' and is_overdue)):
            
            if not search_query or (search_query in str(task.get('title', '')).lower() or search_query in str(task.get('notes', '')).lower()):
                parsed_task = parse_google_task_dates(task)
                parsed_task['customer'] = parse_customer_info_from_notes(parsed_task.get('notes', ''))
                parsed_task['is_overdue'] = is_overdue
                final_filtered_tasks.append(parsed_task)

    final_filtered_tasks.sort(key=lambda x: x.get('created', ''), reverse=True)
    return render_template("tasks_summary.html", tasks=final_filtered_tasks, summary=total_summary_stats, search_query=search_query, status_filter=status_filter)

@app.route('/update_task/<task_id>', methods=['GET', 'POST'])
def update_task_details(task_id):
    service = get_google_tasks_service()
    if not service: abort(503)
    try:
        task_raw = service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
    except HttpError: abort(404)
    
    if request.method == 'POST':
        original_status = task_raw.get('status')
        new_status = request.form.get('status')
        updated_customer_name = str(request.form.get('customer_name', '')).strip()
        
        base_notes_lines = [
            updated_customer_name,
            str(request.form.get('customer_phone', '')).strip(),
            str(request.form.get('address', '')).strip(),
            str(request.form.get('latitude_longitude', '')).strip(),
            str(request.form.get('detail', '')).strip()
        ]
        updated_base_notes = "\n".join(filter(None, base_notes_lines))
        
        history, _ = parse_tech_report_from_notes(task_raw.get('notes', ''))
        
        all_reports_text = ""
        for report in history:
            all_reports_text += f"\n\n--- TECH_REPORT_START ---\n{json.dumps(report, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"
        
        work_summary = str(request.form.get('work_summary', '')).strip()
        if work_summary:
            new_tech_report_data = {
                'summary_date': datetime.datetime.now(THAILAND_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                'work_summary': work_summary,
                'equipment_used': _parse_equipment_string(request.form.get('equipment_used', '')),
                'attachment_urls': [] 
            }
            all_reports_text += f"\n\n--- TECH_REPORT_START ---\n{json.dumps(new_tech_report_data, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"
        
        final_notes = updated_base_notes + all_reports_text
        
        updated_task = update_google_task(task_id, title=f"งานลูกค้า: {updated_customer_name}", notes=final_notes, status=new_status)
        
        if updated_task:
            cache.clear()
            flash('อัปเดตงานเรียบร้อยแล้ว!', 'success')
            if new_status == 'completed' and original_status != 'completed':
                pass 
        else:
            flash('เกิดข้อผิดพลาดในการอัปเดตงาน', 'danger')
        return redirect(url_for('summary'))
        
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

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# --- ฟังก์ชันสำรองข้อมูล ---
@app.route('/backup_data')
def backup_data():
    """สร้างและส่งไฟล์ ZIP ที่มีการสำรองข้อมูล tasks และ settings."""
    try:
        # 1. ดึงข้อมูล
        all_tasks = get_google_tasks_for_report(show_completed=True)
        all_settings = get_app_settings()

        if all_tasks is None:
            flash('ไม่สามารถดึงข้อมูลงานจาก Google Tasks ได้', 'danger')
            return redirect(url_for('settings_page'))

        # 2. สร้างไฟล์ ZIP ในหน่วยความจำ
        memory_file = BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            # เพิ่มข้อมูล tasks
            tasks_data = json.dumps(all_tasks, indent=4, ensure_ascii=False)
            zf.writestr('tasks_backup.json', tasks_data)
            
            # เพิ่มข้อมูล settings
            settings_data = json.dumps(all_settings, indent=4, ensure_ascii=False)
            zf.writestr('settings_backup.json', settings_data)
        
        memory_file.seek(0)

        # 3. ส่งไฟล์ให้ผู้ใช้ดาวน์โหลด
        backup_filename = f"backup_{datetime.date.today()}.zip"
        return Response(
            memory_file,
            mimetype='application/zip',
            headers={'Content-Disposition': f'attachment;filename={backup_filename}'}
        )

    except Exception as e:
        app.logger.error(f"Error creating backup file: {e}")
        flash('เกิดข้อผิดพลาดในการสร้างไฟล์สำรองข้อมูล', 'danger')
        return redirect(url_for('settings_page'))


@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        save_app_settings({
            'report_times': { 'appointment_reminder_hour_thai': int(request.form.get('appointment_reminder_hour')), 'outstanding_report_hour_thai': int(request.form.get('outstanding_report_hour')) },
            'line_recipients': { 'admin_group_id': request.form.get('admin_group_id', '').strip(), 'manager_user_id': request.form.get('manager_user_id', '').strip(), 'technician_group_id': request.form.get('technician_group_id', '').strip() },
            'qrcode_settings': { 'box_size': int(request.form.get('qr_box_size', 8)), 'border': int(request.form.get('qr_border', 4)), 'fill_color': request.form.get('qr_fill_color', '#28a745'), 'back_color': request.form.get('qr_back_color', '#FFFFFF'), 'custom_url': request.form.get('qr_custom_url', '').strip() }
        })
        flash('บันทึกการตั้งค่าเรียบร้อยแล้ว!', 'success')
        cache.clear()
        return redirect(url_for('settings_page'))

    current_settings = get_app_settings()
    general_summary_url = url_for('summary', _external=True)
    qr_url_to_use = current_settings.get('qrcode_settings', {}).get('custom_url', '') or general_summary_url
    qr_settings = current_settings.get('qrcode_settings', {})
    qr_code_base64_general = generate_qr_code_base64(
        qr_url_to_use, 
        box_size=qr_settings.get('box_size', 8), border=qr_settings.get('border', 4),
        fill_color=qr_settings.get('fill_color', '#28a745'), back_color=qr_settings.get('back_color', '#FFFFFF')
    )
    return render_template('settings_page.html', settings=current_settings, qr_code_base64_general=qr_code_base64_general, general_summary_url=general_summary_url)

@app.route('/export_equipment_catalog', methods=['GET'])
def export_equipment_catalog():
    try:
        equipment_catalog = get_app_settings().get('equipment_catalog', [])
        columns = ['รหัสสินค้า/barcodeสินค้า', 'รายการสินค้า', 'หน่วย', 'ราคา']
        data_for_df = [{'รหัสสินค้า/barcodeสินค้า': item.get('barcode', ''), 'รายการสินค้า': item.get('item_name', ''), 'หน่วย': item.get('unit', ''), 'ราคา': item.get('price', 0.0)} for item in equipment_catalog]
        df = pd.DataFrame(data_for_df, columns=columns)
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Equipment Catalog')
        output.seek(0)
        return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment;filename=equipment_catalog_template.xlsx"})
    except Exception as e:
        app.logger.error(f"Error exporting equipment catalog: {e}")
        flash(f"เกิดข้อผิดพลาดในการส่งออกแคตตาล็อกอุปกรณ์: {e}", 'danger')
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
            df.columns = [col.strip().lower() for col in df.columns]
            expected_cols_map = {'รหัสสินค้า/barcodeสินค้า': 'barcode', 'รายการสินค้า': 'item_name', 'หน่วย': 'unit', 'ราคา': 'price'}
            df_renamed = df.rename(columns={k.lower(): v for k, v in expected_cols_map.items()})

            if not all(col in df_renamed.columns for col in expected_cols_map.values()):
                missing_cols = [k for k, v in expected_cols_map.items() if v not in df_renamed.columns]
                flash(f'ไฟล์ Excel ต้องมีคอลัมน์ที่จำเป็น: {", ".join(missing_cols)}', 'danger')
                return redirect(url_for('settings_page'))
                
            new_catalog = df_renamed[list(expected_cols_map.values())].to_dict('records')
            for item in new_catalog:
                try: item['price'] = float(item['price']) if pd.notna(item['price']) else 0.0
                except (ValueError, TypeError): item['price'] = 0.0
            
            current_settings = get_app_settings()
            current_settings['equipment_catalog'] = [item for item in new_catalog if item.get('item_name')]
            if save_app_settings(current_settings):
                flash('นำเข้าแคตตาล็อกอุปกรณ์เรียบร้อยแล้ว!', 'success')
            else:
                flash('เกิดข้อผิดพลาดในการบันทึกแคตตาล็อกอุปกรณ์', 'danger')
        except Exception as e:
            app.logger.error(f"Error importing Excel: {e}")
            flash(f"เกิดข้อผิดพลาดในการนำเข้าไฟล์: {e}", 'danger')
    else:
        flash('รองรับเฉพาะไฟล์ Excel (.xls, .xlsx) เท่านั้น', 'danger')
    return redirect(url_for('settings_page'))


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

def handle_help_command(event):
    help_text = (
        "🤖 **วิธีใช้งานบอท** 🤖\n\n"
        "➡️ `งานค้าง`\nดูรายการงานที่ยังไม่เสร็จ\n\n"
        "➡️ `งานเสร็จ`\nดูรายการงานที่เสร็จแล้ว 5 งานล่าสุด\n\n"
        "➡️ `สรุปรายงาน`\nดูภาพรวมจำนวนงาน\n\n"
        "➡️ `c <ชื่อลูกค้า>`\nค้นหาประวัติงานของลูกค้า (เช่น c สมศรี)\n\n"
        "➡️ `ดูงาน <ID>`\nดูรายละเอียดของงานตาม ID\n\n"
        "➡️ `เสร็จงาน <ID>`\nปิดงานด่วนจาก LINE\n\n"
        "➡️ `เปิดงานใหม่` หรือ `เริ่มลงงาน`\nรับลิงก์สำหรับจัดการงาน"
    )
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text))

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    text_lower = text.lower()

    if text_lower == 'comphone' or text_lower == 'help':
        handle_help_command(event)

    elif text_lower == 'สรุปงาน':
        summary_url = url_for('summary', _external=True)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"ดูสรุปงานทั้งหมดได้ที่: {summary_url}"))

    elif text_lower == 'สร้างงานใหม่' or text_lower == 'เปิดงานใหม่':
        form_url = url_for('form_page', _external=True)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"สร้างงานใหม่ผ่านฟอร์มได้ที่นี่: {form_url}"))
    
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
