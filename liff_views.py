# liff_views.py
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
    get_google_tasks_for_report,
    get_single_task,
    parse_google_task_dates,
    parse_customer_info_from_notes,
    parse_tech_report_from_notes,
    parse_customer_feedback_from_notes,
    get_app_settings,
    generate_qr_code_base64,
    update_google_task,
    cache,
    _get_technician_report_data,
    LIFF_ID_FORM,
    LIFF_ID_TECHNICIAN_LOCATION,
    db, JobItem, BillingStatus,
    find_or_create_drive_folder,
    upload_data_from_memory_to_drive,
    message_queue,
    parse_assigned_technician_from_notes
)

liff_bp = Blueprint('liff', __name__)

THAILAND_TZ = pytz.timezone('Asia/Bangkok')

# --- Helper function for PDF generation ---
def _generate_invoice_pdf_bytes(task_id):
    task_raw = get_single_task(task_id)
    if not task_raw:
        return None
    task = parse_google_task_dates(task_raw)
    task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
    items = JobItem.query.filter_by(task_google_id=task_id).order_by(JobItem.added_at.asc()).all()
    total_cost = sum(item.quantity * item.unit_price for item in items)
    settings = get_app_settings()
    subtotal = total_cost / 1.07
    vat = total_cost - subtotal
    try:
        total_cost_in_words = num2words(total_cost, to='currency', lang='th')
    except Exception:
        total_cost_in_words = "ไม่สามารถแปลงเป็นตัวอักษรได้"

    # --- ✅ โค้ดที่แก้ไข: เปลี่ยนการอ้างอิงโลโก้ ---
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
    # --- ✅ สิ้นสุดโค้ดที่แก้ไข ---
    return HTML(string=invoice_html).write_pdf()

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
    
    for task_item in tasks_raw:
        summary_stats['total'] += 1
        task_status = task_item.get('status', 'needsAction')
        
        if task_item.get('title', '').lower().startswith('[งานภายนอก]'):
            summary_stats['external'] += 1

        is_overdue = False
        is_today = False
        if task_status == 'needsAction':
            summary_stats['needsAction'] += 1
            if task_item.get('due'):
                try:
                    due_dt_utc = datetime.datetime.fromisoformat(task_item['due'].replace('Z', '+00:00'))
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
        elif status_filter == 'external' and task_item.get('title', '').lower().startswith('[งานภายนอก]'): task_passes_filter = True

        if task_passes_filter:
            customer_info = parse_customer_info_from_notes(task_item.get('notes', ''))
            searchable_text = f"{task_item.get('title', '')} {customer_info.get('name', '')} {customer_info.get('organization', '')} {customer_info.get('phone', '')}".lower()
            if not search_query or search_query in searchable_text:
                parsed_task = parse_google_task_dates(task_item)
                parsed_task['customer'] = customer_info
                parsed_task['is_overdue'] = is_overdue
                parsed_task['is_today'] = is_today
                # --- START: ✅ โค้ดที่เพิ่มเข้ามา ---
                parsed_task['assigned_technician'] = parse_assigned_technician_from_notes(task_item.get('notes', ''))
                # --- END: ✅ โค้dที่เพิ่มเข้ามา ---
                final_tasks.append(parsed_task)

    final_tasks.sort(key=lambda x: (x.get('status') == 'completed', x.get('due') is None, datetime.datetime.fromisoformat(x.get('due', '9999-12-31T23:59:59Z').replace('Z', '+00:00'))))
    
    # ... (ส่วน Chart เหมือนเดิม) ...
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

@liff_bp.route('/form')
def form_page():
    settings = get_app_settings()
    technician_templates = settings.get('technician_templates', {})
    return render_template('form.html',
                           task_detail_snippets=technician_templates.get('task_details', []))

@liff_bp.route('/external_job_form')
def external_job_form_page():
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
    task_raw = get_single_task(task_id)
    if not task_raw:
        abort(404)

    task = parse_google_task_dates(task_raw)
    notes = task.get('notes', '')
    
    task['customer'] = parse_customer_info_from_notes(notes)
    task['tech_reports_history'], _ = parse_tech_report_from_notes(notes)
    task['assigned_technician'] = parse_assigned_technician_from_notes(notes)

    settings = get_app_settings()
    technician_list = settings.get('technician_list', [])
    equipment_catalog = settings.get('equipment_catalog', [])
    technician_templates = settings.get('technician_templates', {})

    all_attachments = []
    if task['tech_reports_history']:
        for report in task['tech_reports_history']:
            if report.get('attachments'):
                all_attachments.extend(report['attachments'])

    total_cost = sum(item.quantity * item.unit_price for item in JobItem.query.filter_by(task_google_id=task_id).all())

    return render_template('update_task_details.html',
                           task=task,
                           technician_list=technician_list,
                           all_attachments=all_attachments,
                           progress_report_snippets=technician_templates.get('progress_reports', []),
                           equipment_catalog=equipment_catalog,
                           total_cost=total_cost,
                           LIFF_ID_TO_USE=LIFF_ID_FORM)

@liff_bp.route('/customer_problem_form/<task_id>')
def customer_problem_form(task_id):
    task = get_single_task(task_id)
    if not task:
        abort(404)
    task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
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
    customer = parse_customer_info_from_notes(task.get('notes', ''))
    
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

@liff_bp.route('/billing')
def billing_summary():
    """แสดงหน้าสรุปรายการงานที่เสร็จแล้วเพื่อรอการเก็บเงิน (เวอร์ชันอัปเดต)"""
    search_query = request.args.get('search_query', '').strip().lower()
    completed_tasks_raw = [t for t in (get_google_tasks_for_report(show_completed=True) or []) if t.get('status') == 'completed']
    
    tasks_with_details = []
    summary_data = {
        'pending_billing_total': 0,
        'billed_total': 0,
        'paid_total': 0
    }

    for task_raw in completed_tasks_raw:
        customer_info = parse_customer_info_from_notes(task_raw.get('notes', ''))
        searchable_text = f"{task_raw.get('title', '')} {customer_info.get('name', '')}".lower()

        if search_query and search_query not in searchable_text:
            continue

        items = JobItem.query.filter_by(task_google_id=task_raw['id']).all()
        total_cost = sum(item.quantity * item.unit_price for item in items)
        
        billing_status = BillingStatus.query.filter_by(task_google_id=task_raw['id']).first()
        if not billing_status:
            billing_status = BillingStatus(task_google_id=task_raw['id'])
            db.session.add(billing_status)
            db.session.commit()

        task = parse_google_task_dates(task_raw)
        task['customer'] = customer_info
        task['total_cost'] = total_cost
        task['billing_status'] = billing_status.status
        
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

    task = parse_google_task_dates(task_raw)
    task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
    items = JobItem.query.filter_by(task_google_id=task_id).order_by(JobItem.added_at.asc()).all()
    total_cost = sum(item.quantity * item.unit_price for item in items)
    settings = get_app_settings()

    subtotal = total_cost / 1.07
    vat = total_cost - subtotal
    try:
        total_cost_in_words = num2words(total_cost, to='currency', lang='th')
    except Exception:
        total_cost_in_words = "ไม่สามารถแปลงเป็นตัวอักษรได้"
    
    invoice_html = render_template('invoice_template.html',
                                   task=task,
                                   items=items,
                                   total_cost=total_cost,
                                   subtotal=subtotal,
                                   vat=vat,
                                   total_cost_in_words=total_cost_in_words,
                                   settings=settings,
                                   now=datetime.datetime.now(THAILAND_TZ))

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

@liff_bp.route('/api/generate_invoice_pdf/<task_id>')
def generate_invoice_pdf(task_id):
    """
    สร้างไฟล์ PDF ของใบแจ้งหนี้และส่งกลับเป็น Response เพื่อให้ Web Share API
    สามารถนำไปใช้ได้โดยตรง
    """
    pdf_bytes = _generate_invoice_pdf_bytes(task_id)
    if pdf_bytes:
        task_raw = get_single_task(task_id)
        if not task_raw: abort(404)
        task = parse_google_task_dates(task_raw)
        customer = parse_customer_info_from_notes(task.get('notes', ''))
        
        pdf_filename = f"Invoice-{task['id'][-6:].upper()}-{customer.get('name', 'customer')}.pdf".replace(" ", "_")
        
        response = make_response(pdf_bytes)
        response.headers.set('Content-Type', 'application/pdf')
        
        # --- ✅ โค้ดที่แก้ไข ---
        # เข้ารหัสชื่อไฟล์ให้ถูกต้องตามมาตรฐาน RFC 5987
        response.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(pdf_filename)}"
        # --- ✅ สิ้นสุดโค้ดที่แก้ไข ---

        return response
    
    return "Failed to generate PDF", 500

@liff_bp.route('/invoice/<task_id>/print')
def print_invoice(task_id):
    """แสดงหน้าใบแจ้งหนี้สำหรับพิมพ์ (เวอร์ชันอัปเดต)"""
    task_raw = get_single_task(task_id)
    if not task_raw:
        abort(404)

    task = parse_google_task_dates(task_raw)
    task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
    
    items = JobItem.query.filter_by(task_google_id=task_id).order_by(JobItem.added_at.asc()).all()
    
    total_cost = sum(item.quantity * item.unit_price for item in items)
    
    subtotal = total_cost / 1.07
    vat = total_cost - subtotal

    try:
        total_cost_in_words = num2words(total_cost, to='currency', lang='th')
    except Exception:
        total_cost_in_words = "ไม่สามารถแปลงเป็นตัวอักษรได้"

    settings = get_app_settings()

    return render_template('invoice_template.html',
                           task=task,
                           items=items,
                           total_cost=total_cost,
                           subtotal=subtotal,
                           vat=vat,
                           total_cost_in_words=total_cost_in_words,
                           settings=settings,
                           now=datetime.datetime.now(THAILAND_TZ))
                           
@liff_bp.route('/activity_feed')
def activity_feed():
    """แสดงหน้าสรุปความเคลื่อนไหวของงานทั้งหมด (เวอร์ชันปรับปรุง)"""
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    settings = get_app_settings()
    technician_list = settings.get('technician_list', [])
    activities = []

    for task in tasks_raw:
        task_id = task.get('id')
        task_title_safe = task.get('title', 'N/A')
        customer_name = parse_customer_info_from_notes(task.get('notes', '')).get('name', 'N/A')
        task_url = url_for('liff.task_details', task_id=task_id)

        # 1. กิจกรรมการสร้างงาน
        if task.get('created'):
            created_dt = date_parse(task.get('created'))
            activities.append({
                'timestamp': created_dt.astimezone(THAILAND_TZ),
                'type': 'new_task',
                'description': f'สร้างงานใหม่: <a href="{task_url}"><strong>{task_title_safe}</strong></a> สำหรับลูกค้า <strong>{customer_name}</strong>',
                'technician': 'System',
                'customer': customer_name
            })
            
        # 2. กิจกรรมการปิดงาน
        if task.get('status') == 'completed' and task.get('completed'):
            completed_dt = date_parse(task.get('completed'))
            activities.append({
                'timestamp': completed_dt.astimezone(THAILAND_TZ),
                'type': 'completed',
                'description': f'ปิดงาน <a href="{task_url}"><strong>{task_title_safe}</strong></a> ของลูกค้า <strong>{customer_name}</strong> เรียบร้อยแล้ว',
                'technician': 'System',
                'customer': customer_name
            })

        # 3. กิจกรรมจาก Tech Reports
        history, _ = parse_tech_report_from_notes(task.get('notes', ''))
        for report in history:
            report_type = 'internal_note' if report.get('is_internal') else report.get('type', 'report')
            technicians = report.get('technicians', ['N/A'])
            
            description = ""
            if report_type == 'report':
                description = f'ได้เพิ่มรายงานในงาน <a href="{task_url}"><strong>{task_title_safe}</strong></a>'
            elif report_type == 'internal_note':
                 description = f'ได้เพิ่มบันทึกภายในถึงทีมงานเกี่ยวกับงาน <a href="{task_url}"><strong>{task_title_safe}</strong></a>'
            elif report_type == 'reschedule':
                description = f'ได้เลื่อนนัดงาน <a href="{task_url}"><strong>{task_title_safe}</strong></a>'

            report_dt = date_parse(report.get('summary_date'))
            
            # ทำให้เป็น aware datetime เสมอ
            if report_dt.tzinfo is None:
                report_dt = THAILAND_TZ.localize(report_dt)
            else:
                report_dt = report_dt.astimezone(THAILAND_TZ)

            for tech_name in technicians:
                 activities.append({
                    'timestamp': report_dt,
                    'type': report_type,
                    'description': description,
                    'technician': tech_name,
                    'customer': customer_name
                })

    # 1. เรียงลำดับกิจกรรมทั้งหมดจากใหม่ไปเก่า
    activities.sort(key=lambda x: x['timestamp'], reverse=True)

    # 2. จัดกลุ่มกิจกรรมตามวัน โดยใช้ groupby จาก Python
    grouped_activities = []
    for date, items in groupby(activities, key=lambda x: x['timestamp'].date()):
        grouped_activities.append((date, list(items)))

    return render_template(
        'activity_feed.html', 
        grouped_activities=grouped_activities,
        technician_list=technician_list,
        timedelta=timedelta
    )