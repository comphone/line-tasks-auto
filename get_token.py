from __future__ import print_function
import os
import pickle
import json

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# แก้ SCOPES ตาม API ที่ใช้ เช่น
SCOPES = [
    "https://www.googleapis.com/auth/tasks",       # สำหรับ Google Tasks
    "https://www.googleapis.com/auth/drive"        # สำหรับ Google Drive
]

def main():
    creds = None
    # โหลด client_secrets.json (ดาวน์โหลดจาก Google Console)
    if os.path.exists('token.json'):
        print("token.json already exists.")
        return
    flow = InstalledAppFlow.from_client_secrets_file(
        'client_secrets.json', SCOPES)
    creds = flow.run_local_server(port=0)
    # บันทึก token.json
    with open('token.json', 'w') as token:
        token.write(creds.to_json())
    print("token.json created successfully.")

if __name__ == '__main__':
    main()