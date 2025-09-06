# File: utils.py (ฉบับแก้ไขสมบูรณ์)
import qrcode
import base64
from io import BytesIO
import re
import json
import pytz  # เพิ่มการ import pytz
import mimetypes
import os
from datetime import datetime, date
from dateutil.parser import parse as date_parse
from cachetools import cached, TTLCache
from collections import defaultdict
from flask import current_app
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

# สร้าง Cache และกำหนด Timezone ที่นี่เพื่อแก้ปัญหา Circular Import
util_cache = TTLCache(maxsize=100, ttl=60)
THAILAND_TZ = pytz.timezone('Asia/Bangkok')

# --- Settings Management Functions ---

SETTINGS_FILE = 'settings.json'
_DEFAULT_APP_SETTINGS_STORE = {
    'report_times': {
        'appointment_reminder_hour_thai': 7,
        'outstanding_report_hour_thai': 20,
        'customer_followup_hour_thai': 9
    },
    'line_recipients': {
        'admin_group_id': os.environ.get('LINE_ADMIN_GROUP_ID', ''),
        'technician_group_id': os.environ.get('LINE_TECHNICIAN_GROUP_ID', ''),
        'manager_user_id': ''
    },
    'equipment_catalog': [],
    'auto_backup': { 'enabled': False, 'hour_thai': 2, 'minute_thai': 0 },
    'shop_info': { 'contact_phone': '081-XXX-XXXX', 'line_id': '@ComphoneService' },
    'technician_list': [],
    'popup_notifications': {
        'enabled_arrival': False,
        'message_arrival_template': 'ช่าง [technician_name] กำลังจะถึงบ้านคุณ [customer_name] แล้วครับ/ค่ะ',
        'enabled_completion_customer': True,
        'message_completion_customer_template': 'งาน [task_title] ที่บ้านคุณ [customer_name] เสร็จเรียบร้อยแล้วครับ/ค่ะ',
        'enabled_nearby_job': False,
        'nearby_radius_km': 5,
        'message_nearby_template': 'มีงาน [task_title] อยู่ใกล้คุณ [distance_km] กม. ที่ [customer_name] สนใจรับงานหรือไม่?',
        'liff_popup_base_url': 'https://liff.line.me/2007690244-zBNe26ZO'
    },
    'technician_templates': {
        'task_details': [
            {'key': 'ล้างแอร์', 'value': 'ล้างทำความสะอาดเครื่องปรับอากาศ, ตรวจเช็คน้ำยา, วัดแรงดันไฟฟ้า และทำความสะอาดคอยล์ร้อน-เย็น'},
            {'key': 'ติดตั้งแอร์', 'value': 'ติดตั้งเครื่องปรับอากาศใหม่ ขนาด [ขนาด BTU] พร้อมเดินท่อน้ำยาและสายไฟ, ติดตั้งเบรกเกอร์'},
        ],
        'progress_reports': [
            {'key': 'ลูกค้าเลื่อนนัด', 'value': 'ลูกค้าขอเลื่อนนัดเป็นวันที่ [dd/mm/yyyy] เนื่องจากไม่สะดวก'},
            {'key': 'รออะไหล่', 'value': 'ตรวจสอบแล้วพบว่าต้องรออะไหล่ [ชื่ออะไหล่] จะแจ้งลูกค้าให้ทราบกำหนดการอีกครั้ง'},
        ]
    },
    'message_templates': {
        'welcome_customer': "เรียน คุณ[customer_name],\n\nขอบคุณที่เชื่อมต่อกับ Comphone ครับ/ค่ะ!\nเราจะใช้ LINE นี้เพื่อส่งข้อมูลสำคัญเกี่ยวกับบริการครับ\n\nติดต่อ:\nโทร: [shop_phone]\nLINE ID: [shop_line_id]",
        'problem_report_admin': "🚨 ลูกค้าแจ้งปัญหา!\n\nงาน: [task_title]\nลูกค้า: [customer_name]\nปัญหา: [problem_desc]\n\n🔗 ดูรายละเอียดงาน:\n[task_url]",
        'daily_reminder_header': "...",
        'daily_reminder_task_line': "..."
    },
    'product_categories': []
}

def load_settings_from_file():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            current_app.logger.error(f"Error handling settings.json: {e}")
            if os.path.exists(SETTINGS_FILE) and os.path.getsize(SETTINGS_FILE) == 0:
                os.remove(SETTINGS_FILE)
                current_app.logger.warning(f"Empty settings.json deleted. Using default settings.")
    return None

def save_settings_to_file(settings_data):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f: json.dump(settings_data, f, ensure_ascii=False, indent=4)
        return True
    except IOError as e:
        current_app.logger.error(f"Error writing to settings.json: {e}")
        return False

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

    equipment_catalog = app_settings.get('equipment_catalog', [])
    app_settings['common_equipment_items'] = sorted(list(set(item.get('item_name') for item in equipment_catalog if item.get('item_name'))))

    return app_settings


# --- Google API Helper Functions (Only for Drive) ---
def find_or_create_drive_folder(name, parent_id):
    from app import get_google_drive_service, _execute_google_api_call_with_retry
    service = get_google_drive_service()
    if not service:
        return None

    query = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    try:
        response = _execute_google_api_call_with_retry(service.files().list, q=query, spaces='drive', fields='files(id, name, parents)', pageSize=1)
        files = response.get('files', [])

        if files:
            folder_id = files[0]['id']
            current_app.logger.info(f"Found existing Drive folder '{name}' with ID: {folder_id}. Using this as the master.")
            return folder_id
        else:
            if not parent_id:
                current_app.logger.error(f"Cannot create folder '{name}': parent_id is missing.")
                return None
            current_app.logger.info(f"Folder '{name}' not found. Creating it under parent '{parent_id}'...")
            file_metadata = {
                'name': name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [parent_id]
            }
            folder = _execute_google_api_call_with_retry(service.files().create, body=file_metadata, fields='id')
            folder_id = folder.get('id')
            current_app.logger.info(f"Created new Drive folder '{name}' with ID: {folder_id}")
            return folder_id
    except HttpError as e:
        current_app.logger.error(f"Error finding or creating folder '{name}': {e}")
        return None

def perform_drive_upload(media_body, file_name, folder_id):
    """Performs the actual file upload to Google Drive and sets permissions."""
    from app import get_google_drive_service, _execute_google_api_call_with_retry
    service = get_google_drive_service()
    if not service or not folder_id:
        current_app.logger.error(f"Drive service or Folder ID not configured for upload of '{file_name}'.")
        return None

    try:
        file_metadata = {'name': file_name, 'parents': [folder_id]}
        file_obj = _execute_google_api_call_with_retry(
            service.files().create,
            body=file_metadata,
            media_body=media_body,
            fields='id, webViewLink'
        )
        uploaded_file_id = file_obj['id']

        _execute_google_api_call_with_retry(
            service.permissions().create,
            fileId=uploaded_file_id,
            body={'role': 'reader', 'type': 'anyone'}
        )
        return file_obj
    except Exception as e:
        current_app.logger.error(f'Unexpected error during Drive upload for {file_name}: {e}', exc_info=True)
        return None

def upload_data_from_memory_to_drive(data_in_memory, file_name, mime_type, folder_id):
    media = MediaIoBaseUpload(data_in_memory, mimetype=mime_type, resumable=True)
    file_obj = perform_drive_upload(media, file_name, folder_id)
    return file_obj

# --- Data Handling Functions (SQL-native) ---
def parse_db_customer_data(customer_db):
    """Parses data from a SQLAlchemy Customer object into a standard dictionary format."""
    return {
        'id': customer_db.id,
        'name': customer_db.name,
        'organization': customer_db.organization,
        'phone': customer_db.phone,
        'address': customer_db.address,
        'map_url': customer_db.map_url,
        'line_user_id': customer_db.line_user_id,
        'created_at': customer_db.created_at,
    }

def parse_db_job_data(job_db):
    """Parses data from a SQLAlchemy Job object into a standard dictionary format."""
    return {
        'id': job_db.id,
        'job_title': job_db.job_title,
        'job_type': job_db.job_type,
        'product_details': job_db.product_details,
        'assigned_technician': job_db.assigned_technician,
        'due_date': job_db.due_date,
        'completed_date': job_db.completed_date,
        'status': job_db.status,
        'created_date': job_db.created_date,
        'internal_notes': job_db.internal_notes,
    }

def parse_db_report_data(report_db):
    """Parses data from a SQLAlchemy Report object into a standard dictionary format."""
    return {
        'id': report_db.id,
        'summary_date': report_db.summary_date.isoformat() if report_db.summary_date else None,
        'report_type': report_db.report_type,
        'work_summary': report_db.work_summary,
        'technicians': report_db.technicians.split(',') if report_db.technicians else [],
        'is_internal': report_db.is_internal,
        'attachments': [
            {'id': att.drive_file_id, 'name': att.file_name, 'url': att.file_url}
            for att in report_db.attachments
        ]
    }

@cached(util_cache)
def get_customer_database():
    """Builds a unique customer database from all tasks."""
    from app import db, Customer
    current_app.logger.info("Building customer database via SQLAlchemy...")
    customers_db = Customer.query.order_by(Customer.created_at.desc()).all()
    customers_dict = {
        cust.name.lower(): parse_db_customer_data(cust) for cust in customers_db if cust.name
    }
    return list(customers_dict.values())

def get_technician_report_data(year, month):
    """
    ฟังก์ชันกลางสำหรับดึงและประมวลผลข้อมูลรายงานของช่างจากฐานข้อมูล SQL
    """
    from app import db, Job, Report
    app_settings = get_app_settings()
    technician_list = app_settings.get('technician_list', [])
    official_tech_names = {tech.get('name', '').strip() for tech in technician_list if tech.get('name')}

    start_date = datetime(year, month, 1, 0, 0, 0, tzinfo=THAILAND_TZ)
    end_date = start_date.replace(month=month % 12 + 1, day=1) if month < 12 else start_date.replace(year=year + 1, month=1, day=1)

    report_data = defaultdict(lambda: {'count': 0, 'tasks': []})

    reports_db = Report.query.filter(
        Report.report_type.in_(['report', 'reschedule']),
        Report.is_internal.is_(False),
        Report.summary_date >= start_date.astimezone(pytz.utc),
        Report.summary_date < end_date.astimezone(pytz.utc)
    ).order_by(Report.summary_date).all()

    jobs_with_reports = Job.query.filter(Job.reports.any()).all()

    for job_db in jobs_with_reports:
        if not job_db.completed_date:
            continue

        completed_dt = job_db.completed_date.astimezone(THAILAND_TZ)
        if completed_dt.year == year and completed_dt.month == month:
            task_techs = {name.strip() for name in job_db.assigned_technician.split(',')} if job_db.assigned_technician else set()

            for tech_name in sorted(list(task_techs)):
                if tech_name in official_tech_names:
                    report_data[tech_name]['count'] += 1
                    report_data[tech_name]['tasks'].append({
                        'id': job_db.id,
                        'title': job_db.job_title,
                        'customer_name': job_db.customer.name,
                        'completed_formatted': completed_dt.strftime("%d/%m/%Y")
                    })

    for tech_name in report_data:
        report_data[tech_name]['tasks'].sort(key=lambda x: x['completed_formatted'])

    return dict(sorted(report_data.items())), technician_list

def generate_qr_code_base64(data, box_size=10, border=4, fill_color='#28a745', back_color='#FFFFFF'):
    try:
        qr = qrcode.QRCode(version=1, box_size=box_size, border=border)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color=fill_color, back_color=back_color)
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffered.getvalue()).decode("utf-8")
    except Exception as e:
        current_app.logger.error(f"Error generating QR code: {e}")
        return ""