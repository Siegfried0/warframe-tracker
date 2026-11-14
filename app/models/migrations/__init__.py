"""The schema migrations, one module per range of versions (`v01.py`, `v02_v08.py`, ...).

Each migration is a function `_migrate_vN(db, backend)`, with `_migrate_vN_sqlite` and
`_migrate_vN_mssql` where the backends differ. The runner and `CURRENT_SCHEMA_VERSION` stay in
`models/db.py`, which imports each `_migrate_vN` from its module: nothing here imports from
`models/db.py`, and this package re-exports nothing.

A new migration goes into a new module named for its first version (`v12.py`, renamed `v12_vNN.py`
when it grows), is added to the list in `_apply_migrations` and raises `CURRENT_SCHEMA_VERSION`
in `models/db.py`. An applied migration is never edited.
"""
