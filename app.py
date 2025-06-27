# app.py

import os
import sys
import datetime
import re
import json
import pytz
import mimetypes

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, render_template, redirect, url_for, abort, send_from_directory, flash, jsonify, current_app
from werkzeug.utils import secure_filename
from cachetools import cached, TTLCache
from geopy.distance import geodesic
from flask_login import LoginManager, current_user # Import LoginManager, current_user

# Import db and Models
from models import db, User, Customer 

# Corrected LINE Bot SDK imports for Flex Messages
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, PushMessageRequest, TextMessage, ReplyMessageRequest, FlexMessage
)
from linebot.models import (
    BubbleContainer, CarouselContainer, BoxComponent, TextComponent,
    ButtonComponent, SeparatorComponent, URIAction, PostbackAction, QuickReply, QuickReplyButton
)
from linebot.v3 import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent, ImageMessageContent, FileMessageContent, GroupSource, UserSource 
from linebot.v3.exceptions import InvalidSignatureError

from google.auth.transport.requests import Request as GoogleAuthRequest # Rename to avoid conflict with Flask request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# --- Initialize Flask App (as a function for factory pattern) ---
def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_dev')

    # --- Configuration ---
    app.config['UPLOAD_FOLDER'] = 'static/uploads'
    app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'}
    
    # Database Configuration (for SQLite or PostgreSQL)
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///site.db').replace("postgres://", "postgresql://", 1)
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Initialize SQLAlchemy with the app
    db.init_app(app)

    # Initialize Flask-Login
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login_line' # Specify the login view blueprint.route

    @login_manager.user_loader
    def load_user(user_id):
        # This function tells Flask-Login how to load a user from the ID
        return User.query.get(user_id)

    # Create upload folder if it doesn't exist
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])

    # --- LINE & Google Configs (Moved to app.config for better access) ---
    app.config['LINE_CHANNEL_ACCESS_TOKEN'] = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
    app.config['LINE_CHANNEL_SECRET'] = os.environ.get('LINE_CHANNEL_SECRET')
    app.config['LIFF_ID_FORM'] = os.environ.get('LIFF_ID_FORM')
    # LINE Login specific configs
    app.config['LINE_LOGIN_CHANNEL_ID'] = os.environ.get('LINE_LOGIN_CHANNEL_ID') # New: LINE Login Channel ID
    app.config['LINE_LOGIN_CHANNEL_SECRET'] = os.environ.get('LINE_LOGIN_CHANNEL_SECRET') # New: LINE Login Channel Secret
    app.config['LINE_LOGIN_REDIRECT_URI'] = os.environ.get('LINE_LOGIN_REDIRECT_URI', 'https://line-tasks-auto.onrender.com/callback_line') # New: Callback URL for LINE Login

    app.config['LINE_ADMIN_GROUP_ID'] = os.environ.get('LINE_ADMIN_GROUP_ID')
    app.config['LINE_HR_GROUP_ID'] = os.environ.get('LINE_HR_GROUP_ID')
    app.config['GOOGLE_TASKS_LIST_ID'] = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
    app.config['GOOGLE_DRIVE_FOLDER_ID'] = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')

    if not all([app.config['LINE_CHANNEL_ACCESS_TOKEN'], app.config['LINE_CHANNEL_SECRET'], app.config['LINE_LOGIN_CHANNEL_ID'], app.config['LINE_LOGIN_CHANNEL_SECRET']]):
        sys.exit("LINE Bot and/or LINE Login credentials are not set in environment variables.")

    if not app.config['GOOGLE_DRIVE_FOLDER_ID']:
        app.logger.warning("GOOGLE_DRIVE_FOLDER_ID environment variable is not set. Drive upload will not work.")

    app.config['SCOPES'] = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/drive', 'openid', 'profile', 'email'] # Added openid, profile, email for LINE Login
    app.config['GOOGLE_CREDENTIALS_FILE_NAME'] = 'credentials.json'
    app.config['THAILAND_TZ'] = pytz.timezone('Asia/Bangkok')
    
    # Global cache for Google API calls
    app.cache = TTLCache(maxsize=100, ttl=60)

    # Initialize LINE Bot SDK (needs to be available globally or passed)
    app.line_configuration = Configuration(access_token=app.config['LINE_CHANNEL_ACCESS_TOKEN'])
    app.line_api_client = ApiClient(app.line_configuration)
    app.line_messaging_api = MessagingApi(app.line_api_client)
    app.line_webhook_handler = WebhookHandler(app.config['LINE_CHANNEL_SECRET'])

    # --- Register Blueprints ---
    from routes.web import web_bp
    from routes.line_bot import line_bot_bp
    from routes.auth import auth_bp # New: Import auth blueprint

    app.register_blueprint(web_bp)
    app.register_blueprint(line_bot_bp)
    app.register_blueprint(auth_bp) # Register auth blueprint

    # --- Helper Functions (defined globally here to be accessible via current_app) ---
    # These functions are critical and currently still reside here.
    # In a full refactor, they would move to a 'services' module and be imported.

    def get_google_service(api_name, api_version):
        creds = None
        token_path = 'token.json'
        google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')

        if google_token_json_str:
            try:
                creds_info = json.loads(google_token_json_str)
                creds = Credentials.from_authorized_user_info(creds_info, app.config['SCOPES'])
            except Exception as e:
                app.logger.warning(f"Could not load token from GOOGLE_TOKEN_JSON: {e}")
                creds = None
        elif os.path.exists(token_path):
            creds = Credentials.from_authorized_file(token_path, app.config['SCOPES'])

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(GoogleAuthRequest()) # Use GoogleAuthRequest
                except Exception as e:
                    app.logger.error(f"Error refreshing Google token, re-authenticating: {e}")
                    creds = None
            if not creds:
                if os.path.exists(app.config['GOOGLE_CREDENTIALS_FILE_NAME']):
                    flow = InstalledAppFlow.from_client_secrets_file(app.config['GOOGLE_CREDENTIALS_FILE_NAME'], app.config['SCOPES'])
                    creds = flow.run_console()
                else:
                    app.logger.error("Google credentials file not found.")
                    return None
            with open(token_path, 'w') as token:
                token.write(creds.to_json())
            app.logger.info(f"New token saved to {token_path}. Please update GOOGLE_TOKEN_JSON on Render.")

        if creds:
            return build(api_name, api_version, credentials=creds)
        return None

    # Attach Google service getters to app for easier access in other modules via current_app
    app.get_google_tasks_service = lambda: get_google_service('tasks', 'v1')
    app.get_google_calendar_service = lambda: get_google_service('calendar', 'v3')
    app.get_google_drive_service = lambda: get_google_service('drive', 'v3')
    
    # Expose helper functions as app attributes for access via current_app in blueprints
    app.upload_file_to_google_drive = upload_file_to_google_drive
    app.create_google_task = create_google_task
    app.create_google_calendar_event = create_google_calendar_event
    app.delete_google_task = delete_google_task
    app.update_google_task = update_google_task
    app.get_google_tasks_for_report = get_google_tasks_for_report
    app.get_single_task = get_single_task
    app.extract_lat_lon_from_notes = extract_lat_lon_from_notes
    app.find_nearby_jobs = find_nearby_jobs
    app.parse_customer_info_from_notes = parse_customer_info_from_notes
    app.parse_google_task_dates = parse_google_task_dates
    app.parse_tech_report_from_notes = parse_tech_report_from_notes
    app.allowed_file = allowed_file
    app.create_task_flex_message = create_task_flex_message
    app.create_nearby_job_suggestion_message = create_nearby_job_suggestion_message
    app.create_customer_history_carousel = create_customer_history_carousel
    app.get_app_settings = get_app_settings
    app.save_app_settings = save_app_settings
    # line_messaging_api is already attached to app.line_messaging_api above


    return app

# --- Helper functions that are now part of app object (defined globally here for import by blueprints) ---
# These functions should ideally be in a 'services' module and imported.
# For now, they remain globally defined here but will be accessed via current_app in blueprints.

# LINE and Google client instances need to be available when handlers are decorated.
# They are initialized here globally and functions in blueprints use current_app.line_messaging_api etc.
configuration = Configuration(access_token=os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
line_api_client = ApiClient(configuration)
line_messaging_api = MessagingApi(line_api_client) # This is the object used by the main app and exposed
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET')) # This handler is used by handler.add in routes/line_bot.py


def upload_file_to_google_drive(file_path, file_name, mime_type):
    service = current_app.get_google_drive_service()
    if not service: return None
    if not current_app.config['GOOGLE_DRIVE_FOLDER_ID']: return None
    try:
        file_metadata = {'name': file_name, 'parents': [current_app.config['GOOGLE_DRIVE_FOLDER_ID']], 'mimeType': mime_type}
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        file_obj = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink, webContentLink').execute()
        service.permissions().create(fileId=file_obj['id'], body={'role': 'reader', 'type': 'anyone'}, fields='id').execute()
        current_app.logger.info(f"ไฟล์ถูกอัปโหลดไปที่ Google Drive: {file_obj.get('webViewLink')}")
        return file_obj.get('webViewLink')
    except HttpError as error: current_app.logger.error(f'เกิดข้อผิดพลาดขณะอัปโหลดไป Google Drive: {error}'); return None
    except Exception as e: current_app.logger.error(f'เกิดข้อผิดพลาดที่ไม่คาดคิดระหว่างการอัปโหลด Drive: {e}'); return None

def create_google_task(title, notes=None, due=None):
    service = current_app.get_google_tasks_service()
    if not service: return None
    try: return service.tasks().insert(tasklist=current_app.config['GOOGLE_TASKS_LIST_ID'], body={'title': title, 'notes': notes, 'status': 'needsAction', 'due': due}).execute()
    except HttpError as e: current_app.logger.error(f"Error creating Google Task: {e}"); return None

def create_google_calendar_event(summary, location, description, start_time, end_time, timezone='Asia/Bangkok'):
    service = current_app.get_google_calendar_service()
    if not service: current_app.logger.error("Failed to get Google Calendar service."); return None
    try: return service.events().insert(calendarId='primary', body={'summary': summary, 'location': location, 'description': description, 'start': {'dateTime': start_time, 'timeZone': timezone}, 'end': {'dateTime': end_time, 'timeZone': timezone}, 'reminders': {'useDefault': True}}).execute()
    except HttpError as e: current_app.logger.error(f"Error creating Google Calendar Event: {e}"); return None

def delete_google_task(task_id):
    service = current_app.get_google_tasks_service()
    if not service: return False
    try: service.tasks().delete(tasklist=current_app.config['GOOGLE_TASKS_LIST_ID'], task=task_id).execute(); current_app.logger.info(f"Successfully deleted task ID: {task_id}"); return True
    except HttpError as err: current_app.logger.error(f"API Error deleting task {task_id}: {err}"); return False

def update_google_task(task_id, title=None, notes=None, status=None):
    service = current_app.get_google_tasks_service()
    if not service: return None
    try:
        task = service.tasks().get(tasklist=current_app.config['GOOGLE_TASKS_LIST_ID'], task=task_id).execute()
        if title is not None: task['title'] = title
        if notes is not None: task['notes'] = notes
        if status is not None:
            task['status'] = status
            task['completed'] = datetime.datetime.now(pytz.utc).isoformat() if status == 'completed' else None
        return service.tasks().update(tasklist=current_app.config['GOOGLE_TASKS_LIST_ID'], task=task_id, body=task).execute()
    except HttpError as e: current_app.logger.error(f"Failed to update task {task_id}: {e}"); return None

@cached(lambda: current_app.cache) # Use current_app.cache instance
def get_google_tasks_for_report(show_completed=True):
    current_app.logger.info(f"Cache miss/expired. Calling Google Tasks API... (show_completed={show_completed})")
    service = current_app.get_google_tasks_service()
    if not service: return None
    try: return service.tasks().list(tasklist=current_app.config['GOOGLE_TASKS_LIST_ID'], showCompleted=show_completed, maxResults=100).execute().get('items', [])
    except HttpError as err: current_app.logger.error(f"API Error getting tasks: {err}"); return None

def get_single_task(task_id):
    service = current_app.get_google_tasks_service()
    if not service: return None
    try: return service.tasks().get(tasklist=current_app.config['GOOGLE_TASKS_LIST_ID'], task=task_id).execute()
    except HttpError as err: current_app.logger.error(f"Error getting single task {task_id}: {err}"); return None

def extract_lat_lon_from_notes(notes):
    if not notes: return None, None
    match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", notes)
    if match: return (float(match.group(1)), float(match.group(2)))
    match = re.search(r"พิกัด:\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", notes)
    if match: return (float(match.group(1)), float(match.group(2)))
    map_url_regex = r"https?://(?:www\.)?(?:google\.com/maps/place/|maps\.app\.goo\.gl/)(?:[^/]+/@)?(-?\d+\.\d+),(-?\d+\.\d+)"
    map_url_match = re.search(map_url_regex, notes)
    if map_url_match: return (float(map_url_match.group(1)), float(map_url_match.group(2)))
    return None, None

def find_nearby_jobs(completed_task_id, radius_km=5):
    completed_task = get_single_task(completed_task_id)
    if not completed_task: return []
    origin_lat, origin_lon = extract_lat_lon_from_notes(completed_task.get('notes', ''))
    if origin_lat is None or origin_lon is None: current_app.logger.info(f"Completed task {completed_task_id} has no location data. Skipping nearby search."); return []
    origin_coords = (origin_lat, origin_lon)
    pending_tasks = get_google_tasks_for_report(show_completed=False)
    if not pending_tasks: return []
    nearby_jobs = []
    for task in pending_tasks:
        if task.get('id') == completed_task_id: continue
        task_lat, task_lon = extract_lat_lon_from_notes(task.get('notes', ''))
        if task_lat is not None and task_lon is not None:
            task_coords = (task_lat, task_lon)
            distance = geodesic(origin_coords, task_coords).kilometers
            if distance <= radius_km: task['distance_km'] = round(distance, 1); nearby_jobs.append(task)
    nearby_jobs.sort(key=lambda x: x['distance_km'])
    return nearby_jobs

def parse_customer_info_from_notes(notes):
    info = {'name': '', 'phone': '', 'address': '', 'detail': '', 'map_url': None}
    if not notes: return info
    base_notes_content = re.sub(r"--- TECH_REPORT_START ---\s*.*?\s*--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL).strip()
    lines = [line.strip() for line in base_notes_content.split('\n') if line.strip()]
    if len(lines) > 0: info['name'] = lines[0]
    if len(lines) > 1: info['phone'] = lines[1]
    if len(lines) > 2: info['address'] = lines[2]
    map_url_regex = r"https?://(?:www\.)?(?:google\.com/maps|maps\.app\.goo\.gl)\S+"
    detail_start_line_idx = 3
    if len(lines) > 3:
        if re.match(map_url_regex, lines[3]): info['map_url'] = lines[3]; detail_start_line_idx = 4
        else: detail_start_line_idx = 3
    if len(lines) > detail_start_line_idx -1: info['detail'] = "\n".join(lines[detail_start_line_idx:])
    return info
    
def parse_google_task_dates(task_item):
    parsed_task = task_item.copy()
    for key in ['created', 'due', 'completed']:
        if key in parsed_task and parsed_task[key]:
            try:
                dt_utc = datetime.datetime.fromisoformat(parsed_task[key].replace('Z', '+00:00'))
                dt_thai = dt_utc.astimezone(current_app.config['THAILAND_TZ'])
                parsed_task[f'{key}_formatted'] = dt_thai.strftime("%d/%m/%y %H:%M" if key == 'due' else "%d/%m/%y %H:%M:%S")
            except (ValueError, TypeError): parsed_task[f'{key}_formatted'] = ''
        else: parsed_task[f'{key}_formatted'] = ''
    return parsed_task

def parse_tech_report_from_notes(notes):
    if not notes: return [], ""
    report_blocks = re.findall(r"--- TECH_REPORT_START ---\s*\n(.*?)\n--- TECH_REPORT_END ---", notes, re.DOTALL)
    history = []
    for json_str in report_blocks:
        try: history.append(json.loads(json_str))
        except json.JSONDecodeError as e: current_app.logger.error(f"Error decoding JSON in tech report block: {e}, Content: {json_str[:100]}...")
    original_notes_text = re.sub(r"--- TECH_REPORT_START ---\s*.*?\s*--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL).strip()
    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, original_notes_text

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in current_app.config['ALLOWED_EXTENSIONS']

def create_task_flex_message(task):
    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    parsed_dates = parse_google_task_dates(task)
    update_url = url_for('web.update_task_details', task_id=task.get('id'), _external=True)
    phone_action = None
    if customer_info.get('phone'): phone_number = re.sub(r'\D', '', customer_info['phone']); phone_action = URIAction(label=customer_info['phone'], uri=f"tel:{phone_number}")
    map_action = None
    map_url = customer_info.get('map_url')
    if map_url: map_action = URIAction(label="📍 เปิด Google Maps", uri=map_url)
    body_contents = [
        TextComponent(text=task.get('title', 'ไม่มีหัวข้อ'), weight='bold', size='xl', wrap=True),
        BoxComponent(layout='vertical', margin='lg', spacing='sm', contents=[
            BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='ลูกค้า', color='#aaaaaa', size='sm', flex=2), TextComponent(text=customer_info.get('name', '') or '-', wrap=True, color='#666666', size='sm', flex=5)]),
            BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='โทร', color='#aaaaaa', size='sm', flex=2), TextComponent(text=customer_info.get('phone', '') or '-', wrap=True, color='#1E90FF', size='sm', flex=5, action=phone_action, decoration='underline' if phone_action else 'none')]),
            BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='นัดหมาย', color='#aaaaaa', size='sm', flex=2), TextComponent(text=parsed_dates.get('due_formatted', '') or '-', wrap=True, color='#666666', size='sm', flex=5)])
        ])
    ]
    footer_contents = [ButtonComponent(style='link', height='sm', action=URIAction(label='📝 อัปเดต/สรุปงาน', uri=update_url))]
    if map_action: footer_contents.insert(0, ButtonComponent(style='link', height='sm', action=map_action)); footer_contents.insert(1, SeparatorComponent(margin='md'))
    bubble = BubbleContainer(direction='ltr', header=BoxComponent(layout='vertical', contents=[TextComponent(text='📢 แจ้งเตือนงาน', weight='bold', color='#ffffff')], background_color='#007BFF', padding_all='12px'), body=BoxComponent(layout='vertical', contents=body_contents), footer=BoxComponent(layout='vertical', spacing='sm', contents=footer_contents, flex=0), action=URIAction(uri=update_url))
    return FlexMessage(alt_text=f"แจ้งเตือนงาน: {task.get('title', '')}", contents=bubble)

def create_nearby_job_suggestion_message(completed_task_title, nearby_tasks):
    if not nearby_tasks: return None
    bubbles = []
    for task in nearby_tasks[:12]:
        customer_info = parse_customer_info_from_notes(task.get('notes', ''))
        update_url = url_for('web.update_task_details', task_id=task.get('id'), _external=True)
        phone_action = None
        if customer_info.get('phone'): phone_number = re.sub(r'\D', '', customer_info['phone']); phone_action = URIAction(label=f"📞 โทร: {customer_info['phone']}", uri=f"tel:{phone_number}")
        bubble = BubbleContainer(direction='ltr', header=BoxComponent(layout='vertical', background_color='#FFDDC2', contents=[TextComponent(text='💡 แนะนำงานใกล้เคียง!', weight='bold', color='#BF5A00', size='md')]), body=BoxComponent(layout='vertical', spacing='md', contents=[TextComponent(text=f"ห่างไป {task['distance_km']} กม.", size='sm', color='#555555'), TextComponent(text=f"ลูกค้า: {customer_info.get('name', '') or 'N/A'}", weight='bold', size='lg', wrap=True), TextComponent(text=task.get('title', '-'), wrap=True, size='sm', color='#666666')]), footer=BoxComponent(layout='vertical', spacing='sm', contents=([phone_action] if phone_action else []) + [ButtonComponent(style='link', height='sm', action=URIAction(label='ดูรายละเอียด/แผนที่', uri=update_url))]))
        bubbles.append(bubble)
    alt_text = f"คุณอยู่ใกล้กับงานอื่น! หลังจากปิดงาน '{completed_task_title}'"
    return FlexMessage(alt_text=alt_text, contents=CarouselContainer(contents=bubbles))

def create_customer_history_carousel(tasks, customer_name):
    if not tasks: return None
    bubbles = []
    tasks.sort(key=lambda x: x.get('created', ''), reverse=True)
    for task in tasks[:12]:
        parsed = parse_google_task_dates(task)
        update_url = url_for('web.update_task_details', task_id=task.get('id'), _external=True)
        status_text, status_color = "รอดำเนินการ", "#FFA500"
        if task.get('status') == 'completed': status_text, status_color = "เสร็จสิ้น", "#28A745"
        elif 'due' in task and task['due']:
            try:
                due_dt_utc = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                if due_dt_utc < datetime.datetime.now(pytz.utc): status_text, status_color = "ยังไม่ดำเนินการ", "#DC3545"
            except (ValueError, TypeError): pass
        bubble = BubbleContainer(direction='ltr', body=BoxComponent(layout='vertical', spacing='md', contents=[TextComponent(text=task.get('title', 'N/A'), weight='bold', size='lg', wrap=True), SeparatorComponent(margin='md'), BoxComponent(layout='vertical', margin='md', spacing='sm', contents=[BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='สถานะ', color='#aaaaaa', size='sm', flex=2), TextComponent(text=status_text, wrap=True, color=status_color, size='sm', flex=5, weight='bold')]), BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='วันที่สร้าง', color='#aaaaaa', size='sm', flex=2), TextComponent(text=parsed.get('created_formatted', '') or '-', wrap=True, color='#666666', size='sm', flex=5)])])]), footer=BoxComponent(layout='vertical', spacing='sm', contents=[ButtonComponent(style='link', height='sm', action=URIAction(label='ดูรายละเอียด / อัปเดต', uri=update_url))]))
        bubbles.append(bubble)
    return CarouselContainer(contents=bubbles)

def get_app_settings():
    current_app.logger.info("Using MOCK get_app_settings()")
    return {
        'report_times': {
            'appointment_reminder_hour_thai': int(os.environ.get('APPOINTMENT_REMINDER_HOUR_THAI', 7)), # Read from ENV or default
            'outstanding_report_hour_thai': int(os.environ.get('OUTSTANDING_REPORT_HOUR_THAI', 20)) # Read from ENV or default
        },
        'line_recipients': {
            'admin_group_id': os.environ.get('LINE_ADMIN_GROUP_ID', ''),
            'manager_user_id': os.environ.get('LINE_MANAGER_USER_ID', ''),
            'technician_group_id': os.environ.get('LINE_TECHNICIAN_GROUP_ID', '')
        }
    }

def save_app_settings(settings_data):
    current_app.logger.info(f"Using MOCK save_app_settings() with data: {settings_data}")
    # In a real app, save this to DB. For now, it's a mock.
    # You would update environment variables or a settings table here.
    return True

def check_for_nearby_jobs_and_notify(completed_task_id, source_id):
    nearby_tasks = find_nearby_jobs(completed_task_id)
    if nearby_tasks:
        completed_task = get_single_task(completed_task_id)
        suggestion_message = create_nearby_job_suggestion_message(completed_task.get('title', ''), nearby_tasks)
        if suggestion_message:
            try:
                current_app.line_messaging_api.push_message(PushMessageRequest(to=source_id, messages=[suggestion_message]))
                current_app.logger.info(f"Sent nearby job suggestions to {source_id}")
            except Exception as e:
                current_app.logger.error(f"Failed to send nearby job suggestion: {e}")


# --- Main Application Instance ---
app = create_app()

# --- Cron Job Endpoint (now outside of create_app but uses app context) ---
@app.route('/trigger_daily_reports')
def trigger_daily_reports():
    app.logger.info("Cron job triggered for daily reports.")
    settings = app.get_app_settings()
    now_thai = datetime.datetime.now(app.config['THAILAND_TZ'])
    current_hour = now_thai.hour

    appointment_hour = settings.get('report_times', {}).get('appointment_reminder_hour_thai', 7)
    summary_hour = settings.get('report_times', {}).get('outstanding_report_hour_thai', 20)

    tasks_to_process = app.get_google_tasks_for_report(show_completed=False)
    if tasks_to_process is None:
        return "Failed to get tasks from Google API", 500

    messages_to_send = []
    recipients = []
    
    if current_hour == appointment_hour:
        app.logger.info("Processing daily APPOINTMENT reminders.")
        today_appointments = []
        for task in tasks_to_process:
            if 'due' in task and task['due']:
                 try:
                    dt_utc = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                    dt_thai = dt_utc.astimezone(app.config['THAILAND_TZ'])
                    if dt_thai.date() == now_thai.date():
                        today_appointments.append(task)
                 except (ValueError, TypeError): continue
        
        if today_appointments:
            today_appointments.sort(key=lambda x: x.get('due', ''))
            messages_to_send = [app.create_task_flex_message(task) for task in today_appointments]
            recipients = [id for id in [settings['line_recipients'].get('technician_group_id'), settings['line_recipients'].get('admin_group_id')] if id]

    elif current_hour == summary_hour:
        app.logger.info("Processing daily OUTSTANDING tasks summary.")
        if tasks_to_process:
            tasks_to_process.sort(key=lambda x: x.get('due', '9999-99-99'))
            message_lines = ["--- 🌙 สรุปงานค้าง ---"]
            for task in tasks_to_process:
                info = app.parse_customer_info_from_notes(task.get('notes', ''))
                message_lines.append(f"ลูกค้า: {info.get('name', '') or '-'}\nโทร: {info.get('phone', '') or '-'}\nงาน: {task.get('title')}") 
            messages_to_send = [TextMessage(text="\n\n".join(message_lines))]
            recipients = [id for id in [settings['line_recipients'].get('manager_user_id'), settings['line_recipients'].get('admin_group_id')] if id]

    if messages_to_send and recipients:
        if isinstance(recipients, list):
            for recipient_id in recipients:
                for i in range(0, len(messages_to_send), 5):
                    app.line_messaging_api.push_message(PushMessageRequest(to=recipient_id, messages=messages_to_send[i:i+5]))
        else:
            for i in range(0, len(messages_to_send), 5):
                app.line_messaging_api.push_message(PushMessageRequest(to=recipients, messages=messages_to_send[i:i+5]))
        
        return f"{len(messages_to_send)} messages sent to {len(recipients)} recipients.", 200

    return "No report scheduled or no recipients for this hour.", 200


if __name__ == '__main__':
    # When running locally, ensure app context for db operations
    # and create tables if they don't exist.
    with app.app_context():
        db.create_all() # Create database tables if they don't exist
    
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
