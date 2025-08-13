# liff_views.py
import os
import datetime
import pytz
import base64
import json
from flask import (
    Blueprint, render_template, request, url_for, abort, jsonify,
    current_app, redirect, flash, make_response
)
from dateutil.parser import parse as date_parse

# Import các hàm cần thiết từ app.py
from app import (
    get_google_tasks_for_report,
    get_single_task,
    parse_google_task_dates,
    parse_customer_info_from_notes,
    parse_tech_report_from_notes,
    get_app_settings,
    TEXT_SNIPPETS,
    generate_qr_code_base64,
    update_google_task,
    cache,
    _get_technician_report_data,
    LIFF_ID_FORM,
    LIFF_ID_TECHNICIAN_LOCATION
)

# Tạo Blueprint
liff_bp = Blueprint('liff', __name__)

THAILAND_TZ = pytz.timezone('Asia/Bangkok')

@liff_bp.route('/')
@liff_bp.route('/summary')
def summary():
    """Hiển thị trang dashboard/tóm tắt công việc"""
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    today_thai = datetime.date.today()

    summary_stats = {
        'total': 0, 'needsAction': 0, 'completed': 0,
        'overdue': 0, 'today': 0, 'external': 0
    }
    
    final_tasks = []
    
    for task in tasks_raw:
        summary_stats['total'] += 1
        task_status = task.get('status', 'needsAction')
        
        if task.get('title', '').lower().startswith('[งานภายนอก]'):
            summary_stats['external'] += 1

        is_overdue = False
        is_today = False
        if task_status == 'needsAction':
            summary_stats['needsAction'] += 1
            if task.get('due'):
                try:
                    due_dt_utc = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                    due_dt_local = due_dt_utc.astimezone(THAILAND_TZ)
                    if due_dt_local.date() < today_thai:
                        is_overdue = True
                        summary_stats['overdue'] += 1
                    elif due_dt_local.date() == today_thai:
                        is_today = True
                        summary_stats['today'] += 1
                except (ValueError, TypeError):
                    pass
        else:
            summary_stats['completed'] += 1
            
        task_passes_filter = False
        if status_filter == 'all': task_passes_filter = True
        elif status_filter == 'completed' and task_status == 'completed': task_passes_filter = True
        elif status_filter == 'needsAction' and task_status == 'needsAction': task_passes_filter = True
        elif status_filter == 'overdue' and is_overdue: task_passes_filter = True
        elif status_filter == 'today' and is_today: task_passes_filter = True
        elif status_filter == 'external' and task.get('title', '').lower().startswith('[งานภายนอก]'): task_passes_filter = True

        if task_passes_filter:
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            searchable_text = f"{task.get('title', '')} {customer_info.get('name', '')} {customer_info.get('organization', '')} {customer_info.get('phone', '')}".lower()
            if not search_query or search_query in searchable_text:
                parsed_task = parse_google_task_dates(task)
                parsed_task['customer'] = customer_info
                parsed_task['is_overdue'] = is_overdue
                parsed_task['is_today'] = is_today
                final_tasks.append(parsed_task)

    final_tasks.sort(key=lambda x: (x.get('status') == 'completed', x.get('due') is None, datetime.datetime.fromisoformat(x.get('due', '9999-12-31T23:59:59Z').replace('Z', '+00:00'))))

    # Tính toán dữ liệu biểu đồ
    monthly_completed = {}
    for i in range(12, -1, -1):
        dt = datetime.datetime.now(THAILAND_TZ) - datetime.timedelta(days=i*30)
        monthly_completed[dt.strftime('%Y-%m')] = 0

    for task in tasks_raw:
        if task.get('status') == 'completed' and task.get('completed'):
            try:
                completed_dt = datetime.datetime.fromisoformat(task['completed'].replace('Z', '+00:00')).astimezone(THAILAND_TZ)
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

# ➕ START: Các hàm đã được thêm vào lại
@liff_bp.route('/summary/print')
def summary_print():
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    today_thai = datetime.date.today()
    final_tasks = []
    
    for task in tasks_raw:
        task_status = task.get('status', 'needsAction')
        is_overdue = False
        is_today = False
        if task_status == 'needsAction' and task.get('due'):
            try:
                due_dt_utc = date_parse(task['due'])
                due_dt_local = due_dt_utc.astimezone(THAILAND_TZ)
                if due_dt_local.date() < today_thai: is_overdue = True
                elif due_dt_local.date() == today_thai: is_today = True
            except (ValueError, TypeError): pass
        
        task_passes_filter = False
        if status_filter == 'all': task_passes_filter = True
        elif status_filter == 'completed' and task_status == 'completed': task_passes_filter = True
        elif status_filter == 'needsAction' and task_status == 'needsAction': task_passes_filter = True
        elif status_filter == 'today' and is_today: task_passes_filter = True

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
    
    return render_template("summary_print.html",
                           tasks=final_tasks,
                           search_query=search_query,
                           status_filter=status_filter,
                           now=datetime.datetime.now(THAILAND_TZ))

@liff_bp.route('/calendar')
def calendar_view():
    tasks_raw = get_google_tasks_for_report(show_completed=False) or []
    unscheduled_tasks = []
    for task in tasks_raw:
        if not task.get('due'):
            parsed_task = parse_google_task_dates(task)
            parsed_task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
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
            return redirect(url_for('liff.task_details', task_id=task_id))
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกข้อมูลหลัก', 'danger')
            return redirect(url_for('liff.edit_task', task_id=task_id))

    task = parse_google_task_dates(task_raw)
    _, base_notes = parse_tech_report_from_notes(task_raw.get('notes', ''))
    task['customer'] = parse_customer_info_from_notes(base_notes)
    return render_template('edit_task.html', task=task)

@liff_bp.route('/technician_report')
def technician_report():
    now = datetime.datetime.now(THAILAND_TZ)
    try:
        year, month = int(request.args.get('year', now.year)), int(request.args.get('month', now.month))
    except (ValueError, TypeError):
        year, month = now.year, now.month

    months = [{'value': i, 'name': datetime.date(2000, i, 1).strftime('%B')} for i in range(1, 13)]
    report_data, technician_list = _get_technician_report_data(year, month)

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

    sorted_report, technician_list = _get_technician_report_data(year, month)

    return render_template('technician_report_print.html',
                        report_data=sorted_report,
                        selected_year=year,
                        selected_month=month,
                        now=datetime.datetime.now(THAILAND_TZ),
                        technician_list=technician_list)

@liff_bp.route('/public/report/<task_id>')
def public_task_report(task_id):
    task_raw = get_single_task(task_id)
    if not task_raw or task_raw.get('status') != 'completed':
        abort(404)

    task = parse_google_task_dates(task_raw)
    notes = task.get('notes', '')
    task['customer'] = parse_customer_info_from_notes(notes)
    task['tech_reports_history'], _ = parse_tech_report_from_notes(notes)
    
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

    public_report_url = url_for('liff.public_task_report', task_id=task['id'], _external=True)
    qr_code = generate_qr_code_base64(public_report_url)
    customer = parse_customer_info_from_notes(task.get('notes', ''))
    
    return render_template('public_report_qr.html',
                           qr_code_base64_report=qr_code,
                           task=task,
                           customer_info=customer,
                           public_report_url=public_report_url,
                           LIFF_ID_TECHNICIAN_LOCATION=LIFF_ID_TECHNICIAN_LOCATION,
                           now=datetime.datetime.now(THAILAND_TZ))
# ➕ END: Các hàm được thêm vào lại

@liff_bp.route('/form')
def form_page():
    """Hiển thị form để tạo công việc mới."""
    settings = get_app_settings()
    return render_template('form.html', 
                           task_detail_snippets=TEXT_SNIPPETS['task_details'])

@liff_bp.route('/external_job_form')
def external_job_form_page():
    """Hiển thị form để tạo công việc bên ngoài/công việc yêu cầu bồi thường."""
    from_task_id = request.args.get('from_task_id')
    original_task_data = None
    if from_task_id:
        task_raw = get_single_task(from_task_id)
        if task_raw:
            original_task_data = {
                'customer': parse_customer_info_from_notes(task_raw.get('notes', '')),
                'id': task_raw.get('id')
            }
    return render_template('external_job_form.html', original_task_data=original_task_data)

@liff_bp.route('/task/<task_id>')
def task_details(task_id):
    """Hiển thị thông tin chi tiết của một công việc."""
    task_raw = get_single_task(task_id)
    if not task_raw:
        abort(404)

    task = parse_google_task_dates(task_raw)
    notes = task.get('notes', '')
    
    task['customer'] = parse_customer_info_from_notes(notes)
    task['tech_reports_history'], _ = parse_tech_report_from_notes(notes)
    
    settings = get_app_settings()
    technician_list = settings.get('technician_list', [])
    equipment_catalog = settings.get('equipment_catalog', [])

    all_attachments = []
    if task['tech_reports_history']:
        for report in task['tech_reports_history']:
            if report.get('attachments'):
                all_attachments.extend(report['attachments'])

    return render_template('update_task_details.html',
                           task=task,
                           technician_list=technician_list,
                           all_attachments=all_attachments,
                           progress_report_snippets=TEXT_SNIPPETS['progress_reports'],
                           equipment_catalog=equipment_catalog,
                           LIFF_ID_TO_USE=LIFF_ID_FORM)

@liff_bp.route('/customer_problem_form/<task_id>')
def customer_problem_form(task_id):
    """Hiển thị form để khách hàng báo cáo vấn đề."""
    task = get_single_task(task_id)
    if not task:
        abort(404)
    task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
    return render_template('customer_problem_form.html', task=task, LIFF_ID_FORM=LIFF_ID_FORM)

@liff_bp.route('/generate_onboarding_qr/<task_id>')
def generate_customer_onboarding_qr(task_id):
    """Tạo mã QR để khách hàng thêm bạn LINE."""
    task = get_single_task(task_id)
    if not task:
        abort(404)
    
    settings = get_app_settings()
    line_oa_id = settings.get('shop_info', {}).get('line_id', '@YOUR_LINE_OA_ID').replace('@','')
    line_add_friend_url = f"https://line.me/R/ti/p/@{line_oa_id}?referral={task_id}"
    
    qr_code_b64 = generate_qr_code_base64(line_add_friend_url)
    customer = parse_customer_info_from_notes(task.get('notes', ''))
    
    return render_template('generate_onboarding_qr.html',
                           qr_code_base64=qr_code_b64,
                           liff_url=line_add_friend_url,
                           task=task,
                           customer_info=customer,
                           now=datetime.datetime.now(THAILAND_TZ))

@liff_bp.route('/liff_notification_popup')
def liff_notification_popup():
    """Hiển thị cửa sổ LIFF để thông báo."""
    return render_template('liff_notification_popup.html', LIFF_ID_FORM=LIFF_ID_FORM)

@liff_bp.route('/open_in_line')
def open_in_line():
    """Trang thông báo người dùng mở trong LINE."""
    return render_template('open_in_line.html')

@liff_bp.route('/technician/update_location')
def technician_location_update_page():
    """Hiển thị trang LIFF để kỹ thuật viên cập nhật vị trí."""
    return render_template('technician_location_update.html', LIFF_ID_TECHNICIAN_LOCATION=LIFF_ID_TECHNICIAN_LOCATION)