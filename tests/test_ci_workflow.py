"""The CI workflow and the dependency pins agree.

The public repository runs this suite on GitHub Actions (.github/workflows/tests.yml). The
workflow must test what a local run tests: Python 3.11, the packages pinned in
app/requirements.txt and requirements-dev.txt and no package of its own, `python -m pytest`.
It also stays as small as it is: two GitHub-owned actions by major tag, a runner image pinned
by version, a read-only token, no secrets.

The standard library has no YAML reader, so the check reads the file by a fixed shape: every
line that is not blank or a comment must be a `key: value` line (optionally a `- ` list
item) with a key from a short allowed list. A line of any other shape, or an unknown key, is
refused rather than skipped: a check that silently skipped what it could not read would pass
a workflow it never looked at.
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / '.github' / 'workflows' / 'tests.yml'
REQUIREMENT_FILES = ('app/requirements.txt', 'requirements-dev.txt')

INSTALL = 'python -m pip install -r app/requirements.txt -r requirements-dev.txt'
TEST = 'python -m pytest'

LINE = re.compile(r'^(?P<indent> *)(?:- +)?(?P<key>[a-z][a-z-]*):(?: +(?P<value>.*))?$')
# Each allowed key with the values it may take; None: any value (a step's display name).
EXPECTED = {
    'name': None,
    'on': ['[push, pull_request]'],
    'permissions': [''],
    'contents': ['read'],
    'jobs': [''],
    'test': [''],
    'runs-on': ['ubuntu-24.04'],
    'steps': [''],
    'uses': ['actions/checkout@v4', 'actions/setup-python@v5'],
    'with': [''],
    'python-version': ['3.11'],
    'run': [INSTALL, TEST],
}


def unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '\'"':
        return value[1:-1]
    return value


def read_workflow(text: str) -> dict:
    """Map each key to its values in file order; raise ValueError on any other line shape."""
    found: dict = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r'\s+#.*$', '', raw).rstrip()
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        m = LINE.match(line)
        if not m:
            raise ValueError(f'line {n} is not a plain "key: value" line: {raw!r}')
        found.setdefault(m['key'], []).append(unquote((m['value'] or '').strip()))
    return found


def disagreements(text: str) -> list:
    """Every way the workflow text differs from the agreed shape; empty when it agrees."""
    if '\t' in text:
        return ['a tab in the file']
    if 'secrets.' in text:
        return ['the workflow reads a secret']
    try:
        found = read_workflow(text)
    except ValueError as e:
        return [str(e)]
    problems = [f'unknown key {k!r}' for k in found if k not in EXPECTED]
    problems += [f'{k}: {found.get(k, [])!r}, expected {want!r}'
                 for k, want in EXPECTED.items()
                 if want is not None and found.get(k, []) != want]
    return problems


def test_the_workflow_agrees_with_the_pins():
    assert disagreements(WORKFLOW.read_text(encoding='utf-8')) == []


def test_the_workflow_installs_the_two_requirement_files_that_exist():
    named = re.findall(r'-r (\S+)', INSTALL)
    assert tuple(named) == REQUIREMENT_FILES
    assert all((ROOT / f).is_file() for f in named)


def test_the_documented_install_line_is_the_one_the_workflow_runs():
    """requirements-dev.txt tells a reader how to install; CI must do the same."""
    assert INSTALL in (ROOT / 'requirements-dev.txt').read_text(encoding='utf-8')


def test_every_requirement_is_pinned_to_an_exact_version():
    """A floating requirement would let CI and a local run test different packages."""
    loose = []
    for name in REQUIREMENT_FILES:
        for line in (ROOT / name).read_text(encoding='utf-8').splitlines():
            line = line.split('#', 1)[0].strip()
            if line and not re.fullmatch(r'[A-Za-z0-9._-]+==[0-9][0-9A-Za-z.]*', line):
                loose.append(f'{name}: {line}')
    assert loose == []


def _replace(old: str, new: str):
    text = WORKFLOW.read_text(encoding='utf-8')
    assert old in text, old
    return text.replace(old, new, 1)


# Each row breaks the real workflow in one way; the check must refuse every one.
@pytest.mark.parametrize('old, new', [
    ("python-version: '3.11'", "python-version: '3.12'"),
    ("python-version: '3.11'", "python-version: '3.11.4'"),
    ("python-version: '3.11'", "python-version: 3.1"),
    ("python-version: '3.11'", "python-version-file: .python-version"),
    ('run: python -m pytest', 'run: pip install pytest-xdist && python -m pytest'),
    ('-r requirements-dev.txt', '-r requirements-dev.txt pytest==8.0.0'),
    ('-r app/requirements.txt -r requirements-dev.txt', '-r requirements-dev.txt'),
    ('run: python -m pytest', 'run: python -m pytest -k smoke'),
    ('run: python -m pytest', 'run: |\n          python -m pytest'),
    ('runs-on: ubuntu-24.04', 'runs-on: ubuntu-latest'),
    ('actions/setup-python@v5', 'actions/setup-python@main'),
    ('actions/checkout@v4', 'someone/checkout@v4'),
    ('contents: read', 'contents: write'),
    ('on: [push, pull_request]', 'on: [push]'),
    ('        with:\n', '        with: {python-version: "3.12"}\n'),
    ('        run: python -m pytest', '        run: python -m pytest\n        env:\n          X: y'),
    ('        run: python -m pytest', '        run: python -m pytest\n      - run: ./deploy.sh'),
    ('        run: python -m pytest', '        run: python -m pytest\n        "env": {}'),
    ('        run: python -m pytest', '        run: python -m pytest\n        token: ${{ secrets.T }}'),
    ('    runs-on: ubuntu-24.04', '\trunsOn: ubuntu-24.04'),
])
def test_a_changed_workflow_is_refused(old, new):
    assert disagreements(_replace(old, new)) != []


def test_comments_and_blank_lines_are_ignored():
    text = WORKFLOW.read_text(encoding='utf-8')
    commented = text.replace('runs-on: ubuntu-24.04', 'runs-on: ubuntu-24.04  # the image\n\n# x')
    assert disagreements(commented) == []


def test_a_missing_odbc_driver_leaves_the_database_module_importable(monkeypatch):
    """The runner may lack the ODBC library pyodbc loads; the SQLite backend must not care.

    sys.modules[name] = None makes `import name` raise ImportError, as a pyodbc whose shared
    library is missing does. The module is loaded as a fresh copy under another name, so the
    one the app already imported is left alone.
    """
    monkeypatch.setitem(sys.modules, 'pyodbc', None)
    spec = importlib.util.spec_from_file_location('db_without_pyodbc',
                                                  ROOT / 'app' / 'models' / 'db.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._pyodbc is None
