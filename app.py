# File: app.py
import os
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
import sys
import datetime
import re
import json
import pytz
import base64
from io import BytesIO
from urllib.parse import quote_plus
import time
import threading
from queue import Queue
from PIL import Image
import pandas as pd
from geopy.distance import geodesic

from dotenv import load_dotenv
load_dotenv()

from flask import (
    Flask, request, jsonify, Response, render_template, redirect, url_for, flash,
    session, make_response
)
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect
from cachetools import TTLCache
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account

from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, TextMessage, FlexMessage,
    PushMessageRequest, ReplyMessageRequest, QuickReply, QuickReplyItem
)
from linebot.v3.messaging.models import URIAction
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent, PostbackEvent, FollowEvent,
    GroupSource, UserSource
)
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError

# Import a single utils module that contains all helper functions
import utils
# Import the blueprint for LIFF views
from liff_views import liff_bp

# --- Sentry Initialization ---
SENTRY_DSN = os.environ.get('SENTRY_DSN')
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FlaskIntegration()],
        traces_sample_rate=1.0,
        profiles_sample_rate=1.0
    )

app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_development_only')
csrf = CSRFProtect(app)

# --- Configuration ---
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
app.jinja_env.filters['dateutil_parse'] = utils.date_parse

# LINE Bot Configuration
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '').strip()

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    sys.exit("LINE Bot credentials are not set in environment variables.")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration)
line_messaging_api = MessagingApi(api_client)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Other Configurations
app.config['LIFF_ID_FORM'] = os.environ.get('LIFF_ID_FORM')
app.config['LIFF_ID_TECHNICIAN_LOCATION'] = os.environ.get('LIFF_ID_TECHNICIAN_LOCATION')
app.config['GOOGLE_TASKS_LIST_ID'] = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
app.config['GOOGLE_DRIVE_FOLDER_ID'] = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')
LINE_RATE_LIMIT_PER_MINUTE = int(os.environ.get('LINE_RATE_LIMIT_PER_MINUTE', 100))
app.config['SCOPES'] = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/calendar.events', 'https://www.googleapis.com/auth/drive.file']
app.config['THAILAND_TZ'] = pytz.timezone('Asia/Bangkok')
app.config['cache'] = TTLCache(maxsize=100, ttl=60)
scheduler = BackgroundScheduler(daemon=True, timezone=app.config['THAILAND_TZ'])

# Text snippets for forms
app.config['TEXT_SNIPPETS'] = {
    'task_details': [
        {'key': 'ล้างแอร์', 'value': 'ล้างทำความสะอาดเครื่องปรับอากาศ, ตรวจเช็คน้ำยา, วัดแรงดันไฟฟ้า และทำความสะอาดคอยล์ร้อน-เย็น'},
        {'key': 'ติดตั้งแอร์', 'value': 'ติดตั้งเครื่องปรับอากาศใหม่ ขนาด [ขนาด BTU] พร้อมเดินท่อน้ำยาและสายไฟ, ติดตั้งเบรกเกอร์'},
    ],
    'progress_reports': [
        {'key': 'ลูกค้าเลื่อนนัด', 'value': 'ลูกค้าขอเลื่อนนัดเป็นวันที่ [dd/mm/yyyy] เนื่องจากไม่สะดวก'},
        {'key': 'รออะไหล่', 'value': 'ตรวจสอบแล้วพบว่าต้องรออะไหล่ [ชื่ออะไหล่] จะแจ้งลูกค้าให้ทราบกำหนดการอีกครั้ง'},
    ]
}

# --- Helper and Utility Functions (App-specific) ---

class LineMessageQueue:
    def __init__(self, max_per_minute=100):
        self.queue = Queue()
        self.max_per_minute = max_per_minute
        self.sent_count = 0
        self.last_reset = time.time()
        self.processing_lock = threading.Lock()

    def add_message(self, user_id, messages):
        if not isinstance(messages, list):
            messages = [messages]
        self.queue.put((user_id, messages, time.time()))

    def process_queue(self):
        while True:
            with self.processing_lock:
                if time.time() - self.last_reset >= 60:
                    self.sent_count = 0
                    self.last_reset = time.time()
                if self.sent_count >= self.max_per_minute:
                    sleep_time = 60 - (time.time() - self.last_reset)
                    time.sleep(sleep_time)
                    continue
                if not self.queue.empty():
                    user_id, messages, timestamp = self.queue.get()
                    if time.time() - timestamp > 300: continue
                    try:
                        push_message_request = PushMessageRequest(to=user_id, messages=messages)
                        line_messaging_api.push_message(push_message_request)
                        self.sent_count += 1
                    except Exception as e:
                        app.logger.error(f"LINE API Error sending message to {user_id}: {e}")
            time.sleep(1)

message_queue = LineMessageQueue(max_per_minute=LINE_RATE_LIMIT_PER_MINUTE)
threading.Thread(target=message_queue.process_queue, daemon=True).start()

SETTINGS_FILE = 'settings.json'
LOCATIONS_FILE = 'technician_locations.json'

_DEFAULT_APP_SETTINGS_STORE = {
    # Default settings structure
}

def load_settings_from_file():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
        except (json.JSONDecodeError, IOError): pass
    return None

def save_settings_to_file(settings_data):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f: json.dump(settings_data, f, ensure_ascii=False, indent=4)
        return True
    except IOError: return False

def get_app_settings():
    app_settings = json.loads(json.dumps(_DEFAULT_APP_SETTINGS_STORE))
    loaded_settings = load_settings_from_file()
    if loaded_settings:
        for key, default_value in app_settings.items():
            if key in loaded_settings:
                if isinstance(default_value, dict) and isinstance(loaded_settings[key], dict):
                    app_settings[key].update(loaded_settings[key])
                else:
                    app_settings[key] = loaded_settings[key]
    else:
        save_settings_to_file(app_settings)
    return app_settings

def save_app_settings(settings_data):
    current_settings = get_app_settings()
    for key, value in settings_data.items():
        if isinstance(value, dict) and key in current_settings and isinstance(current_settings[key], dict):
            current_settings[key].update(value)
        else:
            current_settings[key] = value
    return save_settings_to_file(current_settings)

def _execute_google_api_call_with_retry(api_call, *args, **kwargs):
    max_retries = 3
    for i in range(max_retries):
        try:
            return api_call(*args, **kwargs).execute()
        except HttpError as e:
            if e.resp.status in [500, 503, 429] and i < max_retries - 1:
                time.sleep((2 ** i))
            else:
                raise
    return None

def get_google_service(api_name, api_version):
    creds = None
    # ... (Authentication logic remains the same)
    if os.environ.get('GOOGLE_TOKEN_JSON'):
        try:
            creds = Credentials.from_authorized_user_info(json.loads(os.environ.get('GOOGLE_TOKEN_JSON')), app.config['SCOPES'])
            if creds and not creds.valid and creds.refresh_token:
                creds.refresh(Request())
        except Exception:
            creds = None
    if creds and creds.valid:
        return build(api_name, api_version, credentials=creds)
    return None

def generate_qr_code_base64(data):
    # ... (QR code generation logic remains the same)
    pass

@app.context_processor
def inject_global_vars():
    def check_google_api_status():
        service = get_google_service('drive', 'v3')
        if not service: return False
        try:
            _execute_google_api_call_with_retry(service.about().get, fields='user')
            return True
        except Exception:
            return False
    return {
        'now': datetime.datetime.now(app.config['THAILAND_TZ']),
        'google_api_connected': check_google_api_status()
    }

# --- Pass Functions and Config to Blueprint/Utils via app.config ---
app.config['get_app_settings'] = get_app_settings
app.config['get_google_tasks_service'] = lambda: get_google_service('tasks', 'v1')
app.config['get_google_drive_service'] = lambda: get_google_service('drive', 'v3')
app.config['_execute_google_api_call_with_retry'] = _execute_google_api_call_with_retry
app.config['generate_qr_code_base64'] = generate_qr_code_base64

# --- Register Blueprints ---
app.register_blueprint(liff_bp, url_prefix='/')

# --- Core API & Admin Routes ---

@app.route('/api/tasks/create', methods=['POST'])
def api_create_task():
    try:
        # Correctly calling the moved function from utils
        new_task = utils.create_google_task(
            title=request.form.get('task_title'),
            # ... construct notes and due date as before ...
        )
        if new_task:
            app.config['cache'].clear()
            # ... notification logic ...
            return jsonify({'status': 'success', 'redirect_url': url_for('liff.task_details', task_id=new_task['id'])})
        return jsonify({'status': 'error', 'message': 'Failed to create task'}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ... (All other /api/... and /admin/... routes remain here)

@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        # ... logic to save settings from form ...
        save_app_settings(request.form.to_dict(flat=False))
        run_scheduler() # Re-run scheduler with new settings
        flash('Settings saved successfully!', 'success')
        return redirect(url_for('settings_page'))
    return render_template('settings_page.html', settings=get_app_settings())

# ✅ ADDED: Missing route for customer problem form submission
@app.route('/submit_customer_problem', methods=['POST'])
def submit_customer_problem():
    try:
        data = request.json
        task_id = data.get('task_id')
        problem_desc = data.get('problem_description')
        user_id = data.get('customer_line_user_id')

        if not task_id or not problem_desc:
            return jsonify({'status': 'error', 'message': 'Missing data'}), 400

        task = utils.get_single_task(task_id)
        if not task:
            return jsonify({'status': 'error', 'message': 'Task not found'}), 404

        # Update task notes with feedback
        history, base_notes = utils.parse_tech_report_from_notes(task.get('notes', ''))
        feedback = utils.parse_customer_feedback_from_notes(task.get('notes', ''))
        feedback['problem_report'] = {
            'date': datetime.datetime.now(app.config['THAILAND_TZ']).isoformat(),
            'description': problem_desc
        }
        if user_id:
            feedback['customer_line_user_id'] = user_id
        
        # Reconstruct notes
        reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
        final_notes = f"{base_notes}{reports_text}\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

        utils.update_google_task(task_id, notes=final_notes)
        app.config['cache'].clear()

        # Notify admin
        admin_group = get_app_settings().get('line_recipients', {}).get('admin_group_id')
        if admin_group:
            customer_info = utils.parse_customer_info_from_notes(base_notes)
            message_text = f"🚨 ลูกค้าแจ้งปัญหา!\n\nงาน: {task.get('title')}\nลูกค้า: {customer_info.get('name')}\nปัญหา: {problem_desc}\n\n🔗 ดูรายละเอียดงาน:\n{url_for('liff.task_details', task_id=task_id, _external=True)}"
            message_queue.add_message(admin_group, [TextMessage(text=message_text)])

        return jsonify({'status': 'success'})
    except Exception as e:
        app.logger.error(f"Error in submit_customer_problem: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Internal server error'}), 500

# --- LINE Webhook Handlers ---
@app.route("/callback", methods=['POST'])
@csrf.exempt
def callback():
    # ... (Webhook callback logic remains the same)
    pass

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    # ... (Webhook handler logic now calls utils. functions)
    # Example correction:
    # tasks = [t for t in (utils.get_google_tasks_for_report(False) or []) if ...]
    pass

# ... (Other handlers remain here)

# --- Scheduler Setup ---
def run_scheduler():
    # ... (Scheduler setup logic remains the same)
    pass

def cleanup_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)

atexit.register(cleanup_scheduler)

# --- App Initialization ---
if __name__ == '__main__':
    with app.app_context():
        # utils.load_settings_from_drive_on_startup() # This should be moved to utils
        run_scheduler()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=True)