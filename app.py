import os
import datetime
import json
import pytz
import atexit
import zipfile
from io import BytesIO

from dotenv import load_dotenv
load_dotenv()

from flask import (Flask, request, render_template, redirect, url_for, abort,
                   session, jsonify, flash, Blueprint)
from flask_wtf.csrf import CSRFProtect
from cachetools import TTLCache
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, PostbackEvent

# --- Local Module Imports ---
import google_services as gs
import utils
from settings_manager import get_app_settings, save_app_settings
from tool_routes import tools_bp
from customer_routes import customer_bp
from main_routes import main_bp, handle_text_message, handle_postback

from app_scheduler import initialize_scheduler, cleanup_scheduler
from line_notifications import send_update_notification, send_completion_notification, send_new_task_notification

# --- App Initialization ---
app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_dev')
csrf = CSRFProtect(app)

# --- Global Objects & Environment Variables ---
app.cache = TTLCache(maxsize=100, ttl=60)
app.line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET')) 
app.LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM')

# Define constants for file uploads
app.config['UPLOAD_FOLDER'] = 'static/uploads'
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

app.ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'kmz', 'kml', 'doc', 'docx', 'xls', 'xlsx', 'zip', 'rar'} 
app.MAX_FILE_SIZE_MB = 50
app.MAX_FILE_SIZE_BYTES = app.MAX_FILE_SIZE_MB * 1024 * 1024

# --- Register Blueprints ---
app.register_blueprint(main_bp) 
app.register_blueprint(tools_bp)
app.register_blueprint(customer_bp)

# --- Context Processors & Error Handlers ---
@app.context_processor
def inject_global_vars():
    """Injects variables into all templates."""
    return {
        'now': datetime.datetime.now(utils.THAILAND_TZ), 
        'google_api_connected': gs.get_refreshed_credentials() is not None,
        'technician_list': get_app_settings().get('technician_list', []),
        'thaizone': utils.THAILAND_TZ,
        'dateutil_parse': utils.date_parse,
        'get_file_icon': utils.get_file_icon
    }

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    app.logger.error(f"Server Error: {e}", exc_info=True)
    return render_template('500.html'), 500

# --- LINE Webhook Handlers ---
@handler.add(MessageEvent, message=TextMessage)
def handle_line_message_event(event):
    with app.app_context():
        handle_text_message(event)

@handler.add(PostbackEvent)
def handle_line_postback_event(event):
    with app.app_context():
        handle_postback(event)

# --- App Startup ---
if __name__ == '__main__':
    with app.app_context():
        # Load settings from Drive on startup (if configured)
        gs.load_settings_from_drive_on_startup(save_app_settings)
        # Initialize and start scheduler
        initialize_scheduler(app) 
    # Register scheduler cleanup function to run on app exit
    atexit.register(cleanup_scheduler)
    
    port = int(os.environ.get('PORT', 8080))
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() in ['true', '1', 't']
    app.run(host='0.0.0.0', port=port, debug=debug_mode)