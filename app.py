import os
from flask import Flask
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import Blueprints
from main_routes import main_bp
from tool_routes import tools_bp
from api_routes import api_bp
from customer_routes import customer_bp
from line_handler import line_bp

# Import other necessary modules
from app_scheduler import initialize_scheduler
from settings_manager import settings_manager
import utils # Import the new utils file

def create_app():
    """Create and configure an instance of the Flask application."""
    app = Flask(__name__)
    app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_development_only')
    app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB

    # --- VARIABLE: TEXT_SNIPPETS ---
    # Moved from app_old.py. This is a global constant for the app.
    # Reason: This data is static and used in templates, making it suitable as a global config.
    app.config['TEXT_SNIPPETS'] = {
        'task_details': [
            {'key': 'ล้างแอร์', 'value': 'ล้างทำความสะอาดเครื่องปรับอากาศ, ตรวจเช็คน้ำยา, วัดแรงดันไฟฟ้า และทำความสะอาดคอยล์ร้อน-เย็น'},
            {'key': 'ติดตั้งแอร์', 'value': 'ติดตั้งเครื่องปรับอากาศใหม่ ขนาด [ขนาด BTU] พร้อมเดินท่อน้ำยาและสายไฟ, ติดตั้งเบรกเกอร์'},
            {'key': 'ซ่อมตู้เย็น', 'value': 'ซ่อมตู้เย็น [ยี่ห้อ/รุ่น] อาการไม่เย็น, ตรวจสอบคอมเพรสเซอร์และน้ำยา'},
            {'key': 'ตรวจเช็ค', 'value': 'เข้าตรวจเช็คอาการเสียเบื้องต้นตามที่ลูกค้าแจ้ง'}
        ],
        'progress_reports': [
            {'key': 'ลูกค้าเลื่อนนัด', 'value': 'ลูกค้าขอเลื่อนนัดเป็นวันที่ [dd/mm/yyyy] เนื่องจากไม่สะดวก'},
            {'key': 'รออะไหล่', 'value': 'ตรวจสอบแล้วพบว่าต้องรออะไหล่ [ชื่ออะไหล่] จะแจ้งลูกค้าให้ทราบกำหนดการอีกครั้ง'},
            {'key': 'เข้าพื้นที่ไม่ได้', 'value': 'ไม่สามารถเข้าพื้นที่ได้เนื่องจาก [เหตุผล] ได้โทรแจ้งลูกค้าแล้ว'},
            {'key': 'เสร็จบางส่วน', 'value': 'ดำเนินการเสร็จสิ้นบางส่วน เหลือ [สิ่งที่ต้องทำต่อ] จะเข้ามาดำเนินการต่อในวันถัดไป'}
        ]
    }


    # Register Blueprints
    app.register_blueprint(main_bp)
    app.register_blueprint(tools_bp, url_prefix='/tools')
    app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(customer_bp, url_prefix='/customer')
    app.register_blueprint(line_bp) # For /callback

    # Initialize scheduler
    initialize_scheduler(app)

    # --- FUNCTION: inject_global_vars ---
    # Moved from app_old.py. This function injects variables into all templates.
    # Reason: Context processors are a Flask feature that must be registered with the app instance.
    @app.context_processor
    def inject_global_vars():
        """Injects global variables into the template context."""
        return {
            'google_api_connected': utils.check_google_api_status(),
            'text_snippets': app.config['TEXT_SNIPPETS']
        }

    # Error Handlers
    @app.errorhandler(404)
    def page_not_found(e):
        return "404 Not Found", 404

    @app.errorhandler(500)
    def internal_server_error(e):
        # Log the error e
        return "500 Internal Server Error", 500

    return app

if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=True)
