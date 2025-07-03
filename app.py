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
from datetime import timezone

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, render_template, redirect, url_for, abort, flash, jsonify, Response, session
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
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.http import MediaIoBaseUpload

import pandas as pd
from dateutil.parser import parse as date_parse

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
GOOGLE_TASKS_LIST_ID = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
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

# Register dateutil_parse filter for Jinja2
app.jinja_env.filters['dateutil_parse'] = date_parse

# --- Settings Management ---
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
    'qrcode_settings': { 'box_size': 8, 'border': 4, 'fill_color': '#28a745', 'back_color': '#FFFFFF', 'custom_url': '' },
    'equipment_catalog': [],
    'auto_backup': { 'enabled': False, 'hour_thai': 2, 'minute_thai': 0 },
    'shop_info': { 'contact_phone': '081-XXX-XXXX', 'line_id': '@ComphoneService' },
    'technician_list': []
}
_APP_SETTINGS_STORE = {}

#<editor-fold desc="Helper and Utility Functions">

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
    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')

    if google_token_json_str:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(google_token_json_str), SCOPES)
        except Exception as e:
            app.logger.warning(f"Could not load token from GOOGLE_TOKEN_JSON env var: {e}. Please check format.")
            creds = None

    if creds and creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            app.logger.info("Refreshed Google access token.")
            app.logger.info(f"New GOOGLE_TOKEN_JSON after refresh (copy this to Render env var): {creds.to_json()}")
        except Exception as e:
            app.logger.error(f"Error refreshing token: {e}")
            creds = None

    if not creds or not creds.valid:
        app.logger.error("No valid Google credentials available. API service cannot be built.")
        app.logger.error("Please ensure GOOGLE_TOKEN_JSON environment variable is set and valid, or that authorization was successful.")
        return None

    return build(api_name, api_version, credentials=creds)

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
        response = service.files().list(q=query, spaces='drive', fields='files(id, name)', orderBy='modifiedTime desc', pageSize=1).execute()
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
                if key in loaded:
                    if isinstance(default_value, dict) and isinstance(loaded[key], dict):
                        _APP_SETTINGS_STORE[key].update(loaded[key])
                    elif isinstance(default_value, list) and isinstance(loaded[key], list):
                        _APP_SETTINGS_STORE[key] = loaded[key]
                    else:
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
        elif isinstance(value, list) and key in current_settings and isinstance(current_settings[key], list):
            current_settings[key] = value
        else:
            current_settings[key] = value
    _APP_SETTINGS_STORE = current_settings
    return save_settings_to_file(_APP_SETTINGS_STORE)


@cached(cache)
def get_google_tasks_for_report(show_completed=True):
    """Fetches tasks from Google Tasks API."""
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
    if not task_id: return None
    service = get_google_tasks_service()
    if not service: return None
    try:
        return service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
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
            task['completed'] = datetime.datetime.now(pytz.utc).isoformat().replace('+00:00', 'Z')
            task.pop('due', None)
        else:
            task.pop('completed', None)
            if due: task['due'] = due
            else: task.pop('due', None)

        return service.tasks().update(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id, body=task).execute()
    except HttpError as e:
        app.logger.error(f"Failed to update task {task_id}: {e}")
        return None

def parse_customer_info_from_notes(notes):
    """
    Parses customer information and map URL from task notes robustly.
    The main task detail is stored in the task's 'title', not in the notes.
    """
    info = {'name': '', 'phone': '', 'address': '', 'map_url': None}
    if not notes: return info

    name_match = re.search(r"ลูกค้า:\s*(.*)", notes, re.IGNORECASE)
    phone_match = re.search(r"เบอร์โทรศัพท์:\s*(.*)", notes, re.IGNORECASE)
    address_match = re.search(r"ที่อยู่:\s*(.*)", notes, re.IGNORECASE)
    map_url_match = re.search(r"(https?:\/\/(?:www\.)?(?:google\.com\/maps\/[^\s]+|goo\.gl\/maps\/[^\s]+))", notes)


    if name_match:
        info['name'] = name_match.group(1).strip()
    if phone_match:
        info['phone'] = phone_match.group(1).strip()
    if address_match:
        info['address'] = address_match.group(1).strip()
    if map_url_match:
        info['map_url'] = map_url_match.group(1).strip()
    
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
                dt_utc = date_parse(parsed[key])
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
        except json.JSONDecodeError:
            app.logger.warning(f"Failed to decode tech report JSON: {json_str[:100]}...")
    
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
            quantity_str = parts[1].strip() if len(parts) > 1 else '1'
            try:
                quantity_num = float(quantity_str)
                equipment_list.append({"item": item_name, "quantity": quantity_num})
            except ValueError:
                equipment_list.append({"item": item_name, "quantity": quantity_str})
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
                if item.get("quantity") is not None:
                    if isinstance(item['quantity'], (int, float)):
                        line += f" (x{item['quantity']:g})"
                    else:
                        line += f" ({item['quantity']})"
                lines.append(line)
            elif isinstance(item, str):
                lines.append(item)
    return "\n".join(lines) if lines else 'N/A'

@app.context_processor
def inject_now():
    """Injects current datetime and timezone into Jinja2 templates."""
    return {'now': datetime.datetime.now(THAILAND_TZ), 'thaizone': THAILAND_TZ}

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
            zf.writestr('data/settings.json', json.dumps(get_app_settings(), indent=4, ensure_ascii=False))

            project_root = os.path.dirname(os.path.abspath(__file__))
            for folder, _, files in os.walk(project_root):
                for file in files:
                    if file.endswith(('.py', '.html', '.css', '.js', '.json', 'Procfile', 'requirements.txt')) \
                       and file not in ['token.json', '.env', SETTINGS_FILE]:
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

        service.permissions().create(fileId=file_obj['id'], body={'role': 'reader', 'type': 'anyone'}).execute()

        app.logger.info(f"Successfully uploaded backup '{filename}' to Drive (ID: {file_obj['id']})")
        return True
    except HttpError as e:
        app.logger.error(f'Drive upload error: {e}')
        return False

#</editor-fold>

#<editor-fold desc="Scheduled Jobs">

def scheduled_backup_job():
    with app.app_context():
        app.logger.info("Running scheduled backup job...")

        memory_file_zip, filename_zip = _create_backup_zip()
        if memory_file_zip and filename_zip:
            if _upload_backup_to_drive(memory_file_zip, filename_zip, GOOGLE_DRIVE_FOLDER_ID):
                app.logger.info("Automatic full system backup successful.")
            else:
                app.logger.error("Automatic full system backup failed.")

        if GOOGLE_SETTINGS_BACKUP_FOLDER_ID:
            settings_data = get_app_settings()
            settings_json_bytes = BytesIO(json.dumps(settings_data, ensure_ascii=False, indent=4).encode('utf-8'))
            settings_backup_filename = "settings_backup.json"

            service = get_google_drive_service()
            if service:
                try:
                    query = f"name = 'settings_backup.json' and '{GOOGLE_SETTINGS_BACKUP_FOLDER_ID}' in parents"
                    response = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
                    for file_item in response.get('files', []):
                        service.files().delete(fileId=file_item['id']).execute()
                        app.logger.info(f"Deleted old settings_backup.json (ID: {file_item['id']}) from Drive.")
                except HttpError as e:
                    app.logger.warning(f"Could not delete old settings_backup.json from Drive: {e}")

            if _upload_backup_to_drive(settings_json_bytes, settings_backup_filename, GOOGLE_SETTINGS_BACKUP_FOLDER_ID):
                app.logger.info("Automatic settings backup successful.")
            else:
                app.logger.error("Automatic settings backup failed.")

def scheduled_appointment_reminder_job():
    with app.app_context():
        app.logger.info("Running scheduled appointment reminder job...")
        settings = get_app_settings()
        recipients = settings.get('line_recipients', {})

        if not recipients.get('admin_group_id') and not recipients.get('technician_group_id'):
            app.logger.info("No LINE admin or technician group ID set for appointment reminders. Skipping.")
            return

        tasks_raw = get_google_tasks_for_report(show_completed=False) or []
        today_thai = datetime.date.today()
        upcoming_appointments = []

        for task in tasks_raw:
            if task.get('status') == 'needsAction' and task.get('due'):
                try:
                    due_dt_utc = date_parse(task['due'])
                    if due_dt_utc.astimezone(THAILAND_TZ).date() == today_thai:
                        upcoming_appointments.append(task)
                except (ValueError, TypeError):
                    app.logger.warning(f"Could not parse due date for reminder task {task.get('id')}: {task.get('due')}")
                    continue

        if not upcoming_appointments:
            app.logger.info("No upcoming appointments for today.")
            return

        upcoming_appointments.sort(key=lambda x: date_parse(x['due']) if x.get('due') else datetime.datetime.max.replace(tzinfo=pytz.utc))

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
                    app.logger.info(f"Sent appointment reminder for task {task['id']} to admin group.")
                if recipients.get('technician_group_id') and recipients['technician_group_id'] != recipients.get('admin_group_id'):
                    line_bot_api.push_message(recipients['technician_group_id'], TextSendMessage(text=message_text))
                    app.logger.info(f"Sent appointment reminder for task {task['id']} to technician group.")
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
        two_days_ago_utc = now_utc - datetime.timedelta(days=2)
        one_day_ago_utc = now_utc - datetime.timedelta(days=1)


        for task in tasks_raw:
            if task.get('status') == 'completed' and task.get('completed'):
                try:
                    completed_dt_utc = date_parse(task['completed'])

                    if two_days_ago_utc <= completed_dt_utc < one_day_ago_utc:
                        notes = task.get('notes', '')
                        feedback_data = parse_customer_feedback_from_notes(notes)

                        if 'follow_up_sent_date' in feedback_data:
                            app.logger.info(f"Skipping follow-up for task {task['id']}: already sent on {feedback_data['follow_up_sent_date']}")
                            continue

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
                                app.logger.info(f"Sent follow-up message to customer {customer_line_id} for task {task['id']}.")
                            except Exception as e:
                                app.logger.error(f"Failed to send direct follow-up to {customer_line_id}: {e}. Notifying admin.")
                                if admin_group_id:
                                    line_bot_api.push_message(admin_group_id, [TextSendMessage(text=f"⚠️ ส่ง Follow-up ให้ลูกค้า {customer_info.get('name')} (Task ID: {task['id']}) ไม่สำเร็จ โปรดส่งข้อความนี้แทน:"), flex_message])
                        elif admin_group_id:
                            admin_text = f"✅ กรุณาส่งแบบสอบถามนี้ให้ลูกค้า:\nคุณ {customer_info.get('name')} (โทร: {customer_info.get('phone')}, Task ID: {task['id']})"
                            line_bot_api.push_message(admin_group_id, [TextSendMessage(text=admin_text), flex_message])
                            app.logger.info(f"Notified admin to manually send follow-up for task {task['id']}.")

                except Exception as e:
                    app.logger.warning(f"Could not process task {task.get('id')} for follow-up: {e}", exc_info=True)

scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)

def run_scheduler():
    """Initializes and runs the APScheduler jobs."""
    global scheduler

    settings = get_app_settings()

    if scheduler.running:
        app.logger.info("Scheduler already running, shutting down before reconfiguring...")
        scheduler.shutdown(wait=False)
    scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)

    ab = settings.get('auto_backup', {})
    if ab.get('enabled'):
        scheduler.add_job(scheduled_backup_job, CronTrigger(hour=ab.get('hour_thai', 2), minute=ab.get('minute_thai', 0)), id='auto_system_backup', replace_existing=True)
        app.logger.info(f"Scheduled auto backup for {ab.get('hour_thai', 2)}:{ab.get('minute_thai', 0)} Thai time.")
    else:
        if scheduler.get_job('auto_system_backup'):
            scheduler.remove_job('auto_system_backup')
            app.logger.info("Auto backup job disabled and removed.")

    rt = settings.get('report_times', {})
    scheduler.add_job(scheduled_appointment_reminder_job, CronTrigger(hour=rt.get('appointment_reminder_hour_thai', 7), minute=0), id='daily_appointment_reminder', replace_existing=True)
    scheduler.add_job(scheduled_customer_follow_up_job, CronTrigger(hour=rt.get('customer_followup_hour_thai', 9), minute=5), id='daily_customer_followup', replace_existing=True)
    app.logger.info(f"Scheduled appointment reminders for {rt.get('appointment_reminder_hour_thai', 7)}:00 and customer follow-up for {rt.get('customer_followup_hour_thai', 9)}:05 Thai time.")


    scheduler.start()
    app.logger.info("APScheduler started/reconfigured.")
    atexit.register(lambda: scheduler.shutdown(wait=False))

#</editor-fold>

# --- Initial app setup calls ---
with app.app_context():
    load_settings_from_drive_on_startup()
    _APP_SETTINGS_STORE = get_app_settings()
    run_scheduler()


# --- Flask Routes ---
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
                dt_local = THAILAND_TZ.localize(date_parse(appointment_str))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
            except ValueError:
                app.logger.error(f"Invalid appointment format for new task: {appointment_str}")
                flash('รูปแบบวันเวลานัดหมายไม่ถูกต้อง โปรดตรวจสอบ', 'warning')
                return render_template('form.html', form_data=request.form)

        if create_google_task(task_title, notes=notes, due=due_date_gmt):
            cache.clear()
            flash('สร้างงานใหม่เรียบร้อยแล้ว!', 'success')
            return redirect(url_for('summary'))
        else:
            flash('เกิดข้อผิดพลาดในการสร้างงาน', 'danger')
    return render_template('form.html')

@app.route('/summary')
def summary():
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()

    current_time_utc = datetime.datetime.now(pytz.utc)
    final_tasks = []
    stats = {'needsAction': 0, 'completed': 0, 'overdue': 0, 'total': len(tasks_raw)}

    for task in tasks_raw:
        task_status = task.get('status', 'needsAction')
        is_overdue = False
        if task_status == 'needsAction' and task.get('due'):
            try:
                due_dt_utc = date_parse(task['due'])
                if due_dt_utc < current_time_utc: is_overdue = True
            except (ValueError, TypeError): pass

        if task_status == 'completed': stats['completed'] += 1
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

    final_tasks.sort(key=lambda x: (x.get('status') == 'completed', x.get('due') is None, x.get('due', '')))

    completed_tasks_for_chart = [t for t in tasks_raw if t.get('status') == 'completed' and t.get('completed')]
    monthly_counts = defaultdict(int)
    today_thai = datetime.datetime.now(THAILAND_TZ)
    month_labels = []
    chart_values = []

    for i in range(12):
        target_date = today_thai - datetime.timedelta(days=30 * (11 - i))
        month_key = target_date.strftime('%Y-%m')
        month_labels.append(target_date.strftime('%b %Y'))
        
        count = 0
        for task in completed_tasks_for_chart:
            try:
                completed_dt = date_parse(task['completed']).astimezone(THAILAND_TZ)
                if completed_dt.strftime('%Y-%m') == month_key:
                    count += 1
            except Exception as e:
                app.logger.warning(f"Error parsing completed date for chart for task {task.get('id')}: {e}")
        chart_values.append(count)
    
    chart_data = {'labels': month_labels, 'values': chart_values}

    return render_template("dashboard.html",
                           tasks=final_tasks,
                           summary=stats,
                           search_query=search_query,
                           status_filter=status_filter,
                           chart_data=chart_data)


@app.route('/task/<task_id>', methods=['GET', 'POST'])
def task_details(task_id):
    if request.method == 'POST':
        new_title = str(request.form.get('task_title', '')).strip()
        if not new_title:
            flash('กรุณากรอกรายละเอียดงาน', 'danger')
            return redirect(url_for('task_details', task_id=task_id))

        task_raw = get_single_task(task_id)
        if not task_raw:
            flash('ไม่พบงานที่ต้องการอัปเดต', 'danger')
            abort(404)

        notes_lines = [
            f"ลูกค้า: {str(request.form.get('customer_name', '')).strip()}",
            f"เบอร์โทรศัพท์: {str(request.form.get('customer_phone', '')).strip()}",
            f"ที่อยู่: {str(request.form.get('address', '')).strip()}",
        ]
        map_url = str(request.form.get('latitude_longitude', '')).strip()
        if map_url: notes_lines.append(map_url)
        new_base_notes = "\n".join(filter(None, notes_lines))

        history, _ = parse_tech_report_from_notes(task_raw.get('notes', ''))
        feedback_data = parse_customer_feedback_from_notes(task_raw.get('notes', ''))

        work_summary = str(request.form.get('work_summary', '')).strip()
        files = request.files.getlist('files[]')
        selected_technicians = request.form.getlist('technicians')

        if work_summary or any(f.filename for f in files):
            if not selected_technicians:
                flash('กรุณาเลือกช่างผู้รับผิดชอบสำหรับรายงานนี้', 'warning')
                return redirect(url_for('task_details', task_id=task_id))

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
                elif file and not allowed_file(file.filename):
                    flash(f'ไฟล์ {file.filename} ไม่อนุญาตให้แนบ', 'warning')

            history.append({
                'summary_date': datetime.datetime.now(THAILAND_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                'work_summary': work_summary,
                'equipment_used': _parse_equipment_string(request.form.get('equipment_used', '')),
                'attachment_urls': new_attachment_urls,
                'technicians': selected_technicians
            })
            history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)


        all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])

        final_notes = new_base_notes
        if all_reports_text:
            final_notes += all_reports_text
        if feedback_data:
            final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

        due_date_gmt = None
        appointment_str = str(request.form.get('appointment_due', '')).strip()
        if appointment_str:
            try:
                dt_local = THAILAND_TZ.localize(date_parse(appointment_str))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
            except ValueError:
                app.logger.error(f"Invalid appointment format for task {task_id}: {appointment_str}")
                flash('รูปแบบวันเวลานัดหมายไม่ถูกต้อง โปรดตรวจสอบ', 'warning')
                return redirect(url_for('task_details', task_id=task_id))
        
        update_status = request.form.get('status')
        if update_status == 'completed' and 'due' in task_raw:
            due_date_gmt = None

        if update_google_task(task_id, title=new_title, notes=final_notes, status=update_status, due=due_date_gmt):
            cache.clear()
            flash('บันทึกการเปลี่ยนแปลงเรียบร้อยแล้ว!', 'success')
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกการเปลี่ยนแปลง', 'danger')
        
        return redirect(url_for('task_details', task_id=task_id))

    task_raw = get_single_task(task_id)
    if not task_raw: abort(404)
    
    task = parse_google_task_dates(task_raw)
    notes = task.get('notes', '')
    task['customer'] = parse_customer_info_from_notes(notes)
    task['tech_reports_history'], _ = parse_tech_report_from_notes(notes)
    task['customer_feedback'] = parse_customer_feedback_from_notes(notes)
    
    app_settings = get_app_settings()
    
    pre_selected_technicians = []
    if task['tech_reports_history'] and task['tech_reports_history'][0].get('technicians'):
        pre_selected_technicians = task['tech_reports_history'][0]['technicians']

    return render_template('update_task_details.html',
                           task=task,
                           common_equipment_items=app_settings.get('common_equipment_items', []),
                           technician_list=app_settings.get('technician_list', []),
                           pre_selected_technicians=pre_selected_technicians)


@app.route('/delete_task/<task_id>', methods=['POST'])
def delete_task(task_id):
    if delete_google_task(task_id):
        flash('ลบงานเรียบร้อยแล้ว!', 'success')
        cache.clear()
    else:
        flash('เกิดข้อผิดพลาดในการลบงาน', 'danger')
    return redirect(url_for('summary'))

# --- NEW: API route for deleting tasks via JS ---
@app.route('/api/delete_task/<task_id>', methods=['POST'])
def api_delete_task(task_id):
    if delete_google_task(task_id):
        cache.clear()
        return jsonify({'status': 'success', 'message': 'Task deleted successfully.'})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to delete task.'}), 500

@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        technician_names_text = request.form.get('technician_list', '').strip()
        technician_list = [name.strip() for name in technician_names_text.splitlines() if name.strip()]

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
            'technician_list': technician_list
        })

        run_scheduler()
        flash('บันทึกการตั้งค่าเรียบร้อยแล้ว!', 'success')
        cache.clear()
        return redirect(url_for('settings_page'))

    current_settings = get_app_settings()
    general_summary_url = url_for('summary', _external=True)
    qr_url = current_settings.get('qrcode_settings', {}).get('custom_url') or general_summary_url
    qr_settings = current_settings.get('qrcode_settings', {})

    qr_code_base64_general = generate_qr_code_base64(
        qr_url, box_size=qr_settings.get('box_size', 8), border=qr_settings.get('border', 4),
        fill_color=qr_settings.get('fill_color', '#28a745'), back_color=qr_settings.get('back_color', '#FFFFFF')
    )
    return render_template('settings_page.html', settings=current_settings, qr_code_base64_general=qr_code_base64_general, general_summary_url=general_summary_url)

@app.route('/test_notification', methods=['POST'])
def test_notification():
    recipient_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
    if recipient_id:
        try:
            line_bot_api.push_message(recipient_id, TextSendMessage(text="[ทดสอบ] นี่คือข้อความทดสอบจากระบบ"))
            flash(f'ส่งข้อความทดสอบไปที่ ID: {recipient_id} สำเร็จ!', 'success')
        except Exception as e:
            flash(f'เกิดข้อผิดพลาดในการส่ง: {e}', 'danger')
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
    # This will run the backup job synchronously for immediate feedback
    scheduled_backup_job()
    flash('ดำเนินการสำรองข้อมูลอัตโนมัติไปยัง Google Drive แล้ว (โปรดตรวจสอบ logs สำหรับสถานะ)!', 'info')
    return redirect(url_for('settings_page'))

@app.route('/export_equipment_catalog', methods=['GET'])
def export_equipment_catalog():
    try:
        df = pd.DataFrame(get_app_settings().get('equipment_catalog', []))
        if df.empty:
            flash('ไม่มีข้อมูลอุปกรณ์ในแคตตาล็อก', 'warning')
            return redirect(url_for('settings_page') )
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
            required_cols = ['item_name', 'unit', 'price']
            if not all(col in df.columns for col in required_cols):
                flash(f'ไฟล์ Excel ต้องมีคอลัมน์: {", ".join(required_cols)}', 'danger')
            else:
                imported_catalog = []
                for _, row in df.iterrows():
                    item = {'item_name': str(row['item_name']).strip()}
                    if pd.notna(row['unit']):
                        item['unit'] = str(row['unit']).strip()
                    if pd.notna(row['price']):
                        try:
                            item['price'] = float(row['price'])
                        except ValueError:
                            app.logger.warning(f"Non-numeric price found for {row['item_name']}. Setting to 0.")
                            item['price'] = 0.0
                    imported_catalog.append(item)

                save_app_settings({'equipment_catalog': imported_catalog})
                flash('นำเข้าแคตตาล็อกอุปกรณ์เรียบร้อยแล้ว!', 'success')
        except Exception as e:
            flash(f"เกิดข้อผิดพลาดในการนำเข้าไฟล์: {e}", 'danger')
            app.logger.error(f"Error during equipment catalog import: {e}", exc_info=True)
    else:
        flash('รองรับเฉพาะไฟล์ Excel (.xls, .xlsx) เท่านั้น', 'danger')
    return redirect(url_for('settings_page'))

# --- REVISED: Combined import route with duplicate handling ---
@app.route('/api/import_backup_file', methods=['POST'])
def import_backup_file():
    if 'backup_file' not in request.files or not request.files['backup_file'].filename:
        return jsonify({"status": "error", "message": "No backup file selected."}), 400

    file = request.files['backup_file']
    file_type = request.form.get('file_type')

    if file_type not in ['tasks_json', 'settings_json']:
        return jsonify({"status": "error", "message": "Invalid file type specified."}), 400

    if not file.filename.endswith('.json'):
        return jsonify({"status": "error", "message": "Only JSON files are allowed for import."}), 400

    try:
        data = json.load(file.stream)

        if file_type == 'tasks_json':
            if not isinstance(data, list):
                return jsonify({"status": "error", "message": "JSON file content is not a list of tasks."}), 400

            service = get_google_tasks_service()
            if not service:
                return jsonify({"status": "error", "message": "Could not connect to Google Tasks service. Check credentials."}), 500

            created_count, updated_count, skipped_count = 0, 0, 0
            
            for task_data in data:
                original_id = task_data.get('id')
                task_title_for_log = task_data.get('title', 'N/A')

                try:
                    # ---- Process dates first ----
                    for date_field in ['due', 'completed']:
                        if date_field in task_data and task_data[date_field]:
                            try:
                                dt_obj = date_parse(task_data[date_field])
                                task_data[date_field] = dt_obj.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
                            except Exception as e:
                                app.logger.warning(f"Could not parse {date_field} for task '{task_title_for_log}': {e}. Skipping date.")
                                task_data.pop(date_field, None)
                    
                    if not original_id:
                        # If no original ID, must be a new task
                        task_data.pop('id', None) 
                        service.tasks().insert(tasklist=GOOGLE_TASKS_LIST_ID, body=task_data).execute()
                        created_count += 1
                        continue

                    # ---- Try to GET the task first ----
                    try:
                        service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=original_id).execute()
                        # If successful, task EXISTS -> UPDATE it
                        update_google_task(
                            task_id=original_id,
                            title=task_data.get('title'),
                            notes=task_data.get('notes'),
                            status=task_data.get('status'),
                            due=task_data.get('due')
                        )
                        updated_count += 1

                    except HttpError as e:
                        if e.resp.status == 404:
                            # If 404 Not Found, task does NOT exist -> INSERT it
                            task_data.pop('id', None) # Remove ID before inserting
                            read_only_fields = ['kind', 'selfLink', 'position', 'etag', 'updated']
                            for field in read_only_fields: task_data.pop(field, None)
                            
                            service.tasks().insert(tasklist=GOOGLE_TASKS_LIST_ID, body=task_data).execute()
                            created_count += 1
                        else:
                            # Other API errors
                            app.logger.error(f"API error checking task '{task_title_for_log}' (ID: {original_id}): {e}")
                            skipped_count += 1
                
                except Exception as e:
                    app.logger.error(f"Unexpected error processing task '{task_title_for_log}': {e}", exc_info=True)
                    skipped_count += 1
            
            cache.clear()
            message = f"นำเข้าสำเร็จ! สร้างใหม่: {created_count} งาน, อัปเดต: {updated_count} งาน, ข้าม: {skipped_count} งาน"
            return jsonify({"status": "success", "message": message})

        elif file_type == 'settings_json':
            if not isinstance(data, dict):
                return jsonify({"status": "error", "message": "JSON file content is not a settings object."}), 400
            
            if save_app_settings(data):
                run_scheduler()
                cache.clear()
                return jsonify({"status": "success", "message": "นำเข้าการตั้งค่าเรียบร้อยแล้ว!"})
            else:
                return jsonify({"status": "error", "message": "เกิดข้อผิดพลาดในการบันทึกการตั้งค่าที่นำเข้า"}), 500

    except json.JSONDecodeError:
        return jsonify({"status": "error", "message": "ไฟล์ JSON ไม่ถูกต้อง"}), 400
    except Exception as e:
        app.logger.error(f"Error during import: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"เกิดข้อผิดพลาดในการนำเข้า: {e}"}), 500


# NEW: Combined preview route for both tasks and settings
@app.route('/api/preview_backup_file', methods=['POST'])
def preview_backup_file():
    if 'backup_file' not in request.files or not request.files['backup_file'].filename:
        return jsonify({"status": "error", "message": "No backup file selected."}), 400

    file = request.files['backup_file']
    file_type = request.form.get('file_type')

    if file_type not in ['tasks_json', 'settings_json']:
        return jsonify({"status": "error", "message": "Invalid file type specified."}), 400

    if not file.filename.endswith('.json'):
        return jsonify({"status": "error", "message": "Only JSON files are allowed for preview."}), 400

    try:
        data = json.load(file.stream)

        if file_type == 'tasks_json':
            if not isinstance(data, list):
                return jsonify({"status": "error", "message": "JSON file content is not a list of tasks."}), 400
            
            task_count = len(data)
            example_tasks = []
            for i, task in enumerate(data[:5]):
                parsed_task_dates = {'due_formatted': 'N/A'}
                if 'due' in task and task['due']:
                    try:
                        parsed_task_dates = parse_google_task_dates(task)
                    except Exception:
                        pass
                example_tasks.append({
                    'id': task.get('id', 'N/A'),
                    'title': task.get('title', 'No Title'),
                    'status': task.get('status', 'N/A'),
                    'due': parsed_task_dates.get('due_formatted', 'N/A'),
                    'customer_name': parse_customer_info_from_notes(task.get('notes', '')).get('name', 'N/A')
                })
            return jsonify({
                "status": "success",
                "message": f"พบ {task_count} งานในไฟล์. แสดงตัวอย่าง {len(example_tasks)} งานแรก.",
                "type": "tasks",
                "task_count": task_count,
                "example_tasks": example_tasks
            })
        
        elif file_type == 'settings_json':
            if not isinstance(data, dict):
                return jsonify({"status": "error", "message": "JSON file content is not a settings object."}), 400
            
            preview_settings = {
                "appointment_reminder_hour_thai": data.get('report_times', {}).get('appointment_reminder_hour_thai', 'N/A'),
                "admin_group_id": data.get('line_recipients', {}).get('admin_group_id', 'N/A'),
                "shop_contact_phone": data.get('shop_info', {}).get('contact_phone', 'N/A'),
                "technician_list_count": len(data.get('technician_list', []))
            }
            return jsonify({
                "status": "success",
                "message": "พบไฟล์การตั้งค่า. นี่คือตัวอย่างการตั้งค่าบางส่วน:",
                "type": "settings",
                "preview_settings": preview_settings
            })

    except json.JSONDecodeError:
        return jsonify({"status": "error", "message": "ไฟล์ JSON ไม่ถูกต้อง"}), 400
    except Exception as e:
        app.logger.error(f"Error during preview: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"เกิดข้อผิดพลาดในการดูตัวอย่าง: {e}"}), 500


@app.route('/technician_report')
def technician_report():
    now = datetime.datetime.now(THAILAND_TZ)

    try:
        selected_year = int(request.args.get('year', now.year))
        selected_month = int(request.args.get('month', now.month))
    except (ValueError, TypeError):
        selected_year = now.year
        selected_month = now.month
    
    all_months = []
    for i in range(1, 13):
        all_months.append({'value': i, 'name': datetime.date(2000, i, 1).strftime('%B')})

    tasks_raw = get_google_tasks_for_report(show_completed=True) or []

    report_data = defaultdict(lambda: {'count': 0, 'tasks': []})

    for task in tasks_raw:
        if task.get('status') == 'completed' and task.get('completed'):
            try:
                completed_dt_utc = date_parse(task['completed'])
                completed_dt_local = completed_dt_utc.astimezone(THAILAND_TZ)

                if completed_dt_local.year == selected_year and completed_dt_local.month == selected_month:
                    history, _ = parse_tech_report_from_notes(task.get('notes', ''))

                    technicians_on_this_task = set()
                    for report_entry in history:
                        technicians = report_entry.get('technicians', [])
                        if isinstance(technicians, list):
                            for tech_name in technicians:
                                if tech_name and isinstance(tech_name, str):
                                    technicians_on_this_task.add(tech_name.strip())
                    
                    for tech_name in sorted(list(technicians_on_this_task)):
                        report_data[tech_name]['count'] += 1
                        report_data[tech_name]['tasks'].append({
                            'id': task.get('id'),
                            'title': task.get('title'),
                            'completed_formatted': completed_dt_local.strftime("%d/%m/%Y %H:%M")
                        })
            except Exception as e:
                app.logger.warning(f"Could not process completed date or history for task {task.get('id')}: {e}", exc_info=True)
                continue

    current_year = now.year
    years = list(range(current_year - 5, current_year + 2))

    return render_template('technician_report.html',
                           report_data=report_data,
                           selected_year=selected_year,
                           selected_month=selected_month,
                           years=years,
                           months=all_months)

# --- NEW: Route for managing duplicate tasks ---
@app.route('/manage_duplicates')
def manage_duplicates():
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    tasks_by_title = defaultdict(list)

    # Group tasks by title
    for task in tasks_raw:
        if task.get('title'):
            tasks_by_title[task['title'].strip()].append(task)
    
    # Filter for potential duplicates (groups with more than 1 task)
    potential_duplicate_sets = {title: tasks for title, tasks in tasks_by_title.items() if len(tasks) > 1}

    # Parse details for display
    for title, tasks in potential_duplicate_sets.items():
        for i, task in enumerate(tasks):
            current_time_utc = datetime.datetime.now(pytz.utc)
            is_overdue = False
            if task.get('status') == 'needsAction' and task.get('due'):
                try:
                    due_dt_utc = date_parse(task['due'])
                    if due_dt_utc < current_time_utc: is_overdue = True
                except (ValueError, TypeError): pass

            parsed_task = parse_google_task_dates(task)
            parsed_task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
            parsed_task['is_overdue'] = is_overdue
            potential_duplicate_sets[title][i] = parsed_task

    return render_template('manage_duplicates.html', potential_duplicate_sets=potential_duplicate_sets)


# --- Customer Onboarding & Feedback Routes ---

@app.route('/generate_customer_onboarding_qr')
def generate_customer_onboarding_qr():
    task_id = request.args.get('task_id')
    task = get_single_task(task_id)
    if not task: abort(404)
    if not LIFF_ID_FORM:
        flash("ไม่สามารถสร้าง QR Code ได้: ไม่พบ LIFF_ID_FORM ใน Environment Variables", 'danger')
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
        completed_tasks = [task for task in tasks_raw if task.get('status') == 'completed' and task.get('completed')]
        
        if not completed_tasks:
            flash('ไม่พบงานที่เสร็จแล้วสำหรับใช้ทดสอบ.', 'warning')
            return redirect(url_for('settings_page'))
            
        latest_task = max(completed_tasks, key=lambda x: date_parse(x['completed']))
        
        now_utc = datetime.datetime.now(pytz.utc)
        test_completed_time_utc = now_utc - datetime.timedelta(days=1, minutes=5)
        
        original_notes = latest_task.get('notes', '')
        feedback_data = parse_customer_feedback_from_notes(original_notes)
        
        feedback_data.pop('follow_up_sent_date', None)
        
        _, base_notes_content = parse_tech_report_from_notes(original_notes)
        tech_reports_section = "".join(re.findall(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", original_notes, re.DOTALL))
        
        updated_notes = base_notes_content.strip()
        if tech_reports_section:
            updated_notes += "\n\n" + tech_reports_section.strip()
        if feedback_data:
            updated_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        
        update_google_task(latest_task['id'],
                           notes=updated_notes,
                           status='completed',
                           due=None)
        
        latest_task['completed'] = test_completed_time_utc.isoformat().replace('+00:00', 'Z')
        
        cache.clear()
        
        scheduled_customer_follow_up_job()
        
        flash(f"กำลังทดสอบส่งแบบสอบถามสำหรับงานล่าสุด: '{latest_task.get('title')}' (Task ID: {latest_task.get('id')}). โปรดตรวจสอบ LINE Admin Group ID ของคุณ.", 'info')
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

    catalog = get_app_settings().get('equipment_catalog', [])
    catalog_dict = {item['item_name']: item for item in catalog if item.get('item_name')}

    detailed_costs, total_cost = [], 0.0

    if isinstance(equipment_used, list):
        for item_used in equipment_used:
            item_name = item_used.get('item')
            quantity = item_used.get('quantity', 0)

            if isinstance(quantity, (int, float)):
                catalog_item = catalog_dict.get(item_name, {})
                price_per_unit = float(catalog_item.get('price', 0))
                subtotal = quantity * price_per_unit
                total_cost += subtotal

                detailed_costs.append({
                    'item': item_name, 'quantity': quantity, 'unit': catalog_item.get('unit', ''),
                    'price_per_unit': price_per_unit, 'subtotal': subtotal
                })
            else:
                detailed_costs.append({
                    'item': item_name, 'quantity': quantity, 'unit': catalog_dict.get(item_name, {}).get('unit', ''),
                    'price_per_unit': 'N/A', 'subtotal': 'N/A'
                })

    return render_template('public_task_report.html',
                           task=task,
                           customer_info=customer_info,
                           latest_report=latest_report,
                           detailed_costs=detailed_costs,
                           total_cost=total_cost)

@app.route('/submit_customer_problem', methods=['POST'])
def submit_customer_problem():
    data = request.json
    task_id = data.get('task_id')
    problem_desc = data.get('problem_description')
    customer_line_user_id = data.get('customer_line_user_id')

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
    if customer_line_user_id:
        feedback_data['customer_line_user_id'] = customer_line_user_id

    _, base_notes = parse_tech_report_from_notes(notes)
    tech_reports_text = "".join(re.findall(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", notes, re.DOTALL))

    final_notes = f"{base_notes.strip()}\n\n{tech_reports_text.strip()}\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

    update_google_task(task_id=task_id, notes=final_notes, status='needsAction')
    cache.clear()

    admin_group_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
    if admin_group_id:
        customer_info = parse_customer_info_from_notes(notes)
        notif_text = (
            f"🚨 ลูกค้าแจ้งปัญหา!\n"
            f"งาน: {task.get('title')}\n"
            f"ลูกค้า: {customer_info.get('name', 'N/A')}\n"
            f"ปัญหา: {problem_desc}\n"
            f"ดูรายละเอียด: {url_for('task_details', task_id=task_id, _external=True)}"
        )
        try:
            line_bot_api.push_message(admin_group_id, TextSendMessage(text=notif_text))
            app.logger.info(f"Sent problem report notification for task {task_id} to admin.")
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
    
    if feedback_data.get('customer_line_user_id') != customer_line_id:
        feedback_data['customer_line_user_id'] = customer_line_id
        feedback_data['id_saved_date'] = datetime.datetime.now(THAILAND_TZ).isoformat()
        
        _, base_notes = parse_tech_report_from_notes(notes)
        tech_reports_text = "".join(re.findall(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", notes, re.DOTALL))
        
        final_notes = f"{base_notes.strip()}\n\n{tech_reports_text.strip()}\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        
        if update_google_task(task_id=task_id, notes=final_notes):
            cache.clear()
            shop_info = get_app_settings().get('shop_info', {})
            customer_info = parse_customer_info_from_notes(notes)
            welcome_msg = (
                f"เรียน คุณ{customer_info.get('name', 'ลูกค้า')},\n"
                f"ขอบคุณที่ลงทะเบียนกับ Comphone ครับ/ค่ะ!\n"
                f"เราจะใช้ LINE นี้เพื่อส่งแบบสอบถามและข้อมูลสำคัญอื่นๆ ครับ\n\n"
                f"ติดต่อสอบถาม:\n"
                f"โทร: {shop_info.get('contact_phone', '-')}\n"
                f"LINE ID: {shop_info.get('line_id', '-')}"
            )
            try:
                line_bot_api.push_message(customer_line_id, TextSendMessage(text=welcome_msg))
                app.logger.info(f"Sent welcome message to customer {customer_line_id}.")
            except Exception as e:
                app.logger.error(f"Failed to send welcome message to {customer_line_id}: {e}")
            return jsonify({"status": "success"})
        else:
            return jsonify({"status": "error", "message": "Failed to update task"}), 500
    else:
        app.logger.info(f"Customer LINE ID {customer_line_id} for task {task_id} already saved.")
        return jsonify({"status": "success", "message": "LINE ID already saved."})


# --- LINE Bot Handlers ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid LINE signature. Aborting request.")
        abort(400)
    except Exception as e:
        app.logger.error(f"Error handling LINE webhook event: {e}", exc_info=True)
        abort(500)
    return 'OK'

def create_task_list_message(title, tasks, limit=5):
    if not tasks:
        return TextSendMessage(text=f"ไม่พบรายการ{title}ในขณะนี้")

    message = f"📋 {title}\n\n"
    tasks.sort(key=lambda x: date_parse(x['due']) if x.get('due') else datetime.datetime.max.replace(tzinfo=pytz.utc))

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
        'งานเสร็จ': lambda: sorted([t for t in (get_google_tasks_for_report(True) or []) if t.get('status') == 'completed'], key=lambda x: date_parse(x['completed']) if x.get('completed') else datetime.datetime.min.replace(tzinfo=pytz.utc), reverse=True),
        'งานวันนี้': lambda: [t for t in (get_google_tasks_for_report(False) or []) if t.get('due') and date_parse(t['due']).astimezone(THAILAND_TZ).date() == datetime.datetime.now(THAILAND_TZ).date() and t.get('status') == 'needsAction'],
        'งานพรุ่งนี้': lambda: [t for t in (get_google_tasks_for_report(False) or []) if t.get('due') and date_parse(t['due']).astimezone(THAILAND_TZ).date() == (datetime.datetime.now(THAILAND_TZ) + datetime.timedelta(days=1)).date() and t.get('status') == 'needsAction'],
    }

    if text in command_handlers:
        tasks = command_handlers[text]()
        title_map = {
            'งานค้าง': 'รายการงานค้าง',
            'งานเสร็จ': 'รายการงานเสร็จ',
            'งานวันนี้': 'งานวันนี้',
            'งานพรุ่งนี้': 'งานพรุ่งนี้',
        }
        reply = create_task_list_message(title_map.get(text, text), tasks)
        line_bot_api.reply_message(event.reply_token, reply)
    elif text == 'สร้างงานใหม่' and LIFF_ID_FORM:
        quick_reply = QuickReply(items=[QuickReplyButton(action=URIAction(label="เปิดฟอร์มสร้างงาน", uri=f"https://liff.line.me/{LIFF_ID_FORM}"))])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="เปิดฟอร์มเพื่อสร้างงานใหม่ครับ 👇", quick_reply=quick_reply))
    elif text.startswith('ดูงาน '):
        name_query = event.message.text.split(maxsplit=1)[1].strip().lower()
        if not name_query:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="โปรดระบุชื่อลูกค้าที่ต้องการค้นหา เช่น 'ดูงาน สมชาย'"))
            return

        all_tasks = get_google_tasks_for_report(show_completed=True) or []
        
        filtered_tasks = [t for t in all_tasks
                          if name_query in parse_customer_info_from_notes(t.get('notes', '')).get('name', '').lower()]
        
        if not filtered_tasks:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"ไม่พบงานของลูกค้า: {name_query}"))
        else:
            filtered_tasks.sort(key=lambda x: (x.get('status') == 'completed', date_parse(x['due']) if x.get('due') else datetime.datetime.max.replace(tzinfo=pytz.utc)))
            
            bubbles = [create_task_flex_message(t) for t in filtered_tasks[:10]]
            if bubbles:
                line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"ผลการค้นหา: {name_query}", contents=CarouselContainer(contents=bubbles)))
            else:
                 line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"ไม่พบงานของลูกค้า: {name_query}"))
    elif text == 'comphone':
        help_text = (
            "พิมพ์คำสั่งเพื่อดูรายงานหรือจัดการงาน:\n"
            "- *งานค้าง*: ดูรายการงานที่ยังไม่เสร็จ\n"
            "- *งานเสร็จ*: ดูรายการงานที่ทำเสร็จแล้ว\n"
            "- *งานวันนี้*: ดูงานที่นัดหมายไว้สำหรับวันนี้\n"
            "- *งานพรุ่งนี้*: ดูงานที่นัดหมายไว้สำหรับพรุ่งนี้\n"
            "- *สร้างงานใหม่*: เปิดฟอร์มสำหรับสร้างงานใหม่ (ผ่าน LIFF)\n"
            "- *ดูงาน [ชื่อลูกค้า]*: ค้นหางานตามชื่อลูกค้า\n\n"
            f"ข้อมูลเพิ่มเติม: {url_for('summary', _external=True)}"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text))
    else:
        # ทำให้บอทเงียบเมื่อไม่เจอคำสั่งที่กำหนด
        pass


@handler.add(PostbackEvent)
def handle_postback(event):
    data_parts = event.postback.data.split('&')
    data = {}
    for item in data_parts:
        if '=' in item:
            key, value = item.split('=', 1)
            data[key] = value
        else:
            data[item] = True

    action = data.get('action')

    if action == 'customer_feedback':
        task_id = data.get('task_id')
        feedback_type = data.get('feedback')
        
        task = get_single_task(task_id)
        if not task:
            app.logger.warning(f"Postback: Task {task_id} not found for feedback.")
            return

        notes = task.get('notes', '')
        feedback_data = parse_customer_feedback_from_notes(notes)
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
            admin_group_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            problem_desc_from_feedback = feedback_data.get('problem_description', 'ไม่ได้ระบุรายละเอียด')
            if admin_group_id:
                notif_text = (
                    f"🚨 ลูกค้าแจ้งปัญหาผ่าน LINE!\n"
                    f"งาน: {task.get('title', 'N/A')}\n"
                    f"ลูกค้า: {customer_info.get('name', 'N/A')}\n"
                    f"การประเมิน: แจ้งปัญหา\n"
                    f"รายละเอียดปัญหา: {problem_desc_from_feedback}\n"
                    f"ดูรายละเอียด: {url_for('task_details', task_id=task_id, _external=True)}"
                )
                try:
                    line_bot_api.push_message(admin_group_id, TextSendMessage(text=notif_text))
                except Exception as e:
                    app.logger.error(f"Failed to send problem notification to admin from postback: {e}")

        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        except Exception as e:
            app.logger.error(f"Failed to reply to postback: {e}")

# Google OAuth2 authorization route
@app.route('/authorize')
def authorize():
    flow = InstalledAppFlow.from_client_secrets_file(
        'credentials.json', SCOPES)
    flow.redirect_uri = url_for('oauth2callback', _external=True)
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true')
    session['oauth_state'] = state
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    state = session.get('oauth_state')
    if not state or state != request.args.get('state'):
        flash('State mismatch error. Your session might be invalid or a CSRF attack was attempted.', 'danger')
        app.logger.error(f"OAuth2 callback state mismatch. Session state: {state}, Request state: {request.args.get('state')}")
        return redirect(url_for('settings_page'))

    flow = InstalledAppFlow.from_client_secrets_file(
        'credentials.json', SCOPES)
    flow.redirect_uri = url_for('oauth2callback', _external=True)

    try:
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials

        token_json_content = creds.to_json()
        
        flash(f'เชื่อมต่อ Google สำเร็จแล้ว! โปรดคัดลอกข้อความด้านล่างนี้ไปใส่ใน Environment Variable ชื่อ GOOGLE_TOKEN_JSON บน Render.com (หรือแพลตฟอร์มอื่นที่คุณใช้) และรีสตาร์ทแอปพลิเคชัน: <textarea class="form-control mt-2" rows="5" readonly>{token_json_content}</textarea>', 'success')
        app.logger.info("Google OAuth successful. Token saved to token.json. Please update GOOGLE_TOKEN_JSON env var.")
        
    except Exception as e:
        app.logger.error(f"Error during OAuth2 callback: {e}", exc_info=True)
        flash(f'เกิดข้อผิดพลาดในการเชื่อมต่อ Google: {e}', 'danger')
    
    session.pop('oauth_state', None)
    return redirect(url_for('settings_page'))


if __name__ == '__main__':
    if not os.path.exists('credentials.json'):
        app.logger.error("credentials.json not found! Google API functions will not work.")
    
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=True)
