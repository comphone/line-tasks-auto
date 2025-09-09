import os
import datetime
import pytz
import base64
import json
from collections import defaultdict, Counter
from itertools import groupby
from datetime import timedelta, time
from flask import (
    Blueprint, render_template, request, url_for, abort, jsonify,
    current_app, redirect, flash, make_response, session
)
from dateutil.parser import parse as date_parse
from sqlalchemy import func, or_, and_, desc, case
from weasyprint import HTML
from io import BytesIO
from num2words import num2words
from urllib.parse import quote_plus, quote
from linebot.v3.messaging import FlexMessage
from urllib.parse import quote_plus
from app import (
    db, Customer, Job, Report, Attachment, JobItem, BillingStatus, User,
    LIFF_ID_FORM, LIFF_ID_TECHNICIAN_LOCATION, UserActivity,
    message_queue, cache, Warehouse, StockLevel, get_google_drive_service, _execute_google_api_call_with_retry,
    find_or_create_drive_folder, upload_data_from_memory_to_drive, THAILAND_TZ
)
from utils import (
    parse_db_customer_data, parse_db_job_data, parse_db_report_data,
    get_app_settings, generate_qr_code_base64, get_technician_report_data
)

liff_bp = Blueprint('liff', __name__)

@liff_bp.route('/')
@liff_bp.route('/summary')
def summary():
    search_query = request.args.get('search_query', '').strip()
    status_filter = request.args.get('status_filter', 'all').strip()

    query = db.session.query(Job).join(Customer)

    today = datetime.datetime.now(THAILAND_TZ).date()
    today_start_utc = THAILAND_TZ.localize(datetime.datetime.combine(today, time.min)).astimezone(pytz.utc)
    today_end_utc = THAILAND_TZ.localize(datetime.datetime.combine(today, time.max)).astimezone(pytz.utc)

    sort_order = case(
        (and_(Job.status != 'completed', Job.due_date >= today_start_utc, Job.due_date <= today_end_utc), 1),
        (and_(Job.status != 'completed', Job.due_date < today_start_utc), 2),
        (Job.status != 'completed', 3),
        (Job.status == 'completed', 4),
        else_=5
    )

    query = query.order_by(sort_order, Job.due_date.asc(), Job.completed_date.desc())

    all_jobs = query.all()

    summary_stats = {
        'total': len(all_jobs),
        'needsAction': sum(1 for j in all_jobs if j.status == 'needsAction'),
        'completed': sum(1 for j in all_jobs if j.status == 'completed'),
        'today': sum(1 for j in all_jobs if j.status == 'needsAction' and j.due_date and j.due_date.astimezone(THAILAND_TZ).date() == today)
    }

    now_utc = datetime.datetime.now(pytz.utc)
    thirty_days_ago_utc = now_utc - timedelta(days=30)

    completed_jobs_last_30_days = Job.query.filter(
        Job.status == 'completed',
        Job.completed_date >= thirty_days_ago_utc
    ).all()

    completion_dates = [j.completed_date.astimezone(THAILAND_TZ).strftime('%Y-%m-%d') for j in completed_jobs_last_30_days if j.completed_date]
    date_counts = Counter(completion_dates)

    sorted_dates = sorted(date_counts.keys())
    chart_labels = sorted_dates
    chart_values = [date_counts[d] for d in sorted_dates]

    chart_data = {
        'labels': chart_labels,
        'values': chart_values
    }

    final_jobs = all_jobs
    if status_filter != 'all':
        if status_filter == 'today':
            final_jobs = [j for j in all_jobs if j.status == 'needsAction' and j.due_date and j.due_date.astimezone(THAILAND_TZ).date() == today]
        elif status_filter == 'external':
            final_jobs = [j for j in all_jobs if j.job_type == 'external']
        else:
            final_jobs = [j for j in all_jobs if j.status == status_filter]

    if search_query:
        final_jobs = [
            j for j in final_jobs if
            search_query.lower() in (j.job_title or '').lower() or
            search_query.lower() in (j.customer.name or '').lower() or
            search_query.lower() in (j.customer.organization or '').lower() or
            search_query.lower() in (j.customer.phone or '').lower()
        ]

    return render_template('dashboard.html',
                           tasks=final_jobs,
                           summary=summary_stats,
                           search_query=search_query,
                           status_filter=status_filter,
                           chart_data=chart_data)

@liff_bp.route('/customer/<int:customer_id>')
def customer_profile(customer_id):
    customer = Customer.query.get(customer_id)
    if not customer:
        abort(404)
        
    jobs = Job.query.filter_by(customer_id=customer_id).order_by(Job.created_date.desc()).all()
    
    total_spent = 0
    for job in jobs:
        items = JobItem.query.filter_by(job_id=job.id).all()
        total_spent += sum(item.quantity * item.unit_price for item in items)
    
    settings = get_app_settings()

    return render_template(
        'customer_profile.html',
        profile=customer,
        jobs=jobs,
        customer_id=customer_id,
        total_jobs=len(jobs),
        total_spent=total_spent
    )

@liff_bp.route('/customer/<int:customer_id>/job/<int:job_id>')
def job_details(customer_id, job_id):
    job = Job.query.options(
        db.joinedload(Job.customer),
        db.joinedload(Job.reports).joinedload(Report.attachments)
    ).get(job_id)

    if not job or job.customer.id != customer_id:
        abort(404)

    customer_info = job.customer
    settings = get_app_settings()
    technician_list = settings.get('technician_list', [])
    progress_report_snippets = settings.get('technician_templates', {}).get('progress_reports', [])
    equipment_catalog = settings.get('equipment_catalog', [])

    line_user_id = session.get('line_user_id')
    if line_user_id:
        try:
            activity = UserActivity.query.filter_by(line_user_id=line_user_id).first()
            if not activity:
                activity = UserActivity(line_user_id=line_user_id)
                db.session.add(activity)
            
            activity.last_viewed_job_id = job_id
            db.session.commit()
        except Exception as e:
            current_app.logger.error(f"Could not update user activity for {line_user_id}: {e}")
            db.session.rollback()

    return render_template(
        'update_task_details.html',
        task=job,
        job=job,
        customer_info=customer_info,
        technician_list=technician_list,
        progress_report_snippets=progress_report_snippets,
        equipment_catalog=equipment_catalog,
        liff_id=LIFF_ID_FORM,
        thaizone=THAILAND_TZ
    )

@liff_bp.route('/api/customer/<int:customer_id>/job/<int:job_id>/update', methods=['POST'])
def api_update_job_report(customer_id, job_id):
    try:
        job_to_update = Job.query.filter_by(id=job_id, customer_id=customer_id).first()
        if not job_to_update:
            return jsonify({'status': 'error', 'message': 'ไม่พบใบงานที่ต้องการอัปเดต'}), 404

        action = request.form.get('action')
        liff_user_id = request.form.get('technician_line_user_id')
        flash_message = "อัปเดตข้อมูลเรียบร้อยแล้ว"
        
        technicians_report_str = request.form.get('technicians_report', '')
        technicians = [t.strip() for t in technicians_report_str.split(',') if t.strip()]

        if not technicians and liff_user_id:
            settings = get_app_settings()
            tech_info = next((tech for tech in settings.get('technician_list', []) if tech.get('line_user_id') == liff_user_id), None)
            if tech_info:
                technicians = [tech_info['name']]
        if not technicians:
            technicians = ["ไม่ระบุชื่อ"]
            
        new_report = Report(job=job_to_update, technicians=','.join(technicians), is_internal=False)
        
        if action == 'complete_task':
            job_to_update.status = 'completed'
            job_to_update.completed_date = datetime.datetime.utcnow()
            new_report.report_type = 'report'
            new_report.work_summary = request.form.get('work_summary')
            flash_message = 'ปิดงานสำเร็จ!'
            
        elif action == 'reschedule_task':
            new_due_str = request.form.get('reschedule_due')
            if new_due_str:
                dt_local = THAILAND_TZ.localize(date_parse(new_due_str))
                job_to_update.due_date = dt_local.astimezone(pytz.utc)
                job_to_update.status = 'needsAction'
            new_report.report_type = 'reschedule'
            new_report.work_summary = request.form.get('reschedule_reason')
            flash_message = 'เลื่อนนัดเรียบร้อยแล้ว'

        elif action == 'save_report':
            new_report.report_type = 'report'
            new_report.work_summary = request.form.get('work_summary')
            new_report.is_internal = request.form.get('is_internal_note') == 'on'
            flash_message = 'เพิ่มรายงานความคืบหน้าเรียบร้อยแล้ว!'
        
        if new_report.work_summary or request.form.get('uploaded_attachments_json'):
             db.session.add(new_report)
             db.session.flush()

        new_attachments_json = request.form.get('uploaded_attachments_json')
        new_attachments = json.loads(new_attachments_json) if new_attachments_json else []
        for att_data in new_attachments:
            new_att = Attachment(report=new_report, drive_file_id=att_data['id'], file_name=att_data['name'], file_url=att_data['url'])
            db.session.add(new_att)

        db.session.commit()
        return jsonify({'status': 'success', 'message': flash_message})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error in api_update_job_report: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดภายในเซิร์ฟเวอร์'}), 500

@liff_bp.route('/summary/print')
def summary_print():
    jobs = Job.query.all()
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    today_thai = datetime.date.today()
    final_jobs = []
    
    for job in jobs:
        is_overdue = False
        is_today = False
        if job.status == 'needsAction' and job.due_date:
            due_dt_local = job.due_date.astimezone(THAILAND_TZ)
            if due_dt_local.date() < today_thai: is_overdue = True
            elif due_dt_local.date() == today_thai: is_today = True
        
        job_passes_filter = False
        if status_filter == 'all': job_passes_filter = True
        elif status_filter == 'completed' and job.status == 'completed': job_passes_filter = True
        elif status_filter == 'needsAction' and job.status == 'needsAction': job_passes_filter = True
        elif status_filter == 'today' and is_today: job_passes_filter = True

        if job_passes_filter:
            customer = job.customer
            searchable_text = f"{job.job_title} {customer.name or ''} {customer.organization or ''} {customer.phone or ''}".lower()
            if not search_query or search_query in searchable_text:
                final_jobs.append(job)

    final_jobs.sort(key=lambda x: (x.status == 'completed', x.due_date is None, x.due_date if x.due_date else datetime.datetime.max.replace(tzinfo=pytz.utc)))
    
    return render_template("summary_print.html",
                           tasks=final_jobs,
                           search_query=search_query,
                           status_filter=status_filter,
                           now=datetime.datetime.now(THAILAND_TZ))

@liff_bp.route('/calendar')
def calendar_view():
    jobs = Job.query.filter(Job.due_date != None).all()
    unscheduled_jobs = Job.query.filter(Job.due_date == None).all()
            
    unscheduled_jobs.sort(key=lambda x: x.created_date, reverse=True)
    
    return render_template('calendar.html', unscheduled_tasks=unscheduled_jobs)

@liff_bp.route('/edit_task/<int:job_id>', methods=['GET', 'POST'])
def edit_task(job_id):
    job = Job.query.get(job_id)
    if not job:
        abort(404)

    if request.method == 'POST':
        new_title = request.form.get('task_title')
        if not new_title:
            flash('กรุณากรอกรายละเอียดงาน', 'danger')
            return redirect(url_for('liff.edit_task', job_id=job_id))

        job.job_title = new_title
        customer = job.customer
        customer.name = request.form.get('customer_name')
        customer.organization = request.form.get('organization_name')
        customer.phone = request.form.get('customer_phone')
        customer.address = request.form.get('address')
        customer.map_url = request.form.get('latitude_longitude')
        
        appointment_str = request.form.get('appointment_due')
        if appointment_str:
            dt_local = THAILAND_TZ.localize(date_parse(appointment_str))
            job.due_date = dt_local.astimezone(pytz.utc)
        else:
            job.due_date = None

        db.session.commit()
        flash('บันทึกข้อมูลหลักของงานเรียบร้อยแล้ว!', 'success')
        return redirect(url_for('liff.customer_profile', customer_id=customer.id))

    task_data = {
        'id': job.id,
        'title': job.job_title,
        'customer': job.customer,
        'due_date': job.due_date,
        'due_for_input': job.due_date.astimezone(THAILAND_TZ).strftime("%Y-%m-%dT%H:%M") if job.due_date else '',
    }
    return render_template('edit_task.html', task=task_data)

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

@liff_bp.route('/public/report/<int:job_id>')
def public_task_report(job_id):
    job = Job.query.get(job_id)
    if not job or job.status != 'completed':
        abort(404)
        
    reports_db = Report.query.filter_by(job_id=job.id).order_by(Report.summary_date.desc()).all()
    tech_reports_history = [
        parse_db_report_data(report) for report in reports_db
        if not report.is_internal and (report.work_summary or report.attachments)
    ]
    
    task_data = {
        'id': job.id,
        'title': job.job_title,
        'customer': job.customer,
        'completed_formatted': job.completed_date.astimezone(THAILAND_TZ).strftime('%d %B %Y') if job.completed_date else '-',
        'tech_reports_history': tech_reports_history
    }

    response = make_response(render_template('public_report.html', task=task_data))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@liff_bp.route('/generate_public_report_qr/<int:job_id>')
def generate_public_report_qr(job_id):
    job = Job.query.get(job_id)
    if not job:
        abort(404)
    
    settings = get_app_settings()
    line_oa_id = settings.get('shop_info', {}).get('line_id', '@YOUR_LINE_OA_ID').replace('@','')
    public_report_url = url_for('liff.public_task_report', job_id=job_id, _external=True)
    
    qr_code_b64 = generate_qr_code_base64(public_report_url)
    
    return render_template('public_report_qr.html',
                           qr_code_base64_report=qr_code_b64,
                           public_report_url=public_report_url,
                           task={'id': job.id, 'title': job.job_title},
                           customer_info=job.customer,
                           LIFF_ID_TECHNICIAN_LOCATION=LIFF_ID_TECHNICIAN_LOCATION,
                           now=datetime.datetime.now(THAILAND_TZ))

@liff_bp.route('/form')
def form_page():
    settings = get_app_settings()
    technician_templates = settings.get('technician_templates', {})
    return render_template('form.html',
                           task_detail_snippets=technician_templates.get('task_details', []))

@liff_bp.route('/external_job_form')
def external_job_form_page():
    customer_id = request.args.get('customer_id')
    prefill_data = None
    if customer_id:
        customer = Customer.query.get(customer_id)
        if customer:
            prefill_data = customer
            
    return render_template('external_job_form.html', prefill_data=prefill_data)

@liff_bp.route('/task/<int:job_id>')
def task_details(job_id):
    job = Job.query.get(job_id)
    if not job:
        abort(404)
    return redirect(url_for('liff.customer_profile', customer_id=job.customer.id, job_id=job.id), code=301)

@liff_bp.route('/customer_problem_form/<int:job_id>')
def customer_problem_form(job_id):
    job = Job.query.get(job_id)
    if not job:
        abort(404)
    return render_template('customer_problem_form.html', task=job, LIFF_ID_FORM=LIFF_ID_FORM)

@liff_bp.route('/generate_onboarding_qr/<int:customer_id>')
def generate_customer_onboarding_qr(customer_id):
    customer = Customer.query.get(customer_id)
    if not customer:
        abort(404)
    
    settings = get_app_settings()
    line_oa_id = settings.get('shop_info', {}).get('line_id', '@YOUR_LINE_OA_ID').replace('@','')
    line_add_friend_url = f"https://line.me/R/ti/p/@{line_oa_id}?referral={customer_id}"
    
    qr_code_b64 = generate_qr_code_base64(line_add_friend_url)
    
    return render_template('generate_onboarding_qr.html',
                           qr_code_base64=qr_code_b64,
                           liff_url=line_add_friend_url,
                           task={'id': customer.id, 'title': f'โปรไฟล์ลูกค้า: {customer.name}'},
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
    
    completed_jobs = Job.query.filter_by(status='completed').all()
    
    tasks_with_details = []
    summary_data = {'pending_billing_total': 0, 'billed_total': 0, 'paid_total': 0, 'overdue_total': 0}

    for job in completed_jobs:
        customer = job.customer
        searchable_text = f"{job.job_title} {customer.name or ''}".lower()

        if search_query and search_query not in searchable_text:
            continue

        items = JobItem.query.filter_by(job_id=job.id).all()
        total_cost = sum(item.quantity * item.unit_price for item in items)

        billing_status = BillingStatus.query.filter_by(job_id=job.id).first()
        if not billing_status:
            billing_status = BillingStatus(job_id=job.id)
            db.session.add(billing_status)
            db.session.commit()
        
        job_data = {
            'id': job.id,
            'title': job.job_title,
            'customer': job.customer,
            'completed_date': job.completed_date,
            'total_cost': total_cost,
            'billing_status': billing_status.status,
            'billing_status_details': billing_status.to_dict(),
            'customer_line_id': customer.line_user_id
        }
        
        tasks_with_details.append(job_data)
        
        if billing_status.status == 'pending_billing':
            summary_data['pending_billing_total'] += total_cost
        elif billing_status.status == 'billed':
            summary_data['billed_total'] += total_cost
        elif billing_status.status == 'overdue':
            summary_data['overdue_total'] += total_cost
        elif billing_status.status == 'paid':
            summary_data['paid_total'] += total_cost
    
    tasks_with_details.sort(key=lambda x: x.get('completed_date') or datetime.datetime.min, reverse=True)

    return render_template('billing_summary.html', tasks=tasks_with_details, summary=summary_data, search_query=search_query)

@liff_bp.route('/api/billing/<int:job_id>/update_status', methods=['POST'])
def update_billing_status(job_id):
    data = request.json
    new_status = data.get('status')
    
    if not new_status:
        return jsonify({'status': 'error', 'message': 'ไม่พบสถานะใหม่'}), 400

    billing_record = BillingStatus.query.filter_by(job_id=job_id).first()
    if not billing_record:
        billing_record = BillingStatus(job_id=job_id)
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
        current_app.logger.error(f"Error updating billing status for job {job_id}: {e}")
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกข้อมูล'}), 500     
    
@liff_bp.route('/api/billing/batch_update', methods=['POST'])
def api_billing_batch_update():
    data = request.json
    job_ids = data.get('job_ids', [])
    new_status = data.get('status')
    
    if not job_ids or not new_status:
        return jsonify({'status': 'error', 'message': 'ข้อมูลไม่ครบถ้วน'}), 400

    try:
        records_to_update = BillingStatus.query.filter(BillingStatus.job_id.in_(job_ids)).all()
        
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

@liff_bp.route('/api/billing/<int:job_id>/send_invoice', methods=['POST'])
def send_invoice_to_customer(job_id):
    data = request.json
    recipient_id = data.get('recipient_id')
    
    job = Job.query.get(job_id)
    if not job:
        return jsonify({'status': 'error', 'message': 'ไม่พบข้อมูลงาน'}), 404

    customer = job.customer
    if not recipient_id:
        recipient_id = customer.line_user_id
    
    if not recipient_id:
        return jsonify({'status': 'error', 'message': 'ไม่พบผู้รับ LINE ID, กรุณากรอก ID ผู้รับ'}), 404

    items = JobItem.query.filter_by(job_id=job.id).order_by(JobItem.added_at.asc()).all()
    
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
                                   task={'id': job.id, 'title': job.job_title, 'customer': customer},
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
    pdf_filename = f"Invoice-{job.id}-{customer.name or 'customer'}.pdf".replace(" ", "_")

    invoices_folder_id = find_or_create_drive_folder("Invoices", os.environ.get('GOOGLE_DRIVE_FOLDER_ID'))
    if not invoices_folder_id:
        return jsonify({'status': 'error', 'message': 'ไม่สามารถสร้างโฟลเดอร์ Invoices บน Drive ได้'}), 500

    drive_file_info = upload_data_from_memory_to_drive(pdf_file, pdf_filename, 'application/pdf', invoices_folder_id)
    if not drive_file_info or 'webViewLink' not in drive_file_info:
        return jsonify({'status': 'error', 'message': 'ไม่สามารถอัปโหลดใบแจ้งหนี้ไปยัง Google Drive ได้'}), 500
    
    invoice_url = drive_file_info['webViewLink']

    flex_message = create_invoice_flex_message({'title': job.job_title, 'customer': customer}, total_cost, invoice_url)
    message_queue.add_message(recipient_id, [flex_message])
    
    billing_record = BillingStatus.query.filter_by(job_id=job_id).first()
    if billing_record and billing_record.status == 'pending_billing':
        billing_record.status = 'billed'
        billing_record.billed_date = datetime.datetime.utcnow()
        db.session.commit()
            
    return jsonify({'status': 'success', 'message': f'ส่งใบแจ้งหนี้ไปยัง {recipient_id} เรียบร้อยแล้ว'})
    
@liff_bp.route('/customer/<int:customer_id>/job/<int:job_id>/delete', methods=['POST'])
def delete_job_from_profile(customer_id, job_id):
    try:
        job = Job.query.filter_by(id=job_id, customer_id=customer_id).first()
        if not job:
            flash('ไม่พบใบงานที่ต้องการแก้ไข', 'danger')
            return redirect(url_for('liff.customer_profile', customer_id=customer_id))

        db.session.delete(job)
        db.session.commit()

        flash('ลบใบงานย่อยออกจากโปรไฟล์เรียบร้อยแล้ว!', 'success')
        return redirect(url_for('liff.customer_profile', customer_id=customer_id))
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting job {job_id} from task {customer_id}: {e}", exc_info=True)
        flash('เกิดข้อผิดพลาดในการลบใบงาน', 'danger')
        return redirect(url_for('liff.summary'))

@liff_bp.route('/api/customer/<int:customer_id>/job/<int:job_id>/edit_report/<int:report_id>', methods=['POST'])
def edit_report_attachments(customer_id, job_id, report_id):
    from app import _handle_image_upload, sanitize_filename
    try:
        report_to_edit = Report.query.filter_by(id=report_id, job_id=job_id).first()
        if not report_to_edit:
            flash('ไม่พบรายงานที่ต้องการแก้ไข', 'danger')
            return redirect(url_for('liff.job_details', customer_id=customer_id, job_id=job_id))

        attachments_to_keep_ids = request.form.getlist('attachments_to_keep')
        
        original_attachments = report_to_edit.attachments
        
        drive_service = get_google_drive_service()
        if drive_service:
            for att in original_attachments:
                if att.drive_file_id not in attachments_to_keep_ids:
                    try:
                        _execute_google_api_call_with_retry(drive_service.files().delete, fileId=att.drive_file_id)
                    except Exception as e:
                        current_app.logger.error(f"Failed to delete attachment {att.drive_file_id}: {e}")
            
            db.session.commit()
            
            for att in original_attachments:
                if att.drive_file_id not in attachments_to_keep_ids:
                    db.session.delete(att)
        
        new_files = request.files.getlist('new_files[]')
        for file in new_files:
            file_to_upload, filename, mime_type = _handle_image_upload(file, 500) # Use helper
            if file_to_upload:
                customer = report_to_edit.job.customer
                job = report_to_edit.job
                monthly_folder_name = job.created_date.astimezone(THAILAND_TZ).strftime('%Y-%m')
                monthly_folder_id = find_or_create_drive_folder(monthly_folder_name, os.environ.get('GOOGLE_DRIVE_FOLDER_ID'))
                customer_job_folder_name = f"{sanitize_filename(customer.name)} - {job.id}"
                destination_folder_id = find_or_create_drive_folder(customer_job_folder_name, monthly_folder_id)

                if destination_folder_id:
                    drive_file_info = upload_data_from_memory_to_drive(file_to_upload, filename, mime_type, destination_folder_id)
                    if drive_file_info and drive_file_info.get('id'):
                        new_att = Attachment(report=report_to_edit, drive_file_id=drive_file_info['id'], file_name=filename, file_url=drive_file_info['webViewLink'])
                        db.session.add(new_att)

        db.session.commit()
        flash('แก้ไขรูปภาพในรายงานเรียบร้อยแล้ว!', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error editing report attachments: {e}", exc_info=True)
        flash('เกิดข้อผิดพลาดร้ายแรง', 'danger')

    return redirect(url_for('liff.job_details', customer_id=customer_id, job_id=job_id))      

@liff_bp.route('/api/generate_invoice_pdf/<int:job_id>')
def generate_invoice_pdf(job_id):
    return "PDF generation not fully implemented", 501

@liff_bp.route('/invoice/<int:job_id>/print')
def print_invoice(job_id):
    job = Job.query.get(job_id)
    if not job:
        abort(404)

    items = JobItem.query.filter_by(job_id=job.id).order_by(JobItem.added_at.asc()).all()
    
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
                           task={'id': job.id, 'title': job.job_title, 'customer': job.customer},
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
    reports_with_jobs = Report.query.options(db.joinedload(Report.job).joinedload(Job.customer)).order_by(Report.summary_date.desc()).all()
    
    activities = []
    action_map = {
        'report': 'ได้เพิ่มรายงาน',
        'internal_note': 'ได้เพิ่มบันทึกภายใน',
        'reschedule': 'ได้เลื่อนนัดหมาย'
    }

    for report in reports_with_jobs:
        job = report.job
        customer = job.customer
        
        activities.append({
            'job_id': job.id,
            'job_title': job.job_title,
            'customer': customer.name,
            'job_url': url_for('liff.job_details', customer_id=customer.id, job_id=job.id),
            'timestamp': report.summary_date.astimezone(THAILAND_TZ),
            'type': report.report_type,
            'action_text': action_map.get(report.report_type, 'อัปเดตงาน'),
            'technician': report.technicians,
        })

    activities.sort(key=lambda x: x['timestamp'], reverse=True)
    grouped_activities = []
    for date, items in groupby(activities, key=lambda x: x['timestamp'].date()):
        grouped_activities.append((date, list(items)))
        
    settings = get_app_settings()
    technician_list = settings.get('technician_list', [])

    return render_template(
        'activity_feed.html', 
        grouped_activities=grouped_activities,
        technician_list=technician_list,
        timedelta=timedelta
    )

@liff_bp.route('/api/customer/<int:customer_id>/job/<int:job_id>/delete_report/<int:report_id>', methods=['POST'])
def delete_job_report(customer_id, job_id, report_id):
    try:
        report_to_delete = Report.query.filter_by(id=report_id, job_id=job_id).first()
        if not report_to_delete:
            return jsonify({'status': 'error', 'message': 'ไม่พบรายงานที่ต้องการลบ'}), 404

        drive_service = get_google_drive_service()
        if drive_service:
            for att in report_to_delete.attachments:
                try:
                    _execute_google_api_call_with_retry(drive_service.files().delete, fileId=att.drive_file_id)
                    current_app.logger.info(f"Deleted attachment {att.drive_file_id} from Drive.")
                except Exception as e:
                    current_app.logger.error(f"Failed to delete attachment {att.drive_file_id}: {e}")

        db.session.delete(report_to_delete)
        db.session.commit()
        
        return jsonify({'status': 'success', 'message': 'ลบรายงานเรียบร้อยแล้ว'})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting job report: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดร้ายแรงฝั่งเซิร์ฟเวอร์'}), 500

@liff_bp.route('/liff/manage_customer_duplicates')
def manage_customer_duplicates():
    customers_db = Customer.query.all()
    duplicates = defaultdict(list)
    
    for customer in customers_db:
        if customer.name:
            # Group by normalized name
            normalized_name = customer.name.strip().lower()
            duplicates[normalized_name].append(customer)

    # Filter to only groups with more than one customer
    duplicate_customers = {k: v for k, v in duplicates.items() if len(v) > 1}
    
    # Enrich data for template rendering
    for name, customer_list in duplicate_customers.items():
        for customer in customer_list:
            customer.jobs # Pre-load jobs
            
    return render_template('manage_customer_duplicates.html', duplicate_customers=duplicate_customers)

@liff_bp.route('/api/merge_customer_profiles', methods=['POST'])
def api_merge_customer_profiles():
    data = request.json
    master_customer_id = data.get('master_customer_id')
    duplicate_customer_ids = data.get('duplicate_customer_ids')

    if not master_customer_id or not duplicate_customer_ids:
        return jsonify({'status': 'error', 'message': 'ข้อมูลไม่ครบถ้วน'}), 400

    master_customer = Customer.query.get(master_customer_id)
    if not master_customer:
        return jsonify({'status': 'error', 'message': 'ไม่พบโปรไฟล์หลัก'}), 404
        
    try:
        # Get all jobs from duplicate customers
        jobs_to_move = Job.query.filter(Job.customer_id.in_(duplicate_customer_ids)).all()
        
        # Reassign jobs to the master customer
        for job in jobs_to_move:
            job.customer_id = master_customer_id
            
        # Delete the duplicate customers
        Customer.query.filter(Customer.id.in_(duplicate_customer_ids)).delete()
        
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'รวมโปรไฟล์ลูกค้าสำเร็จ'})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error merging customer profiles: {e}")
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการรวมโปรไฟล์'}), 500