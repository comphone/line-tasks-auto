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
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload 

import pandas as pd 

# --- NEW: APScheduler for background tasks ---
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit # เพื่อให้ scheduler หยุดทำงานเมื่อ app ปิด

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
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID') # Folder for general file uploads
GOOGLE_SETTINGS_BACKUP_FOLDER_ID = os.environ.get('GOOGLE_SETTINGS_BACKUP_FOLDER_ID') # NEW: Folder for settings backups

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
    'report_times': { 'appointment_reminder_hour_thai': 7, 'outstanding_report_hour_thai': 20 },
    'line_recipients': { 'admin_group_id': os.environ.get('LINE_ADMIN_GROUP_ID', ''), 'technician_group_id': os.environ.get('LINE_TECHNICIAN_GROUP_ID', '') , 'manager_user_id': ''}, # Added manager_user_id to default
    'qrcode_settings': { 'box_size': 8, 'border': 4, 'fill_color': '#28a745', 'back_color': '#FFFFFF', 'custom_url': '' },
    'equipment_catalog': [],
    'auto_backup': { 'enabled': False, 'hour_thai': 2, 'minute_thai': 0 } 
}
_APP_SETTINGS_STORE = {} # Global variable to hold settings

#<editor-fold desc="Helper and Utility Functions">
def load_settings_from_file():
    """Load application settings from JSON file."""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
        except (json.JSONDecodeError, IOError) as e: 
            app.logger.error(f"Error handling settings.json: {e}")
            # If file is corrupted or empty, delete it and return default settings
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

def get_app_settings():
    """Get current application settings, loading from file or using defaults."""
    global _APP_SETTINGS_STORE
    if not _APP_SETTINGS_STORE:
        loaded = load_settings_from_file()
        # Deep copy default settings to ensure new dictionaries are created
        _APP_SETTINGS_STORE = json.loads(json.dumps(_DEFAULT_APP_SETTINGS_STORE))
        if loaded:
            # Update nested dictionaries carefully
            for key, default_value in _APP_SETTINGS_STORE.items():
                if isinstance(default_value, dict) and key in loaded and isinstance(loaded[key], dict):
                    _APP_SETTINGS_STORE[key].update(loaded[key])
                elif key in loaded: 
                    _APP_SETTINGS_STORE[key] = loaded[key]
        else:
            # If no settings file, save the default settings
            save_settings_to_file(_APP_SETTINGS_STORE)
    
    # Ensure common_equipment_items is always up-to-date
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

# NEW: Function to load settings from Google Drive on startup
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
        # Search for the latest settings_backup.json in the dedicated folder
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
                app.logger.debug(f"Download settings progress: {int(status.progress() * 100)}%.")
            fh.seek(0)
            
            downloaded_settings = json.loads(fh.read().decode('utf-8'))
            
            # Save the downloaded settings locally
            if save_settings_to_file(downloaded_settings):
                app.logger.info("Successfully restored settings from Google Drive backup.")
                # Force reload _APP_SETTINGS_STORE global variable
                global _APP_SETTINGS_STORE
                _APP_SETTINGS_STORE = downloaded_settings 
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

# Attempt to load settings from Google Drive on app startup
load_settings_from_drive_on_startup()

# Initialize settings store (will use loaded settings from Drive or defaults if restore failed)
# This line is called after load_settings_from_drive_on_startup so it picks up restored settings
_APP_SETTINGS_STORE = get_app_settings()

def get_google_service(api_name, api_version):
    """Authenticates and returns a Google API service."""
    creds = None
    token_path = 'token.json'
    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')

    # Try to load credentials from environment variable first
    if google_token_json_str:
        try: 
            creds = Credentials.from_authorized_user_info(json.loads(google_token_json_str), SCOPES)
            app.logger.info("Loaded Google credentials from GOOGLE_TOKEN_JSON environment variable.")
        except Exception as e: 
            app.logger.warning(f"Could not load token from env var, falling back to token.json: {e}")
    
    # Fallback to local token.json file (which will be ephemeral on Render)
    if not creds and os.path.exists(token_path):
        creds = Credentials.from_authorized_file(token_path, SCOPES)
        app.logger.info(f"Loaded Google credentials from local {token_path}.")

    # Refresh token if expired
    if creds and creds.valid and creds.expired and creds.refresh_token:
        try: 
            creds.refresh(Request())
            app.logger.info("Refreshed Google access token.")
            # If refreshed, save back to local file and recommend updating env var
            if not google_token_json_str: # Only save to file if not using env var
                with open(token_path, 'w') as token: token.write(creds.to_json())
                app.logger.info(f"Refreshed token saved to {token_path}. Please update GOOGLE_TOKEN_JSON on Render with this content.")
        except Exception as e:
            app.logger.error(f"Error refreshing token: {e}")
            creds = None # Invalidate creds if refresh fails
    
    # If no valid credentials, try to get new ones from credentials.json (local dev only)
    if not creds or not creds.valid:
        if os.path.exists('credentials.json'):
            app.logger.info("Attempting to get new Google credentials from credentials.json.")
            try:
                # This will typically open a browser for authentication on local dev
                flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                creds = flow.run_console() 
                if creds:
                    with open(token_path, 'w') as token: token.write(creds.to_json())
                    app.logger.info(f"New token saved to {token_path}. Please update GOOGLE_TOKEN_JSON on Render with this content.")
            except Exception as e:
                app.logger.error(f"Error getting new credentials: {e}")
                creds = None
        else:
            app.logger.error("No valid Google credentials available. API service cannot be built.")
            app.logger.error("Please ensure GOOGLE_TOKEN_JSON environment variable is set or credentials.json exists.")

    if not creds or not creds.valid:
        app.logger.error("Final check: No valid Google credentials after all attempts.")
        return None
        
    return build(api_name, api_version, credentials=creds)

def get_google_tasks_service(): return get_google_service('tasks', 'v1')
def get_google_drive_service(): return get_google_service('drive', 'v3')

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
    
    # UPDATED: More robust regex for Google Maps URLs
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

@app.context_processor
def inject_now():
    """Injects current datetime into Jinja2 templates."""
    return {'now': datetime.datetime.now(THAILAND_TZ)}

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

# NEW: Internal function to create the backup zip file
def _create_backup_zip():
    """Creates a zip archive of all tasks, settings, and source code."""
    try:
        all_tasks = get_google_tasks_for_report(show_completed=True)
        all_settings = get_app_settings()
        
        if all_tasks is None or all_settings is None:
            app.logger.error('Failed to get all tasks or settings for backup.')
            return None, None

        memory_file = BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.DEFLATED) as zf:
            zf.writestr('data/tasks_backup.json', json.dumps(all_tasks, indent=4, ensure_ascii=False))
            # No longer write settings_backup.json here as it's uploaded separately
            # zf.writestr('data/settings_backup.json', json.dumps(all_settings, indent=4, ensure_ascii=False)) 
            
            # Include source code in backup
            project_root = os.path.dirname(os.path.abspath(__file__))
            for folder, _, files in os.walk(project_root):
                for file in files:
                    # Exclude temporary or sensitive files from backup
                    if file.endswith(('.py', '.html', '.css', '.js', '.json', '.env', 'Procfile', 'requirements.txt')) and \
                       file not in ['token.json']: # Exclude local token if it exists
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

# NEW: Internal function to upload backup to Google Drive
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
        # Create a MediaIoBaseUpload object from BytesIO
        media = MediaIoBaseUpload(memory_file, mimetype=mime_type, resumable=True)
        file_metadata = {'name': filename, 'parents': [drive_folder_id]}
        
        file_obj = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        
        # Make the uploaded file publicly readable (optional, based on your security needs)
        service.permissions().create(fileId=file_obj['id'], body={'role': 'reader', 'type': 'anyone'}).execute()
        
        app.logger.info(f"Successfully uploaded backup to Drive: {file_obj.get('webViewLink')}")
        return True
    except HttpError as e:
        app.logger.error(f'Google Drive backup upload error for {filename}: {e}')
        return False
    except Exception as e:
        app.logger.error(f"An unexpected error occurred during backup upload for {filename}: {e}")
        return False

# NEW: Scheduled backup job
def scheduled_backup_job():
    """Scheduled job to perform automatic backup to Google Drive."""
    with app.app_context(): # Run within app context for url_for and settings access
        app.logger.info("Running scheduled backup job...")
        
        # 1. Perform full system backup (zip)
        memory_file_zip, filename_zip = _create_backup_zip()
        if memory_file_zip and filename_zip:
            if _upload_backup_to_drive(memory_file_zip, filename_zip, GOOGLE_DRIVE_FOLDER_ID):
                app.logger.info("Automatic full system backup completed successfully to Google Drive.")
            else:
                app.logger.error("Automatic full system backup to Google Drive failed.")
        else:
            app.logger.error("Failed to create full system backup zip file for automatic backup.")

        # 2. Perform settings-only backup (JSON)
        if GOOGLE_SETTINGS_BACKUP_FOLDER_ID:
            settings_data = get_app_settings()
            settings_json_bytes = BytesIO(json.dumps(settings_data, ensure_ascii=False, indent=4).encode('utf-8'))
            settings_backup_filename = "settings_backup.json" # Fixed name for easy retrieval
            
            # Check for existing settings_backup.json and delete it first
            service = get_google_drive_service()
            if service:
                try:
                    query = f"name = '{settings_backup_filename}' and '{GOOGLE_SETTINGS_BACKUP_FOLDER_ID}' in parents"
                    response = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
                    existing_files = response.get('files', [])
                    for f in existing_files:
                        service.files().delete(fileId=f['id']).execute()
                        app.logger.info(f"Deleted existing settings_backup.json (ID: {f['id']}) from Drive.")
                except HttpError as e:
                    app.logger.warning(f"Could not delete existing settings_backup.json: {e}")

            if _upload_backup_to_drive(settings_json_bytes, settings_backup_filename, GOOGLE_SETTINGS_BACKUP_FOLDER_ID):
                app.logger.info("Automatic settings backup completed successfully to Google Drive (JSON).")
            else:
                app.logger.error("Automatic settings backup to Google Drive (JSON) failed.")
        else:
            app.logger.warning("GOOGLE_SETTINGS_BACKUP_FOLDER_ID not set. Skipping automatic settings JSON backup.")


# NEW: Scheduler initialization
scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)

def run_scheduler():
    """Initializes and runs the APScheduler jobs."""
    settings = get_app_settings()
    auto_backup_enabled = settings.get('auto_backup', {}).get('enabled', False)
    auto_backup_hour = settings.get('auto_backup', {}).get('hour_thai', 2)
    auto_backup_minute = settings.get('auto_backup', {}).get('minute_thai', 0)

    # Shutdown existing scheduler to prevent duplicates on reloads (e.g., during debug)
    if scheduler.running:
        app.logger.info("Scheduler is already running. Shutting down existing jobs for reinitialization.")
        scheduler.shutdown(wait=False)
        # ไม่จำเป็นต้อง reinitialize scheduler หรือประกาศ global scheduler อีกครั้ง
        # เพราะตัวแปร scheduler ถูกประกาศเป็น global แล้วที่ด้านบนของไฟล์
        # และเราต้องการทำงานกับ instance เดิม
        
    # Re-add auto backup job based on current settings
    job_id = 'auto_system_backup'
    if auto_backup_enabled:
        if not scheduler.get_job(job_id): # Add if it doesn't exist
            app.logger.info(f"Scheduling automatic backup daily at {auto_backup_hour:02d}:{auto_backup_minute:02d} Thai Time.")
            scheduler.add_job(
                scheduled_backup_job,
                CronTrigger(hour=auto_backup_hour, minute=auto_backup_minute, timezone=THAILAND_TZ),
                id=job_id
            )
        else: # Reschedule if it exists
            app.logger.info(f"Automatic backup job '{job_id}' already exists. Reconfiguring trigger.")
            scheduler.reschedule_job(
                job_id,
                trigger=CronTrigger(hour=auto_backup_hour, minute=auto_backup_minute, timezone=THAILAND_TZ)
            )
    else:
        # If auto backup is disabled, remove the job if it exists
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            app.logger.info("Automatic backup job removed as it is disabled.")

    if not scheduler.running:
        scheduler.start()
        app.logger.info("APScheduler started.")
        # Ensure the scheduler shuts down cleanly when the app exits
        atexit.register(lambda: scheduler.shutdown(wait=False))

# Call run_scheduler on app startup (after initial settings load from Drive or defaults)
# This will schedule the jobs based on current settings
with app.app_context(): # run_scheduler needs app_context for url_for and get_app_settings
    run_scheduler()

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
            flash('สร้างงานใหม่เรียบร้อยแล้ว!', 'success')
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
            upload_errors = [] # NEW: To track upload errors
            if new_attachments_uploaded:
                for file in files:
                    if file and allowed_file(file.filename):
                        filename = secure_filename(file.filename)
                        temp_filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                        try:
                            file.save(temp_filepath)
                            mime_type = file.mimetype or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                            drive_url = upload_file_to_google_drive(temp_filepath, filename, mime_type)
                            if drive_url: 
                                new_attachment_urls.append(drive_url)
                                flash(f"อัปโหลดไฟล์ '{filename}' สำเร็จ!", 'success') # NEW: User feedback
                            else:
                                upload_errors.append(f"ไม่สามารถอัปโหลดไฟล์ '{filename}' ไปยัง Google Drive ได้") # NEW: Track error
                                flash(f"อัปโหลดไฟล์ '{filename}' ไม่สำเร็จ!", 'warning') # NEW: User feedback
                        except Exception as e:
                            upload_errors.append(f"เกิดข้อผิดพลาดในการบันทึกหรืออัปโหลดไฟล์ '{filename}': {e}") # NEW: Track error
                            flash(f"เกิดข้อผิดพลาดในการบันทึกหรืออัปโหลดไฟล์ '{filename}'!", 'warning') # NEW: User feedback
                        finally:
                            if os.path.exists(temp_filepath):
                                os.remove(temp_filepath) # Ensure temporary file is removed
            
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
        
        # Display aggregated upload errors if any
        if upload_errors:
            for err in upload_errors:
                app.logger.error(err) # Log for detailed tracking
        
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
        # --- Handle Logo Upload ---
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
        
        # --- Handle Auto Backup Settings ---
        auto_backup_enabled = request.form.get('auto_backup_enabled') == 'on'
        auto_backup_hour = int(request.form.get('auto_backup_hour'))
        auto_backup_minute = int(request.form.get('auto_backup_minute'))

        # Save other settings
        save_app_settings({
            'report_times': { 
                'appointment_reminder_hour_thai': int(request.form.get('appointment_reminder_hour')), 
                'outstanding_report_hour_thai': int(request.form.get('outstanding_report_hour')) 
            },
            'line_recipients': { 
                'admin_group_id': request.form.get('admin_group_id', '').strip(), 
                'technician_group_id': request.form.get('technician_group_id', '').strip(),
                'manager_user_id': request.form.get('manager_user_id', '').strip() 
            },
            'qrcode_settings': { 
                'box_size': int(request.form.get('qr_box_size', 8)), 
                'border': int(request.form.get('qr_border', 4)), 
                'fill_color': request.form.get('qr_fill_color', '#28a745'), 
                'back_color': request.form.get('qr_back_color', '#FFFFFF'), 
                'custom_url': request.form.get('qr_custom_url', '').strip() 
            },
            'auto_backup': { 
                'enabled': auto_backup_enabled,
                'hour_thai': auto_backup_hour,
                'minute_thai': auto_backup_minute
            }
        })
        
        # After saving settings, re-run the scheduler to apply new backup times
        run_scheduler()

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
    """Web endpoint to manually download a full system backup."""
    memory_file, backup_filename = _create_backup_zip()
    if memory_file and backup_filename:
        flash('สร้างไฟล์สำรองข้อมูลเรียบร้อยแล้ว!', 'success')
        return Response(memory_file, mimetype='application/zip', headers={'Content-Disposition': f'attachment;filename={backup_filename}'})
    else:
        flash('เกิดข้อผิดพลาดในการสร้างไฟล์สำรองข้อมูล', 'danger')
        return redirect(url_for('settings_page'))

@app.route('/trigger_auto_backup_now', methods=['POST'])
def trigger_auto_backup_now():
    """Endpoint to manually trigger the automatic backup job."""
    app.logger.info("Manual trigger for automatic backup initiated.")
    scheduled_backup_job() # Directly call the job function
    flash('ระบบกำลังดำเนินการสำรองข้อมูลอัตโนมัติ (โปรดตรวจสอบ Google Drive ของคุณในภายหลัง)', 'info')
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
            command_map[text_lower](event)
        return
    
    if text_lower.startswith('ดูงาน '):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            handle_view_task_by_name_command(event, parts[1])
            return

    # If the message is not a recognized command, do nothing (remain silent).
    # Removed the 'help_text' reply for unrecognized commands.

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
