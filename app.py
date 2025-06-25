import os
from flask import Flask, render_template, request, redirect
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
scheduler = BackgroundScheduler()
scheduler.start()

# Load configuration from JSON
import json
with open('config.json', 'r') as f:
    config = json.load(f)

GOOGLE_TASKS_SCOPES = ['https://www.googleapis.com/auth/tasks']
credentials = service_account.Credentials.from_service_account_file(
    config['GOOGLE_SERVICE_ACCOUNT_FILE'], scopes=GOOGLE_TASKS_SCOPES)
tasks_service = build('tasks', 'v1', credentials=credentials)

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        topic = request.form["topic"]
        customer = request.form["customer"]
        phone = request.form["phone"]
        address = request.form["address"]
        date = request.form["date"]
        detail = request.form["detail"]
        mapurl = request.form["mapurl"]
        image = request.form["image"]

        # Google Tasks
        task = {
            "title": topic,
            "notes": f"""{detail}
ลูกค้า: {customer}
เบอร์: {phone}
ที่อยู่: {address}
นัดหมาย: {date}
พิกัด: {mapurl}
ภาพ: {image}""",
            "due": f"{date}T23:59:00.000Z"
        }
        tasks_service.tasks().insert(tasklist=config['GOOGLE_TASKS_LIST_ID'], body=task).execute()

        # LINE Notify
        message = f"🛠 งานใหม่: {topic}
ลูกค้า: {customer}
โทร: {phone}
นัดหมาย: {date}
📍พิกัด: {mapurl}"
        requests.post(
            "https://notify-api.line.me/api/notify",
            headers={"Authorization": f"Bearer {config['LINE_TOKEN']}"},
            data={"message": message}
        )
        return redirect("/")
    return render_template("form.html")

@app.route("/summary")
def summary():
    tasks = tasks_service.tasks().list(tasklist=config['GOOGLE_TASKS_LIST_ID']).execute()
    return render_template("tasks_summary.html", tasks=tasks.get("items", []))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(debug=False, host="0.0.0.0", port=port)
