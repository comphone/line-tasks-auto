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
from datetime import timezone, date
import time
import tempfile
import uuid
from queue import Queue
import threading

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
    InvalidSignatureError,
    LineBotApiError
)
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage,
    BubbleContainer, CarouselContainer, BoxComponent, TextComponent,
    ButtonComponent, SeparatorComponent, URIAction, PostbackAction, QuickReply, QuickReplyButton,
    ImageComponent, PostbackEvent
)

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload
from google.oauth2 import service_account

import pandas as pd
from dateutil.parser import parse as date_parse

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit

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

app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_development_only')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

csrf = CSRFProtect(app)

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'kmz', 'kml'}
MAX_FILE_SIZE_MB = 50
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024


if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    sys.exit("LINE Bot credentials are not set in environment variables.")

LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM')
LINE_LOGIN_CHANNEL_ID = os.environ.get('LINE_LOGIN_CHANNEL_ID')
GOOGLE_TASKS_LIST_ID = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')

# เพิ่มตัวแปรสภาพแวดล้อมสำหรับ Line Rate Limit
LINE_RATE_LIMIT_PER_MINUTE = int(os.environ.get('LINE_RATE_LIMIT_PER_MINUTE', 100))

if not GOOGLE_DRIVE_FOLDER_ID:
    app.logger.warning("GOOGLE_DRIVE_FOLDER_ID environment variable is not set. Drive upload will not work.")
if not LIFF_ID_FORM:
    app.logger.warning("LIFF_ID_FORM environment variable is not set. LIFF features will not work.")
if not LINE_LOGIN_CHANNEL_ID:
    app.logger.warning("LINE_LOGIN_CHANNEL_ID environment variable is not set. LIFF initialization might fail.")


SCOPES = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/drive']
THAILAND_TZ = pytz.timezone('Asia/Bangkok')
cache = TTLCache(maxsize=100, ttl=60)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# **ย้ายการประกาศ scheduler มาไว้ที่นี่**
scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)


#<editor-fold desc="Helper and Utility Functions">

# ฟังก์ชัน LineMessageQueue เพื่อจัดการ Rate Limit
class LineMessageQueue:
    def __init__(self, max_per_minute=100):
        self.queue = Queue()
        self.max_per_minute = max_per_minute
        self.sent_count = 0
        self.last_reset = time.time()
        self.processing_lock = threading.Lock() # เพื่อป้องกัน race conditions

    def add_message(self, user_id, message_obj):
        self.queue.put((user_id, message_obj, time.time()))
        app.logger.info(f"Message added to queue for {user_id}. Queue size: {self.queue.qsize()}")

    def process_queue(self):
        while True:
            with self.processing_lock:
                if time.time() - self.last_reset >= 60:
                    self.sent_count = 0
                    self.last_reset = time.time()

                if self.sent_count >= self.max_per_minute:
                    sleep_time = 60 - (time.time() - self.last_reset)
                    app.logger.info(f"LINE Rate limit reached. Sleeping for {sleep_time:.2f} seconds.")
                    time.sleep(sleep_time)
                    continue

                if not self.queue.empty():
                    user_id, message_obj, timestamp = self.queue.get()

                    if time.time() - timestamp > 300: # 5 minutes expiry
                        app.logger.warning(f"Discarding old message for {user_id} (queued {time.time() - timestamp:.2f}s ago).")
                        continue

                    try:
                        line_bot_api.push_message(user_id, message_obj)
                        self.sent_count += 1
                        app.logger.info(f"Message sent to {user_id}. Sent count: {self.sent_count}/{self.max_per_minute}")
                    except LineBotApiError as e:
                        if e.status_code == 429:  # Rate limit
                            app.logger.warning(f"LINE API Rate limit (429) hit while sending to {user_id}. Re-queuing message.")
                            self.queue.put((user_id, message_obj, timestamp))  # Put back in queue
                            time.sleep(5)
                        else:
                            app.logger.error(f"LINE API Error sending message to {user_id}: {e}")
                    except Exception as e:
                        app.logger.error(f"Unexpected error sending LINE message to {user_id}: {e}")
                else:
                    pass

            time.sleep(1)

# สร้างและเริ่มต้น Line Message Queue
message_queue = LineMessageQueue(max_per_minute=LINE_RATE_LIMIT_PER_MINUTE)
threading.Thread(target=message_queue.process_queue, daemon=True).start()
app.logger.info(f"LINE Message Queue started with a limit of {LINE_RATE_LIMIT_PER_MINUTE} messages/minute.")


SETTINGS_FILE = 'settings.json'
# _DEFAULT_APP_SETTINGS_STORE **ย้ายมาไว้ที่นี่** เพื่อให้ถูกประกาศก่อนใช้งานใน get_app_settings()
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


def safe_execute(request_object):
    if hasattr(request_object, 'execute'):
        return request_object.execute()
    return request_object

def _execute_google_api_call_with_retry(api_call, *args, **kwargs):
    max_retries = 3
    base_delay = 1
    for i in range(max_retries):
        try:
            return safe_execute(api_call(*args, **kwargs))
        except HttpError as e:
            if e.resp.status in [500, 502, 503, 504, 429] and i < max_retries - 1:
                delay = base_delay * (2 ** i)
                app.logger.warning(f"Google API transient error (Status: {e.resp.status}). Retrying in {delay} seconds... (Attempt {i+1}/{max_retries})")
                time.sleep(delay)
            else:
                raise
        except Exception as e:
            app.logger.error(f"Unexpected error during Google API call: {e}")
            raise
    return None

def get_google_service(api_name, api_version):
    creds = None

    # --- START SERVICE ACCOUNT FEATURE ---
    SERVICE_ACCOUNT_FILE_CONTENT = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON') # เก็บ JSON ทั้งก้อนใน env
    
    if SERVICE_ACCOUNT_FILE_CONTENT:
        try:
            info = json.loads(SERVICE_ACCOUNT_FILE_CONTENT)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=SCOPES
            )
            app.logger.info("✅ Loaded credentials from Service Account.")
            # ถ้าโหลด Service Account สำเร็จ ก็ใช้ตัวนี้เลย ไม่ต้องไปเช็ค User Credentials
            # ส่วนการรีเฟรชโทเค็นอัตโนมัติจะไม่มีความจำเป็นสำหรับ Service Account
            try:
                service = _execute_google_api_call_with_retry(build, api_name, api_version, credentials=creds)
                app.logger.info(f"✅ Successfully built {api_name} {api_version} service using Service Account")
                return service
            except Exception as e:
                app.logger.error(f"❌ Failed to build Google API service with Service Account: {e}")
                return None

        except Exception as e:
            app.logger.warning(f"Could not load Service Account from GOOGLE_SERVICE_ACCOUNT_JSON env var: {e}. Falling back to User Credentials.")
            creds = None # ตั้งค่า creds เป็น None เพื่อให้ไปใช้ User Credentials
    # --- END SERVICE ACCOUNT FEATURE ---


    # --- FALLBACK TO USER CREDENTIALS (โค้ดเดิมของคุณ) ---
    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')

    if google_token_json_str:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(google_token_json_str), SCOPES)
            app.logger.info(f"Loaded credentials from environment. Valid: {creds.valid}")
            
            # แสดงข้อมูล token เพื่อ debug
            if hasattr(creds, 'expiry') and creds.expiry:
                app.logger.info(f"Token expires at: {creds.expiry}")
                time_left = creds.expiry - datetime.datetime.utcnow()
                app.logger.info(f"Time left: {time_left}")
                
        except Exception as e:
            app.logger.warning(f"Could not load token from GOOGLE_TOKEN_JSON env var: {e}")
            creds = None

    # ตรวจสอบและ refresh token หากจำเป็น
    if creds:
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    app.logger.info("Token expired, attempting refresh...")
                    creds.refresh(Request())
                    
                    # บันทึก token ใหม่เป็นไฟล์สำรอง (เฉพาะในเครื่อง)
                    try:
                        backup_token = {
                            'token': creds.token,
                            'refresh_token': creds.refresh_token,
                            'token_uri': creds.token_uri,
                            'client_id': creds.client_id,
                            'client_secret': creds.client_secret,
                            'scopes': creds.scopes,
                            'expiry': creds.expiry.isoformat() if creds.expiry else None
                        }
                        
                        with open('backup_token.json', 'w') as f:
                            json.dump(backup_token, f, indent=2)
                        app.logger.info("Token backup saved to backup_token.json")
                    except Exception as backup_error:
                        app.logger.warning(f"Could not save backup token: {backup_error}")
                    
                    app.logger.info("="*80)
                    app.logger.info("🔄 Google access token refreshed successfully!")
                    app.logger.info("📋 PLEASE UPDATE YOUR GOOGLE_TOKEN_JSON ENVIRONMENT VARIABLE:")
                    app.logger.info(f"NEW TOKEN: {creds.to_json()}") # โทเค็นใหม่ที่ควรนำไปอัปเดตใน env var
                    app.logger.info("="*80)
                    
                except Exception as e:
                    app.logger.error(f"❌ Error refreshing token: {e}")
                    app.logger.error("🔧 Please run get_token.py to generate a new token")
                    creds = None
            else:
                app.logger.error("❌ Token invalid and cannot be refreshed (no refresh_token)")
                app.logger.error("🔧 Please run get_token.py to generate a new token")
                creds = None

    # สร้าง Google API service (ใช้ creds จาก User Credentials หากไม่มี Service Account)
    if creds and creds.valid:
        try:
            service = _execute_google_api_call_with_retry(build, api_name, api_version, credentials=creds)
            app.logger.info(f"✅ Successfully built {api_name} {api_version} service")
            return service
        except Exception as e:
            app.logger.error(f"❌ Failed to build Google API service: {e}")
            return None
    else:
        app.logger.error("❌ No valid Google credentials available (Service Account or User Credentials).")
        app.logger.error("🔧 Please ensure:")
        app.logger.error("   1. GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_TOKEN_JSON environment variable is set")
        app.logger.error("   2. OAuth consent screen is in Production mode (for User Credentials)")
        app.logger.error("   3. Run get_token.py to generate a fresh token (for User Credentials)")
        return None

def get_google_tasks_service(): return get_google_service('tasks', 'v1')
def get_google_drive_service(): return get_google_service('drive', 'v3')

@app.route('/admin/token_status')
def token_status():
    """แสดงสถานะ Google Token สำหรับ debugging"""
    # ตรวจสอบ Service Account ก่อน
    SERVICE_ACCOUNT_FILE_CONTENT = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    if SERVICE_ACCOUNT_FILE_CONTENT:
        try:
            info = json.loads(SERVICE_ACCOUNT_FILE_CONTENT)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=SCOPES
            )
            return jsonify({
                'status': 'success',
                'message': 'Using Google Service Account',
                'service_account_email': creds.service_account_email,
                'scopes': list(creds.scopes) if creds.scopes else []
            })
        except Exception as e:
            return jsonify({
                'status': 'error',
                'message': f'Error loading Service Account: {e}',
                'detail': 'Service Account JSON might be invalid.'
            })

    # ถ้าไม่มี Service Account, ตรวจสอบ User Token
    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')
    
    if not google_token_json_str:
        return jsonify({
            'status': 'error',
            'message': 'No Google credentials found (neither Service Account nor User Token).'
        })
    
    try:
        creds = Credentials.from_authorized_user_info(json.loads(google_token_json_str), SCOPES)
        
        status_info = {
            'valid': creds.valid,
            'expired': creds.expired,
            'has_refresh_token': bool(creds.refresh_token),
            'token_uri': creds.token_uri,
            'scopes': list(creds.scopes) if creds.scopes else []
        }
        
        if hasattr(creds, 'expiry') and creds.expiry:
            status_info['expires_at'] = creds.expiry.isoformat()
            status_info['expires_in_seconds'] = (creds.expiry - datetime.datetime.utcnow()).total_seconds()
        
        return jsonify({
            'status': 'success',
            'message': 'Using User Credentials',
            'token_info': status_info
        })
        
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'Error parsing User Token: {e}',
            'detail': 'User Token JSON might be invalid or corrupted.'
        })

def sanitize_filename(name):
    if not name:
        return "Unnamed"
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

@cached(cache)
def find_or_create_drive_folder(name, parent_id):
    service = get_google_drive_service()
    if not service:
        return None
    
    query = f"name = '{name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    try:
        response = _execute_google_api_call_with_retry(service.files().list, q=query, spaces='drive', fields='files(id, name)', pageSize=1)
        files = response.get('files', [])
        if files:
            app.logger.info(f"Found existing Drive folder '{name}' with ID: {files[0]['id']}")
            return files[0]['id']
        else:
            app.logger.info(f"Folder '{name}' not found in parent '{parent_id}'. Creating it...")
            file_metadata = {
                'name': name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [parent_id]
            }
            folder = _execute_google_api_call_with_retry(service.files().create, body=file_metadata, fields='id')
            folder_id = folder.get('id')
            app.logger.info(f"Created new Drive folder '{name}' with ID: {folder_id}")
            return folder_id
    except HttpError as e:
        app.logger.error(f"Error finding or creating folder '{name}': {e}")
        return None

@cached(cache)
def get_customer_database():
    app.logger.info("Building customer database from Google Tasks...")
    all_tasks = get_google_tasks_for_report(show_completed=True)
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

def load_settings_from_drive_on_startup():
    settings_backup_folder_id = find_or_create_drive_folder("Settings_Backups", GOOGLE_DRIVE_FOLDER_ID)
    if not settings_backup_folder_id:
        app.logger.error("Could not find or create Settings_Backups folder. Skipping settings restore.")
        return False
        
    service = get_google_drive_service()
    if not service:
        app.logger.error("Could not get Drive service for settings restore.")
        return False

    try:
        query = f"name = 'settings_backup.json' and '{settings_backup_folder_id}' in parents and trashed = false"
        response = _execute_google_api_call_with_retry(service.files().list, q=query, spaces='drive', fields='files(id, name)', orderBy='modifiedTime desc', pageSize=1)
        files = response.get('files', [])

        if files:
            latest_backup_file_id = files[0]['id']
            app.logger.info(f"Found latest settings backup on Drive (ID: {latest_backup_file_id})")

            request = service.files().get_media(fileId=latest_backup_file_id)
            fh = BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
            fh.seek(0)

            downloaded_settings = json.loads(fh.read().decode('utf-8'))

            if save_settings_to_file(downloaded_settings):
                app.logger.info("Successfully restored settings from Google Drive backup.")
                return True
            else:
                app.logger.error("Failed to save restored settings to local file.")
                return False
        else:
            app.logger.info("No settings backup found on Google Drive for automatic restore.")
            return False
    except Exception as e:
        app.logger.error(f"An unexpected error occurred during settings restore from Drive: {e}")
        return False

def backup_settings_to_drive():
    settings_backup_folder_id = find_or_create_drive_folder("Settings_Backups", GOOGLE_DRIVE_FOLDER_ID)
    if not settings_backup_folder_id:
        app.logger.error("Cannot back up settings: Could not find or create Settings_Backups folder.")
        return False

    service = get_google_drive_service()
    if not service:
        app.logger.error("Cannot back up settings: Google Drive service is unavailable.")
        return False

    try:
        query = f"name = 'settings_backup.json' and '{settings_backup_folder_id}' in parents and trashed = false"
        response = _execute_google_api_call_with_retry(service.files().list, q=query, spaces='drive', fields='files(id)')
        for file_item in response.get('files', []):
            try:
                _execute_google_api_call_with_retry(service.files().delete, fileId=file_item['id'])
                app.logger.info(f"Deleted old settings_backup.json (ID: {file_item['id']}) from Drive before saving new one.")
            except HttpError as e:
                app.logger.warning(f"Could not delete old settings file {file_item['id']}: {e}. Proceeding with upload attempt.")

        settings_data = get_app_settings()
        settings_json_bytes = BytesIO(json.dumps(settings_data, ensure_ascii=False, indent=4).encode('utf-8'))
        
        file_metadata = {'name': 'settings_backup.json', 'parents': [settings_backup_folder_id]}
        media = MediaIoBaseUpload(settings_json_bytes, mimetype='application/json', resumable=True)
        
        _execute_google_api_call_with_retry(
            service.files().create,
            body=file_metadata, media_body=media, fields='id'
        )
        app.logger.info("Successfully saved current settings to settings_backup.json on Google Drive.")
        return True

    except Exception as e:
        app.logger.error(f"Failed to backup settings to Google Drive: {e}", exc_info=True)
        return False

@cached(cache)
def get_google_tasks_for_report(show_completed=True):
    service = get_google_tasks_service()
    if not service: return None
    try:
        results = _execute_google_api_call_with_retry(service.tasks().list, tasklist=GOOGLE_TASKS_LIST_ID, showCompleted=show_completed, maxResults=100)
        return results.get('items', [])
    except HttpError as err:
        app.logger.error(f"API Error getting tasks: {err}")
        return None

def get_single_task(task_id):
    if not task_id: return None
    service = get_google_tasks_service()
    if not service: return None
    try:
        return _execute_google_api_call_with_retry(service.tasks().get, tasklist=GOOGLE_TASKS_LIST_ID, task=task_id)
    except HttpError as err:
        app.logger.error(f"Error getting single task {task_id}: {err}")
        return None

def _perform_drive_upload(media_body, file_name, mime_type, folder_id):
    service = get_google_drive_service()
    if not service or not folder_id:
        app.logger.error(f"Drive service or Folder ID not configured for upload of '{file_name}'.")
        return None

    uploaded_file_id = None
    try:
        file_metadata = {'name': file_name, 'parents': [folder_id]}
        app.logger.info(f"Attempting to upload file '{file_name}' to Drive folder '{folder_id}'.")
        
        file_obj = _execute_google_api_call_with_retry(
            service.files().create,
            body=file_metadata, media_body=media_body, fields='id, webViewLink'
        )

        if not file_obj or 'id' not in file_obj:
            app.logger.error(f"Drive upload failed for '{file_name}': File object or ID is missing.")
            return None

        uploaded_file_id = file_obj['id']
        app.logger.info(f"File '{file_name}' uploaded with ID: {uploaded_file_id}. Setting permissions.")

        permission_result = _execute_google_api_call_with_retry(
            service.permissions().create,
            fileId=uploaded_file_id, body={'role': 'reader', 'type': 'anyone'}
        )
        
        if not permission_result or 'id' not in permission_result:
            app.logger.error(f"CRITICAL: Failed to set permissions for '{file_name}' (ID: {uploaded_file_id}). File will be inaccessible. Aborting and cleaning up.")
            try:
                _execute_google_api_call_with_retry(service.files().delete, fileId=uploaded_file_id)
                app.logger.info(f"Cleaned up file '{file_name}' (ID: {uploaded_file_id}) after permission failure.")
            except Exception as delete_error:
                app.logger.error(f"Could not clean up file '{uploaded_file_id}' after permission failure: {delete_error}")
            return None

        app.logger.info(f"Permissions set for '{file_name}' (ID: {uploaded_file_id}).")
        return file_obj

    except Exception as e:
        app.logger.error(f'Unexpected error during Drive upload for {file_name}: {e}', exc_info=True)
        if uploaded_file_id:
             app.logger.info(f"Attempting to clean up file {uploaded_file_id} after unexpected error.")
             try:
                _execute_google_api_call_with_retry(service.files().delete, fileId=uploaded_file_id)
                app.logger.info(f"Cleaned up file '{file_name}' (ID: {uploaded_file_id}) after unexpected error.")
             except Exception as cleanup_error:
                app.logger.error(f"Failed to cleanup file '{uploaded_file_id}' after error: {cleanup_error}")
        return None

def upload_file_from_path_to_drive(file_path, file_name, mime_type, folder_id):
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        app.logger.error(f"File at path '{file_path}' is missing or empty. Aborting upload.")
        return None
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
    return _perform_drive_upload(media, file_name, mime_type, folder_id)

def upload_data_from_memory_to_drive(data_in_memory, file_name, mime_type, folder_id):
    media = MediaIoBaseUpload(data_in_memory, mimetype=mime_type, resumable=True)
    file_obj = _perform_drive_upload(media, file_name, mime_type, folder_id)
    return file_obj


def create_google_task(title, notes=None, due=None):
    service = get_google_tasks_service()
    if not service: return None
    try:
        task_body = {'title': title, 'notes': notes, 'status': 'needsAction'}
        if due: task_body['due'] = due
        return _execute_google_api_call_with_retry(service.tasks().insert, tasklist=GOOGLE_TASKS_LIST_ID, body=task_body)
    except HttpError as e:
        app.logger.error(f"Error creating Google Task: {e}")
        return None

def delete_google_task(task_id):
    service = get_google_tasks_service()
    if not service: return False
    try:
        _execute_google_api_call_with_retry(service.tasks().delete, tasklist=GOOGLE_TASKS_LIST_ID, task=task_id)
        return True
    except HttpError as err:
        app.logger.error(f"API Error deleting task {task_id}: {err}")
        return False

def update_google_task(task_id, title=None, notes=None, status=None, due=None):
    service = get_google_tasks_service()
    if not service: return None
    try:
        task = _execute_google_api_call_with_retry(service.tasks().get, tasklist=GOOGLE_TASKS_LIST_ID, task=task_id)
        if title is not None: task['title'] = title
        if notes is not None: task['notes'] = notes
        if status is not None:
            task['status'] = status

        if status == 'completed':
            task['completed'] = datetime.datetime.now(pytz.utc).isoformat().replace('+00:00', 'Z')
            task['due'] = None
        else:
            task.pop('completed', None)
            if due is not None:
                task['due'] = due
        if due is None and status == 'needsAction':
             pass

        return _execute_google_api_call_with_retry(service.tasks().update, tasklist=GOOGLE_TASKS_LIST_ID, task=task_id, body=task)
    except HttpError as e:
        app.logger.error(f"Failed to update task {task_id}: {e}")
        return None

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
            
            if 'attachments' in report_data:
                pass
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

@app.context_processor
def inject_now():
    return {'now': datetime.datetime.now(THAILAND_TZ), 'thaizone': THAILAND_TZ}

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
        all_tasks = get_google_tasks_for_report(show_completed=True)
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
    service = get_google_drive_service()
    if not service:
        return False
    try:
        _execute_google_api_call_with_retry(service.about().get, fields='user')
        return True
    except HttpError as e:
        if e.resp.status in [401, 403]:
            app.logger.warning(f"Google API authentication check failed: {e}")
            return False
        app.logger.error(f"A non-auth HttpError occurred during API status check: {e}")
        return True
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
            message_queue.add_message(admin_group_id, TextSendMessage(text=f"‼️ เกิดข้อผิดพลาดร้ายแรงในระบบ ‼️\n\n{message[:900]}"))
    except Exception as e:
        app.logger.error(f"Failed to add critical error notification to queue: {e}")

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
    
    message_queue.add_message(admin_group_id, TextSendMessage(text=message_text))
    app.logger.info(f"New task notification for task {task['id']} added to queue for admin group.")


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
    if admin_group_id:
        message_queue.add_message(admin_group_id, TextSendMessage(text=message_text))
        sent_to.add(admin_group_id)
    
    if tech_group_id and tech_group_id not in sent_to:
        message_queue.add_message(tech_group_id, TextSendMessage(text=message_text))

    app.logger.info(f"Completion notification for task {task['id']} added to queue.")


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
    message_queue.add_message(admin_group_id, TextSendMessage(text=message_text))
    app.logger.info(f"Update/reschedule notification for task {task['id']} added to queue for admin group.")


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
                if upload_data_from_memory_to_drive(memory_file_zip, filename_zip, 'application/zip', system_backup_folder_id):
                    app.logger.info("Automatic full system backup successful.")
                else:
                    app.logger.error("Automatic full system backup failed.")
                    overall_success = False
            else:
                app.logger.error("Failed to create full system backup zip.")
                overall_success = False

        if not backup_settings_to_drive():
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
                    message_queue.add_message(admin_group_id, TextSendMessage(text=message_text))
                    sent_to.add(admin_group_id)

                if technician_group_id and technician_group_id not in sent_to:
                    message_queue.add_message(technician_group_id, TextSendMessage(text=message_text))
            except Exception as e:
                app.logger.error(f"Failed to add appointment reminder for task {task['id']} to queue: {e}")

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
                            message_queue.add_message(customer_line_id, flex_message)
                            app.logger.info(f"Follow-up message for task {task['id']} added to queue for customer {customer_line_id}.")
                            
                            feedback_data['follow_up_sent_date'] = datetime.datetime.now(THAILAND_TZ).isoformat()
                            history_reports, base_notes = parse_tech_report_from_notes(notes)
                            
                            tech_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history_reports])
                            
                            new_notes = base_notes.strip()
                            if tech_reports_text: new_notes += tech_reports_text
                            new_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
                            _execute_google_api_call_with_retry(update_google_task, task['id'], notes=new_notes)
                            cache.clear()

                        except Exception as e:
                            app.logger.error(f"Failed to add direct follow-up to {customer_line_id} to queue: {e}. Notifying admin.")
                            if admin_group_id:
                                message_queue.add_message(admin_group_id, [TextSendMessage(text=f"⚠️ ส่ง Follow-up ให้ลูกค้า {customer_info.get('name')} (Task ID: {task['id']}) ไม่สำเร็จ โปรดส่งข้อความนี้แทน:"), flex_message])


                except Exception as e:
                    app.logger.warning(f"Could not process task {task.get('id')} for follow-up: {e}", exc_info=True)

def run_scheduler():
    """Initializes and runs the APScheduler jobs."""
    global scheduler

    settings = get_app_settings()

    if scheduler.running:
        app.logger.info("Scheduler already running, shutting down before reconfiguring...")
        scheduler.shutdown(wait=False)
    
    scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)

    ab = settings.get('auto_backup', {})
    if ab.get('enabled'):
        scheduler.add_job(scheduled_backup_job, CronTrigger(hour=ab.get('hour_thai', 2), minute=ab.get('minute_thai', 0)), id='auto_system_backup', replace_existing=True)
        app.logger.info(f"Scheduled auto backup for {ab.get('hour_thai', 2)}:{ab.get('minute_thai', 0)} Thai time.")
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
    """A clean shutdown function to be called upon application exit."""
    if scheduler is not None and scheduler.running:
        app.logger.info("Scheduler is running, shutting it down.")
        scheduler.shutdown(wait=False)
    else:
        app.logger.info("Scheduler not running or not initialized, skipping shutdown.")
#</editor-fold>

# --- Initial app setup calls ---
with app.app_context():
    load_settings_from_drive_on_startup()
    run_scheduler()

atexit.register(cleanup_scheduler)

# --- Error Handlers ---
@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    app.logger.error(f"Server Error: {e}", exc_info=True)
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

        new_task = create_google_task(task_title, notes=notes, due=due_date_gmt)
        if new_task:
            cache.clear()
            send_new_task_notification(new_task)
            
            uploaded_attachments_json = request.form.get('uploaded_attachments_json')
            uploaded_attachments = []
            if uploaded_attachments_json:
                try:
                    uploaded_attachments = json.loads(uploaded_attachments_json)
                except json.JSONDecodeError:
                    app.logger.warning("Could not decode uploaded_attachments_json on form submission.")

            if uploaded_attachments:
                task_id = new_task['id']
                
                attachments_base_folder_id = find_or_create_drive_folder("Task_Attachments", GOOGLE_DRIVE_FOLDER_ID)
                monthly_folder_name = datetime.datetime.now(THAILAND_TZ).strftime('%Y-%m')
                monthly_folder_id = find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)
                sanitized_customer_name = sanitize_filename(customer_name)
                customer_task_folder_name = f"{sanitized_customer_name} - {task_id}"
                final_upload_folder_id = find_or_create_drive_folder(customer_task_folder_name, monthly_folder_id)
                
                drive_service = get_google_drive_service()
                if final_upload_folder_id and drive_service:
                    for att in uploaded_attachments:
                        try:
                            file_meta = drive_service.files().get(fileId=att['id'], fields='parents').execute()
                            previous_parents = ",".join(file_meta.get('parents', []))
                            drive_service.files().update(
                                fileId=att['id'],
                                addParents=final_upload_folder_id,
                                removeParents=previous_parents,
                                fields='id, parents'
                            ).execute()
                        except Exception as e:
                            app.logger.error(f"Could not move attachment {att['id']} to final folder: {e}")
                
                initial_report = {
                    'type': 'report',
                    'summary_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
                    'work_summary': 'ไฟล์แนบจากการสร้างงานครั้งแรก',
                    'attachments': uploaded_attachments,
                    'technicians': ['System']
                }
                report_text = f"\n\n--- TECH_REPORT_START ---\n{json.dumps(initial_report, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"
                updated_notes = new_task.get('notes', '') + report_text
                update_google_task(task_id, notes=updated_notes)
                cache.clear()

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
    
    file.seek(0, os.SEEK_END)
    file_length = file.tell()
    file.seek(0)
    
    if file_length > MAX_FILE_SIZE_BYTES:
        if file.mimetype and file.mimetype.startswith('image/'):
            try:
                img = Image.open(file)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                
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

    attachments_base_folder_id = find_or_create_drive_folder("Task_Attachments", GOOGLE_DRIVE_FOLDER_ID)
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
        task_raw = get_single_task(task_id)
        if not task_raw:
            return jsonify({'status': 'error', 'message': 'Task not found'}), 404
        
        if task_raw.get('created'):
            try:
                target_date = date_parse(task_raw.get('created')).astimezone(THAILAND_TZ)
            except (ValueError, TypeError):
                pass
        
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

    media_body = MediaIoBaseUpload(file_to_upload, mimetype=mime_type, resumable=True)
    drive_file = _perform_drive_upload(media_body, filename, mime_type, final_upload_folder_id)
    
    if drive_file:
        return jsonify({'status': 'success', 'file_info': {'id': drive_file.get('id'), 'url': drive_file.get('webViewLink'), 'name': filename}})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to upload to Google Drive'}), 500

@app.route('/summary')
def summary():
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    today_thai = datetime.datetime.now(THAILAND_TZ).date()
    final_tasks = []
    stats = {'needsAction': 0, 'completed': 0, 'overdue': 0, 'total': len(tasks_raw), 'today': 0}

    for task in tasks_raw:
        task_status = task.get('status', 'needsAction')
        is_overdue = False
        is_today = False
        if task_status == 'needsAction' and task.get('due'):
            try:
                due_dt_utc = date_parse(task['due'])
                due_dt_local = due_dt_utc.astimezone(THAILAND_TZ)
                if due_dt_local.date() < today_thai:
                    is_overdue = True
                elif due_dt_local.date() == today_thai:
                    is_today = True
            except (ValueError, TypeError):
                pass
        
        if task_status == 'completed':
            stats['completed'] += 1
        else:
            stats['needsAction'] += 1
            if is_overdue:
                stats['overdue'] += 1
            if is_today:
                stats['today'] += 1

        task_passes_filter = False
        if status_filter == 'all':
            task_passes_filter = True
        elif status_filter == 'completed' and task_status == 'completed':
            task_passes_filter = True
        elif status_filter == 'needsAction' and task_status == 'needsAction':
            task_passes_filter = True
        elif status_filter == 'today' and is_today:
            task_passes_filter = True
        
        if task_passes_filter:
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
    month_labels = []
    chart_values = []
    for i in range(12):
        target_d = datetime.datetime.now(THAILAND_TZ) - datetime.timedelta(days=30 * (11 - i))
        month_key = target_d.strftime('%Y-%m')
        month_labels.append(target_d.strftime('%b %y'))
        count = sum(1 for task in completed_tasks_for_chart if date_parse(task['completed']).astimezone(THAILAND_TZ).strftime('%Y-%m') == month_key)
        chart_values.append(count)
    chart_data = {'labels': month_labels, 'values': chart_values}

    return render_template("dashboard.html",
                           tasks=final_tasks, summary=stats,
                           search_query=search_query, status_filter=status_filter,
                           chart_data=chart_data)

@app.route('/summary/print')
def summary_print():
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    today_thai = date.today()
    final_tasks = []
    
    for task in tasks_raw:
        task_status = task.get('status', 'needsAction')
        is_overdue = False
        is_today = False
        if task_status == 'needsAction' and task.get('due'):
            try:
                due_dt_utc = date_parse(task['due'])
                due_dt_local = due_dt_utc.astimezone(THAILAND_TZ)
                if due_dt_local.date() < today_thai: is_overdue = True
                elif due_dt_local.date() == today_thai: is_today = True
            except (ValueError, TypeError): pass
        
        task_passes_filter = False
        if status_filter == 'all': task_passes_filter = True
        elif status_filter == 'completed' and task_status == 'completed': task_passes_filter = True
        elif status_filter == 'needsAction' and task_status == 'needsAction': task_passes_filter = True
        elif status_filter == 'today' and is_today: task_passes_filter = True

        if task_passes_filter:
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            searchable_text = f"{task.get('title', '')} {customer_info.get('name', '')} {customer_info.get('organization', '')} {customer_info.get('phone', '')}".lower()
            if not search_query or search_query in searchable_text:
                parsed_task = parse_google_task_dates(task)
                parsed_task['customer'] = customer_info
                parsed_task['is_overdue'] = is_overdue
                parsed_task['is_today'] = is_today
                final_tasks.append(parsed_task)

    final_tasks.sort(key=lambda x: (x.get('status') == 'completed', x.get('due') is None, date_parse(x.get('due', '9999-12-31T23:59:59Z'))))
    
    return render_template("summary_print.html",
                           tasks=final_tasks,
                           search_query=search_query,
                           status_filter=status_filter,
                           now=datetime.datetime.now(THAILAND_TZ))

@app.route('/calendar')
def calendar_view():
    tasks_raw = get_google_tasks_for_report(show_completed=False) or []
    unscheduled_tasks = []
    for task in tasks_raw:
        if not task.get('due'):
            parsed_task = parse_google_task_dates(task)
            parsed_task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
            unscheduled_tasks.append(parsed_task)
            
    unscheduled_tasks.sort(key=lambda x: x.get('created', ''), reverse=True)
    
    return render_template('calendar.html', unscheduled_tasks=unscheduled_tasks)

@app.route('/api/calendar_tasks')
def api_calendar_tasks():
    try:
        tasks_raw = get_google_tasks_for_report(show_completed=True) or []
        events = []
        today_thai = datetime.datetime.now(THAILAND_TZ).date()

        for task in tasks_raw:
            if not task.get('due'):
                continue

            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            is_overdue = False
            is_today = False
            is_completed = task.get('status') == 'completed'

            try:
                due_dt_utc = date_parse(task['due'])
                due_dt_local = due_dt_utc.astimezone(THAILAND_TZ)
                if not is_completed and due_dt_local.date() < today_thai:
                    is_overdue = True
                elif not is_completed and due_dt_local.date() == today_thai:
                    is_today = True
            except (ValueError, TypeError):
                pass

            event = {
                'id': task.get('id'),
                'title': f"{customer_info.get('name', 'N/A')} - {task.get('title')}",
                'start': task.get('due'),
                'url': url_for('task_details', task_id=task.get('id')),
                'extendedProps': {
                    'is_completed': is_completed,
                    'is_overdue': is_overdue,
                    'is_today': is_today
                }
            }
            events.append(event)
            
        return jsonify(events)
    except Exception as e:
        app.logger.error(f"Error fetching tasks for calendar API: {e}", exc_info=True)
        return jsonify({"error": "Could not fetch tasks from server"}), 500


@app.route('/api/task/schedule_from_calendar', methods=['POST'])
def schedule_task_from_calendar():
    data = request.json
    task_id = data.get('task_id')
    new_due_str = data.get('new_due_date')
    
    if not task_id or not new_due_str:
        return jsonify({'status': 'error', 'message': 'ข้อมูลที่ส่งมาไม่ครบถ้วน (task_id หรือ new_due_date)'}), 400
        
    try:
        task = get_single_task(task_id)
        if not task:
            return jsonify({'status': 'error', 'message': 'ไม่พบงานที่ระบุ'}), 404
        if task.get('status') == 'completed':
            return jsonify({'status': 'error', 'message': 'ไม่สามารถย้ายงานที่เสร็จสิ้นแล้วได้'}), 403

        dt_utc = date_parse(new_due_str)
        due_date_gmt = dt_utc.isoformat().replace('+00:00', 'Z')

        updated_task = update_google_task(task_id, due=due_date_gmt, status='needsAction')
        
        if updated_task:
            cache.clear()
            return jsonify({'status': 'success', 'message': f'อัปเดตวันนัดหมายสำหรับงาน {task_id} เรียบร้อยแล้ว'})
        else:
            return jsonify({'status': 'error', 'message': 'ไม่สามารถอัปเดตงานใน Google Tasks ได้'}), 500
            
    except Exception as e:
        app.logger.error(f"Error scheduling task from calendar: {e}")
        return jsonify({'status': 'error', 'message': f'เกิดข้อผิดพลาดในระบบ: {e}'}), 500

@app.route('/task/<task_id>', methods=['GET', 'POST'])
def task_details(task_id):
    if request.method == 'POST':
        task_raw = get_single_task(task_id)
        if not task_raw:
            if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'status': 'error', 'message': 'ไม่พบงานที่ต้องการอัปเดต'}), 404
            flash('ไม่พบงานที่ต้องการอัปเดต', 'danger')
            abort(404)
        
        action = request.form.get('action')
        update_payload = {}
        notification_to_send = None
        flash_message = None
        flash_category = 'info'

        history, base_notes_text = parse_tech_report_from_notes(task_raw.get('notes', ''))
        feedback_data = parse_customer_feedback_from_notes(task_raw.get('notes', ''))
        
        new_attachments_from_ajax_json = request.form.get('uploaded_attachments_json')
        new_attachments = []
        if new_attachments_from_ajax_json:
            try:
                new_attachments = json.loads(new_attachments_from_ajax_json)
            except json.JSONDecodeError:
                app.logger.error("Failed to decode uploaded_attachments_json from request.")

        if action == 'save_report':
            work_summary = str(request.form.get('work_summary', '')).strip()
            selected_technicians = request.form.get('technicians_report', '').split(',')
            selected_technicians = [t.strip() for t in selected_technicians if t.strip()]

            if not (work_summary or new_attachments):
                return jsonify({'status': 'error', 'message': 'กรุณากรอกสรุปงาน หรือแนบไฟล์รูปภาพสำหรับรายงานใหม่'}), 400
            if not selected_technicians:
                return jsonify({'status': 'error', 'message': 'กรุณาเลือกช่างผู้รับผิดชอบสำหรับรายงานใหม่นี้'}), 400

            history.append({
                'type': 'report', 'summary_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
                'work_summary': work_summary,
                'equipment_used': _parse_equipment_string(request.form.get('equipment_used', '')),
                'attachments': new_attachments,
                'technicians': selected_technicians
            })
            flash_message = 'เพิ่มรายงานความคืบหน้าเรียบร้อยแล้ว!'
            flash_category = 'success'
        
        elif action == 'reschedule_task':
            reschedule_due_str = str(request.form.get('reschedule_due', '')).strip()
            reschedule_reason = str(request.form.get('reschedule_reason', '')).strip()
            selected_technicians = request.form.get('technicians_reschedule', '').split(',')
            selected_technicians = [t.strip() for t in selected_technicians if t.strip()]

            if not reschedule_due_str:
                return jsonify({'status': 'error', 'message': 'กรุณากำหนดวันนัดหมายใหม่'}), 400
            
            try:
                dt_local = THAILAND_TZ.localize(date_parse(reschedule_due_str))
                update_payload['due'] = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
                update_payload['status'] = 'needsAction'
                new_due_date_formatted = dt_local.strftime("%d/%m/%y %H:%M")
                is_today = dt_local.date() == datetime.datetime.now(THAILAND_TZ).date()
                notification_to_send = ('update', new_due_date_formatted, reschedule_reason, selected_technicians, is_today)
            except ValueError:
                return jsonify({'status': 'error', 'message': 'รูปแบบวันเวลานัดหมายใหม่ไม่ถูกต้อง'}), 400

            history.append({
                'type': 'reschedule', 'summary_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
                'reason': reschedule_reason, 'new_due_date': new_due_date_formatted,
                'technicians': selected_technicians
            })
            flash_message = 'เลื่อนนัดและบันทึกเหตุผลเรียบร้อยแล้ว'
            flash_category = 'success'

        elif action == 'complete_task':
            work_summary = str(request.form.get('work_summary', '')).strip()
            if not work_summary:
                return jsonify({'status': 'error', 'message': 'กรุณากรอกสรุปงานเพื่อปิดงาน'}), 400
                
            selected_technicians = request.form.get('technicians_report', '').split(',')
            selected_technicians = [t.strip() for t in selected_technicians if t.strip()]

            if not selected_technicians:
                return jsonify({'status': 'error', 'message': 'กรุณาเลือกช่างผู้รับผิดชอบสำหรับรายงานปิดงาน'}), 400
            
            history.append({
                'type': 'report', 'summary_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
                'work_summary': work_summary,
                'equipment_used': _parse_equipment_string(request.form.get('equipment_used', '')),
                'attachments': new_attachments,
                'technicians': selected_technicians
            })
            
            update_payload['status'] = 'completed'
            notification_to_send = ('completion', selected_technicians)
            flash_message = 'ปิดงานและบันทึกรายงานสรุปเรียบร้อยแล้ว!'
            flash_category = 'success'
        
        else:
            return jsonify({'status': 'error', 'message': 'ไม่พบการกระทำที่ร้องขอ'}), 400
            
        history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
        all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
        
        final_notes = base_notes_text
        if all_reports_text: final_notes += all_reports_text
        if feedback_data: final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        
        update_payload['notes'] = final_notes
        
        updated_task = update_google_task(task_id, **update_payload)

        if updated_task:
            cache.clear()
            if notification_to_send:
                notif_type = notification_to_send[0]
                if notif_type == 'update': send_update_notification(updated_task, *notification_to_send[1:])
                elif notif_type == 'completion': send_completion_notification(updated_task, *notification_to_send[1:])
            
            return jsonify({'status': 'success', 'message': flash_message})
        else:
            flash_message = 'เกิดข้อผิดพลาดในการบันทึกข้อมูลหลัก!'
            return jsonify({'status': 'error', 'message': flash_message}), 500

    task_raw = get_single_task(task_id)
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
            report_date = parse_google_task_dates({'summary_date': report['summary_date']}).get('summary_date_formatted', '')
            for att in report['attachments']:
                att_copy = att.copy()
                att_copy['report_date'] = report_date
                all_attachments.append(att_copy)


    return render_template('update_task_details.html',
                           task=task,
                           common_equipment_items=app_settings.get('common_equipment_items', []),
                           technician_list=app_settings.get('technician_list', []),
                           all_attachments=all_attachments,
                           progress_report_snippets=TEXT_SNIPPETS.get('progress_reports', [])
                           )

@app.route('/api/task/<task_id>/edit_report_text/<int:report_index>', methods=['POST'])
def api_edit_report_text(task_id, report_index):
    data = request.json
    new_summary = data.get('summary', '').strip()
    
    if not new_summary:
        return jsonify({'status': 'error', 'message': 'กรุณากรอกสรุปงาน'}), 400

    task_raw = get_single_task(task_id)
    if not task_raw:
        return jsonify({'status': 'error', 'message': 'ไม่พบงานที่ต้องการอัปเดต'}), 404

    history, base_notes_text = parse_tech_report_from_notes(task_raw.get('notes', ''))
    feedback_data = parse_customer_feedback_from_notes(task_raw.get('notes', ''))
    
    if not (0 <= report_index < len(history)):
        return jsonify({'status': 'error', 'message': 'ไม่พบรายงานที่ต้องการแก้ไข'}), 404

    history[report_index]['work_summary'] = new_summary
    
    all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
    final_notes = base_notes_text
    if all_reports_text: final_notes += all_reports_text
    if feedback_data: final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
    
    if update_google_task(task_id, notes=final_notes):
        cache.clear()
        return jsonify({'status': 'success', 'message': 'แก้ไขรายงานเรียบร้อยแล้ว'})
    else:
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกการแก้ไข'}), 500

@app.route('/task/<task_id>/edit_report/<int:report_index>', methods=['POST'])
def edit_report_attachments(task_id, report_index):
    task_raw = get_single_task(task_id)
    if not task_raw:
        flash('ไม่พบงานที่ต้องการอัปเดต', 'danger')
        abort(404)

    history, base_notes_text = parse_tech_report_from_notes(task_raw.get('notes', ''))
    feedback_data = parse_customer_feedback_from_notes(task_raw.get('notes', ''))
    
    if not (0 <= report_index < len(history)):
        flash('ไม่พบรายงานที่ต้องการแก้ไข', 'danger')
        return redirect(url_for('task_details', task_id=task_id))

    report_to_edit = history[report_index]
    
    attachments_to_keep_ids = request.form.getlist('attachments_to_keep')
    original_attachments = report_to_edit.get('attachments', [])
    updated_attachments = []
    
    drive_service = get_google_drive_service()
    if drive_service:
        for att in original_attachments:
            if att['id'] in attachments_to_keep_ids:
                updated_attachments.append(att)
            else:
                try:
                    drive_service.files().delete(fileId=att['id']).execute()
                    app.logger.info(f"Deleted attachment {att['id']} from Drive.")
                except HttpError as e:
                    app.logger.error(f"Failed to delete attachment {att['id']} from Drive: {e}")
    else:
        updated_attachments = original_attachments
        flash('ไม่สามารถเชื่อมต่อ Google Drive เพื่อลบไฟล์ได้', 'warning')
    
    new_files = request.files.getlist('new_files[]')
    if new_files:
        if task_raw.get('created'):
            created_dt_local = date_parse(task_raw.get('created')).astimezone(THAILAND_TZ)
            monthly_folder_name = created_dt_local.strftime('%Y-%m')
        else:
            monthly_folder_name = "Uncategorized"

        attachments_base_folder_id = find_or_create_drive_folder("Task_Attachments", GOOGLE_DRIVE_FOLDER_ID)
        monthly_folder_id = find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)
        customer_info = parse_customer_info_from_notes(base_notes_text)
        sanitized_customer_name = sanitize_filename(customer_info.get('name', 'Unknown_Customer'))
        customer_task_folder_name = f"{sanitized_customer_name} - {task_id}"
        final_upload_folder_id = find_or_create_drive_folder(customer_task_folder_name, monthly_folder_id)
        
        if final_upload_folder_id:
            for file in new_files:
                if file and allowed_file(file.filename):
                    file.seek(0, os.SEEK_END)
                    file_length = file.tell()
                    file.seek(0)
                    if file_length > MAX_FILE_SIZE_BYTES and file.mimetype and file.mimetype.startswith('image/'):
                        try:
                            img = Image.open(file)
                            if img.mode in ("RGBA", "P"): img = img.convert("RGB")
                            output_buffer = BytesIO()
                            img.save(output_buffer, format='JPEG', quality=85, optimize=True)
                            output_buffer.seek(0)
                            file_to_upload = output_buffer
                            filename = os.path.splitext(secure_filename(file.filename))[0] + '.jpg'
                            mime_type = 'image/jpeg'
                        except Exception as e:
                            app.logger.error(f"Could not compress image in edit_report: {e}")
                            continue
                    else:
                        file_to_upload = file
                        filename = secure_filename(file.filename)
                        mime_type = file.mimetype or mimetypes.guess_type(filename)[0]

                    media_body = MediaIoBaseUpload(file_to_upload, mimetype=mime_type, resumable=True)
                    drive_file = _perform_drive_upload(media_body, filename, mime_type, final_upload_folder_id)
                    if drive_file:
                        updated_attachments.append({'id': drive_file.get('id'), 'url': drive_file.get('webViewLink'), 'name': filename})
        else:
             flash('ไม่สามารถสร้างโฟลเดอร์สำหรับแนบไฟล์ใหม่ใน Google Drive ได้', 'warning')

    report_to_edit['attachments'] = updated_attachments
    history[report_index] = report_to_edit

    all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
    final_notes = base_notes_text
    if all_reports_text: final_notes += all_reports_text
    if feedback_data: final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
    
    if update_google_task(task_id, notes=final_notes):
        cache.clear()
        flash('แก้ไขรูปภาพในรายงานเรียบร้อยแล้ว!', 'success')
    else:
        flash('เกิดข้อผิดพลาดในการบันทึกการเปลี่ยนแปลงรูปภาพ', 'danger')

    return redirect(url_for('task_details', task_id=task_id))

@app.route('/api/task/<task_id>/delete_report/<int:report_index>', methods=['POST'])
def delete_task_report(task_id, report_index):
    task_raw = get_single_task(task_id)
    if not task_raw:
        return jsonify({'status': 'error', 'message': 'ไม่พบงานที่ต้องการอัปเดต'}), 404

    history, base_notes_text = parse_tech_report_from_notes(task_raw.get('notes', ''))
    feedback_data = parse_customer_feedback_from_notes(task_raw.get('notes', ''))
    
    if not (0 <= report_index < len(history)):
        return jsonify({'status': 'error', 'message': 'ไม่พบรายงานที่ต้องการลบ'}), 404

    report_to_delete = history[report_index]
    if report_to_delete.get('attachments'):
        drive_service = get_google_drive_service()
        if drive_service:
            for att in report_to_delete['attachments']:
                try:
                    drive_service.files().delete(fileId=att['id']).execute()
                    app.logger.info(f"Deleted attachment {att['id']} from Drive while deleting report.")
                except HttpError as e:
                    app.logger.error(f"Failed to delete attachment {att['id']} from Drive during report deletion: {e}")

    history.pop(report_index)

    all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
    final_notes = base_notes_text
    if all_reports_text: final_notes += all_reports_text
    if feedback_data: final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
    
    if update_google_task(task_id, notes=final_notes):
        cache.clear()
        return jsonify({'status': 'success', 'message': 'ลบรายงานเรียบร้อยแล้ว'})
    else:
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกหลังลบรายงาน'}), 500


@app.route('/edit_task/<task_id>', methods=['GET', 'POST'])
def edit_task(task_id):
    task_raw = get_single_task(task_id)
    if not task_raw: abort(404)

    if request.method == 'POST':
        new_title = str(request.form.get('task_title', '')).strip()
        if not new_title:
            flash('กรุณากรอกรายละเอียดงาน', 'danger')
            return redirect(url_for('edit_task', task_id=task_id))

        notes_lines = []
        organization_name = str(request.form.get('organization_name', '')).strip()
        if organization_name: notes_lines.append(f"หน่วยงาน: {organization_name}")

        notes_lines.extend([
            f"ลูกค้า: {str(request.form.get('customer_name', '')).strip()}",
            f"เบอร์โทรศัพท์: {str(request.form.get('customer_phone', '')).strip()}",
            f"ที่อยู่: {str(request.form.get('address', '')).strip()}",
        ])
        map_url = str(request.form.get('latitude_longitude', '')).strip()
        if map_url: notes_lines.append(map_url)
        
        new_base_notes = "\n".join(filter(None, notes_lines))

        tech_reports, _ = parse_tech_report_from_notes(task_raw.get('notes', ''))
        feedback_data = parse_customer_feedback_from_notes(task_raw.get('notes', ''))
        
        all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in tech_reports])
        
        final_notes = new_base_notes
        if all_reports_text: final_notes += all_reports_text
        if feedback_data: final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

        due_date_gmt = None
        appointment_str = str(request.form.get('appointment_due', '')).strip()
        if appointment_str:
            try:
                dt_local = THAILAND_TZ.localize(date_parse(appointment_str))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
            except ValueError:
                flash('รูปแบบวันเวลานัดหมายไม่ถูกต้อง', 'warning')
                return redirect(url_for('edit_task', task_id=task_id))

        if update_google_task(task_id, title=new_title, notes=final_notes, due=due_date_gmt):
            cache.clear()
            flash('บันทึกข้อมูลหลักของงานเรียบร้อยแล้ว!', 'success')
            return redirect(url_for('summary'))
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกข้อมูลหลัก', 'danger')
            return redirect(url_for('edit_task', task_id=task_id))

    task = parse_google_task_dates(task_raw)
    task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
    return render_template('edit_task.html', task=task)


@app.route('/delete_task/<task_id>', methods=['POST'])
def delete_task(task_id):
    if delete_google_task(task_id):
        flash('ลบงานเรียบร้อยแล้ว!', 'success')
        cache.clear()
    else:
        flash('เกิดข้อผิดพลาดในการลบงาน', 'danger')
    return redirect(url_for('summary'))

@app.route('/api/delete_task/<task_id>', methods=['POST'])
def api_delete_task(task_id):
    if delete_google_task(task_id):
        cache.clear()
        return jsonify({'status': 'success', 'message': 'Task deleted successfully.'})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to delete task.'}), 500

@app.route('/api/delete_tasks_batch', methods=['POST'])
def api_delete_tasks_batch():
    data = request.json
    task_ids = data.get('task_ids', [])
    if not isinstance(task_ids, list):
        return jsonify({'status': 'error', 'message': 'Invalid input format.'}), 400
    
    deleted, failed = 0, 0
    for task_id in task_ids:
        if delete_google_task(task_id): deleted += 1
        else: failed += 1
    if deleted > 0: cache.clear()
    return jsonify({ 'status': 'success', 'message': f'Deleted {deleted} tasks, {failed} failed.'})

@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        technician_list_json_str = request.form.get('technician_list_json', '[]')
        try:
            technician_list = json.loads(technician_list_json_str)
        except json.JSONDecodeError:
            flash('เกิดข้อผิดพลาดในการอ่านข้อมูลช่าง', 'danger')
            return redirect(url_for('settings_page'))

        appointment_reminder_hour = int(request.form.get('appointment_reminder_hour', 0))
        outstanding_report_hour = int(request.form.get('outstanding_report_hour', 0))
        customer_followup_hour = int(request.form.get('customer_followup_hour', 0))
        auto_backup_hour = int(request.form.get('auto_backup_hour', 0))
        auto_backup_minute = int(request.form.get('auto_backup_minute', 0))

        settings_data = {
            'report_times': {
                'appointment_reminder_hour_thai': appointment_reminder_hour,
                'outstanding_report_hour_thai': outstanding_report_hour,
                'customer_followup_hour_thai': customer_followup_hour
            },
            'line_recipients': {
                'admin_group_id': request.form.get('admin_group_id', '').strip(),
                'technician_group_id': request.form.get('technician_group_id', '').strip(),
                'manager_user_id': request.form.get('manager_user_id', '').strip()
            },
            'auto_backup': {
                'enabled': request.form.get('auto_backup_enabled') == 'on',
                'hour_thai': auto_backup_hour,
                'minute_thai': auto_backup_minute
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
            if backup_settings_to_drive():
                flash('บันทึกและสำรองการตั้งค่าไปที่ Google Drive เรียบร้อยแล้ว!', 'success')
            else:
                flash('บันทึกการตั้งค่าสำเร็จ แต่สำรองไปที่ Google Drive ไม่สำเร็จ!', 'warning')
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกการตั้งค่า!', 'danger')
        return redirect(url_for('settings_page'))

    current_settings = get_app_settings()
    return render_template('settings_page.html', settings=current_settings)

@app.route('/api/upload_avatar', methods=['POST'])
def api_upload_avatar():
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file'}), 400
    
    file.seek(0, os.SEEK_END)
    file_length = file.tell()
    file.seek(0)
    
    if file_length > MAX_FILE_SIZE_BYTES:
        if file.mimetype and file.mimetype.startswith('image/'):
            try:
                img = Image.open(file)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                
                output_buffer = BytesIO()
                img.save(output_buffer, format='JPEG', quality=85, optimize=True)
                output_buffer.seek(0)
                file_to_upload = output_buffer
                filename = os.path.splitext(secure_filename(file.filename))[0] + '.jpg'
                mime_type = 'image/jpeg'
                app.logger.info(f"Compressed avatar '{file.filename}' successfully.")
            except Exception as e:
                app.logger.error(f"Could not compress avatar '{file.filename}': {e}")
                return jsonify({'status': 'error', 'message': f'ไฟล์รูปภาพใหญ่เกินไปและไม่สามารถบีบอัดได้'}), 413
        else:
            return jsonify({'status': 'error', 'message': f'ไฟล์ใหญ่เกินขนาดที่กำหนด ({MAX_FILE_SIZE_MB}MB)'}), 413
    else:
        file_to_upload = file
        filename = secure_filename(file.filename)
        mime_type = file.mimetype or mimetypes.guess_type(filename)[0]

    avatars_folder_id = find_or_create_drive_folder("Technician_Avatars", GOOGLE_DRIVE_FOLDER_ID)
    if not avatars_folder_id:
        return jsonify({'status': 'error', 'message': 'Could not create or find Technician_Avatars folder'}), 500

    media_body = MediaIoBaseUpload(file_to_upload, mimetype=mime_type, resumable=True)
    drive_file = _perform_drive_upload(media_body, filename, mime_type, avatars_folder_id)
    
    if drive_file:
        return jsonify({'status': 'success', 'file_id': drive_file.get('id'), 'url': drive_file.get('webViewLink')})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to upload avatar to Google Drive'}), 500


@app.route('/test_notification', methods=['POST'])
def test_notification():
    recipient_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
    if recipient_id:
        try:
            message_queue.add_message(recipient_id, TextSendMessage(text="[ทดสอบ] นี่คือข้อความทดสอบจากระบบ"))
            flash(f'ส่งข้อความทดสอบไปที่ ID: {recipient_id} สำเร็จ!', 'success')
        except Exception as e:
            flash(f'เกิดข้อผิดพลาดในการส่ง: {e}', 'danger')
    else:
        flash('กรุณากำหนด "LINE Admin Group ID" ก่อน', 'danger')
    return redirect(url_for('settings_page'))

@app.route('/backup_data')
def backup_data():
    system_backup_folder_id = find_or_create_drive_folder("System_Backups", GOOGLE_DRIVE_FOLDER_ID)
    if not system_backup_folder_id:
        flash('ไม่สามารถหาหรือสร้างโฟลเดอร์ System_Backups ใน Google Drive ได้', 'danger')
        return redirect(url_for('settings_page'))
    
    memory_file, filename = _create_backup_zip()
    if memory_file and filename:
        return Response(memory_file.getvalue(), mimetype='application/zip', headers={'Content-Disposition': f'attachment;filename={filename}'})
    else:
        flash('เกิดข้อผิดพลาดในการสร้างไฟล์สำรองข้อมูล', 'danger')
        return redirect(url_for('settings_page'))

@app.route('/trigger_auto_backup_now', methods=['POST'])
def trigger_auto_backup_now():
    if scheduled_backup_job():
        flash('สำรองข้อมูลไปที่ Google Drive สำเร็จ!', 'success')
    else:
        flash('เกิดข้อผิดพลาดในการสำรองข้อมูลไปที่ Google Drive!', 'danger')
    return redirect(url_for('settings_page'))

@app.route('/export_equipment_catalog', methods=['GET'])
def export_equipment_catalog():
    try:
        df = pd.DataFrame(get_app_settings().get('equipment_catalog', []))
        if df.empty:
            flash('ไม่มีข้อมูลอุปกรณ์ในแคตตาล็อก', 'warning')
            return redirect(url_for('settings_page') )
        output = BytesIO()
        df.to_excel(output, index=False, sheet_name='Equipment_Catalog')
        output.seek(0)
        return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment;filename=equipment_catalog.xlsx"})
    except Exception as e:
        flash(f'เกิดข้อผิดพลาดในการส่งออก: {e}', 'danger')
        return redirect(url_for('settings_page'))

@app.route('/import_equipment_catalog', methods=['POST'])
def import_equipment_catalog():
    if 'excel_file' not in request.files or not request.files['excel_file'].filename:
        flash('กรุณาเลือกไฟล์ Excel', 'danger')
        return redirect(url_for('settings_page'))
    file = request.files['excel_file']
    if file and file.filename.endswith(('.xls', '.xlsx')):
        try:
            df = pd.read_excel(file.stream)
            required_cols = ['item_name', 'unit', 'price']
            if not all(col in df.columns for col in required_cols):
                flash(f'ไฟล์ Excel ต้องมีคอลัมน์: {", ".join(required_cols)}', 'danger')
            else:
                imported_catalog = []
                for _, row in df.iterrows():
                    item = {'item_name': str(row['item_name']).strip()}
                    if pd.notna(row['unit']): item['unit'] = str(row['unit']).strip()
                    if pd.notna(row['price']):
                        try: item['price'] = float(row['price'])
                        except ValueError: item['price'] = 0.0
                    imported_catalog.append(item)
                save_app_settings({'equipment_catalog': imported_catalog})
                flash('นำเข้าแคตตาล็อกอุปกรณ์เรียบร้อยแล้ว!', 'success')
        except Exception as e:
            flash(f"เกิดข้อผิดพลาดในการนำเข้าไฟล์: {e}", 'danger')
    else:
        flash('รองรับเฉพาะไฟล์ Excel (.xls, .xlsx) เท่านั้น', 'danger')
    return redirect(url_for('settings_page'))

@app.route('/api/import_backup_file', methods=['POST'])
def api_import_backup_file():
    if 'backup_file' not in request.files:
        return jsonify({"status": "error", "message": "No backup file selected."}), 400
    file, file_type = request.files['backup_file'], request.form.get('file_type')
    if file_type not in ['tasks_json', 'settings_json'] or not file.filename.endswith('.json'):
        return jsonify({"status": "error", "message": "Invalid file or type."}), 400
    try:
        data = json.load(file.stream)
        if file_type == 'tasks_json':
            if not isinstance(data, list): return jsonify({"status": "error", "message": "JSON is not a list."}), 400
            service = get_google_tasks_service()
            if not service: return jsonify({"status": "error", "message": "Cannot connect to Google Tasks."}), 500
            created, updated, skipped = 0, 0, 0
            for task_data in data:
                try:
                    original_id, clean_task_data = task_data.get('id'), {k: v for k, v in task_data.items() if k not in ['kind', 'selfLink', 'position', 'etag', 'updated', 'links', 'webViewLink']}
                    if 'due' in clean_task_data and clean_task_data['due']: clean_task_data['due'] = date_parse(clean_task_data['due']).astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
                    if 'completed' in clean_task_data and clean_task_data['completed']: clean_task_data['completed'] = date_parse(clean_task_data['completed']).astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
                    existing_task = _execute_google_api_call_with_retry(service.tasks().get, tasklist=GOOGLE_TASKS_LIST_ID, task=original_id) if original_id else None
                    if existing_task:
                        _execute_google_api_call_with_retry(service.tasks().update, tasklist=GOOGLE_TASKS_LIST_ID, task=original_id, body={**existing_task, **clean_task_data})
                        updated += 1
                    else:
                        clean_task_data.pop('id', None)
                        _execute_google_api_call_with_retry(service.tasks().insert, tasklist=GOOGLE_TASKS_LIST_ID, body=clean_task_data)
                        created += 1
                except Exception: skipped += 1
            cache.clear()
            return jsonify({"status": "success", "message": f"นำเข้าสำเร็จ! สร้างใหม่: {created}, อัปเดต: {updated}, ข้าม: {skipped}"})
        elif file_type == 'settings_json':
            if not isinstance(data, dict): return jsonify({"status": "error", "message": "JSON is not a dict."}), 400
            if save_app_settings(data):
                run_scheduler(); cache.clear()
                return jsonify({"status": "success", "message": "นำเข้าการตั้งค่าเรียบร้อยแล้ว!"})
            else: return jsonify({"status": "error", "message": "เกิดข้อผิดพลาดในการบันทึกการตั้งค่า"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": f"เกิดข้อผิดพลาด: {e}"}), 500

@app.route('/api/preview_backup_file', methods=['POST'])
def preview_backup_file():
    if 'backup_file' not in request.files:
        return jsonify({"status": "error", "message": "No backup file selected."}), 400
    file, file_type = request.files['backup_file'], request.form.get('file_type')
    if file_type not in ['tasks_json', 'settings_json'] or not file.filename.endswith('.json'):
        return jsonify({"status": "error", "message": "Invalid file or type."}), 400
    try:
        data = json.load(file.stream)
        if file_type == 'tasks_json':
            if not isinstance(data, list): return jsonify({"status": "error", "message": "JSON is not a list."}), 400
            count = len(data)
            examples = [{'title': t.get('title', 'N/A'), 'customer_name': parse_customer_info_from_notes(t.get('notes', '')).get('name', 'N/A')} for t in data[:5]]
            return jsonify({"status": "success", "type": "tasks", "task_count": count, "example_tasks": examples})
        elif file_type == 'settings_json':
            if not isinstance(data, dict): return jsonify({"status": "error", "message": "JSON is not a dict."}), 400
            preview = {
                "admin_group_id": data.get('line_recipients', {}).get('admin_group_id', 'N/A'),
                "technician_list_count": len(data.get('technician_list', []))
            }
            return jsonify({"status": "success", "type": "settings", "preview_settings": preview})
    except Exception as e:
        return jsonify({"status": "error", "message": f"เกิดข้อผิดพลาด: {e}"}), 500

@app.route('/technician_report')
def technician_report():
    now = datetime.datetime.now(THAILAND_TZ)
    try:
        year, month = int(request.args.get('year', now.year)), int(request.args.get('month', now.month))
    except (ValueError, TypeError):
        year, month = now.year, now.month
    
    months = [{'value': i, 'name': datetime.date(2000, i, 1).strftime('%B')} for i in range(1, 13)]
    
    app_settings = get_app_settings()
    technician_list = app_settings.get('technician_list', [])

    tasks = get_google_tasks_for_report(show_completed=True) or []
    report = defaultdict(lambda: {'count': 0, 'tasks': []})

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
                        report[tech_name]['count'] += 1
                        report[tech_name]['tasks'].append({'id': task.get('id'), 'title': task.get('title'), 'completed_formatted': completed_dt.strftime("%d/%m/%Y")})
            except Exception as e:
                app.logger.error(f"Error processing task {task.get('id')} for technician report: {e}")
                continue

    return render_template('technician_report.html',
                           report_data=report, selected_year=year, selected_month=month,
                           years=list(range(now.year - 5, now.year + 2)), months=months,
                           technician_list=technician_list)

@app.route('/manage_duplicates', methods=['GET'])
def manage_duplicates():
    tasks = get_google_tasks_for_report(show_completed=True) or []
    duplicates = defaultdict(list)
    for task in tasks:
        if task.get('title'):
            customer_name = parse_customer_info_from_notes(task.get('notes', '')).get('name', '').strip().lower()
            duplicates[(task['title'].strip(), customer_name)].append(task)
    
    sets = {k: sorted(v, key=lambda t: t.get('created', ''), reverse=True) for k, v in duplicates.items() if len(v) > 1}
    processed_sets = {}
    for key, task_list in sets.items():
        processed_tasks = []
        for task in task_list:
            parsed = parse_google_task_dates(task)
            parsed['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
            parsed['is_overdue'] = task.get('status') == 'needsAction' and task.get('due') and date_parse(task['due']) < datetime.datetime.now(pytz.utc)
            processed_tasks.append(parsed)
        processed_sets[key] = processed_tasks
    return render_template('duplicates.html', duplicates=processed_sets)

@app.route('/delete_duplicates_batch', methods=['POST'])
def delete_duplicates_batch():
    ids = request.form.getlist('task_ids')
    if not ids:
        flash('ไม่พบรายการที่เลือกเพื่อลบ', 'warning')
        return redirect(url_for('manage_duplicates'))
    deleted, failed = 0, 0
    for task_id in ids:
        if delete_google_task(task_id): deleted += 1
        else: failed += 1
    if deleted > 0: cache.clear()
    flash(f'ลบงานที่เลือกสำเร็จ: {deleted} รายการ. ล้มเหลว: {failed} รายการ.', 'success' if failed == 0 else 'warning')
    return redirect(url_for('manage_duplicates'))

@app.route('/manage_equipment_duplicates', methods=['GET'])
def manage_equipment_duplicates():
    catalog = get_app_settings().get('equipment_catalog', [])
    duplicates = defaultdict(list)
    for i, item in enumerate(catalog):
        name = item.get('item_name', '').strip().lower()
        if name: duplicates[name].append({'original_index': i, 'data': item})
    sets = {k: sorted(v, key=lambda x: x['original_index']) for k, v in duplicates.items() if len(v) > 1}
    return render_template('equipment_duplicates.html', duplicates=sets)

@app.route('/delete_equipment_duplicates_batch', methods=['POST'])
def delete_equipment_duplicates_batch():
    indices = sorted([int(idx) for idx in request.form.getlist('item_indices')], reverse=True)
    if not indices:
        flash('ไม่พบรายการอุปกรณ์ที่เลือกเพื่อลบ', 'warning')
        return redirect(url_for('manage_equipment_duplicates'))
    catalog = get_app_settings().get('equipment_catalog', [])
    deleted_count = 0
    for idx in indices:
        if 0 <= idx < len(catalog):
            catalog.pop(idx)
            deleted_count += 1
    if save_app_settings({'equipment_catalog': catalog}):
        flash(f'ลบรายการอุปกรณ์ที่เลือกสำเร็จ: {deleted_count} รายการ.', 'success')
    else:
        flash('เกิดข้อผิดพลาดในการบันทึกการเปลี่ยนแปลงแคตตาล็อกอุปกรณ์', 'danger')
    return redirect(url_for('manage_equipment_duplicates'))

@app.route('/customer_onboarding/<task_id>')
def customer_onboarding_page(task_id):
    task = get_single_task(task_id)
    if not task: abort(404)
    return render_template('customer_onboarding.html', task=task, LINE_LOGIN_CHANNEL_ID=LINE_LOGIN_CHANNEL_ID)

@app.route('/generate_customer_onboarding_qr/<task_id>')
def generate_customer_onboarding_qr(task_id):
    task = get_single_task(task_id)
    if not task or not LIFF_ID_FORM: abort(404)
    onboarding_url = url_for('customer_onboarding_page', task_id=task_id, _external=True)
    liff_url = f"https://liff.line.me/{LIFF_ID_FORM}?liff.state={onboarding_url}"
    qr_code = generate_qr_code_base64(liff_url)
    customer = parse_customer_info_from_notes(task.get('notes', ''))
    return render_template('generate_onboarding_qr.html', qr_code_base64=qr_code, task=task, customer_info=customer, public_report_url=url_for('public_task_report', task_id=task_id, _external=True), qr_code_base64_report=generate_qr_code_base64(url_for('public_task_report', task_id=task_id, _external=True)))

@app.route('/customer_problem_form')
def customer_problem_form():
    task_id = request.args.get('task_id')
    task = get_single_task(task_id)
    if not task: abort(404)
    parsed = parse_google_task_dates(task)
    parsed['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
    return render_template('customer_problem_form.html', task=parsed, LINE_LOGIN_CHANNEL_ID=LINE_LOGIN_CHANNEL_ID)

@app.route('/generate_public_report_qr/<task_id>')
def generate_public_report_qr(task_id):
    task = get_single_task(task_id)
    if not task or task.get('status') != 'completed': abort(404)
    url = url_for('public_task_report', task_id=task_id, _external=True)
    qr = generate_qr_code_base64(url)
    customer = parse_customer_info_from_notes(task.get('notes', ''))
    return render_template('public_report_qr.html', task=task, customer_info=customer, public_report_url=url, qr_code_base64_report=qr)

@app.route('/trigger_customer_follow_up_test', methods=['POST'])
def trigger_customer_follow_up_test():
    with app.app_context():
        tasks = [t for t in (get_google_tasks_for_report(True) or []) if t.get('status') == 'completed' and t.get('completed')]
        if not tasks:
            flash('ไม่พบงานที่เสร็จแล้วสำหรับใช้ทดสอบ.', 'warning')
            return redirect(url_for('settings_page'))
        latest = max(tasks, key=lambda x: date_parse(x.get('completed', '0001-01-01T00:00:00Z')))
        notes = latest.get('notes', '')
        feedback = parse_customer_feedback_from_notes(notes)
        feedback.pop('follow_up_sent_date', None)
        
        history_reports, base_notes = parse_tech_report_from_notes(notes)
        tech_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history_reports])
        new_notes_content = base_notes.strip()
        if tech_reports_text: new_notes_content += tech_reports_text
        new_notes_content += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

        _execute_google_api_call_with_retry(update_google_task, latest['id'], notes=new_notes_content)
        
        latest['completed'] = (datetime.datetime.now(pytz.utc) - datetime.timedelta(days=1, minutes=5)).isoformat().replace('+00:00', 'Z')
        _execute_google_api_call_with_retry(update_google_task, latest['id'], completed=latest['completed'])
        
        cache.clear()
        scheduled_customer_follow_up_job()
        flash(f"กำลังทดสอบส่งแบบสอบถามสำหรับงานล่าสุด: '{latest.get('title')}'", 'info')
    return redirect(url_for('settings_page'))

@app.route('/public_report/<task_id>')
def public_task_report(task_id):
    task = get_single_task(task_id)
    if not task or task.get('status') != 'completed':
        abort(404)
    
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
    
    return render_template('public_task_report.html', 
                           task=task, 
                           customer_info=customer, 
                           latest_report=latest_report, 
                           detailed_costs=costs, 
                           total_cost=total,
                           settings=app_settings)

@app.route('/submit_customer_problem', methods=['POST'])
def submit_customer_problem():
    data = request.json
    task_id, problem_desc, user_id = data.get('task_id'), data.get('problem_description'), data.get('customer_line_user_id')
    if not task_id or not problem_desc: return jsonify({"status": "error"}), 400
    task = get_single_task(task_id)
    if not task: return jsonify({"status": "error"}), 404
    notes = task.get('notes', '')
    feedback = parse_customer_feedback_from_notes(notes)
    feedback.update({'feedback_date': datetime.datetime.now(THAILAND_TZ).isoformat(), 'feedback_type': 'problem_reported', 'customer_line_user_id': user_id})

    reports_history, base = parse_tech_report_from_notes(notes)
    reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in reports_history])
    final_notes = f"{base.strip()}"
    if reports_text: final_notes += reports_text
    final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

    _execute_google_api_call_with_retry(update_google_task, task_id=task_id, notes=final_notes, status='needsAction')
    cache.clear()
    admin_group = get_app_settings().get('line_recipients', {}).get('admin_group_id')
    if admin_group:
        customer = parse_customer_info_from_notes(notes)
        notif = f"🚨 ลูกค้าแจ้งปัญหา!\nงาน: {task.get('title')}\nลูกค้า: {customer.get('name', 'N/A')}\nปัญหา: {problem_desc}\nดูรายละเอียด: {url_for('task_details', task_id=task_id, _external=True)}"
        message_queue.add_message(admin_group, TextSendMessage(text=notif))
    return jsonify({"status": "success"})

@app.route('/save_customer_line_id', methods=['POST'])
def save_customer_line_id():
    data = request.json
    task_id, user_id = data.get('task_id'), data.get('customer_line_user_id')
    if not task_id or not user_id: return jsonify({"status": "error"}), 400
    task = get_single_task(task_id)
    if not task: return jsonify({"status": "error"}), 404
    notes = task.get('notes', '')
    feedback = parse_customer_feedback_from_notes(notes)
    if feedback.get('customer_line_user_id') != user_id:
        feedback['customer_line_user_id'] = user_id
        feedback['id_saved_date'] = datetime.datetime.now(THAILAND_TZ).isoformat()
        
        reports_history, base = parse_tech_report_from_notes(notes)
        reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history_reports])
        final_notes = f"{base.strip()}"
        if reports_text: final_notes += reports_text
        final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        
        if _execute_google_api_call_with_retry(update_google_task, task_id=task_id, notes=final_notes):
            cache.clear()
            shop = get_app_settings().get('shop_info', {})
            customer = parse_customer_info_from_notes(notes)
            welcome = f"เรียน คุณ{customer.get('name', 'ลูกค้า')},\n\nขอบคุณที่เชื่อมต่อกับ Comphone ครับ/ค่ะ!\nเราจะใช้ LINE นี้เพื่อส่งข้อมูลสำคัญเกี่ยวกับบริการครับ\n\nติดต่อ:\nโทร: {shop.get('contact_phone', '-')}\nLINE ID: {shop.get('line_id', '-')}"
            message_queue.add_message(user_id, TextSendMessage(text=welcome))
            return jsonify({"status": "success"})
        else: return jsonify({"status": "error"}), 500
    return jsonify({"status": "success", "message": "already saved"})


# --- LINE Bot Handlers ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid LINE signature. Please check your channel secret.")
        abort(400)
    except Exception as e:
        app.logger.error(f"Error handling LINE webhook event: {e}", exc_info=True)
        #notify_admin_error(f"Webhook Handler Error: {e}") # สามารถเปิดใช้งานเพื่อแจ้งเตือน แต่ต้องระวัง Rate Limit
        abort(500)
    return 'OK'

def create_task_list_message(title, tasks, limit=5):
    if not tasks:
        return TextSendMessage(text=f"ไม่พบรายการ{title}ในขณะนี้")
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
        tasks = [t for t in (get_google_tasks_for_report(False) or []) if t.get('due') and date_parse(t['due']).astimezone(THAILAND_TZ).date() == datetime.datetime.now(THAILAND_TZ).date() and t.get('status') == 'needsAction']
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
        tasks = [t for t in (get_google_tasks_for_report(False) or []) if t.get('status') == 'needsAction']
        reply = create_full_summary_message('รายการงานค้าง', tasks)
    elif text == 'งานเสร็จ':
        tasks = sorted([t for t in (get_google_tasks_for_report(True) or []) if t.get('status') == 'completed'], key=lambda x: date_parse(x.get('completed', '0001-01-01T00:00:00Z')), reverse=True)
        reply = create_task_list_message('รายการงานเสร็จล่าสุด', tasks)
    elif text == 'งานพรุ่งนี้':
        tasks = [t for t in (get_google_tasks_for_report(False) or []) if t.get('due') and date_parse(t['due']).astimezone(THAILAND_TZ).date() == (datetime.datetime.now(THAILAND_TZ) + datetime.timedelta(days=1)).date() and t.get('status') == 'needsAction']
        reply = create_task_list_message('งานพรุ่งนี้', tasks)
    elif text == 'สร้างงานใหม่' and LIFF_ID_FORM:
        reply = TextSendMessage(text="เปิดฟอร์มเพื่อสร้างงานใหม่ครับ 👇", quick_reply=QuickReply(items=[QuickReplyButton(action=URIAction(label="เปิดฟอร์มสร้างงาน", uri=f"https://liff.line.me/{LIFF_ID_FORM}"))]))
    elif text.startswith('ดูงาน '):
        query = event.message.text.split(maxsplit=1)[1].strip().lower()
        if not query: return line_bot_api.reply_message(event.reply_token, TextSendMessage(text="โปรดระบุชื่อลูกค้าที่ต้องการค้นหา"))
        tasks = [t for t in (get_google_tasks_for_report(True) or []) if query in parse_customer_info_from_notes(t.get('notes', '')).get('name', '').lower()]
        if not tasks: return line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"ไม่พบงานของลูกค้า: {query}"))
        tasks.sort(key=lambda x: (x.get('status') == 'completed', date_parse(x.get('due', '9999-12-31T23:59:59Z'))))
        bubbles = [create_task_flex_message(t) for t in tasks[:10]]
        return line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"ผลการค้นหา: {query}", contents=CarouselContainer(contents=bubbles)))
    elif text == 'comphone':
        help_text = (
            "พิมพ์คำสั่งเพื่อดูรายงานหรือจัดการงาน:\n"
            "- *งานค้าง*: ดูรายการงานที่ยังไม่เสร็จทั้งหมด\n"
            "- *งานเสร็จ*: ดูรายการงานที่ทำเสร็จแล้ว 5 รายการล่าสุด\n"
            "- *งานวันนี้*: ดูงานที่นัดหมายสำหรับวันนี้ (แยกข้อความ)\n"
            "- *งานพรุ่งนี้*: ดูสรุปงานที่นัดหมายสำหรับพรุ่งนี้\n"
            "- *สร้างงานใหม่*: เปิดฟอร์มสำหรับสร้างงานใหม่\n"
            "- *ดูงาน [ชื่อลูกค้า]*: ค้นหางานตามชื่อลูกค้า\n\n"
            f"ดูข้อมูลทั้งหมด: {url_for('summary', _external=True)}"
        )
        reply = TextSendMessage(text=help_text)
    
    if reply: line_bot_api.reply_message(event.reply_token, reply)

@handler.add(PostbackEvent)
def handle_postback(event):
    data = dict(x.split('=') for x in event.postback.data.split('&'))
    action, task_id = data.get('action'), data.get('task_id')

    if action == 'customer_feedback':
        task = get_single_task(task_id)
        if not task: return
        notes = task.get('notes', '')
        feedback = parse_customer_feedback_from_notes(notes)
        feedback.update({'feedback_date': datetime.datetime.now(THAILAND_TZ).isoformat(), 'feedback_type': data.get('feedback'), 'customer_line_user_id': event.source.user_id})
        history_reports, base = parse_tech_report_from_notes(notes)
        reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history_reports])
        final_notes = f"{base.strip()}"
        if reports_text: final_notes += reports_text
        final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        _execute_google_api_call_with_retry(update_google_task, task_id, notes=final_notes)
        cache.clear()
        try: line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ขอบคุณสำหรับคำยืนยันครับ/ค่ะ 🙏)"))
        except Exception: pass # หากต้องการใช้คิวสำหรับ reply_message ด้วย ต้องปรับปรุงเพิ่มเติม

@app.route("/admin/organize_files", methods=['GET', 'POST'])
def organize_files():
    if request.method == 'POST':
        service = get_google_drive_service()
        if not service:
            flash('ไม่สามารถเชื่อมต่อ Google Drive API ได้', 'danger')
            return redirect(url_for('organize_files'))

        all_tasks = get_google_tasks_for_report(show_completed=True)
        if all_tasks is None:
            flash('ไม่สามารถดึงข้อมูลงานทั้งหมดได้', 'danger')
            return redirect(url_for('organize_files'))
            
        moved_count, skipped_count, error_count = 0, 0, 0
        
        attachments_base_folder_id = find_or_create_drive_folder("Task_Attachments", GOOGLE_DRIVE_FOLDER_ID)
        if not attachments_base_folder_id:
            flash('ไม่สามารถสร้างหรือค้นหาโฟลเดอร์หลัก "Task_Attachments" ได้', 'danger')
            return redirect(url_for('organize_files'))

        unorganized_files_query = (
            f"'{attachments_base_folder_id}' in parents "
            f"and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
        )
        uncategorized_folder_id = find_or_create_drive_folder("Uncategorized", attachments_base_folder_id)
        if uncategorized_folder_id:
            unorganized_files_query += (
                f" or '{uncategorized_folder_id}' in parents "
                f"and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
            )

        all_files_in_base_and_uncategorized = []
        try:
            page_token = None
            while True:
                response = _execute_google_api_call_with_retry(
                    service.files().list,
                    q=unorganized_files_query,
                    spaces='drive',
                    fields='nextPageToken, files(id, name, parents)',
                    pageSize=100,
                    pageToken=page_token
                )
                all_files_in_base_and_uncategorized.extend(response.get('files', []))
                page_token = response.get('nextPageToken', None)
                if not page_token:
                    break
        except HttpError as e:
            app.logger.error(f"Error listing files for organization: {e}")
            flash('เกิดข้อผิดพลาดในการดึงรายการไฟล์จาก Google Drive', 'danger')
            return redirect(url_for('organize_files'))

        file_parents_map = {f['id']: f.get('parents', []) for f in all_files_in_base_and_uncategorized}

        task_folder_map = {}
        for task in all_tasks:
            try:
                created_dt_local = None
                if task.get('created'):
                    created_dt_local = date_parse(task.get('created')).astimezone(THAILAND_TZ)
                else:
                    app.logger.warning(f"Task {task.get('id')} has no 'created' date. Using current date for monthly folder naming.")
                    created_dt_local = datetime.datetime.now(THAILAND_TZ)

                monthly_folder_name = created_dt_local.strftime('%Y-%m')
                monthly_folder_id = find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)
                
                if not monthly_folder_id:
                    app.logger.error(f"Could not create/find monthly folder {monthly_folder_name} for task {task.get('id')}.")
                    continue

                customer_info = parse_customer_info_from_notes(task.get('notes', ''))
                sanitized_customer_name = sanitize_filename(customer_info.get('name', 'Unknown_Customer'))
                customer_task_folder_name = f"{sanitized_customer_name} - {task.get('id')}"
                
                destination_folder_id = find_or_create_drive_folder(customer_task_folder_name, monthly_folder_id)
                if destination_folder_id:
                    task_folder_map[task.get('id')] = destination_folder_id
                else:
                    app.logger.error(f"Could not create/find task folder {customer_task_folder_name} for task {task.get('id')}.")

            except Exception as e:
                app.logger.error(f"Error processing task {task.get('id')} for folder mapping: {e}")

        for file_item in all_files_in_base_and_uncategorized:
            file_id = file_item.get('id')
            file_name = file_item.get('name', 'Unnamed File')
            current_parents = file_parents_map.get(file_id, [])

            matched_task_id = None
            for task_id_candidate in task_folder_map.keys():
                if task_id_candidate in file_name:
                    matched_task_id = task_id_candidate
                    break
            
            expected_folder_id = None
            if matched_task_id and task_folder_map.get(matched_task_id):
                expected_folder_id = task_folder_map[matched_task_id]
            else:
                for task in all_tasks:
                    history, _ = parse_tech_report_from_notes(task.get('notes', ''))
                    for report in history:
                        for attachment in report.get('attachments', []):
                            if attachment.get('id') == file_id:
                                if task.get('id') in task_folder_map:
                                    expected_folder_id = task_folder_map[task.get('id')]
                                break
                        if expected_folder_id: break
                    if expected_folder_id: break

            if not expected_folder_id:
                app.logger.info(f"File {file_id} ('{file_name}') could not be linked to any task. Skipping.")
                skipped_count += 1
                continue

            if expected_folder_id in current_parents:
                skipped_count += 1
                app.logger.info(f"File {file_id} ('{file_name}') is already in the correct folder. Skipping.")
                continue
            
            try:
                parents_to_remove = [p for p in current_parents if p != expected_folder_id]
                
                _execute_google_api_call_with_retry(
                    service.files().update,
                    fileId=file_id,
                    addParents=expected_folder_id,
                    removeParents=",".join(parents_to_remove),
                    fields='id, parents'
                )
                moved_count += 1
                app.logger.info(f"Moved file {file_id} ('{file_name}') to folder {expected_folder_id}")

            except HttpError as file_error:
                if file_error.resp.status == 404:
                    app.logger.warning(f"File {file_id} ('{file_name}') not found on Drive during move, skipping. Error: {file_error}")
                    skipped_count += 1
                else:
                    app.logger.error(f"Error moving file {file_id} ('{file_name}'): {file_error}")
                    error_count += 1
            except Exception as file_other_error:
                app.logger.error(f"Unexpected error when processing file {file_id} ('{file_name}'): {file_other_error}")
                error_count += 1

        flash(f'การจัดระเบียบไฟล์เสร็จสิ้น! ย้ายสำเร็จ: {moved_count} ไฟล์, ข้าม (อยู่แล้ว/ไม่มีข้อมูล/ไม่พบ): {skipped_count} ไฟล์, เกิดข้อผิดพลาด: {error_count} ไฟล์.', 'success')
        return redirect(url_for('organize_files'))

    return render_template('organize_files.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=True)