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
import sqlite3
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

# NEW: Import the consolidated Google services module
import google_services as gs

import pandas as pd
from dateutil.parser import parse as date_parse

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit

# ==============================================================================
# --- CONSTANTS AND CONFIGURATIONS ---
# ==============================================================================

# --- Flask App Initialization ---
app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_development_only')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
csrf = CSRFProtect(app)

# --- General Settings ---
THAILAND_TZ = pytz.timezone('Asia/Bangkok')
SETTINGS_FILE = 'settings.json'
cache = TTLCache(maxsize=100, ttl=60)

# --- File Upload Settings ---
MAX_FILE_SIZE_MB = 100
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# --- Google API Settings ---
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')
DRIVE_BASE_ATTACHMENT_FOLDER_NAME = "Task_Attachments"
DRIVE_AVATار_FOLDER_NAME = "Technician_Avatars"
DRIVE_SETTINGS_BACKUP_FOLDER_NAME = "Settings_Backups"
DRIVE_SYSTEM_BACKUP_FOLDER_NAME = "System_Backups"

# --- LINE Bot Settings ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM')
LINE_LOGIN_CHANNEL_ID = os.environ.get('LINE_LOGIN_CHANNEL_ID')


# --- Job Type Settings ---
DEFAULT_JOB_TYPE = 'เซอร์วิส'
JOB_TYPES = {
    'all': 'ทั้งหมด',
    'เซอร์วิส': 'เซอร์วิส',
    'หน้าร้าน': 'หน้าร้าน',
    'ส่งซ่อม': 'ส่งซ่อม'
}

# --- Text Snippets for Autocomplete ---
TEXT_SNIPPETS = {
    'task_details': [{'key': 'ตรวจเช็ค', 'value': 'เข้าตรวจเช็คอาการเสียเบื้องต้นตามที่ลูกค้าแจ้ง'}],
    'progress_reports': [{'key': 'รออะไหล่', 'value': 'ตรวจสอบแล้วพบว่าต้องรออะไหล่'}]
}

# --- Global Variables ---
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)
_APP_SETTINGS_STORE = {}

# --- Settings Management ---
SETTINGS_FILE = 'settings.json'
_APP_SETTINGS_STORE = {} # In-memory cache for settings
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
    'technician_list': []
}

# ==============================================================================
# --- GOOGLE API FUNCTION WRAPPERS (FOR CACHING) ---
# ==============================================================================

@cached(cache)
def find_or_create_drive_folder(name, parent_id):
    """Cached wrapper for the gs.find_or_create_drive_folder function."""
    return gs.find_or_create_drive_folder(name, parent_id)

@cached(cache)
def get_google_tasks_for_report(show_completed=True, max_results=100):
    """Cached wrapper for gs.get_google_tasks_for_report."""
    return gs.get_google_tasks_for_report(show_completed=show_completed, max_results=max_results)

@cached(cache)
def get_customer_database():
    """Builds a customer database from Google Tasks. Cached for performance."""
    app.logger.info("Building customer database from Google Tasks...")
    all_tasks = get_google_tasks_for_report(show_completed=True, max_results=500)
    if not all_tasks:
        return []

    customers_dict = {}
    all_tasks.sort(key=lambda x: x.get('created', '0'), reverse=True)

    for task in all_tasks:
        notes = task.get('notes', '')
        if not notes:
            continue
        
        _, base_notes = parse_tech_report_from_notes(notes)
        customer_info = parse_customer_info_from_notes(base_notes)

        name = customer_info.get('name', '').strip()
        phone = customer_info.get('phone', '').strip()

        if not name:
            continue

        customer_key = (name.lower(), phone)
        
        if customer_key not in customers_dict:
            customers_dict[customer_key] = {
                'name': name,
                'phone': phone,
                'organization': customer_info.get('organization', '').strip(),
                'address': customer_info.get('address', '').strip(),
                'map_url': customer_info.get('map_url', '')
            }
    
    app.logger.info(f"Customer database built with {len(customers_dict)} unique customers.")
    return list(customers_dict.values())


# ==============================================================================
# --- APPLICATION LOGIC ---
# ==============================================================================

def notify_admin_error(message):
    """Sends a critical error notification to the admin LINE group."""
    try:
        admin_group_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
        if admin_group_id:
            line_bot_api.push_message(admin_group_id, TextSendMessage(text=f"‼️ เกิดข้อผิดพลาดร้ายแรงในระบบ ‼️\n\n{message[:900]}"))
    except Exception as e:
        app.logger.error(f"Failed to send critical error notification: {e}")

def scheduled_token_refresh_job():
    """Proactively refreshes the Google API token to keep it valid."""
    with app.app_context():
        app.logger.info("--- Running scheduled Google token refresh job ---")
        gs.get_refreshed_credentials(force_refresh=True)

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
    global _APP_SETTINGS_STORE
    if not _APP_SETTINGS_STORE:
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
        _APP_SETTINGS_STORE = app_settings
    return _APP_SETTINGS_STORE

def save_app_settings(settings_data):
    global _APP_SETTINGS_STORE
    current_settings = get_app_settings().copy()
    for key, value in settings_data.items():
        if isinstance(value, dict) and key in current_settings and isinstance(current_settings[key], dict):
            current_settings[key].update(value)
        else:
            current_settings[key] = value
    if save_settings_to_file(current_settings):
        _APP_SETTINGS_STORE = current_settings
        return True
    return False

def sanitize_filename(name):
    if not name:
        return "Unnamed"
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

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
            info['map_url'] = f"https://www.google.com/maps/search/?api=1&query={coords_or_url}" 
        else:
            info['map_url'] = coords_or_url
    
    return info

def parse_customer_feedback_from_notes(notes):
    feedback_data = {}
    if not notes: return feedback_data

    feedback_match = re.search(r"--- CUSTOMER_FEEDBACK_START ---\s*\n(.*?)\n--- CUSTOMER_FEEDBACK_END ---", notes, re.DOTALL)
    if feedback_match:
        try:
            feedback_data = json.loads(feedback_match.group(1))
        except json.JSONDecodeError:
            app.logger.warning("Failed to decode customer feedback JSON from notes.")
    return feedback_data

def parse_google_task_dates(task_item):
    parsed = task_item.copy()
    for key in ['created', 'due', 'completed']:
        if parsed.get(key):
            try:
                dt_utc = date_parse(parsed[key])
                parsed[f'{key}_formatted'] = dt_utc.astimezone(THAILAND_TZ).strftime("%d/%m/%y %H:%M")
                if key == 'due':
                    parsed['due_for_input'] = dt_utc.astimezone(THAILAND_TZ).strftime("%Y-%m-%dT%H:%M")
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
            
            if 'attachments' not in report_data and 'attachment_urls' in report_data and isinstance(report_data['attachment_urls'], list):
                report_data['attachments'] = []
                for url in report_data['attachment_urls']:
                    if isinstance(url, str):
                        match = re.search(r'/d/([a-zA-Z0-9_-]+)', url)
                        file_id = match.group(1) if match else None
                        report_data['attachments'].append({'id': file_id, 'url': url, 'name': 'ไฟล์แนบเก่า'})
                report_data.pop('attachment_urls', None)
            
            if isinstance(report_data.get('equipment_used'), str):
                report_data['equipment_used_display'] = report_data['equipment_used'].replace('\n', '<br>')
            else:
                report_data['equipment_used_display'] = _format_equipment_list(report_data.get('equipment_used', []))
            
            if 'type' not in report_data:
                report_data['type'] = 'report'

            history.append(report_data)
        except json.JSONDecodeError:
            app.logger.warning(f"Failed to decode tech report JSON: {json_str[:100]}...")
    
    temp_notes = notes
    temp_notes = re.sub(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", "", temp_notes, flags=re.DOTALL)
    temp_notes = re.sub(r"--- CUSTOMER_FEEDBACK_START ---.*?--- CUSTOMER_FEEDBACK_END ---", "", temp_notes, flags=re.DOTALL)
    original_notes_text = temp_notes.strip()

    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, original_notes_text

def allowed_file(filename):
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'kmz', 'kml', 'mp4', 'mov', 'doc', 'docx', 'xls', 'xlsx'}
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
                    if isinstance(item['quantity'], (int, float)):
                        line += f" (x{item['quantity']:g})"
                    else:
                        line += f" ({item['quantity']})"
                lines.append(line)
            elif isinstance(item, str):
                lines.append(item)
    return "<br>".join(lines) if lines else 'N/A'

def parse_job_type_from_title(title):
    match = re.match(r'\[(.*?)\](.*)', title)
    if match:
        job_type = match.group(1).strip()
        clean_title = match.group(2).strip()
        if job_type in JOB_TYPES.values():
            return job_type, clean_title
    return DEFAULT_JOB_TYPE, title

def get_file_icon(filename):
    if not filename or '.' not in filename: return 'fas fa-file'
    ext = filename.rsplit('.', 1)[1].lower()
    icon_map = {
        ('jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp'): 'fas fa-file-image text-primary',
        ('mp4', 'mov', 'avi', 'mkv'): 'fas fa-file-video text-info',
        ('pdf',): 'fas fa-file-pdf text-danger',
        ('doc', 'docx'): 'fas fa-file-word text-primary',
        ('xls', 'xlsx'): 'fas fa-file-excel text-success',
        ('zip', 'rar', '7z'): 'fas fa-file-archive text-warning',
        ('kmz', 'kml'): 'fas fa-globe-americas text-success'
    }
    for exts, icon_class in icon_map.items():
        if ext in exts:
            return icon_class
    return 'fas fa-file-alt text-secondary'

def _create_backup_zip():
    memory_file = BytesIO()
    timestamp = datetime.datetime.now(THAILAND_TZ).strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{timestamp}.zip"

    all_tasks = get_google_tasks_for_report(show_completed=True, max_results=1000) or []
    settings_data = get_app_settings()

    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        tasks_json = json.dumps(all_tasks, ensure_ascii=False, indent=4)
        zf.writestr('tasks_backup.json', tasks_json)
        
        settings_json = json.dumps(settings_data, ensure_ascii=False, indent=4)
        zf.writestr('settings_backup.json', settings_json)

    memory_file.seek(0)
    return memory_file, filename

def generate_qr_code_base64(data_to_encode):
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(data_to_encode)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{img_str}"

@app.context_processor
def inject_global_template_vars():
    try:
        creds = gs.get_refreshed_credentials()
        api_connected = creds and creds.valid
    except Exception:
        api_connected = False
        
    return {
        'now': datetime.datetime.now(THAILAND_TZ),
        'thaizone': THAILAND_TZ,
        'get_file_icon': get_file_icon,
        'job_types': JOB_TYPES,
        'LIFF_ID_FORM': LIFF_ID_FORM,
        'dateutil_parse': date_parse,
        'google_api_connected': api_connected
    }

#</editor-fold>

#<editor-fold desc="Scheduled Jobs and Notifications">

def send_new_task_notification(task):
    settings = get_app_settings()
    recipients = settings.get('line_recipients', {})
    admin_group_id = recipients.get('admin_group_id')
    
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
    
    if is_today:
        title = "🗓️ อัปเดตงานวันนี้"
        reason_str = f"รายละเอียด: {reason}\n" if reason else ""
    else:
        title = "🗓️ เลื่อนนัดหมาย"
        reason_str = f"เหตุผล: {reason}\n" if reason else ""

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

        system_backup_folder_id = find_or_create_drive_folder("System_Backups", GOOGLE_DRIVE_FOLDER_ID)
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

        tasks_raw = get_google_tasks_for_report(show_completed=False) or []
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
    problem_action = URIAction(
        label='🚨 ยังมีปัญหาอยู่',
        uri=f"https://liff.line.me/{LIFF_ID_FORM}/customer_problem_form?task_id={task_id}"
    )

    return BubbleContainer(
        body=BoxComponent(
            layout='vertical', spacing='md',
            contents=[
                TextComponent(text="สอบถามหลังการซ่อม", weight='bold', size='lg', color='#1DB446', align='center'),
                SeparatorComponent(margin='md'),
                TextComponent(text=f"เรียนคุณ {customer_name},", size='sm', wrap=True),
                TextComponent(text=f"เกี่ยวกับงาน: {task_title}", size='sm', wrap=True, color='#666666'),
                SeparatorComponent(margin='lg'),
                TextComponent(text="ไม่ทราบว่าหลังจากทีมงานของเราเข้าบริการแล้ว ทุกอย่างเรียบร้อยดีหรือไม่ครับ/คะ?", size='md', wrap=True, align='center'),
                BoxComponent(layout='vertical', spacing='sm', margin='md', contents=[
                    ButtonComponent(
                        style='primary', height='sm', color='#28a745',
                        action=PostbackAction(
                            label='✅ งานเรียบร้อยดี', data=f'action=customer_feedback&task_id={task_id}&feedback=ok',
                            display_text='ขอบคุณสำหรับคำยืนยันครับ/ค่ะ!'
                        )
                    ),
                    ButtonComponent(
                        style='secondary', height='sm', color='#dc3545',
                        action=problem_action
                    )
                ]),
            ]
        )
    )

def scheduled_customer_follow_up_job():
    with app.app_context():
        app.logger.info("Running scheduled customer follow-up job...")
        settings = get_app_settings()
        admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')

        tasks_raw = get_google_tasks_for_report(show_completed=True) or []
        now_utc = datetime.datetime.now(pytz.utc)
        two_days_ago_utc = now_utc - datetime.timedelta(days=2)
        one_day_ago_utc = now_utc - datetime.timedelta(days=1)

        for task in tasks_raw:
            if task.get('status') == 'completed' and task.get('completed'):
                try:
                    completed_dt_utc = date_parse(task['completed'])

                    if two_days_ago_utc <= completed_dt_utc < one_day_ago_utc:
                        notes = task.get('notes', '')
                        feedback_data = parse_customer_feedback_from_notes(notes)

                        if 'follow_up_sent_date' in feedback_data:
                            continue

                        customer_info = parse_customer_info_from_notes(notes)
                        customer_line_id = feedback_data.get('customer_line_user_id')
                        
                        if not customer_line_id:
                            continue

                        flex_content = _create_customer_follow_up_flex_message(
                            task['id'], task['title'], customer_info.get('name', 'N/A'))
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
    """Initializes and runs the APScheduler jobs."""
    global scheduler
    settings = get_app_settings()
    if scheduler.running:
        scheduler.shutdown(wait=False)
    
    scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)

    # Proactive token refresh job
    scheduler.add_job(scheduled_token_refresh_job, 'interval', minutes=30, id='google_token_refresh', replace_existing=True)
    app.logger.info("Scheduled Google token refresh job to run every 30 minutes.")

    # Existing application jobs
    ab = settings.get('auto_backup', {})
    if ab.get('enabled'):
        scheduler.add_job(scheduled_backup_job, CronTrigger(hour=ab.get('hour_thai', 2), minute=ab.get('minute_thai', 0)), id='auto_system_backup', replace_existing=True)
    
    rt = settings.get('report_times', {})
    scheduler.add_job(scheduled_appointment_reminder_job, CronTrigger(hour=rt.get('appointment_reminder_hour_thai', 7)), id='daily_appointment_reminder', replace_existing=True)
    scheduler.add_job(scheduled_customer_follow_up_job, CronTrigger(hour=rt.get('customer_followup_hour_thai', 9)), id='daily_customer_followup', replace_existing=True)
    
    scheduler.start()
    app.logger.info("APScheduler started/reconfigured with all jobs.")

def cleanup_scheduler():
    """A clean shutdown function to be called upon application exit."""
    if scheduler is not None and scheduler.running:
        app.logger.info("Scheduler is running, shutting it down.")
        scheduler.shutdown(wait=False)
    else:
        app.logger.info("Scheduler not running or not initialized, skipping shutdown.")
#</editor-fold>

# --- Initial app setup calls ---
with app.app_context():
    # Pass the local save function to the modular startup function
    gs.load_settings_from_drive_on_startup(save_settings_to_file)
    run_scheduler()

atexit.register(cleanup_scheduler)

# --- Error Handlers ---
@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    app.logger.error(f"Server Error: {e}", exc_info=True)
    notify_admin_error(f"Internal Server Error: {e}")
    return render_template('500.html'), 500


# --- Flask Routes ---
@app.route('/api/customers')
def api_customers():
    customer_list = get_customer_database()
    return jsonify(customer_list)

@app.route("/")
def root_redirect():
    return redirect(url_for('summary'))

@app.route("/form", methods=['GET', 'POST'])
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
            f"ลูกค้า: {customer_name}",
            f"เบอร์โทรศัพท์: {str(request.form.get('phone', '')).strip()}",
            f"ที่อยู่: {str(request.form.get('address', '')).strip()}",
        ])
        map_url = str(request.form.get('latitude_longitude', '')).strip()
        if map_url: notes_lines.append(map_url)

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

            uploaded_attachments_json = request.form.get('uploaded_attachments_json')
            if uploaded_attachments_json:
                try:
                    uploaded_attachments = json.loads(uploaded_attachments_json)
                    if uploaded_attachments:
                        task_id = new_task['id']
                        attachments_base_folder_id = find_or_create_drive_folder(DRIVE_BASE_ATTACHMENT_FOLDER_NAME, GOOGLE_DRIVE_FOLDER_ID)
                        monthly_folder_name = datetime.datetime.now(THAILAND_TZ).strftime('%Y-%m')
                        monthly_folder_id = find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)
                        sanitized_customer_name = sanitize_filename(customer_name)
                        customer_task_folder_name = f"{sanitized_customer_name} - {task_id}"
                        final_upload_folder_id = find_or_create_drive_folder(customer_task_folder_name, monthly_folder_id)

                        drive_service = gs.get_google_drive_service()
                        if final_upload_folder_id and drive_service:
                            for att in uploaded_attachments:
                                try:
                                    file_meta = gs._execute_google_api_call_with_retry(drive_service.files().get, fileId=att['id'], fields='parents')
                                    previous_parents = ",".join(file_meta.get('parents', []))
                                    gs._execute_google_api_call_with_retry(
                                        drive_service.files().update,
                                        fileId=att['id'],
                                        addParents=final_upload_folder_id,
                                        removeParents=previous_parents,
                                        fields='id, parents'
                                    )
                                    app.logger.info(f"Moved attachment {att['id']} to final folder {final_upload_folder_id}")
                                except Exception as e:
                                    app.logger.error(f"Could not move attachment {att['id']} to final folder: {e}")

                        initial_report = {
                            'type': 'report', 'summary_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
                            'work_summary': 'ไฟล์แนบจากการสร้างงานครั้งแรก', 'attachments': uploaded_attachments,
                            'technicians': ['System']
                        }
                        report_text = f"\n\n--- TECH_REPORT_START ---\n{json.dumps(initial_report, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"
                        updated_notes = new_task.get('notes', '') + report_text
                        gs.update_google_task(task_id, notes=updated_notes)
                        cache.clear()

                except (json.JSONDecodeError, KeyError) as e:
                    app.logger.warning(f"Could not process initial attachments on form submission: {e}")

            flash('สร้างงานใหม่เรียบร้อยแล้ว!', 'success')
            return redirect(url_for('task_details', task_id=new_task['id']))
        else:
            flash('เกิดข้อผิดพลาดในการสร้างงาน', 'danger')
            return render_template('form.html', form_data=request.form)

    return render_template('form.html',
                           task_detail_snippets=TEXT_SNIPPETS.get('task_details', [])
                           )

@app.route('/api/upload_attachment', methods=['POST'])
def api_upload_attachment():
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file'}), 400
    
    task_id = request.form.get('task_id')
    if not task_id:
        return jsonify({'status': 'error', 'message': 'Task ID is missing'}), 400

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
        else:
            return jsonify({'status': 'error', 'message': f'ไฟล์ใหญ่เกินขนาดที่กำหนด ({MAX_FILE_SIZE_MB}MB)'}), 413
    else:
        file_to_upload = file
        filename = secure_filename(file.filename)
        mime_type = file.mimetype or mimetypes.guess_type(filename)[0]

    attachments_base_folder_id = find_or_create_drive_folder(DRIVE_BASE_ATTACHMENT_FOLDER_NAME, GOOGLE_DRIVE_FOLDER_ID)
    if not attachments_base_folder_id:
        return jsonify({'status': 'error', 'message': 'Could not create or find base Task_Attachments folder'}), 500

    final_upload_folder_id = None
    target_date = datetime.datetime.now(THAILAND_TZ)

    if task_id == 'new_task_placeholder':
        monthly_folder_name = target_date.strftime('%Y-%m')
        monthly_folder_id = find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)
        temp_upload_folder_name = f"New_Uploads_{target_date.strftime('%Y-%m-%d')}"
        final_upload_folder_id = find_or_create_drive_folder(temp_upload_folder_name, monthly_folder_id)
    else:
        task_raw = gs.get_single_task(task_id)
        if not task_raw:
            return jsonify({'status': 'error', 'message': 'Task not found'}), 404
        
        if task_raw.get('created'):
            try: target_date = date_parse(task_raw.get('created')).astimezone(THAILAND_TZ)
            except (ValueError, TypeError): pass
        
        monthly_folder_name = target_date.strftime('%Y-%m')
        monthly_folder_id = find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)
        if not monthly_folder_id:
            return jsonify({'status': 'error', 'message': f'Could not create or find monthly folder: {monthly_folder_name}'}), 500
        
        _, base_notes_text = parse_tech_report_from_notes(task_raw.get('notes', ''))
        customer_info = parse_customer_info_from_notes(base_notes_text)
        sanitized_customer_name = sanitize_filename(customer_info.get('name', 'Unknown_Customer'))
        customer_task_folder_name = f"{sanitized_customer_name} - {task_id}"
        final_upload_folder_id = find_or_create_drive_folder(customer_task_folder_name, monthly_folder_id)
    
    if not final_upload_folder_id:
        return jsonify({'status': 'error', 'message': 'Could not determine final upload folder'}), 500

    drive_file = gs.upload_data_from_memory_to_drive(file_to_upload, filename, mime_type, final_upload_folder_id)
    
    if drive_file:
        return jsonify({'status': 'success', 'file_info': {'id': drive_file.get('id'), 'url': drive_file.get('webViewLink'), 'name': filename}})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to upload to Google Drive'}), 500

@app.route('/summary')
def summary():
    # Use the cached wrapper function
    tasks_raw = get_google_tasks_for_report(show_completed=True, max_results=250) or []
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    job_type_filter = str(request.args.get('job_type_filter', 'all')).strip()

    today_thai = datetime.datetime.now(THAILAND_TZ).date()
    final_tasks = []
    stats = {'needsAction': 0, 'completed': 0, 'overdue': 0, 'total': len(tasks_raw), 'today': 0}

    for task in tasks_raw:
        job_type, clean_title = parse_job_type_from_title(task.get('title', ''))
        task['job_type'] = job_type
        task['clean_title'] = clean_title
        
        if job_type_filter != 'all' and job_type != job_type_filter:
            continue

        task_status = task.get('status', 'needsAction')
        is_overdue = False
        is_today = False
        if task_status == 'needsAction' and task.get('due'):
            try:
                due_dt_local = date_parse(task['due']).astimezone(THAILAND_TZ)
                if due_dt_local.date() < today_thai: is_overdue = True
                elif due_dt_local.date() == today_thai: is_today = True
            except (ValueError, TypeError): pass

        if task_status == 'completed': stats['completed'] += 1
        else:
            stats['needsAction'] += 1
            if is_overdue: stats['overdue'] += 1
            if is_today: stats['today'] += 1

        task_passes_status_filter = (status_filter == 'all' or
                                     status_filter == task_status or
                                     (status_filter == 'today' and is_today))

        if task_passes_status_filter:
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            searchable_text = f"{task.get('title', '')} {customer_info.get('name', '')} {customer_info.get('organization', '')} {customer_info.get('phone', '')}".lower()

            if not search_query or search_query in searchable_text:
                parsed_task = parse_google_task_dates(task)
                parsed_task['customer'] = customer_info
                parsed_task['is_overdue'] = is_overdue
                parsed_task['is_today'] = is_today
                final_tasks.append(parsed_task)

    final_tasks.sort(key=lambda x: (x.get('status') == 'completed', x.get('due') is None, date_parse(x.get('due', '9999-12-31T23:59:59Z'))))

    completed_tasks_for_chart = [t for t in tasks_raw if t.get('status') == 'completed' and t.get('completed')]
    month_labels, chart_values = [], []
    for i in range(12):
        target_d = datetime.datetime.now(THAILAND_TZ) - datetime.timedelta(days=30 * (11 - i))
        month_key = target_d.strftime('%Y-%m')
        month_labels.append(target_d.strftime('%b %y'))
        count = sum(1 for t in completed_tasks_for_chart if date_parse(t['completed']).astimezone(THAILAND_TZ).strftime('%Y-%m') == month_key)
        chart_values.append(count)
    chart_data = {'labels': month_labels, 'values': chart_values}

    return render_template("dashboard.html",
                           tasks=final_tasks, summary=stats,
                           search_query=search_query, status_filter=status_filter,
                           chart_data=chart_data,
                           job_type_filter=job_type_filter, job_types=JOB_TYPES)

@app.route('/task/<task_id>', methods=['GET', 'POST'])
def task_details(task_id):
    if request.method == 'POST':
        task_raw = gs.get_single_task(task_id)
        if not task_raw:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'status': 'error', 'message': 'ไม่พบงานที่ต้องการอัปเดต'}), 404
            flash('ไม่พบงานที่ต้องการอัปเดต', 'danger')
            abort(404)
        
        # This is a flag to check if a notification should be sent
        notification_to_send = None
        
        # --- Handle Tech Report Submission ---
        if request.form.get('submit_action') == 'add_report':
            history_reports, base_notes = parse_tech_report_from_notes(task_raw.get('notes', ''))
            
            try:
                new_report = {
                    'type': 'report',
                    'work_summary': request.form.get('work_summary', '').strip(),
                    'summary_date': request.form.get('summary_date', datetime.datetime.now(THAILAND_TZ).isoformat()),
                    'technicians': json.loads(request.form.get('technicians_json', '[]')),
                    'equipment_used': _parse_equipment_string(request.form.get('equipment_used_text', '')),
                    'cost': request.form.get('cost', '0'),
                    'attachments': json.loads(request.form.get('uploaded_attachments_json', '[]'))
                }
            except json.JSONDecodeError:
                return jsonify({'status': 'error', 'message': 'ข้อมูลจากฟอร์มไม่ถูกต้อง (JSON format error)'}), 400

            tech_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history_reports])
            new_report_text = f"\n\n--- TECH_REPORT_START ---\n{json.dumps(new_report, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"
            
            customer_feedback = parse_customer_feedback_from_notes(task_raw.get('notes', ''))
            feedback_text = ""
            if customer_feedback:
                feedback_text = f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(customer_feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

            updated_notes = base_notes.strip() + tech_reports_text + new_report_text + feedback_text
            
            update_payload = {'notes': updated_notes}
            flash_message = "เพิ่มรายงานการทำงานเรียบร้อยแล้ว"

        # --- Handle Status Update ---
        else:
            update_payload = {}
            task_status = request.form.get('task_status')
            if task_status and task_status != task_raw.get('status'):
                update_payload['status'] = task_status
            
            flash_message = "อัปเดตสถานะเรียบร้อยแล้ว"
            
            if task_status == 'completed':
                technicians = json.loads(request.form.get('technicians_json', '[]'))
                notification_to_send = ('completion', technicians)

        if not update_payload:
             return jsonify({'status': 'info', 'message': 'ไม่มีข้อมูลให้อัปเดต'})

        updated_task = gs.update_google_task(task_id, **update_payload)

        if updated_task:
            cache.clear()
            if notification_to_send:
                notif_type = notification_to_send[0]
                if notif_type == 'completion': 
                    send_completion_notification(updated_task, *notification_to_send[1:])
            
            return jsonify({'status': 'success', 'message': flash_message})
        else:
            return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกข้อมูล!'}), 500

    # In the GET request part
    task_raw = gs.get_single_task(task_id)
    if not task_raw: abort(404)
    
    task = parse_google_task_dates(task_raw)
    notes = task.get('notes', '')
    task['customer'] = parse_customer_info_from_notes(notes)
    task['tech_reports_history'], _ = parse_tech_report_from_notes(notes)
    task['customer_feedback'] = parse_customer_feedback_from_notes(notes)
    task['is_overdue'] = False
    task['is_today'] = False
    if task.get('status') == 'needsAction' and task.get('due'):
        try:
            due_dt_utc = date_parse(task['due'])
            due_dt_local = due_dt_utc.astimezone(THAILAND_TZ)
            today_thai = datetime.datetime.now(THAILAND_TZ).date()
            if due_dt_local.date() < today_thai:
                task['is_overdue'] = True
            elif due_dt_local.date() == today_thai:
                task['is_today'] = True
        except (ValueError, TypeError): pass
    
    app_settings = get_app_settings()
    
    all_attachments = []
    for report in task['tech_reports_history']:
        if report.get('attachments'):
            report_date = date_parse(report['summary_date']).astimezone(THAILAND_TZ).strftime("%d/%m/%y %H:%M")
            for att in report['attachments']:
                att_copy = att.copy()
                att_copy['report_date'] = report_date
                all_attachments.append(att_copy)

    return render_template('update_task_details.html',
                           task=task,
                           common_equipment_items=app_settings.get('equipment_catalog', []),
                           technician_list=app_settings.get('technician_list', []),
                           all_attachments=all_attachments,
                           progress_report_snippets=TEXT_SNIPPETS.get('progress_reports', [])
                           )


@app.route('/delete_task/<task_id>', methods=['POST'])
def delete_task(task_id):
    if gs.delete_google_task(task_id):
        flash('ลบงานเรียบร้อยแล้ว!', 'success')
        cache.clear()
    else:
        flash('เกิดข้อผิดพลาดในการลบงาน', 'danger')
    return redirect(url_for('summary'))
    
@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
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
            'technician_list': json.loads(request.form.get('technician_list_json', '[]')),
            'equipment_catalog': json.loads(request.form.get('equipment_catalog_json', '[]'))
        }

        if save_app_settings(settings_data):
            run_scheduler()
            cache.clear()
            if gs.backup_settings_to_drive(settings_data):
                flash('บันทึกและสำรองการตั้งค่าไปที่ Google Drive เรียบร้อยแล้ว!', 'success')
            else:
                flash('บันทึกการตั้งค่าสำเร็จ แต่สำรองไปที่ Google Drive ไม่สำเร็จ!', 'warning')
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกการตั้งค่า!', 'danger')
        return redirect(url_for('settings_page'))

    current_settings = get_app_settings()
    return render_template('settings_page.html', settings=current_settings)

@app.route('/settings/api_status')
def api_status():
    """Renders a page with the status of the Google API connection."""
    connection = {'status': 'error', 'message': 'ไม่สามารถตรวจสอบสถานะได้'}
    token_info = {'expiry_formatted': None, 'is_expired': True}
    
    try:
        creds = gs.get_refreshed_credentials()
        if not creds or not creds.valid:
            connection['message'] = 'ไม่พบ Google credentials ที่ถูกต้อง หรือ token หมดอายุและไม่สามารถรีเฟรชได้ กรุณาสร้าง token ใหม่ผ่านหน้าตั้งค่า'
        else:
            service = gs.get_google_drive_service()
            if service:
                connection['status'] = 'ok'
                connection['message'] = 'เชื่อมต่อ Google API สำเร็จ'
            else:
                connection['message'] = 'Credentials ถูกต้อง แต่การสร้าง Service object ไม่สำเร็จ'

            if creds.expiry:
                expiry_local = creds.expiry.astimezone(THAILAND_TZ)
                token_info['expiry_formatted'] = expiry_local.strftime('%d %B %Y เวลา %H:%M:%S')
                token_info['is_expired'] = datetime.datetime.now(THAILAND_TZ) > expiry_local
            else:
                token_info['expiry_formatted'] = 'ไม่มีข้อมูลวันหมดอายุ (อาจเป็น Service Account)'

    except Exception as e:
        app.logger.error(f"Error checking API status: {e}", exc_info=True)
        connection['message'] = f"เกิดข้อผิดพลาดขณะตรวจสอบ: {e}"
    
    return render_template('api_status.html', connection=connection, token_info=token_info)

# === PLACEHOLDER ROUTES ===

@app.route('/calendar')
def calendar_view():
    """Route สำหรับหน้าปฏิทิน"""
    flash('หน้าปฏิทินยังไม่พร้อมใช้งานค่ะ', 'info')
    return redirect(url_for('summary'))

@app.route('/technician_report')
def technician_report():
    """Route สำหรับหน้ารายงานช่าง"""
    flash('หน้ารายงานช่างยังไม่พร้อมใช้งานค่ะ', 'info')
    return redirect(url_for('summary'))

@app.route('/summary/print')
def summary_print():
    """Route สำหรับหน้าพิมพ์สรุปงาน"""
    flash('หน้าสำหรับพิมพ์ยังไม่พร้อมใช้งานค่ะ', 'info')
    return redirect(url_for('summary'))
    
@app.route('/task/<task_id>/edit')
def edit_task(task_id):
    """Route สำหรับหน้าแก้ไขงาน (คนละส่วนกับหน้ารายละเอียด)"""
    flash('หน้าแก้ไขข้อมูลหลักยังไม่พร้อมใช้งานค่ะ', 'info')
    return redirect(url_for('task_details', task_id=task_id))

@app.route('/authorize')
def authorize():
    """Route สำหรับการเชื่อมต่อ Google API ใหม่"""
    flash('ฟังก์ชันเชื่อมต่อ Google API ใหม่ยังไม่ถูกสร้างขึ้น', 'warning')
    return redirect(url_for('settings_page'))

# === QR CODE ROUTES ===

@app.route('/qr/customer_onboarding/<task_id>')
def generate_customer_onboarding_qr(task_id):
    """สร้างและแสดงหน้า QR Code สำหรับให้ลูกค้าสแกน"""
    task = gs.get_single_task(task_id)
    if not task:
        abort(404)
        
    onboarding_url = url_for('customer_onboarding_form', task_id=task_id, _external=True)
    
    qr_code_b64 = generate_qr_code_base64(onboarding_url)
    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    customer_name = customer_info.get('name', 'ลูกค้า')

    return render_template('display_qr.html', 
                           qr_code_base64=qr_code_b64, 
                           task=task,
                           customer_name=customer_name)

@app.route('/customer_onboarding_form/<task_id>')
def customer_onboarding_form(task_id):
    """หน้านี้คือหน้าที่ลูกค้าจะเห็นหลังจากสแกน QR Code"""
    task = gs.get_single_task(task_id)
    if not task:
        return "<h3><center>ไม่พบข้อมูลงานในระบบ</center></h3>", 404
    
    return f"""
    <div style='font-family: sans-serif; text-align: center; padding: 2rem;'>
        <h2>ติดตามงานซ่อม</h2>
        <p>สำหรับงาน: <strong>{task.get('title')}</strong></p>
        <p>ขอบคุณที่ใช้บริการครับ/ค่ะ</p>
    </div>
    """, 200

@app.route('/qr/public_report/<task_id>')
def generate_public_report_qr(task_id):
    """สร้างและแสดงหน้า QR Code สำหรับหน้ารายงานสาธารณะ"""
    task = gs.get_single_task(task_id)
    if not task:
        abort(404)
    
    report_url = url_for('public_report_view', task_id=task_id, _external=True)
    
    qr_code_b64 = generate_qr_code_base64(report_url)
    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    customer_name = customer_info.get('name', 'ลูกค้า')

    return render_template('display_qr.html', 
                           qr_code_base64=qr_code_b64, 
                           task=task,
                           customer_name=customer_name)

@app.route('/report/public/<task_id>')
def public_report_view(task_id):
    """หน้ารายงานสาธารณะสำหรับให้ลูกค้าดูสถานะและประวัติงาน"""
    task_raw = gs.get_single_task(task_id)
    if not task_raw:
        abort(404)
    
    task = parse_google_task_dates(task_raw)
    notes = task.get('notes', '')
    task['customer'] = parse_customer_info_from_notes(notes)
    task['tech_reports_history'], _ = parse_tech_report_from_notes(notes)

    return render_template('public_report.html', task=task)

# === API ROUTES FOR TASK DETAILS PAGE ===

@app.route('/api/task/<task_id>/delete_report/<int:report_index>', methods=['POST'])
def delete_report(task_id, report_index):
    """API สำหรับลบรายงานย่อย (Tech Report)"""
    task_raw = gs.get_single_task(task_id)
    if not task_raw:
        return jsonify({'status': 'error', 'message': 'ไม่พบงานหลัก'}), 404

    history, base_notes = parse_tech_report_from_notes(task_raw.get('notes', ''))
    
    if 0 <= report_index < len(history):
        # ลบไฟล์แนบใน Drive ที่เกี่ยวข้องกับรายงานนี้
        report_to_delete = history[report_index]
        if report_to_delete.get('attachments'):
            drive_service = gs.get_google_drive_service()
            if drive_service:
                for att in report_to_delete['attachments']:
                    try:
                        drive_service.files().delete(fileId=att['id']).execute()
                        app.logger.info(f"Deleted attachment {att['id']} from Drive for report {report_index}.")
                    except Exception as e:
                        app.logger.error(f"Failed to delete attachment {att['id']} from Drive: {e}")

        # ลบรายงานออกจาก list
        del history[report_index]
        
        # สร้าง notes ใหม่
        tech_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
        customer_feedback = parse_customer_feedback_from_notes(task_raw.get('notes', ''))
        feedback_text = ""
        if customer_feedback:
            feedback_text = f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(customer_feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        
        new_notes = base_notes.strip() + tech_reports_text + feedback_text
        
        # อัปเดต Task
        if gs.update_google_task(task_id, notes=new_notes):
            cache.clear()
            return jsonify({'status': 'success', 'message': 'ลบรายงานเรียบร้อยแล้ว'})
        else:
            return jsonify({'status': 'error', 'message': 'ไม่สามารถอัปเดตข้อมูลงานได้'}), 500
    else:
        return jsonify({'status': 'error', 'message': 'ไม่พบรายงานที่ต้องการลบ'}), 404

@app.route('/api/task/<task_id>/edit_report_text/<int:report_index>', methods=['POST'])
def edit_report_text(task_id, report_index):
    """API สำหรับแก้ไขข้อความสรุปในรายงานย่อย"""
    data = request.get_json()
    if not data or 'summary' not in data:
        return jsonify({'status': 'error', 'message': 'ข้อมูลไม่ถูกต้อง'}), 400

    task_raw = gs.get_single_task(task_id)
    if not task_raw:
        return jsonify({'status': 'error', 'message': 'ไม่พบงานหลัก'}), 404
        
    history, base_notes = parse_tech_report_from_notes(task_raw.get('notes', ''))
    
    if 0 <= report_index < len(history):
        history[report_index]['work_summary'] = data['summary'].strip()
        
        tech_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
        customer_feedback = parse_customer_feedback_from_notes(task_raw.get('notes', ''))
        feedback_text = ""
        if customer_feedback:
            feedback_text = f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(customer_feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

        new_notes = base_notes.strip() + tech_reports_text + feedback_text

        if gs.update_google_task(task_id, notes=new_notes):
            cache.clear()
            return jsonify({'status': 'success', 'message': 'บันทึกข้อความสรุปใหม่เรียบร้อยแล้ว'})
        else:
            return jsonify({'status': 'error', 'message': 'ไม่สามารถอัปเดตข้อมูลงานได้'}), 500
    else:
        return jsonify({'status': 'error', 'message': 'ไม่พบรายงานที่ต้องการแก้ไข'}), 404

@app.route('/task/<task_id>/edit_report/<int:report_index>', methods=['POST'])
def edit_report_attachments(task_id, report_index):
    """API สำหรับแก้ไขไฟล์แนบในรายงานย่อย"""
    task_raw = gs.get_single_task(task_id)
    if not task_raw:
        flash('ไม่พบงานที่ต้องการแก้ไข', 'danger')
        return redirect(url_for('summary'))

    history, base_notes = parse_tech_report_from_notes(task_raw.get('notes', ''))
    if not (0 <= report_index < len(history)):
        flash('ไม่พบรายงานที่ต้องการแก้ไข', 'danger')
        return redirect(url_for('task_details', task_id=task_id))

    report_to_edit = history[report_index]
    
    # 1. Handle attachments to keep
    attachments_to_keep_ids = set(request.form.getlist('attachments_to_keep'))
    current_attachments = report_to_edit.get('attachments', [])
    final_attachments = []

    drive_service = gs.get_google_drive_service()
    for att in current_attachments:
        if att['id'] in attachments_to_keep_ids:
            final_attachments.append(att)
        else:
            # Delete from Drive
            if drive_service:
                try:
                    drive_service.files().delete(fileId=att['id']).execute()
                except Exception as e:
                    app.logger.warning(f"Could not delete file {att['id']} from Drive: {e}")

    # 2. Handle new file uploads
    # (This part is simplified. The JS handles uploads via /api/upload_attachment,
    # so we assume this route is for managing the list of attachments, not uploading)
    # If your JS were to submit files here, you'd add the upload logic.
    
    report_to_edit['attachments'] = final_attachments
    history[report_index] = report_to_edit

    # Rebuild notes and update task
    tech_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
    customer_feedback = parse_customer_feedback_from_notes(task_raw.get('notes', ''))
    feedback_text = ""
    if customer_feedback:
        feedback_text = f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(customer_feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
    
    new_notes = base_notes.strip() + tech_reports_text + feedback_text
    gs.update_google_task(task_id, notes=new_notes)
    cache.clear()
    
    flash('แก้ไขไฟล์แนบในรายงานเรียบร้อยแล้ว', 'success')
    return redirect(url_for('task_details', task_id=task_id))
    
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=True)