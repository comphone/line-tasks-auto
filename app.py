
from flask import Flask, render_template, request, redirect
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import json
import os

app = Flask(__name__)
scheduler = BackgroundScheduler()
scheduler.start()

# Load configuration
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

LINE_NOTIFY_TOKEN = config.get("LINE_NOTIFY_TOKEN")
GOOGLE_TASKS_LIST_ID = config.get("GOOGLE_TASKS_LIST_ID", "")
PORT = int(os.environ.get("PORT", 10000))

def send_line_notify(message):
    url = "https://notify-api.line.me/api/notify"
    headers = {"Authorization": f"Bearer {LINE_NOTIFY_TOKEN}"}
    data = {"message": message}
    requests.post(url, headers=headers, data=data)

@app.route("/", methods=["GET", "POST"])
def form():
    if request.method == "POST":
        topic = request.form["topic"]
        customer = request.form["customer"]
        phone = request.form["phone"]
        address = request.form["address"]
        time = request.form["time"]
        location = request.form["location"]
        detail = request.form["detail"]
        message = f"""🛠 งานใหม่: {topic}
👤 ลูกค้า: {customer}
📞 เบอร์: {phone}
📍 ที่อยู่: {address}
🕒 เวลานัด: {time}
📌 พิกัด: {location}
📝 รายละเอียด: {detail}"""
        send_line_notify(message)
        return redirect("/")
    return render_template("form.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
