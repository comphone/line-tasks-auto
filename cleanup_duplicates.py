import os
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from collections import defaultdict

# --- !! สำคัญ !! ---
# ตรวจสอบให้แน่ใจว่า import class ของ Model ทั้งหมดตรงกับใน app.py
from app import db, JobItem, StockMovement, StockLevel

def cleanup_job_item_duplicates_in_batches():
    """
    Standalone script to clean up duplicate JobItems in batches.
    This is designed to be run from the command line and is not subject to web timeouts.
    """
    # ตั้งค่าการเชื่อมต่อฐานข้อมูลจาก Environment Variable เดียวกับแอปหลัก
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        print("!!! ERROR: ไม่พบ DATABASE_URL ใน environment variables")
        return

    print(f"เชื่อมต่อฐานข้อมูล...")
    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine)
    session = Session()
    print("เชื่อมต่อสำเร็จ!")

    try:
        total_deleted_count = 0
        batch_size = 2000  # ลดขนาด Batch ลงเพื่อความปลอดภัย
        run_count = 0

        print("\n--- เริ่มกระบวนการล้างข้อมูลค่าใช้จ่ายที่ซ้ำซ้อน ---")
        print(f"จะทำการลบข้อมูลทีละ {batch_size} รายการ\n")

        while True:
            run_count += 1
            print(f"--- รอบที่ {run_count} ---")

            # Subquery to find groups of duplicates and the ID of the item to keep (the minimum ID)
            subquery = session.query(
                JobItem.job_id,
                JobItem.item_name,
                func.min(JobItem.id).label('id_to_keep')
            ).group_by(
                JobItem.job_id,
                JobItem.item_name
            ).having(
                func.count(JobItem.id) > 1
            ).subquery()

            # Query to get the IDs of all items that are part of a duplicate group
            # but are NOT the item that should be kept.
            items_to_delete_query = session.query(JobItem.id).join(
                subquery,
                (JobItem.job_id == subquery.c.job_id) &
                (JobItem.item_name == subquery.c.item_name) &
                (JobItem.id > subquery.c.id_to_keep)
            )

            # Fetch a batch of IDs to delete
            item_ids_to_delete = [item[0] for item in items_to_delete_query.limit(batch_size).all()]

            if not item_ids_to_delete:
                print("ไม่พบรายการที่ซ้ำซ้อนเพิ่มเติมแล้ว สิ้นสุดการทำงาน")
                break

            print(f"พบ {len(item_ids_to_delete)} รายการที่ซ้ำซ้อนในรอบนี้ กำลังดำเนินการลบ...")

            # Delete associated movements and then the items for the current batch
            session.query(StockMovement).filter(StockMovement.job_item_id.in_(item_ids_to_delete)).delete(synchronize_session=False)
            deleted_in_batch = session.query(JobItem).filter(JobItem.id.in_(item_ids_to_delete)).delete(synchronize_session=False)

            session.commit()

            total_deleted_count += deleted_in_batch
            print(f"ลบสำเร็จ {deleted_in_batch} รายการ (รวมทั้งหมด {total_deleted_count} รายการ)")
            print("-" * 20)

        print("\n--- กระบวนการล้างข้อมูลเสร็จสิ้น ---")
        print(f"สรุป: ลบรายการค่าใช้จ่ายที่ซ้ำซ้อนออกทั้งหมด {total_deleted_count} รายการ")
        print("\nขั้นตอนต่อไปที่แนะนำ: ให้ไปที่หน้า ตั้งค่า > จัดการข้อมูล แล้วกดปุ่ม 'คำนวณสต็อกใหม่ทั้งหมด' เพื่ออัปเดตสต็อกให้ถูกต้อง")

    except Exception as e:
        session.rollback()
        print(f"\n!!! เกิดข้อผิดพลาดร้ายแรง !!!")
        print(str(e))
    finally:
        session.close()
        print("ปิดการเชื่อมต่อฐานข้อมูลแล้ว")

if __name__ == '__main__':
    cleanup_job_item_duplicates_in_batches()