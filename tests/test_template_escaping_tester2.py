"""Proof tests: toasts take HTML, so a synced name drawn into one must be escaped."""
import re


def _scripts(admin_client):
    page = admin_client.get('/farming/').get_data(as_text=True)
    return '\n'.join(re.findall(r'<script[^>]*>(.*?)</script>', page, re.S))


def test_the_advisor_toast_escapes_the_location(admin_client):
    s = _scripts(admin_client)
    assert "M.toast({html: 'Advisor set to: ' + nodeLoc})" not in s
    assert "escText(nodeLoc)" in s


def test_the_goal_added_toasts_escape_the_item_name(admin_client):
    s = _scripts(admin_client)
    assert "M.toast({html: r.goal.item_name +" not in s
    assert s.count("escText(r.goal.item_name)") >= 2
