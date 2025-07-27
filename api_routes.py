from flask import Blueprint, request, jsonify
import google_services as gs
import json

api_bp = Blueprint('api', __name__)

# ... existing API routes ...

# --- NEW API Endpoints for Task Details Page ---

@api_bp.route('/task/<task_id>/action', methods=['POST'])
def handle_task_action(task_id):
    """A single route to handle complete, reschedule, and save_report actions."""
    action = request.form.get('action')
    if not action:
        return jsonify({'status': 'error', 'message': 'Action not specified.'}), 400

    # This is a simplified example. You would have specific logic for each action.
    # For example, you would call gs.update_task with different parameters.
    
    # Logic to get form data
    work_summary = request.form.get('work_summary')
    technicians = request.form.get('technicians_report')
    uploaded_attachments_json = request.form.get('uploaded_attachments_json', '[]')
    new_attachments = json.loads(uploaded_attachments_json)

    # Here you would call a service function to update the task
    # success = gs.add_task_report(task_id, work_summary, technicians, new_attachments)
    
    message_map = {
        'complete_task': 'ปิดงานและบันทึกรายงานสรุปเรียบร้อยแล้ว!',
        'reschedule_task': 'เลื่อนนัดและบันทึกเหตุผลเรียบร้อยแล้ว',
        'save_report': 'เพิ่มรายงานความคืบหน้าเรียบร้อยแล้ว!'
    }
    
    return jsonify({'status': 'success', 'message': message_map.get(action, 'ดำเนินการสำเร็จ')})

@api_bp.route('/task/<task_id>/report/<int:report_index>', methods=['POST', 'DELETE'])
def manage_task_report(task_id, report_index):
    if request.method == 'POST': # Edit text
        data = request.json
        new_summary = data.get('summary')
        # success = gs.edit_report_summary(task_id, report_index, new_summary)
        return jsonify({'status': 'success', 'message': 'แก้ไขรายงานเรียบร้อยแล้ว'})
    
    elif request.method == 'DELETE':
        # success = gs.delete_task_report(task_id, report_index)
        return jsonify({'status': 'success', 'message': 'ลบรายงานเรียบร้อยแล้ว'})

@api_bp.route('/upload_attachment', methods=['POST'])
def api_upload_attachment():
    # Logic from app_old.py to handle file upload to Google Drive
    # It should return a JSON with file_info on success
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part'}), 400
    
    # file = request.files['file']
    # task_id = request.form.get('task_id')
    # drive_file_info = gs.upload_file_from_request(file, task_id)
    # if drive_file_info:
    #    return jsonify({'status': 'success', 'file_info': drive_file_info})
    # else:
    #    return jsonify({'status': 'error', 'message': 'Upload failed'}), 500
    return jsonify({'status': 'success', 'file_info': {'id': 'dummy_id', 'url': '#', 'name': 'dummy.jpg'}}) # Dummy response
