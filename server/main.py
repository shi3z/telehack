import asyncio
import csv
import hashlib
import hmac
import io
import logging
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr

import config
import db
import lk
import mailer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("telehack")

STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_COOKIE = "th_session"

webhook_receiver = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global webhook_receiver
    db.init_db()
    config.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    webhook_receiver = lk.make_webhook_receiver()
    db.cleanup_expired()
    yield
    await lk.close_api()


app = FastAPI(title=config.APP_NAME, lifespan=lifespan)


# ---------------------------------------------------------------- 認証まわり

def get_session_user(request: Request) -> dict | None:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    with db.get_db() as conn:
        row = conn.execute(
            """SELECT s.email, p.name, p.is_admin FROM sessions s
               JOIN participants p ON p.email = s.email
               WHERE s.sid = ? AND s.expires_at > ?""",
            (sid, db.now()),
        ).fetchone()
    return dict(row) if row else None


def require_user(request: Request) -> dict:
    user = get_session_user(request)
    if not user:
        raise HTTPException(401, "ログインが必要です")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "管理者権限が必要です")
    return user


class LoginRequest(BaseModel):
    email: EmailStr


async def issue_login_link(email: str, name: str):
    """ワンタイムトークンを発行してログインリンクをメール送信する"""
    token = secrets.token_urlsafe(32)
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO login_tokens(token, email, expires_at) VALUES(?, ?, ?)",
            (token, email, db.now() + config.LOGIN_TOKEN_TTL_MIN * 60),
        )
    url = f"{config.BASE_URL}/auth/{token}"
    # SMTP がブロックしてもリクエストを待たせない
    try:
        await asyncio.to_thread(mailer.send_login_link, email, name or email, url)
    except Exception as e:
        log.error("メール送信失敗 %s: %s", email, e)
        raise HTTPException(502, "メールを送信できませんでした。しばらくしてもう一度お試しください。")


@app.post("/api/login")
async def request_login(body: LoginRequest):
    """メールアドレスを受け取り、名簿にあればワンタイムURLをメール送信する"""
    email = body.email.lower().strip()
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT email, name FROM participants WHERE email = ?", (email,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "このメールアドレスは参加者名簿に登録されていません")
    await issue_login_link(email, row["name"])
    return {"ok": True, "message": "ログインリンクをメールで送信しました。受信箱を確認してください。"}


@app.get("/auth/{token}")
async def auth_with_token(token: str):
    """ワンタイムURL。有効ならセッションを発行してロビーへ"""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT email, expires_at, used FROM login_tokens WHERE token = ?", (token,)
        ).fetchone()
        if not row or row["used"] or row["expires_at"] < db.now():
            return RedirectResponse("/?error=invalid_token")
        conn.execute("UPDATE login_tokens SET used = 1 WHERE token = ?", (token,))
        conn.execute(
            "UPDATE participants SET last_login_at = ? WHERE email = ?",
            (db.now(), row["email"]),
        )
        sid = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO sessions(sid, email, expires_at) VALUES(?, ?, ?)",
            (sid, row["email"], db.now() + config.SESSION_TTL_HOURS * 3600),
        )
    resp = RedirectResponse("/lobby.html")
    resp.set_cookie(
        SESSION_COOKIE, sid,
        max_age=config.SESSION_TTL_HOURS * 3600,
        httponly=True, samesite="lax",
        secure=config.BASE_URL.startswith("https"),
    )
    return resp


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        with db.get_db() as conn:
            conn.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/me")
async def me(user: dict = Depends(require_user)):
    return user


# ---------------------------------------------------------------- ルーム

@app.get("/api/rooms")
async def list_rooms(user: dict = Depends(require_user)):
    with db.get_db() as conn:
        rooms = [dict(r) for r in conn.execute("SELECT * FROM rooms ORDER BY created_at")]
        rec_rooms = {
            r["room_name"]
            for r in conn.execute("SELECT room_name FROM recordings WHERE status = 'recording'")
        }
    try:
        active = await lk.list_active_rooms()
    except Exception as e:
        log.warning("LiveKit へ接続できません: %s", e)
        active = {}
    for r in rooms:
        r["participants"] = active.get(r["name"], 0)
        r["recording"] = r["name"] in rec_rooms
    return rooms


@app.post("/api/rooms/{room_name}/join")
async def join_room(room_name: str, user: dict = Depends(require_user)):
    with db.get_db() as conn:
        room = conn.execute("SELECT * FROM rooms WHERE name = ?", (room_name,)).fetchone()
    if not room:
        raise HTTPException(404, "ルームが見つかりません")
    token = lk.create_join_token(room_name, identity=user["email"], name=user["name"] or user["email"])
    return {"token": token, "ws_url": config.LIVEKIT_WS_URL, "title": room["title"]}


# ---------------------------------------------------------------- 管理: 名簿

class ParticipantIn(BaseModel):
    email: EmailStr
    name: str = ""
    is_admin: bool = False


@app.get("/api/admin/participants")
async def admin_list_participants(user: dict = Depends(require_admin)):
    """名簿一覧。最終ログイン・アクティブセッション・在室状況付き"""
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM participants ORDER BY email")]
        active_sessions = {
            r["email"]
            for r in conn.execute("SELECT DISTINCT email FROM sessions WHERE expires_at > ?", (db.now(),))
        }
    try:
        online = await lk.list_online_identities()
    except Exception as e:
        log.warning("在室状況を取得できません: %s", e)
        online = {}
    for r in rows:
        r["logged_in"] = r["email"] in active_sessions
        r["online_room"] = online.get(r["email"])
    return rows


class ParticipantPatch(BaseModel):
    name: str | None = None
    is_admin: bool | None = None


@app.patch("/api/admin/participants/{email}")
async def admin_update_participant(email: str, body: ParticipantPatch, user: dict = Depends(require_admin)):
    email = email.lower().strip()
    if body.is_admin is False and email == user["email"]:
        raise HTTPException(400, "自分自身の管理者権限は外せません")
    with db.get_db() as conn:
        if not conn.execute("SELECT 1 FROM participants WHERE email = ?", (email,)).fetchone():
            raise HTTPException(404, "参加者が見つかりません")
        if body.name is not None:
            conn.execute("UPDATE participants SET name = ? WHERE email = ?", (body.name.strip(), email))
        if body.is_admin is not None:
            conn.execute("UPDATE participants SET is_admin = ? WHERE email = ?", (int(body.is_admin), email))
    return {"ok": True}


class EmailList(BaseModel):
    emails: list[EmailStr]


@app.post("/api/admin/participants/send-links")
async def admin_send_links(body: EmailList, user: dict = Depends(require_admin)):
    """指定した参加者にログインリンクを一斉送信"""
    with db.get_db() as conn:
        rows = conn.execute(
            f"SELECT email, name FROM participants WHERE email IN ({','.join('?' * len(body.emails))})",
            [e.lower().strip() for e in body.emails],
        ).fetchall() if body.emails else []
    sent, failed = 0, []
    for r in rows:
        try:
            await issue_login_link(r["email"], r["name"])
            sent += 1
        except Exception as e:
            log.error("リンク送信失敗 %s: %s", r["email"], e)
            failed.append(r["email"])
    return {"ok": True, "sent": sent, "failed": failed}


@app.post("/api/admin/participants/bulk-delete")
async def admin_bulk_delete(body: EmailList, user: dict = Depends(require_admin)):
    emails = [e.lower().strip() for e in body.emails if e.lower().strip() != user["email"]]
    with db.get_db() as conn:
        for email in emails:
            conn.execute("DELETE FROM participants WHERE email = ?", (email,))
            conn.execute("DELETE FROM sessions WHERE email = ?", (email,))
    return {"ok": True, "deleted": len(emails)}


@app.get("/api/admin/participants/export.csv")
async def admin_export_csv(user: dict = Depends(require_admin)):
    with db.get_db() as conn:
        rows = conn.execute("SELECT email, name, is_admin FROM participants ORDER BY email").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "name", "admin"])
    for r in rows:
        w.writerow([r["email"], r["name"], "admin" if r["is_admin"] else ""])
    return Response(
        content="\ufeff" + buf.getvalue(),  # BOM付き: Excelで文字化けしないように
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=participants.csv"},
    )


@app.post("/api/admin/participants")
async def admin_add_participant(body: ParticipantIn, user: dict = Depends(require_admin)):
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO participants(email, name, is_admin, created_at) VALUES(?, ?, ?, ?)
               ON CONFLICT(email) DO UPDATE SET name = excluded.name, is_admin = excluded.is_admin""",
            (body.email.lower().strip(), body.name.strip(), int(body.is_admin), db.now()),
        )
    return {"ok": True}


@app.post("/api/admin/participants/csv")
async def admin_upload_csv(file: UploadFile, user: dict = Depends(require_admin)):
    """CSV一括登録。列: email,name[,admin] (ヘッダー行は自動判定)"""
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    count = 0
    with db.get_db() as conn:
        for row in reader:
            if not row or not row[0].strip():
                continue
            email = row[0].strip().lower()
            if "@" not in email:  # ヘッダー行や不正行をスキップ
                continue
            name = row[1].strip() if len(row) > 1 else ""
            is_admin = 1 if len(row) > 2 and row[2].strip().lower() in ("1", "true", "admin", "yes") else 0
            conn.execute(
                """INSERT INTO participants(email, name, is_admin, created_at) VALUES(?, ?, ?, ?)
                   ON CONFLICT(email) DO UPDATE SET name = excluded.name, is_admin = excluded.is_admin""",
                (email, name, is_admin, db.now()),
            )
            count += 1
    return {"ok": True, "imported": count}


@app.delete("/api/admin/participants/{email}")
async def admin_delete_participant(email: str, user: dict = Depends(require_admin)):
    email = email.lower().strip()
    if email == user["email"]:
        raise HTTPException(400, "自分自身は削除できません")
    with db.get_db() as conn:
        conn.execute("DELETE FROM participants WHERE email = ?", (email,))
        conn.execute("DELETE FROM sessions WHERE email = ?", (email,))
    return {"ok": True}


# ---------------------------------------------------------------- 管理: ルーム

class RoomIn(BaseModel):
    title: str
    description: str = ""
    auto_record: bool = True


@app.post("/api/admin/rooms")
async def admin_create_room(body: RoomIn, user: dict = Depends(require_admin)):
    slug = re.sub(r"[^a-z0-9-]+", "-", body.title.lower()).strip("-")
    if not slug:
        slug = f"room-{secrets.token_hex(3)}"
    with db.get_db() as conn:
        exists = conn.execute("SELECT 1 FROM rooms WHERE name = ?", (slug,)).fetchone()
        if exists:
            slug = f"{slug}-{secrets.token_hex(2)}"
        conn.execute(
            "INSERT INTO rooms(name, title, description, auto_record, created_at) VALUES(?, ?, ?, ?, ?)",
            (slug, body.title.strip(), body.description.strip(), int(body.auto_record), db.now()),
        )
    return {"ok": True, "name": slug}


@app.delete("/api/admin/rooms/{room_name}")
async def admin_delete_room(room_name: str, user: dict = Depends(require_admin)):
    await _stop_room_recordings(room_name)
    with db.get_db() as conn:
        conn.execute("DELETE FROM rooms WHERE name = ?", (room_name,))
    return {"ok": True}


# ---------------------------------------------------------------- 管理: 録画

@app.post("/api/admin/rooms/{room_name}/record/start")
async def admin_start_recording(room_name: str, user: dict = Depends(require_admin)):
    with db.get_db() as conn:
        room = conn.execute("SELECT * FROM rooms WHERE name = ?", (room_name,)).fetchone()
        already = conn.execute(
            "SELECT 1 FROM recordings WHERE room_name = ? AND status = 'recording'", (room_name,)
        ).fetchone()
    if not room:
        raise HTTPException(404, "ルームが見つかりません")
    if already:
        return {"ok": True, "message": "すでに録画中です"}
    try:
        egress_id, filename = await lk.start_room_recording(room_name)
    except Exception as e:
        raise HTTPException(502, f"録画を開始できません(ルームが稼働中か確認してください): {e}")
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO recordings(egress_id, room_name, filename, started_at) VALUES(?, ?, ?, ?)",
            (egress_id, room_name, filename, db.now()),
        )
    return {"ok": True, "egress_id": egress_id}


async def _stop_room_recordings(room_name: str):
    with db.get_db() as conn:
        recs = conn.execute(
            "SELECT egress_id FROM recordings WHERE room_name = ? AND status = 'recording'",
            (room_name,),
        ).fetchall()
    for r in recs:
        try:
            await lk.stop_recording(r["egress_id"])
        except Exception as e:
            log.warning("録画停止に失敗 egress=%s: %s", r["egress_id"], e)


@app.post("/api/admin/rooms/{room_name}/record/stop")
async def admin_stop_recording(room_name: str, user: dict = Depends(require_admin)):
    await _stop_room_recordings(room_name)
    return {"ok": True}


def _download_sig(filename: str, exp: int) -> str:
    return hmac.new(
        config.LIVEKIT_API_SECRET.encode(), f"dl:{filename}:{exp}".encode(), hashlib.sha256
    ).hexdigest()


def _make_download_token(filename: str, ttl_hours: int = 12) -> str:
    exp = db.now() + ttl_hours * 3600
    return f"{exp}.{_download_sig(filename, exp)}"


@app.get("/api/admin/recordings")
async def admin_list_recordings(user: dict = Depends(require_admin)):
    with db.get_db() as conn:
        recs = [dict(r) for r in conn.execute("SELECT * FROM recordings ORDER BY started_at DESC")]
    for r in recs:
        path = config.RECORDINGS_DIR / r["filename"]
        r["size_mb"] = round(path.stat().st_size / 1e6, 1) if path.exists() else None
        # cookie 非依存の署名付きURL(ブラウザのダウンロード処理はセッションcookieを送らないことがある)
        r["download_url"] = (
            f"/api/admin/recordings/{r['filename']}/download?t={_make_download_token(r['filename'])}"
        )
    return recs


@app.get("/api/admin/recordings/{filename}/download")
async def admin_download_recording(filename: str, request: Request, t: str = ""):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "不正なファイル名です")
    # 署名付きトークン、または管理者セッションのどちらかで認可
    authorized = False
    if t and "." in t:
        exp_s, sig = t.split(".", 1)
        if exp_s.isdigit() and int(exp_s) > db.now() and hmac.compare_digest(sig, _download_sig(filename, int(exp_s))):
            authorized = True
    if not authorized:
        user = get_session_user(request)
        if not user or not user["is_admin"]:
            raise HTTPException(401, "ダウンロード権限がありません(管理画面から開き直してください)")
    path = config.RECORDINGS_DIR / filename
    if not path.exists():
        raise HTTPException(404, "ファイルが見つかりません(録画終了直後は数秒かかることがあります)")
    return FileResponse(path, media_type="video/mp4", filename=filename)


# ---------------------------------------------------------------- LiveKit Webhook

@app.post("/api/lk-webhook")
async def lk_webhook(request: Request):
    """LiveKit からのイベント。auto_record のルームは開始と同時に録画する"""
    body = (await request.body()).decode()
    auth = request.headers.get("Authorization", "")
    try:
        event = webhook_receiver.receive(body, auth)
    except Exception as e:
        log.warning("webhook 検証失敗: %s", e)
        raise HTTPException(401, "invalid webhook")

    log.info("webhook event=%s room=%s", event.event, event.room.name if event.room else "-")
    # room_started: ルーム開始時 / participant_joined: 録画失敗・手動停止後の再開にも対応
    if event.event in ("room_started", "participant_joined"):
        room_name = event.room.name
        await _handle_auto_record(room_name)

    elif event.event == "egress_ended":
        info = event.egress_info
        status = "done" if info.status == 3 else "failed"  # 3 = EGRESS_COMPLETE
        with db.get_db() as conn:
            conn.execute(
                "UPDATE recordings SET status = ?, ended_at = ? WHERE egress_id = ?",
                (status, db.now(), info.egress_id),
            )
        log.info("録画終了 egress=%s status=%s", info.egress_id, status)

    return JSONResponse({"ok": True})


_auto_record_lock = asyncio.Lock()


async def _handle_auto_record(room_name: str):
    # 同時多発の webhook で録画が二重起動しないよう直列化する
    async with _auto_record_lock:
        with db.get_db() as conn:
            room = conn.execute(
                "SELECT auto_record FROM rooms WHERE name = ?", (room_name,)
            ).fetchone()
            already = conn.execute(
                "SELECT 1 FROM recordings WHERE room_name = ? AND status = 'recording'", (room_name,)
            ).fetchone()
        if room and room["auto_record"] and not already:
            try:
                egress_id, filename = await lk.start_room_recording(room_name)
                with db.get_db() as conn:
                    conn.execute(
                        "INSERT INTO recordings(egress_id, room_name, filename, started_at) VALUES(?, ?, ?, ?)",
                        (egress_id, room_name, filename, db.now()),
                    )
            except Exception as e:
                log.error("自動録画の開始に失敗 room=%s: %s", room_name, e)


# ---------------------------------------------------------------- 静的ファイル

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
