"""The app draws its icons from its own SVG sprite, never from an icon font.

- The sprite (app/static/icons/icons.svg) holds one <symbol id="i-NAME"> per icon: a 24 x 24
  viewBox, simple shapes only, every colour `none` or `currentColor`, nothing it loads.
- Every name a template passes to the icon() macro, and every name a page script passes to
  wfIcon() or wfSetIcon(), has its symbol; so does every name the icon font still draws on
  the pages not yet moved, and every name those pages choose in a page script.
- The moved pages render with references into the sprite and no `material-icons`; every
  icon on them is hidden from assistive technology or carries a name, and every control
  whose only content is an icon has an accessible name.
- Flask serves the sprite as image/svg+xml, whatever the host's MIME table says.

Offline: nothing here reaches the network (tests/conftest.py).
"""
import mimetypes
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from jinja2 import nodes

from models.db import get_db
from models.item import Item
from models.user import User

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / 'app' / 'templates'
SPRITE = ROOT / 'app' / 'static' / 'icons' / 'icons.svg'
APP_JS = ROOT / 'app' / 'static' / 'js' / 'app.js'
SVG_NS = '{http://www.w3.org/2000/svg}'

# The pages that draw every icon through the macro (the rest still use the icon font).
MOVED = ['base.html', 'auth/login.html', 'auth/register.html', 'auth/setup.html', 'admin.html',
         'settings.html', 'sync.html', 'market.html', 'dashboard.html']

# Every icon name a template or page script used when the sprite was drawn, the names chosen
# in Jinja expressions and page scripts included: each must keep its symbol.
NAMES_IN_USE = [
    'account_circle', 'account_tree', 'add', 'add_task', 'admin_panel_settings', 'agriculture',
    'arrow_downward', 'arrow_upward', 'auto_awesome', 'auto_fix_high', 'backup', 'block',
    'bolt', 'bookmark', 'bookmark_border', 'broken_image', 'check', 'check_box',
    'check_box_outline_blank', 'check_circle', 'chevron_left', 'chevron_right', 'clear',
    'cloud_upload', 'dashboard', 'delete', 'delete_forever', 'delete_sweep', 'description',
    'diamond', 'download', 'edit', 'edit_note', 'folder', 'gps_fixed', 'grid_view',
    'hourglass_empty', 'image', 'info_outline', 'layers', 'list', 'lock', 'lock_reset', 'login',
    'logout', 'map', 'menu', 'notes', 'notifications_active', 'notifications_none',
    'open_in_new', 'person_add', 'push_pin', 'remove', 'remove_circle_outline', 'search',
    'settings', 'shield', 'spa', 'star', 'star_border', 'store', 'sync', 'table_rows',
    'thumb_up', 'view_list', 'vpn_key', 'warning',
]

SHAPES = {'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'ellipse', 'g'}
COLOUR_ATTRS = ('fill', 'stroke', 'color', 'stop-color', 'flood-color', 'lighting-color')
NAME = re.compile(r'[a-z0-9_]+\Z')


# ── The name extractors ─────────────────────────────────────────────────────────

def _literals(node):
    """The string constants an expression can evaluate to: a literal, or a conditional
    whose branches are. Anything else (a variable, a call, a concatenation) is refused: a
    name the test cannot see is a name nobody checks."""
    if isinstance(node, nodes.Const) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, nodes.CondExpr) and node.expr2 is not None:
        return _literals(node.expr1) + _literals(node.expr2)
    raise AssertionError(f'icon name is not a literal: {node!r}')


def macro_names(env, source):
    """Every name passed to icon() in a template source, by parsing it."""
    found = []
    for call in env.parse(source).find_all(nodes.Call):
        if isinstance(call.node, nodes.Name) and call.node.name == 'icon':
            if call.args:
                found += _literals(call.args[0])
            else:
                kw = [k for k in call.kwargs if k.key == 'name']
                assert kw, 'icon() without a name'
                found += _literals(kw[0].value)
    return found


_STRING = re.compile(r"'([^'\\]*)'|\"([^\"\\]*)\"")
_HELPER = re.compile(r'\bwf(Set)?Icon\(')


def _args(text, start):
    """The top-level arguments of the call whose '(' is at text[start - 1]."""
    depth, quote, args, cur = 0, None, [], ''
    for ch in text[start:]:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in '\'"':
            quote = ch
        elif ch in '([{':
            depth += 1
        elif ch in ')]}':
            if depth == 0:
                return args + [cur]
            depth -= 1
        elif ch == ',' and depth == 0:
            args.append(cur)
            cur = ''
            continue
        cur += ch
    raise AssertionError('unclosed helper call')


def script_names(source):
    """Every name a page script passes to wfIcon(name, ...) or wfSetIcon(el, name)."""
    found = []
    for m in _HELPER.finditer(source):
        args = _args(source, m.end())
        arg = args[1 if m.group(1) else 0]
        names = [a or b for a, b in _STRING.findall(arg)]
        assert names, f'icon name is not a literal: {arg.strip()!r}'
        found += names
    return found


_FONT_ICON = re.compile(r'<i class="material-icons[^"]*"[^>]*>(.*?)</i>', re.S)


def font_names(source):
    """Every name the icon font draws in a template: the ligature text, or the string
    literals of a Jinja expression or tag choosing it."""
    found = []
    for inner in _FONT_ICON.findall(source):
        literals = re.findall(r"'([a-z0-9_]+)'", inner)
        bare = re.sub(r'\{\{.*?\}\}|\{%.*?%\}', ' ', inner, flags=re.S).split()
        found += literals + bare
    return found


def template_sources():
    return {p.relative_to(TEMPLATES).as_posix(): p.read_text(encoding='utf-8')
            for p in sorted(TEMPLATES.rglob('*.html'))}


def sprite_symbols():
    root = ET.parse(SPRITE).getroot()
    assert root.tag == SVG_NS + 'svg'
    return root, {s.get('id'): s for s in root}


# Axis 1, input spellings: each way a name reaches the macro or a helper.
MACRO_SPELLINGS = [
    ("{{ icon('h2o_3') }}", ['h2o_3']),
    ("{{ icon('star', 'small') }}", ['star']),
    ("{{ icon(\"star\") }}", ['star']),
    ("{{ icon('star' if x else 'star_border', 'small') }}", ['star', 'star_border']),
    ("{{ icon('a' if x else ('b' if y else 'c')) }}", ['a', 'b', 'c']),
    ("{{ icon(name='lock', label='Locked') }}", ['lock']),
    ("{{ icon('block' if u else 'check_circle', label='A' if u else 'B') }}",
     ['block', 'check_circle']),
    ("{% set x = icon('menu') %}{{ x }}", ['menu']),
]


@pytest.mark.parametrize('source,names', MACRO_SPELLINGS, ids=[s for s, _ in MACRO_SPELLINGS])
def test_the_macro_name_extractor_finds_every_spelling(app, source, names):
    assert macro_names(app.jinja_env, source) == names


@pytest.mark.parametrize('source', ["{{ icon(n) }}", "{{ icon('a' ~ n) }}",
                                    "{{ icon('a' if x) }}", "{{ icon(names[0]) }}"])
def test_the_macro_name_extractor_refuses_a_name_it_cannot_see(app, source):
    with pytest.raises(AssertionError, match='not a literal'):
        macro_names(app.jinja_env, source)


SCRIPT_SPELLINGS = [
    ("wfIcon('folder', 'tiny')", ['folder']),
    ('wfIcon("h2o_3")', ['h2o_3']),
    ("x + wfIcon('broken_image', 'tiny') + ' '", ['broken_image']),
    ("wfSetIcon($btn[0], 'star_border');", ['star_border']),
    ("wfSetIcon(el, watched ? 'bookmark' : 'bookmark_border')", ['bookmark', 'bookmark_border']),
    ("wfSetIcon(f('a', 'b'), 'check')", ['check']),
    ("wfIcon(on ? 'star' : 'star_border', 'small', 'It, (really)')", ['star', 'star_border']),
]


@pytest.mark.parametrize('source,names', SCRIPT_SPELLINGS, ids=[s for s, _ in SCRIPT_SPELLINGS])
def test_the_script_name_extractor_finds_every_spelling(source, names):
    assert script_names(source) == names


@pytest.mark.parametrize('source', ["wfIcon(name)", "wfSetIcon(el, n)", "wfIcon(x.y, 'tiny')"])
def test_the_script_name_extractor_refuses_a_name_it_cannot_see(source):
    with pytest.raises(AssertionError, match='not a literal'):
        script_names(source)


def test_the_font_name_extractor_reads_ligatures_and_jinja_choices():
    source = ('<i class="material-icons tiny">info_outline</i>'
              '<i class="material-icons">{{ \'check_box\' if a else \'check_box_outline_blank\' }}</i>'
              '<i class="material-icons grey-text x">\n  {% if c == \'mod\' %}auto_fix_high'
              '{% else %}shield{% endif %}\n</i>')
    assert font_names(source) == ['info_outline', 'check_box', 'check_box_outline_blank',
                                  'mod', 'auto_fix_high', 'shield']


# ── The sprite ──────────────────────────────────────────────────────────────────

def test_the_sprite_has_a_symbol_for_every_name_in_use():
    _, symbols = sprite_symbols()
    missing = [n for n in NAMES_IN_USE if 'i-' + n not in symbols]
    assert missing == []


def test_every_sprite_entry_is_a_named_symbol_with_a_unique_id():
    root, _ = sprite_symbols()
    ids = []
    for child in root:
        assert child.tag == SVG_NS + 'symbol', child.tag
        ident = child.get('id') or ''
        assert ident.startswith('i-') and NAME.match(ident[2:]), ident
        ids.append(ident)
    assert len(ids) == len(set(ids))
    assert len(ids) >= 60


def _symbol_ids():
    return sorted(sprite_symbols()[1])


@pytest.mark.parametrize('ident', _symbol_ids())
def test_every_symbol_is_a_24_grid_drawing_in_current_colour(ident):
    symbol = sprite_symbols()[1][ident]
    assert symbol.get('viewBox') == '0 0 24 24'
    uses_current = False
    for el in symbol.iter():
        if el is not symbol:
            assert el.tag[len(SVG_NS):] in SHAPES, el.tag
        for attr, value in el.attrib.items():
            assert 'href' not in attr and attr != 'style', (el.tag, attr)
            assert 'url(' not in value, (el.tag, attr)
            if attr in COLOUR_ATTRS:
                assert value in ('none', 'currentColor'), (el.tag, attr, value)
                uses_current |= value == 'currentColor'
        for attr in ('x', 'y', 'cx', 'cy', 'x1', 'y1', 'x2', 'y2'):
            if attr in el.attrib:
                assert 0 <= float(el.attrib[attr]) <= 24, (el.tag, attr)
    assert uses_current


def test_the_sprite_is_well_formed_svg_with_nothing_it_loads():
    text = SPRITE.read_text(encoding='utf-8')
    ET.fromstring(text)
    for word in ('<script', '<style', '<image', '<use', 'href=', 'http:', 'https:', '@import'):
        assert word not in text.replace('http://www.w3.org/2000/svg', ''), word


# ── Every name the templates and scripts use has its symbol ─────────────────────

def test_every_macro_name_in_the_templates_has_its_symbol(app):
    _, symbols = sprite_symbols()
    used = []
    for path, source in template_sources().items():
        used += [(path, n) for n in macro_names(app.jinja_env, source)]
    assert len(used) >= 50
    assert [u for u in used if 'i-' + u[1] not in symbols] == []


def test_every_name_a_page_script_draws_has_its_symbol():
    _, symbols = sprite_symbols()
    used = []
    for path, source in template_sources().items():
        used += [(path, n) for n in script_names(source)]
    used += [('app.js', n) for n in script_names(APP_JS.read_text(encoding='utf-8'))]
    assert ('sync.html', 'folder') in used and ('dashboard.html', 'star_border') in used
    assert [u for u in used if 'i-' + u[1] not in symbols] == []


def test_every_name_the_icon_font_still_draws_has_its_symbol():
    """The pages not yet moved keep the font until they are; their names are drawn already."""
    _, symbols = sprite_symbols()
    used = []
    for path, source in template_sources().items():
        used += [(path, n) for n in font_names(source) if n not in ('mod', 'arcane')]
    assert [u for u in used if 'i-' + u[1] not in symbols] == []


@pytest.mark.parametrize('path', MOVED)
def test_a_moved_template_has_no_font_icon_and_draws_through_the_macro(path):
    source = (TEMPLATES / path).read_text(encoding='utf-8')
    assert 'material-icons' not in source
    assert '<svg' not in source
    assert "{% from '_icons.html' import icon" in source


def test_the_macro_writes_the_sprite_reference_and_hides_a_decoration(app):
    with app.test_request_context():
        macro = app.jinja_env.get_template('_icons.html').module.icon
        plain = str(macro('star_border', 'small'))
        named = str(macro('lock', label='Reset <"password">'))
    assert plain.startswith('<svg class="wf-icon small"')
    assert 'aria-hidden="true"' in plain and 'role=' not in plain
    assert 'focusable="false"' in plain
    assert '<use href="/static/icons/icons.svg#i-star_border"></use>' in plain
    assert 'role="img" aria-label="Reset &lt;&#34;password&#34;&gt;"' in named
    assert 'aria-hidden' not in named


# ── The moved pages, rendered ───────────────────────────────────────────────────

@pytest.fixture
def seeded(app, admin_client):
    """A second user (the admin's row controls), a watched item with a listing (the market
    links), and an owned, unmastered item (the dashboard's mastery star)."""
    with app.app_context():
        admin = User.get_by_username('admin').id
        User.create('alice', password='pw-alice-1')
        Item.upsert('Braton Prime', 'primary', source='standard')
        db = get_db()
        db.execute("UPDATE items SET market_url_name = 'braton_prime_set' "
                   "WHERE name = 'Braton Prime'")
        item = db.execute("SELECT id FROM items WHERE name = 'Braton Prime'").fetchone()[0]
        db.execute('INSERT INTO user_market_watchlist (user_id, item_id) VALUES (?, ?)',
                   (admin, item))
        db.execute('INSERT INTO user_progress (user_id, item_id, in_possession, mastered) '
                   'VALUES (?, ?, 1, 0)', (admin, item))
        db.commit()
    return admin_client


AUTHED_PAGES = ['/dashboard', '/market/', '/settings/', '/admin/', '/sync/']

_CONTROL = re.compile(r'<(a|button)\b([^>]*)>(.*?)</\1>', re.S)
_SVG = re.compile(r'<svg\b([^>]*)>', re.S)


def assert_icons_are_accessible(html):
    for attrs in _SVG.findall(html):
        if 'wf-icon' not in attrs:
            continue
        named = re.search(r'aria-label="([^"]+)"', attrs)
        assert 'aria-hidden="true"' in attrs or ('role="img"' in attrs and named), attrs
    for tag, attrs, inner in _CONTROL.findall(html):
        text = re.sub(r'<[^>]+>', '', inner).strip()
        if text:
            continue
        own = re.search(r'aria-label="[^"]+"', attrs)
        icon = re.search(r'<svg[^>]*role="img"[^>]*aria-label="[^"]+"', inner)
        assert own or icon, f'<{tag}{attrs}> has no accessible name'


def assert_drawn_from_the_sprite(html):
    _, symbols = sprite_symbols()
    assert 'material-icons' not in html
    refs = re.findall(r'<use href="/static/icons/icons\.svg#i-([a-z0-9_]+)"', html)
    assert refs
    assert [r for r in refs if 'i-' + r not in symbols] == []
    assert html.count('class="wf-icon') == len(refs)


@pytest.mark.parametrize('url', AUTHED_PAGES)
def test_a_moved_page_renders_its_icons_from_the_sprite(seeded, url):
    response = seeded.get(url)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert_drawn_from_the_sprite(html)
    assert_icons_are_accessible(html)
    assert '<meta name="icon-sprite" content="/static/icons/icons.svg">' in html


@pytest.mark.parametrize('url,icon_only', [
    ('/admin/', ['Deactivate', 'Reset password', 'Delete user', 'Menu']),
    ('/market/', ['Open on warframe.market', 'Remove from watchlist']),
    ('/dashboard', ['Mark as Mastered', 'Open on warframe.market']),
])
def test_icon_only_controls_name_themselves(seeded, url, icon_only):
    html = seeded.get(url).get_data(as_text=True)
    for label in icon_only:
        assert f'role="img" aria-label="{label}"' in html, label


@pytest.mark.parametrize('url', ['/auth/login', '/auth/register'])
def test_the_sign_in_pages_render_their_icons_from_the_sprite(app, admin_client, url):
    response = app.test_client().get(url)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert_drawn_from_the_sprite(html)
    assert_icons_are_accessible(html)


def test_the_setup_page_renders_its_icons_from_the_sprite(client):
    response = client.get('/auth/setup')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert_drawn_from_the_sprite(html)
    assert_icons_are_accessible(html)


def test_the_dashboard_star_swaps_through_the_helper(seeded):
    html = seeded.get('/dashboard').get_data(as_text=True)
    assert "wfSetIcon($btn[0], 'star_border')" in html
    assert ".find('i')" not in html


# ── Serving the sprite ──────────────────────────────────────────────────────────

def test_the_sprite_is_served_as_svg(client):
    response = client.get('/static/icons/icons.svg')
    assert response.status_code == 200
    assert response.mimetype == 'image/svg+xml'
    assert b'<symbol id="i-star"' in response.data


@pytest.mark.parametrize('host_type', [None, 'text/plain', 'application/octet-stream'],
                         ids=['missing', 'text', 'binary'])
def test_the_sprite_is_svg_whatever_the_host_mime_table_says(monkeypatch, tmp_path, host_type):
    """Windows reads its MIME table from the registry, where .svg can be absent or mapped
    to another type; the app registers the type itself when it starts."""
    import app as app_module
    mimetypes.guess_type('x.svg')            # the table is loaded before it is changed
    if host_type is None:
        monkeypatch.delitem(mimetypes.types_map, '.svg', raising=False)
    else:
        monkeypatch.setitem(mimetypes.types_map, '.svg', host_type)
    flask_app = app_module.create_app({
        'TESTING': True,
        'DATABASE_PATH': str(tmp_path / 'mime.db'),
        'SYNC_INTERVAL_HOURS': 0,
        'AUTO_SYNC_ON_STARTUP': False,
        'SECRET_KEY': 'test-only',
    })
    try:
        response = flask_app.test_client().get('/static/icons/icons.svg')
        assert response.mimetype == 'image/svg+xml'
    finally:
        scheduler = getattr(flask_app, 'scheduler', None)
        if scheduler is not None:
            scheduler.shutdown(wait=False)
