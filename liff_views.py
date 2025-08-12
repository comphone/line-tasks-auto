import os
import datetime
import json
import pytz
from flask import Blueprint, render_template, request, abort, redirect, url_for, make_response
from functools import wraps

# สมมติว่าฟังก์ชันเหล่านี้ถูกย้ายไปที่ไฟล์ helpers.py หรือยังคงอยู่ใน app.py และถูก import มา
# ในที่นี้จะจำลองการ import ฟังก์ชันที่จำเป็นจาก app.py
from app import get_single_task, parse_google_task_dates, parse_customer_info_from_notes, \
                  parse_tech_report_from_notes, get_app_settings, TEXT_SNIPPETS, \
                  get_google_tasks_for_report, date_parse, parse_customer_feedback_from_notes, \
                  generate_qr_code_base64, LIFF_ID_FORM # <--- เพิ่ม LIFF_ID_FORM ตรงนี้

# --- การตั้งค่า LIFF และ Timezone ---
LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM')
LIFF_ID_TECHNICIAN_LOCATION = os.environ.get('LIFF_ID_TECHNICIAN_LOCATION')
THAILAND_TZ = pytz.timezone('Asia/Bangkok')

# --- สร้าง Blueprint ---
liff_bp = Blueprint('liff', __name__)

def liff_page(f):
    """
    Decorator สำหรับตรวจสอบว่า LIFF ID ถูกตั้งค่าแล้วหรือยัง
    และป้องกันการเข้าถึงหน้าที่ต้องใช้ LIFF โดยตรงจากเบราว์เซอร์
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # ตรวจสอบ User-Agent เพื่อดูว่าเปิดจาก LINE หรือไม่
        user_agent = request.headers.get('User-Agent', '')
        if "Line/" not in user_agent:
            # ถ้าไม่ได้เปิดใน LINE ให้แสดงหน้าแนะนำให้เปิดใน LINE
            return render_template('open_in_line.html')
        return f(*args, **kwargs)
    return decorated_function

@liff_bp.route("/")
def root_redirect_liff():
    """
    จัดการการ Redirect จาก liff.state เมื่อเปิด LIFF App
    """
    liff_state = request.args.get('liff.state')
    if liff_state:
        from urllib.parse import unquote
        decoded_path = unquote(liff_state)
        if decoded_path.startswith('/'):
            # ทำการ Redirect ไปยัง Path ที่ต้องการจริงๆ
            return redirect(decoded_path)
    # ถ้าไม่มี liff.state ให้ไปที่หน้า summary ตามปกติ
    return redirect(url_for('liff.summary'))

@liff_bp.route('/summary')
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
        
        is_external_job = task.get('title', '').startswith('[งานภายนอก]')

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
        if status_filter == 'all':
            task_passes_filter = True
        elif status_filter == 'completed' and task_status == 'completed':
            task_passes_filter = True
        elif status_filter == 'needsAction' and task_status == 'needsAction':
            task_passes_filter = True
        elif status_filter == 'today' and is_today:
            task_passes_filter = True
        elif status_filter == 'external' and is_external_job:
            task_passes_filter = True
        
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
    
    completed_tasks_for_chart = [t for t in tasks_raw if t.get('status') == 'completed' and t.get('completed')]
    month_labels = []
    chart_values = []
    for i in range(12):
        target_d = datetime.datetime.now(THAILAND_TZ) - datetime.timedelta(days=30 * (11 - i))
        month_key = target_d.strftime('%Y-%m')
        month_labels.append(target_d.strftime('%b %y'))
        count = sum(1 for task in completed_tasks_for_chart if date_parse(task['completed']).astimezone(THAILAND_TZ).strftime('%Y-%m') == month_key)
        chart_values.append(count)
    chart_data = {'labels': month_labels, 'values': chart_values}

    return render_template("dashboard.html",
                           tasks=final_tasks, summary=stats,
                           search_query=search_query, status_filter=status_filter,
                           chart_data=chart_data,
                           LIFF_ID_TO_USE=LIFF_ID_FORM)

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
            if due_dt_local.date() < today_thai:
                task['is_overdue'] = True
            elif due_dt_local.date() == today_thai:
                task['is_today'] = True
        except (ValueError, TypeError):
            pass
    
    app_settings = get_app_settings()
    
    all_attachments = []
    for report in task['tech_reports_history']:
        if report.get('attachments'):
            all_attachments.extend(report['attachments'])

    # --- ✅✅✅ START: ส่วนที่แก้ไข ✅✅✅ ---
    response = make_response(render_template('update_task_details.html',
                                             task=task,
                                             technician_list=app_settings.get('technician_list', []),
                                             all_attachments=all_attachments,
                                             progress_report_snippets=TEXT_SNIPPETS.get('progress_reports', []),
                                             equipment_catalog=app_settings.get('equipment_catalog', []), # <-- ส่ง catalog ไปที่ template
                                             LIFF_ID_TO_USE=LIFF_ID_FORM))
    # --- ✅✅✅ END: ส่วนที่แก้ไข ✅✅✅ ---
                           
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
    # แก้ไข return statement ให้เป็นดังนี้:
    return render_template('form.html',
                           # ส่งข้อมูล text_snippets แยกตามประเภทให้ถูกต้อง
                           task_detail_snippets=TEXT_SNIPPETS.get('task_details', []),
                           progress_report_snippets=TEXT_SNIPPETS.get('progress_reports', []),
                           technician_list=settings.get('technician_list', []),
                           LIFF_ID_TO_USE=LIFF_ID_FORM)

@liff_bp.route('/form/external')
def external_job_form_page():
    """
    หน้าสำหรับสร้างงานภายนอก
    """
    return render_template('external_job_form.html', LIFF_ID_TO_USE=LIFF_ID_FORM)


@liff_bp.route('/liff/technician/update_location')
def technician_location_liff_page():
    """
    หน้า LIFF สำหรับให้ช่างอัปเดตตำแหน่ง
    """
    # ไม่ต้องใช้ @liff_page เพราะหน้านี้มี logic การตรวจสอบของตัวเอง
    return render_template('technician_location_update.html', 
                           LIFF_ID_TECHNICIAN_LOCATION=LIFF_ID_TECHNICIAN_LOCATION)


@liff_bp.route('/liff_notification_popup')
def liff_notification_popup():
    """
    หน้า LIFF สำหรับแสดง Popup แจ้งเตือน
    """
    return render_template('liff_notification_popup.html', 
                           LIFF_ID_FORM=LIFF_ID_FORM)


@liff_bp.route('/generate_customer_onboarding_qr/<task_id>')
def generate_customer_onboarding_qr(task_id):
    """
    สร้าง QR Code สำหรับให้ลูกค้าเพิ่มเพื่อนและเชื่อมต่อ
    """
    task = get_single_task(task_id)
    if not task:
        abort(404)

    line_oa_id = os.environ.get('LINE_OA_ID', '@comphone') 
    add_friend_url = f"https://line.me/R/ti/p/{line_oa_id}?referral={task_id}"
    
    qr_code = generate_qr_code_base64(add_friend_url)
    customer = parse_customer_info_from_notes(task.get('notes', ''))
    
    response = make_response(render_template('generate_onboarding_qr.html',
                                             qr_code_base64=qr_code,
                                             task=task,
                                             customer_info=customer,
                                             liff_url=add_friend_url,
                                             LIFF_ID_FORM=LIFF_ID_FORM, # ส่ง LIFF_ID_FORM ไปด้วย
                                             now=datetime.datetime.now(THAILAND_TZ)
                                             ))
    
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response
