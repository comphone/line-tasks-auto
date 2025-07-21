def parse_google_task_dates(task_item):
    parsed = task_item.copy()
    for key in ['created', 'due', 'completed']:
        if parsed.get(key):
            try:
                dt_utc = date_parse(parsed[key])
                parsed[f'{key}_formatted'] = dt_utc.astimezone(THAILAND_TZ).strftime("%d/%m/%y %H:%M")
                if key == 'due':
                    parsed['due_for_input'] = dt_utc.astimezone(THAILAND_TZ).strftime("%Y-%m-%dT%H:%M")
            except (ValueError, TypeError) as e:
                app.logger.warning(f"Could not parse date '{parsed[key]}' for key '{key}': {e}")
                parsed[f'{key}_formatted'] = ''
                if key == 'due': 
                    parsed['due_for_input'] = ''
        else:
            parsed[f'{key}_formatted'] = ''
            if key == 'due': 
                parsed['due_for_input'] = ''
    return parsed

def parse_tech_report_from_notes(notes):
    if not notes: 
        return [], ""
    report_blocks = re.findall(r"--- TECH_REPORT_START ---\s*\n(.*?)\n--- TECH_REPORT_END ---", notes, re.DOTALL)
    history = []
    for json_str in report_blocks:
        try:
            report_data = json.loads(json_str)
            
            if 'attachments' not in report_data and 'attachment_urls' in report_data and isinstance(report_data['attachment_urls'], list):
                report_data['attachments'] = []
                for url in report_data['attachment_urls']:
                    if isinstance(url, str):
                        match = re.search(r'/d/([a-zA-Z0-9_-]+)', url)
                        file_id = match.group(1) if match else None
                        report_data['attachments'].append({'id': file_id, 'url': url, 'name': 'Attached File'})
                report_data.pop('attachment_urls', None)
            
            if isinstance(report_data.get('equipment_used'), str):
                report_data['equipment_used_display'] = report_data['equipment_used'].replace('\n', '<br>')
            else:
                report_data['equipment_used_display'] = _format_equipment_list(report_data.get('equipment_used', []))
            
            if 'type' not in report_data:
                report_data['type'] = 'report'

            history.append(report_data)
        except json.JSONDecodeError:
            app.logger.warning(f"Failed to decode tech report JSON: {json_str[:100]}...")
    
    temp_notes = notes
    temp_notes = re.sub(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", "", temp_notes, flags=re.DOTALL)
    temp_notes = re.sub(r"--- CUSTOMER_FEEDBACK_START ---.*?--- CUSTOMER_FEEDBACK_END ---", "", temp_notes, flags=re.DOTALL)
    original_notes_text = temp_notes.strip()

    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, original_notes_text

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def _parse_equipment_string(text_input):
    equipment_list = []
    if not text_input: 
        return equipment_list
    for line in text_input.strip().split('\n'):
        if not line.strip(): 
            continue
        parts = line.split(',', 1)
        item_name = parts[0].strip()
        if item_name:
            quantity_str = parts[1].strip() if len(parts) > 1 else '1'
            try:
                quantity_num = float(quantity_str)
                equipment_list.append({"item": item_name, "quantity": quantity_num})
            except ValueError:
                equipment_list.append({"item": item_name, "quantity": quantity_str})
    return equipment_list

def _format_equipment_list(equipment_data):
    if not equipment_data: 
        return 'N/A'
    if isinstance(equipment_data, str): 
        return equipment_data
    lines = []
    if isinstance(equipment_data, list):
        for item in equipment_data:
            if isinstance(item, dict) and "item" in item:
                line = item['item']
                if item.get("quantity") is not None:
                    if isinstance(item['quantity'], (int, float)):
                        line += f" (x{item['quantity']:g})"
                    else:
                        line += f" ({item['quantity']})"
                lines.append(line)
            elif isinstance(item, str):
                lines.append(item)
    return "<br>".join(lines) if lines else 'N/A'

@app.context_processor
def inject_now():
    return {'now': datetime.datetime.now(THAILAND_TZ), 'thaizone': THAILAND_TZ}

def generate_qr_code_base64(data, box_size=10, border=4, fill_color='#28a745', back_color='#FFFFFF'):
    try:
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=box_size, border=border)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color=fill_color, back_color=back_color)
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffered.getvalue()).decode("utf-8")
    except Exception as e:
        app.logger.error(f"Error generating QR code: {e}")
        return ""

def _create_backup_zip():
    try:
        all_tasks = get_google_tasks_for_report(show_completed=True)
        if all_tasks is None:
            app.logger.error('Failed to get tasks for backup.')
            return None, None

        memory_file = BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f'data/tasks_backup{JSON_EXTENSION}', json.dumps(all_tasks, indent=4, ensure_ascii=False))
            zf.writestr(f'data/settings_backup{JSON_EXTENSION}', json.dumps(get_app_settings(), indent=4, ensure_ascii=False))

            project_root = os.path.dirname(os.path.abspath(__file__))
            for folder, _, files in os.walk(project_root):
                for file in files:
                    if file.endswith(('.py', '.html', '.css', '.js', JSON_EXTENSION, 'Procfile', 'requirements.txt')) \
                       and file not in ['token.json', '.env', SETTINGS_FILE]:
                        file_path = os.path.join(folder, file)
                        archive_name = os.path.relpath(file_path, project_root)
                        zf.write(file_path, arcname=f'code/{archive_name}')
        memory_file.seek(0)
        backup_filename = f"full_system_backup_{datetime.datetime.now(THAILAND_TZ).strftime('%Y%m%d_%H%M%S')}.zip"
        return memory_file, backup_filename
    except Exception as e:
        app.logger.error(f"Error creating full system backup zip: {e}")
        return None, None

def check_google_api_status():
    service = get_google_drive_service()
    if not service:
        return False
    try:
        _execute_google_api_call_with_retry(service.about().get, fields='user')
        return True
    except HttpError as e:
        if e.resp.status in [401, 403]:
            app.logger.warning(f"Google API authentication check failed: {e}")
            return False
        app.logger.error(f"A non-auth HttpError occurred during API status check: {e}")
        return True
    except Exception as e:
        app.logger.error(f"Unexpected error during Google API status check: {e}")
        return False

@app.context_processor
def inject_global_vars():
    return {
        'now': datetime.datetime.now(THAILAND_TZ),
        'google_api_connected': check_google_api_status()
    }

# Scheduled Jobs and Notifications
def notify_admin_error(message):
    try:
        admin_group_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
        if admin_group_id:
            line_bot_api.push_message(admin_group_id, TextSendMessage(text=f"‼️ เกิดข้อผิดพลาดร้ายแรงในระบบ ‼️\n\n{message[:900]}"))
    except Exception as e:
        app.logger.error(f"Failed to send critical error notification: {e}")

def send_new_task_notification(task):
    settings = get_app_settings()
    recipients = settings.get('line_recipients', {})
    admin_group_id = recipients.get('admin_group_id')
    
    if not admin_group_id: 
        return

    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    parsed_dates = parse_google_task_dates(task)
    
    due_info = f"นัดหมาย: {parsed_dates.get('due_formatted')}" if parsed_dates.get('due_formatted') else "นัดหมาย: - (ยังไม่ระบุ)"
    location_info = f"พิกัด: {customer_info.get('map_url')}" if customer_info.get('map_url') else NO_LOCATION_INFO_TEXT

    message_text = (
        f"✨ มีงานใหม่เข้า!\n\n"
        f"ชื่องาน: {task.get('title', '-')}\n"
        f"ลูกค้า: {customer_info.get('name', '-')}\n"
        f"📞 โทร: {customer_info.get('phone', '-')}\n"
        f"🗓️ {due_info}\n"
        f"📍 {location_info}\n\n"
        f"ดูรายละเอียดในเว็บ:\n{url_for('task_details', task_id=task.get('id'), _external=True)}"
    )
    
    try:
        line_bot_api.push_message(admin_group_id, TextSendMessage(text=message_text))
        app.logger.info(f"Sent new task notification for task {task['id']} to admin group.")
    except Exception as e:
        app.logger.error(f"Failed to send new task notification for task {task['id']}: {e}")

def send_completion_notification(task, technicians):
    settings = get_app_settings()
    recipients = settings.get('line_recipients', {})
    admin_group_id = recipients.get('admin_group_id')
    tech_group_id = recipients.get('technician_group_id')

    if not admin_group_id and not tech_group_id: 
        return

    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    technician_str = ", ".join(technicians) if technicians else "ไม่ได้ระบุ"
    
    message_text = (
        f"✅ ปิดงานเรียบร้อย\n\n"
        f"ชื่องาน: {task.get('title', '-')}\n"
        f"ลูกค้า: {customer_info.get('name', '-')}\n"
        f"ช่างผู้รับผิดชอบ: {technician_str}\n\n"
        f"ดูรายละเอียด: {url_for('task_details', task_id=task.get('id'), _external=True)}"
    )
    
    sent_to = set()
    try:
        if admin_group_id:
            line_bot_api.push_message(admin_group_id, TextSendMessage(text=message_text))
            sent_to.add(admin_group_id)
        
        if tech_group_id and tech_group_id not in sent_to:
            line_bot_api.push_message(tech_group_id, TextSendMessage(text=message_text))

    except Exception as e:
        app.logger.error(f"Failed to send completion notification for task {task['id']}: {e}")

def send_update_notification(task, new_due_date_str, reason, technicians, is_today):
    settings = get_app_settings()
    admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')
    if not admin_group_id: 
        return

    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    technician_str = ", ".join(technicians) if technicians else "ไม่ได้ระบุ"
    
    if is_today:
        title = "🗓️ อัปเดตงานวันนี้"
        reason_str = f"รายละเอียด: {reason}\n" if reason else ""
    else:
        title = "🗓️ เลื่อนนัดหมาย"
        reason_str = f"เหตุผล: {reason}\n" if reason else ""

    message_text = (
        f"{title}\n\n"
        f"ชื่องาน: {task.get('title', '-')}\n"
        f"ลูกค้า: {customer_info.get('name', '-')}\n"
        f"📞 โทร: {customer_info.get('phone', '-')}\n"
        f"นัดหมายใหม่: {new_due_date_str}\n"
        f"{reason_str}"
        f"ช่าง: {technician_str}\n\n"
        f"ดูรายละเอียดในเว็บ:\n{url_for('task_details', task_id=task.get('id'), _external=True)}"
    )
    try:
        line_bot_api.push_message(admin_group_id, TextSendMessage(text=message_text))
        app.logger.info(f"Sent update/reschedule notification for task {task['id']} to admin group.")
    except Exception as e:
        app.logger.error(f"Failed to send update/reschedule notification for task {task['id']}: {e}")

def scheduled_backup_job():
    with app.app_context():
        app.logger.info(f"--- Starting Scheduled Backup Job ---")
        overall_success = True

        system_backup_folder_id = find_or_create_drive_folder("System_Backups", GOOGLE_DRIVE_FOLDER_ID)
        if not system_backup_folder_id:
            app.logger.error("Could not find or create System_Backups folder for backup.")
            overall_success = False
        else:
            memory_file_zip, filename_zip = _create_backup_zip()
            if memory_file_zip and filename_zip:
                if upload_data_from_memory_to_drive(memory_file_zip, filename_zip, 'application/zip', system_backup_folder_id):
                    app.logger.info("Automatic full system backup successful.")
                else:
                    app.logger.error("Automatic full system backup failed.")
                    overall_success = False
            else:
                app.logger.error("Failed to create full system backup zip.")
                overall_success = False

        if not backup_settings_to_drive():
            app.logger.error("Automatic settings-only backup failed.")
            overall_success = False
        else:
            app.logger.info("Automatic settings-only backup successful.")
        
        app.logger.info(f"--- Finished Scheduled Backup Job ---")
        return overall_success

def scheduled_appointment_reminder_job():
    with app.app_context():
        app.logger.info("Running scheduled appointment reminder job...")
        settings = get_app_settings()
        recipients = settings.get('line_recipients', {})
        admin_group_id = recipients.get('admin_group_id')
        technician_group_id = recipients.get('technician_group_id')

        if not admin_group_id and not technician_group_id:
            app.logger.info("No LINE admin or technician group ID set for appointment reminders. Skipping.")
            return

        tasks_raw = get_google_tasks_for_report(show_completed=False) or []
        today_thai = datetime.date.today()
        upcoming_appointments = []

        for task in tasks_raw:
            if task.get('status') == 'needsAction' and task.get('due'):
                try:
                    due_dt_utc = date_parse(task['due'])
                    if due_dt_utc.astimezone(THAILAND_TZ).date() == today_thai:
                        upcoming_appointments.append(task)
                except (ValueError, TypeError):
                    app.logger.warning(f"Could not parse due date for reminder task {task.get('id')}: {task.get('due')}")
                    continue

        if not upcoming_appointments:
            app.logger.info("No upcoming appointments for today.")
            return

        upcoming_appointments.sort(key=lambda x: date_parse(x['due']) if x.get('due') else datetime.datetime.max.replace(tzinfo=pytz.utc))

        for task in upcoming_appointments:
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            parsed_dates = parse_google_task_dates(task)
            
            location_info = f"พิกัด: {customer_info.get('map_url')}" if customer_info.get('map_url') else NO_LOCATION_INFO_TEXT
            
            message_text = (
                f"🔔 งานสำหรับวันนี้\n\n"
                f"ชื่องาน: {task.get('title', '-')}\n"
                f"👤 ลูกค้า: {customer_info.get('name', '-')}\n"
                f"📞 โทร: {customer_info.get('phone', '-')}\n"
                f"🗓️ นัดหมาย: {parsed_dates.get('due_formatted', '-')}\n"
                f"📍 {location_info}\n\n"
                f"🔗 ดูรายละเอียด/แก้ไข:\n{url_for('task_details', task_id=task.get('id'), _external=True)}"
            )
            try:
                sent_to = set()
                if admin_group_id:
                    line_bot_api.push_message(admin_group_id, TextSendMessage(text=message_text))
                    sent_to.add(admin_group_id)

                if technician_group_id and technician_group_id not in sent_to:
                    line_bot_api.push_message(technician_group_id, TextSendMessage(text=message_text))
            except Exception as e:
                app.logger.error(f"Failed to send appointment reminder for task {task['id']}: {e}")

def _create_customer_follow_up_flex_message(task_id, task_title, customer_name):
    problem_action = URIAction(
        label='🚨 ยังมีปัญหาอยู่',
        uri=f"https://liff.line.me/{LIFF_ID_FORM}/customer_problem_form?task_id={task_id}"
    )

    return BubbleContainer(
        body=BoxComponent(
            layout='vertical', spacing='md',
            contents=[
                TextComponent(text="สอบถามหลังการซ่อม", weight='bold', size='lg', color='#1DB446', align='center'),
                SeparatorComponent(margin='md'),
                TextComponent(text=f"เรียนคุณ {customer_name},", size='sm', wrap=True),
                TextComponent(text=f"เกี่ยวกับงาน: {task_title}", size='sm', wrap=True, color='#666666'),
                SeparatorComponent(margin='lg'),
                TextComponent(text="ไม่ทราบว่าหลังจากทีมงานของเราเข้าบริการแล้ว ทุกอย่างเรียบร้อยดีหรือไม่ครับ/คะ?", size='md', wrap=True, align='center'),
                BoxComponent(layout='vertical', spacing='sm', margin='md', contents=[
                    ButtonComponent(
                        style='primary', height='sm', color='#28a745',
                        action=PostbackAction(
                            label='✅ งานเรียบร้อยดี', data=f'action=customer_feedback&task_id={task_id}&feedback=ok',
                            display_text='ขอบคุณสำหรับคำยืนยันครับ/ค่ะ!'
                        )
                    ),
                    ButtonComponent(
                        style='secondary', height='sm', color='#dc3545',
                        action=problem_action
                    )
                ]),
            ]
        )
    )

def scheduled_customer_follow_up_job():
    with app.app_context():
        app.logger.info("Running scheduled customer follow-up job...")
        settings = get_app_settings()
        admin_group_id = settings.get('line_recipients', {}).get('admin_group_id')

        tasks_raw = get_google_tasks_for_report(show_completed=True) or []
        now_utc = datetime.datetime.now(pytz.utc)
        two_days_ago_utc = now_utc - datetime.timedelta(days=2)
        one_day_ago_utc = now_utc - datetime.timedelta(days=1)

        for task in tasks_raw:
            if task.get('status') == 'completed' and task.get('completed'):
                try:
                    completed_dt_utc = date_parse(task['completed'])

                    if two_days_ago_utc <= completed_dt_utc < one_day_ago_utc:
                        notes = task.get('notes', '')
                        feedback_data = parse_customer_feedback_from_notes(notes)

                        if 'follow_up_sent_date' in feedback_data:
                            continue

                        customer_info = parse_customer_info_from_notes(notes)
                        customer_line_id = feedback_data.get('customer_line_user_id')
                        
                        if not customer_line_id:
                            continue

                        flex_content = _create_customer_follow_up_flex_message(
                            task['id'], task['title'], customer_info.get('name', 'N/A'))
                        flex_message = FlexSendMessage(alt_text="สอบถามความพึงพอใจหลังการซ่อม", contents=flex_content)

                        try:
                            line_bot_api.push_message(customer_line_id, flex_message)
                            app.logger.info(f"Sent follow-up message to customer {customer_line_id} for task {task['id']}.")
                            
                            feedback_data['follow_up_sent_date'] = datetime.datetime.now(THAILAND_TZ).isoformat()
                            history_reports, base_notes = parse_tech_report_from_notes(notes)
                            
                            tech_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history_reports])
                            
                            new_notes = base_notes.strip()
                            if tech_reports_text: 
                                new_notes += tech_reports_text
                            new_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
                            _execute_google_api_call_with_retry(update_google_task, task['id'], notes=new_notes)

                        except Exception as e:
                            app.logger.error(f"Failed to send direct follow-up to {customer_line_id}: {e}. Notifying admin.")
                            if admin_group_id:
                                line_bot_api.push_message(admin_group_id, [TextSendMessage(text=f"⚠️ ส่ง Follow-up ให้ลูกค้า {customer_info.get('name')} (Task ID: {task['id']}) ไม่สำเร็จ โปรดส่งข้อความนี้แทน:"), flex_message])

                except Exception as e:
                    app.logger.warning(f"Could not process task {task.get('id')} for follow-up: {e}", exc_info=True)

def run_scheduler():
    global scheduler

    settings = get_app_settings()

    if scheduler.running:
        app.logger.info("Scheduler already running, shutting down before reconfiguring...")
        scheduler.shutdown(wait=False)
    
    scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)

    ab = settings.get('auto_backup', {})
    if ab.get('enabled'):
        scheduler.add_job(scheduled_backup_job, CronTrigger(hour=ab.get('hour_thai', 2), minute=ab.get('minute_thai', 0)), id='auto_system_backup', replace_existing=True)
        app.logger.info(f"Scheduled auto backup for {ab.get('hour_thai', 2)}:{ab.get('minute_thai', 0):02d} Thai time.")
    else:
        if scheduler.get_job('auto_system_backup'):
            scheduler.remove_job('auto_system_backup')
            app.logger.info("Auto backup job disabled and removed.")

    rt = settings.get('report_times', {})
    scheduler.add_job(scheduled_appointment_reminder_job, CronTrigger(hour=rt.get('appointment_reminder_hour_thai', 7), minute=0), id='daily_appointment_reminder', replace_existing=True)
    scheduler.add_job(scheduled_customer_follow_up_job, CronTrigger(hour=rt.get('customer_followup_hour_thai', 9), minute=5), id='daily_customer_followup', replace_existing=True)
    app.logger.info(f"Scheduled appointment reminders for {rt.get('appointment_reminder_hour_thai', 7)}:00 and customer follow-up for {rt.get('customer_followup_hour_thai', 9)}:05 Thai time.")

    scheduler.start()
    app.logger.info("APScheduler started/reconfigured.")

def cleanup_scheduler():
    if scheduler is not None and scheduler.running:
        app.logger.info("Scheduler is running, shutting it down.")
        scheduler.shutdown(wait=False)
    else:
        app.logger.info("Scheduler not running or not initialized, skipping shutdown.")

# Initial App Setup Calls
with app.app_context():
    load_settings_from_drive_on_startup()
    get_app_settings()
    run_scheduler()

atexit.register(cleanup_scheduler)

# Error Handlers
@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    app.logger.error(f"Server Error: {e}", exc_info=True)
    notify_admin_error(f"Internal Server Error: {e}")
    return render_template('500.html'), 500

# Flask Routes
@app.route('/api/customers')
def api_customers():
    customer_list = get_customer_database()
    return jsonify(customer_list)

@app.route("/")
def root_redirect():
    return redirect(url_for('summary'))

@app.route("/form", methods=['GET', 'POST'])
def form_page():
    if request.method == 'POST':
        task_title = str(request.form.get('task_title', '')).strip()
        customer_name = str(request.form.get('customer', '')).strip()
        organization_name = str(request.form.get('organization_name', '')).strip()

        if not task_title or not customer_name:
            flash('กรุณากรอกชื่อผู้ติดต่อและรายละเอียดงาน', 'danger')
            return redirect(url_for('form_page'))

        notes_lines = []
        if organization_name: 
            notes_lines.append(f"หน่วยงาน: {organization_name}")
        
        notes_lines.extend([
            f"ลูกค้า: {customer_name}",
            f"เบอร์โทรศัพท์: {str(request.form.get('phone', '')).strip()}",
            f"ที่อยู่: {str(request.form.get('address', '')).strip()}",
        ])
        map_url = str(request.form.get('latitude_longitude', '')).strip()
        if map_url: 
            notes_lines.append(map_url)

        notes = "\n".join(filter(None, notes_lines))

        due_date_gmt = None
        appointment_str = str(request.form.get('appointment', '')).strip()
        if appointment_str:
            try:
                dt_local = THAILAND_TZ.localize(date_parse(appointment_str))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat().replace(ISO_UTC_OFFSET, ZULU_FORMAT_SUFFIX)
            except ValueError:
                flash('รูปแบบวันเวลานัดหมายไม่ถูกต้อง', 'warning')
                return render_template('form.html', form_data=request.form)

        new_task = create_google_task(task_title, notes=notes, due=due_date_gmt)
        if new_task:
            cache.clear()
            send_new_task_notification(new_task)
            
            uploaded_attachments_json = request.form.get('uploaded_attachments_json')
            uploaded_attachments = []
            if uploaded_attachments_json:
                try:
                    uploaded_attachments = json.loads(uploaded_attachments_json)
                except json.JSONDecodeError:
                    app.logger.warning("Could not decode uploaded_attachments_json on form submission.")

            if uploaded_attachments:
                task_id = new_task['id']
                
            if uploaded_attachments:
                task_id = new_task['id']
                
                attachments_base_folder_id = find_or_create_drive_folder("Task_Attachments", GOOGLE_DRIVE_FOLDER_ID)
                monthly_folder_name = datetime.datetime.now(THAILAND_TZ).strftime('%Y-%m')
                monthly_folder_id = find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)
                sanitized_customer_name = sanitize_filename(customer_name)
                customer_task_folder_name = f"{sanitized_customer_name} - {task_id}"
                final_upload_folder_id = find_or_create_drive_folder(customer_task_folder_name, monthly_folder_id)
                
                drive_service = get_google_drive_service()
                if final_upload_folder_id and drive_service:
                    for att in uploaded_attachments:
                        try:
                            file_meta = drive_service.files().get(fileId=att['id'], fields='parents').execute()
                            previous_parents = ",".join(file_meta.get('parents', []))
                            drive_service.files().update(
                                fileId=att['id'],
                                addParents=final_upload_folder_id,
                                removeParents=previous_parents,
                                fields='id, parents'
                            ).execute()
                        except Exception as e:
                            app.logger.error(f"Could not move attachment {att['id']} to final folder: {e}")
                
                initial_report = {
                    'type': 'report',
                    'summary_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
                    'work_summary': 'ไฟล์แนบจากการสร้างงานครั้งแรก',
                    'attachments': uploaded_attachments,
                    'technicians': ['System']
                }
                report_text = f"\n\n--- TECH_REPORT_START ---\n{json.dumps(initial_report, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"
                updated_notes = new_task.get('notes', '') + report_text
                update_google_task(task_id, notes=updated_notes)
                cache.clear()

            flash('สร้างงานใหม่เรียบร้อยแล้ว!', 'success')
            return redirect(url_for('task_details', task_id=new_task['id']))
        else:
            flash('เกิดข้อผิดพลาดในการสร้างงาน', 'danger')
            return render_template('form.html', form_data=request.form)

    return render_template('form.html', task_detail_snippets=TEXT_SNIPPETS.get('task_details', []))

@app.route('/api/upload_attachment', methods=['POST'])
def api_upload_attachment():
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file'}), 400
    
    task_id = request.form.get('task_id')
    if not task_id:
        return jsonify({'status': 'error', 'message': 'Task ID is missing'}), 400

    file.seek(0, os.SEEK_END)
    file_length = file.tell()
    file.seek(0)
    
    if file_length > MAX_FILE_SIZE_BYTES:
        if file.mimetype and file.mimetype.startswith(IMAGE_MIMETYPE_PREFIX):
            try:
                img = Image.open(file)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                
                output_buffer = BytesIO()
                img.save(output_buffer, format='JPEG', quality=85, optimize=True)
                output_buffer.seek(0)
                file_to_upload = output_buffer
                filename = os.path.splitext(secure_filename(file.filename))[0] + '.jpg'
                mime_type = JPEG_MIMETYPE
                app.logger.info(f"Compressed image '{file.filename}' successfully.")
            except Exception as e:
                app.logger.error(f"Could not compress image '{file.filename}': {e}")
                return jsonify({'status': 'error', 'message': f'ไฟล์รูปภาพใหญ่เกินไปและไม่สามารถบีบอัดได้'}), 413
        else:
            return jsonify({'status': 'error', 'message': f'ไฟล์ใหญ่เกินขนาดที่กำหนด ({MAX_FILE_SIZE_MB}MB)'}), 413
    else:
        file_to_upload = file
        filename = secure_filename(file.filename)
        mime_type = file.mimetype or mimetypes.guess_type(filename)[0]

    attachments_base_folder_id = find_or_create_drive_folder("Task_Attachments", GOOGLE_DRIVE_FOLDER_ID)
    if not attachments_base_folder_id:
        return jsonify({'status': 'error', 'message': 'Could not create or find base Task_Attachments folder'}), 500

    final_upload_folder_id = None
    target_date = datetime.datetime.now(THAILAND_TZ)

    if task_id == 'new_task_placeholder':
        monthly_folder_name = target_date.strftime('%Y-%m')
        monthly_folder_id = find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)
        temp_upload_folder_name = f"New_Uploads_{target_date.strftime('%Y-%m-%d')}"
        final_upload_folder_id = find_or_create_drive_folder(temp_upload_folder_name, monthly_folder_id)
    else:
        task_raw = get_single_task(task_id)
        if not task_raw:
            return jsonify({'status': 'error', 'message': 'Task not found'}), 404
        
        if task_raw.get('created'):
            try:
                target_date = date_parse(task_raw.get('created')).astimezone(THAILAND_TZ)
            except (ValueError, TypeError):
                pass
        
        monthly_folder_name = target_date.strftime('%Y-%m')
        monthly_folder_id = find_or_create_drive_folder(monthly_folder_name, attachments_base_folder_id)
        if not monthly_folder_id:
            return jsonify({'status': 'error', 'message': f'Could not create or find monthly folder: {monthly_folder_name}'}), 500
        
        _, base_notes_text = parse_tech_report_from_notes(task_raw.get('notes', ''))
        customer_info = parse_customer_info_from_notes(base_notes_text)
        sanitized_customer_name = sanitize_filename(customer_info.get('name', 'Unknown_Customer'))
        customer_task_folder_name = f"{sanitized_customer_name} - {task_id}"
        final_upload_folder_id = find_or_create_drive_folder(customer_task_folder_name, monthly_folder_id)
    
    if not final_upload_folder_id:
        return jsonify({'status': 'error', 'message': 'Could not determine final upload folder'}), 500

    media_body = MediaIoBaseUpload(file_to_upload, mimetype=mime_type, resumable=True)
    drive_file = _perform_drive_upload(media_body, filename, mime_type, final_upload_folder_id)
    
    if drive_file:
        return jsonify({'status': 'success', 'file_info': {'id': drive_file.get('id'), 'url': drive_file.get('webViewLink'), 'name': filename}})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to upload to Google Drive'}), 500

def _process_and_filter_tasks(tasks_raw, status_filter, search_query):
    final_tasks = []
    stats = {'needsAction': 0, 'completed': 0, 'overdue': 0, 'total': len(tasks_raw), 'today': 0}
    today_thai = datetime.datetime.now(THAILAND_TZ).date()

    for task in tasks_raw:
        task_status = task.get('status', 'needsAction')
        is_overdue = False
        is_today = False

        if task_status == 'needsAction' and task.get('due'):
            try:
                due_dt_local = date_parse(task['due']).astimezone(THAILAND_TZ)
                if due_dt_local.date() < today_thai:
                    is_overdue = True
                elif due_dt_local.date() == today_thai:
                    is_today = True
            except (ValueError, TypeError):
                pass

        if task_status == 'completed':
            stats['completed'] += 1
        else:
            stats['needsAction'] += 1
            if is_overdue:
                stats['overdue'] += 1
            if is_today:
                stats['today'] += 1

        task_passes_filter = (
            status_filter == 'all' or
            status_filter == task_status or
            (status_filter == 'today' and is_today)
        )

        if task_passes_filter:
            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            searchable_text = f"{task.get('title', '')} {customer_info.get('name', '')} {customer_info.get('organization', '')} {customer_info.get('phone', '')}".lower()

            if not search_query or search_query in searchable_text:
                parsed_task = parse_google_task_dates(task)
                parsed_task['customer'] = customer_info
                parsed_task['is_overdue'] = is_overdue
                parsed_task['is_today'] = is_today
                final_tasks.append(parsed_task)

    return final_tasks, stats

def _generate_completion_chart_data(tasks_raw):
    completed_tasks_for_chart = [t for t in tasks_raw if t.get('status') == 'completed' and t.get('completed')]
    month_labels = []
    chart_values = []
    for i in range(12):
        target_d = datetime.datetime.now(THAILAND_TZ) - datetime.timedelta(days=30 * (11 - i))
        month_key = target_d.strftime('%Y-%m')
        month_labels.append(target_d.strftime('%b %y'))
        count = sum(1 for task in completed_tasks_for_chart if date_parse(task['completed']).astimezone(THAILAND_TZ).strftime('%Y-%m') == month_key)
        chart_values.append(count)
    return {'labels': month_labels, 'values': chart_values}

@app.route('/summary')
def summary():
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()

    final_tasks, stats = _process_and_filter_tasks(tasks_raw, status_filter, search_query)
    chart_data = _generate_completion_chart_data(tasks_raw)

    final_tasks.sort(key=lambda x: (x.get('status') == 'completed', x.get('due') is None, date_parse(x.get('due', MAX_DATETIME_STR))))

    return render_template("dashboard.html",
                           tasks=final_tasks, summary=stats,
                           search_query=search_query, status_filter=status_filter,
                           chart_data=chart_data)

@app.route('/summary/print')
def summary_print():
    tasks_raw = get_google_tasks_for_report(show_completed=True) or []
    search_query = str(request.args.get('search_query', '')).strip().lower()
    status_filter = str(request.args.get('status_filter', 'all')).strip()
    
    final_tasks, _ = _process_and_filter_tasks(tasks_raw, status_filter, search_query)
    final_tasks.sort(key=lambda x: (x.get('status') == 'completed', x.get('due') is None, date_parse(x.get('due', MAX_DATETIME_STR))))
    
    return render_template("summary_print.html",
                           tasks=final_tasks,
                           search_query=search_query,
                           status_filter=status_filter,
                           now=datetime.datetime.now(THAILAND_TZ))

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

@app.route('/api/calendar_tasks')
def api_calendar_tasks():
    try:
        tasks_raw = get_google_tasks_for_report(show_completed=True) or []
        events = []
        today_thai = datetime.datetime.now(THAILAND_TZ).date()

        for task in tasks_raw:
            if not task.get('due'):
                continue

            customer_info = parse_customer_info_from_notes(task.get('notes', ''))
            is_overdue = False
            is_today = False
            is_completed = task.get('status') == 'completed'

            try:
                due_dt_local = date_parse(task['due']).astimezone(THAILAND_TZ)
                if not is_completed and due_dt_local.date() < today_thai:
                    is_overdue = True
                elif not is_completed and due_dt_local.date() == today_thai:
                    is_today = True
            except (ValueError, TypeError):
                pass

            event = {
                'id': task.get('id'),
                'title': f"{customer_info.get('name', 'N/A')} - {task.get('title')}",
                'start': task.get('due'),
                'url': url_for('task_details', task_id=task.get('id')),
                'extendedProps': {
                    'is_completed': is_completed,
                    'is_overdue': is_overdue,
                    'is_today': is_today
                }
            }
            events.append(event)
            
        return jsonify(events)
    except Exception as e:
        app.logger.error(f"Error fetching tasks for calendar API: {e}", exc_info=True)
        return jsonify({"error": "Could not fetch tasks from server"}), 500

@app.route('/api/task/schedule_from_calendar', methods=['POST'])
def schedule_task_from_calendar():
    data = request.json
    task_id = data.get('task_id')
    new_due_str = data.get('new_due_date')
    
    if not task_id or not new_due_str:
        return jsonify({'status': 'error', 'message': 'ข้อมูลที่ส่งมาไม่ครบถ้วน (task_id หรือ new_due_date)'}), 400
        
    try:
        task = get_single_task(task_id)
        if not task:
            return jsonify({'status': 'error', 'message': 'ไม่พบงานที่ระบุ'}), 404
        if task.get('status') == 'completed':
            return jsonify({'status': 'error', 'message': 'ไม่สามารถย้ายงานที่เสร็จสิ้นแล้วได้'}), 403

        dt_utc = date_parse(new_due_str).astimezone(pytz.utc)
        due_date_gmt = dt_utc.isoformat().replace(ISO_UTC_OFFSET, ZULU_FORMAT_SUFFIX)

        updated_task = update_google_task(task_id, due=due_date_gmt, status='needsAction')
        
        if updated_task:
            cache.clear()
            return jsonify({'status': 'success', 'message': f'อัปเดตวันนัดหมายสำหรับงาน {task_id} เรียบร้อยแล้ว'})
        else:
            return jsonify({'status': 'error', 'message': 'ไม่สามารถอัปเดตงานใน Google Tasks ได้'}), 500
            
    except Exception as e:
        app.logger.error(f"Error scheduling task from calendar: {e}")
        return jsonify({'status': 'error', 'message': f'เกิดข้อผิดพลาดในระบบ: {e}'}), 500

# Task management helper functions
def _handle_save_report(form_data, new_attachments):
    work_summary = str(form_data.get('work_summary', '')).strip()
    technicians = [t.strip() for t in form_data.get('technicians_report', '').split(',') if t.strip()]

    if not (work_summary or new_attachments):
        return None, jsonify({'status': 'error', 'message': 'กรุณากรอกสรุปงาน หรือแนบไฟล์รูปภาพสำหรับรายงานใหม่'}), 400
    if not technicians:
        return None, jsonify({'status': 'error', 'message': 'กรุณาเลือกช่างผู้รับผิดชอบสำหรับรายงานใหม่นี้'}), 400

    report_entry = {
        'type': 'report', 'summary_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
        'work_summary': work_summary,
        'equipment_used': _parse_equipment_string(form_data.get('equipment_used', '')),
        'attachments': new_attachments,
        'technicians': technicians
    }
    return report_entry, None

def _handle_reschedule_task(form_data):
    due_str = str(form_data.get('reschedule_due', '')).strip()
    reason = str(form_data.get('reschedule_reason', '')).strip()
    technicians = [t.strip() for t in form_data.get('technicians_reschedule', '').split(',') if t.strip()]

    if not due_str:
        return None, None, None, jsonify({'status': 'error', 'message': 'กรุณากำหนดวันนัดหมายใหม่'}), 400

    try:
        dt_local = THAILAND_TZ.localize(date_parse(due_str))
        update_payload = {
            'due': dt_local.astimezone(pytz.utc).isoformat().replace(ISO_UTC_OFFSET, ZULU_FORMAT_SUFFIX),
            'status': 'needsAction'
        }
        new_due_formatted = dt_local.strftime("%d/%m/%y %H:%M")
        is_today = dt_local.date() == datetime.datetime.now(THAILAND_TZ).date()
        notification = ('update', new_due_formatted, reason, technicians, is_today)
        report_entry = {
            'type': 'reschedule', 'summary_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
            'reason': reason, 'new_due_date': new_due_formatted,
            'technicians': technicians
        }
        return report_entry, update_payload, notification, None
    except ValueError:
        return None, None, None, jsonify({'status': 'error', 'message': 'รูปแบบวันเวลานัดหมายใหม่ไม่ถูกต้อง'}), 400

def _handle_complete_task(form_data, new_attachments):
    work_summary = str(form_data.get('work_summary', '')).strip()
    technicians = [t.strip() for t in form_data.get('technicians_report', '').split(',') if t.strip()]

    if not work_summary:
        return None, None, None, jsonify({'status': 'error', 'message': 'กรุณากรอกสรุปงานเพื่อปิดงาน'}), 400
    if not technicians:
        return None, None, None, jsonify({'status': 'error', 'message': 'กรุณาเลือกช่างผู้รับผิดชอบสำหรับรายงานปิดงาน'}), 400

    update_payload = {'status': 'completed'}
    notification = ('completion', technicians)
    report_entry = {
        'type': 'report', 'summary_date': datetime.datetime.now(THAILAND_TZ).isoformat(),
        'work_summary': work_summary,
        'equipment_used': _parse_equipment_string(request.form.get('equipment_used', '')),
        'attachments': new_attachments,
        'technicians': technicians
    }
    return report_entry, update_payload, notification, None

def _handle_task_details_post(task_id, task_raw):
    action = request.form.get('action')
    update_payload = {}
    notification_to_send = None
    new_report_entry = None
    error_response = None

    new_attachments = json.loads(request.form.get('uploaded_attachments_json', '[]'))

    if action == 'save_report':
        new_report_entry, error_response = _handle_save_report(request.form, new_attachments)
        flash_message = 'เพิ่มรายงานความคืบหน้าเรียบร้อยแล้ว!'
    elif action == 'reschedule_task':
        new_report_entry, update_payload, notification_to_send, error_response = _handle_reschedule_task(request.form)
        flash_message = 'เลื่อนนัดและบันทึกเหตุผลเรียบร้อยแล้ว'
    elif action == 'complete_task':
        new_report_entry, update_payload, notification_to_send, error_response = _handle_complete_task(request.form, new_attachments)
        flash_message = 'ปิดงานและบันทึกรายงานสรุปเรียบร้อยแล้ว!'
    else:
        return jsonify({'status': 'error', 'message': 'ไม่พบการกระทำที่ร้องขอ'}), 400

    if error_response:
        return error_response

    history, base_notes_text = parse_tech_report_from_notes(task_raw.get('notes', ''))
    feedback_data = parse_customer_feedback_from_notes(task_raw.get('notes', ''))
    
    if new_report_entry:
        history.append(new_report_entry)

    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    all_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history])
    
    final_notes = base_notes_text
    if all_reports_text: 
        final_notes += all_reports_text
    if feedback_data: 
        final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback_data, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
    update_payload['notes'] = final_notes
    
    updated_task = update_google_task(task_id, **update_payload)

    if updated_task:
        cache.clear()
        if notification_to_send:
            notif_type, *notif_args = notification_to_send
            if notif_type == 'update': 
                send_update_notification(updated_task, *notif_args)
            elif notif_type == 'completion': 
                send_completion_notification(updated_task, *notif_args)
        return jsonify({'status': 'success', 'message': flash_message})
    else:
        return jsonify({'status': 'error', 'message': 'เกิดข้อผิดพลาดในการบันทึกข้อมูลหลัก!'}), 500

@app.route('/task/<task_id>', methods=['GET', 'POST'])
def task_details(task_id):
    task_raw = get_single_task(task_id)
    if not task_raw:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': TASK_NOT_FOUND_MSG}), 404
        flash(TASK_NOT_FOUND_MSG, 'danger')
        abort(404)

    if request.method == 'POST':
        return _handle_task_details_post(task_id, task_raw)

    task = parse_google_task_dates(task_raw)
    notes = task.get('notes', '')
    task['customer'] = parse_customer_info_from_notes(notes)
    task['tech_reports_history'], _ = parse_tech_report_from_notes(notes)
    task['customer_feedback'] = parse_customer_feedback_from_notes(notes)
    task['is_overdue'] = False
    task['is_today'] = False

    if task.get('status') == 'needsAction' and task.get('due'):
        try:
            due_dt_local = date_parse(task['due']).astimezone(THAILAND_TZ)
            today_thai = datetime.datetime.now(THAILAND_TZ).date()
            if due_dt_local.date() < today_thai:
                task['is_overdue'] = True
            elif due_dt_local.date() == today_thai:
                task['is_today'] = True
        except (ValueError, TypeError): 
            pass
    
    app_settings = get_app_settings()
    
    all_attachments = [
        {**att, 'report_date': parse_google_task_dates({'summary_date': report['summary_date']}).get('summary_date_formatted', '')}
        for report in task['tech_reports_history'] if report.get('attachments')
        for att in report['attachments']
    ]

    return render_template('update_task_details.html',
                           task=task,
                           common_equipment_items=app_settings.get('common_equipment_items', []),
                           technician_list=app_settings.get('technician_list', []),
                           all_attachments=all_attachments,
                           progress_report_snippets=TEXT_SNIPPETS.get('progress_reports', []))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=True)import os
import sys
import datetime
import re
import json
import pytz
import mimetypes
import zipfile
from io import BytesIO
from collections import defaultdict
from datetime import timezone, date
import time
import tempfile
import uuid

from PIL import Image

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, render_template, redirect, url_for, abort, flash, jsonify, Response, session
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect
from cachetools import cached, TTLCache
from geopy.distance import geodesic

import qrcode
import base64

from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage,
    BubbleContainer, CarouselContainer, BoxComponent, TextComponent,
    ButtonComponent, SeparatorComponent, URIAction, PostbackAction, QuickReply, QuickReplyButton,
    ImageComponent, PostbackEvent
)

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

import pandas as pd
from dateutil.parser import parse as date_parse

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit

# --- Constants ---
ISO_UTC_OFFSET = '+00:00'
ZULU_FORMAT_SUFFIX = 'Z'
JSON_EXTENSION = '.json'
SETTINGS_BACKUP_FILENAME = 'settings_backup.json'
NO_LOCATION_INFO_TEXT = "พิกัด: - (ไม่มีข้อมูล)"
IMAGE_MIMETYPE_PREFIX = 'image/'
JPEG_MIMETYPE = 'image/jpeg'
MAX_DATETIME_STR = '9999-12-31T23:59:59Z'
TASK_NOT_FOUND_MSG = 'ไม่พบงานที่ต้องการอัปเดต'

TEXT_SNIPPETS = {
    'task_details': [
        {'key': 'ล้างแอร์', 'value': 'ล้างทำความสะอาดเครื่องปรับอากาศ, ตรวจเช็คน้ำยา, วัดแรงดันไฟฟ้า และทำความสะอาดคอยล์ร้อน-เย็น'},
        {'key': 'ติดตั้งแอร์', 'value': 'ติดตั้งเครื่องปรับอากาศใหม่ ขนาด [ขนาด BTU] พร้อมเดินท่อน้ำยาและสายไฟ, ติดตั้งเบรกเกอร์'},
        {'key': 'ซ่อมตู้เย็น', 'value': 'ซ่อมตู้เย็น [ยี่ห้อ/รุ่น] อาการไม่เย็น, ตรวจสอบคอมเพรสเซอร์และน้ำยา'},
        {'key': 'ตรวจเช็ค', 'value': 'เข้าตรวจเช็คอาการเสียเบื้องต้นตามที่ลูกค้าแจ้ง'}
    ],
    'progress_reports': [
        {'key': 'ลูกค้าเลื่อนนัด', 'value': 'ลูกค้าขอเลื่อนนัดเป็นวันที่ [dd/mm/yyyy] เนื่องจากไม่สะดวก'},
        {'key': 'รออะไหล่', 'value': 'ตรวจสอบแล้วพบว่าต้องรออะไหล่ [ชื่ออะไหล่] จะแจ้งลูกค้าให้ทราบกำหนดการอีกครั้ง'},
        {'key': 'เข้าพื้นที่ไม่ได้', 'value': 'ไม่สามารถเข้าพื้นที่ได้เนื่องจาก [เหตุผล] ได้โทรแจ้งลูกค้าแล้ว'},
        {'key': 'เสร็จบางส่วน', 'value': 'ดำเนินการเสร็จสิ้นบางส่วน เหลือ [สิ่งที่ต้องทำต่อ] จะเข้ามาดำเนินการต่อในวันถัดไป'}
    ]
}

# --- Flask App Initialization ---
app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_development_only')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

csrf = CSRFProtect(app)

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'kmz', 'kml'}
MAX_FILE_SIZE_MB = 50
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# --- Environment Variable Loading ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    sys.exit("LINE Bot credentials are not set in environment variables.")

LIFF_ID_FORM = os.environ.get('LIFF_ID_FORM')
LINE_LOGIN_CHANNEL_ID = os.environ.get('LINE_LOGIN_CHANNEL_ID')
GOOGLE_TASKS_LIST_ID = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')

if not GOOGLE_DRIVE_FOLDER_ID:
    app.logger.warning("GOOGLE_DRIVE_FOLDER_ID environment variable is not set. Drive upload will not work.")
if not LIFF_ID_FORM:
    app.logger.warning("LIFF_ID_FORM environment variable is not set. LIFF features will not work.")
if not LINE_LOGIN_CHANNEL_ID:
    app.logger.warning("LINE_LOGIN_CHANNEL_ID environment variable is not set. LIFF initialization might fail.")

# --- Global Configurations ---
SCOPES = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/drive']
THAILAND_TZ = pytz.timezone('Asia/Bangkok')
cache = TTLCache(maxsize=100, ttl=60)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

app.jinja_env.filters['dateutil_parse'] = date_parse
scheduler = BackgroundScheduler(daemon=True, timezone=THAILAND_TZ)

# --- Settings Management ---
SETTINGS_FILE = 'settings.json'
_DEFAULT_APP_SETTINGS_STORE = {
    'report_times': {
        'appointment_reminder_hour_thai': 7,
        'outstanding_report_hour_thai': 20,
        'customer_followup_hour_thai': 9
    },
    'line_recipients': {
        'admin_group_id': os.environ.get('LINE_ADMIN_GROUP_ID', ''),
        'technician_group_id': os.environ.get('LINE_TECHNICIAN_GROUP_ID', ''),
        'manager_user_id': ''
    },
    'equipment_catalog': [],
    'auto_backup': { 'enabled': False, 'hour_thai': 2, 'minute_thai': 0 },
    'shop_info': { 'contact_phone': '081-XXX-XXXX', 'line_id': '@ComphoneService' },
    'technician_list': []
}
_APP_SETTINGS_STORE = {}

# Helper and Utility Functions
def load_settings_from_file():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            app.logger.error(f"Error handling settings.json: {e}")
            if os.path.exists(SETTINGS_FILE) and os.path.getsize(SETTINGS_FILE) == 0:
                os.remove(SETTINGS_FILE)
                app.logger.warning(f"Empty settings.json deleted. Using default settings.")
    return None

def save_settings_to_file(settings_data):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings_data, f, ensure_ascii=False, indent=4)
        return True
    except IOError as e:
        app.logger.error(f"Error writing to settings.json: {e}")
        return False

def get_app_settings():
    global _APP_SETTINGS_STORE
    if _APP_SETTINGS_STORE:
        return _APP_SETTINGS_STORE

    app.logger.info("Settings cache is empty. Loading from file...")
    app_settings = json.loads(json.dumps(_DEFAULT_APP_SETTINGS_STORE))
    loaded_settings = load_settings_from_file()

    if loaded_settings:
        for key, default_value in app_settings.items():
            if key in loaded_settings:
                if isinstance(default_value, dict) and isinstance(loaded_settings[key], dict):
                    app_settings[key].update(loaded_settings[key])
                else:
                    app_settings[key] = loaded_settings[key]
    else:
        save_settings_to_file(app_settings)

    equipment_catalog = app_settings.get('equipment_catalog', [])
    app_settings['common_equipment_items'] = sorted({
        item.get('item_name') for item in equipment_catalog if item.get('item_name')
    })

    _APP_SETTINGS_STORE = app_settings
    return _APP_SETTINGS_STORE

def save_app_settings(settings_data):
    global _APP_SETTINGS_STORE
    current_settings = get_app_settings().copy()

    for key, value in settings_data.items():
        if isinstance(value, dict) and key in current_settings and isinstance(current_settings[key], dict):
            current_settings[key].update(value)
        else:
            current_settings[key] = value

    if save_settings_to_file(current_settings):
        _APP_SETTINGS_STORE = current_settings
        equipment_catalog = _APP_SETTINGS_STORE.get('equipment_catalog', [])
        _APP_SETTINGS_STORE['common_equipment_items'] = sorted({
            item.get('item_name') for item in equipment_catalog if item.get('item_name')
        })
        return True
    return False

def safe_execute(request_object):
    if hasattr(request_object, 'execute'):
        return request_object.execute()
    return request_object

def _execute_google_api_call_with_retry(api_call, *args, **kwargs):
    max_retries = 3
    base_delay = 1
    for i in range(max_retries):
        try:
            return safe_execute(api_call(*args, **kwargs))
        except HttpError as e:
            if e.resp.status in [500, 502, 503, 504, 429] and i < max_retries - 1:
                delay = base_delay * (2 ** i)
                app.logger.warning(f"Google API transient error (Status: {e.resp.status}). Retrying in {delay} seconds... (Attempt {i+1}/{max_retries})")
                time.sleep(delay)
            else:
                raise
        except Exception as e:
            app.logger.error(f"Unexpected error during Google API call: {e}")
            raise
    return None

def get_google_service(api_name, api_version):
    creds = None
    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')

    if google_token_json_str:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(google_token_json_str), SCOPES)
        except Exception as e:
            app.logger.warning(f"Could not load token from GOOGLE_TOKEN_JSON env var: {e}. Please check format.")
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            app.logger.info("Google access token refreshed successfully!")
        except Exception as e:
            app.logger.error(f"Error refreshing token: {e}")
            creds = None

    if creds and creds.valid:
        try:
            service = _execute_google_api_call_with_retry(build, api_name, api_version, credentials=creds)
            return service
        except Exception as e:
            app.logger.error(f"Failed to build Google API service after retries: {e}")
            return None
    else:
        app.logger.error("No valid Google credentials available. API service cannot be built.")
        return None

def get_google_tasks_service(): 
    return get_google_service('tasks', 'v1')

def get_google_drive_service(): 
    return get_google_service('drive', 'v3')

def sanitize_filename(name):
    if not name:
        return "Unnamed"
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

@cached(cache)
def find_or_create_drive_folder(name, parent_id):
    service = get_google_drive_service()
    if not service:
        return None
    
    query = f"name = '{name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    try:
        response = _execute_google_api_call_with_retry(service.files().list, q=query, spaces='drive', fields='files(id, name)', pageSize=1)
        files = response.get('files', [])
        if files:
            app.logger.info(f"Found existing Drive folder '{name}' with ID: {files[0]['id']}")
            return files[0]['id']
        else:
            app.logger.info(f"Folder '{name}' not found in parent '{parent_id}'. Creating it...")
            file_metadata = {
                'name': name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [parent_id]
            }
            folder = _execute_google_api_call_with_retry(service.files().create, body=file_metadata, fields='id')
            folder_id = folder.get('id')
            app.logger.info(f"Created new Drive folder '{name}' with ID: {folder_id}")
            return folder_id
    except HttpError as e:
        app.logger.error(f"Error finding or creating folder '{name}': {e}")
        return None

@cached(cache)
def get_customer_database():
    app.logger.info("Building customer database from Google Tasks...")
    all_tasks = get_google_tasks_for_report(show_completed=True)
    if not all_tasks:
        return []

    customers_dict = {}
    all_tasks.sort(key=lambda x: x.get('created', '0'), reverse=True)

    for task in all_tasks:
        notes = task.get('notes', '')
        if not notes:
            continue
        
        _, base_notes = parse_tech_report_from_notes(notes)
        customer_info = parse_customer_info_from_notes(base_notes)

        name = customer_info.get('name', '').strip()
        phone = customer_info.get('phone', '').strip()

        if not name:
            continue

        customer_key = (name.lower(), phone)
        
        if customer_key not in customers_dict:
            customers_dict[customer_key] = {
                'name': name,
                'phone': phone,
                'organization': customer_info.get('organization', '').strip(),
                'address': customer_info.get('address', '').strip(),
                'map_url': customer_info.get('map_url', '')
            }
    
    app.logger.info(f"Customer database built with {len(customers_dict)} unique customers.")
    return list(customers_dict.values())

def load_settings_from_drive_on_startup():
    settings_backup_folder_id = find_or_create_drive_folder("Settings_Backups", GOOGLE_DRIVE_FOLDER_ID)
    if not settings_backup_folder_id:
        app.logger.error("Could not find or create Settings_Backups folder. Skipping settings restore.")
        return False
        
    service = get_google_drive_service()
    if not service:
        app.logger.error("Could not get Drive service for settings restore.")
        return False

    try:
        query = f"name = '{SETTINGS_BACKUP_FILENAME}' and '{settings_backup_folder_id}' in parents and trashed = false"
        response = _execute_google_api_call_with_retry(service.files().list, q=query, spaces='drive', fields='files(id, name)', orderBy='modifiedTime desc', pageSize=1)
        files = response.get('files', [])

        if files:
            latest_backup_file_id = files[0]['id']
            app.logger.info(f"Found latest settings backup on Drive (ID: {latest_backup_file_id})")

            request = service.files().get_media(fileId=latest_backup_file_id)
            fh = BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
            fh.seek(0)

            downloaded_settings = json.loads(fh.read().decode('utf-8'))

            if save_app_settings(downloaded_settings):
                app.logger.info("Successfully restored settings from Google Drive backup.")
                return True
            else:
                app.logger.error("Failed to save restored settings to local file.")
                return False
        else:
            app.logger.info("No settings backup found on Google Drive for automatic restore.")
            return False
    except Exception as e:
        app.logger.error(f"An unexpected error occurred during settings restore from Drive: {e}")
        return False

def backup_settings_to_drive():
    settings_backup_folder_id = find_or_create_drive_folder("Settings_Backups", GOOGLE_DRIVE_FOLDER_ID)
    if not settings_backup_folder_id:
        app.logger.error("Cannot back up settings: Could not find or create Settings_Backups folder.")
        return False

    service = get_google_drive_service()
    if not service:
        app.logger.error("Cannot back up settings: Google Drive service is unavailable.")
        return False

    try:
        query = f"name = '{SETTINGS_BACKUP_FILENAME}' and '{settings_backup_folder_id}' in parents and trashed = false"
        response = _execute_google_api_call_with_retry(service.files().list, q=query, spaces='drive', fields='files(id)')
        for file_item in response.get('files', []):
            try:
                _execute_google_api_call_with_retry(service.files().delete, fileId=file_item['id'])
                app.logger.info(f"Deleted old {SETTINGS_BACKUP_FILENAME} (ID: {file_item['id']}) from Drive before saving new one.")
            except HttpError as e:
                app.logger.warning(f"Could not delete old settings file {file_item['id']}: {e}. Proceeding with upload attempt.")

        settings_data = get_app_settings()
        settings_json_bytes = BytesIO(json.dumps(settings_data, ensure_ascii=False, indent=4).encode('utf-8'))
        
        file_metadata = {'name': SETTINGS_BACKUP_FILENAME, 'parents': [settings_backup_folder_id]}
        media = MediaIoBaseUpload(settings_json_bytes, mimetype='application/json', resumable=True)
        
        _execute_google_api_call_with_retry(
            service.files().create,
            body=file_metadata, media_body=media, fields='id'
        )
        app.logger.info(f"Successfully saved current settings to {SETTINGS_BACKUP_FILENAME} on Google Drive.")
        return True

    except Exception as e:
        app.logger.error(f"Failed to backup settings to Google Drive: {e}", exc_info=True)
        return False

@cached(cache)
def get_google_tasks_for_report(show_completed=True):
    service = get_google_tasks_service()
    if not service: 
        return None
    try:
        results = _execute_google_api_call_with_retry(service.tasks().list, tasklist=GOOGLE_TASKS_LIST_ID, showCompleted=show_completed, maxResults=100)
        return results.get('items', [])
    except HttpError as err:
        app.logger.error(f"API Error getting tasks: {err}")
        return None

def get_single_task(task_id):
    if not task_id: 
        return None
    service = get_google_tasks_service()
    if not service: 
        return None
    try:
        return _execute_google_api_call_with_retry(service.tasks().get, tasklist=GOOGLE_TASKS_LIST_ID, task=task_id)
    except HttpError as err:
        app.logger.error(f"Error getting single task {task_id}: {err}")
        return None

def _perform_drive_upload(media_body, file_name, mime_type, folder_id):
    service = get_google_drive_service()
    if not service or not folder_id:
        app.logger.error(f"Drive service or Folder ID not configured for upload of '{file_name}'.")
        return None

    try:
        file_metadata = {'name': file_name, 'parents': [folder_id]}
        app.logger.info(f"Attempting to upload file '{file_name}' to Drive folder '{folder_id}'.")
        
        file_obj = _execute_google_api_call_with_retry(
            service.files().create,
            body=file_metadata, media_body=media_body, fields='id, webViewLink'
        )

        if not file_obj or 'id' not in file_obj:
            app.logger.error(f"Drive upload failed for '{file_name}': File object or ID is missing.")
            return None

        uploaded_file_id = file_obj['id']
        app.logger.info(f"File '{file_name}' uploaded with ID: {uploaded_file_id}. Setting permissions.")

        permission_result = _execute_google_api_call_with_retry(
            service.permissions().create,
            fileId=uploaded_file_id, body={'role': 'reader', 'type': 'anyone'}
        )
        
        if not permission_result or 'id' not in permission_result:
            app.logger.error(f"Failed to set permissions for '{file_name}' (ID: {uploaded_file_id}). File may be inaccessible.")
            return file_obj

        app.logger.info(f"Permissions set for '{file_name}' (ID: {uploaded_file_id}).")
        return file_obj

    except Exception as e:
        app.logger.error(f'Unexpected error during Drive upload for {file_name}: {e}', exc_info=True)
        return None

def upload_file_from_path_to_drive(file_path, file_name, mime_type, folder_id):
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        app.logger.error(f"File at path '{file_path}' is missing or empty. Aborting upload.")
        return None
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
    return _perform_drive_upload(media, file_name, mime_type, folder_id)

def upload_data_from_memory_to_drive(data_in_memory, file_name, mime_type, folder_id):
    media = MediaIoBaseUpload(data_in_memory, mimetype=mime_type, resumable=True)
    file_obj = _perform_drive_upload(media, file_name, mime_type, folder_id)
    return file_obj

def create_google_task(title, notes=None, due=None):
    service = get_google_tasks_service()
    if not service: 
        return None
    try:
        task_body = {'title': title, 'notes': notes, 'status': 'needsAction'}
        if due: 
            task_body['due'] = due
        return _execute_google_api_call_with_retry(service.tasks().insert, tasklist=GOOGLE_TASKS_LIST_ID, body=task_body)
    except HttpError as e:
        app.logger.error(f"Error creating Google Task: {e}")
        return None

def delete_google_task(task_id):
    service = get_google_tasks_service()
    if not service: 
        return False
    try:
        _execute_google_api_call_with_retry(service.tasks().delete, tasklist=GOOGLE_TASKS_LIST_ID, task=task_id)
        return True
    except HttpError as err:
        app.logger.error(f"API Error deleting task {task_id}: {err}")
        return False

def update_google_task(task_id, title=None, notes=None, status=None, due=None):
    service = get_google_tasks_service()
    if not service: 
        return None
    try:
        task = _execute_google_api_call_with_retry(service.tasks().get, tasklist=GOOGLE_TASKS_LIST_ID, task=task_id)
        if title is not None: 
            task['title'] = title
        if notes is not None: 
            task['notes'] = notes
        if status is not None:
            task['status'] = status

        if status == 'completed':
            task['completed'] = datetime.datetime.now(pytz.utc).isoformat().replace(ISO_UTC_OFFSET, ZULU_FORMAT_SUFFIX)
            task['due'] = None
        else:
            task.pop('completed', None)
            if due is not None:
                task['due'] = due
        if due is None and status == 'needsAction':
             pass

        return _execute_google_api_call_with_retry(service.tasks().update, tasklist=GOOGLE_TASKS_LIST_ID, task=task_id, body=task)
    except HttpError as e:
        app.logger.error(f"Failed to update task {task_id}: {e}")
        return None

def parse_customer_info_from_notes(notes):
    info = {'name': '', 'phone': '', 'address': '', 'map_url': None, 'organization': ''}
    if not notes: 
        return info

    org_match = re.search(r"หน่วยงาน:\s*(.*)", notes, re.IGNORECASE)
    name_match = re.search(r"ลูกค้า:\s*(.*)", notes, re.IGNORECASE)
    phone_match = re.search(r"เบอร์โทรศัพท์:\s*(.*)", notes, re.IGNORECASE)
    address_match = re.search(r"ที่อยู่:\s*(.*)", notes, re.IGNORECASE)
    map_url_match = re.search(r"(https?:\/\/[^\s]+|(?:\-?\d+\.\d+,\s*\-?\d+\.\d+))", notes)

    if org_match: 
        info['organization'] = org_match.group(1).strip().split(':')[-1].strip()
    if name_match: 
        info['name'] = name_match.group(1).strip().split(':')[-1].strip()
    if phone_match: 
        info['phone'] = phone_match.group(1).strip().split(':')[-1].strip()
    if address_match: 
        info['address'] = address_match.group(1).strip().split(':')[-1].strip()
    
    if map_url_match:
        coords_or_url = map_url_match.group(1).strip()
        if re.match(r"^\-?\d+\.\d+,\s*\-?\d+\.\d+$", coords_or_url):
            info['map_url'] = f"https://www.google.com/maps/search/?api=1&query={coords_or_url}"
        else:
            info['map_url'] = coords_or_url
    
    return info

def parse_customer_feedback_from_notes(notes):
    feedback_data = {}
    if not notes: 
        return feedback_data

    feedback_match = re.search(r"--- CUSTOMER_FEEDBACK_START ---\s*\n(.*?)\n--- CUSTOMER_FEEDBACK_END ---", notes, re.DOTALL)
    if feedback_match:
        try:
            feedback_data = json.loads(feedback_match.group(1))
        except json.JSONDecodeError:
            app.logger.warning("Failed to decode customer feedback JSON from notes.")
    return feedback_data

def parse_google_task_dates(task_item):
    parsed = task_item.copy()
    for key in ['created', 'due', 'completed']:
        if parsed.get(key):
            try:
                dt_utc = date_parse(parsed[key])
                parsed[f'{key}_formatted'] = dt_utc.astimezone(THAILAND_TZ).strftime("%d/%m/%y %H:%M")
                if key == 'due':
                    parsed['due_for_input'] = dt_utc.astimezone(THAILAND_TZ).strftime("%Y-%m-%dT%H:%M")
            except (ValueError, TypeError) as e:
                app.logger.warning(