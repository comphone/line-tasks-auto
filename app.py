from flask import Flask, render_template, request, redirect, url_for, jsonify
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import os
import json

app = Flask(__name__)

UPLOAD_FOLDER = 'static/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

scheduler = BackgroundScheduler()
scheduler.start()

def load_tasks():
    if os.path.exists('tasks.json'):
        with open('tasks.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_tasks(tasks):
    with open('tasks.json', 'w', encoding='utf-8') as f:
        json.dump(tasks, f, ensure_ascii=False, indent=4)

@app.route('/')
def index():
    return render_template('form.html')

@app.route('/submit_task', methods=['POST'])
def submit_task():
    title = request.form['title']
    detail = request.form['detail']
    date = request.form['date']

    tasks = load_tasks()
    task = {"id": len(tasks), "title": title, "detail": detail, "date": date}
    tasks.append(task)
    save_tasks(tasks)

    return redirect(url_for('index'))

@app.route('/tasks_summary')
def tasks_summary():
    tasks = load_tasks()
    return render_template('tasks_summary.html', tasks=tasks)

@app.route('/task_detail/<int:task_id>')
def task_detail(task_id):
    tasks = load_tasks()
    task = next((task for task in tasks if task['id'] == task_id), None)
    return jsonify(task)

def daily_summary():
    tasks = load_tasks()
    summary = [task['title'] for task in tasks]
    print("สรุปงานประจำวันที่ยังไม่เสร็จ:", summary)

scheduler.add_job(daily_summary, 'cron', hour=20, minute=0)

if __name__ == '__main__':
    app.run(debug=True)
