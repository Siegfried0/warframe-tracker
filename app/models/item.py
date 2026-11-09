from .db import get_db

CATEGORIES = [
    'warframe', 'primary', 'secondary', 'melee',
    'companion', 'archwing', 'arch-gun', 'arch-melee', 'k-drive', 'amp',
    'mod', 'arcane', 'resource',
]

# Categories shown in the Collection browser (resources live in the Farming Planner only)
COLLECTION_CATEGORIES = [c for c in CATEGORIES if c != 'resource']

CATEGORY_LABELS = {
    'warframe':  'Warframes',
    'primary':   'Primary Weapons',
    'secondary': 'Secondary Weapons',
    'melee':     'Melee Weapons',
    'companion': 'Companions',
    'archwing':  'Archwings',
    'arch-gun':  'Arch-Guns',
    'arch-melee':'Arch-Melee',
    'k-drive':   'K-Drives / MOAs / Necramechs',
    'amp':       'AMPs',
    'mod':       'Mods',
    'arcane':    'Arcanes',
    'resource':  'Resources',
}

# MR points awarded when fully levelling (mastering) one item in each category.
# Mods and Arcanes contribute 0 MR — tracked for build completeness only.
MR_POINTS_PER_CATEGORY = {
    'warframe':  6000,
    'primary':   3000,
    'secondary': 3000,
    'melee':     3000,
    'companion': 6000,   # Sentinels, Kubrows, Kavats, MOAs, Hounds — 200 pts/rank × 30
    'archwing':  6000,
    'arch-gun':  3000,   # Archwing weapons — 100 pts/rank × 30
    'arch-melee':3000,   # Archwing weapons — 100 pts/rank × 30
    'k-drive':   6000,   # K-Drives and Necramechs — 200 pts/rank × 30
    'amp':       1500,   # Operator amps — 50 pts/rank × 30 (source: wiki.warframe.com/w/Mastery_Rank)
    'mod':       0,
    'arcane':    0,
    'resource':  0,
}

# Cumulative MR points required to reach each rank (index = rank number).
# MR 0–30: threshold(n) = 2500 * n²   (source: wiki.warframe.com/w/Mastery_Rank)
# L1–L30 : 2 250 000 + 147 500 × legendary_rank
_MR30_THRESHOLD = 2500 * 30 * 30    # 2 250 000
_LEGENDARY_GAP  = 147_500
MR_RANK_THRESHOLDS = (
    [2500 * n * n for n in range(31)] +
    [_MR30_THRESHOLD + i * _LEGENDARY_GAP for i in range(1, 31)]
)
# Index 0–30  → MR 0 … MR 30
# Index 31–60 → L1 … L30


def rank_label(rank: int) -> str:
    """Return the display label for a numeric rank index (e.g. 18 → 'MR 18', 31 → 'L1')."""
    if rank <= 30:
        return f'MR {rank}'
    return f'L{rank - 30}'


def rank_for_points(total_mr: int) -> int:
    """The rank index reached with `total_mr` points: the highest index whose threshold it meets."""
    rank = 0
    for i, threshold in enumerate(MR_RANK_THRESHOLDS):
        if total_mr >= threshold:
            rank = i
    return rank


def on_market(item) -> bool:
    """Whether warframe.market trades the item: it is tradable or it has a market slug.

    Items the market sells only as a set (prime warframes) are not tradable in the item
    data, yet the market lists them; an empty slug counts as none.
    """
    return bool(item['tradable']) or bool(item['market_url_name'])


class Item:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def to_dict(self):
        return self.__dict__.copy()

    @classmethod
    def _from_row(cls, row):
        return cls(**dict(row)) if row else None

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    @classmethod
    def get_by_id(cls, item_id):
        row = get_db().execute('SELECT * FROM items WHERE id = ?', (item_id,)).fetchone()
        return cls._from_row(row)

    @classmethod
    def get_all(cls, category=None):
        db = get_db()
        if category:
            rows = db.execute(
                'SELECT * FROM items WHERE category = ? ORDER BY name', (category,)
            ).fetchall()
        else:
            rows = db.execute('SELECT * FROM items ORDER BY category, name').fetchall()
        return [cls._from_row(r) for r in rows]

    @classmethod
    def search(cls, query: str, category=None):
        db = get_db()
        q = f'%{query}%'
        if category:
            rows = db.execute(
                'SELECT * FROM items WHERE category = ? AND name LIKE ? ORDER BY name',
                (category, q),
            ).fetchall()
        else:
            rows = db.execute(
                'SELECT * FROM items WHERE name LIKE ? ORDER BY category, name', (q,)
            ).fetchall()
        return [cls._from_row(r) for r in rows]

    @classmethod
    def count_by_category(cls) -> dict:
        rows = get_db().execute(
            'SELECT category, COUNT(*) AS cnt FROM items GROUP BY category'
        ).fetchall()
        return {r['category']: r['cnt'] for r in rows}

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    @classmethod
    def upsert(cls, name: str, category: str, source=None, wiki_url=None,
               vaulted=False,
               tradable=True, image_url=None,
               grants_mastery=True, mod_type=None, mod_polarity=None,
               mod_max_rank=None):
        """Insert or update one catalogue row. Two sync-owned columns are not written here:
        a new row starts with `resurgence_available` 0 and `market_url_name` NULL, and an
        update keeps both; only the Prime Resurgence sync step sets or clears the first and
        only the market slug sync step the second."""
        db = get_db()
        db.execute('''
            INSERT INTO items
                (name, category, source, wiki_url, vaulted,
                 tradable, image_url, grants_mastery,
                 mod_type, mod_polarity, mod_max_rank)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name, category) DO UPDATE SET
                source               = excluded.source,
                wiki_url             = excluded.wiki_url,
                vaulted              = excluded.vaulted,
                tradable             = excluded.tradable,
                image_url            = CASE
                                         WHEN items.image_url LIKE '/static/%'
                                         THEN items.image_url
                                         ELSE excluded.image_url
                                       END,
                grants_mastery       = excluded.grants_mastery,
                mod_type             = excluded.mod_type,
                mod_polarity         = excluded.mod_polarity,
                mod_max_rank         = excluded.mod_max_rank,
                updated_at           = datetime('now')
        ''', (name, category, source, wiki_url,
              int(vaulted),
              int(tradable), image_url, int(grants_mastery),
              mod_type, mod_polarity, mod_max_rank))
        db.commit()

    @classmethod
    def delete(cls, item_id: int):
        db = get_db()
        db.execute('DELETE FROM items WHERE id = ?', (item_id,))
        db.commit()
