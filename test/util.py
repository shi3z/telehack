"""テスト共通ユーティリティ: DB直接操作(セッション注入など)"""
import secrets
import sqlite3
import time
from pathlib import Path

BASE = "http://localhost:8800"
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "telehack.db"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def inject_session(email: str, ttl: int = 7200) -> str:
    """メール送信を介さずセッションを直接発行する(テスト用)"""
    sid = secrets.token_urlsafe(32)
    conn = db()
    conn.execute(
        "INSERT INTO sessions(sid, email, expires_at) VALUES(?,?,?)",
        (sid, email, int(time.time()) + ttl),
    )
    conn.commit()
    conn.close()
    return sid


def inject_login_token(email: str) -> str:
    """ワンタイムURLトークンを直接発行する(認証フローのテスト用)"""
    token = secrets.token_urlsafe(32)
    conn = db()
    conn.execute(
        "INSERT INTO login_tokens(token, email, expires_at) VALUES(?,?,?)",
        (token, email, int(time.time()) + 900),
    )
    conn.commit()
    conn.close()
    return token


def active_hackathon_id() -> int:
    conn = db()
    hid = conn.execute("SELECT id FROM hackathons WHERE active = 1").fetchone()[0]
    conn.close()
    return hid


async def wait_for(fn, timeout=20, interval=0.5, desc=""):
    """非同期述語 fn() が真を返すまで待つ"""
    import asyncio
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = await fn()
        if last:
            return last
        await asyncio.sleep(interval)
    raise TimeoutError(f"wait_for timeout: {desc} (last={last})")
