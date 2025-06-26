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


# --- Helper Functions ---

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
        else:
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

@cached(cache)
def get_google_tasks_for_report(show_completed=True):
    """Fetches tasks from Google Tasks API, with caching."""
    app.logger.info(f"Cache miss or expired. Calling Google Tasks API... (show_completed={show_completed})")
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
    if name_match:
        info['name'] = name_match.group(1).strip().split('\n')[0]
        
    phone_match = re.search(r"เบอร์โทร:\s*(.*)", notes)
    if phone_match:
        info['phone'] = phone_match.group(1).strip().split('\n')[0]
        
    return info
    
def parse_google_task_dates(task_item):
    """Formats dates from a Google Task item."""
    parsed_task = task_item.copy()
    for key in ['created', 'due', 'completed']:
        if key in parsed_task and parsed_task[key]:
            try:
                dt_utc = datetime.datetime.fromisoformat(parsed_task[key].replace('Z', '+00:00'))
                dt_thai = dt_utc.astimezone(THAILAND_TZ)
                if key == 'due':
                    parsed_task[f'{key}_formatted'] = dt_thai.strftime("%d/%m/%y %H:%M")
                else:
                    parsed_task[f'{key}_formatted'] = dt_thai.strftime("%d/%m/%y %H:%M:%S")
            except (ValueError, TypeError):
                parsed_task[f'{key}_formatted'] = 'N/A'
        else:
            parsed_task[f'{key}_formatted'] = 'N/A'
    return parsed_task

def parse_tech_report_from_notes(notes):
    """Extracts all past technician reports into a history list."""
    if not notes:
        return [], ""
    report_blocks = re.findall(r"--- TECH_REPORT_START ---\s*\n(.*?)\n--- TECH_REPORT_END ---", notes, re.DOTALL)
    history = []
    for json_str in report_blocks:
        try:
            history.append(json.loads(json_str))
        except json.JSONDecodeError:
            continue
    original_notes_text = re.sub(r"--- TECH_REPORT_START ---\s*.*?\s*--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL).strip()
    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, original_notes_text

def update_google_task(task_id, notes=None, status=None):
    """Helper to update a specific task."""
    service = get_google_tasks_service()
    if not service:
        return None
    try:
        task = service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
        if notes is not None:
            task['notes'] = notes
        if status is not None:
            task['status'] = status
            if status == 'completed':
                task['completed'] = datetime.datetime.now(pytz.utc).isoformat()
        
        return service.tasks().update(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id, body=task).execute()
    except HttpError as e:
        app.logger.error(f"Failed to update task {task_id}: {e}")
        return None

def allowed_file(filename):
    """Checks for allowed file extensions."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- Web Page Routes ---

@app.route("/")
def form_page():
    return render_template('form.html')

@app.route('/summary')
def summary():
    """Displays the task summary page with search functionality."""
    search_query = request.args.get('search_query', '').strip().lower()
    tasks_raw = get_google_tasks_for_report(show_completed=True)
    
    if tasks_raw is None:
        flash('ไม่สามารถเชื่อมต่อกับ Google Tasks ได้ในขณะนี้', 'danger')
        tasks_raw = []

    filtered_tasks = []
    if search_query:
        for task in tasks_raw:
            notes = task.get('notes', '').lower()
            title = task.get('title', '').lower()
            customer_info = parse_customer_info_from_notes(notes)
            if (search_query in title or
                search_query in notes or
                search_query in customer_info.get('name', '').lower() or
                search_query in customer_info.get('phone', '')):
                filtered_tasks.append(task)
    else:
        filtered_tasks = tasks_raw

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
        
        # Get next appointment from the latest report in history
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
        work_summary = request.form.get('work_summary', '').strip()
        equipment_used = request.form.get('equipment_used', '').strip()
        time_taken = request.form.get('time_taken', '').strip()
        new_status = request.form.get('status', task.get('status'))
        next_appointment_date_str = request.form.get('next_appointment_date', '').strip()

        # Combine all attachment URLs from history and new uploads
        all_attachment_urls = []
        for report in task.get('tech_reports_history', []):
            all_attachment_urls.extend(report.get('attachment_urls', []))
        
        if 'files[]' in request.files:
            files = request.files.getlist('files[]')
            for file in files:
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                    all_attachment_urls.append(url_for('uploaded_file', filename=filename, _external=True))
        
        all_attachment_urls = list(set(all_attachment_urls)) # Remove duplicates

        next_appointment_gmt = None
        if new_status == 'needsAction' and next_appointment_date_str:
            try:
                next_app_dt_local = THAILAND_TZ.localize(datetime.datetime.fromisoformat(next_appointment_date_str))
                next_appointment_gmt = next_app_dt_local.astimezone(pytz.utc).isoformat()
            except ValueError:
                app.logger.error(f"Invalid next appointment date format: {next_appointment_date_str}")
        
        new_tech_report_data = {
            'summary_date': datetime.datetime.now(THAILAND_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            'work_summary': work_summary,
            'equipment_used': equipment_used,
            'time_taken': time_taken,
            'next_appointment': next_appointment_gmt,
            'attachment_urls': all_attachment_urls # Save all URLs in the latest report
        }
        
        _, original_notes_text = parse_tech_report_from_notes(task.get('notes', ''))
        
        all_reports = task.get('tech_reports_history', []) + [new_tech_report_data]
        
        all_reports_text = ""
        for report in sorted(all_reports, key=lambda x: x.get('summary_date', '9999-99-99')):
            report_json_str = json.dumps(report, ensure_ascii=False, indent=2)
            all_reports_text += f"\n\n--- TECH_REPORT_START ---\n{report_json_str}\n--- TECH_REPORT_END ---"

        updated_notes = original_notes_text + all_reports_text
        
        if update_google_task(task_id, notes=updated_notes, status=new_status):
             flash('อัปเดตงานเรียบร้อยแล้ว!', 'success')
        else:
            flash('เกิดข้อผิดพลาดในการอัปเดตงาน', 'danger')

        return redirect(url_for('summary'))

    return render_template('update_task_details.html', task=task)
    
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Serves uploaded files."""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# --- LINE Webhook and Command Handlers ---
# (This section is assumed to be complete and correct from previous turns)
# Includes /callback route and all handle_... functions for LINE commands

# --- Main Execution ---
if __name__ == '__main__':
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
