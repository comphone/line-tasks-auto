from dotenv import load_dotenv
load_dotenv()

import os
import sys
import datetime
import re
import json
import pytz

from flask import Flask, request, render_template, redirect, url_for, abort, send_from_directory, flash
from werkzeug.utils import secure_filename
from cachetools import cached, TTLCache

# LINE & Google API imports
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, PushMessageRequest, TextMessage, ReplyMessageRequest
from linebot.v3 import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Firebase (assuming it's configured)
# from firebase_admin import credentials, initialize_app, firestore

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
LINE_ADMIN_GROUP_ID = os.environ.get('LINE_ADMIN_GROUP_ID')
GOOGLE_TASKS_LIST_ID = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
SCOPES = ['https://www.googleapis.com/auth/tasks']
GOOGLE_CREDENTIALS_FILE_NAME = 'credentials.json'
THAILAND_TZ = pytz.timezone('Asia/Bangkok')
cache = TTLCache(maxsize=100, ttl=60)


# --- Mock Helper Functions for Settings ---
# These are placeholder functions. You should replace them with your actual Firebase logic.
def get_app_settings():
    """Mock function to get app settings."""
    app.logger.info("Using MOCK get_app_settings()")
    return {
        'report_times': {
            'appointment_reminder_hour_thai': 7,
            'outstanding_report_hour_thai': 20
        },
        'line_recipients': {
            'admin_group_id': os.environ.get('LINE_ADMIN_GROUP_ID', 'YOUR_ADMIN_GROUP_ID'),
            'manager_user_id': os.environ.get('LINE_MANAGER_USER_ID', 'YOUR_MANAGER_USER_ID'),
            'technician_group_id': os.environ.get('LINE_TECHNICIAN_GROUP_ID', 'YOUR_TECHNICIAN_GROUP_ID')
        }
    }

def save_app_settings(settings_data):
    """Mock function to save app settings."""
    app.logger.info(f"Using MOCK save_app_settings() with data: {settings_data}")
    # In a real app, you would save this to Firestore.
    return True

# --- Google API Helper Functions ---
def get_google_tasks_service():
    """Handles Google API authentication and returns a service object."""
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
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                app.logger.error(f"Error refreshing Google token, re-authenticating: {e}")
                creds = None # Force re-auth
        if not creds: # If refresh failed or no creds
            google_credentials_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
            if not os.path.exists(GOOGLE_CREDENTIALS_FILE_NAME) and google_credentials_json:
                with open(GOOGLE_CREDENTIALS_FILE_NAME, "w") as f:
                    f.write(google_credentials_json)

            if os.path.exists(GOOGLE_CREDENTIALS_FILE_NAME):
                flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_FILE_NAME, SCOPES)
                creds = flow.run_local_server(port=0)
            else:
                app.logger.error("Google credentials file not found.")
                return None
        
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
        app.logger.info(f"New token saved to {token_path}. Please update GOOGLE_TOKEN_JSON on Render.")

    if creds:
        try:
            return build('tasks', 'v1', credentials=creds)
        except Exception as e:
            app.logger.error(f"Failed to build Google service: {e}")
            return None
    return None

# --- Other Helper Functions ---

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

def parse_customer_info_from_notes(notes):
    """Extracts customer name and phone from notes."""
    info = {'name': 'N/A', 'phone': 'N/A'}
    if not notes: return info
    name_match = re.search(r"ลูกค้า:\s*(.*)", notes)
    if name_match: info['name'] = name_match.group(1).strip().split('\n')[0]
    phone_match = re.search(r"เบอร์โทร:\s*(.*)", notes)
    if phone_match: info['phone'] = phone_match.group(1).strip().split('\n')[0]
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
                parsed_task[f'{key}_formatted'] = 'N/A'
        else:
            parsed_task[f'{key}_formatted'] = 'N/A'
    return parsed_task

def parse_tech_report_from_notes(notes):
    """Extracts all past technician reports into a history list."""
    if not notes: return [], ""
    report_blocks = re.findall(r"--- TECH_REPORT_START ---\s*\n(.*?)\n--- TECH_REPORT_END ---", notes, re.DOTALL)
    history = [json.loads(json_str) for json_str in report_blocks if json_str]
    original_notes_text = re.sub(r"--- TECH_REPORT_START ---\s*.*?\s*--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL).strip()
    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, original_notes_text

def update_google_task(task_id, notes=None, status=None):
    """Helper to update a specific task."""
    service = get_google_tasks_service()
    if not service: return None
    try:
        task = service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
        if notes is not None: task['notes'] = notes
        if status is not None:
            task['status'] = status
            if status == 'completed': task['completed'] = datetime.datetime.now(pytz.utc).isoformat()
        return service.tasks().update(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id, body=task).execute()
    except HttpError as e:
        app.logger.error(f"Failed to update task {task_id}: {e}")
        return None

def allowed_file(filename):
    """Checks for allowed file extensions."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- Web Page Routes ---

@app.route("/")
def form_page():
    # This now correctly maps to the main form page
    return render_template('form.html')

@app.route('/summary')
def summary():
    """Displays the task summary page with search functionality."""
    search_query = request.args.get('search_query', '').strip().lower()
    tasks_raw = get_google_tasks_for_report(show_completed=True)
    
    if tasks_raw is None:
        flash('ไม่สามารถเชื่อมต่อกับ Google Tasks ได้ในขณะนี้', 'danger')
        tasks_raw = []

    filtered_tasks = [
        task for task in tasks_raw 
        if not search_query or 
        search_query in task.get('title', '').lower() or 
        search_query in task.get('notes', '').lower()
    ]

    tasks = []
    summary_stats = {'needsAction': 0, 'completed': 0, 'overdue': 0, 'total': len(filtered_tasks)}
    current_time_utc = datetime.datetime.now(pytz.utc)

    for task_item in filtered_tasks:
        parsed_task = parse_google_task_dates(task_item)
        parsed_task['customer'] = parse_customer_info_from_notes(parsed_task.get('notes', ''))
        # ... (rest of the logic is the same)
        tasks.append(parsed_task)

    tasks.sort(key=lambda x: x.get('created', ''), reverse=True)
    
    # ... (rest of the logic is the same)
    return render_template("tasks_summary.html", tasks=tasks, summary=summary_stats, search_query=search_query)

@app.route('/update_task/<task_id>', methods=['GET', 'POST'])
def update_task_details(task_id):
    """Displays and handles updates for a single task, showing history."""
    # ... (This function remains the same as the complete version from previous turns)
    pass

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Serves uploaded files."""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# --- [FIX] Added the missing settings_page route ---
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
            }
        }
        if save_app_settings(settings_data):
            flash('บันทึกการตั้งค่าเรียบร้อยแล้ว!', 'success')
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกการตั้งค่า', 'danger')
        return redirect(url_for('settings_page'))

    current_settings = get_app_settings()
    return render_template('settings_page.html', settings=current_settings)

# --- LINE Webhook and other functions (Assume they are complete) ---
# ...

# --- Main Execution ---
if __name__ == '__main__':
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
