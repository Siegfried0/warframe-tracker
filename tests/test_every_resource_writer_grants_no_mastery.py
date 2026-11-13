"""Every writer of a resource row leaves it out of the mastery counts.

Oracle: the game's rule that a resource is no mastery item, whichever sync wrote the row.
Three code paths write ``category='resource'`` rows: the resource sync, the component
ingredient sync (an ingredient name not in the resource list gets a resource row of its
own) and the stale-component cleanup (a stale part with progress is moved to resource).
After a full sync, in the order the sync runs them, no resource row counts in the
dashboard's "N / M items mastered" or in "level these now". The sync's sources are
replaced by fixed data (no network).
"""
import pytest

from models.db import get_db
from models.item import Item
from models.progress import Progress
from models.user import User
from services import wiki_sync_catalogue, wiki_sync_ingredients, wiki_sync_mods, \
    wiki_sync_requests

WARFRAMES_JSON = [{
    'name': 'Excalibur', 'masterable': True,
    'components': [{'name': 'Chassis',
                    'components': [{'name': 'Ferrite', 'itemCount': 100},
                                   {'name': 'Kuva', 'itemCount': 50}]}],
}]


def _fixed_sources(monkeypatch):
    files = {'Resources.json': [{'name': 'Ferrite'}], 'Warframes.json': WARFRAMES_JSON}
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wfcd',
                        lambda name: [dict(i) for i in files.get(name, [])])
    monkeypatch.setattr(wiki_sync_ingredients,
                        '_sync_warframe_component_ingredients_from_wiki', lambda db: 0)


def _ingredient_row(uid):
    """The ingredient sync's own resource row ('Kuva', not in this fixed resource list)."""
    wiki_sync_mods._sync_resources()
    wiki_sync_ingredients._sync_component_ingredients({'complete': True})
    return get_db().execute(
        "SELECT id FROM items WHERE name = 'Kuva' AND category = 'resource'").fetchone()[0]


def _cleanup_row(uid):
    """A stale Zaw part the user mastered, moved to resource by the cleanup."""
    Item.upsert('Old Strike', 'melee', source='zaw')
    item_id = get_db().execute(
        "SELECT id FROM items WHERE name = 'Old Strike'").fetchone()[0]
    get_db().execute('INSERT INTO user_progress (user_id, item_id, in_possession, mastered)'
                     ' VALUES (?, ?, 1, 1)', (uid, item_id))
    get_db().commit()
    wiki_sync_catalogue._cleanup_source_components('melee', 'zaw', {'Some Zaw'})
    wiki_sync_mods._sync_resources()
    wiki_sync_ingredients._sync_component_ingredients({'complete': True})
    assert get_db().execute('SELECT category FROM items WHERE id = ?',
                            (item_id,)).fetchone()[0] == 'resource'
    return item_id


@pytest.mark.parametrize('writer', [_ingredient_row, _cleanup_row],
                         ids=['ingredient-sync', 'stale-component-cleanup'])
def test_a_resource_row_from_any_writer_is_no_mastery_item(app, monkeypatch, writer):
    _fixed_sources(monkeypatch)
    with app.app_context():
        uid = User.create('writer-user', password='pw-writer-1').id
        Item.upsert('Excalibur', 'warframe', source='standard')
        resource_id = writer(uid)
        flag = get_db().execute('SELECT grants_mastery FROM items WHERE id = ?',
                                (resource_id,)).fetchone()[0]
        summary = Progress.get_mastery_summary(uid)
    assert flag == 0
    assert summary['total_items'] == 1          # Excalibur only
    assert summary['total_mastered'] == 0
