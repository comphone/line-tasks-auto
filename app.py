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
from line_handler import handle_text_message, handle_postback
from app_scheduler import initialize_scheduler, cleanup_scheduler
from line_notifications import send_update_notification, send_completion_notification, send_new_task_notification

# --- App Initialization ---
app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_dev')
csrf = CSRFProtect(app)

# --- Global Objects & Environment Variables (Moved handler definition here) ---
app.cache = TTLCache(maxsize=100, ttl=60)
app.line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET')) # Moved this line up
app.LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM')

# --- Define Blueprints and their routes BEFORE app initialization and registration ---
main_bp = Blueprint('main', __name__)

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
    if request.method == 'POST':
        task_raw = gs.get_single_task(task_id)
        if not task_raw: abort(404)
        
        action = request.form.get('action')
        update_payload = {}
        notification_to_send = None
        flash_message = "ดำเนินการเรียบร้อย"
        
        history, base_notes_text = utils.parse_tech_report_from_notes(task_raw.get('notes', ''))
        feedback_data = utils.parse_customer_feedback_from_notes(task_raw.get('notes', ''))
        
        new_attachments = json.loads(request.form.get('uploaded_attachments_json', '[]'))

        if action in ['save_report', 'complete_task']:
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
        final_notes = base_notes_text
        if all_reports_text: final_notes += all_reports_text
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
    
    all_attachments = [att for r in p_task['tech_history'] for att in r.get('attachments', [])]
    
    return render_template('update_task_details.html', task=p_task, settings=get_app_settings(), all_attachments=all_attachments)

# Added this route to address the TemplateNotFound error
@main_bp.route("/summary")
def summary():
    """Redirects to the dashboard as a general summary page to fix TemplateNotFound."""
    # This function is added to handle potential requests for a /summary route
    # and redirects them to the /tools/dashboard route, which serves as a comprehensive summary.
    # It passes along any query arguments (e.g., search_query, status_filter)
    return redirect(url_for('tools.dashboard', **request.args))


@main_bp.route('/settings', methods=['GET', 'POST'])
def settings_page():
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
                'hour_thai': int(request.form.get('hour_thai', 2)),
                'minute_thai': int(request.form.get('minute_thai', 0))
            },
            'shop_info': {
                'contact_phone': request.form.get('shop_contact_phone', '').strip(),
                'line_id': request.form.get('shop_line_id', '').strip()
            },
            'technician_list': technician_list
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

# --- Webhook & OAuth Routes (These now use the defined handler) ---
@main_bp.route("/callback", methods=['POST'])
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

@main_bp.route('/authorize')
def authorize():
    client_secrets_json_str = os.environ.get('GOOGLE_CLIENT_SECRETS_JSON')
    if not client_secrets_json_str:
        flash('ไม่สามารถเริ่มการเชื่อมต่อได้: ไม่ได้ตั้งค่า `GOOGLE_CLIENT_SECRETS_JSON`', 'danger')
        return redirect(url_for('main.settings_page'))
    
    flow = Flow.from_client_config(json.loads(client_secrets_json_str), scopes=gs.SCOPES, redirect_uri=url_for('main.oauth2callback', _external=True, _scheme='https'))
    authorization_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true', prompt='consent')
    session['state'] = state
    return redirect(authorization_url)

@main_bp.route('/oauth2callback')
def oauth2callback():
    state = session.get('state')
    if not state or state != request.args.get('state'): abort(401) 
    
    client_secrets_json_str = os.environ.get('GOOGLE_CLIENT_SECRETS_JSON')
    client_config = json.loads(client_secrets_json_str)
    flow = Flow.from_client_config(client_config, scopes=gs.SCOPES, state=state, redirect_uri=url_for('main.oauth2callback', _external=True, _scheme='https'))
    flow.fetch_token(authorization_response=request.url)
    credentials = flow.credentials
    token_json = credentials.to_json()

    app.logger.info("="*80)
    app.logger.info("!!! NEW GOOGLE TOKEN GENERATED SUCCESSFULLY !!!")
    app.logger.info("COPY THE JSON BELOW AND SET IT AS THE 'GOOGLE_TOKEN_JSON' ENVIRONMENT VARIABLE IN RENDER:")
    app.logger.info(token_json)
    app.logger.info("="*80)
    
    os.environ['GOOGLE_TOKEN_JSON'] = token_json
    gs.get_refreshed_credentials(force_refresh=True)
    
    flash('เชื่อมต่อ Google API สำเร็จ! กรุณาคัดลอก Token ใหม่จาก Log และรีสตาร์ทแอป', 'success')
    return redirect(url_for('main.settings_page'))

# --- Register Blueprints ---
# Register blueprints AFTER all their routes have been defined
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

# --- App Startup ---
if __name__ == '__main__':
    with app.app_context():
        gs.load_settings_from_drive_on_startup(save_app_settings)
        initialize_scheduler(app)
    atexit.register(cleanup_scheduler)
    
    port = int(os.environ.get('PORT', 8080))
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() in ['true', '1', 't']
    app.run(host='0.0.0.0', port=port, debug=debug_mode)