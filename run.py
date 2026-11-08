"""
Dev-convenience launcher — run from the project root: python run.py
For production / Azure, cd into app/ and run: python app.py
"""
import os
import sys
from pathlib import Path

app_dir = Path(__file__).parent / 'app'
sys.path.insert(0, str(app_dir))
os.chdir(str(app_dir))

from app import create_app  # noqa: E402 — sys.path must be set first

if __name__ == '__main__':
    flask_app = create_app()
    flask_app.run(host='0.0.0.0', port=5000, debug=True)
