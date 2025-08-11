# utils.py

import re
import json
import pytz
from datetime import datetime
from dateutil.parser import parse as date_parse
from app import get_google_tasks_service # ยังคงต้อง import service จาก app หลัก

#
# --- VVV คัดลอกฟังก์ชันเหล่านี้มาจาก app.py VVV ---
#

def get_single_task(task_id):
    if not task_id: return None
    service = get_google_tasks_service()
    if not service: return None
    try:
        # ใช้ _execute_google_api_call_with_retry ถ้ามี หรือเรียก service.tasks().get().execute() ตรงๆ
        return service.tasks().get(tasklist='@default', task=task_id).execute()
    except Exception as err:
        print(f"Error getting single task {task_id}: {err}") # ควรใช้ app.logger ใน Production
        return None

def parse_customer_info_from_notes(notes):
    info = {'name': '', 'phone': '', 'address': '', 'map_url': None, 'organization': ''}
    if not notes: return info
    
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
            info['map_url'] = f"https://maps.google.com/maps?q=1{coords_or_url}"
        else:
            info['map_url'] = coords_or_url
    
    return info

def parse_google_task_dates(task_item):
    THAILAND_TZ = pytz.timezone('Asia/Bangkok')
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
                report_data = json.loads(json_str)
                history.append(report_data)
            except json.JSONDecodeError:
                continue
    base_notes_text = re.sub(r"--- CUSTOMER_FEEDBACK_START ---.*?--- CUSTOMER_FEEDBACK_END ---", "", base_notes_with_feedback, flags=re.DOTALL).strip()
    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, base_notes_text