from flask_session import Session
import os
from functools import wraps
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
import sys
import datetime
from datetime import timedelta
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

initial_redis_url_check = os.environ.get('REDIS_URL')
print(f"INITIAL DEBUG: REDIS_URL after load_dotenv: '{initial_redis_url_check}'")

from flask import Flask, request, render_template, redirect, url_for, abort, flash, jsonify, Response, session, make_response
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect
from cachetools import cached, TTLCache
from geopy.distance import geodesic # สำหรับคำนวณระยะทาง
from urllib.parse import urlparse, parse_qs, unquote, quote_plus

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

from redis import Redis

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

# --- โค้ด Sentry ควรวางไว้ที่นี่ ---
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
CORS(app)

app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_development_only')

app.config["SESSION_TYPE"] = "redis"

redis_url = os.environ.get('REDIS_URL')

print(f"DEBUG: REDIS_URL from environment (print): '{redis_url}'") # ใช้ print() เพื่อให้แน่ใจว่าเห็น Log นี้

if redis_url:
    app.config["SESSION_REDIS"] = Redis.from_url(redis_url)
    print("DEBUG: Flask Session configured to use Redis from REDIS_URL (print).")
else:
    app.config["SESSION_REDIS"] = Redis(host='localhost', port=6379, db=0)
    print("DEBUG: REDIS_URL not found. Flask Session attempting to use local Redis (print).")

# --- ย้าย try-except ping มาไว้ตรงนี้ และใช้ print() ---
try:
    app.config["SESSION_REDIS"].ping()
    print("✅ Successfully connected to Redis server (print).")
except Exception as e:
    print(f"❌ Failed to connect to Redis server (print): {e}")
# ----------------------------------------------------

# ตั้งค่า Session ให้คงทนถาวร (จะหมดอายุตาม PERMANENT_SESSION_LIFETIME)
app.config["SESSION_PERMANENT"] = True
# ตั้งค่าให้ Session Cookie มีการเซ็นชื่อเพื่อความปลอดภัย
app.config["SESSION_USE_SIGNER"] = True

# ==================== แก้ไขการตั้งค่า SESSION_COOKIE ====================
# แก้ปัญหา Session ไม่คงอยู่ใน LIFF/LINE environment
if os.environ.get('FLASK_ENV') == 'development':
    # สำหรับ Local Development
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = False
else:
    # สำหรับ Production (Render.com + LINE LIFF)
    app.config["SESSION_COOKIE_SAMESITE"] = "None"
    app.config["SESSION_COOKIE_SECURE"] = True
    
# เพิ่มการตั้งค่าเพื่อให้ Session Cookie ทำงานได้ดีขึ้น
app.config["SESSION_COOKIE_DOMAIN"] = None  # ใช้ domain ปัจจุบัน
app.config["SESSION_COOKIE_PATH"] = "/"     # ใช้ได้ทุก path
# =========================================================================

# ตั้งค่า Session Cookie ให้ส่งผ่าน HTTPS เท่านั้น (สำคัญสำหรับ Production)
app.config["SESSION_COOKIE_SECURE"] = True
# กำหนดอายุของ Session ที่คงทนถาวร (ในที่นี้คือ 30 วัน)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
# เพิ่มการตั้งค่า HTTPOnly สำหรับ Session Cookie (ป้องกันการเข้าถึงจาก JavaScript)
app.config['SESSION_COOKIE_HTTPONLY'] = True 

sess = Session(app)

# --- เพิ่ม Log เพื่อตรวจสอบ Redis host/port ที่กำลังใช้ (ใช้ app.logger.info) ---
if hasattr(app.config["SESSION_REDIS"], 'connection_pool') and hasattr(app.config["SESSION_REDIS"].connection_pool, 'connection_kwargs'):
    conn_kwargs = app.config["SESSION_REDIS"].connection_pool.connection_kwargs
    app.logger.info(f"DEBUG: Attempting to connect Redis at host='{conn_kwargs.get('host')}', port={conn_kwargs.get('port')}, db={conn_kwargs.get('db')}")
elif hasattr(app.config["SESSION_REDIS"], 'connection_pool') and hasattr(app.config["SESSION_REDIS"].connection_pool, 'connection_class'):
    conn_class = app.config["SESSION_REDIS"].connection_pool.connection_class
    app.logger.info(f"DEBUG: Redis connection class: {conn_class.__name__}")
else:
    app.logger.info("DEBUG: Cannot determine specific Redis connection details from app.config['SESSION_REDIS'].")
# --------------------------------------------------------------------------------

csrf = CSRFProtect(app)

app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

app.jinja_env.filters['dateutil_parse'] = date_parse

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'kmz', 'kml'}
MAX_FILE_SIZE_MB = 500
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

@app.route('/liff_verify_session', methods=['POST'])
@csrf.exempt # ต้องยกเว้น CSRF สำหรับ AJAX call
def liff_verify_session():
    """
    Endpoint นี้จะถูกเรียกจาก LIFF SDK (JavaScript) เพื่อยืนยันตัวตนผู้ใช้
    และสร้าง Flask session ขึ้นมาใหม่ หาก session หายไป
    """
    app.logger.info("Received request to /liff_verify_session.")
    
    # ตรวจสอบว่ามี session ใน Flask อยู่แล้วหรือไม่
    if 'line_user_id' in session:
        app.logger.info(f"Flask session already exists for user {session.get('line_user_id')}. No need to re-verify.")
        return jsonify({'status': 'success', 'message': 'Flask session already active.'}), 200

    # ดึง LIFF Access Token จาก Header ที่ LIFF SDK ส่งมา (ถ้ามี)
    # LIFF SDK จะส่ง token มาใน Authorization header หากเรียก liff.getAccessToken()
    liff_access_token = request.headers.get('Authorization')
    if liff_access_token and liff_access_token.startswith('Bearer '):
        liff_access_token = liff_access_token.split(' ')[1]
        app.logger.info("LIFF Access Token found in Authorization header.")
    else:
        app.logger.warning("LIFF Access Token not found in Authorization header. Attempting to get from LIFF SDK directly.")
        # หากไม่พบใน header อาจจะต้องให้ client ส่งมาใน body หรือ query param
        # แต่โดยทั่วไป LIFF SDK จะจัดการให้ถ้าเรียก liff.getAccessToken() ก่อน fetch

    if not liff_access_token:
        app.logger.error("No LIFF Access Token provided for verification.")
        return jsonify({'status': 'error', 'message': 'LIFF Access Token missing.'}), 400

    # ใช้ LIFF Access Token เพื่อดึงข้อมูลโปรไฟล์ผู้ใช้จาก LINE
    profile_url = 'https://api.line.me/v2/profile'
    profile_headers = {'Authorization': f'Bearer {liff_access_token}'}

    try:
        profile_response = requests.get(profile_url, headers=profile_headers)
        profile_response.raise_for_status() # ตรวจสอบ HTTP Error
        profile_info = profile_response.json()

        # สร้าง Flask session ใหม่ - ปรับปรุง
        session.permanent = True  # ตั้งเป็น permanent ก่อน
        session['line_user_id'] = profile_info['userId']
        session['display_name'] = profile_info['displayName']
        session['login_time'] = datetime.datetime.now().isoformat()
        session['re_established'] = True  # flag ว่าเป็น session ที่สร้างใหม่
        
        # บังคับให้ Flask บันทึก session ทันที
        session.modified = True

        app.logger.info(f"Flask session re-established for user {profile_info['userId']} via LIFF token.")
        return jsonify({'status': 'success', 'message': 'Flask session re-established.'}), 200

    except requests.exceptions.RequestException as e:
        app.logger.error(f"LINE Profile API Error during LIFF session verification: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': f'Failed to verify LIFF token with LINE API: {e}'}), 500
    except Exception as e:
        app.logger.error(f"Unexpected error during LIFF session verification: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'An unexpected server error occurred during verification.'}), 500

@app.route('/admin/cookie_headers')
def cookie_headers_checker():
    """
    API endpoint เพื่อแสดง HTTP headers ของ Response รวมถึง Set-Cookie header
    เพื่อตรวจสอบว่า Flask กำลังตั้งค่า Session Cookie อย่างไร
    """
    response = make_response(jsonify({
        "message": "Checking response headers for session cookie settings.",
        "session_id_in_flask_session": session.sid if session.sid else "N/A"
    }))

    # ลองตั้งค่า session value เพื่อให้ Flask สร้าง Set-Cookie header
    if 'test_session_key' not in session:
        session['test_session_key'] = 'test_value'
        session.permanent = True # ทำให้ session นี้เป็น permanent ด้วย
        response.headers['X-Debug-Session-Set'] = 'True'
    else:
        response.headers['X-Debug-Session-Set'] = 'False (already set)'

    # ดึง headers ทั้งหมด
    headers = dict(response.headers)
    
    return jsonify({
        "status": "success",
        "message": "Response headers for /admin/cookie_headers",
        "headers": headers
    })

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
if LINE_CHANNEL_ACCESS_TOKEN.startswith('"') and LINE_CHANNEL_ACCESS_TOKEN.endswith('"'):
    LINE_CHANNEL_ACCESS_TOKEN = LINE_CHANNEL_ACCESS_TOKEN[1:-1].strip()

LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '').strip()
if LINE_CHANNEL_SECRET.startswith('"') and LINE_CHANNEL_SECRET.endswith('"'):
    LINE_CHANNEL_SECRET = LINE_CHANNEL_SECRET[1:-1].strip()

print(f"DEBUG: Checking LINE credentials before sys.exit().")
print(f"DEBUG: LINE_CHANNEL_ACCESS_TOKEN (after strip/quote removal): '{LINE_CHANNEL_ACCESS_TOKEN}'")
print(f"DEBUG: LINE_CHANNEL_SECRET (after strip/quote removal): '{LINE_CHANNEL_SECRET}'")

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    print(f"FATAL ERROR: LINE Bot credentials are not fully set. ACCESS_TOKEN_SET: {bool(LINE_CHANNEL_ACCESS_TOKEN)}, SECRET_SET: {bool(LINE_CHANNEL_SECRET)}")
    # sys.exit("LINE Bot credentials are not set in environment variables.") # <--- คอมเมนต์บรรทัดนี้ชั่วคราว

try:
    print("DEBUG: Initializing LINE Configuration...")
    configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    
    print("DEBUG: Initializing LINE API Client...")
    api_client = ApiClient(configuration)
    
    print("DEBUG: Initializing LINE Messaging API...")
    line_messaging_api = MessagingApi(api_client) # This object will be reused
    
    print("DEBUG: Initializing LINE Webhook Handler...")
    handler = WebhookHandler(LINE_CHANNEL_SECRET)
    
    print("DEBUG: LINE API objects initialized successfully (print).")
    app.logger.info("✅ Successfully initialized LINE Messaging API and Webhook Handler.")
except Exception as e:
    print(f"FATAL ERROR (PRINT): Failed to initialize LINE Messaging API or Webhook Handler: {e}")
    app.logger.critical(f"FATAL ERROR: Failed to initialize LINE Messaging API or Webhook Handler: {e}", exc_info=True)
    # sys.exit("Failed to initialize LINE Bot API. Check credentials.") # <--- คอมเมนต์บรรทัดนี้ชั่วคราว

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

@app.route('/admin/line_config_status')
def line_config_status():
    """
    API endpoint เพื่อตรวจสอบสถานะการตั้งค่า Environment Variables ที่เกี่ยวข้องกับ LINE
    จะคืนค่าเป็น JSON ที่บอกว่าตัวแปรแต่ละตัวถูกตั้งค่าหรือไม่ และมีความยาวถูกต้องหรือไม่
    """
    status_details = {}
    overall_ok = True

    # 1. ตรวจสอบ LINE_CHANNEL_ACCESS_TOKEN
    # ✅ แก้ไข: ใช้ตัวแปร Global LINE_CHANNEL_ACCESS_TOKEN โดยตรง
    access_token = LINE_CHANNEL_ACCESS_TOKEN # ลบ os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
    status_details['LINE_CHANNEL_ACCESS_TOKEN'] = {
        'set': bool(access_token),
        'length': len(access_token),
        'valid_length': len(access_token) > 50 # Access Token มักจะยาวมาก
    }
    if not status_details['LINE_CHANNEL_ACCESS_TOKEN']['set'] or not status_details['LINE_CHANNEL_ACCESS_TOKEN']['valid_length']:
        overall_ok = False

    # 2. ตรวจสอบ LINE_CHANNEL_SECRET
    # ✅ แก้ไข: ใช้ตัวแปร Global LINE_CHANNEL_SECRET โดยตรง
    channel_secret = LINE_CHANNEL_SECRET # ลบ os.environ.get('LINE_CHANNEL_SECRET', '')
    status_details['LINE_CHANNEL_SECRET'] = {
        'set': bool(channel_secret),
        'length': len(channel_secret),
        'valid_length': len(channel_secret) == 32 # Channel Secret ต้องยาว 32 ตัวอักษร
    }
    if not status_details['LINE_CHANNEL_SECRET']['set'] or not status_details['LINE_CHANNEL_SECRET']['valid_length']:
        overall_ok = False

    # 3. ตรวจสอบ LINE_LOGIN_CHANNEL_ID
    # ✅ แก้ไข: ใช้ตัวแปร Global LINE_LOGIN_CHANNEL_ID โดยตรง
    login_channel_id = LINE_LOGIN_CHANNEL_ID # ลบ os.environ.get('LINE_LOGIN_CHANNEL_ID', '')
    status_details['LINE_LOGIN_CHANNEL_ID'] = {
        'set': bool(login_channel_id),
        'length': len(login_channel_id),
        'is_numeric': login_channel_id.isdigit() if login_channel_id else False # ID มักจะเป็นตัวเลข
    }
    if not status_details['LINE_LOGIN_CHANNEL_ID']['set'] or not status_details['LINE_LOGIN_CHANNEL_ID']['is_numeric']:
        overall_ok = False

    # 4. ตรวจสอบ LINE_LOGIN_CHANNEL_SECRET
    # ✅ แก้ไข: ใช้ตัวแปร Global LINE_LOGIN_CHANNEL_SECRET โดยตรง
    login_channel_secret = LINE_LOGIN_CHANNEL_SECRET # ลบ os.environ.get('LINE_LOGIN_CHANNEL_SECRET', '')
    status_details['LINE_LOGIN_CHANNEL_SECRET'] = {
        'set': bool(login_channel_secret),
        'length': len(login_channel_secret),
        'valid_length': len(login_channel_secret) == 32 # Secret ต้องยาว 32 ตัวอักษร
    }
    if not status_details['LINE_LOGIN_CHANNEL_SECRET']['set'] or not status_details['LINE_LOGIN_CHANNEL_SECRET']['valid_length']:
        overall_ok = False

    # 5. ตรวจสอบ LIFF_ID_FORM
    # ✅ แก้ไข: ใช้ตัวแปร Global LIFF_ID_FORM โดยตรง
    liff_id_form = LIFF_ID_FORM # ลบ os.environ.get('LIFF_ID_FORM', '')
    status_details['LIFF_ID_FORM'] = {
        'set': bool(liff_id_form),
        'length': len(liff_id_form),
        'format_ok': '-' in liff_id_form and liff_id_form.count('-') == 1 if liff_id_form else False # LIFF ID มี - 1 ตัว
    }
    if not status_details['LIFF_ID_FORM']['set'] or not status_details['LIFF_ID_FORM']['format_ok']:
        overall_ok = False

    # 6. ตรวจสอบ LIFF_ID_TASK_PAGE (หากคุณใช้)
    # ✅ แก้ไข: ใช้ตัวแปร Global LIFF_ID_TASK_PAGE โดยตรง
    liff_id_task_page = os.environ.get('LIFF_ID_TASK_PAGE', '') # อันนี้ยังต้องใช้ os.environ.get เพราะไม่ได้ประกาศเป็น Global
    status_details['LIFF_ID_TASK_PAGE'] = {
        'set': bool(liff_id_task_page),
        'length': len(liff_id_task_page),
        'format_ok': '-' in liff_id_task_page and liff_id_task_page.count('-') == 1 if liff_id_task_page else False
    }
    if status_details['LIFF_ID_TASK_PAGE']['set'] and not status_details['LIFF_ID_TASK_PAGE']['format_ok']:
        overall_ok = False

    # 7. ตรวจสอบ LIFF_ID_TECHNICIAN_LOCATION (หากคุณใช้)
    # ✅ แก้ไข: ใช้ตัวแปร Global LIFF_ID_TECHNICIAN_LOCATION โดยตรง
    liff_id_tech_loc = LIFF_ID_TECHNICIAN_LOCATION # ลบ os.environ.get('LIFF_ID_TECHNICIAN_LOCATION', '')
    status_details['LIFF_ID_TECHNICIAN_LOCATION'] = {
        'set': bool(liff_id_tech_loc),
        'length': len(liff_id_tech_loc),
        'format_ok': '-' in liff_id_tech_loc and liff_id_tech_loc.count('-') == 1 if liff_id_tech_loc else False
    }
    if status_details['LIFF_ID_TECHNICIAN_LOCATION']['set'] and not status_details['LIFF_ID_TECHNICIAN_LOCATION']['format_ok']:
        overall_ok = False

    # 8. ตรวจสอบ LINE_OA_ID (ถ้ามี)
    # ✅ แก้ไข: ใช้ตัวแปร Global LINE_OA_ID โดยตรง
    line_oa_id = os.environ.get('LINE_OA_ID', '') # อันนี้ยังต้องใช้ os.environ.get เพราะไม่ได้ประกาศเป็น Global
    status_details['LINE_OA_ID'] = {
        'set': bool(line_oa_id),
        'starts_with_at': line_oa_id.startswith('@') if line_oa_id else False
    }
    if status_details['LINE_OA_ID']['set'] and not status_details['LINE_OA_ID']['starts_with_at']:
        overall_ok = False

    return jsonify({
        'overall_status': 'OK' if overall_ok else 'ERROR',
        'message': 'LINE configuration looks good!' if overall_ok else 'Some LINE configurations are incorrect or missing.',
        'details': status_details
    })

line_issues = check_line_bot_configuration()
if line_issues:
    app.logger.error("LINE Bot Configuration Issues:")
    for issue in line_issues:
        app.logger.error(f"  - {issue}")
else:
    app.logger.info("LINE Bot configuration looks good!")
    
app.logger.info(f"==========================================")

LINE_LOGIN_CHANNEL_SECRET = os.environ.get('LINE_LOGIN_CHANNEL_SECRET')

LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM')
# --- NEW: เพิ่ม LIFF ID สำหรับหน้าอัปเดตตำแหน่งช่าง ---
LIFF_ID_TECHNICIAN_LOCATION = os.environ.get('LIFF_ID_TECHNICIAN_LOCATION')
# ----------------------------------------------------
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

SETTINGS_FILE = 'settings.json'
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
    'technician_list': [],
    'popup_notifications': {
        'enabled_arrival': False,
        'message_arrival_template': 'ช่าง [technician_name] กำลังจะถึงบ้านคุณ [customer_name] แล้วครับ/ค่ะ',
        'enabled_completion_customer': True,
        'message_completion_customer_template': 'งาน [task_title] ที่บ้านคุณ [customer_name] เสร็จเรียบร้อยแล้วครับ/ค่ะ',
        'enabled_nearby_job': False,
        'nearby_radius_km': 5,
        'message_nearby_template': 'มีงาน [task_title] อยู่ใกล้คุณ [distance_km] กม. ที่ [customer_name] สนใจรับงานหรือไม่?',
        'liff_popup_base_url': 'https://liff.line.me/2007690244-zBNe26ZO'
    },
    # --- เพิ่มส่วนนี้เข้าไปทั้งหมด ---
    'message_templates': {
        'welcome_customer': "เรียน คุณ[customer_name],\n\nขอบคุณที่เชื่อมต่อกับ Comphone ครับ/ค่ะ!\nเราจะใช้ LINE นี้เพื่อส่งข้อมูลสำคัญเกี่ยวกับบริการครับ\n\nติดต่อ:\nโทร: [shop_phone]\nLINE ID: [shop_line_id]",
    'problem_report_admin': "🚨 ลูกค้าแจ้งปัญหา!\n\nงาน: [task_title]\nลูกค้า: [customer_name]\nปัญหา: [problem_desc]\n\n🔗 ดูรายละเอียดงาน:\n[task_url]",
    'daily_reminder_header': "...",
    'daily_reminder_task_line': "..."
    }
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
    task_url = url_for('task_details', task_id=task.get('id'), _external=True)

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
    current_settings = get_app_settings()
    
    for key, value in settings_data.items():
        if isinstance(value, dict) and key in current_settings and isinstance(current_settings[key], dict):
            current_settings[key].update(value)
        else:
            current_settings[key] = value
            
    return save_settings_to_file(current_settings)

# --- เพิ่ม 2 ฟังก์ชันนี้เข้าไป ---
LOCATIONS_FILE = 'technician_locations.json'

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
    if not notes:
        return [], ""

    # ใช้ re.split เพื่อแยกส่วนข้อมูลลูกค้าและส่วนรายงานออกจากกันอย่างชัดเจน
    parts = re.split(r'\n\s*--- TECH_REPORT_START ---', notes)
    base_notes_with_feedback = parts[0]
    history = []

    # วนลูปเฉพาะส่วนที่เป็นรายงานเท่านั้น
    for part in parts[1:]:
        # หา JSON และส่วนท้ายของบล็อก
        end_match = re.search(r'(.*?)\n\s*--- TECH_REPORT_END ---', part, re.DOTALL)
        if end_match:
            json_str = end_match.group(1).strip()
            try:
                report_data = json.loads(json_str)
                
                # --- โค้ดประมวลผล report_data (เหมือนเดิม) ---
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
                # --- สิ้นสุดโค้ดประมวลผล ---

            except json.JSONDecodeError:
                app.logger.warning(f"Failed to decode tech report JSON: {json_str[:100]}...")

    # ทำความสะอาด base notes เพื่อให้เหลือแค่ข้อมูลลูกค้าจริงๆ
    base_notes_text = re.sub(r"--- CUSTOMER_FEEDBACK_START ---.*?--- CUSTOMER_FEEDBACK_END ---", "", base_notes_with_feedback, flags=re.DOTALL).strip()

    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, base_notes_text
              
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

@app.route('/admin/session_status')
def session_status():
    """
    API endpoint สำหรับตรวจสอบสถานะ Session ปัจจุบัน
    ใช้เพื่อ Debug ปัญหา Session
    """
    session_info = {
        "session_id": session.get('_id', 'No Session ID'),
        "line_user_id": session.get('line_user_id', 'Not Found'),
        "display_name": session.get('display_name', 'Not Found'),
        "login_time": session.get('login_time', 'Not Found'),
        "session_permanent": session.permanent,
        "session_keys": list(session.keys()),
        "cookies_sent": dict(request.cookies),
        "user_agent": request.headers.get('User-Agent', 'Unknown'),
        "referer": request.headers.get('Referer', 'No Referer')
    }
    
    return jsonify({
        "status": "success",
        "message": "Session status retrieved",
        "session_info": session_info,
        "is_logged_in": 'line_user_id' in session
    })

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

def send_completion_notification(task, technicians):
    settings = get_app_settings()
    recipients = settings.get('line_recipients', {})
    admin_group_id = recipients.get('admin_group_id')
    tech_group_id = recipients.get('technician_group_id')
    customer_line_id_from_feedback = parse_customer_feedback_from_notes(task.get('notes', '')).get('customer_line_user_id')

    if not admin_group_id and not tech_group_id and not customer_line_id_from_feedback: return

    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    technician_str = ", ".join(technicians) if technicians else "ไม่ได้ระบุ"
    public_report_url = url_for('public_task_report', task_id=task.get('id'), _external=True)

    message_text_admin_tech = (
        f"✅ ปิดงานเรียบร้อย\n\n"
        f"ชื่องาน: {task.get('title', '-')}\n"
        f"ลูกค้า: {customer_info.get('name', '-')}\n"
        f"ช่างผู้รับผิดชอบ: {technician_str}\n\n"
    )
    
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
    
    if customer_line_id_from_feedback and settings.get('popup_notifications', {}).get('enabled_completion_customer'):
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

def send_update_notification(task, new_due_date_str, reason, technicians, is_today):
    settings = get_app_settings()
    admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')
    if not admin_group_id: return

    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    technician_str = ", ".join(technicians) if technicians else "ไม่ได้ระบุ"
    
    if is_today:
        title_prefix = "🗓️ อัปเดตงานวันนี้"
    else:
        title_prefix = "🗓️ เลื่อนนัดหมาย"

    message_text = (
        f"{title_prefix}\n\n"
        f"ชื่องาน: {task.get('title', '-')}\n"
        f"ลูกค้า: {customer_info.get('name', '-')}\n"
        f"📞 โทร: {customer_info.get('phone', '-')}\n"
        f"นัดหมายใหม่: {new_due_date_str}\n"
        f"เหตุผล: {reason}\n"
        f"ช่าง: {technician_str}\n\n"
    )
    
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
                            message_queue.add_message(recipient_id, TextMessage(text=message_text + f"🔗 ดูรายละเอียด/แก้ไข:\n{url_for('task_details', task_id=task.get('id'), _external=True)}"))
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
                                # สร้างข้อความแจ้งเตือนแอดมิน
                                admin_notification_messages = [
                                    TextMessage(text=f"⚠️ ส่ง Follow-up ให้ลูกค้า {customer_info.get('name')} (Task ID: {task['id']}) ไม่สำเร็จ โปรดส่งข้อความนี้แทน:"),
                                    flex_message
                                ]
                                message_queue.add_message(admin_group_id, admin_notification_messages)

                except Exception as e:
                    app.logger.warning(f"Could not process task {task.get('id')} for follow-up: {e}", exc_info=True)
# --- END of scheduled_customer_follow_up_job replacement ---

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

                if customer_map_url and ('maps.google.com' in customer_map_url or re.match(r"^\-?\d+\.\d+,\s*\-?\d+\.\d+$", customer_map_url)):
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

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # ตรวจสอบ Session ID และข้อมูล User
        app.logger.info(f"login_required: Checking session for path {request.path}")
        app.logger.info(f"Session ID: {session.get('_id', 'No Session ID')}")
        app.logger.info(f"Line User ID in session: {session.get('line_user_id', 'Not Found')}")
        
        if 'line_user_id' not in session:
            app.logger.warning(f"No line_user_id in session for path {request.path}")
            
            # เก็บ URL ปัจจุบันเพื่อ redirect กลับมาหลัง login
            if request.path != '/login':
                session['next_url'] = request.path
                
            flash('กรุณาเข้าสู่ระบบเพื่อใช้งานหน้านี้', 'warning')
            return redirect(url_for('login_page'))
            
        app.logger.info(f"Session valid for user {session['line_user_id']}")
        return f(*args, **kwargs)
    return decorated_function

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404
    
@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({'status': 'error', 'message': f'ไฟล์มีขนาดใหญ่เกินกว่า {MAX_FILE_SIZE_MB}MB'}), 413

@app.errorhandler(500)
def internal_server_error(e):
    app.logger.error(f"Server Error: {e}", exc_info=True)
    return render_template('500.html'), 500

@app.route('/api/customers')
def api_customers():
    customer_list = get_customer_database()
    return jsonify(customer_list)

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
        'task_details_url': url_for('task_details', task_id=task_id, _external=True)
    }
    return jsonify(summary_data)

@app.route('/login')
def login_page():
    """
    หน้านี้จะใช้ LIFF SDK เพื่อจัดการการล็อกอินโดยตรง
    Flask-generated URL จะเป็น fallback หาก LIFF SDK ทำงานไม่ได้
    """
    line_login_url = (f"https://access.line.me/oauth2/v2.1/authorize?response_type=code"
                      f"&client_id={LINE_LOGIN_CHANNEL_ID}"
                      f"&redirect_uri={url_for('callback_line', _external=True)}"
                      f"&state={uuid.uuid4().hex}"
                      f"&scope=profile%20openid%20email")
    
    # ส่ง LIFF_ID_FORM ไปยัง template เพื่อให้ JavaScript ใช้
    return render_template('login.html',
                           line_login_url=line_login_url,
                           LIFF_ID_FORM=os.environ.get('LIFF_ID_FORM', '')) # <--- บรรทัดนี้สำคัญ

@app.route('/logout')
def logout():
    session.clear() # ล้างข้อมูลผู้ใช้ออกจากระบบ
    flash('คุณได้ออกจากระบบเรียบร้อยแล้ว', 'info')
    return redirect(url_for('login_page'))

@app.route("/")
def root_redirect():
    # ตรวจสอบว่ามีการส่ง liff.state มาใน URL หรือไม่
    liff_state = request.args.get('liff.state')

    if liff_state:
        # ถอดรหัส URL และตรวจสอบว่าเป็น Path ที่ถูกต้อง
        decoded_path = unquote(liff_state)
        if decoded_path.startswith('/'):
            app.logger.info(f"LIFF state detected. Redirecting to: {decoded_path}")
            # ทำการ Redirect ไปยัง Path ที่ต้องการจริงๆ
            return redirect(decoded_path)

    # ถ้าไม่มี liff.state ให้ไปที่หน้า summary ตามเดิม
    return redirect(url_for('summary'))

# --- NEW: Route to render the technician location update LIFF page ---
@app.route('/liff/technician/update_location')
@csrf.exempt
def technician_location_liff_page():
    if not LIFF_ID_TECHNICIAN_LOCATION:
        app.logger.error("LIFF_ID_TECHNICIAN_LOCATION is not set. Cannot render technician LIFF page.")
        abort(500, "LIFF ID for this feature is not configured on the server.")
    return render_template('technician_location_update.html', LIFF_ID_TECHNICIAN_LOCATION=LIFF_ID_TECHNICIAN_LOCATION)
# --------------------------------------------------------------------

# --- แทนที่ฟังก์ชันเดิมด้วยโค้ดใหม่นี้ ---
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
# ------------------------------------------
@app.route("/form", methods=['GET', 'POST'])
@login_required
def form_page():
    if request.method == 'POST':
        # --- โค้ดส่วน POST ทั้งหมดของคุณ (เหมือนเดิมทุกประการ) ---
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
                # แก้ไขเล็กน้อย: ส่งข้อมูลที่กรอกแล้วกลับไปพร้อมกับ customer_db
                customer_db = get_customer_database()
                organizations = sorted(list(set(c['organization'] for c in customer_db if c.get('organization'))))
                return render_template('form.html', form_data=request.form, customer_database=customer_db, organization_list=organizations)

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
            # แก้ไขเล็กน้อย: ส่งข้อมูลที่กรอกแล้วกลับไปพร้อมกับ customer_db
            customer_db = get_customer_database()
            organizations = sorted(list(set(c['organization'] for c in customer_db if c.get('organization'))))
            return render_template('form.html', form_data=request.form, customer_database=customer_db, organization_list=organizations)

    # --- โค้ดที่ย้ายมาไว้ตรงนี้ (ส่วนของ GET request) ---
    customer_db = get_customer_database()
    organizations = sorted(list(set(c['organization'] for c in customer_db if c.get('organization'))))
    
    return render_template('form.html',
                           task_detail_snippets=TEXT_SNIPPETS.get('task_details', []),
                           customer_database=customer_db,
                           organization_list=organizations
                           )
                           
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

@app.route('/summary')
@login_required
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
                web_url = url_for('task_details', task_id=task.get('id'))
                liff_url = f"https://liff.line.me/{os.environ.get('LIFF_ID_TASK_PAGE', '')}{web_url}"
                
                parsed_task['web_url'] = web_url
                parsed_task['liff_url'] = liff_url                
                
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

    # ✅ ต้องส่ง LIFF_ID_TASK_PAGE และ google_api_connected ไปยัง Template
    return render_template("dashboard.html",
                           tasks=final_tasks, summary=stats,
                           search_query=search_query, status_filter=status_filter,
                           chart_data=chart_data,
                           LIFF_ID_TASK_PAGE=os.environ.get('LIFF_ID_TASK_PAGE'),
                           google_api_connected=check_google_api_status() # ต้องส่งค่านี้ด้วย
                           )

@app.route('/summary/print')
@login_required
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
@login_required
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
@login_required
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
                return jsonify({'status': 'error', 'message': 'กรุณากรอกสรุปงาน หรือแนบไฟล์รูปภาพ'}), 400
            if not selected_technicians:
                return jsonify({'status': 'error', 'message': 'กรุณาเลือกช่างผู้รับผิดชอบ'}), 400

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
            except ValueError:
                return jsonify({'status': 'error', 'message': 'รูปแบบวันเวลานัดหมายใหม่ไม่ถูกต้อง'}), 400

            history.append({
                'type': 'reschedule', 'summary_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
                'reason': reschedule_reason, 'new_due_date': dt_local.strftime("%d/%m/%y %H:%M"),
                'technicians': selected_technicians
            })
            flash_message = 'เลื่อนนัดและบันทึกเหตุผลเรียบร้อยแล้ว'
            flash_category = 'success'

        elif action == 'complete_task':
            work_summary = str(request.form.get('work_summary', '')).strip()
            selected_technicians = request.form.get('technicians_report', '').split(',')
            selected_technicians = [t.strip() for t in selected_technicians if t.strip()]

            if not work_summary:
                return jsonify({'status': 'error', 'message': 'กรุณากรอกสรุปงานเพื่อปิดงาน'}), 400
            if not selected_technicians:
                return jsonify({'status': 'error', 'message': 'กรุณาเลือกช่างผู้รับผิดชอบ'}), 400
            
            # ✅ รับค่าพิกัดและ user_id ของช่างจากฟอร์ม
            latitude = request.form.get('current_latitude')
            longitude = request.form.get('current_longitude')
            technician_line_user_id = request.form.get('technician_line_user_id')

            # อัปเดตพิกัดบ้านลูกค้า (ถ้ามี)
            if latitude and longitude:
                new_map_url = f"https://www.google.com/maps?q={latitude},{longitude}"
                if re.search(r"https?:\/\/[^\s]+", base_notes_text):
                    base_notes_text = re.sub(r"https?:\/\/[^\s]+", new_map_url, base_notes_text)
                else:
                    base_notes_text += f"\n{new_map_url}"
                app.logger.info(f"Updated customer location for task {task_id} to {new_map_url}")

                # อัปเดตพิกัดล่าสุดของช่าง
                if technician_line_user_id:
                    locations = load_technician_locations()
                    locations[technician_line_user_id] = {
                        'lat': float(latitude), 'lon': float(longitude),
                        'timestamp': datetime.datetime.now(THAILAND_TZ).isoformat()
                    }
                    save_technician_locations(locations)
                    app.logger.info(f"Updated technician {technician_line_user_id} location.")
            
            history.append({
                'type': 'report', 'summary_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
                'work_summary': work_summary,
                'equipment_used': _parse_equipment_string(request.form.get('equipment_used', '')),
                'attachments': new_attachments,
                'technicians': selected_technicians
            })
            
            update_payload['status'] = 'completed'
        
        else:
            return jsonify({'status': 'error', 'message': 'ไม่พบการกระทำที่ร้องขอ'}), 400
            
        history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
        all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
        
        final_notes = base_notes_text
        if all_reports_text: final_notes += all_reports_text
        if feedback_data: final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        
        update_payload['notes'] = final_notes
    
        try:
            updated_task = update_google_task(task_id, **update_payload)

            if updated_task:
                cache.clear()
                
                # ✅ จัดการ Notification และ Redirect หลังบันทึกสำเร็จ
                if action == 'complete_task':
                    technicians = request.form.get('technicians_report', '').split(',')
                    send_completion_notification(updated_task, technicians) # ส่งหาลูกค้า (ถ้ามี LINE ID)
                    
                    # ส่ง Notification สรุปเข้ากลุ่ม
                    settings = get_app_settings()
                    recipients = settings.get('line_recipients', {})
                    admin_group_id = recipients.get('admin_group_id')
                    tech_group_id = recipients.get('technician_group_id')
                    customer_info = parse_customer_info_from_notes(updated_task.get('notes', ''))
                    
                    summary_message = (f"✅ อัปเดตสถานะงาน\n\n"
                                     f"ชื่องาน: {updated_task.get('title', '-')}\n"
                                     f"ลูกค้า: {customer_info.get('name', '-')}\n"
                                     f"ช่าง: {', '.join(technicians)}\n"
                                     f"สถานะ: ปิดงานเรียบร้อยแล้ว") # <-- บรรทัดที่เพิ่มเข้ามา
                    
                    if admin_group_id: message_queue.add_message(admin_group_id, TextMessage(text=summary_message))
                    if tech_group_id and tech_group_id != admin_group_id:
                        message_queue.add_message(tech_group_id, TextMessage(text=summary_message))
                    
                    redirect_url = url_for('generate_customer_onboarding_qr', task_id=task_id)
                    return jsonify({'status': 'success', 'message': 'ปิดงานสำเร็จ!', 'redirect_url': redirect_url})

                return jsonify({'status': 'success', 'message': flash_message})
            else:
                return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกข้อมูลหลัก!'}), 500

        except Exception as e:
            app.logger.error(f'Unexpected error in task_details POST: {e}', exc_info=True)
            return jsonify({'status': 'error', 'message': f'เกิดข้อผิดพลาดที่ไม่คาดคิด: {str(e)}'}), 500
            
    # --- GET Request Logic ---
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
            for att in report['attachments']:
                all_attachments.append(att)

    # ✅ ต้องส่ง LIFF_ID_TASK_PAGE ไปยัง Template
    response = make_response(render_template('update_task_details.html',
                           task=task,
                           technician_list=app_settings.get('technician_list', []),
                           all_attachments=all_attachments,
                           progress_report_snippets=TEXT_SNIPPETS.get('progress_reports', []),
                           LIFF_ID_TASK_PAGE=os.environ.get('LIFF_ID_TASK_PAGE') # <--- บรรทัดนี้สำคัญ
                           ))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

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
@login_required
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
                    _execute_google_api_call_with_retry(drive_service.files().delete, fileId=att['id'])
                    app.logger.info(f"Deleted attachment {att['id']} from Drive.")
                except HttpError as e:
                    if e.resp.status == 404:
                        app.logger.warning(f"Attachment {att['id']} not found on Drive, skipping deletion.")
                    else:
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
                        compressed_file, mime_type, filename = compress_image_to_fit(file, MAX_FILE_SIZE_BYTES)
                        if compressed_file:
                            file_to_upload = compressed_file
                        else:
                            app.logger.error(f"Could not compress image in edit_report: '{file.filename}'")
                            flash(f"ไฟล์ '{file.filename}' ใหญ่เกินไปและไม่สามารถบีบอัดได้", 'danger')
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
@login_required
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
@login_required
def settings_page():
    if request.method == 'POST':
        try:
            data = request.json
            if not data:
                return jsonify({'status': 'error', 'message': 'ไม่พบข้อมูลที่ส่งมา'}), 400

            # รับการตั้งค่าปัจจุบัน
            current_settings = get_app_settings()

            # ตรวจสอบและอัปเดตข้อมูลตาม key ที่ส่งมา
            # วิธีนี้จะทำให้ยืดหยุ่นกว่าการใช้ if/elif ที่ซับซ้อน
            if 'report_times' in data:
                current_settings['report_times'].update(data['report_times'])
            
            if 'message_templates' in data:
                current_settings['message_templates'].update(data['message_templates'])

            if 'popup_notifications' in data:
                # ตรวจสอบและแปลงค่า checkbox ที่อาจไม่มีมาถ้าไม่ถูกเลือก
                pn_data = data['popup_notifications']
                for key in ['enabled_arrival', 'enabled_completion_customer', 'enabled_nearby_job']:
                    if key in pn_data:
                        current_settings['popup_notifications'][key] = bool(pn_data[key])
                    else:
                        # ถ้า key ไม่มีมา (checkbox ไม่ได้ติ๊ก) ให้ตั้งเป็น False
                        current_settings['popup_notifications'][key] = False
                
                # อัปเดตส่วนที่เหลือ
                current_settings['popup_notifications'].update({k: v for k, v in pn_data.items() if not isinstance(v, bool)})


            if 'line_recipients' in data:
                current_settings['line_recipients'].update(data['line_recipients'])

            if 'shop_info' in data:
                current_settings['shop_info'].update(data['shop_info'])

            # *** แก้ไขส่วนสำคัญ: รับข้อมูลช่างจาก key 'technician_list' โดยตรง ***
            if 'technician_list' in data:
                # ไม่ต้องใช้ 'technician_list_json' อีกต่อไป
                technician_list = data.get('technician_list', [])
                if isinstance(technician_list, list):
                    current_settings['technician_list'] = technician_list
                else:
                    return jsonify({'status': 'error', 'message': 'รูปแบบข้อมูลช่างไม่ถูกต้อง'}), 400
            
            if 'auto_backup' in data:
                current_settings['auto_backup'].update(data['auto_backup'])

            # บันทึกการตั้งค่าที่อัปเดตแล้ว
            if save_app_settings(current_settings):
                cache.clear()
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
    return render_template('settings_page.html', settings=settings)
    
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


# --- START of test_notification replacement ---
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
                
                # แก้ไขตรงนี้: ใช้ผลลัพธ์จากฟังก์ชันได้เลย เพราะเป็น FlexMessage อยู่แล้ว
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

def _get_technician_report_data(year, month):
    """
    ฟังก์ชันกลางสำหรับดึงและประมวลผลข้อมูลรายงานของช่าง
    รับปีและเดือนเป็น input และคืนค่า report_data และ technician_list
    """
    app_settings = get_app_settings()
    technician_list = app_settings.get('technician_list', [])
    # --- IMPROVEMENT: Create a set of official technician names for fast checking ---
    official_tech_names = {tech.get('name', '').strip() for tech in technician_list if tech.get('name')}

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
                        # --- IMPROVEMENT: Only include technicians from the official list ---
                        if tech_name in official_tech_names:
                            report[tech_name]['count'] += 1
                            customer_name = parse_customer_info_from_notes(task.get('notes', '')).get('name', 'N/A')
                            report[tech_name]['tasks'].append({
                                'id': task.get('id'),
                                'title': task.get('title'),
                                'customer_name': customer_name,
                                'completed_formatted': completed_dt.strftime("%d/%m/%Y")
                            })
            except Exception as e:
                app.logger.error(f"Error processing task {task.get('id')} for technician report: {e}")
                continue

    for tech_name in report:
        report[tech_name]['tasks'].sort(key=lambda x: x['completed_formatted'])

    # คืนค่าเป็น dict ที่เรียงลำดับตามชื่อช่าง และรายชื่อช่างทั้งหมด
    return dict(sorted(report.items())), technician_list

@app.route('/technician_report')
@login_required
def technician_report():
    now = datetime.datetime.now(THAILAND_TZ)
    try:
        year, month = int(request.args.get('year', now.year)), int(request.args.get('month', now.month))
    except (ValueError, TypeError):
        year, month = now.year, now.month

    months = [{'value': i, 'name': datetime.date(2000, i, 1).strftime('%B')} for i in range(1, 13)]

    # เรียกใช้ฟังก์ชันกลางที่เราสร้างขึ้น
    report_data, technician_list = _get_technician_report_data(year, month)

    # ส่งข้อมูลไปยัง Template
    return render_template('technician_report.html',
                        report_data=report_data, 
                        selected_year=year, 
                        selected_month=month,
                        years=list(range(now.year - 5, now.year + 2)), 
                        months=months,
                        technician_list=technician_list)

@app.route('/technician_report/print')
@login_required
def technician_report_print():
    now = datetime.datetime.now(THAILAND_TZ)
    try:
        year, month = int(request.args.get('year', now.year)), int(request.args.get('month', now.month))
    except (ValueError, TypeError):
        year, month = now.year, now.month

    # เรียกใช้ฟังก์ชันกลางที่เราสร้างขึ้น
    sorted_report, technician_list = _get_technician_report_data(year, month)

    # ส่งข้อมูลไปยัง Template
    return render_template('technician_report_print.html',
                        report_data=sorted_report,
                        selected_year=year,
                        selected_month=month,
                        now=datetime.datetime.now(THAILAND_TZ),
                        technician_list=technician_list)

@app.route('/manage_duplicates', methods=['GET'])
@login_required
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
@login_required
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

@app.route('/generate_customer_onboarding_qr/<task_id>')
def generate_customer_onboarding_qr(task_id):
    task = get_single_task(task_id)
    if not task:
        abort(404)

    # ✅ ดึง LINE OA ID ของคุณจาก Environment Variable (ถ้ามี) หรือใส่ค่าโดยตรง
    # เช่น "@123abcde" (ต้องมี @)
    line_oa_id = os.environ.get('LINE_OA_ID', '@comphone') 

    # ✅ สร้างลิงก์เพิ่มเพื่อนพร้อม Referral Code ที่เป็นรหัสงาน
    add_friend_url = f"https://line.me/R/ti/p/{line_oa_id}?referral={task_id}"
    
    qr_code = generate_qr_code_base64(add_friend_url)
    customer = parse_customer_info_from_notes(task.get('notes', ''))
    
    response = make_response(render_template('generate_onboarding_qr.html',
                                             qr_code_base64=qr_code,
                                             task=task,
                                             customer_info=customer,
                                             liff_url=add_friend_url, # ส่ง URL ใหม่ไปให้ Template
                                             now=datetime.datetime.now(THAILAND_TZ)
                                             ))
    
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response

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

# START: แทนที่ฟังก์ชัน handle_follow_event เดิมทั้งหมดด้วยโค้ดนี้
@handler.add(FollowEvent)
def handle_follow_event(event):
    user_id = event.source.user_id
    
    if not (hasattr(event, 'follow') and hasattr(event.follow, 'referral')):
        app.logger.info(f"User {user_id} followed without a referral.")
        # (ทางเลือก) อาจจะส่งข้อความต้อนรับทั่วไปที่นี่
        # settings = get_app_settings()
        # shop = settings.get('shop_info', {})
        # generic_welcome = f"ขอบคุณที่เพิ่มเพื่อนกับ Comphone ครับ/ค่ะ!\nติดต่อ:\nโทร: {shop.get('contact_phone', '-')}\nLINE ID: {shop.get('line_id', '-')}"
        # message_queue.add_message(user_id, TextMessage(text=generic_welcome))
        return

    task_id = event.follow.referral
    app.logger.info(f"User {user_id} followed via referral link for task: {task_id}")

    try:
        task = get_single_task(task_id)
        if not task:
            raise ValueError(f"Task with ID {task_id} not found.")

        # --- บันทึก User ID ลงใน Google Task ---
        notes = task.get('notes', '')
        feedback = parse_customer_feedback_from_notes(notes)
        
        feedback['customer_line_user_id'] = user_id
        feedback['id_saved_date'] = datetime.datetime.now(THAILAND_TZ).isoformat()

        reports_history, base = parse_tech_report_from_notes(notes)
        reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in reports_history])
        final_notes = f"{base.strip()}"
        if reports_text: final_notes += reports_text
        final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

        update_google_task(task_id=task_id, notes=final_notes)
        cache.clear()
        
        # --- สร้างและส่งข้อความต้อนรับ + ลิงก์รายงาน ---
        welcome_message = render_template_message('welcome_customer', task)
        report_url = url_for('public_task_report', task_id=task_id, _external=True)
        report_message = f"คุณสามารถดูรายละเอียดและสถานะงานซ่อมของคุณได้ที่นี่:\n{report_url}"

        message_queue.add_message(user_id, [
            TextMessage(text=welcome_message),
            TextMessage(text=report_message)
        ])
        app.logger.info(f"Welcome & Report Link messages queued for user {user_id}.")

    except Exception as e:
        app.logger.error(f"Error during handle_follow_event for task {task_id}: {e}", exc_info=True)
        
        # --- ✅ ส่วนสำคัญ: แจ้งเตือนแอดมินเมื่อเกิดข้อผิดพลาด ---
        settings = get_app_settings()
        admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')
        if admin_group_id:
            error_message = (
                f"🚨 **ลูกค้าแอดเพื่อนไม่สำเร็จ** 🚨\n\n"
                f"มีลูกค้าพยายามเพิ่มเพื่อนเพื่อติดตามงาน แต่ระบบเกิดข้อผิดพลาด\n"
                f"**กรุณาติดต่อลูกค้าโดยตรง**\n\n"
                f"Task ID: `{task_id}`\n"
                f"LINE User ID: `{user_id}`\n\n"
                f"สาเหตุ: `{str(e)}`"
            )
            message_queue.add_message(admin_group_id, TextMessage(text=error_message))
# END: สิ้นสุดโค้ดสำหรับแทนที่

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
                msg_text = (f"🔔 งานสำหรับวันนี้\n\n"
                           f"ชื่องาน: {task.get('title', '-')}\n"
                           f"👤 ลูกค้า: {customer.get('name', '-')}\n"
                           f"📞 โทร: {customer.get('phone', '-')}\n"
                           f"🗓️ นัดหมาย: {dates.get('due_formatted', '-')}\n"
                           f"📍 {loc}\n\n"
                           f"🔗 ดูรายละเอียด/แก้ไข:\n{url_for('task_details', task_id=task.get('id'), _external=True)}")
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
            f"ดูข้อมูลทั้งหมด: {url_for('summary', _external=True)}"
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

@app.route("/admin/organize_files", methods=['GET', 'POST'])
@login_required
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

@app.route('/liff_notification_popup')
def liff_notification_popup():
    return render_template('liff_notification_popup.html', LIFF_ID_FORM=LIFF_ID_FORM)

# --- START: เพิ่ม 2 routes นี้เข้าไปใน app.py ---
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
    """
    จัดการการเรียกกลับ (callback) จาก LINE Login หลังจากผู้ใช้ยืนยันตัวตนแล้ว
    ฟังก์ชันนี้จะรับ authorization code จาก LINE, แลกเปลี่ยนเป็น access token,
    ดึงข้อมูลโปรไฟล์ผู้ใช้, บันทึกข้อมูล session, และเปลี่ยนเส้นทางผู้ใช้
    ไปยังหน้าที่ต้องการเดิม (หรือหน้าสรุปหากไม่มีการระบุ)
    """
    # 1. รับรหัสชั่วคราว (code) จาก LINE
    # 'code' คือ authorization code ที่ LINE ส่งกลับมาหลังจากผู้ใช้ยืนยันตัวตน
    code = request.args.get('code')
    if not code:
        # หากไม่มี code แสดงว่าการล็อกอินผิดพลาด
        app.logger.error("LINE Login Error: Missing authorization code in callback.")
        flash('การเข้าสู่ระบบผิดพลาด: ไม่พบ authorization code', 'danger')
        return redirect(url_for('login_page'))

    # 2. เตรียมข้อมูลเพื่อนำรหัสไปแลก Access Token
    # สร้าง URL และข้อมูลที่จำเป็นสำหรับการส่งคำขอ POST ไปยัง LINE เพื่อแลก token
    token_url = 'https://api.line.me/oauth2/v2.1/token'
    token_headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    token_data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': url_for('callback_line', _external=True), # ต้องตรงกับ Callback URL ใน LINE Developers Console
        'client_id': LINE_LOGIN_CHANNEL_ID,
        'client_secret': LINE_LOGIN_CHANNEL_SECRET
    }

    try:
        # 3. ส่งคำขอไปแลก Access Token
        # ใช้ requests.post เพื่อส่งข้อมูลไปแลก token
        response = requests.post(token_url, headers=token_headers, data=token_data)
        response.raise_for_status() # ตรวจสอบว่ามี HTTP Error (เช่น 4xx หรือ 5xx) หรือไม่
        token_info = response.json() # แปลง JSON response เป็น Python dictionary

        # 4. นำ Access Token ไปขอข้อมูลโปรไฟล์ผู้ใช้
        # ใช้ access token ที่ได้มาเพื่อขอข้อมูลโปรไฟล์ของผู้ใช้ LINE
        profile_url = 'https://api.line.me/v2/profile'
        profile_headers = {'Authorization': f'Bearer {token_info["access_token"]}'}
        profile_response = requests.get(profile_url, headers=profile_headers)
        profile_response.raise_for_status() # ตรวจสอบ HTTP Error อีกครั้ง
        profile_info = profile_response.json() # แปลง JSON response เป็น Python dictionary
      
        app.logger.info(f"Session created for user {profile_info['displayName']} ({profile_info['userId']})")
        app.logger.info(f"Session ID after login: {session.get('_id', 'No Session ID')}")

        app.logger.info(f"User {profile_info['displayName']} ({profile_info['userId']}) logged in successfully via web.")

        next_url = session.pop('next_url', None)

        app.logger.info(f"DEBUG: callback_line - next_url retrieved from session: '{next_url}'")

        redirect_target = url_for('summary') # ค่าเริ่มต้น
        if next_url and next_url != '/':
            redirect_target = next_url
            app.logger.info(f"DEBUG: callback_line - Redirecting to stored next_url: '{redirect_target}'")
        else:
            app.logger.info(f"DEBUG: callback_line - No specific next_url or it was root. Redirecting to default: '{redirect_target}'")
        
        return redirect(redirect_target)

    except requests.exceptions.RequestException as e:
        # ดักจับข้อผิดพลาดที่เกี่ยวข้องกับการเชื่อมต่อ HTTP (เช่น Network issues, 4xx หรือ 5xx errors)
        app.logger.error(f"LINE Login Request Error: {e}", exc_info=True)
        flash(f'เกิดข้อผิดพลาดในการเชื่อมต่อกับ LINE: {e}', 'danger')
        return redirect(url_for('login_page'))
    except Exception as e:
        # ดักจับข้อผิดพลาดอื่นๆ ที่ไม่คาดคิด
        app.logger.error(f"An unexpected error occurred during LINE callback: {e}", exc_info=True)
        flash('เกิดข้อผิดพลาดที่ไม่คาดคิดระหว่างการเข้าสู่ระบบ', 'danger')
        return redirect(url_for('login_page'))

@app.route('/public/report/<task_id>')
def public_task_report(task_id):
    """
    หน้ารายงานสาธารณะสำหรับให้ลูกค้าดู
    """
    task_raw = get_single_task(task_id)
    if not task_raw:
        abort(404)

    # ตรวจสอบว่างานเสร็จสิ้นแล้วหรือไม่ (เพื่อความปลอดภัย)
    if task_raw.get('status') != 'completed':
        # อาจจะแสดงข้อความว่า "รายงานจะพร้อมให้ดูเมื่องานเสร็จสิ้น" หรือ 404 ไปเลย
        abort(404)

    task = parse_google_task_dates(task_raw)
    notes = task.get('notes', '')

    # ดึงข้อมูลที่จำเป็นเท่านั้น
    task['customer'] = parse_customer_info_from_notes(notes)
    task['tech_reports_history'], _ = parse_tech_report_from_notes(notes)

    # คัดกรองเฉพาะรายงานที่มีเนื้อหาหรือรูปภาพ
    task['tech_reports_history'] = [
        r for r in task['tech_reports_history'] 
        if r.get('work_summary') or r.get('attachments')
    ]

    response = make_response(render_template('public_report.html', task=task))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response
   
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=True)