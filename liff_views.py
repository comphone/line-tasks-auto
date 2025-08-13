# File: liff_views.py
import datetime
import pytz
import json
from flask import (
    Blueprint, render_template, request, url_for, abort, jsonify,
    current_app, redirect, flash, make_response
)
from dateutil.parser import parse as date_parse

# Import functions from the refactored utils.py
from utils import (
    get_google_tasks_for_report, get_single_task, parse_google_task_dates,
    parse_customer_info_from_notes, parse_tech_report_from_notes,
    parse_customer_feedback_from_notes, get_technician_report_data,
    get_customer_database, update_google_task
)

liff_bp = Blueprint('liff', __name__)

THAILAND_TZ = pytz.timezone('Asia/Bangkok')

@liff_bp.route('/')
@liff_bp.route('/summary')
def summary():
    """Renders the main dashboard with a summary of all tasks."""
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    search_query = str(request.args.get('search_query', '')).strip()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    today_thai = datetime.date.today()
    
    summary_stats = {'total': 0, 'needsAction': 0, 'completed': 0, 'overdue': 0, 'today': 0, 'external': 0}
    final_tasks = []

    for task in tasks_raw:
        summary_stats['total'] += 1
        task_status = task.get('status', 'needsAction')
        
        if task.get('title', '').lower().startswith('[งานภายนอก]'):
            summary_stats['external'] += 1

        is_overdue, is_today = False, False
        if task_status == 'needsAction':
            summary_stats['needsAction'] += 1
            if task.get('due'):
                try:
                    due_dt_utc = date_parse(task['due'])
                    due_dt_local = due_dt_utc.astimezone(THAILAND_TZ)
                    if due_dt_local.date() < today_thai:
                        is_overdue = True
                        summary_stats['overdue'] += 1
                    elif due_dt_local.date() == today_thai:
                        is_today = True
                        summary_stats['today'] += 1
                except (ValueError, TypeError): pass
        else:
            summary_stats['completed'] += 1
            
        # Filtering logic
        task_passes_filter = (status_filter == 'all' or
                              status_filter == task_status or
                              (status_filter == 'overdue' and is_overdue) or
                              (status_filter == 'today' and is_today) or
                              (status_filter == 'external' and task.get('title', '').lower().startswith('[งานภายนอก]')))

        if task_passes_filter:
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            searchable_text = f"{task.get('title', '')} {customer_info.get('name', '')} {customer_info.get('organization', '')} {customer_info.get('phone', '')}".lower()
            if not search_query or search_query.lower() in searchable_text:
                parsed_task = parse_google_task_dates(task)
                parsed_task.update({'customer': customer_info, 'is_overdue': is_overdue, 'is_today': is_today})
                final_tasks.append(parsed_task)
    
    final_tasks.sort(key=lambda x: (x.get('status') == 'completed', x.get('due') is None, date_parse(x.get('due', '9999-12-31T23:59:59Z'))))

    # Chart data generation
    monthly_completed = { (datetime.datetime.now(THAILAND_TZ) - datetime.timedelta(days=i*30)).strftime('%Y-%m'): 0 for i in range(12, -1, -1) }
    for task in tasks_raw:
        if task.get('status') == 'completed' and task.get('completed'):
            try:
                key = date_parse(task['completed']).astimezone(THAILAND_TZ).strftime('%Y-%m')
                if key in monthly_completed: monthly_completed[key] += 1
            except (ValueError, TypeError): continue
            
    sorted_months = sorted(monthly_completed.keys())
    chart_data = {
        'labels': [datetime.datetime.strptime(m, '%Y-%m').strftime('%b %y') for m in sorted_months],
        'values': [monthly_completed[m] for m in sorted_months]
    }
    
    return render_template('dashboard.html', tasks=final_tasks, summary=summary_stats, search_query=search_query, status_filter=status_filter, chart_data=chart_data)

@liff_bp.route('/summary/print')
def summary_print():
    """Generates a printable summary of tasks based on filters."""
    # This route has similar logic to summary() but for a different template.
    # The logic is kept separate for clarity, though it could be refactored further.
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    today_thai = datetime.date.today()
    final_tasks = []

    for task in tasks_raw:
        task_status = task.get('status', 'needsAction')
        is_overdue, is_today = False, False
        if task_status == 'needsAction' and task.get('due'):
            try:
                due_dt_local = date_parse(task['due']).astimezone(THAILAND_TZ)
                if due_dt_local.date() < today_thai: is_overdue = True
                elif due_dt_local.date() == today_thai: is_today = True
            except (ValueError, TypeError): pass
            
        task_passes_filter = (status_filter == 'all' or
                              status_filter == task_status or
                              (status_filter == 'today' and is_today))
                              
        if task_passes_filter:
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            searchable_text = f"{task.get('title', '')} {customer_info.get('name', '')} {customer_info.get('organization', '')} {customer_info.get('phone', '')}".lower()
            if not search_query or search_query in searchable_text:
                parsed_task = parse_google_task_dates(task)
                parsed_task.update({'customer': customer_info, 'is_overdue': is_overdue, 'is_today': is_today})
                final_tasks.append(parsed_task)
                
    final_tasks.sort(key=lambda x: (x.get('status') == 'completed', x.get('due') is None, date_parse(x.get('due', '9999-12-31T23:59:59Z'))))
    return render_template("summary_print.html", tasks=final_tasks, search_query=search_query, status_filter=status_filter, now=datetime.datetime.now(THAILAND_TZ))

@liff_bp.route('/calendar')
def calendar_view():
    """Renders the calendar page with unscheduled tasks."""
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
    """Handles editing the main details of a task."""
    task_raw = get_single_task(task_id)
    if not task_raw: abort(404)
        
    if request.method == 'POST':
        new_title = str(request.form.get('task_title', '')).strip()
        if not new_title:
            flash('กรุณากรอกรายละเอียดงาน', 'danger')
            return redirect(url_for('liff.edit_task', task_id=task_id))
            
        # Reconstruct base notes
        notes_lines = []
        if request.form.get('organization_name'): notes_lines.append(f"หน่วยงาน: {str(request.form.get('organization_name', '')).strip()}")
        notes_lines.append(f"ลูกค้า: {str(request.form.get('customer_name', '')).strip()}")
        notes_lines.append(f"เบอร์โทรศัพท์: {str(request.form.get('customer_phone', '')).strip()}")
        notes_lines.append(f"ที่อยู่: {str(request.form.get('address', '')).strip()}")
        if request.form.get('latitude_longitude'): notes_lines.append(str(request.form.get('latitude_longitude', '')).strip())
        
        new_base_notes = "\n".join(filter(None, notes_lines))
        
        # Preserve existing tech reports and feedback
        original_notes = task_raw.get('notes', '')
        tech_reports, _ = parse_tech_report_from_notes(original_notes)
        feedback_data = parse_customer_feedback_from_notes(original_notes)
        
        all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in tech_reports])
        final_notes = new_base_notes
        if all_reports_text: final_notes += all_reports_text
        if feedback_data: final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        
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
            current_app.config['cache'].clear()
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
    """Renders the technician performance report page."""
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
    """Generates a printable version of the technician report."""
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

@liff_bp.route('/public/report/<task_id>')
def public_task_report(task_id):
    """Renders a public-facing report for a completed task."""
    task_raw = get_single_task(task_id)
    if not task_raw or task_raw.get('status') != 'completed':
        abort(404)
        
    task = parse_google_task_dates(task_raw)
    notes = task.get('notes', '')
    task['customer'] = parse_customer_info_from_notes(notes)
    task['tech_reports_history'], _ = parse_tech_report_from_notes(notes)
    
    # Filter for the latest, most relevant report
    latest_report = next((r for r in task['tech_reports_history'] if r.get('work_summary') or r.get('attachments')), None)
    
    # Cost calculation logic
    settings = current_app.config['get_app_settings']()
    equipment_catalog = settings.get('equipment_catalog', [])
    detailed_costs = []
    total_cost = 0.0
    if latest_report and latest_report.get('equipment_used'):
        catalog_map = {item['item_name'].lower(): item for item in equipment_catalog}
        for used_item in latest_report['equipment_used']:
            item_info = catalog_map.get(used_item['item'].lower(), {})
            price = item_info.get('price', 0.0)
            unit = item_info.get('unit', 'unit')
            quantity = used_item.get('quantity', 0)
            subtotal = price * quantity
            detailed_costs.append({
                'item': used_item['item'],
                'quantity': quantity,
                'unit': unit,
                'price_per_unit': price,
                'subtotal': subtotal
            })
            total_cost += subtotal
    
    response = make_response(render_template('public_task_report.html', 
                                             task=task, 
                                             customer_info=task['customer'],
                                             latest_report=latest_report,
                                             detailed_costs=detailed_costs,
                                             total_cost=total_cost,
                                             settings=settings))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@liff_bp.route('/generate_public_report_qr/<task_id>')
def generate_public_report_qr(task_id):
    """Generates a QR code for the public task report."""
    task = get_single_task(task_id)
    if not task: abort(404)
        
    public_report_url = url_for('liff.public_task_report', task_id=task['id'], _external=True)
    # The generate_qr_code_base64 function is in app.py, accessed via app.config
    qr_code = current_app.config['generate_qr_code_base64'](public_report_url)
    customer = parse_customer_info_from_notes(task.get('notes', ''))
    
    return render_template('public_report_qr.html', 
                           qr_code_base64_report=qr_code, 
                           task=task, 
                           customer_info=customer, 
                           public_report_url=public_report_url, 
                           LIFF_ID_TECHNICIAN_LOCATION=current_app.config.get('LIFF_ID_TECHNICIAN_LOCATION'), 
                           now=datetime.datetime.now(THAILAND_TZ))

@liff_bp.route('/form')
def form_page():
    """Renders the new task creation form."""
    return render_template('form.html', 
                           task_detail_snippets=current_app.config.get('TEXT_SNIPPETS', {}).get('task_details', []))

@liff_bp.route('/external_job_form')
def external_job_form_page():
    """Renders the form for creating external/claim jobs."""
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
    """Renders the detailed view and management page for a single task."""
    task_raw = get_single_task(task_id)
    if not task_raw: abort(404)
        
    task = parse_google_task_dates(task_raw)
    notes = task.get('notes', '')
    task['customer'] = parse_customer_info_from_notes(notes)
    task['tech_reports_history'], _ = parse_tech_report_from_notes(notes)
    
    settings = current_app.config['get_app_settings']()
    technician_list = settings.get('technician_list', [])
    equipment_catalog = settings.get('equipment_catalog', [])
    
    all_attachments = [att for report in task['tech_reports_history'] if report.get('attachments') for att in report['attachments']]
    
    return render_template('update_task_details.html', 
                           task=task, 
                           technician_list=technician_list, 
                           all_attachments=all_attachments, 
                           progress_report_snippets=current_app.config.get('TEXT_SNIPPETS', {}).get('progress_reports', []),
                           equipment_catalog=equipment_catalog,
                           LIFF_ID_TO_USE=current_app.config.get('LIFF_ID_FORM'))

@liff_bp.route('/customer_problem_form/<task_id>')
def customer_problem_form(task_id):
    """Renders a form for customers to report problems with a service."""
    task = get_single_task(task_id)
    if not task: abort(404)
        
    task['customer'] = parse_customer_info_from_notes(task.get('notes', ''))
    return render_template('customer_problem_form.html', task=task, LIFF_ID_FORM=current_app.config.get('LIFF_ID_FORM'))

@liff_bp.route('/generate_onboarding_qr/<task_id>')
def generate_customer_onboarding_qr(task_id):
    """Generates a QR code for customer to follow the LINE OA and link to a task."""
    task = get_single_task(task_id)
    if not task: abort(404)
        
    settings = current_app.config['get_app_settings']()
    line_oa_id = settings.get('shop_info', {}).get('line_id', '@YOUR_LINE_OA_ID').replace('@','')
    line_add_friend_url = f"https://line.me/R/ti/p/@{line_oa_id}?referral={task_id}"
    
    qr_code_b64 = current_app.config['generate_qr_code_base64'](line_add_friend_url)
    customer = parse_customer_info_from_notes(task.get('notes', ''))
    
    return render_template('generate_onboarding_qr.html', 
                           qr_code_base64=qr_code_b64, 
                           liff_url=line_add_friend_url, 
                           task=task, 
                           customer_info=customer, 
                           now=datetime.datetime.now(THAILAND_TZ))

@liff_bp.route('/liff_notification_popup')
def liff_notification_popup():
    """Renders the LIFF popup page for notifications."""
    return render_template('liff_notification_popup.html', LIFF_ID_FORM=current_app.config.get('LIFF_ID_FORM'))

@liff_bp.route('/open_in_line')
def open_in_line():
    """Renders a page instructing users to open the link in LINE."""
    return render_template('open_in_line.html')

@liff_bp.route('/technician/update_location')
def technician_location_update_page():
    """Renders the LIFF page for technicians to update their location."""
    return render_template('technician_location_update.html', LIFF_ID_TECHNICIAN_LOCATION=current_app.config.get('LIFF_ID_TECHNICIAN_LOCATION'))