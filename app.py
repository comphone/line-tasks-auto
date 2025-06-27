import os
import sys
import datetime
import re
import json
import pytz
import mimetypes

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, render_template, redirect, url_for, abort, send_from_directory, flash, jsonify
from werkzeug.utils import secure_filename
from cachetools import cached, TTLCache
from geopy.distance import geodesic

# Corrected LINE Bot SDK imports for Flex Messages
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, PushMessageRequest, TextMessage, ReplyMessageRequest, FlexMessage
)
from linebot.models import (
    BubbleContainer, CarouselContainer, BoxComponent, TextComponent,
    ButtonComponent, SeparatorComponent, URIAction, PostbackAction, QuickReply, QuickReplyButton
)
from linebot.v3 import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent, ImageMessageContent, FileMessageContent, GroupSource, UserSource # Added GroupSource, UserSource for source type check
from linebot.v3.exceptions import InvalidSignatureError

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# --- Initialization & Configurations ---
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_dev')
UPLOAD_FOLDER = 'static/uploads' # This folder is now primarily for temporary storage before Drive upload
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'}

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# --- LINE & Google Configs ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    sys.exit("LINE Bot credentials are not set in environment variables.")

LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM')
LINE_ADMIN_GROUP_ID = os.environ.get('LINE_ADMIN_GROUP_ID')
LINE_HR_GROUP_ID = os.environ.get('LINE_HR_GROUP_ID')
GOOGLE_TASKS_LIST_ID = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')

# Ensure GOOGLE_DRIVE_FOLDER_ID is set if you want to use Drive
if not GOOGLE_DRIVE_FOLDER_ID:
    app.logger.warning("GOOGLE_DRIVE_FOLDER_ID environment variable is not set. Drive upload will not work.")


SCOPES = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/drive']
GOOGLE_CREDENTIALS_FILE_NAME = 'credentials.json'
THAILAND_TZ = pytz.timezone('Asia/Bangkok')
cache = TTLCache(maxsize=100, ttl=60)

# Initialize LINE Bot SDK
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_api_client = ApiClient(configuration)
line_messaging_api = MessagingApi(line_api_client)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- Technician Mention Mapping (Placeholder) ---
TECHNICIAN_LINE_IDS = {
    "ช่างเอ": "Uxxxxxxxxxxxxxxxxxxxxxxxxx1",
    "ช่างบี": "Uxxxxxxxxxxxxxxxxxxxxxxxxx2",
}

# --- Mock Settings Functions (Placeholders) ---
def get_app_settings():
    """Mock function to get app settings."""
    app.logger.info("Using MOCK get_app_settings()")
    return {
        'report_times': {
            'appointment_reminder_hour_thai': 7,
            'outstanding_report_hour_thai': 20
        },
        'line_recipients': {
            'admin_group_id': os.environ.get('LINE_ADMIN_GROUP_ID', ''),
            'manager_user_id': os.environ.get('LINE_MANAGER_USER_ID', ''),
            'technician_group_id': os.environ.get('LINE_TECHNICIAN_GROUP_ID', '')
        }
    }

def save_app_settings(settings_data):
    """Mock function to save app settings."""
    app.logger.info(f"Using MOCK save_app_settings() with data: {settings_data}")
    return True

# --- Google API Helper Functions ---
def get_google_service(api_name, api_version):
    """Handles Google API authentication and returns a service object for the specified API."""
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
        creds = Credentials.from_authorized_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                app.logger.error(f"Error refreshing Google token, re-authenticating: {e}")
                creds = None
        if not creds:
            if os.path.exists(GOOGLE_CREDENTIALS_FILE_NAME):
                flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_FILE_NAME, SCOPES)
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

def get_google_tasks_service():
    """Gets the Google Tasks service object."""
    return get_google_service('tasks', 'v1')

def get_google_calendar_service():
    """Gets the Google Calendar service object."""
    return get_google_service('calendar', 'v3')

def get_google_drive_service():
    """Gets the Google Drive service object."""
    return get_google_service('drive', 'v3')

def upload_file_to_google_drive(file_path, file_name, mime_type):
    """
    Uploads a file to Google Drive and makes it publicly accessible.
    Returns the web view link if successful, otherwise None.
    """
    service = get_google_drive_service()
    if not service:
        app.logger.error("ไม่สามารถเชื่อมต่อ Google Drive service ได้สำหรับการอัปโหลด")
        return None
    
    if not GOOGLE_DRIVE_FOLDER_ID:
        app.logger.warning("ไม่ได้ตั้งค่า GOOGLE_DRIVE_FOLDER_ID ไม่สามารถอัปโหลดไฟล์ไป Google Drive ได้")
        return None

    try:
        file_metadata = {
            'name': file_name,
            'parents': [GOOGLE_DRIVE_FOLDER_ID],
            'mimeType': mime_type
        }
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        
        file_obj = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink, webContentLink' # Request webContentLink for direct download, webViewLink for browser view
        ).execute()

        # Make the file publicly accessible
        service.permissions().create(
            fileId=file_obj['id'],
            body={'role': 'reader', 'type': 'anyone'}, # 'type': 'anyone' makes it publicly accessible
            fields='id'
        ).execute()
        
        app.logger.info(f"ไฟล์ถูกอัปโหลดไปที่ Google Drive: {file_obj.get('webViewLink')}")
        return file_obj.get('webViewLink') # Return webViewLink for browser viewing

    except HttpError as error:
        app.logger.error(f'เกิดข้อผิดพลาดขณะอัปโหลดไป Google Drive: {error}')
        return None
    except Exception as e:
        app.logger.error(f'เกิดข้อผิดพลาดที่ไม่คาดคิดระหว่างการอัปโหลด Drive: {e}')
        return None

# --- Task and Event Creation Functions ---
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

def create_google_calendar_event(summary, location, description, start_time, end_time, timezone='Asia/Bangkok'):
    """Creates a new event in Google Calendar."""
    service = get_google_calendar_service()
    if not service:
        app.logger.error("Failed to get Google Calendar service.")
        return None
    try:
        event = {
            'summary': summary, 'location': location, 'description': description,
            'start': {'dateTime': start_time, 'timeZone': timezone},
            'end': {'dateTime': end_time, 'timeZone': timezone},
            'reminders': {'useDefault': True},
        }
        return service.events().insert(calendarId='primary', body=event).execute()
    except HttpError as e:
        app.logger.error(f"Error creating Google Calendar Event: {e}")
        return None

def delete_google_task(task_id):
    """Deletes a task from Google Tasks."""
    service = get_google_tasks_service()
    if not service: return False
    try:
        service.tasks().delete(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
        app.logger.info(f"Successfully deleted task ID: {task_id}")
        return True
    except HttpError as err:
        app.logger.error(f"API Error deleting task {task_id}: {err}")
        return False

def update_google_task(task_id, title=None, notes=None, status=None): # Added title parameter
    """Helper to update a specific task."""
    service = get_google_tasks_service()
    if not service: return None
    try:
        task = service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
        if title is not None: task['title'] = title # Update title
        if notes is not None: task['notes'] = notes
        if status is not None:
            task['status'] = status
            if status == 'completed': task['completed'] = datetime.datetime.now(pytz.utc).isoformat()
            else: task.pop('completed', None) # Remove completed timestamp if not completed
        return service.tasks().update(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id, body=task).execute()
    except HttpError as e:
        app.logger.error(f"Failed to update task {task_id}: {e}")
        return None

# --- Data Parsing and Utility Functions ---
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

def get_upcoming_events(time_delta_hours=24):
    """Fetches upcoming Google Calendar events."""
    service = get_google_calendar_service()
    if not service: return []
    try:
        now_utc = datetime.datetime.utcnow().isoformat() + 'Z'
        time_max_utc = (datetime.datetime.utcnow() + datetime.timedelta(hours=time_delta_hours)).isoformat() + 'Z'
        
        events_result = service.events().list(
            calendarId='primary', timeMin=now_utc, timeMax=time_max_utc,
            maxResults=10, singleEvents=True, orderBy='startTime'
        ).execute()
        return events_result.get('items', [])
    except HttpError as e:
        app.logger.error(f"Error fetching upcoming events: {e}")
        return []

def extract_lat_lon_from_notes(notes):
    """Extracts latitude and longitude from task notes. Also handles Google Maps URLs."""
    if not notes: return None, None
    # Check for direct coordinates: @-?\d+\.\d+,-?\d+\.\d+
    match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", notes)
    if match: return (float(match.group(1)), float(match.group(2)))
    
    # Check for "พิกัด: -?\d+\.\d+,-?\d+\.\d+"
    match = re.search(r"พิกัด:\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", notes)
    if match: return (float(match.group(1)), float(match.group(2)))

    # Check for Google Maps URL with embedded coordinates
    map_url_regex = r"https?://(?:www\.)?(?:google\.com/maps/place/|maps\.app\.goo\.gl/)(?:[^/]+/@)?(-?\d+\.\d+),(-?\d+\.\d+)"
    map_url_match = re.search(map_url_regex, notes)
    if map_url_match:
        return (float(map_url_match.group(1)), float(map_url_match.group(2)))
        
    return None, None

def find_nearby_jobs(completed_task_id, radius_km=5):
    """Finds nearby pending jobs based on a completed task's location."""
    completed_task = get_single_task(completed_task_id)
    if not completed_task: return []

    origin_lat, origin_lon = extract_lat_lon_from_notes(completed_task.get('notes', ''))
    if origin_lat is None or origin_lon is None:
        app.logger.info(f"Completed task {completed_task_id} has no location data. Skipping nearby search.")
        return []
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
            if distance <= radius_km:
                task['distance_km'] = round(distance, 1)
                nearby_jobs.append(task)
    
    nearby_jobs.sort(key=lambda x: x['distance_km'])
    return nearby_jobs

def parse_customer_info_from_notes(notes):
    """
    Extracts customer name, phone, address, detail, and map_url from notes
    based on line-by-line parsing.
    Returns empty string "" for missing fields.
    """
    info = {
        'name': '', 
        'phone': '', 
        'address': '', 
        'detail': '', 
        'map_url': None
    }
    if not notes: return info

    # Remove tech report blocks first
    base_notes_content = re.sub(r"--- TECH_REPORT_START ---\s*.*?\s*--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL).strip()
    
    lines = [line.strip() for line in base_notes_content.split('\n') if line.strip()] # Only non-empty lines for parsing

    # Line 1: Customer Name
    if len(lines) > 0:
        info['name'] = lines[0]
    
    # Line 2: Phone Number
    if len(lines) > 1:
        info['phone'] = lines[1]
    
    # Line 3: Address
    if len(lines) > 2:
        info['address'] = lines[2]

    # Line 4: Map URL or start of Detail
    map_url_regex = r"https?://(?:www\.)?(?:google\.com/maps|maps\.app\.goo\.gl)\S+"
    detail_start_line_idx = 3 # Default: detail starts from line 4 (index 3)

    if len(lines) > 3:
        if re.match(map_url_regex, lines[3]):
            info['map_url'] = lines[3]
            detail_start_line_idx = 4 # Detail starts from line 5 (index 4)
        else:
            # Line 4 is not a map URL, so it's part of the detail
            detail_start_line_idx = 3 
    
    # Remaining lines for Detail
    if len(lines) > detail_start_line_idx -1: # Adjusted for 0-based indexing
        info['detail'] = "\n".join(lines[detail_start_line_idx:])

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
                parsed_task[f'{key}_formatted'] = '' # Change N/A to empty string
        else:
            parsed_task[f'{key}_formatted'] = '' # Change N/A to empty string
    return parsed_task

def parse_tech_report_from_notes(notes):
    """Extracts all past technician reports into a history list and the original notes text."""
    if not notes: return [], ""
    report_blocks = re.findall(r"--- TECH_REPORT_START ---\s*\n(.*?)\n--- TECH_REPORT_END ---", notes, re.DOTALL)
    history = []
    for json_str in report_blocks:
        try:
            history.append(json.loads(json_str))
        except json.JSONDecodeError as e:
            app.logger.error(f"Error decoding JSON in tech report block: {e}, Content: {json_str[:100]}...")

    # The original notes text is everything outside the TECH_REPORT blocks
    original_notes_text = re.sub(r"--- TECH_REPORT_START ---\s*.*?\s*--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL).strip()
    
    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, original_notes_text

def allowed_file(filename):
    """Checks for allowed file extensions."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- Flex Message Creation Functions ---
def create_task_flex_message(task):
    """Creates a LINE Flex Message for a given task."""
    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    parsed_dates = parse_google_task_dates(task)
    update_url = url_for('update_task_details', task_id=task.get('id'), _external=True)

    phone_action = None
    if customer_info.get('phone'): # Check for non-empty string
        phone_number = re.sub(r'\D', '', customer_info['phone'])
        phone_action = URIAction(label=customer_info['phone'], uri=f"tel:{phone_number}")
    
    map_action = None
    # Use map_url directly from parsed customer_info if available, else try to extract
    map_url = customer_info.get('map_url')
    if map_url: # No fallback needed since parse_customer_info_from_notes is now line-by-line
        map_action = URIAction(label="📍 เปิด Google Maps", uri=map_url)

    body_contents = [
        TextComponent(text=task.get('title', 'ไม่มีหัวข้อ'), weight='bold', size='xl', wrap=True),
        BoxComponent(layout='vertical', margin='lg', spacing='sm', contents=[
            BoxComponent(layout='baseline', spacing='sm', contents=[
                TextComponent(text='ลูกค้า', color='#aaaaaa', size='sm', flex=2),
                TextComponent(text=customer_info.get('name', '') or '-', wrap=True, color='#666666', size='sm', flex=5) # Display empty string as '-'
            ]),
            BoxComponent(layout='baseline', spacing='sm', contents=[
                TextComponent(text='โทร', color='#aaaaaa', size='sm', flex=2),
                TextComponent(text=customer_info.get('phone', '') or '-', wrap=True, color='#1E90FF', size='sm', flex=5, action=phone_action, decoration='underline' if phone_action else 'none')
            ]),
            BoxComponent(layout='baseline', spacing='sm', contents=[
                TextComponent(text='นัดหมาย', color='#aaaaaa', size='sm', flex=2),
                TextComponent(text=parsed_dates.get('due_formatted', '') or '-', wrap=True, color='#666666', size='sm', flex=5) # Display empty string as '-'
            ])
        ])
    ]

    footer_contents = [
        ButtonComponent(style='link', height='sm', action=URIAction(label='📝 อัปเดต/สรุปงาน', uri=update_url))
    ]
    if map_action:
        footer_contents.insert(0, ButtonComponent(style='link', height='sm', action=map_action))
        footer_contents.insert(1, SeparatorComponent(margin='md'))

    bubble = BubbleContainer(direction='ltr',
        header=BoxComponent(layout='vertical', contents=[TextComponent(text='📢 แจ้งเตือนงาน', weight='bold', color='#ffffff')], background_color='#007BFF', padding_all='12px'),
        body=BoxComponent(layout='vertical', contents=body_contents),
        footer=BoxComponent(layout='vertical', spacing='sm', contents=footer_contents, flex=0),
        action=URIAction(uri=update_url)
    )
    return FlexMessage(alt_text=f"แจ้งเตือนงาน: {task.get('title', '')}", contents=bubble)

def create_nearby_job_suggestion_message(completed_task_title, nearby_tasks):
    """Creates a Flex Message Carousel for nearby job suggestions."""
    if not nearby_tasks: return None
    
    bubbles = []
    for task in nearby_tasks[:12]:
        customer_info = parse_customer_info_from_notes(task.get('notes', ''))
        update_url = url_for('update_task_details', task_id=task.get('id'), _external=True)
        
        phone_action = None
        if customer_info.get('phone'): # Check for non-empty string
            phone_number = re.sub(r'\D', '', customer_info['phone'])
            phone_action = URIAction(label=f"📞 โทร: {customer_info['phone']}", uri=f"tel:{phone_number}")

        bubble = BubbleContainer(direction='ltr',
            header=BoxComponent(layout='vertical', background_color='#FFDDC2', contents=[TextComponent(text='💡 แนะนำงานใกล้เคียง!', weight='bold', color='#BF5A00', size='md')]),
            body=BoxComponent(layout='vertical', spacing='md', contents=[
                TextComponent(text=f"ห่างไป {task['distance_km']} กม.", size='sm', color='#555555'),
                TextComponent(text=f"ลูกค้า: {customer_info.get('name', '') or 'N/A'}", weight='bold', size='lg', wrap=True),
                TextComponent(text=task.get('title', '-'), wrap=True, size='sm', color='#666666')
            ]),
            footer=BoxComponent(layout='vertical', spacing='sm', contents=([phone_action] if phone_action else []) + [ButtonComponent(style='link', height='sm', action=URIAction(label='ดูรายละเอียด/แผนที่', uri=update_url))])
        )
        bubbles.append(bubble)

    alt_text = f"คุณอยู่ใกล้กับงานอื่น! หลังจากปิดงาน '{completed_task_title}'"
    return FlexMessage(alt_text=alt_text, contents=CarouselContainer(contents=bubbles))

def create_customer_history_carousel(tasks, customer_name):
    """Creates a Flex Message Carousel Container for a customer's task history."""
    if not tasks: return None

    bubbles = []
    tasks.sort(key=lambda x: x.get('created', ''), reverse=True)
    
    for task in tasks[:12]:
        parsed = parse_google_task_dates(task)
        update_url = url_for('update_task_details', task_id=task.get('id'), _external=True)

        status_text, status_color = "รอดำเนินการ", "#FFA500"
        if task.get('status') == 'completed':
            status_text, status_color = "เสร็จสิ้น", "#28A745"
        elif 'due' in task and task['due']:
            try:
                due_dt_utc = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                if due_dt_utc < datetime.datetime.now(pytz.utc):
                    status_text, status_color = "ยังไม่ดำเนินการ", "#DC3545"
            except (ValueError, TypeError): pass
        
        bubble = BubbleContainer(direction='ltr',
            body=BoxComponent(layout='vertical', spacing='md', contents=[
                TextComponent(text=task.get('title', 'N/A'), weight='bold', size='lg', wrap=True),
                SeparatorComponent(margin='md'),
                BoxComponent(layout='vertical', margin='md', spacing='sm', contents=[
                    BoxComponent(layout='baseline', spacing='sm', contents=[
                        TextComponent(text='สถานะ', color='#aaaaaa', size='sm', flex=2),
                        TextComponent(text=status_text, wrap=True, color=status_color, size='sm', flex=5, weight='bold')
                    ]),
                    BoxComponent(layout='baseline', spacing='sm', contents=[
                        TextComponent(text='วันที่สร้าง', color='#aaaaaa', size='sm', flex=2),
                        TextComponent(text=parsed.get('created_formatted', '') or '-', wrap=True, color='#666666', size='sm', flex=5) # Display empty string as '-'
                    ])
                ])
            ]),
            footer=BoxComponent(layout='vertical', spacing='sm', contents=[ButtonComponent(style='link', height='sm', action=URIAction(label='ดูรายละเอียด / อัปเดต', uri=update_url))])
        )
        bubbles.append(bubble)

    return CarouselContainer(contents=bubbles)

# --- Web Page Routes ---

@app.route("/", methods=['GET', 'POST'])
def form_page():
    if request.method == 'POST':
        customer_name = request.form.get('customer')
        customer_phone = request.form.get('phone')
        address = request.form.get('address')
        detail = request.form.get('detail')
        appointment_str = request.form.get('appointment')
        map_url_from_form = request.form.get('latitude_longitude') # This is now the full Google Maps URL input

        today_str = datetime.datetime.now(THAILAND_TZ).strftime('%d/%m/%y')
        title = f"งานลูกค้า: {customer_name or 'ไม่ระบุชื่อลูกค้า'} ({today_str})" # Use 'ไม่ระบุชื่อลูกค้า' if name is empty
        
        # Construct notes based on line-by-line format
        notes_lines = []
        notes_lines.append(customer_name or '') # Line 1: Customer Name
        notes_lines.append(customer_phone or '') # Line 2: Phone
        notes_lines.append(address or '') # Line 3: Address
        
        if map_url_from_form: # Line 4: Map URL (if present)
            notes_lines.append(map_url_from_form)
        
        # Line 5 (or Line 4 if no map URL): Detail
        # Ensure detail is the last part and handles multiple lines correctly
        if detail:
            # Append detail lines individually if it contains newlines, or as a single entry
            notes_lines.extend(detail.split('\n'))

        # Remove trailing empty strings if any, to keep notes concise
        while notes_lines and notes_lines[-1] == '':
            notes_lines.pop()

        notes = "\n".join(notes_lines)

        due_date_gmt, start_time_iso, end_time_iso = None, None, None
        if appointment_str:
            try:
                dt_local = THAILAND_TZ.localize(datetime.datetime.strptime(appointment_str, "%Y-%m-%d %H:%M"))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat()
                start_time_iso = dt_local.isoformat()
                end_time_iso = (dt_local + datetime.timedelta(hours=1)).isoformat()
            except ValueError:
                app.logger.error(f"Invalid appointment format: {appointment_str}")

        created_task = create_google_task(title, notes=notes, due=due_date_gmt)

        if created_task and start_time_iso:
            app.logger.info("Creating Google Calendar event...")
            create_google_calendar_event(summary=title, location=address or '', description=notes, start_time=start_time_iso, end_time=end_time_iso)
        
        if created_task:
            flex_message = create_task_flex_message(created_task)
            settings = get_app_settings()
            recipients = [id for id in [settings['line_recipients'].get('admin_group_id'), settings['line_recipients'].get('technician_group_id')] if id]
            if recipients:
                try:
                    # Push messages individually if recipients is a list of multiple IDs
                    if isinstance(recipients, list):
                        for recipient_id in recipients:
                             line_messaging_api.push_message(PushMessageRequest(to=recipient_id, messages=[flex_message]))
                    else: # Single recipient
                        line_messaging_api.push_message(PushMessageRequest(to=recipients, messages=[flex_message]))
                except Exception as e:
                    app.logger.error(f"Failed to push Flex Message: {e}")
            flash('สร้างงานและส่งแจ้งเตือนเรียบร้อยแล้ว!', 'success')
            return redirect(url_for('summary'))
        else:
            flash('เกิดข้อผิดพลาดในการสร้างงาน', 'danger')
            return redirect(url_for('form_page'))

    return render_template('form.html')

@app.route('/summary')
def summary():
    """Displays the task summary page with search functionality and status filtering."""
    search_query = request.args.get('search_query', '').strip().lower()
    status_filter = request.args.get('status_filter', 'all').strip() # 'all', 'needsAction', 'overdue', 'completed'

    tasks_raw = get_google_tasks_for_report(show_completed=True)
    
    if tasks_raw is None:
        flash('ไม่สามารถเชื่อมต่อกับ Google Tasks ได้ในขณะนี้', 'danger')
        tasks_raw = []

    current_time_utc = datetime.datetime.now(pytz.utc)

    # First, apply status filter
    filtered_by_status_tasks = []
    for task_item in tasks_raw:
        task_status = task_item.get('status')
        is_overdue_check = False
        if task_status == 'needsAction' and 'due' in task_item and task_item.get('due'):
            try:
                due_dt_utc = datetime.datetime.fromisoformat(task_item['due'].replace('Z', '+00:00'))
                if due_dt_utc < current_time_utc:
                    is_overdue_check = True
            except (ValueError, TypeError):
                pass # Ignore invalid date formats

        if status_filter == 'all':
            filtered_by_status_tasks.append(task_item)
        elif status_filter == 'completed' and task_status == 'completed':
            filtered_by_status_tasks.append(task_item)
        elif status_filter == 'needsAction' and task_status == 'needsAction' and not is_overdue_check:
            filtered_by_status_tasks.append(task_item)
        elif status_filter == 'overdue' and is_overdue_check:
            filtered_by_status_tasks.append(task_item)

    # Then, apply search query filter to the results from status filter
    final_filtered_tasks = []
    for task in filtered_by_status_tasks:
        if not search_query or \
           search_query in task.get('title', '').lower() or \
           search_query in parse_customer_info_from_notes(task.get('notes', '')).get('name', '').lower() or \
           search_query in parse_customer_info_from_notes(task.get('notes', '')).get('phone', '') or \
           search_query in parse_customer_info_from_notes(task.get('notes', '')).get('address', '').lower() or \
           search_query in parse_customer_info_from_notes(task.get('notes', '')).get('detail', '').lower():
            final_filtered_tasks.append(task)


    tasks = []
    # Re-calculate summary stats based on ALL tasks (tasks_raw) to show overall status counts
    # This ensures the counts in the cards remain consistent regardless of current filter
    total_summary_stats = {'needsAction': 0, 'completed': 0, 'overdue': 0, 'total': len(tasks_raw)}
    for task_item in tasks_raw:
        task_status = task_item.get('status')
        is_overdue_check = False
        if task_status == 'needsAction' and 'due' in task_item and task_item.get('due'):
            try:
                due_dt_utc = datetime.datetime.fromisoformat(task_item['due'].replace('Z', '+00:00'))
                if due_dt_utc < current_time_utc:
                    is_overdue_check = True
            except (ValueError, TypeError): pass
        
        if task_status == 'completed':
            total_summary_stats['completed'] += 1
        elif task_status == 'needsAction':
            total_summary_stats['needsAction'] += 1
            if is_overdue_check:
                total_summary_stats['overdue'] += 1


    for task_item in final_filtered_tasks: # Iterate over the final filtered tasks for display
        parsed_task = parse_google_task_dates(task_item)
        parsed_task['customer'] = parse_customer_info_from_notes(parsed_task.get('notes', ''))
        
        history, original_notes_text_removed_tech_reports = parse_tech_report_from_notes(parsed_task.get('notes', ''))
        parsed_task['tech_reports_history'] = history
        parsed_task['notes_display'] = original_notes_text_removed_tech_reports 
        
        status = task_item.get('status')
        if status == 'completed':
            parsed_task['display_status'] = 'เสร็จสิ้น'
        elif status == 'needsAction':
            is_overdue = False
            if 'due' in task_item and task_item.get('due'):
                try:
                    due_dt_utc = datetime.datetime.fromisoformat(task_item['due'].replace('Z', '+00:00'))
                    if due_dt_utc < current_time_utc:
                        is_overdue = True
                except (ValueError, TypeError): pass
            parsed_task['display_status'] = 'ยังไม่ดำเนินการ' if is_overdue else 'รอดำเนินการ'
        tasks.append(parsed_task)

    tasks.sort(key=lambda x: x.get('created', ''), reverse=True)
    
    return render_template("tasks_summary.html", 
                           tasks=tasks, 
                           summary=total_summary_stats, # Pass total stats for the cards
                           search_query=search_query,
                           status_filter=status_filter) # Pass active filter for highlighting


@app.route('/update_task/<task_id>', methods=['GET', 'POST'])
def update_task_details(task_id):
    """Displays and handles updates for a single task, showing history."""
    service = get_google_tasks_service()
    if not service: abort(503, "Google Tasks service is unavailable.")

    try:
        task_raw = service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id).execute()
        task = parse_google_task_dates(task_raw)
        
        # Parse customer info and historical reports
        customer_info_from_task = parse_customer_info_from_notes(task.get('notes', ''))
        history, original_notes_text_removed_tech_reports = parse_tech_report_from_notes(task.get('notes', ''))
        
        # Populate for GET request
        # Ensure values are empty string if not found, instead of 'N/A'
        task['customer_name_initial'] = customer_info_from_task.get('name', '')
        task['customer_phone_initial'] = customer_info_from_task.get('phone', '')
        task['customer_address_initial'] = customer_info_from_task.get('address', '')
        task['customer_detail_initial'] = customer_info_from_task.get('detail', '')
        task['map_url_initial'] = customer_info_from_task.get('map_url', '')

        task['tech_reports_history'] = history
        # The notes_display is not directly used for parsing anymore, but can be for debugging/fallback display
        task['notes_display'] = original_notes_text_removed_tech_reports 
        
        if history and 'next_appointment' in history[0] and history[0]['next_appointment']:
            try:
                next_app_dt_utc = datetime.datetime.fromisoformat(history[0]['next_appointment'].replace('Z', '+00:00'))
                task['tech_next_appointment_datetime_local'] = next_app_dt_utc.astimezone(THAILAND_TZ).strftime("%Y-%m-%dT%H:%M")
            except (ValueError, TypeError): task['tech_next_appointment_datetime_local'] = ''
        else: task['tech_next_appointment_datetime_local'] = ''

    except HttpError: abort(404, "Task not found.")

    if request.method == 'POST':
        original_status = task.get('status')
        new_status = request.form.get('status')
        work_summary = request.form.get('work_summary', '').strip()
        equipment_used = request.form.get('equipment_used', '').strip()
        time_taken = request.form.get('time_taken', '').strip()
        next_appointment_date_str = request.form.get('next_appointment_date', '').strip()

        # New fields for task details update
        # Get values, defaulting to empty string if not provided
        updated_customer_name = request.form.get('customer_name', '').strip()
        updated_customer_phone = request.form.get('customer_phone', '').strip()
        updated_address = request.form.get('address', '').strip()
        updated_detail = request.form.get('detail', '').strip()
        updated_map_url = request.form.get('latitude_longitude', '').strip() # From the map URL input

        # --- Handle File Uploads from web form to Google Drive ---
        all_attachment_urls = []
        # Add existing attachments from all historical tech reports
        for report in task.get('tech_reports_history', []):
            all_attachment_urls.extend(report.get('attachment_urls', []))
        
        if 'files[]' in request.files:
            files = request.files.getlist('files[]')
            for file in files:
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    temp_filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    file.save(temp_filepath) # Save temporarily

                    # Guess MIME type if not provided by browser (e.g., for very old browsers)
                    mime_type = file.mimetype if file.mimetype else mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                    
                    drive_url = upload_file_to_google_drive(temp_filepath, filename, mime_type)
                    
                    if drive_url:
                        all_attachment_urls.append(drive_url)
                    else:
                        app.logger.error(f"Failed to upload {filename} to Google Drive.")
                    
                    os.remove(temp_filepath) # Clean up temporary file

        all_attachment_urls = list(set(all_attachment_urls)) # Remove duplicates

        # --- Prepare new tech report data ---
        next_appointment_gmt = None
        if new_status == 'needsAction' and next_appointment_date_str:
            try:
                next_app_dt_local = THAILAND_TZ.localize(datetime.datetime.fromisoformat(next_appointment_date_str))
                next_appointment_gmt = next_app_dt_local.astimezone(pytz.utc).isoformat()
            except ValueError: app.logger.error(f"Invalid next appointment date format: {next_appointment_date_str}")
        
        # Include current location in tech report if available (from JS)
        current_lat = request.form.get('current_lat')
        current_lon = request.form.get('current_lon')
        current_location_url = None
        if current_lat and current_lon:
            current_location_url = f"https://www.google.com/maps/search/?api=1&query={current_lat},{current_lon}"

        new_tech_report_data = {
            'summary_date': datetime.datetime.now(THAILAND_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            'work_summary': work_summary, 'equipment_used': equipment_used, 'time_taken': time_taken,
            'next_appointment': next_appointment_gmt, 'attachment_urls': all_attachment_urls,
            'location_url': current_location_url # Add location to this report
        }
        
        # --- Reconstruct Task Notes for main details and tech reports (line-by-line) ---
        # Get existing tech reports and the base notes content (without previous tech report blocks)
        history, _ = parse_tech_report_from_notes(task_raw.get('notes', ''))
        
        # Reconstruct the "static" part of notes with updated customer info, address, detail, and map URL
        # based on the new line-by-line format
        base_notes_lines = []
        base_notes_lines.append(updated_customer_name or '') # Line 1: Customer Name
        base_notes_lines.append(updated_customer_phone or '') # Line 2: Phone
        base_notes_lines.append(updated_address or '') # Line 3: Address
        
        if updated_map_url: # Line 4: Map URL (if present)
            base_notes_lines.append(updated_map_url)
        
        # Line 5 (or Line 4 if no map URL): Detail
        if updated_detail:
            base_notes_lines.extend(updated_detail.split('\n'))

        # Remove trailing empty strings
        while base_notes_lines and base_notes_lines[-1] == '':
            base_notes_lines.pop()

        updated_base_notes = "\n".join(base_notes_lines)

        # Append the new tech report to the history
        all_reports_list = sorted(history + [new_tech_report_data], key=lambda x: x.get('summary_date'))
        
        all_reports_text = ""
        for report in all_reports_list:
            # Ensure attachment_urls and location_url are empty lists/None if not present in report
            report_to_dump = report.copy()
            report_to_dump['attachment_urls'] = report_to_dump.get('attachment_urls', [])
            report_to_dump['location_url'] = report_to_dump.get('location_url', None)

            all_reports_text += f"\n\n--- TECH_REPORT_START ---\n{json.dumps(report_to_dump, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"
        
        # Combine base notes with all tech reports
        final_updated_notes = updated_base_notes + all_reports_text

        # Update Google Task with potentially new title, and the full reconstructed notes
        updated_task = update_google_task(
            task_id, 
            title=f"งานลูกค้า: {updated_customer_name or 'ไม่ระบุชื่อลูกค้า'} ({datetime.datetime.now(THAILAND_TZ).strftime('%d/%m/%y')})", # Use 'ไม่ระบุชื่อลูกค้า' if name is empty
            notes=final_updated_notes, 
            status=new_status
        )

        if updated_task:
            flash('อัปเดตงานเรียบร้อยแล้ว!', 'success')
            if new_status == 'completed' and original_status != 'completed':
                settings = get_app_settings()
                tech_group_id = settings['line_recipients'].get('technician_group_id')
                if tech_group_id:
                    check_for_nearby_jobs_and_notify(task_id, tech_group_id)
        else:
            flash('เกิดข้อผิดพลาดในการอัปเดตงาน', 'danger')

        return redirect(url_for('summary'))

    return render_template('update_task_details.html', task=task)
    
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Serves uploaded files. (Primarily for local temp storage / legacy direct links)"""
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

@app.route('/delete_task/<task_id>', methods=['POST'])
def delete_task(task_id):
    """Handles the deletion of a task."""
    if delete_google_task(task_id):
        flash('ลบรายการงานเรียบร้อยแล้ว', 'success')
    else:
        flash('เกิดข้อผิดพลาดในการลบงาน', 'danger')
    return redirect(url_for('summary'))

# --- Cron Job Endpoint ---
@app.route('/trigger_daily_reports')
def trigger_daily_reports():
    """Endpoint for Cron Job to send daily reports."""
    app.logger.info("Cron job triggered for daily reports.")
    settings = get_app_settings()
    now_thai = datetime.datetime.now(THAILAND_TZ)
    current_hour = now_thai.hour

    appointment_hour = settings.get('report_times', {}).get('appointment_reminder_hour_thai', 7)
    summary_hour = settings.get('report_times', {}).get('outstanding_report_hour_thai', 20)

    tasks_to_process = get_google_tasks_for_report(show_completed=False)
    if tasks_to_process is None:
        return "Failed to get tasks from Google API", 500

    messages_to_send = []
    recipients = []
    
    # Check for morning appointment reminders
    if current_hour == appointment_hour:
        app.logger.info("Processing daily APPOINTMENT reminders.")
        today_appointments = []
        for task in tasks_to_process:
            if 'due' in task and task['due']:
                 try:
                    dt_utc = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                    dt_thai = dt_utc.astimezone(THAILAND_TZ) # Fixed: dt_utc should be used here
                    if dt_thai.date() == now_thai.date(): # Fixed: dt_thai should be used here
                        today_appointments.append(task)
                 except (ValueError, TypeError): continue
        
        if today_appointments:
            today_appointments.sort(key=lambda x: x.get('due', ''))
            messages_to_send = [create_task_flex_message(task) for task in today_appointments]
            recipients = [id for id in [settings['line_recipients'].get('technician_group_id'), settings['line_recipients'].get('admin_group_id')] if id]

    # Check for evening outstanding summary
    elif current_hour == summary_hour:
        app.logger.info("Processing daily OUTSTANDING tasks summary.")
        if tasks_to_process:
            tasks_to_process.sort(key=lambda x: x.get('due', '9999-99-99'))
            message_lines = ["--- 🌙 สรุปงานค้าง ---"]
            for task in tasks_to_process:
                info = parse_customer_info_from_notes(task.get('notes', ''))
                # Changed to use info.get() with empty string and then 'or -' for display
                message_lines.append(f"ลูกค้า: {info.get('name', '') or '-'}\nโทร: {info.get('phone', '') or '-'}\nงาน: {task.get('title')}") 
            messages_to_send = [TextMessage(text="\n\n".join(message_lines))]
            recipients = [id for id in [settings['line_recipients'].get('manager_user_id'), settings['line_recipients'].get('admin_group_id')] if id]

    if messages_to_send and recipients:
        # Push up to 5 messages at a time
        # Handle multiple recipients if recipients is a list
        if isinstance(recipients, list):
            for recipient_id in recipients:
                for i in range(0, len(messages_to_send), 5):
                    line_messaging_api.push_message(PushMessageRequest(to=recipient_id, messages=messages_to_send[i:i+5]))
        else: # Single recipient
            for i in range(0, len(messages_to_send), 5):
                line_messaging_api.push_message(PushMessageRequest(to=recipients, messages=messages_to_send[i:i+5]))
        
        return f"{len(messages_to_send)} messages sent to {len(recipients)} recipients.", 200

    return "No report scheduled or no recipients for this hour.", 200

# --- Main Execution ---

# --- Helper to find tasks by customer info ---
def find_tasks_by_customer_info(query_text):
    """
    Finds tasks matching a given query (customer name or phone number).
    Returns a list of matching tasks.
    """
    all_tasks = get_google_tasks_for_report(show_completed=True)
    if all_tasks is None:
        return []

    query_lower = query_text.lower().strip()
    matching_tasks = []

    for task in all_tasks:
        customer_info = parse_customer_info_from_notes(task.get('notes', ''))
        customer_name_lower = customer_info.get('name', '').lower()
        customer_phone = customer_info.get('phone', '')

        if query_lower in customer_name_lower or query_lower == customer_phone:
            matching_tasks.append(task)
    
    # Sort by created date, newest first
    matching_tasks.sort(key=lambda x: x.get('created', ''), reverse=True)
    return matching_tasks

# --- LINE Text Message Handler Example ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text
    reply_token = event.reply_token
    source_type = event.source.type
    source_id = event.source.group_id if isinstance(event.source, GroupSource) else event.source.user_id

    app.logger.info(f"Received LINE message event from source type: {source_type}")
    app.logger.info(f"Source ID: {source_id}")
    app.logger.info(f"Message type: {event.message.type}")
    app.logger.info(f"Message text (original): {user_text}")

    text_lower = user_text.strip().lower()
    
    # Handle file/image messages first (if any were missed before, though handler.add already splits)
    # This block is mostly for completeness if this handler were not limited to TextMessageContent
    if isinstance(event.message, ImageMessageContent) or isinstance(event.message, FileMessageContent):
        message_content_id = event.message.id
        mime_type = event.message.content_type if hasattr(event.message, 'content_type') else 'application/octet-stream'
        file_extension = message_content_id # Placeholder, should derive properly

        if isinstance(event.message, ImageMessageContent):
            file_extension = mime_type.split('/')[-1] if '/' in mime_type else 'jpg'
        elif isinstance(event.message, FileMessageContent) and event.message.file_name:
            file_extension = event.message.file_name.split('.')[-1].lower()

        filename = secure_filename(f"{message_content_id}.{file_extension}")
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)

        try:
            message_content = line_messaging_api.get_message_content(message_content_id)
            with open(filepath, 'wb') as fd:
                for chunk in message_content.iter_content():
                    fd.write(chunk)
            
            drive_url = upload_file_to_google_drive(filepath, filename, mime_type)
            os.remove(filepath)

            if drive_url:
                reply_to_line(reply_token, [TextMessage(text=f"ได้รับไฟล์แล้วและอัปโหลดขึ้น Google Drive เรียบร้อย:\n{drive_url}\nหากต้องการแนบไฟล์นี้กับงานใด โปรดไปที่หน้าอัปเดตงานและเพิ่มลิงก์ด้วยตนเอง หรือแจ้ง ID งานมาหากคุณกำลังอัปเดตงานอยู่ครับ")])
            else:
                reply_to_line(reply_token, [TextMessage(text="เกิดข้อผิดพลาดในการอัปโหลดไฟล์ไปยัง Google Drive")])
        except Exception as e:
            app.logger.error(f"Failed to get message content or save/upload file from LINE: {e}")
            reply_to_line(reply_token, [TextMessage(text="เกิดข้อผิดพลาดในการรับไฟล์")])
        return


    # --- Command Dispatcher ---
    if text_lower == 'นัดหมาย':
        app.logger.info("Command matched: นัดหมาย")
        liff_url = f"https://liff.line.me/{LIFF_ID_FORM}"
        reply_to_line(reply_token, [TextMessage(text="กรุณากดปุ่มด้านล่างเพื่อเปิดฟอร์มสร้างงานและนัดหมายครับ",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=URIAction(label="➕ สร้างงานใหม่", uri=liff_url))
            ]))])
        return

    # 'งาน <ชื่อหรือเบอร์>'
    if text_lower.startswith('งาน '):
        app.logger.info("Command matched: งาน <ชื่อหรือเบอร์>")
        query_text = user_text[len('งาน '):].strip()
        matching_tasks = find_tasks_by_customer_info(query_text)

        if not matching_tasks:
            liff_url = f"https://liff.line.me/{LIFF_ID_FORM}"
            reply_to_line(reply_token, [TextMessage(text=f"ไม่พบงานสำหรับ '{query_text}' ในระบบ ต้องการสร้างงานใหม่เลยไหม?",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=URIAction(label=f"➕ สร้างงาน '{query_text}'", uri=liff_url)) # LIFF can pre-fill based on URL params if implemented
                ]))])
            return
        elif len(matching_tasks) == 1:
            task = matching_tasks[0]
            flex_message = create_task_flex_message(task)
            reply_to_line(reply_token, [TextMessage(text=f"พบงาน 1 รายการสำหรับ '{query_text}':"), flex_message])
            return
        else:
            carousel = create_customer_history_carousel(matching_tasks, query_text)
            alt_text = f"พบหลายงานสำหรับ '{query_text}'"
            flex_message = FlexMessage(alt_text=alt_text, contents=carousel)
            reply_to_line(reply_token, [TextMessage(text=f"พบหลายงานสำหรับ '{query_text}' กรุณาเลือกดู หรือใช้ Task ID ที่แน่นอน:"), flex_message])
            return

    # 'ดูงาน <ID/ชื่อ/เบอร์>'
    if text_lower.startswith('ดูงาน '):
        app.logger.info("Command matched: ดูงาน <ID/ชื่อ/เบอร์>")
        query_text = user_text[len('ดูงาน '):].strip()
        
        task = None
        # Try to find by Task ID first (exact match)
        if len(query_text) > 5 and not (' ' in query_text or re.search(r'\D', query_text)): # Heuristic for potential ID
            task = get_single_task(query_text)
        
        if task:
            flex_message = create_task_flex_message(task)
            reply_to_line(reply_token, [flex_message])
            return
        else: # Try to find by customer info (name/phone)
            matching_tasks = find_tasks_by_customer_info(query_text)
            if not matching_tasks:
                reply_to_line(reply_token, [TextMessage(text=f"ไม่พบงานสำหรับ '{query_text}' ในระบบ")])
                return
            elif len(matching_tasks) == 1:
                flex_message = create_task_flex_message(matching_tasks[0])
                reply_to_line(reply_token, [TextMessage(text=f"พบงาน 1 รายการสำหรับ '{query_text}':"), flex_message])
                return
            else:
                carousel = create_customer_history_carousel(matching_tasks, query_text)
                alt_text = f"พบหลายงานสำหรับ '{query_text}'"
                flex_message = FlexMessage(alt_text=alt_text, contents=carousel)
                reply_to_line(reply_token, [TextMessage(text=f"พบหลายงานสำหรับ '{query_text}' กรุณาเลือกดู หรือใช้ Task ID ที่แน่นอน:"), flex_message])
                return

    # 'เสร็จงาน <ID/ชื่อ/เบอร์>'
    if text_lower.startswith('เสร็จงาน '):
        app.logger.info("Command matched: เสร็จงาน <ID/ชื่อ/เบอร์>")
        query_text = user_text[len('เสร็จงาน '):].strip()
        
        task_to_complete = None
        # Try to find by Task ID first (exact match)
        if len(query_text) > 5 and not (' ' in query_text or re.search(r'\D', query_text)):
            task_to_complete = get_single_task(query_text)
        
        if task_to_complete:
            # Call original complete logic
            updated_task = update_google_task(task_to_complete.get('id'), notes=task_to_complete.get('notes', ''), status='completed')
            if updated_task:
                reply_to_line(reply_token, [TextMessage(text=f"✅ ปิดงาน '{updated_task.get('title')}' เรียบร้อยแล้ว")])
                check_for_nearby_jobs_and_notify(updated_task.get('id'), source_id)
            else:
                reply_to_line(reply_token, [TextMessage(text=f"❌ ไม่สามารถปิดงาน '{query_text}' ได้")])
            return
        else: # Try to find by customer info (name/phone)
            matching_tasks = find_tasks_by_customer_info(query_text)
            if not matching_tasks:
                reply_to_line(reply_token, [TextMessage(text=f"ไม่พบงานสำหรับ '{query_text}' ในระบบ")])
                return
            elif len(matching_tasks) == 1:
                task_to_complete = matching_tasks[0]
                updated_task = update_google_task(task_to_complete.get('id'), notes=task_to_complete.get('notes', ''), status='completed')
                if updated_task:
                    reply_to_line(reply_token, [TextMessage(text=f"✅ ปิดงาน '{updated_task.get('title')}' เรียบร้อยแล้ว")])
                    check_for_nearby_jobs_and_notify(updated_task.get('id'), source_id)
                else:
                    reply_to_line(reply_token, [TextMessage(text=f"❌ ไม่สามารถปิดงาน '{query_text}' ได้")])
                return
            else:
                messages = [TextMessage(text=f"พบหลายงานสำหรับ '{query_text}' กรุณาระบุให้ชัดเจนขึ้น หรือใช้ Task ID:")]
                for task_item in matching_tasks[:3]: # Limit to top 3 suggestions
                    messages.append(TextMessage(text=f"งาน: {task_item.get('title')}\nID: {task_item.get('id')}\nสถานะ: {task_item.get('status')}\nใช้ 'เสร็จงาน {task_item.get('id')}' เพื่อปิดงาน"))
                reply_to_line(reply_token, messages)
                return

    # 'c <ชื่อลูกค้า>'
    if text_lower.startswith('c '):
        app.logger.info("Command matched: c <ชื่อลูกค้า>")
        parts = user_text.split(maxsplit=1)
        if len(parts) > 1: handle_customer_search_command(event, parts[1])
        return

    # Direct Commands without parameters
    if text_lower in COMMANDS:
        app.logger.info(f"Command matched: {text_lower}")
        COMMANDS[text_lower](event)
        return

    app.logger.info(f"No command matched for text: {text_lower}")
    # Optional: Reply with help message if no command is matched
    # reply_to_line(reply_token, [TextMessage(text="ไม่เข้าใจคำสั่งของคุณครับ พิมพ์ 'comphone' เพื่อดูวิธีใช้งาน")])


if __name__ == '__main__':
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    # The port Render.com expects your app to listen on
    # It will set an environment variable PORT, so we should use it.
    # Default to 8080 if not set (e.g., when running locally)
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)

