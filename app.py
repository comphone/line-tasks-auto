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
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, PushMessageRequest, TextMessage, ReplyMessageRequest, TextSendMessage
from linebot.v3 import WebhookHandler
from linebot.v3.webhooks import MessageEvent
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
LINE_ADMIN_USER_ID = os.environ.get('LINE_ADMIN_USER_ID')
LINE_ADMIN_GROUP_ID = os.environ.get('LINE_ADMIN_GROUP_ID')
LINE_MANAGER_USER_ID = os.environ.get('LINE_MANAGER_USER_ID')
LINE_HR_GROUP_ID = os.environ.get('LINE_HR_GROUP_ID')

# Ensure environment variables are loaded
if not LINE_CHANNEL_ACCESS_TOKEN:
    print("LINE_CHANNEL_ACCESS_TOKEN is not set in environment variables.")
    sys.exit(1)
if not LINE_CHANNEL_SECRET:
    print("LINE_CHANNEL_SECRET is not set in environment variables.")
    sys.exit(1)

# Initialize LINE Messaging API client and handler (v3)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_messaging_api = MessagingApi(ApiClient(configuration))
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- Google Tasks API Configuration ---
SCOPES = ['https://www.googleapis.com/auth/tasks']
GOOGLE_TOKEN_JSON = os.environ.get('GOOGLE_TOKEN_JSON')

def get_google_tasks_service():
    creds = None
    if GOOGLE_TOKEN_JSON:
        try:
            creds_data = json.loads(GOOGLE_TOKEN_JSON)
            creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
            app.logger.info("Google token loaded from GOOGLE_TOKEN_JSON env var.")
        except json.JSONDecodeError as e:
            app.logger.error(f"Error decoding GOOGLE_TOKEN_JSON: {e}")
            return None
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            app.logger.info("Google token refreshed.")
        else:
            app.logger.error("No valid Google credentials found. Please set GOOGLE_TOKEN_JSON or run local auth flow.")
            return None
    try:
        service = build('tasks', 'v1', credentials=creds)
        return service
    except HttpError as err:
        app.logger.error(f"Error building Google Tasks service: {err}")
        return None

def get_google_tasks_list_id(service, title="My Tasks"):
    try:
        results = service.tasklists().list().execute()
        items = results.get('items', [])
        for item in items:
            if item['title'] == title:
                return item['id']
        # If 'My Tasks' not found, create it
        new_list = service.tasklists().insert(body={'title': title}).execute()
        return new_list['id']
    except HttpError as err:
        app.logger.error(f"Error getting/creating task list: {err}")
        return None

def get_google_tasks_for_report(show_completed=False):
    service = get_google_tasks_service()
    if not service:
        return None

    task_list_id = get_google_tasks_list_id(service)
    if not task_list_id:
        return None

    try:
        if show_completed:
            # Fetch all tasks including completed ones
            results = service.tasks().list(tasklist=task_list_id, showCompleted=True, showDeleted=False, showHidden=False).execute()
        else:
            # Fetch only non-completed tasks
            results = service.tasks().list(tasklist=task_list_id, showCompleted=False, showDeleted=False, showHidden=False).execute()
        
        return results.get('items', [])
    except HttpError as err:
        app.logger.error(f"Error fetching Google Tasks: {err}")
        return None

def create_google_task(title, notes=None, due_date=None):
    service = get_google_tasks_service()
    if not service:
        return None

    task_list_id = get_google_tasks_list_id(service)
    if not task_list_id:
        return None

    task_body = {
        'title': title,
        'status': 'needsAction'
    }
    if notes:
        task_body['notes'] = notes
    if due_date:
        task_body['due'] = due_date # ISO 8601 format, e.g., '2023-10-27T17:00:00.000Z'
    try:
        task = service.tasks().insert(tasklist=task_list_id, body=task_body).execute()
        return task
    except HttpError as err:
        app.logger.error(f"Error creating Google Task: {err}")
        return None

def update_google_task(task_id, new_notes=None, new_status=None):
    service = get_google_tasks_service()
    if not service:
        return None

    task_list_id = get_google_tasks_list_id(service)
    if not task_list_id:
        return None

    try:
        # First, get the existing task to ensure we don't overwrite other fields
        task = service.tasks().get(tasklist=task_list_id, task=task_id).execute()

        if new_notes is not None:
            task['notes'] = new_notes
        if new_status is not None:
            task['status'] = new_status
            if new_status == 'completed':
                task['completed'] = datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
        
        updated_task = service.tasks().update(tasklist=task_list_id, task=task_id, body=task).execute()
        return updated_task
    except HttpError as err:
        app.logger.error(f"Error updating Google Task {task_id}: {err}")
        return None

def send_message_to_recipients(message_object, recipient_ids):
    for recipient_id in recipient_ids:
        if recipient_id:
            try:
                # Use line_messaging_api for v3 push_message
                line_messaging_api.push_message(PushMessageRequest(to=recipient_id, messages=[message_object]))
                app.logger.info(f"Message sent to LINE recipient: {recipient_id}")
            except Exception as e:
                app.logger.error(f"Error sending message to {recipient_id}: {e}")
        else:
            app.logger.warning("Skipping message send to empty recipient ID: None")

# Helper function to parse dates from Google Tasks
def parse_google_task_dates(task_item):
    parsed = {
        'id': task_item.get('id'),
        'title': task_item.get('title'),
        'status': task_item.get('status'),
        'notes': task_item.get('notes', ''),
        'created_formatted': '',
        'due_formatted': '',
        'completed_formatted': '',
        'display_status': ''
    }

    # Helper for date formatting
    def format_date_str(date_str):
        if date_str:
            try:
                # Assuming date_str might be in ISO format like '2023-10-27T17:00:00.000Z'
                # or '2023-10-27T17:00:00Z'
                dt_obj = datetime.datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                return dt_obj.strftime('%d/%m/%Y %H:%M')
            except ValueError:
                # Fallback for simpler date strings if needed
                return date_str
        return ''

    if 'updated' in task_item:
        parsed['updated_formatted'] = format_date_str(task_item['updated'])

    if 'due' in task_item:
        parsed['due_formatted'] = format_date_str(task_item['due'])

    if 'completed' in task_item:
        parsed['completed_formatted'] = format_date_str(task_item['completed'])

    if 'created' in task_item:
        parsed['created_formatted'] = format_date_str(task_item['created'])

    # Determine display status
    if parsed['status'] == 'completed':
        parsed['display_status'] = 'เสร็จสิ้น'
    elif 'due' in task_item and datetime.datetime.fromisoformat(task_item['due'].replace('Z', '+00:00')) < datetime.datetime.now(datetime.timezone.utc):
        parsed['display_status'] = 'ค้างชำระ'
    else:
        parsed['display_status'] = 'รอดำเนินการ'
    return parsed

# --- Routes ---
@app.route('/')
def index():
    return render_template('index.html')

# Endpoint for handling LINE webhook events
@app.route('/callback', methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']

    # get request body as text
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text_message = event.message.text
    app.logger.info(f"Received message: {text_message} from user {event.source.user_id}")

    # Log every message received
    with open('tasks_log.txt', 'a', encoding='utf-8') as f:
        log_entry = f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - User: {event.source.user_id} - Message: {text_message}\n"
        f.write(log_entry)
        app.logger.info("Task logged to tasks_log.txt.")

    if text_message.lower().startswith('task:'):
        parts = text_message[len('task:'):].split('|')
        if len(parts) >= 3:
            title = parts[0].strip()
            customer_name = parts[1].strip()
            phone_number = parts[2].strip()

            notes = f"ลูกค้า: {customer_name}\nเบอร์โทร: {phone_number}"
            if len(parts) > 3: # Additional notes
                notes += "\n" + "|".join(parts[3:]).strip()

            # Set due date for tomorrow at 5 PM local time (Thailand)
            now_thai = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
            tomorrow = now_thai + datetime.timedelta(days=1)
            due_tomorrow_5pm = tomorrow.replace(hour=17, minute=0, second=0, microsecond=0)
            due_date_iso = due_tomorrow_5pm.isoformat().replace('+07:00', 'Z') # Convert to UTC for Google Tasks

            task = create_google_task(title, notes, due_date_iso)
            if task:
                reply_text = f"งาน '{task['title']}' ถูกสร้างแล้ว กำหนดส่ง: {due_tomorrow_5pm.strftime('%d/%m/%Y %H:%M')} ID: {task['id']}"
                # ใช้ line_messaging_api.reply_message (v3)
                line_messaging_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_text)]
                    )
                )
                
                # Send push message to admin/manager
                notification_message_obj = TextMessage(
                    text=f"***แจ้งงานใหม่***\nหัวข้อ: {title}\nลูกค้า: {customer_name}\nเบอร์โทร: {phone_number}\nกำหนดส่ง: {due_tomorrow_5pm.strftime('%d/%m/%Y %H:%M')}\nสถานะ: รอดำเนินการ\nID งาน: {task['id']}"
                )
                recipients_for_new_task = [LINE_ADMIN_USER_ID, LINE_ADMIN_GROUP_ID, LINE_MANAGER_USER_ID]
                send_message_to_recipients(notification_message_obj, recipients_for_new_task)

            else:
                # ใช้ line_messaging_api.reply_message (v3)
                line_messaging_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="ไม่สามารถสร้าง Task ใน Google Tasks ได้.")]
                    )
                )
        else:
            # ใช้ line_messaging_api.reply_message (v3)
            line_messaging_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="รูปแบบคำสั่งไม่ถูกต้อง. โปรดใช้ 'task:หัวข้อ|ลูกค้า|เบอร์โทร|รายละเอียดเพิ่มเติม(ถ้ามี)'")]
                )
            )

    elif text_message.lower().startswith('complete '):
        try:
            command_parts = text_message[len('complete '):].split(':')
            if len(command_parts) < 2:
                raise ValueError("Command format incorrect")

            task_id = command_parts[0].strip()
            summary_parts = command_parts[1].split('|')

            if len(summary_parts) != 3:
                raise ValueError("Summary format incorrect")

            tech_work_summary = summary_parts[0].strip()
            tech_equipment_used = summary_parts[1].strip()
            tech_time_taken = summary_parts[2].strip()
            tech_summary_date = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime('%d/%m/%Y %H:%M') # Thai local time

            # Get current task notes
            service = get_google_tasks_service()
            if service:
                task_list_id = get_google_tasks_list_id(service)
                if task_list_id:
                    current_task = service.tasks().get(tasklist=task_list_id, task=task_id).execute()
                    current_notes = current_task.get('notes', '')

                    # Append new summary to notes
                    new_notes = (f"{current_notes}\n\n--- สรุปงานโดยช่าง ---\n"
                                 f"วันที่สรุป: {tech_summary_date}\n"
                                 f"สรุปผลการทำงาน: {tech_work_summary}\n"
                                 f"รายการอุปกรณ์ที่ใช้: {tech_equipment_used}\n"
                                 f"ระยะเวลาที่ทำเสร็จ: {tech_time_taken}")

                    updated_task = update_google_task(task_id, new_notes=new_notes, new_status='completed')

                    if updated_task:
                        reply_text = f"งาน '{updated_task['title']}' (ID: {updated_task['id']}) ได้รับการอัปเดตและทำเครื่องหมายว่าเสร็จสิ้นแล้ว"
                        # ใช้ line_messaging_api.reply_message (v3)
                        line_messaging_api.reply_message(
                            ReplyMessageRequest(
                                reply_token=event.reply_token,
                                messages=[TextMessage(text=reply_text)]
                            )
                        )

                        # Send summary report to specified recipients
                        report_summary_message_obj = TextMessage(
                            text=f"***สรุปผลงาน***\n"
                                 f"หัวข้อ: {updated_task['title']}\n"
                                 f"ID งาน: {updated_task['id']}\n"
                                 f"สถานะ: เสร็จสิ้น\n"
                                 f"สรุปผลการทำงาน: {tech_work_summary}\n"
                                 f"รายการอุปกรณ์ที่ใช้: {tech_equipment_used}\n"
                                 f"ระยะเวลาที่ทำเสร็จ: {tech_time_taken}\n"
                                 f"วันที่สรุป: {tech_summary_date}\n"
                                 f"รายละเอียดเพิ่มเติม: {current_notes}"
                        )
                        recipients_for_summary_report = [LINE_ADMIN_USER_ID, LINE_ADMIN_GROUP_ID, LINE_MANAGER_USER_ID, LINE_HR_GROUP_ID]
                        send_message_to_recipients(report_summary_message_obj, recipients_for_summary_report)
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

# Route to display the task summary
@app.route('/summary')
def summary():
    tasks_raw = get_google_tasks_for_report(show_completed=True)
    
    if tasks_raw is None:
        return "ไม่สามารถดึงข้อมูล Google Tasks ได้ โปรดตรวจสอบการเชื่อมต่อ API."

    tasks = []
    task_status_counts = {
        'needsAction': 0,
        'completed': 0,
        'overdue': 0,
        'total': 0
    }

    for task_item in tasks_raw:
        task_status_counts['total'] += 1
        parsed_task = parse_google_task_dates(task_item) # Call the helper function

        # Categorize tasks for counts
        if parsed_task['status'] == 'completed':
            task_status_counts['completed'] += 1
        elif 'due' in task_item and datetime.datetime.fromisoformat(task_item['due'].replace('Z', '+00:00')) < datetime.datetime.now(datetime.timezone.utc):
            task_status_counts['overdue'] += 1
        else:
            task_status_counts['needsAction'] += 1
        
        # Extract tech summary from notes
        notes = parsed_task.get('notes', '')
        
        # Regex to find tech summary details
        tech_summary_match = re.search(
            r'---\s*สรุปงานโดยช่าง\s*---\s*'
            r'วันที่สรุป:\s*(?P<date>.*?)\s*\n'
            r'สรุปผลการทำงาน:\s*(?P<summary>.*?)\s*\n'
            r'รายการอุปกรณ์ที่ใช้:\s*(?P<equipment>.*?)\s*\n'
            r'ระยะเวลาที่ทำเสร็จ:\s*(?P<time>.*)',
            notes, re.DOTALL
        )
        
        if tech_summary_match:
            parsed_task['tech_summary_date'] = tech_summary_match.group('date').strip()
            parsed_task['tech_work_summary'] = tech_summary_match.group('summary').strip()
            parsed_task['tech_equipment_used'] = tech_summary_match.group('equipment').strip()
            parsed_task['tech_time_taken'] = tech_summary_match.group('time').strip()
        else:
            parsed_task['tech_summary_date'] = None
            parsed_task['tech_work_summary'] = None
            parsed_task['tech_equipment_used'] = None
            parsed_task['tech_time_taken'] = None
            
        # Extract attachment URLs from notes
        attachment_urls_match = re.findall(r'(https?://\S+\.(?:jpg|jpeg|png|gif|pdf|docx|doc|xlsx|xls|pptx|ppt|zip|rar|txt))', notes)
        parsed_task['attachment_urls'] = attachment_urls_match if attachment_urls_match else []

        tasks.append(parsed_task)

    # Sort tasks by creation date
    tasks.sort(key=lambda x: x.get('created_formatted', ''), reverse=True)

    # Calculate percentages
    task_status_counts['completed_percent'] = round((task_status_counts['completed'] / task_status_counts['total'] * 100) if task_status_counts['total'] > 0 else 0, 2)
    task_status_counts['needsAction_percent'] = round((task_status_counts['needsAction'] / task_status_counts['total'] * 100) if task_status_counts['total'] > 0 else 0, 2)
    task_status_counts['overdue_percent'] = round((task_status_counts['overdue'] / task_status_counts['total'] * 100) if task_status_counts['total'] > 0 else 0, 2)
    
    return render_template('tasks_summary.html', tasks=tasks, summary=task_status_counts)

# For serving uploaded files (if needed)
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# --- Main execution block ---
if __name__ == '__main__':
    # Ensure uploads directory exists on local run
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    
    # This block is for local development only
    # On Render, Gunicorn/production server will manage app execution.
    app.run(debug=True, host='0.0.0.0', port=os.environ.get('PORT', 10000))
