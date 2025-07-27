import os
import re
import json
import pytz
import zipfile
from io import BytesIO
from datetime import datetime

from dateutil.parser import parse as date_parse
import qrcode
import base64

# --- Local Module Imports ---
from settings_manager import get_app_settings
import google_services as gs

THAILAND_TZ = pytz.timezone('Asia/Bangkok')

TEXT_SNIPPETS = {
    'task_details': [
        {'key': 'ล้างแอร์', 'value': 'ล้างทำความสะอาดเครื่องปรับอากาศ, ตรวจเช็คน้ำยา, วัดแรงดันไฟฟ้า และทำความสะอาดคอยล์ร้อน-เย็น'},
        {'key': 'ติดตั้งแอร์', 'value': 'ติดตั้งเครื่องปรับอากาศใหม่ ขนาด [ขนาด BTU] พร้อมเดินท่อน้ำยาและสายไฟ, ติดตั้งเบรกเกอร์'},
        {'key': 'ซ่อมตู้เย็น', 'value': 'ซ่อมตู้เย็น [ยี่ห้อ/รุ่น] อาการไม่เย็น, ตรวจสอบคอมเพรสเซอร์และน้ำยา'},
        {'key': 'ตรวจเช็ค', 'value': 'เข้าตรวจเช็คอาการเสียเบื้องต้นตามที่ลูกค้าแจ้ง'}
    ],
    'progress_reports': [
        {'key': 'ลูกค้าเลื่อนนัด', 'value': 'ลูกค้าขอเลื่อนนัดเป็นวันที่ [dd/mm/yyyy] เนื่องจากไม่สะดวก'},
        {'key': 'รออะไหล่', 'value': 'ตรวจสอบแล้วพบว่าต้องรออะไหล่ [ชื่ออะไหล่] จะแจ้งลูกค้าให้ทราบกำหนดการอีกครั้ง'},
        {'key': 'เข้าพื้นที่ไม่ได้', 'value': 'ไม่สามารถเข้าพื้นที่ได้เนื่องจาก [เหตุผล] ได้โทรแจ้งลูกค้าแล้ว'},
        {'key': 'เสร็จบางส่วน', 'value': 'ดำเนินการเสร็จสิ้นบางส่วน เหลือ [สิ่งที่ต้องทำต่อ] จะเข้ามาดำเนินการต่อในวันถัดไป'}
    ]
}

def sanitize_filename(name):
    if not name: return "Unnamed"
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def parse_customer_info_from_notes(notes):
    info = {'name': '', 'phone': '', 'address': '', 'map_url': None, 'organization': ''}
    if not notes: return info
    # Use non-capturing groups for the keywords to extract only the value
    org_match = re.search(r"(?i)หน่วยงาน:\s*(.*)", notes)
    name_match = re.search(r"(?i)ลูกค้า:\s*(.*)", notes)
    phone_match = re.search(r"(?i)เบอร์โทรศัพท์:\s*(.*)", notes)
    address_match = re.search(r"(?i)ที่อยู่:\s*(.*)", notes)
    map_url_match = re.search(r"(https?:\/\/[^\s]+|maps\.google\.com\/\?q=\-?\d+\.\d+,\-?\d+\.\d+)", notes)

    if org_match: info['organization'] = org_match.group(1).strip()
    if name_match: info['name'] = name_match.group(1).strip()
    if phone_match: info['phone'] = phone_match.group(1).strip()
    if address_match: info['address'] = address_match.group(1).strip()
    if map_url_match: info['map_url'] = map_url_match.group(1).strip()
    
    return info

def get_notes_parts(notes):
    """Separates notes into base info, tech reports, and customer feedback."""
    if not notes:
        return {}, [], {}

    report_pattern = r"--- TECH_REPORT_START ---\s*\n(.*?)\n--- TECH_REPORT_END ---"
    feedback_pattern = r"--- CUSTOMER_FEEDBACK_START ---\s*\n(.*?)\n--- CUSTOMER_FEEDBACK_END ---"

    report_blocks = re.findall(report_pattern, notes, re.DOTALL)
    feedback_blocks = re.findall(feedback_pattern, notes, re.DOTALL)
    
    history = [json.loads(block) for block in report_blocks]
    feedback = json.loads(feedback_blocks[0]) if feedback_blocks else {}

    base_notes_text = re.sub(report_pattern, '', notes, flags=re.DOTALL)
    base_notes_text = re.sub(feedback_pattern, '', base_notes_text, flags=re.DOTALL).strip()
    
    base_info = parse_customer_info_from_notes(base_notes_text)

    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    
    return base_info, history, feedback

def build_notes_string(base_info, history, feedback):
    """Constructs the full notes string from its component parts."""
    lines = [
        f"หน่วยงาน: {base_info.get('organization', '')}",
        f"ลูกค้า: {base_info.get('name', '')}",
        f"เบอร์โทรศัพท์: {base_info.get('phone', '')}",
        f"ที่อยู่: {base_info.get('address', '')}",
        f"พิกัด: {base_info.get('map_url', '')}"
    ]
    base_text = "\n".join(line for line in lines if line.split(': ', 1)[1])
    
    history_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
    feedback_text = f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---" if feedback else ""
    
    return f"{base_text}{history_text}{feedback_text}"


def parse_google_task_dates(task_item):
    parsed = task_item.copy()
    for key in ['created', 'due', 'completed', 'updated']:
        if parsed.get(key):
            try:
                dt_utc = date_parse(parsed[key])
                parsed[f'{key}_formatted'] = dt_utc.astimezone(THAILAND_TZ).strftime("%d/%m/%y %H:%M")
                if key == 'due':
                    parsed['due_for_input'] = dt_utc.astimezone(THAILAND_TZ).strftime("%Y-%m-%dT%H:%M")
            except (ValueError, TypeError):
                parsed[f'{key}_formatted'] = ''
                if key == 'due': parsed['due_for_input'] = ''
    return parsed

def generate_qr_code_base64(data, box_size=10, border=4):
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=box_size, border=border)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffered.getvalue()).decode("utf-8")

def get_file_icon(filename):
    extension = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if extension in ['jpg', 'jpeg', 'png', 'gif']: return 'fas fa-file-image'
    if extension == 'pdf': return 'fas fa-file-pdf'
    if extension in ['doc', 'docx']: return 'fas fa-file-word'
    if extension in ['xls', 'xlsx']: return 'fas fa-file-excel'
    if extension in ['kml', 'kmz']: return 'fas fa-map'
    return 'fas fa-file'