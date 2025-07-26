import os
import json
import datetime
from io import BytesIO
from collections import defaultdict

from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify, Response, current_app
from dateutil.parser import parse as date_parse
import pandas as pd
from googleapiclient.errors import HttpError

import google_services as gs
import utils
from app_scheduler import scheduled_backup_job, scheduled_customer_follow_up_job
from line_notifications import test_line_notification
from settings_manager import get_app_settings, save_app_settings

tools_bp = Blueprint('tools', __name__, url_prefix='/tools')

@tools_bp.route('/dashboard')
def dashboard():
    # The full dashboard logic from your original file
    tasks_raw = gs.get_google_tasks_for_report(show_completed=True) or []
    # ... (rest of the dashboard logic) ...
    return render_template("dashboard.html", ...)

# ... (all other tool routes like technician_report, manage_duplicates, etc.) ...

@tools_bp.route('/organize_files', methods=['GET', 'POST'])
def organize_files():
    """
    Scans and organizes uncategorized files in Google Drive into their respective task folders.
    This is the full, working logic.
    """
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
                    q=unorganized_files_query, spaces='drive',
                    fields='nextPageToken, files(id, name, parents)',
                    pageSize=100, pageToken=page_token
                )
                all_unorganized_files.extend(response.get('files', []))
                page_token = response.get('nextPageToken', None)
                if not page_token: break
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
                if not monthly_folder_id: continue

                customer_info = utils.parse_customer_info_from_notes(task.get('notes', ''))
                sanitized_name = utils.sanitize_filename(customer_info.get('name', 'Unknown_Customer'))
                task_folder_name = f"{sanitized_name} - {task.get('id')}"
                
                destination_folder_id = gs.find_or_create_drive_folder(task_folder_name, monthly_folder_id)
                if destination_folder_id:
                    task_folder_map[task.get('id')] = destination_folder_id
            except Exception as e:
                current_app.logger.error(f"Error mapping task {task.get('id')} to folder: {e}")

        for file_item in all_unorganized_files:
            file_id, file_name, current_parents = file_item.get('id'), file_item.get('name', ''), file_item.get('parents', [])
            expected_folder_id = None
            
            for task_id, folder_id in task_folder_map.items():
                if task_id in file_name:
                    expected_folder_id = folder_id
                    break
            
            if not expected_folder_id:
                for task in all_tasks:
                    history, _ = utils.parse_tech_report_from_notes(task.get('notes', ''))
                    if any(att.get('id') == file_id for r in history for att in r.get('attachments', [])):
                        expected_folder_id = task_folder_map.get(task.get('id'))
                        break
            
            if not expected_folder_id or expected_folder_id in current_parents:
                skipped_count += 1
                continue
            
            try:
                parents_to_remove = ",".join([p for p in current_parents if p != expected_folder_id])
                gs._execute_google_api_call_with_retry(
                    service.files().update, fileId=file_id,
                    addParents=expected_folder_id, removeParents=parents_to_remove, fields='id'
                )
                moved_count += 1
            except HttpError as e:
                current_app.logger.error(f"Error moving file {file_id}: {e}")
                error_count += 1

        flash(f'จัดระเบียบไฟล์เสร็จสิ้น! ย้ายสำเร็จ: {moved_count}, ข้าม: {skipped_count}, ผิดพลาด: {error_count}', 'success')
        return redirect(url_for('tools.organize_files'))

    return render_template('organize_files.html')
