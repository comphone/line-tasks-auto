# File: liff_views.py (ฉบับแก้ไขสมบูรณ์)

import datetime
import pytz
from flask import (
    Blueprint, render_template, request, abort,
    redirect, url_for, make_response, current_app
)
from functools import wraps
from dateutil.parser import parse as date_parse

# --- VVV [การเปลี่ยนแปลงที่สำคัญที่สุด] VVV ---
# 1. Import ฟังก์ชัน Helper ทั้งหมดจาก utils.py
from utils import (
    get_single_task,
    parse_google_task_dates,
    parse_customer_info_from_notes,
    parse_tech_report_from_notes,
    parse_customer_feedback_from_notes,
    get_app_settings,
    generate_qr_code_base64,
    get_google_tasks_for_report
)
# 2. Import ค่าคงที่จาก config.py
from config import TEXT_SNIPPETS, THAILAND_TZ
# --- ^^^ สิ้นสุดการเปลี่ยนแปลง ^^^ ---


# สร้าง Blueprint สำหรับ LIFF views
liff_bp = Blueprint('liff', __name__)


@liff_bp.route("/summary")
def summary():
    """
    หน้าสรุปงาน (Dashboard) ที่จะแสดงผลใน LIFF
    """
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    today_thai = datetime.datetime.now(THAILAND_TZ).date()
    final_tasks = []
    stats = {'needsAction': 0, 'completed': 0, 'overdue': 0, 'total': len(tasks_raw), 'today': 0, 'external': 0}

    for task in tasks_raw:
        task_status = task.get('status', 'needsAction')
        is_overdue = False
        is_today = False
        if task_status == 'needsAction' and task.get('due'):
            try:
                due_dt_utc = date_parse(task['due'])
                due_dt_local = due_dt_utc.astimezone(THAILAND_TZ)
                if due_dt_local.date() < today_thai:
                    is_overdue = True
                elif due_dt_local.date() == today_thai:
                    is_today = True
            except (ValueError, TypeError):
                pass
        
        is_external_job = task.get('title', '').startswith(('[งานเคลม]', '[งานภายนอก]'))

        if task_status == 'completed':
            stats['completed'] += 1
        else:
            stats['needsAction'] += 1
            if is_overdue:
                stats['overdue'] += 1
            if is_today:
                stats['today'] += 1
        if is_external_job:
            stats['external'] += 1

        task_passes_filter = False
        if status_filter == 'all': task_passes_filter = True
        elif status_filter == 'completed' and task_status == 'completed': task_passes_filter = True
        elif status_filter == 'needsAction' and task_status == 'needsAction': task_passes_filter = True
        elif status_filter == 'today' and is_today: task_passes_filter = True
        elif status_filter == 'external' and is_external_job: task_passes_filter = True
        
        if task_passes_filter:
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            searchable_text = f"{task.get('title', '')} {customer_info.get('name', '')} {customer_info.get('organization', '')} {customer_info.get('phone', '')}".lower()

            if not search_query or search_query in searchable_text:
                parsed_task = parse_google_task_dates(task)
                parsed_task['customer'] = customer_info
                parsed_task['is_overdue'] = is_overdue
                parsed_task['is_today'] = is_today
                final_tasks.append(parsed_task)

    final_tasks.sort(key=lambda x: (x.get('status') == 'completed', x.get('due') is None, date_parse(x.get('due', '9999-12-31T23:59:59Z'))))
    
    return render_template("dashboard.html",
                           tasks=final_tasks, 
                           summary=stats,
                           search_query=search_query, 
                           status_filter=status_filter,
                           LIFF_ID_TO_USE=current_app.config.get('LIFF_ID_FORM'))


@liff_bp.route('/task/<task_id>')
def task_details(task_id):
    """
    หน้ารายละเอียดงาน ที่จะแสดงผลใน LIFF
    """
    task_raw = get_single_task(task_id)
    if not task_raw:
        abort(404)
    
    task = parse_google_task_dates(task_raw)
    notes = task.get('notes', '')
    task['customer'] = parse_customer_info_from_notes(notes)
    task['tech_reports_history'], _ = parse_tech_report_from_notes(notes)
    task['customer_feedback'] = parse_customer_feedback_from_notes(notes)
    
    task['is_overdue'] = False
    task['is_today'] = False
    if task.get('status') == 'needsAction' and task.get('due'):
        try:
            due_dt_utc = date_parse(task['due'])
            due_dt_local = due_dt_utc.astimezone(THAILAND_TZ)
            today_thai = datetime.datetime.now(THAILAND_TZ).date()
            if due_dt_local.date() < today_thai: task['is_overdue'] = True
            elif due_dt_local.date() == today_thai: task['is_today'] = True
        except (ValueError, TypeError): pass
    
    app_settings = get_app_settings()
    all_attachments = [att for report in task['tech_reports_history'] for att in report.get('attachments', [])]

    response = make_response(render_template('update_task_details.html',
                                             task=task,
                                             technician_list=app_settings.get('technician_list', []),
                                             all_attachments=all_attachments,
                                             progress_report_snippets=TEXT_SNIPPETS.get('progress_reports', []),
                                             equipment_catalog=app_settings.get('equipment_catalog', []),
                                             LIFF_ID_TO_USE=current_app.config.get('LIFF_ID_FORM')))
                           
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@liff_bp.route('/form')
def form_page():
    """
    หน้าสำหรับสร้างงานใหม่
    """
    settings = get_app_settings()
    return render_template('form.html',
                           task_detail_snippets=TEXT_SNIPPETS.get('task_details', []),
                           technician_list=settings.get('technician_list', []),
                           LIFF_ID_TO_USE=current_app.config.get('LIFF_ID_FORM'))


@liff_bp.route('/external_claim/new/from/<ref_id>')
def create_external_claim_form(ref_id):
    """
    หน้าสำหรับสร้างงานเคลมโดยอ้างอิงจากงานเดิม
    """
    original_task_raw = get_single_task(ref_id)
    if not original_task_raw:
        abort(404)

    original_task = parse_google_task_dates(original_task_raw)
    original_task['customer'] = parse_customer_info_from_notes(original_task.get('notes', ''))
    
    history, _ = parse_tech_report_from_notes(original_task.get('notes', ''))
    all_equipment = [eq for report in history if isinstance(report.get('equipment_used'), list) for eq in report.get('equipment_used')]
    # ทำให้รายการอุปกรณ์ไม่ซ้ำกัน
    unique_equipment = list({v['item']: v for v in all_equipment}.values())

    return render_template('external_job_form.html', 
                           original_task=original_task, 
                           original_task_equipment=unique_equipment)


@liff_bp.route('/generate_customer_onboarding_qr/<task_id>')
def generate_customer_onboarding_qr(task_id):
    """
    สร้าง QR Code สำหรับให้ลูกค้าเพิ่มเพื่อนและเชื่อมต่อ
    """
    task = get_single_task(task_id)
    if not task:
        abort(404)

    line_oa_id = get_app_settings().get('shop_info', {}).get('line_id', '@your-oa-id')
    add_friend_url = f"https://line.me/R/ti/p/{line_oa_id}?referral={task_id}"
    
    qr_code = generate_qr_code_base64(add_friend_url)
    customer = parse_customer_info_from_notes(task.get('notes', ''))
    
    response = make_response(render_template('generate_onboarding_qr.html',
                                             qr_code_base64=qr_code,
                                             task=task,
                                             customer_info=customer,
                                             liff_url=add_friend_url,
                                             now=datetime.datetime.now(THAILAND_TZ)
                                             ))
    
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response

