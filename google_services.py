import os
import json
import time
from datetime import datetime, timezone, timedelta

from flask import current_app
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

# Local Module Imports
import utils
from settings_manager import settings_manager

# --- Constants ---
SCOPES = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/drive']
GOOGLE_TASKS_LIST_ID = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')

# --- Credential Caching ---
_CACHED_CREDENTIALS = None
_CREDENTIALS_LAST_REFRESH = None
_CREDENTIALS_REFRESH_INTERVAL = timedelta(minutes=45)

# --- Core Authentication and Service Logic ---

def get_refreshed_credentials(force_refresh=False):
    """
    Manages and refreshes Google API credentials, using an in-memory cache.
    This robust version is from the old google_services.py.
    """
    global _CACHED_CREDENTIALS, _CREDENTIALS_LAST_REFRESH
    now = datetime.now(timezone.utc)

    # Return cached credentials if they are still valid and not forced to refresh
    if not force_refresh and _CACHED_CREDENTIALS and _CREDENTIALS_LAST_REFRESH and \
       (now - _CREDENTIALS_LAST_REFRESH < _CREDENTIALS_REFRESH_INTERVAL) and _CACHED_CREDENTIALS.valid:
        return _CACHED_CREDENTIALS

    current_app.logger.info(f"Refreshing Google credentials. Reason: {'Forced' if force_refresh else 'Cache expired or invalid'}")
    creds = None
    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')
    if not google_token_json_str:
        current_app.logger.error("CRITICAL: GOOGLE_TOKEN_JSON environment variable not set.")
        return None
        
    try:
        creds = Credentials.from_authorized_user_info(json.loads(google_token_json_str), SCOPES)
    except Exception as e:
        current_app.logger.error(f"CRITICAL: Could not load token from GOOGLE_TOKEN_JSON: {e}")
        return None

    if creds and (creds.expired or not creds.valid or force_refresh):
        if creds.refresh_token:
            try:
                creds.refresh(Request())
                current_app.logger.info("Google API token refreshed successfully.")
                os.environ['GOOGLE_TOKEN_JSON'] = creds.to_json() # Persist the new token
            except Exception as e:
                current_app.logger.error(f"CRITICAL: Failed to refresh Google API token: {e}")
                _CACHED_CREDENTIALS = None
                return None
        else:
            current_app.logger.error("CRITICAL: No refresh_token found. Cannot refresh credentials.")
            _CACHED_CREDENTIALS = None
            return None

    _CACHED_CREDENTIALS = creds
    _CREDENTIALS_LAST_REFRESH = now
    return _CACHED_CREDENTIALS

def get_google_service(api_name, api_version):
    """Builds a Google API service object."""
    creds = get_refreshed_credentials()
    if creds:
        try:
            return build(api_name, api_version, credentials=creds, cache_discovery=False)
        except Exception as e:
            current_app.logger.error(f"Failed to build Google API service '{api_name} v{api_version}': {e}")
    return None

def _execute_google_api_call_with_retry(api_call, *args, **kwargs):
    """Wrapper for Google API calls with retry logic."""
    for i in range(3):
        try:
            return api_call(*args, **kwargs).execute()
        except HttpError as e:
            if e.resp.status == 401 and i == 0:
                get_refreshed_credentials(force_refresh=True)
                continue
            if e.resp.status in [500, 503, 429] and i < 2:
                time.sleep((2 ** i))
                continue
            raise
    return None

def get_drive_service(): return get_google_service('drive', 'v3')
def get_tasks_service(): return get_google_service('tasks', 'v1')

# --- High-Level Functions ---

def find_or_create_drive_folder(name, parent_id):
    """Finds a folder by name, or creates it if it doesn't exist."""
    service = get_drive_service()
    if not service: return None
    query = f"name = '{name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    response = _execute_google_api_call_with_retry(service.files().list, q=query, fields='files(id)')
    if response and response.get('files'):
        return response['files'][0]['id']
    else:
        file_metadata = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
        folder = _execute_google_api_call_with_retry(service.files().create, body=file_metadata, fields='id')
        return folder.get('id') if folder else None

def organize_drive_files():
    """
    The main logic for organizing files, kept here as it's a core Google Service operation.
    """
    try:
        drive_service = get_drive_service()
        if not drive_service: raise Exception("Drive service unavailable.")

        google_drive_folder_id = settings_manager.get_setting('google_drive_folder_id')
        if not google_drive_folder_id: raise Exception("GOOGLE_DRIVE_FOLDER_ID not set.")

        attachments_base_folder_id = find_or_create_drive_folder("Task_Attachments", google_drive_folder_id)
        if not attachments_base_folder_id: raise Exception("Could not find/create 'Task_Attachments' folder.")

        # This logic remains complex and is best encapsulated here.
        # ... (The full, detailed logic from the previous step) ...
        moved_count, skipped_count, error_count = 0, 0, 0
        # Placeholder for the complex iteration logic
        
        return True, {"moved": moved_count, "skipped": skipped_count, "errors": error_count}
    except Exception as e:
        current_app.logger.error(f"Error in organize_drive_files: {e}")
        return False, {"errors": str(e)}

# ... (Other core functions like get_all_tasks, update_task, upload_file, etc. remain here) ...
