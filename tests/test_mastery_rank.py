"""MR points per category, the rank thresholds, rank labels and the rank a points total reaches.

The oracle is vault/spec/TECH.md, "MR points per mastered item" and "MR rank thresholds and
Legendary ranks": every expected value here is a literal from those tables or their formulas,
never a value read from the code under test.
"""
import pytest

from models.db import get_db
from models.item import (MR_POINTS_PER_CATEGORY, MR_RANK_THRESHOLDS, Item, rank_for_points,
                         rank_label)
from models.progress import Progress
from models.user import User

# TECH.md, "MR points per mastered item": all 13 categories.
POINTS_TABLE = [
    ('warframe', 6000),
    ('companion', 6000),
    ('archwing', 6000),
    ('k-drive', 6000),
    ('amp', 1500),
    ('primary', 3000),
    ('secondary', 3000),
    ('melee', 3000),
    ('arch-gun', 3000),
    ('arch-melee', 3000),
    ('mod', 0),
    ('arcane', 0),
    ('resource', 0),
]

# TECH.md, the representative threshold table, MR 0 to MR 30.
MR_TABLE = [
    0, 2500, 10000, 22500, 40000, 62500, 90000, 122500, 160000, 202500,
    250000, 302500, 360000, 422500, 490000, 562500, 640000, 722500, 810000, 902500,
    1000000, 1102500, 1210000, 1322500, 1440000, 1562500, 1690000, 1822500, 1960000, 2102500,
    2250000,
]


def expected_threshold(index):
    """TECH.md's formulas: 2 500 × n² for MR 0–30, 2 250 000 + 147 500 × L for L1–L30."""
    if index <= 30:
        return 2500 * index * index
    return 2250000 + 147500 * (index - 30)


@pytest.mark.parametrize('category,points', POINTS_TABLE)
def test_each_category_awards_its_tech_md_points(category, points):
    assert MR_POINTS_PER_CATEGORY[category] == points


def test_the_points_table_has_exactly_the_thirteen_categories():
    assert sorted(MR_POINTS_PER_CATEGORY) == sorted(c for c, _ in POINTS_TABLE)


def test_there_are_61_thresholds_mr0_to_l30():
    assert len(MR_RANK_THRESHOLDS) == 61


@pytest.mark.parametrize('index', range(61))
def test_each_threshold_follows_the_tech_md_formula(index):
    assert MR_RANK_THRESHOLDS[index] == expected_threshold(index)


@pytest.mark.parametrize('index,points', list(enumerate(MR_TABLE)))
def test_each_mr_threshold_matches_the_tech_md_table(index, points):
    assert MR_RANK_THRESHOLDS[index] == points


@pytest.mark.parametrize('index,points', [(31, 2397500), (32, 2545000), (33, 2692500),
                                          (60, 6675000)])
def test_legendary_thresholds_match_the_tech_md_examples(index, points):
    assert MR_RANK_THRESHOLDS[index] == points


@pytest.mark.parametrize('index', range(61))
def test_rank_label_is_mr_n_up_to_30_and_l_above(index):
    expected = f'MR {index}' if index <= 30 else f'L{index - 30}'
    assert rank_label(index) == expected


@pytest.mark.parametrize('index,label', [(0, 'MR 0'), (18, 'MR 18'), (30, 'MR 30'),
                                         (31, 'L1'), (60, 'L30')])
def test_rank_label_examples(index, label):
    assert rank_label(index) == label


@pytest.mark.parametrize('index', range(61))
def test_a_total_exactly_at_a_threshold_reaches_that_rank(index):
    assert rank_for_points(expected_threshold(index)) == index


@pytest.mark.parametrize('index', range(1, 61))
def test_one_point_below_a_threshold_stays_one_rank_lower(index):
    assert rank_for_points(expected_threshold(index) - 1) == index - 1


@pytest.mark.parametrize('points,rank', [(0, 0), (6675000, 60), (6675001, 60),
                                         (1000000000, 60)])
def test_zero_and_beyond_l30(points, rank):
    assert rank_for_points(points) == rank


# ---------------------------------------------------------------------------
# Through Progress.get_mastery_summary on the test database
# ---------------------------------------------------------------------------

def _user_and_items(items):
    """A user and the given (category, count, grants_mastery) items, all mastered."""
    user = User.create('summary-user', password='pw-summary-1')
    db = get_db()
    for category, count, grants in items:
        for n in range(count):
            Item.upsert(f'{category} {n}', category, grants_mastery=grants)
        db.execute(
            'INSERT INTO user_progress (user_id, item_id, mastered) '
            'SELECT ?, id, 1 FROM items WHERE category = ?',
            (user.id, category),
        )
    db.commit()
    return user.id


def test_summary_with_nothing_mastered_is_mr0_heading_for_mr1(app):
    with app.app_context():
        user = User.create('empty-user', password='pw-empty-1')
        summary = Progress.get_mastery_summary(user.id)
    assert summary['total_mr'] == 0
    assert summary['mr_rank'] == 0
    assert summary['mr_rank_label'] == 'MR 0'
    assert summary['next_rank'] == 1
    assert summary['next_rank_label'] == 'MR 1'
    assert summary['pts_for_next'] == 2500


def test_summary_counts_points_per_category_and_ignores_mods(app):
    with app.app_context():
        user_id = _user_and_items([('warframe', 2, True), ('amp', 1, True),
                                   ('mod', 3, False)])
        summary = Progress.get_mastery_summary(user_id)
    assert summary['total_mr'] == 13500
    assert summary['by_category'] == {'warframe': 2, 'amp': 1}
    assert summary['mr_rank'] == 2
    assert summary['next_rank'] == 3
    assert summary['pts_in_rank'] == 3500
    assert summary['pts_for_next'] == 12500
    assert summary['pct_to_next'] == 28.0


def test_summary_beyond_l30_is_max_rank_with_no_next_rank(app):
    with app.app_context():
        user_id = _user_and_items([('warframe', 1113, True)])
        summary = Progress.get_mastery_summary(user_id)
    assert summary['total_mr'] == 6678000
    assert summary['mr_rank'] == 60
    assert summary['mr_rank_label'] == 'L30'
    assert summary['next_rank'] is None
    assert summary['next_rank_label'] is None
    assert summary['pts_in_rank'] == 3000
    assert summary['pts_for_next'] == 0
    assert summary['pct_to_next'] == 100.0
