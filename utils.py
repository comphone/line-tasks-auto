import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd
import os
from functools import lru_cache
import json
from datetime import datetime
import pytz

# ตั้งค่าโซนเวลาของประเทศไทย
BANGKOK_TZ = pytz.timezone('Asia/Bangkok')

@lru_cache(maxsize=32)
def get_worksheet(sheet_name):
    """
    เชื่อมต่อกับ Google Sheets และคืนค่า worksheet ที่ระบุ
    ใช้ cache เพื่อหลีกเลี่ยงการเชื่อมต่อใหม่ทุกครั้ง
    """
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            'https://www.googleapis.com/auth/spreadsheets',
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive"
        ]

        creds_json_str = os.getenv('GOOGLE_SHEETS_CREDENTIALS')
        spreadsheet_key = os.getenv('SPREADSHEET_KEY')

        if not spreadsheet_key:
            raise ValueError("SPREADSHEET_KEY environment variable not set.")

        if creds_json_str:
            # สำหรับ Production: ใช้ credentials จาก environment variable
            creds_dict = json.loads(creds_json_str)
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        elif os.path.exists('credentials.json'):
            # สำหรับ Local Development: ใช้ credentials จากไฟล์
            print("Using credentials from credentials.json for local development.")
            creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
        else:
            raise ValueError("Google Sheets credentials not found. Set GOOGLE_SHEETS_CREDENTIALS environment variable or place credentials.json in the root directory.")

        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(spreadsheet_key)
        return spreadsheet.worksheet(sheet_name)

    except gspread.exceptions.SpreadsheetNotFound:
        print(f"Error: Spreadsheet with key '{spreadsheet_key}' not found.")
        raise
    except gspread.exceptions.WorksheetNotFound:
        print(f"Error: Worksheet '{sheet_name}' not found in the spreadsheet.")
        raise
    except Exception as e:
        print(f"An unexpected error occurred in get_worksheet: {e}")
        raise

def get_all_records(sheet_name):
    """ดึงข้อมูลทั้งหมดจากชีท"""
    try:
        worksheet = get_worksheet(sheet_name)
        return worksheet.get_all_records()
    except Exception as e:
        print(f"Error getting all records from '{sheet_name}': {e}")
        return []

def get_sheet_as_dataframe(sheet_name):
    """ดึงข้อมูลจากชีทและแปลงเป็น DataFrame"""
    try:
        worksheet = get_worksheet(sheet_name)
        data = worksheet.get_all_records()
        df = pd.DataFrame(data)
        # แปลงทุกคอลัมน์เป็น string เพื่อหลีกเลี่ยงปัญหาชนิดข้อมูล
        for col in df.columns:
            df[col] = df[col].astype(str)
        return df
    except gspread.exceptions.WorksheetNotFound:
        print(f"Worksheet '{sheet_name}' not found.")
        return pd.DataFrame()
    except Exception as e:
        print(f"An error occurred while fetching sheet '{sheet_name}' as DataFrame: {e}")
        return pd.DataFrame()

def find_row_by_id(sheet_name, item_id, id_column='id'):
    """ค้นหาแถวด้วย ID"""
    try:
        worksheet = get_worksheet(sheet_name)
        records = worksheet.get_all_records()
        for i, record in enumerate(records):
            if str(record.get(id_column)) == str(item_id):
                return i + 2  # +2 เพราะ gspread นับแถวจาก 1 และมี header
        return None
    except Exception as e:
        print(f"Error finding row by ID in '{sheet_name}': {e}")
        return None

def find_rows_by_value(sheet_name, column_name, value):
    """ค้นหาทุกแถวที่คอลัมน์ที่ระบุมีค่าที่ต้องการ"""
    try:
        worksheet = get_worksheet(sheet_name)
        records = worksheet.get_all_records()
        matched_rows = []
        for i, record in enumerate(records):
            if str(record.get(column_name)) == str(value):
                matched_rows.append(record)
        return matched_rows
    except Exception as e:
        print(f"Error finding rows by value in '{sheet_name}': {e}")
        return []

def update_row(sheet_name, row_index, data_dict):
    """อัปเดตข้อมูลในแถว"""
    try:
        worksheet = get_worksheet(sheet_name)
        # สร้าง list ของค่าตามลำดับ header
        headers = worksheet.row_values(1)
        update_values = [data_dict.get(header, '') for header in headers]
        worksheet.update(f'A{row_index}', [update_values])
        return True
    except Exception as e:
        print(f"Error updating row in '{sheet_name}': {e}")
        return False

def add_row(sheet_name, data_dict):
    """เพิ่มแถวใหม่"""
    try:
        worksheet = get_worksheet(sheet_name)
        headers = worksheet.row_values(1)
        new_row = [data_dict.get(h, '') for h in headers]
        worksheet.append_row(new_row)
        return True
    except Exception as e:
        print(f"Error adding row to '{sheet_name}': {e}")
        return False

def delete_row_by_id(sheet_name, item_id, id_column='id'):
    """ลบแถวด้วย ID"""
    try:
        row_index = find_row_by_id(sheet_name, item_id, id_column)
        if row_index:
            worksheet = get_worksheet(sheet_name)
            worksheet.delete_rows(row_index)
            return True
        return False
    except Exception as e:
        print(f"Error deleting row by ID from '{sheet_name}': {e}")
        return False

def get_current_time_bkk():
    """ดึงเวลาปัจจุบันในโซนเวลากรุงเทพฯ"""
    return datetime.now(BANGKOK_TZ)

def format_datetime_bkk(dt_obj):
    """จัดรูปแบบ datetime object เป็น string"""
    if dt_obj and isinstance(dt_obj, datetime):
        return dt_obj.strftime('%Y-%m-%d %H:%M:%S')
    return None
