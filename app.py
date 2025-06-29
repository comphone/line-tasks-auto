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

# --- การแก้ไข: รวมและแก้ไขการ import จาก line-bot-sdk v3 ทั้งหมด ---
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import (
    WebhookHandler, MessageEvent, TextMessageContent, PostbackEvent,
    ImageMessageContent, FileMessageContent, GroupSource, UserSource
)
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, PushMessageRequest,
    ReplyMessageRequest, FlexMessage, TextMessage, BubbleContainer,
    CarouselContainer, BoxComponent, TextComponent, ButtonComponent,
    SeparatorComponent, URIAction, PostbackAction, QuickReply, QuickReplyButton
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

# Initialize LINE Bot SDK
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_api_client = ApiClient(configuration)
line_messaging_api = MessagingApi(line_api_client)
# การแก้ไข: ใช้ WebhookHandler ที่ import มาอย่างถูกต้องแล้ว
handler = WebhookHandler(LINE_CHANNEL_SECRET)

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
    """Retrieves app settings, preferring from file, then defaults.
       Ensures common_equipment_items is always derived from equipment_catalog.
    """
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
                    app.logger.warning(f"equipment_catalog in settings.json is not a list. Resetting to default. Current type: {type(loaded_from_file.get(key))}")
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
# [END app_settings_json_persistence_final]


# --- Google API Helper Functions ---
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
    """Gets the Google Tasks service object."""
    return get_google_service('tasks', 'v1')

def get_google_calendar_service():
    """Gets the Google Calendar service object."""
    return get_google_service('calendar', 'v3')

def get_google_drive_service():
    """Gets the Google Drive service object."""
    return get_google_service('drive', 'v3')

def upload_file_to_google_drive(file_path, file_name, mime_type):
    """
    Uploads a file to Google Drive and makes it publicly accessible.
    Returns the web view link if successful, otherwise None.
    """
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

# --- Task and Event Creation Functions ---
def create_google_task(title, notes=None, due=None):
    """Creates a new task in Google Tasks."""
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
    """Creates a new event in Google Calendar."""
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
    """Deletes a task from Google Tasks."""
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
    """Helper to update a specific task."""
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
                elif 'due' not in task:
                     app.logger.warning(f"Task {task_id} is needsAction but no due date set. Consider setting one.")

        return service.tasks().update(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id, body=task).execute()
    except HttpError as e:
        app.logger.error(f"Failed to update task {task_id}: {e}")
        return None

# --- Data Parsing and Utility Functions ---
@cached(cache)
def get_google_tasks_for_report(show_completed=True):
    """Fetches tasks from Google Tasks API, with caching."""
    app.logger.info(f"Cache miss/expired. Calling Google Tasks API... (show_completed={show_completed})")
    service = get_google_tasks_service()
    if not service: return None
    try:
        results = service.tasks().list(
            tasklist=GOOGLE_TASKS_LIST_ID, showCompleted=show_completed, maxResults=100
        ).execute()
        return results.get('items', [])
    except HttpError as err:
        app.logger.error(f"API Error getting tasks: {err}")
        return None

def get_single_task(task_id):
    """Fetches a single task by its ID."""
    service = get_google_tasks_service()
    if not service: return None
    try:
        result = service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
        return result
    except HttpError as err:
        app.logger.error(f"Error getting single task {task_id}: {err}")
        return None

def get_upcoming_events(time_delta_hours=24):
    """Fetches upcoming Google Calendar events."""
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
    """Extracts latitude and longitude from task notes. Also handles Google Maps URLs."""
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
    """Finds nearby pending jobs based on a completed task's location."""
    completed_task = get_single_task(completed_task_id)
    if not completed_task: return []

    origin_lat, origin_lon = extract_lat_lon_from_notes(completed_task.get('notes', ''))
    if origin_lat is None or origin_lon is None:
        app.logger.info(f"Completed task {completed_task_id} has no location data. Skipping nearby search.")
        return []
    origin_coords = (origin_lat, origin_lon)

    pending_tasks = get_google_tasks_for_report(show_completed=False)
    if not pending_tasks: return []

    nearby_jobs = []
    for task in pending_tasks:
        if task.get('id') == completed_task_id: continue
        task_lat, task_lon = extract_lat_lon_from_notes(task.get('notes', ''))
        if task_lat is not None and task_lon is not None:
            task_coords = (task_lat, task_lon)
            distance = geodesic(origin_coords, task_coords).kilometers
            if distance <= radius_km:
                task['distance_km'] = round(distance, 1)
                nearby_jobs.append(task)

    nearby_jobs.sort(key=lambda x: x['distance_km'])
    return nearby_jobs

def parse_customer_info_from_notes(notes):
    """
    Extracts customer name, phone, address, detail, and map_url from notes
    based on line-by-line parsing.
    Returns empty string "" for missing fields.
    """
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
    """Formats dates from a Google Task item."""
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

def parse_tech_report_from_notes(notes):
    """Extracts all past technician reports into a history list and the original notes text."""
    if not notes: return [], ""
    report_blocks = re.findall(r"--- TECH_REPORT_START ---\s*\n(.*?)\n--- TECH_REPORT_END ---", notes, re.DOTALL)
    history = []
    for json_str in report_blocks:
        try:
            report_data = json.loads(json_str)
            if isinstance(report_data.get('equipment_used'), str):
                report_data['equipment_used_display'] = report_data['equipment_used'].replace('\n', '<br>')
            else:
                report_data['equipment_used_display'] = _format_equipment_list(
                    report_data.get('equipment_used', [])
                )
            history.append(report_data)
        except json.JSONDecodeError as e:
            app.logger.error(f"Error decoding JSON in tech report block: {e}, Content: {json_str[:100]}...")

    original_notes_text = re.sub(r"--- TECH_REPORT_START ---\s*.*?\s*--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL).strip()

    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, original_notes_text

def allowed_file(filename):
    """Checks for allowed file extensions."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def generate_qr_code_base64(url, box_size=10, border=4, fill_color="black", back_color="white"):
    """
    Generates a QR code for the given URL and returns it as a Base64 encoded string.
    Configurable: box_size, border, fill_color, back_color.
    """
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=box_size,
            border=border,
        )
        qr.add_data(url)
        qr.make(fit=True)

        img = qr.make_image(fill_color=fill_color, back_color=back_color)

        buffer = BytesIO()
        img.save(buffer, format="PNG")

        base64_img = base64.b64encode(buffer.getvalue()).decode("utf-8")
        return f"data:image/png;base64,{base64_img}"
    except Exception as e:
        app.logger.error(f"Error generating QR code for URL {url}: {e}")
        return None

def _parse_equipment_string(text_input):
    """
    Parses equipment string like "Item, Quantity Unit\nItem2, Qty2"
    into a list of dicts: [{"item": "Item", "quantity": "Quantity Unit"}].
    Also updates common_equipment_items in settings based on parsed item_names.
    """
    equipment_list = []
    current_settings = get_app_settings()
    common_items_set = set(current_settings.get('common_equipment_items', []))

    if not text_input:
        return equipment_list

    lines = text_input.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue

        parts = line.split(',', 1)
        item_name = parts[0].strip()
        quantity = parts[1].strip() if len(parts) > 1 else ''

        if item_name:
            equipment_list.append({"item": item_name, "quantity": quantity})
            if item_name not in common_items_set:
                common_items_set.add(item_name)

    _APP_SETTINGS_STORE['common_equipment_items'] = sorted(list(common_items_set))

    return equipment_list

def _format_equipment_list(equipment_data):
    """
    Formats equipment data (list of dicts or old string) into a multi-line string for display.
    """
    if not equipment_data:
        return 'N/A'

    formatted_lines = []
    if isinstance(equipment_data, list):
        for eq_item in equipment_data:
            if isinstance(eq_item, dict) and "item" in eq_item:
                line = eq_item['item']
                if eq_item.get("quantity"):
                    line += f", {eq_item['quantity']}"
                formatted_lines.append(line)
            elif isinstance(eq_item, str):
                formatted_lines.append(eq_item)
    elif isinstance(equipment_data, str):
        return equipment_data

    if not formatted_lines:
        return 'N/A'

    return "\n".join(formatted_lines)


# --- Flex Message Creation Functions ---
def create_task_flex_message(task):
    """
    Creates a LINE Flex Message for a given task.
    Includes robust checks for URIAction validity.
    """
    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    parsed_dates = parse_google_task_dates(task)
    update_url = url_for('update_task_details', task_id=task.get('id'), _external=True)

    # --- Robust Phone Action Creation ---
    phone_action = None
    phone_display_text = str(customer_info.get('phone', '')).strip()
    phone_number_cleaned = re.sub(r'\D', '', phone_display_text)

    if phone_number_cleaned and phone_display_text:
        full_phone_uri = f"tel:{phone_number_cleaned}"
        try:
            phone_action = URIAction(label=phone_display_text, uri=full_phone_uri)
        except Exception as e:
            app.logger.error(f"Error creating phone_action URIAction for '{full_phone_uri}': {e}")
            phone_action = None
    else:
        app.logger.debug(f"Skipping phone_action: phone_display_text='{phone_display_text}', phone_number_cleaned='{phone_number_cleaned}'")


    # --- Robust Map Action Creation ---
    map_action = None
    map_url = str(customer_info.get('map_url', '')).strip()

    if map_url and (map_url.startswith('http://') or map_url.startswith('https://')):
        try:
            map_action = URIAction(label="📍 เปิด Google Maps", uri=map_url)
        except Exception as e:
            app.logger.error(f"Error creating map_action URIAction for '{map_url}': {e}")
            map_action = None
    else:
        if map_url:
            app.logger.debug(f"Invalid map URL format for URIAction: '{map_url}'")


    # --- Body Contents Construction with explicit string casting and new highlight style ---
    body_contents = [
        # Highlighted Title
        TextComponent(text=str(task.get('title', 'ไม่มีหัวข้อ')), weight='bold', size='xl', wrap=True),
        SeparatorComponent(margin='md'),

        BoxComponent(layout='vertical', margin='lg', spacing='sm', contents=[
            # Customer Name
            BoxComponent(layout='baseline', spacing='sm', contents=[
                TextComponent(text='ลูกค้า:', color='#007BFF', size='sm', flex=2, weight='bold'),
                TextComponent(text=str(customer_info.get('name', '') or '-'), wrap=True, color='#666666', size='sm', flex=5)
            ]),
            # Phone Number
            BoxComponent(layout='baseline', spacing='sm', contents=[
                TextComponent(text='โทร:', color='#007BFF', size='sm', flex=2, weight='bold'),
                TextComponent(text=str(phone_display_text or '-'), wrap=True, color='#1E90FF', size='sm', flex=5, action=phone_action if phone_action else None, decoration='underline' if phone_action else 'none')
            ]),
            # Appointment Date
            BoxComponent(layout='baseline', spacing='sm', contents=[
                TextComponent(text='นัดหมาย:', color='#007BFF', size='sm', flex=2, weight='bold'),
                TextComponent(text=str(parsed_dates.get('due_formatted', '') or '-'), wrap=True, color='#666666', size='sm', flex=5)
            ])
        ]),
        SeparatorComponent(margin='md'),

        # Detail Section - potentially multi-line
        TextComponent(text='รายละเอียดงาน:', weight='bold', color='#007BFF', size='sm', margin='md'),
        TextComponent(text=str(customer_info.get('detail', '') or '-'), wrap=True, margin='sm', color='#666666')
    ]

    # --- Footer Contents Construction ---
    footer_contents_list = []
    if map_action:
        footer_contents_list.append(ButtonComponent(style='link', height='sm', action=map_action))
        footer_contents_list.append(SeparatorComponent(margin='md'))

    footer_contents_list.append(ButtonComponent(style='link', height='sm', action=URIAction(label='📝 อัปเดต/สรุปงาน', uri=update_url)))

    # --- Bubble Container and Flex Message Creation ---
    bubble = BubbleContainer(direction='ltr',
        header=BoxComponent(layout='vertical', contents=[TextComponent(text='📢 แจ้งเตือนงาน', weight='bold', color='#ffffff')], background_color='#007BFF', padding_all='12px'),
        body=BoxComponent(layout='vertical', contents=body_contents),
        footer=BoxComponent(layout='vertical', spacing='sm', contents=footer_contents_list, flex=0),
        action=URIAction(uri=update_url)
    )
    return FlexMessage(alt_text=f"แจ้งเตือนงาน: {task.get('title', '')}", contents=bubble)

def create_nearby_job_suggestion_message(completed_task_title, nearby_tasks):
    """Creates a Flex Message Carousel for nearby job suggestions."""
    if not nearby_tasks: return None

    bubbles = []
    for task in nearby_tasks[:12]:
        customer_info = parse_customer_info_from_notes(task.get('notes', ''))
        update_url = url_for('update_task_details', task_id=task.get('id'), _external=True)

        phone_action = None
        phone_display_text = str(customer_info.get('phone', '')).strip()
        phone_number_cleaned = re.sub(r'\D', '', phone_display_text)
        if phone_number_cleaned and phone_display_text and re.match(r'^\d+$', phone_number_cleaned):
            full_phone_uri = f"tel:{phone_number_cleaned}"
            try:
                phone_action = URIAction(label=f"📞 โทร: {phone_display_text}", uri=full_phone_uri)
            except Exception as e:
                app.logger.error(f"Error creating phone_action for nearby job: {e}")
                phone_action = None

        bubble = BubbleContainer(direction='ltr',
            header=BoxComponent(layout='vertical', background_color='#FFDDC2', contents=[TextComponent(text='💡 แนะนำงานใกล้เคียง!', weight='bold', color='#BF5A00', size='md')]),
            body=BoxComponent(layout='vertical', spacing='md', contents=[
                TextComponent(text=f"ห่างไป {task['distance_km']} กม.", size='sm', color='#555555'),
                TextComponent(text=str(customer_info.get('name', '') or 'N/A'), weight='bold', size='lg', wrap=True),
                TextComponent(text=str(task.get('title', '-')), wrap=True, size='sm', color='#666666')
            ]),
            footer=BoxComponent(layout='vertical', spacing='sm', contents=([phone_action] if phone_action else []) + [ButtonComponent(style='link', height='sm', action=URIAction(label='ดูรายละเอียด/แผนที่', uri=update_url))])
        )
        bubbles.append(bubble)

    alt_text = f"คุณอยู่ใกล้กับงานอื่น! หลังจากปิดงาน '{completed_task_title}'"
    return FlexMessage(alt_text=alt_text, contents=CarouselContainer(contents=bubbles))

def create_customer_history_carousel(tasks, customer_name):
    """Creates a Flex Message Carousel Container for a customer's task history."""
    if not tasks: return None

    bubbles = []
    for task in tasks[:12]:
        parsed = parse_google_task_dates(task)
        update_url = url_for('update_task_details', task_id=task.get('id'), _external=True)

        status_text, status_color = "รอดำเนินการ", "#FFA500"
        if task.get('status') == 'completed':
            status_text, status_color = "เสร็จสิ้น", "#28A745"
        elif 'due' in task and task['due']:
            try:
                due_dt_utc = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                if due_dt_utc < datetime.datetime.now(pytz.utc):
                    status_text, status_color = "ยังไม่ดำเนินการ", "#DC3545"
            except (ValueError, TypeError): pass

        bubble = BubbleContainer(direction='ltr',
            body=BoxComponent(layout='vertical', spacing='md', contents=[
                TextComponent(text=str(task.get('title', 'N/A')), weight='bold', size='lg', wrap=True),
                SeparatorComponent(margin='md'),
                BoxComponent(layout='vertical', spacing='sm', contents=[
                    BoxComponent(layout='baseline', spacing='sm', contents=[
                        TextComponent(text='สถานะ', color='#aaaaaa', size='sm', flex=2),
                        TextComponent(text=status_text, wrap=True, color=status_color, size='sm', flex=5, weight='bold')
                    ]),
                    BoxComponent(layout='baseline', spacing='sm', contents=[
                        TextComponent(text='วันที่สร้าง', color='#aaaaaa', size='sm', flex=2),
                        TextComponent(text=str(parsed.get('created_formatted', '') or '-'), wrap=True, color='#666666', size='sm', flex=5)
                    ])
                ])
            ]),
            footer=BoxComponent(layout='vertical', spacing='sm', contents=[ButtonComponent(style='link', height='sm', action=URIAction(label='ดูรายละเอียด / อัปเดต', uri=update_url))])
        )
        bubbles.append(bubble)

    return CarouselContainer(contents=bubbles)

# --- Web Page Routes ---

@app.route("/", methods=['GET', 'POST'])
def form_page():
    if request.method == 'POST':
        customer_name = str(request.form.get('customer', '')).strip()
        customer_phone = str(request.form.get('phone', '')).strip()
        address = str(request.form.get('address', '')).strip()
        detail = str(request.form.get('detail', '')).strip()
        appointment_str = str(request.form.get('appointment', '')).strip()
        map_url_from_form = str(request.form.get('latitude_longitude', '')).strip()

        if not customer_name:
            flash('กรุณากรอกชื่อลูกค้า', 'danger')
            return redirect(url_for('form_page'))
        if not detail:
            flash('กรุณากรอกรายละเอียดงาน', 'danger')
            return redirect(url_for('form_page'))


        today_str = datetime.datetime.now(THAILAND_TZ).strftime('%d/%m/%y')
        title = f"งานลูกค้า: {customer_name or 'ไม่ระบุชื่อลูกค้า'} ({today_str})"

        notes_lines = []
        notes_lines.append(customer_name or '')
        notes_lines.append(customer_phone or '')
        notes_lines.append(address or '')

        if map_url_from_form:
            notes_lines.append(map_url_from_form)

        if detail:
            notes_lines.extend(detail.split('\n'))

        while notes_lines and notes_lines[-1] == '':
            notes_lines.pop()

        notes = "\n".join(notes_lines)

        due_date_gmt, start_time_iso, end_time_iso = None, None, None
        if appointment_str:
            try:
                dt_local = THAILAND_TZ.localize(datetime.datetime.strptime(appointment_str, "%Y-%m-%d %H:%M"))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat()
                start_time_iso = dt_local.isoformat()
                end_time_iso = (dt_local + datetime.timedelta(hours=1)).isoformat()
            except ValueError:
                app.logger.error(f"Invalid appointment format: {appointment_str}")

        created_task = create_google_task(title, notes=notes, due=due_date_gmt)

        if created_task:
            flex_message = None
            try:
                flex_message = create_task_flex_message(created_task)
            except Exception as e:
                app.logger.error(f"Failed to create Flex Message for new task {created_task.get('id')}: {e}")
                flash('สร้างงานเรียบร้อยแล้ว แต่ไม่สามารถส่ง Flex Message ได้เนื่องจากข้อผิดพลาดภายในระบบ.', 'warning')

            settings = get_app_settings()
            recipients = [id for id in [settings['line_recipients'].get('admin_group_id'), settings['line_recipients'].get('technician_group_id')] if id]

            messages_to_send_line = []
            if flex_message:
                messages_to_send_line.append(flex_message)
            else:
                messages_to_send_line.append(TextMessage(text=f"งานใหม่ถูกสร้างแล้ว:\nหัวข้อ: {title}\nลูกค้า: {customer_name or '-'}\nเบอร์โทร: {customer_phone or '-'}\nรายละเอียด: {detail or '-'} \n\nดูรายละเอียด/อัปเดต: {url_for('update_task_details', task_id=created_task.get('id'), _external=True)}"))
                flash('สร้างงานเรียบร้อยแล้ว, ส่งข้อความแจ้งเตือน LINE แบบข้อความธรรมดาแทน.', 'info')


            if recipients and messages_to_send_line:
                try:
                    for recipient_id in recipients:
                         line_messaging_api.push_message(PushMessageRequest(to=recipient_id, messages=messages_to_send_line))
                    if flex_message:
                        flash('สร้างงานและส่งแจ้งเตือน LINE เรียบร้อยแล้ว!', 'success')
                except Exception as e:
                    app.logger.error(f"Failed to push message to LINE: {e}")
                    flash('สร้างงานเรียบร้อยแล้ว แต่ไม่สามารถส่งแจ้งเตือน LINE ได้.', 'warning')
            else:
                 flash('สร้างงานเรียบร้อยแล้ว (ไม่มีผู้รับแจ้งเตือน LINE).', 'info')

            return redirect(url_for('summary'))
        else:
            flash('เกิดข้อผิดพลาดในการสร้างงาน', 'danger')
            return redirect(url_for('form_page'))

    return render_template('form.html')

# New Endpoint to lookup customer data
@app.route("/lookup_customer", methods=['GET'])
def lookup_customer():
    customer_name_query = str(request.args.get('customer_name', '')).strip().lower()

    if not customer_name_query:
        return jsonify({})

    tasks_raw = get_google_tasks_for_report(show_completed=False)

    if tasks_raw is None:
        return jsonify({"error": "Failed to retrieve tasks from Google API"}), 500

    found_customer_info = {}
    for task_item in reversed(tasks_raw):
        customer_info = parse_customer_info_from_notes(task_item.get('notes', ''))

        if customer_name_query in str(customer_info.get('name', '')).strip().lower():
            if customer_info.get('phone'):
                found_customer_info['phone'] = str(customer_info['phone']).strip()
            if customer_info.get('address'):
                found_customer_info['address'] = str(customer_info['address']).strip()
            if customer_info.get('detail'):
                found_customer_info['detail'] = str(customer_info['detail']).strip()
            if customer_info.get('map_url'):
                found_customer_info['map_url'] = str(customer_info['map_url']).strip()

            if all(key in found_customer_info and found_customer_info[key] for key in ['phone', 'address', 'detail']):
                break

    return jsonify(found_customer_info)

# [START lookup_equipment_route_by_name]
@app.route("/lookup_equipment", methods=['GET'])
def lookup_equipment():
    query = request.args.get('q', '').strip().lower()
    if not query:
        return jsonify([])

    current_settings = get_app_settings()
    equipment_catalog = current_settings.get('equipment_catalog', [])

    results = []
    for item in equipment_catalog:
        item_name = str(item.get('item_name', '')).strip().lower()
        barcode = str(item.get('barcode', '')).strip().lower()

        if query in item_name:
            results.append({
                'item_name': item.get('item_name', ''),
                'unit': item.get('unit', ''),
                'price': item.get('price', 0.0),
                'barcode': item.get('barcode', '')
            })
        elif barcode and query in barcode:
            results.append({
                'item_name': item.get('item_name', ''),
                'unit': item.get('unit', ''),
                'price': item.get('price', 0.0),
                'barcode': item.get('barcode', '')
            })

    results.sort(key=lambda x: (
        not str(x['item_name']).lower().startswith(query),
        str(x['item_name']).lower()
    ))

    return jsonify(results[:10])
# [END lookup_equipment_route_by_name]


@app.route('/summary')
def summary():
    """Displays the task summary page with search functionality and status filtering. - Modified from app2.py"""
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
    for task in filtered_by_status_tasks:
        task_title_lower = str(task.get('title', '')).lower()
        customer_name_lower = str(parse_customer_info_from_notes(task.get('notes', '')).get('name', '')).lower()
        customer_phone_lower = str(parse_customer_info_from_notes(task.get('notes', '')).get('phone', '')).lower()
        customer_address_lower = str(parse_customer_info_from_notes(task.get('notes', '')).get('address', '')).lower()
        customer_detail_lower = str(parse_customer_info_from_notes(task.get('notes', '')).get('detail', '')).lower()

        if not search_query or \
           search_query in task_title_lower or \
           search_query in customer_name_lower or \
           search_query in customer_phone_lower or \
           search_query in customer_address_lower or \
           search_query in customer_detail_lower:
            final_filtered_tasks.append(task)


    tasks = []
    total_summary_stats = {'needsAction': 0, 'completed': 0, 'overdue': 0, 'total': len(tasks_raw)}
    for task_item in tasks_raw:
        task_status = task_item.get('status')
        is_overdue_check = False
        if task_status == 'needsAction' and task_item.get('due'):
            try:
                due_dt_utc = datetime.datetime.fromisoformat(task_item['due'].replace('Z', '+00:00'))
                if due_dt_utc < current_time_utc:
                    is_overdue_check = True
            except (ValueError, TypeError): pass

        if task_status == 'completed':
            total_summary_stats['completed'] += 1
        elif task_status == 'needsAction':
            total_summary_stats['needsAction'] += 1
            if is_overdue_check:
                total_summary_stats['overdue'] += 1


    for task_item in final_filtered_tasks:
        parsed_task = parse_google_task_dates(task_item)
        parsed_task['customer'] = parse_customer_info_from_notes(parsed_task.get('notes', ''))

        history, original_notes_text_removed_tech_reports = parse_tech_report_from_notes(parsed_task.get('notes', ''))
        parsed_task['tech_reports_history'] = history
        parsed_task['notes_display'] = original_notes_text_removed_tech_reports

        status = task_item.get('status')
        if status == 'completed':
            parsed_task['display_status'] = 'เสร็จสิ้น'
        elif status == 'needsAction':
            is_overdue = False
            if task_item.get('due'):
                try:
                    due_dt_utc = datetime.datetime.fromisoformat(task_item['due'].replace('Z', '+00:00'))
                    if due_dt_utc < current_time_utc:
                        is_overdue = True
                except (ValueError, TypeError): pass
            parsed_task['display_status'] = 'ยังไม่ดำเนินการ' if is_overdue else 'รอดำเนินการ'
        tasks.append(parsed_task)

    tasks.sort(key=lambda x: x.get('created', ''), reverse=True)

    return render_template("tasks_summary.html",
                           tasks=tasks,
                           summary=total_summary_stats,
                           search_query=search_query,
                           status_filter=status_filter)

@app.route('/update_task/<task_id>', methods=['GET', 'POST'])
def update_task_details(task_id):
    """Displays and handles updates for a single task, showing history. - Heavily modified from app2.py"""
    service = get_google_tasks_service()
    if not service: abort(503, "Google Tasks service is unavailable.")

    try:
        task_raw = service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
        task = parse_google_task_dates(task_raw)

        # Parse customer info and historical reports
        customer_info_from_task = parse_customer_info_from_notes(task.get('notes', ''))
        history, original_notes_text_removed_tech_reports = parse_tech_report_from_notes(task.get('notes', ''))

        # Populate for GET request with initial values from task notes
        task['customer_name_initial'] = customer_info_from_task.get('name', '')
        task['customer_phone_initial'] = customer_info_from_task.get('phone', '')
        task['customer_address_initial'] = customer_info_from_task.get('address', '')
        task['customer_detail_initial'] = customer_info_from_task.get('detail', '')
        task['map_url_initial'] = customer_info_from_task.get('map_url', '')

        task['tech_reports_history'] = history
        if history and history[0].get('equipment_used') is not None:
            if isinstance(history[0]['equipment_used'], list):
                task['equipment_used_initial'] = _format_equipment_list(history[0]['equipment_used'])
            else:
                task['equipment_used_initial'] = history[0]['equipment_used']
        else:
            task['equipment_used_initial'] = ''

        if history and 'next_appointment' in history[0] and history[0]['next_appointment']:
            try:
                next_app_dt_utc = datetime.datetime.fromisoformat(history[0]['next_appointment'].replace('Z', '+00:00'))
                task['tech_next_appointment_datetime_local'] = next_app_dt_utc.astimezone(THAILAND_TZ).strftime("%Y-%m-%dT%H:%M")
            except (ValueError, TypeError): task['tech_next_appointment_datetime_local'] = ''
        else: task['tech_next_appointment_datetime_local'] = ''

    except HttpError: abort(404, "Task not found.")

    if request.method == 'POST':
        original_status = task.get('status')
        new_status = request.form.get('status')
        work_summary = str(request.form.get('work_summary', '')).strip()
        equipment_used_input = str(request.form.get('equipment_used', '')).strip()
        next_appointment_date_str = str(request.form.get('next_appointment_date', '')).strip()

        parsed_equipment_used = _parse_equipment_string(equipment_used_input)
        save_app_settings({'common_equipment_items': _APP_SETTINGS_STORE['common_equipment_items']})


        updated_customer_name = str(request.form.get('customer_name', '')).strip()
        updated_customer_phone = str(request.form.get('customer_phone', '')).strip()
        updated_address = str(request.form.get('address', '')).strip()
        updated_detail = str(request.form.get('detail', '')).strip()
        updated_map_url = str(request.form.get('latitude_longitude', '')).strip()

        all_attachment_urls = []
        for report in task.get('tech_reports_history', []):
            all_attachment_urls.extend(report.get('attachment_urls', []))

        if 'files[]' in request.files:
            files = request.files.getlist('files[]')
            for file in files:
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    temp_filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    file.save(temp_filepath)

                    mime_type = file.mimetype if file.mimetype else mimetypes.guess_type(filename)[0] or 'application/octet-stream'

                    drive_url = upload_file_to_google_drive(temp_filepath, filename, mime_type)

                    if drive_url:
                        all_attachment_urls.append(drive_url)
                    else:
                        app.logger.error(f"Failed to upload {filename} to Google Drive.")

                    os.remove(temp_filepath)

        all_attachment_urls = list(set(all_attachment_urls))

        next_appointment_gmt = None
        if next_appointment_date_str:
            try:
                dt_local = THAILAND_TZ.localize(datetime.datetime.fromisoformat(next_appointment_date_str))
                next_appointment_gmt = dt_local.astimezone(pytz.utc).isoformat()
            except ValueError: app.logger.error(f"Invalid next appointment date format: {next_appointment_date_str}")

        current_lat = request.form.get('current_lat')
        current_lon = request.form.get('current_lon')
        current_location_url = None
        if current_lat and current_lon:
            current_location_url = f"https://www.google.com/maps/search/?api=1&query={current_lat},{current_lon}"

        new_tech_report_data = {
            'summary_date': datetime.datetime.now(THAILAND_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            'work_summary': work_summary,
            'equipment_used': parsed_equipment_used,
            'next_appointment': next_appointment_gmt,
            'attachment_urls': all_attachment_urls,
            'location_url': current_location_url
        }

        history, _ = parse_tech_report_from_notes(task_raw.get('notes', ''))

        base_notes_lines = []
        base_notes_lines.append(updated_customer_name or '')
        base_notes_lines.append(updated_customer_phone or '')
        base_notes_lines.append(updated_address or '')

        if updated_map_url:
            base_notes_lines.append(updated_map_url)

        if updated_detail:
            base_notes_lines.extend(updated_detail.split('\n'))

        while base_notes_lines and base_notes_lines[-1] == '':
            base_notes_lines.pop()

        updated_base_notes = "\n".join(base_notes_lines)

        all_reports_list = sorted(history + [new_tech_report_data], key=lambda x: x.get('summary_date'))

        all_reports_text = ""
        for report in all_reports_list:
            report_to_dump = report.copy()
            report_to_dump['attachment_urls'] = report_to_dump.get('attachment_urls', [])
            report_to_dump['location_url'] = report_to_dump.get('location_url', None)

            all_reports_text += f"\n\n--- TECH_REPORT_START ---\n{json.dumps(report_to_dump, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"

        final_updated_notes = updated_base_notes + all_reports_text

        new_due_for_main_task = None
        if new_status == 'needsAction' and next_appointment_gmt:
            new_due_for_main_task = next_appointment_gmt
        elif new_status == 'needsAction' and not next_appointment_gmt and task_raw.get('due'):
            new_due_for_main_task = task_raw.get('due')
        elif new_status == 'completed':
            new_due_for_main_task = None


        updated_task = update_google_task(
            task_id,
            title=f"งานลูกค้า: {updated_customer_name or 'ไม่ระบุชื่อลูกค้า'} ({datetime.datetime.now(THAILAND_TZ).strftime('%d/%m/%y')})",
            notes=final_updated_notes,
            status=new_status,
            due=new_due_for_main_task
        )

        if updated_task:
            flash('อัปเดตงานเรียบร้อยแล้ว!', 'success')
            if new_status == 'completed' and original_status != 'completed':
                settings = get_app_settings()
                tech_group_id = settings['line_recipients'].get('technician_group_id')
                if tech_group_id:
                    completed_message = TextMessage(text=f"งาน '{updated_task.get('title', 'N/A')}' ได้รับการทำเสร็จแล้ว! ✅\n\nดูรายละเอียด: {url_for('update_task_details', task_id=task_id, _external=True)}")
                    try:
                        line_messaging_api.push_message(PushMessageRequest(to=tech_group_id, messages=[completed_message]))
                        app.logger.info(f"Sent completion notification for task {task_id} to technician group.")
                    except Exception as e:
                        app.logger.error(f"Failed to send completion notification to LINE: {e}")

                # check_for_nearby_jobs_and_notify(task_id, tech_group_id)
        else:
            flash('เกิดข้อผิดพลาดในการอัปเดตงาน', 'danger')

        return redirect(url_for('summary'))

    public_report_url_for_task = url_for('summary', _external=True, search_query=task.get('id', ''), status_filter='all')

    qr_settings = get_app_settings().get('qrcode_settings', {})

    qr_code_base64_for_task = generate_qr_code_base64(
        public_report_url_for_task,
        box_size=qr_settings.get('box_size', 6),
        border=qr_settings.get('border', 2),
        fill_color=qr_settings.get('fill_color', '#0056b3'),
        back_color=qr_settings.get('back_color', '#FFFFFF')
    )

    return render_template('update_task_details.html',
                           task=task,
                           qr_code_base64=qr_code_base64_for_task,
                           public_report_url=public_report_url_for_task,
                           common_equipment_items=get_app_settings().get('common_equipment_items', []))

@app.route('/delete_task/<task_id>', methods=['POST'])
def delete_task(task_id):
    """จัดการการลบงาน (Task)"""
    if not task_id:
        flash('ไม่พบรหัสงานที่ต้องการลบ', 'danger')
        return redirect(url_for('summary'))

    # เรียกใช้ฟังก์ชันที่มีอยู่แล้วเพื่อลบงานออกจาก Google Tasks
    was_deleted = delete_google_task(task_id)

    if was_deleted:
        flash('ลบงานเรียบร้อยแล้ว!', 'success')
        # ล้างแคชเพื่อให้หน้า summary แสดงข้อมูลล่าสุด
        cache.clear()
    else:
        flash('เกิดข้อผิดพลาดในการลบงาน', 'danger')

    return redirect(url_for('summary'))

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Serves uploaded files. (Primarily for local temp storage / legacy direct links)"""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    """Handles the settings page display and form submission."""
    if request.method == 'POST':
        settings_data = {
            'report_times': {
                'appointment_reminder_hour_thai': int(request.form.get('appointment_reminder_hour')),
                'outstanding_report_hour_thai': int(request.form.get('outstanding_report_hour'))
            },
            'line_recipients': {
                'admin_group_id': request.form.get('admin_group_id', '').strip(),
                'manager_user_id': request.form.get('manager_user_id', '').strip(),
                'technician_group_id': request.form.get('technician_group_id', '').strip()
            },
            'qrcode_settings': {
                'box_size': int(request.form.get('qr_box_size', 8)),
                'border': int(request.form.get('qr_border', 4)),
                'fill_color': request.form.get('qr_fill_color', '#28a745'),
                'back_color': request.form.get('qr_back_color', '#FFFFFF'),
                'custom_url': request.form.get('qr_custom_url', '').strip()
            }
        }
        # [START import_export_equipment_settings_save_updated]
        # This part of logic is within the POST handling for settings_page

        # Now handle equipment_catalog. It's only updated by explicit import, not via general form submit.
        # So we preserve existing equipment_catalog unless an excel_file is uploaded.
        current_settings_on_post = get_app_settings()
        settings_data['equipment_catalog'] = current_settings_on_post.get('equipment_catalog', [])

        if save_app_settings(settings_data):
            flash('บันทึกการตั้งค่าเรียบร้อยแล้ว!', 'success')
            cache.clear()
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกการตั้งค่า', 'danger')
        return redirect(url_for('settings_page'))

    current_settings = get_app_settings()

    general_summary_url = url_for('summary', _external=True)

    qr_url_to_use = current_settings.get('qrcode_settings', {}).get('custom_url', '').strip()
    if not qr_url_to_use:
        qr_url_to_use = general_summary_url

    qr_settings = current_settings.get('qrcode_settings', {})
    qr_code_base64_general = generate_qr_code_base64(
        qr_url_to_use,
        box_size=qr_settings.get('box_size', 8),
        border=qr_settings.get('border', 4),
        fill_color=qr_settings.get('fill_color', '#28a745'),
        back_color=qr_settings.get('back_color', '#FFFFFF')
    )

    # Format common_equipment_items for display in textarea
    common_equipment_list_for_display = "\n".join(current_settings.get('common_equipment_items', []))

    return render_template('settings_page.html',
                           settings=current_settings,
                           qr_code_base64_general=qr_code_base64_general,
                           general_summary_url=general_summary_url,
                           common_equipment_list_for_display=common_equipment_list_for_display)

# [START export_equipment_catalog_route]
@app.route('/export_equipment_catalog', methods=['GET'])
def export_equipment_catalog():
    """Exports equipment catalog to an Excel file template."""
    try:
        current_settings = get_app_settings()
        equipment_catalog = current_settings.get('equipment_catalog', [])

        columns = ['รหัสสินค้า/barcodeสินค้า', 'รายการสินค้า', 'หน่วย', 'ราคา']

        data_for_df = []
        for item in equipment_catalog:
            data_for_df.append({
                'รหัสสินค้า/barcodeสินค้า': item.get('barcode', ''),
                'รายการสินค้า': item.get('item_name', ''),
                'หน่วย': item.get('unit', ''),
                'ราคา': item.get('price', 0.0)
            })

        df = pd.DataFrame(data_for_df, columns=columns)

        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Equipment Catalog')
        output.seek(0)

        return Response(
            output.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment;filename=equipment_catalog_template.xlsx"}
        )
    except Exception as e:
        app.logger.error(f"Error exporting equipment catalog: {e}")
        flash(f"เกิดข้อผิดพลาดในการส่งออกแคตตาล็อกอุปกรณ์: {e}", 'danger')
        return redirect(url_for('settings_page'))
# [END export_equipment_catalog_route]

# [START import_equipment_catalog_route]
@app.route('/import_equipment_catalog', methods=['POST'])
def import_equipment_catalog():
    """Imports equipment catalog from an Excel file."""
    if 'excel_file' not in request.files:
        flash('ไม่พบไฟล์ที่อัปโหลด', 'danger')
        return redirect(url_for('settings_page'))

    file = request.files['excel_file']
    if file.filename == '':
        flash('กรุณาเลือกไฟล์', 'danger')
        return redirect(url_for('settings_page'))

    if file and allowed_file(file.filename) and file.filename.endswith(('.xls', '.xlsx')):
        try:
            df = pd.read_excel(file.stream)

            df.columns = [col.strip().lower() for col in df.columns]

            expected_cols_map = {
                'รหัสสินค้า/barcodeสินค้า': 'barcode',
                'รายการสินค้า': 'item_name',
                'หน่วย': 'unit',
                'ราคา': 'price'
            }
            df_cols_lower = [c.lower() for c in df.columns]
            missing_cols = [th_name for th_name, en_name in expected_cols_map.items() if en_name.lower() not in df_cols_lower]
            if missing_cols:
                flash(f'ไฟล์ Excel ต้องมีคอลัมน์ที่จำเป็น: {", ".join(missing_cols)}', 'danger')
                return redirect(url_for('settings_page'))

            new_catalog_items = []
            for index, row in df.iterrows():
                item = {}
                for th_col, en_col in expected_cols_map.items():
                    actual_col_name = next((c for c in df.columns if c.lower() == en_col.lower()), None)
                    value = row.get(actual_col_name, '')
                    item[en_col] = str(value).strip() if pd.notna(value) else ''

                try:
                    price_val = item.get('price', 0.0)
                    item['price'] = float(price_val)
                except (ValueError, TypeError):
                    item['price'] = 0.0

                if item['item_name']:
                    new_catalog_items.append(item)

            current_settings_for_import = get_app_settings().copy()
            current_settings_for_import['equipment_catalog'] = new_catalog_items

            if save_app_settings(current_settings_for_import):
                flash('นำเข้าแคตตาล็อกอุปกรณ์เรียบร้อยแล้ว!', 'success')
                cache.clear()
            else:
                flash('เกิดข้อผิดพลาดในการบันทึกแคตตาล็อกอุปกรณ์ที่นำเข้า', 'danger')

        except Exception as e:
            app.logger.error(f"Error importing equipment catalog: {e}")
            flash(f"เกิดข้อผิดพลาดในการนำเข้าไฟล์ Excel: {e}. โปรดตรวจสอบรูปแบบไฟล์.", 'danger')
    else:
        flash('รองรับเฉพาะไฟล์ Excel (.xls, .xlsx) เท่านั้นสำหรับการนำเข้า', 'danger')

    return redirect(url_for('settings_page'))
# [END import_equipment_catalog_route]

@app.route("/callback", methods=['POST'])
def callback():
    """
    รับ Event ที่ส่งมาจาก LINE Platform ผ่าน Webhook
    """
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid signature. Please check your channel secret.")
        abort(400)
    except Exception as e:
        app.logger.error(f"Error handling webhook: {e}")
        abort(500)

    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """
    จัดการข้อความที่ผู้ใช้ส่งมาในห้องแชท
    """
    text = event.message.text.lower().strip()

    if text == 'สรุปงาน':
        summary_url = url_for('summary', _external=True)
        reply_text = f"ดูสรุปงานทั้งหมดได้ที่นี่: {summary_url}"
        line_messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )
    elif text == 'สร้างงานใหม่':
        form_url = url_for('form_page', _external=True)
        reply_text = f"สร้างงานใหม่ผ่านฟอร์มได้ที่นี่: {form_url}"
        line_messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

# --- Main Execution ---
if __name__ == '__main__':
    app.logger.info("Application starting...")
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
