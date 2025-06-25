from flask import Flask, request, render_template, redirect, url_for, jsonify
import os
import json

app = Flask(__name__)

# โหลด config.json
with open('config.json') as f:
    config = json.load(f)

# ตั้งค่าจาก config.json
LINE_CHANNEL_ACCESS_TOKEN = config["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET = config["LINE_CHANNEL_SECRET"]
LINE_USER_ID = config["LINE_USER_ID"]
GOOGLE_TASKS_LIST_ID = config["GOOGLE_TASKS_LIST_ID"]

# กำหนดโฟลเดอร์สำหรับเก็บไฟล์ที่อัปโหลด
UPLOAD_FOLDER = 'static/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route('/')
def index():
    return render_template('form.html')

@app.route('/submit_task', methods=['POST'])
def submit_task():
    title = request.form.get("title")
    customer = request.form.get("customer")
    phone = request.form.get("phone")
    address = request.form.get("address")
    appointment = request.form.get("appointment_time")
    details = request.form.get("details")
    location = request.form.get("location")

    # อัปโหลดภาพ
    image = request.files.get('image')
    image_url = ""
    if image:
        image_path = os.path.join(UPLOAD_FOLDER, image.filename)
        image.save(image_path)
        image_url = request.host_url + image_path

    # ส่งข้อมูลไปยัง LINE Notify หรือ LINE Messaging API ที่นี่
    # ตัวอย่างการส่งข้อความง่ายๆ (สามารถเพิ่มการแจ้งเตือนจริงได้ภายหลัง)

    print("📌 มีงานใหม่: ", title)
    print("👤 ลูกค้า: ", customer)
    print("📞 เบอร์โทร: ", phone)
    print("📍 ที่อยู่: ", address)
    print("📅 เวลานัด: ", appointment)
    print("📝 รายละเอียด: ", details)
    print("🗺️ พิกัด: ", location)
    print("🖼️ รูปภาพ: ", image_url)

    return jsonify({"status": "success", "message": "บันทึกข้อมูลสำเร็จ"})

if __name__ == "__main__":
    # เพิ่ม host และ port ให้ทำงานบน Render ได้
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
