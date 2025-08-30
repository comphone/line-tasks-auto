# File: liff_views.py (โค้ดฉบับสมบูรณ์ที่แก้ไขแล้ว)

import os
import datetime
import pytz
import base64
import json
from itertools import groupby
from datetime import timedelta
from flask import (
    Blueprint, render_template, request, url_for, abort, jsonify,
    current_app, redirect, flash, make_response
)
from dateutil.parser import parse as date_parse
from weasyprint import HTML
from io import BytesIO
from num2words import num2words
from urllib.parse import quote_plus, quote
from linebot.v3.messaging import FlexMessage
from urllib.parse import quote_plus
from app import (
    db, JobItem, BillingStatus, LIFF_ID_FORM, LIFF_ID_TECHNICIAN_LOCATION,
    message_queue, cache, Warehouse, StockLevel, get_google_drive_service, _execute_google_api_call_with_retry
)
from utils import (
    get_google_tasks_for_report, get_single_task, parse_google_task_dates,
    parse_customer_info_from_notes, parse_tech_report_from_notes,
    parse_customer_feedback_from_notes, get_app_settings,
    generate_qr_code_base64, update_google_task, get_technician_report_data,
    find_or_create_drive_folder, upload_data_from_memory_to_drive,
    parse_assigned_technician_from_notes, parse_customer_profile_from_task,
    parse_task_data
)

liff_bp = Blueprint('liff', __name__)

THAILAND_TZ = pytz.timezone('Asia/Bangkok')

@liff_bp.route('/')
@liff_bp.route('/summary')
def summary():
    """แสดงหน้า dashboard/สรุปงาน"""
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()

    summary_stats = {'total': 0, 'needsAction': 0, 'completed': 0, 'overdue': 0, 'today': 0, 'external': 0}
    final_tasks = []

    for task_item in tasks_raw:
        parsed_task = parse_task_data(task_item)
        if not parsed_task:
            continue

        summary_stats['total'] += 1
        if parsed_task.get('title', '').lower().startswith('[งานภายนอก]'):
            summary_stats['external'] += 1
        
        if parsed_task['status'] == 'needsAction':
            summary_stats['needsAction'] += 1
            if parsed_task['is_overdue']:
                summary_stats['overdue'] += 1
            if parsed_task['is_today']:
                summary_stats['today'] += 1
        else:
            summary_stats['completed'] += 1
        
        task_passes_filter = False
        if status_filter == 'all': task_passes_filter = True
        elif status_filter == 'completed' and parsed_task['status'] == 'completed': task_passes_filter = True
        elif status_filter == 'needsAction' and parsed_task['status'] == 'needsAction': task_passes_filter = True
        elif status_filter == 'overdue' and parsed_task['is_overdue']: task_passes_filter = True
        elif status_filter == 'today' and parsed_task['is_today']: task_passes_filter = True
        elif status_filter == 'external' and parsed_task.get('title', '').lower().startswith('[งานภายนอก]'): task_passes_filter = True

        if task_passes_filter:
            customer_info = parsed_task.get('customer', {})
            searchable_text = f"{parsed_task.get('title', '')} {customer_info.get('name', '')} {customer_info.get('organization', '')} {customer_info.get('phone', '')}".lower()
            if not search_query or search_query in searchable_text:
                final_tasks.append(parsed_task)

    final_tasks.sort(key=lambda x: (x.get('status') == 'completed', x.get('due') is None, datetime.datetime.fromisoformat(x.get('due', '9999-12-31T23:59:59Z').replace('Z', '+00:00'))))
    
    monthly_completed = {}
    for i in range(12, -1, -1):
        dt = datetime.datetime.now(THAILAND_TZ) - datetime.timedelta(days=i*30)
        monthly_completed[dt.strftime('%Y-%m')] = 0

    for task_item in tasks_raw:
        if task_item.get('status') == 'completed' and task_item.get('completed'):
            try:
                completed_dt = datetime.datetime.fromisoformat(task_item['completed'].replace('Z', '+00:00')).astimezone(THAILAND_TZ)
                key = completed_dt.strftime('%Y-%m')
                if key in monthly_completed:
                    monthly_completed[key] += 1
            except (ValueError, TypeError):
                continue
    
    sorted_months = sorted(monthly_completed.keys())
    chart_labels = [datetime.datetime.strptime(m, '%Y-%m').strftime('%b %y') for m in sorted_months]
    chart_values = [monthly_completed[m] for m in sorted_months]
    chart_data = {'labels': chart_labels, 'values': chart_values}

    return render_template('dashboard.html',
                           tasks=final_tasks,
                           summary=summary_stats,
                           search_query=search_query,
                           status_filter=status_filter,
                           chart_data=chart_data)

@liff_bp.route('/customer/<customer_task_id>')
def customer_profile(customer_task_id):
    """แสดงหน้าโปรไฟล์และประวัติงานทั้งหมดของลูกค้า (เวอร์ชันแก้ไขล่าสุด)"""
    customer_task = get_single_task(customer_task_id)
    if not customer_task:
        abort(404)

    profile_data = parse_task_data(customer_task)
    
    total_spent = 0
    # คำนวณยอดใช้จ่ายรวมจากทุกใบงานในโปรไฟล์
    for job in profile_data.get('jobs', []):
        items = JobItem.query.filter_by(task_google_id=job.get('job_id')).all()
        total_spent += sum(item.quantity * item.unit_price for item in items)

    if len(profile_data.get('jobs', [])) == 1:
        task_details = profile_data
        
        # --- START: ✅ โค้ดที่แก้ไข ---
        # ดึงข้อมูล reports จาก object ของ job ใบเดียวที่อยู่ใน profile_data
        single_job_data = profile_data.get('jobs', [{}])[0]
        task_details['tech_reports_history'] = single_job_data.get('reports', [])
        # --- END: ✅ โค้ดที่แก้ไข ---
        
        settings = get_app_settings()
        
        return render_template(
            'customer_profile.html',
            profile=profile_data,
            task=task_details,
            customer_task_id=customer_task_id,
            total_jobs=1,
            total_spent=total_spent,
            technician_list=settings.get('technician_list', []),
            equipment_catalog=settings.get('equipment_catalog', []),
            progress_report_snippets=settings.get('technician_templates', {}).get('progress_reports', [])
        )

    return render_template(
        'customer_profile.html',
        profile=profile_data,
        task=None, 
        customer_task_id=customer_task_id,
        total_jobs=len(profile_data.get('jobs', [])),
        total_spent=total_spent
    )

@liff_bp.route('/customer/<customer_task_id>/job/<job_id>')
def job_details(customer_task_id, job_id):
    """แสดงรายละเอียดของใบงาน (Job Order) ใบเดียว (เวอร์ชันแก้ไข)"""
    customer_task = get_single_task(customer_task_id)
    if not customer_task:
        abort(404)

    profile_data = parse_customer_profile_from_task(customer_task)
    job_data = next((job for job in profile_data.get('jobs', []) if job.get('job_id') == job_id), None)

    if not job_data:
        abort(404)

    task_for_template = parse_task_data(customer_task) 
    task_for_template['id'] = job_id
    task_for_template['title'] = job_data.get('job_title')
    task_for_template['tech_reports_history'] = job_data.get('reports', []) 
    
    settings = get_app_settings()

    return render_template(
        'job_details.html',
        job=job_data,
        task=task_for_template,
        customer_info=profile_data.get('customer_info', {}),
        customer_task_id=customer_task_id,
        technician_list=settings.get('technician_list', []),
        equipment_catalog=settings.get('equipment_catalog', []),
        progress_report_snippets=settings.get('technician_templates', {}).get('progress_reports', [])
    )
    
@liff_bp.route('/api/customer/<customer_task_id>/job/<job_id>/update', methods=['POST'])
def api_update_job_report(customer_task_id, job_id):
    """(เวอร์ชันแก้ไขสมบูรณ์) API สำหรับอัปเดตข้อมูลในใบงานย่อย (Job Order)"""
    try:
        task_raw = get_single_task(customer_task_id)
        if not task_raw:
            return jsonify({'status': 'error', 'message': 'ไม่พบโปรไฟล์ลูกค้า'}), 404

        profile_data = parse_customer_profile_from_task(task_raw)
        job_to_update = next((job for job in profile_data.get('jobs', []) if job.get('job_id') == job_id), None)
        if not job_to_update:
            return jsonify({'status': 'error', 'message': 'ไม่พบใบงานที่ต้องการอัปเดต'}), 404

        action = request.form.get('action')
        new_attachments_json = request.form.get('uploaded_attachments_json')
        new_attachments = json.loads(new_attachments_json) if new_attachments_json else []
        liff_user_id = request.form.get('technician_line_user_id')

        flash_message = "อัปเดตข้อมูลเรียบร้อยแล้ว"
        report_data = {'summary_date': datetime.datetime.now(THAILAND_TZ).isoformat()}
        is_internal_note = request.form.get('is_internal_note') == 'on'
        
        technicians_report_str = request.form.get('technicians_report', '')
        technicians = [t.strip() for t in technicians_report_str.split(',') if t.strip()]

        if not technicians and liff_user_id:
            settings = get_app_settings()
            tech_info = next((tech for tech in settings.get('technician_list', []) if tech.get('line_user_id') == liff_user_id), None)
            if tech_info:
                technicians = [tech_info['name']]
        if not technicians:
            technicians = ["ไม่ระบุชื่อ"]

        # --- START: ✅ โค้ดที่แก้ไข ---
        # ตัวแปรสำหรับส่งไปอัปเดต Google Task โดยตรง
        task_update_kwargs = {}

        if action == 'complete_task':
            job_to_update['status'] = 'completed'
            job_to_update['completed_date'] = datetime.datetime.now(pytz.utc).isoformat()
            flash_message = 'ปิดงานสำเร็จ!'
            
            # ตรวจสอบว่าทุก Job ในโปรไฟล์เสร็จสิ้นแล้วหรือยัง
            all_jobs_completed = all(j.get('status') == 'completed' for j in profile_data.get('jobs', []))
            if all_jobs_completed:
                task_update_kwargs['status'] = 'completed'
                flash_message += ' (งานหลักถูกปิดแล้ว)'

            report_data.update({
                'type': 'report',
                'work_summary': str(request.form.get('work_summary', '')).strip(),
                'technicians': technicians, 'is_internal': is_internal_note, 'liff_user_id': liff_user_id
            })

        elif action == 'reschedule_task':
            new_due_str = request.form.get('reschedule_due')
            if new_due_str:
                dt_local = THAILAND_TZ.localize(date_parse(new_due_str))
                utc_due_date = dt_local.astimezone(pytz.utc).isoformat()
                job_to_update['due_date'] = utc_due_date
                task_update_kwargs['due'] = utc_due_date.replace('+00:00', 'Z')
                
                # ถ้างานเคยเสร็จแล้ว ให้เปลี่ยนสถานะกลับเป็น "ยังไม่เสร็จ"
                if task_raw.get('status') == 'completed':
                    task_update_kwargs['status'] = 'needsAction'
            
            flash_message = 'เลื่อนนัดเรียบร้อยแล้ว'
            report_data.update({
                'type': 'reschedule',
                'reason': str(request.form.get('reschedule_reason', '')).strip(),
                'technicians': technicians, 'is_internal': is_internal_note, 'liff_user_id': liff_user_id
            })

        elif action == 'save_report':
            flash_message = 'เพิ่มรายงานความคืบหน้าเรียบร้อยแล้ว!'
            report_data.update({
                'type': 'report',
                'work_summary': str(request.form.get('work_summary', '')).strip(),
                'technicians': technicians, 'is_internal': is_internal_note, 'liff_user_id': liff_user_id
            })
        # --- END: ✅ โค้ดที่แก้ไข ---
        
        report_data['attachments'] = new_attachments
        
        if 'reports' not in job_to_update:
            job_to_update['reports'] = []
        job_to_update['reports'].append(report_data)

        if technicians:
            profile_data['assigned_technician'] = ", ".join(technicians)
        
        final_notes = json.dumps(profile_data, ensure_ascii=False, indent=2)
        
        if len(final_notes.encode('utf-8')) > 8000:
            while len(final_notes.encode('utf-8')) > 8000 and job_to_update.get('reports'):
                job_to_update['reports'].pop(0)
                final_notes = json.dumps(profile_data, ensure_ascii=False, indent=2)
        
        # --- START: ✅ โค้ดที่แก้ไข ---
        # ส่ง notes และ kwargs อื่นๆ (status, due) ไปพร้อมกัน
        if update_google_task(customer_task_id, notes=final_notes, **task_update_kwargs):
        # --- END: ✅ โค้ดที่แก้ไข ---
            cache.clear()
            return jsonify({'status': 'success', 'message': flash_message})
        else:
            return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกข้อมูลลง Google Tasks'}), 500

    except Exception as e:
        current_app.logger.error(f"Error in api_update_job_report: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดภายในเซิร์ฟเวอร์'}), 500  

@liff_bp.route('/summary/print')
def summary_print():
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    final_tasks = []
    
    for task_item in tasks_raw:
        parsed_task = parse_task_data(task_item)
        if not parsed_task:
            continue
            
        task_passes_filter = False
        if status_filter == 'all': task_passes_filter = True
        elif status_filter == 'completed' and parsed_task['status'] == 'completed': task_passes_filter = True
        elif status_filter == 'needsAction' and parsed_task['status'] == 'needsAction': task_passes_filter = True
        elif status_filter == 'today' and parsed_task['is_today']: task_passes_filter = True

        if task_passes_filter:
            customer_info = parsed_task.get('customer', {})
            searchable_text = f"{parsed_task.get('title', '')} {customer_info.get('name', '')} {customer_info.get('organization', '')} {customer_info.get('phone', '')}".lower()
            if not search_query or search_query in searchable_text:
                final_tasks.append(parsed_task)

    final_tasks.sort(key=lambda x: (x.get('status') == 'completed', x.get('due') is None, date_parse(x.get('due', '9999-12-31T23:59:59Z'))))
    
    return render_template("summary_print.html",
                           tasks=final_tasks,
                           search_query=search_query,
                           status_filter=status_filter,
                           now=datetime.datetime.now(THAILAND_TZ))

@liff_bp.route('/calendar')
def calendar_view():
    tasks_raw = get_google_tasks_for_report(show_completed=False) or []
    unscheduled_tasks = []
    for task_item in tasks_raw:
        if not task_item.get('due'):
            parsed_task = parse_task_data(task_item)
            unscheduled_tasks.append(parsed_task)
            
    unscheduled_tasks.sort(key=lambda x: x.get('created', ''), reverse=True)
    
    return render_template('calendar.html', unscheduled_tasks=unscheduled_tasks)

@liff_bp.route('/edit_task/<task_id>', methods=['GET', 'POST'])
def edit_task(task_id):
    task_raw = get_single_task(task_id)
    if not task_raw:
        abort(404)

    if request.method == 'POST':
        new_title = str(request.form.get('task_title', '')).strip()
        if not new_title:
            flash('กรุณากรอกรายละเอียดงาน', 'danger')
            return redirect(url_for('liff.edit_task', task_id=task_id))

        notes_lines = []
        organization_name = str(request.form.get('organization_name', '')).strip()
        if organization_name:
            notes_lines.append(f"หน่วยงาน: {organization_name}")

        notes_lines.extend([
            f"ลูกค้า: {str(request.form.get('customer_name', '')).strip()}",
            f"เบอร์โทรศัพท์: {str(request.form.get('customer_phone', '')).strip()}",
            f"ที่อยู่: {str(request.form.get('address', '')).strip()}",
        ])
        map_url = str(request.form.get('latitude_longitude', '')).strip()
        if map_url:
            notes_lines.append(map_url)
        
        new_base_notes = "\n".join(filter(None, notes_lines))

        original_notes = task_raw.get('notes', '')
        tech_reports, _ = parse_tech_report_from_notes(original_notes)
        feedback_data = parse_customer_feedback_from_notes(original_notes)
        
        all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in tech_reports])
        
        final_notes = new_base_notes
        if all_reports_text:
            final_notes += all_reports_text
        if feedback_data:
            final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

        due_date_gmt = None
        appointment_str = str(request.form.get('appointment_due', '')).strip()
        if appointment_str:
            try:
                dt_local = THAILAND_TZ.localize(date_parse(appointment_str))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
            except ValueError:
                flash('รูปแบบวันเวลานัดหมายไม่ถูกต้อง', 'warning')
                return redirect(url_for('liff.edit_task', task_id=task_id))

        if update_google_task(task_id, title=new_title, notes=final_notes, due=due_date_gmt):
            cache.clear()
            flash('บันทึกข้อมูลหลักของงานเรียบร้อยแล้ว!', 'success')
            return redirect(url_for('liff.customer_profile', customer_task_id=task_id))
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกข้อมูลหลัก', 'danger')
            return redirect(url_for('liff.edit_task', task_id=task_id))

    task = parse_task_data(task_raw)
    return render_template('edit_task.html', task=task)

@liff_bp.route('/technician_report')
def technician_report():
    now = datetime.datetime.now(THAILAND_TZ)
    try:
        year, month = int(request.args.get('year', now.year)), int(request.args.get('month', now.month))
    except (ValueError, TypeError):
        year, month = now.year, now.month

    months = [{'value': i, 'name': datetime.date(2000, i, 1).strftime('%B')} for i in range(1, 13)]
    report_data, technician_list = get_technician_report_data(year, month)

    return render_template('technician_report.html',
                        report_data=report_data,
                        selected_year=year,
                        selected_month=month,
                        years=list(range(now.year - 5, now.year + 2)),
                        months=months,
                        technician_list=technician_list)

@liff_bp.route('/technician_report/print')
def technician_report_print():
    now = datetime.datetime.now(THAILAND_TZ)
    try:
        year, month = int(request.args.get('year', now.year)), int(request.args.get('month', now.month))
    except (ValueError, TypeError):
        year, month = now.year, now.month

    sorted_report, technician_list = get_technician_report_data(year, month)

    return render_template('technician_report_print.html',
                        report_data=sorted_report,
                        selected_year=year,
                        selected_month=month,
                        now=datetime.datetime.now(THAILAND_TZ),
                        technician_list=technician_list)

@liff_bp.route('/technician/my_stock')
def technician_stock_view():
    """แสดงหน้า LIFF สำหรับให้ช่างดูสต็อกของตัวเอง"""
    return render_template(
        'technician_stock_view.html',
        LIFF_ID_FORM=LIFF_ID_FORM
    )

@liff_bp.route('/products')
def product_management():
    settings = get_app_settings()
    equipment_catalog = settings.get('equipment_catalog', [])
    return render_template('product_management.html',
                           equipment_catalog=equipment_catalog)

@liff_bp.route('/public/report/<task_id>')
def public_task_report(task_id):
    task_raw = get_single_task(task_id)
    if not task_raw or task_raw.get('status') != 'completed':
        abort(404)

    task = parse_task_data(task_raw)
    task['tech_reports_history'], _ = parse_tech_report_from_notes(task.get('notes', ''))
    
    task['tech_reports_history'] = [
        r for r in task['tech_reports_history']
        if r.get('work_summary') or r.get('attachments')
    ]

    response = make_response(render_template('public_report.html', task=task))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@liff_bp.route('/generate_public_report_qr/<task_id>')
def generate_public_report_qr(task_id):
    task = get_single_task(task_id)
    if not task:
        abort(404)
    
    settings = get_app_settings()
    line_oa_id = settings.get('shop_info', {}).get('line_id', '@YOUR_LINE_OA_ID').replace('@','')
    line_add_friend_url = f"https://line.me/R/ti/p/@{line_oa_id}?referral={task_id}"
    
    qr_code_b64 = generate_qr_code_base64(line_add_friend_url)
    customer = parse_task_data(task).get('customer', {})
    
    return render_template('generate_onboarding_qr.html',
                           qr_code_base64=qr_code_b64,
                           liff_url=line_add_friend_url,
                           task=task,
                           customer_info=customer,
                           now=datetime.datetime.now(THAILAND_TZ))

@liff_bp.route('/form')
def form_page():
    settings = get_app_settings()
    technician_templates = settings.get('technician_templates', {})
    return render_template('form.html',
                           task_detail_snippets=technician_templates.get('task_details', []))

@liff_bp.route('/external_job_form')
def external_job_form_page():
    customer_task_id = request.args.get('customer_task_id')
    prefill_data = None
    if customer_task_id:
        task_raw = get_single_task(customer_task_id)
        if task_raw:
            profile = parse_task_data(task_raw)
            prefill_data = profile.get('customer')
            
    return render_template('external_job_form.html', prefill_data=prefill_data)

@liff_bp.route('/task/<task_id>')
def task_details(task_id):
    return redirect(url_for('liff.customer_profile', customer_task_id=task_id), code=301)

@liff_bp.route('/customer_problem_form/<task_id>')
def customer_problem_form(task_id):
    task_raw = get_single_task(task_id)
    if not task_raw:
        abort(404)
    task = parse_task_data(task_raw)
    return render_template('customer_problem_form.html', task=task, LIFF_ID_FORM=LIFF_ID_FORM)

@liff_bp.route('/generate_onboarding_qr/<task_id>')
def generate_customer_onboarding_qr(task_id):
    task = get_single_task(task_id)
    if not task:
        abort(404)
    
    settings = get_app_settings()
    line_oa_id = settings.get('shop_info', {}).get('line_id', '@YOUR_LINE_OA_ID').replace('@','')
    line_add_friend_url = f"https://line.me/R/ti/p/@{line_oa_id}?referral={task_id}"
    
    qr_code_b64 = generate_qr_code_base64(line_add_friend_url)
    customer = parse_task_data(task).get('customer', {})
    
    return render_template('generate_onboarding_qr.html',
                           qr_code_base64=qr_code_b64,
                           liff_url=line_add_friend_url,
                           task=task,
                           customer_info=customer,
                           now=datetime.datetime.now(THAILAND_TZ))

@liff_bp.route('/liff_notification_popup')
def liff_notification_popup():
    return render_template('liff_notification_popup.html', LIFF_ID_FORM=LIFF_ID_FORM)

@liff_bp.route('/open_in_line')
def open_in_line():
    return render_template('open_in_line.html')

@liff_bp.route('/technician/update_location')
def technician_location_update_page():
    return render_template('technician_location_update.html', LIFF_ID_TECHNICIAN_LOCATION=LIFF_ID_TECHNICIAN_LOCATION)

@liff_bp.route('/stock')
def stock_management():
    """แสดงหน้าสำหรับจัดการสต็อกสินค้าทั้งหมด"""
    settings = get_app_settings()
    products = settings.get('equipment_catalog', [])
    warehouses = Warehouse.query.order_by(Warehouse.id).all()
    
    stock_levels_raw = StockLevel.query.all()
    stock_levels = {(sl.product_code, sl.warehouse_id): sl.quantity for sl in stock_levels_raw}

    return render_template(
        'stock_management.html',
        products=products,
        warehouses=warehouses,
        stock_levels=stock_levels
    )

@liff_bp.route('/billing')
def billing_summary():
    search_query = request.args.get('search_query', '').strip().lower()
    
    # ดึงเฉพาะงานที่มีสถานะ completed
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    completed_tasks_raw = [t for t in tasks_raw if t.get('status') == 'completed']
    
    tasks_with_details = []
    summary_data = {'pending_billing_total': 0, 'billed_total': 0, 'paid_total': 0}

    for task_raw in completed_tasks_raw:
        task = parse_task_data(task_raw)
        customer_info = task.get('customer', {})
        searchable_text = f"{task.get('title', '')} {customer_info.get('name', '')}".lower()

        if search_query and search_query not in searchable_text:
            continue

        # --- START: ✅ โค้ดที่แก้ไข ---
        # วนลูปเพื่อดึง Job IDs ทั้งหมดที่อยู่ในโปรไฟล์ของลูกค้ารายนี้
        job_ids_in_profile = [job.get('job_id') for job in task.get('jobs', [])]
        
        total_cost = 0
        if job_ids_in_profile:
            # ดึงรายการ JobItem ทั้งหมดที่เกี่ยวข้องกับ Job IDs เหล่านั้น
            items = JobItem.query.filter(JobItem.task_google_id.in_(job_ids_in_profile)).all()
            total_cost = sum(item.quantity * item.unit_price for item in items)
        # --- END: ✅ โค้ดที่แก้ไข ---

        billing_status = BillingStatus.query.filter_by(task_google_id=task_raw['id']).first()
        if not billing_status:
            billing_status = BillingStatus(task_google_id=task_raw['id'])
            db.session.add(billing_status)
            db.session.commit()

        task['total_cost'] = total_cost
        task['billing_status'] = billing_status.status
        task['billing_status_details'] = billing_status.to_dict()
        
        feedback_data = parse_customer_feedback_from_notes(task_raw.get('notes', ''))
        task['customer_line_id'] = feedback_data.get('customer_line_user_id', '')
        
        tasks_with_details.append(task)
        
        if billing_status.status == 'pending_billing':
            summary_data['pending_billing_total'] += total_cost
        elif billing_status.status in ['billed', 'overdue']:
            summary_data['billed_total'] += total_cost
        elif billing_status.status == 'paid':
            summary_data['paid_total'] += total_cost

    tasks_with_details.sort(key=lambda x: x.get('completed', '0'), reverse=True)

    return render_template('billing_summary.html', tasks=tasks_with_details, summary=summary_data, search_query=search_query)

@liff_bp.route('/api/billing/<task_id>/update_status', methods=['POST'])
def update_billing_status(task_id):
    data = request.json
    new_status = data.get('status')
    
    if not new_status:
        return jsonify({'status': 'error', 'message': 'ไม่พบสถานะใหม่'}), 400

    billing_record = BillingStatus.query.filter_by(task_google_id=task_id).first()
    if not billing_record:
        billing_record = BillingStatus(task_google_id=task_id)
        db.session.add(billing_record)

    billing_record.status = new_status
    if new_status == 'billed' and not billing_record.billed_date:
        billing_record.billed_date = datetime.datetime.utcnow()
    elif new_status == 'paid' and not billing_record.paid_date:
        billing_record.paid_date = datetime.datetime.utcnow()

    try:
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'อัปเดตสถานะสำเร็จ'})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating billing status for task {task_id}: {e}")
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกข้อมูล'}), 500     
    
@liff_bp.route('/api/billing/batch_update', methods=['POST'])
def api_billing_batch_update():
    from app import BillingStatus, db
    
    data = request.json
    task_ids = data.get('task_ids', [])
    new_status = data.get('status')
    
    if not task_ids or not new_status:
        return jsonify({'status': 'error', 'message': 'ข้อมูลไม่ครบถ้วน'}), 400

    try:
        records_to_update = BillingStatus.query.filter(BillingStatus.task_google_id.in_(task_ids)).all()
        
        updated_count = 0
        for record in records_to_update:
            record.status = new_status
            if new_status == 'paid' and not record.paid_date:
                record.paid_date = datetime.datetime.utcnow()
            updated_count += 1
            
        db.session.commit()
        return jsonify({'status': 'success', 'message': f'อัปเดตสถานะสำเร็จ {updated_count} รายการ'})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error during batch billing update: {e}")
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกข้อมูล'}), 500

def create_invoice_flex_message(task, total_cost, invoice_url):
    """สร้าง Flex Message สำหรับส่งใบแจ้งหนี้ให้ลูกค้า"""
    customer_name = task['customer'].get('name', 'N/A')
    task_title_short = (task['title'][:30] + '..') if len(task['title']) > 30 else task['title']
    
    flex_json_payload = {
      "type": "bubble",
      "header": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "ใบแจ้งค่าบริการ (Invoice)",
            "weight": "bold",
            "color": "#FFFFFF",
            "size": "lg"
          }
        ],
        "backgroundColor": "#28a745",
        "paddingAll": "20px"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": f"เรียน คุณ{customer_name}",
            "weight": "bold",
            "size": "md",
            "margin": "md"
          },
          {
            "type": "text",
            "text": "บริษัทฯ ขอส่งใบแจ้งค่าบริการสำหรับงานซ่อมของท่าน ดังรายละเอียดในไฟล์เอกสารแนบ",
            "wrap": True,
            "size": "sm",
            "margin": "md"
          },
          {
            "type": "separator",
            "margin": "xl"
          },
          {
            "type": "box",
            "layout": "vertical",
            "margin": "lg",
            "spacing": "sm",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {"type": "text", "text": "ชื่องาน", "color": "#aaaaaa", "size": "sm", "flex": 2},
                  {"type": "text", "text": task_title_short, "wrap": True, "color": "#666666", "size": "sm", "flex": 5}
                ]
              },
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {"type": "text", "text": "ยอดชำระ", "color": "#aaaaaa", "size": "sm", "flex": 2},
                  {"type": "text", "text": f"{total_cost:,.2f} บาท", "wrap": True, "color": "#666666", "size": "sm", "flex": 5, "weight": "bold"}
                ]
              }
            ]
          }
        ]
      },
      "footer": {
        "type": "box",
        "layout": "vertical",
        "spacing": "sm",
        "contents": [
          {
            "type": "button",
            "style": "primary",
            "height": "sm",
            "action": {
              "type": "uri",
              "label": "📄 ดาวน์โหลดใบแจ้งหนี้ (PDF)",
              "uri": invoice_url
            },
            "color": "#198754"
          }
        ],
        "flex": 0
      }
    }
    return FlexMessage(alt_text=f"ใบแจ้งค่าบริการสำหรับงาน {task_title_short}", contents=flex_json_payload)

@liff_bp.route('/api/billing/<task_id>/send_invoice', methods=['POST'])
def send_invoice_to_customer(task_id):
    """API สำหรับสร้างและส่งใบแจ้งหนี้ PDF ให้ลูกค้าทาง LINE (เวอร์ชันแก้ไข)"""
    data = request.json
    recipient_id = data.get('recipient_id')

    task_raw = get_single_task(task_id)
    if not task_raw:
        return jsonify({'status': 'error', 'message': 'ไม่พบข้อมูลงาน'}), 404

    if not recipient_id:
        feedback_data = parse_customer_feedback_from_notes(task_raw.get('notes', ''))
        recipient_id = feedback_data.get('customer_line_user_id')
    
    if not recipient_id:
        return jsonify({'status': 'error', 'message': 'ไม่พบผู้รับ LINE ID, กรุณากรอก ID ผู้รับ'}), 404

    task = parse_task_data(task_raw)
    items = JobItem.query.filter_by(task_google_id=task_id).order_by(JobItem.added_at.asc()).all()
    total_cost = sum(item.quantity * item.unit_price for item in items)
    settings = get_app_settings()

    subtotal = total_cost / 1.07
    vat = total_cost - subtotal
    try:
        total_cost_in_words = num2words(total_cost, to='currency', lang='th')
    except Exception:
        total_cost_in_words = "ไม่สามารถแปลงเป็นตัวอักษรได้"
    
    logo_path = os.path.join(current_app.root_path, 'static', 'logo.png')
    
    invoice_html = render_template('invoice_template.html',
                                   task=task,
                                   items=items,
                                   total_cost=total_cost,
                                   subtotal=subtotal,
                                   vat=vat,
                                   total_cost_in_words=total_cost_in_words,
                                   settings=settings,
                                   now=datetime.datetime.now(THAILAND_TZ),
                                   logo_path=logo_path)

    pdf_bytes = HTML(string=invoice_html).write_pdf()
    pdf_file = BytesIO(pdf_bytes)
    pdf_filename = f"Invoice-{task['id'][-6:].upper()}-{task['customer'].get('name', 'customer')}.pdf".replace(" ", "_")

    invoices_folder_id = find_or_create_drive_folder("Invoices", os.environ.get('GOOGLE_DRIVE_FOLDER_ID'))
    if not invoices_folder_id:
        return jsonify({'status': 'error', 'message': 'ไม่สามารถสร้างโฟลเดอร์ Invoices บน Drive ได้'}), 500

    drive_file_info = upload_data_from_memory_to_drive(pdf_file, pdf_filename, 'application/pdf', invoices_folder_id)
    if not drive_file_info or 'webViewLink' not in drive_file_info:
        return jsonify({'status': 'error', 'message': 'ไม่สามารถอัปโหลดใบแจ้งหนี้ไปยัง Google Drive ได้'}), 500
    
    invoice_url = drive_file_info['webViewLink']

    flex_message = create_invoice_flex_message(task, total_cost, invoice_url)
    message_queue.add_message(recipient_id, [flex_message])
    
    billing_record = BillingStatus.query.filter_by(task_google_id=task_id).first()
    if billing_record and billing_record.status == 'pending_billing':
        billing_record.status = 'billed'
        billing_record.billed_date = datetime.datetime.utcnow()
        db.session.commit()
            
    return jsonify({'status': 'success', 'message': f'ส่งใบแจ้งหนี้ไปยัง {recipient_id} เรียบร้อยแล้ว'})
    
@liff_bp.route('/customer/<customer_task_id>/job/<job_id>/delete', methods=['POST'])
def delete_job_from_profile(customer_task_id, job_id):
    """ลบใบงานย่อย (Job) ออกจากโปรไฟล์ของลูกค้า"""
    try:
        task_raw = get_single_task(customer_task_id)
        if not task_raw:
            flash('ไม่พบโปรไฟล์ลูกค้าที่ต้องการแก้ไข', 'danger')
            return redirect(url_for('liff.summary'))

        profile_data = parse_customer_profile_from_task(task_raw)

        original_job_count = len(profile_data.get('jobs', []))
        profile_data['jobs'] = [job for job in profile_data.get('jobs', []) if job.get('job_id') != job_id]

        if len(profile_data['jobs']) == original_job_count:
            flash(f'ไม่พบใบงาน ID: {job_id} ในโปรไฟล์นี้', 'warning')
            return redirect(url_for('liff.customer_profile', customer_task_id=customer_task_id))

        final_notes = json.dumps(profile_data, ensure_ascii=False, indent=2)
        if update_google_task(customer_task_id, notes=final_notes):
            cache.clear()
            flash('ลบใบงานย่อยออกจากโปรไฟล์เรียบร้อยแล้ว!', 'success')
            return redirect(url_for('liff.customer_profile', customer_task_id=customer_task_id))
        else:
            raise Exception("Failed to update Google Task.")
    except Exception as e:
        current_app.logger.error(f"Error deleting job {job_id} from task {customer_task_id}: {e}", exc_info=True)
        flash('เกิดข้อผิดพลาดในการลบใบงาน', 'danger')
        return redirect(url_for('liff.summary'))

@liff_bp.route('/customer/<customer_task_id>/job/<job_id>/edit_report/<int:report_index>', methods=['POST'])
def edit_report_attachments(customer_task_id, job_id, report_index):
    """(เวอร์ชันใหม่) แก้ไขไฟล์แนบในรายงานของใบงานย่อย (Job)"""
    try:
        task_raw = get_single_task(customer_task_id)
        if not task_raw:
            flash('ไม่พบโปรไฟล์ลูกค้า', 'danger')
            return redirect(url_for('liff.summary'))

        profile_data = parse_customer_profile_from_task(task_raw)
        job_index = next((i for i, job in enumerate(profile_data.get('jobs', [])) if job.get('job_id') == job_id), -1)

        if job_index == -1 or not (0 <= report_index < len(profile_data['jobs'][job_index].get('reports', []))):
            flash('ไม่พบรายงานที่ต้องการแก้ไข', 'danger')
            return redirect(url_for('liff.customer_profile', customer_task_id=customer_task_id))

        report_to_edit = profile_data['jobs'][job_index]['reports'][report_index]
        attachments_to_keep_ids = request.form.getlist('attachments_to_keep')
        original_attachments = report_to_edit.get('attachments', [])
        updated_attachments = [att for att in original_attachments if att['id'] in attachments_to_keep_ids]

        drive_service = get_google_drive_service()
        if drive_service:
            for att in original_attachments:
                if att['id'] not in attachments_to_keep_ids:
                    try:
                        _execute_google_api_call_with_retry(drive_service.files().delete, fileId=att['id'])
                    except Exception as e:
                        current_app.logger.error(f"Failed to delete attachment {att['id']}: {e}")

        # Placeholder for new file upload logic
        # new_files = request.files.getlist('new_files[]')
        # if new_files: ...

        profile_data['jobs'][job_index]['reports'][report_index]['attachments'] = updated_attachments

        final_notes = json.dumps(profile_data, ensure_ascii=False, indent=2)
        if update_google_task(customer_task_id, notes=final_notes):
            cache.clear()
            flash('แก้ไขรูปภาพในรายงานเรียบร้อยแล้ว!', 'success')
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกการเปลี่ยนแปลง', 'danger')
    except Exception as e:
        current_app.logger.error(f"Error editing report attachments: {e}", exc_info=True)
        flash('เกิดข้อผิดพลาดร้ายแรง', 'danger')

    return redirect(url_for('liff.job_details', customer_task_id=customer_task_id, job_id=job_id))      

@liff_bp.route('/api/generate_invoice_pdf/<task_id>')
def generate_invoice_pdf(task_id):
    """สร้างไฟล์ PDF ของใบแจ้งหนี้"""
    # This function would need to be implemented fully
    # For now, it's a placeholder
    return "PDF generation not fully implemented", 501

@liff_bp.route('/invoice/<task_id>/print')
def print_invoice(task_id):
    """แสดงหน้าใบแจ้งหนี้สำหรับพิมพ์"""
    task_raw = get_single_task(task_id)
    if not task_raw:
        abort(404)

    task = parse_task_data(task_raw)
    items = JobItem.query.filter_by(task_google_id=task_id).order_by(JobItem.added_at.asc()).all()
    
    total_cost = sum(item.quantity * item.unit_price for item in items)
    subtotal = total_cost / 1.07
    vat = total_cost - subtotal

    try:
        total_cost_in_words = num2words(total_cost, to='currency', lang='th')
    except Exception:
        total_cost_in_words = "ไม่สามารถแปลงเป็นตัวอักษรได้"

    settings = get_app_settings()
    
    logo_path = os.path.join(current_app.root_path, 'static', 'logo.png')

    return render_template('invoice_template.html',
                           task=task,
                           items=items,
                           total_cost=total_cost,
                           subtotal=subtotal,
                           vat=vat,
                           total_cost_in_words=total_cost_in_words,
                           settings=settings,
                           now=datetime.datetime.now(THAILAND_TZ),
                           logo_path=logo_path)
                           
@liff_bp.route('/activity_feed')
def activity_feed():
    """แสดงหน้าสรุปความเคลื่อนไหวของงานทั้งหมด"""
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    settings = get_app_settings()
    technician_list = settings.get('technician_list', [])
    activities = []

    action_map = {
        'report': 'ได้เพิ่มรายงาน',
        'internal_note': 'ได้เพิ่มบันทึกภายใน',
        'reschedule': 'ได้เลื่อนนัดหมาย'
    }

    for task_raw in tasks_raw:
        task = parse_task_data(task_raw)
        customer_task_id = task.get('id')
        task_title_safe = task.get('title', 'N/A')
        customer_name = task.get('customer', {}).get('name', 'N/A')
        correct_task_url = url_for('liff.customer_profile', customer_task_id=customer_task_id)

        common_data = {
            'task_id': customer_task_id,
            'task_title': task_title_safe,
            'customer': customer_name,
            'task_url': correct_task_url
        }

        if task.get('created'):
            created_dt = date_parse(task.get('created'))
            activities.append({
                **common_data,
                'timestamp': created_dt.astimezone(THAILAND_TZ),
                'type': 'new_task',
                'action_text': 'สร้างงานใหม่',
                'technician': 'System',
            })
            
        if task.get('status') == 'completed' and task.get('completed'):
            completed_dt = date_parse(task.get('completed'))
            activities.append({
                **common_data,
                'timestamp': completed_dt.astimezone(THAILAND_TZ),
                'type': 'completed',
                'action_text': 'ปิดงาน',
                'technician': 'System',
            })

        history, _ = parse_tech_report_from_notes(task.get('notes', ''))
        for report in history:
            report_type = 'internal_note' if report.get('is_internal') else report.get('type', 'report')
            technicians = report.get('technicians', ['N/A'])
            
            report_dt = date_parse(report.get('summary_date'))
            report_dt = report_dt.astimezone(THAILAND_TZ) if report_dt.tzinfo else THAILAND_TZ.localize(report_dt)

            for tech_name in technicians:
                 activities.append({
                    **common_data,
                    'timestamp': report_dt,
                    'type': report_type,
                    'action_text': action_map.get(report_type, 'อัปเดตงาน'),
                    'technician': tech_name,
                })

    activities.sort(key=lambda x: x['timestamp'], reverse=True)
    grouped_activities = []
    for date, items in groupby(activities, key=lambda x: x['timestamp'].date()):
        grouped_activities.append((date, list(items)))

    return render_template(
        'activity_feed.html', 
        grouped_activities=grouped_activities,
        technician_list=technician_list,
        timedelta=timedelta
    )

@liff_bp.route('/api/customer/<customer_task_id>/job/<job_id>/delete_report/<int:report_index>', methods=['POST'])
def delete_job_report(customer_task_id, job_id, report_index):
    """(ฟังก์ชันใหม่) ลบรายงาน (Report) ออกจากใบงานย่อย (Job)"""
    try:
        task_raw = get_single_task(customer_task_id)
        if not task_raw:
            return jsonify({'status': 'error', 'message': 'ไม่พบโปรไฟล์ลูกค้า'}), 404

        profile_data = parse_customer_profile_from_task(task_raw)
        job_index = next((i for i, job in enumerate(profile_data.get('jobs', [])) if job.get('job_id') == job_id), -1)

        if job_index == -1 or not (0 <= report_index < len(profile_data['jobs'][job_index].get('reports', []))):
            return jsonify({'status': 'error', 'message': 'ไม่พบรายงานที่ต้องการลบ'}), 404

        report_to_delete = profile_data['jobs'][job_index]['reports'].pop(report_index)
        
        if report_to_delete.get('attachments'):
            drive_service = get_google_drive_service()
            if drive_service:
                for att in report_to_delete['attachments']:
                    try:
                        _execute_google_api_call_with_retry(drive_service.files().delete, fileId=att['id'])
                        current_app.logger.info(f"Deleted attachment {att['id']} from Drive.")
                    except Exception as e:
                        current_app.logger.error(f"Failed to delete attachment {att['id']}: {e}")

        final_notes = json.dumps(profile_data, ensure_ascii=False, indent=2)
        if update_google_task(customer_task_id, notes=final_notes):
            cache.clear()
            return jsonify({'status': 'success', 'message': 'ลบรายงานเรียบร้อยแล้ว'})
        else:
            profile_data['jobs'][job_index]['reports'].insert(report_index, report_to_delete)
            return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกข้อมูลหลังลบรายงาน'}), 500

    except Exception as e:
        current_app.logger.error(f"Error deleting job report: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดร้ายแรงฝั่งเซิร์ฟเวอร์'}), 500