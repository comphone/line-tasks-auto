from flask import Flask, render_template, request, jsonify
from datetime import datetime
import json, os, uuid

app = Flask(__name__)

DATA_FILE = 'tasks.json'

def load_tasks():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_tasks(tasks):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)

@app.route('/', methods=['GET', 'POST'])
def form():
    if request.method == 'POST':
        tasks = load_tasks()
        new_task = {
            "id": str(uuid.uuid4()),
            "title": request.form['title'],
            "description": request.form['description'],
            "status": request.form['status'],
            "date": request.form['date'],
            "technician": request.form['technician']
        }
        tasks.append(new_task)
        save_tasks(tasks)
        return render_template('form.html', message="✅ บันทึกเรียบร้อยแล้ว")
    return render_template('form.html')

@app.route('/summary')
def summary():
    date = request.args.get('date')
    tasks = load_tasks()
    if date:
        tasks = [t for t in tasks if t['date'] == date]
    return render_template('tasks_summary.html', tasks=tasks)

@app.route('/task/<task_id>')
def task_detail(task_id):
    tasks = load_tasks()
    task = next((t for t in tasks if t['id'] == task_id), None)
    return jsonify(task if task else {"error": "ไม่พบข้อมูล"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
