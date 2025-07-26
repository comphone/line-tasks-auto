import os
import json
import datetime
import pytz

from flask import Blueprint, render_template, request, abort, jsonify, url_for, current_app
from linebot import LineBotApi
from linebot.models import TextSendMessage

import google_services as gs
import utils
# --- FIX: Import directly from settings_manager ---
from settings_manager import get_app_settings
from line_notifications import send_problem_notification

customer_bp = Blueprint('customer', __name__, url_prefix='/customer')

def get_line_bot_api():
    return current_app.line_bot_api

@customer_bp.route('/report/<task_id>')
def public_task_report(task_id):
    """Displays a public report for a completed task, including costs."""
    task = gs.get_single_task(task_id)
    if not task or task.get('status') != 'completed':
        abort(404)
    
    notes = task.get('notes', '')
    customer = utils.parse_customer_info_from_notes(notes)
    reports, _ = utils.parse_tech_report_from_notes(notes)
    latest_report = reports[0] if reports else {}
    
    app_settings = get_app_settings() 
    equipment = latest_report.get('equipment_used', [])
    catalog = {item['item_name']: item for item in app_settings.get('equipment_catalog', [])}
    costs, total = [], 0.0
    
    if isinstance(equipment, list):
        for item in equipment:
            name, qty = item.get('item'), item.get('quantity', 0)
            if isinstance(qty, (int, float)):
                cat_item = catalog.get(name, {})
                price = float(cat_item.get('price', 0))
                subtotal = qty * price
                total += subtotal
                costs.append({'item': name, 'quantity': qty, 'unit': cat_item.get('unit', ''), 'price_per_unit': price, 'subtotal': subtotal})
            else:
                costs.append({'item': name, 'quantity': qty, 'unit': catalog.get(name, {}).get('unit', ''), 'price_per_unit': 'N/A', 'subtotal': 'N/A'})
    
    return render_template('public_task_report.html', task=task, customer_info=customer, latest_report=latest_report, detailed_costs=costs, total_cost=total, settings=app_settings)

@customer_bp.route('/onboarding/<task_id>')
def customer_onboarding_page(task_id):
    """Page for a customer to link their LINE account to a task."""
    task = gs.get_single_task(task_id)
    if not task: abort(404)
    return render_template('customer_onboarding.html', task=task, LINE_LOGIN_CHANNEL_ID=os.environ.get('LINE_LOGIN_CHANNEL_ID'))

@customer_bp.route('/generate_onboarding_qr/<task_id>')
def generate_customer_onboarding_qr(task_id):
    """Generates a QR code for the customer onboarding page."""
    task = gs.get_single_task(task_id)
    liff_id = os.environ.get('LIFF_ID_FORM')
    if not task or not liff_id: abort(404)
    
    onboarding_url = url_for('customer.customer_onboarding_page', task_id=task_id, _external=True)
    liff_url = f"https://liff.line.me/{liff_id}?liff.state={onboarding_url}"
    qr_code = utils.generate_qr_code_base64(liff_url)
    customer = utils.parse_customer_info_from_notes(task.get('notes', ''))
    return render_template('generate_onboarding_qr.html', qr_code_base64=qr_code, task=task, customer_info=customer)

@customer_bp.route('/problem_form')
def customer_problem_form():
    """Form for customers to report a problem with a completed task."""
    task_id = request.args.get('task_id')
    task = gs.get_single_task(task_id)
    if not task: abort(404)
    parsed = utils.parse_google_task_dates(task)
    parsed['customer'] = utils.parse_customer_info_from_notes(task.get('notes', ''))
    return render_template('customer_problem_form.html', task=parsed, LINE_LOGIN_CHANNEL_ID=os.environ.get('LINE_LOGIN_CHANNEL_ID'))

@customer_bp.route('/submit_problem', methods=['POST'])
def submit_customer_problem():
    """API endpoint to handle problem form submissions."""
    data = request.json
    task_id, problem_desc, user_id = data.get('task_id'), data.get('problem_description'), data.get('customer_line_user_id')
    if not task_id or not problem_desc: return jsonify({"status": "error"}), 400
    
    task = gs.get_single_task(task_id)
    if not task: return jsonify({"status": "error"}), 404
    
    notes = task.get('notes', '')
    feedback = utils.parse_customer_feedback_from_notes(notes)
    feedback.update({
        'feedback_date': datetime.datetime.now(utils.THAILAND_TZ).isoformat(), 
        'feedback_type': 'problem_reported', 
        'customer_line_user_id': user_id,
        'problem_description': problem_desc
    })

    reports_history, base = utils.parse_tech_report_from_notes(notes)
    reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in reports_history])
    final_notes = f"{base.strip()}"
    if reports_text: final_notes += reports_text
    final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"

    gs.update_google_task(task_id=task_id, notes=final_notes, status='needsAction')
    current_app.cache.clear()
    
    send_problem_notification(task, problem_desc)

    return jsonify({"status": "success"})

@customer_bp.route('/save_line_id', methods=['POST'])
def save_customer_line_id():
    """API endpoint to save a customer's LINE User ID to a task."""
    data = request.json
    task_id, user_id = data.get('task_id'), data.get('customer_line_user_id')
    if not task_id or not user_id: return jsonify({"status": "error"}), 400
    
    task = gs.get_single_task(task_id)
    if not task: return jsonify({"status": "error"}), 404
    
    notes = task.get('notes', '')
    feedback = utils.parse_customer_feedback_from_notes(notes)
    
    if feedback.get('customer_line_user_id') != user_id:
        feedback['customer_line_user_id'] = user_id
        feedback['id_saved_date'] = datetime.datetime.now(utils.THAILAND_TZ).isoformat()
        
        reports_history, base = utils.parse_tech_report_from_notes(notes)
        reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in reports_history])
        final_notes = f"{base.strip()}"
        if reports_text: final_notes += reports_text
        final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        
        if gs.update_google_task(task_id=task_id, notes=final_notes):
            current_app.cache.clear()
            shop = get_app_settings().get('shop_info', {})
            customer = utils.parse_customer_info_from_notes(notes)
            welcome = f"เรียน คุณ{customer.get('name', 'ลูกค้า')},\n\nขอบคุณที่เชื่อมต่อกับ Comphone ครับ/ค่ะ!\nเราจะใช้ LINE นี้เพื่อส่งข้อมูลสำคัญเกี่ยวกับบริการครับ\n\nติดต่อ:\nโทร: {shop.get('contact_phone', '-')}\nLINE ID: {shop.get('line_id', '-')}"
            try:
                get_line_bot_api().push_message(user_id, TextSendMessage(text=welcome))
            except Exception as e:
                current_app.logger.error(f"Failed to send welcome message to {user_id}: {e}")
            return jsonify({"status": "success"})
        else: 
            return jsonify({"status": "error"}), 500
            
    return jsonify({"status": "success", "message": "already saved"})
