import re
import json
import pytz
from datetime import datetime
from dateutil.parser import parse as date_parse

# ... (โค้ดอื่นๆ ที่มีอยู่แล้วใน utils.py) ...

def parse_tech_report_from_notes(notes):
    """
    Parses technician reports embedded in the main notes string of a task.
    This function was missing from the refactoring and is now restored.
    """
    if not notes:
        return [], ""
    
    # Find all report blocks
    report_blocks = re.findall(r"--- TECH_REPORT_START ---\s*\n(.*?)\n--- TECH_REPORT_END ---", notes, re.DOTALL)
    history = []
    for json_str in report_blocks:
        try:
            report_data = json.loads(json_str)
            history.append(report_data)
        except json.JSONDecodeError:
            # Log this warning in a real application
            print(f"Warning: Failed to decode tech report JSON.")
            continue
    
    # Remove report blocks to get the original base notes
    temp_notes = re.sub(r"--- TECH_REPORT_START ---.*?--- TECH_REPORT_END ---", "", notes, flags=re.DOTALL)
    temp_notes = re.sub(r"--- CUSTOMER_FEEDBACK_START ---.*?--- CUSTOMER_FEEDBACK_END ---", "", temp_notes, flags=re.DOTALL)
    original_notes_text = temp_notes.strip()

    # Sort history by date, newest first
    history.sort(key=lambda x: x.get('summary_date', '0000-00-00'), reverse=True)
    
    return history, original_notes_text
