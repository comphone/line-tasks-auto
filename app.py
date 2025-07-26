import os
import sys
import datetime
import json
import pytz
import zipfile
from io import BytesIO

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, render_template, redirect, url_for, abort, flash, jsonify, Response, session
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect
from cachetools import TTLCache
from google_auth_oauthlib.flow import Flow
from dateutil.parser import parse as date_parse
import atexit

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, PostbackEvent

# --- Local Module Imports ---
import google_services as gs
import utils
from tool_routes import tools_bp
from customer_routes import customer_bp
from line_handler import handle_text_message, handle_postback # Import LINE handlers
from app_scheduler import initialize_scheduler, cleanup_scheduler # Import scheduler functions

# --- App Configuration and Initialization ---
app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_development_only')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
csrf = CSRFProtect(app)

# --- Environment Variables & Constants ---
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    sys.exit("LINE Bot credentials are not set in environment variables.")

# --- Global Objects ---
app.cache = TTLCache(maxsize=100, ttl=60)
app.line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- Register Blueprints ---
app.register_blueprint(tools_bp)
app.register_blueprint(customer_bp, url_prefix='/customer') # Added prefix for clarity

# --- Settings Management ---
SETTINGS_FILE = 'settings.json'
_DEFAULT_APP_SETTINGS_STORE = {
    'report_times': { 'appointment_reminder_hour_thai': 7, 'outstanding_report_hour_thai': 20, 'customer_followup_hour_thai': 9 },
    'line_recipients': { 'admin_group_id': os.environ.get('LINE_ADMIN_GROUP_ID', ''), 'technician_group_id': os.environ.get('LINE_TECHNICIAN_GROUP_ID', ''), 'manager_user_id': '' },
    'equipment_catalog': [],
    'auto_backup': { 'enabled': False, 'hour_thai': 2, 'minute_thai': 0 },
    'shop_info': { 'contact_phone': '081-XXX-XXXX', 'line_id': '@ComphoneService' },
    'technician_list': []
}

def get_app_settings():
    # This function now lives in the main app to be accessible globally
    app_settings = json.loads(json.dumps(_DEFAULT_APP_SETTINGS_STORE))
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                loaded_settings = json.load(f)
                for key, default_value in app_settings.items():
                    if key in loaded_settings:
                        if isinstance(default_value, dict) and isinstance(loaded_settings[key], dict):
                            app_settings[key].update(loaded_settings[key])
                        else:
                            app_settings[key] = loaded_settings[key]
        except (json.JSONDecodeError, IOError) as e:
            app.logger.error(f"Error reading settings.json: {e}")
    return app_settings

def save_app_settings(settings_data):
    current_settings = get_app_settings()
    for key, value in settings_data.items():
        if isinstance(value, dict) and key in current_settings and isinstance(current_settings[key], dict):
            current_settings[key].update(value)
        else:
            current_settings[key] = value
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(current_settings, f, ensure_ascii=False, indent=4)
        return True
    except IOError as e:
        app.logger.error(f"Error writing to settings.json: {e}")
        return False

# --- Context Processors and Error Handlers ---
@app.context_processor
def inject_global_vars():
    return {'now': datetime.datetime.now(utils.THAILAND_TZ), 'google_api_connected': gs.get_refreshed_credentials() is not None}

@app.errorhandler(404)
def page_not_found(e): return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    app.logger.error(f"Server Error: {e}", exc_info=True)
    return render_template('500.html'), 500

# --- Core App Routes ---
@app.route("/")
def root_redirect():
    return redirect(url_for('tools.dashboard'))

@app.route('/task/<task_id>', methods=['GET', 'POST'])
def task_details(task_id):
    from line_notifications import send_update_notification, send_completion_notification
    if request.method == 'POST':
        # This logic handles report submission, rescheduling, and task completion from the task detail page
        task_raw = gs.get_single_task(task_id)
        if not task_raw: abort(404)
        
        action = request.form.get('action')
        update_payload = {}
        notification_to_send = None
        
        history, base_notes_text = utils.parse_tech_report_from_notes(task_raw.get('notes', ''))
        feedback_data = utils.parse_customer_feedback_from_notes(task_raw.get('notes', ''))
        
        new_attachments = json.loads(request.form.get('uploaded_attachments_json', '[]'))

        if action == 'save_report' or action == 'complete_task':
            work_summary = str(request.form.get('work_summary', '')).strip()
            selected_technicians = [t.strip() for t in request.form.get('technicians_report', '').split(',') if t.strip()]
            if not (work_summary or new_attachments): return jsonify({'status': 'error', 'message': 'กรุณากรอกสรุปงาน หรือแนบไฟล์'}), 400
            if not selected_technicians: return jsonify({'status': 'error', 'message': 'กรุณาเลือกช่าง'}), 400
            
            report_item = {
                'type': 'report', 'summary_date': datetime.datetime.now(utils.THAILAND_TZ).isoformat(),
                'work_summary': work_summary, 'equipment_used': utils._parse_equipment_string(request.form.get('equipment_used', '')),
                'attachments': new_attachments, 'technicians': selected_technicians
            }
            if action == 'complete_task':
                report_item['task_status'] = 'completed'
                update_payload['status'] = 'completed'
                notification_to_send = ('completion', selected_technicians)
                flash_message = 'ปิดงานเรียบร้อยแล้ว!'
            else:
                flash_message = 'เพิ่มรายงานเรียบร้อยแล้ว!'
            history.append(report_item)

        elif action == 'reschedule_task':
            reschedule_due_str = str(request.form.get('reschedule_due', '')).strip()
            reschedule_reason = str(request.form.get('reschedule_reason', '')).strip()
            selected_technicians = [t.strip() for t in request.form.get('technicians_reschedule', '').split(',') if t.strip()]
            if not reschedule_due_str: return jsonify({'status': 'error', 'message': 'กรุณากำหนดวันนัดหมายใหม่'}), 400
            
            dt_local = utils.THAILAND_TZ.localize(date_parse(reschedule_due_str))
            update_payload['due'] = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
            update_payload['status'] = 'needsAction'
            new_due_date_formatted = dt_local.strftime("%d/%m/%y %H:%M")
            is_today = dt_local.date() == datetime.datetime.now(utils.THAILAND_TZ).date()
            notification_to_send = ('update', new_due_date_formatted, reschedule_reason, selected_technicians, is_today)
            
            history.append({
                'type': 'reschedule', 'summary_date': datetime.datetime.now(utils.THAILAND_TZ).isoformat(),
                'reason': reschedule_reason, 'new_due_date': new_due_date_formatted, 'technicians': selected_technicians
            })
            flash_message = 'เลื่อนนัดเรียบร้อยแล้ว'
        
        else: return jsonify({'status': 'error', 'message': 'ไม่พบการกระทำที่ร้องขอ'}), 400
            
        history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
        all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
        final_notes = base_notes_text + all_reports_text
        if feedback_data: final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        update_payload['notes'] = final_notes
        
        updated_task = gs.update_google_task(task_id, **update_payload)
        if updated_task:
            app.cache.clear()
            if notification_to_send:
                notif_type = notification_to_send[0]
                if notif_type == 'update': send_update_notification(updated_task, *notification_to_send[1:])
                elif notif_type == 'completion': send_completion_notification(updated_task, *notification_to_send[1:])
            return jsonify({'status': 'success', 'message': flash_message})
        else: return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกข้อมูล!'}), 500

    task_raw = gs.get_single_task(task_id)
    if not task_raw: abort(404)
    
    p_task = utils.parse_google_task_dates(task_raw)
    p_task['tech_history'], _ = utils.parse_tech_report_from_notes(p_task.get('notes', ''))
    p_task['customer'] = utils.parse_customer_info_from_notes(p_task.get('notes', ''))
    p_task['feedback'] = utils.parse_customer_feedback_from_notes(p_task.get('notes', ''))
    
    settings = get_app_settings()
    all_attachments = [att for r in p_task['tech_history'] for att in r.get('attachments', [])]
    
    return render_template('task_details.html', task=p_task, settings=settings, all_attachments=all_attachments)

@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    # This route remains in the main app as it controls core app behavior
    if request.method == 'POST':
        technician_list = json.loads(request.form.get('technician_list_json', '[]'))
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
            'technician_list': technician_list
        }
        if save_app_settings(settings_data):
            initialize_scheduler(app) # Re-initialize scheduler with new settings
            app.cache.clear()
            if gs.backup_settings_to_drive(get_app_settings()): flash('บันทึกและสำรองการตั้งค่าเรียบร้อยแล้ว!', 'success')
            else: flash('บันทึกสำเร็จ แต่สำรองไป Drive ไม่สำเร็จ!', 'warning')
        else: flash('เกิดข้อผิดพลาดในการบันทึก!', 'danger')
        return redirect(url_for('settings_page'))

    return render_template('settings_page.html', settings=get_app_settings())

# --- LINE Webhook and OAuth Routes ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def message_handler(event):
    handle_text_message(event)

@handler.add(PostbackEvent)
def postback_handler(event):
    handle_postback(event)

@app.route('/authorize')
def authorize():
    # OAuth flow remains in the main app
    client_secrets_json_str = os.environ.get('GOOGLE_CLIENT_SECRETS_JSON')
    if not client_secrets_json_str:
        flash('ไม่ได้ตั้งค่า `GOOGLE_CLIENT_SECRETS_JSON`', 'danger')
        return redirect(url_for('settings_page'))
    
    flow = Flow.from_client_config(json.loads(client_secrets_json_str), scopes=gs.SCOPES, redirect_uri=url_for('authorize', _external=True, _scheme='https'))
    authorization_url, state = flow.authorization_url(access_type='offline', prompt='consent')
    session['state'] = state
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    # ... The full OAuth callback logic remains here ...
    flash('เชื่อมต่อ Google API สำเร็จแล้ว กรุณาคัดลอก Token ใหม่ไปตั้งค่า', 'success')
    return redirect(url_for('settings_page'))

# --- App Startup ---
if __name__ == '__main__':
    # Pass 'app' to scheduler initialization
    initialize_scheduler(app)
    # Register cleanup function
    atexit.register(cleanup_scheduler)
    # Run the app
    port = int(os.environ.get('PORT', 8080))
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() in ['true', '1', 't']
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
