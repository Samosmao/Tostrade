"""Account storage: SQLite for local demos; PostgreSQL for Render/Supabase.

No connection string is ever sent to the client. The application connects
server-to-server and lets the driver close and commit each short transaction.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


class DuplicateUser(Exception):
    pass


def is_postgres(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://"))


@contextmanager
def connection(db_path: Path, database_url: str = ""):
    if database_url:
        if not is_postgres(database_url):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection URL")
        # Supabase shared-pooler connections should be encrypted. An explicit
        # sslmode=disable is useful for a local test Postgres only.
        from psycopg import connect
        from psycopg.rows import dict_row
        params = dict(parse_qsl(urlsplit(database_url).query))
        kwargs = {"connect_timeout": 10, "row_factory": dict_row}
        if "sslmode" not in params:
            kwargs["sslmode"] = "require"
        conn = connect(database_url, **kwargs)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_users(db_path: Path, database_url: str = "") -> None:
    with connection(db_path, database_url) as conn:
        if database_url:
            # A non-exposed schema and default-deny RLS protect password hashes
            # if the project also has Supabase's public Data API enabled. The
            # owner/server connection bypasses RLS; no end-user policies exist.
            conn.execute("CREATE SCHEMA IF NOT EXISTS xau_private")
            conn.execute("REVOKE ALL ON SCHEMA xau_private FROM PUBLIC")
            conn.execute("""CREATE TABLE IF NOT EXISTS xau_private.users (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("REVOKE ALL ON xau_private.users FROM PUBLIC")
            conn.execute("ALTER TABLE xau_private.users ENABLE ROW LEVEL SECURITY")
        else:
            # Local SQLite is only a development fallback. The database file
            # must never be used for persistent accounts on Render Free.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")


def find_by_id(db_path: Path, database_url: str, user_id: int):
    query = ("SELECT id, name, email FROM xau_private.users WHERE id = %s" if database_url
             else "SELECT id, name, email FROM users WHERE id = ?")
    with connection(db_path, database_url) as conn:
        return conn.execute(query, (user_id,)).fetchone()


def find_by_email(db_path: Path, database_url: str, email: str):
    query = ("SELECT id, name, email, password_hash FROM xau_private.users WHERE email = %s" if database_url
             else "SELECT id, name, email, password_hash FROM users WHERE email = ?")
    with connection(db_path, database_url) as conn:
        return conn.execute(query, (email,)).fetchone()


def create_user(db_path: Path, database_url: str, name: str, email: str, password_hash: str) -> int:
    try:
        with connection(db_path, database_url) as conn:
            if database_url:
                row = conn.execute(
                    "INSERT INTO xau_private.users (name, email, password_hash) VALUES (%s, %s, %s) RETURNING id",
                    (name, email, password_hash),
                ).fetchone()
                return row["id"]
            cursor = conn.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                (name, email, password_hash),
            )
            return cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        raise DuplicateUser from exc
    except Exception as exc:
        if database_url:
            from psycopg.errors import UniqueViolation
            if isinstance(exc, UniqueViolation):
                raise DuplicateUser from exc
        raise
