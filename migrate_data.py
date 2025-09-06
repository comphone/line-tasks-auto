import json
from datetime import datetime
from app import app, db, Customer, Job, Report, Attachment
from utils import get_google_tasks_for_report, parse_customer_profile_from_task
from dateutil.parser import parse as date_parse
import pytz

THAILAND_TZ = pytz.timezone('Asia/Bangkok')

def run_migration():
    """ The main migration logic. """
    print("--- Starting Data Migration from Google Tasks to SQL ---")

    # 1. Fetch all tasks from Google
    all_google_tasks = get_google_tasks_for_report(show_completed=True)
    if not all_google_tasks:
        print("No tasks found in Google Tasks. Migration finished.")
        return

    print(f"Found {len(all_google_tasks)} customer profiles (tasks) to migrate.")
    
    migrated_customers = 0
    migrated_jobs = 0
    migrated_reports = 0

    # 2. Loop through each Google Task (each is a customer profile)
    for task in all_google_tasks:
        google_task_id = task.get('id')
        
        # Check if this customer profile already exists
        existing_customer = Customer.query.filter_by(google_task_id=google_task_id).first()
        if existing_customer:
            print(f"Skipping customer '{task.get('title')}' (ID: {google_task_id}). Already migrated.")
            continue

        print(f"Migrating customer: {task.get('title')}")
        
        # Use the robust parser from utils.py
        profile_data = parse_customer_profile_from_task(task)
        customer_info = profile_data.get('customer_info', {})

        # 3. Create a new Customer record
        new_customer = Customer(
            google_task_id=google_task_id,
            name=customer_info.get('name') or task.get('title'),
            organization=customer_info.get('organization'),
            phone=customer_info.get('phone'),
            address=customer_info.get('address'),
            map_url=customer_info.get('map_url'),
            created_at=date_parse(task.get('created')) if task.get('created') else datetime.utcnow()
        )
        db.session.add(new_customer)
        
        # 4. Loop through jobs within the profile
        for job_data in profile_data.get('jobs', []):
            new_job = Job(
                customer=new_customer,
                google_job_id=job_data.get('job_id'),
                job_title=job_data.get('job_title'),
                status=job_data.get('status', 'needsAction'),
                created_date=date_parse(job_data.get('created_date')) if job_data.get('created_date') else datetime.utcnow(),
                due_date=date_parse(job_data.get('due_date')) if job_data.get('due_date') else None,
                completed_date=date_parse(job_data.get('completed_date')) if job_data.get('completed_date') else None
            )
            db.session.add(new_job)
            migrated_jobs += 1
            
            # 5. Loop through reports within the job
            for report_data in job_data.get('reports', []):
                new_report = Report(
                    job=new_job,
                    summary_date=date_parse(report_data.get('summary_date')),
                    report_type=report_data.get('type', 'report'),
                    work_summary=report_data.get('work_summary') or report_data.get('reason'),
                    technicians=",".join(report_data.get('technicians', [])),
                    is_internal=report_data.get('is_internal', False)
                )
                db.session.add(new_report)
                migrated_reports += 1
                
                # 6. Loop through attachments within the report
                for attachment_data in report_data.get('attachments', []):
                    new_attachment = Attachment(
                        report=new_report,
                        drive_file_id=attachment_data.get('id'),
                        file_name=attachment_data.get('name'),
                        file_url=attachment_data.get('url')
                    )
                    db.session.add(new_attachment)

        migrated_customers += 1

    # 7. Commit all changes to the database
    try:
        db.session.commit()
        print("\n--- Migration Complete! ---")
        print(f"✅ Migrated Customers: {migrated_customers}")
        print(f"✅ Migrated Jobs: {migrated_jobs}")
        print(f"✅ Migrated Reports: {migrated_reports}")
    except Exception as e:
        db.session.rollback()
        print(f"\n--- ❌ MIGRATION FAILED! ---")
        print(f"An error occurred: {e}")
        print("All database changes have been rolled back.")

# --- START: ✅ เพิ่มโค้ดส่วนนี้เพื่อให้รันไฟล์ได้โดยตรง ---
if __name__ == '__main__':
    with app.app_context():
        run_migration()
# --- END: ✅ เพิ่มโค้ดส่วนนี้ ---