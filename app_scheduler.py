import datetime
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dateutil.parser import parse as date_parse
import json

import google_services as gs
import utils
from settings_manager import get_app_settings
from line_notifications import line_bot_api # ใช้ line_bot_api ที่สร้างไว้แล้ว
from linebot.models import TextSendMessage, FlexSendMessage

# สร้าง scheduler instance ไว้ใช้งานร่วมกัน
scheduler = BackgroundScheduler(daemon=True, timezone=pytz.timezone('Asia/Bangkok'))

def scheduled_backup_job():
    """Job สำหรับการสำรองข้อมูลอัตโนมัติไปยัง Google Drive"""
    # ต้อง import ภายในฟังก์ชันเพื่อหลีกเลี่ยง circular import
    from app import _create_backup_zip, app
    with app.app_context():
        app.logger.info("--- Starting Scheduled Backup Job ---")
        system_backup_folder_id = gs.find_or_create_drive_folder("System_Backups", gs.GOOGLE_DRIVE_FOLDER_ID)
        if not system_backup_folder_id:
            app.logger.error("Could not find or create System_Backups folder for backup.")
            return

        memory_file_zip, filename_zip = _create_backup_zip()
        if memory_file_zip and filename_zip:
            if not gs.upload_data_from_memory_to_drive(memory_file_zip, filename_zip, 'application/zip', system_backup_folder_id):
                app.logger.error("Automatic full system backup failed.")
        else:
            app.logger.error("Failed to create full system backup zip.")
        
        if not gs.backup_settings_to_drive(get_app_settings()):
            app.logger.error("Automatic settings-only backup failed.")
        
        app.logger.info("--- Finished Scheduled Backup Job ---")

def scheduled_appointment_reminder_job():
    """Job สำหรับส่งการแจ้งเตือนงานประจำวันไปยัง LINE"""
    from app import app
    with app.app_context():
        app.logger.info("Running scheduled appointment reminder job...")
        recipients = get_app_settings().get('line_recipients', {})
        admin_group_id = recipients.get('admin_group_id')
        technician_group_id = recipients.get('technician_group_id')
        if not (admin_group_id or technician_group_id) or not line_bot_api: return

        tasks_raw = gs.get_google_tasks_for_report(show_completed=False) or []
        today_thai = datetime.date.today()
        upcoming_appointments = [
            task for task in tasks_raw
            if task.get('status') == 'needsAction' and task.get('due') and
            date_parse(task['due']).astimezone(utils.THAILAND_TZ).date() == today_thai
        ]
        if not upcoming_appointments: return

        upcoming_appointments.sort(key=lambda x: date_parse(x['due']))
        # ... (ส่วนที่เหลือของ logic การส่งข้อความ)
        pass # Placeholder for brevity

def scheduled_customer_follow_up_job():
    """Job สำหรับส่งข้อความติดตามลูกค้าหลังงานเสร็จ"""
    from app import app
    with app.app_context():
        app.logger.info("Running scheduled customer follow-up job...")
        # ... (logic การดึง task และส่ง flex message ติดตามผล)
        pass # Placeholder for brevity

def initialize_scheduler(app):
    """เริ่มต้นและตั้งค่าการทำงานของ scheduler"""
    with app.app_context():
        settings = get_app_settings()
        
        global scheduler
        if scheduler.running:
            scheduler.shutdown(wait=False)
        
        scheduler = BackgroundScheduler(daemon=True, timezone=pytz.timezone('Asia/Bangkok'))

        ab = settings.get('auto_backup', {})
        if ab.get('enabled'):
            scheduler.add_job(scheduled_backup_job, CronTrigger(hour=ab.get('hour_thai', 2), minute=ab.get('minute_thai', 0)), id='auto_system_backup', replace_existing=True)

        rt = settings.get('report_times', {})
        scheduler.add_job(scheduled_appointment_reminder_job, CronTrigger(hour=rt.get('appointment_reminder_hour_thai', 7)), id='daily_appointment_reminder', replace_existing=True)
        scheduler.add_job(scheduled_customer_follow_up_job, CronTrigger(hour=rt.get('customer_followup_hour_thai', 9)), id='daily_customer_followup', replace_existing=True)

        if not scheduler.running:
            scheduler.start()
            app.logger.info("APScheduler started/reconfigured.")

def cleanup_scheduler():
    if scheduler and scheduler.running:
        scheduler.shutdown()
