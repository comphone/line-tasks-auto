from flask import Blueprint, render_template, request, redirect, url_for, flash
import google_services as gs
import utils
from settings_manager import settings_manager

main_bp = Blueprint('main', __name__)

@main_bp.route('/task/<task_id>', methods=['GET'])
def task_details(task_id):
    task = gs.get_task_by_id(task_id)
    if not task:
        flash(f'ไม่พบงานรหัส: {task_id}', 'error')
        return redirect(url_for('tools.dashboard'))

    # Prepare all necessary data for the feature-rich template
    task = utils.parse_google_task_dates(task)
    notes = task.get('notes', '')
    task['customer'] = utils.parse_customer_info_from_notes(notes)
    
    # --- FIX: Ensure tech_reports_history is always a list ---
    # Use the newly added function from utils.py and provide a default empty list
    reports, base_notes = utils.parse_tech_report_from_notes(notes)
    task['tech_reports_history'] = reports if reports is not None else []
    # --- END FIX ---
    
    all_attachments = [att for report in task['tech_reports_history'] for att in report.get('attachments', [])]

    settings = settings_manager.get_all_settings()
    text_snippets = settings.get('text_snippets', {})
    
    return render_template('update_task_details.html',
                           task=task,
                           all_attachments=all_attachments,
                           technician_list=settings.get('technician_list', []),
                           progress_report_snippets=text_snippets.get('progress_reports', [])
                          )

# ... (โค้ดส่วนอื่นๆ ของ main_routes.py) ...
