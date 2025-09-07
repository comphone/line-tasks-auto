import os
import sys
from datetime import datetime
from googleapiclient.errors import HttpError

# เพิ่ม Path ของโปรเจกต์เพื่อให้สามารถ import app ได้
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from app import app, db, Job, Customer, Report, Attachment
from app import get_google_drive_service, find_or_create_drive_folder, sanitize_filename

def consolidate_legacy_attachments():
    """
    สคริปต์สำหรับตรวจสอบและย้ายไฟล์ Attachment เก่าทั้งหมดใน Google Drive
    ให้มาอยู่ในโครงสร้างโฟลเดอร์ที่ถูกต้องตามระบบปัจจุบัน
    """
    print("--- 🚀 เริ่มกระบวนการตรวจสอบและย้ายไฟล์ Attachment เก่า ---")
    
    drive_service = get_google_drive_service()
    if not drive_service:
        print("❌ เกิดข้อผิดพลาด: ไม่สามารถเชื่อมต่อกับ Google Drive API ได้")
        return

    # 1. หา ID ของโฟลเดอร์หลักสำหรับเก็บไฟล์แนบ
    attachments_base_folder_id = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')
    if not attachments_base_folder_id:
        print("❌ เกิดข้อผิดพลาด: ไม่ได้ตั้งค่า GOOGLE_DRIVE_FOLDER_ID ในไฟล์ .env")
        return
        
    print(f"🗂️  โฟลเดอร์หลักของโปรเจกต์: {attachments_base_folder_id}")

    # 2. ดึงข้อมูล Attachment ทั้งหมดจากฐานข้อมูล
    with app.app_context():
        # Query ข้อมูลที่จำเป็นทั้งหมดในครั้งเดียวเพื่อประสิทธิภาพ
        attachments_to_check = db.session.query(Attachment, Job, Customer).select_from(Attachment).join(Report).join(Job).join(Customer).all()

    total_attachments = len(attachments_to_check)
    print(f"🔎 พบไฟล์แนบในฐานข้อมูลทั้งหมด {total_attachments} รายการที่ต้องตรวจสอบ...")

    moved_count = 0
    skipped_count = 0
    error_count = 0
    not_found_count = 0

    # 3. วนลูปตรวจสอบและย้ายไฟล์แต่ละรายการ
    for index, (attachment, job, customer) in enumerate(attachments_to_check):
        file_id = attachment.drive_file_id
        
        print(f"\n[{index + 1}/{total_attachments}] กำลังตรวจสอบไฟล์ ID: {file_id} (ของงาน ID: {job.id})")

        try:
            # 3.1. ดึงข้อมูลไฟล์จาก Drive เพื่อดูว่าตอนนี้อยู่ที่ไหน
            file_metadata = drive_service.files().get(
                fileId=file_id, fields='id, name, parents, trashed'
            ).execute()

            if file_metadata.get('trashed'):
                print(f"    - 🗑️ สถานะ: ไฟล์ถูกลบไปแล้ว (อยู่ในถังขยะ) -> ข้าม")
                skipped_count += 1
                continue

            current_parents = file_metadata.get('parents', [])
            if not current_parents:
                print(f"    - ⚠️ สถานะ: ไฟล์ไม่มีโฟลเดอร์แม่ (Orphaned File) -> ข้าม")
                skipped_count += 1
                continue
            
            current_parent_id = current_parents[0]

            # 3.2. คำนวณหาโฟลเดอร์ปลายทางที่ถูกต้อง
            monthly_folder_name = job.created_date.strftime('%Y-%m')
            sanitized_customer_name = sanitize_filename(customer.name or 'Unknown_Customer')
            customer_job_folder_name = f"{sanitized_customer_name} - {job.id}"

            # 3.3. ตรวจสอบว่าไฟล์อยู่ในตำแหน่งที่ถูกต้องแล้วหรือยัง
            # โดยเช็คว่า parent folder ของไฟล์ เป็นโฟลเดอร์ของงานนี้หรือไม่
            parent_folder_info = drive_service.files().get(fileId=current_parent_id, fields='name').execute()
            if parent_folder_info['name'] == customer_job_folder_name:
                print(f"    - ✅ สถานะ: อยู่ในโฟลเดอร์ที่ถูกต้องแล้ว ('{customer_job_folder_name}') -> ข้าม")
                skipped_count += 1
                continue

            # 3.4. ถ้ายังไม่อยู่ในที่ที่ถูก ให้ทำการย้าย
            print(f"    - 🚚 สถานะ: ไฟล์อยู่ผิดที่! (ปัจจุบันอยู่ในโฟลเดอร์ '{parent_folder_info['name']}')")
            
            # สร้าง/หาโฟลเดอร์ปลายทาง
            print(f"    - 📂 กำลังหา/สร้างโฟลเดอร์ปลายทาง: '{monthly_folder_name}/{customer_job_folder_name}'")
            attachments_folder_id = find_or_create_drive_folder("Task_Attachments", attachments_base_folder_id)
            monthly_folder_id = find_or_create_drive_folder(monthly_folder_name, attachments_folder_id)
            destination_folder_id = find_or_create_drive_folder(customer_job_folder_name, monthly_folder_id)

            if not destination_folder_id:
                print(f"    - ❌ ไม่สามารถสร้างโฟลเดอร์ปลายทางได้ -> ข้าม")
                error_count += 1
                continue
            
            # ย้ายไฟล์
            drive_service.files().update(
                fileId=file_id,
                addParents=destination_folder_id,
                removeParents=current_parent_id
            ).execute()
            
            print(f"    - ✨ ย้ายไฟล์ '{file_metadata['name']}' สำเร็จ!")
            moved_count += 1

        except HttpError as e:
            if e.resp.status == 404:
                print(f"    - ❌ สถานะ: ไม่พบไฟล์นี้ใน Google Drive (อาจถูกลบถาวร) -> ข้าม")
                not_found_count += 1
            else:
                print(f"    - ❌ เกิดข้อผิดพลาดจาก Google API: {e} -> ข้าม")
                error_count += 1
        except Exception as e:
            print(f"    - ❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e} -> ข้าม")
            error_count += 1

    print("\n--- ✅ กระบวนการทั้งหมดเสร็จสิ้น ---")
    print(f"สรุปผล:")
    print(f"  - ย้ายไฟล์สำเร็จ: {moved_count} รายการ")
    print(f"  - ข้าม (อยู่ในที่ที่ถูกแล้ว/ถูกลบ): {skipped_count} รายการ")
    print(f"  - ไม่พบไฟล์ใน Drive: {not_found_count} รายการ")
    print(f"  - เกิดข้อผิดพลาด: {error_count} รายการ")
    print("------------------------------------")


if __name__ == "__main__":
    consolidate_legacy_attachments()