from dotenv import load_dotenv
load_dotenv() # โหลดตัวแปรสภาพแวดล้อมจากไฟล์ .env

import os
import sys
import datetime
import re
import json
from flask import Flask, request, render_template, redirect, url_for, abort, send_from_directory

# สำหรับจัดการชื่อไฟล์ที่ปลอดภัยและการอัปโหลดไฟล์
from werkzeug.utils import secure_filename

# LINE Messaging API (ใช้ v3 เพื่อหลีกเลี่ยงการแจ้งเตือน Deprecated)
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, PushMessageRequest, TextMessage, ReplyMessageRequest
from linebot.v3 import WebhookHandler # แก้ไข: WebhookHandler มาจาก linebot.v3 โดยตรง
from linebot.v3.webhooks import MessageEvent # แก้ไข: MessageEvent ยังคงมาจาก linebot.v3.webhooks
from linebot.exceptions import InvalidSignatureError

# Google Tasks API
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# เริ่มต้น Flask App
app = Flask(__name__)

# --- การตั้งค่า ---
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'docx', 'doc', 'xlsx', 'xls', 'pptx', 'ppt', 'zip', 'rar', 'txt'} # ประเภทไฟล์ที่อนุญาตให้อัปโหลด

# สร้างโฟลเดอร์อัปโหลดหากยังไม่มี
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# การตั้งค่า LINE Bot API - ดึงจากตัวแปรสภาพแวดล้อมเพื่อความปลอดภัย
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')

# ID ผู้รับ LINE - ดึงจากตัวแปรสภาพแวดล้อม
# สำคัญ: ตรวจสอบให้แน่ใจว่าตั้งค่าเหล่านี้ถูกต้องบน Render และ/หรือในไฟล์ .env ของคุณ
LINE_ADMIN_GROUP_ID = os.environ.get('LINE_ADMIN_GROUP_ID')
LINE_MANAGER_USER_ID = os.environ.get('LINE_MANAGER_USER_ID')
LINE_HR_GROUP_ID = os.environ.get('LINE_HR_GROUP_ID')
LINE_TECHNICIAN_GROUP_ID = os.environ.get('LINE_TECHNICIAN_GROUP_ID')

# ตรวจสอบให้แน่ใจว่าตัวแปรสภาพแวดล้อมที่สำคัญถูกตั้งค่า
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    app.logger.error("LINE_CHANNEL_ACCESS_TOKEN or LINE_CHANNEL_SECRET is not set in environment variables.")
    sys.exit(1) # จบการทำงานหากตัวแปรที่จำเป็นไม่ถูกตั้งค่า

# เริ่มต้น LINE Messaging API client และ handler (v3)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_api_client = ApiClient(configuration) # เปลี่ยนชื่อตัวแปรเพื่อหลีกเลี่ยงความสับสน
line_messaging_api = MessagingApi(line_api_client) # ใช้ MessagingApi จาก linebot.v3.messaging
handler = WebhookHandler(LINE_CHANNEL_SECRET) # ใช้ WebhookHandler จาก linebot.v3

# การตั้งค่า Google Tasks API
SCOPES = ['https://www.googleapis.com/auth/tasks']
# สำหรับการ Deploy บน Render, 'credentials.json' ควรถูกสร้างจากตัวแปรสภาพแวดล้อม GOOGLE_CREDENTIALS_JSON
GOOGLE_CREDENTIALS_FILE_NAME = 'credentials.json'

# --- ฟังก์ชันตัวช่วย ---

def allowed_file(filename):
    """ตรวจสอบว่านามสกุลไฟล์ที่อัปโหลดได้รับอนุญาตหรือไม่"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_google_tasks_service():
    """
    รับรองความถูกต้องด้วย Google Tasks API
    จัดลำดับความสำคัญในการโหลดโทเค็นจากตัวแปรสภาพแวดล้อม GOOGLE_TOKEN_JSON สำหรับ Render
    หากไม่สำเร็จ จะลองจากไฟล์ token.json หรือเริ่มกระบวนการ OAuth ใหม่โดยใช้ credentials.json
    (หรือตัวแปรสภาพแวดล้อม GOOGLE_CREDENTIALS_JSON)
    """
    creds = None

    # 1. ลองโหลดโทเค็นจากตัวแปรสภาพแวดล้อม (สำหรับ Render deployment)
    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')
    if google_token_json_str:
        try:
            creds_info = json.loads(google_token_json_str)
            creds = Credentials.from_authorized_user_info(creds_info, SCOPES)
            app.logger.info("Google token loaded from GOOGLE_TOKEN_JSON env var.")
        except Exception as e:
            app.logger.warning(f"Could not load token from GOOGLE_TOKEN_JSON env var: {e}. Attempting other methods.")
            creds = None

    # 2. หากไม่โหลดจาก env var ลองโหลดจากไฟล์ token.json ในเครื่อง (สำหรับการพัฒนาในเครื่อง)
    if not creds and os.path.exists('token.json'):
        try:
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
            app.logger.info("Google token loaded from local token.json.")
        except Exception as e:
            app.logger.warning(f"Could not load token from local token.json: {e}. Attempting re-authentication.")
            creds = None

    # 3. หากไม่มีข้อมูลรับรองที่ถูกต้อง ลองรีเฟรชหรือเริ่มกระบวนการ OAuth ใหม่
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                app.logger.info("Google token refreshed.")
            except Exception as e:
                app.logger.error(f"Error refreshing Google token: {e}. Will attempt full re-authentication.")
                creds = None
        else:
            # สร้าง credentials.json จากตัวแปรสภาพแวดล้อม GOOGLE_CREDENTIALS_JSON หากมี
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
                # สำหรับการพัฒนาในเครื่อง, run_local_server จะเปิดเบราว์เซอร์เพื่อการรับรองความถูกต้อง
                # การ Deploy บน Render อาศัยการอัปเดตตัวแปรสภาพแวดล้อม GOOGLE_TOKEN_JSON ด้วยตนเอง.
                # ถ้าคุณใช้ Render และต้องการให้มีการรับรองอัตโนมัติ (เช่น ผ่าน Service Account)
                # คุณจะต้องใช้ flow ที่ต่างออกไป
                creds = flow.run_local_server(port=0) 
                app.logger.info("Google OAuth flow completed locally.")
            except Exception as e:
                app.logger.error(f"Error during Google OAuth flow: {e}. Ensure {GOOGLE_CREDENTIALS_FILE_NAME} is valid.")
                return None
            
            # บันทึกโทเค็นใหม่ในเครื่องสำหรับการใช้งานในอนาคต (หากอยู่ในการพัฒนาในเครื่อง)
            if creds and not os.environ.get('GOOGLE_TOKEN_JSON'): # บันทึกเฉพาะเมื่อไม่ได้ใช้ตัวแปรสภาพแวดล้อมสำหรับโทเค็น
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
    """สร้าง Task ใหม่ใน Google Tasks"""
    service = get_google_tasks_service()
    if not service:
        app.logger.error("Failed to get Google Tasks service for creation.")
        return None
    try:
        task_list_id = '@default' # รายการ Task เริ่มต้น
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
    """อัปเดต Task ที่มีอยู่ใน Google Tasks"""
    service = get_google_tasks_service()
    if not service:
        app.logger.error("Failed to get Google Tasks service for update.")
        return None
    try:
        task_list_id = '@default'
        current_task = service.tasks().get(tasklist=task_list_id, task=task_id).execute()
        
        if title:
            current_task['title'] = title
        if notes is not None: # ต้องตรวจสอบ None เพราะ notes อาจเป็นค่าว่าง
            current_task['notes'] = notes
        if due:
            current_task['due'] = due
        
        if status:
            current_task['status'] = status
            if status == 'completed':
                current_task['completed'] = datetime.datetime.now().isoformat() + 'Z'
            elif 'completed' in current_task: # ถ้าเปลี่ยนสถานะจาก completed ให้ลบฟิลด์ completed
                del current_task['completed']

        result = service.tasks().update(tasklist=task_list_id, task=task_id, body=current_task).execute()
        app.logger.info(f"Google Task {task_id} updated. New Status: {result.get('status')}")
        return result
    except HttpError as err:
        app.logger.error(f"Error updating Google Task {task_id}: {err}")
        return None

def get_google_tasks_for_report(show_completed=False, due_min=None, due_max=None):
    """ดึง Task จาก Google Tasks สำหรับวัตถุประสงค์ในการรายงาน"""
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
    """รับ Task ที่ครบกำหนดภายในสิ้นวันนี้ที่ยังไม่เสร็จสมบูรณ์"""
    today_end = datetime.datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
    outstanding_tasks = get_google_tasks_for_report(
        show_completed=False, # เฉพาะ Task ที่ยังไม่เสร็จ
        due_max=today_end.isoformat(timespec='milliseconds') + "Z"
    )
    return outstanding_tasks

def get_daily_summary_tasks():
    """รับ Task ที่สร้างหรือเสร็จสิ้นในวันนี้"""
    today_start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = datetime.datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)

    all_tasks = get_google_tasks_for_report(show_completed=True) # ดึงทั้งหมดเพื่อกรองตามวันที่สร้าง/เสร็จสิ้น

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
    ส่งข้อความ LINE (TextMessage object) ไปยังรายการ ID ผู้ใช้ LINE หรือ ID กลุ่ม LINE โดยใช้ v3 API.
    :param message_object: ออบเจกต์ TextMessage.
    :param recipient_ids: รายการ User ID หรือ Group ID (สตริง).
    """
    for recipient_id in recipient_ids:
        if recipient_id:
            try:
                # ใช้ PushMessageRequest สำหรับ v3 API
                line_messaging_api.push_message(
                    PushMessageRequest(
                        to=recipient_id,
                        messages=[message_object]
                    )
                )
                app.logger.info(f"Message sent to LINE recipient: {recipient_id}")
            except Exception as e:
                app.logger.error(f"Failed to send message to LINE recipient {recipient_id}: {e}")
        else:
            app.logger.warning(f"Skipping message send to empty recipient ID: {recipient_id}")


def send_daily_reports():
    """
    ฟังก์ชันที่จะถูกเรียกโดย Render Cron Job ในเวลาที่กำหนด (เช่น 6 โมงเช้า และ 2 ทุ่ม).
    กำหนดว่าจะส่งรายงานใดตามชั่วโมงปัจจุบัน.
    """
    current_hour_utc = datetime.datetime.now(datetime.timezone.utc).hour # Render ใช้ UTC
    
    # ปรับชั่วโมงสำหรับเขตเวลาไทย (UTC+7)
    current_hour_thai = (current_hour_utc + 7) % 24 
    app.logger.info(f"Cron job triggered. Current UTC hour: {current_hour_utc}, Thai local hour: {current_hour_thai}")

    report_message_text = ""

    # รายงานงานค้างเวลา 6 โมงเช้าตามเวลาไทย
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
                        due_date_str = (due_date_dt + datetime.timedelta(hours=7)).strftime("%Y-%m-%d %H:%M")
                    except ValueError:
                        pass
                report_message_text += f"- {title} (ครบกำหนด: {due_date_str})\n"
        else:
            report_message_text += "ไม่มีงานค้าง"
        
        # กำหนดผู้รับสำหรับรายงานงานค้าง (เช่น ผู้ดูแลระบบ, ผู้จัดการ)
        recipients_for_outstanding_report = [LINE_ADMIN_GROUP_ID, LINE_MANAGER_USER_ID] 
        send_message_to_recipients(TextMessage(text=report_message_text), recipients_for_outstanding_report)
        app.logger.info("Daily outstanding tasks report sent.")

    # รายงานสรุปประจำวันเวลา 2 ทุ่มตามเวลาไทย
    elif current_hour_thai == 20:
        daily_tasks = get_daily_summary_tasks()
        report_message_text = "--- สรุปงานประจำวัน ---\n"
        needs_action_count = 0
        completed_count = 0
        overdue_count = 0
        total_count = 0

        if daily_tasks:
            for task in daily_tasks:
                total_count += 1
                status = task.get('status')
                is_overdue = False
                if 'due' in task and status == 'needsAction':
                    try:
                        due_dt = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                        if due_dt < datetime.datetime.now(datetime.timezone.utc):
                            is_overdue = True
                    except ValueError:
                        pass

                if status == 'needsAction' and not is_overdue:
                    needs_action_count += 1
                elif status == 'completed':
                    completed_count += 1
                elif is_overdue:
                    overdue_count += 1
            
            report_message_text += f"งานทั้งหมดที่เกี่ยวข้องวันนี้: {total_count} งาน\n"
            report_message_text += f"  - รอดำเนินการ: {needs_action_count} งาน\n"
            report_message_text += f"  - เสร็จสิ้น: {completed_count} งาน\n"
            report_message_text += f"  - ค้างชำระ: {overdue_count} งาน\n"
        else:
            report_message_text += "ไม่มีกิจกรรมงานในวันนี้"
        
        # กำหนดผู้รับสำหรับรายงานสรุป (เช่น ผู้ดูแลระบบ, ผู้จัดการ, HR)
        recipients_for_summary_report = [LINE_ADMIN_GROUP_ID, LINE_MANAGER_USER_ID, LINE_HR_GROUP_ID] 
        send_message_to_recipients(TextMessage(text=report_message_text), recipients_for_summary_report)
        app.logger.info("Daily summary report sent.")

def parse_tech_report_from_notes(notes):
    """
    แยกวิเคราะห์ข้อมูลรายงานช่างและ URL ไฟล์แนบจาก notes ที่มีโครงสร้าง JSON.
    """
    tech_report_data = {
        'summary_date': None,
        'work_summary': None,
        'equipment_used': None,
        'time_taken': None,
        'next_appointment': None,
        'attachment_urls': []
    }
    notes_display = notes # ส่วนของ notes ที่จะแสดงผล ไม่รวม JSON

    # ค้นหาส่วน JSON ที่เราฝังไว้
    tech_report_match = re.search(r"--- TECH_REPORT_START ---\s*\n(.*?)\n--- TECH_REPORT_END ---", notes, re.DOTALL)
    if tech_report_match:
        json_str = tech_report_match.group(1)
        try:
            data = json.loads(json_str)
            tech_report_data.update(data)
            # ลบส่วน JSON ออกจาก notes_display
            notes_display = notes.replace(tech_report_match.group(0), "").strip()
        except json.JSONDecodeError as e:
            app.logger.error(f"Error decoding JSON from notes: {e}")
            # ถ้าถอดรหัส JSON ไม่ได้ ให้ถือว่าไม่มีข้อมูล tech report ที่ถูกต้อง
            tech_report_data = {
                'summary_date': None, 'work_summary': None, 'equipment_used': None,
                'time_taken': None, 'next_appointment': None, 'attachment_urls': []
            }
            notes_display = notes # ถ้ามี error ใน JSON ให้แสดง notes เดิมทั้งหมดไปก่อน
            
    # แยกวิเคราะห์ URL ไฟล์แนบที่อาจจะอยู่ในรูปแบบเดิม (ถ้าไม่มี JSON) หรือเป็นส่วนเสริม
    # ตรวจสอบว่าไม่ซ้ำกับที่อยู่ใน tech_report_data['attachment_urls']
    legacy_attachment_urls = re.findall(r'https?://\S+\.(?:png|jpg|jpeg|gif|pdf|docx|doc|xls|xlsx|pptx|ppt|zip|rar|txt)', notes_display)
    
    # รวม URL ทั้งหมดและกำจัด URL ที่ซ้ำกัน
    all_attachment_urls = list(set(tech_report_data['attachment_urls'] + legacy_attachment_urls))
    
    return tech_report_data, all_attachment_urls, notes_display

def parse_google_task_dates(task_item):
    """
    แยกวิเคราะห์และจัดรูปแบบวันที่ 'created', 'due', 'completed' จากออบเจกต์ Google Tasks API
    และเพิ่มฟิลด์ที่จัดรูปแบบแล้ว ('_formatted') ไปยัง dictionary ของ task_item
    """
    parsed_task = task_item.copy() # ทำสำเนาเพื่อหลีกเลี่ยงการแก้ไขต้นฉบับ
    
    # จัดรูปแบบวันที่ 'created'
    if 'created' in parsed_task:
        try:
            created_dt = datetime.datetime.fromisoformat(parsed_task['created'].replace('Z', '+00:00'))
            # แปลงเป็นเวลาท้องถิ่นไทย (+7 UTC) สำหรับการแสดงผล
            parsed_task['created_formatted'] = (created_dt + datetime.timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            parsed_task['created_formatted'] = 'N/A'
    else:
        parsed_task['created_formatted'] = 'N/A'

    # จัดรูปแบบวันที่ 'due'
    if 'due' in parsed_task:
        try:
            due_dt = datetime.datetime.fromisoformat(parsed_task['due'].replace('Z', '+00:00'))
            # แปลงเป็นเวลาท้องถิ่นไทย (+7 UTC) สำหรับการแสดงผล
            parsed_task['due_formatted'] = (due_dt + datetime.timedelta(hours=7)).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            parsed_task['due_formatted'] = 'N/A'
    else:
        parsed_task['due_formatted'] = 'N/A'

    # จัดรูปแบบวันที่ 'completed'
    if 'completed' in parsed_task:
        try:
            completed_dt = datetime.datetime.fromisoformat(parsed_task['completed'].replace('Z', '+00:00'))
            # แปลงเป็นเวลาท้องถิ่นไทย (+7 UTC) สำหรับการแสดงผล
            parsed_task['completed_formatted'] = (completed_dt + datetime.timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            parsed_task['completed_formatted'] = 'N/A'
    else:
        parsed_task['completed_formatted'] = 'N/A'
        
    return parsed_task

# --- Flask Routes ---

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text_message = event.message.text
    app.logger.info(f"Received message: {text_message}")

    # Command to create a new task: task:หัวข้อ|ลูกค้า|เบอร์โทร|กำหนดส่ง(YYYY-MM-DD HH:MM)|สถานที่
    if text_message.lower().startswith("task:"):
        parts = text_message[len("task:"):].split('|')
        if len(parts) >= 3:
            title = parts[0].strip()
            customer_name = parts[1].strip()
            customer_phone = parts[2].strip()
            
            notes_for_task = f"ลูกค้า: {customer_name}\nเบอร์โทร: {customer_phone}"
            
            due_date = None
            if len(parts) > 3 and parts[3].strip():
                try:
                    # Parse date and time in Thai local time (assuming +07:00 offset from UTC)
                    due_dt_local = datetime.datetime.strptime(parts[3].strip(), "%Y-%m-%d %H:%M")
                    # Convert to UTC and add 'Z' for Google Tasks
                    
                    # Assuming local input is Thai time (+7 UTC)
                    due_dt_utc = due_dt_local - datetime.timedelta(hours=7)
                    due_date = due_dt_utc.isoformat() + 'Z' # Append Z for UTC
                    notes_for_task += f"\nกำหนดส่ง: {parts[3].strip()}" # Keep local formatted date in notes
                except ValueError:
                    line_messaging_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text="รูปแบบวันที่/เวลาไม่ถูกต้อง. โปรดใช้YYYY-MM-DD HH:MM")]
                        )
                    )
                    return

            if len(parts) > 4 and parts[4].strip():
                location = parts[4].strip()
                notes_for_task += f"\nสถานที่: {location}"

            task = create_google_task(title, notes=notes_for_task, due=due_date)
            if task:
                # สร้าง URL สำหรับอัปเดตงานสำหรับช่างเทคนิค
                update_url = url_for('update_task_details', task_id=task.get('id'), _external=True)
                
                # กำหนดผู้รับสำหรับแจ้งเตือนงานใหม่ (เช่น ช่างเทคนิค)
                # เพิ่ม LINE_ADMIN_GROUP_ID ด้วยเพื่อให้ผู้ดูแลระบบเห็นการแจ้งเตือนนี้
                recipients_for_new_task = [LINE_TECHNICIAN_GROUP_ID, LINE_ADMIN_GROUP_ID]
                task_message = TextMessage(text=f"งานใหม่ถูกสร้างแล้ว:\nหัวข้อ: {task.get('title')}\nID: {task.get('id')}\nรายละเอียด: {task.get('notes')}\nสถานะ: {task.get('status')}\n\nอัปเดตงานที่นี่: {update_url}")
                send_message_to_recipients(task_message, recipients_for_new_task)
                
                # ตอบกลับผู้ใช้ที่สร้างงาน
                line_messaging_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=f"สร้างงานเรียบร้อยแล้ว: {task.get('title')} (ID: {task.get('id')})\nคุณสามารถดูและอัปเดตงานได้ที่: {update_url}")]
                    )
                )
            else:
                line_messaging_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="ไม่สามารถสร้าง Task ใน Google Tasks ได้")]
                    )
                )
        else:
            line_messaging_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="รูปแบบคำสั่งไม่ถูกต้อง. โปรดใช้ 'task:หัวข้อ|ลูกค้า|เบอร์โทร|กำหนดส่ง(YYYY-MM-DD HH:MM)|สถานที่'")]
                )
            )

    # Command to complete a task (Legacy from LINE, now prefer web form)
    # complete <Google_Task_ID>: สรุปผล | อุปกรณ์ | ระยะเวลา
    elif text_message.lower().startswith("complete "):
        try:
            command_parts = text_message[len("complete "):].split(':', 1)
            task_id = command_parts[0].strip()
            
            if len(command_parts) > 1:
                summary_parts = command_parts[1].strip().split('|')
                if len(summary_parts) >= 3:
                    summary_result = summary_parts[0].strip()
                    equipment_used = summary_parts[1].strip()
                    time_taken = summary_parts[2].strip()

                    service = get_google_tasks_service()
                    if service:
                        # Get current task notes to append structured summary
                        current_task = service.tasks().get(tasklist='@default', task=task_id).execute()
                        current_notes = current_task.get('notes', '')
                        
                        # แยกข้อมูลเดิม ถ้ามี
                        old_tech_report, old_attachment_urls, remaining_notes = parse_tech_report_from_notes(current_notes)
                        
                        # เตรียมข้อมูล Tech Report ใหม่
                        tech_report_data = {
                            'summary_date': datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            'work_summary': summary_result,
                            'equipment_used': equipment_used,
                            'time_taken': time_taken,
                            'next_appointment': old_tech_report.get('next_appointment'), # คงค่าเดิมไว้ หากมี
                            'attachment_urls': old_attachment_urls # คง URL เดิมไว้
                        }
                        
                        # สร้าง notes ใหม่โดยรวม remaining_notes และ JSON string
                        new_notes_content = json.dumps(tech_report_data, ensure_ascii=False)
                        new_notes = f"{remaining_notes.strip()}\n\n--- TECH_REPORT_START ---\n{new_notes_content}\n--- TECH_REPORT_END ---"
                        new_notes = new_notes.strip() # ลบช่องว่างที่เกินมา

                        updated_task = update_google_task(task_id, notes=new_notes, status='completed')
                        if updated_task:
                            line_messaging_api.reply_message(
                                ReplyMessageRequest(
                                    reply_token=event.reply_token,
                                    messages=[TextMessage(text=f"อัปเดตงาน ID {task_id} เป็น 'เสร็จสิ้น' พร้อมสรุปผลเรียบร้อยแล้ว")]
                                )
                            )
                            # ส่งรายงานสรุปไปยังกลุ่มที่เกี่ยวข้องหลังจากงานเสร็จสิ้น
                            report_summary_message_obj = TextMessage(text=f"งาน ID {task_id} ได้รับการสรุปและเสร็จสิ้นแล้ว:\nหัวข้อ: {updated_task.get('title')}\nสรุปผล: {summary_result}\nอุปกรณ์: {equipment_used}\nเวลาที่ใช้: {time_taken}")
                            recipients_for_summary_report = [LINE_ADMIN_GROUP_ID, LINE_MANAGER_USER_ID, LINE_HR_GROUP_ID] 
                            send_message_to_recipients(report_summary_message_obj, recipients_for_summary_report)
                        else:
                            line_messaging_api.reply_message(
                                ReplyMessageRequest(
                                    reply_token=event.reply_token,
                                    messages=[TextMessage(text="ไม่สามารถอัปเดต Task ใน Google Tasks ได้.")]
                                )
                            )
                    else:
                        line_messaging_api.reply_message(
                            ReplyMessageRequest(
                                reply_token=event.reply_token,
                                messages=[TextMessage(text="ไม่สามารถเชื่อมต่อ Google Tasks ได้ในขณะนี้")]
                            )
                        )

                else:
                    line_messaging_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text="รูปแบบคำสั่งไม่ถูกต้อง. โปรดใช้ 'complete <Google_Task_ID>: สรุปผล | อุปกรณ์ | ระยะเวลา'")]
                        )
                    )
            else:
                line_messaging_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="รูปแบบคำสั่งไม่ถูกต้อง. โปรดใช้ 'complete <Google_Task_ID>: สรุปผล | อุปกรณ์ | ระยะเวลา'")]
                    )
                )

        except Exception as e:
            app.logger.error(f"Error processing 'complete' command: {e}")
            line_messaging_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="รูปแบบคำสั่งไม่ถูกต้องหรือเกิดข้อผิดพลาด. โปรดใช้รูปแบบ 'complete <Google_Task_ID>: สรุปผล | อุปกรณ์ | ระยะเวลา'")]
                )
            )
    
    # Reply for unrecognized commands
    else:
        line_messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="กรุณาส่งข้อความในรูปแบบที่ถูกต้อง เช่น 'task:หัวข้อ|ลูกค้า|เบอร์โทร...' หรือ 'complete <Google_Task_ID>: สรุปผล | อุปกรณ์ | ระยะเวลา'\nหรือเยี่ยมชมหน้าเว็บหลักเพื่อสร้างงาน: {url_for('create_task_page', _external=True)}")]
            )
        )

# --- Flask Routes ---

@app.route("/", methods=['GET', 'POST'])
def create_task_page(): 
    """
    จัดการการส่งฟอร์มการสร้าง Task
    เมื่อเข้าถึงด้วย GET จะแสดงฟอร์มสร้างงานใหม่
    เมื่อเข้าถึงด้วย POST จะประมวลผลการสร้างงานและเปลี่ยนเส้นทางไปยังหน้าสรุปงาน
    """
    if request.method == 'POST':
        command_type = request.form.get('command_type')

        if command_type == 'create_task':
            title = request.form['title']
            customer_name = request.form['customer_name']
            customer_phone = request.form['customer_phone']
            due_date_str = request.form.get('due_date')
            location = request.form.get('location')

            notes = f"ลูกค้า: {customer_name}\nเบอร์โทร: {customer_phone}"
            due_date_gmt = None
            if due_date_str:
                try:
                    # แปลงรูปแบบ datetime-local (เช่น '2025-06-25T14:30') เป็นออบเจกต์ datetime ของ Python
                    due_dt_local = datetime.datetime.fromisoformat(due_date_str)
                    # สมมติว่าอินพุตเป็นเวลาท้องถิ่นไทย (+7 UTC), แปลงเป็น UTC สำหรับ Google Tasks
                    due_dt_utc = due_dt_local - datetime.timedelta(hours=7)
                    due_date_gmt = due_dt_utc.isoformat() + 'Z' # เพิ่ม Z สำหรับ UTC
                    notes += f"\nกำหนดส่ง: {due_date_str.replace('T', ' ')}" # เก็บวันที่ที่จัดรูปแบบเดิมไว้ใน notes
                except ValueError:
                    app.logger.error(f"Invalid due date format from form: {due_date_str}")
                    pass
            if location:
                notes += f"\nสถานที่: {location}"

            created_task = create_google_task(title, notes=notes, due=due_date_gmt)
            if created_task:
                app.logger.info(f"Task created via web form: {created_task.get('title')}")
                # ส่งการแจ้งเตือน LINE หลังจากสร้าง Task จาก Web Form
                new_task_notification_text = (
                    f"งานใหม่ถูกสร้างจากเว็บฟอร์ม: {title}\n"
                    f"ลูกค้า: {customer_name}\n"
                    f"โทร: {customer_phone}\n"
                    f"กำหนดส่ง: {due_date_str.replace('T', ' ') if due_date_str else '-'}\n"
                    f"สถานที่: {location or '-'}\n"
                    f"ID สำหรับสรุปงาน: {created_task.get('id')}\n"
                    f"อัปเดตงานที่นี่: {url_for('update_task_details', task_id=created_task.get('id'), _external=True)}\n"
                    f"(ใช้คำสั่ง LINE 'complete {created_task.get('id')}: สรุป | อุปกรณ์ | เวลา')"
                )
                
                # กำหนดผู้รับ: LINE_ADMIN_GROUP_ID, LINE_TECHNICIAN_GROUP_ID
                recipients_for_new_web_task = [LINE_ADMIN_GROUP_ID, LINE_TECHNICIAN_GROUP_ID]
                send_message_to_recipients(TextMessage(text=new_task_notification_text), recipients_for_new_web_task)
                
                return redirect(url_for('summary')) # เปลี่ยนเส้นทางไปยังหน้าสรุปงานหลังจากสร้าง Task สำเร็จ
            else:
                app.logger.error("Failed to create task via web form.")
                return "Failed to create task", 500
        return "Invalid command", 400

    else: # request.method == 'GET'
        # สำหรับคำขอ GET ไปยังหน้า '/', แสดงฟอร์มสร้างงาน
        return render_template('create_task_form.html') 

@app.route('/update_task/<task_id>', methods=['GET', 'POST'])
def update_task_details(task_id):
    """
    หน้าสำหรับช่างเทคนิคอัปเดตรายละเอียดงานและสถานะ
    รองรับการอัปโหลดหลายรูปภาพ
    """
    service = get_google_tasks_service()
    if not service:
        app.logger.error("Google Tasks service not available for update_task_details.")
        return "ไม่สามารถเชื่อมต่อ Google Tasks ได้ในขณะนี้", 500

    try:
        task_list_id = '@default'
        google_task_raw = service.tasks().get(tasklist=task_list_id, task=task_id).execute()
        
        # จัดรูปแบบวันที่และการแสดงผลสถานะสำหรับเทมเพลต
        task = parse_google_task_dates(google_task_raw)
        task['display_status'] = 'รอดำเนินการ' if task['status'] == 'needsAction' else 'เสร็จสิ้น'
        
        # แยกข้อมูล Tech Report และ Attachment URLs จาก notes
        tech_report, attachment_urls, remaining_notes = parse_tech_report_from_notes(task.get('notes', ''))
        
        # เพิ่มข้อมูล tech_report และ attachment_urls เข้าไปในออบเจกต์ task ที่จะส่งไปให้ template
        task['tech_report'] = tech_report
        task['attachment_urls'] = attachment_urls
        task['notes_display'] = remaining_notes # notes ส่วนที่เหลือที่ไม่ได้เป็น JSON

        # สำหรับค่าเริ่มต้นใน datetime-local input field
        if tech_report.get('next_appointment'):
            try:
                # แปลง ISO format (UTC) กลับเป็น datetime-local (ไทย) สำหรับการแสดงผล
                next_app_dt_utc = datetime.datetime.fromisoformat(tech_report['next_appointment'].replace('Z', '+00:00'))
                next_app_dt_local = next_app_dt_utc + datetime.timedelta(hours=7)
                task['tech_next_appointment_datetime_local'] = next_app_dt_local.strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                task['tech_next_appointment_datetime_local'] = '' # หากมีปัญหาในการแปลง
        else:
            task['tech_next_appointment_datetime_local'] = ''

    except HttpError as err:
        app.logger.error(f"Error getting task {task_id} for update: {err}")
        return f"ไม่พบงาน ID {task_id} หรือเกิดข้อผิดพลาดในการเข้าถึง", 404
    except Exception as e:
        app.logger.error(f"Unexpected error when fetching task {task_id}: {e}")
        return "เกิดข้อผิดพลาดภายใน", 500

    if request.method == 'POST':
        work_summary = request.form.get('work_summary', '').strip()
        equipment_used = request.form.get('equipment_used', '').strip()
        time_taken = request.form.get('time_taken', '').strip()
        new_status = request.form.get('status', task.get('status'))
        next_appointment_date_str = request.form.get('next_appointment_date', '').strip()

        # เก็บ URL ของไฟล์แนบเก่าไว้
        existing_attachment_urls = tech_report.get('attachment_urls', [])
        
        uploaded_file_urls = []
        if 'files[]' in request.files:
            files = request.files.getlist('files[]')
            for file in files:
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    file.save(file_path)
                    uploaded_file_urls.append(url_for('uploaded_file', filename=filename, _external=True))
                else:
                    app.logger.warning(f"Skipping disallowed file: {file.filename}")

        # รวม URL ไฟล์แนบเก่าและใหม่
        all_attachment_urls = list(set(existing_attachment_urls + uploaded_file_urls))

        next_appointment_gmt = None
        if new_status == 'needsAction' and next_appointment_date_str:
            try:
                next_app_dt_local = datetime.datetime.fromisoformat(next_appointment_date_str)
                next_app_dt_utc = next_app_dt_local - datetime.timedelta(hours=7)
                next_appointment_gmt = next_app_dt_utc.isoformat() + 'Z'
            except ValueError:
                app.logger.error(f"Invalid next appointment date format: {next_appointment_date_str}")
                # สามารถเพิ่ม flash message ให้ผู้ใช้ทราบได้
        
        # สร้าง Tech Report Structure ใหม่
        updated_tech_report_data = {
            'summary_date': datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'work_summary': work_summary,
            'equipment_used': equipment_used,
            'time_taken': time_taken,
            'next_appointment': next_appointment_gmt,
            'attachment_urls': all_attachment_urls
        }
        
        # สร้าง notes ใหม่
        # รักษา notes เดิมที่ไม่ใช่ Tech Report JSON
        new_notes_content = json.dumps(updated_tech_report_data, ensure_ascii=False)
        updated_notes = f"{remaining_notes.strip()}\n\n--- TECH_REPORT_START ---\n{new_notes_content}\n--- TECH_REPORT_END ---"
        updated_notes = updated_notes.strip() # ลบช่องว่างที่เกินมา

        updated_task = update_google_task(task_id, notes=updated_notes, status=new_status)

        if updated_task:
            app.logger.info(f"Task {task_id} updated via web form by technician.")
            
            # ส่งรายงานสรุปไปยัง LINE Group
            report_lines = [
                f"งาน ID {task_id} ได้รับการอัปเดตแล้ว:",
                f"หัวข้อ: {updated_task.get('title', 'N/A')}",
                f"สถานะ: {'เสร็จสิ้น' if new_status == 'completed' else 'ยังไม่เสร็จ'}",
                f"สรุปผล: {work_summary or 'ไม่มี'}",
                f"อุปกรณ์: {equipment_used or 'ไม่มี'}",
                f"เวลาที่ใช้: {time_taken or 'ไม่มี'}"
            ]
            if next_appointment_date_str and new_status == 'needsAction':
                report_lines.append(f"นัดลูกค้าอีกครั้ง: {next_appointment_date_str.replace('T', ' ')}")
            if all_attachment_urls:
                report_lines.append("ไฟล์แนบ: " + ", ".join(all_attachment_urls))

            report_summary_message_obj = TextMessage(text="\n".join(report_lines))
            recipients_for_summary_report = [LINE_ADMIN_GROUP_ID, LINE_MANAGER_USER_ID, LINE_HR_GROUP_ID] 
            send_message_to_recipients(report_summary_message_obj, recipients_for_summary_report)

            return redirect(url_for('summary'))
        else:
            app.logger.error(f"Failed to update task {task_id} via web form.")
            return "ไม่สามารถอัปเดตงานได้", 500

    return render_template('update_task_details.html', task=task)


@app.route('/summary')
def summary():
    """แสดงผลสรุป Task ที่ดึงมาจาก Google Tasks พร้อมสถิติที่คำนวณได้"""
    # ดึงงานทั้งหมดจาก Google Tasks (รวมงานที่เสร็จสิ้นแล้วด้วย)
    tasks_raw = get_google_tasks_for_report(show_completed=True) 

    tasks = []
    task_status_counts = {
        'needsAction': 0,
        'completed': 0,
        'overdue': 0,
        'total': 0
    }

    current_time_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    for task_item in tasks_raw:
        parsed_task = parse_google_task_dates(task_item) # เรียกใช้ฟังก์ชันตัวช่วย

        # กำหนดสถานะการแสดงผลและนับจำนวนงาน
        status = parsed_task.get('status', 'unknown')
        parsed_task['display_status'] = 'รอดำเนินการ' # สถานะการแสดงผลเริ่มต้น
        is_overdue = False

        if status == 'completed':
            parsed_task['display_status'] = 'เสร็จสิ้น'
            task_status_counts['completed'] += 1
        elif status == 'needsAction':
            task_status_counts['needsAction'] += 1
            # ตรวจสอบงานที่ค้างชำระ (Overdue)
            if parsed_task['due_formatted'] != 'N/A':
                try:
                    due_dt_utc = datetime.datetime.fromisoformat(task_item['due'].replace('Z', '+00:00')).replace(tzinfo=None)
                    if due_dt_utc < current_time_utc:
                        is_overdue = True
                        parsed_task['display_status'] = 'ค้างชำระ' # ค้างชำระ
                        task_status_counts['overdue'] += 1
                except ValueError:
                    pass # ไม่สนใจวันที่ที่ไม่สามารถแยกวิเคราะห์ได้

            # แยกข้อมูลสรุปงานจากช่างจากช่อง 'notes' (ถ้ามี)
        tech_report_data, attachment_urls, remaining_notes = parse_tech_report_from_notes(parsed_task.get('notes', ''))
        parsed_task['tech_report'] = tech_report_data
        parsed_task['attachment_urls'] = attachment_urls
        parsed_task['notes_display'] = remaining_notes # notes ส่วนที่เหลือที่ไม่ได้เป็น JSON

        tasks.append(parsed_task)
        task_status_counts['total'] += 1 # นับรวมใน total หลังจากประมวลผล status

    # จัดเรียงงานตามวันที่สร้าง (งานใหม่สุดอยู่บนสุด)
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

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """ให้บริการไฟล์ที่อัปโหลด"""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# --- บล็อกการดำเนินการหลัก ---
if __name__ == '__main__':
    # ตรวจสอบให้แน่ใจว่าโฟลเดอร์อัปโหลดมีอยู่เมื่อรันในเครื่อง
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    
    # บล็อกนี้สำหรับการพัฒนาในเครื่องเท่านั้น
    # บน Render, แอปจะถูกรันโดย Gunicorn หรือ WSGI server ที่คล้ายกัน
    # ดังนั้น app.run() จะไม่ถูกเรียก
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
