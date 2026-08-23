import sqlite3
import time
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS hackathons (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    -- 共有タイマー
    timer_end_at INTEGER,
    timer_label  TEXT NOT NULL DEFAULT '',
    -- 発表モード
    pres_active  INTEGER NOT NULL DEFAULT 0,
    pres_order   TEXT NOT NULL DEFAULT '[]',   -- team_id のJSON配列(ランダム順)
    pres_index   INTEGER NOT NULL DEFAULT 0,
    pres_seconds INTEGER NOT NULL DEFAULT 300,
    pres_end_at  INTEGER,
    -- 配置指示: free=自由 / hall=全体会場へ召集 / teams=チームルームへ送出
    placement    TEXT NOT NULL DEFAULT 'free',
    -- 作品まわり
    works_anonymous INTEGER NOT NULL DEFAULT 1,
    voting_open     INTEGER NOT NULL DEFAULT 0,
    reveal_stage    INTEGER NOT NULL DEFAULT 0   -- 0=未発表 1=3位まで 2=2位まで 3=1位まで
);
CREATE TABLE IF NOT EXISTS works (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    hackathon_id INTEGER NOT NULL,
    team_id      INTEGER NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    UNIQUE(hackathon_id, team_id)
);
CREATE TABLE IF NOT EXISTS votes (
    hackathon_id INTEGER NOT NULL,
    voter_email  TEXT NOT NULL,
    work_id      INTEGER NOT NULL,
    created_at   INTEGER NOT NULL,
    UNIQUE(hackathon_id, voter_email)
);
CREATE TABLE IF NOT EXISTS teams (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    hackathon_id INTEGER NOT NULL,
    name         TEXT NOT NULL,
    room_name    TEXT NOT NULL UNIQUE
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
    name         TEXT PRIMARY KEY,   -- LiveKit ルーム名 (URL安全なslug)
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    auto_record  INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER NOT NULL,
    hackathon_id INTEGER,
    team_id      INTEGER,
    kind         TEXT NOT NULL DEFAULT 'free'   -- free / team / hall / play
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

PARTICIPANTS_V2 = """
CREATE TABLE participants_v2 (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    hackathon_id  INTEGER NOT NULL,
    email         TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    team_id       INTEGER,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    last_login_at INTEGER,
    UNIQUE(hackathon_id, email)
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


def active_hackathon(conn) -> sqlite3.Row:
    return conn.execute("SELECT * FROM hackathons WHERE active = 1 LIMIT 1").fetchone()


def init_db():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript(SCHEMA)

        # 最低1つのハッカソンを保証
        if not db.execute("SELECT 1 FROM hackathons").fetchone():
            db.execute(
                "INSERT INTO hackathons(name, active, created_at) VALUES(?, 1, ?)",
                ("ハッカソン #1", now()),
            )
        if not db.execute("SELECT 1 FROM hackathons WHERE active = 1").fetchone():
            db.execute("UPDATE hackathons SET active = 1 WHERE id = (SELECT MAX(id) FROM hackathons)")
        hid = active_hackathon(db)["id"]

        # participants を v1 (email PK) から v2 (ハッカソン単位) へ移行
        cols = {r["name"] for r in db.execute("PRAGMA table_info(participants)")}
        if cols and "hackathon_id" not in cols:
            db.executescript(PARTICIPANTS_V2)
            db.execute(
                """INSERT INTO participants_v2(hackathon_id, email, name, is_admin, created_at, last_login_at)
                   SELECT ?, email, name, is_admin, created_at, last_login_at FROM participants""",
                (hid,),
            )
            db.execute("DROP TABLE participants")
            db.execute("ALTER TABLE participants_v2 RENAME TO participants")
        elif not cols:
            db.executescript(PARTICIPANTS_V2.replace("participants_v2", "participants"))

        # rooms への列追加(旧DB)
        rcols = {r["name"] for r in db.execute("PRAGMA table_info(rooms)")}
        if "hackathon_id" not in rcols:
            db.execute("ALTER TABLE rooms ADD COLUMN hackathon_id INTEGER")
            db.execute("ALTER TABLE rooms ADD COLUMN team_id INTEGER")
        if "kind" not in rcols:
            db.execute("ALTER TABLE rooms ADD COLUMN kind TEXT NOT NULL DEFAULT 'free'")
        db.execute("UPDATE rooms SET hackathon_id = ? WHERE hackathon_id IS NULL", (hid,))

        ensure_hall(db, hid)
        seed_admins(db, hid)


def ensure_hall(db, hackathon_id: int):
    """ハッカソンの全体会場ルームを保証する"""
    name = f"hall-{hackathon_id}"
    db.execute(
        """INSERT OR IGNORE INTO rooms(name, title, description, auto_record, created_at, hackathon_id, kind)
           VALUES(?, '全体会場', '全員が集まるメインルーム', 0, ?, ?, 'hall')""",
        (name, now(), hackathon_id),
    )
    return name


def seed_admins(db, hackathon_id: int):
    """ADMIN_EMAILS を指定ハッカソンの名簿に管理者として登録する"""
    for email in config.ADMIN_EMAILS:
        db.execute(
            """INSERT INTO participants(hackathon_id, email, name, is_admin, created_at)
               VALUES(?, ?, ?, 1, ?)
               ON CONFLICT(hackathon_id, email) DO UPDATE SET is_admin = 1""",
            (hackathon_id, email, email.split("@")[0], now()),
        )


def cleanup_expired():
    with get_db() as db:
        db.execute("DELETE FROM login_tokens WHERE expires_at < ? OR used = 1", (now() - 3600,))
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))
