from .db import get_db
from .item import MR_POINTS_PER_CATEGORY, MR_RANK_THRESHOLDS, rank_label


class Progress:

    @classmethod
    def get_for_user(cls, user_id: int, item_id: int):
        row = get_db().execute(
            'SELECT * FROM user_progress WHERE user_id = ? AND item_id = ?',
            (user_id, item_id),
        ).fetchone()
        return dict(row) if row else None

    @classmethod
    def get_all_for_user(cls, user_id: int, category=None) -> list[dict]:
        db = get_db()
        # Include MR-track items (grants_mastery=1) plus the non-MR mod/arcane collections.
        # This intentionally excludes K-Drive component parts (grants_mastery=0, category='k-drive').
        base = '''
            SELECT i.*,
                   COALESCE(p.in_possession,    0) AS in_possession,
                   COALESCE(p.mastered,         0) AS mastered,
                   COALESCE(p.subsumed,         0) AS subsumed,
                   COALESCE(p.resurgence_watch, 0) AS resurgence_watch,
                   p.owned_rank,
                   p.target_rank,
                   p.note,
                   p.updated_at AS progress_updated
            FROM items i
            LEFT JOIN user_progress p ON p.item_id = i.id AND p.user_id = ?
            WHERE (i.grants_mastery = 1 OR i.category IN ('mod', 'arcane'))
        '''
        if category:
            rows = db.execute(base + 'AND i.category = ? ORDER BY i.name',
                              (user_id, category)).fetchall()
        else:
            rows = db.execute(base + 'ORDER BY i.category, i.name',
                              (user_id,)).fetchall()
        return [dict(r) for r in rows]

    @classmethod
    def upsert(cls, user_id: int, item_id: int, **fields):
        db = get_db()
        existing = cls.get_for_user(user_id, item_id)
        if existing:
            set_parts = [f'{k} = ?' for k in fields] + ["updated_at = datetime('now')"]
            db.execute(
                f'UPDATE user_progress SET {", ".join(set_parts)} WHERE user_id = ? AND item_id = ?',
                list(fields.values()) + [user_id, item_id],
            )
        else:
            cols = ['user_id', 'item_id'] + list(fields.keys())
            placeholders = ', '.join('?' for _ in cols)
            db.execute(
                f'INSERT INTO user_progress ({", ".join(cols)}) VALUES ({placeholders})',
                [user_id, item_id] + list(fields.values()),
            )
        db.commit()

    @classmethod
    def get_mastery_summary(cls, user_id: int) -> dict:
        db = get_db()
        rows = db.execute('''
            SELECT i.category, COUNT(*) AS cnt
            FROM user_progress p
            JOIN items i ON i.id = p.item_id
            WHERE p.user_id = ? AND p.mastered = 1 AND i.grants_mastery = 1
            GROUP BY i.category
        ''', (user_id,)).fetchall()

        by_cat = {r['category']: r['cnt'] for r in rows}
        total_mr = sum(MR_POINTS_PER_CATEGORY.get(cat, 0) * cnt for cat, cnt in by_cat.items())
        total_mastered = sum(by_cat.values())
        total_items = db.execute('SELECT COUNT(*) FROM items').fetchone()[0]

        # Find current MR rank (highest index where total_mr meets the threshold)
        mr_rank = 0
        for i, threshold in enumerate(MR_RANK_THRESHOLDS):
            if total_mr >= threshold:
                mr_rank = i

        max_rank = len(MR_RANK_THRESHOLDS) - 1
        if mr_rank < max_rank:
            pts_at_rank = MR_RANK_THRESHOLDS[mr_rank]
            pts_at_next = MR_RANK_THRESHOLDS[mr_rank + 1]
            pts_in_rank = total_mr - pts_at_rank
            pts_for_next = pts_at_next - pts_at_rank
            next_rank = mr_rank + 1
            pct_to_next = round(pts_in_rank / pts_for_next * 100, 1)
        else:
            pts_in_rank = total_mr - MR_RANK_THRESHOLDS[mr_rank]
            pts_for_next = 0
            next_rank = None
            pct_to_next = 100.0

        return {
            'total_mr': total_mr,
            'total_mastered': total_mastered,
            'total_items': total_items,
            'by_category': by_cat,
            'mr_rank': mr_rank,
            'mr_rank_label': rank_label(mr_rank),
            'next_rank': next_rank,
            'next_rank_label': rank_label(next_rank) if next_rank is not None else None,
            'pts_in_rank': pts_in_rank,
            'pts_for_next': pts_for_next,
            'pct_to_next': pct_to_next,
            'rank_thresholds': MR_RANK_THRESHOLDS,
        }

    @classmethod
    def get_immediate_gains(cls, user_id: int) -> list[dict]:
        """MR-relevant items owned but not yet mastered — level these now."""
        rows = get_db().execute('''
            SELECT i.*, p.in_possession, p.mastered, p.note
            FROM items i
            JOIN user_progress p ON p.item_id = i.id AND p.user_id = ?
            WHERE p.in_possession = 1 AND p.mastered = 0
              AND i.grants_mastery = 1
            ORDER BY i.category, i.name
        ''', (user_id,)).fetchall()
        return [dict(r) for r in rows]

    @classmethod
    def get_unowned_items(cls, user_id: int, category=None) -> list[dict]:
        """Items not in possession and not mastered — farming targets."""
        db = get_db()
        base = '''
            SELECT i.*,
                   COALESCE(p.in_possession,    0) AS in_possession,
                   COALESCE(p.mastered,         0) AS mastered,
                   COALESCE(p.resurgence_watch, 0) AS resurgence_watch
            FROM items i
            LEFT JOIN user_progress p ON p.item_id = i.id AND p.user_id = ?
            WHERE COALESCE(p.in_possession, 0) = 0
              AND COALESCE(p.mastered,      0) = 0
        '''
        if category:
            rows = db.execute(base + 'AND i.category = ? ORDER BY i.name',
                              (user_id, category)).fetchall()
        else:
            rows = db.execute(base + 'ORDER BY i.category, i.name',
                              (user_id,)).fetchall()
        return [dict(r) for r in rows]

    @classmethod
    def get_paged_for_user(cls, user_id: int, category: str,
                            filters: dict = None,
                            query: str = '',
                            page: int = 1,
                            per_page: int = 48) -> tuple[list[dict], int]:
        """Return one page of filtered items plus the total filtered count."""
        if filters is None:
            filters = {}
        db = get_db()
        is_arcane = (category == 'arcane')

        sql = '''
            SELECT i.*,
                   COALESCE(p.in_possession,    0) AS in_possession,
                   COALESCE(p.mastered,         0) AS mastered,
                   COALESCE(p.subsumed,         0) AS subsumed,
                   COALESCE(p.resurgence_watch, 0) AS resurgence_watch,
                   p.owned_rank,
                   p.target_rank,
                   p.note,
                   p.updated_at AS progress_updated
            FROM items i
            LEFT JOIN user_progress p ON p.item_id = i.id AND p.user_id = ?
            WHERE (i.grants_mastery = 1 OR i.category IN ('mod', 'arcane'))
              AND i.category = ?
        '''
        params: list = [user_id, category]

        if query:
            sql += ' AND LOWER(i.name) LIKE ?'
            params.append('%' + query.lower() + '%')

        owned_val = filters.get('owned', 'all')
        if owned_val != 'all':
            if is_arcane:
                sql += (' AND COALESCE(p.owned_rank, 0) > 0'
                        if owned_val == '1'
                        else ' AND COALESCE(p.owned_rank, 0) = 0')
            else:
                sql += ' AND COALESCE(p.in_possession, 0) = ?'
                params.append(int(owned_val))

        mastered_val = filters.get('mastered', 'all')
        if mastered_val != 'all':
            if is_arcane:
                if mastered_val == '1':
                    sql += ' AND i.mod_max_rank > 0 AND COALESCE(p.owned_rank, 0) >= i.mod_max_rank'
                else:
                    sql += ' AND NOT (i.mod_max_rank > 0 AND COALESCE(p.owned_rank, 0) >= i.mod_max_rank)'
            else:
                sql += ' AND COALESCE(p.mastered, 0) = ?'
                params.append(int(mastered_val))

        subsumed_val = filters.get('subsumed', 'all')
        if subsumed_val != 'all':
            sql += ' AND COALESCE(p.subsumed, 0) = ?'
            params.append(int(subsumed_val))

        source_val = filters.get('source', 'all')
        if source_val not in ('all', ''):
            if source_val == 'prime':
                sql += " AND i.source = 'prime'"
            elif source_val == 'non-prime':
                sql += " AND i.source != 'prime'"
            else:
                sql += ' AND i.source = ?'
                params.append(source_val)

        vaulted_val = filters.get('vaulted', 'all')
        if vaulted_val != 'all':
            sql += ' AND i.vaulted = ?'
            params.append(int(vaulted_val))

        resurgence_val = filters.get('resurgence', 'all')
        if resurgence_val == 'watched':
            sql += ' AND COALESCE(p.resurgence_watch, 0) = 1'
        elif resurgence_val == 'available':
            sql += ' AND i.resurgence_available = 1'

        modtype_val = filters.get('modtype', 'all')
        if modtype_val not in ('all', ''):
            sql += ' AND i.mod_type = ?'
            params.append(modtype_val)

        polarity_val = filters.get('polarity', 'all')
        if polarity_val not in ('all', ''):
            sql += ' AND i.mod_polarity = ?'
            params.append(polarity_val)

        targetmet_val = filters.get('targetmet', 'all')
        if targetmet_val == 'met':
            sql += (' AND COALESCE(p.target_rank, 0) > 0'
                    ' AND COALESCE(p.owned_rank, 0) >= COALESCE(p.target_rank, 0)')
        elif targetmet_val == 'notmet':
            sql += (' AND NOT (COALESCE(p.target_rank, 0) > 0'
                    ' AND COALESCE(p.owned_rank, 0) >= COALESCE(p.target_rank, 0))')

        count_row = db.execute(
            f'SELECT COUNT(*) FROM ({sql}) AS sub', params
        ).fetchone()
        total = count_row[0] if count_row else 0

        offset = (page - 1) * per_page
        rows = db.execute(
            sql + ' ORDER BY i.name LIMIT ? OFFSET ?',
            params + [per_page, offset],
        ).fetchall()
        return [dict(r) for r in rows], total

    @classmethod
    def get_filter_options(cls, user_id: int, category: str) -> dict:
        """Return distinct filter option values for the filter bar (full category, not just one page)."""
        db = get_db()
        base = '''
            FROM items i
            LEFT JOIN user_progress p ON p.item_id = i.id AND p.user_id = ?
            WHERE (i.grants_mastery = 1 OR i.category IN ('mod', 'arcane'))
              AND i.category = ?
        '''
        args = (user_id, category)

        all_sources = [
            r['source'] for r in db.execute(
                f'SELECT DISTINCT i.source {base} AND i.source IS NOT NULL ORDER BY i.source',
                args,
            ).fetchall()
        ]

        if category == 'arcane':
            return {'sources': [], 'mod_types': [], 'polarities': [],
                    'arc_sources': all_sources}

        variant_sources = sorted(s for s in all_sources if s and s != 'prime')
        mod_types: list = []
        polarities: list = []
        if category == 'mod':
            mod_types = [
                r['mod_type'] for r in db.execute(
                    f'SELECT DISTINCT i.mod_type {base} AND i.mod_type IS NOT NULL ORDER BY i.mod_type',
                    args,
                ).fetchall()
            ]
            polarities = [
                r['mod_polarity'] for r in db.execute(
                    f'SELECT DISTINCT i.mod_polarity {base} AND i.mod_polarity IS NOT NULL ORDER BY i.mod_polarity',
                    args,
                ).fetchall()
            ]

        return {'sources': variant_sources, 'mod_types': mod_types,
                'polarities': polarities, 'arc_sources': []}

    @classmethod
    def clear_all_for_user(cls, user_id: int):
        db = get_db()
        db.execute('DELETE FROM user_progress WHERE user_id = ?', (user_id,))
        db.commit()

    @classmethod
    def get_resurgence_watched_and_available(cls, user_id: int) -> list[dict]:
        rows = get_db().execute('''
            SELECT i.*
            FROM items i
            JOIN user_progress p ON p.item_id = i.id AND p.user_id = ?
            WHERE p.resurgence_watch = 1
              AND i.resurgence_available = 1
              AND COALESCE(p.in_possession, 0) = 0
        ''', (user_id,)).fetchall()
        return [dict(r) for r in rows]
