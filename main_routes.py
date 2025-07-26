import os
import datetime
import json
import pytz
from io import BytesIO
import re # Added re for parsing map_url
from PIL import Image # For image compression in api_upload_attachment, api_upload_avatar
import mimetypes # For guessing mime type

from flask import (Blueprint, request, render_template, redirect, url_for, abort,
                   session, jsonify, flash, current_app)
from werkzeug.utils import secure_filename

import google_services as gs
import utils
from settings_manager import get_app_settings # Only get_app_settings needed here
from line_notifications import send_update_notification, send_completion_notification, send_new_task_notification # Specific imports for clarity

main_bp = Blueprint('main', __name__)

# --- Constants for file uploads (should be fetched from current_app.config or current_app directly if defined in app.py) ---
# If these are defined in app.py, they will be accessible via current_app
# For robustness in case app.py is not yet defining them in current_app,
# we can define fallback values or ensure app.py sets them.
# Assuming app.py now sets these on current_app for consistency.
# Example: current_app.ALLOWED_EXTENSIONS, current_app.MAX_FILE_SIZE_BYTES

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

# API endpoint for FullCalendar events
@main_bp.route('/api/calendar_tasks')
def api_calendar_tasks():
    tasks = gs.get_google_tasks_for_report(show_completed=True) or []
    events = []
    today_thai = datetime.datetime.now(utils.THAILAND_TZ).date()

    for task in tasks:
        event = {
            'id': task.get('id'),
            'title': task.get('title', 'No Title'),
            'extendedProps': {
                'is_completed': False,
                'is_overdue': False,
                'is_today': False
            }
        }
        
        customer_info = utils.parse_customer_info_from_notes(task.get('notes', ''))
        event['title'] = f"{customer_info.get('name', '')}: {task.get('title', 'No Title')}".strip()

        if task.get('due'):
            try:
                due_dt_utc = utils.date_parse(task['due'])
                due_dt_local = due_dt_utc.astimezone(utils.THAILAND_TZ)
                event['start'] = due_dt_local.isoformat()
                event['allDay'] = (due_dt_local.hour == 0 and due_dt_local.minute == 0 and due_dt_local.second == 0)

                if task.get('status') == 'completed':
                    event['extendedProps']['is_completed'] = True
                    event['color'] = '#198754'
                    event['borderColor'] = '#198754'
                elif due_dt_local.date() < today_thai:
                    event['extendedProps']['is_overdue'] = True
                    event['color'] = '#dc3545'
                    event['borderColor'] = '#dc3545'
                elif due_dt_local.date() == today_thai:
                    event['extendedProps']['is_today'] = True
                    event['color'] = '#0dcaf0'
                    event['borderColor'] = '#0dcaf0'
                else:
                    event['color'] = '#ffc107'
                    event['borderColor'] = '#ffc107'
                
            except (ValueError, TypeError):
                pass
        
        if task.get('status') == 'completed' and task.get('completed') and not event.get('start'):
            try:
                completed_dt_utc = utils.date_parse(task['completed'])
                completed_dt_local = completed_dt_utc.astimezone(utils.THAILAND_TZ)
                event['start'] = completed_dt_local.isoformat()
                event['extendedProps']['is_completed'] = True
                event['color'] = '#198754'
                event['borderColor'] = '#198754'
            except (ValueError, TypeError):
                pass
        
        if event.get('start'):
            events.append(event)

    return jsonify(events)


@main_bp.route('/api/task/schedule_from_calendar', methods=['POST'])
def schedule_from_calendar():
    data = request.json
    task_id = data.get('task_id')
    new_due_date_str = data.get('new_due_date')

    if not task_id or not new_due_date_str:
        return jsonify({'status': 'error', 'message': 'Missing task_id or new_due_date'}), 400

    try:
        dt_utc = utils.date_parse(new_due_date_str)
        new_due_date_gmt = dt_utc.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
    except ValueError:
        return jsonify({'status': 'error', 'message': 'Invalid date format'}), 400

    task = gs.get_single_task(task_id)
    if not task:
        return jsonify({'status': 'error', 'message': 'Task not found'}), 404

    updated_task = gs.update_google_task(task_id=task_id, due=new_due_date_gmt)
    
    if updated_task:
        current_app.cache.clear()
        return jsonify({'status': 'success', 'message': 'Task due date updated successfully'})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to update task due date'}), 500


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
                dt_local = utils.THAILAND_TZ.localize(utils.date_parse(appointment_str))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
            except ValueError:
                flash('รูปแบบวันเวลานัดหมายไม่ถูกต้อง', 'warning')
        
        new_task = gs.create_google_task(task_title, notes=notes, due=due_date_gmt)
        if new_task:
            current_app.cache.clear()
            send_new_task_notification(new_task)
            flash('สร้างงานใหม่เรียบร้อยแล้ว!', 'success')
            return redirect(url_for('main.task_details', task_id=new_task['id']))
        else:
            flash('เกิดข้อผิดพลาดในการสร้างงาน', 'danger')
    
    return render_template('form.html',
                           task_detail_snippets=utils.TEXT_SNIPPETS.get('task_details', [])
                           )


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
            
            dt_local = utils.THAILAND_TZ.localize(utils.date_parse(reschedule_due_str))
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
            current_app.cache.clear()
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
    
    all_attachments = []
    for report_item in p_task['tech_history']:
        if report_item.get('attachments'):
            for att in report_item['attachments']:
                all_attachments.append(att)

    return render_template('update_task_details.html',
                           task=p_task,
                           settings=get_app_settings(),
                           all_attachments=all_attachments,
                           progress_report_snippets=utils.TEXT_SNIPPETS.get('progress_reports', [])
                           )

# Added this route to address the TemplateNotFound error (as a summary view)
@main_bp.route("/summary")
def summary():
    """Redirects to the dashboard as a general summary page to fix TemplateNotFound."""
    return redirect(url_for('tools.dashboard', **request.args))

# Added the edit_task route
@main_bp.route('/edit_task/<task_id>', methods=['GET', 'POST'])
def edit_task(task_id):
    task_raw = gs.get_single_task(task_id)
    if not task_raw:
        abort(404)

    if request.method == 'POST':
        task_title = str(request.form.get('task_title', '')).strip()
        organization_name = str(request.form.get('organization_name', '')).strip()
        customer_name = str(request.form.get('customer_name', '')).strip()
        customer_phone = str(request.form.get('customer_phone', '')).strip()
        address = str(request.form.get('address', '')).strip()
        latitude_longitude = str(request.form.get('latitude_longitude', '')).strip()
        appointment_due_str = str(request.form.get('appointment_due', '')).strip()

        if not task_title or not customer_name:
            flash('กรุณากรอกชื่อผู้ติดต่อและรายละเอียดงาน', 'danger')
            return redirect(url_for('main.edit_task', task_id=task_id))

        existing_notes = task_raw.get('notes', '')
        history_reports, _ = utils.parse_tech_report_from_notes(existing_notes)
        feedback_data = utils.parse_customer_feedback_from_notes(existing_notes)

        new_base_notes_lines = [
            f"หน่วยงาน: {organization_name}",
            f"ลูกค้า: {customer_name}",
            f"เบอร์โทรศัพท์: {customer_phone}",
            f"ที่อยู่: {address}",
            f"พิกัด: {latitude_longitude}"
        ]
        new_base_notes = "\n".join(filter(None, new_base_notes_lines))

        final_notes = new_base_notes
        if history_reports:
            all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history_reports])
            final_notes += all_reports_text
        if feedback_data:
            final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

        due_date_gmt = None
        if appointment_due_str:
            try:
                dt_local = utils.THAILAND_TZ.localize(utils.date_parse(appointment_due_str))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
            except ValueError:
                flash('รูปแบบวันเวลานัดหมายไม่ถูกต้อง', 'warning')
                return redirect(url_for('main.edit_task', task_id=task_id))

        update_payload = {
            'title': task_title,
            'notes': final_notes,
            'due': due_date_gmt
        }

        updated_task = gs.update_google_task(task_id, **update_payload)
        if updated_task:
            current_app.cache.clear()
            flash('แก้ไขข้อมูลงานเรียบร้อยแล้ว!', 'success')
            return redirect(url_for('main.task_details', task_id=task_id))
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกการแก้ไขงาน', 'danger')
            return redirect(url_for('main.edit_task', task_id=task_id))

    p_task = utils.parse_google_task_dates(task_raw)
p_task['customer'] = utils.parse_customer_info_from_notes(task_raw.get('notes', ''))
    
    return render_template('edit_task.html', task=p_task)

# Delete Task Route
@main_bp.route('/delete_task/<task_id>', methods=['POST'])
def delete_task(task_id):
    if gs.delete_google_task(task_id):
        flash('ลบงานเรียบร้อยแล้ว!', 'success')
        current_app.cache.clear()
    else:
        flash('เกิดข้อผิดพลาดในการลบงาน', 'danger')
    return redirect(url_for('tools.dashboard')) # Redirect to dashboard after deletion


# API for file uploads (attachments for tasks, etc.)
@main_bp.route('/api/upload_attachment', methods=['POST'])
def api_upload_attachment():
    # from werkzeug.utils import secure_filename # Already imported by app.py
    # from PIL import Image # Already imported by app.py
    # import mimetypes # Already imported by app.py

    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file'}), 400
    
    task_id = request.form.get('task_id')
    if not task_id:
        return jsonify({'status': 'error', 'message': 'Task ID is missing'}), 400

    file.seek(0, os.SEEK_END)
    file_length = file.tell()
    file.seek(0)
    
    # Check and potentially compress image if too large
    if file_length > current_app.MAX_FILE_SIZE_BYTES: # Use current_app.MAX_FILE_SIZE_BYTES
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
                current_app.logger.info(f"Compressed image '{file.filename}' successfully.")
            except Exception as e:
                current_app.logger.error(f"Could not compress image '{file.filename}': {e}")
                return jsonify({'status': 'error', 'message': f'ไฟล์รูปภาพใหญ่เกินไปและไม่สามารถบีบอัดได้'}), 413
        else:
            return jsonify({'status': 'error', 'message': f'ไฟล์ใหญ่เกินขนาดที่กำหนด ({current_app.MAX_FILE_SIZE_MB}MB)'}), 413 # Use current_app.MAX_FILE_SIZE_MB
    else:
        file_to_upload = file
        filename = secure_filename(file.filename)
        mime_type = file.mimetype or mimetypes.guess_type(filename)[0]

    attachments_base_folder_id = gs.find_or_create_drive_folder("Task_Attachments", gs.GOOGLE_DRIVE_FOLDER_ID)
    if not attachments_base_folder_id:
        return jsonify({'status': 'error', 'message': 'Could not create or find base Task_Attachments folder'}), 500

    final_upload_folder_id = None
    target_date = datetime.datetime.now(utils.THAILAND_TZ) # Use local timezone for folder naming

    # Determine the target folder based on task_id or create a temporary one for new tasks
    if task_id == 'new_task_placeholder':
        monthly_folder_name = target_date.strftime('%Y-%m')
        monthly_folder_id = gs.find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)
        temp_upload_folder_name = f"New_Uploads_{target_date.strftime('%Y-%m-%d_%H%M%S')}" # Add timestamp for uniqueness
        final_upload_folder_id = gs.find_or_create_drive_folder(temp_upload_folder_name, monthly_folder_id)
    else:
        task_raw = gs.get_single_task(task_id)
        if not task_raw:
            return jsonify({'status': 'error', 'message': 'Task not found'}), 404
        
        # Use task creation date for consistent monthly folder
        if task_raw.get('created'):
            try:
                target_date = utils.date_parse(task_raw.get('created')).astimezone(utils.THAILAND_TZ)
            except (ValueError, TypeError):
                pass # Use current date if parsing fails
        
        monthly_folder_name = target_date.strftime('%Y-%m')
        monthly_folder_id = gs.find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)
        if not monthly_folder_id:
            return jsonify({'status': 'error', 'message': f'Could not create or find monthly folder: {monthly_folder_name}'}), 500
        
        customer_info = utils.parse_customer_info_from_notes(task_raw.get('notes', ''))
        sanitized_customer_name = utils.sanitize_filename(customer_info.get('name', 'Unknown_Customer'))
        customer_task_folder_name = f"{sanitized_customer_name} - {task_id}"
        final_upload_folder_id = gs.find_or_create_drive_folder(customer_task_folder_name, monthly_folder_id)
    
    if not final_upload_folder_id:
        return jsonify({'status': 'error', 'message': 'Could not determine final upload folder'}), 500

    media_body = gs.MediaIoBaseUpload(file_to_upload, mimetype=mime_type, resumable=True)
    drive_file = gs._perform_drive_upload(media_body, filename, mime_type, final_upload_folder_id)
    
    if drive_file:
        return jsonify({'status': 'success', 'file_info': {'id': drive_file.get('id'), 'url': drive_file.get('webViewLink'), 'name': filename}})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to upload to Google Drive'}), 500

# API for technician avatar uploads
@main_bp.route('/api/upload_avatar', methods=['POST'])
def api_upload_avatar():
    from werkzeug.utils import secure_filename
    from PIL import Image
    import mimetypes

    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file'}), 400

    file.seek(0, os.SEEK_END)
    file_length = file.tell()
    file.seek(0)

    # Compress image if too large, similar to attachment upload
    if file_length > current_app.MAX_FILE_SIZE_BYTES:
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
                current_app.logger.info(f"Compressed avatar '{file.filename}' successfully.")
            except Exception as e:
                current_app.logger.error(f"Could not compress avatar '{file.filename}': {e}")
                return jsonify({'status': 'error', 'message': f'ไฟล์รูปภาพใหญ่เกินไปและไม่สามารถบีบอัดได้'}), 413
        else:
            return jsonify({'status': 'error', 'message': f'ไฟล์ใหญ่เกินขนาดที่กำหนด ({current_app.MAX_FILE_SIZE_MB}MB)'}), 413
    else:
        file_to_upload = file
        filename = secure_filename(file.filename)
        mime_type = file.mimetype or mimetypes.guess_type(filename)[0]

    avatars_folder_id = gs.find_or_create_drive_folder("Technician_Avatars", gs.GOOGLE_DRIVE_FOLDER_ID)
    if not avatars_folder_id:
        return jsonify({'status': 'error', 'message': 'Could not create or find Technician_Avatars folder'}), 500

    media_body = gs.MediaIoBaseUpload(file_to_upload, mimetype=mime_type, resumable=True)
    drive_file = gs._perform_drive_upload(media_body, filename, mime_type, avatars_folder_id)

    if drive_file:
        return jsonify({'status': 'success', 'file_id': drive_file.get('id'), 'url': drive_file.get('webViewLink')})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to upload avatar to Google Drive'}), 500


@main_bp.route('/api/task/<task_id>/edit_report_text/<int:report_index>', methods=['POST'])
def api_edit_report_text(task_id, report_index):
    data = request.json
    new_summary = data.get('summary', '').strip()
    
    if not new_summary:
        return jsonify({'status': 'error', 'message': 'กรุณากรอกสรุปงาน'}), 400

    task_raw = gs.get_single_task(task_id)
    if not task_raw:
        return jsonify({'status': 'error', 'message': 'ไม่พบงานที่ต้องการอัปเดต'}), 404

    history, base_notes_text = utils.parse_tech_report_from_notes(task_raw.get('notes', ''))
    feedback_data = utils.parse_customer_feedback_from_notes(task_raw.get('notes', ''))
    
    if not (0 <= report_index < len(history)):
        return jsonify({'status': 'error', 'message': 'ไม่พบรายงานที่ต้องการแก้ไข'}), 404

    history[report_index]['work_summary'] = new_summary
    
    all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
    final_notes = base_notes_text
    if all_reports_text: final_notes += all_reports_text
    if feedback_data: final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
    
    if gs.update_google_task(task_id, notes=final_notes):
        current_app.cache.clear()
        return jsonify({'status': 'success', 'message': 'แก้ไขรายงานเรียบร้อยแล้ว'})
    else:
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกการแก้ไข'}), 500


@main_bp.route('/task/<task_id>/edit_report/<int:report_index>', methods=['POST'])
def edit_report_attachments(task_id, report_index):
    # from werkzeug.utils import secure_filename # Already imported in function
    # from PIL import Image # Already imported in function
    # import mimetypes # Already imported in function

    task_raw = gs.get_single_task(task_id)
    if not task_raw:
        flash('ไม่พบงานที่ต้องการอัปเดต', 'danger')
        abort(404)

    history, base_notes_text = utils.parse_tech_report_from_notes(task_raw.get('notes', ''))
    feedback_data = utils.parse_customer_feedback_from_notes(task_raw.get('notes', ''))
    
    if not (0 <= report_index < len(history)):
        flash('ไม่พบรายงานที่ต้องการแก้ไข', 'danger')
        return redirect(url_for('main.task_details', task_id=task_id))

    report_to_edit = history[report_index]
    
    attachments_to_keep_ids = request.form.getlist('attachments_to_keep')
    original_attachments = report_to_edit.get('attachments', [])
    updated_attachments = []
    
    drive_service = gs.get_google_drive_service()
    if drive_service:
        for att in original_attachments:
            if att['id'] in attachments_to_keep_ids:
                updated_attachments.append(att)
            else:
                try:
                    gs._execute_google_api_call_with_retry(drive_service.files().delete, fileId=att['id'])
                    current_app.logger.info(f"Deleted attachment {att['id']} from Drive.")
                except gs.HttpError as e:
                    current_app.logger.error(f"Failed to delete attachment {att['id']} from Drive during report deletion: {e}")
    else:
        updated_attachments = original_attachments
        flash('ไม่สามารถเชื่อมต่อ Google Drive เพื่อลบไฟล์ได้', 'warning')
    
    new_files = request.files.getlist('new_files[]')
    if new_files:
        if task_raw.get('created'):
            created_dt_local = utils.date_parse(task_raw.get('created')).astimezone(utils.THAILAND_TZ)
            monthly_folder_name = created_dt_local.strftime('%Y-%m')
        else:
            monthly_folder_name = "Uncategorized"

        attachments_base_folder_id = gs.find_or_create_drive_folder("Task_Attachments", gs.GOOGLE_DRIVE_FOLDER_ID)
        monthly_folder_id = gs.find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)
        customer_info = utils.parse_customer_info_from_notes(base_notes_text)
        sanitized_customer_name = utils.sanitize_filename(customer_info.get('name', 'Unknown_Customer'))
        customer_task_folder_name = f"{sanitized_customer_name} - {task_id}"
        final_upload_folder_id = gs.find_or_create_drive_folder(customer_task_folder_name, monthly_folder_id)
        
        if final_upload_folder_id:
            for file in new_files:
                if file and utils.allowed_file(file.filename):
                    file.seek(0, os.SEEK_END)
                    file_length = file.tell()
                    file.seek(0)
                    if file_length > current_app.MAX_FILE_SIZE_BYTES and file.mimetype and file.mimetype.startswith('image/'):
                        try:
                            img = Image.open(file)
                            if img.mode in ("RGBA", "P"): img = img.convert("RGB")
                            output_buffer = BytesIO()
                            img.save(output_buffer, format='JPEG', quality=85, optimize=True)
                            output_buffer.seek(0)
                            file_to_upload = output_buffer
                            filename = os.path.splitext(secure_filename(file.filename))[0] + '.jpg'
                            mime_type = 'image/jpeg'
                        except Exception as e:
                            current_app.logger.error(f"Could not compress image in edit_report: {e}")
                            continue
                    else:
                        file_to_upload = file
                        filename = secure_filename(file.filename)
                        mime_type = file.mimetype or mimetypes.guess_type(filename)[0]

                    media_body = gs.MediaIoBaseUpload(file_to_upload, mimetype=mime_type, resumable=True)
                    drive_file = gs._perform_drive_upload(media_body, filename, mime_type, final_upload_folder_id)
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
    
    if gs.update_google_task(task_id, notes=final_notes):
        current_app.cache.clear()
        flash('แก้ไขรูปภาพในรายงานเรียบร้อยแล้ว!', 'success')
    else:
        flash('เกิดข้อผิดพลาดในการบันทึกการเปลี่ยนแปลงรูปภาพ', 'danger')

    return redirect(url_for('main.task_details', task_id=task_id))

@main_bp.route('/api/task/<task_id>/delete_report/<int:report_index>', methods=['POST'])
def delete_task_report(task_id, report_index):
    task_raw = gs.get_single_task(task_id)
    if not task_raw:
        return jsonify({'status': 'error', 'message': 'ไม่พบงานที่ต้องการอัปเดต'}), 404

    history, base_notes_text = utils.parse_tech_report_from_notes(task_raw.get('notes', ''))
    feedback_data = utils.parse_customer_feedback_from_notes(task_raw.get('notes', ''))
    
    if not (0 <= report_index < len(history)):
        return jsonify({'status': 'error', 'message': 'ไม่พบรายงานที่ต้องการลบ'}), 404

    report_to_delete = history[report_index]
    if report_to_delete.get('attachments'):
        drive_service = gs.get_google_drive_service()
        if drive_service:
            for att in report_to_delete['attachments']:
                try:
                    gs._execute_google_api_call_with_retry(drive_service.files().delete, fileId=att['id'])
                    current_app.logger.info(f"Deleted attachment {att['id']} from Drive while deleting report.")
                except gs.HttpError as e:
                    current_app.logger.error(f"Failed to delete attachment {att['id']} from Drive during report deletion: {e}")

    history.pop(report_index)

    all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
    final_notes = base_notes_text
    if all_reports_text: final_notes += all_reports_text
    if feedback_data: final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
    
    if gs.update_google_task(task_id, notes=final_notes):
        current_app.cache.clear()
        return jsonify({'status': 'success', 'message': 'ลบรายงานเรียบร้อยแล้ว'})
    else:
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกหลังลบรายงาน'}), 500


@main_bp.route('/edit_task/<task_id>', methods=['GET', 'POST'])
def edit_task(task_id):
    task_raw = gs.get_single_task(task_id)
    if not task_raw:
        abort(404)

    if request.method == 'POST':
        task_title = str(request.form.get('task_title', '')).strip()
        organization_name = str(request.form.get('organization_name', '')).strip()
        customer_name = str(request.form.get('customer_name', '')).strip()
        customer_phone = str(request.form.get('customer_phone', '')).strip()
        address = str(request.form.get('address', '')).strip()
        latitude_longitude = str(request.form.get('latitude_longitude', '')).strip()
        appointment_due_str = str(request.form.get('appointment_due', '')).strip()

        if not task_title or not customer_name:
            flash('กรุณากรอกชื่อผู้ติดต่อและรายละเอียดงาน', 'danger')
            return redirect(url_for('main.edit_task', task_id=task_id))

        existing_notes = task_raw.get('notes', '')
        history_reports, _ = utils.parse_tech_report_from_notes(existing_notes)
        feedback_data = utils.parse_customer_feedback_from_notes(existing_notes)

        new_base_notes_lines = [
            f"หน่วยงาน: {organization_name}",
            f"ลูกค้า: {customer_name}",
            f"เบอร์โทรศัพท์: {customer_phone}",
            f"ที่อยู่: {address}",
            f"พิกัด: {latitude_longitude}"
        ]
        new_base_notes = "\n".join(filter(None, new_base_notes_lines))

        final_notes = new_base_notes
        if history_reports:
            all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history_reports])
            final_notes += all_reports_text
        if feedback_data:
            final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

        due_date_gmt = None
        if appointment_due_str:
            try:
                dt_local = utils.THAILAND_TZ.localize(utils.date_parse(appointment_due_str))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
            except ValueError:
                flash('รูปแบบวันเวลานัดหมายไม่ถูกต้อง', 'warning')
                return redirect(url_for('main.edit_task', task_id=task_id))

        update_payload = {
            'title': task_title,
            'notes': final_notes,
            'due': due_date_gmt
        }

        updated_task = gs.update_google_task(task_id, **update_payload)
        if updated_task:
            current_app.cache.clear()
            flash('แก้ไขข้อมูลงานเรียบร้อยแล้ว!', 'success')
            return redirect(url_for('main.task_details', task_id=task_id))
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกการแก้ไขงาน', 'danger')
            return redirect(url_for('main.edit_task', task_id=task_id))

    p_task = utils.parse_google_task_dates(task_raw)
p_task['customer'] = utils.parse_customer_info_from_notes(task_raw.get('notes', ''))
    
    return render_template('edit_task.html', task=p_task)


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
                'hour_thai': int(request.form.get('auto_backup_hour', 2)), # Consistent with form name
                'minute_thai': int(request.form.get('auto_backup_minute', 0)) # Consistent with form name
            },
            'shop_info': {
                'contact_phone': request.form.get('shop_contact_phone', '').strip(),
                'line_id': request.form.get('shop_line_id', '').strip()
            },
            'technician_list': technician_list
        }
        if save_app_settings(settings_data):
            initialize_scheduler(app)
            current_app.cache.clear()
            if gs.backup_settings_to_drive(get_app_settings()):
                flash('บันทึกและสำรองการตั้งค่าเรียบร้อยแล้ว!', 'success')
            else:
                flash('บันทึกสำเร็จ แต่สำรองไป Drive ไม่สำเร็จ!', 'warning')
        else:
            flash('เกิดข้อผิดพลาดในการบันทึก!', 'danger')
        return redirect(url_for('main.settings_page'))
    
    current_settings = get_app_settings()
    current_app.logger.info(f"Loading settings page. Technician list: {current_settings.get('technician_list')}") # Log to check technician_list
    return render_template('settings_page.html', settings=current_settings)

# --- Webhook & OAuth Routes ---
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
    with current_app.app_context(): handle_text_message(event)

@handler.add(PostbackEvent)
def postback_handler(event):
    with current_app.app_context(): handle_postback(event)

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

    current_app.logger.info("="*80)
    current_app.logger.info("!!! NEW GOOGLE TOKEN GENERATED SUCCESSFULLY !!!")
    current_app.logger.info("COPY THE JSON BELOW AND SET IT AS THE 'GOOGLE_TOKEN_JSON' ENVIRONMENT VARIABLE IN RENDER:")
    current_app.logger.info(token_json)
    current_app.logger.info("="*80)
    
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
    current_app.logger.error(f"Server Error: {e}", exc_info=True)
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