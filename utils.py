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

# This file contains helper functions that do not depend on the Flask app context.
# They are used for data parsing, formatting, and other utility tasks across the application.

THAILAND_TZ = pytz.timezone('Asia/Bangkok')

def sanitize_filename(name):
    """Removes illegal characters from a string to make it a valid filename."""
    if not name:
        return "Unnamed"
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

# ... (โค้ดส่วนอื่นๆ ของ utils.py ยังคงเดิม) ...

def _format_equipment_list(equipment_data):
    """Formats a list of equipment dicts into an HTML string."""
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
            # In a real app, use logger
            print('Failed to get tasks for backup.')
            return None, None

        memory_file = BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('data/tasks_backup.json', json.dumps(all_tasks, indent=4, ensure_ascii=False))
            zf.writestr('data/settings_backup.json', json.dumps(get_app_settings(), indent=4, ensure_ascii=False))

            project_root = os.path.dirname(os.path.abspath(__file__))
            for folder, _, files in os.walk(project_root):
                # Avoid zipping virtual environments or pycache
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
