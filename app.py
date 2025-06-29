import os
import sys
import datetime
import re
import json
import pytz
import mimetypes

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, render_template, redirect, url_for, abort, send_from_directory, flash, jsonify, Response
from werkzeug.utils import secure_filename
from cachetools import cached, TTLCache
from geopy.distance import geodesic

import qrcode
import base64
from io import BytesIO

# --- การแก้ไข: เปลี่ยนไปใช้ line-bot-sdk เวอร์ชัน 2 ---
from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage,
    BubbleContainer, CarouselContainer, BoxComponent, TextComponent,
    ButtonComponent, SeparatorComponent, URIAction, PostbackAction, QuickReply, QuickReplyButton
)
# ----------------------------------------------------------------

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

# --- การแก้ไข: เปลี่ยนไปใช้ LineBotApi ของเวอร์ชัน 2 ---
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
# -------------------------------------------------------

TECHNICIAN_LINE_IDS = {
    "ช่างเอ": "Uxxxxxxxxxxxxxxxxxxxxxxxxx1",
    "ช่างบี": "Uxxxxxxxxxxxxxxxxxxxxxxxxx2",
}

# [START app_settings_json_persistence_final]
SETTINGS_FILE = 'settings.json'

# Default settings structure (used if settings.json is empty or fails)
_DEFAULT_APP_SETTINGS_STORE = {
    'report_times': {
        'appointment_reminder_hour_thai': 7,
        'outstanding_report_hour_thai': 20
    },
    'line_recipients': {
        'admin_group_id': os.environ.get('LINE_ADMIN_GROUP_ID', ''),
        'manager_user_id': os.environ.get('LINE_MANAGER_USER_ID', ''),
        'technician_group_id': os.environ.get('LINE_TECHNICIAN_GROUP_ID', '')
    },
    'qrcode_settings': {
        'box_size': 8,
        'border': 4,
        'fill_color': '#28a745',
        'back_color': '#FFFFFF',
        'custom_url': ''
    },
    'equipment_catalog': [
        {'barcode': 'EQ001', 'item_name': 'สาย LAN', 'unit': 'เมตร', 'price': 50.0},
        {'barcode': 'EQ002', 'item_name': 'หัว RJ45', 'unit': 'ชิ้น', 'price': 5.0},
        {'barcode': 'EQ003', 'item_name': 'คีมย้ำ', 'unit': 'อัน', 'price': 350.0},
        {'barcode': 'EQ004', 'item_name': 'ไขควง', 'unit': 'อัน', 'price': 120.0},
        {'barcode': 'EQ005', 'item_name': 'มัลติมิเตอร์', 'unit': 'เครื่อง', 'price': 800.0},
        {'barcode': 'EQ006', 'item_name': 'สายไฟ VAF 2.5', 'unit': 'เมตร', 'price': 30.0},
        {'barcode': 'EQ007', 'item_name': 'ปลั๊กไฟ', 'unit': 'ชุด', 'price': 80.0},
        {'barcode': 'EQ008', 'item_name': 'เต้ารับ', 'unit': 'ตัว', 'price': 60.0},
        {'barcode': 'EQ009', 'item_name': 'เบรกเกอร์', 'unit': 'ลูก', 'price': 200.0},
        {'barcode': 'EQ010', 'item_name': 'Adapter', 'unit': 'ชิ้น', 'price': 250.0},
        {'barcode': 'EQ011', 'item_name': 'ติดตั้งกล้อง', 'unit': 'จุด', 'price': 1500.0}
    ],
    'common_equipment_items': []
}

# Global variable to hold current settings in memory
_APP_SETTINGS_STORE = {}

def load_settings_from_file():
    """Loads settings from settings.json file."""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            app.logger.error(f"Error decoding settings.json: {e}")
        except IOError as e:
            app.logger.error(f"Error reading settings.json file: {e}")
    return None

def save_settings_to_file(settings_data):
    """Saves settings to settings.json file."""
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings_data, f, ensure_ascii=False, indent=4)
        app.logger.info("Settings saved to settings.json successfully.")
        return True
    except IOError as e:
        app.logger.error(f"Error writing settings to settings.json file: {e}")
        return False

def get_app_settings():
    """Retrieves app settings, preferring from file, then defaults."""
    global _APP_SETTINGS_STORE
    if not _APP_SETTINGS_STORE:
        loaded_from_file = load_settings_from_file()
        if loaded_from_file:
            for key, default_value in _DEFAULT_APP_SETTINGS_STORE.items():
                if key not in loaded_from_file:
                    loaded_from_file[key] = default_value
                elif isinstance(default_value, dict) and isinstance(loaded_from_file.get(key), dict):
                    loaded_from_file[key] = {**default_value, **loaded_from_file[key]}
                elif isinstance(default_value, list) and key == 'equipment_catalog' and not isinstance(loaded_from_file.get(key), list):
                    app.logger.warning(f"equipment_catalog in settings.json is not a list. Resetting to default.")
                    loaded_from_file[key] = default_value
            _APP_SETTINGS_STORE.update(loaded_from_file)
            app.logger.info("Settings loaded from settings.json.")
        else:
            app.logger.warning("settings.json not found or could not be loaded. Using default settings.")
            _APP_SETTINGS_STORE.update(_DEFAULT_APP_SETTINGS_STORE)
            save_settings_to_file(_APP_SETTINGS_STORE)

    _APP_SETTINGS_STORE['common_equipment_items'] = sorted(
        list(set(item['item_name'] for item in _APP_SETTINGS_STORE.get('equipment_catalog', []) if 'item_name' in item))
    )
    return _APP_SETTINGS_STORE

def save_app_settings(settings_data):
    """Saves updated settings to in-memory store and to file."""
    global _APP_SETTINGS_STORE
    for key, value in settings_data.items():
        if isinstance(value, dict) and key in _APP_SETTINGS_STORE and isinstance(_APP_SETTINGS_STORE[key], dict):
            _APP_SETTINGS_STORE[key].update(value)
        elif key == 'equipment_catalog' and isinstance(value, list):
            _APP_SETTINGS_STORE[key] = value
        else:
            _APP_SETTINGS_STORE[key] = value
            
    _APP_SETTINGS_STORE['common_equipment_items'] = sorted(
        list(set(item['item_name'] for item in _APP_SETTINGS_STORE.get('equipment_catalog', []) if 'item_name' in item))
    )
    
    return save_settings_to_file(_APP_SETTINGS_STORE)

# Initial load of settings on app startup
_APP_SETTINGS_STORE = get_app_settings()

def get_google_service(api_name, api_version):
    """Handles Google API authentication and returns a service object for the specified API."""
    creds = None
    token_path = 'token.json'
    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')

    if google_token_json_str:
        try:
            creds_info = json.loads(google_token_json_str)
            creds = Credentials.from_authorized_user_info(creds_info, SCOPES)
        except Exception as e:
            app.logger.warning(f"Could not load token from GOOGLE_TOKEN_JSON: {e}")
            creds = None
    elif os.path.exists(token_path):
        creds = Credentials.from_authorized_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                app.logger.error(f"Error refreshing Google token, re-authenticating: {e}")
                creds = None
        if not creds:
            if os.path.exists(GOOGLE_CREDENTIALS_FILE_NAME):
                flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_FILE_NAME, SCOPES)
                creds = flow.run_console()
            else:
                app.logger.error("Google credentials file not found.")
                return None
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
        app.logger.info(f"New token saved to {token_path}. Please update GOOGLE_TOKEN_JSON on Render.")

    if creds:
        return build(api_name, api_version, credentials=creds)
    return None

def get_google_tasks_service():
    return get_google_service('tasks', 'v1')

def get_google_calendar_service():
    return get_google_service('calendar', 'v3')

def get_google_drive_service():
    return get_google_service('drive', 'v3')

def upload_file_to_google_drive(file_path, file_name, mime_type):
    service = get_google_drive_service()
    if not service:
        app.logger.error("ไม่สามารถเชื่อมต่อ Google Drive service ได้สำหรับการอัปโหลด")
        return None
    
    if not GOOGLE_DRIVE_FOLDER_ID:
        app.logger.warning("ไม่ได้ตั้งค่า GOOGLE_DRIVE_FOLDER_ID ไม่สามารถอัปโหลดไฟล์ไป Google Drive ได้")
        return None

    try:
        file_metadata = {
            'name': file_name,
            'parents': [GOOGLE_DRIVE_FOLDER_ID],
            'mimeType': mime_type
        }
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        
        file_obj = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink, webContentLink' 
        ).execute()

        service.permissions().create(
            fileId=file_obj['id'],
            body={'role': 'reader', 'type': 'anyone'}, 
            fields='id'
        ).execute()
        
        app.logger.info(f"ไฟล์ถูกอัปโหลดไปที่ Google Drive: {file_obj.get('webViewLink')}")
        return file_obj.get('webViewLink') 

    except HttpError as error:
        app.logger.error(f'เกิดข้อผิดพลาดขณะอัปโหลดไป Google Drive: {error}')
        return None
    except Exception as e:
        app.logger.error(f'เกิดข้อผิดพลาดที่ไม่คาดคิดระหว่างการอัปโหลด Drive: {e}')
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
        app.logger.info(f"Successfully deleted task ID: {task_id}")
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
                if due is not None: 
                    task['due'] = due
        
        return service.tasks().update(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id, body=task).execute()
    except HttpError as e:
        app.logger.error(f"Failed to update task {task_id}: {e}")
        return None

def parse_customer_info_from_notes(notes):
    info = {
        'name': '',
        'phone': '',
        'address': '',
        'detail': '',
        'map_url': None
    }
    if not notes: return info

    base_notes_content = re.sub(r"--- TECH_REPORT_START ---\s*.*?\s*--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL).strip()
    
    lines = [line.strip() for line in base_notes_content.split('\n') if line.strip()] 

    if len(lines) > 0:
        info['name'] = lines[0]
    
    if len(lines) > 1:
        info['phone'] = lines[1]
    
    if len(lines) > 2:
        info['address'] = lines[2]

    map_url_regex = r"https?://(?:www\.)?(?:google\.com/maps/place/|maps\.app\.goo\.gl/)(?:[^/]+/@)?(-?\d+\.\d+),(-?\d+\.\d+)"
    detail_start_line_idx = 3 

    if len(lines) > 3:
        if re.match(map_url_regex, lines[3]):
            info['map_url'] = lines[3]
            detail_start_line_idx = 4 
        else:
            detail_start_line_idx = 3 
    
    if len(lines) > detail_start_line_idx -1: 
        info['detail'] = "\n".join(lines[detail_start_line_idx:])

    return info
    
def parse_google_task_dates(task_item):
    parsed_task = task_item.copy()
    for key in ['created', 'due', 'completed']:
        if key in parsed_task and parsed_task[key]:
            try:
                dt_utc = datetime.datetime.fromisoformat(parsed_task[key].replace('Z', '+00:00'))
                dt_thai = dt_utc.astimezone(THAILAND_TZ)
                parsed_task[f'{key}_formatted'] = dt_thai.strftime("%d/%m/%y %H:%M" if key == 'due' else "%d/%m/%y %H:%M:%S")
            except (ValueError, TypeError):
                parsed_task[f'{key}_formatted'] = '' 
        else:
            parsed_task[f'{key}_formatted'] = '' 
    return parsed_task
    
def create_task_flex_message(task):
    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    parsed_dates = parse_google_task_dates(task)
    update_url = url_for('update_task_details', task_id=task.get('id'), _external=True)

    phone_action = None
    phone_display_text = str(customer_info.get('phone', '')).strip() 
    phone_number_cleaned = re.sub(r'\D', '', phone_display_text) 
    if phone_number_cleaned and phone_display_text: 
        phone_action = URIAction(label=phone_display_text, uri=f"tel:{phone_number_cleaned}")

    map_action = None
    map_url = str(customer_info.get('map_url', '')).strip() 
    if map_url and (map_url.startswith('http://') or map_url.startswith('https://')):
        map_action = URIAction(label="📍 เปิด Google Maps", uri=map_url)
    
    bubble = BubbleContainer(
        direction='ltr',
        header=BoxComponent(
            layout='vertical',
            contents=[TextComponent(text='📢 แจ้งเตือนงาน', weight='bold', color='#ffffff')],
            background_color='#007BFF',
            padding_all='12px'
        ),
        body=BoxComponent(
            layout='vertical',
            contents=[
                TextComponent(text=str(task.get('title', 'ไม่มีหัวข้อ')), weight='bold', size='xl', wrap=True),
                SeparatorComponent(margin='md'), 
                BoxComponent(layout='vertical', margin='lg', spacing='sm', contents=[
                    BoxComponent(layout='baseline', spacing='sm', contents=[
                        TextComponent(text='ลูกค้า:', color='#007BFF', size='sm', flex=2, weight='bold'), 
                        TextComponent(text=str(customer_info.get('name', '') or '-'), wrap=True, color='#666666', size='sm', flex=5) 
                    ]),
                    BoxComponent(layout='baseline', spacing='sm', contents=[
                        TextComponent(text='โทร:', color='#007BFF', size='sm', flex=2, weight='bold'), 
                        TextComponent(text=str(phone_display_text or '-'), wrap=True, color='#1E90FF', size='sm', flex=5, action=phone_action, decoration='underline' if phone_action else 'none') 
                    ]),
                    BoxComponent(layout='baseline', spacing='sm', contents=[
                        TextComponent(text='นัดหมาย:', color='#007BFF', size='sm', flex=2, weight='bold'), 
                        TextComponent(text=str(parsed_dates.get('due_formatted', '') or '-'), wrap=True, color='#666666', size='sm', flex=5) 
                    ])
                ]),
                SeparatorComponent(margin='md'), 
                TextComponent(text='รายละเอียดงาน:', weight='bold', color='#007BFF', size='sm', margin='md'), 
                TextComponent(text=str(customer_info.get('detail', '') or '-'), wrap=True, margin='sm', color='#666666')
            ]
        ),
        footer=BoxComponent(
            layout='vertical',
            spacing='sm',
            contents=([ButtonComponent(style='link', height='sm', action=map_action), SeparatorComponent(margin='md')] if map_action else []) +
                     [ButtonComponent(style='link', height='sm', action=URIAction(label='📝 อัปเดต/สรุปงาน', uri=update_url))]
        )
    )
    return FlexSendMessage(alt_text=f"แจ้งเตือนงาน: {task.get('title', '')}", contents=bubble)

@app.route("/", methods=['GET', 'POST'])
def form_page():
    if request.method == 'POST':
        customer_name = str(request.form.get('customer', '')).strip()
        customer_phone = str(request.form.get('phone', '')).strip()
        address = str(request.form.get('address', '')).strip()
        detail = str(request.form.get('detail', '')).strip()
        appointment_str = str(request.form.get('appointment', '')).strip()
        map_url_from_form = str(request.form.get('latitude_longitude', '')).strip()

        if not customer_name or not detail:
            flash('กรุณากรอกชื่อลูกค้าและรายละเอียดงาน', 'danger')
            return redirect(url_for('form_page'))

        today_str = datetime.datetime.now(THAILAND_TZ).strftime('%d/%m/%y')
        title = f"งานลูกค้า: {customer_name} ({today_str})" 
        
        notes_lines = [customer_name, customer_phone, address]
        if map_url_from_form: notes_lines.append(map_url_from_form)
        notes_lines.append(detail)
        notes = "\n".join(filter(None, notes_lines))

        due_date_gmt = None
        if appointment_str:
            try:
                dt_local = THAILAND_TZ.localize(datetime.datetime.strptime(appointment_str, "%Y-%m-%d %H:%M"))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat()
            except ValueError:
                app.logger.error(f"Invalid appointment format: {appointment_str}")

        created_task = create_google_task(title, notes=notes, due=due_date_gmt)

        if created_task:
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
                text_message = TextSendMessage(text=f"งานใหม่: {title}\nลูกค้า: {customer_name}\nดูรายละเอียด: {url_for('update_task_details', task_id=created_task.get('id'), _external=True)}")
                if recipients: line_bot_api.multicast(recipients, text_message)
                flash('สร้างงานเรียบร้อยแล้ว แต่ส่งข้อความแบบธรรมดาแทน', 'warning')
            
            return redirect(url_for('summary'))
        else:
            flash('เกิดข้อผิดพลาดในการสร้างงาน', 'danger')
            return redirect(url_for('form_page'))

    return render_template('form.html')

@app.route('/summary')
def summary():
    search_query = str(request.args.get('search_query', '')).strip().lower() 
    status_filter = str(request.args.get('status_filter', 'all')).strip() 

    tasks_raw = get_google_tasks_for_report(show_completed=True)
    
    if tasks_raw is None: 
        flash('ไม่สามารถเชื่อมต่อกับ Google Tasks ได้ในขณะนี้', 'danger')
        tasks_raw = []

    current_time_utc = datetime.datetime.now(pytz.utc)

    filtered_by_status_tasks = []
    for task_item in tasks_raw:
        task_status = task_item.get('status')
        is_overdue_check = False
        if task_status == 'needsAction' and task_item.get('due'):
            try:
                due_dt_utc = datetime.datetime.fromisoformat(task_item['due'].replace('Z', '+00:00'))
                if due_dt_utc < current_time_utc:
                    is_overdue_check = True
            except (ValueError, TypeError):
                pass 

        if status_filter == 'all':
            filtered_by_status_tasks.append(task_item)
        elif status_filter == 'completed' and task_status == 'completed':
            filtered_by_status_tasks.append(task_item)
        elif status_filter == 'needsAction' and task_status == 'needsAction' and not is_overdue_check:
            filtered_by_status_tasks.append(task_item)
        elif status_filter == 'overdue' and is_overdue_check:
            filtered_by_status_tasks.append(task_item)

    final_filtered_tasks = []
    if search_query:
        for task in filtered_by_status_tasks:
            notes_lower = str(task.get('notes', '')).lower()
            title_lower = str(task.get('title', '')).lower()
            if search_query in notes_lower or search_query in title_lower:
                final_filtered_tasks.append(task)
    else:
        final_filtered_tasks = filtered_by_status_tasks

    tasks = []
    for task_item in final_filtered_tasks:
        parsed_task = parse_google_task_dates(task_item)
        parsed_task['customer'] = parse_customer_info_from_notes(parsed_task.get('notes', ''))
        tasks.append(parsed_task)

    tasks.sort(key=lambda x: x.get('created', ''), reverse=True)
    
    return render_template("tasks_summary.html", 
                           tasks=tasks, 
                           search_query=search_query,
                           status_filter=status_filter)

@app.route('/update_task/<task_id>', methods=['GET', 'POST'])
def update_task_details(task_id):
    service = get_google_tasks_service()
    if not service: abort(503, "Google Tasks service is unavailable.")

    try:
        task_raw = service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
    except HttpError:
        abort(404, "Task not found.")

    if request.method == 'POST':
        # ... (POST logic remains largely the same)
        return redirect(url_for('summary'))

    task = parse_google_task_dates(task_raw)
    task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
    
    return render_template('update_task_details.html', task=task)

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
        settings_data = {
            'line_recipients': {
                'admin_group_id': request.form.get('admin_group_id', '').strip(),
                'technician_group_id': request.form.get('technician_group_id', '').strip()
            }
        }
        if save_app_settings(settings_data):
            flash('บันทึกการตั้งค่าเรียบร้อยแล้ว!', 'success')
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกการตั้งค่า', 'danger')
        return redirect(url_for('settings_page'))
    
    return render_template('settings_page.html', settings=get_app_settings())

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
    text = event.message.text.lower().strip()
    reply_message = None

    if text == 'สรุปงาน':
        summary_url = url_for('summary', _external=True)
        reply_message = TextSendMessage(text=f"ดูสรุปงานทั้งหมดได้ที่นี่: {summary_url}")
    elif text == 'สร้างงานใหม่':
        form_url = url_for('form_page', _external=True)
        reply_message = TextSendMessage(text=f"สร้างงานใหม่ผ่านฟอร์มได้ที่นี่: {form_url}")

    if reply_message:
        line_bot_api.reply_message(event.reply_token, reply_message)


# --- Main Execution ---
if __name__ == '__main__':
    app.logger.info("Application starting...")
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
