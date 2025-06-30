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

# --- Google API Imports ---
from google.oauth2.credentials import Credentials 
from google.auth.transport.requests import Request 
from google_auth_oauthlib.flow import InstalledAppFlow 
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload 
from googleapiclient.http import MediaIoBaseUpload 

import pandas as pd 

# --- APScheduler for background tasks ---
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit # เพื่อให้ scheduler หยุดทำงานเมื่อ app_context()

# --- Global Configurations (these remain global, but will be used in create_app) ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM') 
GOOGLE_TASKS_LIST_ID = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')
GOOGLE_SETTINGS_BACKUP_FOLDER_ID = os.environ.get('GOOGLE_SETTINGS_BACKUP_FOLDER_ID')

SCOPES = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/drive']
THAILAND_TZ = pytz.timezone('Asia/Bangkok')
cache = TTLCache(maxsize=100, ttl=60)

# --- Scheduler instance (global) ---
scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)

# --- Global variable for app settings ---
_APP_SETTINGS_STORE = {} 

# --- Helper and Utility Functions (remain outside create_app, but use app.app_context where needed) ---
def load_settings_from_file():
    """Load application settings from JSON file."""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
        except (json.JSONDecodeError, IOError) as e: 
            # Use app.logger in create_app context or ensure Flask app is available.
            # For this global helper, we can use print or a default logging if app is not ready.
            print(f"ERROR: Error handling settings.json: {e}", file=sys.stderr)
            if os.path.exists(SETTINGS_FILE) and os.path.getsize(SETTINGS_FILE) == 0:
                os.remove(SETTINGS_FILE)
                print(f"WARNING: Empty settings.json deleted. Using default settings.", file=sys.stderr)
    return None

def save_settings_to_file(settings_data):
    """Save application settings to JSON file."""
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f: json.dump(settings_data, f, ensure_ascii=False, indent=4)
        return True
    except IOError as e:
        print(f"ERROR: Error writing to settings.json: {e}", file=sys.stderr)
        return False

# get_google_service must be defined before get_google_drive_service
def get_google_service(api_name, api_version):
    """Authenticates and returns a Google API service."""
    creds = None
    token_path = 'token.json'
    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')

    if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
        print("ERROR: LINE Bot credentials are not set in environment variables.", file=sys.stderr)
        return None # Return early if basic LINE configs are missing, as Google API might also fail without full setup

    # Try to load credentials from environment variable first (PREFERRED for Render)
    if google_token_json_str:
        try: 
            creds = Credentials.from_authorized_user_info(json.loads(google_token_json_str), SCOPES)
            print("INFO: Loaded Google credentials from GOOGLE_TOKEN_JSON environment variable.", file=sys.stderr)
        except Exception as e: 
            print(f"WARNING: Could not load token from env var, falling back to token.json: {e}", file=sys.stderr)
    
    # Fallback to local token.json file (Ephemeral on Render, only useful for initial local setup)
    if not creds and os.path.exists(token_path):
        creds = Credentials.from_authorized_file(token_path, SCOPES)
        print(f"INFO: Loaded Google credentials from local {token_path}.", file=sys.stderr)

    # Refresh token if expired
    if creds and creds.valid and creds.expired and creds.refresh_token:
        try: 
            creds.refresh(Request())
            print("INFO: Refreshed Google access token.", file=sys.stderr)
            # If refreshed, save back to local file and recommend updating env var
            if not google_token_json_str: # Only save to file if not using env var
                with open(token_path, 'w') as token: token.write(creds.to_json())
                print(f"INFO: Refreshed token saved to {token_path}. Please update GOOGLE_TOKEN_JSON on Render with this content.", file=sys.stderr)
        except Exception as e:
            print(f"ERROR: Error refreshing token: {e}", file=sys.stderr)
            creds = None # Invalidate creds if refresh fails
    
    # --- IMPORTANT: REMOVED run_console() FOR DEPLOYMENT ON RENDER ---
    if not creds or not creds.valid:
        print("ERROR: No valid Google credentials available. API service cannot be built.", file=sys.stderr)
        print("ERROR: Please ensure GOOGLE_TOKEN_JSON environment variable is set.", file=sys.stderr)
        print("ERROR: If running locally, ensure credentials.json exists for initial setup.", file=sys.stderr)
            
    if not creds or not creds.valid:
        print("ERROR: Final check: No valid Google credentials after all attempts.", file=sys.stderr)
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
        print("WARNING: GOOGLE_SETTINGS_BACKUP_FOLDER_ID not set. Skipping settings restore from Drive.", file=sys.stderr)
        return False

    service = get_google_drive_service() # This function is now defined
    if not service:
        print("ERROR: Could not get Drive service for settings restore on startup.", file=sys.stderr)
        return False

    try:
        # Search for the latest settings_backup.json in the dedicated folder
        query = f"name = 'settings_backup.json' and '{GOOGLE_SETTINGS_BACKUP_FOLDER_ID}' in parents"
        response = service.files().list(q=query, spaces='drive', fields='files(id, name, createdTime)', orderBy='createdTime desc', pageSize=1).execute()
        files = response.get('files', [])

        if files:
            latest_backup_file_id = files[0]['id']
            print(f"INFO: Found latest settings backup on Drive: {files[0]['name']} (ID: {latest_backup_file_id})", file=sys.stderr)

            request = service.files().get_media(fileId=latest_backup_file_id)
            fh = BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
                # app.logger.debug(f"Download settings progress: {int(status.progress() * 100)}%.") # Removed app.logger.debug
            fh.seek(0)
            
            downloaded_settings = json.loads(fh.read().decode('utf-8'))
            
            # Save the downloaded settings locally
            if save_settings_to_file(downloaded_settings):
                print("INFO: Successfully restored settings from Google Drive backup.", file=sys.stderr)
                # We update the global _APP_SETTINGS_STORE later when get_app_settings is called
                return True
            else:
                print("ERROR: Failed to save restored settings to local file.", file=sys.stderr)
                return False
        else:
            print("INFO: No settings backup found on Google Drive for automatic restore.", file=sys.stderr)
            return False
    except HttpError as e:
        print(f"ERROR: Google Drive API error during settings restore: {e}", file=sys.stderr)
        return False
    except json.JSONDecodeError as e:
        print(f"ERROR: Error decoding settings JSON from Drive: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"ERROR: An unexpected error occurred during settings restore from Drive: {e}", file=sys.stderr)
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
    # app.logger.info(f"Fetching tasks (show_completed={show_completed})") # Removed app.logger.info
    service = get_google_tasks_service()
    if not service: return None
    try:
        results = service.tasks().list(tasklist=GOOGLE_TASKS_LIST_ID, showCompleted=show_completed, maxResults=100).execute()
        return results.get('items', [])
    except HttpError as err:
        print(f"ERROR: API Error getting tasks: {err}", file=sys.stderr)
        return None

def get_single_task(task_id):
    """Fetches a single task from Google Tasks API."""
    service = get_google_tasks_service()
    if not service: return None
    try:
        return service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
    except HttpError as err:
        print(f"ERROR: Error getting single task {task_id}: {err}", file=sys.stderr)
        return None
        
def upload_file_to_google_drive(file_path, file_name, mime_type, folder_id=GOOGLE_DRIVE_FOLDER_ID):
    """Uploads a file to a specified Google Drive folder."""
    service = get_google_drive_service()
    if not service or not folder_id:
        print("ERROR: Drive service or folder ID is not configured for upload.", file=sys.stderr)
        return None
    try:
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        file_metadata = {'name': file_name, 'parents': [folder_id]}
        file_obj = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        
        # Make the uploaded file publicly readable
        service.permissions().create(fileId=file_obj['id'], body={'role': 'reader', 'type': 'anyone'}).execute()
        
        print(f"INFO: Uploaded to Drive: {file_obj.get('webViewLink')}", file=sys.stderr)
        return file_obj.get('webViewLink')
    except HttpError as e:
        print(f'ERROR: Drive upload error: {e}', file=sys.stderr)
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
        print(f"ERROR: Error creating Google Task: {e}", file=sys.stderr)
        return None
        
def delete_google_task(task_id):
    """Deletes a task from Google Tasks."""
    service = get_google_tasks_service()
    if not service: return False
    try:
        service.tasks().delete(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
        return True
    except HttpError as err:
        print(f"ERROR: API Error deleting task {task_id}: {err}", file=sys.stderr)
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
        print(f"ERROR: Failed to update task {task_id}: {e}", file=sys.stderr)
        return None

def parse_customer_info_from_notes(notes):
    """Parses customer information and map URL from task notes."""
    info = {'name': '', 'phone': '', 'address': '', 'map_url': None}
    if not notes: return info
    
    info['name'] = (re.search(r"ลูกค้า:\s*(.*)", notes, re.IGNORECASE) or re.search(r"customer:\s*(.*)", notes, re.IGNORECASE)).group(1).strip() if (re.search(r"ลูกค้า:", notes) or re.search(r"customer:", notes)) else ''
    info['phone'] = (re.search(r"เบอร์โทรศัพท์:\s*(.*)", notes, re.IGNORECASE) or re.search(r"phone:\s*(.*)", notes, re.IGNORECASE)).group(1).strip() if (re.search(r"เบอร์โทรศัพท์:", notes) or re.search(r"phone:", notes)) else ''
    info['address'] = (re.search(r"ที่อยู่:\s*(.*)", notes, re.IGNORECASE) or re.search(r"address:\s*(.*)", notes, re.IGNORECASE)).group(1).strip() if (re.search(r"ที่อยู่:", notes) or re.search(r"address:", notes)) else ''
    
    # app.logger.debug(f"Parsing notes for map_url: {notes}") # Removed app.logger.debug
    # Regex to capture various Google Maps URL formats (e.g., /maps?q=, /maps/search/, /maps/place/, @lat,long)
    map_url_match = re.search(r"(https?://(?:www\.)?google\.com/maps/(?:place|search)/\?api=1&query=[-\d\.]+,[-\d\.]+|https?://(?:www\.)?google\.com/maps\?q=[-\d\.]+,[-\d\.]+|https?://(?:www\.)?google\.com/maps/@[\d\.]+,[\d\.]+,[\d\.]z.*)", notes)
    if map_url_match:
        info['map_url'] = map_url_match.group(0).strip() # Use group(0) to get the whole matched string
        # app.logger.debug(f"Parsed map_url: {info['map_url']}") # Removed app.logger.debug
        
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
            print("WARNING: Failed to decode customer feedback JSON from notes.", file=sys.stderr)
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

# app.context_processor should be inside create_app
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
        print(f"ERROR: Error generating QR code: {e}", file=sys.stderr)
        return "" # Return empty string on error

# NEW: Internal function to create the backup zip file
def _create_backup_zip():
    """Creates a zip archive of all tasks, settings, and source code."""
    try:
        all_tasks = get_google_tasks_for_report(show_completed=True)
        all_settings = get_app_settings()
        
        if all_tasks is None or all_settings is None:
            print('ERROR: Failed to get all tasks or settings for backup.', file=sys.stderr)
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
        print(f"INFO: Created backup zip: {backup_filename}", file=sys.stderr)
        return memory_file, backup_filename
    except Exception as e:
        print(f"ERROR: Error creating full system backup zip: {e}", file=sys.stderr)
        return None, None

# NEW: Internal function to upload backup to Google Drive
def _upload_backup_to_drive(memory_file, filename, drive_folder_id):
    """Uploads the given memory file (zip or json) to Google Drive."""
    if not memory_file or not filename:
        print("ERROR: No memory file or filename provided for Drive upload.", file=sys.stderr)
        return False
    
    service = get_google_drive_service()
    if not service or not drive_folder_id:
        print("ERROR: Drive service or folder ID is not configured for upload.", file=sys.stderr)
        return False
    
    try:
        mime_type = 'application/zip' if filename.endswith('.zip') else 'application/json'
        media = MediaIoBaseUpload(memory_file, mimetype=mime_type, resumable=True) 
        file_metadata = {'name': filename, 'parents': [drive_folder_id]}
        
        file_obj = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        
        # Make the uploaded file publicly readable
        service.permissions().create(fileId=file_obj['id'], body={'role': 'reader', 'type': 'anyone'}).execute()
        
        print(f"INFO: Successfully uploaded backup to Drive: {file_obj.get('webViewLink')}", file=sys.stderr)
        return True
    except HttpError as e:
        print(f'ERROR: Google Drive backup upload error for {filename}: {e}', file=sys.stderr)
        return False
    except Exception as e:
        print(f"ERROR: An unexpected error occurred during backup upload for {filename}: {e}", file=sys.stderr)
        return False

# NEW: Scheduled backup job
def scheduled_backup_job():
    """Scheduled job to perform automatic backup to Google Drive."""
    # Ensure this runs within an app context if needed for Flask features (like url_for, flash)
    with app.app_context(): 
        print("INFO: Running scheduled backup job...", file=sys.stderr)
        
        # 1. Perform full system backup (zip)
        memory_file_zip, filename_zip = _create_backup_zip()
        if memory_file_zip and filename_zip:
            if _upload_backup_to_drive(memory_file_zip, filename_zip, GOOGLE_DRIVE_FOLDER_ID):
                print("INFO: Automatic full system backup completed successfully to Google Drive.", file=sys.stderr)
            else:
                print("ERROR: Automatic full system backup to Google Drive failed.", file=sys.stderr)
        else:
            print("ERROR: Failed to create full system backup zip file for automatic backup.", file=sys.stderr)

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
                        print(f"INFO: Deleted existing settings_backup.json (ID: {f['id']}) from Drive.", file=sys.stderr)
                except HttpError as e:
                    print(f"WARNING: Could not delete existing settings_backup.json: {e}", file=sys.stderr)

            if _upload_backup_to_drive(settings_json_bytes, settings_backup_filename, GOOGLE_SETTINGS_BACKUP_FOLDER_ID):
                print("INFO: Automatic settings backup completed successfully to Google Drive (JSON).", file=sys.stderr)
            else:
                print("ERROR: Automatic settings backup to Google Drive (JSON) failed.", file=sys.stderr)
        else:
            print("WARNING: GOOGLE_SETTINGS_BACKUP_FOLDER_ID not set. Skipping automatic settings JSON backup.", file=sys.stderr)


# NEW: Scheduled job for appointment reminders
def scheduled_appointment_reminder_job():
    """
    Scheduled job to send LINE notifications for appointments due today.
    """
    with app.app_context():
        print("INFO: Running scheduled appointment reminder job...", file=sys.stderr)
        settings = get_app_settings()
        admin_group_id = settings.get('line_recipients', {}).get('admin_group_id', '')
        technician_group_id = settings.get('line_recipients', {}).get('technician_group_id', '')
        
        if not admin_group_id and not technician_group_id:
            print("WARNING: No LINE recipient IDs configured for appointment reminders. Skipping.", file=sys.stderr)
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
                    print(f"WARNING: Could not parse due date for task {task.get('id')}: {task.get('due')}", file=sys.stderr)
                    continue
        
        if not upcoming_appointments:
            print("INFO: No upcoming appointments found for today.", file=sys.stderr)
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
                    print(f"INFO: Sent {len(messages_to_send)} appointment reminders to admin group.", file=sys.stderr)
                if technician_group_id and technician_group_id != admin_group_id: # Avoid sending duplicate if same group
                    line_bot_api.push_message(technician_group_id, messages_to_send)
                    print(f"INFO: Sent {len(messages_to_send)} appointment reminders to technician group.", file=sys.stderr)
            except Exception as e:
                print(f"ERROR: Failed to send appointment reminder LINE messages: {e}", file=sys.stderr)

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
        print("INFO: Running scheduled customer follow-up job...", file=sys.stderr)
        settings = get_app_settings()
        admin_group_id = settings.get('line_recipients', {}).get('admin_group_id', '')
        technician_group_id = settings.get('line_recipients', {}).get('technician_group_id', '')
        
        # New: If customer_line_user_id is available, send directly to customer.
        # Otherwise, send to admin/technician group for manual forwarding.
        send_to_customer_directly = False # Default to false unless we have customer ID
        
        if not admin_group_id and not technician_group_id:
            print("WARNING: No LINE recipient IDs configured for customer follow-up. Skipping.", file=sys.stderr)
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
                    print(f"WARNING: Could not parse completed date for task {task.get('id')}: {task.get('completed')}", file=sys.stderr)
                    continue
        
        if not follow_up_tasks:
            print("INFO: No tasks eligible for customer follow-up today.", file=sys.stderr)
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
            print(f"INFO: Marked task {task.get('id')} as follow-up sent and updated notes.", file=sys.stderr)
            cache.clear() 

            # --- Sending Logic: Direct to Customer vs. Admin Group ---
            if customer_line_user_id_for_task:
                print(f"INFO: Sending direct follow-up to customer {customer_line_user_id_for_task} for task {task.get('id')}.", file=sys.stderr)
                try:
                    line_bot_api.push_message(customer_line_user_id_for_task, FlexSendMessage(alt_text="แบบสอบถามความพึงพอใจบริการ", contents=customer_follow_up_flex))
                    # Also send a small notification to admin that direct message was sent
                    admin_notify_text = f"✅ ส่งแบบสอบถามความพึงพอใจลูกค้า [{customer_info.get('name', '-')}] ไปยังลูกค้าโดยตรงเรียบร้อยแล้ว"
                    if admin_group_id:
                        line_bot_api.push_message(admin_group_id, TextSendMessage(text=admin_notify_text))
                except Exception as e:
                    print(f"ERROR: Failed to send direct customer follow-up to {customer_line_user_id_for_task}: {e}", file=sys.stderr)
                    # Fallback to group if direct send fails
                    admin_fallback_text = (
                        f"⚠️ ส่งแบบสอบถามความพึงพอใจลูกค้า [{customer_info.get('name', '-')}] โดยตรงไม่สำเร็จ\n"
                        f"โปรดส่งแบบสอบถามนี้ให้ลูกค้าแทนครับ/ค่ะ\n"
                        f"รายละเอียดงาน: {task.get('title', '-').splitlines()[0] if task.get('title') else '-'}\n"
                        f"ลิงก์งาน: {url_for('task_details', task_id=task.get('id'), _external=True)}"
                    )
                    if admin_group_id:
                        line_bot_api.push_message(admin_group_id, [TextSendMessage(text=admin_fallback_text), FlexSendMessage(alt_text="แบบสอบถามความพึงพอใจบริการ", contents=customer_follow_up_flex)])
                    print(f"INFO: Sent fallback follow-up to admin group for task {task.get('id')}.", file=sys.stderr)
            else:
                print(f"INFO: No LINE User ID for customer {customer_info.get('name', '-')}. Sending follow-up to admin/tech group for manual forwarding.", file=sys.stderr)
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
                        print(f"INFO: Sent group follow-up messages for task {task.get('id')} to admin group.", file=sys.stderr)
                    if technician_group_id and technician_group_id != admin_group_id:
                        line_bot_api.push_message(technician_group_id, messages_to_send_group)
                        print(f"INFO: Sent group follow-up messages for task {task.get('id')} to technician group.", file=sys.stderr)
                except Exception as e:
                    print(f"ERROR: Failed to send group follow-up LINE messages for task {task.get('id')}: {e}", file=sys.stderr)
            
# NEW: handle postback event for customer feedback
@handler.add(MessageEvent, message=TextMessage) # Keep existing TextMessage handler
@handler.add(PostbackEvent) # Add PostbackEvent handler
def handle_postback(event):
    if isinstance(event, PostbackEvent):
        print(f"INFO: Received PostbackEvent: {event.postback.data}", file=sys.stderr)
        data = event.postback.data
        params = dict(item.split('=') for item in data.split('&'))

        action = params.get('action')
        if action == 'customer_feedback':
            task_id = params.get('task_id')
            feedback_type = params.get('feedback')
            customer_line_user_id = event.source.userId # NEW: Get customer's LINE User ID from Postback

            task = get_single_task(task_id)
            if not task:
                print(f"ERROR: Postback for unknown task_id: {task_id}", file=sys.stderr)
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
            print(f"INFO: Task {task_id} updated with feedback: {feedback_type}. Status set to {new_task_status}. Customer ID: {customer_line_user_id}", file=sys.stderr)
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
                        print(f"INFO: Sent problem notification for task {task_id} to admin group.", file=sys.stderr)
                    except Exception as e:
                        print(f"ERROR: Failed to send problem notification to admin group: {e}", file=sys.stderr)
            
        
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
            final_notes += f"\n\n--- TECH_REPORT_START ---\n{json.dumps(report, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"
        
    final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(customer_feedback_existing, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
    
    updated_task = update_google_task(task_id=task_id, notes=final_notes, status=task['status'], due=task.get('due'))
    
    if updated_task:
        print(f"INFO: Successfully saved customer LINE ID {customer_line_user_id} for task {task_id}.", file=sys.stderr)
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
            print(f"INFO: Sent welcome message to new customer {customer_line_user_id}.", file=sys.stderr)
        except Exception as e:
            print(f"ERROR: Failed to send welcome message to customer {customer_line_user_id}: {e}", file=sys.stderr)

        return jsonify({"status": "success", "message": "LINE ID saved"}), 200
    else:
        print(f"ERROR: Failed to save customer LINE ID {customer_line_user_id} for task {task_id}.", file=sys.stderr)
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
            print(f"ERROR: Invalid preferred_datetime format from form: {preferred_datetime_thai}", file=sys.stderr)
    
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
                print(f"INFO: Sent problem notification for task {task_id} to admin group.", file=sys.stderr)
            except Exception as e:
                print(f"ERROR: Failed to send problem notification to admin group: {e}", file=sys.stderr)

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
                print(f"INFO: Sent thank you message to customer {customer_line_user_id} for task {task_id}.", file=sys.stderr)
            except Exception as e:
                print(f"ERROR: Failed to send thank you message to customer {customer_line_user_id}: {e}", file=sys.stderr)

    else:
        flash('เกิดข้อผิดพลาดในการบันทึกปัญหา', 'danger')

    return render_template('liff_close_page.html', message="บันทึกข้อมูลเรียบร้อยแล้ว!")

# --- Scheduler instance (global) ---
scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)

def run_scheduler():
    """Initializes and runs the APScheduler jobs."""
    global scheduler # Ensure scheduler variable is accessible and properly re-initialized if needed

    settings = get_app_settings()
    
    # Auto Backup Settings
    auto_backup_enabled = settings.get('auto_backup', {}).get('enabled', False)
    auto_backup_hour = settings.get('auto_backup', {}).get('hour_thai', 2)
    auto_backup_minute = settings.get('auto_backup', {}).get('minute_thai', 0)

    # Appointment Reminder Settings
    appointment_reminder_hour = settings.get('report_times', {}).get('appointment_reminder_hour_thai', 7)
    customer_followup_hour = settings.get('report_times', {}).get('customer_followup_hour_thai', 9) # NEW: Get customer followup hour
    
    # Shutdown existing scheduler to prevent duplicates on reloads (e.g., during debug)
    if scheduler.running:
        print("INFO: Scheduler is already running. Shutting down existing jobs for reinitialization.", file=sys.stderr)
        scheduler.shutdown(wait=False)
        # Reinitialize the global scheduler instance to clear old jobs
        scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)
        
    # Re-add auto backup job based on current settings
    auto_backup_job_id = 'auto_system_backup'
    if auto_backup_enabled:
        if not scheduler.get_job(auto_backup_job_id): # Add if it doesn't exist
            print(f"INFO: Scheduling automatic backup daily at {auto_backup_hour:02d}:{auto_backup_minute:02d} Thai Time.", file=sys.stderr)
            scheduler.add_job(
                scheduled_backup_job,
                CronTrigger(hour=auto_backup_hour, minute=auto_backup_minute, timezone=THAILAND_TZ),
                id=auto_backup_job_id
            )
        else: # Reschedule if it exists
            print(f"INFO: Automatic backup job '{auto_backup_job_id}' already exists. Reconfiguring trigger.", file=sys.stderr)
            scheduler.reschedule_job(
                auto_backup_job_id,
                trigger=CronTrigger(hour=auto_backup_hour, minute=auto_backup_minute, timezone=THAILAND_TZ)
            )
    else:
        # If auto backup is disabled, remove the job if it exists
        if scheduler.get_job(auto_backup_job_id):
            scheduler.remove_job(auto_backup_job_id)
            print("INFO: Automatic backup job removed as it is disabled.", file=sys.stderr)

    # Add/Reconfigure appointment reminder job
    appointment_reminder_job_id = 'daily_appointment_reminder'
    if not scheduler.get_job(appointment_reminder_job_id):
        print(f"INFO: Scheduling daily appointment reminders at {appointment_reminder_hour:02d}:00 Thai Time.", file=sys.stderr)
        scheduler.add_job(
            scheduled_appointment_reminder_job,
            CronTrigger(hour=appointment_reminder_hour, minute=0, timezone=THAILAND_TZ), # Run at the top of the hour
            id=appointment_reminder_job_id
        )
    else:
        print(f"INFO: Appointment reminder job '{appointment_reminder_job_id}' already exists. Reconfiguring trigger.", file=sys.stderr)
        scheduler.reschedule_job(
            appointment_reminder_job_id,
            trigger=CronTrigger(hour=appointment_reminder_hour, minute=0, timezone=THAILAND_TZ)
        )

    # NEW: Add/Reconfigure customer follow-up job
    customer_follow_up_job_id = 'customer_follow_up_survey'
    if not scheduler.get_job(customer_follow_up_job_id):
        print(f"INFO: Scheduling customer follow-up surveys daily at {customer_followup_hour:02d}:00 Thai Time.", file=sys.stderr)
        scheduler.add_job(
            scheduled_customer_follow_up_job,
            CronTrigger(hour=customer_followup_hour, minute=0, timezone=THAILAND_TZ), # Run at the top of the hour
            id=customer_follow_up_job_id
        )
    else:
        print(f"INFO: Customer follow-up job '{customer_follow_up_job_id}' already exists. Reconfiguring trigger.", file=sys.stderr)
        scheduler.reschedule_job(
            customer_follow_up_job_id,
            trigger=CronTrigger(hour=customer_followup_hour, minute=0, timezone=THAILAND_TZ)
        )

    if not scheduler.running:
        scheduler.start()
        print("INFO: APScheduler started.", file=sys.stderr)
        # Ensure the scheduler shuts down cleanly when the app exits
        atexit.register(lambda: scheduler.shutdown(wait=False))


# --- Main Application Initialization Function ---
def create_app():
    # Attempt to load settings from Google Drive on app startup
    # This must happen before _APP_SETTINGS_STORE is fully initialized with defaults
    # so that the restored settings can take precedence.
    load_settings_from_drive_on_startup() 

    # After restore attempt, ensure _APP_SETTINGS_STORE global variable is correctly populated.
    # This handles cases where restore failed (using defaults) or was successful.
    global _APP_SETTINGS_STORE
    _APP_SETTINGS_STORE = get_app_settings() 
    
    # Now that settings are loaded, initialize and run the scheduler based on these settings.
    run_scheduler()

    # Flask context processor (must be defined after app is created)
    @app.context_processor
    def inject_now():
        """Injects current datetime into Jinja2 templates."""
        return {'now': datetime.datetime.now(THAILAND_TZ)}

    return app


# --- Flask Routes (remain outside create_app, as Flask will collect them when create_app is called) ---
@app.route("/")
def root_redirect():
    """Redirects root URL to summary page."""
    return redirect(url_for('summary'))
    
@app.route("/form", methods=['GET', 'POST'])
def form_page():
    """Handles new task creation form."""
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
            except ValueError: print(f"ERROR: Invalid appointment format: {appointment_str}", file=sys.stderr)

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
    """Displays a summary of tasks."""
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


@app.route('/task/<task_id>', methods=['GET', 'POST'])
def task_details(task_id):
    """Displays and handles updates for a single task."""
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

        upload_errors = [] 

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
                        try:
                            file.save(temp_filepath)
                            mime_type = file.mimetype or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                            drive_url = upload_file_to_google_drive(temp_filepath, filename, mime_type)
                            if drive_url: 
                                new_attachment_urls.append(drive_url)
                                flash(f"อัปโหลดไฟล์ '{filename}' สำเร็จ!", 'success') 
                            else:
                                upload_errors.append(f"ไม่สามารถอัปโหลดไฟล์ '{filename}' ไปยัง Google Drive ได้") 
                                flash(f"อัปโหลดไฟล์ '{filename}' ไม่สำเร็จ!", 'warning') 
                        except Exception as e:
                            upload_errors.append(f"เกิดข้อผิดพลาดในการบันทึกหรืออัปโหลดไฟล์ '{filename}': {e}") 
                            flash(f"เกิดข้อผิดพลาดในการบันทึกหรืออัปโหลดไฟล์ '{filename}'!", 'warning') 
                        finally:
                            if os.path.exists(temp_filepath):
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

        # Preserve existing customer feedback block if it exists
        customer_feedback_existing = parse_customer_feedback_from_notes(task_raw.get('notes', ''))
        if customer_feedback_existing:
            final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(customer_feedback_existing, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        
        due_date_gmt = None
        if appointment_str:
            try:
                dt_local = THAILAND_TZ.localize(datetime.datetime.strptime(appointment_str, "%Y-%m-%dT%H:%M"))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat()
            except ValueError: 
                print(f"ERROR: Invalid reschedule format: {appointment_str}", file=sys.stderr)
        
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
        
        if upload_errors:
            for err in upload_errors:
                print(err, file=sys.stderr)
        
        return redirect(url_for('task_details', task_id=task_id))

    task = parse_google_task_dates(task_raw)
    task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
    task['tech_reports_history'], _ = parse_tech_report_from_notes(task.get('notes', ''))
    
    return render_template('update_task_details.html', task=task, common_equipment_items=get_app_settings().get('common_equipment_items', []))


@app.route('/delete_task/<task_id>', methods=['POST'])
def delete_task(task_id):
    """Deletes a task."""
    if delete_google_task(task_id):
        flash('ลบงานเรียบร้อยแล้ว!', 'success')
        cache.clear()
    else:
        flash('เกิดข้อผิดพลาดในการลบงาน', 'danger')
    return redirect(url_for('summary'))


@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    """Handles application settings."""
    if request.method == 'POST':
        if 'logo_file' in request.files:
            logo_file = request.files['logo_file']
            if logo_file and logo_file.filename != '' and allowed_file(logo_file.filename):
                filename = 'logo.png' 
                filepath = os.path.join(app.root_path, 'static', filename)
                try:
                    logo_file.save(filepath)
                    flash('อัปเดตโลโก้เรียบร้อยแล้ว!', 'success')
                except Exception as e:
                    print(f"ERROR: Could not save logo: {e}", file=sys.stderr)
                    flash('เกิดข้อผิดพลาดในการบันทึกโลโก้', 'danger')
                return redirect(url_for('settings_page'))
        
        auto_backup_enabled = request.form.get('auto_backup_enabled') == 'on'
        auto_backup_hour = int(request.form.get('auto_backup_hour'))
        auto_backup_minute = int(request.form.get('auto_backup_minute'))

        # Get shop info from form
        shop_contact_phone = request.form.get('shop_contact_phone', '').strip()
        shop_line_id = request.form.get('shop_line_id', '').strip()

        save_app_settings({
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
            },
            'shop_info': { # Save shop info
                'contact_phone': shop_contact_phone,
                'line_id': shop_line_id
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
    """Tests LINE notification to admin group."""
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
        print(f"ERROR: Failed to send test notification: {e}", file=sys.stderr)
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
    print("INFO: Manual trigger for automatic backup initiated.", file=sys.stderr)
    scheduled_backup_job() # Directly call the job function
    flash('ระบบกำลังดำเนินการสำรองข้อมูลอัตโนมัติ (โปรดตรวจสอบ Google Drive ของคุณในภายหลัง)', 'info')
    return redirect(url_for('settings_page'))

@app.route('/export_equipment_catalog', methods=['GET'])
def export_equipment_catalog():
    """Exports equipment catalog to Excel."""
    try:
        equipment_catalog = get_app_settings().get('equipment_catalog', [])
        df = pd.DataFrame(equipment_catalog)
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Equipment_Catalog')
        output.seek(0)
        return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment;filename=equipment_catalog.xlsx"})
    except Exception as e:
        print(f"ERROR: Error exporting equipment catalog: {e}", file=sys.stderr)
        flash('เกิดข้อผิดพลาดในการส่งออกแคตตาล็อกอุปกรณ์', 'danger')
        return redirect(url_for('settings_page'))

@app.route('/import_equipment_catalog', methods=['POST'])
def import_equipment_catalog():
    """Imports equipment catalog from Excel."""
    if 'excel_file' not in request.files or not request.files['excel_file'].filename:
        flash('กรุณาเลือกไฟล์ Excel', 'danger')
        return redirect(url_for('settings_page'))
    file = request.files['excel_file']
    if file and file.filename.endswith(('.xls', '.xlsx')):
        try:
            df = pd.read_excel(file.stream)
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
            print(f"ERROR: Error importing Excel: {e}", file=sys.stderr)
            flash(f"เกิดข้อผิดพลาดในการนำเข้าไฟล์: {e}", 'danger')
    else:
        flash('รองรับเฉพาะไฟล์ Excel (.xls, .xlsx) เท่านั้น', 'danger')
    return redirect(url_for('settings_page'))

# --- LINE Bot Handlers ---
def create_task_list_message(title, tasks, limit=None):
    """Creates a text message for LINE from a list of tasks."""
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
    """Handles 'outstanding tasks' LINE command."""
    tasks_raw = get_google_tasks_for_report(show_completed=False) or []
    outstanding_tasks = [task for task in tasks_raw if task.get('status') == 'needsAction']
    reply_message = create_task_list_message("รายการงานที่ยังไม่เสร็จ", outstanding_tasks)
    line_bot_api.reply_message(event.reply_token, reply_message)

def handle_completed_tasks_command(event):
    """Handles 'completed tasks' LINE command."""
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    completed_tasks = [task for task in tasks_raw if task.get('status') == 'completed']
    completed_tasks.sort(key=lambda x: x.get('completed', ''), reverse=True)
    reply_message = create_task_list_message("งานที่เสร็จล่าสุด (5 รายการ)", completed_tasks, limit=5)
    line_bot_api.reply_message(event.reply_token, reply_message)

def handle_daily_tasks_command(event, day_type):
    """Handles 'today's tasks' or 'tomorrow's tasks' LINE commands."""
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
        if task.get('status') == 'needsAction' and task.get('due'):
            try:
                due_dt_utc = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                due_date_local = due_dt_utc.astimezone(THAILAND_TZ).date()
                if due_date_local == target_date:
                    filtered_tasks.append(task)
            except (ValueError, TypeError):
                continue
    
    reply_message = create_task_list_message(title, filtered_tasks)
    line_bot_api.reply_message(event.reply_token, reply_message)

def handle_create_new_task_command(event):
    """Handles 'create new task' LINE command."""
    if not LIFF_ID_FORM:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ไม่สามารถสร้างงานได้: ไม่พบ LIFF ID สำหรับฟอร์ม"))
        return

    liff_url = f"https://liff.line.me/{LIFF_ID_FORM}" 

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
    """Handles 'view task by name' LINE command."""
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
        print(f"ERROR: Error in handle_view_task_by_name_command: {e}", file=sys.stderr)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ขออภัย, เกิดข้อผิดพลาดในการค้นหางานครับ"))

def create_task_flex_message(task):
    """Creates a Flex Message bubble for a task."""
    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    parsed_dates = parse_google_task_dates(task)
    update_url = url_for('task_details', task_id=task.get('id'), _external=True)
    
    bubble = BubbleContainer(
        direction='ltr',
        body=BoxComponent(
            layout='vertical',
            spacing='md',
            contents=[
                TextComponent(text=task.get('title', 'ไม่มีรายละเอียด'), weight='bold', size='lg', wrap=True),
                SeparatorComponent(margin='md'),
                BoxComponent(
                    layout='vertical',
                    margin='lg',
                    spacing='sm',
                    contents=[
                        BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='ลูกค้า:', color='#AAAAAA', size='sm', flex=2), TextComponent(text=customer_info.get('name', '-'), wrap=True, color='#666666', size='sm', flex=5)]),
                        BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='นัดหมาย:', color='#AAAAAA', size='sm', flex=2), TextComponent(text=parsed_dates.get('due_formatted', '-'), wrap=True, color='#666666', size='sm', flex=5)])
                    ]
                ),
            ]
        ),
        footer=BoxComponent(
            layout='vertical',
            spacing='sm',
            contents=[
                ButtonComponent(style='primary', height='sm', action=URIAction(label='📝 เปิดในเว็บ', uri=update_url))
            ]
        )
    )
    return bubble

@app.route("/callback", methods=['POST'])
def callback():
    """Handles LINE webhook events."""
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    # app.logger.info("Request body: " + body) # Use print for logs before app is fully set up
    print("INFO: LINE Webhook Request body: " + body, file=sys.stderr)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print(f"ERROR: InvalidSignatureError: {body}", file=sys.stderr)
        abort(400)
    except Exception as e:
        print(f"ERROR: Unhandled error in webhook: {e}", file=sys.stderr)
        abort(500)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage) # Keep existing TextMessage handler
@handler.add(PostbackEvent) # Add PostbackEvent handler
def handle_line_event(event): # Renamed to handle both MessageEvent and PostbackEvent
    """Handles various LINE events (messages and postbacks)."""
    if isinstance(event, PostbackEvent):
        print(f"INFO: Received PostbackEvent: {event.postback.data}", file=sys.stderr)
        data = event.postback.data
        params = dict(item.split('=') for item in data.split('&'))

        action = params.get('action')
        if action == 'customer_feedback':
            task_id = params.get('task_id')
            feedback_type = params.get('feedback')
            customer_line_user_id = event.source.userId # NEW: Get customer's LINE User ID from Postback

            task = get_single_task(task_id)
            if not task:
                print(f"ERROR: Postback for unknown task_id: {task_id}", file=sys.stderr)
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
            print(f"INFO: Task {task_id} updated with feedback: {feedback_type}. Status set to {new_task_status}. Customer ID: {customer_line_user_id}", file=sys.stderr)
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
                        print(f"INFO: Sent problem notification for task {task_id} to admin group.", file=sys.stderr)
                    except Exception as e:
                        print(f"ERROR: Failed to send problem notification to admin group: {e}", file=sys.stderr)
            
        
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
    """Generates QR code for customer onboarding."""
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
    """Renders the customer onboarding LIFF form."""
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
        print(f"INFO: Successfully saved customer LINE ID {customer_line_user_id} for task {task_id}.", file=sys.stderr)
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
            print(f"INFO: Sent welcome message to new customer {customer_line_user_id}.", file=sys.stderr)
        except Exception as e:
            print(f"ERROR: Failed to send welcome message to customer {customer_line_user_id}: {e}", file=sys.stderr)

        return jsonify({"status": "success", "message": "LINE ID saved"}), 200
    else:
        print(f"ERROR: Failed to save customer LINE ID {customer_line_user_id} for task {task_id}.", file=sys.stderr)
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
            print(f"ERROR: Invalid preferred_datetime format from form: {preferred_datetime_thai}", file=sys.stderr)
    
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
        for report in sorted(tech_reports_history, key=lambda x: x.get('summary_date', '')):
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
                print(f"INFO: Sent problem notification for task {task_id} to admin group.", file=sys.stderr)
            except Exception as e:
                print(f"ERROR: Failed to send problem notification to admin group: {e}", file=sys.stderr)

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
                print(f"INFO: Sent thank you message to customer {customer_line_user_id} for task {task_id}.", file=sys.stderr)
            except Exception as e:
                print(f"ERROR: Failed to send thank you message to customer {customer_line_user_id}: {e}", file=sys.stderr)

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
            onload = function() {{ 
                if (liff.isInClient()) {{
                    setTimeout(() => {{ liff.closeWindow(); }}, 1000);
                }}
            }};
        </script>
    </body>
    </html>
    """

# --- Main entry point for Gunicorn ---
# Gunicorn will call this to get the Flask app instance.
# All app initialization logic is now encapsulated here.
def get_wsgi_app():
    if not os.environ.get('FLASK_SECRET_KEY'):
        print("WARNING: FLASK_SECRET_KEY environment variable is not set. Using a default key.", file=sys.stderr)
        app.secret_key = 'a_very_secret_key_for_dev' # Only for development, set env var in production!

    if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
        print("ERROR: LINE Bot credentials (LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET) are not set in environment variables.", file=sys.stderr)
        # sys.exit("LINE Bot credentials are not set in environment variables.") # Don't exit here, just log error

    # Initialize app settings and scheduler after everything is defined
    # These functions use global variables that need to be correctly populated
    with app.app_context():
        load_settings_from_drive_on_startup()
        global _APP_SETTINGS_STORE
        _APP_SETTINGS_STORE = get_app_settings()
        run_scheduler()
    
    # Context processor needs to be set up when the app is created or within app_context
    @app.context_processor
    def inject_now():
        return {'now': datetime.datetime.now(THAILAND_TZ)}

    return app

# Gunicorn will look for 'application' or 'app' by default.
# We explicitly name it 'app' for Gunicorn to find it.
app = get_wsgi_app()

if __name__ == '__main__':
    # This block is for local development only, not typically run by Gunicorn on Render.
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)

