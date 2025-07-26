import os
import sys
import datetime
import re
import json
import pytz
import mimetypes
import zipfile
from io import BytesIO
from collections import defaultdict
from datetime import timezone, date, timedelta
import time
import tempfile
import uuid

from PIL import Image

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, render_template, redirect, url_for, abort, flash, jsonify, Response, session
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect
from cachetools import cached, TTLCache
from geopy.distance import geodesic

import qrcode
import base64

from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage,
    BubbleContainer, CarouselContainer, BoxComponent, TextComponent,
    ButtonComponent, SeparatorComponent, URIAction, PostbackAction, QuickReply, QuickReplyButton,
    ImageComponent, PostbackEvent
)

# Import for Google OAuth flow, which remains in the main app
from google_auth_oauthlib.flow import Flow

import pandas as pd
from dateutil.parser import parse as date_parse

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit

# --- Import the separated Google services module ---
import google_services as gs

# --- App Configuration and Initialization ---
app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_development_only')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
csrf = CSRFProtect(app)

# --- Environment Variables & Constants ---
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'kmz', 'kml'}
MAX_FILE_SIZE_MB = 50
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM')
LINE_LOGIN_CHANNEL_ID = os.environ.get('LINE_LOGIN_CHANNEL_ID')
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    sys.exit("LINE Bot credentials are not set in environment variables.")
if not GOOGLE_DRIVE_FOLDER_ID:
    app.logger.warning("GOOGLE_DRIVE_FOLDER_ID environment variable is not set. Drive upload may not work.")
if not LIFF_ID_FORM:
    app.logger.warning("LIFF_ID_FORM environment variable is not set. LIFF features may not work.")
if not LINE_LOGIN_CHANNEL_ID:
    app.logger.warning("LINE_LOGIN_CHANNEL_ID environment variable is not set. LIFF initialization might fail.")

THAILAND_TZ = pytz.timezone('Asia/Bangkok')
cache = TTLCache(maxsize=100, ttl=60)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

app.jinja_env.filters['dateutil_parse'] = date_parse
scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)

SETTINGS_FILE = 'settings.json'
_DEFAULT_APP_SETTINGS_STORE = {
    'report_times': { 'appointment_reminder_hour_thai': 7, 'outstanding_report_hour_thai': 20, 'customer_followup_hour_thai': 9 },
    'line_recipients': { 'admin_group_id': os.environ.get('LINE_ADMIN_GROUP_ID', ''), 'technician_group_id': os.environ.get('LINE_TECHNICIAN_GROUP_ID', ''), 'manager_user_id': '' },
    'equipment_catalog': [],
    'auto_backup': { 'enabled': False, 'hour_thai': 2, 'minute_thai': 0 },
    'shop_info': { 'contact_phone': '081-XXX-XXXX', 'line_id': '@ComphoneService' },
    'technician_list': []
}
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

#<editor-fold desc="Helper and Utility Functions">

def load_settings_from_file():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            app.logger.error(f"Error handling settings.json: {e}")
            if os.path.exists(SETTINGS_FILE) and os.path.getsize(SETTINGS_FILE) == 0:
                os.remove(SETTINGS_FILE)
                app.logger.warning(f"Empty settings.json deleted. Using default settings.")
    return None

def save_settings_to_file(settings_data):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f: json.dump(settings_data, f, ensure_ascii=False, indent=4)
        return True
    except IOError as e:
        app.logger.error(f"Error writing to settings.json: {e}")
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

def save_app_settings(settings_data):
    current_settings = get_app_settings()
    
    for key, value in settings_data.items():
        if isinstance(value, dict) and key in current_settings and isinstance(current_settings[key], dict):
            current_settings[key].update(value)
        else:
            current_settings[key] = value
            
    return save_settings_to_file(current_settings)

def sanitize_filename(name):
    if not name: return "Unnamed"
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

@cached(cache)
def get_customer_database():
    app.logger.info("Building customer database from Google Tasks...")
    all_tasks = gs.get_google_tasks_for_report(show_completed=True)
    if not all_tasks: return []

    customers_dict = {}
    all_tasks.sort(key=lambda x: x.get('created', '0'), reverse=True)

    for task in all_tasks:
        notes = task.get('notes', '')
        if not notes: continue
        
        _, base_notes = parse_tech_report_from_notes(notes)
        customer_info = parse_customer_info_from_notes(base_notes)

        name = customer_info.get('name', '').strip()
        phone = customer_info.get('phone', '').strip()

        if not name: continue
        customer_key = (name.lower(), phone)
        
        if customer_key not in customers_dict:
            customers_dict[customer_key] = {
                'name': name, 'phone': phone,
                'organization': customer_info.get('organization', '').strip(),
                'address': customer_info.get('address', '').strip(),
                'map_url': customer_info.get('map_url', '')
            }
    
    app.logger.info(f"Customer database built with {len(customers_dict)} unique customers.")
    return list(customers_dict.values())

def parse_customer_info_from_notes(notes):
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
    feedback_data = {}
    if not notes: return feedback_data
    feedback_match = re.search(r"--- CUSTOMER_FEEDBACK_START ---\s*\n(.*?)\n--- CUSTOMER_FEEDBACK_END ---", notes, re.DOTALL)
    if feedback_match:
        try: feedback_data = json.loads(feedback_match.group(1))
        except json.JSONDecodeError: app.logger.warning("Failed to decode customer feedback JSON from notes.")
    return feedback_data

def parse_google_task_dates(task_item):
    parsed = task_item.copy()
    for key in ['created', 'due', 'completed', 'updated']:
        if parsed.get(key):
            try:
                dt_utc = date_parse(parsed[key])
                parsed[f'{key}_formatted'] = dt_utc.astimezone(THAILAND_TZ).strftime("%d/%m/%y %H:%M")
                if key == 'due': parsed['due_for_input'] = dt_utc.astimezone(THAILAND_TZ).strftime("%Y-%m-%dT%H:%M")
            except (ValueError, TypeError) as e:
                app.logger.warning(f"Could not parse date '{parsed[key]}' for key '{key}': {e}")
                parsed[f'{key}_formatted'] = ''
                if key == 'due': parsed['due_for_input'] = ''
        else:
            parsed[f'{key}_formatted'] = ''
            if key == 'due': parsed['due_for_input'] = ''
    return parsed

def parse_tech_report_from_notes(notes):
    if not notes: return [], ""
    report_blocks = re.findall(r"--- TECH_REPORT_START ---\s*\n(.*?)\n--- TECH_REPORT_END ---", notes, re.DOTALL)
    history = []
    for json_str in report_blocks:
        try:
            report_data = json.loads(json_str)
            if 'attachments' in report_data: pass
            elif 'attachment_urls' in report_data and isinstance(report_data['attachment_urls'], list):
                report_data['attachments'] = []
                for url in report_data['attachment_urls']:
                    if isinstance(url, str):
                        match = re.search(r'/d/([a-zA-Z0-9_-]+)', url)
                        file_id = match.group(1) if match else None
                        report_data['attachments'].append({'id': file_id, 'url': url})
                report_data.pop('attachment_urls', None)
            
            if isinstance(report_data.get('equipment_used'), str):
                report_data['equipment_used_display'] = report_data['equipment_used'].replace('\n', '<br>')
            else:
                report_data['equipment_used_display'] = _format_equipment_list(report_data.get('equipment_used', []))
            
            if 'type' not in report_data: report_data['type'] = 'report'
            history.append(report_data)
        except json.JSONDecodeError:
            app.logger.warning(f"Failed to decode tech report JSON: {json_str[:100]}...")
    
    temp_notes = re.sub(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL)
    temp_notes = re.sub(r"--- CUSTOMER_FEEDBACK_START ---.*?--- CUSTOMER_FEEDBACK_END ---", "", temp_notes, flags=re.DOTALL)
    original_notes_text = temp_notes.strip()

    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, original_notes_text

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def _parse_equipment_string(text_input):
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
    if not equipment_data: return 'N/A'
    if isinstance(equipment_data, str): return equipment_data
    lines = []
    if isinstance(equipment_data, list):
        for item in equipment_data:
            if isinstance(item, dict) and "item" in item:
                line = item['item']
                if item.get("quantity") is not None:
                    if isinstance(item['quantity'], (int, float)): line += f" (x{item['quantity']:g})"
                    else: line += f" ({item['quantity']})"
                lines.append(line)
            elif isinstance(item, str): lines.append(item)
    return "<br>".join(lines) if lines else 'N/A'

def generate_qr_code_base64(data, box_size=10, border=4, fill_color='#28a745', back_color='#FFFFFF'):
    try:
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=box_size, border=border)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color=fill_color, back_color=back_color)
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffered.getvalue()).decode("utf-8")
    except Exception as e:
        app.logger.error(f"Error generating QR code: {e}")
        return ""

def _create_backup_zip():
    try:
        all_tasks = gs.get_google_tasks_for_report(show_completed=True)
        if all_tasks is None:
            app.logger.error('Failed to get tasks for backup.')
            return None, None

        memory_file = BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('data/tasks_backup.json', json.dumps(all_tasks, indent=4, ensure_ascii=False))
            zf.writestr('data/settings_backup.json', json.dumps(get_app_settings(), indent=4, ensure_ascii=False))

            project_root = os.path.dirname(os.path.abspath(__file__))
            for folder, _, files in os.walk(project_root):
                for file in files:
                    if file.endswith(('.py', '.html', '.css', '.js', '.json', 'Procfile', 'requirements.txt')) \
                       and file not in ['token.json', '.env', SETTINGS_FILE]:
                        file_path = os.path.join(folder, file)
                        archive_name = os.path.relpath(file_path, project_root)
                        zf.write(file_path, arcname=f'code/{archive_name}')
        memory_file.seek(0)
        backup_filename = f"full_system_backup_{datetime.datetime.now(THAILAND_TZ).strftime('%Y%m%d_%H%M%S')}.zip"
        return memory_file, backup_filename
    except Exception as e:
        app.logger.error(f"Error creating full system backup zip: {e}")
        return None, None

def check_google_api_status():
    """Checks if a valid Google API credential can be obtained."""
    try:
        # get_refreshed_credentials returns None on failure
        return gs.get_refreshed_credentials() is not None
    except Exception as e:
        app.logger.error(f"Unexpected error during Google API status check: {e}")
        return False

@app.context_processor
def inject_global_vars():
    return {
        'now': datetime.datetime.now(THAILAND_TZ),
        'google_api_connected': check_google_api_status()
    }

#</editor-fold>

#<editor-fold desc="Scheduled Jobs and Notifications">

def notify_admin_error(message):
    try:
        admin_group_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
        if admin_group_id:
            line_bot_api.push_message(admin_group_id, TextSendMessage(text=f"‼️ เกิดข้อผิดพลาดร้ายแรงในระบบ ‼️\n\n{message[:900]}"))
    except Exception as e:
        app.logger.error(f"Failed to send critical error notification: {e}")

def send_new_task_notification(task):
    settings = get_app_settings()
    admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')
    if not admin_group_id: return

    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    parsed_dates = parse_google_task_dates(task)
    due_info = f"นัดหมาย: {parsed_dates.get('due_formatted')}" if parsed_dates.get('due_formatted') else "นัดหมาย: - (ยังไม่ระบุ)"
    location_info = f"พิกัด: {customer_info.get('map_url')}" if customer_info.get('map_url') else "พิกัด: - (ไม่มีข้อมูล)"

    message_text = (
        f"✨ มีงานใหม่เข้า!\n\n"
        f"ชื่องาน: {task.get('title', '-')}\n"
        f"ลูกค้า: {customer_info.get('name', '-')}\n"
        f"📞 โทร: {customer_info.get('phone', '-')}\n"
        f"🗓️ {due_info}\n"
        f"📍 {location_info}\n\n"
        f"ดูรายละเอียดในเว็บ:\n{url_for('task_details', task_id=task.get('id'), _external=True)}"
    )
    try:
        line_bot_api.push_message(admin_group_id, TextSendMessage(text=message_text))
        app.logger.info(f"Sent new task notification for task {task['id']} to admin group.")
    except Exception as e:
        app.logger.error(f"Failed to send new task notification for task {task['id']}: {e}")

def send_completion_notification(task, technicians):
    settings = get_app_settings()
    recipients = settings.get('line_recipients', {})
    admin_group_id = recipients.get('admin_group_id')
    tech_group_id = recipients.get('technician_group_id')
    if not admin_group_id and not tech_group_id: return

    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    technician_str = ", ".join(technicians) if technicians else "ไม่ได้ระบุ"
    message_text = (
        f"✅ ปิดงานเรียบร้อย\n\n"
        f"ชื่องาน: {task.get('title', '-')}\n"
        f"ลูกค้า: {customer_info.get('name', '-')}\n"
        f"ช่างผู้รับผิดชอบ: {technician_str}\n\n"
        f"ดูรายละเอียด: {url_for('task_details', task_id=task.get('id'), _external=True)}"
    )
    sent_to = set()
    try:
        if admin_group_id:
            line_bot_api.push_message(admin_group_id, TextSendMessage(text=message_text))
            sent_to.add(admin_group_id)
        if tech_group_id and tech_group_id not in sent_to:
            line_bot_api.push_message(tech_group_id, TextSendMessage(text=message_text))
    except Exception as e:
        app.logger.error(f"Failed to send completion notification for task {task['id']}: {e}")

def send_update_notification(task, new_due_date_str, reason, technicians, is_today):
    settings = get_app_settings()
    admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')
    if not admin_group_id: return

    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    technician_str = ", ".join(technicians) if technicians else "ไม่ได้ระบุ"
    title = "🗓️ อัปเดตงานวันนี้" if is_today else "🗓️ เลื่อนนัดหมาย"
    reason_str = f"รายละเอียด: {reason}\n" if is_today and reason else f"เหตุผล: {reason}\n" if reason else ""

    message_text = (
        f"{title}\n\n"
        f"ชื่องาน: {task.get('title', '-')}\n"
        f"ลูกค้า: {customer_info.get('name', '-')}\n"
        f"📞 โทร: {customer_info.get('phone', '-')}\n"
        f"นัดหมายใหม่: {new_due_date_str}\n"
        f"{reason_str}"
        f"ช่าง: {technician_str}\n\n"
        f"ดูรายละเอียดในเว็บ:\n{url_for('task_details', task_id=task.get('id'), _external=True)}"
    )
    try:
        line_bot_api.push_message(admin_group_id, TextSendMessage(text=message_text))
        app.logger.info(f"Sent update/reschedule notification for task {task['id']} to admin group.")
    except Exception as e:
        app.logger.error(f"Failed to send update/reschedule notification for task {task['id']}: {e}")

def scheduled_backup_job():
    with app.app_context():
        app.logger.info(f"--- Starting Scheduled Backup Job ---")
        overall_success = True
        system_backup_folder_id = gs.find_or_create_drive_folder("System_Backups", GOOGLE_DRIVE_FOLDER_ID)
        if not system_backup_folder_id:
            app.logger.error("Could not find or create System_Backups folder for backup.")
            overall_success = False
        else:
            memory_file_zip, filename_zip = _create_backup_zip()
            if memory_file_zip and filename_zip:
                if gs.upload_data_from_memory_to_drive(memory_file_zip, filename_zip, 'application/zip', system_backup_folder_id):
                    app.logger.info("Automatic full system backup successful.")
                else:
                    app.logger.error("Automatic full system backup failed.")
                    overall_success = False
            else:
                app.logger.error("Failed to create full system backup zip.")
                overall_success = False

        if not gs.backup_settings_to_drive(get_app_settings()):
            app.logger.error("Automatic settings-only backup failed.")
            overall_success = False
        else:
            app.logger.info("Automatic settings-only backup successful.")
        
        app.logger.info(f"--- Finished Scheduled Backup Job ---")
        return overall_success

def scheduled_appointment_reminder_job():
    with app.app_context():
        app.logger.info("Running scheduled appointment reminder job...")
        settings = get_app_settings()
        recipients = settings.get('line_recipients', {})
        admin_group_id = recipients.get('admin_group_id')
        technician_group_id = recipients.get('technician_group_id')

        if not admin_group_id and not technician_group_id:
            app.logger.info("No LINE admin or technician group ID set for appointment reminders. Skipping.")
            return

        tasks_raw = gs.get_google_tasks_for_report(show_completed=False) or []
        today_thai = datetime.date.today()
        upcoming_appointments = []

        for task in tasks_raw:
            if task.get('status') == 'needsAction' and task.get('due'):
                try:
                    due_dt_utc = date_parse(task['due'])
                    if due_dt_utc.astimezone(THAILAND_TZ).date() == today_thai:
                        upcoming_appointments.append(task)
                except (ValueError, TypeError):
                    app.logger.warning(f"Could not parse due date for reminder task {task.get('id')}: {task.get('due')}")
                    continue

        if not upcoming_appointments:
            app.logger.info("No upcoming appointments for today.")
            return

        upcoming_appointments.sort(key=lambda x: date_parse(x['due']) if x.get('due') else datetime.datetime.max.replace(tzinfo=pytz.utc))

        for task in upcoming_appointments:
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            parsed_dates = parse_google_task_dates(task)
            location_info = f"พิกัด: {customer_info.get('map_url')}" if customer_info.get('map_url') else "พิกัด: - (ไม่มีข้อมูล)"
            message_text = (
                f"🔔 งานสำหรับวันนี้\n\n"
                f"ชื่องาน: {task.get('title', '-')}\n"
                f"👤 ลูกค้า: {customer_info.get('name', '-')}\n"
                f"📞 โทร: {customer_info.get('phone', '-')}\n"
                f"🗓️ นัดหมาย: {parsed_dates.get('due_formatted', '-')}\n"
                f"📍 {location_info}\n\n"
                f"🔗 ดูรายละเอียด/แก้ไข:\n{url_for('task_details', task_id=task.get('id'), _external=True)}"
            )
            try:
                sent_to = set()
                if admin_group_id:
                    line_bot_api.push_message(admin_group_id, TextSendMessage(text=message_text))
                    sent_to.add(admin_group_id)
                if technician_group_id and technician_group_id not in sent_to:
                    line_bot_api.push_message(technician_group_id, TextSendMessage(text=message_text))
            except Exception as e:
                app.logger.error(f"Failed to send appointment reminder for task {task['id']}: {e}")

def _create_customer_follow_up_flex_message(task_id, task_title, customer_name):
    problem_action = URIAction(label='🚨 ยังมีปัญหาอยู่', uri=f"https://liff.line.me/{LIFF_ID_FORM}/customer_problem_form?task_id={task_id}")
    return BubbleContainer(
        body=BoxComponent(layout='vertical', spacing='md', contents=[
            TextComponent(text="สอบถามหลังการซ่อม", weight='bold', size='lg', color='#1DB446', align='center'), SeparatorComponent(margin='md'),
            TextComponent(text=f"เรียนคุณ {customer_name},", size='sm', wrap=True),
            TextComponent(text=f"เกี่ยวกับงาน: {task_title}", size='sm', wrap=True, color='#666666'), SeparatorComponent(margin='lg'),
            TextComponent(text="ไม่ทราบว่าหลังจากทีมงานของเราเข้าบริการแล้ว ทุกอย่างเรียบร้อยดีหรือไม่ครับ/คะ?", size='md', wrap=True, align='center'),
            BoxComponent(layout='vertical', spacing='sm', margin='md', contents=[
                ButtonComponent(style='primary', height='sm', color='#28a745', action=PostbackAction(label='✅ งานเรียบร้อยดี', data=f'action=customer_feedback&task_id={task_id}&feedback=ok', display_text='ขอบคุณสำหรับคำยืนยันครับ/ค่ะ!')),
                ButtonComponent(style='secondary', height='sm', color='#dc3545', action=problem_action)
            ]),
        ])
    )

def scheduled_customer_follow_up_job():
    with app.app_context():
        app.logger.info("Running scheduled customer follow-up job...")
        settings = get_app_settings()
        admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')

        tasks_raw = gs.get_google_tasks_for_report(show_completed=True) or []
        now_utc = datetime.datetime.now(pytz.utc)
        one_day_ago_utc = now_utc - timedelta(days=1)
        two_days_ago_utc = now_utc - timedelta(days=2)

        for task in tasks_raw:
            if task.get('status') == 'completed' and task.get('completed'):
                try:
                    completed_dt_utc = date_parse(task['completed'])
                    if two_days_ago_utc <= completed_dt_utc < one_day_ago_utc:
                        notes = task.get('notes', '')
                        feedback_data = parse_customer_feedback_from_notes(notes)
                        if 'follow_up_sent_date' in feedback_data: continue

                        customer_info = parse_customer_info_from_notes(notes)
                        customer_line_id = feedback_data.get('customer_line_user_id')
                        if not customer_line_id: continue

                        flex_content = _create_customer_follow_up_flex_message(task['id'], task['title'], customer_info.get('name', 'N/A'))
                        flex_message = FlexSendMessage(alt_text="สอบถามความพึงพอใจหลังการซ่อม", contents=flex_content)

                        try:
                            line_bot_api.push_message(customer_line_id, flex_message)
                            app.logger.info(f"Sent follow-up message to customer {customer_line_id} for task {task['id']}.")
                            
                            feedback_data['follow_up_sent_date'] = datetime.datetime.now(THAILAND_TZ).isoformat()
                            history_reports, base_notes = parse_tech_report_from_notes(notes)
                            tech_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history_reports])
                            new_notes = base_notes.strip()
                            if tech_reports_text: new_notes += tech_reports_text
                            new_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
                            gs.update_google_task(task['id'], notes=new_notes)
                            cache.clear()
                        except Exception as e:
                            app.logger.error(f"Failed to send direct follow-up to {customer_line_id}: {e}. Notifying admin.")
                            if admin_group_id:
                                line_bot_api.push_message(admin_group_id, [TextSendMessage(text=f"⚠️ ส่ง Follow-up ให้ลูกค้า {customer_info.get('name')} (Task ID: {task['id']}) ไม่สำเร็จ โปรดส่งข้อความนี้แทน:"), flex_message])
                except Exception as e:
                    app.logger.warning(f"Could not process task {task.get('id')} for follow-up: {e}", exc_info=True)

def run_scheduler():
    global scheduler
    settings = get_app_settings()
    if scheduler.running:
        app.logger.info("Scheduler already running, shutting down before reconfiguring...")
        scheduler.shutdown(wait=False)
    
    scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)
    ab = settings.get('auto_backup', {})
    if ab.get('enabled'):
        scheduler.add_job(scheduled_backup_job, CronTrigger(hour=ab.get('hour_thai', 2), minute=ab.get('minute_thai', 0)), id='auto_system_backup', replace_existing=True)
        app.logger.info(f"Scheduled auto backup for {ab.get('hour_thai', 2)}:{ab.get('minute_thai', 0):02d} Thai time.")
    else:
        if scheduler.get_job('auto_system_backup'):
            scheduler.remove_job('auto_system_backup')
            app.logger.info("Auto backup job disabled and removed.")

    rt = settings.get('report_times', {})
    scheduler.add_job(scheduled_appointment_reminder_job, CronTrigger(hour=rt.get('appointment_reminder_hour_thai', 7), minute=0), id='daily_appointment_reminder', replace_existing=True)
    scheduler.add_job(scheduled_customer_follow_up_job, CronTrigger(hour=rt.get('customer_followup_hour_thai', 9), minute=5), id='daily_customer_followup', replace_existing=True)
    app.logger.info(f"Scheduled appointment reminders for {rt.get('appointment_reminder_hour_thai', 7)}:00 and customer follow-up for {rt.get('customer_followup_hour_thai', 9)}:05 Thai time.")

    scheduler.start()
    app.logger.info("APScheduler started/reconfigured.")

def cleanup_scheduler():
    if scheduler is not None and scheduler.running:
        app.logger.info("Scheduler is running, shutting it down.")
        scheduler.shutdown(wait=False)
    else:
        app.logger.info("Scheduler not running or not initialized, skipping shutdown.")

#</editor-fold>

# --- Initial app setup calls ---
with app.app_context():
    if check_google_api_status():
        gs.load_settings_from_drive_on_startup(save_app_settings)
    else:
        app.logger.warning("Skipping settings load from Drive due to API connection issue.")
    run_scheduler()

atexit.register(cleanup_scheduler)

# --- Error Handlers ---
@app.errorhandler(404)
def page_not_found(e): return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    app.logger.error(f"Server Error: {e}", exc_info=True)
    return render_template('500.html'), 500

#<editor-fold desc="Web Application Routes">

@app.route("/")
def root_redirect(): return redirect(url_for('summary'))

@app.route('/summary')
def summary():
    search_query = request.args.get('search', '')
    status_filter = request.args.get('status', 'all')
    
    all_tasks_raw = gs.get_google_tasks_for_report(show_completed=True)
    if all_tasks_raw is None:
        flash('ไม่สามารถเชื่อมต่อกับ Google Tasks API ได้', 'danger')
        return render_template('summary.html', tasks=[], search_query=search_query, status_filter=status_filter)

    processed_tasks = []
    today = datetime.datetime.now(THAILAND_TZ).date()
    now_utc = datetime.datetime.now(pytz.utc)

    for task in all_tasks_raw:
        if not task.get('title'): continue

        p_task = parse_google_task_dates(task)
        p_task['customer'] = parse_customer_info_from_notes(p_task.get('notes', ''))
        is_completed = p_task.get('status') == 'completed'
        is_overdue = False
        is_today = False
        
        if not is_completed and p_task.get('due'):
            due_dt = date_parse(p_task['due'])
            if due_dt < now_utc: is_overdue = True
            if due_dt.astimezone(THAILAND_TZ).date() == today: is_today = True

        p_task['is_completed'] = is_completed
        p_task['is_overdue'] = is_overdue
        p_task['is_today'] = is_today

        matches_search = (search_query.lower() in p_task.get('title', '').lower() or
                          search_query.lower() in p_task['customer'].get('name', '').lower() or
                          search_query.lower() in p_task['customer'].get('phone', '').lower() or
                          search_query.lower() in p_task['customer'].get('organization', '').lower())
        matches_status = (status_filter == 'all' or
                          (status_filter == 'completed' and is_completed) or
                          (status_filter == 'overdue' and is_overdue) or
                          (status_filter == 'today' and is_today) or
                          (status_filter == 'pending' and not is_completed and not is_overdue and not is_today))

        if matches_search and matches_status: processed_tasks.append(p_task)
            
    processed_tasks.sort(key=lambda x: (x['is_completed'], not x['is_overdue'], x.get('due') or 'z'), reverse=False)
    return render_template('summary.html', tasks=processed_tasks, search_query=search_query, status_filter=status_filter)

@app.route('/summary/print')
def summary_print():
    search_query = request.args.get('search', '')
    status_filter_key = request.args.get('status', 'all')
    status_map = {'all': 'ทั้งหมด', 'pending': 'ยังไม่เสร็จ', 'completed': 'เสร็จเรียบร้อย', 'overdue': 'เลยกำหนด', 'today': 'งานวันนี้'}
    status_filter_display = status_map.get(status_filter_key, 'ทั้งหมด')

    all_tasks_raw = gs.get_google_tasks_for_report(show_completed=True)
    if all_tasks_raw is None: return "Error: Could not fetch tasks from Google API.", 500

    processed_tasks = []
    today = datetime.datetime.now(THAILAND_TZ).date()
    now_utc = datetime.datetime.now(pytz.utc)

    for task in all_tasks_raw:
        if not task.get('title'): continue
        p_task = parse_google_task_dates(task)
        p_task['customer'] = parse_customer_info_from_notes(p_task.get('notes', ''))
        is_completed = p_task.get('status') == 'completed'
        is_overdue = False
        is_today = False
        if not is_completed and p_task.get('due'):
            due_dt = date_parse(p_task['due'])
            if due_dt < now_utc: is_overdue = True
            if due_dt.astimezone(THAILAND_TZ).date() == today: is_today = True
        p_task['is_completed'] = is_completed
        p_task['is_overdue'] = is_overdue
        p_task['is_today'] = is_today
        
        matches_search = (search_query.lower() in p_task.get('title', '').lower() or
                          search_query.lower() in p_task['customer'].get('name', '').lower() or
                          search_query.lower() in p_task['customer'].get('phone', '').lower() or
                          search_query.lower() in p_task['customer'].get('organization', '').lower())
        matches_status = (status_filter_key == 'all' or
                          (status_filter_key == 'completed' and is_completed) or
                          (status_filter_key == 'overdue' and is_overdue) or
                          (status_filter_key == 'today' and is_today) or
                          (status_filter_key == 'pending' and not is_completed and not is_overdue and not is_today))
        if matches_search and matches_status: processed_tasks.append(p_task)
            
    processed_tasks.sort(key=lambda x: (x['is_completed'], not x['is_overdue'], x.get('due') or 'z'), reverse=False)
    return render_template('summary_print.html', tasks=processed_tasks, search_query=search_query, status_filter=status_filter_display)

@app.route('/task/<task_id>', methods=['GET', 'POST'])
def task_details(task_id):
    if request.method == 'POST':
        task_raw = gs.get_single_task(task_id)
        if not task_raw:
            flash('ไม่พบงานที่ต้องการอัปเดต', 'danger')
            abort(404)
        
        action = request.form.get('action')
        update_payload = {}
        notification_to_send = None
        
        history, base_notes_text = parse_tech_report_from_notes(task_raw.get('notes', ''))
        feedback_data = parse_customer_feedback_from_notes(task_raw.get('notes', ''))
        
        new_attachments_from_ajax_json = request.form.get('uploaded_attachments_json')
        new_attachments = []
        if new_attachments_from_ajax_json:
            try: new_attachments = json.loads(new_attachments_from_ajax_json)
            except json.JSONDecodeError: app.logger.error("Failed to decode uploaded_attachments_json from request.")

        if action == 'save_report':
            work_summary = str(request.form.get('work_summary', '')).strip()
            selected_technicians = [t.strip() for t in request.form.get('technicians_report', '').split(',') if t.strip()]
            if not (work_summary or new_attachments): return jsonify({'status': 'error', 'message': 'กรุณากรอกสรุปงาน หรือแนบไฟล์รูปภาพ'}), 400
            if not selected_technicians: return jsonify({'status': 'error', 'message': 'กรุณาเลือกช่างผู้รับผิดชอบ'}), 400

            history.append({
                'type': 'report', 'summary_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
                'work_summary': work_summary, 'equipment_used': _parse_equipment_string(request.form.get('equipment_used', '')),
                'attachments': new_attachments, 'technicians': selected_technicians
            })
            flash_message = 'เพิ่มรายงานความคืบหน้าเรียบร้อยแล้ว!'
        
        elif action == 'reschedule_task':
            reschedule_due_str = str(request.form.get('reschedule_due', '')).strip()
            reschedule_reason = str(request.form.get('reschedule_reason', '')).strip()
            selected_technicians = [t.strip() for t in request.form.get('technicians_reschedule', '').split(',') if t.strip()]
            if not reschedule_due_str: return jsonify({'status': 'error', 'message': 'กรุณากำหนดวันนัดหมายใหม่'}), 400
            
            try:
                dt_local = THAILAND_TZ.localize(date_parse(reschedule_due_str))
                update_payload['due'] = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
                update_payload['status'] = 'needsAction'
                new_due_date_formatted = dt_local.strftime("%d/%m/%y %H:%M")
                is_today = dt_local.date() == datetime.datetime.now(THAILAND_TZ).date()
                notification_to_send = ('update', new_due_date_formatted, reschedule_reason, selected_technicians, is_today)
            except ValueError: return jsonify({'status': 'error', 'message': 'รูปแบบวันเวลานัดหมายใหม่ไม่ถูกต้อง'}), 400

            history.append({
                'type': 'reschedule', 'summary_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
                'reason': reschedule_reason, 'new_due_date': new_due_date_formatted, 'technicians': selected_technicians
            })
            flash_message = 'เลื่อนนัดและบันทึกเหตุผลเรียบร้อยแล้ว'

        elif action == 'complete_task':
            work_summary = str(request.form.get('work_summary', '')).strip()
            if not work_summary: return jsonify({'status': 'error', 'message': 'กรุณากรอกสรุปงานเพื่อปิดงาน'}), 400
            selected_technicians = [t.strip() for t in request.form.get('technicians_report', '').split(',') if t.strip()]
            if not selected_technicians: return jsonify({'status': 'error', 'message': 'กรุณาเลือกช่างผู้รับผิดชอบ'}), 400
            
            history.append({
                'type': 'report', 'summary_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
                'work_summary': work_summary, 'equipment_used': _parse_equipment_string(request.form.get('equipment_used', '')),
                'attachments': new_attachments, 'technicians': selected_technicians, 'task_status': 'completed'
            })
            update_payload['status'] = 'completed'
            notification_to_send = ('completion', selected_technicians)
            flash_message = 'ปิดงานและบันทึกรายงานสรุปเรียบร้อยแล้ว!'
        
        else: return jsonify({'status': 'error', 'message': 'ไม่พบการกระทำที่ร้องขอ'}), 400
            
        history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
        all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
        final_notes = base_notes_text
        if all_reports_text: final_notes += all_reports_text
        if feedback_data: final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        
        update_payload['notes'] = final_notes
        updated_task = gs.update_google_task(task_id, **update_payload)

        if updated_task:
            cache.clear()
            if notification_to_send:
                notif_type = notification_to_send[0]
                if notif_type == 'update': send_update_notification(updated_task, *notification_to_send[1:])
                elif notif_type == 'completion': send_completion_notification(updated_task, *notification_to_send[1:])
            return jsonify({'status': 'success', 'message': flash_message})
        else:
            return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกข้อมูลหลัก!'}), 500

    task_raw = gs.get_single_task(task_id)
    if not task_raw: abort(404)
    
    p_task = parse_google_task_dates(task_raw)
    p_task['tech_history'], _ = parse_tech_report_from_notes(p_task.get('notes', ''))
    p_task['customer'] = parse_customer_info_from_notes(p_task.get('notes', ''))
    p_task['feedback'] = parse_customer_feedback_from_notes(p_task.get('notes', ''))
    
    feedback_qr_data = url_for('customer_feedback_form', task_id=task_id, _external=True)
    p_task['feedback_qr_code'] = generate_qr_code_base64(feedback_qr_data)
    
    settings = get_app_settings()
    all_attachments = []
    for report in p_task['tech_history']:
        if report.get('attachments'):
            report_date = parse_google_task_dates({'summary_date': report['summary_date']}).get('summary_date_formatted', '')
            for att in report['attachments']:
                att_copy = att.copy()
                att_copy['report_date'] = report_date
                all_attachments.append(att_copy)

    return render_template('task_details.html', task=p_task, settings=settings, text_snippets=TEXT_SNIPPETS, all_attachments=all_attachments)

@app.route('/task/<task_id>/edit', methods=['GET', 'POST'])
def edit_task(task_id):
    task = gs.get_single_task(task_id)
    if not task: abort(404)

    if request.method == 'POST':
        task_title = request.form.get('task_title')
        appointment_due_str = request.form.get('appointment_due')
        org_name = request.form.get('organization_name', '').strip()
        cust_name = request.form.get('customer_name', '').strip()
        cust_phone = request.form.get('customer_phone', '').strip()
        address = request.form.get('address', '').strip()
        map_url = request.form.get('latitude_longitude', '').strip()

        customer_notes_parts = []
        if org_name: customer_notes_parts.append(f"หน่วยงาน: {org_name}")
        if cust_name: customer_notes_parts.append(f"ลูกค้า: {cust_name}")
        if cust_phone: customer_notes_parts.append(f"เบอร์โทรศัพท์: {cust_phone}")
        if address: customer_notes_parts.append(f"ที่อยู่: {address}")
        if map_url: customer_notes_parts.append(f"พิกัด: {map_url}")
        customer_notes_section = "\n".join(customer_notes_parts)

        tech_history, _ = parse_tech_report_from_notes(task.get('notes', ''))
        feedback_data = parse_customer_feedback_from_notes(task.get('notes', ''))
        tech_reports_text = ""
        if tech_history: tech_reports_text = "\n\n" + "\n\n".join([f"--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in tech_history])
        feedback_text = ""
        if feedback_data: feedback_text = f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        new_notes = customer_notes_section + tech_reports_text + feedback_text

        new_due_iso = None
        if appointment_due_str:
            try:
                dt_local = THAILAND_TZ.localize(datetime.datetime.strptime(appointment_due_str, '%Y-%m-%dT%H:%M'))
                new_due_iso = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
            except ValueError:
                flash('รูปแบบวันที่และเวลาไม่ถูกต้อง', 'danger')
                return redirect(url_for('edit_task', task_id=task_id))

        updated_task = gs.update_google_task(task_id, title=task_title, notes=new_notes, due=new_due_iso)
        if updated_task:
            cache.clear()
            flash('อัปเดตข้อมูลงานเรียบร้อยแล้ว', 'success')
            return redirect(url_for('task_details', task_id=task_id))
        else:
            flash('เกิดข้อผิดพลาดในการอัปเดตข้อมูล', 'danger')
            return redirect(url_for('edit_task', task_id=task_id))

    p_task = parse_google_task_dates(task)
    p_task['customer'] = parse_customer_info_from_notes(p_task.get('notes', ''))
    return render_template('edit_task.html', task=p_task)

@app.route('/calendar')
def calendar_page():
    all_tasks = gs.get_google_tasks_for_report(show_completed=False)
    if all_tasks is None:
        flash('ไม่สามารถโหลดข้อมูลงานได้', 'danger')
        unscheduled_tasks = []
    else:
        unscheduled_tasks = [
            {**task, 'customer': parse_customer_info_from_notes(task.get('notes', ''))}
            for task in all_tasks if not task.get('due')
        ]
    return render_template('calendar.html', unscheduled_tasks=unscheduled_tasks)

@app.route('/technician_report')
def technician_report():
    now = datetime.datetime.now(THAILAND_TZ)
    try:
        selected_month = int(request.args.get('month', now.month))
        selected_year = int(request.args.get('year', now.year))
    except (ValueError, TypeError):
        selected_month = now.month
        selected_year = now.year

    months = [{'value': i, 'name': THAILAND_TZ.localize(datetime.datetime(2000, i, 1)).strftime('%B')} for i in range(1, 13)]
    years = list(range(now.year + 1, now.year - 5, -1))

    all_completed_tasks = gs.get_google_tasks_for_report(show_completed=True)
    if all_completed_tasks is None:
        flash('ไม่สามารถดึงข้อมูลรายงานได้', 'danger')
        return render_template('technician_report.html', report_data={}, months=months, years=years, selected_month=selected_month, selected_year=selected_year, technician_list=[])

    report_data = defaultdict(lambda: {'count': 0, 'tasks': []})
    for task in all_completed_tasks:
        if task.get('status') != 'completed' or not task.get('completed'): continue
        try: completed_dt = date_parse(task['completed']).astimezone(THAILAND_TZ)
        except (ValueError, TypeError): continue

        if completed_dt.year == selected_year and completed_dt.month == selected_month:
            tech_history, _ = parse_tech_report_from_notes(task.get('notes', ''))
            completion_report = next((r for r in tech_history if r.get('task_status') == 'completed'), None)
            technicians = []
            if completion_report and 'technicians' in completion_report: technicians = completion_report['technicians']
            elif tech_history and 'technicians' in tech_history[0]: technicians = tech_history[0]['technicians']
            if not technicians: technicians = ["ไม่ได้ระบุ"]
            p_task = parse_google_task_dates(task)
            for tech_name in technicians:
                report_data[tech_name]['count'] += 1
                report_data[tech_name]['tasks'].append(p_task)
    
    for tech_name in report_data: report_data[tech_name]['tasks'].sort(key=lambda x: x.get('completed', ''), reverse=True)
    technician_list = get_app_settings().get('technician_list', [])
    return render_template('technician_report.html', report_data=dict(sorted(report_data.items())), months=months, years=years, selected_month=selected_month, selected_year=selected_year, technician_list=technician_list)

@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        technician_list_json_str = request.form.get('technician_list_json', '[]')
        try: technician_list = json.loads(technician_list_json_str)
        except json.JSONDecodeError:
            flash('เกิดข้อผิดพลาดในการอ่านข้อมูลช่าง', 'danger')
            return redirect(url_for('settings_page'))

        settings_data = {
            'report_times': {
                'appointment_reminder_hour_thai': int(request.form.get('appointment_reminder_hour', 7)),
                'outstanding_report_hour_thai': int(request.form.get('outstanding_report_hour', 20)),
                'customer_followup_hour_thai': int(request.form.get('customer_followup_hour', 9))
            },
            'line_recipients': {
                'admin_group_id': request.form.get('admin_group_id', '').strip(),
                'technician_group_id': request.form.get('technician_group_id', '').strip(),
                'manager_user_id': request.form.get('manager_user_id', '').strip()
            },
            'auto_backup': {
                'enabled': request.form.get('auto_backup_enabled') == 'on',
                'hour_thai': int(request.form.get('auto_backup_hour', 2)),
                'minute_thai': int(request.form.get('auto_backup_minute', 0))
            },
            'shop_info': {
                'contact_phone': request.form.get('shop_contact_phone', '').strip(),
                'line_id': request.form.get('shop_line_id', '').strip()
            },
            'technician_list': technician_list
        }
        if save_app_settings(settings_data):
            run_scheduler()
            cache.clear()
            if gs.backup_settings_to_drive(get_app_settings()): flash('บันทึกและสำรองการตั้งค่าไปที่ Google Drive เรียบร้อยแล้ว!', 'success')
            else: flash('บันทึกการตั้งค่าสำเร็จ แต่สำรองไปที่ Google Drive ไม่สำเร็จ!', 'warning')
        else: flash('เกิดข้อผิดพลาดในการบันทึกการตั้งค่า!', 'danger')
        return redirect(url_for('settings_page'))

    current_settings = get_app_settings()
    return render_template('settings_page.html', settings=current_settings)

@app.route('/public_report/<task_id>')
def public_task_report(task_id):
    task = gs.get_single_task(task_id)
    if not task or task.get('status') != 'completed': abort(404)
    
    notes = task.get('notes', '')
    customer = parse_customer_info_from_notes(notes)
    reports, _ = parse_tech_report_from_notes(notes)
    latest_report = reports[0] if reports else {}
    app_settings = get_app_settings() 
    equipment = latest_report.get('equipment_used', [])
    catalog = {item['item_name']: item for item in app_settings.get('equipment_catalog', [])}
    costs, total = [], 0.0
    
    if isinstance(equipment, list):
        for item in equipment:
            name, qty = item.get('item'), item.get('quantity', 0)
            if isinstance(qty, (int, float)):
                cat_item = catalog.get(name, {})
                price = float(cat_item.get('price', 0))
                subtotal = qty * price
                total += subtotal
                costs.append({'item': name, 'quantity': qty, 'unit': cat_item.get('unit', ''), 'price_per_unit': price, 'subtotal': subtotal})
            else:
                costs.append({'item': name, 'quantity': qty, 'unit': catalog.get(name, {}).get('unit', ''), 'price_per_unit': 'N/A', 'subtotal': 'N/A'})
    
    return render_template('public_task_report.html', task=task, customer_info=customer, latest_report=latest_report, detailed_costs=costs, total_cost=total, settings=app_settings)

@app.route('/customer_feedback_form')
def customer_feedback_form():
    task_id = request.args.get('task_id')
    task = gs.get_single_task(task_id)
    if not task: abort(404)
    parsed = parse_google_task_dates(task)
    parsed['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
    return render_template('customer_feedback_form.html', task=parsed, LINE_LOGIN_CHANNEL_ID=LINE_LOGIN_CHANNEL_ID)

#</editor-fold>

#<editor-fold desc="API, Forms and Data Handling Routes">

@app.route('/form', methods=['GET', 'POST'])
def form_page():
    if request.method == 'POST':
        task_title = str(request.form.get('task_title', '')).strip()
        customer_name = str(request.form.get('customer', '')).strip()
        organization_name = str(request.form.get('organization_name', '')).strip()

        if not task_title or not customer_name:
            flash('กรุณากรอกชื่อผู้ติดต่อและรายละเอียดงาน', 'danger')
            return redirect(url_for('form_page'))

        notes_lines = []
        if organization_name: notes_lines.append(f"หน่วยงาน: {organization_name}")
        notes_lines.extend([
            f"ลูกค้า: {customer_name}", f"เบอร์โทรศัพท์: {str(request.form.get('phone', '')).strip()}",
            f"ที่อยู่: {str(request.form.get('address', '')).strip()}",
        ])
        map_url = str(request.form.get('latitude_longitude', '')).strip()
        if map_url: notes_lines.append(f"พิกัด: {map_url}")
        notes = "\n".join(filter(None, notes_lines))

        due_date_gmt = None
        appointment_str = str(request.form.get('appointment', '')).strip()
        if appointment_str:
            try:
                dt_local = THAILAND_TZ.localize(date_parse(appointment_str))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
            except ValueError:
                flash('รูปแบบวันเวลานัดหมายไม่ถูกต้อง', 'warning')
                return render_template('form.html', form_data=request.form)

        new_task = gs.create_google_task(task_title, notes=notes, due=due_date_gmt)
        if new_task:
            cache.clear()
            send_new_task_notification(new_task)
            flash('สร้างงานใหม่เรียบร้อยแล้ว!', 'success')
            return redirect(url_for('task_details', task_id=new_task['id']))
        else:
            flash('เกิดข้อผิดพลาดในการสร้างงาน', 'danger')
            return render_template('form.html', form_data=request.form)

    return render_template('form.html', text_snippets=TEXT_SNIPPETS)

@app.route('/api/customers')
def api_customers():
    search_term = request.args.get('q', '').lower()
    customer_list = get_customer_database()
    if search_term:
        filtered_customers = [c for c in customer_list if 
            search_term in c.get('name', '').lower() or 
            search_term in c.get('phone', '').lower() or
            search_term in c.get('organization', '').lower()]
    else: filtered_customers = customer_list
    return jsonify(filtered_customers[:20])

@app.route('/api/calendar_tasks')
def api_calendar_tasks():
    all_tasks = gs.get_google_tasks_for_report(show_completed=True)
    if all_tasks is None: return jsonify({"error": "Could not fetch tasks"}), 500

    events = []
    today = datetime.datetime.now(THAILAND_TZ).date()
    now_utc = datetime.datetime.now(pytz.utc)

    for task in all_tasks:
        if not task.get('due'): continue
        customer_info = parse_customer_info_from_notes(task.get('notes', ''))
        customer_name = customer_info.get('name') or customer_info.get('organization') or 'N/A'
        is_completed = task.get('status') == 'completed'
        is_overdue = False
        is_today = False
        due_dt = date_parse(task['due'])
        if not is_completed:
            if due_dt < now_utc: is_overdue = True
            if due_dt.astimezone(THAILAND_TZ).date() == today: is_today = True

        events.append({
            'id': task['id'], 'title': f"{customer_name}: {task['title']}", 'start': task['due'], 'allDay': False,
            'extendedProps': {'is_completed': is_completed, 'is_overdue': is_overdue, 'is_today': is_today}
        })
    return jsonify(events)

@app.route('/api/task/schedule_from_calendar', methods=['POST'])
def schedule_task_from_calendar():
    data = request.get_json()
    task_id = data.get('task_id')
    new_due_date_iso = data.get('new_due_date')
    if not task_id or not new_due_date_iso: return jsonify({'status': 'error', 'message': 'Missing task_id or new_due_date'}), 400

    try:
        due_utc = date_parse(new_due_date_iso)
        due_iso_str = due_utc.isoformat().replace('+00:00', 'Z')
        updated_task = gs.update_google_task(task_id, due=due_iso_str, status='needsAction')
        if updated_task:
            cache.clear()
            return jsonify({'status': 'success', 'message': 'นัดหมายถูกอัปเดตแล้ว'})
        else: return jsonify({'status': 'error', 'message': 'ไม่สามารถอัปเดตงานใน Google Tasks ได้'}), 500
    except Exception as e:
        app.logger.error(f"Error scheduling task from calendar: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/upload_attachment', methods=['POST'])
def api_upload_attachment():
    if 'file' not in request.files: return jsonify({'status': 'error', 'message': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({'status': 'error', 'message': 'No selected file'}), 400
    
    task_id = request.form.get('task_id')
    if not task_id: return jsonify({'status': 'error', 'message': 'Task ID is missing'}), 400

    file.seek(0, os.SEEK_END)
    file_length = file.tell()
    file.seek(0)
    
    if file_length > MAX_FILE_SIZE_BYTES:
        if file.mimetype and file.mimetype.startswith('image/'):
            try:
                img = Image.open(file)
                if img.mode in ("RGBA", "P"): img = img.convert("RGB")
                output_buffer = BytesIO()
                img.save(output_buffer, format='JPEG', quality=85, optimize=True)
                output_buffer.seek(0)
                file_to_upload = output_buffer
                filename = os.path.splitext(secure_filename(file.filename))[0] + '.jpg'
                mime_type = 'image/jpeg'
                app.logger.info(f"Compressed image '{file.filename}' successfully.")
            except Exception as e:
                app.logger.error(f"Could not compress image '{file.filename}': {e}")
                return jsonify({'status': 'error', 'message': f'ไฟล์รูปภาพใหญ่เกินไปและไม่สามารถบีบอัดได้'}), 413
        else: return jsonify({'status': 'error', 'message': f'ไฟล์ใหญ่เกินขนาดที่กำหนด ({MAX_FILE_SIZE_MB}MB)'}), 413
    else:
        file_to_upload = file
        filename = secure_filename(file.filename)
        mime_type = file.mimetype or mimetypes.guess_type(filename)[0]

    attachments_base_folder_id = gs.find_or_create_drive_folder("Task_Attachments", GOOGLE_DRIVE_FOLDER_ID)
    if not attachments_base_folder_id: return jsonify({'status': 'error', 'message': 'Could not create base Task_Attachments folder'}), 500

    target_date = datetime.datetime.now(THAILAND_TZ)
    task_raw = gs.get_single_task(task_id) if task_id != 'new_task_placeholder' else None
    if task_raw and task_raw.get('created'):
        try: target_date = date_parse(task_raw.get('created')).astimezone(THAILAND_TZ)
        except (ValueError, TypeError): pass
    
    monthly_folder_name = target_date.strftime('%Y-%m')
    monthly_folder_id = gs.find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)
    if not monthly_folder_id: return jsonify({'status': 'error', 'message': f'Could not create monthly folder: {monthly_folder_name}'}), 500
    
    customer_info = parse_customer_info_from_notes(task_raw.get('notes', '')) if task_raw else {'name': 'New_Uploads'}
    sanitized_customer_name = sanitize_filename(customer_info.get('name', 'Unknown_Customer'))
    customer_task_folder_name = f"{sanitized_customer_name} - {task_id}"
    final_upload_folder_id = gs.find_or_create_drive_folder(customer_task_folder_name, monthly_folder_id)
    if not final_upload_folder_id: return jsonify({'status': 'error', 'message': 'Could not determine final upload folder'}), 500

    drive_file = gs.upload_data_from_memory_to_drive(file_to_upload, filename, mime_type, final_upload_folder_id)
    if drive_file: return jsonify({'status': 'success', 'file_info': {'id': drive_file.get('id'), 'url': drive_file.get('webViewLink'), 'name': filename}})
    else: return jsonify({'status': 'error', 'message': 'Failed to upload to Google Drive'}), 500

@app.route('/submit_customer_problem', methods=['POST'])
def submit_customer_problem():
    data = request.json
    task_id, problem_desc, user_id = data.get('task_id'), data.get('problem_description'), data.get('customer_line_user_id')
    if not task_id or not problem_desc: return jsonify({"status": "error"}), 400
    task = gs.get_single_task(task_id)
    if not task: return jsonify({"status": "error"}), 404
    notes = task.get('notes', '')
    feedback = parse_customer_feedback_from_notes(notes)
    feedback.update({'feedback_date': datetime.datetime.now(THAILAND_TZ).isoformat(), 'feedback_type': 'problem_reported', 'customer_line_user_id': user_id, 'problem_description': problem_desc})

    reports_history, base = parse_tech_report_from_notes(notes)
    reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in reports_history])
    final_notes = f"{base.strip()}"
    if reports_text: final_notes += reports_text
    final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

    gs.update_google_task(task_id=task_id, notes=final_notes, status='needsAction')
    cache.clear()
    admin_group = get_app_settings().get('line_recipients', {}).get('admin_group_id')
    if admin_group:
        customer = parse_customer_info_from_notes(notes)
        notif = f"🚨 ลูกค้าแจ้งปัญหา!\nงาน: {task.get('title')}\nลูกค้า: {customer.get('name', 'N/A')}\nปัญหา: {problem_desc}\nดูรายละเอียด: {url_for('task_details', task_id=task_id, _external=True)}"
        try: line_bot_api.push_message(admin_group, TextSendMessage(text=notif))
        except Exception as e: app.logger.error(f"Failed to send problem notification: {e}")
    return jsonify({"status": "success"})

#</editor-fold>

#<editor-fold desc="LINE Bot and Webhook">

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try: handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid LINE signature. Please check your channel secret.")
        abort(400)
    except Exception as e:
        app.logger.error(f"Error handling LINE webhook event: {e}", exc_info=True)
        abort(500)
    return 'OK'

def create_task_list_message(title, tasks, limit=5):
    if not tasks: return TextSendMessage(text=f"ไม่พบรายการ{title}ในขณะนี้")
    message = f"📋 {title}\n\n"
    tasks.sort(key=lambda x: date_parse(x['due']) if x.get('due') else datetime.datetime.max.replace(tzinfo=pytz.utc))
    for i, task in enumerate(tasks[:limit]):
        customer = parse_customer_info_from_notes(task.get('notes', ''))
        due = parse_google_task_dates(task).get('due_formatted', 'ไม่มีกำหนด')
        message += f"{i+1}. {task.get('title')}\n   - ลูกค้า: {customer.get('name', 'N/A')}\n   - นัดหมาย: {due}\n\n"
    if len(tasks) > limit: message += f"... และอีก {len(tasks) - limit} รายการ"
    return TextSendMessage(text=message)

def create_task_flex_message(task):
    customer = parse_customer_info_from_notes(task.get('notes', ''))
    dates = parse_google_task_dates(task)
    return BubbleContainer(
        body=BoxComponent(layout='vertical', spacing='md', contents=[
            TextComponent(text=task.get('title', '...'), weight='bold', size='lg', wrap=True), SeparatorComponent(margin='md'),
            BoxComponent(layout='vertical', margin='lg', spacing='sm', contents=[
                BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='ลูกค้า:', color='#AAAAAA', size='sm', flex=2), TextComponent(text=customer.get('name', '-'), wrap=True, color='#666666', size='sm', flex=5)]),
                BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='นัดหมาย:', color='#AAAAAA', size='sm', flex=2), TextComponent(text=dates.get('due_formatted', '-'), wrap=True, color='#666666', size='sm', flex=5)])
            ]),
        ]),
        footer=BoxComponent(layout='vertical', spacing='sm', contents=[
            ButtonComponent(style='primary', height='sm', action=URIAction(label='📝 เปิดในเว็บ', uri=url_for('task_details', task_id=task['id'], _external=True)))
        ])
    )

def create_full_summary_message(title, tasks):
    if not tasks: return TextSendMessage(text=f"ไม่พบรายการ{title}ในขณะนี้")
    tasks.sort(key=lambda x: date_parse(x.get('due')) if x.get('due') else date_parse(x.get('created', '9999-12-31T23:59:59Z')))
    lines = [f"📋 {title} (ทั้งหมด {len(tasks)} งาน)\n"]
    for i, task in enumerate(tasks):
        customer = parse_customer_info_from_notes(task.get('notes', ''))
        due = parse_google_task_dates(task).get('due_formatted', 'ยังไม่ระบุ')
        line = f"{i+1}. {task.get('title', 'N/A')}"
        if customer.get('name'): line += f"\n   - 👤 {customer.get('name')}"
        line += f"\n   - 🗓️ {due}"
        lines.append(line)
    message = "\n\n".join(lines)
    if len(message) > 4900: message = message[:4900] + "\n\n... (ข้อความยาวเกินไป)"
    return TextSendMessage(text=message)

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    text = event.message.text.strip().lower()
    reply = None
    if text == 'งานวันนี้':
        tasks = [t for t in (gs.get_google_tasks_for_report(show_completed=False) or []) if t.get('due') and date_parse(t['due']).astimezone(THAILAND_TZ).date() == datetime.datetime.now(THAILAND_TZ).date() and t.get('status') == 'needsAction']
        if not tasks: return line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ไม่พบงานสำหรับวันนี้"))
        tasks.sort(key=lambda x: date_parse(x['due']))
        messages = []
        for task in tasks[:5]:
            customer, dates = parse_customer_info_from_notes(task.get('notes', '')), parse_google_task_dates(task)
            loc = f"พิกัด: {customer.get('map_url')}" if customer.get('map_url') else "พิกัด: - (ไม่มีข้อมูล)"
            msg_text = f"🔔 งานสำหรับวันนี้\n\nชื่องาน: {task.get('title', '-')}\n👤 ลูกค้า: {customer.get('name', '-')}\n📞 โทร: {customer.get('phone', '-')}\n🗓️ นัดหมาย: {dates.get('due_formatted', '-')}\n📍 {loc}\n\n🔗 ดูรายละเอียด/แก้ไข:\n{url_for('task_details', task_id=task.get('id'), _external=True)}"
            messages.append(TextSendMessage(text=msg_text))
        return line_bot_api.reply_message(event.reply_token, messages)
    elif text == 'งานค้าง':
        tasks = [t for t in (gs.get_google_tasks_for_report(show_completed=False) or []) if t.get('status') == 'needsAction']
        reply = create_full_summary_message('รายการงานค้าง', tasks)
    elif text == 'งานเสร็จ':
        tasks = sorted([t for t in (gs.get_google_tasks_for_report(show_completed=True) or []) if t.get('status') == 'completed'], key=lambda x: date_parse(x.get('completed', '0001-01-01T00:00:00Z')), reverse=True)
        reply = create_task_list_message('รายการงานเสร็จล่าสุด', tasks)
    elif text == 'งานพรุ่งนี้':
        tasks = [t for t in (gs.get_google_tasks_for_report(show_completed=False) or []) if t.get('due') and date_parse(t['due']).astimezone(THAILAND_TZ).date() == (datetime.datetime.now(THAILAND_TZ) + timedelta(days=1)).date() and t.get('status') == 'needsAction']
        reply = create_task_list_message('งานพรุ่งนี้', tasks)
    elif text == 'สร้างงานใหม่' and LIFF_ID_FORM:
        reply = TextSendMessage(text="เปิดฟอร์มเพื่อสร้างงานใหม่ครับ 👇", quick_reply=QuickReply(items=[QuickReplyButton(action=URIAction(label="เปิดฟอร์มสร้างงาน", uri=f"https://liff.line.me/{LIFF_ID_FORM}"))]))
    elif text.startswith('ดูงาน '):
        query = event.message.text.split(maxsplit=1)[1].strip().lower()
        if not query: return line_bot_api.reply_message(event.reply_token, TextSendMessage(text="โปรดระบุชื่อลูกค้าที่ต้องการค้นหา"))
        tasks = [t for t in (gs.get_google_tasks_for_report(show_completed=True) or []) if query in parse_customer_info_from_notes(t.get('notes', '')).get('name', '').lower()]
        if not tasks: return line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"ไม่พบงานของลูกค้า: {query}"))
        tasks.sort(key=lambda x: (x.get('status') == 'completed', date_parse(x.get('due', '9999-12-31T23:59:59Z'))))
        bubbles = [create_task_flex_message(t) for t in tasks[:10]]
        return line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"ผลการค้นหา: {query}", contents=CarouselContainer(contents=bubbles)))
    elif text == 'comphone':
        help_text = ("พิมพ์คำสั่งเพื่อดูรายงานหรือจัดการงาน:\n- *งานค้าง*: ดูรายการงานที่ยังไม่เสร็จทั้งหมด\n- *งานเสร็จ*: ดูรายการงานที่ทำเสร็จแล้ว 5 รายการล่าสุด\n- *งานวันนี้*: ดูงานที่นัดหมายสำหรับวันนี้ (แยกข้อความ)\n- *งานพรุ่งนี้*: ดูสรุปงานที่นัดหมายสำหรับพรุ่งนี้\n- *สร้างงานใหม่*: เปิดฟอร์มสำหรับสร้างงานใหม่\n- *ดูงาน [ชื่อลูกค้า]*: ค้นหางานตามชื่อลูกค้า\n\n"
                     f"ดูข้อมูลทั้งหมด: {url_for('summary', _external=True)}")
        reply = TextSendMessage(text=help_text)
    
    if reply: line_bot_api.reply_message(event.reply_token, reply)

@handler.add(PostbackEvent)
def handle_postback(event):
    data = dict(x.split('=') for x in event.postback.data.split('&'))
    action, task_id = data.get('action'), data.get('task_id')

    if action == 'customer_feedback':
        task = gs.get_single_task(task_id)
        if not task: return
        notes = task.get('notes', '')
        feedback = parse_customer_feedback_from_notes(notes)
        feedback.update({'feedback_date': datetime.datetime.now(THAILAND_TZ).isoformat(), 'feedback_type': data.get('feedback'), 'customer_line_user_id': event.source.user_id})
        history_reports, base = parse_tech_report_from_notes(notes)
        reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history_reports])
        final_notes = f"{base.strip()}"
        if reports_text: final_notes += reports_text
        final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        gs.update_google_task(task_id, notes=final_notes)
        cache.clear()
        try: line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ขอบคุณสำหรับคำยืนยันครับ/ค่ะ 🙏"))
        except Exception: pass

#</editor-fold>

#<editor-fold desc="Admin and Tool Routes">

@app.route('/tools/organize_files')
def organize_files():
    flash('ฟังก์ชัน "จัดระเบียบไฟล์" ยังไม่พร้อมใช้งานค่ะ', 'info')
    return redirect(url_for('settings_page'))

@app.route('/tools/manage_duplicates')
def manage_duplicates():
    flash('ฟังก์ชัน "จัดการข้อมูลงานซ้ำ" ยังไม่พร้อมใช้งานค่ะ', 'info')
    return redirect(url_for('settings_page'))

@app.route('/test_notification', methods=['POST'])
def test_notification():
    recipient_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
    if recipient_id:
        try:
            line_bot_api.push_message(recipient_id, TextSendMessage(text="[ทดสอบ] นี่คือข้อความทดสอบจากระบบ"))
            flash(f'ส่งข้อความทดสอบไปที่ ID: {recipient_id} สำเร็จ!', 'success')
        except Exception as e: flash(f'เกิดข้อผิดพลาดในการส่ง: {e}', 'danger')
    else: flash('กรุณากำหนด "LINE Admin Group ID" ก่อน', 'danger')
    return redirect(url_for('settings_page'))

@app.route('/backup_data')
def backup_data():
    memory_file, filename = _create_backup_zip()
    if memory_file and filename:
        return Response(memory_file.getvalue(), mimetype='application/zip', headers={'Content-Disposition': f'attachment;filename={filename}'})
    else:
        flash('เกิดข้อผิดพลาดในการสร้างไฟล์สำรองข้อมูล', 'danger')
        return redirect(url_for('settings_page'))

@app.route('/trigger_auto_backup_now', methods=['POST'])
def trigger_auto_backup_now():
    if scheduled_backup_job(): flash('สำรองข้อมูลไปที่ Google Drive สำเร็จ!', 'success')
    else: flash('เกิดข้อผิดพลาดในการสำรองข้อมูลไปที่ Google Drive!', 'danger')
    return redirect(url_for('settings_page'))

#</editor-fold>

#<editor-fold desc="Google OAuth Routes">

@app.route('/authorize')
def authorize():
    client_secrets_json_str = os.environ.get('GOOGLE_CLIENT_SECRETS_JSON')
    if not client_secrets_json_str:
        flash('ไม่สามารถเริ่มการเชื่อมต่อได้: ไม่ได้ตั้งค่า `GOOGLE_CLIENT_SECRETS_JSON` บน Server', 'danger')
        app.logger.error("`GOOGLE_CLIENT_SECRETS_JSON` environment variable not found.")
        return redirect(url_for('settings_page'))
        
    try: client_config = json.loads(client_secrets_json_str)
    except json.JSONDecodeError:
        flash('เกิดข้อผิดพลาดในการอ่านข้อมูล `GOOGLE_CLIENT_SECRETS_JSON`', 'danger')
        app.logger.error("Failed to parse `GOOGLE_CLIENT_SECRETS_JSON`.")
        return redirect(url_for('settings_page'))

    flow = Flow.from_client_config(
        client_config, scopes=gs.SCOPES,
        redirect_uri=url_for('oauth2callback', _external=True)
    )
    authorization_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true', prompt='consent')
    session['state'] = state
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    state = session.get('state')
    if not state or state != request.args.get('state'):
        app.logger.error("State mismatch in OAuth callback. Possible CSRF attack.")
        abort(401) 

    client_secrets_json_str = os.environ.get('GOOGLE_CLIENT_SECRETS_JSON')
    if not client_secrets_json_str:
        app.logger.error("Server configuration error: Client secrets not found during callback.")
        return "Server configuration error: Client secrets not found.", 500
        
    client_config = json.loads(client_secrets_json_str)
    flow = Flow.from_client_config(
        client_config, scopes=gs.SCOPES, state=state,
        redirect_uri=url_for('oauth2callback', _external=True)
    )
    flow.fetch_token(authorization_response=request.url)
    credentials = flow.credentials
    token_json = credentials.to_json()

    app.logger.info("="*80)
    app.logger.info("!!! NEW GOOGLE TOKEN GENERATED SUCCESSFULLY !!!")
    app.logger.info("COPY THE JSON BELOW AND SET IT AS THE 'GOOGLE_TOKEN_JSON' ENVIRONMENT VARIABLE IN RENDER:")
    app.logger.info(token_json)
    app.logger.info("="*80)
    
    os.environ['GOOGLE_TOKEN_JSON'] = token_json
    gs.get_refreshed_credentials(force_refresh=True) # Force immediate refresh with the new token
    
    flash('เชื่อมต่อกับ Google API สำเร็จแล้ว! กรุณาตรวจสอบ Log ของระบบเพื่อคัดลอก Token ใหม่ไปตั้งค่า และทำการรีสตาร์ทแอปพลิเคชัน', 'success')
    return redirect(url_for('settings_page'))

#</editor-fold>

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() in ['true', '1', 't']
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
