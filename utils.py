# File: utils.py (ฉบับแก้ไข)
import qrcode
import base64
from io import BytesIO
import re
import json
import pytz
import mimetypes
import os
from datetime import datetime, date
from dateutil.parser import parse as date_parse
from cachetools import cached, TTLCache
from collections import defaultdict
from flask import current_app
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

# สร้าง Cache สำหรับไฟล์นี้โดยเฉพาะ
util_cache = TTLCache(maxsize=100, ttl=60)

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


# --- Google API Helper Functions ---
# START: โค้ดส่วนที่แก้ไข
# เราจะ import ฟังก์ชันจาก app.py โดยตรงภายในฟังก์ชันที่เรียกใช้
# เพื่อหลีกเลี่ยง Circular Import

# --- Google Tasks Functions ---

def get_google_tasks_for_report(show_completed=True):
    """Fetches a list of tasks from the configured Google Tasks list."""
    from app import get_google_tasks_service, _execute_google_api_call_with_retry, GOOGLE_TASKS_LIST_ID
    service = get_google_tasks_service()
    if not service: return None
    try:
        results = _execute_google_api_call_with_retry(
            service.tasks().list,
            tasklist=GOOGLE_TASKS_LIST_ID,
            showCompleted=show_completed,
            maxResults=100
        )
        return results.get('items', [])
    except Exception as err:
        current_app.logger.error(f"API Error getting tasks in utils: {err}")
        return None

def get_single_task(task_id):
    """Fetches a single task by its ID."""
    from app import get_google_tasks_service, _execute_google_api_call_with_retry, GOOGLE_TASKS_LIST_ID
    if not task_id: return None
    service = get_google_tasks_service()
    if not service: return None
    try:
        return _execute_google_api_call_with_retry(
            service.tasks().get,
            tasklist=GOOGLE_TASKS_LIST_ID,
            task=task_id
        )
    except Exception as err:
        current_app.logger.error(f"Error getting single task {task_id} in utils: {err}")
        return None

def create_google_task(title, notes=None, due=None):
    """Creates a new task in Google Tasks."""
    from app import get_google_tasks_service, _execute_google_api_call_with_retry, GOOGLE_TASKS_LIST_ID
    service = get_google_tasks_service()
    if not service: return None
    try:
        task_body = {'title': title, 'notes': notes, 'status': 'needsAction'}
        if due:
            task_body['due'] = due
        return _execute_google_api_call_with_retry(
            service.tasks().insert,
            tasklist=GOOGLE_TASKS_LIST_ID,
            body=task_body
        )
    except HttpError as e:
        current_app.logger.error(f"Error creating Google Task: {e}")
        return None

def update_google_task(task_id, **kwargs):
    """Updates an existing Google Task."""
    from app import get_google_tasks_service, _execute_google_api_call_with_retry, GOOGLE_TASKS_LIST_ID
    service = get_google_tasks_service()
    if not service: return None
    try:
        task = get_single_task(task_id)
        if not task:
            current_app.logger.error(f"Task {task_id} not found for update.")
            return None
        
        task.update(kwargs)

        if kwargs.get('status') == 'completed' and 'completed' not in task:
             task['completed'] = datetime.now(pytz.utc).isoformat().replace('+00:00', 'Z')
             task.pop('due', None)
        elif kwargs.get('status') == 'needsAction':
             task.pop('completed', None)

        return _execute_google_api_call_with_retry(
            service.tasks().update,
            tasklist=GOOGLE_TASKS_LIST_ID,
            task=task_id,
            body=task
        )
    except HttpError as e:
        current_app.logger.error(f"Failed to update task {task_id}: {e}")
        return None

def delete_google_task(task_id):
    """Deletes a Google Task."""
    from app import get_google_tasks_service, _execute_google_api_call_with_retry, GOOGLE_TASKS_LIST_ID
    service = get_google_tasks_service()
    if not service: return False
    try:
        _execute_google_api_call_with_retry(
            service.tasks().delete,
            tasklist=GOOGLE_TASKS_LIST_ID,
            task=task_id
        )
        return True
    except HttpError as err:
        current_app.logger.error(f"API Error deleting task {task_id}: {err}")
        return False

# --- Google Drive Functions ---

@cached(util_cache)
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
# END: โค้ดส่วนที่แก้ไข

def upload_data_from_memory_to_drive(data_in_memory, file_name, mime_type, folder_id):
    media = MediaIoBaseUpload(data_in_memory, mimetype=mime_type, resumable=True)
    file_obj = perform_drive_upload(media, file_name, folder_id)
    return file_obj

# --- Data Parsing Functions ---
def parse_customer_info_from_notes(notes):
    """Parses customer information from the notes field of a task."""
    info = {'name': '', 'phone': '', 'address': '', 'map_url': None, 'organization': ''}
    if not notes: return info
    
    # ใช้ re.MULTILINE เพื่อให้ ^ ตรงกับจุดเริ่มต้นของแต่ละบรรทัด
    org_match = re.search(r"^\s*หน่วยงาน:\s*(.*)", notes, re.IGNORECASE | re.MULTILINE)
    name_match = re.search(r"^\s*ลูกค้า:\s*(.*)", notes, re.IGNORECASE | re.MULTILINE)
    phone_match = re.search(r"^\s*เบอร์โทรศัพท์:\s*(.*)", notes, re.IGNORECASE | re.MULTILINE)
    address_match = re.search(r"^\s*ที่อยู่:\s*(.*)", notes, re.IGNORECASE | re.MULTILINE)
    
    map_url_match = re.search(r"(https?:\/\/[^\s]+|(?:\-?\d+\.\d+,\s*\-?\d+\.\d+))", notes)
    
    if org_match: info['organization'] = org_match.group(1).strip()
    if name_match: info['name'] = name_match.group(1).strip()
    if phone_match: info['phone'] = phone_match.group(1).strip()
    if address_match: info['address'] = address_match.group(1).strip()
    
    if map_url_match:
        coords_or_url = map_url_match.group(1).strip()
        if re.match(r"^\-?\d+\.\d+,\s*\-?\d+\.\d+$", coords_or_url):
            info['map_url'] = f"http://googleusercontent.com/maps/google.com/14{coords_or_url}"
        else:
            info['map_url'] = coords_or_url
            
    return info

def parse_tech_report_from_notes(notes):
    """Parses technician reports from the notes field."""
    if not notes: return [], ""
    
    parts = re.split(r'\n\s*--- TECH_REPORT_START ---', notes)
    base_notes_with_feedback = parts[0]
    history = []
    
    for part in parts[1:]:
        end_match = re.search(r'(.*?)\n\s*--- TECH_REPORT_END ---', part, re.DOTALL)
        if end_match:
            json_str = end_match.group(1).strip()
            try:
                history.append(json.loads(json_str))
            except json.JSONDecodeError:
                current_app.logger.warning(f"Failed to decode tech report JSON: {json_str[:100]}...")
                
    base_notes_text = re.sub(r"--- CUSTOMER_FEEDBACK_START ---.*?--- CUSTOMER_FEEDBACK_END ---", "", base_notes_with_feedback, flags=re.DOTALL).strip()
    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    
    return history, base_notes_text

def parse_customer_feedback_from_notes(notes):
    """Parses customer feedback from the notes field."""
    if not notes: return {}
    
    feedback_match = re.search(r"--- CUSTOMER_FEEDBACK_START ---\s*\n(.*?)\n--- CUSTOMER_FEEDBACK_END ---", notes, re.DOTALL)
    if feedback_match:
        try:
            return json.loads(feedback_match.group(1))
        except json.JSONDecodeError:
            current_app.logger.warning("Failed to decode customer feedback JSON.")
            
    return {}

def parse_google_task_dates(task_item):
    """Formats dates from a Google Task item into a more readable format."""
    THAILAND_TZ = pytz.timezone('Asia/Bangkok')
    parsed = task_item.copy()
    
    for key in ['created', 'due', 'completed']:
        if parsed.get(key):
            try:
                dt_utc = date_parse(parsed[key])
                dt_thai = dt_utc.astimezone(THAILAND_TZ)
                parsed[f'{key}_formatted'] = dt_thai.strftime("%d/%m/%y %H:%M")
                if key == 'due':
                    parsed['due_for_input'] = dt_thai.strftime("%Y-%m-%dT%H:%M")
            except (ValueError, TypeError):
                parsed[f'{key}_formatted'] = ''
                if key == 'due': parsed['due_for_input'] = ''
        else:
            parsed[f'{key}_formatted'] = ''
            if key == 'due': parsed['due_for_input'] = ''
            
    return parsed

def parse_assigned_technician_from_notes(notes):
    """Parses the assigned technician's name from the notes field."""
    if not notes:
        return None
    
    match = re.search(r"^\s*Assigned to:\s*(.*)$", notes, re.MULTILINE | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None

def parse_customer_profile_from_task(task):
    """
    Parses the entire customer profile, including all jobs, from a single Google Task's notes.
    """
    notes = task.get('notes', '{}')
    try:
        profile_data = json.loads(notes)
        if 'customer_info' not in profile_data:
            profile_data['customer_info'] = {}
        if 'jobs' not in profile_data:
            profile_data['jobs'] = []
        return profile_data
    except json.JSONDecodeError:
        # This part handles legacy tasks by converting them into the new profile structure on-the-fly
        base_customer_notes = notes.split('--- TECH_REPORT_START ---')[0]
        customer_info = parse_customer_info_from_notes(base_customer_notes)
        
        legacy_job = {
            'job_id': task.get('id'),
            'job_title': task.get('title', 'N/A'),
            'created_date': task.get('created'),
            'due_date': task.get('due'),
            'status': task.get('status', 'needsAction'),
            'service_items': [], 'reports': [], 'expenses': []
        }
        return {
            'customer_info': customer_info,
            'jobs': [legacy_job]
        }

def parse_task_data(task_item):
    """
    A universal parser for a Google Task item.
    It intelligently handles both new JSON-based profiles and old plain-text tasks.
    Returns a standardized dictionary.
    """
    if not task_item:
        return {}

    notes = task_item.get('notes', '')
    customer_info = {}
    jobs = []
    is_legacy_task = False

    try:
        # New system: Notes are a JSON string with customer profile data
        profile_data = json.loads(notes)
        customer_info = profile_data.get('customer_info', {})
        jobs = profile_data.get('jobs', [])
    except json.JSONDecodeError:
        # Old system: Notes are plain text, and the task itself is the job
        is_legacy_task = True
        base_notes = notes.split('--- TECH_REPORT_START ---')[0]
        customer_info = parse_customer_info_from_notes(base_notes)
        jobs = [{
            'job_id': task_item.get('id'),
            'job_title': task_item.get('title'),
            'created_date': task_item.get('created'),
            'due_date': task_item.get('due'),
            'status': task_item.get('status', 'needsAction'),
        }]

    # Standardize and enrich the task data
    parsed_task = parse_google_task_dates(task_item)
    today_thai = date.today()
    is_overdue = False
    is_today = False

    if parsed_task.get('status') == 'needsAction' and parsed_task.get('due'):
        try:
            due_dt_utc = date_parse(parsed_task['due'])
            due_dt_local = due_dt_utc.astimezone(pytz.timezone('Asia/Bangkok'))
            if due_dt_local.date() < today_thai:
                is_overdue = True
            elif due_dt_local.date() == today_thai:
                is_today = True
        except (ValueError, TypeError):
            pass
    
    # In the new system, the task title is the customer name, so we use that.
    # In the old system, customer info is parsed from notes.
    final_customer_name = customer_info.get('name')
    if not final_customer_name and not is_legacy_task:
        final_customer_name = task_item.get('title') # Fallback for new system profiles
    customer_info['name'] = final_customer_name
    
    return {
        **parsed_task,
        'customer': customer_info,
        'jobs': jobs,
        'is_legacy': is_legacy_task,
        'is_overdue': is_overdue,
        'is_today': is_today,
        'assigned_technician': parse_assigned_technician_from_notes(notes)
    }

@cached(util_cache)
def get_customer_database():
    """Builds a unique customer database from all tasks."""
    current_app.logger.info("Building customer database via utils...")
    all_tasks = get_google_tasks_for_report(show_completed=True)
    if not all_tasks:
        return []
        
    customers_dict = {}
    all_tasks.sort(key=lambda x: x.get('created', '0'), reverse=True)
    
    for task in all_tasks:
        # Use the new universal parser to get consistent customer info
        parsed_data = parse_task_data(task)
        customer_info = parsed_data.get('customer')
        name = customer_info.get('name', '').strip()
        if name:
            customer_key = name.lower()
            if customer_key not in customers_dict:
                # Add the task ID to the customer data for linking
                customer_info['id'] = task.get('id')
                customers_dict[customer_key] = customer_info
                
    return list(customers_dict.values())

def get_technician_report_data(year, month):
    """
    ฟังก์ชันกลางสำหรับดึงและประมวลผลข้อมูลรายงานของช่าง
    (Internal function, called by a route in liff_views)
    """
    app_settings = get_app_settings()
    technician_list = app_settings.get('technician_list', [])
    official_tech_names = {tech.get('name', '').strip() for tech in technician_list if tech.get('name')}

    tasks = get_google_tasks_for_report(show_completed=True) or []
    report = defaultdict(lambda: {'count': 0, 'tasks': []})
    THAILAND_TZ = pytz.timezone('Asia/Bangkok')

    for task in tasks:
        if task.get('status') == 'completed' and task.get('completed'):
            try:
                completed_dt = date_parse(task['completed']).astimezone(THAILAND_TZ)
                if completed_dt.year == year and completed_dt.month == month:
                    history, _ = parse_tech_report_from_notes(task.get('notes', ''))
                    task_techs = set()
                    for r in history:
                        for t_name in r.get('technicians', []):
                            if isinstance(t_name, str):
                                task_techs.add(t_name.strip())

                    for tech_name in sorted(list(task_techs)):
                        if tech_name in official_tech_names:
                            report[tech_name]['count'] += 1
                            customer_name = parse_customer_info_from_notes(task.get('notes', '')).get('name', 'N/A')
                            report[tech_name]['tasks'].append({
                                'id': task.get('id'),
                                'title': task.get('title'),
                                'customer_name': customer_name,
                                'completed_formatted': completed_dt.strftime("%d/%m/%Y")
                            })
            except Exception as e:
                current_app.logger.error(f"Error processing task {task.get('id')} for technician report: {e}")
                continue

    for tech_name in report:
        report[tech_name]['tasks'].sort(key=lambda x: x['completed_formatted'])

    return dict(sorted(report.items())), technician_list

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