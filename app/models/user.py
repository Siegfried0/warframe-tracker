from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .db import INTEGRITY_ERRORS, get_db


class User(UserMixin):
    def __init__(self, id, username, email, role, auth_type, is_active, last_login, created_at=None):
        self.id = id
        self.username = username
        self.email = email
        self.role = role
        self.auth_type = auth_type
        self._is_active = bool(is_active)
        self.last_login = last_login
        self.created_at = created_at

    # Flask-Login requires is_active as a property
    @property
    def is_active(self):
        return self._is_active

    @property
    def is_admin(self):
        return self.role == 'admin'

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    @classmethod
    def get_by_id(cls, user_id):
        row = get_db().execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        return cls._from_row(row)

    @classmethod
    def get_by_username(cls, username):
        row = get_db().execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        return cls._from_row(row)

    @classmethod
    def get_oidc_account(cls, sub):
        """The OIDC account of ``sub``: only a row with auth_type 'oidc' matches, never a local
        or EasyAuth row."""
        row = get_db().execute(
            "SELECT * FROM users WHERE auth_type = 'oidc' AND oidc_sub = ?", (sub,)
        ).fetchone()
        return cls._from_row(row)

    @classmethod
    def get_easyauth_account(cls, principal_id):
        """The EasyAuth account of ``principal_id``: only a row with auth_type 'easyauth'
        matches, never a local or OIDC row holding the same value."""
        row = get_db().execute(
            "SELECT * FROM users WHERE auth_type = 'easyauth' AND oidc_sub = ?", (principal_id,)
        ).fetchone()
        return cls._from_row(row)

    @classmethod
    def oidc_sub_held(cls, sub, other_than: str) -> bool:
        """Whether a row whose auth_type is not ``other_than`` holds ``sub`` in the unique
        oidc_sub column (so no ``other_than`` row can take it)."""
        row = get_db().execute(
            'SELECT 1 FROM users WHERE auth_type <> ? AND oidc_sub = ?', (other_than, sub)
        ).fetchone()
        return row is not None

    @classmethod
    def email_taken(cls, email) -> bool:
        row = get_db().execute('SELECT 1 FROM users WHERE email = ?', (email,)).fetchone()
        return row is not None

    @classmethod
    def get_all(cls):
        rows = get_db().execute('SELECT * FROM users ORDER BY username').fetchall()
        return [cls._from_row(r) for r in rows]

    @classmethod
    def count(cls):
        return get_db().execute('SELECT COUNT(*) FROM users').fetchone()[0]

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    @classmethod
    def create(cls, username, password=None, email=None, role='user',
               auth_type='local', oidc_sub=None):
        db = get_db()
        pw_hash = generate_password_hash(password) if password else None
        cur = db.execute(
            '''INSERT INTO users (username, email, password_hash, role, auth_type, oidc_sub)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (username, email, pw_hash, role, auth_type, oidc_sub),
        )
        db.commit()
        return cls.get_by_id(cur.lastrowid)

    @classmethod
    def create_oidc_account(cls, username, email, sub):
        """Insert an OIDC account (no password), admin only on an empty table
        (``_insert_external``). Raises the backend's integrity error when the username, email
        or sub is taken."""
        cls._insert_external(username, email, 'oidc', sub)

    @classmethod
    def create_easyauth_account(cls, username, principal_id):
        """Insert an EasyAuth account (no password, no email), admin only on an empty table
        (``_insert_external``). Raises the backend's integrity error when the username or the
        principal id is taken."""
        cls._insert_external(username, None, 'easyauth', principal_id)

    @classmethod
    def _insert_external(cls, username, email, auth_type, sub):
        """Insert an account signed in elsewhere. Its role is decided in the same statement:
        'admin' when the table is empty, else 'user', never by a count read before it. On
        SQLite one statement holds the write lock throughout, so two first sign-ins at once
        make at most one admin; on MSSQL under READ COMMITTED both could read an empty
        table."""
        db = get_db()
        db.execute(
            '''INSERT INTO users (username, email, password_hash, role, auth_type, oidc_sub)
               SELECT ?, ?, NULL,
                      CASE WHEN EXISTS (SELECT 1 FROM users) THEN 'user' ELSE 'admin' END,
                      ?, ?''',
            (username, email, auth_type, sub),
        )
        db.commit()

    @classmethod
    def create_first_admin(cls, username, password, email=None):
        """Insert the local admin only while no user exists, decided inside the insert: the
        new admin, or None when any row exists by then (a second setup racing the first is
        refused, never made a ``user``). A failure on a unique column is the same case: on
        MSSQL under READ COMMITTED two setups can both read an empty table, and the second
        insert then fails on the username or email the first one holds; it is rolled back
        and None returned, nothing written."""
        db = get_db()
        try:
            cur = db.execute(
                '''INSERT INTO users (username, email, password_hash, role, auth_type, oidc_sub)
                   SELECT ?, ?, ?, 'admin', 'local', NULL
                   WHERE NOT EXISTS (SELECT 1 FROM users)''',
                (username, email, generate_password_hash(password)),
            )
        except INTEGRITY_ERRORS:
            db.rollback()
            return None
        inserted = cur.rowcount == 1
        db.commit()
        return cls.get_by_username(username) if inserted else None

    def check_password(self, password: str) -> bool:
        row = get_db().execute('SELECT password_hash FROM users WHERE id = ?', (self.id,)).fetchone()
        if not row or not row['password_hash']:
            return False
        return check_password_hash(row['password_hash'], password)

    def set_password(self, password: str):
        db = get_db()
        db.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                   (generate_password_hash(password), self.id))
        db.commit()

    def update_last_login(self):
        db = get_db()
        db.execute("UPDATE users SET last_login = datetime('now') WHERE id = ?", (self.id,))
        db.commit()

    def set_active(self, active: bool):
        db = get_db()
        db.execute('UPDATE users SET is_active = ? WHERE id = ?', (int(active), self.id))
        db.commit()
        self._is_active = active

    def set_role(self, role: str):
        db = get_db()
        db.execute('UPDATE users SET role = ? WHERE id = ?', (role, self.id))
        db.commit()
        self.role = role

    def delete(self):
        db = get_db()
        db.execute('DELETE FROM users WHERE id = ?', (self.id,))
        db.commit()

    # ------------------------------------------------------------------

    @classmethod
    def _from_row(cls, row):
        if row is None:
            return None
        return cls(
            id=row['id'],
            username=row['username'],
            email=row['email'],
            role=row['role'],
            auth_type=row['auth_type'],
            is_active=row['is_active'],
            last_login=row['last_login'],
            created_at=row['created_at'],
        )
