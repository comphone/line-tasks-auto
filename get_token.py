from __future__ import print_function
import os
import json
from google_auth_oauthlib.flow import InstalledAppFlow
from http.server import HTTPServer, BaseHTTPRequestHandler
import webbrowser

# กำหนด SCOPES ตาม API ที่คุณใช้งาน
SCOPES = [
    "https://www.googleapis.com/auth/tasks",       # สำหรับ Google Tasks
    "https://www.googleapis.com/auth/drive"        # สำหรับ Google Drive
]

# HTML template สำหรับแสดงผล Token และปุ่ม Copy
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>รับ Google API Token</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #f4f7f6; color: #333; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }}
        .container {{ background-color: #fff; padding: 2rem 3rem; border-radius: 15px; box-shadow: 0 10px 30px rgba(0,0,0,0.1); text-align: center; max-width: 600px; }}
        h1 {{ color: #4CAF50; }}
        p {{ font-size: 1.1rem; }}
        textarea {{ width: 100%; height: 200px; margin-top: 1rem; padding: 0.5rem; border-radius: 8px; border: 1px solid #ccc; font-family: monospace; font-size: 0.9rem; }}
        button {{ background-color: #007BFF; color: white; border: none; padding: 0.8rem 1.5rem; font-size: 1rem; border-radius: 8px; cursor: pointer; transition: background-color 0.2s; margin-top: 1rem; }}
        button:hover {{ background-color: #0056b3; }}
        .status-success {{ color: #28a745; font-weight: bold; }}
    </style>
</head>
<body>
    <div class="container">
        <h1><i class="fas fa-check-circle"></i> Token ถูกสร้างเรียบร้อยแล้ว</h1>
        <p>คัดลอกข้อความ JSON ด้านล่างนี้ทั้งหมด และนำไปใส่ใน Environment Variable ที่ชื่อว่า <code>GOOGLE_TOKEN_JSON</code> บน Render.com ของคุณ</p>
        <textarea id="tokenJson" readonly>{token_data}</textarea>
        <button onclick="copyToken()">คัดลอก JSON</button>
        <p id="copyStatus" style="margin-top: 1rem;"></p>
    </div>
    <script>
        function copyToken() {{
            const tokenText = document.getElementById('tokenJson');
            tokenText.select();
            tokenText.setSelectionRange(0, 99999); // For mobile devices
            document.execCommand('copy');
            const copyStatus = document.getElementById('copyStatus');
            copyStatus.innerHTML = '<span class="status-success">คัดลอกไปยังคลิปบอร์ดแล้ว!</span>';
            setTimeout(() => {{ copyStatus.innerHTML = ''; }}, 3000);
        }}
    </script>
</body>
</html>
"""

class TokenDisplayHandler(BaseHTTPRequestHandler):
    """
    HTTP server handler to display the generated token.
    """
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        with open('token.json', 'r') as token_file:
            token_content = token_file.read()
        
        # Replace the placeholder with the actual token data
        response_html = HTML_TEMPLATE.format(token_data=token_content)
        self.wfile.write(response_html.encode('utf-8'))

def main():
    """
    Main function to authorize and generate Google API token.
    """
    creds = None
    if os.path.exists('token.json'):
        print("token.json already exists. Please delete it if you want to regenerate.")
        with open('token.json', 'r') as token_file:
            token_content = token_file.read()
            print("\nExisting Token:\n", token_content)
        return

    # Load client_secrets.json (downloaded from Google Console)
    if not os.path.exists('client_secrets.json'):
        print("Error: client_secrets.json not found. Please download it from your Google Cloud Console project.")
        return

    flow = InstalledAppFlow.from_client_secrets_file(
        'client_secrets.json', SCOPES)
    
    # Run the local server in a separate thread to show the token
    creds = flow.run_local_server(port=8080)

    # Save the credentials for the next run
    with open('token.json', 'w') as token:
        token.write(creds.to_json())
    print("\ntoken.json created successfully.")
    
    # Open the browser to display the token
    server_address = ('localhost', 8081)
    httpd = HTTPServer(server_address, TokenDisplayHandler)
    
    print(f"\nServing token display page at http://{server_address[0]}:{server_address[1]}")
    webbrowser.open(f'http://{server_address[0]}:{server_address[1]}')
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()
    print("Server stopped.")

if __name__ == '__main__':
    main()