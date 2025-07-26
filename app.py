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

# --- Global Objects ---
app.cache = TTLCache(maxsize=100, ttl=60)
app.line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))

# --- Register Blueprints ---
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

# --- Utility function required by scheduler ---
def _create_backup_zip():
    try:
        all_tasks = gs.get_google_tasks_for_report(show_completed=True)
        if all_tasks is None: return None, None

        memory_file = BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('data/tasks_backup.json', json.dumps(all_tasks, indent=4, ensure_ascii=False))
            zf.writestr('data/settings_backup.json', json.dumps(get_app_settings(), indent=4, ensure_ascii=False))
            
            project_root = os.path.dirname(os.path.abspath(__file__))
            for folder, _, files in os.walk(project_root):
                for file in files:
                    if file.endswith(('.py', '.html', '.css', '.js', '.json', 'Procfile', 'requirements.txt')) and file not in ['.env', SETTINGS_FILE]:
                        file_path = os.path.join(folder, file)
                        archive_name = os.path.relpath(file_path, project_root)
                        zf.write(file_path, arcname=f'code/{archive_name}')
        memory_file.seek(0)
        backup_filename = f"full_system_backup_{datetime.datetime.now(utils.THAILAND_TZ).strftime('%Y%m%d_%H%M%S')}.zip"
        return memory_file, backup_filename
    except Exception as e:
        app.logger.error(f"Error creating full system backup zip: {e}")
        return None, None

# --- Core App Routes (under main_bp) ---
@main_bp.route("/")
def root_redirect():
    return redirect(url_for('tools.dashboard'))

@main_bp.route('/form', methods=['GET', 'POST'])
def form_page():
    if request.method == 'POST':
        task_title = str(request.form.get('task_title', '')).strip()
        customer_name = str(request.form.get('customer', '')).strip()
        if not task_title or not customer_name:
            flash('กรุณากรอกชื่อผู้ติดต่อและรายละเอียดงาน', 'danger')
            return redirect(url_for('main.form_page'))

        # ... (full logic from original file to construct notes and due date) ...
        
        new_task = gs.create_google_task(task_title, notes=notes, due=due_date_gmt)
        if new_task:
            app.cache.clear()
            send_new_task_notification(new_task)
            flash('สร้างงานใหม่เรียบร้อยแล้ว!', 'success')
            return redirect(url_for('main.task_details', task_id=new_task['id']))
        else:
            flash('เกิดข้อผิดพลาดในการสร้างงาน', 'danger')
            return render_template('form.html', form_data=request.form)

    return render_template('form.html')

@main_bp.route('/task/<task_id>', methods=['GET', 'POST'])
def task_details(task_id):
    if request.method == 'POST':
        task_raw = gs.get_single_task(task_id)
        if not task_raw: abort(404)
        
        # ... (full logic for POST: save_report, reschedule_task, complete_task) ...
        
        updated_task = gs.update_google_task(task_id, **update_payload)
        if updated_task:
            app.cache.clear()
            # ... (logic to send notifications based on action) ...
            return jsonify({'status': 'success', 'message': flash_message})
        else:
            return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกข้อมูล!'}), 500

    task_raw = gs.get_single_task(task_id)
    if not task_raw: abort(404)
    
    p_task = utils.parse_google_task_dates(task_raw)
    p_task['tech_history'], _ = utils.parse_tech_report_from_notes(p_task.get('notes', ''))
    # ... (rest of the GET logic to prepare data for template) ...
    
    return render_template('task_details.html', task=p_task, settings=get_app_settings())

@main_bp.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        technician_list = json.loads(request.form.get('technician_list_json', '[]'))
        settings_data = {
            # ... (full logic to gather all form data into a dict) ...
        }
        if save_app_settings(settings_data):
            initialize_scheduler(app)
            app.cache.clear()
            if gs.backup_settings_to_drive(get_app_settings()):
                flash('บันทึกและสำรองการตั้งค่าเรียบร้อยแล้ว!', 'success')
            else:
                flash('บันทึกสำเร็จ แต่สำรองไป Drive ไม่สำเร็จ!', 'warning')
        else:
            flash('เกิดข้อผิดพลาดในการบันทึก!', 'danger')
        return redirect(url_for('main.settings_page'))
    return render_template('settings_page.html', settings=get_app_settings())

# --- Webhook & OAuth Routes ---
@app.route("/callback", methods=['POST'])
def callback():
    # ... (full callback logic) ...
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def message_handler(event):
    with app.app_context(): handle_text_message(event)

@handler.add(PostbackEvent)
def postback_handler(event):
    with app.app_context(): handle_postback(event)

@app.route('/authorize')
def authorize():
    client_secrets_json_str = os.environ.get('GOOGLE_CLIENT_SECRETS_JSON')
    if not client_secrets_json_str:
        flash('ไม่สามารถเริ่มการเชื่อมต่อได้: ไม่ได้ตั้งค่า `GOOGLE_CLIENT_SECRETS_JSON`', 'danger')
        return redirect(url_for('main.settings_page'))
    
    flow = Flow.from_client_config(json.loads(client_secrets_json_str), scopes=gs.SCOPES, redirect_uri=url_for('authorize', _external=True, _scheme='https'))
    authorization_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true', prompt='consent')
    session['state'] = state
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    state = session.get('state')
    if not state or state != request.args.get('state'): abort(401) 
    
    # ... (full logic to fetch token) ...

    os.environ['GOOGLE_TOKEN_JSON'] = token_json
    gs.get_refreshed_credentials(force_refresh=True)
    
    flash('เชื่อมต่อ Google API สำเร็จ! กรุณาคัดลอก Token ใหม่จาก Log และรีสตาร์ทแอป', 'success')
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
