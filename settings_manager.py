import os
import json

SETTINGS_FILE = 'settings.json'
_DEFAULT_APP_SETTINGS_STORE = {
    'report_times': { 'appointment_reminder_hour_thai': 7, 'outstanding_report_hour_thai': 20, 'customer_followup_hour_thai': 9 },
    'line_recipients': { 'admin_group_id': os.environ.get('LINE_ADMIN_GROUP_ID', ''), 'technician_group_id': os.environ.get('LINE_TECHNICIAN_GROUP_ID', ''), 'manager_user_id': '' },
    'equipment_catalog': [],
    'auto_backup': { 'enabled': False, 'hour_thai': 2, 'minute_thai': 0 },
    'shop_info': { 'contact_phone': '081-XXX-XXXX', 'line_id': '@ComphoneService' },
    'technician_list': []
}

def get_app_settings():
    """Reads settings from the JSON file, falling back to defaults."""
    app_settings = json.loads(json.dumps(_DEFAULT_APP_SETTINGS_STORE))
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                loaded_settings = json.load(f)
                for key, default_value in app_settings.items():
                    if key in loaded_settings:
                        if isinstance(default_value, dict) and isinstance(loaded_settings[key], dict):
                            app_settings[key].update(loaded_settings[key])
                        else:
                            app_settings[key] = loaded_settings[key]
        except (json.JSONDecodeError, IOError):
            # In case of error, just use the default and overwrite the corrupted file later.
            pass
    return app_settings

def save_app_settings(settings_data):
    """Saves the provided settings dictionary to the JSON file."""
    current_settings = get_app_settings()
    for key, value in settings_data.items():
        if isinstance(value, dict) and key in current_settings and isinstance(current_settings[key], dict):
            current_settings[key].update(value)
        else:
            current_settings[key] = value
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(current_settings, f, ensure_ascii=False, indent=4)
        return True
    except IOError:
        return False