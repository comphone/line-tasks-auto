# File: config.py
import os
import pytz
TEXT_SNIPPETS = { ... }

# --- ค่าคงที่และตัวแปรพื้นฐาน ---
SCOPES = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/calendar.events', 'https://www.googleapis.com/auth/drive.file']
THAILAND_TZ = pytz.timezone('Asia/Bangkok')
SETTINGS_FILE = 'settings.json'
LOCATIONS_FILE = 'technician_locations.json'
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'kmz', 'kml'}
MAX_FILE_SIZE_MB = 500
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# --- Text Snippets ---
TEXT_SNIPPETS = {
    'task_details': [
        {'key': 'ล้างแอร์', 'value': 'ล้างทำความสะอาดเครื่องปรับอากาศ, ตรวจเช็คน้ำยา, วัดแรงดันไฟฟ้า และทำความสะอาดคอยล์ร้อน-เย็น'},
        {'key': 'ติดตั้งแอร์', 'value': 'ติดตั้งเครื่องปรับอากาศใหม่ ขนาด [ขนาด BTU] พร้อมเดินท่อน้ำยาและสายไฟ, ติดตั้งเบรกเกอร์'},
        # ... (เพิ่มรายการอื่นๆ ตามเดิม) ...
    ],
    'progress_reports': [
        {'key': 'ลูกค้าเลื่อนนัด', 'value': 'ลูกค้าขอเลื่อนนัดเป็นวันที่ [dd/mm/yyyy] เนื่องจากไม่สะดวก'},
        {'key': 'รออะไหล่', 'value': 'ตรวจสอบแล้วพบว่าต้องรออะไหล่ [ชื่ออะไหล่] จะแจ้งลูกค้าให้ทราบกำหนดการอีกครั้ง'},
        # ... (เพิ่มรายการอื่นๆ ตามเดิม) ...
    ]
}

# --- โหลดค่าจาก Environment Variables ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '').strip()
LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM')
LIFF_ID_TECHNICIAN_LOCATION = os.environ.get('LIFF_ID_TECHNICIAN_LOCATION')
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')
GOOGLE_TASKS_LIST_ID = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
LINE_RATE_LIMIT_PER_MINUTE = int(os.environ.get('LINE_RATE_LIMIT_PER_MINUTE', 100))