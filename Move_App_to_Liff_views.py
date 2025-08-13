@liff_bp.route('/')
@liff_bp.route('/summary')
def summary():
    """Renders the main dashboard/summary page."""
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    today_thai = datetime.date.today()

    summary_stats = {
        'total': 0,
        'needsAction': 0,
        'completed': 0,
        'overdue': 0,
        'today': 0,
        'external': 0 # For the new "external job" card
    }
    
    final_tasks = []
    
    for task in tasks_raw:
        summary_stats['total'] += 1
        task_status = task.get('status', 'needsAction')
        
        # Check if it's an external job
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

    # Chart data calculation
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


@app.route('/calendar')
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
	

@app.route('/edit_task/<task_id>', methods=['GET', 'POST'])
def edit_task(task_id):
    task_raw = get_single_task(task_id)
    if not task_raw:
        abort(404)

    if request.method == 'POST':
        new_title = str(request.form.get('task_title', '')).strip()
        if not new_title:
            flash('กรุณากรอกรายละเอียดงาน', 'danger')
            return redirect(url_for('edit_task', task_id=task_id))

        # Reconstruct the base notes section from the form
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

        # Preserve existing reports and feedback from the original notes
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
                return redirect(url_for('edit_task', task_id=task_id))

        if update_google_task(task_id, title=new_title, notes=final_notes, due=due_date_gmt):
            cache.clear()
            flash('บันทึกข้อมูลหลักของงานเรียบร้อยแล้ว!', 'success')
            return redirect(url_for('liff.task_details', task_id=task_id))
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกข้อมูลหลัก', 'danger')
            return redirect(url_for('edit_task', task_id=task_id))

    # For GET request
    task = parse_google_task_dates(task_raw)
    _, base_notes = parse_tech_report_from_notes(task_raw.get('notes', ''))
    task['customer'] = parse_customer_info_from_notes(base_notes)
    return render_template('edit_task.html', task=task)

	
@app.route('/technician_report')
def technician_report():
    now = datetime.datetime.now(THAILAND_TZ)
    try:
        year, month = int(request.args.get('year', now.year)), int(request.args.get('month', now.month))
    except (ValueError, TypeError):
        year, month = now.year, now.month

    months = [{'value': i, 'name': datetime.date(2000, i, 1).strftime('%B')} for i in range(1, 13)]

    # เรียกใช้ฟังก์ชันกลางที่เราสร้างขึ้น
    report_data, technician_list = _get_technician_report_data(year, month)

    # ส่งข้อมูลไปยัง Template
    return render_template('technician_report.html',
                        report_data=report_data, 
                        selected_year=year, 
                        selected_month=month,
                        years=list(range(now.year - 5, now.year + 2)), 
                        months=months,
                        technician_list=technician_list)


@app.route('/technician_report/print')
def technician_report_print():
    now = datetime.datetime.now(THAILAND_TZ)
    try:
        year, month = int(request.args.get('year', now.year)), int(request.args.get('month', now.month))
    except (ValueError, TypeError):
        year, month = now.year, now.month

    # เรียกใช้ฟังก์ชันกลางที่เราสร้างขึ้น
    sorted_report, technician_list = _get_technician_report_data(year, month)

    # ส่งข้อมูลไปยัง Template
    return render_template('technician_report_print.html',
                        report_data=sorted_report,
                        selected_year=year,
                        selected_month=month,
                        now=datetime.datetime.now(THAILAND_TZ),
                        technician_list=technician_list)


@app.route('/public/report/<task_id>')
def public_task_report(task_id):
    """
    หน้ารายงานสาธารณะสำหรับให้ลูกค้าดู
    """
    task_raw = get_single_task(task_id)
    if not task_raw:
        abort(404)

    # ตรวจสอบว่างานเสร็จสิ้นแล้วหรือไม่ (เพื่อความปลอดภัย)
    if task_raw.get('status') != 'completed':
        # อาจจะแสดงข้อความว่า "รายงานจะพร้อมให้ดูเมื่องานเสร็จสิ้น" หรือ 404 ไปเลย
        abort(404)

    task = parse_google_task_dates(task_raw)
    notes = task.get('notes', '')

    # ดึงข้อมูลที่จำเป็นเท่านั้น
    task['customer'] = parse_customer_info_from_notes(notes)
    task['tech_reports_history'], _ = parse_tech_report_from_notes(notes)

    # คัดกรองเฉพาะรายงานที่มีเนื้อหาหรือรูปภาพ
    task['tech_reports_history'] = [
        r for r in task['tech_reports_history'] 
        if r.get('work_summary') or r.get('attachments')
    ]

    response = make_response(render_template('public_report.html', task=task))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

from liff_views import liff_bp
app.register_blueprint(liff_bp, url_prefix='/')	


@app.route('/generate_public_report_qr/<task_id>')
def generate_public_report_qr(task_id):
    """
    สร้าง QR Code สำหรับให้ลูกค้าดูรายงานสาธารณะ (หน้าสำหรับพิมพ์/แชร์ QR)
    """
    task = get_single_task(task_id)
    if not task:
        abort(404)

    # สร้าง URL ของรายงานสาธารณะจริง ๆ
    public_report_url = url_for('public_task_report', task_id=task.id, _external=True)
    
    # สร้าง QR Code จาก URL รายงานสาธารณะ
    qr_code = generate_qr_code_base64(public_report_url)
    customer = parse_customer_info_from_notes(task.get('notes', ''))
    
    response = make_response(render_template('public_report_qr.html',
                                             qr_code_base64_report=qr_code,
                                             task=task,
                                             customer_info=customer,
                                             public_report_url=public_report_url,
                                             LIFF_ID_TECHNICIAN_LOCATION=LIFF_ID_TECHNICIAN_LOCATION, # ส่ง LIFF ID ของช่างไปให้ถ้าจำเป็น
                                             now=datetime.datetime.now(THAILAND_TZ)
                                             ))
    
    # ตั้งค่า Cache Control เพื่อให้เบราว์เซอร์ไม่เก็บแคชหน้านี้
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response					