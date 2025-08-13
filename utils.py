import re
import json
import pytz
from dateutil.parser import parse as date_parse
from cachetools import cached, TTLCache
from collections import defaultdict
from flask import current_app

# สร้าง Cache สำหรับไฟล์นี้โดยเฉพาะ
util_cache = TTLCache(maxsize=100, ttl=60)

# --- ฟังก์ชันที่เกี่ยวข้องกับ Google Tasks ---
def get_google_tasks_for_report(show_completed=True):
    service = current_app.config['get_google_tasks_service']()
    if not service: return None
    try:
        results = current_app.config['_execute_google_api_call_with_retry'](service.tasks().list, tasklist=current_app.config['GOOGLE_TASKS_LIST_ID'], showCompleted=show_completed, maxResults=100)
        return results.get('items', [])
    except Exception as err:
        current_app.logger.error(f"API Error getting tasks in utils: {err}")
        return None

def get_single_task(task_id):
    if not task_id: return None
    service = current_app.config['get_google_tasks_service']()
    if not service: return None
    try:
        return current_app.config['_execute_google_api_call_with_retry'](service.tasks().get, tasklist=current_app.config['GOOGLE_TASKS_LIST_ID'], task=task_id)
    except Exception as err:
        current_app.logger.error(f"Error getting single task {task_id} in utils: {err}")
        return None

# --- ฟังก์ชันประมวลผลข้อมูล (Parsers) ---
def parse_customer_info_from_notes(notes):
    info = {'name': '', 'phone': '', 'address': '', 'map_url': None, 'organization': ''}
    if not notes: return info
    org_match = re.search(r"หน่วยงาน:\s*(.*)", notes, re.IGNORECASE)
    name_match = re.search(r"ลูกค้า:\s*(.*)", notes, re.IGNORECASE)
    phone_match = re.search(r"เบอร์โทรศัพท์:\s*(.*)", notes, re.IGNORECASE)
    address_match = re.search(r"ที่อยู่:\s*(.*)", notes, re.IGNORECASE)
    map_url_match = re.search(r"(https?:\/\/[^\s]+|(?:\-?\d+\.\d+,\s*\-?\d+\.\d+))", notes)
    if org_match: info['organization'] = org_match.group(1).strip()
    if name_match: info['name'] = name_match.group(1).strip()
    if phone_match: info['phone'] = phone_match.group(1).strip()
    if address_match: info['address'] = address_match.group(1).strip()
    if map_url_match:
        coords_or_url = map_url_match.group(1).strip()
        if re.match(r"^\-?\d+\.\d+,\s*\-?\d+\.\d+$", coords_or_url):
            info['map_url'] = f"http://googleusercontent.com/maps/google.com/8{coords_or_url}"
        else:
            info['map_url'] = coords_or_url
    return info

def parse_tech_report_from_notes(notes):
    if not notes: return [], ""
    parts = re.split(r'\n\s*--- TECH_REPORT_START ---', notes)
    base_notes_with_feedback = parts[0]
    history = []
    for part in parts[1:]:
        end_match = re.search(r'(.*?)\n\s*--- TECH_REPORT_END ---', part, re.DOTALL)
        if end_match:
            json_str = end_match.group(1).strip()
            try:
                history.append(json.loads(json_str))
            except json.JSONDecodeError:
                current_app.logger.warning(f"Failed to decode tech report JSON: {json_str[:100]}...")
    base_notes_text = re.sub(r"--- CUSTOMER_FEEDBACK_START ---.*?--- CUSTOMER_FEEDBACK_END ---", "", base_notes_with_feedback, flags=re.DOTALL).strip()
    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    return history, base_notes_text

def parse_customer_feedback_from_notes(notes):
    if not notes: return {}
    feedback_match = re.search(r"--- CUSTOMER_FEEDBACK_START ---\s*\n(.*?)\n--- CUSTOMER_FEEDBACK_END ---", notes, re.DOTALL)
    if feedback_match:
        try:
            return json.loads(feedback_match.group(1))
        except json.JSONDecodeError:
            current_app.logger.warning("Failed to decode customer feedback JSON.")
    return {}

def parse_google_task_dates(task_item):
    THAILAND_TZ = pytz.timezone('Asia/Bangkok')
    parsed = task_item.copy()
    for key in ['created', 'due', 'completed']:
        if parsed.get(key):
            try:
                dt_utc = date_parse(parsed[key])
                parsed[f'{key}_formatted'] = dt_utc.astimezone(THAILAND_TZ).strftime("%d/%m/%y %H:%M")
                if key == 'due':
                    parsed['due_for_input'] = dt_utc.astimezone(THAILAND_TZ).strftime("%Y-%m-%dT%H:%M")
            except (ValueError, TypeError):
                parsed[f'{key}_formatted'] = ''
                if key == 'due': parsed['due_for_input'] = ''
        else:
            parsed[f'{key}_formatted'] = ''
            if key == 'due': parsed['due_for_input'] = ''
    return parsed

@cached(util_cache)
def get_customer_database():
    current_app.logger.info("Building customer database via utils...")
    all_tasks = get_google_tasks_for_report(show_completed=True)
    if not all_tasks:
        return []
    customers_dict = {}
    all_tasks.sort(key=lambda x: x.get('created', '0'), reverse=True)
    for task in all_tasks:
        _, base_notes = parse_tech_report_from_notes(task.get('notes', ''))
        customer_info = parse_customer_info_from_notes(base_notes)
        name = customer_info.get('name', '').strip()
        if name:
            customer_key = name.lower()
            if customer_key not in customers_dict:
                customers_dict[customer_key] = customer_info
    return list(customers_dict.values())

def _get_technician_report_data(year, month):
    app_settings = current_app.config['get_app_settings']()
    technician_list = app_settings.get('technician_list', [])
    official_tech_names = {tech.get('name', '').strip() for tech in technician_list if tech.get('name')}
    tasks = get_google_tasks_for_report(show_completed=True) or []
    report = defaultdict(lambda: {'count': 0, 'tasks': []})
    THAILAND_TZ = pytz.timezone('Asia/Bangkok')
    for task in tasks:
        if task.get('status') == 'completed' and task.get('completed'):
            try:
                completed_dt = date_parse(task['completed']).astimezone(THAILAND_TZ)
                if completed_dt.year == year and completed_dt.month == month:
                    history, _ = parse_tech_report_from_notes(task.get('notes', ''))
                    task_techs = {t_name.strip() for r in history for t_name in r.get('technicians', []) if isinstance(t_name, str)}
                    for tech_name in sorted(list(task_techs)):
                        if tech_name in official_tech_names:
                            report[tech_name]['count'] += 1
                            customer_name = parse_customer_info_from_notes(task.get('notes', '')).get('name', 'N/A')
                            report[tech_name]['tasks'].append({
                                'id': task.get('id'), 'title': task.get('title'),
                                'customer_name': customer_name,
                                'completed_formatted': completed_dt.strftime("%d/%m/%Y")
                            })
            except Exception as e:
                current_app.logger.error(f"Error processing task {task.get('id')} for tech report: {e}")
    for tech_name in report:
        report[tech_name]['tasks'].sort(key=lambda x: x['completed_formatted'])
    return dict(sorted(report.items())), technician_list