# routes/web.py

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app
from models import db, Customer # Import db and Customer model
import datetime
import pytz
import os
import re
import json
import mimetypes
from werkzeug.utils import secure_filename
from geopy.distance import geodesic
from cachetools import cached, TTLCache # Import for local caching in helpers

from flask_login import login_required, current_user # Import Flask-Login decorators and current_user
from routes.auth import role_required # Import our custom role_required decorator

web_bp = Blueprint('web', __name__)

# --- Helper Functions (Temporarily duplicated from app.py to avoid circular import issues
# in this refactoring phase. In a more advanced refactor, these would be in a 'services' module) ---

# Re-define or import necessary helpers here, referencing current_app as needed
# These are copies of the ones in app.py with current_app context.

def get_google_service(api_name, api_version):
    creds = None
    token_path = 'token.json'
    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')

    if google_token_json_str:
        try:
            creds_info = json.loads(google_token_json_str)
            creds = Credentials.from_authorized_user_info(creds_info, current_app.config['SCOPES'])
        except Exception as e:
            current_app.logger.warning(f"Could not load token from GOOGLE_TOKEN_JSON: {e}")
            creds = None
    elif os.path.exists(token_path):
        creds = Credentials.from_authorized_file(token_path, current_app.config['SCOPES'])

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                from google.auth.transport.requests import Request as GoogleAuthRequest # Local import to avoid conflict
                creds.refresh(GoogleAuthRequest())
            except Exception as e:
                current_app.logger.error(f"Error refreshing Google token, re-authenticating: {e}")
                creds = None
        if not creds:
            if os.path.exists(current_app.config['GOOGLE_CREDENTIALS_FILE_NAME']):
                flow = InstalledAppFlow.from_client_secrets_file(current_app.config['GOOGLE_CREDENTIALS_FILE_NAME'], current_app.config['SCOPES'])
                creds = flow.run_console()
            else:
                current_app.logger.error("Google credentials file not found.")
                return None
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
        current_app.logger.info(f"New token saved to {token_path}. Please update GOOGLE_TOKEN_JSON on Render.")

    if creds:
        return build(api_name, api_version, credentials=creds)
    return None

def get_google_tasks_service(): return get_google_service('tasks', 'v1')
def get_google_calendar_service(): return get_google_service('calendar', 'v3')
def get_google_drive_service(): return get_google_service('drive', 'v3')

def upload_file_to_google_drive(file_path, file_name, mime_type):
    service = get_google_drive_service()
    if not service: current_app.logger.error("ไม่สามารถเชื่อมต่อ Google Drive service ได้สำหรับการอัปโหลด"); return None
    if not current_app.config['GOOGLE_DRIVE_FOLDER_ID']: current_app.logger.warning("ไม่ได้ตั้งค่า GOOGLE_DRIVE_FOLDER_ID ไม่สามารถอัปโหลดไฟล์ไป Google Drive ได้"); return None
    try:
        file_metadata = {'name': file_name, 'parents': [current_app.config['GOOGLE_DRIVE_FOLDER_ID']], 'mimeType': mime_type}
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        file_obj = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink, webContentLink').execute()
        service.permissions().create(fileId=file_obj['id'], body={'role': 'reader', 'type': 'anyone'}, fields='id').execute()
        current_app.logger.info(f"ไฟล์ถูกอัปโหลดไปที่ Google Drive: {file_obj.get('webViewLink')}")
        return file_obj.get('webViewLink')
    except HttpError as error: current_app.logger.error(f'เกิดข้อผิดพลาดขณะอัปโหลดไป Google Drive: {error}'); return None
    except Exception as e: current_app.logger.error(f'เกิดข้อผิดพลาดที่ไม่คาดคิดระหว่างการอัปโหลด Drive: {e}'); return None

def create_google_task(title, notes=None, due=None):
    service = get_google_tasks_service()
    if not service: return None
    try: return service.tasks().insert(tasklist=current_app.config['GOOGLE_TASKS_LIST_ID'], body={'title': title, 'notes': notes, 'status': 'needsAction', 'due': due}).execute()
    except HttpError as e: current_app.logger.error(f"Error creating Google Task: {e}"); return None

def create_google_calendar_event(summary, location, description, start_time, end_time, timezone='Asia/Bangkok'):
    service = get_google_calendar_service()
    if not service: current_app.logger.error("Failed to get Google Calendar service."); return None
    try: return service.events().insert(calendarId='primary', body={'summary': summary, 'location': location, 'description': description, 'start': {'dateTime': start_time, 'timeZone': timezone}, 'end': {'dateTime': end_time, 'timeZone': timezone}, 'reminders': {'useDefault': True}}).execute()
    except HttpError as e: current_app.logger.error(f"Error creating Google Calendar Event: {e}"); return None

def delete_google_task(task_id):
    service = get_google_tasks_service()
    if not service: return False
    try: service.tasks().delete(tasklist=current_app.config['GOOGLE_TASKS_LIST_ID'], task=task_id).execute(); current_app.logger.info(f"Successfully deleted task ID: {task_id}"); return True
    except HttpError as err: current_app.logger.error(f"API Error deleting task {task_id}: {err}"); return False

def update_google_task(task_id, title=None, notes=None, status=None):
    service = get_google_tasks_service()
    if not service: return None
    try:
        task = service.tasks().get(tasklist=current_app.config['GOOGLE_TASKS_LIST_ID'], task=task_id).execute()
        if title is not None: task['title'] = title
        if notes is not None: task['notes'] = notes
        if status is not None: task['status'] = status; task['completed'] = datetime.datetime.now(pytz.utc).isoformat() if status == 'completed' else None
        return service.tasks().update(tasklist=current_app.config['GOOGLE_TASKS_LIST_ID'], task=task_id, body=task).execute()
    except HttpError as e: current_app.logger.error(f"Failed to update task {task_id}: {e}"); return None

@cached(lambda: current_app.cache) # Use current_app.cache instance
def get_google_tasks_for_report(show_completed=True):
    current_app.logger.info(f"Cache miss/expired. Calling Google Tasks API... (show_completed={show_completed})")
    service = get_google_tasks_service()
    if not service: return None
    try: return service.tasks().list(tasklist=current_app.config['GOOGLE_TASKS_LIST_ID'], showCompleted=show_completed, maxResults=100).execute().get('items', [])
    except HttpError as err: current_app.logger.error(f"API Error getting tasks: {err}"); return None

def get_single_task(task_id):
    service = get_google_tasks_service()
    if not service: return None
    try: return service.tasks().get(tasklist=current_app.config['GOOGLE_TASKS_LIST_ID'], task=task_id).execute()
    except HttpError as err: current_app.logger.error(f"Error getting single task {task_id}: {err}"); return None

def extract_lat_lon_from_notes(notes):
    if not notes: return None, None
    match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", notes)
    if match: return (float(match.group(1)), float(match.group(2)))
    match = re.search(r"พิกัด:\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", notes)
    if match: return (float(match.group(1)), float(match.group(2)))
    map_url_regex = r"https?://(?:www\.)?(?:google\.com/maps/place/|maps\.app\.goo\.gl/)(?:[^/]+/@)?(-?\d+\.\d+),(-?\d+\.\d+)"
    map_url_match = re.search(map_url_regex, notes)
    if map_url_match: return (float(map_url_match.group(1)), float(map_url_match.group(2)))
    return None, None

def find_nearby_jobs(completed_task_id, radius_km=5):
    completed_task = get_single_task(completed_task_id)
    if not completed_task: return []
    origin_lat, origin_lon = extract_lat_lon_from_notes(completed_task.get('notes', ''))
    if origin_lat is None or origin_lon is None: current_app.logger.info(f"Completed task {completed_task_id} has no location data. Skipping nearby search."); return []
    origin_coords = (origin_lat, origin_lon)
    pending_tasks = get_google_tasks_for_report(show_completed=False)
    if not pending_tasks: return []
    nearby_jobs = []
    for task in pending_tasks:
        if task.get('id') == completed_task_id: continue
        task_lat, task_lon = extract_lat_lon_from_notes(task.get('notes', ''))
        if task_lat is not None and task_lon is not None:
            task_coords = (task_lat, task_lon)
            distance = geodesic(origin_coords, task_coords).kilometers
            if distance <= radius_km: task['distance_km'] = round(distance, 1); nearby_jobs.append(task)
    nearby_jobs.sort(key=lambda x: x['distance_km'])
    return nearby_jobs

def parse_customer_info_from_notes(notes):
    info = {'name': '', 'phone': '', 'address': '', 'detail': '', 'map_url': None}
    if not notes: return info
    base_notes_content = re.sub(r"--- TECH_REPORT_START ---\s*.*?\s*--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL).strip()
    lines = [line.strip() for line in base_notes_content.split('\n') if line.strip()]
    if len(lines) > 0: info['name'] = lines[0]
    if len(lines) > 1: info['phone'] = lines[1]
    if len(lines) > 2: info['address'] = lines[2]
    map_url_regex = r"https?://(?:www\.)?(?:google\.com/maps|maps\.app\.goo\.gl)\S+"
    detail_start_line_idx = 3
    if len(lines) > 3:
        if re.match(map_url_regex, lines[3]): info['map_url'] = lines[3]; detail_start_line_idx = 4
        else: detail_start_line_idx = 3
    if len(lines) > detail_start_line_idx -1: info['detail'] = "\n".join(lines[detail_start_line_idx:])
    return info
    
def parse_google_task_dates(task_item):
    parsed_task = task_item.copy()
    for key in ['created', 'due', 'completed']:
        if key in parsed_task and parsed_task[key]:
            try:
                dt_utc = datetime.datetime.fromisoformat(parsed_task[key].replace('Z', '+00:00'))
                dt_thai = dt_utc.astimezone(current_app.config['THAILAND_TZ'])
                parsed_task[f'{key}_formatted'] = dt_thai.strftime("%d/%m/%y %H:%M" if key == 'due' else "%d/%m/%y %H:%M:%S")
            except (ValueError, TypeError): parsed_task[f'{key}_formatted'] = ''
        else: parsed_task[f'{key}_formatted'] = ''
    return parsed_task

def parse_tech_report_from_notes(notes):
    if not notes: return [], ""
    report_blocks = re.findall(r"--- TECH_REPORT_START ---\s*\n(.*?)\n--- TECH_REPORT_END ---", notes, re.DOTALL)
    history = []
    for json_str in report_blocks:
        try: history.append(json.loads(json_str))
        except json.JSONDecodeError as e: current_app.logger.error(f"Error decoding JSON in tech report block: {e}, Content: {json_str[:100]}...")
    original_notes_text = re.sub(r"--- TECH_REPORT_START ---\s*.*?\s*--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL).strip()
    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, original_notes_text

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in current_app.config['ALLOWED_EXTENSIONS']

def create_task_flex_message(task):
    customer_info = parse_customer_info_from_notes(task.get('notes', ''))
    parsed_dates = parse_google_task_dates(task)
    update_url = url_for('web.update_task_details', task_id=task.get('id'), _external=True)
    phone_action = None
    if customer_info.get('phone'): phone_number = re.sub(r'\D', '', customer_info['phone']); phone_action = URIAction(label=customer_info['phone'], uri=f"tel:{phone_number}")
    map_action = None
    map_url = customer_info.get('map_url')
    if map_url: map_action = URIAction(label="📍 เปิด Google Maps", uri=map_url)
    body_contents = [
        TextComponent(text=task.get('title', 'ไม่มีหัวข้อ'), weight='bold', size='xl', wrap=True),
        BoxComponent(layout='vertical', margin='lg', spacing='sm', contents=[
            BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='ลูกค้า', color='#aaaaaa', size='sm', flex=2), TextComponent(text=customer_info.get('name', '') or '-', wrap=True, color='#666666', size='sm', flex=5)]),
            BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='โทร', color='#aaaaaa', size='sm', flex=2), TextComponent(text=customer_info.get('phone', '') or '-', wrap=True, color='#1E90FF', size='sm', flex=5, action=phone_action, decoration='underline' if phone_action else 'none')]),
            BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='นัดหมาย', color='#aaaaaa', size='sm', flex=2), TextComponent(text=parsed_dates.get('due_formatted', '') or '-', wrap=True, color='#666666', size='sm', flex=5)])
        ])
    ]
    footer_contents = [ButtonComponent(style='link', height='sm', action=URIAction(label='📝 อัปเดต/สรุปงาน', uri=update_url))]
    if map_action: footer_contents.insert(0, ButtonComponent(style='link', height='sm', action=map_action)); footer_contents.insert(1, SeparatorComponent(margin='md'))
    bubble = BubbleContainer(direction='ltr', header=BoxComponent(layout='vertical', contents=[TextComponent(text='📢 แจ้งเตือนงาน', weight='bold', color='#ffffff')], background_color='#007BFF', padding_all='12px'), body=BoxComponent(layout='vertical', contents=body_contents), footer=BoxComponent(layout='vertical', spacing='sm', contents=footer_contents, flex=0), action=URIAction(uri=update_url))
    return FlexMessage(alt_text=f"แจ้งเตือนงาน: {task.get('title', '')}", contents=bubble)

def create_nearby_job_suggestion_message(completed_task_title, nearby_tasks):
    if not nearby_tasks: return None
    bubbles = []
    for task in nearby_tasks[:12]:
        customer_info = parse_customer_info_from_notes(task.get('notes', ''))
        update_url = url_for('web.update_task_details', task_id=task.get('id'), _external=True)
        phone_action = None
        if customer_info.get('phone'): phone_number = re.sub(r'\D', '', customer_info['phone']); phone_action = URIAction(label=f"📞 โทร: {customer_info['phone']}", uri=f"tel:{phone_number}")
        bubble = BubbleContainer(direction='ltr', header=BoxComponent(layout='vertical', background_color='#FFDDC2', contents=[TextComponent(text='💡 แนะนำงานใกล้เคียง!', weight='bold', color='#BF5A00', size='md')]), body=BoxComponent(layout='vertical', spacing='md', contents=[TextComponent(text=f"ห่างไป {task['distance_km']} กม.", size='sm', color='#555555'), TextComponent(text=f"ลูกค้า: {customer_info.get('name', '') or 'N/A'}", weight='bold', size='lg', wrap=True), TextComponent(text=task.get('title', '-'), wrap=True, size='sm', color='#666666')]), footer=BoxComponent(layout='vertical', spacing='sm', contents=([phone_action] if phone_action else []) + [ButtonComponent(style='link', height='sm', action=URIAction(label='ดูรายละเอียด/แผนที่', uri=update_url))]))
        bubbles.append(bubble)
    alt_text = f"คุณอยู่ใกล้กับงานอื่น! หลังจากปิดงาน '{completed_task_title}'"
    return FlexMessage(alt_text=alt_text, contents=CarouselContainer(contents=bubbles))

def create_customer_history_carousel(tasks, customer_name):
    if not tasks: return None
    bubbles = []
    tasks.sort(key=lambda x: x.get('created', ''), reverse=True)
    for task in tasks[:12]:
        parsed = parse_google_task_dates(task)
        update_url = url_for('web.update_task_details', task_id=task.get('id'), _external=True)
        status_text, status_color = "รอดำเนินการ", "#FFA500"
        if task.get('status') == 'completed': status_text, status_color = "เสร็จสิ้น", "#28A745"
        elif 'due' in task and task['due']:
            try:
                due_dt_utc = datetime.datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
                if due_dt_utc < datetime.datetime.now(pytz.utc): status_text, status_color = "ยังไม่ดำเนินการ", "#DC3545"
            except (ValueError, TypeError): pass
        bubble = BubbleContainer(direction='ltr', body=BoxComponent(layout='vertical', spacing='md', contents=[TextComponent(text=task.get('title', 'N/A'), weight='bold', size='lg', wrap=True), SeparatorComponent(margin='md'), BoxComponent(layout='vertical', margin='md', spacing='sm', contents=[BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='สถานะ', color='#aaaaaa', size='sm', flex=2), TextComponent(text=status_text, wrap=True, color=status_color, size='sm', flex=5, weight='bold')]), BoxComponent(layout='baseline', spacing='sm', contents=[TextComponent(text='วันที่สร้าง', color='#aaaaaa', size='sm', flex=2), TextComponent(text=parsed.get('created_formatted', '') or '-', wrap=True, color='#666666', size='sm', flex=5)])])]), footer=BoxComponent(layout='vertical', spacing='sm', contents=[ButtonComponent(style='link', height='sm', action=URIAction(label='ดูรายละเอียด / อัปเดต', uri=update_url))]))
        bubbles.append(bubble)
    return CarouselContainer(contents=bubbles)

def get_app_settings():
    current_app.logger.info("Using MOCK get_app_settings()")
    return {
        'report_times': {
            'appointment_reminder_hour_thai': int(os.environ.get('APPOINTMENT_REMINDER_HOUR_THAI', 7)), # Read from ENV or default
            'outstanding_report_hour_thai': int(os.environ.get('OUTSTANDING_REPORT_HOUR_THAI', 20)) # Read from ENV or default
        },
        'line_recipients': {
            'admin_group_id': os.environ.get('LINE_ADMIN_GROUP_ID', ''),
            'manager_user_id': os.environ.get('LINE_MANAGER_USER_ID', ''),
            'technician_group_id': os.environ.get('LINE_TECHNICIAN_GROUP_ID', '')
        }
    }

def save_app_settings(settings_data):
    current_app.logger.info(f"Using MOCK save_app_settings() with data: {settings_data}")
    # In a real app, save this to DB. For now, it's a mock.
    # You would update environment variables or a settings table here.
    return True

def check_for_nearby_jobs_and_notify(completed_task_id, source_id):
    nearby_tasks = find_nearby_jobs(completed_task_id)
    if nearby_tasks:
        completed_task = get_single_task(completed_task_id)
        suggestion_message = create_nearby_job_suggestion_message(completed_task.get('title', ''), nearby_tasks)
        if suggestion_message:
            try:
                current_app.line_messaging_api.push_message(PushMessageRequest(to=source_id, messages=[suggestion_message]))
                current_app.logger.info(f"Sent nearby job suggestions to {source_id}")
            except Exception as e:
                current_app.logger.error(f"Failed to send nearby job suggestion: {e}")


# --- Flask Routes ---
@web_bp.route("/", methods=['GET', 'POST'])
@login_required # Require login for form submission and viewing
@role_required(roles=['admin', 'technician', 'customer']) # Allow all logged-in roles to create/view form
def form_page():
    if request.method == 'POST':
        customer_name = request.form.get('customer')
        customer_phone = request.form.get('phone')
        address = request.form.get('address')
        detail = request.form.get('detail')
        appointment_str = request.form.get('appointment')
        map_url_from_form = request.form.get('latitude_longitude')

        today_str = datetime.datetime.now(current_app.config['THAILAND_TZ']).strftime('%d/%m/%y')
        title = f"งานลูกค้า: {customer_name or 'ไม่ระบุชื่อลูกค้า'} ({today_str})"
        
        # --- Save/Retrieve Customer to/from DB ---
        customer_obj = Customer.query.filter_by(phone=customer_phone).first() if customer_phone else None
        if not customer_obj:
            customer_obj = Customer(
                name=customer_name or '',
                phone=customer_phone or '',
                address=address or '',
                map_url=map_url_from_form,
                latitude=extract_lat_lon_from_notes(map_url_from_form)[0],
                longitude=extract_lat_lon_from_notes(map_url_from_form)[1]
            )
            db.session.add(customer_obj)
            try:
                db.session.commit()
                current_app.logger.info(f"New customer added to DB: {customer_obj.name}")
            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f"Error adding customer to DB: {e}")
                customer_obj = None
        else:
            customer_obj.name = customer_name or customer_obj.name
            customer_obj.address = address or customer_obj.address
            customer_obj.map_url = map_url_from_form or customer_obj.map_url
            customer_obj.latitude = extract_lat_lon_from_notes(map_url_from_form)[0] or customer_obj.latitude
            customer_obj.longitude = extract_lat_lon_from_notes(map_url_from_form)[1] or customer_obj.longitude
            try:
                db.session.commit()
                current_app.logger.info(f"Existing customer updated in DB: {customer_obj.name}")
            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f"Error updating customer in DB: {e}")


        # Construct notes based on line-by-line format for Google Tasks
        notes_lines = []
        notes_lines.append(customer_name or '')
        notes_lines.append(customer_phone or '')
        notes_lines.append(address or '')
        
        if map_url_from_form:
            notes_lines.append(map_url_from_form)
        
        if detail:
            notes_lines.extend(detail.split('\n'))

        while notes_lines and notes_lines[-1] == '':
            notes_lines.pop()

        notes = "\n".join(notes_lines)

        due_date_gmt, start_time_iso, end_time_iso = None, None, None
        if appointment_str:
            try:
                dt_local = current_app.config['THAILAND_TZ'].localize(datetime.datetime.strptime(appointment_str, "%Y-%m-%d %H:%M"))
                due_date_gmt = dt_local.astimezone(pytz.utc).isoformat()
                start_time_iso = dt_local.isoformat()
                end_time_iso = (dt_local + datetime.timedelta(hours=1)).isoformat()
            except ValueError:
                current_app.logger.error(f"Invalid appointment format: {appointment_str}")

        created_task = create_google_task(title, notes=notes, due=due_date_gmt)

        if created_task and start_time_iso:
            current_app.logger.info("Creating Google Calendar event...")
            create_google_calendar_event(summary=title, location=address or '', description=notes, start_time=start_time_iso, end_time=end_time_iso)
        
        if created_task:
            flex_message = create_task_flex_message(created_task)
            settings = get_app_settings()
            recipients = [id for id in [settings['line_recipients'].get('admin_group_id'), settings['line_recipients'].get('technician_group_id')] if id]
            if recipients:
                try:
                    if isinstance(recipients, list):
                        for recipient_id in recipients:
                             current_app.line_messaging_api.push_message(PushMessageRequest(to=recipient_id, messages=[flex_message]))
                    else:
                        current_app.line_messaging_api.push_message(PushMessageRequest(to=recipients, messages=[flex_message]))
                except Exception as e:
                    current_app.logger.error(f"Failed to push Flex Message: {e}")
            flash('สร้างงานและส่งแจ้งเตือนเรียบร้อยแล้ว!', 'success')
            return redirect(url_for('web.summary'))
        else:
            flash('เกิดข้อผิดพลาดในการสร้างงาน', 'danger')
            return redirect(url_for('web.form_page'))

    return render_template('form.html')

@web_bp.route('/search_customers')
@login_required # Require login
@role_required(roles=['admin', 'technician']) # Only admins and technicians can search customers
def search_customers():
    query = request.args.get('q', '').strip()
    if len(query) < 2:
        return jsonify([])
    
    # Search in DB first
    customers_from_db = Customer.query.filter(
        (Customer.name.ilike(f'%{query}%')) | 
        (Customer.phone.ilike(f'%{query}%'))
    ).limit(10).all()

    results = []
    for cust in customers_from_db:
        results.append({
            'name': cust.name,
            'phone': cust.phone or '',
            'address': cust.address or '',
            'map_url': cust.map_url or ''
        })
    
    return jsonify(results)


@web_bp.route('/summary')
@login_required # Require login
@role_required(roles=['admin', 'technician', 'customer']) # All logged-in users can view summary
def summary():
    """Displays the task summary page with search functionality and status filtering."""
    search_query = request.args.get('search_query', '').strip().lower()
    status_filter = request.args.get('status_filter', 'all').strip()

    tasks_raw = get_google_tasks_for_report(show_completed=True)
    
    if tasks_raw is None:
        flash('ไม่สามารถเชื่อมต่อกับ Google Tasks ได้ในขณะนี้', 'danger')
        tasks_raw = []

    current_time_utc = datetime.datetime.now(pytz.utc)

    # First, apply status filter
    filtered_by_status_tasks = []
    for task_item in tasks_raw:
        task_status = task_item.get('status')
        is_overdue_check = False
        if task_status == 'needsAction' and 'due' in task_item and task_item.get('due'):
            try:
                due_dt_utc = datetime.datetime.fromisoformat(task_item['due'].replace('Z', '+00:00'))
                if due_dt_utc < current_time_utc:
                    is_overdue_check = True
            except (ValueError, TypeError):
                pass

        if status_filter == 'all':
            filtered_by_status_tasks.append(task_item)
        elif status_filter == 'completed' and task_status == 'completed':
            filtered_by_status_tasks.append(task_item)
        elif status_filter == 'needsAction' and task_status == 'needsAction' and not is_overdue_check:
            filtered_by_status_tasks.append(task_item)
        elif status_filter == 'overdue' and is_overdue_check:
            filtered_by_status_tasks.append(task_item)

    # Then, apply search query filter to the results from status filter
    final_filtered_tasks = []
    for task in filtered_by_status_tasks:
        if not search_query or \
           search_query in task.get('title', '').lower() or \
           search_query in parse_customer_info_from_notes(task.get('notes', '')).get('name', '').lower() or \
           search_query in parse_customer_info_from_notes(task.get('notes', '')).get('phone', '') or \
           search_query in parse_customer_info_from_notes(task.get('notes', '')).get('address', '').lower() or \
           search_query in parse_customer_info_from_notes(task.get('notes', '')).get('detail', '').lower():
            final_filtered_tasks.append(task)


    tasks = []
    total_summary_stats = {'needsAction': 0, 'completed': 0, 'overdue': 0, 'total': len(tasks_raw)}
    for task_item in tasks_raw:
        task_status = task_item.get('status')
        is_overdue_check = False
        if task_status == 'needsAction' and 'due' in task_item and task_item.get('due'):
            try:
                due_dt_utc = datetime.datetime.fromisoformat(task_item['due'].replace('Z', '+00:00'))
                if due_dt_utc < current_time_utc:
                    is_overdue_check = True
            except (ValueError, TypeError): pass
        
        if task_status == 'completed':
            total_summary_stats['completed'] += 1
        elif task_status == 'needsAction':
            total_summary_stats['needsAction'] += 1
            if is_overdue_check:
                total_summary_stats['overdue'] += 1


    for task_item in final_filtered_tasks:
        parsed_task = parse_google_task_dates(task_item)
        parsed_task['customer'] = parse_customer_info_from_notes(parsed_task.get('notes', ''))
        
        history, original_notes_text_removed_tech_reports = parse_tech_report_from_notes(parsed_task.get('notes', ''))
        parsed_task['tech_reports_history'] = history
        parsed_task['notes_display'] = original_notes_text_removed_tech_reports 
        
        status = task_item.get('status')
        if status == 'completed':
            parsed_task['display_status'] = 'เสร็จสิ้น'
        elif status == 'needsAction':
            is_overdue = False
            if 'due' in task_item and task_item.get('due'):
                try:
                    due_dt_utc = datetime.datetime.fromisoformat(task_item['due'].replace('Z', '+00:00'))
                    if due_dt_utc < current_time_utc:
                        is_overdue = True
                except (ValueError, TypeError): pass
            parsed_task['display_status'] = 'ยังไม่ดำเนินการ' if is_overdue else 'รอดำเนินการ'
        tasks.append(parsed_task)

    tasks.sort(key=lambda x: x.get('created', ''), reverse=True)
    
    return render_template("tasks_summary.html", 
                           tasks=tasks, 
                           summary=total_summary_stats,
                           search_query=search_query,
                           status_filter=status_filter)

@web_bp.route('/update_task/<task_id>', methods=['GET', 'POST'])
@login_required # Require login
@role_required(roles=['admin', 'technician']) # Only admins and technicians can update tasks
def update_task_details(task_id):
    """Displays and handles updates for a single task, showing history."""
    service = get_google_tasks_service()
    if not service: abort(503, "Google Tasks service is unavailable.")

    try:
        task_raw = service.tasks().get(tasklist=current_app.config['GOOGLE_TASKS_LIST_ID'], task=task_id).execute()
        task = parse_google_task_dates(task_raw)
        
        customer_info_from_task = parse_customer_info_from_notes(task.get('notes', ''))
        history, original_notes_text_removed_tech_reports = parse_tech_report_from_notes(task.get('notes', ''))
        
        task['customer_name_initial'] = customer_info_from_task.get('name', '')
        task['customer_phone_initial'] = customer_info_from_task.get('phone', '')
        task['customer_address_initial'] = customer_info_from_task.get('address', '')
        task['customer_detail_initial'] = customer_info_from_task.get('detail', '')
        task['map_url_initial'] = customer_info_from_task.get('map_url', '')

        task['tech_reports_history'] = history
        task['notes_display'] = original_notes_text_removed_tech_reports 
        
        if history and 'next_appointment' in history[0] and history[0]['next_appointment']:
            try:
                next_app_dt_utc = datetime.datetime.fromisoformat(history[0]['next_appointment'].replace('Z', '+00:00'))
                task['tech_next_appointment_datetime_local'] = next_app_dt_utc.astimezone(current_app.config['THAILAND_TZ']).strftime("%Y-%m-%dT%H:%M")
            except (ValueError, TypeError): task['tech_next_appointment_datetime_local'] = ''
        else: task['tech_next_appointment_datetime_local'] = ''

    except HttpError: abort(404, "Task not found.")

    if request.method == 'POST':
        original_status = task.get('status')
        new_status = request.form.get('status')
        work_summary = request.form.get('work_summary', '').strip()
        equipment_used = request.form.get('equipment_used', '').strip()
        time_taken = request.form.get('time_taken', '').strip()
        next_appointment_date_str = request.form.get('next_appointment_date', '').strip()

        updated_customer_name = request.form.get('customer_name', '').strip()
        updated_customer_phone = request.form.get('customer_phone', '').strip()
        updated_address = request.form.get('address', '').strip()
        updated_detail = request.form.get('detail', '').strip()
        updated_map_url = request.form.get('latitude_longitude', '').strip()

        # --- Update Customer in DB (if exists, or create new if phone changed) ---
        customer_obj = Customer.query.filter_by(phone=updated_customer_phone).first() if updated_customer_phone else None
        if customer_obj:
            # Update existing customer
            customer_obj.name = updated_customer_name or customer_obj.name
            customer_obj.address = updated_address or customer_obj.address
            customer_obj.map_url = updated_map_url or customer_obj.map_url
            customer_obj.latitude = extract_lat_lon_from_notes(updated_map_url)[0] or customer_obj.latitude
            customer_obj.longitude = extract_lat_lon_from_notes(updated_map_url)[1] or customer_obj.longitude
            try:
                db.session.commit()
                current_app.logger.info(f"Customer updated in DB via task update: {customer_obj.name}")
            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f"Error updating customer in DB: {e}")
        elif updated_customer_phone: # If no customer with this phone, create a new one
            new_customer = Customer(
                name=updated_customer_name or '',
                phone=updated_customer_phone or '',
                address=updated_address or '',
                map_url=updated_map_url,
                latitude=extract_lat_lon_from_notes(updated_map_url)[0],
                longitude=extract_lat_lon_from_notes(updated_map_url)[1]
            )
            db.session.add(new_customer)
            try:
                db.session.commit()
                current_app.logger.info(f"New customer created in DB via task update: {new_customer.name}")
            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f"Error creating new customer in DB: {e}")

        # --- Handle File Uploads from web form to Google Drive ---
        all_attachment_urls = []
        for report in task.get('tech_reports_history', []):
            all_attachment_urls.extend(report.get('attachment_urls', []))
        
        if 'files[]' in request.files:
            files = request.files.getlist('files[]')
            for file in files:
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    temp_filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
                    file.save(temp_filepath)

                    mime_type = file.mimetype if file.mimetype else mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                    
                    drive_url = upload_file_to_google_drive(temp_filepath, filename, mime_type)
                    
                    if drive_url:
                        all_attachment_urls.append(drive_url)
                    else:
                        current_app.logger.error(f"Failed to upload {filename} to Google Drive.")
                    
                    os.remove(temp_filepath)

        all_attachment_urls = list(set(all_attachment_urls))

        # --- Prepare new tech report data ---
        next_appointment_gmt = None
        if new_status == 'needsAction' and next_appointment_date_str:
            try:
                dt_local = current_app.config['THAILAND_TZ'].localize(datetime.datetime.fromisoformat(next_appointment_date_str))
                next_appointment_gmt = dt_local.astimezone(pytz.utc).isoformat()
            except ValueError: current_app.logger.error(f"Invalid next appointment date format: {next_appointment_date_str}")
        
        current_lat = request.form.get('current_lat')
        current_lon = request.form.get('current_lon')
        current_location_url = None
        if current_lat and current_lon:
            current_location_url = f"https://www.google.com/maps/search/?api=1&query={current_lat},{current_lon}"

        new_tech_report_data = {
            'summary_date': datetime.datetime.now(current_app.config['THAILAND_TZ']).strftime("%Y-%m-%d %H:%M:%S"),
            'work_summary': work_summary, 'equipment_used': equipment_used, 'time_taken': time_taken,
            'next_appointment': next_appointment_gmt, 'attachment_urls': all_attachment_urls,
            'location_url': current_location_url
        }
        
        history, _ = parse_tech_report_from_notes(task_raw.get('notes', ''))
        
        base_notes_lines = []
        base_notes_lines.append(updated_customer_name or '')
        base_notes_lines.append(updated_customer_phone or '')
        base_notes_lines.append(updated_address or '')
        
        if updated_map_url:
             base_notes_lines.append(updated_map_url)
        
        if updated_detail:
            base_notes_lines.extend(updated_detail.split('\n'))

        while base_notes_lines and base_notes_lines[-1] == '':
            base_notes_lines.pop()

        updated_base_notes = "\n".join(base_notes_lines)

        all_reports_list = sorted(history + [new_tech_report_data], key=lambda x: x.get('summary_date'))
        
        all_reports_text = ""
        for report in all_reports_list:
            report_to_dump = report.copy()
            report_to_dump['attachment_urls'] = report_to_dump.get('attachment_urls', [])
            report_to_dump['location_url'] = report_to_dump.get('location_url', None)

            all_reports_text += f"\n\n--- TECH_REPORT_START ---\n{json.dumps(report_to_dump, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---"
        
        final_updated_notes = updated_base_notes + all_reports_text

        updated_task = update_google_task(
            task_id, 
            title=f"งานลูกค้า: {updated_customer_name or 'ไม่ระบุชื่อลูกค้า'} ({datetime.datetime.now(current_app.config['THAILAND_TZ']).strftime('%d/%m/%y')})",
            notes=final_updated_notes, 
            status=new_status
        )

        if updated_task:
            flash('อัปเดตงานเรียบร้อยแล้ว!', 'success')
            if new_status == 'completed' and original_status != 'completed':
                settings = get_app_settings()
                tech_group_id = settings['line_recipients'].get('technician_group_id')
                if tech_group_id:
                    check_for_nearby_jobs_and_notify(updated_task.get('id'), tech_group_id)
        else:
            flash('เกิดข้อผิดพลาดในการอัปเดตงาน', 'danger')

        return redirect(url_for('web.summary'))
    
    return render_template('update_task_details.html', task=task)
    
@web_bp.route('/uploads/<filename>')
def uploaded_file(filename):
    """Serves uploaded files. (Primarily for local temp storage / legacy direct links)"""
    return send_from_directory(current_app.config['UPLOAD_FOLDER'], filename)

@web_bp.route('/settings', methods=['GET', 'POST'])
@login_required # Require login
@role_required(roles=['admin']) # Only admins can access settings
def settings_page():
    """Handles the settings page display and form submission."""
    if request.method == 'POST':
        settings_data = {
            'report_times': {
                'appointment_reminder_hour_thai': int(request.form.get('appointment_reminder_hour')),
                'outstanding_report_hour_thai': int(request.form.get('outstanding_report_hour'))
            },
            'line_recipients': {
                'admin_group_id': request.form.get('admin_group_id', '').strip(),
                'manager_user_id': request.form.get('manager_user_id', '').strip(),
                'technician_group_id': request.form.get('technician_group_id', '').strip()
            }
        }
        if save_app_settings(settings_data):
            flash('บันทึกการตั้งค่าเรียบร้อยแล้ว!', 'success')
        else:
            flash('เกิดข้อผิดพลาดในการบันทึกการตั้งค่า', 'danger')
        return redirect(url_for('web.settings_page'))
    
    current_settings = get_app_settings()
    return render_template('settings_page.html', settings=current_settings)

@web_bp.route('/delete_task/<task_id>', methods=['POST'])
@login_required # Require login
@role_required(roles=['admin']) # Only admins can delete tasks
def delete_task(task_id):
    """Handles the deletion of a task."""
    if delete_google_task(task_id):
        flash('ลบรายการงานเรียบร้อยแล้ว', 'success')
    else:
        flash('เกิดข้อผิดพลาดในการลบงาน', 'danger')
    return redirect(url_for('web.summary'))
