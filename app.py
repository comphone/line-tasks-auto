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
    ImageComponent,
    PostbackEvent 
)
# ---------------------------------------------

# --- Google API Imports (สำคัญ: InstalledAppFlow ต้องถูก import) ---
from google.oauth2.credentials import Credentials 
from google.auth.transport.requests import Request 
from google_auth_oauthlib.flow import InstalledAppFlow 
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload 
from googleapiclient.http import MediaIoBaseUpload 

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
    'report_times': { 'appointment_reminder_hour_thai': 7, 'outstanding_report_hour_thai': 20, 'customer_followup_hour_thai': 9 }, 
    'line_recipients': { 'admin_group_id': os.environ.get('LINE_ADMIN_GROUP_ID', ''), 'technician_group_id': os.environ.get('LINE_TECHNICIAN_GROUP_ID', '') , 'manager_user_id': ''}, 
    'qrcode_settings': { 'box_size': 8, 'border': 4, 'fill_color': '#28a745', 'back_color': '#FFFFFF', 'custom_url': '' },
    'equipment_catalog': [],
    'auto_backup': { 'enabled': False, 'hour_thai': 2, 'minute_thai': 0 },
    'shop_info': { 'contact_phone': '081-XXX-XXXX', 'line_id': '@ComphoneService' } # NEW: Shop contact info
}
_APP_SETTINGS_STORE = {} 

#<editor-fold desc="Helper and Utility Functions">
# --- All Helper and Utility Functions should be defined first ---

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

# get_google_service must be defined before get_google_drive_service
def get_google_service(api_name, api_version):
    """Authenticates and returns a Google API service."""
    creds = None
    token_path = 'token.json'
    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')

    # Try to load credentials from environment variable first (PREFERRED for Render)
    if google_token_json_str:
        try: 
            creds = Credentials.from_authorized_user_info(json.loads(google_token_json_str), SCOPES)
            app.logger.info("Loaded Google credentials from GOOGLE_TOKEN_JSON environment variable.")
        except Exception as e: 
            app.logger.warning(f"Could not load token from env var, falling back to token.json: {e}")
    
    # Fallback to local token.json file (Ephemeral on Render, only useful for initial local setup)
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
    
    # --- IMPORTANT: REMOVED run_console() FOR DEPLOYMENT ON RENDER ---
    # This block is for local development only, where InstalledAppFlow.run_console() can open a browser.
    # On Render, this will cause an error because there is no display/console for interaction.
    # The primary authentication method for Render must be GOOGLE_TOKEN_JSON.
    if not creds or not creds.valid:
        # if os.path.exists('credentials.json'): 
        #     app.logger.info("Attempting to get new Google credentials from credentials.json.")
        #     try:
        #         flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
        #         creds = flow.run_console() # THIS LINE CAUSES ERROR ON RENDER
        #         if creds:
        #             with open(token_path, 'w') as token: token.write(creds.to_json())
        #             app.logger.info(f"New token saved to {token_path}. Please update GOOGLE_TOKEN_JSON on Render with this content.")
        #     except Exception as e:
        #         app.logger.error(f"Error getting new credentials: {e}")
        #         creds = None
        # else: 
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

    service = get_google_drive_service() # This function is now defined
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
                # We update the global _APP_SETTINGS_STORE later when get_app_settings is called
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

# get_app_settings and save_app_settings need to be defined after basic loader/saver functions
def get_app_settings():
    """Get current application settings, loading from file or using defaults."""
    global _APP_SETTINGS_STORE
    # If _APP_SETTINGS_STORE is empty, it means this is the first call,
    # or it was intentionally cleared. We try to load from file (which might
    # have been restored from Drive).
    if not _APP_SETTINGS_STORE: 
        loaded = load_settings_from_file()
        _APP_SETTINGS_STORE = json.loads(json.dumps(_DEFAULT_APP_SETTINGS_STORE)) # Start with defaults
        if loaded:
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
    current_settings = get_app_settings() # Get current settings (may load from file if not in global var)
    for key, value in settings_data.items():
        if isinstance(value, dict) and key in current_settings and isinstance(current_settings[key], dict):
            current_settings[key].update(value)
        else: 
            current_settings[key] = value
    _APP_SETTINGS_STORE = current_settings # Update global variable
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
        # Corrected zipfile.DEFLATED to zipfile.ZIP_DEFLATED
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf: 
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
        media = MediaIoBaseUpload(memory_file, mimetype=mime_type, resumable=True) 
        file_metadata = {'name': filename, 'parents': [drive_folder_id]}
        
        file_obj = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        
        # Make the uploaded file publicly readable
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


# NEW: Scheduled job for appointment reminders
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
                    # LINE API allows sending multiple messages in a single push_message call
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
                            # NEW: For 'problem' feedback, point to a LIFF URL for problem form
                            action=URIAction(label='👎 มีปัญหา', uri=f"https://liff.line.me/{LIFF_ID_FORM}?page=customer_problem&task_id={task_id}"), # Pass task_id
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
        
        # New: If customer_line_user_id is available, send directly to customer.
        # Otherwise, send to admin/technician group for manual forwarding.
        send_to_customer_directly = False # Default to false unless we have customer ID
        
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
            
# NEW: handle postback event for customer feedback
@handler.add(MessageEvent, message=TextMessage) # Keep existing TextMessage handler
@handler.add(PostbackEvent) # Add PostbackEvent handler
def handle_postback(event):
    if isinstance(event, PostbackEvent):
        app.logger.info(f"Received PostbackEvent: {event.postback.data}")
        data = event.postback.data
        params = dict(item.split('=') for item in data.split('&'))

        action = params.get('action')
        if action == 'customer_feedback':
            task_id = params.get('task_id')
            feedback_type = params.get('feedback')
            customer_line_user_id = event.source.userId # NEW: Get customer's LINE User ID from Postback

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
                'customer_line_user_id': customer_line_user_id # NEW: Save customer LINE ID
            })
            
            feedback_json_str = json.dumps(customer_feedback_data_existing, ensure_ascii=False, indent=2)
            
            # Reconstruct notes with updated/new feedback and existing tech reports
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
        # Existing TextMessage handler logic
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

# NEW: Route to generate customer onboarding QR code
@app.route('/generate_customer_onboarding_qr')
def generate_customer_onboarding_qr():
    task_id = request.args.get('task_id')
    task = get_single_task(task_id)
    if not task:
        flash("ไม่พบข้อมูลงานสำหรับสร้าง QR Code", 'danger')
        return redirect(url_for('summary'))

    # Build LIFF URL for customer onboarding, passing task_id
    # The 'page=onboarding' parameter will tell the LIFF app which part of the HTML to show
    onboarding_liff_url = f"https://liff.line.me/{LIFF_ID_FORM}?page=onboarding&task_id={task_id}"
    
    qr_code_base64 = generate_qr_code_base64(onboarding_liff_url, box_size=10, border=4, fill_color='#000000', back_color='#FFFFFF')
    
    customer_info = parse_customer_info_from_notes(task.get('notes', ''))

    return render_template('generate_onboarding_qr.html', 
                           qr_code_base64=qr_code_base64,
                           task=task,
                           customer_info=customer_info,
                           onboarding_url=onboarding_liff_url)

# NEW: Route for customer onboarding form (LIFF App) - This will be customer_onboarding.html
@app.route('/customer_onboarding')
def customer_onboarding_page():
    # This page will be part of the LIFF app and retrieve task_id from URL params
    # It will use LIFF SDK to get user ID and send to /save_customer_line_id
    task_id = request.args.get('task_id') # Get task_id passed via LIFF URL
    task = get_single_task(task_id) # Fetch task details for display or validation
    if not task:
        # Handle case where task_id is missing or invalid
        return render_template('liff_close_page.html', message="ไม่พบข้อมูลงาน")

    parsed_task = parse_google_task_dates(task)
    parsed_task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
    
    return render_template('customer_onboarding.html', task=parsed_task)


# NEW: Route to save customer LINE User ID
@app.route('/save_customer_line_id', methods=['POST'])
def save_customer_line_id():
    task_id = request.form.get('task_id')
    customer_line_user_id = request.form.get('customer_line_user_id')
    
    task = get_single_task(task_id)
    if not task:
        return jsonify({"status": "error", "message": "Task not found"}), 404

    current_notes = task.get('notes', '')
    
    # Get existing tech reports and base notes without feedback block
    tech_history, base_customer_info_notes = parse_tech_report_from_notes(current_notes)
    
    # Get existing customer feedback data (including any existing LINE ID or initial feedback sent flag)
    customer_feedback_existing = parse_customer_feedback_from_notes(current_notes)
    
    # Update or add customer_line_user_id
    customer_feedback_existing['customer_line_user_id'] = customer_line_user_id
    customer_feedback_existing['id_saved_date'] = datetime.datetime.now(THAILAND_TZ).strftime("%Y-%m-%d %H:%M:%S")
    
    final_notes = base_customer_info_notes.strip()
    if tech_history:
        all_reports_text = ""
        for report in sorted(tech_history, key=lambda x: x.get('summary_date', '')):
            all_reports_text += f"\n\n--- TECH_REPORT_START ---\n{json.dumps(report, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"
        final_notes += all_reports_text
        
    final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(customer_feedback_existing, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
    
    updated_task = update_google_task(task_id=task_id, notes=final_notes, status=task['status'], due=task.get('due'))
    
    if updated_task:
        app.logger.info(f"Successfully saved customer LINE ID {customer_line_user_id} for task {task_id}.")
        cache.clear()
        
        # Send a welcome/thank you message directly to the customer via LINE
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


# NEW: Route for customer problem form (LIFF App)
@app.route('/customer_problem_form')
def customer_problem_form():
    task_id = request.args.get('task_id')
    # Fetch task details if needed for display in the form (optional)
    task = get_single_task(task_id)
    if not task:
        flash("ไม่พบข้อมูลงานสำหรับแจ้งปัญหา", 'danger')
        return redirect(url_for('summary')) # Or a generic error page
    
    parsed_task = parse_google_task_dates(task)
    parsed_task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))

    return render_template('customer_problem_form.html', task=parsed_task)

# NEW: Route to handle submission from customer problem form
@app.route('/submit_customer_problem', methods=['POST'])
def submit_customer_problem():
    task_id = request.form.get('task_id')
    problem_description = request.form.get('problem_description')
    preferred_datetime_str = request.form.get('preferred_datetime')

    task = get_single_task(task_id)
    if not task:
        flash("ไม่พบข้อมูลงานที่เกี่ยวข้อง", 'danger')
        return redirect(url_for('summary'))

    # Parse preferred datetime
    preferred_datetime_thai = None
    if preferred_datetime_str:
        try:
            preferred_datetime_thai = THAILAND_TZ.localize(datetime.datetime.strptime(preferred_datetime_str, "%Y-%m-%dT%H:%M"))
        except ValueError:
            app.logger.error(f"Invalid preferred_datetime format from form: {preferred_datetime_thai}")
    
    # Construct new feedback entry
    customer_problem_data = {
        'problem_date': datetime.datetime.now(THAILAND_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        'problem_description': problem_description,
        'preferred_datetime': preferred_datetime_thai.strftime("%Y-%m-%d %H:%M") if preferred_datetime_thai else 'ไม่มีระบุ',
        'feedback_type': 'problem_reported' # Mark as problem reported
    }

    # Retrieve existing feedback data to get customer_line_user_id
    current_notes = task.get('notes', '')
    customer_feedback_existing = parse_customer_feedback_from_notes(current_notes)
    # Update problem data to existing feedback data
    customer_feedback_existing.update(customer_problem_data) 
    customer_line_user_id = customer_feedback_existing.get('customer_line_user_id') # Get stored customer ID

    # Reconstruct notes with updated/new feedback and existing tech reports
    tech_reports_history, base_customer_info_notes = parse_tech_report_from_notes(current_notes)
    
    final_notes = base_customer_info_notes.strip()
    if tech_reports_history:
        all_reports_text = ""
        for report in sorted(tech_reports_history, key=lambda x: x.get('summary_date', '')):
            final_notes += f"\n\n--- TECH_REPORT_START ---\n{json.dumps(report, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"
        
    final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(customer_feedback_existing, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

    # Update task status to 'needsAction' and notes
    updated_task = update_google_task(
        task_id=task_id,
        notes=final_notes,
        status='needsAction', # Change status back to 'needsAction'
        due=task.get('due') # Keep existing due date
    )
    cache.clear()

    if updated_task:
        flash('บันทึกปัญหาและแจ้งผู้ดูแลเรียบร้อยแล้ว!', 'success')
        
        # Send LINE notification to admin/manager (group message)
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

        # NEW: Send thank you message to customer (direct message)
        if customer_line_user_id:
            thank_you_message_to_customer = (
                f"เรียน ลูกค้า {customer_info.get('name', '-')},\n"
                f"Comphone ได้รับแจ้งปัญหาเกี่ยวกับงาน: {task.get('title', '-')}\n"
                f"เรียบร้อยแล้วครับ/ค่ะ\n"
                f"ทีมงานกำลังตรวจสอบข้อมูลและจะติดต่อกลับเพื่อดูแลท่านโดยเร็วที่สุดครับ/ค่ะ\n\n"
                f"หากมีข้อสงสัยเร่งด่วน โปรดติดต่อเราได้ที่:\n"
                f"โทร: {shop_info.get('contact_phone', '081-XXX-XXXX')}\n"
                f"LINE ID: {shop_info.get('line_id', '@ComphoneService')}\n\n"
                f"ขออภัยในความไม่สะดวกอีกครั้งครับ/ค่ะ\n"
                f"Comphone - ยินดีบริการเสมอครับ/ค่ะ"
            )
            try:
                line_bot_api.push_message(customer_line_user_id, TextSendMessage(text=thank_you_message_to_customer))
                app.logger.info(f"Sent thank you message to customer {customer_line_user_id} for task {task_id}.")
            except Exception as e:
                app.logger.error(f"Failed to send thank you message to customer {customer_line_user_id}: {e}")

    else:
        flash('เกิดข้อผิดพลาดในการบันทึกปัญหา', 'danger')

    return render_template('liff_close_page.html', message="บันทึกข้อมูลเรียบร้อยแล้ว!")

# NEW: Simple LIFF close page
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
            // Try to close LIFF window after a short delay
            window.onload = function() {{
                if (liff.isInClient()) {{
                    setTimeout(() => {{ liff.closeWindow(); }}, 1000);
                }}
            }};
        </script>
    </body>
    </html>
    """

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
```

**2. `update_task_details.html`**

ไฟล์นี้จะมีการเพิ่มปุ่ม "สร้าง QR Code สำหรับลูกค้า" เพื่อให้ช่างสามารถสร้างและแสดง QR Code ให้ลูกค้าสแกนได้ทันทีหลังจากที่งานเสร็จสิ้นลง เพื่อให้ระบบสามารถเก็บ LINE User ID ของลูกค้าได้


```html
{% extends "base.html" %}

{% block title %}จัดการงาน: {{ task.title }}{% endblock %}

{% block content %}
<form action="{{ url_for('task_details', task_id=task.id) }}" method="POST" enctype="multipart/form-data">
    <div class="d-flex justify-content-between align-items-center mb-4">
        <h1 class="h2 mb-0">รายละเอียดและจัดการงาน</h1>
        <div>
            <a href="{{ url_for('summary') }}" class="btn btn-secondary"><i class="fas fa-arrow-left me-2"></i>กลับไปหน้าสรุป</a>
            <!-- ปุ่ม "บันทึกการเปลี่ยนแปลง" ถูกย้ายไปอยู่ด้านล่าง -->
        </div>
    </div>

    <div class="row">
        <!-- Left Column: Main Details & Customer Info -->
        <div class="col-lg-7">
            <div class="card mb-4">
                <div class="card-header h5"><i class="fas fa-edit me-2"></i>แก้ไขข้อมูลหลัก</div>
                <div class="card-body">
                    <!-- Task Title -->
                    <div class="mb-3">
                        <label for="task_title" class="form-label"><strong>รายละเอียดงาน (อาการเสีย, สิ่งที่ต้องทำ)</strong></label>
                        <textarea class="form-control" id="task_title" name="task_title" rows="3" required>{{ task.title }}</textarea>
                    </div>
                    <hr>
                    <!-- Customer Info -->
                    <div class="row">
                        <div class="col-md-6 mb-3">
                            <label for="customer_name" class="form-label">ชื่อลูกค้า</label>
                            <input type="text" class="form-control" id="customer_name" name="customer_name" value="{{ task.customer.name or '' }}">
                        </div>
                        <div class="col-md-6 mb-3">
                            <label for="customer_phone" class="form-label">เบอร์โทรศัพท์</label>
                            <input type="tel" class="form-control" id="customer_phone" name="customer_phone" value="{{ task.customer.phone or '' }}">
                        </div>
                    </div>
                    <div class="mb-3">
                        <label for="address" class="form-label">ที่อยู่ลูกค้า</label>
                        <textarea class="form-control" id="address" name="address" rows="2">{{ task.customer.address or '' }}</textarea>
                    </div>
                    <div class="mb-3">
                        <label for="latitude_longitude" class="form-label">พิกัดแผนที่ (Google Maps URL)</label>
                        <input type="url" class="form-control" id="latitude_longitude" name="latitude_longitude" value="{{ task.customer.map_url or '' }}">
                    </div>
                </div>
            </div>

            <div class="card mb-4">
                <div class="card-header h5"><i class="fas fa-clipboard-check me-2"></i>สถานะและนัดหมาย</div>
                <div class="card-body">
                     <div class="row">
                        <div class="col-md-6 mb-3">
                            <label for="status" class="form-label">สถานะงาน</label>
                            <select class="form-select" id="status" name="status">
                                <option value="needsAction" {% if task.status == 'needsAction' %}selected{% endif %}>งานยังไม่เสร็จ</option>
                                <option value="completed" {% if task.status == 'completed' %}selected{% endif %}>งานเสร็จเรียบร้อย</option>
                            </select>
                        </div>
                        <div class="col-md-6 mb-3">
                             <label for="appointment_due" class="form-label">วันเวลานัดหมาย</label>
                             <input type="datetime-local" class="form-control" id="appointment_due" name="appointment_due" value="{{ task.due_for_input }}">
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Right Column: New Report & History -->
        <div class="col-lg-5">
            <div class="card border-success mb-4">
                <div class="card-header bg-success text-white h5"><i class="fas fa-plus-circle me-2"></i>สรุปการทำงานและอุปกรณ์ที่ใช้</div>
                <div class="card-body">
                    <div class="mb-3">
                        <label for="work_summary" class="form-label">สรุปการทำงาน</label>
                        <textarea class="form-control" id="work_summary" name="work_summary" rows="4" placeholder="กรอกรายละเอียดการดำเนินงาน..."></textarea>
                    </div>
                    <div class="mb-3">
                        <label for="equipment_used" class="form-label">อุปกรณ์ที่ใช้</label>
                        <textarea class="form-control" id="equipment_used" name="equipment_used" rows="4" list="equipment_datalist" placeholder="เช่น สาย LAN, 10 เมตร"></textarea>
                        <datalist id="equipment_datalist">
                            {% for item in common_equipment_items %}<option value="{{ item }}">{% endfor %}
                        </datalist>
                    </div>
                    <div class="mb-3">
                        <label for="files" class="form-label">แนบไฟล์</label>
                        <input type="file" class="form-control" id="files" name="files[]" multiple>
                    </div>
                </div>
            </div>

            {# NEW: Section to generate Customer Onboarding QR Code #}
            <div class="card mb-4 border-info">
                <div class="card-header bg-info text-white h5">
                    <i class="fas fa-qrcode me-2"></i>สำหรับลูกค้า (หลังงานเสร็จ)
                </div>
                <div class="card-body text-center">
                    <p class="mb-3">เมื่อช่างปิดงานซ่อมแล้ว โปรดให้ลูกค้าสแกน QR Code นี้ เพื่อเชื่อมต่อกับระบบ Comphone ครับ/ค่ะ</p>
                    <a href="{{ url_for('generate_customer_onboarding_qr', task_id=task.id) }}" target="_blank" class="btn btn-info btn-lg">
                        <i class="fas fa-qrcode me-2"></i>สร้าง QR Code สำหรับลูกค้า
                    </a>
                    <p class="mt-3 text-muted small">**สำคัญ:** การสแกน QR Code นี้จะทำให้ระบบสามารถส่งข้อความติดตามผลไปยังลูกค้าโดยตรง</p>
                </div>
            </div>
            {# END NEW: Section to generate Customer Onboarding QR Code #}

        </div>
    </div>

    <!-- ปุ่ม "บันทึกการเปลี่ยนแปลง" ที่ถูกย้ายมาอยู่ด้านล่าง -->
    <div class="d-grid gap-2 mb-4">
        <button type="submit" class="btn btn-primary btn-lg"><i class="fas fa-save me-2"></i>บันทึกการเปลี่ยนแปลง</button>
    </div>
</form>

<!-- History Section is outside the form -->
<div class="row">
    <div class="col-12">
        <div class="card">
            <div class="card-header h5"><i class="fas fa-history me-2"></i>ประวัติการทำงาน</div>
            <div class="card-body">
                {% if task.tech_reports_history %}
                    {% for report in task.tech_reports_history %}
                    <div class="border-start border-4 border-secondary ps-3 mb-3">
                        <p class="mb-1"><strong>สรุปเมื่อ:</strong> {{ report.summary_date }}</p>
                        <p class="mb-1" style="white-space: pre-wrap;">{{ report.work_summary or '-' }}</p>
                        {% if report.equipment_used_display %}
                            <p class="mb-0 small text-muted"><strong>อุปกรณ์:</strong> {{ report.equipment_used_display|replace('\n', ', ') }}</p>
                        {% endif %}
                    </div>
                    {% endfor %}
                {% else %}
                    <p class="text-muted text-center">ยังไม่มีประวัติการทำงาน</p>
                {% endif %}
            </div>
        </div>
    </div>
</div>

{# NEW: Collapsible Danger Zone with Delete Button and Confirmation Modal #}
<div class="card border-danger my-4">
    <div class="card-header bg-danger text-white h5 d-flex justify-content-between align-items-center">
        <span><i class="fas fa-exclamation-triangle me-2"></i>โซนอันตราย</span>
        <button class="btn btn-sm btn-outline-light" type="button" data-bs-toggle="collapse" data-bs-target="#dangerZoneCollapse" aria-expanded="false" aria-controls="dangerZoneCollapse">
            <i class="fas fa-chevron-down"></i> <span class="visually-hidden">Toggle Danger Zone</span>
        </button>
    </div>
    <div class="collapse" id="dangerZoneCollapse"> {# This div will be collapsible #}
        <div class="card-body text-center">
            <p class="text-muted">หากต้องการลบงานนี้อย่างถาวร โปรดกดปุ่มด้านล่าง</p>
            <button type="button" class="btn btn-danger" data-bs-toggle="modal" data-bs-target="#confirmDeleteModal">
                <i class="fas fa-trash-alt me-2"></i>ลบงานนี้ทิ้ง
            </button>
        </div>
    </div>
</div>

<!-- Confirm Delete Modal (remains the same) -->
<div class="modal fade" id="confirmDeleteModal" tabindex="-1" aria-labelledby="confirmDeleteModalLabel" aria-hidden="true">
    <div class="modal-dialog">
        <div class="modal-content">
            <div class="modal-header bg-danger text-white">
                <h5 class="modal-title" id="confirmDeleteModalLabel">ยืนยันการลบงาน</h5>
                <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
            </div>
            <div class="modal-body">
                <p>คุณแน่ใจหรือไม่ว่าต้องการลบงานนี้อย่างถาวร?</p>
                <p class="text-danger">การกระทำนี้ไม่สามารถย้อนกลับได้!</p>
            </div>
            <div class="modal-footer">
                <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">ยกเลิก</button>
                <form action="{{ url_for('delete_task', task_id=task.id) }}" method="POST" class="d-inline-block">
                    <button type="submit" class="btn btn-danger">ยืนยันการลบ</button>
                </form>
            </div>
        </div>
    </div>
</div>
{% endblock %}
```

**3. `customer_onboarding.html` (ไฟล์ใหม่)**

ไฟล์นี้จะใช้เมื่อลูกค้าสแกน QR Code ที่ช่างสร้างขึ้น หน้าเว็บนี้จะดึง LINE User ID ของลูกค้าและส่งไปยัง Backend เพื่อบันทึกใน Google Task


```html
{% extends "base.html" %}

{% block title %}เชื่อมต่อกับ Comphone{% endblock %}

{% block head_extra %}
    <script src="https://static.line-scdn.net/liff/2.21.0/sdk.js"></script>
    <style>
        body { font-family: 'Inter', sans-serif; text-align: center; background-color: #f8f9fa; color: #343a40; }
        .container { max-width: 500px; margin-top: 50px; padding: 20px; background-color: #ffffff; border-radius: 15px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }
        h1 { color: #1DB446; font-weight: bold; margin-bottom: 20px; }
        .message-box { padding: 15px; background-color: #e0ffe0; border: 1px solid #1DB446; border-radius: 10px; margin-top: 20px; }
        .loading-spinner { border: 4px solid rgba(0,0,0,.1); border-left-color: #1DB446; border-radius: 50%; width: 30px; height: 30px; animation: spin 1s linear infinite; margin: 20px auto; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        .fas.fa-check-circle { color: #28a745; font-size: 2em; margin-bottom: 10px; }
        .btn-close-liff { background-color: #007bff; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; margin-top: 20px; }
    </style>
{% endblock %}

{% block content %}
<div class="container mx-auto">
    <h1>เชื่อมต่อกับ Comphone</h1>
    <p>สแกน QR Code เพื่อให้เราสามารถส่งข่าวสารและโปรโมชั่นพิเศษให้คุณโดยตรง</p>

    <div id="loading" class="loading-spinner"></div>
    <div id="statusMessage" class="message-box" style="display:none;"></div>
    <button id="closeButton" class="btn-close-liff" style="display:none;">ปิดหน้านี้</button>

    <input type="hidden" id="taskId" value="{{ task.id }}">
</div>

<script>
    document.addEventListener('DOMContentLoaded', function() {
        const loadingDiv = document.getElementById('loading');
        const statusMessageDiv = document.getElementById('statusMessage');
        const closeButton = document.getElementById('closeButton');
        const taskId = document.getElementById('taskId').value;

        loadingDiv.style.display = 'block';
        statusMessageDiv.style.display = 'none';
        closeButton.style.display = 'none';

        liff.init({
            liffId: "{{ LIFF_ID_FORM }}" // Your LIFF ID from Environment Variable
        })
        .then(() => {
            if (!liff.isLoggedIn()) {
                liff.login(); // Redirect to LINE Login if not logged in
            } else {
                liff.getProfile()
                    .then(profile => {
                        const customerLineUserId = profile.userId;
                        statusMessageDiv.innerText = 'กำลังเชื่อมต่อข้อมูลของคุณ...';
                        statusMessageDiv.style.display = 'block';

                        // Send user ID and task_id to your Flask backend
                        fetch('/save_customer_line_id', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/x-www-form-urlencoded',
                            },
                            body: `task_id=${taskId}&customer_line_user_id=${customerLineUserId}`
                        })
                        .then(response => response.json())
                        .then(data => {
                            loadingDiv.style.display = 'none';
                            if (data.status === 'success') {
                                statusMessageDiv.innerHTML = '<i class="fas fa-check-circle"></i><br>Comphone ได้รับข้อมูลการเชื่อมต่อของคุณแล้ว<br>ขอบคุณที่ให้โอกาสเราดูแลครับ/ค่ะ!';
                                statusMessageDiv.style.backgroundColor = '#d4edda'; // Light green
                                statusMessageDiv.style.color = '#155724'; // Dark green text
                            } else {
                                statusMessageDiv.innerHTML = `<i class="fas fa-exclamation-triangle"></i><br>เกิดข้อผิดพลาดในการเชื่อมต่อ: ${data.message || 'ไม่ทราบข้อผิดพลาด'}<br>โปรดลองอีกครั้งหรือติดต่อผู้ดูแล`;
                                statusMessageDiv.style.backgroundColor = '#f8d7da'; // Light red
                                statusMessageDiv.style.color = '#721c24'; // Dark red text
                            }
                            closeButton.style.display = 'block';
                        })
                        .catch(error => {
                            loadingDiv.style.display = 'none';
                            statusMessageDiv.innerHTML = `<i class="fas fa-times-circle"></i><br>เกิดข้อผิดพลาดในการส่งข้อมูล: ${error.message || 'ไม่สามารถติดต่อเซิร์ฟเวอร์ได้'}<br>โปรดลองอีกครั้ง`;
                            statusMessageDiv.style.backgroundColor = '#f8d7da';
                            statusMessageDiv.style.color = '#721c24';
                            statusMessageDiv.style.display = 'block';
                            closeButton.style.display = 'block';
                        });
                    })
                    .catch(err => {
                        loadingDiv.style.display = 'none';
                        statusMessageDiv.innerHTML = `<i class="fas fa-times-circle"></i><br>ไม่สามารถดึงข้อมูล LINE ของคุณได้: ${err.message || 'กรุณาตรวจสอบสิทธิ์ LIFF'}`;
                        statusMessageDiv.style.backgroundColor = '#f8d7da';
                        statusMessageDiv.style.color = '#721c24';
                        statusMessageDiv.style.display = 'block';
                        closeButton.style.display = 'block';
                    });
            }
        })
        .catch(err => {
            loadingDiv.style.display = 'none';
            statusMessageDiv.innerHTML = `<i class="fas fa-times-circle"></i><br>เกิดข้อผิดพลาดในการเริ่มต้น LIFF: ${err.message || 'กรุณาลองใหม่'}<br>ตรวจสอบ LIFF ID หรือการตั้งค่า`;
            statusMessageDiv.style.backgroundColor = '#f8d7da';
            statusMessageDiv.style.color = '#721c24';
            statusMessageDiv.style.display = 'block';
            closeButton.style.display = 'block';
        });

        closeButton.addEventListener('click', function() {
            if (liff.isInClient()) {
                liff.closeWindow();
            } else {
                alert('คุณกำลังดูหน้านี้ในเบราว์เซอร์ปกติ ไม่ใช่ใน LINE. หากอยู่ใน LINE คุณสามารถปิดหน้านี้ได้เลย');
                // For testing outside LINE, you might want to redirect
                // window.location.href = 'about:blank';
            }
        });
    });
</script>
{% endblock %}
```

**4. `customer_problem_form.html` (ไฟล์นี้เป็นโค้ดเดิม ไม่มีการเปลี่ยนแปลง)**

คุณสามารถใช้ไฟล์ `customer_problem_form.html` ที่ผมเคยให้ไปก่อนหน้านี้ได้เลยครับ เนื่องจากโครงสร้างฟอร์มยังคงเดิม และ Logic การส่งข้อมูลถูกจัดการใน `app.py` แล้ว

**5. `generate_onboarding_qr.html` (ไฟล์ใหม่)**

ไฟล์นี้จะใช้เมื่อช่างกดปุ่ม "สร้าง QR Code สำหรับลูกค้า" ในหน้ารายละเอียดงาน เพื่อแสดง QR Code ที่ลูกค้าจะสแกน

```html
{% extends "base.html" %}

{% block title %}QR Code สำหรับลูกค้า{% endblock %}

{% block head_extra %}
    <style>
        body { text-align: center; }
        .qr-container { 
            background-color: white; 
            padding: 30px; 
            border-radius: 15px; 
            box-shadow: 0 4px 15px rgba(0,0,0,0.1); 
            max-width: 400px; 
            margin: 50px auto; 
        }
        .qr-code-img { 
            width: 100%; 
            height: auto; 
            max-width: 300px; 
            margin-bottom: 20px; 
            border: 5px solid #1DB446; /* Green border for professional look */
            border-radius: 10px;
        }
        .instructions {
            font-size: 1.1em;
            color: #555;
            line-height: 1.6;
        }
        .important-note {
            background-color: #fff3cd; /* Light yellow */
            border-left: 5px solid #ffc107; /* Yellow border */
            padding: 15px;
            margin-top: 25px;
            border-radius: 8px;
            text-align: left;
            font-size: 0.95em;
        }
        .important-note strong {
            color: #856404; /* Darker yellow text */
        }
        .btn-print {
            background-color: #007bff;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 5px;
            cursor: pointer;
            margin-top: 20px;
            text-decoration: none;
            display: inline-block;
        }
        .btn-print:hover {
            background-color: #0056b3;
        }
        @media print {
            .no-print {
                display: none !important;
            }
            body {
                background-color: #fff !important;
            }
            .qr-container {
                box-shadow: none !important;
                border: 1px solid #dee2e6 !important;
            }
        }
    </style>
{% endblock %}

{% block content %}
<div class="qr-container">
    <h2 class="mb-3" style="color: #1DB446;">QR Code สำหรับเชื่อมต่อลูกค้า</h2>
    <p class="text-muted">สำหรับงาน: <strong>{{ task.title }}</strong></p>
    <p class="text-muted">ลูกค้า: <strong>{{ customer_info.name or '-' }}</strong> (โทร: {{ customer_info.phone or '-' }})</p>
    
    {% if qr_code_base64 %}
        <img src="{{ qr_code_base64 }}" alt="Customer Onboarding QR Code" class="qr-code-img">
        <div class="instructions">
            <p>โปรดให้ลูกค้าสแกน QR Code นี้ด้วยแอปพลิเคชัน LINE</p>
            <p>เมื่อสแกนแล้ว ลูกค้าจะถูกนำไปยังหน้ายืนยันการเชื่อมต่อ</p>
            <p>การเชื่อมต่อนี้จะทำให้เราสามารถส่งข้อความติดตามผลงานหรือแจ้งโปรโมชั่นให้ลูกค้าได้โดยตรง</p>
        </div>
    {% else %}
        <p class="text-danger">ไม่สามารถสร้าง QR Code ได้ โปรดตรวจสอบการตั้งค่า LIFF ID.</p>
    {% endif %}

    <div class="important-note">
        <strong>ข้อควรจำสำหรับช่าง:</strong>
        <ul>
            <li>อธิบายให้ลูกค้าทราบว่า QR Code นี้มีไว้เพื่ออะไร (เช่น "สแกนเพื่อรับข่าวสารและบริการหลังการซ่อมจาก Comphone ครับ/ค่ะ")</li>
            <li>แจ้งลูกค้าว่าเมื่อสแกนแล้วจะมีข้อความต้อนรับส่งไปยัง LINE ส่วนตัวของลูกค้า</li>
            <li>หากลูกค้าแจ้งปัญหาในอนาคต ระบบจะใช้ LINE นี้ในการติดต่อกลับโดยตรง</li>
        </ul>
    </div>

    <div class="no-print">
        <a href="javascript:window.print()" class="btn-print"><i class="fas fa-print me-2"></i>พิมพ์ QR Code นี้</a>
        <a href="{{ url_for('task_details', task_id=task.id) }}" class="btn btn-secondary mt-3"><i class="fas fa-arrow-left me-2"></i>กลับไปหน้ารายละเอียดงาน</a>
    </div>
</div>
{% endblock %}

{% block body_extra %}
{# No extra script needed here, as LIFF logic is in customer_onboarding.html #}
{% endblock %}
```

---

### **6. `settings_page.html` (ปรับปรุงเพื่อเพิ่มช่องตั้งค่าข้อมูลร้านค้า)**

ไฟล์นี้จะให้คุณสามารถกรอกข้อมูลเบอร์โทรศัพท์และ LINE ID ของร้าน เพื่อนำไปใช้ในข้อความต้อนรับลูกค้า


```html
{% extends "base.html" %}

{% block title %}ตั้งค่าระบบ{% endblock %}

{% block content %}
<h1 class="mb-4">ตั้งค่าระบบ</h1>

{# REMOVED: Section for "Manage System Logo" #}


<form action="{{ url_for('settings_page') }}" method="POST">
    <div class="card mb-4">
        <div class="card-header bg-primary text-white"><i class="fas fa-cogs me-2"></i>การตั้งค่าทั่วไป</div>
        <div class="card-body">
            <div class="row">
                <div class="col-md-4 mb-3"> 
                    <label for="appointment_reminder_hour" class="form-label">เวลาแจ้งเตือนนัดหมาย (0-23)</label>
                    <input type="number" class="form-control" id="appointment_reminder_hour" name="appointment_reminder_hour" value="{{ settings.report_times.appointment_reminder_hour_thai }}" min="0" max="23">
                </div>
                <div class="col-md-4 mb-3"> 
                    <label for="outstanding_report_hour" class="form-label">เวลารายงานงานค้าง (0-23)</label>
                    <input type="number" class="form-control" id="outstanding_report_hour" name="outstanding_report_hour" value="{{ settings.report_times.outstanding_report_hour_thai }}" min="0" max="23">
                </div>
                <div class="col-md-4 mb-3"> 
                    <label for="customer_followup_hour" class="form-label">เวลาแจ้งเตือนติดตามลูกค้า (0-23)</label>
                    <input type="number" class="form-control" id="customer_followup_hour" name="customer_followup_hour" value="{{ settings.report_times.customer_followup_hour_thai }}" min="0" max="23">
                </div>
            </div>
        </div>
    </div>
    <div class="card mb-4">
        <div class="card-header bg-info text-white"><i class="fab fa-line me-2"></i>ตั้งค่าการแจ้งเตือน LINE</div>
        <div class="card-body">
            <div class="mb-3">
                <label for="admin_group_id" class="form-label">LINE Admin Group/User ID</label>
                <input type="text" class="form-control" id="admin_group_id" name="admin_group_id" value="{{ settings.line_recipients.admin_group_id }}">
            </div>
            <div class="mb-3">
                <label for="technician_group_id" class="form-label">LINE Technician Group ID</label>
                <input type="text" class="form-control" id="technician_group_id" name="technician_group_id" value="{{ settings.line_recipients.technician_group_id }}">
            </div>
            <div class="mb-3">
                <label for="manager_user_id" class="form-label">LINE Manager User ID</label>
                <input type="text" class="form-control" id="manager_user_id" name="manager_user_id" value="{{ settings.line_recipients.manager_user_id }}">
            </div>
        </div>
    </div>

    {# NEW: Shop Information Settings #}
    <div class="card mb-4 border-success">
        <div class="card-header bg-success text-white">
            <h5 class="mb-0"><i class="fas fa-store me-2"></i>ข้อมูลร้านค้า (สำหรับข้อความลูกค้า)</h5>
        </div>
        <div class="card-body">
            <div class="mb-3">
                <label for="shop_contact_phone" class="form-label">เบอร์โทรศัพท์ร้านค้า</label>
                <input type="tel" class="form-control" id="shop_contact_phone" name="shop_contact_phone" value="{{ settings.shop_info.contact_phone }}">
            </div>
            <div class="mb-3">
                <label for="shop_line_id" class="form-label">LINE ID ร้านค้า (เช่น @ComphoneService)</label>
                <input type="text" class="form-control" id="shop_line_id" name="shop_line_id" value="{{ settings.shop_info.line_id }}">
            </div>
        </div>
    </div>
    {# END NEW: Shop Information Settings #}

    <div class="card mb-4">
        <div class="card-header bg-secondary text-white"><i class="fas fa-qrcode me-2"></i>ตั้งค่า QR Code</div>
        <div class="card-body">
             <div class="row">
                <div class="col-md-8">
                    <div class="mb-3">
                        <label for="qr_custom_url" class="form-label">URL ที่กำหนดเอง (ถ้ามี)</label>
                        <input type="url" class="form-control" id="qr_custom_url" name="qr_custom_url" value="{{ settings.qrcode_settings.custom_url }}" placeholder="{{ general_summary_url }}">
                    </div>
                     <div class="row">
                        <div class="col-md-6 mb-3">
                            <label for="qr_box_size" class="form-label">ขนาด Box</label>
                            <input type="number" class="form-control" id="qr_box_size" name="qr_box_size" value="{{ settings.qrcode_settings.box_size }}">
                        </div>
                        <div class="col-md-6 mb-3">
                            <label for="qr_border" class="form-label">ขนาดขอบ</label>
                            <input type="number" class="form-control" id="qr_border" name="qr_border" value="{{ settings.qrcode_settings.border }}">
                        </div>
                    </div>
                     <div class="row">
                        <div class="col-md-6 mb-3">
                            <label for="qr_fill_color" class="form-label">สี QR Code</label>
                            <input type="color" class="form-control form-control-color" id="qr_fill_color" name="qr_fill_color" value="{{ settings.qrcode_settings.fill_color }}">
                        </div>
                        <div class="col-md-6 mb-3">
                            <label for="qr_back_color" class="form-label">สีพื้นหลัง</label>
                            <input type="color" class="form-control form-control-color" id="qr_back_color" name="qr_back_color" value="{{ settings.qrcode_settings.back_color }}">
                        </div>
                    </div>
                </div>
                <div class="col-md-4 text-center">
                    <p><strong>ตัวอย่าง QR Code</strong></p>
                    <img src="{{ qr_code_base64_general }}" alt="QR Code" class="img-fluid rounded border">
                </div>
            </div>
        </div>
    </div>

    {# NEW: Auto Backup Settings #}
    <div class="card mb-4 border-primary">
        <div class="card-header bg-primary text-white">
            <h5 class="mb-0"><i class="fas fa-cloud-upload-alt me-2"></i>การสำรองข้อมูลอัตโนมัติ (ไปยัง Google Drive)</h5>
        </div>
        <div class="card-body">
            <div class="form-check form-switch mb-3">
                <input class="form-check-input" type="checkbox" id="auto_backup_enabled" name="auto_backup_enabled" {% if settings.auto_backup.enabled %}checked{% endif %}>
                <label class="form-check-label" for="auto_backup_enabled">เปิดใช้งานการสำรองข้อมูลอัตโนมัติ</label>
            </div>
            <div class="row">
                <div class="col-md-6 mb-3">
                    <label for="auto_backup_hour" class="form-label">เวลาสำรอง (ชั่วโมง, 0-23)</label>
                    <input type="number" class="form-control" id="auto_backup_hour" name="auto_backup_hour" value="{{ settings.auto_backup.hour_thai }}" min="0" max="23">
                </div>
                <div class="col-md-6 mb-3">
                    <label for="auto_backup_minute" class="form-label">เวลาสำรอง (นาที, 0-59)</label>
                    <input type="number" class="form-control" id="auto_backup_minute" name="auto_backup_minute" value="{{ settings.auto_backup.minute_thai }}" min="0" max="59">
                </div>
            </div>
            <p class="text-muted small">ระบบจะทำการสำรองข้อมูลทั้งหมด (Google Tasks, การตั้งค่า, แคตตาล็อกอุปกรณ์, และโค้ด) ไปยัง Google Drive Folder ID ที่กำหนดไว้ใน Environment Variable `GOOGLE_DRIVE_FOLDER_ID`</p>
            <p class="text-danger small">**ข้อควรระวัง:** Render.com เป็นระบบไฟล์ชั่วคราว (Ephemeral Filesystem) ดังนั้นหากไม่ได้ตั้งค่า `GOOGLE_DRIVE_FOLDER_ID` หรือ `GOOGLE_SETTINGS_BACKUP_FOLDER_ID` หรือเกิดข้อผิดพลาดในการอัปโหลด ไฟล์ `settings.json` (รวมถึงแคตตาล็อกอุปกรณ์) และไฟล์ที่อัปโหลดจะหายไปเมื่อเซิร์ฟเวอร์รีสตาร์ท</p>
        </div>
    </div>
    {# END NEW: Auto Backup Settings #}

    <button type="submit" class="btn btn-primary btn-lg d-block w-100 mb-4"><i class="fas fa-save me-2"></i>บันทึกการตั้งค่า</button>
</form>

<div class="card mb-4">
    <div class="card-header"><i class="fas fa-paper-plane me-2"></i>ทดสอบระบบ</div>
    <div class="card-body">
        <p>กดปุ่มเพื่อทดสอบส่งข้อความแจ้งเตือนไปยัง LINE Admin Group ID</p>
        <form action="{{ url_for('test_notification') }}" method="POST">
            <button type="submit" class="btn btn-info"><i class="fab fa-line me-2"></i>ทดสอบส่งแจ้งเตือน</button>
        </form>
        <hr class="my-3">
        <p>กดปุ่มเพื่อทดสอบการส่งแบบสอบถามความพึงพอใจลูกค้า สำหรับงานที่เพิ่งเสร็จ</p>
        <form action="{{ url_for('trigger_customer_follow_up_test') }}" method="POST">
            <button type="submit" class="btn btn-info"><i class="fas fa-user-check me-2"></i>ทดสอบส่งแบบสอบถามติดตามลูกค้า</button>
        </form>
    </div>
</div>

<div class="card mb-4">
    <div class="card-header"><i class="fas fa-boxes me-2"></i>จัดการแคตตาล็อกอุปกรณ์</div>
    <div class="card-body">
        <div class="mb-3">
            <a href="{{ url_for('export_equipment_catalog') }}" class="btn btn-success"><i class="fas fa-file-excel me-2"></i>ส่งออกเป็น Excel</a>
        </div>
        <hr>
        <form action="{{ url_for('import_equipment_catalog') }}" method="post" enctype="multipart/form-data">
            <div class="mb-3">
                <label for="excel_file" class="form-label">นำเข้าไฟล์ Excel (.xlsx)</label>
                <input type="file" class="form-control" id="excel_file" name="excel_file" required accept=".xlsx, .xls">
            </div>
            <button type="submit" class="btn btn-primary"><i class="fas fa-file-import me-2"></i>นำเข้า</button>
        </form>
    </div>
</div>

<div class="card border-warning">
    <div class="card-header bg-warning text-dark"><i class="fas fa-archive me-2"></i>สำรองข้อมูลระบบ</div>
    <div class="card-body">
        <p>ดาวน์โหลดไฟล์สำรองข้อมูลทั้งหมดของระบบ (Google Tasks, การตั้งค่า, โค้ด)</p>
        <a href="{{ url_for('backup_data') }}" class="btn btn-warning"><i class="fas fa-download me-2"></i>ดาวน์โหลด Backup (.zip)</a>
        <hr class="my-3">
        <p>คุณยังสามารถสั่งสำรองข้อมูลอัตโนมัติไปยัง Google Drive ทันที (โดยไม่ต้องรอเวลาที่ตั้งไว้) โดยกดปุ่มนี้</p>
        <form action="{{ url_for('trigger_auto_backup_now') }}" method="POST">
            <button type="submit" class="btn btn-info"><i class="fas fa-cloud-upload-alt me-2"></i>สั่งสำรองข้อมูลไป Google Drive ทันที</button>
        </form>
    </div>
</div>
{% endblock %}

{% block body_extra %}
<script>
// REMOVED: JavaScript for live preview of logo (as the logo upload section is removed)
/*
document.addEventListener('DOMContentLoaded', function() {
    const logoUploadInput = document.getElementById('logoUpload');
    const logoPreviewImage = document.getElementById('logoPreview');

    if (logoUploadInput && logoPreviewImage) {
        logoUploadInput.addEventListener('change', function(event) {
            const file = event.target.files[0];
            if (file) {
                const reader = new FileReader();
                reader.onload = function(e) {
                    logoPreviewImage.src = e.target.result;
                }
                reader.readAsDataURL(file);
            }
        });
    }
});
*/
</script>
{% endblock %}
```

---

### **ขั้นตอนการนำไปใช้งานทั้งหมด**

1.  **คัดลอกโค้ดเต็มของ `app.py`** ที่อยู่ในส่วน **`1. app.py (ปรับปรุงแล้ว)`** ไปวาง **ทับทั้งไฟล์ `app.py` เดิมของคุณ**.
2.  **คัดลอกโค้ดเต็มของ `update_task_details.html`** ที่อยู่ในส่วน **`2. update_task_details.html (เพิ่มปุ่มสร้าง QR Code)`** ไปวาง **ทับทั้งไฟล์ `update_task_details.html` เดิมของคุณ**.
3.  **สร้างไฟล์ใหม่ชื่อ `customer_onboarding.html`** ในโฟลเดอร์ `templates/` ของโปรเจกต์คุณ และคัดลอกโค้ดเต็มของ `customer_onboarding.html` ที่อยู่ในส่วน **`3. customer_onboarding.html (ไฟล์ใหม่)`** ไปวางในไฟล์นี้.
4.  **ตรวจสอบไฟล์ `customer_problem_form.html`**: ใช้ไฟล์ `customer_problem_form.html` ที่ผมให้ไปก่อนหน้านี้ได้เลย (ตรวจสอบว่าอยู่ในโฟลเดอร์ `templates/` และมีเนื้อหาถูกต้องตามที่เคยให้ไป).
5.  **คัดลอกโค้ดเต็มของ `settings_page.html`** ที่อยู่ในส่วน **`4. settings_page.html (ปรับปรุงเพื่อเพิ่มช่องตั้งค่าข้อมูลร้านค้า)`** ไปวาง **ทับทั้งไฟล์ `settings_page.html` เดิมของคุณ**.
6.  **บันทึกไฟล์ทั้งหมด** ในโปรเจกต์ของคุณ.
7.  **ตรวจสอบและตั้งค่า Environment Variables บน Render.com อย่างละเอียด (สำคัญมาก):**
    * **`GOOGLE_TOKEN_JSON`**: **(สำคัญที่สุด)** ต้องเป็น JSON string ที่ถูกต้องสมบูรณ์ และสร้างใหม่ล่าสุด.
    * **`GOOGLE_DRIVE_FOLDER_ID`**: ID โฟลเดอร์ Google Drive สำหรับไฟล์แนบงาน.
    * **`GOOGLE_SETTINGS_BACKUP_FOLDER_ID`**: ID โฟลเดอร์ Google Drive สำหรับสำรอง `settings.json`.
    * **`LINE_ADMIN_GROUP_ID`**: ID กลุ่ม LINE สำหรับผู้ดูแล.
    * **`LINE_TECHNICIAN_GROUP_ID`**: ID กลุ่ม LINE สำหรับช่าง (ถ้ามี).
    * **`LINE_MANAGER_USER_ID`**: LINE User ID ของผู้จัดการ/ช่างที่ต้องการ `@mention` เมื่อมีปัญหา.
    * **`LIFF_ID_FORM`**: ID ของ LIFF App ที่คุณสร้างใน LINE Developers Console (ตรวจสอบว่า `Endpoint URL` บน LINE Developers Console ชี้ไปที่ URL หลักของ Render Web Service ของคุณอย่างถูกต้อง).
8.  **Deploy หรือรีสตาร์ท Web Service บน Render.com**.

### **วิธีการทดสอบฟีเจอร์ใหม่**

เมื่อ Deploy สำเร็จและแอปพลิเคชันทำงานแล้ว:

1.  **ตั้งค่าข้อมูลร้านค้า**:
    * เข้าเว็บแอปของคุณ (URL ของ Render Service).
    * ไปที่เมนู `ตั้งค่าระบบ`.
    * เลื่อนลงไปที่ส่วน `ข้อมูลร้านค้า (สำหรับข้อความลูกค้า)`.
    * กรอก `เบอร์โทรศัพท์ร้านค้า` และ `LINE ID ร้านค้า` ที่คุณต้องการให้แสดงในข้อความขอบคุณลูกค้า.
    * กด `บันทึกการตั้งค่า`.

2.  **ทดสอบการเก็บ LINE User ID ของลูกค้า (Onboarding)**:
    * เข้าเว็บแอปของคุณ.
    * ไปที่ `สรุปสถานะงาน` แล้วคลิกเข้าไปใน `รายละเอียดและจัดการงาน` ของงานใดงานหนึ่ง.
    * เลื่อนลงไปที่ส่วน `สำหรับลูกค้า (หลังงานเสร็จ)`.
    * คลิกปุ่ม `สร้าง QR Code สำหรับลูกค้า`.
    * หน้าเว็บใหม่จะแสดง QR Code และคำแนะนำ. **ใช้ LINE App บนมือถือของลูกค้า (หรือบัญชี LINE ทดสอบ) สแกน QR Code นี้**.
    * เมื่อสแกนแล้ว ควรมีหน้าเว็บ LIFF App เปิดขึ้นมา และแสดงข้อความว่า "Comphone ได้รับข้อมูลการเชื่อมต่อของคุณแล้ว".
    * **ตรวจสอบ**:
        * **Log ของ Render.com**: คุณควรจะเห็น Log ข้อความประมาณ `Successfully saved customer LINE ID Uxxxxxxxxxxxxxxx for task YYYYYYYYYYYYYYY.` และ `Sent welcome message to new customer Uxxxxxxxxxxxxxxx.`
        * **LINE ส่วนตัวของลูกค้า (บัญชีที่สแกน)**: ควรได้รับข้อความต้อนรับจากบอทของคุณ.
        * **Google Tasks**: เข้าไปดู `Notes` ของงานนั้นๆ คุณควรจะเห็นข้อมูล `customer_line_user_id` บันทึกอยู่ในบล็อก `--- CUSTOMER_FEEDBACK_START ---`.

3.  **ทดสอบการแจ้งเตือนติดตามลูกค้า (Flex Message)**:
    * ใน Google Tasks ของคุณ เลือกงานที่เพิ่งทำขั้นตอน Onboarding ลูกค้า (ข้อ 2) ไปแล้ว และมี `customer_line_user_id` บันทึกใน Notes แล้ว.
    * ตั้งสถานะของงานนั้นเป็น `Completed` และตั้งเวลา `Completed date` ให้เป็น **เมื่อวานนี้** (หรือประมาณ 24-48 ชั่วโมงที่แล้ว) เช่น ถ้าวันนี้ 1 ก.ค. 09:00 น. คุณอาจตั้งเป็น 30 มิ.ย. 09:00 น.
    * **รอให้ถึงเวลาที่ตั้งไว้ใน `เวลาแจ้งเตือนติดตามลูกค้า` (ในหน้าตั้งค่าระบบ)** หรือกดปุ่ม `ทดสอบส่งแบบสอบถามติดตามลูกค้า` ในหน้าตั้งค่าระบบ.
    * **ตรวจสอบ**:
        * **LINE ส่วนตัวของลูกค้า (บัญชีที่สแกน QR Code)**: ควรได้รับ Flex Message "🙏 แบบสอบถามความพึงพอใจบริการ 🙏" โดยตรงจากบอทของคุณ
        * **LINE Admin Group**: ควรได้รับข้อความแจ้งว่า "✅ ส่งแบบสอบถามความพึงพอใจลูกค้า... ไปยังลูกค้าโดยตรงเรียบร้อยแล้ว".

4.  **ทดสอบการแจ้งปัญหาและส่งข้อความขอบคุณ:**
    * จาก Flex Message ที่ลูกค้าได้รับ (ในข้อ 3), ให้ลูกค้า (หรือบัญชีทดสอบ) กดปุ่ม `👎 มีปัญหา`.
    * หน้าเว็บ LIFF App สำหรับแจ้งปัญหา (`customer_problem_form.html`) ควรจะเปิดขึ้นมา.
    * กรอก `รายละเอียดปัญหา` และ `วันและเวลาที่สะดวก`.
    * กด `ส่งข้อมูลปัญหา`.
    * **ตรวจสอบ**:
        * **LINE Admin Group**: ควรได้รับข้อความแจ้งเตือน "🚨 ลูกค้าแจ้งปัญหางาน! 🚨" พร้อมรายละเอียดและลิงก์งาน (ถ้าตั้ง `LINE_MANAGER_USER_ID` ไว้ ก็จะมีแสดงในข้อความด้วย).
        * **LINE ส่วนตัวของลูกค้า**: ควรได้รับข้อความขอบคุณจากบอทของคุณ พร้อมเบอร์โทรและ LINE ID ของร้าน.
        * **Google Tasks**: สถานะงานนั้นควรจะกลับไปเป็น `ยังไม่เสร็จ` และ `Notes` ควรมีข้อมูลปัญหาที่ลูกค้ากรอกเพิ่มเข้ามาในบล็อก `--- CUSTOMER_FEEDBACK_START ---`.

หากทำตามขั้นตอนเหล่านี้ครบถ้วนและผลลัพธ์ตรงตามที่คาดหวัง แสดงว่าระบบของคุณทำงานได้อย่างสมบูรณ์แบบแล้วครับ! ยินดีด้วยคร
