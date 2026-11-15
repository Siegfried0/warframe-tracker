"""A relative DATABASE_PATH is taken from app/, whichever launcher starts the app, and the
configuration page says so.

`python run.py` is started from the project root but runs the app with app/ as its working
directory (as Gunicorn with `--chdir app` does), so a relative path such as `tracker.db`
names `app/tracker.db`, not a file in the folder the command was typed in. The configuration
page must tell the reader which folder that is: the restore steps on the backup page put the
backup "at the path DATABASE_PATH names".
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RECORDER = '''
import flask
flask.Flask.run = lambda self, **kwargs: print('RUN')
'''


def test_run_py_from_the_project_root_opens_a_relative_database_path_inside_app(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    shutil.copy2(ROOT / 'run.py', project / 'run.py')
    shutil.copytree(ROOT / 'app', project / 'app',
                    ignore=shutil.ignore_patterns('static', '__pycache__', '*.db', '*.db-*'))
    double = tmp_path / 'double'
    double.mkdir()
    (double / 'sitecustomize.py').write_text(RECORDER, encoding='utf-8')
    env = {k: v for k, v in os.environ.items()
           if k not in ('MSSQL_CONNECTION_STRING', 'EASYAUTH_ENABLED', 'FLASK_RUN_HOST',
                        'FLASK_DEBUG', 'PYTHONPATH', 'BACKUP_LOCAL_PATH')}
    env.update(PYTHONPATH=str(double), DATABASE_PATH='relative-probe.db',
               SECRET_KEY='a-test-key', SYNC_INTERVAL_HOURS='0', AUTO_SYNC_ON_STARTUP='false',
               PYTHONIOENCODING='utf-8')
    result = subprocess.run([sys.executable, 'run.py'], cwd=str(project), capture_output=True,
                            text=True, timeout=120, env=env)
    assert result.returncode == 0, result.stderr
    assert 'RUN' in result.stdout
    assert (project / 'app' / 'relative-probe.db').is_file()
    assert not (project / 'relative-probe.db').exists()


def test_the_configuration_page_says_a_relative_database_path_is_taken_from_app():
    page = (ROOT / 'docs' / 'configuration.md').read_text(encoding='utf-8')
    row = next(line for line in page.splitlines() if line.startswith('| `DATABASE_PATH`'))
    meaning = row.split('|')[3]
    # The row must name the folder a relative path is taken from: app/ (the working directory
    # of both `python run.py` and the Gunicorn command), not only "the directory the app runs
    # in", which a reader who typed the command in the project root takes for the root.
    assert re.search(r'relative[^|]*`app/`', meaning), meaning
