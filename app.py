import os
from flask import Response
from io import BytesIO
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError
import requests
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from sqlalchemy.orm import relationship, backref
from sqlalchemy import text
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
from datetime import timezone, date, time
import time
import tempfile
import uuid
from queue import Queue
import threading
import requests
import random
from PIL import Image
from functools import wraps
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
load_dotenv()
from flask import Flask, request, render_template, redirect, url_for, abort, flash, jsonify, Response, session, make_response, current_app
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect
from cachetools import cached, TTLCache
from geopy.distance import geodesic
from urllib.parse import urlparse, parse_qs, unquote, quote_plus
from utils import (
    parse_db_customer_data, parse_db_job_data, parse_db_report_data,
    get_app_settings, generate_qr_code_base64,
    find_or_create_drive_folder, upload_data_from_memory_to_drive,
    get_customer_database, get_technician_report_data
)
import qrcode
import base64
from urllib.parse import quote_plus
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest,
    PushMessageRequest, TextMessage, FlexMessage, QuickReply, QuickReplyItem
)
from linebot.v3.messaging.models import URIAction
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent, PostbackEvent, ImageMessageContent,
    FileMessageContent, GroupSource, UserSource, FollowEvent
)
from datetime import timedelta
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
    sentry_sdk.init(dsn=SENTRY_DSN, integrations=[FlaskIntegration()], traces_sample_rate=1.0, profiles_sample_rate=1.0)

app = Flask(__name__, static_folder='static')

if os.environ.get('RENDER') == 'true' and not os.environ.get('DATABASE_URL'):
    raise RuntimeError("FATAL: DATABASE_URL environment variable is not set on Render. Please create a PostgreSQL database and link it to this service.")

app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///data.sqlite')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True, 'pool_recycle': 280}

db = SQLAlchemy(app)
migrate = Migrate(app, db)
THAILAND_TZ = pytz.timezone('Asia/Bangkok') # ย้าย THAILAND_TZ มาไว้ที่นี่

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(80), nullable=False, default='user')
    is_active = db.Column(db.Boolean, default=True)
    def set_password(self, password): self.password_hash = generate_password_hash(password)
    def check_password(self, password): return check_password_hash(self.password_hash, password)
    def to_dict(self): return {'id': self.id, 'username': self.username, 'role': self.role, 'is_active': self.is_active}

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = "กรุณาเข้าสู่ระบบเพื่อเข้าถึงหน้านี้"
login_manager.login_message_category = "info"

@login_manager.user_loader
def load_user(user_id): return User.query.get(int(user_id))

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            flash('คุณไม่มีสิทธิ์เข้าถึงหน้านี้', 'danger')
            return redirect(url_for('liff.summary'))
        return f(*args, **kwargs)
    return decorated_function

class Customer(db.Model):
    __tablename__ = 'customers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    organization = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    address = db.Column(db.Text, nullable=True)
    map_url = db.Column(db.Text, nullable=True)
    line_user_id = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    jobs = db.relationship('Job', backref='customer', lazy=True)

class Job(db.Model):
    __tablename__ = 'jobs'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    job_title = db.Column(db.Text, nullable=False)
    job_type = db.Column(db.String(50), nullable=False, default='service')
    assigned_technician = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(50), nullable=False, default='needsAction')
    created_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    due_date = db.Column(db.DateTime, nullable=True)
    completed_date = db.Column(db.DateTime, nullable=True)
    product_details = db.Column(db.JSON, nullable=True)
    internal_notes = db.Column(db.JSON, nullable=True)
    reports = db.relationship('Report', backref='job', lazy=True)
    items = db.relationship('JobItem', backref='job', lazy=True)

    @property
    def is_today(self):
        if not self.due_date or self.status == 'completed': 
            return False
        
        # --- START: โค้ดส่วนที่แก้ไข ---
        try:
            # ทำให้ 'aware' ก่อนแปลง ถ้าหากมันเป็น 'naive'
            if self.due_date.tzinfo is None:
                due_date_utc = pytz.utc.localize(self.due_date)
            else:
                due_date_utc = self.due_date
            
            due_date_local = due_date_utc.astimezone(THAILAND_TZ)
            today_local = datetime.datetime.now(THAILAND_TZ).date()
            return due_date_local.date() == today_local
        except Exception:
            return False
        # --- END: โค้ดส่วนที่แก้ไข ---

    @property
    def is_overdue(self):
        if not self.due_date or self.status == 'completed': 
            return False
            
        # --- START: โค้ดส่วนที่แก้ไข ---
        try:
            # ทำให้ 'aware' ก่อนแปลง ถ้าหากมันเป็น 'naive'
            if self.due_date.tzinfo is None:
                due_date_utc = pytz.utc.localize(self.due_date)
            else:
                due_date_utc = self.due_date

            due_date_local = due_date_utc.astimezone(THAILAND_TZ)
            today_local = datetime.datetime.now(THAILAND_TZ).date()
            return due_date_local.date() < today_local
        except Exception:
            return False
        # --- END: โค้ดส่วนที่แก้ไข ---

    @property
    def tech_reports_history(self):
        history = []
        sorted_reports = sorted(self.reports, key=lambda r: r.summary_date, reverse=True)
        for report in sorted_reports:
            attachments = [{'id': att.drive_file_id, 'name': att.file_name, 'url': att.file_url} for att in report.attachments]
            history.append({
                'id': report.id, 'type': report.report_type, 'summary_date': report.summary_date.isoformat(),
                'work_summary': report.work_summary, 'reason': report.work_summary,
                'technicians': report.technicians.split(',') if report.technicians else [],
                'is_internal': report.is_internal, 'attachments': attachments
            })
        return history

class Report(db.Model):
    __tablename__ = 'reports'
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('jobs.id'), nullable=False)
    summary_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    report_type = db.Column(db.String(50), nullable=False, default='report') # 'report', 'reschedule', 'internal'
    work_summary = db.Column(db.Text, nullable=True)
    technicians = db.Column(db.String(255), nullable=True)
    is_internal = db.Column(db.Boolean, default=False)
    attachments = db.relationship('Attachment', backref='report', lazy=True)

class Attachment(db.Model):
    __tablename__ = 'attachments'
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey('reports.id'), nullable=False)
    drive_file_id = db.Column(db.String(255), nullable=False)
    file_name = db.Column(db.String(255), nullable=True)
    file_url = db.Column(db.Text, nullable=True)

class JobItem(db.Model):
    __tablename__ = 'job_items'
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('jobs.id'), nullable=False)
    item_name = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=1)
    unit_price = db.Column(db.Float, nullable=False, default=0)
    status = db.Column(db.String(50), nullable=False, default='pending')
    added_by = db.Column(db.String(100))
    added_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'job_id': self.job_id,
            'item_name': self.item_name,
            'quantity': self.quantity,
            'unit_price': self.unit_price,
            'status': self.status,
            'added_by': self.added_by,
            'added_at': self.added_at.isoformat()
        }

class BillingStatus(db.Model):
    __tablename__ = 'billing_status'
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('jobs.id'), unique=True, nullable=False, index=True)
    status = db.Column(db.String(50), nullable=False, default='pending_billing')
    billed_date = db.Column(db.DateTime)
    paid_date = db.Column(db.DateTime)
    payment_due_date = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    job = db.relationship('Job', backref=db.backref('billing_status', uselist=False))

    def to_dict(self):
        return {
            'job_id': self.job_id,
            'status': self.status,
            'billed_date': self.billed_date.isoformat() if self.billed_date else None,
            'paid_date': self.paid_date.isoformat() if self.paid_date else None,
            'payment_due_date': self.payment_due_date.isoformat() if self.payment_due_date else None
        }

class Warehouse(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    type = db.Column(db.String(50), nullable=False, default='main')
    technician_name = db.Column(db.String(100), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'technician_name': self.technician_name,
            'is_active': self.is_active
        }

class StockLevel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_code = db.Column(db.String(100), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=0)
    
    warehouse = db.relationship('Warehouse', backref=db.backref('stock_levels', lazy=True))
    __table_args__ = (db.UniqueConstraint('product_code', 'warehouse_id', name='_product_warehouse_uc'),)

class StockMovement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_code = db.Column(db.String(100), nullable=False, index=True)
    quantity_change = db.Column(db.Float, nullable=False)
    from_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True)
    to_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True)
    movement_type = db.Column(db.String(50), nullable=False)
    job_item_id = db.Column(db.Integer, db.ForeignKey('job_items.id'), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    user = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

class UserActivity(db.Model):
    __tablename__ = 'user_activity'
    id = db.Column(db.Integer, primary_key=True)
    line_user_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    last_viewed_job_id = db.Column(db.Integer, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

CORS(app)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_development_only')
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
app.jinja_env.filters['dateutil_parse'] = date_parse
csrf = CSRFProtect(app)

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'kmz', 'kml'}
MAX_FILE_SIZE_MB = 500
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
SETTINGS_FILE = 'settings.json'

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
if LINE_CHANNEL_ACCESS_TOKEN.startswith('"') and LINE_CHANNEL_ACCESS_TOKEN.endswith('"'):
    LINE_CHANNEL_ACCESS_TOKEN = LINE_CHANNEL_ACCESS_TOKEN[1:-1].strip()

LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '').strip()
if LINE_CHANNEL_SECRET.startswith('"') and LINE_CHANNEL_SECRET.endswith('"'):
    LINE_CHANNEL_SECRET = LINE_CHANNEL_SECRET[1:-1].strip()

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    sys.exit("LINE Bot credentials are not set in environment variables.")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration)
line_messaging_api = MessagingApi(api_client)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

app.logger.info(f"======== DEBUG LINE CREDENTIALS ========")
app.logger.info(f"Channel Secret configured: {bool(LINE_CHANNEL_SECRET)}")
app.logger.info(f"Secret Length: {len(LINE_CHANNEL_SECRET)}")
app.logger.info(f"Secret (masked): {'*' * (len(LINE_CHANNEL_SECRET) - 4) + LINE_CHANNEL_SECRET[-4:] if len(LINE_CHANNEL_SECRET) > 4 else '****'}")
app.logger.info(f"Access Token configured: {bool(LINE_CHANNEL_ACCESS_TOKEN)}")
app.logger.info(f"Access Token (masked): {'*' * (len(LINE_CHANNEL_ACCESS_TOKEN) - 6) + LINE_CHANNEL_ACCESS_TOKEN[-6:] if len(LINE_CHANNEL_ACCESS_TOKEN) > 6 else '****'}")

def check_line_bot_configuration():
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

SCOPES = ['https://www.googleapis.com/auth/drive.file']
THAILAND_TZ = pytz.timezone('Asia/Bangkok')
cache = TTLCache(maxsize=100, ttl=60)

scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)

def save_settings_to_file(settings_data):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings_data, f, ensure_ascii=False, indent=4)
        return True
    except Exception as e:
        current_app.logger.error(f"Failed to save settings to {SETTINGS_FILE}: {e}")
        return False

#<editor-fold desc="Helper and Utility Functions">

class LineMessageQueue:
    def __init__(self, max_per_minute=100):
        self.queue = Queue()
        self.max_per_minute = max_per_minute
        self.sent_count = 0
        self.last_reset = time.time()
        self.processing_lock = threading.Lock()

    def add_message(self, user_id, messages):
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
                        push_message_request = PushMessageRequest(
                            to=user_id,
                            messages=messages
                        )
                        
                        line_messaging_api.push_message(push_message_request)
                                                    
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
  
def render_template_message(template_key, customer, job):
    settings = get_app_settings()
    template_str = settings.get('message_templates', {}).get(template_key, '')
    if not template_str:
        return f"ไม่พบ Template สำหรับ '{template_key}'"

    replacements = {
        '[customer_name]': customer.name or '-',
        '[customer_phone]': customer.phone or '-',
        '[customer_address]': customer.address or '-',
        '[task_title]': job.job_title or '-',
        '[due_date]': job.due_date.astimezone(THAILAND_TZ).strftime("%d/%m/%y %H:%M") if job.due_date else '-',
        '[map_url]': customer.map_url or '-',
        '[shop_phone]': settings.get('shop_info', {}).get('contact_phone', '-'),
        '[shop_line_id]': settings.get('shop_info', {}).get('line_id', '-'),
        '[task_url]': url_for('liff.customer_profile', customer_id=customer.id, job_id=job.id, _external=True)
    }

    for placeholder, value in replacements.items():
        template_str = template_str.replace(placeholder, str(value))
        
    return template_str    

def save_app_settings(settings_data):
    current_settings = get_app_settings()

    for key, value in settings_data.items():
        if key == 'equipment_catalog' and isinstance(value, list):
            validated_catalog = []
            for item in value:
                if isinstance(item, dict) and item.get('item_name'):
                    try:
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
                        validated_catalog.append(new_item)
                    except (ValueError, TypeError):
                        current_app.logger.warning(f"Skipping invalid equipment item: {item}")
                        continue
            current_settings['equipment_catalog'] = validated_catalog

        elif key == 'product_categories' and isinstance(value, list):
            validated_categories = sorted(list(set([str(cat).strip() for cat in value if str(cat).strip()])))
            current_settings['product_categories'] = validated_categories

        elif isinstance(value, dict) and key in current_settings and isinstance(current_settings[key], dict):
            current_settings[key].update(value)
        else:
            current_settings[key] = value

    return save_settings_to_file(current_settings)

def load_technician_locations():
    LOCATIONS_FILE = 'technician_locations.json'
    if not os.path.exists(LOCATIONS_FILE):
        return {}
    try:
        with open(LOCATIONS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def save_technician_locations(locations_data):
    LOCATIONS_FILE = 'technician_locations.json'
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

def get_google_service(api_name, api_version, scopes):
    creds = None
    SERVICE_ACCOUNT_FILE_CONTENT = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    
    if SERVICE_ACCOUNT_FILE_CONTENT:
        try:
            info = json.loads(SERVICE_ACCOUNT_FILE_CONTENT)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=scopes
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
            creds = Credentials.from_authorized_user_info(json.loads(google_token_json_str), scopes)
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
                            'client_secret': creds.client_secret, 'scopes': list(creds.scopes),
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

def get_google_drive_service():
    return get_google_service('drive', 'v3', ['https://www.googleapis.com/auth/drive.file'])

GOOGLE_CUSTOM_SEARCH_API_KEY = os.environ.get('GOOGLE_CUSTOM_SEARCH_API_KEY')
GOOGLE_CUSTOM_SEARCH_CX = os.environ.get('GOOGLE_CUSTOM_SEARCH_CX')

@app.route('/api/search_images')
def api_search_images():
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
            'num': 8
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
    query = request.args.get('q', '').strip().lower()
    if len(query) < 2:
        return jsonify([])
    
    settings = get_app_settings()
    catalog = settings.get('equipment_catalog', [])
    
    results = [
        item for item in catalog 
        if query in item.get('item_name', '').lower()
    ][:10]
    
    return jsonify(results)

@app.route('/api/proxy_drive_image/<file_id>')
def proxy_drive_image(file_id):
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
    try:
        customer_name = str(request.form.get('customer_name', '')).strip()
        task_title = str(request.form.get('task_title', '')).strip()
        job_type = str(request.form.get('job_type', 'service'))
        
        if not customer_name or not task_title:
            return jsonify({'status': 'error', 'message': 'กรุณากรอกชื่อผู้ติดต่อและรายละเอียดงาน'}), 400

        customer = None
        customer_id_from_form = request.form.get('customer_id')

        if customer_id_from_form:
            customer = Customer.query.get(customer_id_from_form)
        
        if not customer:
            customer = Customer.query.filter(Customer.name.ilike(customer_name)).first()

        if not customer:
            customer = Customer(
                name=customer_name,
                organization=request.form.get('organization_name'),
                phone=request.form.get('phone'),
                address=request.form.get('address'),
                map_url=request.form.get('latitude_longitude')
            )
            db.session.add(customer)
            db.session.flush()

        new_job = Job(
            customer=customer,
            job_title=task_title,
            job_type=job_type,
        )
        
        appointment_str = request.form.get('appointment')
        if appointment_str:
            dt_local = THAILAND_TZ.localize(date_parse(appointment_str))
            new_job.due_date = dt_local.astimezone(pytz.utc)

        if job_type == 'product':
            new_job.product_details = {
                'type': request.form.get('product_type'),
                'brand': request.form.get('product_brand'),
                'model': request.form.get('product_model'),
                'serial_number': request.form.get('product_sn'),
                'accessories': request.form.get('product_accessories')
            }

        db.session.add(new_job)
        db.session.commit()
        
        return jsonify({
            'status': 'success', 
            'message': 'สร้างใบงานใหม่เรียบร้อยแล้ว!', 
            'redirect_url': url_for('liff.customer_profile', customer_id=customer.id)
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error in api_create_task: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดภายในเซิร์ฟเวอร์'}), 500


@app.route('/api/external_tasks/create', methods=['POST'])
def api_create_external_task():
    try:
        customer_name = request.form.get('customer_name')
        task_title_raw = request.form.get('task_title')
        external_partner = request.form.get('external_partner')

        if not customer_name or not task_title_raw:
            return jsonify({'status': 'error', 'message': 'กรุณากรอกชื่อผู้ติดต่อและรายละเอียดงาน'}), 400

        job_title = f"[งานภายนอก/เคลม] {task_title_raw}"
        customer = Customer.query.filter(Customer.name.ilike(customer_name)).first()
        
        if not customer:
            customer = Customer(
                name=customer_name,
                organization=request.form.get('organization_name'),
                phone=request.form.get('phone'),
                address=request.form.get('address'),
            )
            db.session.add(customer)
            db.session.flush()

        new_job = Job(
            customer=customer,
            job_title=job_title,
            job_type='external',
            assigned_technician=external_partner
        )
        
        return_date_str = request.form.get('return_date')
        if return_date_str:
            dt_local = THAILAND_TZ.localize(date_parse(f"{return_date_str}T09:00:00"))
            new_job.due_date = dt_local.astimezone(pytz.utc)
            
        db.session.add(new_job)
        db.session.commit()
        
        return jsonify({
            'status': 'success', 
            'message': 'บันทึกงานภายนอก/งานเคลมเรียบร้อยแล้ว!', 
            'redirect_url': url_for('liff.customer_profile', customer_id=customer.id)
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error in api_create_external_task: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดภายในเซิร์ฟเวอร์'}), 500
    
@app.route('/api/customer/<int:customer_id>/job/<int:job_id>/add_internal_note', methods=['POST'])
def add_internal_note_to_job(customer_id, job_id):
    data = request.json
    note_text = data.get('note_text', '').strip()
    user = data.get('user', 'Admin')

    if not note_text:
        return jsonify({'status': 'error', 'message': 'กรุณาพิมพ์ข้อความ'}), 400

    job_to_update = Job.query.filter_by(id=job_id, customer_id=customer_id).first()
    if not job_to_update:
        return jsonify({'status': 'error', 'message': 'ไม่พบใบงานที่ต้องการอัปเดต'}), 404
        
    new_note_data = {
        "user": user,
        "timestamp": datetime.datetime.now(THAILAND_TZ).isoformat(),
        "text": note_text
    }
    
    if not job_to_update.internal_notes:
        job_to_update.internal_notes = []
        
    job_to_update.internal_notes.append(new_note_data)

    db.session.commit()
    return jsonify({
        'status': 'success', 
        'message': 'เพิ่มบันทึกภายในเรียบร้อยแล้ว',
        'new_note': new_note_data
    })

@app.route('/api/task/<int:task_id>/edit_main', methods=['POST'])
def api_edit_task_main(task_id):
    try:
        job_to_update = Job.query.get(task_id)
        if not job_to_update:
            return jsonify({'status': 'error', 'message': 'ไม่พบงาน'}), 404

        new_title = request.form.get('task_title')
        if not new_title:
            return jsonify({'status': 'error', 'message': 'กรุณากรอกรายละเอียดงาน'}), 400

        job_to_update.job_title = new_title

        customer = job_to_update.customer
        customer.name = request.form.get('customer_name')
        customer.organization = request.form.get('organization_name')
        customer.phone = request.form.get('customer_phone')
        customer.address = request.form.get('address')
        customer.map_url = request.form.get('latitude_longitude')
        
        appointment_str = request.form.get('appointment_due')
        if appointment_str:
            dt_local = THAILAND_TZ.localize(date_parse(appointment_str))
            job_to_update.due_date = dt_local.astimezone(pytz.utc)

        db.session.commit()
        return jsonify({'status': 'success', 'message': 'บันทึกข้อมูลหลักของงานเรียบร้อยแล้ว!', 'redirect_url': url_for('liff.customer_profile', customer_id=customer.id)})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error in api_edit_task_main for task {task_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดภายในเซิร์ฟเวอร์'}), 500

def send_assignment_notification(job):
    settings = get_app_settings()
    technician_list = settings.get('technician_list', [])
    technician_line_id = None
    for tech in technician_list:
        if tech.get('name') == job.assigned_technician:
            technician_line_id = tech.get('line_user_id')
            break
    
    if not technician_line_id:
        app.logger.warning(f"Cannot send assignment notification for job {job.id}: Technician '{job.assigned_technician}' has no LINE User ID.")
        return

    customer = job.customer
    
    message_text = (
        f"🔔 คุณได้รับมอบหมายงานใหม่!\n\n"
        f"ชื่องาน: {job.job_title}\n"
        f"ลูกค้า: {customer.name or '-'}\n"
        f"📞 โทร: {customer.phone or '-'}\n"
        f"🗓️ นัดหมาย: {job.due_date.astimezone(THAILAND_TZ).strftime('%d/%m/%y %H:%M') if job.due_date else '-'}\n\n"
        f"กรุณาตรวจสอบรายละเอียดและยืนยันการรับทราบ"
    )

    payload = {
        'recipient_line_id': technician_line_id,
        'notification_type': 'new_task',
        'task_id': job.id,
        'custom_message': message_text
    }
    _send_popup_notification(payload)
    app.logger.info(f"Assignment notification for job {job.id} queued for technician {job.assigned_technician} ({technician_line_id}).")

@app.route('/api/task/<int:job_id>/assign', methods=['POST'])
def api_assign_task(job_id):
    data = request.json
    technician_name = data.get('technician_name')

    job = Job.query.get(job_id)
    if not job:
        return jsonify({'status': 'error', 'message': 'ไม่พบงาน'}), 404

    job.assigned_technician = technician_name
    db.session.commit()
    
    if technician_name:
        send_assignment_notification(job)
        message = f'มอบหมายงานให้ {technician_name} และส่งแจ้งเตือนแล้ว'
    else:
        message = 'ยกเลิกการมอบหมายงานสำเร็จ'

    return jsonify({
        'status': 'success', 
        'message': message,
        'technician_name': technician_name or None
    })

@app.route('/api/task/<int:job_id>/add_internal_note', methods=['POST'])
def add_internal_note(job_id):
    data = request.json
    note_text = data.get('note_text', '').strip()
    user = data.get('user', 'Admin')

    if not note_text:
        return jsonify({'status': 'error', 'message': 'กรุณาพิมพ์ข้อความ'}), 400

    job_to_update = Job.query.get(job_id)
    if not job_to_update:
        return jsonify({'status': 'error', 'message': 'ไม่พบงาน'}), 404

    if not job_to_update.internal_notes:
        job_to_update.internal_notes = []
    
    new_note_data = {
        "user": user,
        "timestamp": datetime.datetime.now(THAILAND_TZ).isoformat(),
        "text": note_text,
        "is_internal": True
    }
    job_to_update.internal_notes.append(new_note_data)

    db.session.commit()
    
    try:
        settings = get_app_settings()
        customer = job_to_update.customer
        task_url = url_for('liff.customer_profile', customer_id=customer.id, job_id=job_to_update.id, _external=True)

        notification_message = (
            f"💬 ข้อความภายใน (งาน: {job_to_update.job_title or '-'})"
            f"👤 ลูกค้า: {customer.name or 'N/A'}\n"
            f"🗣️ โดย: {user}\n"
            f"📝 ข้อความ: {note_text}\n\n"
            f"🔗 เปิดดูที่นี่: {task_url}"
        )
        
        admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')
        if admin_group_id:
            message_queue.add_message(admin_group_id, TextMessage(text=notification_message))

    except Exception as e:
        current_app.logger.error(f"Failed to send internal note notification for job {job_id}: {e}")

    return jsonify({
        'status': 'success', 
        'message': 'เพิ่มบันทึกภายในเรียบร้อยแล้ว',
        'new_note': new_note_data
    })

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

def sanitize_filename(name, fallback='Unnamed'):
    """
    Sanitizes a string to be used as a filename. If the name is empty,
    it returns the fallback string.
    """
    if not name or not str(name).strip():
        return fallback

    # ใช้ str(name) เพื่อป้องกัน error หากข้อมูลไม่ใช่ string
    name_str = str(name).strip()

    # ลบอักขระพิเศษที่ไม่สามารถใช้ในชื่อไฟล์ได้
    return re.sub(r'[\\/*?:"<>|]', "", name_str)

@cached(cache)
def find_or_create_drive_folder(name, parent_id):
    service = get_google_drive_service()
    if not service:
        return None
    
    query = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    try:
        response = _execute_google_api_call_with_retry(service.files().list, q=query, spaces='drive', fields='files(id, name, parents)', pageSize=1)
        files = response.get('files', [])
        
        if files:
            folder_id = files[0]['id']
            app.logger.info(f"Found existing Drive folder '{name}' with ID: {folder_id}. Using this as the master.")
            return folder_id
        else:
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

            if save_app_settings(downloaded_settings):
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

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def _create_backup_zip():
    try:
        # Get tasks from SQLAlchemy
        all_customers = Customer.query.all()
        tasks_data = []

        # Helper function to convert datetime to string
        def default_serializer(o):
            if isinstance(o, (datetime.datetime, datetime.date, datetime.time)):
                return o.isoformat()
            raise TypeError(f"Type {type(o)} not serializable")

        for customer in all_customers:
            customer_data = parse_db_customer_data(customer)
            customer_data['jobs'] = [parse_db_job_data(job) for job in customer.jobs]
            for job_data in customer_data['jobs']:
                 job_id = job_data['id']
                 job_instance = Job.query.get(job_id)
                 job_data['reports'] = [parse_db_report_data(report) for report in job_instance.reports]
                 job_data['items'] = [item.to_dict() for item in job_instance.items]
            tasks_data.append(customer_data)
        
        memory_file = BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            # Use the helper function in json.dumps
            zf.writestr('data/tasks_backup.json', json.dumps(tasks_data, indent=4, ensure_ascii=False, default=default_serializer))
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
        'google_api_connected': check_google_api_status(),
        'thaizone': THAILAND_TZ  # <--- เพิ่มบรรทัดนี้เข้าไป
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

def _create_liff_notification_flex_message(recipient_line_id, notification_type, job, message_text, liff_base_url, **kwargs):
    
    full_liff_url = f"{liff_base_url}?type={notification_type}&job_id={job.id}"

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
    recipient_line_id = payload.get('recipient_line_id')
    notification_type = payload.get('notification_type')
    job_id = payload.get('job_id')
    
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

        job = Job.query.get(job_id)
        if not job:
            app.logger.warning(f"Job ID {job_id} not found for notification.")
            return False
            
        customer = job.customer
        
        message_text = payload.get('custom_message', '')

        flex_message = _create_liff_notification_flex_message(
            recipient_line_id, notification_type, job, message_text,
            liff_base_url, **payload
        )

        message_queue.add_message(recipient_line_id, flex_message)
        app.logger.info(f"Internal popup notification '{notification_type}' queued for {recipient_line_id}.")
        return True
    except Exception as e:
        app.logger.error(f"Error in _send_popup_notification: {e}", exc_info=True)
        return False

def send_new_task_notification(job):
    settings = get_app_settings()
    recipients = settings.get('line_recipients', {})
    admin_group_id = recipients.get('admin_group_id')
    
    if not admin_group_id: return

    customer = job.customer
    
    message_text = (
        f"✨ มีงานใหม่เข้า!\n\n"
        f"ชื่องาน: {job.job_title}\n"
        f"ลูกค้า: {customer.name or '-'}\n"
        f"📞 โทร: {customer.phone or '-'}\n"
        f"🗓️ นัดหมาย: {job.due_date.astimezone(THAILAND_TZ).strftime('%d/%m/%y %H:%M') if job.due_date else '-'}\n"
        f"📍 พิกัด: {customer.map_url or '-'}\n\n"
    )
    
    payload = {
        'recipient_line_id': admin_group_id,
        'notification_type': 'new_task',
        'job_id': job.id,
        'custom_message': message_text
    }
    
    _send_popup_notification(payload)

def send_completion_notification(job, report):
    settings = get_app_settings()
    recipients = settings.get('line_recipients', {})
    admin_group_id = recipients.get('admin_group_id')
    tech_group_id = recipients.get('technician_group_id')
    customer_line_id = job.customer.line_user_id

    if not any([admin_group_id, tech_group_id, customer_line_id]): return

    customer = job.customer
    technician_str = report.technicians if report.technicians else "ไม่ได้ระบุ"
    public_report_url = url_for('liff.public_task_report', job_id=job.id, _external=True)

    message_text_admin_tech = (
        f"✅ ปิดงานเรียบร้อย\n\n"
        f"ชื่องาน: {job.job_title or '-'}\n"
        f"ลูกค้า: {customer.name or '-'}\n"
        f"ช่างผู้รับผิดชอบ: {technician_str}\n\n"
    )

    sent_to = set()
    for recipient_id in [admin_group_id, tech_group_id]:
        if recipient_id and recipient_id not in sent_to:
            payload = {
                'recipient_line_id': recipient_id,
                'notification_type': 'completion',
                'job_id': job.id,
                'custom_message': message_text_admin_tech,
                'public_report_url': public_report_url
            }
            _send_popup_notification(payload)
            sent_to.add(recipient_id)
    
    if customer_line_id and settings.get('popup_notifications', {}).get('enabled_completion_customer'):
        payload = {
            'recipient_line_id': customer_line_id,
            'notification_type': 'completion',
            'job_id': job.id,
            'custom_message': settings.get('popup_notifications', {}).get('message_completion_customer_template', 'งาน [task_title] ที่บ้านคุณ [customer_name] เสร็จเรียบร้อยแล้วครับ/ค่ะ')\
                .replace('[task_title]', job.job_title or '-')\
                .replace('[customer_name]', customer.name or '-'),
            'public_report_url': public_report_url
        }
        _send_popup_notification(payload)

def send_update_notification(job, report):
    settings = get_app_settings()
    admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')
    if not admin_group_id: return

    customer = job.customer
    technician_str = report.technicians if report.technicians else "ไม่ได้ระบุ"
    
    title_prefix = "🗓️ อัปเดตงาน" if job.due_date and job.due_date.astimezone(THAILAND_TZ).date() == datetime.date.today() else "🗓️ เลื่อนนัดหมาย"

    message_text = (
        f"{title_prefix}\n\n"
        f"ชื่องาน: {job.job_title or '-'}\n"
        f"ลูกค้า: {customer.name or '-'}\n"
        f"📞 โทร: {customer.phone or '-'}\n"
        f"นัดหมายใหม่: {job.due_date.astimezone(THAILAND_TZ).strftime('%d/%m/%y %H:%M') if job.due_date else '-'}\n"
        f"สรุป: {report.work_summary or report.reason or '-'}\n"
        f"ช่าง: {technician_str}\n\n"
    )
    
    payload = {
        'recipient_line_id': admin_group_id,
        'notification_type': 'update',
        'job_id': job.id,
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
    with app.app_context():
        current_app.logger.info("Running scheduled overdue check job...")
        now = datetime.datetime.utcnow()
        
        overdue_records = BillingStatus.query.filter(
            BillingStatus.status == 'billed',
            BillingStatus.payment_due_date.isnot(None),
            BillingStatus.payment_due_date < now
        ).all()

        if overdue_records:
            for record in overdue_records:
                record.status = 'overdue'
                current_app.logger.info(f"Job {record.job_id} moved to overdue.")
            db.session.commit()
            current_app.logger.info(f"Updated {len(overdue_records)} jobs to overdue status.")        
        
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

        today_start = THAILAND_TZ.localize(datetime.datetime.combine(datetime.date.today(), datetime.time.min)).astimezone(pytz.utc)
        today_end = THAILAND_TZ.localize(datetime.datetime.combine(datetime.date.today(), datetime.time.max)).astimezone(pytz.utc)
        
        jobs = Job.query.filter(
            Job.status == 'needsAction',
            Job.due_date.between(today_start, today_end)
        ).all()
        
        if not jobs: return

        for job in sorted(jobs, key=lambda x: x.due_date):
            customer = job.customer
            message_text = render_template_message('daily_reminder_task_line', customer, job)
            liff_base_url = settings.get('popup_notifications', {}).get('liff_popup_base_url')
            
            sent_to = set()
            for recipient_id in [admin_group_id, technician_group_id]:
                if recipient_id and recipient_id not in sent_to:
                        if not liff_base_url:
                            message_queue.add_message(recipient_id, TextMessage(text=message_text + f"🔗 ดูรายละเอียด/แก้ไข:\n{url_for('liff.job_details', job_id=job.id, _external=True)}"))
                        else:
                            payload = {
                                'recipient_line_id': recipient_id,
                                'notification_type': 'new_task',
                                'job_id': job.id,
                                'custom_message': message_text
                            }
                            _send_popup_notification(payload)
                        sent_to.add(recipient_id)

def scheduled_customer_follow_up_job():
    with app.app_context():
        app.logger.info("Running scheduled customer follow-up job...")
        settings = get_app_settings()
        admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')

        now_utc = datetime.datetime.now(pytz.utc)
        two_days_ago_utc = now_utc - datetime.timedelta(days=2)
        one_day_ago_utc = now_utc - datetime.timedelta(days=1)
        
        jobs = Job.query.filter(
            Job.status == 'completed',
            Job.completed_date.between(two_days_ago_utc, one_day_ago_utc)
        ).all()

        for job in jobs:
            customer = job.customer
            if not customer.line_user_id: continue

            # Check if a follow-up has already been sent for this job
            if any(r.report_type == 'follow_up' for r in job.reports):
                continue

            flex_message = _create_customer_follow_up_flex_message(job.id, job.job_title, customer.name or 'N/A')

            try:
                message_queue.add_message(customer.line_user_id, flex_message)
                app.logger.info(f"Follow-up message for job {job.id} added to queue for customer {customer.line_user_id}.")
                
                # Create a special report entry to mark that a follow-up was sent
                new_report = Report(
                    job=job,
                    summary_date=datetime.datetime.now(THAILAND_TZ),
                    report_type='follow_up',
                    work_summary='Sent automated customer follow-up message.',
                    is_internal=True
                )
                db.session.add(new_report)
                db.session.commit()
            except Exception as e:
                app.logger.error(f"Failed to add direct follow-up to {customer.line_user_id} to queue: {e}. Notifying admin.")
                if admin_group_id:
                    admin_notification_messages = [
                        TextMessage(text=f"⚠️ ส่ง Follow-up ให้ลูกค้า {customer.name} (Job ID: {job.id}) ไม่สำเร็จ โปรดส่งข้อความนี้แทน:"),
                        flex_message
                    ]
                    message_queue.add_message(admin_group_id, admin_notification_messages)
            except Exception as e:
                app.logger.warning(f"Could not process job {job.id} for follow-up: {e}", exc_info=True)

def scheduled_nearby_job_alert_job():
    with app.app_context():
        app.logger.info("Running scheduled nearby job alert job...")
        
        settings = get_app_settings()
        popup_settings = settings.get('popup_notifications', {})
        if not popup_settings.get('enabled_nearby_job'):
            app.logger.info("Nearby job alert is disabled. Skipping.")
            return

        nearby_radius_km = float(popup_settings.get('nearby_radius_km', 5))
        
        technician_list = settings.get('technician_list', [])
        locations = load_technician_locations()

        active_technicians = []
        for tech in technician_list:
            if tech.get('line_user_id') in locations:
                tech_location = locations[tech['line_user_id']]
                tech['last_known_lat'] = tech_location.get('lat')
                tech['last_known_lon'] = tech_location.get('lon')
                active_technicians.append(tech)

        pending_jobs = Job.query.filter(
            Job.status == 'needsAction',
            Job.assigned_technician == None,
            Job.customer.has(Customer.map_url.isnot(None))
        ).all()

        for technician in active_technicians:
            tech_coords = (technician['last_known_lat'], technician['last_known_lon'])
            for job in pending_jobs:
                customer = job.customer
                
                if f"NEARBY_ALERT_SENT_TO_TECH_{technician['line_user_id']}" in job.internal_notes or job.assigned_technician:
                    continue

                map_url = customer.map_url
                match = re.search(r"(\-?\d+\.\d+),(\-?\d+\.\d+)", map_url)
                
                if match:
                    task_lat, task_lon = float(match.group(1)), float(match.group(2))
                    task_coords = (task_lat, task_lon)
                    
                    try:
                        distance_km = geodesic(tech_coords, task_coords).km
                        if distance_km <= nearby_radius_km:
                            payload = {
                                'recipient_line_id': technician['line_user_id'],
                                'notification_type': 'nearby_job',
                                'job_id': job.id,
                                'technician_name': technician['name'],
                                'distance_km': distance_km,
                                'customer_name': customer.name or '',
                                'customer_phone': customer.phone or '',
                                'customer_address': customer.address or '',
                                'customer_map_url': customer.map_url or '',
                                'shop_phone': settings.get('shop_info', {}).get('contact_phone', ''),
                                'logo_url': url_for('static', filename='logo.png', _external=True)
                            }
                            if _send_popup_notification(payload):
                                app.logger.info(f"Nearby job notification triggered for technician {technician['name']} (Job: {job.id}). Distance: {distance_km:.1f} km.")
                                
                                if not job.internal_notes: job.internal_notes = []
                                job.internal_notes.append(f"NEARBY_ALERT_SENT_TO_TECH_{technician['line_user_id']}_{datetime.datetime.now(THAILAND_TZ).isoformat()}")
                                db.session.commit()
                            else:
                                app.logger.error(f"Failed to trigger nearby job notification for {technician['name']} and job {job.id}")
                                
                    except Exception as e:
                        app.logger.warning(f"Could not calculate distance for job {job.id} / technician {technician['name']}: {e}")
                    
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
 
    scheduler.add_job(scheduled_overdue_check_job, CronTrigger(hour=8, minute=0), id='daily_overdue_check', replace_existing=True)
    app.logger.info("Scheduled daily overdue invoice check for 08:00 Thai time.")
 
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
    try:
        query = request.args.get('q', '').strip().lower()
        
        if len(query) < 2:
            return jsonify([])

        all_customers = get_customer_database()

        results = []
        for customer in all_customers:
            customer_name = customer.get('name', '') or ''
            customer_org = customer.get('organization', '') or ''
            searchable_text = f"{customer_name} {customer_org}".lower()
            
            if query in searchable_text:
                results.append(customer)
            
            if len(results) >= 10:
                break
        
        return jsonify(results)
    
    except Exception as e:
        app.logger.error(f"Error in api_search_customers: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/api/products', methods=['POST'])
def api_add_product():
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
            'category': data.get('category', 'ไม่มีหมวดหมู่'),
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
    data = request.json
    try:
        settings = get_app_settings()
        catalog = settings.get('equipment_catalog', [])
        if not (0 <= item_index < len(catalog)):
            return jsonify({'status': 'error', 'message': 'ไม่พบสินค้าที่ต้องการแก้ไข'}), 404
        
        catalog[item_index]['item_name'] = data.get('item_name', catalog[item_index]['item_name']).strip()
        catalog[item_index]['category'] = data.get('category', catalog[item_index].get('category', 'ไม่มีหมวดหมู่'))
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
    data = request.json
    change = data.get('change', 0)

    try:
        settings = get_app_settings()
        catalog = settings.get('equipment_catalog', [])

        if not (0 <= item_index < len(catalog)):
            return jsonify({'status': 'error', 'message': 'ไม่พบสินค้า'}), 404

        current_stock = int(catalog[item_index].get('stock_quantity', 0))
        new_stock = current_stock + change
        catalog[item_index]['stock_quantity'] = new_stock

        if save_app_settings({'equipment_catalog': catalog}):
            backup_settings_to_drive()
            return jsonify({'status': 'success', 'new_stock': new_stock})
        else:
            raise Exception("ไม่สามารถบันทึกการตั้งค่าได้")

    except Exception as e:
        app.logger.error(f"Error in api_adjust_stock: {e}")
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดฝั่งเซิร์ฟเวอร์'}), 500

@app.route('/api/products/<int:item_index>', methods=['DELETE'])
def api_delete_product(item_index):
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

@app.route('/api/task/<int:job_id>/items', methods=['GET'])
def get_task_items(job_id):
    items = JobItem.query.filter_by(job_id=job_id).order_by(JobItem.added_at.asc()).all()
    return jsonify([item.to_dict() for item in items])

@app.route('/api/task/<int:job_id>/items', methods=['POST'])
@csrf.exempt
def add_task_items(job_id):
    data = request.json
    items_data = data.get('items', [])
    
    try:
        job = Job.query.get(job_id)
        if not job:
            return jsonify({'status': 'error', 'message': 'ไม่พบข้อมูลงาน'}), 404
        
        warehouse_to_use = None
        added_by_user = "Admin"
        assigned_technicians_str = job.assigned_technician
        
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

        # --- จุดที่แก้ไข ---
        # ได้นำบรรทัด JobItem.query.filter_by(job_id=job_id).delete() ออกจากจุดนี้
        # เพื่อให้ฟังก์ชันทำการ "เพิ่ม" ข้อมูลใหม่เข้าไปเสมอ แทนการ "เขียนทับ"
        # และได้นำโค้ดคืนสต็อกที่เกี่ยวข้องออกไปด้วย
        # ------------------

        if items_data:
            settings = get_app_settings()
            catalog = settings.get('equipment_catalog', [])
            catalog_dict_by_name = {item['item_name'].lower(): item for item in catalog}
            catalog_changed = False

            # วนลูปเพื่อเพิ่มรายการใหม่ที่ส่งเข้ามา
            for item_data in items_data:
                item_name_lower = item_data['item_name'].lower()
                if item_name_lower not in catalog_dict_by_name:
                    # หากเป็นสินค้าใหม่ที่ไม่มีในระบบ ให้เพิ่มเข้าไปใน catalog
                    new_product = {
                        'item_name': item_data['item_name'],
                        'category': 'ไม่มีหมวดหมู่',
                        'product_code': item_data['item_name'],
                        'unit': 'ชิ้น',
                        'price': float(item_data.get('unit_price', 0)),
                        'cost_price': 0,
                        'stock_quantity': 0,
                        'image_url': ''
                    }
                    catalog.append(new_product)
                    catalog_dict_by_name[item_name_lower] = new_product
                    catalog_changed = True

                # สร้างรายการ JobItem ใหม่
                new_job_item = JobItem(
                    job_id=job_id, item_name=item_data['item_name'],
                    quantity=float(item_data['quantity']), unit_price=float(item_data.get('unit_price', 0)),
                    added_by=added_by_user
                )
                db.session.add(new_job_item)
                db.session.flush()

                # ทำการตัดสต็อก
                product_code = catalog_dict_by_name.get(item_name_lower, {}).get('product_code', item_data['item_name'])
                quantity_used = float(item_data['quantity'])

                stock_level = StockLevel.query.filter_by(product_code=product_code, warehouse_id=warehouse_id_to_use).first()
                if not stock_level:
                    stock_level = StockLevel(product_code=product_code, warehouse_id=warehouse_id_to_use, quantity=0)
                    db.session.add(stock_level)
                
                stock_level.quantity -= quantity_used

                # บันทึกการเคลื่อนไหวของสต็อก
                movement = StockMovement(
                    product_code=product_code, quantity_change=quantity_used,
                    from_warehouse_id=warehouse_id_to_use, to_warehouse_id=None,
                    movement_type='sale_consumption', job_item_id=new_job_item.id,
                    notes=f"Used in Job:{job_id}", user=added_by_user
                )
                db.session.add(movement)

            if catalog_changed:
                save_app_settings({'equipment_catalog': catalog})
                backup_settings_to_drive()
        
        db.session.commit()
        # เปลี่ยนข้อความตอบกลับให้ชัดเจนขึ้น
        return jsonify({'status': 'success', 'message': f'เพิ่มรายการและตัดสตอกจากคลัง "{warehouse_to_use.name}" เรียบร้อยแล้ว'}), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving job items for job {job_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกรายการ'}), 500

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving job items for job {job_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกรายการ'}), 500

@app.route('/api/items/use', methods=['POST'])
def api_use_items():
    data = request.json
    items_used = data.get('items', [])
    if not items_used:
        return jsonify({'status': 'error', 'message': 'No items provided'}), 400

    try:
        settings = get_app_settings()
        catalog = settings.get('equipment_catalog', [])
        catalog_dict = {item['item_name']: item for item in catalog}

        for used_item in items_used:
            item_name = used_item.get('item_name')
            quantity_used = used_item.get('quantity', 0)

            if item_name in catalog_dict:
                current_stock = catalog_dict[item_name].get('stock_quantity', 0)
                catalog_dict[item_name]['stock_quantity'] = max(0, current_stock - quantity_used)

        updated_catalog = list(catalog_dict.values())
        if save_app_settings({'equipment_catalog': updated_catalog}):
            return jsonify({'status': 'success', 'message': 'ตัดสต็อกเรียบร้อยแล้ว'})
        else:
            raise Exception("Failed to save updated catalog")

    except Exception as e:
        app.logger.error(f"Error processing stock deduction: {e}")
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการตัดสต็อก'}), 500

@app.route('/api/equipment_catalog')
def api_equipment_catalog():
    settings = get_app_settings()
    catalog = settings.get('equipment_catalog', [])
    return jsonify(catalog)

@app.route('/api/task_summary/<int:job_id>')
def api_task_summary(job_id):
    job = Job.query.get(job_id)
    if not job:
        return jsonify({'error': 'Task not found'}), 404
    
    customer = job.customer
    
    summary_data = {
        'id': job.id,
        'title': job.job_title,
        'due_formatted': job.due_date.astimezone(THAILAND_TZ).strftime("%d/%m/%y %H:%M") if job.due_date else None,
        'customer': {
            'name': customer.name,
            'phone': customer.phone,
            'address': customer.address,
            'map_url': customer.map_url
        },
        'task_details_url': url_for('liff.job_details', customer_id=customer.id, job_id=job.id, _external=True)
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
        locations = load_technician_locations()
        
        locations[line_user_id] = {
            'lat': float(lat),
            'lon': float(lon),
            'timestamp': datetime.datetime.now(THAILAND_TZ).isoformat()
        }
        
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
    job_id = request.form.get('job_id')
    customer_id = request.form.get('customer_id')

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

    job = Job.query.filter_by(id=job_id, customer_id=customer_id).first()
    if not job:
        return jsonify({'status': 'error', 'message': 'Job not found'}), 404

    customer = job.customer
    
    monthly_folder_name = job.created_date.astimezone(THAILAND_TZ).strftime('%Y-%m')
    monthly_folder_id = find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)
    if not monthly_folder_id:
        return jsonify({'status': 'error', 'message': f'Could not create or find monthly folder: {monthly_folder_name}'}), 500

    sanitized_customer_name = sanitize_filename(job.customer.name, fallback=f"Customer_{job.customer.id}")
    customer_job_folder_name = f"{sanitized_customer_name} - {job_id}"
    final_upload_folder_id = find_or_create_drive_folder(customer_job_folder_name, monthly_folder_id)

    if not final_upload_folder_id:
        return jsonify({'status': 'error', 'message': 'Could not determine final upload folder'}), 500

    media_body = MediaIoBaseUpload(file_to_upload, mimetype=mime_type, resumable=True)
    drive_file = _perform_drive_upload(media_body, filename, mime_type, final_upload_folder_id)

    if drive_file:
        return jsonify({'status': 'success', 'file_info': {'id': drive_file.get('id'), 'url': drive_file.get('webViewLink'), 'name': filename}})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to upload to Google Drive'}), 500

def compress_image_to_fit(file, max_size_bytes):
    try:
        img = Image.open(file)
        
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        
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
    jobs = Job.query.all()
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    today_thai = date.today()
    final_jobs = []
    
    for job in jobs:
        is_overdue = False
        is_today = False
        if job.status == 'needsAction' and job.due_date:
            due_dt_local = job.due_date.astimezone(THAILAND_TZ)
            if due_dt_local.date() < today_thai: is_overdue = True
            elif due_dt_local.date() == today_thai: is_today = True
        
        job_passes_filter = False
        if status_filter == 'all': job_passes_filter = True
        elif status_filter == 'completed' and job.status == 'completed': job_passes_filter = True
        elif status_filter == 'needsAction' and job.status == 'needsAction': job_passes_filter = True
        elif status_filter == 'today' and is_today: job_passes_filter = True
        elif status_filter == 'external' and job.job_type == 'external': job_passes_filter = True

        if job_passes_filter:
            customer = job.customer
            searchable_text = f"{job.job_title} {customer.name or ''} {customer.organization or ''} {customer.phone or ''}".lower()
            if not search_query or search_query in searchable_text:
                final_jobs.append(job)

    final_jobs.sort(key=lambda x: (x.status == 'completed', x.due_date is None, x.due_date if x.due_date else datetime.datetime.max.replace(tzinfo=pytz.utc)))
    
    return render_template("summary_print.html",
                           tasks=final_jobs,
                           search_query=search_query,
                           status_filter=status_filter,
                           now=datetime.datetime.now(THAILAND_TZ))

@app.route('/api/calendar_tasks')
def api_calendar_tasks():
    try:
        jobs = Job.query.all()
        events = []
        today_thai = datetime.datetime.now(THAILAND_TZ).date()

        for job in jobs:
            if not job.due_date: continue # ไม่สร้าง event ถ้าไม่มีวันนัดหมาย
            
            customer = job.customer
            is_overdue = False
            is_today = False
            is_completed = job.status == 'completed' # <--- เพิ่มตัวแปรนี้

            try:
                due_dt_local = job.due_date.astimezone(THAILAND_TZ)
                if not is_completed and due_dt_local.date() < today_thai:
                    is_overdue = True
                elif not is_completed and due_dt_local.date() == today_thai:
                    is_today = True
            except (ValueError, TypeError):
                pass

            event = {
                'id': job.id,
                'title': f"{customer.name or 'N/A'} - {job.job_title}",
                'start': job.due_date.isoformat().replace('+00:00', 'Z'),
                'url': url_for('liff.job_details', customer_id=customer.id, job_id=job.id),
                'extendedProps': {
                    'is_completed': is_completed, # <--- เพิ่ม property นี้
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
    job_id = data.get('job_id')
    new_due_str = data.get('new_due_date')
    
    if not job_id or not new_due_str:
        return jsonify({'status': 'error', 'message': 'ข้อมูลที่ส่งมาไม่ครบถ้วน (job_id หรือ new_due_date)'}), 400
        
    try:
        job = Job.query.get(job_id)
        if not job:
            return jsonify({'status': 'error', 'message': 'ไม่พบงานที่ระบุ'}), 404
        if job.status == 'completed':
            return jsonify({'status': 'error', 'message': 'ไม่สามารถย้ายงานที่เสร็จสิ้นแล้วได้'}), 403

        job.due_date = date_parse(new_due_str).astimezone(pytz.utc)
        job.status = 'needsAction'
        db.session.commit()
        
        return jsonify({'status': 'success', 'message': f'อัปเดตวันนัดหมายสำหรับงาน {job_id} เรียบร้อยแล้ว'})
            
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error scheduling task from calendar: {e}")
        return jsonify({'status': 'error', 'message': f'เกิดข้อผิดพลาดในระบบ: {e}'}), 500

@app.route('/api/task/<int:job_id>/update_location', methods=['POST'])
def api_update_task_location(job_id):
    data = request.json
    new_map_url = data.get('map_url')

    if not new_map_url:
        return jsonify({'status': 'error', 'message': 'ไม่พบข้อมูลพิกัดใหม่'}), 400

    job = Job.query.get(job_id)
    if not job:
        return jsonify({'status': 'error', 'message': 'ไม่พบงานที่ต้องการอัปเดต'}), 404

    try:
        job.customer.map_url = new_map_url
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'อัปเดตพิกัดเรียบร้อยแล้ว'})

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error updating task location for {job_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดฝั่งเซิร์ฟเวอร์'}), 500

@app.route('/api/task/<int:job_id>/edit_report_text/<int:report_id>', methods=['POST'])
def api_edit_report_text(job_id, report_id):
    data = request.json
    new_summary = data.get('summary', '').strip()
    
    if not new_summary:
        return jsonify({'status': 'error', 'message': 'กรุณากรอกสรุปงาน'}), 400

    report_to_edit = Report.query.filter_by(id=report_id, job_id=job_id).first()
    if not report_to_edit:
        return jsonify({'status': 'error', 'message': 'ไม่พบรายงานที่ต้องการแก้ไข'}), 404

    report_to_edit.work_summary = new_summary
    db.session.commit()
    
    return jsonify({'status': 'success', 'message': 'แก้ไขรายงานเรียบร้อยแล้ว'})

@app.route('/api/task/<int:job_id>/delete_report/<int:report_id>', methods=['POST'])
def delete_task_report(job_id, report_id):
    report_to_delete = Report.query.filter_by(id=report_id, job_id=job_id).first()
    if not report_to_delete:
        return jsonify({'status': 'error', 'message': 'ไม่พบรายงานที่ต้องการลบ'}), 404

    drive_service = get_google_drive_service()
    if drive_service:
        for att in report_to_delete.attachments:
            try:
                drive_service.files().delete(fileId=att.drive_file_id).execute()
                app.logger.info(f"Deleted attachment {att.drive_file_id} from Drive while deleting report.")
            except HttpError as e:
                app.logger.error(f"Failed to delete attachment {att.drive_file_id} from Drive during report deletion: {e}")

    db.session.delete(report_to_delete)
    db.session.commit()
    
    return jsonify({'status': 'success', 'message': 'ลบรายงานเรียบร้อยแล้ว'})

@app.route('/delete_task/<int:job_id>', methods=['POST'])
def delete_task(job_id):
    job = Job.query.get(job_id)
    if job:
        db.session.delete(job)
        db.session.commit()
        flash('ลบงานเรียบร้อยแล้ว!', 'success')
    else:
        flash('เกิดข้อผิดพลาดในการลบงาน', 'danger')
    return redirect(url_for('liff.summary'))

@app.route('/api/delete_task/<int:job_id>', methods=['POST'])
def api_delete_task(job_id):
    job = Job.query.get(job_id)
    if job:
        db.session.delete(job)
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'Task deleted successfully.'})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to delete task.'}), 500

@app.route('/api/job/<int:job_id>/reopen', methods=['POST'])
def api_reopen_job(job_id):
    job = Job.query.get(job_id)
    if not job or job.status != 'completed':
        return jsonify({'status': 'error', 'message': 'ไม่พบงานที่เสร็จสิ้นแล้ว'}), 404

    try:
        problem_description = request.form.get('problem_description')
        if not problem_description:
            return jsonify({'status': 'error', 'message': 'กรุณาระบุรายละเอียดปัญหา'}), 400

        liff_user_id = request.form.get('technician_line_user_id')
        technician_name = "ไม่ระบุ"
        if liff_user_id:
            settings = get_app_settings()
            tech_info = next((tech for tech in settings.get('technician_list', []) if tech.get('line_user_id') == liff_user_id), None)
            if tech_info:
                technician_name = tech_info.get('name', "ไม่ระบุ")
        
        # 1. เปลี่ยนสถานะและล้างวันที่เสร็จ
        job.status = 'needsAction'
        job.completed_date = None
        
        # 2. ตั้งวันนัดหมายใหม่ (ถ้ามี)
        new_due_str = request.form.get('new_due_date')
        if new_due_str:
            dt_local = THAILAND_TZ.localize(date_parse(new_due_str))
            job.due_date = dt_local.astimezone(pytz.utc)

        # 3. สร้าง Report เพื่อเป็นหลักฐานการเปิดงานซ้ำ
        reopen_report = Report(
            job=job,
            report_type='reopened',
            work_summary=f"เปิดงานอีกครั้งเนื่องจาก: {problem_description}",
            technicians=technician_name,
            is_internal=False
        )
        db.session.add(reopen_report)
        db.session.commit()

        flash('เปิดงานอีกครั้งเนื่องจากมีปัญหาเรียบร้อยแล้ว!', 'success')
        return jsonify({'status': 'success', 'message': 'เปิดงานอีกครั้งเรียบร้อยแล้ว'})
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error reopening job {job_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดฝั่งเซิร์ฟเวอร์'}), 500

@app.route('/api/delete_tasks_batch', methods=['POST'])
def api_delete_tasks_batch():
    data = request.json
    job_ids = data.get('job_ids', [])
    if not isinstance(job_ids, list):
        return jsonify({'status': 'error', 'message': 'Invalid input format.'}), 400

    deleted_count, failed_count = 0, 0
    for job_id in job_ids:
        job = Job.query.get(job_id)
        if job:
            db.session.delete(job)
            db.session.commit()
            deleted_count += 1
        else:
            failed_count += 1

    return jsonify({
        'status': 'success',
        'message': f'ลบงานสำเร็จ: {deleted_count} รายการ, ล้มเหลว: {failed_count} รายการ.',
        'deleted_count': deleted_count,
        'failed_count': failed_count
    })

@app.route('/api/update_tasks_status_batch', methods=['POST'])
def api_update_tasks_status_batch():
    data = request.json
    job_ids = data.get('job_ids', [])
    new_status = data.get('status')

    if not all([isinstance(job_ids, list), new_status in ['needsAction', 'completed']]):
        return jsonify({'status': 'error', 'message': 'Invalid input data.'}), 400

    updated_count, failed_count = 0, 0
    for job_id in job_ids:
        job = Job.query.get(job_id)
        if job:
            job.status = new_status
            if new_status == 'completed': job.completed_date = datetime.datetime.utcnow()
            db.session.commit()
            updated_count += 1
        else:
            failed_count += 1

    return jsonify({
        'status': 'success',
        'message': f'อัปเดตสถานะสำเร็จ: {updated_count} รายการ, ล้มเหลว: {failed_count} รายการ.',
        'updated_count': updated_count,
        'failed_count': failed_count
    })

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('settings_page'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user, remember=True)
            return redirect(url_for('settings_page'))
        else:
            flash('ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('ออกจากระบบเรียบร้อยแล้ว', 'success')
    return redirect(url_for('login'))
    
@app.route('/api/users', methods=['GET'])
@login_required
@admin_required
def get_users():
    users = User.query.all()
    return jsonify([user.to_dict() for user in users])

@app.route('/api/users', methods=['POST'])
@login_required
@admin_required
def save_user():
    data = request.json
    if not data.get('username') or not data.get('password') or not data.get('role'):
        return jsonify({'status': 'error', 'message': 'กรุณากรอกข้อมูลให้ครบถ้วน'}), 400

    user_id = data.get('id')
    if user_id: # Edit existing user
        user = User.query.get(user_id)
        if not user:
            return jsonify({'status': 'error', 'message': 'ไม่พบผู้ใช้งาน'}), 404
    else: # Add new user
        if User.query.filter_by(username=data['username']).first():
            return jsonify({'status': 'error', 'message': 'ชื่อผู้ใช้งานนี้มีอยู่แล้ว'}), 409
        user = User()
        db.session.add(user)

    user.username = data['username']
    user.role = data['role']
    if data['password']: # Only update password if provided
        user.set_password(data['password'])

    db.session.commit()
    return jsonify({'status': 'success', 'message': 'บันทึกข้อมูลผู้ใช้เรียบร้อยแล้ว', 'user': user.to_dict()})

@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@login_required
@admin_required
def delete_user(user_id):
    if user_id == current_user.id:
        return jsonify({'status': 'error', 'message': 'ไม่สามารถลบบัญชีของตัวเองได้'}), 403

    user = User.query.get(user_id)
    if not user:
        return jsonify({'status': 'error', 'message': 'ไม่พบผู้ใช้งาน'}), 404

    db.session.delete(user)
    db.session.commit()
    return jsonify({'status': 'success', 'message': 'ลบผู้ใช้งานเรียบร้อยแล้ว'})
    
@app.route('/settings', methods=['GET', 'POST'])
@login_required
@admin_required
def settings_page():
    if request.method == 'POST':
        try:
            data = request.json
            if not data:
                return jsonify({'status': 'error', 'message': 'ไม่พบข้อมูลที่ส่งมา'}), 400

            current_settings = get_app_settings()

            if 'report_times' in data:
                current_settings['report_times'].update(data['report_times'])
            
            if 'message_templates' in data:
                current_settings['message_templates'].update(data['message_templates'])

            if 'popup_notifications' in data:
                pn_data = data['popup_notifications']
                for key in ['enabled_arrival', 'enabled_completion_customer', 'enabled_nearby_job']:
                    current_settings['popup_notifications'][key] = bool(pn_data.get(key, False))
                
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

            if 'technician_templates' in data:
                templates_data = data.get('technician_templates', {})
                if isinstance(templates_data, dict) and 'task_details' in templates_data and 'progress_reports' in templates_data:
                    current_settings['technician_templates'] = templates_data
                else:
                     return jsonify({'status': 'error', 'message': 'รูปแบบข้อมูลเทมเพลตไม่ถูกต้อง'}), 400

            if save_app_settings(current_settings):
                cache.clear()
                run_scheduler()
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
    
    settings = get_app_settings()
    warehouses = Warehouse.query.order_by(Warehouse.id).all()
    return render_template('settings_page.html',
                           settings=settings,
                           warehouses=warehouses)
 
@app.route('/api/upload_avatar', methods=['POST'])
def api_upload_avatar():
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file'}), 400

    file_to_upload, filename, mime_type = _handle_image_upload(file, MAX_FILE_SIZE_MB)

    if not file_to_upload:
        error_message, error_code, _ = file_to_upload, filename, mime_type
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
    response.headers['Content-Security-Policy'] = "img-src 'self' drive.google.com *.googleusercontent.com via.placeholder.com data:;"
    return response

def _handle_image_upload(file_storage, max_size_mb):
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
                for quality in range(90, 20, -10):
                    output_buffer.seek(0)
                    output_buffer.truncate()
                    img.save(output_buffer, format='JPEG', quality=quality, optimize=True)
                    if output_buffer.tell() <= max_size_bytes:
                        output_buffer.seek(0)
                        filename = os.path.splitext(original_filename)[0] + '.jpg'
                        current_app.logger.info(f"Compressed image '{original_filename}' with quality={quality}.")
                        return output_buffer, filename, 'image/jpeg'
                
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
        return file_storage, original_filename, file_storage.mimetype or mimetypes.guess_type(original_filename)[0]

@app.route('/api/upload_product_image', methods=['POST'])
def api_upload_product_image():
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file'}), 400

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
            app.logger.info(f"Compressed product image '{file.filename}' successfully.")

        except Exception as e:
            app.logger.error(f"Could not process product image '{file.filename}': {e}")
            return jsonify({'status': 'error', 'message': 'ไฟล์รูปภาพไม่ถูกต้อง'}), 400
    else:
        return jsonify({'status': 'error', 'message': 'รองรับเฉพาะไฟล์รูปภาพเท่านั้น'}), 400

    product_images_folder_id = find_or_create_drive_folder("Product_Images", GOOGLE_DRIVE_FOLDER_ID)
    if not product_images_folder_id:
        return jsonify({'status': 'error', 'message': 'Could not create or find Product_Images folder in Drive'}), 500

    media_body = MediaIoBaseUpload(file_to_upload, mimetype=mime_type, resumable=True)
    drive_file = _perform_drive_upload(media_body, filename, mime_type, product_images_folder_id)
    
    if drive_file:
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
    
    assets_folder_id = find_or_create_drive_folder("Company_Assets", GOOGLE_DRIVE_FOLDER_ID)
    if not assets_folder_id:
        return jsonify({'status': 'error', 'message': 'Could not create or find Company_Assets folder'}), 500

    filename = "payment_qr_code.png"
    mime_type = file.mimetype or 'image/png'
    
    file_bytes = BytesIO(file.read())
    media_body = MediaIoBaseUpload(file_bytes, mimetype=mime_type, resumable=True)
    
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
            job = Job.query.order_by(Job.created_date.desc()).first()
            if not job:
                flash('ไม่พบข้อมูลงานสำหรับใช้ทดสอบ', 'warning')
                return redirect(url_for('settings_page'))

            if test_type == 'customer_completion':
                payload = {
                    'recipient_line_id': recipient_id, 'notification_type': 'completion',
                    'job_id': job.id, 'public_report_url': url_for('liff.public_task_report', job_id=job.id, _external=True)
                }
                _send_popup_notification(payload)

            elif test_type == 'customer_follow_up':
                flex_message_to_send = _create_customer_follow_up_flex_message(job.id, job.job_title, job.customer.name or 'N/A')
                message_queue.add_message(recipient_id, flex_message_to_send)

            elif test_type == 'admin_new_task':
                send_new_task_notification(job) 

        flash(f'ส่งข้อความทดสอบประเภท "{test_type}" ไปยัง ID: {recipient_id} สำเร็จ!', 'success')
    except Exception as e:
        flash(f'เกิดข้อผิดพลาดในการส่ง: {e}', 'danger')
        app.logger.error(f"Error in test_notification: {e}", exc_info=True)
        
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
            # This logic is for restoring old Google Tasks data into the new SQL database
            if not isinstance(data, list):
                return jsonify({"status": "error", "message": "JSON is not a list."}), 400

            created_customers, created_jobs, skipped = 0, 0, 0
            for customer_data in data:
                try:
                    new_customer = Customer(
                        name=customer_data['name'],
                        organization=customer_data.get('organization'),
                        phone=customer_data.get('phone'),
                        address=customer_data.get('address'),
                        map_url=customer_data.get('map_url'),
                        line_user_id=customer_data.get('line_user_id'),
                        created_at=date_parse(customer_data['created_at'])
                    )
                    db.session.add(new_customer)
                    db.session.flush()
                    created_customers += 1

                    for job_data in customer_data.get('jobs', []):
                        new_job = Job(
                            customer=new_customer,
                            job_title=job_data['job_title'],
                            job_type=job_data.get('job_type', 'service'),
                            assigned_technician=job_data.get('assigned_technician'),
                            status=job_data.get('status'),
                            created_date=date_parse(job_data['created_date']),
                            due_date=date_parse(job_data['due_date']) if job_data.get('due_date') else None,
                            completed_date=date_parse(job_data['completed_date']) if job_data.get('completed_date') else None,
                            product_details=job_data.get('product_details'),
                            internal_notes=job_data.get('internal_notes')
                        )
                        db.session.add(new_job)
                        db.session.flush()
                        created_jobs += 1

                        for report_data in job_data.get('reports', []):
                            new_report = Report(
                                job=new_job,
                                summary_date=date_parse(report_data['summary_date']),
                                report_type=report_data['report_type'],
                                work_summary=report_data.get('work_summary'),
                                technicians=','.join(report_data.get('technicians', [])) if isinstance(report_data.get('technicians'), list) else report_data.get('technicians'),
                                is_internal=report_data.get('is_internal', False)
                            )
                            db.session.add(new_report)
                            db.session.flush()
                            for att_data in report_data.get('attachments', []):
                                new_att = Attachment(
                                    report=new_report,
                                    drive_file_id=att_data['id'],
                                    file_name=att_data.get('name'),
                                    file_url=att_data.get('url')
                                )
                                db.session.add(new_att)
                    db.session.commit()
                except Exception as e:
                    db.session.rollback()
                    skipped += 1
            return jsonify({"status": "success", "message": f"นำเข้าสำเร็จ! สร้างลูกค้าใหม่: {created_customers}, สร้างงาน: {created_jobs}, ข้าม: {skipped}"})
        elif file_type == 'settings_json':
            if not isinstance(data, dict): return jsonify({"status": "error", "message": "JSON is not a dict."}), 400
            if save_app_settings(data):
                run_scheduler(); cache.clear()
                return jsonify({"status": "success", "message": "นำเข้าการตั้งค่าเรียบร้อยแล้ว!"})
            else: return jsonify({"status": "error", "message": "เกิดข้อผิดพลาดในการบันทึกการตั้งค่า"}), 500
    except Exception as e:
        db.session.rollback()
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
            examples = [{'title': t.get('title', 'N/A'), 'customer_name': t.get('customer', {}).get('name', 'N/A')} for t in data[:5]]
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
    jobs = Job.query.order_by(Job.created_date.desc()).all()
    duplicates = defaultdict(list)
    for job in jobs:
        if job.job_title:
            customer_name = job.customer.name.strip().lower() if job.customer else ''
            duplicates[(job.job_title.strip(), customer_name)].append(job)
    
    sets = {k: sorted(v, key=lambda t: t.created_date, reverse=True) for k, v in duplicates.items() if len(v) > 1}
    return render_template('duplicates.html', duplicates=sets)

@app.route('/delete_duplicates_batch', methods=['POST'])
def delete_duplicates_batch():
    ids = request.form.getlist('job_ids')
    if not ids:
        flash('ไม่พบรายการที่เลือกเพื่อลบ', 'warning')
        return redirect(url_for('manage_duplicates'))
    deleted, failed = 0, 0
    for job_id in ids:
        job = Job.query.get(job_id)
        if job:
            db.session.delete(job)
            db.session.commit()
            deleted += 1
        else:
            failed += 1
    
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
@csrf.exempt
def callback():
    try:
        signature = request.headers.get('X-Line-Signature')
        
        app.logger.info("=== DEBUG WEBHOOK START ===")
        app.logger.info(f"Request method: {request.method}")
        app.logger.info(f"Request headers: {dict(request.headers)}")
        app.logger.info(f"Content-Type: {request.content_type}")
        app.logger.info(f"Request URL: {request.url}")
        
        if not signature:
            app.logger.error("❌ Missing X-Line-Signature header")
            return 'Missing signature', 400

        body = request.get_data()
        
        app.logger.info(f"📦 Received webhook request - Body length: {len(body)}")
        app.logger.info(f"🔑 Signature present: {bool(signature)}")
        app.logger.info(f"📄 Body content (first 500 chars): {body.decode('utf-8')[:500]}...")
        
        if not LINE_CHANNEL_SECRET:
            app.logger.error("❌ LINE_CHANNEL_SECRET is not configured")
            return 'Channel secret not configured', 500
            
        app.logger.info(f"✅ Channel Secret configured: {bool(LINE_CHANNEL_SECRET)}")
        app.logger.info(f"📏 Channel Secret length: {len(LINE_CHANNEL_SECRET)}")
        
        if len(LINE_CHANNEL_SECRET) != 32:
            app.logger.error(f"❌ Invalid Channel Secret length. Expected: 32, Got: {len(LINE_CHANNEL_SECRET)}")
            return 'Invalid channel secret length', 500

        try:
            app.logger.info("🔄 Attempting to handle webhook...")
            handler.handle(body.decode('utf-8'), signature)
            app.logger.info("✅ Webhook handled successfully")
            app.logger.info("=== DEBUG WEBHOOK END (SUCCESS) ===")
            return 'OK', 200
            
        except InvalidSignatureError as e:
            app.logger.error(f"❌ Invalid LINE signature: {e}")
            app.logger.error("🔑 Expected signature should be calculated from:")
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


def create_full_summary_message(title, jobs):
    if not jobs: return TextMessage(text=f"ไม่พบรายการ{title}ในขณะนี้")
    jobs.sort(key=lambda x: x.due_date if x.due_date else x.created_date)
    lines = [f"📋 {title} (ทั้งหมด {len(jobs)} งาน)\n"]
    for i, job in enumerate(jobs):
        customer = job.customer
        due = job.due_date.astimezone(THAILAND_TZ).strftime("%d/%m/%y %H:%M") if job.due_date else 'ยังไม่ระบุ'
        line = f"{i+1}. {job.job_title or 'N/A'}"
        if customer.name: line += f"\n   - 👤 {customer.name}"
        line += f"\n   - 🗓️ {due}"
        lines.append(line)
    message = "\n\n".join(lines)
    if len(message) > 4900: message = message[:4900] + "\n\n... (ข้อความยาวเกินไป)"
    return TextMessage(text=message)

@handler.add(FollowEvent)
def handle_follow_event(event):
    user_id = event.source.user_id
    
    if hasattr(event, 'follow') and hasattr(event.follow, 'referral'):
        job_id = event.follow.referral
        app.logger.info(f"User {user_id} followed via referral link for job: {job_id}")

        job = Job.query.get(job_id)
        if job:
            customer = job.customer
            customer.line_user_id = user_id
            db.session.commit()
            
            settings = get_app_settings()
            
            welcome_message = settings.get('message_templates', {}).get('welcome_customer', '')\
                .replace('[customer_name]', customer.name or '-')\
                .replace('[shop_phone]', settings.get('shop_info', {}).get('contact_phone', '-'))\
                .replace('[shop_line_id]', settings.get('shop_info', {}).get('line_id', '-'))
            
            report_url = url_for('liff.public_task_report', job_id=job_id, _external=True)
            report_message = f"คุณสามารถดูรายละเอียดและสถานะงานซ่อมของคุณได้ที่นี่:\n{report_url}"

            message_queue.add_message(user_id, [
                TextMessage(text=welcome_message),
                TextMessage(text=report_message)
            ])
            app.logger.info(f"Welcome & Report Link messages queued for user {user_id}.")
    else:
        app.logger.info(f"User {user_id} followed without a referral.")

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
        today_start = THAILAND_TZ.localize(datetime.datetime.combine(datetime.date.today(), datetime.time.min)).astimezone(pytz.utc)
        today_end = THAILAND_TZ.localize(datetime.datetime.combine(datetime.date.today(), datetime.time.max)).astimezone(pytz.utc)
        
        jobs = Job.query.filter(
            Job.status == 'needsAction',
            Job.due_date.between(today_start, today_end)
        ).all()
        
        if not jobs:
            messages = [TextMessage(text="ไม่พบงานสำหรับวันนี้")]
        else:
            for job in sorted(jobs, key=lambda x: x.due_date):
                customer = job.customer
                loc = f"พิกัด: {customer.map_url}" if customer.map_url else "พิกัด: - (ไม่มีข้อมูล)"
                
                msg_text = (f"🔔 งานสำหรับวันนี้\n\n"
                           f"👤 ลูกค้า: {customer.name or '-'}\n"
                           f"📞 โทร: {customer.phone or '-'}\n"
                           f"ชื่องาน: {job.job_title or '-'}\n"
                           f"🗓️ นัดหมาย: {job.due_date.astimezone(THAILAND_TZ).strftime('%d/%m/%y %H:%M') if job.due_date else '-'}\n"
                           f"📍 {loc}\n\n"
                           f"🔗 ดูรายละเอียด/แก้ไข:\n{url_for('liff.job_details', job_id=job.id, customer_id=customer.id, _external=True)}")
                
                messages.append(TextMessage(text=msg_text))

    elif text == 'งานค้าง':
        jobs = Job.query.filter_by(status='needsAction').all()
        messages = [create_full_summary_message('รายการงานค้าง', jobs)]
        
    elif text == 'งานเสร็จ':
        jobs = Job.query.filter_by(status='completed').order_by(Job.completed_date.desc()).limit(5).all()
        messages = [create_full_summary_message('รายการงานเสร็จล่าสุด', jobs)]
        
    elif text == 'งานพรุ่งนี้':
        tomorrow_start = THAILAND_TZ.localize(datetime.datetime.combine(datetime.date.today() + datetime.timedelta(days=1), datetime.time.min)).astimezone(pytz.utc)
        tomorrow_end = THAILAND_TZ.localize(datetime.datetime.combine(datetime.date.today() + datetime.timedelta(days=1), datetime.time.max)).astimezone(pytz.utc)
        
        jobs = Job.query.filter(
            Job.status == 'needsAction',
            Job.due_date.between(tomorrow_start, tomorrow_end)
        ).all()
        messages = [create_full_summary_message('งานพรุ่งนี้', jobs)]
        
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
            jobs = Job.query.join(Customer).filter(Customer.name.ilike(f'%{query}%') | Job.job_title.ilike(f'%{query}%')).limit(10).all()
            if not jobs:
                messages = [TextMessage(text=f"ไม่พบงานของลูกค้า: {query}")]
            else:
                messages = [create_full_summary_message('ผลการค้นหา', jobs)]
                
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
            line_messaging_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=messages
                )
            )
        except Exception as e:
            app.logger.error(f"Error replying to text message: {e}")

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    line_user_id = event.source.user_id
    if not line_user_id:
        return

    # 1. ค้นหางานล่าสุดที่ผู้ใช้นี้เปิดดู
    activity = UserActivity.query.filter_by(line_user_id=line_user_id).first()

    # ตรวจสอบว่ากิจกรรมล่าสุดเกิดขึ้นภายใน 30 นาทีที่ผ่านมาหรือไม่
    thirty_minutes_ago = datetime.datetime.utcnow() - timedelta(minutes=30)

    if not activity or not activity.last_viewed_job_id or activity.updated_at < thirty_minutes_ago:
        # ถ้าไม่พบ หรือนานเกินไป ให้ส่งข้อความแจ้งเตือน
        reply_text = "ไม่สามารถระบุงานที่เกี่ยวข้องได้ โปรดอัปโหลดรูปภาพผ่านหน้ารายละเอียดงานโดยตรงครับ/ค่ะ"
        message_queue.add_message(line_user_id, TextMessage(text=reply_text))
        return

    job_id = activity.last_viewed_job_id
    job = Job.query.get(job_id)
    if not job:
        return

    try:
        # 2. ดาวน์โหลดรูปภาพจาก LINE
        message_content = line_messaging_api.get_message_content(message_id=event.message.id)
        image_bytes = BytesIO(message_content)

        # 3. ใช้ Logic การอัปโหลดไฟล์เดิม
        # --- จุดที่แก้ไข ---
        filename = f"line_upload_{datetime.datetime.now(THAILAND_TZ).strftime('%Y%m%d_%H%M%S')}.jpg"
        # ------------------
        mime_type = 'image/jpeg'

        attachments_base_folder_id = find_or_create_drive_folder("Task_Attachments", GOOGLE_DRIVE_FOLDER_ID)
        monthly_folder_name = job.created_date.strftime('%Y-%m')
        monthly_folder_id = find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)
        sanitized_customer_name = sanitize_filename(job.customer.name, fallback=f"Customer_{job.customer.id}")
        customer_job_folder_name = f"{sanitized_customer_name} - {job.id}"
        final_upload_folder_id = find_or_create_drive_folder(customer_job_folder_name, monthly_folder_id)

        if not final_upload_folder_id:
            raise Exception("Could not create final upload folder.")

        media_body = MediaIoBaseUpload(image_bytes, mimetype=mime_type, resumable=True)
        drive_file = _perform_drive_upload(media_body, filename, mime_type, final_upload_folder_id)

        if not drive_file or 'id' not in drive_file:
            raise Exception("Failed to upload to Google Drive.")

        # 4. สร้าง Report และ Attachment ในฐานข้อมูล
        settings = get_app_settings()
        technician_name = "ไม่ระบุ"
        tech_info = next((tech for tech in settings.get('technician_list', []) if tech.get('line_user_id') == line_user_id), None)
        if tech_info:
            technician_name = tech_info.get('name', "ไม่ระบุ")

        new_report = Report(
            job_id=job.id,
            report_type='report',
            work_summary=f"[รูปภาพจาก LINE] เพิ่มรูปภาพโดย {technician_name}",
            technicians=technician_name,
            is_internal=False 
        )
        db.session.add(new_report)
        db.session.flush()

        new_attachment = Attachment(
            report_id=new_report.id,
            drive_file_id=drive_file['id'],
            file_name=filename,
            file_url=drive_file.get('webViewLink')
        )
        db.session.add(new_attachment)
        db.session.commit()

        # 5. ส่งข้อความยืนยันกลับไปหาผู้ใช้
        reply_text = f"✅ รูปภาพถูกเพิ่มไปยังงานของ '{job.customer.name}' เรียบร้อยแล้ว"
        message_queue.add_message(line_user_id, TextMessage(text=reply_text))

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error handling direct image upload for user {line_user_id}: {e}")
        reply_text = f"เกิดข้อผิดพลาดในการบันทึกรูปภาพ: {e}"
        message_queue.add_message(line_user_id, TextMessage(text=reply_text))

@handler.add(PostbackEvent)
def handle_postback(event):
    data = dict(x.split('=') for x in event.postback.data.split('&'))
    action = data.get('action')
    job_id = data.get('job_id')
    feedback_type = data.get('feedback')

    if action == 'customer_feedback':
        job = Job.query.get(job_id)
        if not job: return

        new_report = Report(
            job=job,
            summary_date=datetime.datetime.now(THAILAND_TZ),
            report_type='follow_up_feedback',
            work_summary=f"Customer feedback: {feedback_type}",
            is_internal=True
        )
        db.session.add(new_report)
        db.session.commit()

        reply_text = "ขอบคุณสำหรับคำยืนยันครับ/ค่ะ 🙏"

        if feedback_type == 'problem':
            reply_text = "รับทราบปัญหาครับ/ค่ะ เดี๋ยวทีมงานจะรีบติดต่อกลับไปนะครับ/คะ"
            
            settings = get_app_settings()
            admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')
            if admin_group_id:
                admin_message = settings.get('message_templates', {}).get('problem_report_admin', '')\
                    .replace('[task_title]', job.job_title or '-')\
                    .replace('[customer_name]', job.customer.name or '-')\
                    .replace('[problem_desc]', 'ลูกค้ากดปุ่มแจ้งว่ายังมีปัญหาอยู่')\
                    .replace('[task_url]', url_for('liff.job_details', job_id=job.id, customer_id=job.customer.id, _external=True))
                
                message_queue.add_message(admin_group_id, TextMessage(text=admin_message))
                app.logger.info(f"Problem report for job {job.id} sent to admin group.")

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
    try:
        run_at_time = datetime.datetime.now(THAILAND_TZ) + datetime.timedelta(seconds=3)
        scheduler.add_job(
            background_organize_files_job, 
            'date', 
            run_date=run_at_time, 
            id='manual_file_organization_v2',
            replace_existing=True,
            misfire_grace_time=300
        )
        
        flash('🚀 เริ่มกระบวนการจัดระเบียบไฟล์เบื้องหลังแล้ว! ระบบจะแจ้งเตือนผ่าน LINE เมื่อทำงานเสร็จ (อาจใช้เวลาหลายนาที)', 'success')
        current_app.logger.info("Background file organization job has been triggered.")
    except Exception as e:
        flash(f'เกิดข้อผิดพลาดในการเริ่มงานเบื้องหลัง: {e}', 'danger')
        current_app.logger.error(f"Failed to trigger background job: {e}")

    return redirect(url_for('settings_page'))

@app.route('/admin/line_bot_status')
def get_line_bot_status():
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
    return "OK", 200

@app.route('/api/settings/categories', methods=['GET', 'POST'])
def api_manage_categories():
    settings = get_app_settings()
    if request.method == 'GET':
        return jsonify(settings.get('product_categories', []))
    
    if request.method == 'POST':
        data = request.json
        new_categories = data.get('categories', [])
        if isinstance(new_categories, list):
            if save_app_settings({'product_categories': new_categories}):
                return jsonify({'status': 'success', 'message': 'บันทึกหมวดหมู่เรียบร้อยแล้ว'})
        return jsonify({'status': 'error', 'message': 'ข้อมูลไม่ถูกต้อง'}), 400

@app.route('/api/search_product_image')
def search_product_image():
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
    payload = json.dumps({"q": query, "num": 20})
    
    try:
        response = requests.post("https://google.serper.dev/images", headers=headers, data=payload, timeout=10)
        response.raise_for_status()
        images = response.json().get('images', [])
        
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

@app.route('/admin/organize_files', methods=['GET'])
def organize_files():
    return render_template('organize_files.html')

@app.route('/admin/cleanup_drive', methods=['POST'])
def cleanup_drive_folders():
    service = get_google_drive_service()
    if not service:
        flash('ไม่สามารถเชื่อมต่อ Google Drive API ได้', 'danger')
        return redirect(url_for('settings_page'))

    log_messages = []
    
    try:
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

        folders_by_name = defaultdict(list)
        for folder in all_root_folders:
            folders_by_name[folder['name']].append(folder)

        for name, folders in folders_by_name.items():
            if len(folders) <= 1:
                log_messages.append(f"✅ โฟลเดอร์ '{name}' ไม่ซ้ำซ้อน ข้ามไป...")
                continue

            master_folder = folders[0]
            duplicate_folders = folders[1:]
            log_messages.append(f"⚠️ พบโฟลเดอร์ '{name}' ซ้ำกัน {len(folders)} อัน กำลังรวมไฟล์ไปที่ ID: {master_folder['id']}")

            for dup_folder in duplicate_folders:
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
                
                _execute_google_api_call_with_retry(service.files().delete, fileId=dup_folder['id'])
                log_messages.append(f"   - 🗑️ ลบโฟลเดอร์ซ้ำ ID: {dup_folder['id']} เรียบร้อยแล้ว")

    except HttpError as e:
        log_messages.append(f"❌ เกิดข้อผิดพลาดขณะทำงาน: {e}")
        current_app.logger.error(f"Error during cleanup_drive_folders: {e}")
    
    flash('<strong>การทำความสะอาด Google Drive เสร็จสิ้น:</strong><br>' + '<br>'.join(log_messages), 'info')
    return redirect(url_for('settings_page'))


def background_organize_files_job():
    with app.app.context():
        app.logger.info("--- 🚀 Starting Proactive Google Drive File Organization Job (V2) ---")
        service = get_google_drive_service()
        if not service:
            notify_admin_error("จัดระเบียบไฟล์ล้มเหลว: ไม่สามารถเชื่อมต่อ Google Drive service ได้")
            return

        jobs = Job.query.filter(Job.reports.any()).all()
        if not jobs:
            app.logger.info("No jobs with reports found. Organization job finished.")
            return

        moved_count, skipped_count, error_count = 0, 0, 0
        
        attachments_base_folder_id = find_or_create_drive_folder("Task_Attachments", GOOGLE_DRIVE_FOLDER_ID)
        if not attachments_base_folder_id:
            notify_admin_error("จัดระเบียบไฟล์ล้มเหลว: ไม่สามารถสร้างหรือหาโฟลเดอร์ Task_Attachments ได้")
            return
        
        for job in jobs:
            try:
                monthly_folder_name = job.created_date.astimezone(THAILAND_TZ).strftime('%Y-%m')
                monthly_folder_id = find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)

                sanitized_customer_name = sanitize_filename(customer.name, fallback=f"Customer_{customer.id}")
                customer_job_folder_name = f"{sanitized_customer_name} - {job.id}"
                destination_folder_id = find_or_create_drive_folder(customer_job_folder_name, monthly_folder_id)

                if not destination_folder_id:
                    error_count += 1
                    app.logger.error(f"Failed to create destination folder for job {job.id}.")
                    continue
                
                for report in job.reports:
                    for att in report.attachments:
                        try:
                            file_parents = service.files().get(fileId=att.drive_file_id, fields='parents').execute().get('parents', [])
                            if attachments_base_folder_id in file_parents and destination_folder_id not in file_parents:
                                service.files().update(
                                    fileId=att.drive_file_id,
                                    addParents=destination_folder_id,
                                    removeParents=attachments_base_folder_id
                                ).execute()
                                moved_count += 1
                                app.logger.info(f"Successfully moved attachment {att.drive_file_id} to folder for job {job.id}.")
                            else:
                                skipped_count += 1
                        except HttpError as e:
                            error_count += 1
                            app.logger.error(f"Error moving file {att.drive_file_id}: {e}")

            except Exception as e:
                error_count += 1
                app.logger.error(f"Error processing job {job.id}: {e}", exc_info=True)

        log_summary = f"🗂️ จัดระเบียบไฟล์ใน Drive เสร็จสิ้น!\n\n- ย้ายไฟล์สำเร็จ: {moved_count} ไฟล์\n- ข้ามไป: {skipped_count} ไฟล์\n- เกิดข้อผิดพลาด: {error_count} ไฟล์"
        app.logger.info(f"--- ✅ Finished Proactive File Organization Job --- \n{log_summary}")
        
        admin_group_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
        if admin_group_id:
            message_queue.add_message(admin_group_id, TextMessage(text=log_summary))

@app.route('/api/warehouses/save', methods=['POST'])
@csrf.exempt
def save_warehouse():
    data = request.json
    try:
        if not data.get('name'):
            return jsonify({'status': 'error', 'message': 'กรุณากรอกชื่อคลังสินค้า'}), 400

        if data.get('id'):
            wh = Warehouse.query.get(data['id'])
            if not wh:
                return jsonify({'status': 'error', 'message': 'ไม่พบคลังสินค้าที่ต้องการแก้ไข'}), 404
        else:
            wh = Warehouse()
            db.session.add(wh)
        
        wh.name = data['name']
        wh.type = data['type']
        
        technician_names = data.get('technician_names', []) 
        if wh.type == 'technician_van' and isinstance(technician_names, list):
            wh.technician_name = ",".join(sorted(technician_names)) 
        else:
            wh.technician_name = None
        
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
        quantity_change = float(data.get('quantity'))

        if not all([product_code, to_warehouse_id, quantity_change is not None]):
            return jsonify({'status': 'error', 'message': 'ข้อมูลไม่ครบถ้วน'}), 400

        stock_level = StockLevel.query.filter_by(product_code=product_code, warehouse_id=to_warehouse_id).first()
        if not stock_level:
            stock_level = StockLevel(product_code=product_code, warehouse_id=to_warehouse_id, quantity=0)
            db.session.add(stock_level)
        stock_level.quantity += quantity_change

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

        from_stock = StockLevel.query.filter_by(product_code=product_code, warehouse_id=from_warehouse_id).first()
        if not from_stock:
            from_stock = StockLevel(product_code=product_code, warehouse_id=from_warehouse_id, quantity=0)
            db.session.add(from_stock)
        from_stock.quantity -= quantity

        to_stock = StockLevel.query.filter_by(product_code=product_code, warehouse_id=to_warehouse_id).first()
        if not to_stock:
            to_stock = StockLevel(product_code=product_code, warehouse_id=to_warehouse_id, quantity=0)
            db.session.add(to_stock)
        to_stock.quantity += quantity

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
    liff_user_id = request.args.get('liff_user_id')
    if not liff_user_id:
        return jsonify({'status': 'error', 'message': 'ไม่พบ LIFF User ID'}), 400

    try:
        settings = get_app_settings()
        technician_list = settings.get('technician_list', [])
        tech_info = next((tech for tech in technician_list if tech.get('line_user_id') == liff_user_id), None)
        
        if not tech_info:
            return jsonify({'status': 'error', 'message': 'ไม่พบข้อมูลช่างที่ผูกกับ LIFF User ID นี้'}), 404
        
        technician_name = tech_info.get('name')

        warehouse = Warehouse.query.filter_by(technician_name=technician_name, is_active=True).first()
        if not warehouse:
            return jsonify({'status': 'error', 'message': f'ไม่พบคลังสินค้าที่ผูกกับช่าง {technician_name}'}), 404

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
    current_app.logger.info("Starting data integrity scan (V3 - Optimized)...")
    jobs_with_issues = []
    
    try:
        jobs = Job.query.options(db.joinedload(Job.reports)).all()
        jobs_dict = {job.id: job for job in jobs}

        attachments_base_folder_id = find_or_create_drive_folder("Task_Attachments", GOOGLE_DRIVE_FOLDER_ID)
        if not attachments_base_folder_id:
            raise Exception("Could not find base attachments folder.")

        all_job_folders = []
        monthly_folders = _get_drive_folders_in_folder(attachments_base_folder_id)
        for month_folder in monthly_folders:
            all_job_folders.extend(_get_drive_folders_in_folder(month_folder['id']))
        
        job_id_pattern = re.compile(r'-\s([a-zA-Z0-9_-]{1,})$')
        
        for folder in all_job_folders:
            match = job_id_pattern.search(folder['name'])
            if not match: continue

            try:
                job_id = int(match.group(1))
                job = jobs_dict.get(job_id)

                if not job: continue

                drive_files = _get_drive_files_in_folder(folder['id'])
                if not drive_files: continue

                attachment_ids_in_reports = {att.drive_file_id for report in job.reports for att in report.attachments}
                
                missing_attachments_count = sum(1 for drive_file in drive_files if drive_file['id'] not in attachment_ids_in_reports)

                if missing_attachments_count > 0:
                    customer = job.customer
                    jobs_with_issues.append({
                        'job_id': job.id,
                        'job_title': job.job_title,
                        'customer_name': customer.name,
                        'missing_count': missing_attachments_count,
                        'folder_id': folder['id']
                    })
            except (ValueError, TypeError):
                current_app.logger.warning(f"Could not parse job_id from folder name: {folder['name']}. Skipping.")
                continue
        
        current_app.logger.info(f"Scan complete (V3). Found {len(jobs_with_issues)} jobs with issues.")
        return jsonify({'status': 'success', 'issues': jobs_with_issues})

    except Exception as e:
        current_app.logger.error(f"An error occurred during health check scan: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500

def _get_drive_folders_in_folder(folder_id):
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

@app.route('/api/health_check/repair/<int:job_id>', methods=['POST'])
def health_check_repair(job_id):
    job = Job.query.options(db.joinedload(Job.reports)).get(job_id)
    if not job:
        return jsonify({'status': 'error', 'message': 'Job not found.'}), 404

    attachments_base_folder_id = find_or_create_drive_folder("Task_Attachments", GOOGLE_DRIVE_FOLDER_ID)
    if not attachments_base_folder_id:
        return jsonify({'status': 'error', 'message': 'Could not find base attachments folder.'}), 500

    target_folder_id = None
    monthly_folders = _get_drive_folders_in_folder(attachments_base_folder_id)
    for month_folder in monthly_folders:
        customer_folders = _get_drive_folders_in_folder(month_folder['id'])
        for folder in customer_folders:
            if str(job_id) in folder['name']:
                target_folder_id = folder['id']
                current_app.logger.info(f"Found matching folder '{folder['name']}' (ID: {target_folder_id}) for job {job_id}")
                break
        if target_folder_id: break

    if not target_folder_id:
        return jsonify({'status': 'error', 'message': f'Could not find a corresponding Google Drive folder containing the Job ID: {job_id}'}), 404

    drive_service = get_google_drive_service() # ย้ายมาตรงนี้เพื่อให้แน่ใจว่ามี service ก่อนใช้งาน
    if not drive_service:
        return jsonify({'status': 'error', 'message': 'Google Drive service not available.'}), 500

    drive_files = _get_drive_files_in_folder(target_folder_id)

    attachment_ids_in_reports = {att.drive_file_id for report in job.reports for att in report.attachments}

    recovered_attachments_data = []
    for drive_file in drive_files:
        if drive_file['id'] not in attachment_ids_in_reports:
            recovered_attachments_data.append(drive_file)

    if not recovered_attachments_data:
        return jsonify({'status': 'success', 'message': 'ไม่พบไฟล์ที่ขาดหายไป (อาจซ่อมแซมไปแล้ว)'})

    new_report = Report(
        job=job,
        summary_date=datetime.now(THAILAND_TZ),
        report_type='report',
        work_summary="[ข้อมูลกู้คืนอัตโนมัติ] พบไฟล์แนบที่ไม่ได้ถูกบันทึกในประวัติ",
        technicians="System Recovery",
        is_internal=True
    )
    db.session.add(new_report)
    db.session.flush()

    for drive_file in recovered_attachments_data:
        try:
            # *** ส่วนที่เพิ่มเข้ามา: ตั้งค่า Permission ให้ไฟล์ ***
            drive_service.permissions().create(
                fileId=drive_file['id'], body={'role': 'reader', 'type': 'anyone'}
            ).execute()
            current_app.logger.info(f"Successfully set permission for recovered file: {drive_file['id']}")
            # *************************************************

            new_att = Attachment(
                report=new_report,
                drive_file_id=drive_file['id'],
                file_name=drive_file['name'],
                file_url=f"https://drive.google.com/file/d/{drive_file['id']}/view?usp=drivesdk"
            )
            db.session.add(new_att)
        except Exception as e:
            current_app.logger.error(f"Failed to set permission or save attachment for {drive_file['id']}: {e}")


    db.session.commit()
    current_app.logger.info(f"Successfully repaired job {job_id}, added {len(recovered_attachments_data)} attachments.")
    return jsonify({'status': 'success', 'message': f'ซ่อมแซมสำเร็จ! เพิ่มรูปภาพที่ขาดหายไป {len(recovered_attachments_data)} รูป'})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=True)