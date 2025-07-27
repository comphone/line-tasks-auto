from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_from_directory
import pandas as pd
import google_services as gs
from datetime import datetime
import json
from functools import lru_cache
from utils import get_current_time_bkk, format_datetime_bkk
import os
from line_notifications import send_line_notification
from settings_manager import settings_manager
import logging

main_bp = Blueprint('main', __name__)

def format_timestamp(ts_string):
    """
    Helper function to format timestamp string.
    If the format is '%Y-%m-%d %H:%M:%S', it keeps it.
    Otherwise, it tries to parse and format it.
    """
    if not ts_string or pd.isna(ts_string):
        return "N/A"
    try:
        # Check if it's already in the desired format
        datetime.strptime(ts_string, '%Y-%m-%d %H:%M:%S')
        return ts_string
    except ValueError:
        try:
            # Try to parse other common formats
            dt_obj = pd.to_datetime(ts_string)
            return dt_obj.strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            return ts_string # Return original if parsing fails

# Caching for dropdown data to reduce Google Sheets API calls
@lru_cache(maxsize=128)
def get_cached_customers():
    return gs.get_all_customers()

@lru_cache(maxsize=128)
def get_cached_equipments():
    return gs.get_all_equipments()

@lru_cache(maxsize=128)
def get_cached_technicians():
    return gs.get_all_technicians()


@main_bp.route('/')
def index():
    return redirect(url_for('tools.dashboard'))

@main_bp.route('/form', methods=['GET', 'POST'])
def form():
    try:
        all_customers = get_cached_customers()
        all_equipments = get_cached_equipments()
        all_technicians = get_cached_technicians()

        if request.method == 'POST':
            try:
                now_bkk = get_current_time_bkk()
                task_data = {
                    'id': gs.generate_task_id(),
                    'timestamp': format_datetime_bkk(now_bkk),
                    'last_update': format_datetime_bkk(now_bkk),
                    'customer_name': request.form.get('customer_name'),
                    'customer_id': request.form.get('customer_id'),
                    'equipment_name': request.form.get('equipment_name'),
                    'equipment_id': request.form.get('equipment_id'),
                    'problem_description': request.form.get('problem_description'),
                    'status': 'เปิด',
                    'assigned_to': request.form.get('assigned_to'),
                    'priority': request.form.get('priority'),
                    'notes': request.form.get('notes'),
                    'location': request.form.get('location'),
                    'contact_person': request.form.get('contact_person'),
                    'phone_number': request.form.get('phone_number'),
                    'created_by': 'Web Form',
                    'completion_date': '',
                    'resolution_details': '',
                    'attachments': '',
                    'related_tasks': '',
                    'customer_rating': '',
                    'feedback': ''
                }

                success = gs.create_task(task_data)

                if success:
                    flash('สร้างงานใหม่สำเร็จแล้ว!', 'success')
                    
                    # Send LINE Notification
                    try:
                        message = (
                            f"📌 สร้างงานใหม่แล้ว\n"
                            f"🏢 ลูกค้า: {task_data['customer_name']}\n"
                            f"🔧 อุปกรณ์: {task_data['equipment_name']}\n"
                            f"📝 ปัญหา: {task_data['problem_description']}\n"
                            f"👤 ผู้รับผิดชอบ: {task_data['assigned_to'] or 'ยังไม่ระบุ'}\n"
                            f"สถานะ: {task_data['status']}"
                        )
                        admin_line_id = settings_manager.get_setting('admin_line_id')
                        if admin_line_id:
                            send_line_notification(admin_line_id, message)
                        else:
                            logging.warning("Admin LINE ID not set, skipping notification.")
                    except Exception as e:
                        logging.error(f"Failed to send LINE notification: {e}")
                        flash('สร้างงานสำเร็จ แต่ไม่สามารถส่งแจ้งเตือนผ่าน LINE ได้', 'warning')

                    return redirect(url_for('tools.dashboard'))
                else:
                    flash('เกิดข้อผิดพลาดในการสร้างงาน', 'error')

            except Exception as e:
                logging.error(f"Error processing form submission: {e}")
                flash(f'เกิดข้อผิดพลาด: {e}', 'error')

        return render_template('form.html',
                               all_customers=all_customers,
                               all_equipments=all_equipments,
                               all_technicians=all_technicians)
    except Exception as e:
        logging.error(f"Error loading form page: {e}")
        flash('ไม่สามารถโหลดข้อมูลสำหรับฟอร์มได้ กรุณาลองอีกครั้ง', 'error')
        return render_template('form.html',
                               all_customers=[],
                               all_equipments=[],
                               all_technicians=[])

@main_bp.route('/edit_task/<task_id>', methods=['GET', 'POST'])
def edit_task(task_id):
    if request.method == 'POST':
        try:
            now_bkk = get_current_time_bkk()
            task_data = {
                'customer_name': request.form.get('customer_name'),
                'customer_id': request.form.get('customer_id'),
                'equipment_name': request.form.get('equipment_name'),
                'equipment_id': request.form.get('equipment_id'),
                'problem_description': request.form.get('problem_description'),
                'status': request.form.get('status'),
                'assigned_to': request.form.get('assigned_to'),
                'priority': request.form.get('priority'),
                'notes': request.form.get('notes'),
                'location': request.form.get('location'),
                'contact_person': request.form.get('contact_person'),
                'phone_number': request.form.get('phone_number'),
                'resolution_details': request.form.get('resolution_details'),
                'last_update': format_datetime_bkk(now_bkk)
            }

            # Handle completion date
            if task_data['status'] == 'ปิด' and not gs.get_task_by_id(task_id).get('completion_date'):
                task_data['completion_date'] = format_datetime_bkk(now_bkk)

            success = gs.update_task(task_id, task_data)

            if success:
                flash('อัปเดตงานสำเร็จแล้ว!', 'success')
                return redirect(url_for('tools.dashboard'))
            else:
                flash('เกิดข้อผิดพลาดในการอัปเดตงาน', 'error')

        except Exception as e:
            logging.error(f"Error updating task {task_id}: {e}")
            flash(f'เกิดข้อผิดพลาด: {e}', 'error')

    # For GET request
    try:
        task = gs.get_task_by_id(task_id)
        if not task:
            flash(f'ไม่พบงานรหัส: {task_id}', 'error')
            return redirect(url_for('tools.dashboard'))

        all_customers = get_cached_customers()
        all_equipments = get_cached_equipments()
        all_technicians = get_cached_technicians()

        return render_template('edit_task.html',
                               task=task,
                               all_customers=all_customers,
                               all_equipments=all_equipments,
                               all_technicians=all_technicians)
    except Exception as e:
        logging.error(f"Error loading edit page for task {task_id}: {e}")
        flash('ไม่สามารถโหลดข้อมูลงานได้ กรุณาลองอีกครั้ง', 'error')
        return redirect(url_for('tools.dashboard'))


@main_bp.route('/delete_task/<task_id>', methods=['POST'])
def delete_task(task_id):
    try:
        success = gs.delete_task(task_id)
        if success:
            flash(f'ลบงานรหัส {task_id} สำเร็จแล้ว', 'success')
        else:
            flash(f'ไม่สามารถลบงานรหัส {task_id} ได้', 'error')
    except Exception as e:
        logging.error(f"Error deleting task {task_id}: {e}")
        flash(f'เกิดข้อผิดพลาดในการลบงาน: {e}', 'error')
    return redirect(url_for('tools.dashboard'))


@main_bp.route('/task/<task_id>')
def task_details(task_id):
    try:
        task = gs.get_task_by_id(task_id)

        # === FIX START: Add a guard clause to handle cases where the task is not found ===
        if not task:
            flash(f"ไม่พบงานรหัส: {task_id}", "error")
            # Redirect to a safe page like the dashboard
            return redirect(url_for('tools.dashboard'))
        # === FIX END ===

        all_tasks_df = gs.get_all_tasks()
        all_tasks = all_tasks_df.to_dict('records') if not all_tasks_df.empty else []

        # Ensure task data is processed safely
        task['last_update'] = format_timestamp(task.get('last_update'))
        task['timestamp'] = format_timestamp(task.get('timestamp'))
        # Safely get history and URLs, providing an empty list as a fallback
        task['tech_reports_history'] = gs.get_tech_reports_by_task_id(task_id) or []
        task['image_urls'] = gs.get_image_urls_for_task(task_id) or []

        all_customers = get_cached_customers()
        all_equipments = get_cached_equipments()
        all_technicians = get_cached_technicians()

        return render_template('update_task_details.html',
                               task=task,
                               all_tasks_json=json.dumps(all_tasks),
                               current_task_id=task_id,
                               all_customers=all_customers,
                               all_equipments=all_equipments,
                               all_technicians=all_technicians
                               )
    except Exception as e:
        logging.error(f"Error loading details for task {task_id}: {e}")
        flash(f'เกิดข้อผิดพลาดในการโหลดรายละเอียดงาน: {e}', 'error')
        return redirect(url_for('tools.dashboard'))

@main_bp.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """Serve uploaded files."""
    upload_folder = os.path.join(os.getcwd(), 'uploads')
    return send_from_directory(upload_folder, filename)
