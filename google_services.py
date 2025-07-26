import os
import json
import pytz
import time
from datetime import datetime, timezone, timedelta
from io import BytesIO

from flask import current_app
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload, MediaIoBaseDownload
from linebot import LineBotApi
from linebot.models import TextSendMessage

# --- Constants ---
SCOPES = ['https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/drive']
GOOGLE_TASKS_LIST_ID = os.environ.get('GOOGLE_TASKS_LIST_ID', '@default')
GOOGLE_DRIVE_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')

# --- In-memory cache for credentials ---
_CACHED_CREDENTIALS = None
_CREDENTIALS_LAST_REFRESH = None
_CREDENTIALS_REFRESH_INTERVAL = timedelta(minutes=45)

def _notify_admin_error(message):
    """Sends a critical error notification related to Google API issues."""
    try:
        # This is a self-contained notifier to avoid circular dependencies with app.py
        admin_group_id = os.environ.get('LINE_ADMIN_GROUP_ID')
        access_token = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
        if admin_group_id and access_token:
            line_bot_api = LineBotApi(access_token)
            line_bot_api.push_message(admin_group_id, TextSendMessage(text=f"‼️ G-API Critical Error ‼️\n\n{message[:900]}"))
    except Exception as e:
        # Use current_app logger if available, otherwise print
        try:
            current_app.logger.error(f"Failed to send critical Google API error notification: {e}")
        except RuntimeError:
            print(f"Failed to send critical Google API error notification: {e}")

# --- Core Google Service and Authentication Logic ---

def get_refreshed_credentials(force_refresh=False):
    """Manages Google API credentials, caching them and refreshing proactively."""
    global _CACHED_CREDENTIALS, _CREDENTIALS_LAST_REFRESH
    now = datetime.now(timezone.utc)

    if not force_refresh and _CACHED_CREDENTIALS and _CREDENTIALS_LAST_REFRESH and \
       (now - _CREDENTIALS_LAST_REFRESH < _CREDENTIALS_REFRESH_INTERVAL) and _CACHED_CREDENTIALS.valid:
        return _CACHED_CREDENTIALS

    current_app.logger.info(f"Refreshing Google credentials. Reason: {'Forced' if force_refresh else 'Cache expired or invalid'}")
    creds = None
    google_token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')
    if google_token_json_str:
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
                # Persist the new token to the environment variable. This is crucial for multi-process/dyno environments.
                os.environ['GOOGLE_TOKEN_JSON'] = creds.to_json()
            except Exception as e:
                current_app.logger.error(f"CRITICAL: Failed to refresh Google API token: {e}")
                _notify_admin_error("ไม่สามารถรีเฟรช Google API token ได้ กรุณาสร้าง GOOGLE_TOKEN_JSON ใหม่และอัปเดตในระบบ")
                _CACHED_CREDENTIALS = None
                return None
        else:
            current_app.logger.error("CRITICAL: No refresh_token found. Cannot refresh credentials.")
            _notify_admin_error("ไม่พบ Refresh Token! กรุณาสร้าง GOOGLE_TOKEN_JSON ใหม่ทั้งหมด")
            _CACHED_CREDENTIALS = None
            return None

    if creds and creds.valid:
        _CACHED_CREDENTIALS = creds
        _CREDENTIALS_LAST_REFRESH = now
        return _CACHED_CREDENTIALS

    current_app.logger.error("Could not obtain valid Google credentials.")
    return None

def get_google_service(api_name, api_version):
    """Builds a Google API service object using the robust credential management system."""
    creds = get_refreshed_credentials()
    if creds:
        try:
            # cache_discovery=False is recommended for environments where the file system is not persistent.
            return build(api_name, api_version, credentials=creds, cache_discovery=False)
        except Exception as e:
            current_app.logger.error(f"Failed to build Google API service '{api_name} v{api_version}': {e}")
    return None

def _execute_google_api_call_with_retry(api_call, *args, **kwargs):
    """Wrapper for Google API calls with retry logic and reactive token refresh."""
    for i in range(3): # Total of 3 attempts
        try:
            return api_call(*args, **kwargs).execute()
        except HttpError as e:
            # If unauthorized, force a token refresh and retry immediately (only on the first attempt)
            if e.resp.status == 401 and i == 0:
                current_app.logger.warning("Received 401 Unauthorized. Forcing token refresh and retrying.")
                get_refreshed_credentials(force_refresh=True)
                continue # Retry the API call
            # If server error or rate limited, wait and retry
            if e.resp.status in [500, 503, 429] and i < 2: # Don't sleep on the last attempt
                sleep_time = (2 ** i)
                current_app.logger.warning(f"Received status {e.resp.status}. Retrying in {sleep_time}s.")
                time.sleep(sleep_time)
                continue
            current_app.logger.error(f"Unrecoverable Google API HttpError: {e}")
            raise # Re-raise the exception if all retries fail or it's an unrecoverable error
    return None

# --- Service Accessor Functions ---
def get_google_tasks_service(): return get_google_service('tasks', 'v1')
def get_google_drive_service(): return get_google_service('drive', 'v3')

# --- High-Level API Functions ---

def find_or_create_drive_folder(name, parent_id):
    """Finds a folder by name within a parent, or creates it if it doesn't exist. Not cached here."""
    service = get_google_drive_service()
    if not service: return None
    
    query = f"name = '{name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    try:
        response = _execute_google_api_call_with_retry(service.files().list, q=query, spaces='drive', fields='files(id, name)', pageSize=1)
        files = response.get('files', [])
        if files:
            current_app.logger.info(f"Found existing Drive folder '{name}' with ID: {files[0]['id']}")
            return files[0]['id']
        else:
            current_app.logger.info(f"Folder '{name}' not found in parent '{parent_id}'. Creating it...")
            file_metadata = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
            folder = _execute_google_api_call_with_retry(service.files().create, body=file_metadata, fields='id')
            folder_id = folder.get('id')
            current_app.logger.info(f"Created new Drive folder '{name}' with ID: {folder_id}")
            return folder_id
    except HttpError as e:
        current_app.logger.error(f"Error finding or creating folder '{name}': {e}")
        return None

def get_google_tasks_for_report(show_completed=True, max_results=100):
    """Gets all tasks from the specified list. Not cached here."""
    service = get_google_tasks_service()
    if not service: return None
    try:
        results = _execute_google_api_call_with_retry(service.tasks().list, tasklist=GOOGLE_TASKS_LIST_ID, showCompleted=show_completed, maxResults=max_results)
        return results.get('items', [])
    except HttpError as err:
        current_app.logger.error(f"API Error getting tasks: {err}")
        return None

def get_single_task(task_id):
    """Retrieves a single task by its ID."""
    if not task_id: return None
    service = get_google_tasks_service()
    if not service: return None
    try:
        return _execute_google_api_call_with_retry(service.tasks().get, tasklist=GOOGLE_TASKS_LIST_ID, task=task_id)
    except HttpError as err:
        # A 404 here is a normal operational possibility, so log as info
        if err.resp.status == 404:
            current_app.logger.info(f"Could not find task {task_id}, it may have been deleted.")
        else:
            current_app.logger.error(f"Error getting single task {task_id}: {err}")
        return None

def create_google_task(title, notes=None, due=None):
    """Creates a new Google Task."""
    service = get_google_tasks_service()
    if not service: return None
    try:
        task_body = {'title': title, 'notes': notes, 'status': 'needsAction'}
        if due: task_body['due'] = due
        return _execute_google_api_call_with_retry(service.tasks().insert, tasklist=GOOGLE_TASKS_LIST_ID, body=task_body)
    except HttpError as e:
        current_app.logger.error(f"Error creating Google Task: {e}")
        return None

def delete_google_task(task_id):
    """Deletes a Google Task by its ID."""
    service = get_google_tasks_service()
    if not service: return False
    try:
        # A delete call returns None on success, so we don't need the return value
        _execute_google_api_call_with_retry(service.tasks().delete, tasklist=GOOGLE_TASKS_LIST_ID, task=task_id)
        return True
    except HttpError as err:
        current_app.logger.error(f"API Error deleting task {task_id}: {err}")
        return False

def update_google_task(task_id, title=None, notes=None, status=None, due=None, completed=None):
    """Updates an existing Google Task."""
    service = get_google_tasks_service()
    if not service: return None
    try:
        task = get_single_task(task_id)
        if not task:
             return None # The task doesn't exist.
        
        if title is not None: task['title'] = title
        if notes is not None: task['notes'] = notes
        if status is not None:
            task['status'] = status

        # Handle completion status and timestamps
        if status == 'completed':
            # Only set completed time if it's not already set to avoid overwriting
            if not task.get('completed'):
                task['completed'] = datetime.now(pytz.utc).isoformat().replace('+00:00', 'Z')
            task['due'] = None # Clear due date on completion
        else: # if status is 'needsAction'
            task.pop('completed', None) # Remove completion timestamp
            if due is not None:
                task['due'] = due
        
        # Allow overriding the 'completed' timestamp directly if provided
        if completed is not None:
             task['completed'] = completed
        
        return _execute_google_api_call_with_retry(service.tasks().update, tasklist=GOOGLE_TASKS_LIST_ID, task=task_id, body=task)
    except HttpError as e:
        current_app.logger.error(f"Failed to update task {task_id}: {e}")
        return None

def _perform_drive_upload(media_body, file_name, mime_type, folder_id):
    """Core logic for uploading a file to a specific Drive folder."""
    service = get_google_drive_service()
    if not service or not folder_id:
        current_app.logger.error(f"Drive service or Folder ID not configured for upload of '{file_name}'.")
        return None
    try:
        file_metadata = {'name': file_name, 'parents': [folder_id]}
        file_obj = _execute_google_api_call_with_retry(
            service.files().create, 
            body=file_metadata, 
            media_body=media_body, 
            fields='id, webViewLink'
        )

        if not file_obj or 'id' not in file_obj:
            current_app.logger.error(f"Drive upload failed for '{file_name}'. API call did not return a file object.")
            return None

        # Make file publicly readable
        uploaded_file_id = file_obj['id']
        permission_result = _execute_google_api_call_with_retry(
            service.permissions().create, 
            fileId=uploaded_file_id, 
            body={'role': 'reader', 'type': 'anyone'}
        )
        if not permission_result or 'id' not in permission_result:
            current_app.logger.warning(f"Failed to set public permissions for '{file_name}' (ID: {uploaded_file_id}). File may be inaccessible.")

        return file_obj
    except Exception as e:
        current_app.logger.error(f'Unexpected error during Drive upload for {file_name}: {e}', exc_info=True)
        return None

def upload_data_from_memory_to_drive(data_in_memory, file_name, mime_type, folder_id):
    """Uploads data from an in-memory BytesIO object to Google Drive."""
    media = MediaIoBaseUpload(data_in_memory, mimetype=mime_type, resumable=True)
    file_obj = _perform_drive_upload(media, file_name, mime_type, folder_id)
    return file_obj

def upload_file_from_path_to_drive(file_path, file_name, mime_type, folder_id):
    """Uploads a local file from a given path to Google Drive."""
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        current_app.logger.error(f"File at path '{file_path}' is missing or empty. Aborting upload.")
        return None
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
    file_obj = _perform_drive_upload(media, file_name, mime_type, folder_id)
    return file_obj

def load_settings_from_drive_on_startup(save_settings_func):
    """Loads the latest 'settings_backup.json' from Drive and saves it locally using a provided function."""
    current_app.logger.info("Attempting to restore settings from Google Drive on startup...")
    settings_backup_folder_id = find_or_create_drive_folder("Settings_Backups", GOOGLE_DRIVE_FOLDER_ID)
    if not settings_backup_folder_id:
        current_app.logger.error("Could not find or create Settings_Backups folder. Skipping settings restore.")
        return False
        
    service = get_google_drive_service()
    if not service: return False

    try:
        query = f"name = 'settings_backup.json' and '{settings_backup_folder_id}' in parents and trashed = false"
        response = _execute_google_api_call_with_retry(service.files().list, q=query, spaces='drive', fields='files(id, name)', orderBy='modifiedTime desc', pageSize=1)
        files = response.get('files', [])

        if files:
            latest_backup_file_id = files[0]['id']
            current_app.logger.info(f"Found latest settings backup on Drive (ID: {latest_backup_file_id})")
            
            request = service.files().get_media(fileId=latest_backup_file_id)
            fh = BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: 
                status, done = downloader.next_chunk()
            fh.seek(0)
            downloaded_settings = json.loads(fh.read().decode('utf-8'))

            if save_settings_func(downloaded_settings):
                current_app.logger.info("Successfully restored and saved settings from Google Drive backup.")
                return True
            else:
                current_app.logger.error("Failed to save restored settings to local file.")
                return False
        else:
            current_app.logger.info("No settings backup found on Google Drive for automatic restore.")
            return False
    except Exception as e:
        current_app.logger.error(f"An unexpected error occurred during settings restore from Drive: {e}", exc_info=True)
        return False

def backup_settings_to_drive(settings_data):
    """Backs up the provided settings dictionary to 'settings_backup.json' in Google Drive."""
    settings_backup_folder_id = find_or_create_drive_folder("Settings_Backups", GOOGLE_DRIVE_FOLDER_ID)
    if not settings_backup_folder_id: return False

    service = get_google_drive_service()
    if not service: return False

    try:
        # Clean up old backup(s) to ensure only one exists
        query = f"name = 'settings_backup.json' and '{settings_backup_folder_id}' in parents and trashed = false"
        response = _execute_google_api_call_with_retry(service.files().list, q=query, spaces='drive', fields='files(id)')
        for file_item in response.get('files', []):
            _execute_google_api_call_with_retry(service.files().delete, fileId=file_item['id'])
        
        # Upload new backup
        settings_json_bytes = BytesIO(json.dumps(settings_data, ensure_ascii=False, indent=4).encode('utf-8'))
        file_metadata = {'name': 'settings_backup.json', 'parents': [settings_backup_folder_id]}
        media = MediaIoBaseUpload(settings_json_bytes, mimetype='application/json', resumable=True)
        _execute_google_api_call_with_retry(service.files().create, body=file_metadata, media_body=media, fields='id')
        current_app.logger.info("Successfully saved current settings to settings_backup.json on Google Drive.")
        return True
    except Exception as e:
        current_app.logger.error(f"Failed to backup settings to Google Drive: {e}", exc_info=True)
        return False