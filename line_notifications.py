import os
from flask import url_for, flash, current_app
from linebot import LineBotApi
from linebot.models import TextSendMessage, FlexSendMessage

import utils
from settings_manager import get_app_settings

# ฟังก์ชันนี้จะดึง line_bot_api instance จาก app context
# เพื่อให้แน่ใจว่าเราใช้ instance ที่ถูกสร้างใน app.py
def get_line_bot_api():
    return current_app.line_bot_api

def send_new_task_notification(task):
    """ส่งการแจ้งเตือนเมื่อมีงานใหม่ถูกสร้างขึ้น"""
    line_bot_api = get_line_bot_api()
    admin_group_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
    if not admin_group_id or not line_bot_api: return

    customer_info = utils.parse_customer_info_from_notes(task.get('notes', ''))
    parsed_dates = utils.parse_google_task_dates(task)
    due_info = f"นัดหมาย: {parsed_dates.get('due_formatted')}" if parsed_dates.get('due_formatted') else "นัดหมาย: - (ยังไม่ระบุ)"
    location_info = f"พิกัด: {customer_info.get('map_url')}" if customer_info.get('map_url') else "พิกัด: - (ไม่มีข้อมูล)"

    message_text = (
        f"✨ มีงานใหม่เข้า!\n\n"
        f"ชื่องาน: {task.get('title', '-')}\n"
        f"ลูกค้า: {customer_info.get('name', '-')}\n"
        f"📞 โทร: {customer_info.get('phone', '-')}\n"
        f"🗓️ {due_info}\n"
        f"📍 {location_info}\n\n"
        f"ดูรายละเอียดในเว็บ:\n{url_for('main.task_details', task_id=task.get('id'), _external=True)}"
    )
    try:
        line_bot_api.push_message(admin_group_id, TextSendMessage(text=message_text))
    except Exception as e:
        current_app.logger.error(f"Error sending new task notification: {e}")

def send_completion_notification(task, technicians):
    """ส่งการแจ้งเตือนเมื่อมีการปิดงาน"""
    line_bot_api = get_line_bot_api()
    recipients = get_app_settings().get('line_recipients', {})
    admin_group_id = recipients.get('admin_group_id')
    tech_group_id = recipients.get('technician_group_id')
    if not (admin_group_id or tech_group_id) or not line_bot_api: return

    customer_info = utils.parse_customer_info_from_notes(task.get('notes', ''))
    technician_str = ", ".join(technicians) if technicians else "ไม่ได้ระบุ"
    message_text = (
        f"✅ ปิดงานเรียบร้อย\n\n"
        f"ชื่องาน: {task.get('title', '-')}\n"
        f"ลูกค้า: {customer_info.get('name', '-')}\n"
        f"ช่างผู้รับผิดชอบ: {technician_str}\n\n"
        f"ดูรายละเอียด: {url_for('main.task_details', task_id=task.get('id'), _external=True)}"
    )
    
    sent_to = set()
    try:
        if admin_group_id:
            line_bot_api.push_message(admin_group_id, TextSendMessage(text=message_text))
            sent_to.add(admin_group_id)
        if tech_group_id and tech_group_id not in sent_to:
            line_bot_api.push_message(tech_group_id, TextSendMessage(text=message_text))
    except Exception as e:
        current_app.logger.error(f"Failed to send completion notification for task {task['id']}: {e}")

def send_update_notification(task, new_due_date_str, reason, technicians, is_today):
    """ส่งการแจ้งเตือนเมื่อมีการเลื่อนนัดหรืออัปเดตงาน"""
    line_bot_api = get_line_bot_api()
    admin_group_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
    if not admin_group_id or not line_bot_api: return

    customer_info = utils.parse_customer_info_from_notes(task.get('notes', ''))
    technician_str = ", ".join(technicians) if technicians else "ไม่ได้ระบุ"
    title = "🗓️ อัปเดตงานวันนี้" if is_today else "🗓️ เลื่อนนัดหมาย"
    reason_str = f"รายละเอียด: {reason}\n" if is_today and reason else f"เหตุผล: {reason}\n" if reason else ""
    message_text = (
        f"{title}\n\n"
        f"ชื่องาน: {task.get('title', '-')}\n"
        f"ลูกค้า: {customer_info.get('name', '-')}\n"
        f"📞 โทร: {customer_info.get('phone', '-')}\n"
        f"นัดหมายใหม่: {new_due_date_str}\n"
        f"{reason_str}"
        f"ช่าง: {technician_str}\n\n"
        f"ดูรายละเอียดในเว็บ:\n{url_for('main.task_details', task_id=task.get('id'), _external=True)}"
    )
    try:
        line_bot_api.push_message(admin_group_id, TextSendMessage(text=message_text))
    except Exception as e:
        current_app.logger.error(f"Failed to send update/reschedule notification for task {task['id']}: {e}")

def send_problem_notification(task, problem_description):
    """ส่งการแจ้งเตือนเมื่อลูกค้าแจ้งปัญหา"""
    line_bot_api = get_line_bot_api()
    admin_group_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
    if not admin_group_id or not line_bot_api: return

    customer = utils.parse_customer_info_from_notes(task.get('notes', ''))
    notification_text = (
        f"🚨 ลูกค้าแจ้งปัญหา!\n\n"
        f"งาน: {task.get('title')}\n"
        f"ลูกค้า: {customer.get('name', 'N/A')}\n"
        f"ปัญหา: {problem_description}\n\n"
        f"ดูรายละเอียด: {url_for('main.task_details', task_id=task.get('id'), _external=True)}"
    )
    try:
        line_bot_api.push_message(admin_group_id, TextSendMessage(text=notification_text))
    except Exception as e:
        current_app.logger.error(f"Failed to send problem notification: {e}")

def test_line_notification():
    """ส่งข้อความทดสอบไปยังกลุ่มแอดมิน"""
    line_bot_api = get_line_bot_api()
    recipient_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
    if recipient_id and line_bot_api:
        try:
            line_bot_api.push_message(recipient_id, TextSendMessage(text="[ทดสอบ] นี่คือข้อความทดสอบจากระบบ"))
            flash(f'ส่งข้อความทดสอบไปที่ ID: {recipient_id} สำเร็จ!', 'success')
        except Exception as e:
            flash(f'เกิดข้อผิดพลาดในการส่ง: {e}', 'danger')
    else:
        flash('กรุณากำหนด "LINE Admin Group ID" ก่อน หรือตรวจสอบ Access Token', 'danger')