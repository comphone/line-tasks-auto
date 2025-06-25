
import os
from flask import Flask, request, render_template, redirect, url_for, send_from_directory
from werkzeug.utils import secure_filename
import datetime

app = Flask(__name__)
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/', methods=['GET', 'POST'])
def form():
    if request.method == 'POST':
        topic = request.form.get('topic')
        customer = request.form.get('customer')
        phone = request.form.get('phone')
        address = request.form.get('address')
        appointment = request.form.get('appointment')
        latitude = request.form.get('latitude')
        longitude = request.form.get('longitude')
        detail = request.form.get('detail')
        file_url = None

        file = request.files.get('attachment')
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            file_url = filepath

        with open("tasks_log.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now()}|{topic}|{customer}|{phone}|{address}|{appointment}|{latitude},{longitude}|{detail}|{file_url}\n")

        return redirect(url_for('summary'))

    return render_template('form.html')

@app.route('/summary')
def summary():
    tasks = []
    if os.path.exists("tasks_log.txt"):
        with open("tasks_log.txt", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) == 9:
                    dt, topic, customer, phone, address, appointment, coord, detail, file_url = parts
                    tasks.append({
                        "datetime": dt,
                        "topic": topic,
                        "customer": customer,
                        "phone": phone,
                        "address": address,
                        "appointment": appointment,
                        "coord": coord,
                        "detail": detail,
                        "file_url": file_url
                    })
    return render_template("tasks_summary.html", tasks=tasks)

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
