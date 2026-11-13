"""The buy-or-farm farming time of drop rows as the drop sync writes them, not as a test
writes them by hand.

The WFCD item files are mocked at ``_fetch_wfcd`` (a minimal copy of their shape: a
component's drop names its reward in ``type`` and its place in ``location``, the game mode
in brackets); no test reaches the network.
"""
import copy

import pytest

from models.db import get_db
from models.item import Item
from services import wiki_sync, wiki_sync_requests
from services.buy_vs_farm import expected_hours, item_sources


def _sync_one_part(app, monkeypatch, location, chance):
    wfcd = {'Warframes.json': [{
        'name': 'Rhino', 'masterable': True,
        'components': [{'name': 'Chassis', 'drops': [
            {'location': location, 'type': 'Rhino Chassis Blueprint', 'chance': chance}]}],
    }]}
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wfcd',
                        lambda name: copy.deepcopy(wfcd.get(name, [])))
    with app.app_context():
        Item.upsert('Rhino', 'warframe', source='standard', tradable=True)
        db = get_db()
        wiki_sync._sync_wfcd_component_drops(db)
        db.commit()
        item_id = db.execute("SELECT id FROM items WHERE name = 'Rhino'").fetchone()[0]
        row = db.execute("SELECT avg_runs FROM drop_sources WHERE item_id = ? "
                         "AND source_type = 'component'", (item_id,)).fetchone()
        return row['avg_runs'], expected_hours(item_sources(item_id))


# (mission named in the synced row's location, its minutes in the documented table)
MISSION_ROWS = [
    ('Venus/Fossa (Assassination)', 6),
    ('Ceres/Draco (Capture)', 3),
    ('Earth/E Prime (Exterminate)', 4),
    ('Mars/War (Spy)', 6),
]


@pytest.mark.parametrize('location, minutes', MISSION_ROWS, ids=[r[0] for r in MISSION_ROWS])
def test_a_synced_part_drop_counts_the_minutes_of_its_mission(app, monkeypatch,
                                                              location, minutes):
    runs, hours = _sync_one_part(app, monkeypatch, location, 0.3874)
    assert hours == pytest.approx(runs * minutes / 60)

