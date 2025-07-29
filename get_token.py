from __future__ import print_function
import os
import json
from google_auth_oauthlib.flow import InstalledAppFlow
from http.server import HTTPServer, BaseHTTPRequestHandler
import webbrowser

# 🔄 อัพเดต SCOPES ตามที่ใช้จริง
SCOPES = [
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/drive", 
    "https://www.googleapis.com/auth/calendar"
]

class TokenDisplayHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        
        with open('token.json', 'r') as token_file:
            token_content = token_file.read()
        
        html_content = f'''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Google API Token - Production Ready</title>
            <style>
                body {{ font-family: Arial, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; }}
                .success {{ color: #28a745; font-weight: bold; }}
                .warning {{ color: #ffc107; font-weight: bold; }}
                .token-box {{ background: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0; }}
                textarea {{ width: 100%; height: 200px; font-family: monospace; }}
                button {{ background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; }}
            </style>
        </head>
        <body>
            <h1 class="success">✅ Token สร้างเรียบร้อย - Production Mode</h1>
            
            <div class="warning">
                <h3>⚠️ สำคัญ: ต้อง Publish App เป็น Production</h3>
                <p>1. ไปที่ <a href="https://console.cloud.google.com/" target="_blank">Google Cloud Console</a></p>
                <p>2. เลือก Project → APIs & Services → OAuth consent screen</p>
                <p>3. เปลี่ยน Publishing status จาก "Testing" เป็น <strong>"In production"</strong></p>
                <p>4. คลิก "Publish App"</p>
            </div>
            
            <div class="token-box">
                <h3>📋 คัดลอก Token นี้ไปใส่ใน GOOGLE_TOKEN_JSON:</h3>
                <textarea id="tokenJson" readonly>{token_content}</textarea>
                <button onclick="copyToken()">📋 คัดลอก Token</button>
                <p id="copyStatus" style="margin-top: 10px;"></p>
            </div>
            
            <div class="success">
                <h3>🎉 หลังจากนี้ Token จะไม่หมดอายุใน 7 วัน!</h3>
                <p>Token จะใช้ได้นานขึ้นมากหลังจากเปลี่ยนเป็น Production mode</p>
            </div>
            
            <script>
                function copyToken() {{
                    const textarea = document.getElementById('tokenJson');
                    textarea.select();
                    document.execCommand('copy');
                    document.getElementById('copyStatus').innerHTML = '<span style="color: green;">✅ คัดลอกแล้ว!</span>';
                    setTimeout(() => {{ document.getElementById('copyStatus').innerHTML = ''; }}, 3000);
                }}
            </script>
        </body>
        </html>
        '''
        
        self.wfile.write(html_content.encode('utf-8'))

def main():
    if os.path.exists('token.json'):
        print("⚠️  token.json มีอยู่แล้ว ลบออกถ้าต้องการสร้างใหม่")
        with open('token.json', 'r') as token_file:
            print("🔑 Token ปัจจุบัน:")
            print(token_file.read())
        return

    if not os.path.exists('client_secrets.json'):
        print("❌ ไม่พบ client_secrets.json - ดาวน์โหลดจาก Google Cloud Console")
        return

    print("🚀 กำลังสร้าง Token สำหรับ Production...")
    
    flow = InstalledAppFlow.from_client_secrets_file('client_secrets.json', SCOPES)
    creds = flow.run_local_server(port=8080)

    # บันทึก token
    with open('token.json', 'w') as token:
        token.write(creds.to_json())
    
    print("✅ token.json สร้างเรียบร้อย")
    print("🌐 เปิดเบราว์เซอร์เพื่อดู Token...")
    
    # เริ่ม server เพื่อแสดง token
    server_address = ('localhost', 8081)
    httpd = HTTPServer(server_address, TokenDisplayHandler)
    
    webbrowser.open(f'http://{server_address[0]}:{server_address[1]}')
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()

if __name__ == '__main__':
    main()