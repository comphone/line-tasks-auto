from flask import Blueprint, render_template, request, redirect, url_for, abort
import utils
import google_services as gs
from settings_manager import settings_manager
import os

customer_bp = Blueprint('customer', __name__)

# --- Customer QR Code Generation Routes ---
# Reason for this group: All these routes are customer-facing and serve a similar purpose 
# of providing a QR code for a specific customer action. Grouping them in a dedicated 
# customer blueprint keeps the logic clean and separate from admin tools.

@customer_bp.route('/generate_qr/<qr_type>/<task_id>')
def generate_qr(qr_type, task_id):
    """
    A single, flexible route for generating all types of customer-facing QR codes.
    This combines all '/generate_..._qr/' routes from app_old.py into one.
    """
    task = gs.get_task_by_id(task_id)
    if not task:
        abort(404)

    target_url = ''
    page_title = 'QR Code'
    liff_id = os.environ.get('LIFF_ID_FORM') # Get LIFF ID from environment

    if qr_type == 'onboarding':
        if not liff_id:
            flash('LIFF_ID_FORM is not configured in the environment.', 'danger')
            return redirect(url_for('main.task_details', task_id=task_id))
        # This URL will be opened inside the LIFF browser
        onboarding_page_url = url_for('customer.customer_onboarding_page', task_id=task_id, _external=True)
        target_url = f"https://liff.line.me/{liff_id}?liff.state={onboarding_page_url}"
        page_title = 'QR Code สำหรับลงทะเบียนลูกค้า'
    
    elif qr_type == 'report':
        target_url = url_for('customer.public_task_report', task_id=task_id, _external=True)
        page_title = 'QR Code สำหรับดูรายงานสรุป'

    # Add other QR types here as needed
    # elif qr_type == 'payment':
    #     target_url = url_for('customer.payment_page', task_id=task_id, _external=True)
    #     page_title = 'QR Code สำหรับชำระเงิน'
    
    else:
        # If the qr_type is unknown, return a 404 error
        abort(404)

    qr_code_b64 = utils.generate_qr_code_base64(target_url)
    customer_info = utils.parse_customer_info_from_notes(task.get('notes', ''))
    
    return render_template('generate_qr_code.html', 
                           task=task, 
                           customer_info=customer_info, 
                           target_url=target_url, 
                           qr_code_base64=qr_code_b64,
                           page_title=page_title)

# --- Pages that are targets of the QR codes ---

@customer_bp.route('/onboarding/<task_id>')
def customer_onboarding_page(task_id):
    """Page for customer to confirm their LINE ID."""
    task = gs.get_task_by_id(task_id)
    if not task:
        abort(404)
    return render_template('customer_onboarding.html', task=task)

@customer_bp.route('/public_report/<task_id>')
def public_task_report(task_id):
    """Publicly accessible report for a completed task."""
    task = gs.get_task_by_id(task_id)
    if not task or task.get('status') != 'completed':
        abort(404)
    
    # This logic can be expanded based on app_old.py
    reports, _ = gs.parse_tech_report_from_notes(task.get('notes', ''))
    customer_info = utils.parse_customer_info_from_notes(task.get('notes', ''))

    return render_template('public_task_report.html', 
                           task=task,
                           reports=reports,
                           customer_info=customer_info,
                           settings=settings_manager.get_all_settings())
