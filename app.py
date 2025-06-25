import os
from flask import Flask, request, render_template, redirect, url_for, abort, send_from_directory
from werkzeug.utils import secure_filename
import datetime
import re # สำหรับ regex ในการแยกพิกัดจาก Google Maps URL

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
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Google Tasks API Configuration
# ถ้าคุณเปลี่ยน SCOPES ให้ลบไฟล์ token.json
SCOPES = ['https://www.googleapis.com/auth/tasks']
GOOGLE_CREDENTIALS_FILE = 'credentials.json' # ตรวจสอบให้แน่ใจว่าไฟล์นี้อยู่ใน root directory

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_google_tasks_service():
    creds = None
    # The file token.json stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
                app.logger.error(f"Google credentials file not found: {GOOGLE_CREDENTIALS_FILE}")
                # ใน Production environment คุณอาจต้องหาวิธีจัดการไฟล์นี้อย่างปลอดภัย
                # หรือตั้งค่า OAuth ผ่าน environment variables
                return None
            flow = InstalledAppFlow.from_client_secrets_file(
                GOOGLE_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('tasks', 'v1', credentials=creds)

def create_google_task(title, notes=None, due=None):
    service = get_google_tasks_service()
    if not service:
        print("Failed to get Google Tasks service.")
        return None
    try:
        task_list_id = '@default'  # หรือ ID ของ Task List ที่คุณต้องการใช้

        task = {
            'title': title,
            'notes': notes
        }
        if due:
            # due time should be in RFC3339 format
            # e.g., '2025-07-01T10:00:00.000Z'
            task['due'] = due

        result = service.tasks().insert(tasklist=task_list_id, body=task).execute()
        print(f"Google Task created: {result['title']} ({result['id']})")
        return result
    except HttpError as err:
        print(f"Error creating Google Task: {err}")
        return None

@app.route('/', methods=['GET', 'POST'])
def form():
    if request.method == 'POST':
        topic = request.form.get('topic')
        customer = request.form.get('customer')
        phone = request.form.get('phone')
        address = request.form.get('address')
        appointment = request.form.get('appointment')
        latitude = request.form.get('latitude') # ดึงจาก hidden input
        longitude = request.form.get('longitude') # ดึงจาก hidden input
        detail = request.form.get('detail')
        file_url = None

        file = request.files.get('attachment')
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            file_url = filepath

        coord = f"{latitude},{longitude}" if latitude and longitude else ""

        with open("tasks_log.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now()}|{topic}|{customer}|{phone}|{address}|{appointment}|{coord}|{detail}|{file_url}\n")

        # สร้าง Google Task
        if topic:
            task_title = f"{topic} ({customer})" if customer else topic
            task_notes = f"โทร: {phone or '-'}\nที่อยู่: {address or '-'}\nรายละเอียด: {detail or '-'}"
            if appointment:
                task_notes += f"\nนัดหมาย: {appointment}"
            if coord and coord != ',':
                task_notes += f"\nพิกัด: https://www.google.com/maps/search/?api=1&query={coord}"

            # แปลง appointment ให้เป็น ISO 8601 สำหรับ Google Tasks API
            due_date_gmt = None
            if appointment:
                try:
                    # สมมติว่า appointment มาในรูปแบบ "Y-m-d H:i"
                    dt_obj = datetime.datetime.strptime(appointment, "%Y-%m-%d %H:%M")
                    # Google Tasks ต้องการเวลาในรูปแบบ RFC3339 พร้อม Timezone (Z สำหรับ GMT)
                    # อาจจะต้องพิจารณา Timezone ของผู้ใช้จริง
                    due_date_gmt = dt_obj.isoformat() + "Z"
                except ValueError:
                    app.logger.warning(f"Could not parse appointment date: {appointment}")

            create_google_task(task_title, task_notes, due=due_date_gmt)


        return redirect(url_for('summary'))

    return render_template('form.html')

@app.route('/summary')
def summary():
    tasks = []
    if os.path.exists("tasks_log.txt"):
        with open("tasks_log.txt", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) == 9:
                    dt, topic, customer, phone, address, appointment, coord, detail, file_url = parts
                    tasks.append({
                        "datetime": dt,
                        "topic": topic,
                        "customer": customer,
                        "phone": phone,
                        "address": address,
                        "appointment": appointment,
                        "coord": coord,
                        "detail": detail,
                        "file_url": file_url
                    })
    # เรียงลำดับ tasks ตาม datetime จากใหม่ไปเก่า
    tasks.sort(key=lambda x: datetime.datetime.strptime(x["datetime"].split('.')[0], "%Y-%m-%d %H:%M:%S"), reverse=True)
    return render_template("tasks_summary.html", tasks=tasks)

# LINE Callback Route
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
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
    # รูปแบบข้อความ LINE ตัวอย่าง:
    # task:หัวข้อ|ลูกค้า|เบอร์โทร|ที่อยู่|วันเวลา_นัดหมาย(YYYY-MM-DD HH:MM)|ละติจูด,ลองจิจูด|รายละเอียด
    # task:ไปหาคุณสมศักดิ์|สมศักดิ์ สุขใจ|0812345678|123 ถนนสุขุมวิท|2025-07-01 10:30|13.7563,100.5018|แจ้งเรื่องเอกสารใหม่
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

        latitude = ""
        longitude = ""
        if coord_str:
            # พยายามแยกพิกัดจาก string หรือ URL
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
                # ถ้าไม่ใช่ URL ลองแยกแบบ lat,long
                coords_parts = coord_str.split(',')
                if len(coords_parts) == 2:
                    try:
                        latitude = str(float(coords_parts[0].strip()))
                        longitude = str(float(coords_parts[1].strip()))
                    except ValueError:
                        pass # ไม่ใช่รูปแบบพิกัดที่ถูกต้อง

        final_coord = f"{latitude},{longitude}" if latitude and longitude else coord_str # เก็บ original string ถ้าแยกไม่ได้

        with open("tasks_log.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now()}|{topic}|{customer}|{phone}|{address}|{appointment}|{final_coord}|{detail}|None\n")

        # สร้าง Google Task จาก LINE message
        task_title = f"{topic} (LINE)"
        task_notes = f"ลูกค้า: {customer or '-'}\nโทร: {phone or '-'}\nที่อยู่: {address or '-'}\nรายละเอียด: {detail or '-'}"
        if appointment:
            task_notes += f"\nนัดหมาย: {appointment}"
        if final_coord and final_coord != ',':
            task_notes += f"\nพิกัด: https://www.google.com/maps/search/?api=1&query={final_coord}"

        due_date_gmt = None
        if appointment:
            try:
                dt_obj = datetime.datetime.strptime(appointment, "%Y-%m-%d %H:%M")
                due_date_gmt = dt_obj.isoformat() + "Z"
            except ValueError:
                app.logger.warning(f"Could not parse LINE appointment date: {appointment}")

        create_google_task(task_title, task_notes, due=due_date_gmt)


        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"Task '{topic}' ได้รับการบันทึกแล้ว!")
        )
    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="กรุณาส่งข้อความในรูปแบบที่ถูกต้อง เช่น 'task:หัวข้อ|ลูกค้า|เบอร์โทร|ที่อยู่|วันเวลา_นัดหมาย(YYYY-MM-DD HH:MM)|ละติจูด,ลองจิจูด|รายละเอียด'")
        )


if __name__ == '__main__':
    # สำหรับการรันบน Local development
    # ตรวจสอบให้แน่ใจว่าได้ตั้งค่า LINE_CHANNEL_ACCESS_TOKEN และ LINE_CHANNEL_SECRET ใน Environment Variables
    # หรือในไฟล์ .env และใช้ python-dotenv ในการโหลด
    # app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
