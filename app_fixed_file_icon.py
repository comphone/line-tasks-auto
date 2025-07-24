
def some_existing_function():
    pass

@app.context_processor
def inject_now():
    return {
        'now': datetime.datetime.now(THAILAND_TZ),
        'thaizone': THAILAND_TZ
    }



def get_file_icon(filename):
    """Returns a Font Awesome icon class based on file extension."""
    if not filename or '.' not in filename:
        return 'fas fa-file'
    ext = filename.rsplit('.', 1)[1].lower()
    if ext in ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp']:
        return 'fas fa-file-image text-primary'
    if ext in ['mp4', 'mov', 'avi', 'webm', 'mkv']:
        return 'fas fa-file-video text-info'
    if ext == 'pdf':
        return 'fas fa-file-pdf text-danger'
    if ext in ['doc', 'docx']:
        return 'fas fa-file-word text-primary'
    if ext in ['xls', 'xlsx']:
        return 'fas fa-file-excel text-success'
    return 'fas fa-file-alt text-secondary'



@app.context_processor
def inject_now():
    return {
        'now': datetime.datetime.now(THAILAND_TZ),
        'thaizone': THAILAND_TZ,
        'get_file_icon': get_file_icon
    }



def _create_backup_zip():
    try:
        all_tasks = get_google_tasks_for_report(show_completed=True)
        if all_tasks is None:
            app.logger.error('Failed to get tasks for backup.')
            return None, None

        memory_file = BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('data/tasks_backup.json', json.dumps(all_tasks, indent=4, ensure_ascii=False))
            zf.writestr('data/settings_backup.json', json.dumps(get_app_settings(), indent=4, ensure_ascii=False))

            project_root = os.path.dirname(os.path.abspath(__file__))
            for folder, _, files in os.walk(project_root):
                for file in files:
                    if file.endswith(('.py', '.html', '.css', '.js', '.json', 'Procfile', 'requirements.txt')) and file not in ['token.json', '.env', SETTINGS_FILE]:
                        file_path = os.path.join(folder, file)
                        archive_name = os.path.relpath(file_path, project_root)
                        zf.write(file_path, arcname=f'code/{archive_name}')

        memory_file.seek(0)
        backup_filename = f"full_system_backup_{datetime.datetime.now(THAILAND_TZ).strftime('%Y%m%d_%H%M%S')}.zip"
        return memory_file, backup_filename
    except Exception as e:
        app.logger.error(f"Error creating full system backup zip: {e}")
        return None, None
