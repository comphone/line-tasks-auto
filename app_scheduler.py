import datetime
import pytz
import json
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dateutil.parser import parse as date_parse
from flask import current_app, url_for

# --- Local Module Imports ---
import google_services as gs
import utils
from settings_manager import get_app_settings
import line_notifications # แก้ไข: Import ทั้งโมดูลแทนที่จะนำเข้า line_bot_api โดยตรง
from linebot.models import TextSendMessage, FlexSendMessage, BubbleContainer, BoxComponent, TextComponent, SeparatorComponent, ButtonComponent, URIAction, PostbackAction
from utils import create_backup_zip

scheduler = BackgroundScheduler(daemon=True, timezone=pytz.timezone('Asia/Bangkok'))

def _create_customer_follow_up_flex_message(task_id, task_title, customer_name):
    """
    Creates a Flex Message for customer follow-up.
    """
    # This needs the app context to get the LIFF_ID_FORM
    liff_id = current_app.LIFF_ID_FORM
    problem_action = URIAction(
        label='🚨 ยังมีปัญหาอยู่',
        uri=f"https://liff.line.me/{liff_id}/customer/problem_form?task_id={task_id}"
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

def scheduled_backup_job():
    """Job for automatic backup to Google Drive."""
    with current_app.app_context():
        current_app.logger.info("--- Starting Scheduled Backup Job ---")
        
        memory_file_zip, filename_zip = create_backup_zip()
        
        if memory_file_zip and filename_zip:
            system_backup_folder_id = gs.find_or_create_drive_folder("System_Backups", gs.GOOGLE_DRIVE_FOLDER_ID)
            if system_backup_folder_id:
                if not gs.upload_data_from_memory_to_drive(memory_file_zip, filename_zip, 'application/zip', system_backup_folder_id):
                    current_app.logger.error("Automatic full system backup failed.")
            else:
                current_app.logger.error("Could not find or create System_Backups folder for backup.")
        else:
            current_app.logger.error("Failed to create full system backup zip.")
        
        if not gs.backup_settings_to_drive(get_app_settings()):
            current_app.logger.error("Automatic settings-only backup failed.")
        
        current_app.logger.info("--- Finished Scheduled Backup Job ---")
        return True # Indicate success for manual trigger
    return False

def scheduled_appointment_reminder_job():
    """Job for sending daily appointment reminders via LINE."""
    with current_app.app_context():
        current_app.logger.info("Running scheduled appointment reminder job...")
        recipients = get_app_settings().get('line_recipients', {})
        admin_group_id = recipients.get('admin_group_id')
        technician_group_id = recipients.get('technician_group_id')

        # แก้ไข: ดึง line_bot_api instance จาก current_app context
        line_bot_api_instance = line_notifications.get_line_bot_api()
        
        if not (admin_group_id or technician_group_id) or not line_bot_api_instance: return

        tasks_raw = gs.get_google_tasks_for_report(show_completed=False) or []
        today_thai = datetime.date.today()
        
        upcoming_appointments = []
        for task in tasks_raw:
             if task.get('status') == 'needsAction' and task.get('due'):
                try:
                    due_dt_utc = date_parse(task['due'])
                    if due_dt_utc.astimezone(utils.THAILAND_TZ).date() == today_thai:
                        upcoming_appointments.append(task)
                except (ValueError, TypeError):
                    current_app.logger.warning(f"Could not parse due date for reminder task {task.get('id')}")

        if not upcoming_appointments:
            current_app.logger.info("No upcoming appointments for today.")
            return

        upcoming_appointments.sort(key=lambda x: date_parse(x['due']))
        
        for task in upcoming_appointments:
            customer_info = utils.parse_customer_info_from_notes(task.get('notes', ''))
            parsed_dates = utils.parse_google_task_dates(task)
            location_info = f"พิกัด: {customer_info.get('map_url')}" if customer_info.get('map_url') else "พิกัด: - (ไม่มีข้อมูล)"
            message_text = (
                f"🔔 งานสำหรับวันนี้\n\n"
                f"ชื่องาน: {task.get('title', '-')}\n"
                f"👤 ลูกค้า: {customer_info.get('name', '-')}\n"
                f"📞 โทร: {customer_info.get('phone', '-')}\n"
                f"🗓️ นัดหมาย: {parsed_dates.get('due_formatted', '-')}\n"
                f"📍 {location_info}\n\n"
                f"🔗 ดูรายละเอียด/แก้ไข:\n{url_for('main.task_details', task_id=task.get('id'), _external=True)}"
            )
            try:
                sent_to = set()
                if admin_group_id:
                    line_bot_api_instance.push_message(admin_group_id, TextSendMessage(text=message_text))
                    sent_to.add(admin_group_id)
                if technician_group_id and technician_group_id not in sent_to:
                    line_bot_api_instance.push_message(technician_group_id, TextSendMessage(text=message_text))
            except Exception as e:
                current_app.logger.error(f"Failed to send appointment reminder for task {task['id']}: {e}")

def scheduled_customer_follow_up_job():
    """Job for sending follow-up messages to customers after task completion."""
    with current_app.app_context():
        current_app.logger.info("Running scheduled customer follow-up job...")
        admin_group_id = get_app_settings().get('line_recipients', {}).get('admin_group_id')
        tasks_raw = gs.get_google_tasks_for_report(show_completed=True) or []
        now_utc = datetime.datetime.now(pytz.utc)
        one_day_ago_utc = now_utc - datetime.timedelta(days=1)
        two_days_ago_utc = now_utc - datetime.timedelta(days=2)

        # แก้ไข: ดึง line_bot_api instance จาก current_app context
        line_bot_api_instance = line_notifications.get_line_bot_api()

        for task in tasks_raw:
            if task.get('status') == 'completed' and task.get('completed'):
                try:
                    completed_dt_utc = date_parse(task['completed'])
                    if two_days_ago_utc <= completed_dt_utc < one_day_ago_utc:
                        notes = task.get('notes', '')
                        feedback_data = utils.parse_customer_feedback_from_notes(notes)
                        if 'follow_up_sent_date' in feedback_data: continue

                        customer_info = utils.parse_customer_info_from_notes(notes)
                        customer_line_id = feedback_data.get('customer_line_user_id')
                        if not customer_line_id: continue

                        flex_content = _create_customer_follow_up_flex_message(task['id'], task['title'], customer_info.get('name', 'N/A'))
                        flex_message = FlexSendMessage(alt_text="สอบถามความพึงพอใจหลังการซ่อม", contents=flex_content)

                        try:
                            line_bot_api_instance.push_message(customer_line_id, flex_message)
                            feedback_data['follow_up_sent_date'] = datetime.datetime.now(utils.THAILAND_TZ).isoformat()
                            history_reports, base_notes = utils.parse_tech_report_from_notes(notes)
                            tech_reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history_reports])
                            new_notes = base_notes.strip()
                            if tech_reports_text: new_notes += tech_reports_text
                            new_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
                            gs.update_google_task(task['id'], notes=new_notes)
                            current_app.cache.clear()
                        except Exception as e:
                            current_app.logger.error(f"Failed to send direct follow-up to {customer_line_id}: {e}. Notifying admin.")
                            if admin_group_id:
                                line_bot_api_instance.push_message(admin_group_id, [TextSendMessage(text=f"⚠️ ส่ง Follow-up ให้ลูกค้า {customer_info.get('name')} ไม่สำเร็จ:"), flex_message])
                except Exception as e:
                    current_app.logger.warning(f"Could not process task {task.get('id')} for follow-up: {e}")

def initialize_scheduler(app):
    """Initializes and configures the APScheduler jobs."""
    with app.app_context():
        settings = get_app_settings()
        
        global scheduler
        if scheduler.running:
            scheduler.shutdown(wait=False)
        
        scheduler = BackgroundScheduler(daemon=True, timezone=pytz.timezone('Asia/Bangkok'))

        ab = settings.get('auto_backup', {})
        if ab.get('enabled'):
            scheduler.add_job(scheduled_backup_job, CronTrigger(hour=ab.get('hour_thai', 2), minute=ab.get('minute_thai', 0)), id='auto_system_backup', replace_existing=True)
            app.logger.info(f"Scheduled auto backup for {ab.get('hour_thai', 2)}:{ab.get('minute_thai', 0):02d} Thai time.")

        rt = settings.get('report_times', {})
        scheduler.add_job(scheduled_appointment_reminder_job, CronTrigger(hour=rt.get('appointment_reminder_hour_thai', 7)), id='daily_appointment_reminder', replace_existing=True)
        scheduler.add_job(scheduled_customer_follow_up_job, CronTrigger(hour=rt.get('customer_followup_hour_thai', 9)), id='daily_customer_followup', replace_existing=True)
        app.logger.info(f"Scheduled appointment reminders for {rt.get('appointment_reminder_hour_thai', 7)}:00 and customer follow-up for {rt.get('customer_followup_hour_thai', 9)}:05 Thai time.")

        if not scheduler.running:
            try:
                scheduler.start()
                app.logger.info("APScheduler started/reconfigured.")
            except (KeyboardInterrupt, SystemExit):
                pass

def cleanup_scheduler():
    if scheduler and scheduler.running:
        scheduler.shutdown()