# routes/auth.py

from flask import Blueprint, request, redirect, url_for, flash, current_app, session
from flask_login import login_user, logout_user, login_required, current_user
from models import db, User # Import db and User model
import requests # For making HTTP requests to LINE API
import json
import os
import base64 # For decoding JWT
from functools import wraps # For decorators

auth_bp = Blueprint('auth', __name__)

# Decorator for Role-Based Access Control
def role_required(roles):
    """
    Decorator to restrict access to a route based on user roles.
    Assumes Flask-Login is configured and current_user is available.
    """
    def decorator(f):
        @wraps(f)
        @login_required # Ensure user is logged in before checking role
        def decorated_function(*args, **kwargs):
            if not current_user.is_active:
                flash('บัญชีผู้ใช้ของคุณไม่ Active. กรุณาติดต่อผู้ดูแลระบบ.', 'warning')
                logout_user() # Log out inactive user
                return redirect(url_for('auth.login_line')) # Redirect to login
            
            # current_app.logger.info(f"User {current_user.name} (Role: {current_user.role}) attempting to access {request.path}")
            if current_user.role not in roles:
                flash('คุณไม่มีสิทธิ์เข้าถึงหน้านี้.', 'danger')
                return redirect(url_for('web.summary')) # Redirect to a safe page like summary
            return f(*args, **kwargs)
        return decorated_function
    return decorator

@auth_bp.route("/login_line")
def login_line():
    """
    Redirects user to LINE Login authorization URL.
    """
    line_login_channel_id = current_app.config['LINE_LOGIN_CHANNEL_ID']
    line_login_redirect_uri = current_app.config['LINE_LOGIN_REDIRECT_URI']
    
    # Generate a random state and nonce for security
    state = os.urandom(16).hex()
    nonce = os.urandom(16).hex()
    session['oauth_state'] = state
    session['oauth_nonce'] = nonce

    auth_url = (
        f"https://access.line.me/oauth2/v2.1/authorize?"
        f"response_type=code&"
        f"client_id={line_login_channel_id}&"
        f"redirect_uri={line_login_redirect_uri}&"
        f"state={state}&"
        f"scope=openid%20profile%20email&" # Request profile and email scopes
        f"nonce={nonce}"
    )
    current_app.logger.info(f"Redirecting to LINE Login: {auth_url}")
    return redirect(auth_url)

@auth_bp.route("/callback_line")
def callback_line():
    """
    Handles the callback from LINE Login after user authorization.
    Exchanges authorization code for access token and user profile.
    """
    code = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')
    error_description = request.args.get('error_description')

    if error:
        flash(f"LINE Login error: {error} - {error_description}", 'danger')
        current_app.logger.error(f"LINE Login error: {error} - {error_description}")
        return redirect(url_for('web.form_page')) # Redirect to home or login page

    if 'oauth_state' not in session or state != session['oauth_state']:
        flash("Invalid state parameter. Possible CSRF attack.", 'danger')
        current_app.logger.error("Invalid state parameter during LINE Login callback.")
        session.pop('oauth_state', None)
        return redirect(url_for('web.form_page'))

    session.pop('oauth_state', None) # Remove state from session

    # Exchange authorization code for access token
    token_url = "https://api.line.me/oauth2/v2.1/token"
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    data = {
        'grant_type': 'authorization_code',
        'client_id': current_app.config['LINE_LOGIN_CHANNEL_ID'],
        'client_secret': current_app.config['LINE_LOGIN_CHANNEL_SECRET'],
        'code': code,
        'redirect_uri': current_app.config['LINE_LOGIN_REDIRECT_URI']
    }

    try:
        response = requests.post(token_url, headers=headers, data=data)
        response.raise_for_status() # Raise an HTTPError for bad responses (4xx or 5xx)
        token_info = response.json()
        
        id_token = token_info.get('id_token')
        access_token = token_info.get('access_token')

        if not id_token:
            flash("Failed to get ID token from LINE.", 'danger')
            current_app.logger.error("No ID token in LINE token response.")
            return redirect(url_for('web.form_page'))

        # Verify ID Token and get user profile
        try:
            # Decode the payload part of the JWT (base64url decoded)
            # This is a simplified decode for quick access to claims, without full verification
            payload = id_token.split('.')[1]
            # Pad the payload if its length is not a multiple of 4
            padding = '=' * (4 - len(payload) % 4)
            decoded_payload = base64.urlsafe_b64decode(payload + padding).decode('utf-8')
            id_token_claims = json.loads(decoded_payload)
            
            line_user_id = id_token_claims.get('sub') # 'sub' is the user ID
            display_name = id_token_claims.get('name')
            email = id_token_claims.get('email')
            nonce = id_token_claims.get('nonce') # Nonce should match session['oauth_nonce']

            if nonce != session.pop('oauth_nonce', None):
                flash("Invalid nonce parameter. Possible replay attack.", 'danger')
                current_app.logger.error(f"Invalid nonce: expected {session.get('oauth_nonce')}, got {nonce}")
                return redirect(url_for('web.form_page'))


        except Exception as e:
            flash(f"Failed to decode ID token: {e}", 'danger')
            current_app.logger.error(f"Failed to decode ID token: {e}")
            return redirect(url_for('web.form_page'))

        current_app.logger.info(f"LINE User ID: {line_user_id}, Name: {display_name}, Email: {email}")

        # Check if user exists in your DB, otherwise create them
        user = User.query.get(line_user_id)
        if not user:
            user = User(id=line_user_id, name=display_name or "LINE User", email=email)
            
            # --- TEMPORARY: Assign ADMIN role for a specific LINE User ID for testing ---
            # Replace 'YOUR_ADMIN_LINE_USER_ID_HERE' with your actual LINE User ID for testing purposes
            # You can find your LINE User ID by sending a message to your bot and checking the logs
            # or using a tool that reveals your LINE User ID.
            master_admin_id = os.environ.get('MASTER_ADMIN_LINE_ID')
            if master_admin_id and line_user_id == master_admin_id:
                user.role = 'admin'
                current_app.logger.info(f"Assigned ADMIN role to {user.name} ({user.id}) via MASTER_ADMIN_LINE_ID.")
            else:
                user.role = 'customer' # Default role for new users
                current_app.logger.info(f"New user registered with default role: {user.name} ({user.id}) - Role: {user.role}")

            db.session.add(user)
            db.session.commit()
            
        else:
            # Update user info if needed
            user.name = display_name or user.name
            user.email = email or user.email
            db.session.commit()
            current_app.logger.info(f"Existing user logged in: {user.name} ({user.id}) - Role: {user.role}")

        # Log in the user with Flask-Login
        login_user(user)
        flash(f"เข้าสู่ระบบสำเร็จ! ยินดีต้อนรับ, {user.name}", 'success')
        return redirect(url_for('web.summary')) # Redirect to summary page after login

    except requests.exceptions.HTTPError as e:
        flash(f"HTTP Error during token exchange: {e.response.text}", 'danger')
        current_app.logger.error(f"HTTP Error during token exchange: {e.response.text}")
        return redirect(url_for('web.form_page'))
    except requests.exceptions.RequestException as e:
        flash(f"Network error during token exchange: {e}", 'danger')
        current_app.logger.error(f"Network error during token exchange: {e}")
        return redirect(url_for('web.form_page'))

@auth_bp.route("/logout")
@login_required # User must be logged in to log out
def logout():
    """
    Logs out the current user.
    """
    logout_user()
    flash("ออกจากระบบเรียบร้อยแล้ว", 'info')
    return redirect(url_for('web.form_page'))
