"""Proof test: a component name drawn into the resource dots' title attribute must be escaped."""
import re


def test_the_resource_dot_title_escapes_the_component_name(admin_client):
    resp = admin_client.get('/farming/')
    page = resp.get_data(as_text=True).replace('\r\n', '\n')
    # dotTitle starts as the bare component name and is then put into title="..."
    assert "var dotTitle  = cname;" not in page
    assert re.search(r"var dotTitle\s*=\s*escText\(cname\)", page)
