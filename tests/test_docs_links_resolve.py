"""Every relative link in the public documentation leads somewhere.

The pages under docs/ and the README link each other by relative path, often with a heading
anchor (`page.md#heading`). A renamed page or heading leaves such a link dead without any
other test noticing. Each relative link must name a file that exists in the tree and, when it
carries an anchor, a heading of that file as GitHub turns headings into anchors (lower case,
punctuation dropped except `-` and `_`, spaces to `-`, a repeated slug numbered `-1`, `-2`).
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGES = sorted([ROOT / 'README.md', *(ROOT / 'docs').rglob('*.md')])

LINK = re.compile(r'(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
FENCE = re.compile(r'^\s*(```|~~~)')
HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')


def _without_fences(text):
    lines, inside = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            inside = not inside
            continue
        lines.append('' if inside else line)
    return lines


def slug(heading):
    text = re.sub(r'`([^`]*)`', r'\1', heading)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = text.strip().lower()
    text = re.sub(r'[^\w\- ]', '', text)
    return text.replace(' ', '-')


def anchors(page):
    seen, out = {}, set()
    for line in _without_fences(page.read_text(encoding='utf-8')):
        m = HEADING.match(line)
        if not m:
            continue
        base = slug(m.group(2))
        n = seen.get(base, 0)
        out.add(base if n == 0 else f'{base}-{n}')
        seen[base] = n + 1
    return out


def links(page):
    for number, line in enumerate(_without_fences(page.read_text(encoding='utf-8')), 1):
        for m in LINK.finditer(line):
            target = m.group(1).strip('<>')
            if re.match(r'^[a-z][a-z0-9+.-]*:', target, re.I) or target.startswith('//'):
                continue                                # an external address or mailto:
            yield number, target


@pytest.mark.parametrize('heading, expected', [
    ('Backup (admins only)', 'backup-admins-only'),
    ('The admin panel', 'the-admin-panel'),
    ('Buy or farm', 'buy-or-farm'),
    ('`SECRET_KEY` and the server', 'secret_key-and-the-server'),
    ('Data Export & Import', 'data-export--import'),
])
def test_the_slug_rule_is_githubs(heading, expected):
    assert slug(heading) == expected


@pytest.mark.parametrize('page', PAGES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_relative_link_of_a_page_resolves_to_a_file_and_heading(page):
    dead = []
    for number, target in links(page):
        path, _, anchor = target.partition('#')
        dest = (page.parent / path).resolve() if path else page
        if not dest.exists():
            dead.append(f'{number}: {target} (no such file)')
        elif anchor and dest.suffix == '.md' and anchor not in anchors(dest):
            dead.append(f'{number}: {target} (no such heading)')
    assert not dead, dead
