from dotenv import load_dotenv
load_dotenv()

import os
import sys
import datetime
import re
import json
from flask import Flask, request, render_template, redirect, url_for, abort, send_from_directory

# For secure filename handling and file uploads
from werkzeug.utils import secure_filename

# LINE Messaging API (Using v3 for best practice and to resolve deprecation warnings)
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, PushMessageRequest, TextMessage, ReplyMessageRequest
from linebot.v3.webhooks import WebhookHandler # Corrected: Changed from WebhookParser to WebhookHandler
from linebot.exceptions import InvalidSignatureError

# Google Tasks API
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Initialize Flask App
app = Flask(__name__)

# --- Configuration ---
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf'}

# Create upload folder if it doesn't exist
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# LINE Bot API Configuration - Get from Environment Variables for security
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')

# LINE Recipient IDs - Get from Environment Variables
# IMPORTANT: Ensure these are set correctly on Render and/or in your local .env file
# หากไม่มีค่าใน Environment Variable จะเป็น None ซึ่งฟังก์ชัน send_message_to_recipients จะข้ามไป
LINE_ADMIN_GROUP_ID = os.environ.get('LINE_ADMIN_GROUP_ID')
LINE_MANAGER_USER_ID = os.environ.get('LINE_MANAGER_USER_ID')
LINE_HR_GROUP_ID = os.environ.get('LINE_HR_GROUP_ID')
LINE_TECHNICIAN_GROUP_ID = os.environ.get('LINE_TECHNICIAN_GROUP_ID')

# Initialize LINE Messaging API v3
# Configuration for LINE API client
line_configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_messaging_api = MessagingApi(ApiClient(line_configuration)) # Use line_messaging_api for push/reply messages
handler = WebhookHandler(LINE_CHANNEL_SECRET) # Corrected: Adjusted to use WebhookHandler

# Google Tasks API Configuration
SCOPES = ['https://www.googleapis.com/auth/tasks']
# For Render deployment, 'credentials.json' should be created from GOOGLE_CREDENTIALS_JSON env var
GOOGLE_CREDENTIALS_FILE_NAME = 'credentials.json'

# --- Helper Functions ---

def allowed_file(filename):
    """Checks if the file extension is allowed."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- New Helper Function for Parsing Google Task Dates (Moved to top for scope) ---
def parse_google_task_dates(task):
    """Helper to parse and format dates from Google Task API response."""
    parsed_task = task.copy()
    for key in ['created', 'updated', 'completed', 'due']:
        if key in parsed_task and parsed_task[key]:
            try:
                # Google Tasks dates are ISO 8601, often with 'Z' for UTC
                dt_obj = datetime.datetime.fromisoformat(parsed_task[key].replace('Z', '+00:00'))
                # Format to a readable string (e.g., "YYYY-MM-DD HH:MM:SS")
                parsed_task[f'{key}_formatted'] = dt_obj.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                parsed_task[f'{key}_formatted'] = "N/A"
        else:
            parsed_task[f'{key}_formatted'] = "N/A"
    return parsed_task

# --- End New Helper Function ---


def get_google_tasks_service():
    """
    Authenticates with Google Tasks API.
    Prioritizes loading token from GOOGLE_TOKEN_JSON env var for Render.
    Falls back to local token.json or initiates OAuth flow using credentials.json (or GOOGLE_CREDENTIALS_JSON env var).
    """
    creds = None

    # 1. Try to load token from environment variable (for Render deployment)
    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')
    if google_token_json_str:
        try:
            creds_info = json.loads(google_token_json_str)
            creds = Credentials.from_authorized_user_info(creds_info, SCOPES)
            app.logger.info("Google token loaded from GOOGLE_TOKEN_JSON env var.")
        except Exception as e:
            app.logger.warning(f"Could not load token from GOOGLE_TOKEN_JSON env var: {e}. Attempting other methods.")
            creds = None

    # 2. If not loaded from env var, try to load from local token.json (for local development)
    if not creds and os.path.exists('token.json'):
        try:
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
            app.logger.info("Google token loaded from local token.json.")
        except Exception as e:
            app.logger.warning(f"Could not load token from local token.json: {e}. Attempting re-authentication.")
            creds = None

    # 3. If no valid credentials, try to refresh or initiate new OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                app.logger.info("Google token refreshed.")
            except Exception as e:
                app.logger.error(f"Error refreshing Google token: {e}. Will attempt full re-authentication.")
                creds = None
        else:
            # Create credentials.json from GOOGLE_CREDENTIALS_JSON env var if it exists
            if not os.path.exists(GOOGLE_CREDENTIALS_FILE_NAME):
                google_credentials_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
                if google_credentials_json:
                    try:
                        with open(GOOGLE_CREDENTIALS_FILE_NAME, 'w') as f:
                            f.write(google_credentials_json)
                        app.logger.info(f"Created {GOOGLE_CREDENTIALS_FILE_NAME} from env var.")
                    except Exception as e:
                        app.logger.error(f"Error creating {GOOGLE_CREDENTIALS_FILE_NAME} from env var: {e}")
                        return None
                else:
                    app.logger.error(f"Google credentials file not found: {GOOGLE_CREDENTIALS_FILE_NAME} and GOOGLE_CREDENTIALS_JSON env var is not set.")
                    return None
            
            try:
                flow = InstalledAppFlow.from_client_secrets_file(
                    GOOGLE_CREDENTIALS_FILE_NAME, SCOPES)
                # For local development, run_local_server opens browser for auth
                # Render deployment relies on GOOGLE_TOKEN_JSON env var being manually updated.
                creds = flow.run_local_server(port=0) 
                app.logger.info("Google OAuth flow completed locally.")
            except Exception as e:
                app.logger.error(f"Error during Google OAuth flow: {e}. Ensure {GOOGLE_CREDENTIALS_FILE_NAME} is valid.")
                return None
            
            # Save the new token locally for future use (if on local dev)
            if creds and not os.environ.get('GOOGLE_TOKEN_JSON'): # Only save if not using env var for token
                try:
                    with open('token.json', 'w') as token:
                        token.write(creds.to_json())
                    app.logger.info("Local token.json saved.")
                except Exception as e:
                    app.logger.error(f"Error saving local token.json: {e}")

    if creds:
        return build('tasks', 'v1', credentials=creds)
    return None

def create_google_task(title, notes=None, due=None):
    """Creates a new task in Google Tasks."""
    service = get_google_tasks_service()
    if not service:
        app.logger.error("Failed to get Google Tasks service for creation.")
        return None
    try:
        task_list_id = '@default' # Default task list
        task_body = {
            'title': title,
            'notes': notes,
            'status': 'needsAction'
        }
        if due:
            task_body['due'] = due

        result = service.tasks().insert(tasklist=task_list_id, body=task_body).execute()
        app.logger.info(f"Google Task created: {result.get('title')} (ID: {result.get('id')})")
        return result
    except HttpError as err:
        app.logger.error(f"Error creating Google Task: {err}")
        return None

def update_google_task(task_id, title=None, notes=None, due=None, status=None):
    """Updates an existing task in Google Tasks."""
    service = get_google_tasks_service()
    if not service:
        app.logger.error("Failed to get Google Tasks service for update.")
        return None
    try:
        task_list_id = '@default'
        current_task = service.tasks().get(tasklist=task_list_id, task=task_id).execute()
        
        if title:
            current_task['title'] = title
        if notes is not None:
            current_task['notes'] = notes
        if due:
            current_task['due'] = due
        
        if status:
            current_task['status'] = status
            if status == 'completed':
                current_task['completed'] = datetime.datetime.now().isoformat() + 'Z'
            elif 'completed' in current_task:
                del current_task['completed']

        result = service.tasks().update(tasklist=task_list_id, task=task_id, body=current_task).execute()
        app.logger.info(f"Google Task {task_id} updated. New Status: {result.get('status')}")
        return result
    except HttpError as err:
        app.logger.error(f"Error updating Google Task {task_id}: {err}")
        return None

def get_google_tasks_for_report(show_completed=False, due_min=None, due_max=None):
    """Fetches tasks from Google Tasks for reporting purposes."""
    service = get_google_tasks_service()
    if not service:
        app.logger.error("Failed to get Google Tasks service for report.")
        return []
    try:
        task_list_id = '@default'
        results = service.tasks().list(
            tasklist=task_list_id,
            showCompleted=show_completed,
            dueMin=due_min,
            dueMax=due_max
        ).execute()
        return results.get('items', [])
    except HttpError as err:
        app.logger.error(f"Error getting Google Tasks for report: {err}")
        return []

def get_daily_outstanding_tasks():
    """Gets tasks due by end of today that are not completed."""
    today_end = datetime.datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
    outstanding_tasks = get_google_tasks_for_report(
        show_completed=False, # Only uncompleted tasks
        due_max=today_end.isoformat(timespec='milliseconds') + "Z"
    )
    return outstanding_tasks

def get_daily_summary_tasks():
    """Gets tasks created or completed today."""
    today_start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = datetime.datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)

    all_tasks = get_google_tasks_for_report(show_completed=True) # Fetch all to filter by created/completed date

    daily_tasks = []
    for task in all_tasks:
        created_dt = None
        completed_dt = None

        if 'created' in task:
            try:
                created_dt = datetime.datetime.fromisoformat(task['created'].replace('Z', '+00:00'))
            except ValueError:
                pass
        
        if 'completed' in task:
            try:
                completed_dt = datetime.datetime.fromisoformat(task['completed'].replace('Z', '+00:00'))
            except ValueError:
                pass

        if (created_dt and today_start <= created_dt <= today_end) or \
           (completed_dt and today_start <= completed_dt <= today_end):
            daily_tasks.append(task)
            
    return daily_tasks

def send_message_to_recipients(message_object, recipient_ids):
    """
    Sends a LINE message (TextSendMessage object) to a list of LINE User IDs or Group IDs.
    Uses line_messaging_api from Line SDK v3.
    :param message_object: The TextSendMessage object.
    :param recipient_ids: A list of User IDs or Group IDs (strings).
    """
    for recipient_id in recipient_ids:
        # แก้ไขตรงนี้ให้ตรวจสอบแค่ว่า recipient_id มีค่า (ไม่เป็น None หรือสตริงว่าง)
        if recipient_id:
            try:
                # ใช้ line_messaging_api.push_message แทน line_bot_api.push_message (v3)
                line_messaging_api.push_message(
                    PushMessageRequest(
                        to=recipient_id,
                        messages=[message_object] # messages ต้องเป็น list
                    )
                )
                app.logger.info(f"Message sent to LINE recipient: {recipient_id}")
            except Exception as e:
                app.logger.error(f"Failed to send message to LINE recipient {recipient_id}: {e}")
        else:
            app.logger.warning(f"Skipping message send to empty recipient ID: {recipient_id}") # <--- อัปเดตข้อความแจ้งเตือน

def send_daily_reports():
    """
    Function to be called by a Render Cron Job at specific times (e.g., 6 AM and 8 PM).
    Determines which report to send based on current hour.
    """
    current_hour_utc = datetime.datetime.now(datetime.timezone.utc).hour # Render uses UTC
    
    # Adjust hour for Thai timezone (UTC+7)
    # If Render is at UTC, 6:00 AM (Thai) = 23:00 PM (UTC of previous day)
    # If Render is at UTC, 8:00 PM (Thai) = 13:00 PM (UTC)

    # Convert current_hour_utc to Thai local hour for logic
    current_hour_thai = (current_hour_utc + 7) % 24 
    app.logger.info(f"Cron job triggered. Current UTC hour: {current_hour_utc}, Thai local hour: {current_hour_thai}")

    report_message_text = ""

    # Report outstanding tasks at 6:00 AM Thai time
    if current_hour_thai == 6:
        outstanding_tasks = get_daily_outstanding_tasks()
        report_message_text = "--- รายงานงานค้างประจำวัน ---\n"
        if outstanding_tasks:
            for task in outstanding_tasks:
                title = task.get('title', 'N/A')
                due_date_str = "N/A"
                if 'due' in task:
                    try:
                        due_date_dt = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                        due_date_str = due_date_dt.strftime("%Y-%m-%d %H:%M")
                    except ValueError:
                        pass
                report_message_text += f"- {title} (Due: {due_date_str})\n"
        else:
            report_message_text += "ไม่มีงานค้างในวันนี้\n"
        
        app.logger.info(f"Preparing daily outstanding tasks report.")
        recipients = [LINE_ADMIN_GROUP_ID, LINE_MANAGER_USER_ID] # Send to Admin Group and Manager
        send_message_to_recipients(TextMessage(text=report_message_text), recipients)
        # Note: TextSendMessage is deprecated in v3, use TextMessage directly

    # Summarize daily tasks at 8:00 PM Thai time
    elif current_hour_thai == 20:
        summary_tasks = get_daily_summary_tasks()
        report_message_text = "--- สรุปงานประจำวัน ---\n"
        if summary_tasks:
            for task in summary_tasks:
                title = task.get('title', 'N/A')
                status = task.get('status', 'unknown')
                report_message_text += f"- {title} (สถานะ: {'เสร็จสิ้น' if status == 'completed' else 'ค้าง'})\n"
        else:
            report_message_text += "ไม่มีงานที่ถูกสร้างหรือเสร็จสิ้นในวันนี้\n"
        
        app.logger.info(f"Preparing daily summary tasks report.")
        recipients = [LINE_ADMIN_GROUP_ID, LINE_HR_GROUP_ID] # Send to Admin Group and HR Group
        send_message_to_recipients(TextMessage(text=report_message_text), recipients)
        # Note: TextSendMessage is deprecated in v3, use TextMessage directly
    else:
        app.logger.info(f"No report scheduled for Thai hour {current_hour_thai}.")


# --- Flask Routes ---

@app.route('/', methods=['GET', 'POST'])
def form():
    """Handles the task creation form submission."""
    if request.method == 'POST':
        topic = request.form.get('topic')
        customer = request.form.get('customer')
        phone = request.form.get('phone')
        address = request.form.get('address')
        appointment = request.form.get('appointment')
        latitude = request.form.get('latitude')
        longitude = request.form.get('longitude')
        detail = request.form.get('detail')
        task_status = "PENDING" # Initial status

        # Handle multiple file uploads
        file_urls = []
        files = request.files.getlist('attachments')
        for file in files:
            if file and allowed_file(file.filename):
                timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
                original_filename = secure_filename(file.filename)
                unique_filename = f"{timestamp}_{original_filename}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
                file.save(filepath)
                file_urls.append(os.path.join('static', 'uploads', unique_filename)) 
        
        file_urls_str = ",".join(file_urls) # Store as comma-separated string in log file

        coord = f"{latitude},{longitude}" if latitude and longitude else ""

        # Save to tasks_log.txt
        try:
            with open("tasks_log.txt", "a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now()}|{topic}|{customer}|{phone}|{address}|{appointment}|{coord}|{detail}|{file_urls_str}|{task_status}\n")
            app.logger.info("Task logged to tasks_log.txt.")
        except IOError as e:
            app.logger.error(f"Error writing to tasks_log.txt: {e}")

        # Create Google Task
        if topic:
            task_title = f"{topic} ({customer})" if customer else topic
            
            google_task_notes = f"โทร: {phone or '-'}\nที่อยู่: {address or '-'}\nรายละเอียด: {detail or '-'}"
            if appointment:
                google_task_notes += f"\nนัดหมาย: {appointment}"
            if coord and coord != ',':
                # แก้ไข URL Google Maps
                google_task_notes += f"\nพิกัด: https://www.google.com/maps/search/?api=1&query={latitude},{longitude}"
            if file_urls:
                full_file_urls = []
                for f_url in file_urls:
                    # Generate full external URLs for Google Tasks notes
                    # _external=True requires the app to know its public URL (Flask handles this implicitly on Render)
                    full_file_urls.append(url_for('static', filename=f_url.replace('static/', ''), _external=True))
                google_task_notes += f"\nไฟล์แนบ: {', '.join(full_file_urls)}"

            due_date_gmt = None
            if appointment:
                try:
                    dt_obj = datetime.datetime.strptime(appointment, "%Y-%m-%d %H:%M")
                    due_date_gmt = dt_obj.isoformat() + "Z"
                except ValueError:
                    app.logger.warning(f"Could not parse appointment date for Google Task: {appointment}")

            created_task = create_google_task(task_title, google_task_notes, due=due_date_gmt)
            
            # --- ส่วนนี้เพื่อส่ง LINE Notification หลังสร้าง Task จาก Web Form ---
            if created_task:
                new_task_notification_text = (
                    f"งานใหม่ถูกสร้างจากเว็บฟอร์ม: {topic}\n"
                    f"ลูกค้า: {customer}\n"
                    f"โทร: {phone}\n"
                    f"ที่อยู่: {address}\n"
                    f"นัดหมาย: {appointment or '-'}\n"
                    f"พิกัด: {('https://www.google.com/maps/search/?api=1&query=' + latitude + ',' + longitude) if latitude and longitude else '-'}\n"
                    f"รายละเอียด: {detail or '-'}\n"
                    f"ID สำหรับสรุปงาน: {created_task.get('id')}\n"
                    f"(ใช้คำสั่ง 'complete {created_task.get('id')}: สรุป | อุปกรณ์ | เวลา')"
                )
                
                # กำหนดผู้รับ: LINE_ADMIN_GROUP_ID, LINE_TECHNICIAN_GROUP_ID
                recipients_for_new_web_task = [LINE_ADMIN_GROUP_ID, LINE_TECHNICIAN_GROUP_ID]
                send_message_to_recipients(TextMessage(text=new_task_notification_text), recipients_for_new_web_task)
            # --- จบส่วนที่เพิ่ม ---

        return redirect(url_for('summary'))

    return render_template('form.html')

# --- Flask Route for Summary Page (Updated to fetch from Google Tasks) ---
@app.route('/summary')
def summary():
    """Displays a summary of tasks fetched directly from Google Tasks,
    along with calculated statistics."""
    # ดึงงานทั้งหมดจาก Google Tasks (รวมงานที่เสร็จสิ้นแล้วด้วย)
    tasks_raw = get_google_tasks_for_report(show_completed=True) 
    
    tasks = []
    task_status_counts = {
        'needsAction': 0,
        'completed': 0,
        'overdue': 0,
        'total': 0
    }
    
    current_time_utc = datetime.datetime.now(datetime.timezone.utc)

    for task_item in tasks_raw:
        task_status_counts['total'] += 1
        
        # แปลงและจัดรูปแบบวันที่จาก Google Task API
        parsed_task = parse_google_task_dates(task_item)
        
        # กำหนดสถานะการแสดงผลและนับจำนวนงาน
        status = parsed_task.get('status', 'unknown')
        parsed_task['display_status'] = 'รอดำเนินการ' # Default display status
        
        if status == 'completed':
            parsed_task['display_status'] = 'เสร็จสิ้น'
            task_status_counts['completed'] += 1
        elif status == 'needsAction':
            task_status_counts['needsAction'] += 1
            # ตรวจสอบงานที่ค้างชำระ (Overdue)
            if 'due' in parsed_task and parsed_task['due'] and parsed_task['due_formatted'] != 'N/A':
                try:
                    # ต้องแปลงเป็น UTC ก่อนเปรียบเทียบ
                    due_dt = datetime.datetime.fromisoformat(parsed_task['due'].replace('Z', '+00:00'))
                    if due_dt < current_time_utc:
                        parsed_task['display_status'] = 'ค้างชำระ' # Overdue
                        task_status_counts['overdue'] += 1
                except ValueError:
                    pass # ไม่สนใจวันที่ที่ไม่สามารถแยกวิเคราะห์ได้
        
        # แยกข้อมูลสรุปงานจากช่างจากช่อง 'notes' (ถ้ามี)
        notes = parsed_task.get('notes', '')
        # Regex เพื่อค้นหาสรุปงานจากช่างใน notes
        summary_match = re.search(
            r"--- สรุปงานโดยช่าง \((.*?)\) ---\n"
            r"สรุปผลการทำงาน: (.*?)\n"
            r"รายการอุปกรณ์ที่ใช้: (.*?)\n"
            r"ระยะเวลาที่ทำเสร็จ: (.*?)\n",
            notes, re.DOTALL
        )
        if summary_match:
            parsed_task['tech_summary_date'] = summary_match.group(1)
            parsed_task['tech_work_summary'] = summary_match.group(2)
            parsed_task['tech_equipment_used'] = summary_match.group(3)
            parsed_task['tech_time_taken'] = summary_match.group(4)
        else:
            # กำหนดค่าเริ่มต้นเป็น None หากไม่พบข้อมูลสรุปช่าง
            parsed_task['tech_work_summary'] = None
            parsed_task['tech_equipment_used'] = None
            parsed_task['tech_time_taken'] = None

        # แยก URL ของไฟล์แนบจาก notes (หากมี)
        # ตรวจสอบว่าคำว่า "ไฟล์แนบ:" มีอยู่จริงใน notes ก่อนจะพยายามแยก
        file_urls_match = re.search(r"ไฟล์แนบ: (.*)", notes)
        if file_urls_match:
            # ใช้ list comprehension เพื่อ strip() แต่ละ URL ก่อนเก็บ
            parsed_task['attachment_urls'] = [url.strip() for url in file_urls_match.group(1).split(',')]
        else:
            parsed_task['attachment_urls'] = []


        tasks.append(parsed_task)

    # จัดเรียงงานตามวันที่สร้าง (งานใหม่สุดอยู่บนสุด)
    # ใช้ created_formatted ซึ่งเป็น string ที่จัดรูปแบบแล้ว ทำให้เรียงได้ง่ายขึ้น
    tasks.sort(key=lambda x: x.get('created_formatted', '0000-00-00 00:00:00'), reverse=True)
    
    # คำนวณเปอร์เซ็นต์สำหรับแสดงผล
    total_tasks = task_status_counts['total']
    if total_tasks > 0:
        task_status_counts['completed_percent'] = round((task_status_counts['completed'] / total_tasks) * 100, 2)
        task_status_counts['needsAction_percent'] = round((task_status_counts['needsAction'] / total_tasks) * 100, 2)
        task_status_counts['overdue_percent'] = round((task_status_counts['overdue'] / total_tasks) * 100, 2)
    else:
        task_status_counts['completed_percent'] = 0
        task_status_counts['needsAction_percent'] = 0
        task_status_counts['overdue_percent'] = 0

    return render_template("tasks_summary.html", tasks=tasks, summary=task_status_counts)
# --- End Flask Route for Summary Page ---

@app.route("/callback", methods=['POST'])
def callback():
    """Handles LINE webhook events."""
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        # handler.handle(body, signature)
        # WebhookHandler can use handler.handle(body, signature) if you have the appropriate @handler.add_message_event decorators.
        # Ensure 'MessageEvent' and 'TextMessage' are imported from linebot.v3.webhooks if you use them directly.
        handler.handle(body, signature) # Using handle method directly with WebhookHandler

    except InvalidSignatureError:
        app.logger.error("Invalid signature. Check channel access token/channel secret.")
        abort(400)
    except Exception as e:
        app.logger.error(f"Unhandled exception in LINE webhook handler: {e}")
        abort(500)

    return 'OK'

# The decorators for WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessage as LineTextMessage # Renamed to avoid conflict with Flask's TextMessage
from linebot.v3.messaging import MessageAction, PostbackAction # Import necessary actions if used elsewhere

@handler.add(MessageEvent, message=LineTextMessage) # Use LineTextMessage to avoid conflict
def handle_message(event):
    """Processes incoming LINE text messages."""
    text_message = event.message.text
    app.logger.info(f"Received LINE message: {text_message}")

    # --- Debugging: Log Source ID (for getting recipient IDs) ---
    if event.source.type == 'group':
        app.logger.info(f"Message from Group ID: {event.source.group_id}")
        print(f"!!! Group ID: {event.source.group_id} !!!") # Prints to Render logs
    elif event.source.type == 'user':
        app.logger.info(f"Message from User ID: {event.source.user_id}")
        print(f"!!! User ID: {event.source.user_id} !!!") # Prints to Render logs
    # --- End Debugging Block ---

    # Command to create a new task:
    # Format: task:หัวข้อ|ลูกค้า|เบอร์โทร|ที่อยู่|วันเวลา_นัดหมาย(YYYY-MM-DD HH:MM)|ละติจูด,ลองจิจูด|รายละเอียด
    if text_message.lower().startswith("task:"):
        try:
            parts_str = text_message[len("task:"):]
            parts = parts_str.split('|')

            topic = parts[0].strip() if len(parts) > 0 else "No Topic"
            customer = parts[1].strip() if len(parts) > 1 else ""
            phone = parts[2].strip() if len(parts) > 2 else ""
            address = parts[3].strip() if len(parts) > 3 else ""
            appointment = parts[4].strip() if len(parts) > 4 else ""
            coord_str = parts[5].strip() if len(parts) > 5 else ""
            detail = parts[6].strip() if len(parts) > 6 else ""
            task_status = "PENDING"

            latitude = ""
            longitude = ""
            if coord_str:
                map_url_regex = r"(?:@(-?\d+\.\d+),(-?\d+\.\d+))|(?:\/maps\/place\/(?:[^/]+\/)?@(-?\d+\.\d+),(-?\d+\.\d+))"
                match = re.search(map_url_regex, coord_str)
                if match:
                    # Determine which group contains lat/lng based on the regex match
                    if match.group(1) and match.group(2): # @lat,lng
                        latitude = match.group(1)
                        longitude = match.group(2)
                    elif match.group(3) and match.group(4): # /place/@lat,lng
                        latitude = match.group(3)
                        longitude = match.group(4)
                else: # Assume direct lat,long if not a map URL
                    coords_parts = coord_str.split(',')
                    if len(coords_parts) == 2:
                        try:
                            latitude = str(float(coords_parts[0].strip()))
                            longitude = str(float(coords_parts[1].strip()))
                        except ValueError:
                            pass
            final_coord = f"{latitude},{longitude}" if latitude and longitude else coord_str

            # Save to tasks_log.txt (no file attachments from LINE command)
            with open("tasks_log.txt", "a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now()}|{topic}|{customer}|{phone}|{address}|{appointment}|{final_coord}|{detail}|None|{task_status}\n")
            app.logger.info("Task from LINE logged to tasks_log.txt.")

            # Create Google Task from LINE message
            task_title = f"{topic} (จาก LINE)"
            task_notes = f"ลูกค้า: {customer or '-'}\nโทร: {phone or '-'}\nที่อยู่: {address or '-'}\nรายละเอียด: {detail or '-'}"
            if appointment:
                task_notes += f"\nนัดหมาย: {appointment}"
            if final_coord and final_coord != ',':
                # แก้ไข URL Google Maps
                task_notes += f"\nพิกัด: https://www.google.com/maps/search/?api=1&query={latitude},{longitude}"

            due_date_gmt = None
            if appointment:
                try:
                    dt_obj = datetime.datetime.strptime(appointment, "%Y-%m-%d %H:%M")
                    due_date_gmt = dt_obj.isoformat() + "Z"
                except ValueError:
                    app.logger.warning(f"Could not parse LINE appointment date: {appointment}")

            created_task = create_google_task(task_title, task_notes, due=due_date_gmt)
            
            # Reply to user and notify relevant groups/users
            if created_task:
                reply_message_text = f"Task '{topic}' ได้รับการบันทึกแล้ว! (ID: {created_task.get('id')})"
                # ใช้ line_messaging_api.reply_message แทน line_bot_api.reply_message (v3)
                line_messaging_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_message_text)]
                    )
                )
                
                # Notify Admin Group and Technician Group about the new task
                new_task_notification_text = (
                    f"งานใหม่ถูกสร้าง: {topic}\n"
                    f"ลูกค้า: {customer}\n"
                    f"ID สำหรับสรุปงาน: {created_task.get('id')}\n"
                    f"(ใช้คำสั่ง 'complete {created_task.get('id')}: สรุป | อุปกรณ์ | เวลา')"
                )
                
                recipients_for_new_task = [LINE_ADMIN_GROUP_ID, LINE_TECHNICIAN_GROUP_ID]
                send_message_to_recipients(TextMessage(text=new_task_notification_text), recipients_for_new_task)
            else:
                # ใช้ line_messaging_api.reply_message (v3)
                line_messaging_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="เกิดข้อผิดพลาดในการสร้าง Task กรุณาลองใหม่")]
                    )
                )

        except Exception as e:
            app.logger.error(f"Error processing 'task:' command: {e}")
            # ใช้ line_messaging_api.reply_message (v3)
            line_messaging_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="รูปแบบคำสั่งไม่ถูกต้องหรือเกิดข้อผิดพลาด. โปรดใช้รูปแบบ 'task:หัวข้อ|ลูกค้า|เบอร์โทร|ที่อยู่|วันเวลา_นัดหมาย(YYYY-MM-DD HH:MM)|ละติจูด,ลองจิจูด|รายละเอียด'")]
                )
            )
    
    # Command for technician to complete and summarize a task:
    # Format: complete <Google_Task_ID>: สรุปผลการทำงาน | รายการอุปกรณ์ที่ใช้ | ระยะเวลาที่ทำเสร็จ
    elif text_message.lower().startswith("complete "):
        try:
            command_body = text_message[len("complete "):].strip()
            
            parts_colon = command_body.split(':', 1)
            google_task_id = parts_colon[0].strip()

            work_summary = ""
            equipment_used = ""
            time_taken = ""

            if len(parts_colon) > 1:
                summary_detail = parts_colon[1].strip()
                summary_parts = summary_detail.split('|', 2)
                
                work_summary = summary_parts[0].strip() if len(summary_parts) > 0 else ""
                equipment_used = summary_parts[1].strip() if len(summary_parts) > 1 else ""
                time_taken = summary_parts[2].strip() if len(summary_parts) > 2 else ""
            
            summary_notes_text = (
                f"\n\n--- สรุปงานโดยช่าง ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ---\n"
                f"สรุปผลการทำงาน: {work_summary or '-'}\n"
                f"รายการอุปกรณ์ที่ใช้: {equipment_used or '-'}\n"
                f"ระยะเวลาที่ทำเสร็จ: {time_taken or '-'}\n"
            )

            service = get_google_tasks_service()
            if service:
                task_list_id = '@default'
                try:
                    existing_task = service.tasks().get(tasklist=task_list_id, task=google_task_id).execute()
                except HttpError as e:
                    if e.resp.status == 404:
                        # ใช้ line_messaging_api.reply_message (v3)
                        line_messaging_api.reply_message(
                            ReplyMessageRequest(
                                reply_token=event.reply_token,
                                messages=[TextMessage(text=f"ไม่พบ Task ID: {google_task_id}")]
                            )
                        )
                        app.logger.warning(f"Task ID {google_task_id} not found for complete command.")
                        return
                    else:
                        raise e # Re-raise other HTTP errors

                current_notes = existing_task.get('notes', '')
                new_notes = current_notes + summary_notes_text if current_notes else summary_notes_text
                
                updated_task = update_google_task(
                    task_id=google_task_id,
                    notes=new_notes,
                    status='completed'
                )

                if updated_task:
                    # ใช้ line_messaging_api.reply_message (v3)
                    line_messaging_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=f"งาน '{updated_task.get('title', 'N/A')}' (ID: {google_task_id}) ได้รับการสรุปและทำเครื่องหมายว่าเสร็จสิ้นแล้ว!")]
                        )
                    )

                    # Send summary report to relevant LINE groups/users
                    admin_report_message_text = (
                        f"--- รายงานสรุปงานจากช่าง (LINE) ---\n"
                        f"Task ID: {google_task_id}\n"
                        f"หัวข้อ: {updated_task.get('title', 'N/A')}\n"
                        f"สรุปผล: {work_summary or '-'}\n"
                        f"อุปกรณ์ที่ใช้: {equipment_used or '-'}\n"
                        f"ระยะเวลา: {time_taken or '-'}\n"
                        f"เวลาสรุป: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"สถานะ: เสร็จสิ้น\n"
                    )
                    
                    recipients_for_summary_report = [LINE_ADMIN_GROUP_ID, LINE_MANAGER_USER_ID, LINE_HR_GROUP_ID]
                    send_message_to_recipients(TextMessage(text=admin_report_message_text), recipients_for_summary_report)
                else:
                    # ใช้ line_messaging_api.reply_message (v3)
                    line_messaging_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text="ไม่สามารถอัปเดต Task ใน Google Tasks ได้.")]
                        )
                    )
            else:
                # ใช้ line_messaging_api.reply_message (v3)
                line_messaging_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="ไม่สามารถเชื่อมต่อ Google Tasks ได้ในขณะนี้")]
                    )
                )

        except Exception as e:
            app.logger.error(f"Error processing 'complete' command: {e}")
            # ใช้ line_messaging_api.reply_message (v3)
            line_messaging_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="รูปแบบคำสั่งไม่ถูกต้องหรือเกิดข้อผิดพลาด. โปรดใช้รูปแบบ 'complete <Google_Task_ID>: สรุปผล | อุปกรณ์ | ระยะเวลา'")]
                )
            )
    
    # Reply for unrecognized commands
    else:
        # ใช้ line_messaging_api.reply_message (v3)
        line_messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="กรุณาส่งข้อความในรูปแบบที่ถูกต้อง เช่น 'task:หัวข้อ|ลูกค้า|เบอร์โทร...' หรือ 'complete <Google_Task_ID>: สรุปผล | อุปกรณ์ | ระยะเวลา'")]
            )
        )

# --- Main execution block ---
if __name__ == '__main__':
    # Ensure uploads directory exists on local run
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    
    # This block is for local development only
    # On Render, the app is run by gunicorn
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
