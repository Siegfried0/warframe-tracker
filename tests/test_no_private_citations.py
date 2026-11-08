"""The published files state each rule or reason themselves.

They never point at the private planning records instead: no decision-record number, no
task id and no board issue id appears in the code, its comments and docstrings, the tests
or the documentation. Third-party files under app/static/vendor are not ours and are not
searched.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PUBLISHED = ['app', 'tests', 'run.py', 'requirements-dev.txt', 'README.md', 'LICENSE', 'docs',
             '.github/workflows']
SKIPPED_DIRS = {'__pycache__', 'vendor', 'images'}
TEXT_SUFFIXES = {'', '.py', '.html', '.js', '.css', '.md', '.txt', '.json', '.yml', '.yaml',
                 '.toml', '.ini', '.cfg'}
CITATION = re.compile(r'\bADR[- ]?\d+|\bT-\d{3}\b|\bWAR-\d+\b')


def published_files():
    for entry in PUBLISHED:
        path = ROOT / entry
        if path.is_file():
            yield path
        elif path.is_dir():
            for f in sorted(path.rglob('*')):
                if f.is_file() and f.suffix in TEXT_SUFFIXES and \
                        not SKIPPED_DIRS & set(f.relative_to(ROOT).parts):
                    yield f


def citations(text: str) -> list:
    return [(n, m.group()) for n, line in enumerate(text.splitlines(), 1)
            for m in CITATION.finditer(line)]


def test_no_published_file_cites_a_private_record():
    found = [f'{f.relative_to(ROOT).as_posix()}:{n}: {hit}'
             for f in published_files()
             for n, hit in citations(f.read_text(encoding='utf-8', errors='replace'))]
    assert found == []


def test_the_search_covers_code_tests_and_readme():
    names = {f.relative_to(ROOT).as_posix() for f in published_files()}
    assert {'README.md', 'app/app.py', 'app/templates/settings.html',
            'tests/test_no_private_citations.py'} <= names
    assert not [n for n in names if '/vendor/' in n or '__pycache__' in n]


# The positive rows are glued from parts so that this file does not cite anything itself.
ADR, TASK, ISSUE = 'AD' + 'R', 'T' + '-0', 'WA' + 'R-'


@pytest.mark.parametrize('line, hit', [
    (f'the toggle rule ({ADR}-0002)', True), (f'see {ADR} 7', True), (f'{ADR}0003 §1', True),
    (f'split pending: {TASK}13', True), (f'hand-forward from {ISSUE}97', True),
    ('T-1000-ish', False), ('MT-013', False), ('LT-020', False), ('an adr table', False),
    ('HARDWARE-12', False), ('Tier T-1 relic', False),
])
def test_the_citation_pattern_matches_record_names_only(line, hit):
    assert bool(citations(line)) is hit
