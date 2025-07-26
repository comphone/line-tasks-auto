import os
import datetime
import json
import pytz
import atexit

from dotenv import load_dotenv
load_dotenv()

from flask import (Flask, request, render_template, redirect, url_for, abort,
                   session, jsonify, flash)
from flask_wtf.csrf import CSRFProtect
from cachetools import TTLCache
from google_auth_oauthlib.flow import Flow
from dateutil.parser import parse as date_parse
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

# --- Register Blueprints ---
main_bp = Blueprint('main', __name__)
app.register_blueprint(main_bp)
app.register_blueprint(tools_bp)
app.register_blueprint(customer_bp)

# --- Context Processors & Error Handlers ---
@app.context_processor
def inject_global_vars():
    """Injects variables into all templates."""
    return {'now': datetime.datetime.now(utils.THAILAND_TZ), 'google_api_connected': gs.get_refreshed_credentials() is not None}

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    app.logger.error(f"Server Error: {e}", exc_info=True)
    return render_template('500.html'), 500

# --- Core App Routes (under main_bp) ---
@main_bp.route("/")
def root_redirect():
    return redirect(url_for('tools.dashboard'))

@main_bp.route('/calendar')
def calendar_page():
    """Displays the task calendar page."""
    all_tasks = gs.get_google_tasks_for_report(show_completed=False)
    if all_tasks is None:
        flash('ไม่สามารถโหลดข้อมูลงานได้', 'danger')
        unscheduled_tasks = []
    else:
        unscheduled_tasks = [
            {**task, 'customer': utils.parse_customer_info_from_notes(task.get('notes', ''))}
            for task in all_tasks if not task.get('due')
        ]
    return render_template('calendar.html', unscheduled_tasks=unscheduled_tasks)

@main_bp.route('/form', methods=['GET', 'POST'])
def form_page():
    if request.method == 'POST':
        task_title = str(request.form.get('task_title', '')).strip()
        customer_name = str(request.form.get('customer', '')).strip()
        if not task_title or not customer_name:
            flash('กรุณากรอกชื่อผู้ติดต่อและรายละเอียดงาน', 'danger')
            return redirect(url_for('main.form_page'))

        notes_lines = [
            f"หน่วยงาน: {str(request.form.get('organization_name', '')).strip()}",
            f"ลูกค้า: {customer_name}",
            f"เบอร์โทรศัพท์: {str(request.form.get('phone', '')).strip()}",
            f"ที่อยู่: {str(request.form.get('address', '')).strip()}",
            f"พิกัด: {str(request.form.get('latitude_longitude', '')).strip()}"
        ]
        notes = "\n".join(filter(None, [line.split(': ', 1)[1] and line for line in notes_lines]))

        due_date_gmt = None
        appointment_str = str(request.form.get('appointment', '')).strip()
        if appointment_str:
            try:
                dt_local = utils.THAILAND_TZ.localize(date_parse(appointment_str))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
            except ValueError:
                flash('รูปแบบวันเวลานัดหมายไม่ถูกต้อง', 'warning')
        
        new_task = gs.create_google_task(task_title, notes=notes, due=due_date_gmt)
        if new_task:
            app.cache.clear()
            send_new_task_notification(new_task)
            flash('สร้างงานใหม่เรียบร้อยแล้ว!', 'success')
            return redirect(url_for('main.task_details', task_id=new_task['id']))
        else:
            flash('เกิดข้อผิดพลาดในการสร้างงาน', 'danger')
    return render_template('form.html')

@main_bp.route('/task/<task_id>', methods=['GET', 'POST'])
def task_details(task_id):
    # This logic is complex and remains in the main app file for now.
    # It could be refactored further in the future.
    if request.method == 'POST':
        # ... (Full POST logic for updating tasks)
        pass
    
    task_raw = gs.get_single_task(task_id)
    if not task_raw: abort(404)
    
    p_task = utils.parse_google_task_dates(task_raw)
    p_task['tech_history'], _ = utils.parse_tech_report_from_notes(p_task.get('notes', ''))
    p_task['customer'] = utils.parse_customer_info_from_notes(p_task.get('notes', ''))
    p_task['feedback'] = utils.parse_customer_feedback_from_notes(p_task.get('notes', ''))
    
    all_attachments = [att for r in p_task['tech_history'] for att in r.get('attachments', [])]
    
    return render_template('update_task_details.html', task=p_task, settings=get_app_settings(), all_attachments=all_attachments)

@main_bp.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        # ... (Full POST logic for saving settings) ...
        pass
    return render_template('settings_page.html', settings=get_app_settings())

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
    # ... (Full OAuth logic) ...
    pass

@app.route('/oauth2callback')
def oauth2callback():
    # ... (Full OAuth callback logic) ...
    pass

# --- App Startup ---
if __name__ == '__main__':
    with app.app_context():
        gs.load_settings_from_drive_on_startup(save_app_settings)
        initialize_scheduler(app)
    atexit.register(cleanup_scheduler)
    
    port = int(os.environ.get('PORT', 8080))
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() in ['true', '1', 't']
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
