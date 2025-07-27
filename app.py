# ... (โค้ดส่วนบนของ app.py) ...

def create_app():
    app = Flask(__name__)
    # ... (โค้ดส่วนอื่นๆ ของ create_app) ...

    # --- FIX: Add helper function for templates ---
    @app.context_processor
    def utility_processor():
        def get_file_icon(filename):
            """Returns a Font Awesome icon class based on file extension."""
            ext = filename.split('.')[-1].lower() if '.' in filename else ''
            if ext in ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp']:
                return 'fas fa-file-image'
            if ext == 'pdf':
                return 'fas fa-file-pdf'
            if ext in ['kmz', 'kml']:
                return 'fas fa-map-marked-alt'
            return 'fas fa-file'
        return dict(get_file_icon=get_file_icon)
    # --- END FIX ---

    return app

if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=True)
