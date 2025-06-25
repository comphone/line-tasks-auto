import os
from flask import Flask, request, render_template, redirect, url_for, jsonify
from werkzeug.utils import secure_filename
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import json
import base64

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

GOOGLE_TASKS_API_URL = "https://tasks.googleapis.com/tasks/v1/lists/{task_list_id}/tasks"
LINE_API_URL = "https://api.line.me/v2/bot/message/push"

GOOGLE_TASKS_TOKEN = os.environ.get("GOOGLE_TASKS_TOKEN")
TASK_LIST_ID = os.environ.get("GOOGLE_TASKS_LIST_ID")
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")

tasks = []

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        title = request.form['title']
        customer = request.form['customer']
        phone = request.form['phone']
        address = request.form['address']
        datetime_str = request.form['datetime']
        location = request.form['location']
        detail = request.form['detail']

        image_filename = None
        if 'image' in request.files:
            image = request.files['image']
            if image and allowed_file(image.filename):
                image_filename = secure_filename(image.filename)
                image.save(os.path.join(app.config['UPLOAD_FOLDER'], image_filename))

        task_data = {
            'title': title,
            'customer': customer,
            'phone': phone,
            'address': address,
            'datetime': datetime_str,
            'location': location,
            'detail': detail,
            'image': image_filename,
            'created_at': datetime.now().isoformat()
        }
        tasks.append(task_data)

        # Google Tasks API
        if GOOGLE_TASKS_TOKEN and TASK_LIST_ID:
            headers = {"Authorization": f"Bearer {GOOGLE_TASKS_TOKEN}"}
            payload = {
                "title": title,
                "notes": f"{detail}
ลูกค้า: {customer}
โทร: {phone}
ที่อยู่: {address}
เวลา: {datetime_str}"
            }
            requests.post(GOOGLE_TASKS_API_URL.format(task_list_id=TASK_LIST_ID), headers=headers, json=payload)

        # LINE Notify
        if LINE_TOKEN and LINE_USER_ID:
            headers = {
                "Authorization": f"Bearer {LINE_TOKEN}",
                "Content-Type": "application/json"
            }
            message = f"
🛠 งานใหม่: {title}
ลูกค้า: {customer}
📞 {phone}
📍 {address}
🕒 {datetime_str}"
            payload = {
                "to": LINE_USER_ID,
                "messages": [{"type": "text", "text": message}]
            }
            requests.post(LINE_API_URL, headers=headers, data=json.dumps(payload))

        return redirect(url_for('index'))

    return render_template('form.html')

@app.route('/summary')
def summary():
    return render_template('tasks_summary.html', tasks=tasks)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(debug=False, host='0.0.0.0', port=port)
