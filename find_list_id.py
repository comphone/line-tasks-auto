import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- Function to get credentials ---
def get_google_creds():
    """Helper to get credentials from environment variables or local file."""
    # Check for Render's environment variable first
    google_creds_json_str = os.environ.get('GOOGLE_CREDENTIALS_JSON')
    if google_creds_json_str:
        try:
            creds_info = json.loads(google_creds_json_str)
            # Add the required scope for tasks
            scopes = ['https://www.googleapis.com/auth/tasks.readonly']
            return service_account.Credentials.from_service_account_info(creds_info, scopes=scopes)
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            print(f"Error loading credentials from environment variable: {e}")
            return None
    
    # Fallback to local file for local testing
    elif os.path.exists('service_account.json'):
        try:
            scopes = ['https://www.googleapis.com/auth/tasks.readonly']
            return service_account.Credentials.from_service_account_file('service_account.json', scopes=scopes)
        except Exception as e:
            print(f"Error loading credentials from file: {e}")
            return None
    else:
        print("Error: No Google credentials found. Please set GOOGLE_CREDENTIALS_JSON or place service_account.json in the directory.")
        return None

# --- Main script ---
def main():
    """Fetches and prints all Google Task lists."""
    creds = get_google_creds()
    if not creds:
        return

    try:
        # Build the service
        service = build('tasks', 'v1', credentials=creds)

        # Call the Tasks API
        print("Fetching Task Lists...\n")
        results = service.tasklists().list(maxResults=10).execute()
        items = results.get('items', [])

        if not items:
            print("No task lists found.")
        else:
            print("Found the following task lists:")
            print("---------------------------------")
            for item in items:
                print(f"  List Name: {item['title']}")
                print(f"  List ID:   {item['id']}")
                print("---------------------------------")
            print("\nPlease copy the 'List ID' of the list you want to use (e.g., 'Comphone Tasks').")

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == '__main__':
    main()
