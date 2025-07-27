# ... (Existing imports and functions in utils.py)
import zipfile
import json
from datetime import datetime
import os

# Import necessary modules from the new structure
from google_services import get_google_tasks_for_report # Example
from settings_manager import settings_manager

# ... (Existing functions like sanitize_filename, generate_qr_code_base64, etc.)

def create_backup_zip():
    """
    Creates a zip archive of current tasks, settings, and code files.
    Moved from old google_services.py.
    Reason: This is a utility function for data packaging, not a direct Google API call.
    """
    try:
        current_app.logger.info("Starting to create system backup zip.")
        memory_file = BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # 1. Backup settings
            settings = settings_manager.get_all_settings()
            zipf.writestr('settings.json', json.dumps(settings, ensure_ascii=False, indent=4))

            # 2. Backup Google Tasks
            tasks = get_google_tasks_for_report(show_completed=True, max_results=500) # Assuming this function exists
            if tasks:
                zipf.writestr('tasks_backup.json', json.dumps(tasks, ensure_ascii=False, indent=4))

            # 3. Backup Code Files (optional, can be complex)
            # ... (Logic to walk through project directory and add files) ...

        memory_file.seek(0)
        timestamp = datetime.now(BANGKOK_TZ).strftime("%Y%m%d_%H%M%S")
        backup_filename = f"full_backup_{timestamp}.zip"
        return memory_file, backup_filename
    except Exception as e:
        current_app.logger.error(f"Error creating full system backup zip: {e}")
        return None, None
