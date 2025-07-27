import os
import datetime
import json
import pytz
from io import BytesIO
from PIL import Image
import mimetypes

from flask import Blueprint, request, jsonify, current_app
from werkzeug.utils import secure_filename
from googleapiclient.http import MediaIoBaseUpload

import google_services as gs
import utils

api_bp = Blueprint('api', __name__, url_prefix='/api')

# --- Constants ---
MAX_FILE_SIZE_MB = 50
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')


# --- Helper Function for File Upload ---
def _handle_file_upload(file, task_id, is_avatar=False):
    """Handles file validation, compression, and uploading to a structured folder in Drive."""
    if not file or file.filename == '':
        return {'status': 'error', 'message': 'No file selected'}, 400

    file.seek(0, os.SEEK_END)
    file_length = file.tell()
    file.seek(0)
    
    filename = secure_filename(file.filename)
    mime_type = file.mimetype or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    file_to_upload = file

    # Image compression for large files
    if file_length > MAX_FILE_SIZE_BYTES:
        if mime_type.startswith('image/'):
            try:
                img = Image.open(file)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                
                output_buffer = BytesIO()
                img.save(output_buffer, format='JPEG', quality=85, optimize=True)
                output_buffer.seek(0)
                file_to_upload = output_buffer
                filename = os.path.splitext(filename)[0] + '.jpg'
                mime_type = 'image/jpeg'
                current_app.logger.info(f"Compressed image '{file.filename}' successfully.")
            except Exception as e:
                current_app.logger.error(f"Could not compress image '{file.filename}': {e}")
                return {'status': 'error', 'message': 'Image is too large and compression failed'}, 413
        else:
            return {'status': 'error', 'message': f'File size exceeds the {MAX_FILE_SIZE_MB}MB limit'}, 413
    
    # Determine upload folder
    if is_avatar:
        upload_folder_id = gs.find_or_create_drive_folder("Technician_Avatars", GOOGLE_DRIVE_FOLDER_ID)
    else:
        attachments_base_folder_id = gs.find_or_create_drive_folder("Task_Attachments", GOOGLE_DRIVE_FOLDER_ID)
        if not attachments_base_folder_id:
            return {'status': 'error', 'message': 'Could not access base attachments folder'}, 500
        
        task_raw = gs.get_single_task(task_id) if task_id != 'new_task_placeholder' else None
        target_date = datetime.datetime.now(utils.THAILAND_TZ)
        if task_raw and task_raw.get('created'):
            try:
                target_date = utils.date_parse(task_raw['created']).astimezone(utils.THAILAND_TZ)
            except (ValueError, TypeError): pass

        monthly_folder_name = target_date.strftime('%Y-%m')
        monthly_folder_id = gs.find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)

        customer_name = "Unknown_Customer"
        if task_raw:
             customer_name = utils.parse_customer_info_from_notes(task_raw.get('notes', '')).get('name', customer_name)
        
        task_folder_name = f"{utils.sanitize_filename(customer_name)} - {task_id}"
        upload_folder_id = gs.find_or_create_drive_folder(task_folder_name, monthly_folder_id)

    if not upload_folder_id:
         return {'status': 'error', 'message': 'Could not determine the final upload folder'}, 500

    # Perform upload
    media_body = MediaIoBaseUpload(file_to_upload, mimetype=mime_type, resumable=True)
    drive_file = gs._perform_drive_upload(media_body, filename, mime_type, upload_folder_id)

    if drive_file:
        return {
            'status': 'success',
            'file_info': {'id': drive_file.get('id'), 'url': drive_file.get('webViewLink'), 'name': filename}
        }, 200
    else:
        return {'status': 'error', 'message': 'Failed to upload to Google Drive'}, 500

# --- API Routes ---

@api_bp.route('/upload_attachment', methods=['POST'])
def api_upload_attachment():
    file = request.files.get('file')
    task_id = request.form.get('task_id', 'new_task_placeholder')
    result, status_code = _handle_file_upload(file, task_id, is_avatar=False)
    return jsonify(result), status_code

@api_bp.route('/upload_avatar', methods=['POST'])
def api_upload_avatar():
    file = request.files.get('file')
    # Avatar is not tied to a specific task, so task_id is a placeholder
    result, status_code = _handle_file_upload(file, task_id="avatar", is_avatar=True)
    return jsonify(result), status_code
    
@api_bp.route('/task/<task_id>/edit_report_text/<int:report_index>', methods=['POST'])
def api_edit_report_text(task_id, report_index):
    new_summary = request.json.get('summary', '').strip()
    if not new_summary:
        return jsonify({'status': 'error', 'message': 'Summary text cannot be empty'}), 400
    
    task_raw = gs.get_single_task(task_id)
    if not task_raw: return jsonify({'status': 'error', 'message': 'Task not found'}), 404

    history, base_notes, feedback = utils.get_notes_parts(task_raw.get('notes', ''))
    if not (0 <= report_index < len(history)):
        return jsonify({'status': 'error', 'message': 'Report index out of bounds'}), 404

    history[report_index]['work_summary'] = new_summary
    new_notes = utils.build_notes_string(base_notes, history, feedback)
    
    if gs.update_google_task(task_id, notes=new_notes):
        current_app.cache.clear()
        return jsonify({'status': 'success', 'message': 'Report updated successfully'})
    return jsonify({'status': 'error', 'message': 'Failed to save updated notes'}), 500

@api_bp.route('/task/<task_id>/delete_report/<int:report_index>', methods=['POST'])
def api_delete_report(task_id, report_index):
    task_raw = gs.get_single_task(task_id)
    if not task_raw: return jsonify({'status': 'error', 'message': 'Task not found'}), 404

    history, base_notes, feedback = utils.get_notes_parts(task_raw.get('notes', ''))
    if not (0 <= report_index < len(history)):
        return jsonify({'status': 'error', 'message': 'Report index out of bounds'}), 404

    # Delete attachments from Drive
    report_to_delete = history.pop(report_index)
    if report_to_delete.get('attachments'):
        drive_service = gs.get_google_drive_service()
        for att in report_to_delete['attachments']:
            try:
                gs._execute_google_api_call_with_retry(drive_service.files().delete, fileId=att['id'])
            except Exception as e:
                current_app.logger.error(f"Failed to delete attachment {att.get('id')} from Drive: {e}")

    new_notes = utils.build_notes_string(base_notes, history, feedback)
    if gs.update_google_task(task_id, notes=new_notes):
        current_app.cache.clear()
        return jsonify({'status': 'success', 'message': 'Report deleted successfully'})
    return jsonify({'status': 'error', 'message': 'Failed to save changes after deletion'}), 500

@api_bp.route('/task/delete/<task_id>', methods=['POST'])
def api_delete_task(task_id):
    if gs.delete_google_task(task_id):
        current_app.cache.clear()
        return jsonify({'status': 'success', 'message': 'Task deleted successfully'})
    return jsonify({'status': 'error', 'message': 'Failed to delete task'}), 500

@api_bp.route('/tasks/delete_batch', methods=['POST'])
def api_delete_tasks_batch():
    task_ids = request.json.get('task_ids', [])
    if not task_ids:
        return jsonify({'status': 'warning', 'message': 'No task IDs provided'}), 400
    
    deleted_count, failed_count = 0, 0
    for task_id in task_ids:
        if gs.delete_google_task(task_id):
            deleted_count += 1
        else:
            failed_count += 1
    
    if deleted_count > 0:
        current_app.cache.clear()

    return jsonify({
        'status': 'success' if failed_count == 0 else 'warning',
        'message': f'Deleted: {deleted_count}, Failed: {failed_count}',
        'deleted_count': deleted_count,
        'failed_count': failed_count
    })