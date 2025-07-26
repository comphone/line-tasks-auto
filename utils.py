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
        print(f"Error generating QR code: {e}")
        return ""

def create_backup_zip():
    """Creates a zip archive of important application data and code."""
    try:
        all_tasks = gs.get_google_tasks_for_report(show_completed=True)
        if all_tasks is None:
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
                    if file.endswith(('.py', '.html', '.css', '.js', '.json', 'Procfile', 'requirements.txt', '.yaml')):
                        file_path = os.path.join(folder, file)
                        archive_name = os.path.relpath(file_path, project_root)
                        zf.write(file_path, arcname=f'code/{archive_name}')
        
        memory_file.seek(0)
        backup_filename = f"full_system_backup_{datetime.now(THAILAND_TZ).strftime('%Y%m%d_%H%M%S')}.zip"
        return memory_file, backup_filename
    except Exception as e:
        print(f"Error creating full system backup zip: {e}")
        return None, None
