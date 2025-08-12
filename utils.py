# File: utils.py
import os
import json
import datetime
import pytz
import re
import zipfile
from io import BytesIO
from collections import defaultdict
import qrcode
import base64
from dateutil.parser import parse as date_parse

from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from google.auth.transport.requests import Request

from config import (
    SCOPES, THAILAND_TZ, SETTINGS_FILE, LOCATIONS_FILE, 
    GOOGLE_TASKS_LIST_ID, GOOGLE_DRIVE_FOLDER_ID
)

# --- Google API Service Functions ---
def get_google_credentials():
    """ฟังก์ชันสำหรับดึง Google Credentials"""
    SERVICE_ACCOUNT_FILE_CONTENT = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    if SERVICE_ACCOUNT_FILE_CONTENT:
        try:
            info = json.loads(SERVICE_ACCOUNT_FILE_CONTENT)
            return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception as e:
            print(f"Error loading Service Account: {e}")
            return None

    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')
    if not google_token_json_str:
        return None
    
    try:
        creds = Credentials.from_authorized_user_info(json.loads(google_token_json_str), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds
    except Exception as e:
        print(f"Error loading User Credentials: {e}")
        return None

def safe_execute(request):
    """ปลอดภัยในการเรียก Google API"""
    try:
        return request.execute()
    except HttpError as e:
        print(f"Google API Error: {e}")
        return None

def get_google_tasks_service():
    """สร้าง Google Tasks service"""
    creds = get_google_credentials()
    if not creds:
        return None
    try:
        return build('tasks', 'v1', credentials=creds)
    except Exception as e:
        print(f"Error creating Tasks service: {e}")
        return None

def get_google_drive_service():
    """สร้าง Google Drive service"""
    creds = get_google_credentials()
    if not creds:
        return None
    try:
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"Error creating Drive service: {e}")
        return None

# --- Settings Management ---
def get_app_settings():
    """โหลดการตั้งค่าจากไฟล์"""
    default_settings = {
        'line_recipients': {
            'admin_group_id': '',
            'technician_group_id': '',
            'manager_user_id': ''
        },
        'shop_info': {
            'contact_phone': '',
            'line_id': '@your-oa-id'
        },
        'technician_list': [],
        'equipment_catalog': [],
        'report_times': {
            'appointment_reminder_hour_thai': 7,
            'outstanding_report_hour_thai': 18,
            'customer_followup_hour_thai': 9
        },
        'message_templates': {
            'welcome_customer': 'สวัสดีครับ/ค่ะ [customer_name] ขอบคุณที่ใช้บริการ [shop_phone]',
            'problem_report_admin': '🚨 ลูกค้ารายงานปัญหา\n\nงาน: [task_title]\nลูกค้า: [customer_name]\nปัญหา: [problem_desc]\n\nดูรายละเอียด: [task_url]',
            'daily_reminder_header': '📋 สรุปงานประจำวัน ([task_count] งาน)',
            'daily_reminder_task_line': '🔔 งานวันนี้\n\nชื่องาน: [task_title]\n👤 ลูกค้า: [customer_name]\n📞 โทร: [customer_phone]\n🗓️ นัดหมาย: [due_date]\n📍 พิกัด: [map_url]\n\n🔗 ดูรายละเอียด/แก้ไข:\n[task_url]'
        },
        'popup_notifications': {
            'enabled_arrival': False,
            'enabled_completion_customer': False,
            'enabled_nearby_job': False,
            'liff_popup_base_url': '',
            'nearby_radius_km': 5,
            'message_arrival_template': 'ช่าง [technician_name] กำลังจะถึงครับ/ค่ะ คุณ [customer_name]',
            'message_completion_customer_template': 'งาน [task_title] ที่บ้านคุณ [customer_name] เสร็จเรียบร้อยแล้วครับ/ค่ะ',
            'message_nearby_template': 'มีงานใกล้เคียง [distance_km] กม. - [task_title] (ลูกค้า: [customer_name])'
        },
        'auto_backup': {
            'enabled': False,
            'hour_thai': 2,
            'minute_thai': 0
        }
    }
    
    if not os.path.exists(SETTINGS_FILE):
        save_app_settings(default_settings)
        return default_settings
    
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            saved_settings = json.load(f)
        
        # Merge with defaults to ensure all keys exist
        def merge_settings(default, saved):
            result = default.copy()
            for key, value in saved.items():
                if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                    result[key] = merge_settings(result[key], value)
                else:
                    result[key] = value
            return result
        
        return merge_settings(default_settings, saved_settings)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error loading settings: {e}")
        return default_settings

def save_app_settings(settings):
    """บันทึกการตั้งค่าลงไฟล์"""
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=4)
        return True
    except Exception as e:
        print(f"Error saving settings: {e}")
        return False

# --- Task Management ---
def get_single_task(task_id):
    """ดึงข้อมูลงานเดียว"""
    service = get_google_tasks_service()
    if not service:
        return None
    try:
        return safe_execute(service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id))
    except Exception as e:
        print(f"Error getting single task: {e}")
        return None

def get_google_tasks_for_report(show_completed=True):
    """ดึงรายการงานทั้งหมด"""
    service = get_google_tasks_service()
    if not service:
        return None
    try:
        results = safe_execute(service.tasks().list(
            tasklist=GOOGLE_TASKS_LIST_ID, 
            showCompleted=show_completed, 
            maxResults=100
        ))
        return results.get('items', []) if results else []
    except Exception as e:
        print(f"Error getting tasks: {e}")
        return []

def create_google_task(title, notes=None, due=None):
    """สร้างงานใหม่"""
    service = get_google_tasks_service()
    if not service:
        return None
    try:
        task_body = {'title': title, 'notes': notes, 'status': 'needsAction'}
        if due:
            task_body['due'] = due
        return safe_execute(service.tasks().insert(tasklist=GOOGLE_TASKS_LIST_ID, body=task_body))
    except Exception as e:
        print(f"Error creating task: {e}")
        return None

def update_google_task(task_id, title=None, notes=None, status=None, due=None):
    """อัปเดตงาน"""
    service = get_google_tasks_service()
    if not service:
        return None
    try:
        task = safe_execute(service.tasks().get(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id))
        if not task:
            return None
        
        if title is not None:
            task['title'] = title
        if notes is not None:
            task['notes'] = notes
        if status is not None:
            task['status'] = status
            if status == 'completed':
                task['completed'] = datetime.datetime.now(pytz.utc).isoformat().replace('+00:00', 'Z')
                task['due'] = None
            else:
                task.pop('completed', None)
                if due is not None:
                    task['due'] = due
        
        return safe_execute(service.tasks().update(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id, body=task))
    except Exception as e:
        print(f"Error updating task: {e}")
        return None

def delete_google_task(task_id):
    """ลบงาน"""
    service = get_google_tasks_service()
    if not service:
        return False
    try:
        safe_execute(service.tasks().delete(tasklist=GOOGLE_TASKS_LIST_ID, task=task_id))
        return True
    except Exception as e:
        print(f"Error deleting task: {e}")
        return False

# --- Data Parsing Functions ---
def parse_google_task_dates(task):
    """แปลงวันที่ในงานให้เป็นรูปแบบที่ใช้งานได้"""
    task_copy = task.copy()
    
    # Parse due date
    if task.get('due'):
        try:
            due_dt_utc = date_parse(task['due'])
            due_dt_local = due_dt_utc.astimezone(THAILAND_TZ)
            task_copy['due_formatted'] = due_dt_local.strftime('%d/%m/%Y %H:%M')
            task_copy['due_for_input'] = due_dt_local.strftime('%Y-%m-%dT%H:%M')
        except (ValueError, TypeError):
            task_copy['due_formatted'] = 'วันที่ไม่ถูกต้อง'
            task_copy['due_for_input'] = ''
    else:
        task_copy['due_formatted'] = None
        task_copy['due_for_input'] = ''
    
    # Parse completed date
    if task.get('completed'):
        try:
            completed_dt_utc = date_parse(task['completed'])
            completed_dt_local = completed_dt_utc.astimezone(THAILAND_TZ)
            task_copy['completed_formatted'] = completed_dt_local.strftime('%d/%m/%Y %H:%M')
        except (ValueError, TypeError):
            task_copy['completed_formatted'] = 'วันที่ไม่ถูกต้อง'
    else:
        task_copy['completed_formatted'] = None
    
    # Parse created date
    if task.get('created'):
        try:
            created_dt_utc = date_parse(task['created'])
            created_dt_local = created_dt_utc.astimezone(THAILAND_TZ)
            task_copy['created_formatted'] = created_dt_local.strftime('%d/%m/%Y %H:%M')
        except (ValueError, TypeError):
            task_copy['created_formatted'] = 'วันที่ไม่ถูกต้อง'
    else:
        task_copy['created_formatted'] = None
    
    return task_copy

def parse_customer_info_from_notes(notes):
    """แยกข้อมูลลูกค้าจาก notes"""
    info = {
        'name': '',
        'phone': '',
        'address': '',
        'map_url': None,
        'organization': ''
    }
    
    if not notes:
        return info
    
    # Extract organization
    org_match = re.search(r'หน่วยงาน:\s*(.*)', notes, re.IGNORECASE)
    if org_match:
        info['organization'] = org_match.group(1).strip()
    
    # Extract customer name
    name_match = re.search(r'ลูกค้า:\s*(.*)', notes, re.IGNORECASE)
    if name_match:
        info['name'] = name_match.group(1).strip()
    
    # Extract phone
    phone_match = re.search(r'เบอร์โทรศัพท์:\s*(.*)', notes, re.IGNORECASE)
    if phone_match:
        info['phone'] = phone_match.group(1).strip()
    
    # Extract address
    address_match = re.search(r'ที่อยู่:\s*(.*)', notes, re.IGNORECASE)
    if address_match:
        info['address'] = address_match.group(1).strip()
    
    # Extract map URL
    url_match = re.search(r'https?://[^\s]+', notes)
    if url_match:
        info['map_url'] = url_match.group(0)
    
    return info

def parse_tech_report_from_notes(notes):
    """แยกรายงานช่างจาก notes"""
    history = []
    base_notes = notes or ''
    
    if not notes:
        return history, base_notes
    
    # Find all tech reports
    report_pattern = r'--- TECH_REPORT_START ---\s*(.*?)\s*--- TECH_REPORT_END ---'
    matches = re.findall(report_pattern, notes, re.DOTALL)
    
    for match in matches:
        try:
            report_data = json.loads(match.strip())
            history.append(report_data)
        except json.JSONDecodeError:
            continue
    
    # Remove tech reports from base notes
    base_notes = re.sub(report_pattern, '', notes, flags=re.DOTALL)
    
    # Remove customer feedback section
    feedback_pattern = r'--- CUSTOMER_FEEDBACK_START ---.*?--- CUSTOMER_FEEDBACK_END ---'
    base_notes = re.sub(feedback_pattern, '', base_notes, flags=re.DOTALL)
    
    return history, base_notes.strip()

def parse_customer_feedback_from_notes(notes):
    """แยกข้อมูล feedback ลูกค้าจาก notes"""
    feedback_data = {}
    if not notes:
        return feedback_data
    
    feedback_match = re.search(
        r'--- CUSTOMER_FEEDBACK_START ---\s*(.*?)\s*--- CUSTOMER_FEEDBACK_END ---', 
        notes, re.DOTALL
    )
    if feedback_match:
        try:
            feedback_data = json.loads(feedback_match.group(1).strip())
        except json.JSONDecodeError:
            pass
    
    return feedback_data

# --- Location Management ---
def load_technician_locations():
    """โหลดตำแหน่งช่าง"""
    if not os.path.exists(LOCATIONS_FILE):
        return {}
    try:
        with open(LOCATIONS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading technician locations: {e}")
        return {}

def save_technician_locations(locations):
    """บันทึกตำแหน่งช่าง"""
    try:
        with open(LOCATIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(locations, f, ensure_ascii=False, indent=4)
        return True
    except Exception as e:
        print(f"Error saving technician locations: {e}")
        return False

# --- Utility Functions ---
def sanitize_filename(name):
    """ทำความสะอาดชื่อไฟล์"""
    if not name:
        return 'Unknown'
    # Remove invalid characters
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', str(name))
    # Remove extra spaces and limit length
    sanitized = ' '.join(sanitized.split())[:50]
    return sanitized or 'Unknown'

def generate_qr_code_base64(data, box_size=10, border=4, fill_color='#28a745', back_color='#FFFFFF'):
    """สร้าง QR Code เป็น base64"""
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=box_size,
            border=border
        )
        qr.add_data(data)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color=fill_color, back_color=back_color)
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        
        return "data:image/png;base64," + base64.b64encode(buffered.getvalue()).decode("utf-8")
    except Exception as e:
        print(f"Error generating QR code: {e}")
        return ""

def get_customer_database():
    """ดึงฐานข้อมูลลูกค้า"""
    tasks = get_google_tasks_for_report(show_completed=True) or []
    customers = {}
    
    for task in tasks:
        customer_info = parse_customer_info_from_notes(task.get('notes', ''))
        if customer_info.get('name'):
            customer_key = customer_info['name'].lower()
            if customer_key not in customers:
                customers[customer_key] = customer_info
    
    return list(customers.values())

def allowed_file(filename):
    """ตรวจสอบว่าไฟล์ที่อัปโหลดได้รับอนุญาตหรือไม่"""
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'kmz', 'kml'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def _parse_equipment_string(text_input):
    """แปลงข้อความอุปกรณ์เป็น list"""
    equipment_list = []
    if not text_input:
        return equipment_list
    
    for line in text_input.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split(',', 1)
        item_name = parts[0].strip()
        if item_name:
            quantity_str = parts[1].strip() if len(parts) > 1 else '1'
            try:
                quantity_num = float(quantity_str)
                equipment_list.append({"item": item_name, "quantity": quantity_num})
            except ValueError:
                equipment_list.append({"item": item_name, "quantity": quantity_str})
    
    return equipment_list

# --- Drive Functions ---
def find_or_create_drive_folder(name, parent_id):
    """หาหรือสร้างโฟลเดอร์ใน Google Drive"""
    service = get_google_drive_service()
    if not service:
        return None
    
    # Search for existing folder
    query = f"name = '{name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    try:
        response = safe_execute(service.files().list(q=query, spaces='drive', fields='files(id, name)', pageSize=1))
        files = response.get('files', []) if response else []
        
        if files:
            return files[0]['id']
        else:
            # Create new folder
            file_metadata = {
                'name': name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [parent_id]
            }
            folder = safe_execute(service.files().create(body=file_metadata, fields='id'))
            return folder.get('id') if folder else None
    except Exception as e:
        print(f"Error finding or creating folder '{name}': {e}")
        return None

# --- Backup Functions ---
def _create_backup_zip():
    """สร้างไฟล์ backup zip"""
    try:
        all_tasks = get_google_tasks_for_report(show_completed=True)
        if all_tasks is None:
            return None, None

        memory_file = BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('data/tasks_backup.json', json.dumps(all_tasks, indent=4, ensure_ascii=False))
            zf.writestr('data/settings_backup.json', json.dumps(get_app_settings(), indent=4, ensure_ascii=False))

        memory_file.seek(0)
        backup_filename = f"full_system_backup_{datetime.datetime.now(THAILAND_TZ).strftime('%Y%m%d_%H%M%S')}.zip"
        return memory_file, backup_filename
    except Exception as e:
        print(f"Error creating backup zip: {e}")
        return None, None

def backup_settings_to_drive():
    """สำรอง settings ไป Google Drive"""
    settings_backup_folder_id = find_or_create_drive_folder("Settings_Backups", GOOGLE_DRIVE_FOLDER_ID)
    if not settings_backup_folder_id:
        return False

    service = get_google_drive_service()
    if not service:
        return False

    try:
        # Delete old backup file
        query = f"name = 'settings_backup.json' and '{settings_backup_folder_id}' in parents and trashed = false"
        response = safe_execute(service.files().list(q=query, spaces='drive', fields='files(id)'))
        for file_item in response.get('files', []):
            safe_execute(service.files().delete(fileId=file_item['id']))

        # Upload new backup
        settings_data = get_app_settings()
        settings_json_bytes = BytesIO(json.dumps(settings_data, ensure_ascii=False, indent=4).encode('utf-8'))
        
        file_metadata = {'name': 'settings_backup.json', 'parents': [settings_backup_folder_id]}
        media = MediaIoBaseUpload(settings_json_bytes, mimetype='application/json', resumable=True)
        
        safe_execute(service.files().create(body=file_metadata, media_body=media, fields='id'))
        return True

    except Exception as e:
        print(f"Failed to backup settings to Google Drive: {e}")
        return False

def load_settings_from_drive_on_startup():
    """โหลด settings จาก Google Drive เมื่อเริ่มต้น"""
    settings_backup_folder_id = find_or_create_drive_folder("Settings_Backups", GOOGLE_DRIVE_FOLDER_ID)
    if not settings_backup_folder_id:
        return False
        
    service = get_google_drive_service()
    if not service:
        return False

    try:
        query = f"name = 'settings_backup.json' and '{settings_backup_folder_id}' in parents and trashed = false"
        response = safe_execute(service.files().list(
            q=query, spaces='drive', fields='files(id, name)', 
            orderBy='modifiedTime desc', pageSize=1
        ))
        files = response.get('files', []) if response else []

        if files:
            latest_backup_file_id = files[0]['id']
            request = service.files().get_media(fileId=latest_backup_file_id)
            
            from googleapiclient.http import MediaIoBaseDownload
            fh = BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
            fh.seek(0)

            downloaded_settings = json.loads(fh.read().decode('utf-8'))
            return save_app_settings(downloaded_settings)
        else:
            return False
    except Exception as e:
        print(f"Error restoring settings from Drive: {e}")
        return False

# --- Messaging Functions ---
def create_task_flex_message(task):
    """สร้าง Flex Message สำหรับงาน"""
    customer = parse_customer_info_from_notes(task.get('notes', ''))
    dates = parse_google_task_dates(task)
    
    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": task.get('title', 'ไม่มีชื่องาน'),
                    "weight": "bold",
                    "size": "md",
                    "wrap": True
                },
                {
                    "type": "text",
                    "text": f"ลูกค้า: {customer.get('name', 'ไม่ระบุ')}",
                    "size": "sm",
                    "color": "#666666",
                    "margin": "md"
                },
                {
                    "type": "text",
                    "text": f"นัดหมาย: {dates.get('due_formatted', 'ไม่มีกำหนด')}",
                    "size": "sm",
                    "color": "#666666"
                }
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "link",
                    "height": "sm",
                    "action": {
                        "type": "uri",
                        "label": "ดูรายละเอียด",
                        "uri": f"/task/{task.get('id', '')}"
                    }
                }
            ]
        }
    }

def render_template_message(template_name, task=None):
    """แทนที่ template ด้วยข้อมูลจริง"""
    settings = get_app_settings()
    templates = settings.get('message_templates', {})
    template_str = templates.get(template_name, '')
    
    if not template_str or not task:
        return template_str
    
    customer = parse_customer_info_from_notes(task.get('notes', ''))
    dates = parse_google_task_dates(task)
    shop = settings.get('shop_info', {})
    
    replacements = {
        '[task_title]': task.get('title', '-'),
        '[customer_name]': customer.get('name', '-'),
        '[customer_phone]': customer.get('phone', '-'),
        '[due_date]': dates.get('due_formatted', '-'),
        '[map_url]': customer.get('map_url', '-'),
        '[shop_phone]': shop.get('contact_phone', '-'),
        '[shop_line_id]': shop.get('line_id', '-'),
        '[task_url]': f"/task/{task.get('id', '')}"
    }
    
    result = template_str
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    
    return result

# --- Notification Functions ---
def send_new_task_notification(task):
    """ส่งแจ้งเตือนงานใหม่"""
    # Implementation depends on your messaging system
    pass

def send_completion_notification(task, technicians):
    """ส่งแจ้งเตือนปิดงาน"""
    # Implementation depends on your messaging system
    pass

def send_update_notification(task, new_due_date_str, reason, technicians, is_today):
    """ส่งแจ้งเตือนอัปเดตงาน"""
    # Implementation depends on your messaging system
    pass

def _send_popup_notification(payload):
    """ส่งการแจ้งเตือนแบบ popup"""
    # Implementation depends on your LIFF system
    pass

def _create_customer_follow_up_flex_message(task_id, task_title, customer_name):
    """สร้าง Flex Message สำหรับ follow up ลูกค้า"""
    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "สอบถามหลังการซ่อม",
                    "weight": "bold",
                    "size": "lg",
                    "