import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# --- アプリ全般 ---
APP_NAME = os.getenv("APP_NAME", "Telehack")
# 参加者に送るリンクの起点 URL(本番では https://example.com など)
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "telehack.db")))
RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", str(BASE_DIR / "recordings")))

# --- 認証 ---
# ワンタイムURLの有効期限(分)とセッション寿命(時間)
LOGIN_TOKEN_TTL_MIN = int(os.getenv("LOGIN_TOKEN_TTL_MIN", "15"))
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "24"))
# 起動時に名簿へ管理者として登録するメールアドレス(カンマ区切り)
ADMIN_EMAILS = [e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()]

# --- LiveKit ---
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "http://localhost:7880")          # サーバー側 API 用
LIVEKIT_WS_URL = os.getenv("LIVEKIT_WS_URL", "ws://localhost:7880")      # ブラウザ接続用
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
# egress コンテナ内の出力先(docker-compose で ./recordings にマウント)
EGRESS_OUT_DIR = os.getenv("EGRESS_OUT_DIR", "/out")

# --- メール ---
# SMTP_HOST が未設定なら DEV モード: リンクをサーバーログに出力する
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_STARTTLS = _bool("SMTP_STARTTLS", "true")
MAIL_FROM = os.getenv("MAIL_FROM", "noreply@example.com")
