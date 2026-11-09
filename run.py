"""
Development launcher, run from the project root: python run.py
It runs app/app.py as `python app.py` would (the same host, debug and key rules) from the
project root. Production runs Gunicorn instead (gunicorn app:app, from app/).
"""
import os
import runpy
import sys
from pathlib import Path

app_dir = Path(__file__).resolve().parent / 'app'

if __name__ == '__main__':
    # Debug mode's reloader starts this script again from the working directory set below
    # (app/): name the script by its full path so that the restart still finds it.
    script = os.path.abspath(sys.argv[0])
    sys.orig_argv = [script if arg == sys.argv[0] else arg for arg in sys.orig_argv]
    sys.argv[0] = script
    sys.path.insert(0, str(app_dir))
    os.chdir(str(app_dir))
    runpy.run_path(str(app_dir / 'app.py'), run_name='__main__')
