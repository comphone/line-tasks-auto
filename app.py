from dotenv import load_dotenv
load_dotenv()

import os
import sys
import datetime
import re
import json
import pytz
import threading
import time

from flask import Flask, request, render_template, redirect, url_for, abort, send_from_directory, flash, jsonify
from werkzeug.utils import secure_filename
from cachetools import cached, TTLCache
from geopy.distance import geodesic

# LINE & Google API imports
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, PushMessageRequest, TextMessage, ReplyMessageRequest, FlexMessage
)
from linebot.models import (
    BubbleContainer, CarouselContainer, BoxComponent, TextComponent,
    ButtonComponent, SeparatorComponent, URIAction, PostbackAction, QuickReply, QuickReplyButton
)
from linebot.v3 import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent
from linebot.v3.exceptions import InvalidSignatureError

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# --- Initialization & Configurations ---
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_dev')
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'}

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# --- LINE & Google Configs ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    sys.exit("LINE Bot credentials are not set in environment variables.")

LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM')
LINE_ADMIN_GROUP_ID = os.environ.get('LINE_ADMIN_GROUP_ID')
LINE_HR_GROUP_ID = os.environ.get('LINE_HR_GROUP_ID')
GOOGLE_TASKS_LIST_ID = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')

SCOPES = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/drive']
GOOGLE_CREDENTIALS_FILE_NAME = 'credentials.json'
THAILAND_TZ = pytz.timezone('Asia/Bangkok')
cache = TTLCache(maxsize=100, ttl=60)

# Initialize LINE Bot SDK
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_api_client = ApiClient(configuration)
line_messaging_api = MessagingApi(line_api_client)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- Technician Mention Mapping (Placeholder) ---
TECHNICIAN_LINE_IDS = {
    "ช่างเอ": "Uxxxxxxxxxxxxxxxxxxxxxxxxx1",
    "ช่างบี": "Uxxxxxxxxxxxxxxxxxxxxxxxxx2",
    # Add more technicians and their LINE User IDs here
}

# (All other helper functions from previous versions remain here)
# ... get_google_service, get_google_tasks_service, etc.
# ... create_google_task, create_google_calendar_event, etc.
# ... parse functions, find_nearby_jobs, create_flex_messages, etc.

# --- New Helper Functions ---
def upload_to_google_drive(file_path):
    """Uploads a file to a specific Google Drive folder."""
    if not GOOGLE_DRIVE_FOLDER_ID:
        app.logger.warning("GOOGLE_DRIVE_FOLDER_ID is not set. Skipping upload.")
        return None
    try:
        service = get_google_service('drive', 'v3')
        if not service: raise Exception("Could not get Google Drive service.")
        file_metadata = {'name': os.path.basename(file_path), 'parents': [GOOGLE_DRIVE_FOLDER_ID]}
        media = MediaFileUpload(file_path, mimetype='image/jpeg', resumable=True)
        file = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        return file.get('webViewLink')
    except Exception as e:
        app.logger.error(f"Failed to upload to Google Drive: {e}")
        return None

def handle_mentions(text, recipients_set):
    """Checks for @mentions and adds the corresponding LINE ID to recipients."""
    mentioned_users = re.findall(r"@(\w+)", text)
    for user in mentioned_users:
        if user in TECHNICIAN_LINE_IDS:
            recipients_set.add(TECHNICIAN_LINE_IDS[user])
            
def request_customer_feedback(customer_line_id, task_id, task_title):
    """Sends a feedback request to the customer."""
    # ... (Implementation from previous turn)
    pass
    
# --- Updated Web Page Routes ---

@app.route("/", methods=['GET', 'POST'])
def form_page():
    if request.method == 'POST':
        # ... (Form processing logic)
        
        # --- Handle File Uploads to Google Drive ---
        uploaded_drive_links = []
        if 'attachments' in request.files:
            files = request.files.getlist('attachments')
            for file in files:
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    file.save(file_path)
                    
                    drive_link = upload_to_google_drive(file_path)
                    if drive_link:
                        uploaded_drive_links.append(drive_link)
                    # Clean up local file after upload
                    os.remove(file_path)

        if uploaded_drive_links:
            notes += "\n\nไฟล์แนบ:\n" + "\n".join(uploaded_drive_links)
            
        # ... (Rest of the logic to create task and calendar event)

        # --- Handle @mentions ---
        recipients_to_notify = {id for id in [settings['line_recipients'].get('admin_group_id'), settings['line_recipients'].get('technician_group_id')] if id}
        handle_mentions(detail, recipients_to_notify)
        
        if created_task:
            flex_message = create_task_flex_message(created_task)
            if recipients_to_notify:
                line_messaging_api.push_message(PushMessageRequest(to=list(recipients_to_notify), messages=[flex_message]))
            # ... (flash message and redirect)

    return render_template('form.html')


@app.route('/search_customers')
def search_customers():
    """API endpoint for customer lookup."""
    query = request.args.get('q', '').lower()
    tasks = get_google_tasks_for_report(show_completed=True)
    
    customers = {}
    if tasks:
        for task in tasks:
            info = parse_customer_info_from_notes(task.get('notes', ''))
            if info['name'] != 'N/A' and query in info['name'].lower():
                # Use name as key to avoid duplicates, storing the most recent info
                if info['name'] not in customers:
                    customers[info['name']] = info

    return jsonify(list(customers.values()))

# --- Updated LINE Handlers ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """Handles incoming messages and calls the correct function."""
    text = event.message.text.strip()
    text_lower = text.lower()

    if text_lower == 'นัดหมาย':
        liff_url = f"https://liff.line.me/{LIFF_ID_FORM}"
        reply_to_line(event.reply_token, [TextMessage(text="กรุณากดปุ่มด้านล่างเพื่อเปิดฟอร์มสร้างงาน",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=URIAction(label="➕ สร้างงานใหม่", uri=liff_url))
            ]))])
        return

    if text_lower.startswith('แจ้งปัญหา '):
        # Logic to handle issue reporting and @mentioning the original technician
        pass
        
    # ... (other command handlers like 'c', 'ดูงาน', etc.)
    
@handler.add(PostbackEvent)
def handle_postback(event):
    """Handles postback events from feedback buttons."""
    # ... (Logic from previous turn to handle customer feedback)
    pass

# ... (rest of the file remains the same)
