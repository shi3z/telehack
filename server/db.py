import sqlite3
import time
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    email      TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    is_admin   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS login_tokens (
    token      TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    sid        TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
    name        TEXT PRIMARY KEY,   -- LiveKit ルーム名 (URL安全なslug)
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    auto_record INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS recordings (
    egress_id  TEXT PRIMARY KEY,
    room_name  TEXT NOT NULL,
    filename   TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'recording',  -- recording / done / failed
    started_at INTEGER NOT NULL,
    ended_at   INTEGER
);
"""


def now() -> int:
    return int(time.time())


@contextmanager
def get_db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript(SCHEMA)
        # 追加カラムのマイグレーション
        cols = {r["name"] for r in db.execute("PRAGMA table_info(participants)")}
        if "last_login_at" not in cols:
            db.execute("ALTER TABLE participants ADD COLUMN last_login_at INTEGER")
        for email in config.ADMIN_EMAILS:
            db.execute(
                """INSERT INTO participants(email, name, is_admin, created_at)
                   VALUES(?, ?, 1, ?)
                   ON CONFLICT(email) DO UPDATE SET is_admin = 1""",
                (email, email.split("@")[0], now()),
            )


def cleanup_expired():
    with get_db() as db:
        db.execute("DELETE FROM login_tokens WHERE expires_at < ? OR used = 1", (now() - 3600,))
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))
