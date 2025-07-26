import os
import sys
import datetime
import json
import pytz
import atexit
import zipfile
from io import BytesIO

from dotenv import load_dotenv
load_dotenv()

from flask import (Flask, request, render_template, redirect, url_for, abort,
                   session, jsonify, flash)
from flask_wtf.csrf import CSRFProtect
from cachetools import TTLCache
from google_auth_oauthlib.flow import Flow
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, PostbackEvent

# --- Local Module Imports ---
import google_services as gs
import utils
from settings_manager import get_app_settings, save_app_settings
from tool_routes import tools_bp
from customer_routes import customer_bp
from line_handler import handle_text_message, handle_postback
from app_scheduler import initialize_scheduler, cleanup_scheduler
from line_notifications import send_update_notification, send_completion_notification

# --- App Initialization ---
app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_dev')
csrf = CSRFProtect(app)

# --- Global Objects ---
app.cache = TTLCache(maxsize=100, ttl=60)
app.line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))

# --- Register Blueprints ---
# สร้าง Blueprint สำหรับ Route หลักในไฟล์นี้
main_bp = Blueprint('main', __name__)
app.register_blueprint(main_bp)
app.register_blueprint(tools_bp)
app.register_blueprint(customer_bp)

# --- Context Processors & Error Handlers ---
@app.context_processor
def inject_global_vars():
    return {'now': datetime.datetime.now(utils.THAILAND_TZ), 'google_api_connected': gs.get_refreshed_credentials() is not None}

@app.errorhandler(404)
def page_not_found(e): return render_template('404.html'), 404
@app.errorhandler(500)
def internal_server_error(e):
    app.logger.error(f"Server Error: {e}", exc_info=True)
    return render_template('500.html'), 500

# --- Core App Routes (under main_bp) ---
@main_bp.route("/")
def root_redirect():
    return redirect(url_for('tools.dashboard'))

@main_bp.route('/task/<task_id>', methods=['GET', 'POST'])
def task_details(task_id):
    # ... (Logic for task details page remains here as it's a core view) ...
    return render_template('task_details.html', ...)

@main_bp.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        # ... (Logic for saving settings) ...
        flash('บันทึกการตั้งค่าเรียบร้อยแล้ว', 'success')
        return redirect(url_for('main.settings_page'))
    return render_template('settings_page.html', settings=get_app_settings())

# --- Utility function required by scheduler ---
def _create_backup_zip():
    # ... (The logic for creating a zip backup) ...
    return None, None # Placeholder

# --- Webhook & OAuth Routes ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def message_handler(event):
    with app.app_context(): handle_text_message(event)

@handler.add(PostbackEvent)
def postback_handler(event):
    with app.app_context(): handle_postback(event)

@app.route('/authorize')
def authorize():
    # ... (OAuth logic) ...
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    # ... (OAuth callback logic) ...
    return redirect(url_for('main.settings_page'))

# --- App Startup ---
if __name__ == '__main__':
    with app.app_context():
        gs.load_settings_from_drive_on_startup(save_app_settings)
        initialize_scheduler(app)
    atexit.register(cleanup_scheduler)
    
    port = int(os.environ.get('PORT', 8080))
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() in ['true', '1', 't']
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
