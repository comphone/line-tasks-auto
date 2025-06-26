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
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    sys.exit("LINE Bot credentials are not set in environment variables.")

LINE_ADMIN_GROUP_ID = os.environ.get('LINE_ADMIN_GROUP_ID')
GOOGLE_TASKS_LIST_ID = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
SCOPES = ['https://www.googleapis.com/auth/tasks']
GOOGLE_CREDENTIALS_FILE_NAME = 'credentials.json'
THAILAND_TZ = pytz.timezone('Asia/Bangkok')
cache = TTLCache(maxsize=100, ttl=60)

# Initialize LINE Bot SDK
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_api_client = ApiClient(configuration)
line_messaging_api = MessagingApi(line_api_client)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


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
                creds = None
        if not creds:
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

@app.route("/", methods=['GET', 'POST'])
def form_page():
    if request.method == 'POST':
        # Logic to handle form submission
        # ...
        return redirect(url_for('summary'))
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
        
        history, original_notes = parse_tech_report_from_notes(parsed_task.get('notes', ''))
        parsed_task['tech_reports_history'] = history
        parsed_task['notes_display'] = original_notes
        
        status = task_item.get('status')
        if status == 'completed':
            summary_stats['completed'] += 1
            parsed_task['display_status'] = 'เสร็จสิ้น'
        elif status == 'needsAction':
            summary_stats['needsAction'] += 1
            is_overdue = False
            if 'due' in task_item and task_item.get('due'):
                try:
                    due_dt_utc = datetime.datetime.fromisoformat(task_item['due'].replace('Z', '+00:00'))
                    if due_dt_utc < current_time_utc:
                        is_overdue = True
                        summary_stats['overdue'] += 1
                except (ValueError, TypeError):
                    pass
            parsed_task['display_status'] = 'ค้างชำระ' if is_overdue else 'รอดำเนินการ'

        tasks.append(parsed_task)

    tasks.sort(key=lambda x: x.get('created', ''), reverse=True)
    
    total = summary_stats['total']
    if total > 0:
        summary_stats['completed_percent'] = round((summary_stats['completed'] / total) * 100, 1)
        summary_stats['needsAction_percent'] = round((summary_stats['needsAction'] / total) * 100, 1)
        summary_stats['overdue_percent'] = round((summary_stats['overdue'] / total) * 100, 1)
    else:
        summary_stats.update({'completed_percent': 0, 'needsAction_percent': 0, 'overdue_percent': 0})

    return render_template("tasks_summary.html", tasks=tasks, summary=summary_stats, search_query=search_query)

@app.route('/update_task/<task_id>', methods=['GET', 'POST'])
def update_task_details(task_id):
    """Displays and handles updates for a single task, showing history."""
    service = get_google_tasks_service()
    if not service:
        abort(503, "Google Tasks service is unavailable.")

    try:
        task_raw = service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
        task = parse_google_task_dates(task_raw)
        
        history, original_notes = parse_tech_report_from_notes(task.get('notes', ''))
        task['tech_reports_history'] = history
        task['notes_display'] = original_notes
        
        if history and 'next_appointment' in history[0] and history[0]['next_appointment']:
            try:
                next_app_dt_utc = datetime.datetime.fromisoformat(history[0]['next_appointment'].replace('Z', '+00:00'))
                task['tech_next_appointment_datetime_local'] = next_app_dt_utc.astimezone(THAILAND_TZ).strftime("%Y-%m-%dT%H:%M")
            except (ValueError, TypeError):
                task['tech_next_appointment_datetime_local'] = ''
        else:
            task['tech_next_appointment_datetime_local'] = ''
    except HttpError:
        abort(404, "Task not found.")

    if request.method == 'POST':
        # ... (Form processing logic from previous turn)
        pass

    return render_template('update_task_details.html', task=task)
    
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Serves uploaded files."""
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
            }
        }
        if save_app_settings(settings_data):
            flash('บันทึกการตั้งค่าเรียบร้อยแล้ว!', 'success')
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกการตั้งค่า', 'danger')
        return redirect(url_for('settings_page'))

    current_settings = get_app_settings()
    return render_template('settings_page.html', settings=current_settings)

# --- LINE Webhook and Command Handlers ---

@app.route("/callback", methods=['POST'])
def callback():
    """Endpoint for receiving data from LINE Webhook"""
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

def reply_to_line(reply_token, text_message):
    """Central function for sending reply messages."""
    try:
        line_messaging_api.reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text_message)])
        )
    except Exception as e:
        app.logger.error(f"Failed to reply to LINE: {e}")

def handle_help_command(event):
    """Handles the 'comphone' command."""
    reply_message = (
        "🤖 **วิธีใช้งานบอท** 🤖\n\n"
        "พิมพ์คำสั่งต่อไปนี้เพื่อสั่งงาน:\n\n"
        "➡️ `งานค้าง`\nดูรายการงานที่ยังไม่เสร็จ\n\n"
        "➡️ `งานเสร็จ`\nดูรายการงานที่เสร็จแล้ว 5 งานล่าสุด\n\n"
        "➡️ `สรุปรายงาน`\nดูภาพรวมจำนวนงาน\n\n"
        "➡️ `ดูงาน <ID>`\nดูรายละเอียดของงานตาม ID\n\n"
        "➡️ `เสร็จงาน <ID>`\nปิดงานด่วนจาก LINE"
    )
    reply_to_line(event.reply_token, reply_message)

def handle_outstanding_tasks_command(event):
    """Handles 'งานค้าง' command."""
    tasks = get_google_tasks_for_report(show_completed=False)
    if tasks is None:
        return reply_to_line(event.reply_token, "⚠️ เกิดข้อผิดพลาดในการดึงข้อมูลงาน")
    if not tasks:
        return reply_to_line(event.reply_token, "✅ ยอดเยี่ยม! ไม่มีงานค้างในขณะนี้")
        
    message_lines = ["--- 📋 รายการงานค้าง ---"]
    tasks.sort(key=lambda x: x.get('due', '9999-99-99'))
    for i, task in enumerate(tasks[:15]): # Limit to 15 to avoid long messages
        parsed_task = parse_google_task_dates(task)
        message_lines.append(f"{i+1}. {task.get('title', 'N/A')}\n(ID: {task.get('id')})")
    reply_to_line(event.reply_token, "\n\n".join(message_lines))


def handle_completed_tasks_command(event):
    """Handles 'งานเสร็จ' command."""
    tasks = get_google_tasks_for_report(show_completed=True)
    if tasks is None:
        return reply_to_line(event.reply_token, "⚠️ เกิดข้อผิดพลาดในการดึงข้อมูลงาน")
    
    completed_tasks = [t for t in tasks if t.get('status') == 'completed']
    if not completed_tasks:
        return reply_to_line(event.reply_token, "ยังไม่มีงานที่ทำเสร็จ")

    message_lines = ["--- ✅ 5 รายการงานที่เสร็จล่าสุด ---"]
    completed_tasks.sort(key=lambda x: x.get('completed', ''), reverse=True)
    for i, task in enumerate(completed_tasks[:5]):
        message_lines.append(f"{i+1}. {task.get('title', 'N/A')}")
    reply_to_line(event.reply_token, "\n".join(message_lines))


def handle_summary_command(event):
    """Handles 'สรุปรายงาน' command."""
    tasks = get_google_tasks_for_report(show_completed=True)
    if tasks is None:
        return reply_to_line(event.reply_token, "⚠️ เกิดข้อผิดพลาดในการดึงข้อมูลงาน")

    stats = {'needsAction': 0, 'completed': 0, 'overdue': 0}
    current_time_utc = datetime.datetime.now(pytz.utc)
    for task in tasks:
        if task.get('status') == 'completed':
            stats['completed'] += 1
        elif task.get('status') == 'needsAction':
            stats['needsAction'] += 1
            if task.get('due'):
                try:
                    due_dt_utc = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                    if due_dt_utc < current_time_utc:
                        stats['overdue'] += 1
                except (ValueError, TypeError): pass
    
    reply_message = (
        f"--- 📊 สรุปรายงาน ---\n"
        f"งานทั้งหมด: {len(tasks)}\n"
        f"✅ เสร็จสิ้น: {stats['completed']}\n"
        f"⏳ รอดำเนินการ: {stats['needsAction']}\n"
        f"❗️ ค้างชำระ: {stats['overdue']}"
    )
    reply_to_line(event.reply_token, reply_message)

def handle_view_task_command(event, task_id):
    """Handles 'ดูงาน <ID>' command."""
    task = get_single_task(task_id)
    if not task:
        return reply_to_line(event.reply_token, f"ไม่พบงาน ID: {task_id}")
        
    parsed = parse_google_task_dates(task)
    customer = parse_customer_info_from_notes(task.get('notes', ''))
    update_url = url_for('update_task_details', task_id=task.get('id'), _external=True)

    details = [
        f"🔍 รายละเอียดงาน: {task.get('title', 'N/A')}",
        f"สถานะ: {'เสร็จสิ้น' if task.get('status') == 'completed' else 'รอดำเนินการ'}",
        f"ลูกค้า: {customer.get('name', 'N/A')}",
        f"โทร: {customer.get('phone', 'N/A')}",
        f"กำหนดส่ง: {parsed.get('due_formatted', 'N/A')}",
        f"\n👉 แก้ไข/อัปเดตงาน:\n{update_url}"
    ]
    reply_to_line(event.reply_token, "\n".join(details))


def handle_complete_task_command(event, task_id):
    """Handles 'เสร็จงาน <ID>' command."""
    updated_task = update_google_task(task_id, status='completed')
    if updated_task:
        reply_to_line(event.reply_token, f"✅ ปิดงาน '{updated_task.get('title')}' เรียบร้อยแล้ว")
    else:
        reply_to_line(event.reply_token, f"❌ ไม่สามารถปิดงาน ID: {task_id} ได้")

# Command Dispatcher Dictionary
COMMANDS = {
    'comphone': handle_help_command,
    'งานค้าง': handle_outstanding_tasks_command,
    'งานเสร็จ': handle_completed_tasks_command,
    'สรุปรายงาน': handle_summary_command,
}

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """Handles incoming messages and calls the correct function."""
    text = event.message.text.strip()
    text_lower = text.lower()

    if text_lower.startswith('ดูงาน '):
        parts = text.split()
        if len(parts) > 1: handle_view_task_command(event, parts[1])
        return

    if text_lower.startswith('เสร็จงาน '):
        parts = text.split()
        if len(parts) > 1: handle_complete_task_command(event, parts[1])
        return

    if text_lower in COMMANDS:
        COMMANDS[text_lower](event)

# --- Main Execution ---
if __name__ == '__main__':
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)

