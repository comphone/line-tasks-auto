import os
import json

SETTINGS\_FILE = 'settings.json'
\_DEFAULT\_APP\_SETTINGS\_STORE = {
'report\_times': { 'appointment\_reminder\_hour\_thai': 7, 'outstanding\_report\_hour\_thai': 20, 'customer\_followup\_hour\_thai': 9 },
'line\_recipients': { 'admin\_group\_id': os.environ.get('LINE\_ADMIN\_GROUP\_ID', ''), 'technician\_group\_id': os.environ.get('LINE\_TECHNICIAN\_GROUP\_ID', ''), 'manager\_user\_id': '' },
'equipment\_catalog': [],
'auto\_backup': { 'enabled': False, 'hour\_thai': 2, 'minute\_thai': 0 },
'shop\_info': { 'contact\_phone': '081-XXX-XXXX', 'line\_id': '@ComphoneService' },
'technician\_list': []
}

def get\_app\_settings():
"""Reads settings from the JSON file, falling back to defaults."""
app\_settings = json.loads(json.dumps(\_DEFAULT\_APP\_SETTINGS\_STORE))
if os.path.exists(SETTINGS\_FILE):
try:
with open(SETTINGS\_FILE, 'r', encoding='utf-8') as f:
loaded\_settings = json.load(f)
for key, default\_value in app\_settings.items():
if key in loaded\_settings:
if isinstance(default\_value, dict) and isinstance(loaded\_settings[key], dict):
app\_settings[key].update(loaded\_settings[key])
else:
app\_settings[key] = loaded\_settings[key]
except (json.JSONDecodeError, IOError):
\# In case of error, just use the default and overwrite the corrupted file later.
pass
return app\_settings

def save\_app\_settings(settings\_data):
"""Saves the provided settings dictionary to the JSON file."""
current\_settings = get\_app\_settings()
for key, value in settings\_data.items():
if isinstance(value, dict) and key in current\_settings and isinstance(current\_settings[key], dict):
current\_settings[key].update(value)
else:
current\_settings[key] = value
try:
with open(SETTINGS\_FILE, 'w', encoding='utf-8') as f:
json.dump(current\_settings, f, ensure\_ascii=False, indent=4)
return True
except IOError:
return False