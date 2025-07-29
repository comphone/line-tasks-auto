#!/usr/bin/env python3
"""
Enhanced Google API Token Generator
- สร้าง token ที่มีอายุยืนขึ้น (Production mode)
- ตรวจสอบการตั้งค่า OAuth consent screen
- สร้าง token ที่ปลอดภัยและไม่หลุดบ่อย
"""

import os
import json
import sys
from datetime import datetime, timedelta
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Scopes เดียวกับที่ใช้ใน app.py
SCOPES = [
    'https://www.googleapis.com/auth/tasks',
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/calendar.events'
]

def check_oauth_setup():
    """ตรวจสอบการตั้งค่า OAuth consent screen"""
    print("🔍 ตรวจสอบการตั้งค่า OAuth consent screen...")
    print("📋 ให้แน่ใจว่า:")
    print("   ✅ OAuth consent screen เป็น 'In production' mode")
    print("   ✅ Application type เป็น 'Desktop application'")
    print("   ✅ เปิดใช้งาน APIs: Tasks, Drive, Calendar")
    print("")

def create_credentials():
    """สร้าง credentials ใหม่"""
    creds = None
    
    # ตรวจสอบไฟล์ client_secrets.json
    if not os.path.exists('client_secrets.json'):
        print("❌ ไม่พบไฟล์ client_secrets.json")
        print("🔧 วิธีสร้าง:")
        print("   1. ไปที่ Google Cloud Console")
        print("   2. APIs & Services → Credentials")
        print("   3. สร้าง OAuth 2.0 Client ID แบบ 'Desktop application'")
        print("   4. ดาวน์โหลด JSON และตั้งชื่อว่า client_secrets.json")
        return None
    
    # ตรวจสอบรูปแบบไฟล์
    with open('client_secrets.json', 'r') as f:
        client_config = json.load(f)
    
    if 'installed' not in client_config:
        print("❌ client_secrets.json ผิดรูปแบบ!")
        print("🔧 ต้องเป็นแบบ 'Desktop application' ไม่ใช่ 'Web application'")
        return None
    
    print("✅ client_secrets.json ถูกต้อง")
    
    # ตรวจสอบ token เก่า
    if os.path.exists('token.json'):
        print("🔍 พบ token เก่า กำลังตรวจสอบ...")
        try:
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
            if creds and creds.valid:
                print("✅ Token เก่ายังใช้ได้")
                return creds
            elif creds and creds.expired and creds.refresh_token:
                print("🔄 Token หมดอายุ กำลัง refresh...")
                try:
                    creds.refresh(Request())
                    print("✅ Refresh token สำเร็จ")
                    return creds
                except Exception as e:
                    print(f"❌ Refresh ไม่สำเร็จ: {e}")
                    print("🔧 จะสร้าง token ใหม่")
        except Exception as e:
            print(f"❌ Token เก่าใช้ไม่ได้: {e}")
    
    # สร้าง token ใหม่
    print("🔐 เริ่มสร้าง token ใหม่...")
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            'client_secrets.json', 
            SCOPES,
            redirect_uri='http://localhost:8080'  # ระบุ redirect URI ชัดเจน
        )
        
        print("🌐 กำลังเปิดเบราว์เซอร์เพื่อ authorize...")
        print("📝 หมายเหตุ: ถ้าเบราว์เซอร์ไม่เปิด ให้คัดลอก URL ที่แสดงในคอนโซล")
        
        creds = flow.run_local_server(
            port=8080,
            prompt='select_account',
            authorization_prompt_message='Please visit this URL to authorize the application: {url}',
            success_message='Authorization successful! You can close this tab.',
            open_browser=True
        )
        
        print("✅ Authorization สำเร็จ!")
        return creds
        
    except Exception as e:
        print(f"❌ Error during authorization: {e}")
        print("🔧 แนะนำ:")
        print("   1. ตรวจสอบ internet connection")
        print("   2. ปิด VPN (ถ้ามี)")
        print("   3. ลองใช้ port อื่น (แก้ไขในโค้ด)")
        return None

def save_token(creds):
    """บันทึก token"""
    if not creds:
        return False
    
    # บันทึกเป็นไฟล์
    with open('token.json', 'w') as token:
        token.write(creds.to_json())
    print("💾 บันทึก token.json สำเร็จ")
    
    # สร้าง environment variable format
    token_json = creds.to_json()
    print("\n" + "="*80)
    print("🎯 GOOGLE_TOKEN_JSON Environment Variable:")
    print("="*80)
    print(token_json)
    print("="*80)
    
    # แสดงข้อมูล token
    token_data = json.loads(token_json)
    print(f"\n📊 Token Information:")
    print(f"   🔑 Client ID: {token_data.get('client_id', 'N/A')[:50]}...")
    
    if 'expiry' in token_data and token_data['expiry']:
        expiry = datetime.fromisoformat(token_data['expiry'].replace('Z', '+00:00'))
        print(f"   ⏰ Expires: {expiry}")
        time_left = expiry - datetime.now().replace(tzinfo=expiry.tzinfo)
        print(f"   ⏳ Time left: {time_left}")
    else:
        print("   ♾️  Token: No expiry (Production mode)")
    
    return True

def test_token(creds):
    """ทดสอบ token ที่สร้างแล้ว"""
    print("\n🧪 ทดสอบ token...")
    
    try:
        # ทดสอบ Tasks API
        service = build('tasks', 'v1', credentials=creds)
        tasklists = service.tasklists().list().execute()
        print("✅ Tasks API: OK")
        
        # ทดสอบ Drive API
        service = build('drive', 'v3', credentials=creds)
        results = service.files().list(pageSize=1).execute()
        print("✅ Drive API: OK")
        
        # ทดสอบ Calendar API
        service = build('calendar', 'v3', credentials=creds)
        calendars = service.calendarList().list().execute()
        print("✅ Calendar API: OK")
        
        print("🎉 ทุก API ทำงานปกติ!")
        return True
        
    except Exception as e:
        print(f"❌ Error testing APIs: {e}")
        return False

def main():
    print("🚀 Enhanced Google API Token Generator")
    print("="*50)
    
    # ตรวจสอบการตั้งค่า
    check_oauth_setup()
    
    # สร้าง credentials
    creds = create_credentials()
    if not creds:
        print("❌ ไม่สามารถสร้าง token ได้")
        sys.exit(1)
    
    # บันทึก token
    if not save_token(creds):
        print("❌ ไม่สามารถบันทึก token ได้")
        sys.exit(1)
    
    # ทดสอบ token
    if not test_token(creds):
        print("⚠️  Token สร้างได้แต่อาจมีปัญหาใน API access")
    
    print("\n🎯 ขั้นตอนต่อไป:")
    print("1. 📋 คัดลอก GOOGLE_TOKEN_JSON ข้างต้น")
    print("2. 🔧 ไปอัพเดตใน Render.com Environment Variables")
    print("3. 🚀 Deploy โค้ดใหม่")
    print("4. 🔍 ตรวจสอบที่ /admin/token_status")
    
    print("\n✅ เสร็จสิ้น!")

if __name__ == '__main__':
    main()