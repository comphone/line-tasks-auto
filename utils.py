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
from flask import current_app

# --- Local Module Imports ---
from settings_manager import get_app_settings, SETTINGS_FILE

THAILAND_TZ = pytz.timezone('Asia/Bangkok')

# Define text snippets for quick replies/autocomplete in forms
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
    """Removes illegal characters from a string to make it a valid filename."""
    if not name:
        return "Unnamed"
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def parse_customer_info_from_notes(notes):
    """Extracts customer information from the notes string of a task."""
    info = {'name': '', 'phone': '', 'address': '', 'map_url': None, 'organization': ''}
    if not notes: return info

    org_match = re.search(r"หน่วยงาน:\s*(.*)", notes, re.IGNORECASE)
    name_match = re.search(r"ลูกค้า:\s*(.*)", notes, re.IGNORECASE)
    phone_match = re.search(r"เบอร์โทรศัพท์:\s*(.*)", notes, re.IGNORECASE)
    address_match = re.search(r"ที่อยู่:\s*(.*)", notes, re.IGNORECASE)
    map_url_match = re.search(r"(https?:\/\/[^\s]+|(?:\-?\d+\.\d+,\s*\-?\d+\.\d+))", notes)

    if org_match: info['organization'] = org_match.group(1).strip().split(':')[-1].strip()
    if name_match: info['name'] = name_match.group(1).strip().split(':')[-1].strip()
    if phone_match: info['phone'] = phone_match.group(1).strip().split(':')[-1].strip()
    if address_match: info['address'] = address_match.group(1).strip().split(':')[-1].strip()
    
    if map_url_match:
        coords_or_url = map_url_match.group(1).strip()
        if re.match(r"^\-?\d+\.\d+,\s*\-?\d+\.\d+$", coords_or_url):
            info['map_url'] = f"https://maps.google.com/maps?q={coords_or_url}" 
        else:
            info['map_url'] = coords_or_url
    
    return info

def parse_customer_feedback_from_notes(notes):
    """Extracts the structured customer feedback block from notes."""
    feedback_data = {}
    if not notes: return feedback_data
    feedback_match = re.search(r"--- CUSTOMER_FEEDBACK_START ---\s*\n(.*?)\n--- CUSTOMER_FEEDBACK_END ---", notes, re.DOTALL)
    if feedback_match:
        try:
            feedback_data = json.loads(feedback_match.group(1))
        except json.JSONDecodeError:
            if hasattr(current_app, 'logger'):
                current_app.logger.warning(f"Failed to decode customer feedback JSON.")
            else:
                print(f"Warning: Failed to decode customer feedback JSON.")
    return feedback_data

def parse_google_task_dates(task_item):
    """Parses and formats various date fields from a Google Task item."""
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
        else:
            parsed[f'{key}_formatted'] = ''
            if key == 'due': parsed['due_for_input'] = ''
    return parsed

def parse_tech_report_from_notes(notes):
    """Extracts all technician report blocks and the remaining base notes text."""
    if not notes: return [], ""
    report_blocks = re.findall(r"--- TECH_REPORT_START ---\s*\n(.*?)\n--- TECH_REPORT_END ---", notes, re.DOTALL)
    history = []
    for json_str in report_blocks:
        try:
            report_data = json.loads(json_str)
            if 'attachments' not in report_data and 'attachment_urls' in report_data and isinstance(report_data['attachment_urls'], list):
                report_data['attachments'] = [{'id': re.search(r'/d/([a-zA-Z0-9_-]+)', url).group(1) if re.search(r'/d/([a-zA-Z0-9_-]+)', url) else None, 'url': url} for url in report_data['attachment_urls'] if isinstance(url, str)]
                report_data.pop('attachment_urls', None)
            if 'type' not in report_data:
                report_data['type'] = 'report'
            history.append(report_data)
        except json.JSONDecodeError:
            if hasattr(current_app, 'logger'):
                current_app.logger.warning(f"Failed to decode tech report JSON: {json_str[:100]}...")
            else:
                print(f"Warning: Failed to decode tech report JSON: {json_str[:100]}...")
    
    temp_notes = re.sub(r"--- (TECH_REPORT_START|CUSTOMER_FEEDBACK_START) ---.*?--- (TECH_REPORT_END|CUSTOMER_FEEDBACK_END) ---", "", notes, flags=re.DOTALL)
    original_notes_text = temp_notes.strip()

    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, original_notes_text

def generate_qr_code_base64(data, box_size=10, border=4, fill_color='#28a745', back_color='#FFFFFF'):
    """Generates a QR code and returns it as a base64 encoded string."""
    try:
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=box_size, border=border)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color=fill_color, back_color=back_color)
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffered.getvalue()).decode("utf-8")
    except Exception as e:
        if hasattr(current_app, 'logger'):
            current_app.logger.error(f"Error generating QR code: {e}")
        else:
            print(f"Error generating QR code: {e}")
        return ""

def create_backup_zip():
    """Creates a zip archive of important application data and code."""
    try:
        # Import here to avoid circular dependency
        import google_services as gs
        
        all_tasks = gs.get_google_tasks_for_report(show_completed=True)
        if all_tasks is None:
            if hasattr(current_app, 'logger'):
                current_app.logger.error('Failed to get tasks for backup.')
            else:
                print('Failed to get tasks for backup.')
            return None, None

        memory_file = BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('data/tasks_backup.json', json.dumps(all_tasks, indent=4, ensure_ascii=False))
            zf.writestr('data/settings_backup.json', json.dumps(get_app_settings(), indent=4, ensure_ascii=False))

            project_root = os.path.dirname(os.path.abspath(__file__))
            for folder, _, files in os.walk(project_root):
                if '.venv' in folder or '__pycache__' in folder:
                    continue
                for file in files:
                    if file.endswith(('.py', '.html', '.css', '.js', '.json', 'Procfile', 'requirements.txt', '.yaml')) \
                       and file not in ['token.json', '.env', SETTINGS_FILE]:
                        file_path = os.path.join(folder, file)
                        archive_name = os.path.relpath(file_path, project_root)
                        zf.write(file_path, arcname=f'code/{archive_name}')
        
        memory_file.seek(0)
        backup_filename = f"full_system_backup_{datetime.now(THAILAND_TZ).strftime('%Y%m%d_%H%M%S')}.zip"
        return memory_file, backup_filename
    except Exception as e:
        if hasattr(current_app, 'logger'):
            current_app.logger.error(f"Error creating full system backup zip: {e}")
        else:
            print(f"Error creating full system backup zip: {e}")
        return None, None

def allowed_file(filename):
    """Checks if a file extension is allowed."""
    allowed = getattr(current_app, 'ALLOWED_EXTENSIONS', {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'kmz', 'kml', 'doc', 'docx', 'xls', 'xlsx', 'zip', 'rar'})
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed

def _parse_equipment_string(text_input):
    """Parses equipment string from notes into a list of dicts."""
    equipment_list = []
    if not text_input: return equipment_list
    for line in text_input.strip().split('\n'):
        if not line.strip(): continue
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

def _format_equipment_list(equipment_data):
    """Formats equipment list for display in HTML."""
    if not equipment_data: return 'N/A'
    if isinstance(equipment_data, str): return equipment_data
    lines = []
    if isinstance(equipment_data, list):
        for item in equipment_data:
            if isinstance(item, dict) and "item" in item:
                line = item['item']
                if item.get("quantity") is not None:
                    if isinstance(item['quantity'], (int, float)):
                        line += f" (x{item['quantity']:g})"
                    else:
                        line += f" ({item['quantity']})"
                lines.append(line)
            elif isinstance(item, str):
                lines.append(item)
    return "<br>".join(lines) if lines else 'N/A'

def get_file_icon(filename):
    """Returns a Font Awesome icon class based on file extension."""
    extension = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if extension in ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp']:
        return 'fas fa-file-image'
    elif extension == 'pdf':
        return 'fas fa-file-pdf'
    elif extension in ['doc', 'docx']:
        return 'fas fa-file-word'
    elif extension in ['xls', 'xlsx']:
        return 'fas fa-file-excel'
    elif extension in ['zip', 'rar']:
        return 'fas fa-file-archive'
    elif extension in ['kml', 'kmz']:
        return 'fas fa-map'
    elif extension in ['mp4', 'mov', 'avi']:
        return 'fas fa-file-video'
    else:
        return 'fas fa-file'