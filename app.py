import os
from flask import Flask, request, render_template, redirect, url_for, abort, send_from_directory
from werkzeug.utils import secure_filename
import datetime
import re
import json # เพื่อช่วยในการจัดการไฟล์ credentials.json หรือ token.json

# สำหรับ LINE Messaging API
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# สำหรับ Google Tasks API
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

app = Flask(__name__)
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf'}

# สร้างโฟลเดอร์ UPLOAD_FOLDER ถ้ายังไม่มี
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# LINE Bot API Configuration
# ดึงจาก Environment Variables เพื่อความปลอดภัย
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
# กำหนด LINE Admin Group ID ที่จะใช้ส่งรายงานอัตโนมัติ
YOUR_ADMIN_GROUP_ID = os.environ.get('LINE_ADMIN_GROUP_ID', 'YOUR_LINE_ADMIN_GROUP_ID_HERE') # *** แก้ไขตรงนี้ด้วย Group ID จริงๆ ***

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Google Tasks API Configuration
SCOPES = ['https://www.googleapis.com/auth/tasks']
GOOGLE_CREDENTIALS_FILE = 'credentials.json'

# ฟังก์ชันสำหรับจัดการ Google Authentication
def get_google_tasks_service():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
                google_credentials_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
                if google_credentials_json:
                    try:
                        with open(GOOGLE_CREDENTIALS_FILE, 'w') as f:
                            f.write(google_credentials_json)
                        app.logger.info("Created credentials.json from GOOGLE_CREDENTIALS_JSON env var.")
                    except Exception as e:
                        app.logger.error(f"Error creating credentials.json from env var: {e}")
                        return None
                else:
                    app.logger.error(f"Google credentials file not found: {GOOGLE_CREDENTIALS_FILE} and GOOGLE_CREDENTIALS_JSON env var is not set.")
                    return None
            
            try:
                flow = InstalledAppFlow.from_client_secrets_file(
                    GOOGLE_CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0) # อาจจะต้องรันบน local ก่อนเพื่อให้ได้ token.json
            except Exception as e:
                app.logger.error(f"Error during Google OAuth flow: {e}. Ensure credentials.json is valid or token.json exists.")
                return None
        
        if creds:
            with open('token.json', 'w') as token:
                token.write(creds.to_json())
            app.logger.info("Google token.json saved.")

    if creds:
        return build('tasks', 'v1', credentials=creds)
    return None

def create_google_task(title, notes=None, due=None):
    service = get_google_tasks_service()
    if not service:
        print("Failed to get Google Tasks service.")
        return None
    try:
        task_list_id = '@default'
        task_body = {
            'title': title,
            'notes': notes,
            'status': 'needsAction' # Default status for new tasks
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
    service = get_google_tasks_service()
    if not service:
        app.logger.error("Failed to get Google Tasks service for update.")
        return None
    try:
        task_list_id = '@default'
        current_task = service.tasks().get(tasklist=task_list_id, task=task_id).execute()
        
        if title:
            current_task['title'] = title
        if notes is not None: # Allow notes to be empty string
            current_task['notes'] = notes
        if due:
            current_task['due'] = due
        
        if status:
            current_task['status'] = status
            if status == 'completed':
                current_task['completed'] = datetime.datetime.now().isoformat() + 'Z'
            elif 'completed' in current_task: # If changing from completed, remove completed timestamp
                del current_task['completed']

        result = service.tasks().update(tasklist=task_list_id, task=task_id, body=current_task).execute()
        app.logger.info(f"Google Task {task_id} updated. New Status: {result.get('status')}")
        return result
    except HttpError as err:
        app.logger.error(f"Error updating Google Task {task_id}: {err}")
        return None

# ฟังก์ชันสำหรับดึง Tasks จาก Google Tasks (สำหรับรายงาน)
def get_google_tasks_for_report(show_completed=False, due_min=None, due_max=None):
    service = get_google_tasks_service()
    if not service:
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
    today_end = datetime.datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
    
    # Fetch tasks that are not completed. 'dueMax' covers tasks due by end of today.
    # Google Task status 'needsAction' is for uncompleted tasks.
    outstanding_tasks = get_google_tasks_for_report(
        show_completed=False,
        due_max=today_end.isoformat(timespec='milliseconds') + "Z"
    )
    
    return outstanding_tasks

def get_daily_summary_tasks():
    today_start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = datetime.datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)

    all_tasks = get_google_tasks_for_report(show_completed=True) # Needs to be True to get completed tasks

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

        # Check if created today or completed today
        if (created_dt and today_start <= created_dt <= today_end) or \
           (completed_dt and today_start <= completed_dt <= today_end):
            daily_tasks.append(task)
            
    return daily_tasks

# ฟังก์ชันสำหรับส่งรายงานประจำวัน (ต้องถูกเรียกใช้โดย Cron Job)
def send_daily_reports():
    current_hour = datetime.datetime.now().hour
    report_message = ""

    # รายงานงานค้างประจำวันตอน 6:00 น.
    if current_hour == 6:
        outstanding_tasks = get_daily_outstanding_tasks()
        report_message = "--- รายงานงานค้างประจำวัน ---\n"
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
                report_message += f"- {title} (Due: {due_date_str})\n"
        else:
            report_message += "ไม่มีงานค้างในวันนี้\n"
        
        app.logger.info(f"Sending daily outstanding tasks report: {report_message}")
        if YOUR_ADMIN_GROUP_ID != 'YOUR_LINE_ADMIN_GROUP_ID_HERE':
            line_bot_api.push_message(YOUR_ADMIN_GROUP_ID, TextSendMessage(text=report_message))
        else:
            app.logger.warning("LINE_ADMIN_GROUP_ID is not set. Cannot send outstanding tasks report.")

    # สรุปงานภายในวันตอน 20:00 น.
    elif current_hour == 20:
        summary_tasks = get_daily_summary_tasks()
        report_message = "--- สรุปงานประจำวัน ---\n"
        if summary_tasks:
            for task in summary_tasks:
                title = task.get('title', 'N/A')
                status = task.get('status', 'unknown')
                report_message += f"- {title} (สถานะ: {'เสร็จสิ้น' if status == 'completed' else 'ค้าง'})\n"
        else:
            report_message += "ไม่มีงานที่ถูกสร้างหรือเสร็จสิ้นในวันนี้\n"
        
        app.logger.info(f"Sending daily summary tasks report: {report_message}")
        if YOUR_ADMIN_GROUP_ID != 'YOUR_LINE_ADMIN_GROUP_ID_HERE':
            line_bot_api.push_message(YOUR_ADMIN_GROUP_ID, TextSendMessage(text=report_message))
        else:
            app.logger.warning("LINE_ADMIN_GROUP_ID is not set. Cannot send daily summary report.")


@app.route('/', methods=['GET', 'POST'])
def form():
    if request.method == 'POST':
        topic = request.form.get('topic')
        customer = request.form.get('customer')
        phone = request.form.get('phone')
        address = request.form.get('address')
        appointment = request.form.get('appointment')
        latitude = request.form.get('latitude')
        longitude = request.form.get('longitude')
        detail = request.form.get('detail')
        task_status = "PENDING" # กำหนดสถานะเริ่มต้น

        # --- จัดการการอัปโหลดไฟล์หลายภาพ ---
        file_urls = [] # List to store relative paths of all uploaded files
        files = request.files.getlist('attachments') # Get all files from the 'attachments' input
        for file in files:
            if file and allowed_file(file.filename):
                timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
                original_filename = secure_filename(file.filename)
                unique_filename = f"{timestamp}_{original_filename}"
                
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
                file.save(filepath)
                # Store path relative to the static folder for easier template rendering
                file_urls.append(os.path.join('static', 'uploads', unique_filename)) 
        
        file_urls_str = ",".join(file_urls) # Convert list of paths to comma-separated string

        coord = f"{latitude},{longitude}" if latitude and longitude else ""

        # บันทึกลง tasks_log.txt พร้อมสถานะและ file_urls_str
        with open("tasks_log.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now()}|{topic}|{customer}|{phone}|{address}|{appointment}|{coord}|{detail}|{file_urls_str}|{task_status}\n")

        # สร้าง Google Task
        if topic:
            task_title = f"{topic} ({customer})" if customer else topic
            
            # สร้าง notes สำหรับ Google Task รวมถึงลิงก์รูปภาพ
            google_task_notes = f"โทร: {phone or '-'}\nที่อยู่: {address or '-'}\nรายละเอียด: {detail or '-'}"
            if appointment:
                google_task_notes += f"\nนัดหมาย: {appointment}"
            if coord and coord != ',':
                # ใช้ Google Maps URL ที่ดีกว่า
                google_task_notes += f"\nพิกัด: http://maps.google.com/?q={latitude},{longitude}"
            if file_urls:
                # สร้าง Full External URL สำหรับแต่ละไฟล์เพื่อใส่ใน Google Tasks notes
                full_file_urls = []
                for f_url in file_urls:
                    # Replace 'static/' with empty string to get filename for url_for
                    # This relies on the app's base URL being known for _external=True
                    # which Flask handles during runtime.
                    full_file_urls.append(url_for('static', filename=f_url.replace('static/', ''), _external=True))
                google_task_notes += f"\nไฟล์แนบ: {', '.join(full_file_urls)}"

            due_date_gmt = None
            if appointment:
                try:
                    dt_obj = datetime.datetime.strptime(appointment, "%Y-%m-%d %H:%M")
                    due_date_gmt = dt_obj.isoformat() + "Z"
                except ValueError:
                    app.logger.warning(f"Could not parse appointment date: {appointment}")

            create_google_task(task_title, google_task_notes, due=due_date_gmt)

        return redirect(url_for('summary'))

    return render_template('form.html')

@app.route('/summary')
def summary():
    tasks = []
    if os.path.exists("tasks_log.txt"):
        with open("tasks_log.txt", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split('|')
                # ตรวจสอบจำนวนส่วนที่ถูกต้อง (ต้องมี 10 ส่วนสำหรับ status และ file_urls_str)
                if len(parts) >= 10: 
                    dt, topic, customer, phone, address, appointment, coord, detail, file_urls_str, task_status = parts[:10]
                elif len(parts) == 9: # รองรับไฟล์เก่าที่ยังไม่มี status
                    dt, topic, customer, phone, address, appointment, coord, detail, file_urls_str = parts[:9]
                    task_status = "PENDING" # Default for old entries
                else:
                    app.logger.warning(f"Skipping malformed line in tasks_log.txt: {line.strip()}")
                    continue # ข้ามบรรทัดที่ไม่ถูกต้อง

                # แปลง file_urls_str ให้เป็น list
                file_urls_list = file_urls_str.split(',') if file_urls_str and file_urls_str != 'None' else []
                
                tasks.append({
                    "datetime": dt,
                    "topic": topic,
                    "customer": customer,
                    "phone": phone,
                    "address": address,
                    "appointment": appointment,
                    "coord": coord,
                    "detail": detail,
                    "file_urls": file_urls_list, # Pass list of URLs to template
                    "status": task_status
                })
    tasks.sort(key=lambda x: datetime.datetime.strptime(x["datetime"].split('.')[0], "%Y-%m-%d %H:%M:%S"), reverse=True)
    return render_template("tasks_summary.html", tasks=tasks)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text_message = event.message.text
    # รูปแบบข้อความ LINE สำหรับสร้าง Task:
    # task:หัวข้อ|ลูกค้า|เบอร์โทร|ที่อยู่|วันเวลา_นัดหมาย(YYYY-MM-DD HH:MM)|ละติจูด,ลองจิจูด|รายละเอียด
    app.logger.info(f"Received LINE message: {text_message}")

    if text_message.lower().startswith("task:"):
        parts_str = text_message[len("task:"):]
        parts = parts_str.split('|')

        topic = parts[0].strip() if len(parts) > 0 else "No Topic"
        customer = parts[1].strip() if len(parts) > 1 else ""
        phone = parts[2].strip() if len(parts) > 2 else ""
        address = parts[3].strip() if len(parts) > 3 else ""
        appointment = parts[4].strip() if len(parts) > 4 else ""
        coord_str = parts[5].strip() if len(parts) > 5 else ""
        detail = parts[6].strip() if len(parts) > 6 else ""
        task_status = "PENDING" # สถานะเริ่มต้นสำหรับ Task ที่สร้างจาก LINE

        latitude = ""
        longitude = ""
        if coord_str:
            map_url_regex = r"(?:@(-?\d+\.\d+),(-?\d+\.\d+))|(?:\/maps\/place\/(?:[^/]+\/)?@(-?\d+\.\d+),(-?\d+\.\d+))"
            match = re.search(map_url_regex, coord_str)
            if match:
                if match.group(1) and match.group(2):
                    latitude = match.group(1)
                    longitude = match.group(2)
                elif match.group(3) and match.group(4):
                    latitude = match.group(3)
                    longitude = match.group(4)
            else:
                coords_parts = coord_str.split(',')
                if len(coords_parts) == 2:
                    try:
                        latitude = str(float(coords_parts[0].strip()))
                        longitude = str(float(coords_parts[1].strip()))
                    except ValueError:
                        pass

        final_coord = f"{latitude},{longitude}" if latitude and longitude else coord_str

        # บันทึกลง tasks_log.txt พร้อมสถานะ (ไม่มีไฟล์แนบจาก LINE command)
        with open("tasks_log.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now()}|{topic}|{customer}|{phone}|{address}|{appointment}|{final_coord}|{detail}|None|{task_status}\n")

        # สร้าง Google Task จาก LINE message
        task_title = f"{topic} (จาก LINE)"
        task_notes = f"ลูกค้า: {customer or '-'}\nโทร: {phone or '-'}\nที่อยู่: {address or '-'}\nรายละเอียด: {detail or '-'}"
        if appointment:
            task_notes += f"\nนัดหมาย: {appointment}"
        if final_coord and final_coord != ',':
            task_notes += f"\nพิกัด: http://maps.google.com/?q={latitude},{longitude}"

        due_date_gmt = None
        if appointment:
            try:
                dt_obj = datetime.datetime.strptime(appointment, "%Y-%m-%d %H:%M")
                due_date_gmt = dt_obj.isoformat() + "Z"
            except ValueError:
                app.logger.warning(f"Could not parse LINE appointment date: {appointment}")

        created_task = create_google_task(task_title, task_notes, due=due_date_gmt)
        
        # แจ้งผู้ใช้พร้อม Google Task ID (หากสร้างสำเร็จ)
        if created_task:
            reply_message = f"Task '{topic}' ได้รับการบันทึกแล้ว! (ID: {created_task.get('id')})"
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_message)
            )
            # อาจส่งแจ้งเตือน ID นี้ไปยังกลุ่มช่างด้วย
            if YOUR_ADMIN_GROUP_ID != 'YOUR_LINE_ADMIN_GROUP_ID_HERE':
                 line_bot_api.push_message(
                     YOUR_ADMIN_GROUP_ID,
                     TextSendMessage(text=f"งานใหม่ถูกสร้าง: {topic}\nID สำหรับสรุปงาน: {created_task.get('id')}\n(ใช้คำสั่ง 'complete {created_task.get('id')}: สรุป | อุปกรณ์ | เวลา')")
                 )
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="เกิดข้อผิดพลาดในการสร้าง Task กรุณาลองใหม่"))

    # --- คำสั่งสำหรับสรุปงานของช่าง (ผ่าน LINE command) ---
    elif text_message.lower().startswith("complete "):
        try:
            # รูปแบบคำสั่ง: "complete <Google_Task_ID>: สรุปผลการทำงาน | รายการอุปกรณ์ที่ใช้ | ระยะเวลาที่ทำเสร็จ"
            command_body = text_message[len("complete "):].strip()
            
            # แยก Google Task ID และส่วนของรายละเอียด
            parts_colon = command_body.split(':', 1)
            google_task_id = parts_colon[0].strip()

            if len(parts_colon) > 1:
                summary_detail = parts_colon[1].strip() # "สรุปผล | อุปกรณ์ | ระยะเวลา"
                summary_parts = summary_detail.split('|', 2) # แยกเป็น 3 ส่วน
                
                work_summary = summary_parts[0].strip() if len(summary_parts) > 0 else ""
                equipment_used = summary_parts[1].strip() if len(summary_parts) > 1 else ""
                time_taken = summary_parts[2].strip() if len(summary_parts) > 2 else ""
            else:
                work_summary = ""
                equipment_used = ""
                time_taken = ""
            
            # เตรียม Notes ที่จะเพิ่มใน Google Task
            summary_notes_text = f"\n\n--- สรุปงานโดยช่าง ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ---\n" \
                                 f"สรุปผลการทำงาน: {work_summary or '-'}\n" \
                                 f"รายการอุปกรณ์ที่ใช้: {equipment_used or '-'}\n" \
                                 f"ระยะเวลาที่ทำเสร็จ: {time_taken or '-'}\n"

            # ดึง Task เดิมเพื่อเพิ่ม Notes และเปลี่ยนสถานะ
            service = get_google_tasks_service()
            if service:
                task_list_id = '@default'
                try:
                    existing_task = service.tasks().get(tasklist=task_list_id, task=google_task_id).execute()
                except HttpError as e:
                    if e.resp.status == 404:
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"ไม่พบ Task ID: {google_task_id}"))
                        app.logger.warning(f"Task ID {google_task_id} not found for complete command.")
                        return
                    else:
                        raise e # Re-raise other HTTP errors

                # Append summary notes to existing notes
                current_notes = existing_task.get('notes', '')
                new_notes = current_notes + summary_notes_text if current_notes else summary_notes_text
                
                updated_task = update_google_task(
                    task_id=google_task_id,
                    notes=new_notes,
                    status='completed' # เปลี่ยนสถานะเป็นเสร็จสิ้น
                )

                if updated_task:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"งาน '{updated_task.get('title', 'N/A')}' (ID: {google_task_id}) ได้รับการสรุปและทำเครื่องหมายว่าเสร็จสิ้นแล้ว!"))

                    # ส่งสรุปงานไปยัง LINE Group สำหรับทุกคนทราบ
                    admin_report_message = f"--- รายงานสรุปงานจากช่าง (LINE) ---\n" \
                                           f"Task ID: {google_task_id}\n" \
                                           f"หัวข้อ: {updated_task.get('title', 'N/A')}\n" \
                                           f"สรุปผล: {work_summary or '-'}\n" \
                                           f"อุปกรณ์ที่ใช้: {equipment_used or '-'}\n" \
                                           f"ระยะเวลา: {time_taken or '-'}\n" \
                                           f"เวลาสรุป: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n" \
                                           f"สถานะ: เสร็จสิ้น\n"
                    
                    if YOUR_ADMIN_GROUP_ID != 'YOUR_LINE_ADMIN_GROUP_ID_HERE':
                        line_bot_api.push_message(YOUR_ADMIN_GROUP_ID, TextSendMessage(text=admin_report_message))
                    else:
                        app.logger.warning("LINE_ADMIN_GROUP_ID is not set. Cannot send technician summary report.")

                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ไม่สามารถอัปเดต Task ใน Google Tasks ได้."))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ไม่สามารถเชื่อมต่อ Google Tasks ได้ในขณะนี้"))

        except Exception as e:
            app.logger.error(f"Error processing complete command: {e}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="รูปแบบคำสั่งไม่ถูกต้องหรือเกิดข้อผิดพลาด. โปรดใช้รูปแบบ 'complete <Google_Task_ID>: สรุปผล | อุปกรณ์ | ระยะเวลา'"))
    
    # --- ข้อความตอบกลับสำหรับคำสั่งที่ไม่รู้จัก ---
    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="กรุณาส่งข้อความในรูปแบบที่ถูกต้อง เช่น 'task:หัวข้อ|ลูกค้า|เบอร์โทร...' หรือ 'complete <Google_Task_ID>: สรุปผล | อุปกรณ์ | ระยะเวลา'")
        )


if __name__ == '__main__':
    # สำหรับการรันบน Local development
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
