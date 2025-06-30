from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file('credentials.json', ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/tasks'])
creds = flow.run_local_server(port=0)
with open('token.json', 'w') as token:
    token.write(creds.to_json())
