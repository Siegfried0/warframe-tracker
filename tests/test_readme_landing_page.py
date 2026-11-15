"""The README is the landing page: it keeps the section the app's own pages point to, and it
links every page of the documentation.

The Settings page sends admins to the README's "Backups" section for the backup variables; a
README without that heading leaves the hint pointing at nothing. A docs page the README does
not link is reachable only by browsing the folder.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / 'README.md').read_text(encoding='utf-8')
LINK = re.compile(r'\]\(([^)\s#]+)(?:#[^)\s]*)?\)')


def test_settings_page_points_to_a_backups_section_the_readme_has():
    settings = (ROOT / 'app' / 'templates' / 'settings.html').read_text(encoding='utf-8')
    assert 'the README, "Backups"' in settings
    assert re.search(r'^## Backups\s*$', README, re.MULTILINE)


def test_readme_links_every_docs_page():
    linked = {(ROOT / target).resolve() for target in LINK.findall(README)}
    pages = {page.resolve() for page in (ROOT / 'docs').rglob('*.md')}
    assert pages, 'no docs pages found'
    assert sorted(p.relative_to(ROOT).as_posix() for p in pages - linked) == []
