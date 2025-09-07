import sys
import os
from googleapiclient.errors import HttpError

# เพิ่ม Path ของโปรเจกต์เพื่อให้สามารถ import app ได้
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from app import app, db, Attachment
from app import get_google_drive_service

def repair_permissions():
    """
    สคริปต์สำหรับวนลูปเช็ค Attachment ทั้งหมดในฐานข้อมูล
    และตั้งค่า Permission ใน Google Drive ให้ถูกต้อง ('anyone with link can read')
    """
    print("--- 🚀 เริ่มกระบวนการซ่อมแซม Permission ของไฟล์ทั้งหมด ---")

    drive_service = get_google_drive_service()
    if not drive_service:
        print("❌ เกิดข้อผิดพลาด: ไม่สามารถเชื่อมต่อกับ Google Drive API ได้")
        return

    with app.app_context():
        all_attachments = Attachment.query.all()

    total = len(all_attachments)
    print(f"🔎 พบไฟล์แนบในฐานข้อมูลทั้งหมด {total} รายการที่ต้องตรวจสอบ...")

    success_count = 0
    error_count = 0

    for index, attachment in enumerate(all_attachments):
        file_id = attachment.drive_file_id
        print(f"[{index + 1}/{total}] กำลังตรวจสอบไฟล์ ID: {file_id} ... ", end="")

        try:
            # สร้าง Permission ใหม่ (ปลอดภัยที่จะรันซ้ำ)
            drive_service.permissions().create(
                fileId=file_id,
                body={'role': 'reader', 'type': 'anyone'},
                fields='id'
            ).execute()
            print("✅ ตั้งค่า Permission สำเร็จ")
            success_count += 1
        except HttpError as e:
            if e.resp.status == 404:
                print("❌ ไม่พบไฟล์ใน Drive")
            else:
                print(f"❌ เกิดข้อผิดพลาดจาก API: {e}")
            error_count += 1
        except Exception as e:
            print(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}")
            error_count += 1

    print("\n--- ✅ กระบวนการทั้งหมดเสร็จสิ้น ---")
    print(f"สรุปผล: สำเร็จ {success_count} | ผิดพลาด/ไม่พบไฟล์ {error_count}")
    print("------------------------------------")

if __name__ == "__main__":
    repair_permissions()