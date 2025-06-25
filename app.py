import os
from flask import Flask, render_template, request, redirect, url_for, send_from_directory
from werkzeug.utils import secure_filename
from datetime import datetime
import requests
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# LINE API
LINE_ACCESS_TOKEN = os.getenv('LINE_ACCESS_TOKEN')
LINE_USER_ID = os.getenv('LINE_USER_ID')

# Google Tasks API
SCOPES = ['https://www.googleapis.com/auth/tasks']
SERVICE_ACCOUNT_FILE = 'credentials.json'

credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES)
service = build('tasks', 'v1', credentials=credentials)
TASKS_LIST_ID = os.getenv('GOOGLE_TASKS_LIST_ID')

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/')
def form():
    return render_template('form.html')

@app.route('/submit', methods=['POST'])
def submit():
    topic = request.form['topic']
    customer = request.form['customer']
    phone = request.form['phone']
    address = request.form['address']
    datetime_str = request.form['datetime']
    location = request.form['location']
    detail = request.form['detail']
    filename = None

    file = request.files['file']
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

    # LINE
    line_message = f"🛠 งานใหม่: {topic}\nลูกค้า: {customer}\nเบอร์: {phone}\nที่อยู่: {address}\nนัดหมาย: {datetime_str}\nพิกัด: {location}\nรายละเอียด: {detail}"
    requests.post("https://api.line.me/v2/bot/message/push",
                  headers={
                      "Authorization": f"Bearer {LINE_ACCESS_TOKEN}",
                      "Content-Type": "application/json"
                  },
                  json={
                      "to": LINE_USER_ID,
                      "messages": [{"type": "text", "text": line_message}]
                  })

    # Google Tasks
    task_body = {
        "title": f"{topic} - {customer}",
        "notes": f"{detail}\nเบอร์: {phone}\nที่อยู่: {address}\nนัดหมาย: {datetime_str}\nพิกัด: {location}"
    }
    service.tasks().insert(tasklist=TASKS_LIST_ID, body=task_body).execute()

    return redirect(url_for('form'))

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)