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
    ImageComponent, PostbackEvent
)
# ---------------------------------------------

# --- Google API Imports ---
from google.oauth2 import service_account
from google.auth.transport.requests import Request 
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload 
from googleapiclient.http import MediaIoBaseUpload 

import pandas as pd 

# --- APScheduler for background tasks ---
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit

# --- Initialization & Configurations ---
app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_dev')
UPLOAD_FOLDER = 'static/uploads' 
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# --- LINE & Google Configs ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    sys.exit("LINE Bot credentials are not set in environment variables.")

LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM') 
LINE_ADMIN_GROUP_ID = os.environ.get('LINE_ADMIN_GROUP_ID')
# GOOGLE_TASKS_LIST_ID is now managed within settings.json
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')
GOOGLE_SETTINGS_BACKUP_FOLDER_ID = os.environ.get('GOOGLE_SETTINGS_BACKUP_FOLDER_ID')

if not GOOGLE_DRIVE_FOLDER_ID:
    app.logger.warning("GOOGLE_DRIVE_FOLDER_ID environment variable is not set. Drive upload will not work.")
if not GOOGLE_SETTINGS_BACKUP_FOLDER_ID:
    app.logger.warning("GOOGLE_SETTINGS_BACKUP_FOLDER_ID environment variable is not set. Automatic settings backup/restore will not work.")

SCOPES = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/drive']
THAILAND_TZ = pytz.timezone('Asia/Bangkok')
cache = TTLCache(maxsize=100, ttl=60)

# Initialize LINE Bot SDK
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- Settings Management ---
SETTINGS_FILE = 'settings.json'
_DEFAULT_APP_SETTINGS_STORE = {
    'google_tasks_list_id': None, # NEW: To store the ID of the list owned by the Service Account
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
    'qrcode_settings': { 'box_size': 8, 'border': 4, 'fill_color': '#28a745', 'back_color': '#FFFFFF', 'custom_url': '' },
    'equipment_catalog': [],
    'auto_backup': { 'enabled': False, 'hour_thai': 2, 'minute_thai': 0 },
    'shop_info': { 'contact_phone': '081-XXX-XXXX', 'line_id': '@ComphoneService' },
    'technician_list': [],
    'sales_offers': {
        'post_feedback_offer_enabled': False,
        'post_feedback_offer_message': 'ขอบคุณสำหรับความไว้วางใจครับ/ค่ะ! สนใจสมัครแพ็กเกจล้างแอร์รายปีในราคาพิเศษหรือไม่ครับ/คะ? ติดต่อสอบถามได้เลย!',
        'report_promotion_enabled': False,
        'report_promotion_text': 'โปรโมชันพิเศษ! ลด 10% สำหรับการใช้บริการครั้งถัดไปภายใน 3 เดือน เพียงแจ้งรหัสงานนี้'
    }
}
_APP_SETTINGS_STORE = {} 

#<editor-fold desc="Helper and Utility Functions">
# --- All Helper and Utility Functions ---

def load_settings_from_file():
    """Load application settings from JSON file."""
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
    """Save application settings to JSON file."""
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f: json.dump(settings_data, f, ensure_ascii=False, indent=4)
        return True
    except IOError as e:
        app.logger.error(f"Error writing to settings.json: {e}")
        return False

def get_google_service(api_name, api_version):
    """Authenticates and returns a Google API service using a Service Account."""
    creds = None
    google_creds_json_str = os.environ.get('GOOGLE_CREDENTIALS_JSON')

    if google_creds_json_str:
        try:
            creds_info = json.loads(google_creds_json_str)
            creds = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            app.logger.error(f"Could not load Service Account credentials from env var: {e}")
            return None
    else:
        # Fallback for local development if you have a file named 'service_account.json'
        if os.path.exists('service_account.json'):
            try:
                creds = service_account.Credentials.from_service_account_file('service_account.json', scopes=SCOPES)
            except Exception as e:
                app.logger.error(f"Could not load Service Account from file: {e}")
                return None
        else:
            app.logger.error("No Google credentials available. Please set GOOGLE_CREDENTIALS_JSON environment variable.")
            return None
            
    try:
        return build(api_name, api_version, credentials=creds, cache_discovery=False)
    except Exception as e:
        app.logger.error(f"Failed to build Google API service '{api_name}': {e}")
        return None

def get_google_tasks_service(): return get_google_service('tasks', 'v1')
def get_google_drive_service(): return get_google_service('drive', 'v3')


def load_settings_from_drive_on_startup():
    """Attempts to load the latest settings_backup.json from Google Drive."""
    if not GOOGLE_SETTINGS_BACKUP_FOLDER_ID:
        app.logger.warning("GOOGLE_SETTINGS_BACKUP_FOLDER_ID not set. Skipping settings restore.")
        return False

    service = get_google_drive_service()
    if not service:
        app.logger.error("Could not get Drive service for settings restore.")
        return False

    try:
        query = f"name = 'settings_backup.json' and '{GOOGLE_SETTINGS_BACKUP_FOLDER_ID}' in parents"
        response = service.files().list(q=query, spaces='drive', fields='files(id, name)', orderBy='createdTime desc', pageSize=1).execute()
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

def get_app_settings():
    """Get current application settings, loading from file or using defaults."""
    global _APP_SETTINGS_STORE
    if not _APP_SETTINGS_STORE: 
        loaded = load_settings_from_file()
        _APP_SETTINGS_STORE = json.loads(json.dumps(_DEFAULT_APP_SETTINGS_STORE))
        if loaded:
            for key, default_value in _APP_SETTINGS_STORE.items():
                if isinstance(default_value, dict) and key in loaded and isinstance(loaded[key], dict):
                    _APP_SETTINGS_STORE[key].update(loaded[key])
                elif key in loaded: 
                    _APP_SETTINGS_STORE[key] = loaded[key]
        else:
            save_settings_to_file(_APP_SETTINGS_STORE)
    
    equipment_catalog = _APP_SETTINGS_STORE.get('equipment_catalog', [])
    _APP_SETTINGS_STORE['common_equipment_items'] = sorted(list(set(item.get('item_name') for item in equipment_catalog if item.get('item_name'))))
    return _APP_SETTINGS_STORE

def save_app_settings(settings_data):
    """Save application settings, merging with current settings."""
    global _APP_SETTINGS_STORE
    current_settings = get_app_settings()
    for key, value in settings_data.items():
        if isinstance(value, dict) and key in current_settings and isinstance(current_settings[key], dict):
            current_settings[key].update(value)
        else: 
            current_settings[key] = value
    _APP_SETTINGS_STORE = current_settings
    return save_settings_to_file(_APP_SETTINGS_STORE)

def get_tasks_list_id():
    """Gets the Task List ID from settings, creating a new list if it doesn't exist."""
    settings = get_app_settings()
    list_id = settings.get('google_tasks_list_id')

    if list_id:
        return list_id

    app.logger.warning("No Google Tasks List ID found in settings. Attempting to create a new one.")
    service = get_google_tasks_service()
    if not service:
        app.logger.error("Cannot create new Task List: Google service is not available.")
        return None
    
    try:
        list_title = f"Comphone Tasks (Created: {datetime.datetime.now(THAILAND_TZ).strftime('%Y-%m-%d %H:%M')})"
        new_list = service.tasklists().insert(body={'title': list_title}).execute()
        new_list_id = new_list['id']
        app.logger.info(f"Successfully created new Google Tasks List '{list_title}' with ID: {new_list_id}")
        
        # Save the new ID to settings
        settings['google_tasks_list_id'] = new_list_id
        save_app_settings(settings)
        
        return new_list_id
    except HttpError as e:
        app.logger.error(f"Failed to create new Google Tasks List: {e}")
        return None

@cached(cache)
def get_google_tasks_for_report(show_completed=True):
    """Fetches tasks from Google Tasks API."""
    service = get_google_tasks_service()
    task_list_id = get_tasks_list_id()
    if not service or not task_list_id: return None
    try:
        results = service.tasks().list(tasklist=task_list_id, showCompleted=show_completed, maxResults=100).execute()
        return results.get('items', [])
    except HttpError as err:
        app.logger.error(f"API Error getting tasks: {err}")
        return None

def get_single_task(task_id):
    """Fetches a single task from Google Tasks API."""
    if not task_id: return None
    service = get_google_tasks_service()
    task_list_id = get_tasks_list_id()
    if not service or not task_list_id: return None
    try:
        return service.tasks().get(tasklist=task_list_id, task=task_id).execute()
    except HttpError as err:
        app.logger.error(f"Error getting single task {task_id}: {err}")
        return None
        
def upload_file_to_google_drive(file_path, file_name, mime_type, folder_id=None):
    """Uploads a file to a specified Google Drive folder."""
    if folder_id is None:
        folder_id = GOOGLE_DRIVE_FOLDER_ID

    service = get_google_drive_service()
    if not service or not folder_id:
        app.logger.error("Drive service or folder ID is not configured for upload.")
        return None
    try:
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        file_metadata = {'name': file_name, 'parents': [folder_id]}
        file_obj = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        
        service.permissions().create(fileId=file_obj['id'], body={'role': 'reader', 'type': 'anyone'}).execute()
        
        app.logger.info(f"Uploaded to Drive: {file_obj.get('webViewLink')}")
        return file_obj.get('webViewLink')
    except HttpError as e:
        app.logger.error(f'Drive upload error: {e}')
        return None

def create_google_task(title, notes=None, due=None):
    """Creates a new task in Google Tasks."""
    service = get_google_tasks_service()
    task_list_id = get_tasks_list_id()
    if not service or not task_list_id: return None
    try:
        task_body = {'title': title, 'notes': notes, 'status': 'needsAction'}
        if due: task_body['due'] = due
        return service.tasks().insert(tasklist=task_list_id, body=task_body).execute()
    except HttpError as e:
        app.logger.error(f"Error creating Google Task: {e}")
        return None
        
def delete_google_task(task_id):
    """Deletes a task from Google Tasks."""
    service = get_google_tasks_service()
    task_list_id = get_tasks_list_id()
    if not service or not task_list_id: return False
    try:
        service.tasks().delete(tasklist=task_list_id, task=task_id).execute()
        return True
    except HttpError as err:
        app.logger.error(f"API Error deleting task {task_id}: {err}")
        return False

def update_google_task(task_id, title=None, notes=None, status=None, due=None):
    """Updates an existing task in Google Tasks."""
    service = get_google_tasks_service()
    task_list_id = get_tasks_list_id()
    if not service or not task_list_id: return None
    try:
        task = service.tasks().get(tasklist=task_list_id, task=task_id).execute()
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
                
        return service.tasks().update(tasklist=task_list_id, task=task_id, body=task).execute()
    except HttpError as e:
        app.logger.error(f"Failed to update task {task_id}: {e}")
        return None

def parse_customer_info_from_notes(notes):
    """
    Parses customer information and map URL from task notes robustly.
    """
    info = {'name': '', 'phone': '', 'address': '', 'map_url': None}
    if not notes: return info

    name_match = re.search(r"ลูกค้า:\s*(.*)", notes, re.IGNORECASE)
    phone_match = re.search(r"เบอร์โทรศัพท์:\s*(.*)", notes, re.IGNORECASE)
    address_match = re.search(r"ที่อยู่:\s*(.*)", notes, re.IGNORECASE)
    map_url_match = re.search(r"https?://(?:www\.)?google\.com/maps/.*", notes)

    if name_match:
        info['name'] = name_match.group(1).strip()
    if phone_match:
        info['phone'] = phone_match.group(1).strip()
    if address_match:
        info['address'] = address_match.group(1).strip()
    if map_url_match:
        info['map_url'] = map_url_match.group(0).strip()
    
    return info

def parse_customer_feedback_from_notes(notes):
    """Parses customer feedback data from task notes."""
    feedback_data = {}
    if not notes: return feedback_data
    
    feedback_match = re.search(r"--- CUSTOMER_FEEDBACK_START ---\s*\n(.*?)\n--- CUSTOMER_FEEDBACK_END ---", notes, re.DOTALL)
    if feedback_match:
        try:
            feedback_data = json.loads(feedback_match.group(1))
        except json.JSONDecodeError:
            app.logger.warning("Failed to decode customer feedback JSON from notes.")
    return feedback_data

def parse_google_task_dates(task_item):
    """Parses and formats date fields from a Google Task item."""
    parsed = task_item.copy()
    for key in ['created', 'due', 'completed']:
        if parsed.get(key):
            try:
                dt_str = parsed[key].replace('Z', '+00:00')
                dt_utc = datetime.datetime.fromisoformat(dt_str)
                parsed[f'{key}_formatted'] = dt_utc.astimezone(THAILAND_TZ).strftime("%d/%m/%y %H:%M")
                if key == 'due':
                    parsed['due_for_input'] = dt_utc.astimezone(THAILAND_TZ).strftime("%Y-%m-%dT%H:%M")
            except (ValueError, TypeError) as e:
                app.logger.warning(f"Could not parse date '{parsed[key]}' for key '{key}': {e}")
                parsed[f'{key}_formatted'] = ''
                if key == 'due': parsed['due_for_input'] = ''
        else: 
            parsed[f'{key}_formatted'] = ''
            if key == 'due': parsed['due_for_input'] = ''
    return parsed

def parse_tech_report_from_notes(notes):
    """Parses technical report history from task notes."""
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
    
    original_notes_text = re.sub(r"--- (?:TECH_REPORT_START|CUSTOMER_FEEDBACK_START) ---.*?--- (?:TECH_REPORT_END|CUSTOMER_FEEDBACK_END) ---", "", notes, flags=re.DOTALL).strip()
    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, original_notes_text
    
def allowed_file(filename):
    """Checks if a filename has an allowed extension."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def _parse_equipment_string(text_input):
    """Parses equipment string into a list of dictionaries."""
    equipment_list = []
    if not text_input: return equipment_list
    for line in text_input.strip().split('\n'):
        if not line.strip(): continue
        parts = line.split(',', 1)
        item_name = parts[0].strip()
        if item_name:
            equipment_list.append({"item": item_name, "quantity": parts[1].strip() if len(parts) > 1 else '1'})
    return equipment_list

def _format_equipment_list(equipment_data):
    """Formats a list of equipment data into a display string."""
    if not equipment_data: return 'N/A'
    if isinstance(equipment_data, str): return equipment_data
    lines = []
    if isinstance(equipment_data, list):
        for item in equipment_data:
            if isinstance(item, dict) and "item" in item:
                line = item['item']
                if item.get("quantity"): line += f" (x{item['quantity']})"
                lines.append(line)
            elif isinstance(item, str): lines.append(item)
    return "\n".join(lines) if lines else 'N/A'

@app.context_processor
def inject_now():
    """Injects current datetime into Jinja2 templates."""
    return {'now': datetime.datetime.now(THAILAND_TZ)}

def generate_qr_code_base64(data, box_size=8, border=4, fill_color='black', back_color='white'):
    """Generates a base64 encoded QR code image."""
    try:
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=box_size, border=border)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color=fill_color, back_color=back_color)
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffered.getvalue()).decode("utf-8")
    except Exception as e:
        app.logger.error(f"Error generating QR code: {e}")
        return ""

def _create_backup_zip():
    """Creates a zip archive of all tasks, settings, and source code."""
    try:
        all_tasks = get_google_tasks_for_report(show_completed=True)
        if all_tasks is None:
            app.logger.error('Failed to get tasks for backup.')
            return None, None

        memory_file = BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf: 
            zf.writestr('data/tasks_backup.json', json.dumps(all_tasks, indent=4, ensure_ascii=False))
            
            project_root = os.path.dirname(os.path.abspath(__file__))
            for folder, _, files in os.walk(project_root):
                for file in files:
                    if file.endswith(('.py', '.html', '.css', '.js', '.json', '.env', 'Procfile', 'requirements.txt')) and file != 'token.json' and file != 'service_account.json':
                        file_path = os.path.join(folder, file)
                        archive_name = os.path.relpath(file_path, project_root)
                        zf.write(file_path, arcname=f'code/{archive_name}')
        memory_file.seek(0)
        backup_filename = f"full_system_backup_{datetime.datetime.now(THAILAND_TZ).strftime('%Y%m%d_%H%M%S')}.zip"
        return memory_file, backup_filename
    except Exception as e:
        app.logger.error(f"Error creating full system backup zip: {e}")
        return None, None

def _upload_backup_to_drive(memory_file, filename, drive_folder_id):
    """Uploads the given memory file (zip or json) to Google Drive."""
    if not all([memory_file, filename, drive_folder_id]):
        app.logger.error("Missing memory_file, filename, or drive_folder_id for Drive upload.")
        return False
    
    service = get_google_drive_service()
    if not service: return False
    
    try:
        mime_type = 'application/zip' if filename.endswith('.zip') else 'application/json'
        media = MediaIoBaseUpload(memory_file, mimetype=mime_type, resumable=True) 
        file_metadata = {'name': filename, 'parents': [drive_folder_id]}
        
        file_obj = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        app.logger.info(f"Successfully uploaded backup '{filename}' to Drive (ID: {file_obj['id']})")
        return True
    except Exception as e:
        app.logger.error(f'Google Drive backup upload error for {filename}: {e}')
        return False

#</editor-fold>

#<editor-fold desc="Scheduled Jobs">
# --- All Scheduled Jobs and Scheduler Management ---

def _backup_settings_to_drive():
    """Helper function to back up the current settings to Google Drive."""
    if not GOOGLE_SETTINGS_BACKUP_FOLDER_ID:
        app.logger.warning("Cannot back up settings: GOOGLE_SETTINGS_BACKUP_FOLDER_ID not set.")
        return False
    
    settings_data = get_app_settings()
    settings_json_bytes = BytesIO(json.dumps(settings_data, ensure_ascii=False, indent=4).encode('utf-8'))
    settings_backup_filename = "settings_backup.json"
    
    if _upload_backup_to_drive(settings_json_bytes, settings_backup_filename, GOOGLE_SETTINGS_BACKUP_FOLDER_ID):
        app.logger.info("Successfully backed up settings to Google Drive.")
        return True
    else:
        app.logger.error("Failed to back up settings to Google Drive.")
        return False

def scheduled_backup_job():
    with app.app_context():
        app.logger.info("Running scheduled backup job...")
        
        memory_file_zip, filename_zip = _create_backup_zip()
        if memory_file_zip and filename_zip:
            if _upload_backup_to_drive(memory_file_zip, filename_zip, GOOGLE_DRIVE_FOLDER_ID):
                app.logger.info("Automatic full system backup successful.")
            else:
                app.logger.error("Automatic full system backup failed.")

        _backup_settings_to_drive()


def scheduled_appointment_reminder_job():
    with app.app_context():
        app.logger.info("Running scheduled appointment reminder job...")
        settings = get_app_settings()
        recipients = settings.get('line_recipients', {})
        
        if not recipients.get('admin_group_id') and not recipients.get('technician_group_id'):
            return

        tasks_raw = get_google_tasks_for_report(show_completed=False) or []
        today_thai = datetime.date.today()
        upcoming_appointments = []

        for task in tasks_raw:
            if task.get('status') == 'needsAction' and task.get('due'):
                try:
                    due_dt_utc = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                    if due_dt_utc.astimezone(THAILAND_TZ).date() == today_thai:
                        upcoming_appointments.append(task)
                except (ValueError, TypeError): continue
        
        if not upcoming_appointments: return
            
        upcoming_appointments.sort(key=lambda x: x['due'])
        
        for task in upcoming_appointments:
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            parsed_dates = parse_google_task_dates(task)
            message_text = (
                f"🔔 แจ้งเตือนงานวันนี้:\n"
                f"ลูกค้า: {customer_info.get('name', '-')}\n"
                f"รายละเอียด: {task.get('title', '-')}\n"
                f"นัดหมาย: {parsed_dates.get('due_formatted', '-')}\n\n"
                f"ดูรายละเอียด: {url_for('task_details', task_id=task.get('id'), _external=True)}"
            )
            try:
                if recipients.get('admin_group_id'):
                    line_bot_api.push_message(recipients['admin_group_id'], TextSendMessage(text=message_text))
                if recipients.get('technician_group_id') and recipients['technician_group_id'] != recipients.get('admin_group_id'):
                    line_bot_api.push_message(recipients['technician_group_id'], TextSendMessage(text=message_text))
            except Exception as e:
                app.logger.error(f"Failed to send appointment reminder for task {task['id']}: {e}")

def _create_customer_follow_up_flex_message(task_id, task_title, customer_name):
    problem_action = PostbackAction(label='👎 มีปัญหา', data=f'action=customer_feedback&task_id={task_id}&feedback=problem_reported', display_text='ฉันพบปัญหาหลังการซ่อม')
    if LIFF_ID_FORM:
        problem_action = URIAction(label='👎 มีปัญหา', uri=f"https://liff.line.me/{LIFF_ID_FORM}/customer_problem_form?task_id={task_id}")

    return BubbleContainer(
        body=BoxComponent(
            layout='vertical', spacing='md',
            contents=[
                TextComponent(text="🙏 แบบสอบถามความพึงพอใจ 🙏", weight='bold', size='lg', color='#1DB446', align='center'),
                SeparatorComponent(margin='md'),
                TextComponent(text=f"ลูกค้า: {customer_name}", size='sm', wrap=True),
                TextComponent(text=f"งาน: {task_title}", size='sm', wrap=True, color='#666666'),
                SeparatorComponent(margin='lg'),
                TextComponent(text="ท่านพอใจกับบริการล่าสุดหรือไม่?", size='md', wrap=True, align='center'),
                BoxComponent(layout='vertical', spacing='sm', margin='md', contents=[
                    ButtonComponent(style='primary', height='sm', color='#28a745', action=PostbackAction(label='👍 พอใจมาก', data=f'action=customer_feedback&task_id={task_id}&feedback=very_satisfied', display_text='ขอบคุณสำหรับความคิดเห็นครับ/ค่ะ!')),
                    ButtonComponent(style='secondary', height='sm', color='#6c757d', action=PostbackAction(label='👌 พอใจ', data=f'action=customer_feedback&task_id={task_id}&feedback=satisfied', display_text='ขอบคุณสำหรับความคิดเห็นครับ/ค่ะ!')),
                    ButtonComponent(style='danger', height='sm', color='#dc3545', action=problem_action)
                ]),
            ]
        )
    )

def scheduled_customer_follow_up_job():
    with app.app_context():
        app.logger.info("Running scheduled customer follow-up job...")
        settings = get_app_settings()
        admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')
        
        tasks_raw = get_google_tasks_for_report(show_completed=True) or []
        now_utc = datetime.datetime.now(pytz.utc)
        one_day_ago = now_utc - datetime.timedelta(days=1)
        two_days_ago = now_utc - datetime.timedelta(days=2)

        for task in tasks_raw:
            if task.get('status') == 'completed' and task.get('completed'):
                try:
                    completed_dt_utc = datetime.datetime.fromisoformat(task['completed'].replace('Z', '+00:00'))
                    
                    if two_days_ago <= completed_dt_utc < one_day_ago:
                        notes = task.get('notes', '')
                        feedback_data = parse_customer_feedback_from_notes(notes)
                        
                        if 'follow_up_sent_date' in feedback_data: continue

                        customer_info = parse_customer_info_from_notes(notes)
                        customer_line_id = feedback_data.get('customer_line_user_id')
                        
                        flex_content = _create_customer_follow_up_flex_message(
                            task['id'], task['title'], customer_info.get('name', 'N/A'))
                        flex_message = FlexSendMessage(alt_text="แบบสอบถามความพึงพอใจบริการ", contents=flex_content)

                        feedback_data['follow_up_sent_date'] = datetime.datetime.now(THAILAND_TZ).isoformat()
                        
                        _, base_notes = parse_tech_report_from_notes(notes)
                        tech_reports_text = "".join(re.findall(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", notes, re.DOTALL))
                        
                        new_notes = base_notes.strip()
                        if tech_reports_text: new_notes += "\n\n" + tech_reports_text.strip()
                        new_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
                        
                        update_google_task(task['id'], notes=new_notes)
                        cache.clear()

                        if customer_line_id:
                            try:
                                line_bot_api.push_message(customer_line_id, flex_message)
                            except Exception as e:
                                app.logger.error(f"Fallback: Failed to send direct follow-up to {customer_line_id}: {e}")
                                if admin_group_id: line_bot_api.push_message(admin_group_id, [TextSendMessage(text=f"⚠️ ส่ง Follow-up ให้ลูกค้า {customer_info.get('name')} ไม่สำเร็จ โปรดส่งข้อความนี้แทน:"), flex_message])
                        elif admin_group_id:
                            admin_text = f"✅ กรุณาส่งแบบสอบถามนี้ให้ลูกค้า:\nคุณ {customer_info.get('name')} (โทร: {customer_info.get('phone')})"
                            line_bot_api.push_message(admin_group_id, [TextSendMessage(text=admin_text), flex_message])

                except Exception as e:
                    app.logger.warning(f"Could not process task {task.get('id')} for follow-up: {e}")

scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)

def run_scheduler():
    """Initializes and runs the APScheduler jobs."""
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
    atexit.register(lambda: scheduler.shutdown(wait=False))

#</editor-fold>

# --- Initial app setup calls ---
with app.app_context(): 
    load_settings_from_drive_on_startup() 
    _APP_SETTINGS_STORE = get_app_settings()
    get_tasks_list_id() # Ensure the task list exists on startup
    run_scheduler()


# --- Flask Routes ---
@app.route("/")
def root_redirect():
    return redirect(url_for('dashboard'))

@app.route("/summary")
def summary_redirect():
    return redirect(url_for('dashboard'))

@app.route("/dashboard")
def dashboard():
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    
    current_time_utc = datetime.datetime.now(pytz.utc)
    final_tasks = []
    stats = {'needsAction': 0, 'completed': 0, 'overdue': 0, 'total': len(tasks_raw)}
    
    monthly_completion_data = defaultdict(int)
    
    for task in tasks_raw:
        task_status = task.get('status', 'needsAction')
        is_overdue = False
        if task_status == 'needsAction' and task.get('due'):
            try:
                due_dt_utc = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                if due_dt_utc < current_time_utc: is_overdue = True
            except (ValueError, TypeError): pass
        
        if task_status == 'completed': 
            stats['completed'] += 1
            if task.get('completed'):
                try:
                    completed_dt_utc = datetime.datetime.fromisoformat(task['completed'].replace('Z', '+00:00'))
                    completed_dt_local = completed_dt_utc.astimezone(THAILAND_TZ)
                    month_key = completed_dt_local.strftime("%Y-%m")
                    monthly_completion_data[month_key] += 1
                except (ValueError, TypeError): pass
        else:
            stats['needsAction'] += 1
            if is_overdue: stats['overdue'] += 1

        if (status_filter == 'all' or
            (status_filter == 'completed' and task_status == 'completed') or
            (status_filter == 'needsAction' and task_status == 'needsAction' and not is_overdue) or
            (status_filter == 'overdue' and is_overdue)):
            
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            searchable_text = f"{task.get('title', '')} {customer_info.get('name', '')} {customer_info.get('phone', '')}".lower()
            
            if not search_query or search_query in searchable_text:
                parsed_task = parse_google_task_dates(task)
                parsed_task['customer'] = customer_info
                parsed_task['is_overdue'] = is_overdue
                final_tasks.append(parsed_task)

    final_tasks.sort(key=lambda x: (x.get('status') != 'needsAction', x.get('due') is None, x.get('due', '')))
    
    chart_labels = []
    chart_values = []
    today = datetime.date.today()
    for i in range(11, -1, -1):
        month = today - relativedelta(months=i)
        month_key = month.strftime("%Y-%m")
        chart_labels.append(month.strftime("%b %y"))
        chart_values.append(monthly_completion_data[month_key])

    chart_data = {
        'labels': chart_labels,
        'values': chart_values
    }

    return render_template("dashboard.html", 
                           tasks=final_tasks, 
                           summary=stats, 
                           search_query=search_query, 
                           status_filter=status_filter,
                           chart_data=json.dumps(chart_data))

@app.route("/form", methods=['GET', 'POST'])
def form_page():
    if request.method == 'POST':
        task_title = str(request.form.get('task_title', '')).strip()
        customer_name = str(request.form.get('customer', '')).strip()

        if not task_title or not customer_name:
            flash('กรุณากรอกชื่อลูกค้าและรายละเอียดงาน', 'danger')
            return redirect(url_for('form_page'))
        
        notes_lines = [
            f"ลูกค้า: {customer_name}",
            f"เบอร์โทรศัพท์: {str(request.form.get('phone', '')).strip()}",
            f"ที่อยู่: {str(request.form.get('address', '')).strip()}",
        ]
        map_url = str(request.form.get('latitude_longitude', '')).strip()
        if map_url: notes_lines.append(map_url)
        
        notes = "\n".join(filter(None, notes_lines))
        
        due_date_gmt = None
        appointment_str = str(request.form.get('appointment', '')).strip()
        if appointment_str:
            try:
                dt_local = THAILAND_TZ.localize(datetime.datetime.strptime(appointment_str, "%Y-%m-%d %H:%M"))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat()
            except ValueError: app.logger.error(f"Invalid appointment format: {appointment_str}")

        if create_google_task(task_title, notes=notes, due=due_date_gmt):
            cache.clear()
            flash('สร้างงานใหม่เรียบร้อยแล้ว!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('เกิดข้อผิดพลาดในการสร้างงาน', 'danger')
    return render_template('form.html')

@app.route('/task/<task_id>', methods=['GET', 'POST'])
def task_details(task_id):
    if request.method == 'POST':
        new_title = str(request.form.get('task_title', '')).strip()
        if not new_title:
            flash('กรุณากรอกรายละเอียดงาน', 'danger')
            return redirect(url_for('task_details', task_id=task_id))

        task_raw = get_single_task(task_id)
        if not task_raw: abort(404)

        new_base_notes_lines = [
            f"ลูกค้า: {str(request.form.get('customer_name', '')).strip()}",
            f"เบอร์โทรศัพท์: {str(request.form.get('customer_phone', '')).strip()}",
            f"ที่อยู่: {str(request.form.get('address', '')).strip()}",
        ]
        map_url = str(request.form.get('latitude_longitude', '')).strip()
        if map_url: new_base_notes_lines.append(map_url)
        
        new_base_notes = "\n".join(filter(None, new_base_notes_lines))

        history, _ = parse_tech_report_from_notes(task_raw.get('notes', ''))
        feedback_data = parse_customer_feedback_from_notes(task_raw.get('notes', ''))

        work_summary = str(request.form.get('work_summary', '')).strip()
        files = request.files.getlist('files[]')
        
        selected_technicians = request.form.getlist('technicians')

        if work_summary or any(f.filename for f in files):
            new_attachment_urls = []
            for file in files:
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    temp_filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    try:
                        file.save(temp_filepath)
                        drive_url = upload_file_to_google_drive(temp_filepath, filename, file.mimetype)
                        if drive_url: new_attachment_urls.append(drive_url)
                    finally:
                        if os.path.exists(temp_filepath): os.remove(temp_filepath)
            
            history.append({
                'summary_date': datetime.datetime.now(THAILAND_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                'work_summary': work_summary,
                'equipment_used': _parse_equipment_string(request.form.get('equipment_used', '')),
                'attachment_urls': new_attachment_urls,
                'technicians': selected_technicians
            })

        all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in sorted(history, key=lambda x: x.get('summary_date', ''))])
        
        final_notes = new_base_notes + all_reports_text
        if feedback_data:
            final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        
        due_date_gmt = None
        appointment_str = str(request.form.get('appointment_due', '')).strip()
        if appointment_str:
            try:
                dt_local = THAILAND_TZ.localize(datetime.datetime.strptime(appointment_str, "%Y-%m-%dT%H:%M"))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat()
            except ValueError: pass
        
        if update_google_task(task_id, title=new_title, notes=final_notes, status=request.form.get('status'), due=due_date_gmt):
            cache.clear()
            flash('บันทึกการเปลี่ยนแปลงเรียบร้อยแล้ว!', 'success')
        
        return redirect(url_for('task_details', task_id=task_id))

    # --- GET Request ---
    task_raw = get_single_task(task_id)
    if not task_raw: abort(404)
    
    task = parse_google_task_dates(task_raw)
    notes = task.get('notes', '')
    task['customer'] = parse_customer_info_from_notes(notes)
    task['tech_reports_history'], _ = parse_tech_report_from_notes(notes)
    task['customer_feedback'] = parse_customer_feedback_from_notes(notes)
    
    app_settings = get_app_settings()
    return render_template('update_task_details.html', task=task, common_equipment_items=app_settings.get('common_equipment_items', []), technician_list=app_settings.get('technician_list', []))

@app.route('/delete_task/<task_id>', methods=['POST'])
def delete_task(task_id):
    if delete_google_task(task_id):
        flash('ลบงานเรียบร้อยแล้ว!', 'success')
        cache.clear()
    else:
        flash('เกิดข้อผิดพลาดในการลบงาน', 'danger')
    return redirect(url_for('dashboard'))

@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        technician_names_text = request.form.get('technician_list', '').strip()
        technician_list = [name.strip() for name in technician_names_text.splitlines() if name.strip()]

        current_settings = get_app_settings()
        google_tasks_list_id = current_settings.get('google_tasks_list_id')

        new_settings = {
            'google_tasks_list_id': google_tasks_list_id,
            'report_times': { 
                'appointment_reminder_hour_thai': int(request.form.get('appointment_reminder_hour')), 
                'outstanding_report_hour_thai': int(request.form.get('outstanding_report_hour')),
                'customer_followup_hour_thai': int(request.form.get('customer_followup_hour'))
            },
            'line_recipients': { 
                'admin_group_id': request.form.get('admin_group_id', '').strip(), 
                'technician_group_id': request.form.get('technician_group_id', '').strip(),
                'manager_user_id': request.form.get('manager_user_id', '').strip() 
            },
            'qrcode_settings': { 
                'box_size': int(request.form.get('qr_box_size')), 'border': int(request.form.get('qr_border')), 
                'fill_color': request.form.get('qr_fill_color'), 'back_color': request.form.get('qr_back_color'), 
                'custom_url': request.form.get('qr_custom_url', '').strip() 
            },
            'auto_backup': { 
                'enabled': request.form.get('auto_backup_enabled') == 'on',
                'hour_thai': int(request.form.get('auto_backup_hour')),
                'minute_thai': int(request.form.get('auto_backup_minute'))
            },
            'shop_info': {
                'contact_phone': request.form.get('shop_contact_phone', '').strip(),
                'line_id': request.form.get('shop_line_id', '').strip()
            },
            'technician_list': technician_list,
            'sales_offers': {
                'post_feedback_offer_enabled': request.form.get('post_feedback_offer_enabled') == 'on',
                'post_feedback_offer_message': request.form.get('post_feedback_offer_message', '').strip(),
                'report_promotion_enabled': request.form.get('report_promotion_enabled') == 'on',
                'report_promotion_text': request.form.get('report_promotion_text', '').strip()
            }
        }
        
        if save_app_settings(new_settings):
            flash('บันทึกการตั้งค่าเรียบร้อยแล้ว! กำลังสำรองข้อมูลไปยัง Google Drive...', 'success')
            with app.app_context():
                if _backup_settings_to_drive():
                    flash('สำรองข้อมูลการตั้งค่าไปยัง Google Drive สำเร็จ!', 'info')
                else:
                    flash('สำรองข้อมูลการตั้งค่าไปยัง Google Drive ล้มเหลว!', 'danger')
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกไฟล์ตั้งค่า', 'danger')

        run_scheduler()
        cache.clear()
        return redirect(url_for('settings_page'))
    
    current_settings = get_app_settings()
    general_summary_url = url_for('dashboard', _external=True)
    qr_url = current_settings.get('qrcode_settings', {}).get('custom_url') or general_summary_url
    qr_settings = current_settings.get('qrcode_settings', {})
    
    qr_code_base64_general = generate_qr_code_base64(
        qr_url, box_size=qr_settings.get('box_size', 8), border=qr_settings.get('border', 4),
        fill_color=qr_settings.get('fill_color', '#28a745'), back_color=qr_settings.get('back_color', '#FFFFFF')
    )
    return render_template('settings_page.html', settings=current_settings, qr_code_base64_general=qr_code_base64_general, general_summary_url=general_summary_url)

@app.route('/import_settings', methods=['POST'])
def import_settings():
    if 'settings_file' not in request.files or not request.files['settings_file'].filename:
        flash('กรุณาเลือกไฟล์ตั้งค่า (.json)', 'danger')
        return redirect(url_for('settings_page'))
    
    file = request.files['settings_file']
    if file and file.filename.endswith('.json'):
        try:
            uploaded_settings = json.load(file.stream)
            current_settings = get_app_settings()
            google_tasks_list_id = current_settings.get('google_tasks_list_id')
            uploaded_settings['google_tasks_list_id'] = google_tasks_list_id

            if save_app_settings(uploaded_settings):
                flash('นำเข้าและบันทึกการตั้งค่าใหม่เรียบร้อยแล้ว!', 'success')
                with app.app_context():
                    if _backup_settings_to_drive():
                        app.logger.info("Successfully backed up newly imported settings to Google Drive.")
                    else:
                        app.logger.error("Failed to back up newly imported settings to Google Drive.")
            else:
                flash('เกิดข้อผิดพลาดในการบันทึกไฟล์ตั้งค่า', 'danger')

        except json.JSONDecodeError:
            flash('ไฟล์ที่อัปโหลดไม่ใช่รูปแบบ JSON ที่ถูกต้อง', 'danger')
        except Exception as e:
            flash(f"เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}", 'danger')
    else:
        flash('รองรับเฉพาะไฟล์ .json เท่านั้น', 'danger')
        
    return redirect(url_for('settings_page'))

@app.route('/api/preview_tasks_import', methods=['POST'])
def preview_tasks_import():
    if 'tasks_backup_file' not in request.files or not request.files['tasks_backup_file'].filename:
        return jsonify({"error": "กรุณาเลือกไฟล์"}), 400

    file = request.files['tasks_backup_file']
    if not (file and file.filename.endswith('.json')):
        return jsonify({"error": "รองรับเฉพาะไฟล์ .json เท่านั้น"}), 400

    try:
        tasks_data = json.load(file.stream)
        if not isinstance(tasks_data, list):
            return jsonify({"error": "รูปแบบไฟล์ไม่ถูกต้อง: ไฟล์ต้องเป็นรายการ (list) ของงาน"}), 400
        
        preview_data = []
        for task in tasks_data:
            if not (isinstance(task, dict) and 'title' in task):
                continue

            original_title = task.get('title', 'N/A')
            original_notes = task.get('notes', '')

            # Parse customer info from the notes
            customer_info = parse_customer_info_from_notes(original_notes)

            # Clean the notes to show only remaining details
            cleaned_notes = original_notes
            if customer_info.get('name'):
                cleaned_notes = re.sub(r"ลูกค้า:\s*" + re.escape(customer_info['name']), '', cleaned_notes, flags=re.IGNORECASE).strip()
            if customer_info.get('phone'):
                cleaned_notes = re.sub(r"เบอร์โทรศัพท์:\s*" + re.escape(customer_info['phone']), '', cleaned_notes, flags=re.IGNORECASE).strip()
            if customer_info.get('address'):
                cleaned_notes = re.sub(r"ที่อยู่:\s*" + re.escape(customer_info['address']), '', cleaned_notes, flags=re.IGNORECASE).strip()
            if customer_info.get('map_url'):
                cleaned_notes = cleaned_notes.replace(customer_info['map_url'], '').strip()
            
            # UPDATED: Remove old report and feedback blocks
            cleaned_notes = re.sub(r"--- (?:TECH_REPORT_START|CUSTOMER_FEEDBACK_START) ---.*", "", cleaned_notes, re.DOTALL).strip()

            preview_data.append({
                'title': original_title,
                'customer_name': customer_info.get('name', ''),
                'customer_phone': customer_info.get('phone', ''),
                'customer_address': customer_info.get('address', ''),
                'cleaned_notes': cleaned_notes
            })
        
        return jsonify(preview_data)

    except json.JSONDecodeError:
        return jsonify({"error": "ไฟล์ที่อัปโหลดไม่ใช่รูปแบบ JSON ที่ถูกต้อง"}), 400
    except Exception as e:
        app.logger.error(f"Error during task preview: {e}")
        return jsonify({"error": f"เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}"}), 500


@app.route('/api/import_tasks_from_backup', methods=['POST'])
def import_tasks_from_backup():
    try:
        tasks_to_import = request.get_json()
        if not isinstance(tasks_to_import, list):
            return jsonify({"status": "error", "message": "ข้อมูลที่ส่งมาไม่ถูกต้อง"}), 400

        imported_count = 0
        for task_data in tasks_to_import:
            if isinstance(task_data, dict) and 'title' in task_data:
                # Reconstruct notes cleanly from parsed data
                notes_lines = []
                if task_data.get('customer_name'):
                    notes_lines.append(f"ลูกค้า: {task_data['customer_name']}")
                if task_data.get('customer_phone'):
                    notes_lines.append(f"เบอร์โทรศัพท์: {task_data['customer_phone']}")
                if task_data.get('customer_address'):
                    notes_lines.append(f"ที่อยู่: {task_data['customer_address']}")
                
                # Add the remaining cleaned notes if they exist
                if task_data.get('cleaned_notes'):
                    notes_lines.append(f"\n{task_data['cleaned_notes']}")

                final_notes = "\n".join(notes_lines)
                
                create_google_task(
                    title=task_data['title'], 
                    notes=final_notes
                )
                imported_count += 1
        
        cache.clear()
        flash(f'นำเข้าข้อมูลงานเก่าจำนวน {imported_count} รายการสำเร็จ!', 'success')
        return jsonify({"status": "success", "count": imported_count})

    except Exception as e:
        app.logger.error(f"Error during final task import: {e}")
        return jsonify({"status": "error", "message": f"เกิดข้อผิดพลาด: {e}"}), 500

# Other routes (test_notification, backup, etc.) remain the same...
@app.route('/test_notification', methods=['POST'])
def test_notification():
    recipient_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
    if recipient_id:
        try:
            line_bot_api.push_message(recipient_id, TextSendMessage(text="[ทดสอบ] นี่คือข้อความทดสอบจากระบบ"))
            flash(f'ส่งข้อความทดสอบไปที่ ID: {recipient_id} สำเร็จ!', 'success')
        except Exception as e:
            flash(f'เกิดข้อผิดพลาด: {e}', 'danger')
    else:
        flash('กรุณากำหนด "LINE Admin Group ID" ก่อน', 'danger')
    return redirect(url_for('settings_page'))

@app.route('/backup_data')
def backup_data():
    memory_file, filename = _create_backup_zip()
    if memory_file and filename:
        return Response(memory_file, mimetype='application/zip', headers={'Content-Disposition': f'attachment;filename={filename}'})
    else:
        flash('เกิดข้อผิดพลาดในการสร้างไฟล์สำรองข้อมูล', 'danger')
        return redirect(url_for('settings_page'))

@app.route('/trigger_auto_backup_now', methods=['POST'])
def trigger_auto_backup_now():
    scheduled_backup_job()
    flash('กำลังสำรองข้อมูลอัตโนมัติไปยัง Google Drive...', 'info')
    return redirect(url_for('settings_page'))

@app.route('/export_equipment_catalog', methods=['GET'])
def export_equipment_catalog():
    try:
        df = pd.DataFrame(get_app_settings().get('equipment_catalog', []))
        if df.empty:
            flash('ไม่มีข้อมูลอุปกรณ์ในแคตตาล็อก', 'warning')
            return redirect(url_for('settings_page'))
        output = BytesIO()
        df.to_excel(output, index=False, sheet_name='Equipment_Catalog')
        output.seek(0)
        return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment;filename=equipment_catalog.xlsx"})
    except Exception as e:
        flash(f'เกิดข้อผิดพลาดในการส่งออก: {e}', 'danger')
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
            if not all(col in df.columns for col in ['item_name', 'unit', 'price']):
                flash('ไฟล์ Excel ต้องมีคอลัมน์: item_name, unit, price', 'danger')
            else:
                current_settings = get_app_settings()
                current_settings['equipment_catalog'] = df.to_dict('records')
                save_app_settings(current_settings)
                flash('นำเข้าแคตตาล็อกอุปกรณ์เรียบร้อยแล้ว!', 'success')
        except Exception as e:
            flash(f"เกิดข้อผิดพลาดในการนำเข้าไฟล์: {e}", 'danger')
    else:
        flash('รองรับเฉพาะไฟล์ Excel (.xls, .xlsx) เท่านั้น', 'danger')
    return redirect(url_for('settings_page'))

@app.route('/technician_report')
def technician_report():
    now = datetime.datetime.now(THAILAND_TZ)
    
    try:
        selected_year = int(request.args.get('year', now.year))
        selected_month = int(request.args.get('month', now.month))
    except (ValueError, TypeError):
        selected_year = now.year
        selected_month = now.month

    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    
    report_data = defaultdict(lambda: {'count': 0, 'tasks': []})

    for task in tasks_raw:
        if task.get('status') == 'completed' and task.get('completed'):
            try:
                completed_dt_utc = datetime.datetime.fromisoformat(task['completed'].replace('Z', '+00:00'))
                completed_dt_local = completed_dt_utc.astimezone(THAILAND_TZ)

                if completed_dt_local.year == selected_year and completed_dt_local.month == selected_month:
                    history, _ = parse_tech_report_from_notes(task.get('notes', ''))
                    
                    technicians_on_this_task = set()
                    for report_entry in history:
                        technicians = report_entry.get('technicians', [])
                        for tech_name in technicians:
                            technicians_on_this_task.add(tech_name)
                    
                    for tech_name in technicians_on_this_task:
                        report_data[tech_name]['count'] += 1
                        report_data[tech_name]['tasks'].append({
                            'id': task.get('id'),
                            'title': task.get('title'),
                            'completed_formatted': completed_dt_local.strftime("%d/%m/%Y")
                        })
            except (ValueError, TypeError) as e:
                app.logger.warning(f"Could not process completed date for task {task.get('id')}: {e}")
                continue

    current_year = now.year
    years = list(range(current_year - 5, current_year + 2))
    months = [{'value': i, 'name': datetime.date(2000, i, 1).strftime('%B')} for i in range(1, 13)]

    return render_template('technician_report.html', 
                           report_data=report_data,
                           selected_year=selected_year,
                           selected_month=selected_month,
                           years=years,
                           months=months)


# --- Customer Onboarding & Feedback Routes ---
@app.route('/generate_customer_onboarding_qr')
def generate_customer_onboarding_qr():
    task_id = request.args.get('task_id')
    task = get_single_task(task_id)
    if not task: abort(404)
    if not LIFF_ID_FORM:
        flash("ไม่สามารถสร้าง QR Code ได้: ไม่พบ LIFF_ID_FORM", 'danger')
        return redirect(url_for('task_details', task_id=task_id))

    onboarding_url = f"https://liff.line.me/{LIFF_ID_FORM}/customer_onboarding.html?task_id={task_id}"
    qr_code_base64 = generate_qr_code_base64(onboarding_url, box_size=10)
    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    return render_template('generate_onboarding_qr.html', qr_code_base64=qr_code_base64, task=task, customer_info=customer_info, onboarding_url=onboarding_url)

@app.route('/customer_problem_form')
def customer_problem_form():
    task_id = request.args.get('task_id')
    task = get_single_task(task_id)
    if not task: abort(404)
    
    parsed_task = parse_google_task_dates(task)
    parsed_task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
    return render_template('customer_problem_form.html', task=parsed_task, LIFF_ID_FORM=LIFF_ID_FORM)

@app.route('/trigger_customer_follow_up_test', methods=['POST'])
def trigger_customer_follow_up_test():
    with app.app_context():
        tasks_raw = get_google_tasks_for_report(show_completed=True) or []
        completed_tasks = [task for task in tasks_raw if task.get('status') == 'completed']
        if not completed_tasks:
            flash('ไม่พบงานที่เสร็จแล้วสำหรับใช้ทดสอบ', 'warning')
            return redirect(url_for('settings_page'))
            
        latest_task = max(completed_tasks, key=lambda x: x.get('completed', ''))
        
        now_utc = datetime.datetime.now(pytz.utc)
        one_day_ago = now_utc - datetime.timedelta(days=1, minutes=5)
        latest_task['completed'] = one_day_ago.isoformat()
        
        notes = latest_task.get('notes', '')
        feedback_data = parse_customer_feedback_from_notes(notes)
        feedback_data.pop('follow_up_sent_date', None)
        
        _, base_notes = parse_tech_report_from_notes(notes)
        tech_reports_text = "".join(re.findall(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", notes, re.DOTALL))
        
        final_notes = base_notes.strip()
        if tech_reports_text: final_notes += "\n\n" + tech_reports_text.strip()
        if feedback_data: final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        
        update_google_task(latest_task['id'], notes=final_notes, status='completed', due=one_day_ago.isoformat())
        cache.clear()

        scheduled_customer_follow_up_job()
        flash(f"กำลังทดสอบส่งแบบสอบถามสำหรับงานล่าสุด: '{latest_task.get('title')}'", 'info')
    return redirect(url_for('settings_page'))

@app.route('/public_report/<task_id>')
def public_task_report(task_id):
    task = get_single_task(task_id)
    if not task: abort(404)

    notes = task.get('notes', '')
    customer_info = parse_customer_info_from_notes(notes)
    tech_reports, _ = parse_tech_report_from_notes(notes)
    
    latest_report = tech_reports[0] if tech_reports else {}
    equipment_used = latest_report.get('equipment_used', [])
    
    app_settings = get_app_settings()
    catalog = app_settings.get('equipment_catalog', [])
    catalog_dict = {item['item_name']: item for item in catalog}
    sales_offers = app_settings.get('sales_offers', {})


    detailed_costs, total_cost = [], 0.0

    if isinstance(equipment_used, list):
        for item_used in equipment_used:
            item_name = item_used.get('item')
            try:
                quantity = float(re.findall(r"[\d\.]+", item_used.get('quantity', '1'))[0])
            except (IndexError, ValueError): quantity = 1.0

            catalog_item = catalog_dict.get(item_name, {})
            price_per_unit = float(catalog_item.get('price', 0))
            subtotal = quantity * price_per_unit
            total_cost += subtotal
            
            detailed_costs.append({
                'item': item_name, 'quantity': quantity, 'unit': catalog_item.get('unit', ''),
                'price_per_unit': price_per_unit or 'N/A', 'subtotal': subtotal or 'N/A'
            })
            
    return render_template('public_task_report.html', 
                           task=task, 
                           customer_info=customer_info, 
                           latest_report=latest_report, 
                           detailed_costs=detailed_costs, 
                           total_cost=total_cost,
                           sales_offers=sales_offers)

@app.route('/submit_customer_problem', methods=['POST'])
def submit_customer_problem():
    data = request.json
    task_id = data.get('task_id')
    problem_desc = data.get('problem_description')

    if not task_id or not problem_desc:
        return jsonify({"status": "error", "message": "Missing required data"}), 400

    task = get_single_task(task_id)
    if not task: return jsonify({"status": "error", "message": "Task not found"}), 404

    notes = task.get('notes', '')
    feedback_data = parse_customer_feedback_from_notes(notes)
    feedback_data.update({
        'feedback_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
        'feedback_type': 'problem_reported',
        'problem_description': problem_desc
    })
    
    _, base_notes = parse_tech_report_from_notes(notes)
    tech_reports_text = "".join(re.findall(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", notes, re.DOTALL))
    
    final_notes = f"{base_notes.strip()}\n\n{tech_reports_text.strip()}\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
    
    update_google_task(task_id=task_id, notes=final_notes, status='needsAction')
    cache.clear()

    admin_group_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
    if admin_group_id:
        customer_info = parse_customer_info_from_notes(notes)
        notif_text = f"🚨 ลูกค้าแจ้งปัญหา!\nงาน: {task.get('title')}\nลูกค้า: {customer_info.get('name')}\nปัญหา: {problem_desc}\nดูรายละเอียด: {url_for('task_details', task_id=task_id, _external=True)}"
        try:
            line_bot_api.push_message(admin_group_id, TextSendMessage(text=notif_text))
        except Exception as e:
            app.logger.error(f"Failed to send problem notification to admin: {e}")
        
    return jsonify({"status": "success", "message": "Problem reported."})

@app.route('/save_customer_line_id', methods=['POST'])
def save_customer_line_id():
    data = request.json
    task_id = data.get('task_id')
    customer_line_id = data.get('customer_line_user_id')
    
    if not task_id or not customer_line_id:
        return jsonify({"status": "error", "message": "Missing task_id or customer_line_user_id"}), 400

    task = get_single_task(task_id)
    if not task: return jsonify({"status": "error", "message": "Task not found"}), 404

    notes = task.get('notes', '')
    feedback_data = parse_customer_feedback_from_notes(notes)
    feedback_data['customer_line_user_id'] = customer_line_id
    feedback_data['id_saved_date'] = datetime.datetime.now(THAILAND_TZ).isoformat()
    
    _, base_notes = parse_tech_report_from_notes(notes)
    tech_reports_text = "".join(re.findall(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", notes, re.DOTALL))
    
    final_notes = f"{base_notes.strip()}\n\n{tech_reports_text.strip()}\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
    
    if update_google_task(task_id=task_id, notes=final_notes):
        cache.clear()
        shop_info = get_app_settings().get('shop_info', {})
        customer_info = parse_customer_info_from_notes(notes)
        welcome_msg = f"เรียน คุณ{customer_info.get('name', 'ลูกค้า')},\nขอบคุณที่ลงทะเบียนกับ Comphone ครับ/ค่ะ!\nเราจะใช้ LINE นี้เพื่อส่งแบบสอบถามและข้อมูลสำคัญอื่นๆ ครับ\n\nติดต่อสอบถาม:\nโทร: {shop_info.get('contact_phone', '-')}\nLINE ID: {shop_info.get('line_id', '-')}"
        try:
            line_bot_api.push_message(customer_line_id, TextSendMessage(text=welcome_msg))
        except Exception as e:
            app.logger.error(f"Failed to send welcome message to {customer_line_id}: {e}")
        return jsonify({"status": "success"})
    else:
        return jsonify({"status": "error", "message": "Failed to update task"}), 500


# --- LINE Bot Handlers ---
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

def create_task_list_message(title, tasks, limit=5):
    if not tasks:
        return TextSendMessage(text=f"ไม่พบรายการ{title}ในขณะนี้")
    
    message = f"📋 {title}\n\n"
    tasks.sort(key=lambda x: (x.get('due') is None, x.get('due', '')))
    
    for i, task in enumerate(tasks[:limit]):
        customer = parse_customer_info_from_notes(task.get('notes', ''))
        due = parse_google_task_dates(task).get('due_formatted', 'ไม่มีกำหนด')
        message += f"{i+1}. {task.get('title')}\n   - ลูกค้า: {customer.get('name', 'N/A')}\n   - นัดหมาย: {due}\n\n"
    
    if len(tasks) > limit:
        message += f"... และอีก {len(tasks) - limit} รายการ"
    return TextSendMessage(text=message)

def create_task_flex_message(task):
    customer = parse_customer_info_from_notes(task.get('notes', ''))
    dates = parse_google_task_dates(task)
    update_url = url_for('task_details', task_id=task['id'], _external=True)
    
    return BubbleContainer(
        body=BoxComponent(layout='vertical', spacing='md', contents=[
            TextComponent(text=task.get('title', '...'), weight='bold', size='lg', wrap=True),
            SeparatorComponent(margin='md'),
            BoxComponent(layout='vertical', margin='lg', spacing='sm', contents=[
                BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='ลูกค้า:', color='#AAAAAA', size='sm', flex=2), TextComponent(text=customer.get('name', '-'), wrap=True, color='#666666', size='sm', flex=5)]),
                BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='นัดหมาย:', color='#AAAAAA', size='sm', flex=2), TextComponent(text=dates.get('due_formatted', '-'), wrap=True, color='#666666', size='sm', flex=5)])
            ]),
        ]),
        footer=BoxComponent(layout='vertical', spacing='sm', contents=[
            ButtonComponent(style='primary', height='sm', action=URIAction(label='📝 เปิดในเว็บ', uri=update_url))
        ])
    )

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    text = event.message.text.strip().lower()

    command_handlers = {
        'งานค้าง': lambda: [t for t in (get_google_tasks_for_report(False) or []) if t.get('status') == 'needsAction'],
        'งานเสร็จ': lambda: sorted([t for t in (get_google_tasks_for_report(True) or []) if t.get('status') == 'completed'], key=lambda x: x.get('completed', ''), reverse=True),
    }

    if text in command_handlers:
        tasks = command_handlers[text]()
        reply = create_task_list_message(f"รายการ{text}", tasks)
        line_bot_api.reply_message(event.reply_token, reply)
    elif text in ['งานวันนี้', 'งานพรุ่งนี้']:
        target_date = datetime.datetime.now(THAILAND_TZ).date()
        title = "งานวันนี้"
        if 'พรุ่งนี้' in text: 
            target_date += datetime.timedelta(days=1)
            title = "งานพรุ่งนี้"
        
        tasks = [t for t in (get_google_tasks_for_report(False) or []) if t.get('due') and datetime.datetime.fromisoformat(t['due'].replace('Z', '+00:00')).astimezone(THAILAND_TZ).date() == target_date]
        line_bot_api.reply_message(event.reply_token, create_task_list_message(title, tasks))
    elif text == 'สร้างงานใหม่' and LIFF_ID_FORM:
        quick_reply = QuickReply(items=[QuickReplyButton(action=URIAction(label="เปิดฟอร์มสร้างงาน", uri=f"https://liff.line.me/{LIFF_ID_FORM}"))])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="เปิดฟอร์มเพื่อสร้างงานใหม่ครับ 👇", quick_reply=quick_reply))
    elif text.startswith('ดูงาน '):
        name = event.message.text.split(maxsplit=1)[1]
        tasks = [t for t in (get_google_tasks_for_report(True) or []) if name.lower() in parse_customer_info_from_notes(t.get('notes', '')).get('name', '').lower()]
        if not tasks:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"ไม่พบงานของลูกค้า: {name}"))
        else:
            bubbles = [create_task_flex_message(t) for t in tasks[:10]]
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"ผลการค้นหา: {name}", contents=CarouselContainer(contents=bubbles)))
    elif text == 'comphone':
        help_text = "พิมพ์คำสั่ง:\n- งานค้าง\n- งานเสร็จ\n- งานวันนี้\n- งานพรุ่งนี้\n- สร้างงานใหม่\n- ดูงาน [ชื่อลูกค้า]"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text))

@handler.add(PostbackEvent)
def handle_postback(event):
    data = dict(item.split('=') for item in event.postback.data.split('&'))
    action = data.get('action')

    if action == 'customer_feedback':
        task = get_single_task(data.get('task_id'))
        if not task: return

        notes = task.get('notes', '')
        feedback_data = parse_customer_feedback_from_notes(notes)
        feedback_type = data.get('feedback')
        
        feedback_data.update({
            'feedback_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
            'feedback_type': feedback_type,
            'customer_line_user_id': event.source.user_id
        })
        
        _, base_notes = parse_tech_report_from_notes(notes)
        tech_reports_text = "".join(re.findall(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", notes, re.DOTALL))
        final_notes = f"{base_notes.strip()}\n\n{tech_reports_text.strip()}\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        
        update_google_task(task['id'], notes=final_notes)
        cache.clear()
        
        reply_text = "ขอบคุณสำหรับความคิดเห็นครับ/ค่ะ 🙏"
        if feedback_type == 'problem_reported':
            reply_text = "รับทราบปัญหาครับ/ค่ะ ทางเราจะรีบติดต่อกลับเพื่อดูแลโดยเร็วที่สุด"
        
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        except Exception as e:
            app.logger.error(f"Failed to reply to postback: {e}")

        settings = get_app_settings()
        sales_offers = settings.get('sales_offers', {})
        if (sales_offers.get('post_feedback_offer_enabled') and 
            feedback_type in ['very_satisfied', 'satisfied']):
            
            offer_message = sales_offers.get('post_feedback_offer_message')
            if offer_message:
                try:
                    line_bot_api.push_message(event.source.user_id, TextSendMessage(text=offer_message))
                except Exception as e:
                    app.logger.error(f"Failed to send sales offer to {event.source.user_id}: {e}")


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
