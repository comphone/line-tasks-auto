import os
import json
from flask import Flask, request, render_template, redirect
from werkzeug.utils import secure_filename
from datetime import datetime
import threading
import time
import requests

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'static/uploads'

# โหลด config
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

LINE_TOKEN = config["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_USER_ID = config["LINE_USER_ID"]
TASK_LIST_ID = config["GOOGLE_TASKS_LIST_ID"]

# ฟังก์ชันส่ง LINE Notify
def send_line_notify(message):
    headers = {"Authorization": "Bearer " + LINE_TOKEN}
    payload = {"message": message}
    requests.post("https://notify-api.line.me/api/notify", headers=headers, data=payload)

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        title = request.form["title"]
        customer = request.form["customer"]
        phone = request.form["phone"]
        address = request.form["address"]
        schedule = request.form["schedule"]
        detail = request.form["detail"]
        location = request.form["location"]
        image = request.files.get("image")

        filename = ""
        if image:
            filename = datetime.now().strftime("%Y%m%d%H%M%S_") + secure_filename(image.filename)
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image.save(image_path)

        notes = f"ลูกค้า: {customer}\nเบอร์โทร: {phone}\nที่อยู่: {address}\nเวลานัด: {schedule}\nพิกัด: {location}\nรายละเอียด: {detail}\n"

        # ส่งแจ้งเตือน LINE
        line_message = f"🔔 แจ้งงานใหม่: {title}\n👤 ลูกค้า: {customer}\n📅 นัดหมาย: {schedule}\n📍 พิกัด: {location}\n📷 รูป: {request.url_root}static/uploads/{filename}\n📝 {detail}"
        send_line_notify(line_message)

        return redirect("/")

    return render_template("form.html")

# สร้างโฟลเดอร์อัปโหลดถ้ายังไม่มี
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

# Task Scheduler
def task_scheduler():
    while True:
        now = datetime.now()
        if now.strftime("%H:%M") == "08:00":
            send_line_notify("🔔 แจ้งเตือน Task ค้าง ❗️")
        elif now.strftime("%H:%M") == "18:00":
            send_line_notify("✅ สรุปงานประจำวันที่ {} 📋".format(now.strftime("%Y-%m-%d")))
        time.sleep(60)

threading.Thread(target=task_scheduler, daemon=True).start()

if __name__ == "__main__":
import os
port = int(os.environ.get("PORT", 5000))
app.run(host="0.0.0.0", port=port)
