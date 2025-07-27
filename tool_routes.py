import os
import json
import datetime
from io import BytesIO
from collections import defaultdict
import pytz

from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify, Response, current_app
from dateutil.parser import parse as date_parse
import pandas as pd
from googleapiclient.errors import HttpError

import google_services as gs
import utils
from app_scheduler import scheduled_backup_job, scheduled_customer_follow_up_job
from line_notifications import test_line_notification
from settings_manager import get_app_settings, save_app_settings
from google_services import create_backup_zip # Modified: Import from google_services

tools_bp = Blueprint('tools', __name__, url_prefix='/tools')

@tools_bp.route('/dashboard')
def dashboard():
    """The main dashboard page with stats and charts."""
    tasks_raw = gs.get_google_tasks_for_report(show_completed=True) or []
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    today_thai = datetime.datetime.now(utils.THAILAND_TZ).date()
    final_tasks = []
    stats = {'needsAction': 0, 'completed': 0, 'overdue': 0, 'total': len(tasks_raw), 'today': 0}

    for task in tasks_raw:
        task_status = task.get('status', 'needsAction')
        is_overdue = False
        is_today = False
        if task_status == 'needsAction' and task.get('due'):
            try:
                due_dt_utc = date_parse(task['due'])
                due_dt_local = due_dt_utc.astimezone(utils.THAILAND_TZ)
                if due_dt_local.date() < today_thai:
                    is_overdue = True
                elif due_dt_local.date() == today_thai:
                    is_today = True
            except (ValueError, TypeError):
                pass
        
        if task_status == 'completed': stats['completed'] += 1
        else:
            stats['needsAction'] += 1
            if is_overdue: stats['overdue'] += 1
            if is_today: stats['today'] += 1

        task_passes_filter = (status_filter == 'all' or
                              (status_filter == 'completed' and task_status == 'completed') or
                              (status_filter == 'needsAction' and task_status == 'needsAction') or
                              (status_filter == 'today' and is_today))
        
        if task_passes_filter:
            customer_info = utils.parse_customer_info_from_notes(task.get('notes', ''))
            searchable_text = f"{task.get('title', '')} {customer_info.get('name', '')} {customer_info.get('organization', '')} {customer_info.get('phone', '')}".lower()

            if not search_query or search_query in searchable_text:
                parsed_task = utils.parse_google_task_dates(task)
                parsed_task['customer'] = customer_info
                parsed_task['is_overdue'] = is_overdue
                parsed_task['is_today'] = is_today
                final_tasks.append(parsed_task)

    final_tasks.sort(key=lambda x: (x.get('status') == 'completed', x.get('due') is None, date_parse(x.get('due', '9999-12-31T23:59:59Z'))))
    
    completed_tasks_for_chart = [t for t in tasks_raw if t.get('status') == 'completed' and t.get('completed')]
    month_labels = []
    chart_values = []
    for i in range(12):
        target_d = datetime.datetime.now(utils.THAILAND_TZ) - datetime.timedelta(days=30 * (11 - i))
        month_key = target_d.strftime('%Y-%m')
        month_labels.append(target_d.strftime('%b %y'))
        count = sum(1 for task in completed_tasks_for_chart if date_parse(task['completed']).astimezone(utils.THAILAND_TZ).strftime('%Y-%m') == month_key)
        chart_values.append(count)
    chart_data = {'labels': month_labels, 'values': chart_values}

    # Added job_types and job_type_filter for consistency with dashboard.html
    # This might need refinement based on how job types are stored/filtered in your actual app.
    # For now, it's a placeholder to prevent error if these variables are expected.
    job_types = {'all': 'ทั้งหมด', 'service': 'เซอร์วิส (นอกสถานที่)', 'in_shop': 'ซ่อมหน้าร้าน', 'supplier': 'ส่งซ่อมร้านนอก'}
    job_type_filter = str(request.args.get('job_type_filter', 'all')).strip()


    return render_template("dashboard.html", tasks=final_tasks, summary=stats, search_query=search_query, status_filter=status_filter, chart_data=chart_data, job_types=job_types, job_type_filter=job_type_filter)

@tools_bp.route('/summary')
def summary():
    """Redirects to the dashboard as a general summary page."""
    # This function is added to handle potential requests for a /summary route
    # and redirects them to the /dashboard route, which serves as a comprehensive summary.
    return redirect(url_for('tools.dashboard', **request.args))


@tools_bp.route('/summary/print')
def summary_print():
    """Generates a printable summary report based on current filters."""
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter_key = str(request.args.get('status_filter', 'all')).strip()
    status_map = {'all': 'ทั้งหมด', 'needsAction': 'ยังไม่เสร็จ', 'completed': 'เสร็จเรียบร้อย', 'today': 'งานวันนี้'}
    status_filter_display = status_map.get(status_filter_key, 'ทั้งหมด')

    tasks_raw = gs.get_google_tasks_for_report(show_completed=True) or []
    final_tasks = []
    today_thai = datetime.datetime.now(utils.THAILAND_TZ).date()

    for task in tasks_raw:
        task_status = task.get('status', 'needsAction')
        is_today = False
        is_overdue = False
        if task_status == 'needsAction' and task.get('due'):
            try:
                due_dt_local = date_parse(task['due']).astimezone(utils.THAILAND_TZ)
                if due_dt_local.date() == today_thai:
                    is_today = True
                if due_dt_local.date() < today_thai:
                    is_overdue = True
            except (ValueError, TypeError):
                pass
        
        task_passes_filter = (status_filter_key == 'all' or
                              (status_filter_key == 'completed' and task_status == 'completed') or
                              (status_filter_key == 'needsAction' and task_status == 'needsAction') or
                              (status_filter_key == 'today' and is_today))
        
        if task_passes_filter:
            customer_info = utils.parse_customer_info_from_notes(task.get('notes', ''))
            searchable_text = f"{task.get('title', '')} {customer_info.get('name', '')} {customer_info.get('organization', '')} {customer_info.get('phone', '')}".lower()
            if not search_query or search_query in searchable_text:
                parsed_task = utils.parse_google_task_dates(task)
                parsed_task['customer'] = customer_info
                parsed_task['is_today'] = is_today
                parsed_task['is_overdue'] = is_overdue
                final_tasks.append(parsed_task)

    final_tasks.sort(key=lambda x: (x.get('status') == 'completed', x.get('due') is None, date_parse(x.get('due', '9999-12-31T23:59:59Z'))))

    return render_template("summary_print.html",
                           tasks=final_tasks,
                           search_query=search_query,
                           status_filter=status_filter_display,
                           now=datetime.datetime.now(utils.THAILAND_TZ)) # Pass 'now' to template


@tools_bp.route('/technician_report')
def technician_report():
    """Generates a report of completed tasks per technician for a selected month and year."""
    now = datetime.datetime.now(utils.THAILAND_TZ)
    try:
        year, month = int(request.args.get('year', now.year)), int(request.args.get('month', now.month))
    except (ValueError, TypeError):
        year, month = now.year, now.month
    
    months = [{'value': i, 'name': datetime.date(2000, i, 1).strftime('%B')} for i in range(1, 13)]
    
    app_settings = get_app_settings()
    technician_list = app_settings.get('technician_list', [])

    tasks = gs.get_google_tasks_for_report(show_completed=True) or []
    report = defaultdict(lambda: {'count': 0, 'tasks': []})

    for task in tasks:
        if task.get('status') == 'completed' and task.get('completed'):
            try:
                completed_dt = date_parse(task['completed']).astimezone(utils.THAILAND_TZ)
                if completed_dt.year == year and completed_dt.month == month:
                    history, _ = utils.parse_tech_report_from_notes(task.get('notes', ''))
                    task_techs = set()
                    for r in history:
                        for t_name in r.get('technicians', []):
                            if isinstance(t_name, str): task_techs.add(t_name.strip())

                    for tech_name in sorted(list(task_techs)):
                        report[tech_name]['count'] += 1
                        report[tech_name]['tasks'].append({'id': task.get('id'), 'title': task.get('title'), 'completed_formatted': completed_dt.strftime("%d/%m/%Y")})
            except Exception as e:
                current_app.logger.error(f"Error processing task {task.get('id')} for technician report: {e}")
                continue

    return render_template('technician_report.html', report_data=report, selected_year=year, selected_month=month, years=list(range(now.year - 5, now.year + 2)), months=months, technician_list=technician_list)

@tools_bp.route('/manage_duplicates')
def manage_duplicates():
    """Finds and displays tasks that might be duplicates."""
    tasks = gs.get_google_tasks_for_report(show_completed=True) or []
    duplicates = defaultdict(list)
    for task in tasks:
        if task.get('title'):
            customer_name = utils.parse_customer_info_from_notes(task.get('notes', '')).get('name', '').strip().lower()
            duplicates[(task['title'].strip(), customer_name)].append(task)
    
    sets = {k: sorted(v, key=lambda t: t.get('created', ''), reverse=True) for k, v in duplicates.items() if len(v) > 1}
    processed_sets = {}
    for key, task_list in sets.items():
        processed_tasks = []
        for task in task_list:
            parsed = utils.parse_google_task_dates(task)
            parsed['customer'] = utils.parse_customer_info_from_notes(task.get('notes', ''))
            parsed['is_overdue'] = task.get('status') == 'needsAction' and task.get('due') and date_parse(task['due']) < datetime.datetime.now(pytz.utc)
            processed_tasks.append(parsed)
        processed_sets[key] = processed_tasks
    return render_template('manage_duplicates.html', potential_duplicate_sets=processed_sets)

@tools_bp.route('/delete_duplicates_batch', methods=['POST'])
def delete_duplicates_batch():
    """Handles the batch deletion of selected duplicate tasks."""
    ids = request.form.getlist('task_ids')
    if not ids:
        flash('ไม่พบรายการที่เลือกเพื่อลบ', 'warning')
        return redirect(url_for('tools.manage_duplicates'))
    deleted, failed = 0, 0
    for task_id in ids:
        if gs.delete_google_task(task_id): deleted += 1
        else: failed += 1
    if deleted > 0: current_app.cache.clear()
    flash(f'ลบงานที่เลือกสำเร็จ: {deleted} รายการ. ล้มเหลว: {failed} รายการ.', 'success' if failed == 0 else 'warning')
    return redirect(url_for('tools.manage_duplicates'))

@tools_bp.route('/manage_equipment_duplicates')
def manage_equipment_duplicates():
    """Finds and displays equipment in the catalog that might be duplicates."""
    catalog = get_app_settings().get('equipment_catalog', [])
    duplicates = defaultdict(list)
    for i, item in enumerate(catalog):
        name = item.get('item_name', '').strip().lower()
        if name: duplicates[name].append({'original_index': i, 'data': item})
    sets = {k: sorted(v, key=lambda x: x['original_index']) for k, v in duplicates.items() if len(v) > 1}
    return render_template('equipment_duplicates.html', duplicates=sets)

@tools_bp.route('/delete_equipment_duplicates_batch', methods=['POST'])
def delete_equipment_duplicates_batch():
    """Handles the batch deletion of selected duplicate equipment."""
    indices = sorted([int(idx) for idx in request.form.getlist('item_indices')], reverse=True)
    if not indices:
        flash('ไม่พบรายการอุปกรณ์ที่เลือกเพื่อลบ', 'warning')
        return redirect(url_for('tools.manage_equipment_duplicates'))
    catalog = get_app_settings().get('equipment_catalog', [])
    deleted_count = 0
    for idx in indices:
        if 0 <= idx < len(catalog):
            catalog.pop(idx)
            deleted_count += 1
    if save_app_settings({'equipment_catalog': catalog}):
        flash(f'ลบรายการอุปกรณ์ที่เลือกสำเร็จ: {deleted_count} รายการ.', 'success')
    else:
        flash('เกิดข้อผิดพลาดในการบันทึกการเปลี่ยนแปลงแคตตาล็อกอุปกรณ์', 'danger')
    return redirect(url_for('tools.manage_equipment_duplicates'))

@tools_bp.route('/organize_files', methods=['GET', 'POST'])
def organize_files():
    """Scans and organizes uncategorized files in Google Drive into their respective task folders."""
    if request.method == 'POST':
        service = gs.get_google_drive_service()
        if not service:
            flash('ไม่สามารถเชื่อมต่อ Google Drive API ได้', 'danger')
            return redirect(url_for('tools.organize_files'))

        all_tasks = gs.get_google_tasks_for_report(show_completed=True)
        if all_tasks is None:
            flash('ไม่สามารถดึงข้อมูลงานทั้งหมดได้', 'danger')
            return redirect(url_for('tools.organize_files'))
            
        moved_count, skipped_count, error_count = 0, 0, 0
        
        attachments_base_folder_id = gs.find_or_create_drive_folder("Task_Attachments", gs.GOOGLE_DRIVE_FOLDER_ID)
        if not attachments_base_folder_id:
            flash('ไม่สามารถสร้างหรือค้นหาโฟลเดอร์หลัก "Task_Attachments" ได้', 'danger')
            return redirect(url_for('tools.organize_files'))

        unorganized_files_query = (
            f"'{attachments_base_folder_id}' in parents "
            f"and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
        )
        
        all_unorganized_files = []
        try:
            page_token = None
            while True:
                response = gs._execute_google_api_call_with_retry(
                    service.files().list,
                    q=unorganized_files_query,
                    spaces='drive',
                    fields='nextPageToken, files(id, name, parents)',
                    pageSize=100,
                    pageToken=page_token
                )
                all_unorganized_files.extend(response.get('files', []))
                page_token = response.get('nextPageToken', None)
                if not page_token:
                    break
        except HttpError as e:
            current_app.logger.error(f"Error listing files for organization: {e}")
            flash('เกิดข้อผิดพลาดในการดึงรายการไฟล์จาก Google Drive', 'danger')
            return redirect(url_for('tools.organize_files'))

        task_folder_map = {}
        for task in all_tasks:
            try:
                created_dt_local = date_parse(task.get('created', datetime.datetime.now(pytz.utc).isoformat())).astimezone(utils.THAILAND_TZ)
                monthly_folder_name = created_dt_local.strftime('%Y-%m')
                monthly_folder_id = gs.find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)
                
                if not monthly_folder_id:
                    continue

                customer_info = utils.parse_customer_info_from_notes(task.get('notes', ''))
                sanitized_customer_name = utils.sanitize_filename(customer_info.get('name', 'Unknown_Customer'))
                customer_task_folder_name = f"{sanitized_customer_name} - {task.get('id')}"
                
                destination_folder_id = gs.find_or_create_drive_folder(customer_task_folder_name, monthly_folder_id)
                if destination_folder_id:
                    task_folder_map[task.get('id')] = destination_folder_id
            except Exception as e:
                current_app.logger.error(f"Error processing task {task.get('id')} for folder mapping: {e}")

        for file_item in all_unorganized_files:
            file_id = file_item.get('id')
            file_name = file_item.get('name', 'Unnamed File')
            current_parents = file_item.get('parents', [])

            expected_folder_id = None
            for task_id_candidate in task_folder_map.keys():
                if task_id_candidate in file_name:
                    expected_folder_id = task_folder_map.get(task_id_candidate)
                    break
            
            if not expected_folder_id:
                for task in all_tasks:
                    history, _ = utils.parse_tech_report_from_notes(task.get('notes', ''))
                    for report in history:
                        for attachment in report.get('attachments', []):
                            if attachment.get('id') == file_id:
                                expected_folder_id = task_folder_map.get(task.get('id'))
                                break
                        if expected_folder_id: break
                    if expected_folder_id: break

            if not expected_folder_id or expected_folder_id in current_parents:
                skipped_count += 1
                continue
            
            try:
                parents_to_remove = [p for p in current_parents if p != expected_folder_id]
                gs._execute_google_api_call_with_retry(
                    service.files().update,
                    fileId=file_id,
                    addParents=expected_folder_id,
                    removeParents=",".join(parents_to_remove),
                    fields='id, parents'
                )
                moved_count += 1
            except HttpError as file_error:
                current_app.logger.error(f"Error moving file {file_id} ('{file_name}'): {file_error}")
                error_count += 1

        flash(f'จัดระเบียบไฟล์เสร็จสิ้น! ย้ายสำเร็จ: {moved_count}, ข้าม: {skipped_count}, ผิดพลาด: {error_count}', 'success')
        return redirect(url_for('tools.organize_files'))

    return render_template('organize_files.html')

@tools_bp.route('/export_equipment_catalog')
def export_equipment_catalog():
    """Exports the current equipment catalog to an Excel file."""
    try:
        df = pd.DataFrame(get_app_settings().get('equipment_catalog', []))
        if df.empty:
            flash('ไม่มีข้อมูลอุปกรณ์ในแคตตาล็อก', 'warning')
            return redirect(url_for('main.settings_page'))
        output = BytesIO()
        df.to_excel(output, index=False, sheet_name='Equipment_Catalog')
        output.seek(0)
        return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment;filename=equipment_catalog.xlsx"})
    except Exception as e:
        flash(f'เกิดข้อผิดพลาดในการส่งออก: {e}', 'danger')
        return redirect(url_for('main.settings_page'))

@tools_bp.route('/import_equipment_catalog', methods=['POST'])
def import_equipment_catalog():
    """Imports an equipment catalog from an uploaded Excel file."""
    if 'excel_file' not in request.files or not request.files['excel_file'].filename:
        flash('กรุณาเลือกไฟล์ Excel', 'danger')
        return redirect(url_for('main.settings_page'))
    file = request.files['excel_file']
    if file and file.filename.endswith(('.xls', '.xlsx')):
        try:
            df = pd.read_excel(file.stream)
            required_cols = ['item_name', 'unit', 'price']
            if not all(col in df.columns for col in required_cols):
                flash(f'ไฟล์ Excel ต้องมีคอลัมน์: {", ".join(required_cols)}', 'danger')
            else:
                imported_catalog = []
                for _, row in df.iterrows():
                    item = {'item_name': str(row['item_name']).strip()}
                    if pd.notna(row['unit']): item['unit'] = str(row['unit']).strip()
                    if pd.notna(row['price']):
                        try: item['price'] = float(row['price'])
                        except ValueError: item['price'] = 0.0
                    imported_catalog.append(item)
                save_app_settings({'equipment_catalog': imported_catalog})
                flash('นำเข้าแคตตาล็อกอุปกรณ์เรียบร้อยแล้ว!', 'success')
        except Exception as e:
            flash(f"เกิดข้อผิดพลาดในการนำเข้าไฟล์: {e}", 'danger')
    else:
        flash('รองรับเฉพาะไฟล์ Excel (.xls, .xlsx) เท่านั้น', 'danger')
    return redirect(url_for('main.settings_page'))

@tools_bp.route('/backup_data')
def backup_data():
    """Triggers the creation and download of a full system backup zip file."""
    memory_file, filename = create_backup_zip()
    if memory_file and filename:
        return Response(memory_file.getvalue(), mimetype='application/zip', headers={'Content-Disposition': f'attachment;filename={filename}'})
    else:
        flash('เกิดข้อผิดพลาดในการสร้างไฟล์สำรองข้อมูล', 'danger')
        return redirect(url_for('main.settings_page'))

@tools_bp.route('/trigger_auto_backup_now', methods=['POST'])
def trigger_auto_backup_now():
    """Manually triggers the customer follow-up job for testing purposes."""
    with current_app.app_context():
        # This route already exists in app.py, so call it directly or remove this duplicate.
        # This implementation calls the scheduled_backup_job directly.
        if scheduled_backup_job():
            flash('สำรองข้อมูลไปที่ Google Drive สำเร็จ!', 'success')
        else:
            flash('เกิดข้อผิดพลาดในการสำรองข้อมูลไปที่ Google Drive!', 'danger')
    return redirect(url_for('main.settings_page'))

@tools_bp.route('/test_notification', methods=['POST'])
def test_notification():
    """Sends a test notification to the configured admin group."""
    # This function already exists in line_notifications.py, call it directly.
    test_line_notification()
    return redirect(url_for('main.settings_page'))

@tools_bp.route('/trigger_customer_follow_up_test', methods=['POST'])
def trigger_customer_follow_up_test():
    """Manually triggers the customer follow-up job for testing purposes."""
    with current_app.app_context():
        tasks = [t for t in (gs.get_google_tasks_for_report(True) or []) if t.get('status') == 'completed' and t.get('completed')]
        if not tasks:
            flash('ไม่พบงานที่เสร็จแล้วสำหรับใช้ทดสอบ.', 'warning')
            return redirect(url_for('main.settings_page'))
        latest = max(tasks, key=lambda x: date_parse(x.get('completed', '0001-01-01T00:00:00Z')))
        notes = latest.get('notes', '')
        feedback = utils.parse_customer_feedback_from_notes(notes)
        feedback.pop('follow_up_sent_date', None)
        
        history_reports, base_notes = utils.parse_tech_report_from_notes(notes)
        tech_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history_reports])
        new_notes_content = base_notes.strip()
        if tech_reports_text: new_notes_content += tech_reports_text
        new_notes_content += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

        gs.update_google_task(latest['id'], notes=new_notes_content)
        
        latest['completed'] = (datetime.datetime.now(pytz.utc) - datetime.timedelta(days=1, minutes=5)).isoformat().replace('+00:00', 'Z')
        gs.update_google_task(latest['id'], completed=latest['completed'])
        
        current_app.cache.clear()
        scheduled_customer_follow_up_job()
        flash(f"กำลังทดสอบส่งแบบสอบถามสำหรับงานล่าสุด: '{latest.get('title')}'", 'info')
    return redirect(url_for('main.settings_page'))