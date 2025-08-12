import os
import re
import json
import pytz
from datetime import datetime
from dateutil.parser import parse as date_parse
from config import SETTINGS_FILE, THAILAND_TZ, LOCATIONS_FILE

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
        'liff_popup_base_url': 'https://liff.line.me/2007690244-zBNe26ZO' # ควรย้ายไปอยู่ใน config
    },
    'message_templates': {
        'welcome_customer': "เรียน คุณ[customer_name],\n\nขอบคุณที่เชื่อมต่อกับ Comphone ครับ/ค่ะ!\nเราจะใช้ LINE นี้เพื่อส่งข้อมูลสำคัญเกี่ยวกับบริการครับ\n\nติดต่อ:\nโทร: [shop_phone]\nLINE ID: [shop_line_id]",
        'problem_report_admin': "🚨 ลูกค้าแจ้งปัญหา!\n\nงาน: [task_title]\nลูกค้า: [customer_name]\nปัญหา: [problem_desc]\n\n🔗 ดูรายละเอียดงาน:\n[task_url]",
        'daily_reminder_header': "...",
        'daily_reminder_task_line': "..."
    }
}

def load_settings_from_file():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            # ในกรณีที่ไฟล์เสียหรืออ่านไม่ได้ ให้คืนค่า None
            return None
    return None

def save_settings_to_file(settings_data):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings_data, f, ensure_ascii=False, indent=4)
        return True
    except IOError:
        return False

def get_app_settings():
    # สร้าง deep copy ของ default settings เพื่อป้องกันการแก้ไขค่าเริ่มต้น
    app_settings = json.loads(json.dumps(_DEFAULT_APP_SETTINGS_STORE))
    loaded_settings = load_settings_from_file()
    
    if loaded_settings:
        # วนลูปเพื่อ merge การตั้งค่าที่โหลดมาอย่างปลอดภัย
        for key, default_value in app_settings.items():
            if key in loaded_settings:
                if isinstance(default_value, dict) and isinstance(loaded_settings[key], dict):
                    # ถ้าเป็น dict ให้ merge แทนที่จะเขียนทับ
                    app_settings[key].update(loaded_settings[key])
                else:
                    app_settings[key] = loaded_settings[key]
    else:
        # ถ้าไม่มีไฟล์ settings.json เลย ให้สร้างขึ้นมาจาก default
        save_settings_to_file(app_settings)
        
    return app_settings

def get_single_task(task_id):
    """
    ดึงข้อมูลงานเดียวโดยใช้ Local Import เพื่อทำลายวงจร
    """
    # 3. Local Import: การ import เฉพาะจุดที่จำเป็น เพื่อแก้ปัญหา Circular Import
    from app import get_google_tasks_service, _execute_google_api_call_with_retry
    
    if not task_id:
        return None
    service = get_google_tasks_service()
    if not service:
        return None
    try:
        return _execute_google_api_call_with_retry(service.tasks().get, tasklist='@default', task=task_id)
    except Exception as err:
        print(f"Error getting single task {task_id}: {err}")
        return None

def parse_customer_info_from_notes(notes):
    info = {'name': '', 'phone': '', 'address': '', 'map_url': None, 'organization': ''}
    if not notes:
        return info

    org_match = re.search(r"หน่วยงาน:\s*([^\n]*)", notes, re.IGNORECASE)
    name_match = re.search(r"ลูกค้า:\s*([^\n]*)", notes, re.IGNORECASE)
    phone_match = re.search(r"เบอร์โทรศัพท์:\s*([^\n]*)", notes, re.IGNORECASE)
    address_match = re.search(r"ที่อยู่:\s*([^\n]*)", notes, re.IGNORECASE)
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

def parse_customer_feedback_from_notes(notes):
    if not notes:
        return {}
    feedback_match = re.search(r"--- CUSTOMER_FEEDBACK_START ---\s*\n(.*?)\n--- CUSTOMER_FEEDBACK_END ---", notes, re.DOTALL)
    if feedback_match:
        try:
            return json.loads(feedback_match.group(1))
        except json.JSONDecodeError:
            print(f"Warning: Failed to decode customer feedback from notes.")
    return {}

def parse_google_task_dates(task_item):
    parsed = task_item.copy()
    for key in ['created', 'due', 'completed']:
        if parsed.get(key):
            try:
                dt_utc = date_parse(parsed[key])
                parsed[f'{key}_formatted'] = dt_utc.astimezone(THAILAND_TZ).strftime("%d/%m/%y %H:%M")
                if key == 'due':
                    parsed['due_for_input'] = dt_utc.astimezone(THAILAND_TZ).strftime("%Y-%m-%dT%H:%M")
            except (ValueError, TypeError):
                parsed[f'{key}_formatted'] = ''
                if key == 'due': parsed['due_for_input'] = ''
        else:
            parsed[f'{key}_formatted'] = ''
            if key == 'due': parsed['due_for_input'] = ''
    return parsed

def parse_tech_report_from_notes(notes):
    if not notes:
        return [], ""
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
                continue
    base_notes_text = re.sub(r"--- CUSTOMER_FEEDBACK_START ---.*?--- CUSTOMER_FEEDBACK_END ---", "", base_notes_with_feedback, flags=re.DOTALL).strip()
    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, base_notes_text
    
def load_technician_locations():
    """
    โหลดข้อมูลพิกัดช่างจากไฟล์ JSON
    """
    # ฟังก์ชันนี้จำเป็นต้อง import os และ json ภายในตัวเองเพื่อความสมบูรณ์
    import os
    import json
    from config import LOCATIONS_FILE # ดึงชื่อไฟล์มาจาก config

    if os.path.exists(LOCATIONS_FILE):
        try:
            with open(LOCATIONS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            # หากไฟล์เสียหรืออ่านไม่ได้ ให้คืนค่า dict ว่าง
            print(f"Warning: Error reading or parsing {LOCATIONS_FILE}. Returning empty dict.")
            return {}
    return {}

def save_technician_locations(locations_data):
    """
    บันทึกข้อมูลพิกัดช่างลงในไฟล์ JSON
    """
    # ฟังก์ชันนี้จำเป็นต้อง import os และ json ภายในตัวเองเพื่อความสมบูรณ์
    import os
    import json
    from config import LOCATIONS_FILE # ดึงชื่อไฟล์มาจาก config

    try:
        with open(LOCATIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(locations_data, f, ensure_ascii=False, indent=4)
        return True
    except IOError as e:
        print(f"Error saving to {LOCATIONS_FILE}: {e}")
        return False