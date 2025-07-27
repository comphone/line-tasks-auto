import os
import datetime
import json
import pytz

from flask import (Blueprint, request, render_template, redirect, url_for, abort,
                   session, jsonify, flash, current_app)
from google_auth_oauthlib.flow import Flow

import google_services as gs
import utils
from settings_manager import get_app_settings, save_app_settings
from line_notifications import send_update_notification, send_completion_notification, send_new_task_notification
from app_scheduler import initialize_scheduler

main_bp = Blueprint('main', __name__)

@main_bp.route("/")
def root_redirect():
    return redirect(url_for('tools.dashboard'))

@main_bp.route('/calendar')
def calendar_page():
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
        task_title = request.form.get('task_title', '').strip()
        customer_name = request.form.get('customer', '').strip()
        if not task_title or not customer_name:
            flash('กรุณากรอกชื่อผู้ติดต่อและรายละเอียดงาน', 'danger')
            return redirect(url_for('main.form_page'))

        base_notes = utils.build_notes_string(
            base_info={
                'organization': request.form.get('organization_name', '').strip(),
                'name': customer_name,
                'phone': request.form.get('phone', '').strip(),
                'address': request.form.get('address', '').strip(),
                'map_url': request.form.get('latitude_longitude', '').strip()
            },
            history=[], 
            feedback={}
        )

        due_date_gmt = None
        if request.form.get('appointment'):
            try:
                dt_local = utils.THAILAND_TZ.localize(utils.date_parse(request.form.get('appointment')))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
            except ValueError:
                flash('รูปแบบวันเวลานัดหมายไม่ถูกต้อง', 'warning')

        new_task = gs.create_google_task(task_title, notes=base_notes, due=due_date_gmt)
        if new_task:
            current_app.cache.clear()
            send_new_task_notification(new_task)
            flash('สร้างงานใหม่เรียบร้อยแล้ว!', 'success')
            return redirect(url_for('main.task_details', task_id=new_task['id']))
        else:
            flash('เกิดข้อผิดพลาดในการสร้างงาน', 'danger')
    
    return render_template('form.html', task_detail_snippets=utils.TEXT_SNIPPETS.get('task_details', []))

@main_bp.route('/task/<task_id>', methods=['GET', 'POST'])
def task_details(task_id):
    if request.method == 'POST':
        task_raw = gs.get_single_task(task_id)
        if not task_raw: return jsonify({'status': 'error', 'message': 'Task not found'}), 404
        
        action = request.form.get('action')
        update_payload = {}
        
        history, base_notes, feedback = utils.get_notes_parts(task_raw.get('notes', ''))
        
        uploaded_attachments = json.loads(request.form.get('uploaded_attachments_json', '[]'))
        
        if action == 'complete_task':
            work_summary = request.form.get('work_summary', '').strip()
            technicians = [t.strip() for t in request.form.get('technicians_report', '').split(',') if t]
            if not work_summary: return jsonify({'status': 'error', 'message': 'กรุณากรอกสรุปงาน'}), 400
            if not technicians: return jsonify({'status': 'error', 'message': 'กรุณาเลือกช่าง'}), 400
            
            history.append({'type': 'report', 'summary_date': datetime.datetime.now(utils.THAILAND_TZ).isoformat(), 'work_summary': work_summary, 'attachments': uploaded_attachments, 'technicians': technicians})
            update_payload['status'] = 'completed'
            send_completion_notification(task_raw, technicians)
            flash_message = 'ปิดงานเรียบร้อยแล้ว!'

        elif action == 'save_report':
            work_summary = request.form.get('work_summary', '').strip()
            technicians = [t.strip() for t in request.form.get('technicians_report', '').split(',') if t]
            if not (work_summary or uploaded_attachments): return jsonify({'status': 'error', 'message': 'กรุณากรอกสรุปงาน หรือแนบไฟล์'}), 400
            if not technicians: return jsonify({'status': 'error', 'message': 'กรุณาเลือกช่าง'}), 400

            history.append({'type': 'report', 'summary_date': datetime.datetime.now(utils.THAILAND_TZ).isoformat(), 'work_summary': work_summary, 'attachments': uploaded_attachments, 'technicians': technicians})
            flash_message = 'เพิ่มรายงานเรียบร้อยแล้ว!'

        elif action == 'reschedule_task':
            reschedule_due = request.form.get('reschedule_due', '').strip()
            reason = request.form.get('reschedule_reason', '').strip()
            technicians = [t.strip() for t in request.form.get('technicians_reschedule', '').split(',') if t]
            if not reschedule_due: return jsonify({'status': 'error', 'message': 'กรุณากำหนดวันนัดหมายใหม่'}), 400
            
            dt_local = utils.THAILAND_TZ.localize(utils.date_parse(reschedule_due))
            update_payload['due'] = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
            update_payload['status'] = 'needsAction'
            
            history.append({'type': 'reschedule', 'summary_date': datetime.datetime.now(utils.THAILAND_TZ).isoformat(), 'reason': reason, 'new_due_date': dt_local.strftime("%d/%m/%y %H:%M"), 'technicians': technicians})
            send_update_notification(task_raw, dt_local.strftime("%d/%m/%y %H:%M"), reason, technicians, dt_local.date() == datetime.date.today())
            flash_message = 'เลื่อนนัดเรียบร้อยแล้ว'
        
        else:
            return jsonify({'status': 'error', 'message': 'Invalid action'}), 400
            
        update_payload['notes'] = utils.build_notes_string(base_notes, history, feedback)
        
        if gs.update_google_task(task_id, **update_payload):
            current_app.cache.clear()
            return jsonify({'status': 'success', 'message': flash_message})
        return jsonify({'status': 'error', 'message': 'Failed to update task'}), 500

    # GET request
    task_raw = gs.get_single_task(task_id)
    if not task_raw: abort(404)
    
    p_task = utils.parse_google_task_dates(task_raw)
    p_task['customer'] = utils.parse_customer_info_from_notes(p_task.get('notes', ''))
    p_task['tech_reports_history'], _, _ = utils.get_notes_parts(p_task.get('notes', ''))
    
    all_attachments = [att for report in p_task['tech_reports_history'] for att in report.get('attachments', [])]

    return render_template('update_task_details.html',
                           task=p_task,
                           all_attachments=all_attachments,
                           technician_list=get_app_settings().get('technician_list', []),
                           progress_report_snippets=utils.TEXT_SNIPPETS.get('progress_reports', []))

@main_bp.route('/edit_task/<task_id>', methods=['GET', 'POST'])
def edit_task(task_id):
    task_raw = gs.get_single_task(task_id)
    if not task_raw: abort(404)

    if request.method == 'POST':
        _, history, feedback = utils.get_notes_parts(task_raw.get('notes', ''))
        
        base_info = {
            'organization': request.form.get('organization_name', '').strip(),
            'name': request.form.get('customer_name', '').strip(),
            'phone': request.form.get('customer_phone', '').strip(),
            'address': request.form.get('address', '').strip(),
            'map_url': request.form.get('latitude_longitude', '').strip()
        }
        
        due_gmt = None
        if request.form.get('appointment_due'):
            try:
                dt_local = utils.THAILAND_TZ.localize(utils.date_parse(request.form.get('appointment_due')))
                due_gmt = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
            except ValueError:
                flash('รูปแบบวันเวลานัดหมายไม่ถูกต้อง', 'warning')

        update_payload = {
            'title': request.form.get('task_title', '').strip(),
            'notes': utils.build_notes_string(base_info, history, feedback),
            'due': due_gmt
        }
        
        if gs.update_google_task(task_id, **update_payload):
            current_app.cache.clear()
            flash('แก้ไขข้อมูลงานเรียบร้อยแล้ว!', 'success')
            return redirect(url_for('main.task_details', task_id=task_id))
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกการแก้ไข', 'danger')

    p_task = utils.parse_google_task_dates(task_raw)
    p_task['customer'] = utils.parse_customer_info_from_notes(task_raw.get('notes', ''))
    return render_template('edit_task.html', task=p_task)

@main_bp.route('/delete_task/<task_id>', methods=['POST'])
def delete_task(task_id):
    if gs.delete_google_task(task_id):
        flash('ลบงานเรียบร้อยแล้ว!', 'success')
        current_app.cache.clear()
    else:
        flash('เกิดข้อผิดพลาดในการลบงาน', 'danger')
    return redirect(url_for('tools.dashboard'))

@main_bp.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
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
            'shop_info': {
                'contact_phone': request.form.get('shop_contact_phone', '').strip(),
                'line_id': request.form.get('shop_line_id', '').strip()
            },
            'technician_list': json.loads(request.form.get('technician_list_json', '[]'))
        }
        if save_app_settings(settings_data):
            initialize_scheduler(current_app)
            current_app.cache.clear()
            gs.backup_settings_to_drive(get_app_settings())
            flash('บันทึกและสำรองการตั้งค่าเรียบร้อยแล้ว!', 'success')
        else:
            flash('เกิดข้อผิดพลาดในการบันทึก!', 'danger')
        return redirect(url_for('main.settings_page'))
    
    return render_template('settings_page.html', settings=get_app_settings())

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
    
    flow = Flow.from_client_config(json.loads(os.environ.get('GOOGLE_CLIENT_SECRETS_JSON')), scopes=gs.SCOPES, state=state, redirect_uri=url_for('main.oauth2callback', _external=True, _scheme='https'))
    flow.fetch_token(authorization_response=request.url)
    
    os.environ['GOOGLE_TOKEN_JSON'] = flow.credentials.to_json()
    gs.get_refreshed_credentials(force_refresh=True)
    
    flash('เชื่อมต่อ Google API สำเร็จ! กรุณาคัดลอก Token ใหม่จาก Log และรีสตาร์ทแอป', 'success')
    return redirect(url_for('main.settings_page'))