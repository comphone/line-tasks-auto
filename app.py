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

from flask import Flask, request, render_template, redirect, url_for, abort, send_from_directory, flash, jsonify, Response, current_app 
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
    ImageComponent,
    PostbackEvent 
)
# ---------------------------------------------

# --- Google API Imports ---
from google.oauth2.credentials import Credentials 
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

# --- Global Configurations (constants, remain global) ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM') 
GOOGLE_TASKS_LIST_ID = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')
GOOGLE_SETTINGS_BACKUP_FOLDER_ID = os.environ.get('GOOGLE_SETTINGS_BACKUP_FOLDER_ID')

SCOPES = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/drive']
THAILAND_TZ = pytz.timezone('Asia/Bangkok')
cache = TTLCache(maxsize=100, ttl=60)

# --- UPLOAD_FOLDER and ALLOWED_EXTENSIONS (Global Scope) ---
UPLOAD_FOLDER = 'static/uploads' 
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'} 

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# --- SETTINGS_FILE (Global Scope) ---
SETTINGS_FILE = 'settings.json' 

# --- DEFAULT APP SETTINGS STORE (Global Scope) ---
_DEFAULT_APP_SETTINGS_STORE = {
    'report_times': { 'appointment_reminder_hour_thai': 7, 'outstanding_report_hour_thai': 20, 'customer_followup_hour_thai': 9 }, 
    'line_recipients': { 'admin_group_id': os.environ.get('LINE_ADMIN_GROUP_ID', ''), 'technician_group_id': os.environ.get('LINE_TECHNICIAN_GROUP_ID', '') , 'manager_user_id': ''}, 
    'qrcode_settings': { 'box_size': 8, 'border': 4, 'fill_color': '#28a745', 'back_color': '#FFFFFF', 'custom_url': '' },
    'equipment_catalog': [],
    'auto_backup': { 'enabled': False, 'hour_thai': 2, 'minute_thai': 0 },
    'shop_info': { 'contact_phone': '081-XXX-XXXX', 'line_id': '@ComphoneService' }
}

# --- Global variable for current app settings (to be populated on app init) ---
_APP_SETTINGS_STORE = {} 

# --- Flask App Instance (Global Scope) ---
app = Flask(__name__, static_folder='static')

# --- Initialize LINE Bot SDK instances (Global Scope) ---
# These need to be global so @handler.add decorators can bind to them directly.
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


# --- Helper and Utility Functions ---
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
    """Authenticates and returns a Google API service."""
    creds = None
    token_path = 'token.json'
    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')

    if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
        app.logger.error("LINE Bot credentials are not set in environment variables.")
        return None 

    if google_token_json_str:
        try: 
            creds = Credentials.from_authorized_user_info(json.loads(google_token_json_str), SCOPES)
            app.logger.info("Loaded Google credentials from GOOGLE_TOKEN_JSON environment variable.")
        except Exception as e: 
            app.logger.warning(f"Could not load token from env var, falling back to token.json: {e}")
    
    if not creds and os.path.exists(token_path):
        creds = Credentials.from_authorized_file(token_path, SCOPES)
        app.logger.info(f"Loaded Google credentials from local {token_path}.")

    if creds and creds.valid and creds.expired and creds.refresh_token:
        try: 
            creds.refresh(Request())
            app.logger.info("Refreshed Google access token.")
            if not google_token_json_str: 
                with open(token_path, 'w') as token: token.write(creds.to_json())
                app.logger.info(f"Refreshed token saved to {token_path}. Please update GOOGLE_TOKEN_JSON on Render with this content.")
        except Exception as e:
            app.logger.error(f"Error refreshing token: {e}")
            creds = None 
    
    if not creds or not creds.valid:
        app.logger.error("No valid Google credentials available. API service cannot be built.")
        app.logger.error("Please ensure GOOGLE_TOKEN_JSON environment variable is set.")
        app.logger.error("If running locally, ensure credentials.json exists for initial setup.")
            
    if not creds or not creds.valid:
        app.logger.error("Final check: No valid Google credentials after all attempts.")
        return None
        
    return build(api_name, api_version, credentials=creds)

def get_google_tasks_service(): return get_google_service('tasks', 'v1')
def get_google_drive_service(): return get_google_service('drive', 'v3')


def load_settings_from_drive_on_startup():
    """
    Attempts to load the latest settings_backup.json from Google Drive
    and save it locally to ensure persistence across Render restarts.
    """
    if not GOOGLE_SETTINGS_BACKUP_FOLDER_ID:
        app.logger.warning("GOOGLE_SETTINGS_BACKUP_FOLDER_ID not set. Skipping settings restore from Drive.")
        return False

    service = get_google_drive_service() 
    if not service:
        app.logger.error("Could not get Drive service for settings restore on startup.")
        return False

    try:
        query = f"name = 'settings_backup.json' and '{GOOGLE_SETTINGS_BACKUP_FOLDER_ID}' in parents"
        response = service.files().list(q=query, spaces='drive', fields='files(id, name, createdTime)', orderBy='createdTime desc', pageSize=1).execute()
        files = response.get('files', [])

        if files:
            latest_backup_file_id = files[0]['id']
            app.logger.info(f"Found latest settings backup on Drive: {files[0]['name']} (ID: {latest_backup_file_id})")

            request = service.files().get_media(fileId=latest_backup_file_id)
            fh = BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
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
    except HttpError as e:
        app.logger.error(f"Google Drive API error during settings restore: {e}")
        return False
    except json.JSONDecodeError as e:
        app.logger.error(f"Error decoding settings JSON from Drive: {e}")
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


@cached(cache)
def get_google_tasks_for_report(show_completed=True):
    """Fetches tasks from Google Tasks API."""
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
    """Fetches a single task from Google Tasks API."""
    service = get_google_tasks_service()
    if not service: return None
    try:
        return service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
    except HttpError as err:
        app.logger.error(f"Error getting single task {task_id}: {err}")
        return None
        
def upload_file_to_google_drive(file_path, file_name, mime_type, folder_id=GOOGLE_DRIVE_FOLDER_ID):
    """Uploads a file to a specified Google Drive folder."""
    service = get_google_drive_service()
    if not service or not folder_id:
        app.logger.error("Drive service or folder ID is not configured for upload.")
        return None
    try:
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        file_metadata = {'name': file_name, 'parents': [folder_id]}
        file_obj = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        
        # Make the uploaded file publicly readable
        service.permissions().create(fileId=file_obj['id'], body={'role': 'reader', 'type': 'anyone'}).execute()
        
        app.logger.info(f"Uploaded to Drive: {file_obj.get('webViewLink')}")
        return file_obj.get('webViewLink')
    except HttpError as e:
        app.logger.error(f'Drive upload error: {e}')
        return None

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
        
def delete_google_task(task_id):
    """Deletes a task from Google Tasks."""
    service = get_google_tasks_service()
    if not service: return False
    try:
        service.tasks().delete(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
        return True
    except HttpError as err:
        app.logger.error(f"API Error deleting task {task_id}: {err}")
        return False

def update_google_task(task_id, title=None, notes=None, status=None, due=None):
    """Updates an existing task in Google Tasks."""
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
    """Parses customer information and map URL from task notes."""
    info = {'name': '', 'phone': '', 'address': '', 'map_url': None}
    if not notes: return info
    
    info['name'] = (re.search(r"ลูกค้า:\s*(.*)", notes, re.IGNORECASE) or re.search(r"customer:\s*(.*)", notes, re.IGNORECASE)).group(1).strip() if (re.search(r"ลูกค้า:", notes) or re.search(r"customer:", notes)) else ''
    info['phone'] = (re.search(r"เบอร์โทรศัพท์:\s*(.*)", notes, re.IGNORECASE) or re.search(r"phone:\s*(.*)", notes, re.IGNORECASE)).group(1).strip() if (re.search(r"เบอร์โทรศัพท์:", notes) or re.search(r"phone:", notes)) else ''
    info['address'] = (re.search(r"ที่อยู่:\s*(.*)", notes, re.IGNORECASE) or re.search(r"address:\s*(.*)", notes, re.IGNORECASE)).group(1).strip() if (re.search(r"ที่อยู่:", notes) or re.search(r"address:", notes)) else ''
    
    app.logger.debug(f"Parsing notes for map_url: {notes}")
    # Regex to capture various Google Maps URL formats (e.g., /maps?q=, /maps/search/, /maps/place/, @lat,long)
    map_url_match = re.search(r"(https?://(?:www\.)?google\.com/maps/(?:place|search)/\?api=1&query=[-\d\.]+,[-\d\.]+|https?://(?:www\.)?google\.com/maps\?q=[-\d\.]+,[-\d\.]+|https?://(?:www\.)?google\.com/maps/@[\d\.]+,[\d\.]+,[\d\.]z.*)", notes)
    if map_url_match:
        info['map_url'] = map_url_match.group(0).strip() # Use group(0) to get the whole matched string
        app.logger.debug(f"Parsed map_url: {info['map_url']}")
        
    if not any(info.values()):
        base_content = re.sub(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL).strip()
        lines = [line.strip() for line in base_content.split('\n') if line.strip()]
        if lines: info['name'] = lines.pop(0)
        if lines: info['phone'] = lines.pop(0)
        if lines: info['address'] = lines.pop(0)
        if lines and re.match(r"https?://(?:www\.)?google\.com/maps.*", lines[0]): info['map_url'] = lines.pop(0)

    return info

# NEW: Function to parse customer feedback data from notes
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
    
    original_notes_text = re.sub(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL).strip()
    
    # Remove customer feedback block from base notes before returning, as it's parsed separately
    original_notes_text = re.sub(r"--- CUSTOMER_FEEDBACK_START ---.*?--- CUSTOMER_FEEDBACK_END ---", "", original_notes_text, flags=re.DOTALL).strip()

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
            equipment_list.append({"item": item_name, "quantity": parts[1].strip() if len(parts) > 1 else ''})
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
                if item.get("quantity"): line += f", {item['quantity']}"
                lines.append(line)
            elif isinstance(item, str): lines.append(item)
    return "\n".join(lines) if lines else 'N/A'

def generate_qr_code_base64(data, box_size=8, border=4, fill_color='black', back_color='white'):
    """Generates a base64 encoded QR code image."""
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

def _create_backup_zip():
    """Creates a zip archive of all tasks, settings, and source code."""
    try:
        all_tasks = get_google_tasks_for_report(show_completed=True)
        all_settings = get_app_settings()
        
        if all_tasks is None or all_settings is None:
            app.logger.error('Failed to get all tasks or settings for backup.')
            return None, None

        memory_file = BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf: 
            zf.writestr('data/tasks_backup.json', json.dumps(all_tasks, indent=4, ensure_ascii=False))
            
            project_root = os.path.dirname(os.path.abspath(__file__))
            for folder, _, files in os.walk(project_root):
                for file in files:
                    if file.endswith(('.py', '.html', '.css', '.js', '.json', '.env', 'Procfile', 'requirements.txt')) and \
                       file not in ['token.json']: 
                        file_path = os.path.join(folder, file)
                        archive_name = os.path.relpath(file_path, project_root)
                        zf.write(file_path, arcname=f'code/{archive_name}')
        memory_file.seek(0)
        backup_filename = f"full_system_backup_{datetime.date.today().strftime('%Y%m%d_%H%M%S')}.zip"
        app.logger.info(f"Created backup zip: {backup_filename}")
        return memory_file, backup_filename
    except Exception as e:
        app.logger.error(f"Error creating full system backup zip: {e}")
        return None, None

def _upload_backup_to_drive(memory_file, filename, drive_folder_id):
    """Uploads the given memory file (zip or json) to Google Drive."""
    if not memory_file or not filename:
        app.logger.error("No memory file or filename provided for Drive upload.")
        return False
    
    service = get_google_drive_service()
    if not service or not drive_folder_id:
        app.logger.error("Drive service or folder ID is not configured for upload.")
        return False
    
    try:
        mime_type = 'application/zip' if filename.endswith('.zip') else 'application/json'
        media = MediaIoBaseUpload(memory_file, mimetype=mime_type, resumable=True) 
        file_metadata = {'name': filename, 'parents': [drive_folder_id]}
        
        file_obj = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        
        service.permissions().create(fileId=file_obj['id'], body={'role': 'reader', 'type': 'anyone'}).execute()
        
        app.logger.info(f"Successfully uploaded backup to Drive: {file_obj.get('webViewLink')}")
        return True
    except HttpError as e:
        app.logger.error(f'Google Drive backup upload error for {filename}: {e}')
        return False
    except Exception as e:
        app.logger.error(f"An unexpected error occurred during backup upload for {filename}: {e}")
        return False

def scheduled_backup_job():
    """Scheduled job to perform automatic backup to Google Drive."""
    with app.app_context(): # Run within app context for url_for and settings access
        app.logger.info("Running scheduled backup job...")
        
        memory_file_zip, filename_zip = _create_backup_zip()
        if memory_file_zip and filename_zip:
            if _upload_backup_to_drive(memory_file_zip, filename_zip, GOOGLE_DRIVE_FOLDER_ID):
                app.logger.info("Automatic full system backup completed successfully to Google Drive.")
            else:
                app.logger.error("Automatic full system backup to Google Drive failed.")
        else:
            app.logger.error("Failed to create full system backup zip file for automatic backup.")

        if GOOGLE_SETTINGS_BACKUP_FOLDER_ID:
            settings_data = get_app_settings()
            settings_json_bytes = BytesIO(json.dumps(settings_data, ensure_ascii=False, indent=4).encode('utf-8'))
            settings_backup_filename = "settings.json" # Changed to settings.json
            
            service = get_google_drive_service()
            if service:
                try:
                    # Search for existing settings.json and delete it first
                    query = f"name = '{settings_backup_filename}' and '{GOOGLE_SETTINGS_BACKUP_FOLDER_ID}' in parents"
                    response = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
                    existing_files = response.get('files', [])
                    for f in existing_files:
                        service.files().delete(fileId=f['id']).execute()
                        app.logger.info(f"Deleted existing settings.json (ID: {f['id']}) from Drive.")
                except HttpError as e:
                    app.logger.warning(f"Could not delete existing settings.json: {e}")

            if _upload_backup_to_drive(settings_json_bytes, settings_backup_filename, GOOGLE_SETTINGS_BACKUP_FOLDER_ID):
                app.logger.info("Automatic settings backup completed successfully to Google Drive (JSON).")
            else:
                app.logger.error("Automatic settings backup to Google Drive (JSON) failed.")
        else:
            app.logger.warning("GOOGLE_SETTINGS_BACKUP_FOLDER_ID not set. Skipping automatic settings JSON backup.")


def scheduled_appointment_reminder_job():
    """
    Scheduled job to send LINE notifications for appointments due today.
    """
    with app.app_context():
        app.logger.info("Running scheduled appointment reminder job...")
        settings = get_app_settings()
        admin_group_id = settings.get('line_recipients', {}).get('admin_group_id', '')
        technician_group_id = settings.get('line_recipients', {}).get('technician_group_id', '')
        
        if not admin_group_id and not technician_group_id:
            app.logger.warning("No LINE recipient IDs configured for appointment reminders. Skipping.")
            return

        tasks_raw = get_google_tasks_for_report(show_completed=False) or []
        
        today_start_thai = THAILAND_TZ.localize(datetime.datetime.combine(datetime.date.today(), datetime.time.min))
        today_end_thai = THAILAND_TZ.localize(datetime.datetime.combine(datetime.date.today(), datetime.time.max))

        upcoming_appointments = []
        for task in tasks_raw:
            if task.get('status') == 'needsAction' and task.get('due'):
                try:
                    due_dt_utc = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                    due_dt_thai = due_dt_utc.astimezone(THAILAND_TZ)
                    
                    if today_start_thai <= due_dt_thai <= today_end_thai:
                        upcoming_appointments.append(task)
                except (ValueError, TypeError):
                    app.logger.warning(f"Could not parse due date for task {task.get('id')}: {task.get('due')}")
                    continue
        
        if not upcoming_appointments:
            app.logger.info("No upcoming appointments found for today.")
            return
            
        # Sort by due date
        upcoming_appointments.sort(key=lambda x: datetime.datetime.fromisoformat(x['due'].replace('Z', '+00:00')) if x.get('due') else datetime.datetime.max.replace(tzinfo=pytz.utc))

        messages_to_send = []
        for task in upcoming_appointments:
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            parsed_dates = parse_google_task_dates(task)
            
            # Concise message format
            message_text = (
                f"🔔 แจ้งเตือนงานนัดหมาย:\n"
                f"ลูกค้า: {customer_info.get('name', '-')}\n"
                f"โทร: {customer_info.get('phone', '-')}\n"
                f"รายละเอียด: {task.get('title', '-').splitlines()[0] if task.get('title') else '-'}\n" # Take first line of title
                f"นัดหมาย: {parsed_dates.get('due_formatted', '-')}\n\n"
                f"ดูรายละเอียดงานเพิ่มเติม: {url_for('task_details', task_id=task.get('id'), _external=True)}"
            )
            messages_to_send.append(TextSendMessage(text=message_text))

        # Send messages to recipients
        if messages_to_send:
            try:
                if admin_group_id:
                    line_bot_api.push_message(admin_group_id, messages_to_send)
                    app.logger.info(f"Sent {len(messages_to_send)} appointment reminders to admin group.")
                if technician_group_id and technician_group_id != admin_group_id: # Avoid sending duplicate if same group
                    line_bot_api.push_message(technician_group_id, messages_to_send)
                    app.logger.info(f"Sent {len(messages_to_send)} appointment reminders to technician group.")
            except Exception as e:
                app.logger.error(f"Failed to send appointment reminder LINE messages: {e}")


# NEW: Function to create the customer follow-up Flex Message
def _create_customer_follow_up_flex_message(task_id, task_title, customer_name, customer_phone, technician_id_to_mention=None):
    detail_url = url_for('task_details', task_id=task_id, _external=True)
    
    # Text for manager mention (if available)
    mention_text = ""
    if technician_id_to_mention:
        mention_text = f"ผู้ดูแล: @{technician_id_to_mention}\n" 

    return BubbleContainer(
        direction='ltr',
        body=BoxComponent(
            layout='vertical',
            spacing='md',
            contents=[
                TextComponent(text="🙏 แบบสอบถามความพึงพอใจบริการ 🙏", weight='bold', size='md', color='#1DB446'),
                SeparatorComponent(margin='md'),
                TextComponent(text=f"งาน: {task_title}", size='sm', wrap=True),
                TextComponent(text=f"ลูกค้า: {customer_name}", size='sm', wrap=True),
                TextComponent(text=f"โทร: {customer_phone}", size='sm'),
                TextComponent(text="\nคุณพอใจกับบริการซ่อมของเราหรือไม่?", size='sm', wrap=True, weight='bold'),
                BoxComponent(
                    layout='vertical',
                    spacing='sm',
                    contents=[
                        ButtonComponent(
                            style='primary',
                            height='sm',
                            action=PostbackAction(label='👍 พอใจมาก', data=f'action=customer_feedback&task_id={task_id}&feedback=very_satisfied', display_text='ขอบคุณสำหรับความคิดเห็นที่ดีครับ!'),
                            color='#33CC33'
                        ),
                        ButtonComponent(
                            style='secondary',
                            height='sm',
                            action=PostbackAction(label='👌 พอใจ', data=f'action=customer_feedback&task_id={task_id}&feedback=satisfied', display_text='ขอบคุณสำหรับความคิดเห็นครับ!'),
                            color='#66CC99'
                        ),
                        ButtonComponent(
                            style='danger',
                            height='sm',
                            action=URIAction(label='👎 มีปัญหา', uri=f"https://liff.line.me/{LIFF_ID_FORM}?page=customer_problem&task_id={task_id}"), 
                            color='#FF6666'
                        )
                    ]
                ),
                SeparatorComponent(margin='md'),
                ButtonComponent(
                    style='link',
                    height='sm',
                    action=URIAction(label='ดูรายละเอียดงานเต็ม', uri=detail_url),
                    color='#007BFF'
                )
            ]
        )
    )

# NEW: Scheduled job for customer follow-up
def scheduled_customer_follow_up_job():
    """
    Scheduled job to send customer follow-up surveys after 24-48 hours of completion.
    """
    with app.app_context():
        app.logger.info("Running scheduled customer follow-up job...")
        settings = get_app_settings()
        admin_group_id = settings.get('line_recipients', {}).get('admin_group_id', '')
        technician_group_id = settings.get('line_recipients', {}).get('technician_group_id', '')
        
        if not admin_group_id and not technician_group_id:
            app.logger.warning("No LINE recipient IDs configured for customer follow-up. Skipping.")
            return

        tasks_raw = get_google_tasks_for_report(show_completed=True) or []
        
        now_thai = datetime.datetime.now(THAILAND_TZ)
        one_day_ago = now_thai - datetime.timedelta(days=1)
        two_days_ago = now_thai - datetime.timedelta(days=2) # For 24-48 hour window

        follow_up_tasks = []
        for task in tasks_raw:
            if task.get('status') == 'completed' and task.get('completed'):
                try:
                    completed_dt_utc = datetime.datetime.fromisoformat(task['completed'].replace('Z', '+00:00'))
                    completed_dt_thai = completed_dt_utc.astimezone(THAILAND_TZ)
                    
                    # Check if completed 24-48 hours ago AND no follow-up sent yet
                    if two_days_ago <= completed_dt_thai < one_day_ago:
                        notes_text = task.get('notes', '')
                        customer_feedback_data = parse_customer_feedback_from_notes(notes_text)
                        
                        follow_up_sent_flag = customer_feedback_data.get('follow_up_sent_date') is not None
                        
                        if not follow_up_sent_flag:
                            follow_up_tasks.append(task)
                            task['_customer_line_user_id'] = customer_feedback_data.get('customer_line_user_id') # Store customer ID if found
                            # Add temporary flag for this run to avoid duplicate in same batch
                            task['_temp_follow_up_eligible'] = True 

                except (ValueError, TypeError):
                    app.logger.warning(f"Could not parse completed date for task {task.get('id')}: {task.get('completed')}")
                    continue
        
        if not follow_up_tasks:
            app.logger.info("No tasks eligible for customer follow-up today.")
            return
            
        for task in follow_up_tasks:
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            customer_line_user_id_for_task = task.get('_customer_line_user_id') # Get stored ID for this task
            
            customer_follow_up_flex = _create_customer_follow_up_flex_message(
                task_id=task.get('id'),
                task_title=task.get('title'),
                customer_name=customer_info.get('name'),
                customer_phone=customer_info.get('phone')
            )
            
            # Mark task as followed-up in Google Tasks notes to prevent resending
            current_notes = task.get('notes', '')
            tech_history_existing, base_customer_info_notes_existing = parse_tech_report_from_notes(current_notes)
            customer_feedback_existing = parse_customer_feedback_from_notes(current_notes) 
            
            customer_feedback_existing.update({
                'follow_up_sent_date': now_thai.strftime("%Y-%m-%d %H:%M:%S"),
                'initial_feedback': 'waiting_for_feedback', # Initial state
                'customer_line_user_id': customer_line_user_id_for_task # Ensure customer ID is saved in notes now
            })

            final_notes = base_customer_info_notes_existing.strip() 
            if tech_history_existing: 
                all_reports_text = ""
                for report in sorted(tech_history_existing, key=lambda x: x.get('summary_date', '')):
                    all_reports_text += f"\n\n--- TECH_REPORT_START ---\n{json.dumps(report, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"
                final_notes += all_reports_text

            final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(customer_feedback_existing, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

            update_google_task(task_id=task['id'], notes=final_notes, status=task['status'], due=task.get('due'))
            app.logger.info(f"Marked task {task.get('id')} as follow-up sent and updated notes.")
            cache.clear() 

            # --- Sending Logic: Direct to Customer vs. Admin Group ---
            if customer_line_user_id_for_task:
                app.logger.info(f"Sending direct follow-up to customer {customer_line_user_id_for_task} for task {task.get('id')}.")
                try:
                    line_bot_api.push_message(customer_line_user_id_for_task, FlexSendMessage(alt_text="แบบสอบถามความพึงพอใจบริการ", contents=customer_follow_up_flex))
                    # Also send a small notification to admin that direct message was sent
                    admin_notify_text = f"✅ ส่งแบบสอบถามความพึงพอใจลูกค้า [{customer_info.get('name', '-')}] ไปยังลูกค้าโดยตรงเรียบร้อยแล้ว"
                    if admin_group_id:
                        line_bot_api.push_message(admin_group_id, TextSendMessage(text=admin_notify_text))
                except Exception as e:
                    app.logger.error(f"Failed to send direct customer follow-up to {customer_line_user_id_for_task}: {e}")
                    # Fallback to group if direct send fails
                    admin_fallback_text = (
                        f"⚠️ ส่งแบบสอบถามความพึงพอใจลูกค้า [{customer_info.get('name', '-')}] โดยตรงไม่สำเร็จ\n"
                        f"โปรดส่งแบบสอบถามนี้ให้ลูกค้าแทนครับ/ค่ะ\n"
                        f"รายละเอียดงาน: {task.get('title', '-').splitlines()[0] if task.get('title') else '-'}\n"
                        f"ลิงก์งาน: {url_for('task_details', task_id=task.get('id'), _external=True)}"
                    )
                    if admin_group_id:
                        line_bot_api.push_message(admin_group_id, [TextSendMessage(text=admin_fallback_text), FlexSendMessage(alt_text="แบบสอบถามความพึงพอใจบริการ", contents=customer_follow_up_flex)])
                    app.logger.info(f"Sent fallback follow-up to admin group for task {task.get('id')}.")
            else:
                app.logger.info(f"No LINE User ID for customer {customer_info.get('name', '-')}. Sending follow-up to admin/tech group for manual forwarding.")
                admin_message_text = (
                    f"✅ งานซ่อมลูกค้า {customer_info.get('name', '-')}(โทร: {customer_info.get('phone', '-')}) เสร็จสิ้นครบ 1 วันแล้ว:\n"
                    f"โปรดส่งแบบสอบถามติดตามผลนี้ให้ลูกค้าครับ/ค่ะ\n"
                    f"รายละเอียดงาน: {task.get('title', '-').splitlines()[0] if task.get('title') else '-'}\n"
                    f"ลิงก์งาน: {url_for('task_details', task_id=task.get('id'), _external=True)}"
                )
                messages_to_send_group = [TextSendMessage(text=admin_message_text), FlexSendMessage(alt_text="แบบสอบถามความพึงพอใจบริการ", contents=customer_follow_up_flex)]

                try:
                    if admin_group_id:
                        line_bot_api.push_message(admin_group_id, messages_to_send_group)
                        app.logger.info(f"Sent group follow-up messages for task {task.get('id')} to admin group.")
                    if technician_group_id and technician_group_id != admin_group_id:
                        line_bot_api.push_message(technician_group_id, messages_to_send_group)
                        app.logger.info(f"Sent group follow-up messages for task {task.get('id')} to technician group.")
                except Exception as e:
                    app.logger.error(f"Failed to send group follow-up LINE messages for task {task.get('id')}: {e}")
            
@app.route('/generate_customer_onboarding_qr')
def generate_customer_onboarding_qr():
    """Generates QR code for customer onboarding."""
    task_id = request.args.get('task_id')
    task = get_single_task(task_id)
    if not task:
        flash("ไม่พบข้อมูลงานสำหรับสร้าง QR Code", 'danger')
        return redirect(url_for('summary'))

    onboarding_liff_url = f"https://liff.line.me/{LIFF_ID_FORM}?page=onboarding&task_id={task_id}"
    
    qr_code_base64 = generate_qr_code_base64(onboarding_liff_url, box_size=10, border=4, fill_color='#000000', back_color='#FFFFFF')
    
    customer_info = parse_customer_info_from_notes(task.get('notes', ''))

    return render_template('generate_onboarding_qr.html', 
                           qr_code_base64=qr_code_base64,
                           task=task,
                           customer_info=customer_info,
                           onboarding_url=onboarding_liff_url)

@app.route('/customer_onboarding')
def customer_onboarding_page():
    """Renders the customer onboarding LIFF form."""
    task_id = request.args.get('task_id') 
    task = get_single_task(task_id) 
    if not task:
        return render_template('liff_close_page.html', message="ไม่พบข้อมูลงาน")

    parsed_task = parse_google_task_dates(task)
    parsed_task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
    
    return render_template('customer_onboarding.html', task=parsed_task)


@app.route('/save_customer_line_id', methods=['POST'])
def save_customer_line_id():
    """Saves customer LINE User ID to Google Task notes."""
    task_id = request.form.get('task_id')
    customer_line_user_id = request.form.get('customer_line_user_id')
    
    task = get_single_task(task_id)
    if not task:
        return jsonify({"status": "error", "message": "Task not found"}), 404

    current_notes = task.get('notes', '')
    
    tech_history, base_customer_info_notes = parse_tech_report_from_notes(current_notes)
    customer_feedback_existing = parse_customer_feedback_from_notes(current_notes)
    
    customer_feedback_existing['customer_line_user_id'] = customer_line_user_id
    customer_feedback_existing['id_saved_date'] = datetime.datetime.now(THAILAND_TZ).strftime("%Y-%m-%d %H:%M:%S")
    
    final_notes = base_customer_info_notes.strip()
    if tech_history:
        all_reports_text = ""
        for report in sorted(tech_history, key=lambda x: x.get('summary_date', '')):
            final_notes += f"\n\n--- TECH_REPORT_START ---\n{json.dumps(report, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"
        
    final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(customer_feedback_existing, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
    
    updated_task = update_google_task(task_id=task_id, notes=final_notes, status=task['status'], due=task.get('due'))
    
    if updated_task:
        app.logger.info(f"Successfully saved customer LINE ID {customer_line_user_id} for task {task_id}.")
        cache.clear()
        
        settings = get_app_settings()
        shop_info = settings.get('shop_info', {})
        customer_info = parse_customer_info_from_notes(task.get('notes', ''))
        
        welcome_message = (
            f"เรียน ลูกค้า {customer_info.get('name', '-')},\n"
            f"Comphone ยินดีที่ได้ให้บริการครับ/ค่ะ! 😊\n"
            f"เราจะใช้ LINE นี้ในการส่งข้อมูลสำคัญหรือโปรโมชั่นพิเศษในอนาคตครับ/ค่ะ\n\n"
            f"หากมีข้อสงสัยใดๆ หรือต้องการความช่วยเหลือเพิ่มเติม ติดต่อเราได้ที่:\n"
            f"โทร: {shop_info.get('contact_phone', '081-XXX-XXXX')}\n"
            f"LINE ID: {shop_info.get('line_id', '@ComphoneService')}\n\n"
            f"ขอบคุณที่เลือกใช้บริการ Comphone ครับ/ค่ะ"
        )
        try:
            line_bot_api.push_message(customer_line_user_id, TextSendMessage(text=welcome_message))
            app.logger.info(f"Sent welcome message to new customer {customer_line_user_id}.")
        except Exception as e:
            app.logger.error(f"Failed to send welcome message to customer {customer_line_user_id}: {e}")

        return jsonify({"status": "success", "message": "LINE ID saved"}), 200
    else:
        app.logger.error(f"Failed to save customer LINE ID {customer_line_user_id} for task {task_id}.")
        return jsonify({"status": "error", "message": "Failed to save LINE ID"}), 500


@app.route('/customer_problem_form')
def customer_problem_form():
    """Renders the customer problem LIFF form."""
    task_id = request.args.get('task_id')
    task = get_single_task(task_id)
    if not task:
        flash("ไม่พบข้อมูลงานสำหรับแจ้งปัญหา", 'danger')
        return redirect(url_for('summary'))
    
    parsed_task = parse_google_task_dates(task)
    parsed_task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))

    return render_template('customer_problem_form.html', task=parsed_task)

@app.route('/submit_customer_problem', methods=['POST'])
def submit_customer_problem():
    """Handles submission from customer problem form."""
    task_id = request.form.get('task_id')
    problem_description = request.form.get('problem_description')
    preferred_datetime_str = request.form.get('preferred_datetime')

    task = get_single_task(task_id)
    if not task:
        flash("ไม่พบข้อมูลงานที่เกี่ยวข้อง", 'danger')
        return redirect(url_for('summary'))

    preferred_datetime_thai = None
    if preferred_datetime_str:
        try:
            preferred_datetime_thai = THAILAND_TZ.localize(datetime.datetime.strptime(preferred_datetime_str, "%Y-%m-%dT%H:%M"))
        except ValueError:
            app.logger.error(f"Invalid preferred_datetime format from form: {preferred_datetime_thai}")
    
    customer_problem_data = {
        'problem_date': datetime.datetime.now(THAILAND_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        'problem_description': problem_description,
        'preferred_datetime': preferred_datetime_thai.strftime("%Y-%m-%d %H:%M") if preferred_datetime_thai else 'ไม่มีระบุ',
        'feedback_type': 'problem_reported'
    }

    current_notes = task.get('notes', '')
    customer_feedback_existing = parse_customer_feedback_from_notes(current_notes)
    customer_feedback_existing.update(customer_problem_data) 
    customer_line_user_id = customer_feedback_existing.get('customer_line_user_id') 

    tech_reports_history, base_customer_info_notes = parse_tech_report_from_notes(current_notes)
    
    final_notes = base_customer_info_notes.strip()
    if tech_reports_history: 
        all_reports_text = ""
        for report in sorted(tech_history_existing, key=lambda x: x.get('summary_date', '')):
            final_notes += f"\n\n--- TECH_REPORT_START ---\n{json.dumps(report, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"
        
    final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(customer_feedback_existing, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

    updated_task = update_google_task(
        task_id=task_id,
        notes=final_notes,
        status='needsAction', 
        due=task.get('due') 
    )
    cache.clear()

    if updated_task:
        flash('บันทึกปัญหาและแจ้งผู้ดูแลเรียบร้อยแล้ว!', 'success')
        
        settings = get_app_settings()
        admin_group_id = settings.get('line_recipients', {}).get('admin_group_id', '')
        manager_user_id = settings.get('line_recipients', {}).get('manager_user_id', '')
        shop_info = settings.get('shop_info', {})
        
        customer_info = parse_customer_info_from_notes(task.get('notes', ''))
        
        notification_text = (
            f"🚨 ลูกค้าแจ้งปัญหา! 🚨\n"
            f"งาน: {task.get('title', '-')}\n"
            f"ลูกค้า: {customer_info.get('name', '-')}\n"
            f"โทร: {customer_info.get('phone', '-')}\n"
            f"รายละเอียดปัญหา: {problem_description or '-'}\n"
            f"วันเวลาที่ลูกค้าสะดวก: {preferred_datetime_thai.strftime('%d/%m/%y %H:%M') if preferred_datetime_thai else 'ไม่มีระบุ'}\n"
            f"สถานะงานถูกเปลี่ยนเป็น: 'ยังไม่เสร็จ'\n\n"
            f"ดูรายละเอียดงาน: {url_for('task_details', task_id=task_id, _external=True)}"
        )
        if manager_user_id:
            notification_text += f"\n(ถึงผู้ดูแล: @{manager_user_id})" 

        if admin_group_id:
            try:
                line_bot_api.push_message(admin_group_id, TextSendMessage(text=notification_text))
                app.logger.info(f"Sent problem notification for task {task_id} to admin group.")
            except Exception as e:
                app.logger.error(f"Failed to send problem notification to admin group: {e}")

        if customer_line_user_id:
            thank_you_message_to_customer = (
                f"เรียน ลูกค้า {customer_info.get('name', '-')},\n"
                f"Comphone ได้รับแจ้งปัญหาเกี่ยวกับงาน: {task.get('title', '-')}\n"
                f"เรียบร้อยแล้วครับ/ค่ะ\n"
                f"ทีมงานกำลังตรวจสอบข้อมูลและจะติดต่อกลับเพื่อดูแลท่านโดยเร็วที่สุดครับ/ค่ะ\n\n"
                f"หากมีข้อสงสัยใดๆ หรือต้องการความช่วยเหลือเพิ่มเติม ติดต่อเราได้ที่:\n"
                f"โทร: {shop_info.get('contact_phone', '081-XXX-XXXX')}\n"
                f"LINE ID: {shop_info.get('line_id', '@ComphoneService')}\n\n"
                f"ขออภัยในความไม่สะดวกอีกครั้งครับ/ค่ะ\n"
                f"Comphone - ยินดีบริการเสมอครับ/ค่ะ"
            )
            try:
                line_bot_api.push_message(customer_line_user_id, TextSendMessage(text=thank_you_message_to_customer))
                app.logger.info(f"Sent thank you message to customer {customer_line_user_id}.")
            except Exception as e:
                app.logger.error(f"Failed to send thank you message to customer {customer_line_user_id}: {e}")

    else:
        flash('เกิดข้อผิดพลาดในการบันทึกปัญหา', 'danger')

    return render_template('liff_close_page.html', message="บันทึกข้อมูลเรียบร้อยแล้ว!")

@app.route('/liff_close_page')
def liff_close_page():
    message = request.args.get('message', 'ดำเนินการเสร็จสิ้น')
    return f"""
    <!DOCTYPE html>
    <html lang="th">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>ยืนยัน</title>
        <script src="https://static.line-scdn.net/liff/2.21.0/sdk.js"></script>
        <style>
            body {{ font-family: sans-serif; text-align: center; padding: 20px; }}
            .message {{ font-size: 1.2em; color: #333; }}
        </style>
    </head>
    <body>
        <div class="message">{message}</div>
        <script>
            onload = function() {{ 
                if (liff.isInClient()) {{
                    setTimeout(() => {{ liff.closeWindow(); }}, 1000);
                }}
            }};
        </script>
    </body>
    </html>
    """

# --- LINE Bot Handler ---
# This function handles all incoming LINE webhook events (messages and postbacks).
@handler.add(MessageEvent, message=TextMessage)
@handler.add(PostbackEvent)
def handle_line_events(event): 
    """Handles various LINE webhook events (messages and postbacks)."""
    if isinstance(event, PostbackEvent):
        app.logger.info(f"Received PostbackEvent: {event.postback.data}")
        data = event.postback.data
        params = dict(item.split('=') for item in data.split('&'))

        action = params.get('action')
        if action == 'customer_feedback':
            task_id = params.get('task_id')
            feedback_type = params.get('feedback')
            customer_line_user_id = event.source.userId 

            task = get_single_task(task_id)
            if not task:
                app.logger.error(f"Postback for unknown task_id: {task_id}")
                return
            
            current_notes = task.get('notes', '')
            tech_reports_history, base_customer_info_notes = parse_tech_report_from_notes(current_notes)
            customer_feedback_data_existing = parse_customer_feedback_from_notes(current_notes) 
            
            customer_feedback_data_existing.update({
                'feedback_date': datetime.datetime.now(THAILAND_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                'feedback_type': feedback_type,
                'customer_line_user_id': customer_line_user_id 
            })
            
            feedback_json_str = json.dumps(customer_feedback_data_existing, ensure_ascii=False, indent=2)
            
            final_notes = base_customer_info_notes.strip()
            if tech_reports_history: 
                all_reports_text = ""
                for report in sorted(tech_reports_history, key=lambda x: x.get('summary_date', '')):
                    final_notes += f"\n\n--- TECH_REPORT_START ---\n{json.dumps(report, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"
                
            final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{feedback_json_str}\n--- CUSTOMER_FEEDBACK_END ---"

            new_task_status = 'needsAction' if feedback_type == 'problem' else task['status']
            update_google_task(task_id=task_id, notes=final_notes, status=new_task_status, due=task.get('due'))
            app.logger.info(f"Task {task_id} updated with feedback: {feedback_type}. Status set to {new_task_status}. Customer ID: {customer_line_user_id}")
            cache.clear()

            if feedback_type == 'problem':
                settings = get_app_settings()
                admin_group_id = settings.get('line_recipients', {}).get('admin_group_id', '')
                manager_user_id = settings.get('line_recipients', {}).get('manager_user_id', '')
                
                customer_info = parse_customer_info_from_notes(task.get('notes', ''))
                
                notification_messages = []
                notification_text = (
                    f"⚠️ แจ้งเตือน: ลูกค้าแจ้งปัญหา! ⚠️\n"
                    f"งาน: {task.get('title', '-')}\n"
                    f"ลูกค้า: {customer_info.get('name', '-')}\n"
                    f"โทร: {customer_info.get('phone', '-')}\n"
                    f"รายละเอียดปัญหา: {problem_description or '-'}\n"
                    f"วันเวลาที่ลูกค้าสะดวก: {parsed_dates.get('due_formatted', '-')}\n" # Use parsed_dates for formatted time
                    f"สถานะงานถูกเปลี่ยนเป็น: 'ยังไม่เสร็จ'\n\n" 
                    f"โปรดตรวจสอบและติดต่อกลับลูกค้า:\n{url_for('task_details', task_id=task_id, _external=True)}\n"
                )

                if manager_user_id:
                    notification_text += f"\n(ถึงผู้ดูแล: @{manager_user_id})" 
                
                problem_form_url_for_admin = url_for('customer_problem_form', task_id=task_id, _external=True)
                notification_text += f"\nลิงก์แจ้งปัญหาลูกค้า: {problem_form_url_for_admin}"


                notification_messages.append(TextSendMessage(text=notification_text))
                
                if admin_group_id:
                    try:
                        line_bot_api.push_message(admin_group_id, notification_messages)
                        app.logger.info(f"Sent problem notification for task {task_id} to admin group.")
                    except Exception as e:
                        app.logger.error(f"Failed to send problem notification to admin group: {e}")
            
        
    elif isinstance(event, MessageEvent) and isinstance(event.message, TextMessage):
        text = event.message.text.strip()
        text_lower = text.lower()
        
        command_map = {
            'งานค้าง': handle_outstanding_tasks_command,
            'งานเสร็จ': handle_completed_tasks_command,
            'งานวันนี้': lambda e: handle_daily_tasks_command(e, 'today'),
            'งานพรุ่งนี้': lambda e: handle_daily_tasks_command(e, 'tomorrow'),
            'สร้างงานใหม่': handle_create_new_task_command, 
            'สรุปรายงาน': lambda e: line_bot_api.reply_message(e.reply_token, TextSendMessage(text=f"ดูสรุปรายงานทั้งหมดได้ที่: {url_for('summary', _external=True)}")),
            'comphone': None 
        }
        
        if text_lower in command_map:
            if text_lower == 'comphone':
                help_text = (
                    "สวัสดีครับ! พิมพ์คำสั่งที่ต้องการ:\n\n"
                    "➡️ `งานค้าง`\nดูรายการงานที่ยังไม่เสร็จ\n\n"
                    "➡️ `งานเสร็จ`\nดูงานที่ทำเสร็จล่าสุด\n\n"
                    "➡️ `งานวันนี้`\nดูงานที่มีกำหนดเสร็จในวันนี้\n\n"
                    "➡️ `งานพรุ่งนี้`\nดูงานที่มีกำหนดเสร็จในวันพรุ่งนี้\n\n"
                    "➡️ `ดูงาน ชื่อลูกค้า`\nค้นหางานของลูกค้าคนนั้นๆ (เช่น: `ดูงาน สมศรี`)\n\n"
                    "➡️ `สร้างงานใหม่`\nเปิดฟอร์มสำหรับสร้างงานใหม่\n\n"
                    "➡️ `สรุปรายงาน`\nรับลิงก์เพื่อเปิดเว็บสรุปงาน\n\n"
                    "หากคุณต้องการดูเมนูนี้อีกครั้ง พิมพ์ `comphone`"
                )
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text))
            else:
                if command_map[text_lower]: 
                    command_map[text_lower](event) 
                return
        
        if text_lower.startswith('ดูงาน '):
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                handle_view_task_by_name_command(event, parts[1])
                return

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
