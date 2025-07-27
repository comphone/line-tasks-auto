from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from collections import defaultdict
from datetime import datetime
from dateutil.parser import parse as date_parse
import pandas as pd
from io import BytesIO

# Import services and utils
import google_services as gs
import utils
from settings_manager import settings_manager
from line_notifications import send_line_notification
# from app_scheduler import run_scheduled_backup_job # This needs to be correctly imported

tools_bp = Blueprint('tools', __name__)

# --- Existing Routes for Context ---
@tools_bp.route('/dashboard')
def dashboard():
    tasks_df = gs.get_all_tasks()
    tasks = tasks_df.to_dict('records') if not tasks_df.empty else []
    return render_template('dashboard.html', tasks=tasks)

@tools_bp.route('/settings', methods=['GET', 'POST'])
def settings_page():
     if request.method == 'POST':
        flash('Settings saved successfully!', 'success')
        return redirect(url_for('tools.settings_page'))
     settings = settings_manager.get_all_settings()
     return render_template('settings_page.html', settings=settings)

# --- Routes with Implemented Logic ---

@tools_bp.route('/technician_report')
def technician_report():
    """
    Displays a report of completed tasks per technician for a selected month and year.
    """
    now = datetime.now(utils.BANGKOK_TZ)
    try:
        year = int(request.args.get('year', now.year))
        month = int(request.args.get('month', now.month))
    except (ValueError, TypeError):
        year, month = now.year, now.month
    
    months = [{'value': i, 'name': datetime(2000, i, 1).strftime('%B')} for i in range(1, 13)]
    years = list(range(now.year - 5, now.year + 2))
    
    tasks_df = gs.get_all_tasks()
    completed_tasks = tasks_df[tasks_df['status'] == 'completed'].to_dict('records')

    report = defaultdict(lambda: {'count': 0, 'tasks': []})

    for task in completed_tasks:
        if task.get('completion_date'):
            try:
                completed_dt = date_parse(task['completion_date']).astimezone(utils.BANGKOK_TZ)
                if completed_dt.year == year and completed_dt.month == month:
                    history, _ = gs.parse_tech_report_from_notes(task.get('notes', ''))
                    task_techs = set()
                    for r in history:
                        for t_name in r.get('technicians', []):
                            if isinstance(t_name, str):
                                task_techs.add(t_name.strip())

                    for tech_name in sorted(list(task_techs)):
                        report[tech_name]['count'] += 1
                        report[tech_name]['tasks'].append({
                            'id': task.get('id'), 
                            'title': task.get('problem_description'), 
                            'completed_formatted': completed_dt.strftime("%d/%m/%Y")
                        })
            except Exception as e:
                # app.logger.error(f"Error processing task {task.get('id')} for technician report: {e}")
                continue

    return render_template('technician_report.html',
                           report_data=dict(sorted(report.items())), 
                           selected_year=year, 
                           selected_month=month,
                           years=years, 
                           months=months)

@tools_bp.route('/manage_duplicates')
def manage_duplicates():
    """
    Finds and displays duplicate tasks based on title and customer name.
    Logic moved from app_old.py.
    """
    tasks_df = gs.get_all_tasks()
    all_tasks = tasks_df.to_dict('records')
    
    duplicates = defaultdict(list)
    for task in all_tasks:
        # Use problem_description as the main title and parse customer name
        title = task.get('problem_description', '').strip()
        customer_name = task.get('customer_name', '').strip()
        
        if title and customer_name:
            # Group by a tuple of (title, customer_name)
            duplicates[(title.lower(), customer_name.lower())].append(task)
    
    # Filter for groups that have more than one task (actual duplicates)
    # and sort them by creation date to easily identify the original
    duplicate_sets = {
        key: sorted(tasks, key=lambda t: t.get('timestamp', ''), reverse=True) 
        for key, tasks in duplicates.items() if len(tasks) > 1
    }

    # Process tasks in each set for display
    processed_sets = {}
    for key, task_list in duplicate_sets.items():
        processed_tasks = [utils.parse_google_task_dates(task) for task in task_list]
        processed_sets[key] = processed_tasks

    return render_template('manage_duplicates.html', duplicates=processed_sets)

@tools_bp.route('/delete_task_duplicates', methods=['POST'])
def delete_task_duplicates():
    """
    Handles the deletion of selected duplicate tasks.
    """
    task_ids_to_delete = request.form.getlist('task_ids')
    if not task_ids_to_delete:
        flash('No tasks selected for deletion.', 'warning')
        return redirect(url_for('tools.manage_duplicates'))

    deleted_count = 0
    failed_count = 0
    for task_id in task_ids_to_delete:
        if gs.delete_task(task_id):
            deleted_count += 1
        else:
            failed_count += 1
    
    if deleted_count > 0:
        flash(f'Successfully deleted {deleted_count} duplicate tasks.', 'success')
    if failed_count > 0:
        flash(f'Failed to delete {failed_count} tasks.', 'danger')

    return redirect(url_for('tools.manage_duplicates'))


# --- Routes with PLACEHOLDER LOGIC ---

@tools_bp.route('/organize_files', methods=['GET', 'POST'])
def organize_files():
    if request.method == 'POST':
        flash('File organization process started!', 'info')
        # TODO: Implement the file organization logic by calling a function in google_services.py
        return redirect(url_for('tools.organize_files'))
    return render_template('organize_files.html')

@tools_bp.route('/backup_data')
def backup_data():
    memory_file, filename = utils._create_backup_zip()
    if memory_file and filename:
        return Response(memory_file.getvalue(), mimetype='application/zip', headers={'Content-Disposition': f'attachment;filename={filename}'})
    else:
        flash('Error creating backup file.', 'danger')
        return redirect(url_for('tools.settings_page'))

# ... other placeholder routes ...
