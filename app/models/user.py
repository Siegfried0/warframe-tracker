from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db


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
    def get_by_oidc_sub(cls, sub):
        row = get_db().execute('SELECT * FROM users WHERE oidc_sub = ?', (sub,)).fetchone()
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
        """Insert an OIDC account (no password). Its role is decided in the same statement:
        'admin' when the table is empty, else 'user'. On SQLite one statement holds the write
        lock throughout, so two first sign-ins at once make at most one admin; on MSSQL under
        READ COMMITTED both could read an empty table. Raises the backend's integrity error
        when the username, email or sub is taken."""
        db = get_db()
        db.execute(
            '''INSERT INTO users (username, email, password_hash, role, auth_type, oidc_sub)
               SELECT ?, ?, NULL,
                      CASE WHEN EXISTS (SELECT 1 FROM users) THEN 'user' ELSE 'admin' END,
                      'oidc', ?''',
            (username, email, sub),
        )
        db.commit()

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
