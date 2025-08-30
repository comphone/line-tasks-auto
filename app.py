import os
from flask import Response
from io import BytesIO
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError
import requests
from flask_sqlalchemy import SQLAlchemy
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
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
import requests # เพิ่ม import requests สำหรับเรียก API ภายในแอปตัวเอง
import random
from PIL import Image

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, render_template, redirect, url_for, abort, flash, jsonify, Response, session, make_response, current_app
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect
from cachetools import cached, TTLCache
from geopy.distance import geodesic # สำหรับคำนวณระยะทาง
from urllib.parse import urlparse, parse_qs, unquote, quote_plus

from utils import (
    get_google_tasks_for_report, get_single_task, create_google_task, update_google_task, delete_google_task,
    parse_customer_info_from_notes, parse_tech_report_from_notes, parse_customer_feedback_from_notes,
    parse_google_task_dates, get_customer_database, get_technician_report_data, find_or_create_drive_folder,
    parse_assigned_technician_from_notes, parse_customer_profile_from_task, save_settings_to_file, get_app_settings,
    parse_task_data
)

import qrcode
import base64
from urllib.parse import quote_plus # สำหรับเข้ารหัส URL parameters
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    QuickReply,
    QuickReplyItem
)
from linebot.v3.messaging.models import (
    URIAction
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent, PostbackEvent,
    ImageMessageContent, FileMessageContent,
    GroupSource, UserSource, FollowEvent
)
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError

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

from flask_cors import CORS

SENTRY_DSN = os.environ.get('SENTRY_DSN')
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[
            FlaskIntegration(),
        ],
        traces_sample_rate=1.0,
        profiles_sample_rate=1.0
    )

app = Flask(__name__, static_folder='static')

if os.environ.get('RENDER') == 'true' and not os.environ.get('DATABASE_URL'):
    raise RuntimeError("FATAL: DATABASE_URL environment variable is not set on Render. Please create a PostgreSQL database and link it to this service.")

app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///data.sqlite')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True, 'pool_recycle': 280}

db = SQLAlchemy(app)

class JobItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_google_id = db.Column(db.String(100), nullable=False, index=True)
    item_name = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=1)
    unit_price = db.Column(db.Float, nullable=False, default=0)
    status = db.Column(db.String(50), nullable=False, default='pending')
    added_by = db.Column(db.String(100))
    added_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'task_google_id': self.task_google_id,
            'item_name': self.item_name,
            'quantity': self.quantity,
            'unit_price': self.unit_price,
            'status': self.status,
            'added_by': self.added_by,
            'added_at': self.added_at.isoformat()
        }

class BillingStatus(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_google_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    status = db.Column(db.String(50), nullable=False, default='pending_billing')
    billed_date = db.Column(db.DateTime)
    paid_date = db.Column(db.DateTime)
    # --- START: เพิ่มฟิลด์ใหม่ ---
    payment_due_date = db.Column(db.DateTime, nullable=True) # วันครบกำหนดชำระ
    # --- END: เพิ่มฟิลด์ใหม่ ---
    updated_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    def to_dict(self):
        return {
            'task_google_id': self.task_google_id,
            'status': self.status,
            'billed_date': self.billed_date.isoformat() if self.billed_date else None,
            'paid_date': self.paid_date.isoformat() if self.paid_date else None,
            # --- START: เพิ่มฟิลด์ใหม่ ---
            'payment_due_date': self.payment_due_date.isoformat() if self.payment_due_date else None
            # --- END: เพิ่มฟิลด์ใหม่ ---
        }

class Warehouse(db.Model):
    """ตารางสำหรับเก็บข้อมูลคลังสินค้าทั้งหมด (คลังหลัก, รถช่าง)"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False) # e.g., "คลังหลัก", "รถช่าง A"
    type = db.Column(db.String(50), nullable=False, default='main') # 'main' or 'technician_van'
    technician_name = db.Column(db.String(100), nullable=True) # ชื่อช่างที่ผูกกับคลังนี้
    is_active = db.Column(db.Boolean, default=True)

class StockLevel(db.Model):
    """ตารางสำหรับเก็บจำนวนสินค้าคงเหลือในแต่ละคลัง"""
    id = db.Column(db.Integer, primary_key=True)
    product_code = db.Column(db.String(100), nullable=False, index=True) # รหัสสินค้า
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=0) # รองรับทศนิยมและสต็อกติดลบ
    
    warehouse = db.relationship('Warehouse', backref=db.backref('stock_levels', lazy=True))
    __table_args__ = (db.UniqueConstraint('product_code', 'warehouse_id', name='_product_warehouse_uc'),)

class StockMovement(db.Model):
    """ตารางสำหรับบันทึกประวัติการเคลื่อนไหวของสต็อกทั้งหมด"""
    id = db.Column(db.Integer, primary_key=True)
    product_code = db.Column(db.String(100), nullable=False, index=True)
    quantity_change = db.Column(db.Float, nullable=False) # จำนวนที่เปลี่ยนแปลง (บวกคือรับเข้า, ลบคือจ่ายออก)
    from_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True)
    to_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True)
    movement_type = db.Column(db.String(50), nullable=False) # e.g., 'initial', 'transfer', 'sale', 'adjustment'
    job_item_id = db.Column(db.Integer, db.ForeignKey('job_item.id'), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    user = db.Column(db.String(100), nullable=True) # ผู้ทำรายการ
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

CORS(app) # --- เพิ่มบรรทัดนี้เพื่อเปิดใช้งาน CORS ---
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_development_only')
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
app.jinja_env.filters['dateutil_parse'] = date_parse
csrf = CSRFProtect(app)

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'kmz', 'kml'}
MAX_FILE_SIZE_MB = 500
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# Get the variable and remove leading/trailing whitespace
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
# If quotes exist after stripping, remove them and strip again
if LINE_CHANNEL_ACCESS_TOKEN.startswith('"') and LINE_CHANNEL_ACCESS_TOKEN.endswith('"'):
    LINE_CHANNEL_ACCESS_TOKEN = LINE_CHANNEL_ACCESS_TOKEN[1:-1].strip()

LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '').strip()
# VVVV เพิ่ม 2 บรรทัดนี้ VVVV
if LINE_CHANNEL_SECRET.startswith('"') and LINE_CHANNEL_SECRET.endswith('"'):
    LINE_CHANNEL_SECRET = LINE_CHANNEL_SECRET[1:-1].strip()

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    sys.exit("LINE Bot credentials are not set in environment variables.")

# Initialize LINE Bot API v3 objects once and reuse them globally
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration)
line_messaging_api = MessagingApi(api_client) # This object will be reused
handler = WebhookHandler(LINE_CHANNEL_SECRET)

app.logger.info(f"======== DEBUG LINE CREDENTIALS ========")
app.logger.info(f"Channel Secret configured: {bool(LINE_CHANNEL_SECRET)}")
app.logger.info(f"Secret Length: {len(LINE_CHANNEL_SECRET)}")
app.logger.info(f"Secret (masked): {'*' * (len(LINE_CHANNEL_SECRET) - 4) + LINE_CHANNEL_SECRET[-4:] if len(LINE_CHANNEL_SECRET) > 4 else '****'}")
app.logger.info(f"Access Token configured: {bool(LINE_CHANNEL_ACCESS_TOKEN)}")
app.logger.info(f"Access Token (masked): {'*' * (len(LINE_CHANNEL_ACCESS_TOKEN) - 6) + LINE_CHANNEL_ACCESS_TOKEN[-6:] if len(LINE_CHANNEL_ACCESS_TOKEN) > 6 else '****'}")

def check_line_bot_configuration():
    """ตรวจสอบการตั้งค่า LINE Bot"""
    issues = []
    
    if not LINE_CHANNEL_ACCESS_TOKEN:
        issues.append("LINE_CHANNEL_ACCESS_TOKEN ไม่ได้ตั้งค่า")
    elif len(LINE_CHANNEL_ACCESS_TOKEN) < 50:
        issues.append("LINE_CHANNEL_ACCESS_TOKEN อาจไม่ถูกต้อง (สั้นเกินไป)")
        
    if not LINE_CHANNEL_SECRET:
        issues.append("LINE_CHANNEL_SECRET ไม่ได้ตั้งค่า")
    elif len(LINE_CHANNEL_SECRET) != 32:
        issues.append(f"LINE_CHANNEL_SECRET ไม่ถูกต้อง (ความยาว: {len(LINE_CHANNEL_SECRET)}, ควรเป็น 32)")
        
    return issues     

# ตรวจสอบปัญหา
line_issues = check_line_bot_configuration()
if line_issues:
    app.logger.error("LINE Bot Configuration Issues:")
    for issue in line_issues:
        app.logger.error(f"  - {issue}")
else:
    app.logger.info("LINE Bot configuration looks good!")
    
LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM')
LIFF_ID_TECHNICIAN_LOCATION = os.environ.get('LIFF_ID_TECHNICIAN_LOCATION')
LIFF_ID_STOCK_VIEW = os.environ.get('LIFF_ID_STOCK_VIEW')
LINE_LOGIN_CHANNEL_ID = os.environ.get('LINE_LOGIN_CHANNEL_ID')
GOOGLE_TASKS_LIST_ID = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')

LINE_RATE_LIMIT_PER_MINUTE = int(os.environ.get('LINE_RATE_LIMIT_PER_MINUTE', 100))

if not GOOGLE_DRIVE_FOLDER_ID:
    app.logger.warning("GOOGLE_DRIVE_FOLDER_ID environment variable is not set. Drive upload will not work.")
if not LIFF_ID_FORM:
    app.logger.warning("LIFF_ID_FORM environment variable is not set. LIFF features will not work.")
if not LINE_LOGIN_CHANNEL_ID:
    app.logger.warning("LINE_LOGIN_CHANNEL_ID environment variable is not set. LIFF initialization might fail.")

SCOPES = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/calendar.events', 'https://www.googleapis.com/auth/drive.file']
THAILAND_TZ = pytz.timezone('Asia/Bangkok')
cache = TTLCache(maxsize=100, ttl=60)

scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)

#<editor-fold desc="Helper and Utility Functions">

class LineMessageQueue:
    def __init__(self, max_per_minute=100):
        self.queue = Queue()
        self.max_per_minute = max_per_minute
        self.sent_count = 0
        self.last_reset = time.time()
        self.processing_lock = threading.Lock()

    def add_message(self, user_id, messages):
        # Ensure messages is a list
        if not isinstance(messages, list):
            messages = [messages]
        self.queue.put((user_id, messages, time.time()))
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
                    user_id, messages, timestamp = self.queue.get()

                    if time.time() - timestamp > 300:
                        app.logger.warning(f"Discarding old message for {user_id} (queued {time.time() - timestamp:.2f}s ago).")
                        continue

                    try:
                        # Use v3 push message API
                        push_message_request = PushMessageRequest(
                            to=user_id,
                            messages=messages
                        )
                        
                        # Use the global line_messaging_api object
                        line_messaging_api.push_message(push_message_request) # <--- แก้ไขให้ย่อหน้าตรงกัน
                                                    
                        self.sent_count += 1
                        app.logger.info(f"Message sent to {user_id}. Sent count: {self.sent_count}/{self.max_per_minute}")
                    except Exception as e:
                        if hasattr(e, 'status') and e.status == 429:
                            app.logger.warning(f"LINE API Rate limit (429) hit while sending to {user_id}. Re-queuing message.")
                            self.queue.put((user_id, messages, timestamp))
                            time.sleep(5)
                        else:
                            app.logger.error(f"LINE API Error sending message to {user_id}: {e}")

            time.sleep(1)

message_queue = LineMessageQueue(max_per_minute=LINE_RATE_LIMIT_PER_MINUTE)
threading.Thread(target=message_queue.process_queue, daemon=True).start()
app.logger.info(f"LINE Message Queue started with a limit of {LINE_RATE_LIMIT_PER_MINUTE} messages/minute.")
  
def render_template_message(template_key, task):
    """
    ฟังก์ชันกลางสำหรับสร้างข้อความจาก Template โดยใช้ข้อมูลจาก Task
    """
    if not task:
        return ""
        
    settings = get_app_settings()
    template_str = settings.get('message_templates', {}).get(template_key, '')
    if not template_str:
        return f"ไม่พบ Template สำหรับ '{template_key}'"

    # ดึงข้อมูลที่ต้องใช้บ่อยๆ
    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    parsed_dates = parse_google_task_dates(task)
    shop_info = settings.get('shop_info', {})
    task_url = url_for('liff.task_details', task_id=task.get('id'), _external=True)

    # สร้าง Dictionary ของข้อมูลที่จะใช้แทนที่
    replacements = {
        '[customer_name]': customer_info.get('name', '-'),
        '[customer_phone]': customer_info.get('phone', '-'),
        '[customer_address]': customer_info.get('address', '-'),
        '[task_title]': task.get('title', '-'),
        '[due_date]': parsed_dates.get('due_formatted', '-'),
        '[map_url]': customer_info.get('map_url', '-'),
        '[shop_phone]': shop_info.get('contact_phone', '-'),
        '[shop_line_id]': shop_info.get('line_id', '-'),
        '[task_url]': task_url
    }

    # วนลูปเพื่อแทนที่ค่าทั้งหมด
    for placeholder, value in replacements.items():
        template_str = template_str.replace(placeholder, str(value))
        
    return template_str    

def save_app_settings(settings_data):
    """
    ฟังก์ชันกลางสำหรับบันทึกการตั้งค่าทั้งหมด
    มีการตรวจสอบข้อมูล Catalog ก่อนบันทึกเสมอ (เวอร์ชันแก้ไข)
    """
    current_settings = get_app_settings()

    for key, value in settings_data.items():
        if key == 'equipment_catalog' and isinstance(value, list):
            # ตรวจสอบและกรองข้อมูล Catalog ที่ส่งมาใหม่
            validated_catalog = []
            for item in value:
                if isinstance(item, dict) and item.get('item_name'):
                    try:
                        # ✅✅✅ START: โค้ดที่แก้ไข ✅✅✅
                        # แก้ไขส่วนนี้ให้บันทึกข้อมูลสินค้าครบทุกฟิลด์
                        new_item = {
                            'item_name': str(item['item_name']).strip(),
                            'category': str(item.get('category', 'ไม่มีหมวดหมู่')).strip(),
                            'product_code': str(item.get('product_code', '')).strip(),
                            'unit': str(item.get('unit', '')).strip(),
                            'price': float(item.get('price', 0)),
                            'cost_price': float(item.get('cost_price', 0)),
                            'stock_quantity': int(item.get('stock_quantity', 0)),
                            'image_url': str(item.get('image_url', '')).strip()
                        }
                        # ✅✅✅ END: โค้ดที่แก้ไข ✅✅✅
                        validated_catalog.append(new_item)
                    except (ValueError, TypeError):
                        current_app.logger.warning(f"Skipping invalid equipment item: {item}")
                        continue
            current_settings['equipment_catalog'] = validated_catalog

        elif key == 'product_categories' and isinstance(value, list):
            # ตรวจสอบและกรองข้อมูลหมวดหมู่ที่ส่งมา
            validated_categories = sorted(list(set([str(cat).strip() for cat in value if str(cat).strip()])))
            current_settings['product_categories'] = validated_categories

        elif isinstance(value, dict) and key in current_settings and isinstance(current_settings[key], dict):
            current_settings[key].update(value)
        else:
            current_settings[key] = value

    return save_settings_to_file(current_settings)

def load_technician_locations():
    if not os.path.exists(LOCATIONS_FILE):
        return {}
    try:
        with open(LOCATIONS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def save_technician_locations(locations_data):
    try:
        with open(LOCATIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(locations_data, f, ensure_ascii=False, indent=4)
        return True
    except IOError:
        return False

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
    SERVICE_ACCOUNT_FILE_CONTENT = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    
    if SERVICE_ACCOUNT_FILE_CONTENT:
        try:
            info = json.loads(SERVICE_ACCOUNT_FILE_CONTENT)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=SCOPES
            )
            app.logger.info("✅ Loaded credentials from Service Account.")
            try:
                service = _execute_google_api_call_with_retry(build, api_name, api_version, credentials=creds)
                app.logger.info(f"✅ Successfully built {api_name} {api_version} service using Service Account")
                return service
            except Exception as e:
                app.logger.error(f"❌ Failed to build Google API service with Service Account: {e}")
                return None
        except Exception as e:
            app.logger.warning(f"Could not load Service Account from GOOGLE_SERVICE_ACCOUNT_JSON env var: {e}. Falling back to User Credentials.")
            creds = None

    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')
    if google_token_json_str:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(google_token_json_str), SCOPES)
            app.logger.info(f"Loaded credentials from environment. Valid: {creds.valid}")
            if hasattr(creds, 'expiry') and creds.expiry:
                app.logger.info(f"Token expires at: {creds.expiry}")
                time_left = creds.expiry - datetime.datetime.utcnow()
                app.logger.info(f"Time left: {time_left}")
        except Exception as e:
            app.logger.warning(f"Could not load token from GOOGLE_TOKEN_JSON env var: {e}")
            creds = None
    if creds:
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    app.logger.info("Token expired, attempting refresh...")
                    creds.refresh(Request())
                    try:
                        backup_token = {
                            'token': creds.token, 'refresh_token': creds.refresh_token,
                            'token_uri': creds.token_uri, 'client_id': creds.client_id,
                            'client_secret': creds.client_secret, 'scopes': creds.scopes,
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
                    app.logger.info(f"NEW TOKEN: {creds.to_json()}")
                    app.logger.info("="*80)
                except Exception as e:
                    app.logger.error(f"❌ Error refreshing token: {e}")
                    app.logger.error("🔧 Please run get_token.py to generate a new token")
                    creds = None
            else:
                app.logger.error("❌ Token invalid and cannot be refreshed (no refresh_token)")
                app.logger.error("🔧 Please run get_token.py to generate a new token")
                creds = None
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

GOOGLE_CUSTOM_SEARCH_API_KEY = os.environ.get('GOOGLE_CUSTOM_SEARCH_API_KEY')
GOOGLE_CUSTOM_SEARCH_CX = os.environ.get('GOOGLE_CUSTOM_SEARCH_CX')

@app.route('/api/search_images')
def api_search_images():
    """API สำหรับค้นหารูปภาพจาก Google Custom Search"""
    query = request.args.get('q')
    if not query:
        return jsonify({'status': 'error', 'message': 'ต้องระบุคำค้นหา'}), 400

    if not GOOGLE_CUSTOM_SEARCH_API_KEY or not GOOGLE_CUSTOM_SEARCH_CX:
        app.logger.error("Google Custom Search API credentials are not set.")
        return jsonify({'status': 'error', 'message': 'การตั้งค่า API ไม่สมบูรณ์'}), 500

    try:
        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            'q': query,
            'cx': GOOGLE_CUSTOM_SEARCH_CX,
            'key': GOOGLE_CUSTOM_SEARCH_API_KEY,
            'searchType': 'image',
            'num': 8 # ดึงมา 8 รูปภาพ
        }
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        
        image_urls = [item['link'] for item in data.get('items', [])]
        return jsonify({'status': 'success', 'images': image_urls})

    except requests.exceptions.RequestException as e:
        app.logger.error(f"Error calling Google Custom Search API: {e}")
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการเชื่อมต่อกับ Google'}), 500
    except Exception as e:
        app.logger.error(f"Unexpected error in api_search_images: {e}")
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดภายในเซิร์ฟเวอร์'}), 500

@app.route('/api/search-equipment-catalog')
def api_search_equipment_catalog():
    """API สำหรับค้นหาอุปกรณ์จาก Catalog (Autocomplete)"""
    query = request.args.get('q', '').strip().lower()
    if len(query) < 2:
        return jsonify([])
    
    settings = get_app_settings()
    catalog = settings.get('equipment_catalog', [])
    
    # ค้นหา item ที่มีชื่อตรงกับ query
    results = [
        item for item in catalog 
        if query in item.get('item_name', '').lower()
    ][:10] # แสดงผลสูงสุด 10 รายการ
    
    return jsonify(results)

@app.route('/api/proxy_drive_image/<file_id>')
def proxy_drive_image(file_id):
    """
    Acts as a proxy to download a file from Google Drive.
    This is necessary to avoid CORS issues when using the Web Share API.
    """
    service = get_google_drive_service()
    if not service:
        return "Google Drive service not available", 500

    try:
        request = service.files().get_media(fileId=file_id)
        fh = BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
        
        fh.seek(0)
        
        # พยายามหา MimeType จาก Google Drive ก่อน
        try:
            file_metadata = service.files().get(fileId=file_id, fields='mimeType').execute()
            mime_type = file_metadata.get('mimeType', 'application/octet-stream')
        except Exception:
            mime_type = 'application/octet-stream'

        return Response(fh.getvalue(), mimetype=mime_type)

    except HttpError as e:
        if e.resp.status == 404:
            return "File not found on Google Drive", 404
        app.logger.error(f"Google Drive API error for file {file_id}: {e}")
        return "Error accessing file on Google Drive", 500
    except Exception as e:
        app.logger.error(f"Unexpected error in proxy for file {file_id}: {e}")
        return "Internal server error", 500
       
@app.route('/api/tasks/create', methods=['POST'])
def api_create_task():
    """
    API for creating a new job. Handles different job types (product repair vs. service).
    """
    try:
        customer_name = str(request.form.get('customer_name', '')).strip()
        task_title = str(request.form.get('task_title', '')).strip()
        job_type = str(request.form.get('job_type', 'service')) # 'product' or 'service'

        if not customer_name or not task_title:
            return jsonify({'status': 'error', 'message': 'กรุณากรอกชื่อผู้ติดต่อและรายละเอียดงาน'}), 400

        # Search for existing customer
        all_tasks = get_google_tasks_for_report(show_completed=True) or []
        customer_id_from_form = str(request.form.get('customer_id', '')).strip()
        
        customer_task = None
        if customer_id_from_form:
             customer_task = next((t for t in all_tasks if t.get('id') == customer_id_from_form), None)
        if not customer_task:
            customer_task = next((t for t in all_tasks if t.get('title', '').strip().lower() == customer_name.lower()), None)
        
        # Prepare new job data
        new_job = {
            'job_id': f"JOB-{uuid.uuid4().hex[:8].upper()}",
            'job_title': task_title,
            'job_type': job_type, # <-- เพิ่มประเภทงาน
            'created_date': datetime.datetime.now(pytz.utc).isoformat(),
            'status': 'needsAction',
            'service_items': [],
            'reports': [],
            'expenses': []
        }

        # --- START: โค้ดที่เพิ่มใหม่สำหรับบันทึกรายละเอียดสินค้า ---
        if job_type == 'product':
            new_job['product_details'] = {
                'type': str(request.form.get('product_type', '')).strip(),
                'brand': str(request.form.get('product_brand', '')).strip(),
                'model': str(request.form.get('product_model', '')).strip(),
                'serial_number': str(request.form.get('product_sn', '')).strip(),
                'accessories': str(request.form.get('product_accessories', '')).strip()
            }
        # --- END: โค้ดที่เพิ่มใหม่ ---

        appointment_str = str(request.form.get('appointment', '')).strip()
        if appointment_str:
            dt_local = THAILAND_TZ.localize(date_parse(appointment_str))
            new_job['due_date'] = dt_local.astimezone(pytz.utc).isoformat()

        if customer_task:
            # Existing Customer
            customer_task_id = customer_task['id']
            profile_data = parse_customer_profile_from_task(customer_task)
            profile_data['jobs'].append(new_job)
            
            # Update customer info if it has changed
            profile_data['customer_info'].update({
                'phone': str(request.form.get('phone', '')).strip(),
                'address': str(request.form.get('address', '')).strip(),
                'organization': str(request.form.get('organization_name', '')).strip(),
                'map_url': str(request.form.get('latitude_longitude', '')).strip()
            })

            updated_task = save_customer_profile_to_task(customer_task_id, profile_data)
            if not updated_task:
                 return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการเพิ่มใบงานใหม่ให้ลูกค้าเดิม'}), 500
        else:
            # New Customer
            customer_info = {
                'name': customer_name,
                'phone': str(request.form.get('phone', '')).strip(),
                'address': str(request.form.get('address', '')).strip(),
                'organization': str(request.form.get('organization_name', '')).strip(),
                'map_url': str(request.form.get('latitude_longitude', '')).strip()
            }
            profile_data = {'customer_info': customer_info, 'jobs': [new_job]}
            notes_json = json.dumps(profile_data, ensure_ascii=False, indent=2)
            
            new_customer_task = create_google_task(title=customer_name, notes=notes_json)
            if not new_customer_task:
                return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการสร้างโปรไฟล์ลูกค้าใหม่'}), 500
            customer_task_id = new_customer_task['id']

        cache.clear()
        
        return jsonify({
            'status': 'success', 
            'message': 'สร้างใบงานใหม่เรียบร้อยแล้ว!', 
            'redirect_url': url_for('liff.customer_profile', customer_task_id=customer_task_id)
        })

    except Exception as e:
        current_app.logger.error(f"Error in api_create_task: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดภายในเซิร์ฟเวอร์'}), 500


@app.route('/api/external_tasks/create', methods=['POST'])
def api_create_external_task():
    """API for creating a new external/claim job within the customer profile system."""
    try:
        customer_name = str(request.form.get('customer_name', '')).strip()
        task_title_raw = str(request.form.get('task_title', '')).strip()
        external_partner = str(request.form.get('external_partner', '')).strip()

        if not customer_name or not task_title_raw:
            return jsonify({'status': 'error', 'message': 'กรุณากรอกชื่อผู้ติดต่อและรายละเอียดงาน'}), 400

        # Add a prefix to identify this job type
        job_title = f"[งานภายนอก/เคลม] {task_title_raw}"

        # Find existing customer or prepare to create a new one
        all_tasks = get_google_tasks_for_report(show_completed=True) or []
        customer_task = next((t for t in all_tasks if t.get('title', '').strip().lower() == customer_name.lower()), None)
        
        customer_task_id = None
        profile_data = None

        # Prepare the new job data
        new_job = {
            'job_id': f"JOB-EXT-{uuid.uuid4().hex[:6].upper()}",
            'job_title': job_title,
            'created_date': datetime.datetime.now(pytz.utc).isoformat(),
            'status': 'needsAction',
            'external_partner': external_partner, # Store the external partner info
            'service_items': [],
            'reports': [],
            'expenses': []
        }
        
        return_date_str = str(request.form.get('return_date', '')).strip()
        if return_date_str:
            dt_local = THAILAND_TZ.localize(date_parse(f"{return_date_str}T09:00:00"))
            new_job['due_date'] = dt_local.astimezone(pytz.utc).isoformat()

        if customer_task:
            # --- Add Job to Existing Customer ---
            customer_task_id = customer_task['id']
            profile_data = parse_customer_profile_from_task(customer_task)
            profile_data['jobs'].append(new_job)
            
            # Update customer info just in case it was changed on the form
            profile_data['customer_info']['phone'] = str(request.form.get('phone', profile_data['customer_info'].get('phone', ''))).strip()
            profile_data['customer_info']['address'] = str(request.form.get('address', profile_data['customer_info'].get('address', ''))).strip()
            profile_data['customer_info']['organization'] = str(request.form.get('organization_name', profile_data['customer_info'].get('organization', ''))).strip()

            if not save_customer_profile_to_task(customer_task_id, profile_data):
                return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการเพิ่มงานภายนอกให้ลูกค้าเดิม'}), 500

        else:
            # --- Create New Customer Profile with this External Job ---
            customer_info = {
                'name': customer_name,
                'phone': str(request.form.get('phone', '')).strip(),
                'address': str(request.form.get('address', '')).strip(),
                'organization': str(request.form.get('organization_name', '')).strip(),
                'map_url': '' # External jobs might not have a map URL initially
            }
            profile_data = { 'customer_info': customer_info, 'jobs': [new_job] }
            notes_json = json.dumps(profile_data, ensure_ascii=False, indent=2)
            
            new_customer_task = create_google_task(title=customer_name, notes=notes_json)
            if not new_customer_task:
                return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการสร้างโปรไฟล์ลูกค้าใหม่สำหรับงานภายนอก'}), 500
            customer_task_id = new_customer_task['id']

        cache.clear()
        
        return jsonify({
            'status': 'success', 
            'message': 'บันทึกงานภายนอก/งานเคลมเรียบร้อยแล้ว!', 
            'redirect_url': url_for('liff.customer_profile', customer_task_id=customer_task_id)
        })

    except Exception as e:
        current_app.logger.error(f"Error in api_create_external_task: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดภายในเซิร์ฟเวอร์'}), 500
    
@app.route('/api/customer/<customer_task_id>/job/<job_id>/add_internal_note', methods=['POST'])
def add_internal_note_to_job(customer_task_id, job_id):
    """
    (ปรับปรุง) API สำหรับเพิ่มบันทึกภายในสำหรับงานย่อยโดยเฉพาะ
    """
    data = request.json
    note_text = data.get('note_text', '').strip()
    user = data.get('user', 'Admin')

    if not note_text:
        return jsonify({'status': 'error', 'message': 'กรุณาพิมพ์ข้อความ'}), 400

    task_raw = get_single_task(customer_task_id)
    if not task_raw:
        return jsonify({'status': 'error', 'message': 'ไม่พบโปรไฟล์ลูกค้า'}), 404

    profile_data = parse_customer_profile_from_task(task_raw)
    
    job_to_update = next((job for job in profile_data.get('jobs', []) if job.get('job_id') == job_id), None)
    if not job_to_update:
        return jsonify({'status': 'error', 'message': 'ไม่พบใบงานที่ต้องการอัปเดต'}), 404
        
    new_note_data = {
        "user": user,
        "timestamp": datetime.datetime.now(THAILAND_TZ).isoformat(),
        "text": note_text
    }
    
    if 'internal_notes' not in job_to_update:
        job_to_update['internal_notes'] = []
        
    job_to_update['internal_notes'].append(new_note_data)

    final_notes = json.dumps(profile_data, ensure_ascii=False, indent=2)

    if update_google_task(customer_task_id, notes=final_notes):
        cache.clear()
        
        # ... (ส่วนการส่งแจ้งเตือนยังคงเหมือนเดิม แต่สามารถปรับให้รวม job_id ได้) ...

        return jsonify({
            'status': 'success', 
            'message': 'เพิ่มบันทึกภายในเรียบร้อยแล้ว',
            'new_note': new_note_data
        })
    else:
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกข้อมูล'}), 500

@app.route('/api/task/<task_id>/edit_main', methods=['POST'])
def api_edit_task_main(task_id):
    """API สำหรับแก้ไขข้อมูลหลักของงาน"""
    try:
        task_raw = get_single_task(task_id)
        if not task_raw:
            return jsonify({'status': 'error', 'message': 'ไม่พบงาน'}), 404

        new_title = str(request.form.get('task_title', '')).strip()
        if not new_title:
            return jsonify({'status': 'error', 'message': 'กรุณากรอกรายละเอียดงาน'}), 400

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
                return jsonify({'status': 'error', 'message': 'รูปแบบวันเวลานัดหมายไม่ถูกต้อง'}), 400

        if update_google_task(task_id, title=new_title, notes=final_notes, due=due_date_gmt):
            cache.clear()
            return jsonify({'status': 'success', 'message': 'บันทึกข้อมูลหลักของงานเรียบร้อยแล้ว!', 'redirect_url': url_for('liff.summary')})
        else:
            return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกข้อมูลหลัก'}), 500
    except Exception as e:
        app.logger.error(f"Error in api_edit_task_main for task {task_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดภายในเซิร์ฟเวอร์'}), 500

def send_assignment_notification(task, technician_name, technician_line_id):
    """Sends a direct message to a technician about a new assignment."""
    if not technician_line_id:
        app.logger.warning(f"Cannot send assignment notification for task {task['id']}: Technician '{technician_name}' has no LINE User ID.")
        return

    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    parsed_dates = parse_google_task_dates(task)
    
    message_text = (
        f"🔔 คุณได้รับมอบหมายงานใหม่!\n\n"
        f"ชื่องาน: {task.get('title', '-')}\n"
        f"ลูกค้า: {customer_info.get('name', '-')}\n"
        f"📞 โทร: {customer_info.get('phone', '-')}\n"
        f"🗓️ นัดหมาย: {parsed_dates.get('due_formatted', '-')}\n\n"
        f"กรุณาตรวจสอบรายละเอียดและยืนยันการรับทราบ"
    )

    payload = {
        'recipient_line_id': technician_line_id,
        'notification_type': 'new_task', # ใช้ประเภทเดียวกับงานใหม่
        'task_id': task['id'],
        'custom_message': message_text
    }
    _send_popup_notification(payload)
    app.logger.info(f"Assignment notification for task {task['id']} queued for technician {technician_name} ({technician_line_id}).")

@app.route('/api/task/<task_id>/assign', methods=['POST'])
def api_assign_task(task_id):
    data = request.json
    technician_name = data.get('technician_name', '').strip()

    task_raw = get_single_task(task_id)
    if not task_raw:
        return jsonify({'status': 'error', 'message': 'ไม่พบงาน'}), 404

    notes = task_raw.get('notes', '')
    
    # ลบบรรทัด 'Assigned to:' เดิมออกก่อน (ถ้ามี)
    notes_lines = [line for line in notes.splitlines() if not re.match(r"^\s*Assigned to:", line, re.IGNORECASE)]
    
    # เพิ่มบรรทัด 'Assigned to:' ใหม่ ถ้ามีชื่อช่างส่งมา
    if technician_name:
        notes_lines.append(f"Assigned to: {technician_name}")

    final_notes = "\n".join(notes_lines)

    if update_google_task(task_id, notes=final_notes):
        cache.clear()

        # ส่งการแจ้งเตือนถ้ามีการมอบหมายงานใหม่
        if technician_name:
            settings = get_app_settings()
            technician_list = settings.get('technician_list', [])
            technician_line_id = None
            for tech in technician_list:
                if tech.get('name') == technician_name:
                    technician_line_id = tech.get('line_user_id')
                    break
            
            if technician_line_id:
                send_assignment_notification(task_raw, technician_name, technician_line_id)
                message = f'มอบหมายงานให้ {technician_name} และส่งแจ้งเตือนแล้ว'
            else:
                message = f'มอบหมายงานให้ {technician_name} สำเร็จ (แต่ไม่พบ LINE ID สำหรับแจ้งเตือน)'
        else:
            message = 'ยกเลิกการมอบหมายงานสำเร็จ'

        return jsonify({
            'status': 'success', 
            'message': message,
            'technician_name': technician_name or None
        })
    else:
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกข้อมูล'}), 500

def parse_internal_notes_from_notes(notes):
    """Parses internal notes from the task notes field."""
    if not notes:
        return []
    
    # ใช้ re.findall เพื่อดึงข้อความทั้งหมดที่อยู่ระหว่าง block --- INTERNAL_NOTE_START ---
    pattern = r"--- INTERNAL_NOTE_START ---\s*\n(.*?)\n--- INTERNAL_NOTE_END ---"
    matches = re.findall(pattern, notes, re.DOTALL)
    
    # แยกแต่ละ note ออกจากกัน โดยคาดว่าจะมี metadata (เช่น timestamp, user)
    parsed_notes = []
    for match in matches:
        try:
            # สมมติว่ารูปแบบคือ JSON string
            note_data = json.loads(match.strip())
            parsed_notes.append(note_data)
        except json.JSONDecodeError:
            # สำหรับ note แบบเก่าที่เป็นแค่ข้อความธรรมดา
            parsed_notes.append({'text': match.strip(), 'user': 'Unknown', 'timestamp': 'N/A'})
            
    # เรียงลำดับจากใหม่ไปเก่า
    parsed_notes.sort(key=lambda x: x.get('timestamp', '0'), reverse=True)
    return parsed_notes

@app.route('/api/task/<task_id>/add_internal_note', methods=['POST'])
def add_internal_note(task_id):
    data = request.json
    note_text = data.get('note_text', '').strip()
    user = data.get('user', 'Admin')

    if not note_text:
        return jsonify({'status': 'error', 'message': 'กรุณาพิมพ์ข้อความ'}), 400

    task_raw = get_single_task(task_id)
    if not task_raw:
        return jsonify({'status': 'error', 'message': 'ไม่พบงาน'}), 404

    current_notes = task_raw.get('notes', '')
    
    new_note_data = {
        "user": user,
        "timestamp": datetime.datetime.now(THAILAND_TZ).isoformat(),
        "text": note_text,
        "is_internal": True # เพิ่ม Flag เพื่อระบุว่าเป็น Internal Note
    }
    new_note_block = f"\n\n--- TECH_REPORT_START ---\n{json.dumps(new_note_data, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"

    final_notes = current_notes + new_note_block

    if update_google_task(task_id, notes=final_notes):
        cache.clear()
        
        # --- START: โค้ดสำหรับส่งแจ้งเตือน ---
        try:
            settings = get_app_settings()
            customer_info = parse_customer_info_from_notes(task_raw.get('notes', ''))
            task_url = url_for('liff.customer_profile', customer_task_id=task_id, _external=True)

            notification_message = (
                f"💬 ข้อความภายใน (งาน: {task_raw.get('title', '-')})\n"
                f"👤 ลูกค้า: {customer_info.get('name', 'N/A')}\n"
                f"🗣️ โดย: {user}\n"
                f"📝 ข้อความ: {note_text}\n\n"
                f"🔗 เปิดดูที่นี่: {task_url}"
            )
            
            # 1. ส่งเข้ากลุ่มแอดมิน
            admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')
            if admin_group_id:
                message_queue.add_message(admin_group_id, TextMessage(text=notification_message))

            # 2. ส่งหาช่างที่รับผิดชอบ
            assigned_technician = parse_assigned_technician_from_notes(task_raw.get('notes', ''))
            if assigned_technician:
                tech_list = settings.get('technician_list', [])
                tech_info = next((tech for tech in tech_list if tech.get('name') == assigned_technician), None)
                if tech_info and tech_info.get('line_user_id'):
                    # ไม่ส่งแจ้งเตือนกลับหาคนที่ส่งข้อความมาเอง
                    liff_user_id = data.get('liff_user_id', '')
                    if not liff_user_id or liff_user_id != tech_info.get('line_user_id'):
                         message_queue.add_message(tech_info['line_user_id'], TextMessage(text=notification_message))

        except Exception as e:
            current_app.logger.error(f"Failed to send internal note notification for task {task_id}: {e}")
        # --- END: โค้ดสำหรับส่งแจ้งเตือน ---

        return jsonify({
            'status': 'success', 
            'message': 'เพิ่มบันทึกภายในเรียบร้อยแล้ว',
            'new_note': new_note_data
        })
    else:
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกข้อมูล'}), 500

@app.route('/admin/token_status')
def token_status():
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


@app.route('/debug/liff')
def debug_liff():
    """ตรวจสอบการตั้งค่า LIFF IDs"""
    return jsonify({
        'LIFF_ID_FORM': LIFF_ID_FORM,
        'LIFF_ID_TECHNICIAN_LOCATION': LIFF_ID_TECHNICIAN_LOCATION,
        'environment_configured': bool(LIFF_ID_FORM),
        'liff_form_length': len(LIFF_ID_FORM) if LIFF_ID_FORM else 0,
        'liff_tech_length': len(LIFF_ID_TECHNICIAN_LOCATION) if LIFF_ID_TECHNICIAN_LOCATION else 0,
        'debug_info': {
            'liff_form_value': LIFF_ID_FORM[:10] + '...' if LIFF_ID_FORM and len(LIFF_ID_FORM) > 10 else LIFF_ID_FORM,
            'liff_tech_value': LIFF_ID_TECHNICIAN_LOCATION[:10] + '...' if LIFF_ID_TECHNICIAN_LOCATION and len(LIFF_ID_TECHNICIAN_LOCATION) > 10 else LIFF_ID_TECHNICIAN_LOCATION
        }
    })

@app.route('/admin/environment_check')
def environment_check():
    """ตรวจสอบ Environment Variables ทั้งหมด"""
    return jsonify({
        'LINE_CHANNEL_ACCESS_TOKEN': bool(LINE_CHANNEL_ACCESS_TOKEN),
        'LINE_CHANNEL_SECRET': bool(LINE_CHANNEL_SECRET),
        'LIFF_ID_FORM': LIFF_ID_FORM,
        'LIFF_ID_TECHNICIAN_LOCATION': LIFF_ID_TECHNICIAN_LOCATION,
        'LINE_LOGIN_CHANNEL_ID': LINE_LOGIN_CHANNEL_ID,
        'all_required_configured': all([
            LINE_CHANNEL_ACCESS_TOKEN, 
            LINE_CHANNEL_SECRET, 
            LIFF_ID_FORM
        ]),
        'missing_variables': [
            var for var, value in {
                'LINE_CHANNEL_ACCESS_TOKEN': LINE_CHANNEL_ACCESS_TOKEN,
                'LINE_CHANNEL_SECRET': LINE_CHANNEL_SECRET,
                'LIFF_ID_FORM': LIFF_ID_FORM,
                'LIFF_ID_TECHNICIAN_LOCATION': LIFF_ID_TECHNICIAN_LOCATION
            }.items() if not value
        ]
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
    
    # NEW LOGIC: Search for the folder by name first, regardless of parent
    query = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    try:
        response = _execute_google_api_call_with_retry(service.files().list, q=query, spaces='drive', fields='files(id, name, parents)', pageSize=1)
        files = response.get('files', [])
        
        if files:
            # If folder(s) with this name exist, return the first one found.
            folder_id = files[0]['id']
            app.logger.info(f"Found existing Drive folder '{name}' with ID: {folder_id}. Using this as the master.")
            return folder_id
        else:
            # If no folder with this name exists anywhere, create it under the intended parent.
            if not parent_id:
                app.logger.error(f"Cannot create folder '{name}': parent_id is missing.")
                return None
            app.logger.info(f"Folder '{name}' not found. Creating it under parent '{parent_id}'...")
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
            task.pop('due', None)
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

def save_customer_profile_to_task(task_id, profile_data):
    """
    ฟังก์ชันเสริมสำหรับแปลงข้อมูลโปรไฟล์เป็น JSON และบันทึกลง Google Task
    """
    try:
        # แปลง Python dictionary ของโปรไฟล์ให้เป็น JSON string ที่จัดรูปแบบสวยงาม
        final_notes = json.dumps(profile_data, ensure_ascii=False, indent=2)
        
        # เรียกใช้ฟังก์ชัน update_google_task เพื่อบันทึกข้อมูล notes ที่อัปเดตแล้ว
        return update_google_task(task_id, notes=final_notes)
        
    except Exception as e:
        current_app.logger.error(f"Error saving customer profile for task {task_id}: {e}", exc_info=True)
        return None

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
            message_queue.add_message(
                admin_group_id, 
                [TextMessage(text=f"‼️ เกิดข้อผิดพลาดร้ายแรงในระบบ ‼️\n\n{message[:900]}")]
            )
    except Exception as e:
        app.logger.error(f"Failed to add critical error notification to queue: {e}")

def _create_liff_notification_flex_message(recipient_line_id, notification_type, task_id, message_text,
                                          customer_info, shop_info, logo_url, liff_base_url,
                                          technician_name=None, distance_km=None, public_report_url=None):
    
    # --- ✅ แก้ไข Indentation Error และปรับปรุง URL ให้สั้นลง ---
    # โค้ดทั้งหมดในฟังก์ชันนี้ถูกย่อหน้าให้ถูกต้องแล้ว
    full_liff_url = f"{liff_base_url}?type={notification_type}&task_id={task_id}"

    # ส่วนนี้กำหนดข้อความต่างๆ ตามประเภทการแจ้งเตือน
    alt_text_map = {
        'new_task': f"งานใหม่: {message_text[:30]}...",
        'arrival': f"ช่างจะถึง: {message_text[:30]}...",
        'completion': f"งานเสร็จ: {message_text[:30]}...",
        'nearby_job': f"งานใกล้: {message_text[:30]}...",
        'update': f"อัปเดตงาน: {message_text[:30]}..."
    }
    button_label_map = {
        'new_task': "เปิดดูรายละเอียดงาน",
        'arrival': "ดูแผนที่/โทรหาลูกค้า",
        'completion': "ดูรายงานสรุปงาน",
        'nearby_job': "ดูรายละเอียดงาน/ติดต่อลูกค้า",
        'update': "เปิดดูรายละเอียดงาน"
    }
    title_map = {
        'new_task': "✨ มีงานใหม่เข้า!",
        'arrival': "🔔 ช่างกำลังจะถึง!",
        'completion': "✅ งานเสร็จเรียบร้อย!",
        'nearby_job': "📍 มีงานใกล้เคียง!",
        'update': "🗓️ อัปเดตงาน!"
    }

    # สร้าง Flex Message ในรูปแบบ Dictionary ที่ถูกต้องสำหรับ v3
    flex_json_payload = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {
                    "type": "text", "text": title_map.get(notification_type, "แจ้งเตือน"),
                    "weight": "bold", "size": "lg", "color": "#1DB446", "align": "center"
                },
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": message_text, "wrap": True, "size": "md"},
                {"type": "separator", "margin": "lg"},
                {
                    "type": "button", "style": "primary", "height": "sm", "color": "#007bff",
                    "action": {
                        "type": "uri",
                        "label": button_label_map.get(notification_type, "เปิดดูรายละเอียด"),
                        "uri": full_liff_url
                    }
                }
            ]
        }
    }
    
    return FlexMessage(
        alt_text=alt_text_map.get(notification_type, "แจ้งเตือนจากระบบ"),
        contents=flex_json_payload
    )

def _send_popup_notification(payload):
    """Internal helper function to build and queue a LIFF popup notification."""
    recipient_line_id = payload.get('recipient_line_id')
    notification_type = payload.get('notification_type')
    task_id = payload.get('task_id')
    
    if not recipient_line_id or not notification_type:
        app.logger.error(f"Popup notification missing recipient or type in payload: {payload}")
        return False

    try:
        settings = get_app_settings()
        popup_settings = settings.get('popup_notifications', {})
        liff_base_url = popup_settings.get('liff_popup_base_url')

        if not liff_base_url:
            app.logger.error("LIFF popup URL not configured in settings. Skipping popup notification.")
            return False

        task = get_single_task(task_id) if task_id and task_id != 'test-task-id' else None
        customer_info = parse_customer_info_from_notes(task.get('notes', '')) if task else {}
        shop_info = settings.get('shop_info', {})
        logo_url = url_for('static', filename='logo.png', _external=True)

        # Get message text from payload or generate it
        message_text = payload.get('custom_message', '')
        if not message_text:
            template_str = popup_settings.get(f'message_{notification_type}_template', '')
            if notification_type == 'arrival':
                message_text = template_str.replace('[technician_name]', payload.get('technician_name', '-')) \
                                           .replace('[customer_name]', customer_info.get('name', '-'))
            elif notification_type == 'completion':
                message_text = template_str.replace('[task_title]', task.get('title', '-')) \
                                           .replace('[customer_name]', customer_info.get('name', '-'))
            elif notification_type == 'nearby_job':
                 message_text = template_str.replace('[task_title]', task.get('title', '-')) \
                                           .replace('[distance_km]', f"{payload.get('distance_km', 0):.1f}") \
                                           .replace('[customer_name]', customer_info.get('name', '-'))

        if not message_text:
            app.logger.warning(f"Could not generate message for notification type {notification_type}")
            return False

        flex_message = _create_liff_notification_flex_message(
            recipient_line_id, notification_type, task_id, message_text,
            customer_info, shop_info, logo_url, liff_base_url,
            technician_name=payload.get('technician_name'),
            distance_km=payload.get('distance_km'),
            public_report_url=payload.get('public_report_url')
        )

        message_queue.add_message(recipient_line_id, flex_message)
        app.logger.info(f"Internal popup notification '{notification_type}' queued for {recipient_line_id}.")
        return True
    except Exception as e:
        app.logger.error(f"Error in _send_popup_notification: {e}", exc_info=True)
        return False

@app.route('/api/trigger_mobile_popup_notification', methods=['POST'])
def api_trigger_mobile_popup_notification():
    if _send_popup_notification(request.json):
        return jsonify({'status': 'success', 'message': 'ส่งการแจ้งเตือนแล้ว'}), 200
    else:
        return jsonify({'status': 'error', 'message': 'ไม่สามารถส่งการแจ้งเตือนได้'}), 500

def send_new_task_notification(task):
    settings = get_app_settings()
    recipients = settings.get('line_recipients', {})
    admin_group_id = recipients.get('admin_group_id')
    
    if not admin_group_id: return

    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    parsed_dates = parse_google_task_dates(task)
    
    message_text = (
        f"✨ มีงานใหม่เข้า!\n\n"
        f"ชื่องาน: {task.get('title', '-')}\n"
        f"ลูกค้า: {customer_info.get('name', '-')}\n"
        f"📞 โทร: {customer_info.get('phone', '-')}\n"
        f"🗓️ นัดหมาย: {parsed_dates.get('due_formatted', '-')}\n"
        f"📍 พิกัด: {customer_info.get('map_url', '-')}\n\n"
    )
    
    payload = {
        'recipient_line_id': admin_group_id,
        'notification_type': 'new_task',
        'task_id': task['id'],
        'custom_message': message_text
    }
    
    _send_popup_notification(payload)

def send_completion_notification(task, technicians, attachments=[]):
    settings = get_app_settings()
    recipients = settings.get('line_recipients', {})
    admin_group_id = recipients.get('admin_group_id')
    tech_group_id = recipients.get('technician_group_id')
    customer_line_id_from_feedback = parse_customer_feedback_from_notes(task.get('notes', '')).get('customer_line_user_id')

    if not any([admin_group_id, tech_group_id, customer_line_id_from_feedback]): return

    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    technician_str = ", ".join(technicians) if technicians else "ไม่ได้ระบุ"
    public_report_url = url_for('liff.public_task_report', task_id=task.get('id'), _external=True)

    message_text_admin_tech = (
        f"✅ ปิดงานเรียบร้อย\n\n"
        f"ชื่องาน: {task.get('title', '-')}\n"
        f"ลูกค้า: {customer_info.get('name', '-')}\n"
        f"ช่างผู้รับผิดชอบ: {technician_str}\n\n"
    )

    # --- START: โค้ดที่เพิ่มเข้ามาสำหรับสร้างลิงก์รูปภาพ ---
    if attachments:
        image_links = [f"• [ดูรูปภาพ {i+1}]({att['url']})" for i, att in enumerate(attachments)]
        message_text_admin_tech += "📷 รูปภาพแนบ:\n" + "\n".join(image_links)
    # --- END: โค้ดที่เพิ่มเข้ามา ---

    sent_to = set()
    for recipient_id in [admin_group_id, tech_group_id]:
        if recipient_id and recipient_id not in sent_to:
            payload = {
                'recipient_line_id': recipient_id,
                'notification_type': 'completion',
                'task_id': task['id'],
                'custom_message': message_text_admin_tech,
                'public_report_url': public_report_url
            }
            _send_popup_notification(payload)
            sent_to.add(recipient_id)
    
    if customer_line_id_from_feedback and settings.get('popup_notifications', {}).get('enabled_completion_customer'):
        # (ส่วนนี้ไม่มีการเปลี่ยนแปลง)
        payload = {
            'recipient_line_id': customer_line_id_from_feedback,
            'notification_type': 'completion',
            'task_id': task['id'],
            'custom_message': settings.get('popup_notifications', {}).get('message_completion_customer_template', 'งาน [task_title] ที่บ้านคุณ [customer_name] เสร็จเรียบร้อยแล้วครับ/ค่ะ')\
                .replace('[task_title]', task.get('title', '-'))\
                .replace('[customer_name]', customer_info.get('name', '-')),
            'public_report_url': public_report_url
        }
        _send_popup_notification(payload)

def send_update_notification(task, new_due_date_str, reason, technicians, is_today, attachments=[]):
    settings = get_app_settings()
    admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')
    if not admin_group_id: return

    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    technician_str = ", ".join(technicians) if technicians else "ไม่ได้ระบุ"
    
    title_prefix = "🗓️ อัปเดตงาน" if is_today else "🗓️ เลื่อนนัดหมาย"

    # สร้างข้อความหลัก
    message_text = (
        f"{title_prefix}\n\n"
        f"ชื่องาน: {task.get('title', '-')}\n"
        f"ลูกค้า: {customer_info.get('name', '-')}\n"
        f"📞 โทร: {customer_info.get('phone', '-')}\n"
        f"นัดหมายใหม่: {new_due_date_str}\n"
        f"สรุป: {reason}\n"
        f"ช่าง: {technician_str}\n\n"
    )

    # --- START: โค้ดที่เพิ่มเข้ามาสำหรับสร้างลิงก์รูปภาพ ---
    if attachments:
        image_links = [f"• [ดูรูปภาพ {i+1}]({att['url']})" for i, att in enumerate(attachments)]
        message_text += "📷 รูปภาพแนบ:\n" + "\n".join(image_links)
    # --- END: โค้ดที่เพิ่มเข้ามา ---
    
    payload = {
        'recipient_line_id': admin_group_id,
        'notification_type': 'update',
        'task_id': task['id'],
        'custom_message': message_text
    }
    
    _send_popup_notification(payload)

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
        
def scheduled_overdue_check_job():
    """Scheduled job to automatically move billed tasks to overdue."""
    with app.app_context():
        current_app.logger.info("Running scheduled overdue check job...")
        now = datetime.datetime.utcnow()
        
        # ค้นหางานทั้งหมดที่สถานะเป็น "วางบิลแล้ว" และมี "วันครบกำหนดชำระ" ผ่านไปแล้ว
        overdue_records = BillingStatus.query.filter(
            BillingStatus.status == 'billed',
            BillingStatus.payment_due_date.isnot(None),
            BillingStatus.payment_due_date < now
        ).all()

        if overdue_records:
            for record in overdue_records:
                record.status = 'overdue'
                current_app.logger.info(f"Task {record.task_google_id} moved to overdue.")
            db.session.commit()
            current_app.logger.info(f"Updated {len(overdue_records)} tasks to overdue status.")        

def scheduled_overdue_check_job():
    """Scheduled job to automatically move billed tasks to overdue."""
    with app.app_context():
        current_app.logger.info("Running scheduled overdue check job...")
        now = datetime.datetime.utcnow()
        
        # ค้นหางานทั้งหมดที่สถานะเป็น "วางบิลแล้ว" และมี "วันครบกำหนดชำระ" ผ่านไปแล้ว
        overdue_records = BillingStatus.query.filter(
            BillingStatus.status == 'billed',
            BillingStatus.payment_due_date.isnot(None),
            BillingStatus.payment_due_date < now
        ).all()

        if overdue_records:
            for record in overdue_records:
                record.status = 'overdue'
                current_app.logger.info(f"Task {record.task_google_id} moved to overdue.")
            db.session.commit()
            current_app.logger.info(f"Updated {len(overdue_records)} tasks to overdue status.")
        
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

        upcoming_appointments.sort(key=lambda x: date_parse(x['due']) if x.get('due') else datetime.datetime.max.replace(tzinfo=pytz.utc))

        for task in upcoming_appointments:
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            parsed_dates = parse_google_task_dates(task)
            
            settings = get_app_settings()
            header_template = settings.get('message_templates', {}).get('daily_reminder_header', '')
            task_line_template = settings.get('message_templates', {}).get('daily_reminder_task_line', '')

            # (ส่วนหนึ่งของ scheduled_appointment_reminder_job)
            message_text = render_template_message('daily_reminder_task_line', task)
            
            liff_base_url = settings.get('popup_notifications', {}).get('liff_popup_base_url')
            
            sent_to = set()
            for recipient_id in [admin_group_id, technician_group_id]:
                if recipient_id and recipient_id not in sent_to:
                        if not liff_base_url:
                            message_queue.add_message(recipient_id, TextMessage(text=message_text + f"🔗 ดูรายละเอียด/แก้ไข:\n{url_for('liff.task_details', task_id=task.get('id'), _external=True)}"))
                        else:
                            payload = {
                                'recipient_line_id': recipient_id,
                                'notification_type': 'new_task',
                                'task_id': task['id'],
                                'custom_message': message_text
                            }
                            
                            _send_popup_notification(payload)
                        sent_to.add(recipient_id)

def _create_customer_follow_up_flex_message(task_id, task_title, customer_name):
    # สร้าง Flex Message ในรูปแบบ Dictionary ที่ถูกต้องสำหรับ v3
    flex_json_payload = {
        "type": "bubble",
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "text", "text": "สอบถามหลังการซ่อม", "weight": "bold", "size": "lg", "color": "#1DB446", "align": "center"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": f"เรียนคุณ {customer_name},", "size": "sm", "wrap": True},
                {"type": "text", "text": f"เกี่ยวกับงาน: {task_title}", "size": "sm", "wrap": True, "color": "#666666"},
                {"type": "separator", "margin": "lg"},
                {"type": "text", "text": "ไม่ทราบว่าหลังจากทีมงานของเราเข้าบริการแล้ว ทุกอย่างเรียบร้อยดีหรือไม่ครับ/คะ?", "size": "md", "wrap": True, "align": "center"},
                {
                    "type": "box", "layout": "vertical", "spacing": "sm", "margin": "md",
                    "contents": [
                        {
                            "type": "button", "style": "primary", "height": "sm", "color": "#28a745",
                            "action": {
                                "type": "postback", "label": "✅ งานเรียบร้อยดี", "data": f'action=customer_feedback&task_id={task_id}&feedback=ok',
                                "displayText": "ขอบคุณสำหรับคำยืนยันครับ/ค่ะ!"
                            }
                        },
                        {
                            "type": "button", "style": "secondary", "height": "sm", "color": "#dc3545",
                            "action": {
                                "type": "postback",
                                "label": "🚨 ยังมีปัญหาอยู่",
                                "data": f'action=customer_feedback&task_id={task_id}&feedback=problem',
                                "displayText": "ฉันยังมีปัญหาเกี่ยวกับงานนี้ ต้องการให้ทีมงานติดต่อกลับ"
                            }
                        }
                    ] # <--- ✅ จุดที่แก้ไข SyntaxError
                }
            ]
        }
    }
    
    return FlexMessage(
        alt_text="สอบถามความพึงพอใจหลังการซ่อม",
        contents=flex_json_payload
    )

def scheduled_customer_follow_up_job():
    with app.app_context():
        app.logger.info("Running scheduled customer follow-up job...")
        settings = get_app_settings()
        admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')

        tasks_raw = get_google_tasks_for_report(show_completed=True) or []
        now_utc = datetime.datetime.now(pytz.utc)
        # ตรวจสอบงานที่เสร็จสิ้นในช่วง 24-48 ชั่วโมงที่ผ่านมา
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
                            continue # ข้ามถ้าเคยส่งไปแล้ว

                        customer_info = parse_customer_info_from_notes(notes)
                        customer_line_id = feedback_data.get('customer_line_user_id')
                        
                        if not customer_line_id:
                            continue

                        # แก้ไขตรงนี้: _create_customer_follow_up_flex_message ส่งกลับมาเป็น FlexMessage อยู่แล้ว
                        flex_message = _create_customer_follow_up_flex_message(
                            task['id'], task['title'], customer_info.get('name', 'N/A'))

                        try:
                            message_queue.add_message(customer_line_id, flex_message)
                            app.logger.info(f"Follow-up message for task {task['id']} added to queue for customer {customer_line_id}.")
                            
                            # อัปเดต notes ใน task เพื่อบันทึกว่าได้ส่ง follow-up ไปแล้ว
                            feedback_data['follow_up_sent_date'] = datetime.datetime.now(THAILAND_TZ).isoformat()
                            history_reports, base = parse_tech_report_from_notes(notes)
                            
                            tech_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history_reports])
                            
                            new_notes = base.strip()
                            if tech_reports_text: new_notes += tech_reports_text
                            new_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
                            _execute_google_api_call_with_retry(update_google_task, task['id'], notes=new_notes)
                            cache.clear()

                        except Exception as e:
                            app.logger.error(f"Failed to add direct follow-up to {customer_line_id} to queue: {e}. Notifying admin.")
                            if admin_group_id:
                                # สร้างข้อความแจ้งเตือนแอดมิน
                                admin_notification_messages = [
                                    TextMessage(text=f"⚠️ ส่ง Follow-up ให้ลูกค้า {customer_info.get('name')} (Task ID: {task['id']}) ไม่สำเร็จ โปรดส่งข้อความนี้แทน:"),
                                    flex_message
                                ]
                                message_queue.add_message(admin_group_id, admin_notification_messages)

                except Exception as e:
                    app.logger.warning(f"Could not process task {task.get('id')} for follow-up: {e}", exc_info=True)

def scheduled_nearby_job_alert_job():
    with app.app_context():
        app.logger.info("Running scheduled nearby job alert job...")
        
        settings = get_app_settings()  # <--- ✅ เพิ่มบรรทัดนี้ที่นี่
        
        popup_settings = settings.get('popup_notifications', {})
        if not popup_settings.get('enabled_nearby_job'):
            app.logger.info("Nearby job alert is disabled. Skipping.")
            return

        nearby_radius_km = popup_settings.get('nearby_radius_km', 5)
        
        technician_list = settings.get('technician_list', [])
        locations = load_technician_locations()

        active_technicians = []
        for tech in technician_list:
            if tech.get('line_user_id') in locations:
                tech_location = locations[tech['line_user_id']]
                tech['last_known_lat'] = tech_location.get('lat')
                tech['last_known_lon'] = tech_location.get('lon')
                active_technicians.append(tech)

        tasks_raw = get_google_tasks_for_report(show_completed=False) or []
        pending_tasks = [
            t for t in tasks_raw
            if t.get('status') == 'needsAction' and t.get('notes')
        ]

        for technician in active_technicians:
            tech_coords = (technician['last_known_lat'], technician['last_known_lon'])
            for task in pending_tasks:
                customer_info = parse_customer_info_from_notes(task.get('notes', ''))
                customer_map_url = customer_info.get('map_url')
                
                task_notes = task.get('notes', '')
                if f"NEARBY_ALERT_SENT_TO_TECH_{technician['line_user_id']}" in task_notes:
                    continue

                if customer_map_url and ('maps.google.com' in customer_map_url or re.match(r"^\-?\d+\.\d+,\s*\-?\d+\.\d+)", customer_map_url)):
                    match = re.search(r"(\-?\d+\.\d+),(\-?\d+\.\d+)", customer_map_url)
                    if match:
                        task_lat, task_lon = float(match.group(1)), float(match.group(2))
                        task_coords = (task_lat, task_lon)
                        
                        try:
                            distance_km = geodesic(tech_coords, task_coords).km
                            if distance_km <= nearby_radius_km:
                                payload = {
                                    'recipient_line_id': technician['line_user_id'],
                                    'notification_type': 'nearby_job',
                                    'task_id': task['id'],
                                    'technician_name': technician['name'],
                                    'distance_km': distance_km,
                                    'customer_name': customer_info.get('name', ''),
                                    'customer_phone': customer_info.get('phone', ''),
                                    'customer_address': customer_info.get('address', ''),
                                    'customer_map_url': customer_info.get('map_url', ''),
                                    'shop_phone': settings.get('shop_info', {}).get('contact_phone', ''),
                                    'logo_url': url_for('static', filename='logo.png', _external=True)
                                }
                                if _send_popup_notification(payload):
                                    app.logger.info(f"Nearby job notification triggered for technician {technician['name']} (Task: {task['id']}). Distance: {distance_km:.1f} km.")
                                    
                                    # อัปเดต notes ใน task เพื่อไม่ให้แจ้งเตือนซ้ำ
                                    current_notes = task.get('notes', '')
                                    new_notes = f"{current_notes}\nNEARBY_ALERT_SENT_TO_TECH_{technician['line_user_id']}_{datetime.datetime.now(THAILAND_TZ).isoformat()}"
                                    _execute_google_api_call_with_retry(update_google_task, task['id'], notes=new_notes)
                                    cache.clear()
                                else:
                                    app.logger.error(f"Failed to trigger nearby job notification for {technician['name']} and task {task['id']}")
                                    
                        except Exception as e:
                            app.logger.warning(f"Could not calculate distance for task {task['id']} / technician {technician['name']}: {e}")
                        
        app.logger.info("Finished scheduled nearby job alert job.")

from liff_views import liff_bp
app.register_blueprint(liff_bp, url_prefix='/')

def run_scheduler():
    global scheduler

    settings = get_app_settings()

    if scheduler.running:
        app.logger.info("Scheduler already running, shutting down before reconfiguring...")
        scheduler.shutdown(wait=False)
    
    scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)

    # Job: Auto Backup to Google Drive
    ab = settings.get('auto_backup', {})
    if ab.get('enabled'):
        scheduler.add_job(scheduled_backup_job, CronTrigger(hour=ab.get('hour_thai', 2), minute=ab.get('minute_thai', 0)), id='auto_system_backup', replace_existing=True)
        app.logger.info(f"Scheduled auto backup for {ab.get('hour_thai', 2)}:{ab.get('minute_thai', 0)} Thai time.")
    else:
        if scheduler.get_job('auto_system_backup'):
            scheduler.remove_job('auto_system_backup')
            app.logger.info("Auto backup job disabled and removed.")

    # Job: Daily Reminders and Follow-ups
    rt = settings.get('report_times', {})
    scheduler.add_job(scheduled_appointment_reminder_job, CronTrigger(hour=rt.get('appointment_reminder_hour_thai', 7), minute=0), id='daily_appointment_reminder', replace_existing=True)
    scheduler.add_job(scheduled_customer_follow_up_job, CronTrigger(hour=rt.get('customer_followup_hour_thai', 9), minute=5), id='daily_customer_followup', replace_existing=True)
 
    # --- START: ✅ เพิ่ม Job ใหม่สำหรับ Kanban Board ---
    # Job: Check for Overdue Invoices (for Kanban Board Automation)
    # ทำงานทุกวันตอน 8 โมงเช้า เพื่อตรวจสอบบิลที่เกินกำหนดชำระ
    scheduler.add_job(scheduled_overdue_check_job, CronTrigger(hour=8, minute=0), id='daily_overdue_check', replace_existing=True)
    app.logger.info("Scheduled daily overdue invoice check for 08:00 Thai time.")
    # --- END: ✅ เพิ่ม Job ใหม่สำหรับ Kanban Board ---
 
    # Job: Nearby Job Alerts for Technicians
    popup_settings = settings.get('popup_notifications', {})
    if popup_settings.get('enabled_nearby_job'):
        scheduler.add_job(scheduled_nearby_job_alert_job, CronTrigger(minute='*/15'), id='nearby_job_alerts', replace_existing=True)
        app.logger.info(f"Scheduled nearby job alerts every 15 minutes.")
    else:
        if scheduler.get_job('nearby_job_alerts'):
            scheduler.remove_job('nearby_job_alerts')
            app.logger.info("Nearby job alerts disabled and removed.")

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

with app.app_context():
    load_settings_from_drive_on_startup()   

atexit.register(cleanup_scheduler)

@app.route('/api/customers')
def api_customers():
    customer_list = get_customer_database()
    return jsonify(customer_list)

@app.route('/api/search-customers')
def api_search_customers():
    """
    API Endpoint สำหรับค้นหาลูกค้าแบบ Real-time
    โดยดึงข้อมูลจาก get_customer_database() ที่มีอยู่แล้ว
    """
    try:
        query = request.args.get('q', '').strip().lower()
        
        if len(query) < 2:
            return jsonify([])

        # ดึงฐานข้อมูลลูกค้าทั้งหมด (จาก Cache หรือ Google Tasks)
        # ฟังก์ชันนี้คุณมีอยู่แล้วใน app.py
        all_customers = get_customer_database()

        # ทำการค้นหาในหน่วยความจำ (In-memory search)
        results = []
        for customer in all_customers:
            # สร้างข้อความสำหรับค้นหาจากชื่อและหน่วยงาน
            customer_name = customer.get('name', '') or ''
            customer_org = customer.get('organization', '') or ''
            searchable_text = f"{customer_name} {customer_org}".lower()
            
            if query in searchable_text:
                results.append(customer)
            
            # จำกัดผลลัพธ์เพื่อไม่ให้แสดงผลมากเกินไป
            if len(results) >= 10:
                break
        
        return jsonify(results)
    
    except Exception as e:
        app.logger.error(f"Error in api_search_customers: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/api/products', methods=['POST'])
def api_add_product():
    """API สำหรับเพิ่มสินค้าใหม่ลงในแคตตาล็อก (เพิ่ม category)"""
    data = request.json
    item_name = data.get('item_name', '').strip()
    if not item_name:
        return jsonify({'status': 'error', 'message': 'กรุณากรอกชื่อสินค้า'}), 400
    try:
        settings = get_app_settings()
        catalog = settings.get('equipment_catalog', [])
        if any(item.get('item_name', '').lower() == item_name.lower() for item in catalog):
            return jsonify({'status': 'error', 'message': 'มีสินค้านี้ในระบบแล้ว'}), 409
        new_item = {
            'item_name': item_name,
            'category': data.get('category', 'ไม่มีหมวดหมู่'), # <-- เพิ่มฟิลด์ใหม่
            'product_code': data.get('product_code', ''),
            'unit': data.get('unit', ''),
            'price': float(data.get('price', 0)),
            'cost_price': float(data.get('cost_price', 0)),
            'stock_quantity': int(data.get('stock_quantity', 0)),
            'image_url': data.get('image_url', '')
        }
        catalog.append(new_item)
        if save_app_settings({'equipment_catalog': catalog}):
            backup_settings_to_drive()
            return jsonify({'status': 'success', 'message': 'เพิ่มสินค้าใหม่สำเร็จ', 'item': new_item}), 201
        else:
            raise Exception("ไม่สามารถบันทึกการตั้งค่าได้")
    except (ValueError, TypeError):
        return jsonify({'status': 'error', 'message': 'ราคา, ราคาทุน และสต็อกต้องเป็นตัวเลขเท่านั้น'}), 400
    except Exception as e:
        app.logger.error(f"Error in api_add_product: {e}")
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดฝั่งเซิร์ฟเวอร์'}), 500

@app.route('/api/products/<int:item_index>', methods=['PUT'])
def api_update_product(item_index):
    """API สำหรับแก้ไขข้อมูลสินค้า (เพิ่ม category)"""
    data = request.json
    try:
        settings = get_app_settings()
        catalog = settings.get('equipment_catalog', [])
        if not (0 <= item_index < len(catalog)):
            return jsonify({'status': 'error', 'message': 'ไม่พบสินค้าที่ต้องการแก้ไข'}), 404
        
        catalog[item_index]['item_name'] = data.get('item_name', catalog[item_index]['item_name']).strip()
        catalog[item_index]['category'] = data.get('category', catalog[item_index].get('category', 'ไม่มีหมวดหมู่')) # <-- เพิ่มฟิลด์ใหม่
        catalog[item_index]['product_code'] = data.get('product_code', catalog[item_index].get('product_code', '')).strip()
        catalog[item_index]['unit'] = data.get('unit', catalog[item_index].get('unit', '')).strip()
        catalog[item_index]['price'] = float(data.get('price', catalog[item_index]['price']))
        catalog[item_index]['cost_price'] = float(data.get('cost_price', catalog[item_index].get('cost_price', 0)))
        catalog[item_index]['stock_quantity'] = int(data.get('stock_quantity', catalog[item_index]['stock_quantity']))
        catalog[item_index]['image_url'] = data.get('image_url', catalog[item_index].get('image_url', ''))

        if save_app_settings({'equipment_catalog': catalog}):
            backup_settings_to_drive()
            return jsonify({'status': 'success', 'message': 'อัปเดตข้อมูลสินค้าสำเร็จ', 'item': catalog[item_index]})
        else:
            raise Exception("ไม่สามารถบันทึกการตั้งค่าได้")
    except (ValueError, TypeError):
        return jsonify({'status': 'error', 'message': 'ราคา, ราคาทุน และสต็อกต้องเป็นตัวเลขเท่านั้น'}), 400
    except Exception as e:
        app.logger.error(f"Error in api_update_product: {e}")
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดฝั่งเซิร์ฟเวอร์'}), 500

@app.route('/api/products/<int:item_index>/adjust_stock', methods=['POST'])
def api_adjust_stock(item_index):
    """API สำหรับการปรับสต็อกด่วน (+/-)"""
    data = request.json
    change = data.get('change', 0) # รับค่า +1 หรือ -1

    try:
        settings = get_app_settings()
        catalog = settings.get('equipment_catalog', [])

        if not (0 <= item_index < len(catalog)):
            return jsonify({'status': 'error', 'message': 'ไม่พบสินค้า'}), 404

        # ปรับปรุงสต็อก (อนุญาตให้ติดลบได้)
        current_stock = int(catalog[item_index].get('stock_quantity', 0))
        new_stock = current_stock + change
        catalog[item_index]['stock_quantity'] = new_stock

        if save_app_settings({'equipment_catalog': catalog}):
            backup_settings_to_drive()
            # ส่วนนี้คือจุดที่จะเพิ่ม "การบันทึกประวัติ" ในอนาคต
            # log_stock_movement(product_name=catalog[item_index]['item_name'], change=change, new_quantity=new_stock, user='admin')
            return jsonify({'status': 'success', 'new_stock': new_stock})
        else:
            raise Exception("ไม่สามารถบันทึกการตั้งค่าได้")

    except Exception as e:
        app.logger.error(f"Error in api_adjust_stock: {e}")
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดฝั่งเซิร์ฟเวอร์'}), 500

@app.route('/api/products/<int:item_index>', methods=['DELETE'])
def api_delete_product(item_index):
    """API สำหรับลบสินค้าออกจากแคตตาล็อก"""
    try:
        settings = get_app_settings()
        catalog = settings.get('equipment_catalog', [])

        if not (0 <= item_index < len(catalog)):
            return jsonify({'status': 'error', 'message': 'ไม่พบสินค้าที่ต้องการลบ'}), 404

        deleted_item = catalog.pop(item_index)
        
        if save_app_settings({'equipment_catalog': catalog}):
            backup_settings_to_drive()
            return jsonify({'status': 'success', 'message': f"ลบสินค้า '{deleted_item['item_name']}' สำเร็จ"})
        else:
            raise Exception("ไม่สามารถบันทึกการตั้งค่าได้")
            
    except Exception as e:
        app.logger.error(f"Error in api_delete_product: {e}")
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดฝั่งเซิร์ฟเวอร์'}), 500

@app.route('/api/task/<task_id>/items', methods=['GET'])
def get_task_items(task_id):
    """API สำหรับดึงรายการอุปกรณ์ทั้งหมดของงานนั้นๆ"""
    items = JobItem.query.filter_by(task_google_id=task_id).order_by(JobItem.added_at.asc()).all()
    return jsonify([item.to_dict() for item in items])

@app.route('/api/task/<task_id>/items', methods=['POST'])
@csrf.exempt
def add_task_items(task_id):
    """
    (เวอร์ชันปรับปรุง) API สำหรับบันทึกรายการอุปกรณ์
    - ถ้ามีช่างรับผิดชอบ: ตัดสตอกจากคลังของช่าง
    - ถ้าไม่มีช่างรับผิดชอบ: ตัดสตอกจากคลังหลัก
    - **เพิ่มสินค้าใหม่เข้า Catalog โดยอัตโนมัติหากไม่พบ**
    """
    data = request.json
    items_data = data.get('items', [])
    
    try:
        task_raw = get_single_task(task_id)
        if not task_raw:
            return jsonify({'status': 'error', 'message': 'ไม่พบข้อมูลงาน'}), 404
        
        warehouse_to_use = None
        added_by_user = "Admin"
        assigned_technicians_str = parse_assigned_technician_from_notes(task_raw.get('notes', ''))
        
        if assigned_technicians_str:
            added_by_user = assigned_technicians_str
            assigned_technicians_set = {name.strip() for name in assigned_technicians_str.split(',')}
            all_van_warehouses = Warehouse.query.filter_by(type='technician_van', is_active=True).all()
            for wh in all_van_warehouses:
                if wh.technician_name:
                    warehouse_techs_set = {name.strip() for name in wh.technician_name.split(',')}
                    if not warehouse_techs_set.isdisjoint(assigned_technicians_set):
                        warehouse_to_use = wh
                        break
            if not warehouse_to_use:
                return jsonify({'status': 'error', 'message': 'ไม่พบคลังสินค้าสำหรับทีมช่าง'}), 404
        else:
            main_warehouse = Warehouse.query.filter_by(type='main', is_active=True).first()
            if main_warehouse:
                warehouse_to_use = main_warehouse
            else:
                return jsonify({'status': 'error', 'message': 'ไม่พบคลังสินค้าหลักสำหรับตัดสต็อก'}), 404

        warehouse_id_to_use = warehouse_to_use.id

        JobItem.query.filter_by(task_google_id=task_id).delete()
        old_movements = StockMovement.query.filter(StockMovement.notes.like(f"%Job:{task_id}%")).all()
        for movement in old_movements:
            stock_level = StockLevel.query.filter_by(product_code=movement.product_code, warehouse_id=movement.from_warehouse_id).first()
            if stock_level:
                stock_level.quantity += movement.quantity_change
            db.session.delete(movement)

        if items_data:
            settings = get_app_settings()
            catalog = settings.get('equipment_catalog', [])
            catalog_dict_by_name = {item['item_name'].lower(): item for item in catalog}
            catalog_changed = False

            for item_data in items_data:
                # --- START: ✅ โค้ดที่แก้ไข ---
                # ตรวจสอบว่ามีสินค้านี้ใน Catalog หรือไม่
                item_name_lower = item_data['item_name'].lower()
                if item_name_lower not in catalog_dict_by_name:
                    # ถ้าไม่มี ให้เพิ่มเข้าไปใหม่
                    new_product = {
                        'item_name': item_data['item_name'],
                        'category': 'ไม่มีหมวดหมู่',
                        'product_code': item_data['item_name'], # ใช้ชื่อเป็นรหัสชั่วคราว
                        'unit': 'ชิ้น',
                        'price': float(item_data.get('unit_price', 0)),
                        'cost_price': 0,
                        'stock_quantity': 0,
                        'image_url': ''
                    }
                    catalog.append(new_product)
                    catalog_dict_by_name[item_name_lower] = new_product
                    catalog_changed = True
                # --- END: ✅ โค้ดที่แก้ไข ---

                new_job_item = JobItem(
                    task_google_id=task_id, item_name=item_data['item_name'],
                    quantity=float(item_data['quantity']), unit_price=float(item_data.get('unit_price', 0)),
                    added_by=added_by_user
                )
                db.session.add(new_job_item)
                db.session.flush()

                product_code = catalog_dict_by_name.get(item_name_lower, {}).get('product_code', item_data['item_name'])
                quantity_used = float(item_data['quantity'])

                stock_level = StockLevel.query.filter_by(product_code=product_code, warehouse_id=warehouse_id_to_use).first()
                if not stock_level:
                    stock_level = StockLevel(product_code=product_code, warehouse_id=warehouse_id_to_use, quantity=0)
                    db.session.add(stock_level)
                
                stock_level.quantity -= quantity_used

                movement = StockMovement(
                    product_code=product_code, quantity_change=quantity_used,
                    from_warehouse_id=warehouse_id_to_use, to_warehouse_id=None,
                    movement_type='sale_consumption', job_item_id=new_job_item.id,
                    notes=f"Used in Job:{task_id}", user=added_by_user
                )
                db.session.add(movement)

            # ถ้ามีการเพิ่มสินค้าใหม่ ให้บันทึก settings.json
            if catalog_changed:
                save_app_settings({'equipment_catalog': catalog})
                backup_settings_to_drive()
        
        db.session.commit()
        return jsonify({'status': 'success', 'message': f'บันทึกรายการและตัดสตอกจากคลัง "{warehouse_to_use.name}" เรียบร้อยแล้ว'}), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving job items for task {task_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกรายการ'}), 500

@app.route('/api/items/use', methods=['POST'])
def api_use_items():
    """
    API สำหรับรับรายการเบิกใช้จากช่าง และทำการตัดสต็อก
    """
    data = request.json
    items_used = data.get('items', []) # รับรายการที่ใช้ไป
    if not items_used:
        return jsonify({'status': 'error', 'message': 'No items provided'}), 400

    try:
        settings = get_app_settings()
        catalog = settings.get('equipment_catalog', [])

        # สร้าง Dictionary เพื่อง่ายต่อการค้นหาและอัปเดต
        catalog_dict = {item['item_name']: item for item in catalog}

        # วนลูปตัดสต็อก
        for used_item in items_used:
            item_name = used_item.get('item_name')
            quantity_used = used_item.get('quantity', 0)

            if item_name in catalog_dict:
                # ลบจำนวนออกจากสต็อก (ป้องกันไม่ให้ติดลบ)
                current_stock = catalog_dict[item_name].get('stock_quantity', 0)
                catalog_dict[item_name]['stock_quantity'] = max(0, current_stock - quantity_used)

        # แปลง Dictionary กลับเป็น List แล้วบันทึก
        updated_catalog = list(catalog_dict.values())
        if save_app_settings({'equipment_catalog': updated_catalog}):
            return jsonify({'status': 'success', 'message': 'ตัดสต็อกเรียบร้อยแล้ว'})
        else:
            raise Exception("Failed to save updated catalog")

    except Exception as e:
        app.logger.error(f"Error processing stock deduction: {e}")
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการตัดสต็อก'}), 500
# --- END: เพิ่ม API ใหม่ ---

@app.route('/api/equipment_catalog')
def api_equipment_catalog():
    settings = get_app_settings()
    catalog = settings.get('equipment_catalog', [])
    return jsonify(catalog)

@app.route('/api/task_summary/<task_id>')
def api_task_summary(task_id):
    """
    API สำหรับให้ LIFF Popup ดึงข้อมูลงานแบบสรุป
    """
    task_raw = get_single_task(task_id)
    if not task_raw:
        return jsonify({'error': 'Task not found'}), 404
    
    task = parse_google_task_dates(task_raw)
    task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
    
    # ส่งคืนเฉพาะข้อมูลที่จำเป็น
    summary_data = {
        'id': task.get('id'),
        'title': task.get('title'),
        'due_formatted': task.get('due_formatted'),
        'customer': {
            'name': task['customer'].get('name'),
            'phone': task['customer'].get('phone'),
            'address': task['customer'].get('address'),
            'map_url': task['customer'].get('map_url')
        },
        'task_details_url': url_for('liff.customer_profile', customer_task_id=task_id, _external=True)
    }
    return jsonify(summary_data)

@app.route('/api/technician-location/update', methods=['POST'])
@csrf.exempt
def api_update_technician_location():
    data = request.json
    line_user_id = data.get('line_user_id')
    lat = data.get('latitude')
    lon = data.get('longitude')

    if not all([line_user_id, lat, lon]):
        return jsonify({'status': 'error', 'message': 'Missing required data.'}), 400

    try:
        # โหลดเฉพาะข้อมูลพิกัด
        locations = load_technician_locations()
        
        # อัปเดตข้อมูลพิกัดของช่างคนนั้นๆ
        locations[line_user_id] = {
            'lat': float(lat),
            'lon': float(lon),
            'timestamp': datetime.datetime.now(THAILAND_TZ).isoformat()
        }
        
        # บันทึกเฉพาะไฟล์พิกัดที่อัปเดตแล้ว
        if save_technician_locations(locations):
            app.logger.info(f"Successfully updated location for technician with LINE ID: {line_user_id}")
            return jsonify({'status': 'success', 'message': 'Location updated successfully.'})
        else:
            raise IOError("Failed to save locations file.")

    except (ValueError, TypeError) as e:
        return jsonify({'status': 'error', 'message': f'Invalid location data format: {e}'}), 400
    except Exception as e:
        app.logger.error(f"Error updating technician location: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'An internal server error occurred.'}), 500
                           
@app.route('/api/upload_attachment', methods=['POST'])
def api_upload_attachment():
    task_id = request.form.get('task_id')

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
            compressed_file, mime_type, filename = compress_image_to_fit(file, MAX_FILE_SIZE_BYTES)
            if compressed_file:
                file_to_upload = compressed_file
                app.logger.info(f"Compressed image '{file.filename}' successfully.")
            else:
                return jsonify({'status': 'error', 'message': f'ไฟล์รูปภาพใหญ่เกินไปและไม่สามารถบีบอัดให้มีขนาดต่ำกว่า {MAX_FILE_SIZE_MB}MB ได้'}), 413
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

def compress_image_to_fit(file, max_size_bytes):
    """Compress image to fit within max_size_bytes"""
    try:
        img = Image.open(file)
        
        # Convert RGBA/P to RGB
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        
        # Start with high quality
        quality = 95
        output_buffer = BytesIO()
        
        while quality >= 20:
            output_buffer = BytesIO()
            img.save(output_buffer, format='JPEG', quality=quality, optimize=True)
            
            if output_buffer.tell() <= max_size_bytes:
                output_buffer.seek(0)
                filename = os.path.splitext(file.filename)[0] + '.jpg'
                return output_buffer, 'image/jpeg', filename
            
            quality -= 5
            
        # If still too large, resize
        width, height = img.size
        while width > 800 or height > 800:
            width = int(width * 0.8)
            height = int(height * 0.8)
            img = img.resize((width, height), Image.Resampling.LANCZOS)
            
            output_buffer = BytesIO()
            img.save(output_buffer, format='JPEG', quality=70, optimize=True)
            
            if output_buffer.tell() <= max_size_bytes:
                output_buffer.seek(0)
                filename = os.path.splitext(file.filename)[0] + '.jpg'
                return output_buffer, 'image/jpeg', filename
                
        return None, None, None
        
    except Exception as e:
        app.logger.error(f"Error compressing image: {e}")
        return None, None, None

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
                'url': url_for('liff.task_details', task_id=task.get('id')),
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

@app.route('/api/task/<task_id>/update_location', methods=['POST'])
def api_update_task_location(task_id):
    data = request.json
    new_map_url = data.get('map_url')

    if not new_map_url:
        return jsonify({'status': 'error', 'message': 'ไม่พบข้อมูลพิกัดใหม่'}), 400

    task_raw = get_single_task(task_id)
    if not task_raw:
        return jsonify({'status': 'error', 'message': 'ไม่พบงานที่ต้องการอัปเดต'}), 404

    try:
        notes = task_raw.get('notes', '')
        history, base_notes_text = parse_tech_report_from_notes(notes)
        feedback_data = parse_customer_feedback_from_notes(notes)

        # Regex pattern to find an existing map URL or coordinates
        map_url_pattern = r"https?:\/\/[^\s]*google[^\s]*|(?:\-?\d+\.\d+,\s*\-?\d+\.\d+)"

        if re.search(map_url_pattern, base_notes_text):
            # If an old URL exists, replace it
            updated_base_notes = re.sub(map_url_pattern, new_map_url, base_notes_text)
        else:
            # If no old URL, append the new one
            updated_base_notes = base_notes_text.strip() + f"\n{new_map_url}"

        # Reconstruct the notes
        all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
        final_notes = updated_base_notes
        if all_reports_text: final_notes += all_reports_text
        if feedback_data: final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

        if update_google_task(task_id, notes=final_notes):
            cache.clear()
            return jsonify({'status': 'success', 'message': 'อัปเดตพิกัดเรียบร้อยแล้ว'})
        else:
            raise Exception("Failed to update Google Task.")

    except Exception as e:
        app.logger.error(f"Error updating task location for {task_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดฝั่งเซิร์ฟเวอร์'}), 500

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

@app.route('/api/task/<task_id>/delete_report/<int:report_index>', methods=['POST'])
def delete_task_report(task_id, report_index):
    task_raw = get_single_task(task_id)
    if not task_raw:
        return jsonify({'status': 'error', 'message': 'ไม่พบงานที่ต้องการอัปเดต'}), 404

    history, base_notes_text = parse_tech_report_from_notes(task_raw.get('notes', ''))
    feedback_data = parse_customer_feedback_from_notes(task_raw.get('notes', ''))

    if not (0 <= report_index < len(history)):
        return jsonify({'status': 'error', 'message': 'ไม่พบรายงานที่ต้องการลบ'}), 404

    # --- ส่วนลบไฟล์ใน Google Drive ---
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

    history.pop(report_index) # ลบรายงานออกจาก list

    # --- บันทึก Notes ที่อัปเดตแล้ว ---
    all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
    final_notes = base_notes_text
    if all_reports_text: final_notes += all_reports_text
    if feedback_data: final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

    if update_google_task(task_id, notes=final_notes):
        cache.clear()
        return jsonify({'status': 'success', 'message': 'ลบรายงานเรียบร้อยแล้ว'})
    else:
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกหลังลบรายงาน'}), 500

@app.route('/delete_task/<task_id>', methods=['POST'])
def delete_task(task_id):
    if delete_google_task(task_id):
        flash('ลบงานเรียบร้อยแล้ว!', 'success')
        cache.clear()
    else:
        flash('เกิดข้อผิดพลาดในการลบงาน', 'danger')
    return redirect(url_for('liff.summary'))

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

    deleted_count, failed_count = 0, 0
    for task_id in task_ids:
        if delete_google_task(task_id):
            deleted_count += 1
        else:
            failed_count += 1

    if deleted_count > 0:
        cache.clear()

    return jsonify({
        'status': 'success',
        'message': f'ลบงานสำเร็จ: {deleted_count} รายการ, ล้มเหลว: {failed_count} รายการ.',
        'deleted_count': deleted_count,
        'failed_count': failed_count
    })

@app.route('/api/update_tasks_status_batch', methods=['POST'])
def api_update_tasks_status_batch():
    data = request.json
    task_ids = data.get('task_ids', [])
    new_status = data.get('status')

    if not all([isinstance(task_ids, list), new_status in ['needsAction', 'completed']]):
        return jsonify({'status': 'error', 'message': 'Invalid input data.'}), 400

    updated_count, failed_count = 0, 0
    for task_id in task_ids:
        if update_google_task(task_id, status=new_status):
            updated_count += 1
        else:
            failed_count += 1

    if updated_count > 0:
        cache.clear()

    return jsonify({
        'status': 'success',
        'message': f'อัปเดตสถานะสำเร็จ: {updated_count} รายการ, ล้มเหลว: {failed_count} รายการ.',
        'updated_count': updated_count,
        'failed_count': failed_count
    })
    
@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        try:
            data = request.json
            if not data:
                return jsonify({'status': 'error', 'message': 'ไม่พบข้อมูลที่ส่งมา'}), 400

            # รับการตั้งค่าปัจจุบัน
            current_settings = get_app_settings()

            # ตรวจสอบและอัปเดตข้อมูลตาม key ที่ส่งมา
            if 'report_times' in data:
                current_settings['report_times'].update(data['report_times'])
            
            if 'message_templates' in data:
                current_settings['message_templates'].update(data['message_templates'])

            if 'popup_notifications' in data:
                pn_data = data['popup_notifications']
                for key in ['enabled_arrival', 'enabled_completion_customer', 'enabled_nearby_job']:
                    # ถ้า key ไม่มีมา (checkbox ไม่ได้ติ๊ก) ให้ตั้งเป็น False
                    current_settings['popup_notifications'][key] = bool(pn_data.get(key, False))
                
                # อัปเดตส่วนที่เหลือ
                current_settings['popup_notifications'].update({k: v for k, v in pn_data.items() if not isinstance(v, bool)})

            if 'line_recipients' in data:
                current_settings['line_recipients'].update(data['line_recipients'])

            if 'shop_info' in data:
                current_settings['shop_info'].update(data['shop_info'])

            if 'technician_list' in data:
                technician_list = data.get('technician_list', [])
                if isinstance(technician_list, list):
                    current_settings['technician_list'] = technician_list
                else:
                    return jsonify({'status': 'error', 'message': 'รูปแบบข้อมูลช่างไม่ถูกต้อง'}), 400           
           
            if 'auto_backup' in data:
                current_settings['auto_backup'].update(data['auto_backup'])

            if 'text_snippets' in data:
                templates_data = data.get('text_snippets', {})
                # Basic validation to ensure we're getting the expected structure
                if isinstance(templates_data, dict) and 'task_details' in templates_data and 'progress_reports' in templates_data:
                    current_settings['technician_templates'] = templates_data
                else:
                     return jsonify({'status': 'error', 'message': 'รูปแบบข้อมูลเทมเพลตไม่ถูกต้อง'}), 400

            if 'technician_templates' in data:
                templates_data = data.get('technician_templates', {})
                # ตรวจสอบโครงสร้างข้อมูลเบื้องต้น
                if isinstance(templates_data, dict) and 'task_details' in templates_data and 'progress_reports' in templates_data:
                    current_settings['technician_templates'] = templates_data
                else:
                     return jsonify({'status': 'error', 'message': 'รูปแบบข้อมูลเทมเพลตไม่ถูกต้อง'}), 400

            # บันทึกการตั้งค่าที่อัปเดตแล้ว
            if save_app_settings(current_settings):
                cache.clear()
                run_scheduler() # --- เพิ่มบรรทัดนี้เพื่อรีโหลด Scheduler ทันที ---
                backup_success = backup_settings_to_drive()
                message = 'บันทึกการตั้งค่าเรียบร้อยแล้ว'
                if not backup_success:
                    message += ' (แต่สำรองข้อมูลไปที่ Google Drive ไม่สำเร็จ)'
                return jsonify({'status': 'success', 'message': message})
            else:
                return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกการตั้งค่า'}), 500
        
        except Exception as e:
            app.logger.error(f"Error processing settings POST request: {e}", exc_info=True)
            return jsonify({'status': 'error', 'message': f'เกิดข้อผิดพลาดในระบบ: {str(e)}'}), 500
    
    # This part handles the initial page load (GET request)
    settings = get_app_settings()
    warehouses = Warehouse.query.order_by(Warehouse.id).all() # <-- เพิ่มบรรทัดนี้
    return render_template('settings_page.html',
                           settings=settings,
                           warehouses=warehouses) # <-- เพิ่ม warehouses เข้าไป  
 
@app.route('/api/upload_avatar', methods=['POST'])
def api_upload_avatar():
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file'}), 400

    # Use the new centralized helper function
    file_to_upload, filename, mime_type = _handle_image_upload(file, MAX_FILE_SIZE_MB)

    # Check if the helper function returned an error
    if not file_to_upload:
        error_message, error_code, _ = file_to_upload, filename, mime_type # This unpacking will now be safe
        return jsonify({'status': 'error', 'message': error_message}), error_code

    avatars_folder_id = find_or_create_drive_folder("Technician_Avatars", GOOGLE_DRIVE_FOLDER_ID)
    if not avatars_folder_id:
        return jsonify({'status': 'error', 'message': 'Could not create or find Technician_Avatars folder'}), 500

    media_body = MediaIoBaseUpload(file_to_upload, mimetype=mime_type, resumable=True)
    drive_file = _perform_drive_upload(media_body, filename, mime_type, avatars_folder_id)

    if drive_file:
        return jsonify({'status': 'success', 'file_id': drive_file.get('id'), 'url': drive_file.get('webViewLink')})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to upload avatar to Google Drive'}), 500

@app.after_request
def add_security_headers(response):
    # กำหนดให้สามารถโหลดรูปภาพจากโดเมนของ Google Drive และ Google User Content ได้
    response.headers['Content-Security-Policy'] = "img-src 'self' drive.google.com *.googleusercontent.com via.placeholder.com data:;"
    return response

def _handle_image_upload(file_storage, max_size_mb):
    """
    Handles image processing, compression, and validation.
    Returns (file_like_object, filename, mime_type) on success,
    or (error_message, error_code, None) on failure.
    """
    max_size_bytes = max_size_mb * 1024 * 1024
    original_filename = secure_filename(file_storage.filename)
    
    file_storage.seek(0, os.SEEK_END)
    file_length = file_storage.tell()
    file_storage.seek(0)

    if file_length > max_size_bytes:
        if file_storage.mimetype and file_storage.mimetype.startswith('image/'):
            try:
                img = Image.open(file_storage)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                
                output_buffer = BytesIO()
                # Try reducing quality first
                for quality in range(90, 20, -10):
                    output_buffer.seek(0)
                    output_buffer.truncate()
                    img.save(output_buffer, format='JPEG', quality=quality, optimize=True)
                    if output_buffer.tell() <= max_size_bytes:
                        output_buffer.seek(0)
                        filename = os.path.splitext(original_filename)[0] + '.jpg'
                        current_app.logger.info(f"Compressed image '{original_filename}' with quality={quality}.")
                        return output_buffer, filename, 'image/jpeg'
                
                # If still too large, resize
                width, height = img.size
                while output_buffer.tell() > max_size_bytes and (width > 800 or height > 800):
                    width = int(width * 0.9)
                    height = int(height * 0.9)
                    img = img.resize((width, height), Image.Resampling.LANCZOS)
                    output_buffer.seek(0)
                    output_buffer.truncate()
                    img.save(output_buffer, format='JPEG', quality=80, optimize=True)

                if output_buffer.tell() <= max_size_bytes:
                    output_buffer.seek(0)
                    filename = os.path.splitext(original_filename)[0] + '.jpg'
                    current_app.logger.info(f"Compressed and resized image '{original_filename}' to {width}x{height}.")
                    return output_buffer, filename, 'image/jpeg'
                
                return f'ไฟล์รูปภาพใหญ่เกินไปและไม่สามารถบีบอัดให้มีขนาดต่ำกว่า {max_size_mb}MB ได้', 413, None

            except Exception as e:
                current_app.logger.error(f"Error compressing image: {e}")
                return 'เกิดข้อผิดพลาดขณะบีบอัดรูปภาพ', 500, None
        else:
            return f'ไฟล์ใหญ่เกินขนาดที่กำหนด ({max_size_mb}MB)', 413, None
    else:
        # File is within size limit, return it as is
        return file_storage, original_filename, file_storage.mimetype or mimetypes.guess_type(original_filename)[0]

@app.route('/api/upload_product_image', methods=['POST'])
def api_upload_product_image():
    """API สำหรับอัปโหลดรูปภาพสินค้าโดยเฉพาะ"""
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file'}), 400

    # ตรวจสอบว่าเป็นไฟล์รูปภาพและบีบอัดถ้าจำเป็น
    if file.mimetype and file.mimetype.startswith('image/'):
        try:
            # บีบอัดรูปภาพให้มีคุณภาพเหมาะสมและขนาดไม่ใหญ่เกินไป
            img = Image.open(file)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            
            output_buffer = BytesIO()
            img.save(output_buffer, format='JPEG', quality=85, optimize=True)
            output_buffer.seek(0)
            
            file_to_upload = output_buffer
            filename = os.path.splitext(secure_filename(file.filename))[0] + '.jpg'
            mime_type = 'image/jpeg'
            app.logger.info(f"Compressed product image '{file.filename}' successfully.")

        except Exception as e:
            app.logger.error(f"Could not process product image '{file.filename}': {e}")
            return jsonify({'status': 'error', 'message': 'ไฟล์รูปภาพไม่ถูกต้อง'}), 400
    else:
        return jsonify({'status': 'error', 'message': 'รองรับเฉพาะไฟล์รูปภาพเท่านั้น'}), 400

    # สร้างหรือค้นหาโฟลเดอร์สำหรับรูปภาพสินค้าใน Google Drive
    product_images_folder_id = find_or_create_drive_folder("Product_Images", GOOGLE_DRIVE_FOLDER_ID)
    if not product_images_folder_id:
        return jsonify({'status': 'error', 'message': 'Could not create or find Product_Images folder in Drive'}), 500

    # อัปโหลดไฟล์ขึ้น Drive
    media_body = MediaIoBaseUpload(file_to_upload, mimetype=mime_type, resumable=True)
    drive_file = _perform_drive_upload(media_body, filename, mime_type, product_images_folder_id)
    
    if drive_file:
        # ส่งคืน ID ของไฟล์ที่อัปโหลดสำเร็จ
        return jsonify({'status': 'success', 'file_id': drive_file.get('id')})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to upload image to Google Drive'}), 500

@app.route('/api/upload_payment_qr', methods=['POST'])
def api_upload_payment_qr():
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file'}), 400
    
    # สร้างหรือค้นหาโฟลเดอร์สำหรับเก็บไฟล์ของบริษัท
    assets_folder_id = find_or_create_drive_folder("Company_Assets", GOOGLE_DRIVE_FOLDER_ID)
    if not assets_folder_id:
        return jsonify({'status': 'error', 'message': 'Could not create or find Company_Assets folder'}), 500

    # ใช้ชื่อไฟล์มาตรฐานเพื่อให้อัปเดตทับไฟล์เดิมได้ง่าย
    filename = "payment_qr_code.png"
    mime_type = file.mimetype or 'image/png'
    
    # อ่านไฟล์เข้า memory
    file_bytes = BytesIO(file.read())
    media_body = MediaIoBaseUpload(file_bytes, mimetype=mime_type, resumable=True)
    
    # อัปโหลดไฟล์ขึ้น Drive
    drive_file = _perform_drive_upload(media_body, filename, mime_type, assets_folder_id)
    
    if drive_file:
        return jsonify({'status': 'success', 'file_id': drive_file.get('id')})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to upload QR code to Google Drive'}), 500

@app.route('/test_notification', methods=['POST'])
def test_notification():
    recipient_id = request.form.get('test_recipient')
    test_type = request.form.get('test_type')
    
    if not recipient_id:
        flash('กรุณาระบุ ID ผู้รับ', 'danger')
        return redirect(url_for('settings_page'))

    try:
        if test_type == 'simple_text':
            message = request.form.get('test_message', '[ทดสอบ] ข้อความว่าง')
            message_queue.add_message(recipient_id, TextMessage(text=message))
        else:
            tasks = get_google_tasks_for_report(show_completed=True) or []
            if not tasks:
                flash('ไม่พบข้อมูลงานสำหรับใช้ทดสอบ', 'warning')
                return redirect(url_for('settings_page'))

            task_to_test = None
            if test_type in ['customer_completion', 'customer_follow_up']:
                completed_tasks = [t for t in tasks if t.get('status') == 'completed']
                if completed_tasks:
                    task_to_test = max(completed_tasks, key=lambda x: date_parse(x.get('completed', '')))
            
            if not task_to_test:
                task_to_test = tasks[0]

            if test_type == 'customer_completion':
                payload = {
                    'recipient_line_id': recipient_id, 'notification_type': 'completion',
                    'task_id': task_to_test['id'], 'public_report_url': url_for('public_task_report', task_id=task_to_test['id'], _external=True)
                }
                _send_popup_notification(payload)

            elif test_type == 'customer_follow_up':
                customer_info = parse_customer_info_from_notes(task_to_test.get('notes', ''))
                
                # ใช้ผลลัพธ์จากฟังก์ชันได้เลย เพราะเป็น FlexMessage อยู่แล้ว
                flex_message_to_send = _create_customer_follow_up_flex_message(
                    task_to_test['id'], 
                    task_to_test['title'], 
                    customer_info.get('name', 'N/A')
                )
                message_queue.add_message(recipient_id, flex_message_to_send)

            elif test_type == 'admin_new_task':
                send_new_task_notification(task_to_test) 

        flash(f'ส่งข้อความทดสอบประเภท "{test_type}" ไปยัง ID: {recipient_id} สำเร็จ!', 'success')
    except Exception as e:
        flash(f'เกิดข้อผิดพลาดในการส่ง: {e}', 'danger')
        app.logger.error(f"Error in test_notification: {e}", exc_info=True)
        
    return redirect(url_for('settings_page'))
# --- END of test_notification replacement ---

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

@app.route("/callback", methods=['POST'])
@csrf.exempt  # เพิ่มบรรทัดนี้เพื่อยกเว้น CSRF check
def callback():
    try:
        # Get X-Line-Signature header value
        signature = request.headers.get('X-Line-Signature')
        
        # เพิ่ม debug logging
        app.logger.info("=== DEBUG WEBHOOK START ===")
        app.logger.info(f"Request method: {request.method}")
        app.logger.info(f"Request headers: {dict(request.headers)}")
        app.logger.info(f"Content-Type: {request.content_type}")
        app.logger.info(f"Request URL: {request.url}")
        
        if not signature:
            app.logger.error("❌ Missing X-Line-Signature header")
            return 'Missing signature', 400

        # Get request body as bytes, NOT as text
        body = request.get_data()
        
        # เพิ่ม debug log เพื่อตรวจสอบ
        app.logger.info(f"📦 Received webhook request - Body length: {len(body)}")
        app.logger.info(f"🔑 Signature present: {bool(signature)}")
        app.logger.info(f"📄 Body content (first 500 chars): {body.decode('utf-8')[:500]}...")
        
        # ตรวจสอบว่า Channel Secret ถูกต้องหรือไม่
        if not LINE_CHANNEL_SECRET:
            app.logger.error("❌ LINE_CHANNEL_SECRET is not configured")
            return 'Channel secret not configured', 500
            
        app.logger.info(f"✅ Channel Secret configured: {bool(LINE_CHANNEL_SECRET)}")
        app.logger.info(f"📏 Channel Secret length: {len(LINE_CHANNEL_SECRET)}")
        
        # ตรวจสอบความยาวของ Channel Secret (ควรเป็น 32 ตัวอักษร)
        if len(LINE_CHANNEL_SECRET) != 32:
            app.logger.error(f"❌ Invalid Channel Secret length. Expected: 32, Got: {len(LINE_CHANNEL_SECRET)}")
            return 'Invalid channel secret length', 500

        try:
            # Handle webhook body
            # The handler needs the body as a decoded string (utf-8)
            app.logger.info("🔄 Attempting to handle webhook...")
            handler.handle(body.decode('utf-8'), signature)
            app.logger.info("✅ Webhook handled successfully")
            app.logger.info("=== DEBUG WEBHOOK END (SUCCESS) ===")
            return 'OK', 200
            
        except InvalidSignatureError as e:
            app.logger.error(f"❌ Invalid LINE signature: {e}")
            app.logger.error(f"🔑 Expected signature should be calculated from:")
            app.logger.error(f"   - Channel Secret: {'*' * (len(LINE_CHANNEL_SECRET) - 4) + LINE_CHANNEL_SECRET[-4:] if len(LINE_CHANNEL_SECRET) > 4 else '****'}")
            app.logger.error(f"   - Request body: {body.decode('utf-8')[:100]}...")
            app.logger.error(f"   - Received signature: {signature}")
            app.logger.error("=== DEBUG WEBHOOK END (SIGNATURE ERROR) ===")
            return 'Invalid signature', 400
            
        except Exception as e:
            app.logger.error(f"❌ Error handling LINE webhook event: {e}", exc_info=True)
            app.logger.error("=== DEBUG WEBHOOK END (HANDLER ERROR) ===")
            return 'Internal server error', 500
            
    except Exception as e:
        app.logger.error(f"❌ Unexpected error in callback: {e}", exc_info=True)
        app.logger.error("=== DEBUG WEBHOOK END (UNEXPECTED ERROR) ===")
        return 'Unexpected error', 500



def create_task_list_message(title, tasks, limit=5):
    if not tasks:
        return TextMessage(text=f"ไม่พบรายการ{title}ในขณะนี้")
    message = f"📋 {title}\n\n"
    tasks.sort(key=lambda x: date_parse(x['due']) if x.get('due') else datetime.datetime.max.replace(tzinfo=pytz.utc))
    for i, task in enumerate(tasks[:limit]):
        customer = parse_customer_info_from_notes(task.get('notes', ''))
        due = parse_google_task_dates(task).get('due_formatted', 'ไม่มีกำหนด')
        message += f"{i+1}. {task.get('title')}\n   - ลูกค้า: {customer.get('name', 'N/A')}\n   - นัดหมาย: {due}\n\n"
    if len(tasks) > limit: message += f"... และอีก {len(tasks) - limit} รายการ"
    return TextMessage(text=message)

def create_full_summary_message(title, tasks):
    if not tasks: return TextMessage(text=f"ไม่พบรายการ{title}ในขณะนี้")
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
    return TextMessage(text=message)

@handler.add(FollowEvent)
def handle_follow_event(event):
    # 1. ดึง User ID ของลูกค้าที่เพิ่งแอดเรามา
    user_id = event.source.user_id
    
    # 2. ตรวจสอบว่ามี Referral Code (รหัสงาน) แนบมาด้วยหรือไม่
    if hasattr(event, 'follow') and hasattr(event.follow, 'referral'):
        task_id = event.follow.referral
        app.logger.info(f"User {user_id} followed via referral link for task: {task_id}")

        # 3. บันทึก User ID ลงใน Google Task (เหมือนที่ save_customer_line_id เคยทำ)
        task = get_single_task(task_id)
        if task:
            notes = task.get('notes', '')
            feedback = parse_customer_feedback_from_notes(notes)
            
            feedback['customer_line_user_id'] = user_id
            feedback['id_saved_date'] = datetime.datetime.now(THAILAND_TZ).isoformat()

            reports_history, base = parse_tech_report_from_notes(notes)
            reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in reports_history])
            final_notes = f"{base.strip()}"
            if reports_text: final_notes += reports_text
            final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

            if update_google_task(task_id=task_id, notes=final_notes):
                cache.clear()
                
                # 4. ส่งข้อความต้อนรับและลิงก์รายงานซ่อม
                settings = get_app_settings()
                shop = settings.get('shop_info', {})
                customer = parse_customer_info_from_notes(notes)

                # (ส่วนหนึ่งของ handle_follow_event)
                welcome_message = render_template_message('welcome_customer', task)
                
                report_url = url_for('public_task_report', task_id=task_id, _external=True)
                report_message = f"คุณสามารถดูรายละเอียดและสถานะงานซ่อมของคุณได้ที่นี่:\n{report_url}"

                # ส่งข้อความทั้งหมดในครั้งเดียว
                message_queue.add_message(user_id, [
                    TextMessage(text=welcome_message),
                    TextMessage(text=report_message)
                ])
                app.logger.info(f"Welcome & Report Link messages queued for user {user_id}.")
    else:
        # กรณีที่แอดเพื่อนมาแบบปกติ (ไม่มี Referral)
        app.logger.info(f"User {user_id} followed without a referral.")
        # อาจจะส่งข้อความต้อนรับทั่วไปที่นี่

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    text = event.message.text.strip().lower()
    messages = []

    if text == 'myid':
        source = event.source
        reply_text = ""
        if isinstance(source, GroupSource):
            reply_text = f"✅ Group ID ของกลุ่มนี้คือ:\n{source.group_id}"
        elif isinstance(source, UserSource):
            reply_text = f"✅ User ID ของคุณคือ:\n{source.user_id}"
        else:
            reply_text = "คำสั่งนี้ใช้ได้ในแชทส่วนตัวหรือในกลุ่มเท่านั้น"

        if reply_text:
            messages.append(TextMessage(text=reply_text))
   
    elif text == 'งานวันนี้':
        tasks = [t for t in (get_google_tasks_for_report(False) or []) 
                if t.get('due') and date_parse(t['due']).astimezone(THAILAND_TZ).date() == datetime.datetime.now(THAILAND_TZ).date() 
                and t.get('status') == 'needsAction']
        
        if not tasks:
            messages = [TextMessage(text="ไม่พบงานสำหรับวันนี้")]
        else:
            tasks.sort(key=lambda x: date_parse(x['due']))
            for task in tasks[:5]:
                customer, dates = parse_customer_info_from_notes(task.get('notes', '')), parse_google_task_dates(task)
                loc = f"พิกัด: {customer.get('map_url')}" if customer.get('map_url') else "พิกัด: - (ไม่มีข้อมูล)"
                
                # --- ✅✅✅ START: โค้ดที่แก้ไข ✅✅✅ ---
                msg_text = (f"🔔 งานสำหรับวันนี้\n\n"
                           f"👤 ลูกค้า: {customer.get('name', '-')}\n"
                           f"📞 โทร: {customer.get('phone', '-')}\n"
                           f"ชื่องาน: {task.get('title', '-')}\n"
                           f"🗓️ นัดหมาย: {dates.get('due_formatted', '-')}\n"
                           f"📍 {loc}\n\n"
                           f"🔗 ดูรายละเอียด/แก้ไข:\n{url_for('liff.task_details', task_id=task.get('id'), _external=True)}")
                # --- ✅✅✅ END: โค้ดที่แก้ไข ✅✅✅ ---
                
                messages.append(TextMessage(text=msg_text))

    elif text == 'งานค้าง':
        tasks = [t for t in (get_google_tasks_for_report(False) or []) if t.get('status') == 'needsAction']
        messages = [create_full_summary_message('รายการงานค้าง', tasks)]
        
    elif text == 'งานเสร็จ':
        tasks = sorted([t for t in (get_google_tasks_for_report(True) or []) if t.get('status') == 'completed'], 
                      key=lambda x: date_parse(x.get('completed', '0001-01-01T00:00:00Z')), reverse=True)
        messages = [create_task_list_message('รายการงานเสร็จล่าสุด', tasks)]
        
    elif text == 'งานพรุ่งนี้':
        tasks = [t for t in (get_google_tasks_for_report(False) or []) 
                if t.get('due') and date_parse(t['due']).astimezone(THAILAND_TZ).date() == (datetime.datetime.now(THAILAND_TZ) + datetime.timedelta(days=1)).date() 
                and t.get('status') == 'needsAction']
        messages = [create_task_list_message('งานพรุ่งนี้', tasks)]
        
    elif text == 'สร้างงานใหม่' and LIFF_ID_FORM:
        quick_reply_items = [
            QuickReplyItem(
                action=URIAction(
                    label="เปิดฟอร์มสร้างงาน",
                    uri=f"https://liff.line.me/{LIFF_ID_FORM}"
                )
            )
        ]
        messages = [TextMessage(
            text="เปิดฟอร์มเพื่อสร้างงานใหม่ครับ 👇",
            quick_reply=QuickReply(items=quick_reply_items)
        )]
        
    elif text.startswith('ดูงาน '):
        query = event.message.text.split(maxsplit=1)[1].strip().lower()
        if not query:
            messages = [TextMessage(text="โปรดระบุชื่อลูกค้าที่ต้องการค้นหา")]
        else:
            tasks = [t for t in (get_google_tasks_for_report(True) or []) 
                    if query in parse_customer_info_from_notes(t.get('notes', '')).get('name', '').lower()]
            if not tasks:
                messages = [TextMessage(text=f"ไม่พบงานของลูกค้า: {query}")]
            else:
                tasks.sort(key=lambda x: (x.get('status') == 'completed', date_parse(x.get('due', '9999-12-31T23:59:59Z'))))
                bubbles = [create_task_flex_message(t) for t in tasks[:10]]
                flex_carousel = {
                    "type": "carousel",
                    "contents": bubbles
                }
                messages = [FlexMessage(
                    alt_text=f"ผลการค้นหา: {query}",
                    contents=flex_carousel
                )]
                
    elif text == 'comphone':
        help_text = (
            "พิมพ์คำสั่งเพื่อดูรายงานหรือจัดการงาน:\n"
            "- *งานค้าง*: ดูรายการงานที่ยังไม่เสร็จทั้งหมด\n"
            "- *งานเสร็จ*: ดูรายการงานที่ทำเสร็จแล้ว 5 รายการล่าสุด\n"
            "- *งานวันนี้*: ดูงานที่นัดหมายสำหรับวันนี้ (แยกข้อความ)\n"
            "- *งานพรุ่งนี้*: ดูสรุปงานที่นัดหมายสำหรับพรุ่งนี้\n"
            "- *สร้างงานใหม่*: เปิดฟอร์มสำหรับสร้างงานใหม่\n"
            "- *ดูงาน [ชื่อลูกค้า]*: ค้นหางานตามชื่อลูกค้า\n\n"
            f"ดูข้อมูลทั้งหมด: {url_for('liff.summary', _external=True)}"
        )
        messages = [TextMessage(text=help_text)]
    
    if messages:
        try:
            # Use the global line_messaging_api object
            line_messaging_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=messages
                )
            )
        except Exception as e:
            app.logger.error(f"Error replying to text message: {e}")

@handler.add(PostbackEvent)
def handle_postback(event):
    data = dict(x.split('=') for x in event.postback.data.split('&'))
    action = data.get('action')
    task_id = data.get('task_id')
    feedback_type = data.get('feedback') # รับค่า feedback (ok หรือ problem)

    if action == 'customer_feedback':
        task = get_single_task(task_id)
        if not task:
            return

        # --- บันทึก Feedback ลงใน Task Notes (เหมือนเดิม) ---
        notes = task.get('notes', '')
        feedback = parse_customer_feedback_from_notes(notes)
        feedback.update({
            'feedback_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
            'feedback_type': feedback_type,
            'customer_line_user_id': event.source.user_id
        })
        history_reports, base = parse_tech_report_from_notes(notes)
        reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history_reports])
        final_notes = f"{base.strip()}"
        if reports_text: final_notes += reports_text
        final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        _execute_google_api_call_with_retry(update_google_task, task_id, notes=final_notes)
        cache.clear()

        # --- ส่วนที่เพิ่มเข้ามา: ตรวจสอบและส่งแจ้งเตือน ---
        reply_text = "ขอบคุณสำหรับคำยืนยันครับ/ค่ะ 🙏" # ข้อความตอบกลับเริ่มต้น

        if feedback_type == 'problem':
            reply_text = "รับทราบปัญหาครับ/ค่ะ เดี๋ยวทีมงานจะรีบติดต่อกลับไปนะครับ/คะ"
            
            settings = get_app_settings()
            admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')
            if admin_group_id:
                # สร้างข้อความแจ้งเตือนแอดมินโดยใช้ Template ใหม่
                admin_message = render_template_message('problem_report_admin', task)
                # แทนที่ส่วนของรายละเอียดปัญหา
                admin_message = admin_message.replace('[problem_desc]', 'ลูกค้ากดปุ่มแจ้งว่ายังมีปัญหาอยู่')
                
                # ส่งเข้าคิวเพื่อแจ้งเตือน
                message_queue.add_message(admin_group_id, TextMessage(text=admin_message))
                app.logger.info(f"Problem report for task {task_id} sent to admin group.")

        # --- ตอบกลับลูกค้า (เหมือนเดิม) ---
        try:
            line_messaging_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
        except Exception as e:
            app.logger.warning(f"Could not send postback reply: {e}")

@app.route('/admin/trigger_organize_files', methods=['POST'])
def trigger_organize_files():
    """
    (เวอร์ชันปรับปรุง) This new route triggers the file organization job to run in the background.
    """
    try:
        # ตั้งเวลาให้เริ่มทำงานในอีก 3 วินาที เพื่อให้ request นี้ตอบกลับไปก่อน
        run_at_time = datetime.datetime.now(THAILAND_TZ) + datetime.timedelta(seconds=3)
        scheduler.add_job(
            background_organize_files_job, 
            'date', 
            run_date=run_at_time, 
            id='manual_file_organization_v2', # ใช้ ID ใหม่
            replace_existing=True,
            misfire_grace_time=300 # อนุญาตให้ทำงานช้าได้ 5 นาที
        )
        
        flash('🚀 เริ่มกระบวนการจัดระเบียบไฟล์เบื้องหลังแล้ว! ระบบจะแจ้งเตือนผ่าน LINE เมื่อทำงานเสร็จ (อาจใช้เวลาหลายนาที)', 'success')
        current_app.logger.info("Background file organization job has been triggered.")
    except Exception as e:
        flash(f'เกิดข้อผิดพลาดในการเริ่มงานเบื้องหลัง: {e}', 'danger')
        current_app.logger.error(f"Failed to trigger background job: {e}")

    return redirect(url_for('settings_page'))

@app.route('/admin/line_bot_status')
def get_line_bot_status():
    """ตรวจสอบสถานะการตั้งค่าของ LINE Bot"""
    details = {
        "channel_access_token_configured": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "channel_secret_configured": bool(LINE_CHANNEL_SECRET),
        "channel_secret_length": len(LINE_CHANNEL_SECRET),
        "webhook_handler_initialized": handler is not None,
        "messaging_api_initialized": line_messaging_api is not None,
        "issues": []
    }

    if not details["channel_access_token_configured"]:
        details["issues"].append("LINE_CHANNEL_ACCESS_TOKEN ยังไม่ได้ตั้งค่าใน environment variables")
    if not details["channel_secret_configured"]:
        details["issues"].append("LINE_CHANNEL_SECRET ยังไม่ได้ตั้งค่าใน environment variables")
    elif details["channel_secret_length"] != 32:
        details["issues"].append(f"ความยาวของ Channel Secret ไม่ถูกต้อง (ควรจะเป็น 32 แต่ตอนนี้คือ {details['channel_secret_length']})")

    is_ok = not bool(details["issues"])

    return jsonify({
        "status": "success" if is_ok else "error",
        "message": "การตั้งค่า LINE Bot พร้อมใช้งาน" if is_ok else "พบปัญหาในการตั้งค่า LINE Bot",
        "details": details
    })

@app.route('/callback_line')
def callback_line():
    # This endpoint is required for the LINE Login callback.
    # For LIFF apps, often no server-side action is needed here
    # as the LIFF SDK handles the token on the client-side.
    return "OK", 200

@app.route('/api/settings/categories', methods=['GET', 'POST'])
def api_manage_categories():
    """API สำหรับจัดการหมวดหมู่สินค้า"""
    settings = get_app_settings()
    if request.method == 'GET':
        return jsonify(settings.get('product_categories', []))
    
    if request.method == 'POST':
        data = request.json
        new_categories = data.get('categories', [])
        if isinstance(new_categories, list):
            # ใช้ฟังก์ชัน save_app_settings ที่อัปเดตแล้ว
            if save_app_settings({'product_categories': new_categories}):
                return jsonify({'status': 'success', 'message': 'บันทึกหมวดหมู่เรียบร้อยแล้ว'})
        return jsonify({'status': 'error', 'message': 'ข้อมูลไม่ถูกต้อง'}), 400

@app.route('/api/search_product_image')
def search_product_image():
    """
    API endpoint to search for product images online using Serper.dev API.
    """
    query = request.args.get('q')
    if not query:
        return jsonify({"error": "Query parameter is required"}), 400

    serper_api_key = os.environ.get('SERPER_API_KEY')
    if not serper_api_key:
        return jsonify({"error": "Serper API key is not configured on the server"}), 500

    headers = {
        'X-API-KEY': serper_api_key,
        'Content-Type': 'application/json'
    }
    payload = json.dumps({"q": query, "num": 20}) # ค้นหา 20 รูป
    
    try:
        response = requests.post("https://google.serper.dev/images", headers=headers, data=payload, timeout=10)
        response.raise_for_status() # Raise an exception for bad status codes
        images = response.json().get('images', [])
        
        # คัดกรองและจัดรูปแบบผลลัพธ์ให้ใช้งานง่าย
        formatted_images = [
            {"thumbnail": img.get("thumbnailUrl"), "url": img.get("imageUrl"), "title": img.get("title")}
            for img in images if img.get("thumbnailUrl") and img.get("imageUrl")
        ]
        return jsonify(formatted_images)
        
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Error calling Serper API: {e}")
        return jsonify({"error": f"Could not connect to image search service: {e}"}), 503
    except Exception as e:
        app.logger.error(f"An unexpected error occurred during image search: {e}")
        return jsonify({"error": "An internal server error occurred"}), 500

# ✅✅✅ START: โค้ดที่แก้ไขให้ถูกต้อง ✅✅✅
# รวม with app.app_context(): ให้เหลือแค่บล็อกเดียว
with app.app_context():
    # บรรทัดนี้จะสั่งให้ดึงไฟล์ settings_backup.json จาก Google Drive
    # มาใช้งานทุกครั้งที่แอปเริ่มทำงาน ทำให้ข้อมูลสินค้าไม่หาย
    load_settings_from_drive_on_startup()
    
    # บรรทัดนี้จะสร้างตารางในฐานข้อมูล (ถ้ายังไม่มี)
    db.create_all()
# ✅✅✅ END: โค้ดที่แก้ไขให้ถูกต้อง ✅✅✅

@app.route('/admin/organize_files', methods=['GET'])
def organize_files():
    """
    แสดงหน้าสำหรับให้ผู้ใช้เริ่มกระบวนการจัดระเบียบไฟล์
    """
    return render_template('organize_files.html')

@app.route('/admin/cleanup_drive', methods=['POST'])
def cleanup_drive_folders():
    """
    (เวอร์ชันปรับปรุง) Finds duplicate folders by listing contents within the main app folder,
    merges them, and deletes the empty duplicates.
    """
    service = get_google_drive_service()
    if not service:
        flash('ไม่สามารถเชื่อมต่อ Google Drive API ได้', 'danger')
        return redirect(url_for('settings_page'))

    log_messages = []
    
    try:
        # 1. List all folders directly inside the main app folder
        main_folder_id = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')
        if not main_folder_id:
            flash('ไม่ได้ตั้งค่า GOOGLE_DRIVE_FOLDER_ID ใน Environment Variables', 'danger')
            return redirect(url_for('settings_page'))

        query = f"'{main_folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        response = _execute_google_api_call_with_retry(
            service.files().list,
            q=query,
            spaces='drive',
            fields='files(id, name)'
        )
        all_root_folders = response.get('files', [])

        # 2. Group folders by name using Python
        folders_by_name = defaultdict(list)
        for folder in all_root_folders:
            folders_by_name[folder['name']].append(folder)

        # 3. Iterate through the grouped folders to find and process duplicates
        for name, folders in folders_by_name.items():
            if len(folders) <= 1:
                log_messages.append(f"✅ โฟลเดอร์ '{name}' ไม่ซ้ำซ้อน ข้ามไป...")
                continue

            # Designate the first folder as the master folder
            master_folder = folders[0]
            duplicate_folders = folders[1:]
            log_messages.append(f"⚠️ พบโฟลเดอร์ '{name}' ซ้ำกัน {len(folders)} อัน กำลังรวมไฟล์ไปที่ ID: {master_folder['id']}")

            for dup_folder in duplicate_folders:
                # Move all contents from the duplicate to the master
                page_token = None
                while True:
                    res_files = _execute_google_api_call_with_retry(
                        service.files().list,
                        q=f"'{dup_folder['id']}' in parents and trashed=false",
                        fields="nextPageToken, files(id, parents)",
                        pageToken=page_token
                    )
                    for file_item in res_files.get('files', []):
                        original_parents = ",".join(file_item.get('parents'))
                        _execute_google_api_call_with_retry(
                            service.files().update,
                            fileId=file_item['id'],
                            addParents=master_folder['id'],
                            removeParents=original_parents
                        )
                        log_messages.append(f"   - 🚚 ย้ายไฟล์/โฟลเดอร์ ID: {file_item['id']} ไปยัง '{name}' หลัก")
                    page_token = res_files.get('nextPageToken', None)
                    if not page_token:
                        break
                
                # Delete the now-empty duplicate folder
                _execute_google_api_call_with_retry(service.files().delete, fileId=dup_folder['id'])
                log_messages.append(f"   - 🗑️ ลบโฟลเดอร์ซ้ำ ID: {dup_folder['id']} เรียบร้อยแล้ว")

    except HttpError as e:
        log_messages.append(f"❌ เกิดข้อผิดพลาดขณะทำงาน: {e}")
        current_app.logger.error(f"Error during cleanup_drive_folders: {e}")
    
    flash('<strong>การทำความสะอาด Google Drive เสร็จสิ้น:</strong><br>' + '<br>'.join(log_messages), 'info')
    return redirect(url_for('settings_page'))


def background_organize_files_job():
    """
    (เวอร์ชันปรับปรุง V2) Proactively finds unorganized files and moves them to their correct, structured folders.
    This ensures that the Health Check tool can always find the files.
    """
    with app.app.context():
        app.logger.info("--- 🚀 Starting Proactive Google Drive File Organization Job (V2) ---")
        service = get_google_drive_service()
        if not service:
            notify_admin_error("จัดระเบียบไฟล์ล้มเหลว: ไม่สามารถเชื่อมต่อ Google Drive service ได้")
            return

        all_tasks = get_google_tasks_for_report(show_completed=True)
        if not all_tasks:
            notify_admin_error("จัดระเบียบไฟล์ล้มเหลว: ไม่สามารถดึงข้อมูลงานจาก Google Tasks ได้")
            return
        
        tasks_dict = {task['id']: task for task in all_tasks}
        moved_count, skipped_count, error_count = 0, 0, 0
        
        attachments_base_folder_id = find_or_create_drive_folder("Task_Attachments", GOOGLE_DRIVE_FOLDER_ID)
        if not attachments_base_folder_id:
            notify_admin_error("จัดระเบียบไฟล์ล้มเหลว: ไม่สามารถสร้างหรือหาโฟลเดอร์ Task_Attachments ได้")
            return

        # 1. ค้นหาไฟล์ทั้งหมดที่ยังไม่ได้ถูกจัดระเบียบ (อยู่ใต้ Task_Attachments โดยตรง)
        unorganized_files_query = f"'{attachments_base_folder_id}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
        unorganized_files = _get_drive_files_in_folder(attachments_base_folder_id)
        
        app.logger.info(f"Found {len(unorganized_files)} unorganized files to process.")

        for file_item in unorganized_files:
            file_id = file_item.get('id')
            file_name = file_item.get('name', '')
            
            # 2. ดึง Task ID จากชื่อไฟล์ (ระบบจะตั้งชื่อไฟล์ให้มี Task ID ต่อท้ายเสมอ)
            task_id_match = re.search(r'([a-zA-Z0-9_-]{20,})', file_name)
            if not task_id_match:
                skipped_count += 1
                app.logger.warning(f"Skipping file '{file_name}': Could not extract Task ID from filename.")
                continue
            
            task_id = task_id_match.group(1)
            task = tasks_dict.get(task_id)

            if not task:
                skipped_count += 1
                app.logger.warning(f"Skipping file '{file_name}': No matching task found for ID {task_id}.")
                continue

            try:
                # 3. สร้างเส้นทางโฟลเดอร์ที่ถูกต้อง: /Task_Attachments/YYYY-MM/ชื่อลูกค้า - [Task ID]/
                created_dt_local = date_parse(task.get('created')).astimezone(THAILAND_TZ)
                monthly_folder_name = created_dt_local.strftime('%Y-%m')
                monthly_folder_id = find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)

                customer_info = parse_customer_info_from_notes(task.get('notes', ''))
                sanitized_customer_name = sanitize_filename(customer_info.get('name', 'Unknown_Customer'))
                customer_task_folder_name = f"{sanitized_customer_name} - {task_id}"
                destination_folder_id = find_or_create_drive_folder(customer_task_folder_name, monthly_folder_id)

                if not destination_folder_id:
                    error_count += 1
                    app.logger.error(f"Failed to create destination folder for task {task_id}.")
                    continue
                
                # 4. ย้ายไฟล์ไปยังโฟลเดอร์ปลายทาง
                _execute_google_api_call_with_retry(
                    service.files().update,
                    fileId=file_id,
                    addParents=destination_folder_id,
                    removeParents=attachments_base_folder_id
                )
                moved_count += 1
                app.logger.info(f"Successfully moved '{file_name}' to folder for task {task_id}.")

            except Exception as e:
                error_count += 1
                app.logger.error(f"Error moving file '{file_name}' for task {task_id}: {e}", exc_info=True)

        # 5. ส่งการแจ้งเตือนสรุปผล
        log_summary = f"🗂️ จัดระเบียบไฟล์ใน Drive เสร็จสิ้น!\n\n- ย้ายไฟล์สำเร็จ: {moved_count} ไฟล์\n- ข้ามไป: {skipped_count} ไฟล์\n- เกิดข้อผิดพลาด: {error_count} ไฟล์"
        app.logger.info(f"--- ✅ Finished Proactive File Organization Job --- \n{log_summary}")
        
        admin_group_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
        if admin_group_id:
            message_queue.add_message(admin_group_id, TextMessage(text=log_summary))

@app.route('/api/warehouses/save', methods=['POST'])
@csrf.exempt
def save_warehouse():
    """ (เวอร์ชันปรับปรุง) API สำหรับบันทึกข้อมูลคลังสินค้า, รองรับช่างหลายคน """
    data = request.json
    try:
        if not data.get('name'):
            return jsonify({'status': 'error', 'message': 'กรุณากรอกชื่อคลังสินค้า'}), 400

        if data.get('id'): # Edit existing
            wh = Warehouse.query.get(data['id'])
            if not wh:
                return jsonify({'status': 'error', 'message': 'ไม่พบคลังสินค้าที่ต้องการแก้ไข'}), 404
        else: # Add new
            wh = Warehouse()
            db.session.add(wh)
        
        wh.name = data['name']
        wh.type = data['type']
        
        # --- START: โค้ดที่แก้ไข ---
        # รับค่าเป็น List ของรายชื่อช่าง และแปลงเป็น String เพื่อบันทึก
        technician_names = data.get('technician_names', []) 
        if wh.type == 'technician_van' and isinstance(technician_names, list):
            # เรียงลำดับชื่อก่อน join เพื่อให้ข้อมูลเป็นระเบียบ
            wh.technician_name = ",".join(sorted(technician_names)) 
        else:
            wh.technician_name = None
        # --- END: โค้dที่แก้ไข ---
        
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'บันทึกข้อมูลคลังสินค้าเรียบร้อยแล้ว'})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving warehouse: {e}")
        return jsonify({'status': 'error', 'message': f'เกิดข้อผิดพลาด: {str(e)}'}), 500

@app.route('/api/warehouses/<int:warehouse_id>/delete', methods=['DELETE'])
@csrf.exempt
def delete_warehouse(warehouse_id):
    try:
        wh = Warehouse.query.get(warehouse_id)
        if not wh:
            return jsonify({'status': 'error', 'message': 'ไม่พบคลังสินค้าที่ต้องการลบ'}), 404
        
        # เพิ่มเงื่อนไข: ห้ามลบคลังที่มีของอยู่
        if wh.stock_levels:
             return jsonify({'status': 'error', 'message': 'ไม่สามารถลบคลังได้เนื่องจากยังมีสินค้าคงเหลืออยู่'}), 400

        db.session.delete(wh)
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'ลบคลังสินค้าเรียบร้อยแล้ว'})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting warehouse: {e}")
        return jsonify({'status': 'error', 'message': f'เกิดข้อผิดพลาด: {str(e)}'}), 500

@app.route('/api/stock/adjust', methods=['POST'])
@csrf.exempt
def api_stock_adjust():
    data = request.json
    try:
        product_code = data.get('product_code')
        to_warehouse_id = int(data.get('to_warehouse_id'))
        quantity_change = float(data.get('quantity')) # รับของเข้าเป็นค่าบวก, ปรับออกเป็นค่าลบ

        if not all([product_code, to_warehouse_id, quantity_change is not None]):
            return jsonify({'status': 'error', 'message': 'ข้อมูลไม่ครบถ้วน'}), 400

        # Update Stock Level
        stock_level = StockLevel.query.filter_by(product_code=product_code, warehouse_id=to_warehouse_id).first()
        if not stock_level:
            stock_level = StockLevel(product_code=product_code, warehouse_id=to_warehouse_id, quantity=0)
            db.session.add(stock_level)
        stock_level.quantity += quantity_change

        # Create Movement Log
        movement = StockMovement(
            product_code=product_code,
            quantity_change=quantity_change,
            to_warehouse_id=to_warehouse_id,
            movement_type='adjustment',
            notes=data.get('notes'),
            user=data.get('user')
        )
        db.session.add(movement)
        
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'ปรับปรุงสต็อกเรียบร้อยแล้ว'})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error in stock adjustment: {e}")
        return jsonify({'status': 'error', 'message': f'เกิดข้อผิดพลาด: {str(e)}'}), 500


@app.route('/api/stock/transfer', methods=['POST'])
@csrf.exempt
def api_stock_transfer():
    data = request.json
    try:
        product_code = data.get('product_code')
        from_warehouse_id = int(data.get('from_warehouse_id'))
        to_warehouse_id = int(data.get('to_warehouse_id'))
        quantity = float(data.get('quantity'))

        if not all([product_code, from_warehouse_id, to_warehouse_id, quantity > 0]):
            return jsonify({'status': 'error', 'message': 'ข้อมูลไม่ครบถ้วนหรือจำนวนไม่ถูกต้อง'}), 400

        # Update Stock Level (From)
        from_stock = StockLevel.query.filter_by(product_code=product_code, warehouse_id=from_warehouse_id).first()
        if not from_stock:
            from_stock = StockLevel(product_code=product_code, warehouse_id=from_warehouse_id, quantity=0)
            db.session.add(from_stock)
        from_stock.quantity -= quantity

        # Update Stock Level (To)
        to_stock = StockLevel.query.filter_by(product_code=product_code, warehouse_id=to_warehouse_id).first()
        if not to_stock:
            to_stock = StockLevel(product_code=product_code, warehouse_id=to_warehouse_id, quantity=0)
            db.session.add(to_stock)
        to_stock.quantity += quantity

        # Create Movement Log
        movement = StockMovement(
            product_code=product_code,
            quantity_change=quantity,
            from_warehouse_id=from_warehouse_id,
            to_warehouse_id=to_warehouse_id,
            movement_type='transfer',
            notes=data.get('notes'),
            user=data.get('user')
        )
        db.session.add(movement)
        
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'โอนย้ายสินค้าเรียบร้อยแล้ว'})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error in stock transfer: {e}")
        return jsonify({'status': 'error', 'message': f'เกิดข้อผิดพลาด: {str(e)}'}), 500

@app.route('/api/technician/stock_data')
def get_technician_stock_data():
    """API สำหรับให้ LIFF ดึงข้อมูลสต็อกของช่างที่ล็อกอินอยู่"""
    liff_user_id = request.args.get('liff_user_id')
    if not liff_user_id:
        return jsonify({'status': 'error', 'message': 'ไม่พบ LIFF User ID'}), 400

    try:
        # 1. ค้นหาช่างจาก LIFF User ID
        settings = get_app_settings()
        technician_list = settings.get('technician_list', [])
        tech_info = next((tech for tech in technician_list if tech.get('line_user_id') == liff_user_id), None)
        
        if not tech_info:
            return jsonify({'status': 'error', 'message': 'ไม่พบข้อมูลช่างที่ผูกกับ LIFF User ID นี้'}), 404
        
        technician_name = tech_info.get('name')

        # 2. ค้นหาคลังของช่างคนนี้
        warehouse = Warehouse.query.filter_by(technician_name=technician_name, is_active=True).first()
        if not warehouse:
            return jsonify({'status': 'error', 'message': f'ไม่พบคลังสินค้าที่ผูกกับช่าง {technician_name}'}), 404

        # 3. ดึงข้อมูลสินค้าทั้งหมดในคลังของช่าง
        stock_levels = StockLevel.query.filter_by(warehouse_id=warehouse.id).all()
        
        product_catalog = {p.get('product_code', p.get('item_name')): p for p in settings.get('equipment_catalog', [])}
        
        stock_items = []
        for sl in stock_levels:
            product_info = product_catalog.get(sl.product_code, {})
            stock_items.append({
                'item_name': product_info.get('item_name', sl.product_code),
                'product_code': sl.product_code,
                'quantity': sl.quantity,
                'image_url': product_info.get('image_url')
            })
        
        stock_items.sort(key=lambda x: x['item_name'])

        return jsonify({
            'status': 'success',
            'warehouse_name': warehouse.name,
            'technician_name': technician_name,
            'stock_items': stock_items
        })

    except Exception as e:
        current_app.logger.error(f"Error fetching technician stock data: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดฝั่งเซิร์ฟเวอร์'}), 500

def _get_drive_files_in_folder(folder_id):
    """Helper function to list all files within a specific Google Drive folder."""
    from app import get_google_drive_service, _execute_google_api_call_with_retry
    service = get_google_drive_service()
    if not service:
        current_app.logger.error("Drive service not available for file listing.")
        return []
    
    files = []
    page_token = None
    try:
        while True:
            response = _execute_google_api_call_with_retry(
                service.files().list,
                q=f"'{folder_id}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false",
                spaces='drive',
                fields='nextPageToken, files(id, name, createdTime)',
                pageSize=200,
                pageToken=page_token
            )
            files.extend(response.get('files', []))
            page_token = response.get('nextPageToken', None)
            if page_token is None:
                break
    except Exception as e:
        current_app.logger.error(f"Failed to list files in folder {folder_id}: {e}")
    return files

@app.route('/api/health_check/scan')
def health_check_scan():
    """
    (เวอร์ชันปรับปรุง V3 - Optimized) Scans for tasks with discrepancies with fewer API calls.
    """
    current_app.logger.info("Starting data integrity scan (V3 - Optimized)...")
    tasks_with_issues = []
    
    try:
        # 1. Get all tasks from Google Tasks
        all_tasks = get_google_tasks_for_report(show_completed=True)
        if all_tasks is None:
            raise Exception("Could not fetch Google Tasks.")
        
        tasks_dict = {task['id']: task for task in all_tasks}

        # 2. Efficiently get all customer folders from Google Drive in one go
        attachments_base_folder_id = find_or_create_drive_folder("Task_Attachments", GOOGLE_DRIVE_FOLDER_ID)
        if not attachments_base_folder_id:
            raise Exception("Could not find base attachments folder.")

        all_customer_folders = []
        monthly_folders = _get_drive_folders_in_folder(attachments_base_folder_id)
        for month_folder in monthly_folders:
            all_customer_folders.extend(_get_drive_folders_in_folder(month_folder['id']))
        
        # 3. Loop through folders (fewer items than tasks) and check against the in-memory task list
        task_id_pattern = re.compile(r'-\s([a-zA-Z0-9_-]{20,})$')
        
        for folder in all_customer_folders:
            match = task_id_pattern.search(folder['name'])
            if not match:
                continue

            task_id = match.group(1)
            task = tasks_dict.get(task_id)

            if not task:
                continue

            drive_files = _get_drive_files_in_folder(folder['id'])
            if not drive_files:
                continue

            # Check for discrepancies
            history, _ = parse_tech_report_from_notes(task.get('notes', ''))
            attachment_ids_in_notes = {att['id'] for report in history for att in report.get('attachments', [])}
            
            missing_attachments_count = sum(1 for drive_file in drive_files if drive_file['id'] not in attachment_ids_in_notes)

            if missing_attachments_count > 0:
                customer_info = parse_customer_info_from_notes(task.get('notes', ''))
                tasks_with_issues.append({
                    'task_id': task_id,
                    'task_title': task['title'],
                    'customer_name': customer_info.get('name', 'N/A'),
                    'missing_count': missing_attachments_count,
                    'folder_id': folder['id']
                })
        
        current_app.logger.info(f"Scan complete (V3). Found {len(tasks_with_issues)} tasks with issues.")
        return jsonify({'status': 'success', 'issues': tasks_with_issues})

    except Exception as e:
        current_app.logger.error(f"An error occurred during health check scan: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500

def _get_drive_folders_in_folder(folder_id):
    """Helper function to list all sub-folders within a specific Google Drive folder."""
    service = get_google_drive_service()
    if not service:
        current_app.logger.error("Drive service not available for folder listing.")
        return []
    
    folders = []
    page_token = None
    try:
        while True:
            response = _execute_google_api_call_with_retry(
                service.files().list,
                q=f"'{folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                spaces='drive',
                fields='nextPageToken, files(id, name)',
                pageSize=200,
                pageToken=page_token
            )
            folders.extend(response.get('files', []))
            page_token = response.get('nextPageToken', None)
            if page_token is None:
                break
    except Exception as e:
        current_app.logger.error(f"Failed to list folders in folder {folder_id}: {e}")
    return folders

@app.route('/api/health_check/repair/<task_id>', methods=['POST'])
def health_check_repair(task_id):
    """
    (เวอร์ชันปรับปรุง V2) Repairs a single task using a more robust folder-finding logic.
    """
    from utils import get_single_task, update_google_task, parse_tech_report_from_notes, parse_customer_profile_from_task
    
    current_app.logger.info(f"Attempting to repair task (V2): {task_id}")
    task = get_single_task(task_id)
    if not task:
        return jsonify({'status': 'error', 'message': 'Task not found.'}), 404

    # 1. Find the correct Drive folder for this task by scanning
    attachments_base_folder_id = find_or_create_drive_folder("Task_Attachments", GOOGLE_DRIVE_FOLDER_ID)
    if not attachments_base_folder_id:
        return jsonify({'status': 'error', 'message': 'Could not find base attachments folder.'}), 500

    target_folder_id = None
    monthly_folders = _get_drive_folders_in_folder(attachments_base_folder_id)
    for month_folder in monthly_folders:
        customer_folders = _get_drive_folders_in_folder(month_folder['id'])
        for folder in customer_folders:
            if task_id in folder['name']:
                target_folder_id = folder['id']
                current_app.logger.info(f"Found matching folder '{folder['name']}' (ID: {target_folder_id}) for task {task_id}")
                break
        if target_folder_id:
            break
            
    if not target_folder_id:
        return jsonify({'status': 'error', 'message': f'Could not find a corresponding Google Drive folder containing the Task ID: {task_id}'}), 404

    # 2. Get all files from the found folder
    drive_files = _get_drive_files_in_folder(target_folder_id)
    
    # 3. Get current attachments from notes
    notes = task.get('notes', '')
    history, base_notes = parse_tech_report_from_notes(notes)
    
    attachment_ids_in_notes = {att['id'] for report in history for att in report.get('attachments', [])}
    
    # 4. Identify attachments that are in Drive but not in notes
    recovered_attachments = []
    for drive_file in drive_files:
        if drive_file['id'] not in attachment_ids_in_notes:
            recovered_attachments.append({
                'id': drive_file['id'],
                'name': drive_file['name'],
                'url': f"https://drive.google.com/file/d/{drive_file['id']}/view?usp=drivesdk"
            })

    if not recovered_attachments:
        return jsonify({'status': 'success', 'message': 'ไม่พบไฟล์ที่ขาดหายไป (อาจซ่อมแซมไปแล้ว)'})

    # 5. Create a new "recovered" report and append it
    profile_data = parse_customer_profile_from_task(task) # Use the new profile parser
    
    new_report = {
        "type": "report",
        "summary_date": datetime.datetime.now(THAILAND_TZ).isoformat(),
        "work_summary": "[ข้อมูลกู้คืนอัตโนมัติ] พบไฟล์แนบที่ไม่ได้ถูกบันทึกในประวัติ",
        "attachments": recovered_attachments,
        "technicians": ["System Recovery"],
        "is_recovered": True
    }
    
    # Check if the task is using the new JSON structure
    try:
        json.loads(task.get('notes', '{}'))
        # New structure: append to the 'reports' list of the correct job
        # For simplicity in repair, we'll assume it's the first job if multiple exist.
        if profile_data.get('jobs'):
             # Find the job this attachment might belong to, or add to the first one
             job_index_to_add = 0 # Default to the first job
             profile_data['jobs'][job_index_to_add].setdefault('reports', []).append(new_report)
        else: # Handle case where task is new format but has no jobs array yet
             profile_data['jobs'] = [{'reports': [new_report]}]
        
        final_notes = json.dumps(profile_data, ensure_ascii=False, indent=2)

    except json.JSONDecodeError:
        # Old structure: append the whole block
        history.append(new_report)
        all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
        final_notes = base_notes.strip() + all_reports_text

    # 6. Update the Google Task with the new notes
    if update_google_task(task_id, notes=final_notes):
        cache.clear()
        current_app.logger.info(f"Successfully repaired task {task_id}, added {len(recovered_attachments)} attachments.")
        return jsonify({'status': 'success', 'message': f'ซ่อมแซมสำเร็จ! เพิ่มรูปภาพที่ขาดหายไป {len(recovered_attachments)} รูป'})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to update Google Task with repaired data.'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=True)