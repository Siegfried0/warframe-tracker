"""The farming and sync pages' scripts escape every server string they draw into HTML.

Names, categories, locations and URLs come from sync (WFCD, the wiki); a name holding `"`,
`<` or `&` must show as text, never break an attribute or render as markup. Each script
builds its HTML by string concatenation, so the rule is pinned two ways: every known site
names its escape, and no bare `+ obj.<string field>` is left anywhere in the scripts.
"""
import re

import pytest


ESCAPED_SITES = [
    ('/farming/', 'escText(data.node)'),
    ('/farming/', 'escText(opp.name)'),
    ('/farming/', "escText((opp.goal_labels || []).join(', '))"),
    ('/farming/', 'escText(h.name)'),
    ('/farming/', 'escText(plan.market_url)'),
    ('/farming/', 'escText(comp.image_url)'),
    ('/farming/', 'escText(ing.image_url)'),
    ('/farming/', 'escText(ing.ingredient_name)'),
    ('/farming/', 'title="\' + escText(res.ingredient_name) + \'">\' + escText(res.ingredient_name)'),
    ('/farming/', 'escText(res.image_url)'),
    ('/farming/', 'escText(bestDrop.location)'),
    ('/farming/', '<td class="white-text">\' + escText(res.ingredient_name) + shared'),
    ('/farming/', "escText((res.goal_labels || []).join(', '))"),
    ('/farming/', 'escText(item.image_url)'),
    ('/farming/', 'escText(item.name)'),
    ('/farming/', 'escText(item.category)'),
    ('/farming/', 'var dotTitle  = escText(cname);'),
    ('/farming/', '<span class="white-text" style="font-size:.9rem">\' + escText(n) + \'</span>'),
    ('/farming/', 'data-state="\' + escText(state) + \'"'),
    ('/farming/', "M.toast({html: escText(r.error || 'Could not add the goal.')})"),
    ('/farming/', "M.toast({html: escText(r.goal.item_name) + ' is already in your goals.'})"),
    ('/farming/', "M.toast({html: escText(r.goal.item_name) + ' added to farming goals.'})"),
    ('/farming/', "M.toast({html: 'Advisor set to: ' + escText(nodeLoc)})"),
    ('/sync/', 'escText(cat.category)'),
    ('/sync/', 'escText(item.category)'),
    ('/sync/', 'escText(item.name)'),
    ('/collection/warframe', "M.toast({html: escHtml(r.error || 'Failed to add goal.')})"),
    ('/collection/warframe',
     "M.toast({html: escHtml(r.goal.item_name) + ' is already in your farming goals.'})"),
    ('/collection/warframe',
     "M.toast({html: escHtml(r.goal.item_name) + ' added to Farming Planner. <a href=\"/farming/\" "
     "class=\"white-text\">View</a>'})"),
]

# A server string concatenated into the markup bare: `+ obj.field` (escText(obj.field) or
# encodeURIComponent(obj.field) are not matched, since the field follows a parenthesis). The
# checkbox id that keeps only [a-z0-9] of a name is safe as it is.
BARE_STRING_FIELD = re.compile(
    r"\+\s*\w+\.(?:name|ingredient_name|node|category|location|image_url|market_url"
    r"|goal_labels|item_name)\b(?!\s*\()(?!\.replace\(/\[\^a-z0-9\]/gi, '-'\))")
# A list of goal labels joined into the markup, unless the join is inside escText.
BARE_JOINED_LABELS = re.compile(r"(?<!escText\()\(\w+\.goal_labels \|\| \[\]\)\.join")
# An escape of the double quote alone (it leaves <, > and & as markup).
QUOTE_ONLY_REPLACE = re.compile(r"\.replace\(/\"/g")

ESC_TEXT = ("function escText(s) {\n"
            "  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')\n"
            "                  .replace(/\"/g, '&quot;');\n"
            "}")


def _page(admin_client, path):
    resp = admin_client.get(path)
    assert resp.status_code == 200
    return resp.get_data(as_text=True).replace('\r\n', '\n')


def _scripts(page):
    return '\n'.join(re.findall(r'<script[^>]*>(.*?)</script>', page, re.S))


@pytest.mark.parametrize('path,expression', ESCAPED_SITES)
def test_the_page_escapes_each_synced_string_it_draws(admin_client, path, expression):
    assert expression in _page(admin_client, path)


@pytest.mark.parametrize('path', ['/farming/', '/sync/'])
def test_no_page_script_draws_a_synced_string_bare(admin_client, path):
    scripts = _scripts(_page(admin_client, path))
    assert ESC_TEXT in scripts
    scripts = scripts.replace(ESC_TEXT, '')  # the escape itself is the one allowed replace
    assert BARE_STRING_FIELD.findall(scripts) == []
    assert BARE_JOINED_LABELS.findall(scripts) == []
    assert QUOTE_ONLY_REPLACE.findall(scripts) == []


@pytest.mark.parametrize('path', ['/farming/', '/sync/'])
def test_each_page_escapes_all_four_html_characters_once(admin_client, path):
    page = _page(admin_client, path)
    assert page.count('function escText(') == 1
    assert ESC_TEXT in page


@pytest.mark.parametrize('kept', [
    'data-key="\' + encodeURIComponent(res.ingredient_name) + \'"',
    'value="\' + res.qty_owned + \'"',
    "var ingCbId = 'gi-' + goalId + '-' + ing.ingredient_name.replace(/[^a-z0-9]/gi, '-');",
])
def test_the_farming_page_keeps_its_numbers_and_encoded_keys_as_they_are(admin_client, kept):
    assert kept in _page(admin_client, '/farming/')


# Every bare variable a script concatenates after a literal, and why it is safe: a number, a
# fixed string, an id of [a-z0-9-] only, an encodeURIComponent key, markup built from escaped
# parts, or a server string escaped where it is assigned (dotTitle, locSafe). A new variable
# here must be one of these, or be escaped where it is drawn.
SAFE_BARE_VARIABLES = {
    '/farming/': {'barFill', 'barPct', 'cbId', 'circleClass', 'color', 'compCrafted', 'compPct',
                  'compTotal', 'dotStyle', 'dotTitle', 'dots', 'gid', 'goalId', 'ingCbId',
                  'ingId', 'locSafe', 'pct', 'qtyLabel', 'resPct', 'safeKey', 'stateTitle'},
    '/sync/': {'bar', 'cls', 'color', 'details', 'listHtml', 'pct'},
}
BARE_VARIABLE = re.compile(r"'\s*\+\s*([A-Za-z_]\w*)\s*(?=[+;)}\n])")


@pytest.mark.parametrize('path', ['/farming/', '/sync/'])
def test_every_bare_variable_a_page_script_draws_is_a_known_safe_one(admin_client, path):
    scripts = _scripts(_page(admin_client, path))
    assert set(BARE_VARIABLE.findall(scripts)) == SAFE_BARE_VARIABLES[path]


# A toast's html is markup: it must open with a fixed string or an escape, never a bare server
# value (`r.error`, `r.goal.item_name`); a later bare part is caught by the scans above.
TOAST_OPENING_BARE = re.compile(r"M\.toast\(\{html:(?!\s*(?:['\"]|escText\())")


@pytest.mark.parametrize('path', ['/farming/', '/sync/'])
def test_every_toast_a_page_script_shows_opens_with_a_literal_or_an_escape(admin_client, path):
    scripts = _scripts(_page(admin_client, path))
    assert TOAST_OPENING_BARE.findall(scripts) == []


# The collection page draws its toasts with its own `escHtml`: every server value inside a
# toast's html (a field of the answer `r`, or of the failed request's `xhr`) is inside an
# escape, so only a toast's own fixed markup (a `View` link) is drawn as HTML.
ESC_HTML = ("function escHtml(s) {\n"
            "    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')\n"
            "                    .replace(/\"/g, '&quot;');\n"
            "  }")
SERVER_VALUE = re.compile(r"\b(?:r|res|xhr)\b\s*(?:\.|&&|\|\|)")
COLLECTION_TOAST_OPENING_BARE = re.compile(
    r"M\.toast\(\{html:(?!\s*(?:['\"]|escHtml\(|watched \? '))")


def _balanced_from(text, start):
    """The index just past the parenthesis closing the one opened before `start`."""
    depth, i = 1, start
    while depth:
        depth += {'(': 1, ')': -1}.get(text[i], 0)
        i += 1
    return i


def _toast_calls(scripts):
    """The text of each `M.toast({html: ...})` call."""
    return [scripts[m.start():_balanced_from(scripts, m.end())]
            for m in re.finditer(r'M\.toast\(\{html:', scripts)]


def _outside_escapes(call):
    """The call with every `escHtml(...)` and its argument removed."""
    while 'escHtml(' in call:
        start = call.index('escHtml(')
        call = call[:start] + call[_balanced_from(call, start + len('escHtml(')):]
    return call


@pytest.mark.parametrize('category', ['warframe', 'mod', 'arcane'])
def test_every_collection_toast_draws_server_values_only_through_its_escape(admin_client, category):
    page = _page(admin_client, f'/collection/{category}')
    assert page.count('function escHtml(') == 1
    assert ESC_HTML in page
    scripts = _scripts(page)
    calls = _toast_calls(scripts)
    assert len(calls) == 10
    assert [c for c in calls if SERVER_VALUE.search(_outside_escapes(c))] == []
    assert COLLECTION_TOAST_OPENING_BARE.findall(scripts) == []
