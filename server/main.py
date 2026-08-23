import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import random
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
_pres_lock = asyncio.Lock()


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
    """アクティブなハッカソンの名簿と突合したセッションユーザー"""
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    with db.get_db() as conn:
        row = conn.execute(
            """SELECT s.email, p.name, p.is_admin, p.team_id, p.id AS pid,
                      h.id AS hackathon_id, h.name AS hackathon_name,
                      t.name AS team_name, t.room_name AS team_room
               FROM sessions s
               JOIN hackathons h ON h.active = 1
               JOIN participants p ON p.email = s.email AND p.hackathon_id = h.id
               LEFT JOIN teams t ON t.id = p.team_id
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
    token = secrets.token_urlsafe(32)
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO login_tokens(token, email, expires_at) VALUES(?, ?, ?)",
            (token, email, db.now() + config.LOGIN_TOKEN_TTL_MIN * 60),
        )
    url = f"{config.BASE_URL}/auth/{token}"
    try:
        await asyncio.to_thread(mailer.send_login_link, email, name or email, url)
    except Exception as e:
        log.error("メール送信失敗 %s: %s", email, e)
        raise HTTPException(502, "メールを送信できませんでした。しばらくしてもう一度お試しください。")


@app.post("/api/login")
async def request_login(body: LoginRequest):
    email = body.email.lower().strip()
    with db.get_db() as conn:
        h = db.active_hackathon(conn)
        row = conn.execute(
            "SELECT email, name FROM participants WHERE hackathon_id = ? AND email = ?",
            (h["id"], email),
        ).fetchone()
    if not row:
        raise HTTPException(404, "このメールアドレスは参加者名簿に登録されていません")
    await issue_login_link(email, row["name"])
    return {"ok": True, "message": "ログインリンクをメールで送信しました。受信箱を確認してください。"}


@app.get("/auth/{token}")
async def auth_with_token(token: str):
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


# ---------------------------------------------------------------- 進行状態 (全クライアントがポーリング)

async def _tick_presentation(hid: int):
    """発表タイマーが切れていたら次のチームへ自動で進める"""
    async with _pres_lock:
        with db.get_db() as conn:
            h = conn.execute("SELECT * FROM hackathons WHERE id = ?", (hid,)).fetchone()
            if not h or not h["pres_active"] or not h["pres_end_at"] or db.now() < h["pres_end_at"]:
                return
        await _advance_presentation(hid)


async def _advance_presentation(hid: int):
    with db.get_db() as conn:
        h = conn.execute("SELECT * FROM hackathons WHERE id = ?", (hid,)).fetchone()
        order = json.loads(h["pres_order"])
        idx = h["pres_index"] + 1
        if idx >= len(order):
            conn.execute(
                "UPDATE hackathons SET pres_active = 0, pres_end_at = NULL WHERE id = ?", (hid,)
            )
            msg = {"type": "announce", "text": "🎉 全チームの発表が終了しました!"}
        else:
            conn.execute(
                "UPDATE hackathons SET pres_index = ?, pres_end_at = ? WHERE id = ?",
                (idx, db.now() + h["pres_seconds"], hid),
            )
            team = conn.execute("SELECT name FROM teams WHERE id = ?", (order[idx],)).fetchone()
            msg = {"type": "announce", "text": f"📣 次の発表: {team['name'] if team else '?'}"}
        rooms = [r["name"] for r in conn.execute(
            "SELECT name FROM rooms WHERE hackathon_id = ?", (hid,))]
    await lk.broadcast_data(rooms, msg)
    await lk.broadcast_data(rooms, {"type": "pres"})


def _standings(conn, hid: int) -> list[dict]:
    """得票順の作品一覧(同数はID順)"""
    rows = conn.execute(
        """SELECT w.id, w.title, w.url, w.team_id, t.name AS team_name,
                  (SELECT COUNT(*) FROM votes v WHERE v.work_id = w.id AND v.hackathon_id = w.hackathon_id) AS votes
           FROM works w LEFT JOIN teams t ON t.id = w.team_id
           WHERE w.hackathon_id = ?
           ORDER BY votes DESC, w.id ASC""",
        (hid,),
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/state")
async def get_state(user: dict = Depends(require_user)):
    hid = user["hackathon_id"]
    await _tick_presentation(hid)
    with db.get_db() as conn:
        h = conn.execute("SELECT * FROM hackathons WHERE id = ?", (hid,)).fetchone()
        pres = None
        if h["pres_active"]:
            order_ids = json.loads(h["pres_order"])
            teams = {t["id"]: t["name"] for t in conn.execute(
                "SELECT id, name FROM teams WHERE hackathon_id = ?", (hid,))}
            cur = order_ids[h["pres_index"]] if h["pres_index"] < len(order_ids) else None
            pres = {
                "active": True,
                "order": [{"team_id": i, "name": teams.get(i, "?")} for i in order_ids],
                "index": h["pres_index"],
                "seconds": h["pres_seconds"],
                "end_at": h["pres_end_at"],
                "current_team_id": cur,
                "current_team_name": teams.get(cur, "?") if cur else None,
                "i_am_presenting": bool(cur and user["team_id"] == cur),
            }
        reveal = {"stage": h["reveal_stage"], "total": 0, "items": []}
        if h["reveal_stage"] > 0:
            top = _standings(conn, hid)[:3]
            reveal["total"] = len(top)
            # stage=1 で最下位(3位相当)から公開していく
            for s in range(1, min(h["reveal_stage"], len(top)) + 1):
                w = top[len(top) - s]
                reveal["items"].append(
                    {"rank": len(top) - s + 1, "title": w["title"],
                     "team": w["team_name"], "votes": w["votes"]}
                )
    return {
        "hackathon": {"id": hid, "name": h["name"],
                      "works_anonymous": bool(h["works_anonymous"]),
                      "voting_open": bool(h["voting_open"])},
        "me": {"email": user["email"], "name": user["name"], "is_admin": user["is_admin"],
               "team_id": user["team_id"], "team_name": user["team_name"],
               "team_room": user["team_room"]},
        "timer": ({"end_at": h["timer_end_at"], "label": h["timer_label"]}
                  if h["timer_end_at"] and h["timer_end_at"] > db.now() else None),
        "placement": h["placement"],
        "hall_room": f"hall-{hid}",
        "stage_room": f"stage-{hid}",
        "presentation": pres,
        "reveal": reveal,
    }


@app.post("/api/stage/token")
async def stage_token(user: dict = Depends(require_user)):
    """ステージ(全体配信/発表)ルームのトークン。配信可否はサーバーが判断"""
    hid = user["hackathon_id"]
    can_publish = bool(user["is_admin"])
    if not can_publish:
        with db.get_db() as conn:
            h = conn.execute("SELECT * FROM hackathons WHERE id = ?", (hid,)).fetchone()
            if h["pres_active"]:
                order = json.loads(h["pres_order"])
                cur = order[h["pres_index"]] if h["pres_index"] < len(order) else None
                can_publish = bool(cur and user["team_id"] == cur)
    token = lk.create_join_token(
        f"stage-{hid}",
        identity=user["email"],
        name=user["name"] or user["email"],
        can_publish=can_publish,
        hidden=not can_publish,   # 視聴者は参加者リストに出さない
    )
    return {"token": token, "ws_url": config.LIVEKIT_WS_URL,
            "room": f"stage-{hid}", "can_publish": can_publish}


# ---------------------------------------------------------------- ルーム

@app.get("/api/rooms")
async def list_rooms(user: dict = Depends(require_user)):
    hid = user["hackathon_id"]
    with db.get_db() as conn:
        rooms = [dict(r) for r in conn.execute(
            """SELECT * FROM rooms WHERE hackathon_id = ? AND kind IN ('hall','team','free')
               ORDER BY CASE kind WHEN 'hall' THEN 0 WHEN 'team' THEN 1 ELSE 2 END, created_at""",
            (hid,))]
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
        r["is_mine"] = bool(user["team_id"] and r["team_id"] == user["team_id"])
    return rooms


@app.post("/api/rooms/{room_name}/join")
async def join_room(room_name: str, user: dict = Depends(require_user)):
    with db.get_db() as conn:
        room = conn.execute(
            "SELECT * FROM rooms WHERE name = ? AND hackathon_id = ?",
            (room_name, user["hackathon_id"]),
        ).fetchone()
    if not room:
        raise HTTPException(404, "ルームが見つかりません")
    token = lk.create_join_token(room_name, identity=user["email"], name=user["name"] or user["email"])
    return {"token": token, "ws_url": config.LIVEKIT_WS_URL, "title": room["title"]}


# ---------------------------------------------------------------- 作品と投票

class WorkIn(BaseModel):
    title: str
    url: str


@app.get("/api/works")
async def list_works(user: dict = Depends(require_user)):
    hid = user["hackathon_id"]
    with db.get_db() as conn:
        h = conn.execute("SELECT * FROM hackathons WHERE id = ?", (hid,)).fetchone()
        works = _standings(conn, hid)
        my_vote = conn.execute(
            "SELECT work_id FROM votes WHERE hackathon_id = ? AND voter_email = ?",
            (hid, user["email"]),
        ).fetchone()
    anonymous = bool(h["works_anonymous"]) and not user["is_admin"]
    out = []
    for w in sorted(works, key=lambda x: x["id"]):
        out.append({
            "id": w["id"], "title": w["title"], "url": w["url"],
            "team_name": None if anonymous else w["team_name"],
            "mine": bool(user["team_id"] and w["team_id"] == user["team_id"]),
            "votes": w["votes"] if user["is_admin"] else None,
        })
    return {"works": out, "voting_open": bool(h["voting_open"]),
            "anonymous": bool(h["works_anonymous"]),
            "my_vote": my_vote["work_id"] if my_vote else None}


@app.post("/api/works")
async def register_work(body: WorkIn, user: dict = Depends(require_user)):
    if not user["team_id"]:
        raise HTTPException(400, "チームに所属していないため作品を登録できません")
    url = body.url.strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "URLは http(s):// で始まる必要があります")
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO works(hackathon_id, team_id, title, url, created_at)
               VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(hackathon_id, team_id)
               DO UPDATE SET title = excluded.title, url = excluded.url""",
            (user["hackathon_id"], user["team_id"], body.title.strip(), url, db.now()),
        )
    return {"ok": True}


@app.post("/api/works/{work_id}/vote")
async def vote_work(work_id: int, user: dict = Depends(require_user)):
    hid = user["hackathon_id"]
    with db.get_db() as conn:
        h = conn.execute("SELECT voting_open FROM hackathons WHERE id = ?", (hid,)).fetchone()
        if not h["voting_open"]:
            raise HTTPException(400, "投票は現在受け付けていません")
        work = conn.execute(
            "SELECT * FROM works WHERE id = ? AND hackathon_id = ?", (work_id, hid)
        ).fetchone()
        if not work:
            raise HTTPException(404, "作品が見つかりません")
        if user["team_id"] and work["team_id"] == user["team_id"]:
            raise HTTPException(400, "自分のチームの作品には投票できません")
        conn.execute(
            """INSERT INTO votes(hackathon_id, voter_email, work_id, created_at)
               VALUES(?, ?, ?, ?)
               ON CONFLICT(hackathon_id, voter_email)
               DO UPDATE SET work_id = excluded.work_id, created_at = excluded.created_at""",
            (hid, user["email"], work_id, db.now()),
        )
    return {"ok": True}


@app.post("/api/works/{work_id}/play")
async def play_work(work_id: int, user: dict = Depends(require_user)):
    """プレイ用ルームを作って参加トークンを返す。自動録画ONなので画面+カメラが録画される"""
    hid = user["hackathon_id"]
    with db.get_db() as conn:
        work = conn.execute(
            "SELECT * FROM works WHERE id = ? AND hackathon_id = ?", (work_id, hid)
        ).fetchone()
        if not work:
            raise HTTPException(404, "作品が見つかりません")
        room_name = f"play-{work_id}-{secrets.token_hex(3)}"
        title = f"プレイ: {work['title']} / {user['name'] or user['email']}"
        conn.execute(
            """INSERT INTO rooms(name, title, description, auto_record, created_at, hackathon_id, kind)
               VALUES(?, ?, '', 1, ?, ?, 'play')""",
            (room_name, title, db.now(), hid),
        )
    token = lk.create_join_token(room_name, identity=user["email"], name=user["name"] or user["email"])
    return {"room": room_name, "token": token, "ws_url": config.LIVEKIT_WS_URL,
            "work": {"title": work["title"], "url": work["url"]}}


# ---------------------------------------------------------------- 管理: ハッカソン / チーム

class HackathonIn(BaseModel):
    name: str


@app.get("/api/admin/hackathons")
async def admin_hackathons(user: dict = Depends(require_admin)):
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT h.*,
                      (SELECT COUNT(*) FROM participants p WHERE p.hackathon_id = h.id) AS participants,
                      (SELECT COUNT(*) FROM teams t WHERE t.hackathon_id = h.id) AS teams
               FROM hackathons h ORDER BY h.id DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/admin/hackathons")
async def admin_create_hackathon(body: HackathonIn, user: dict = Depends(require_admin)):
    """新しいハッカソンを作成してアクティブにする(チームIDは作り直し)"""
    with db.get_db() as conn:
        conn.execute("UPDATE hackathons SET active = 0")
        cur = conn.execute(
            "INSERT INTO hackathons(name, active, created_at) VALUES(?, 1, ?)",
            (body.name.strip() or "ハッカソン", db.now()),
        )
        hid = cur.lastrowid
        db.ensure_hall(conn, hid)
        db.seed_admins(conn, hid)
    return {"ok": True, "id": hid}


@app.post("/api/admin/hackathons/{hid}/activate")
async def admin_activate_hackathon(hid: int, user: dict = Depends(require_admin)):
    with db.get_db() as conn:
        if not conn.execute("SELECT 1 FROM hackathons WHERE id = ?", (hid,)).fetchone():
            raise HTTPException(404, "ハッカソンが見つかりません")
        conn.execute("UPDATE hackathons SET active = 0")
        conn.execute("UPDATE hackathons SET active = 1 WHERE id = ?", (hid,))
        db.ensure_hall(conn, hid)
        db.seed_admins(conn, hid)
    return {"ok": True}


class TeamIn(BaseModel):
    name: str


def _create_team(conn, hid: int, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO teams(hackathon_id, name, room_name) VALUES(?, ?, '')",
        (hid, name),
    )
    tid = cur.lastrowid
    room_name = f"team-{tid}"
    conn.execute("UPDATE teams SET room_name = ? WHERE id = ?", (room_name, tid))
    conn.execute(
        """INSERT INTO rooms(name, title, description, auto_record, created_at, hackathon_id, team_id, kind)
           VALUES(?, ?, ?, 1, ?, ?, ?, 'team')""",
        (room_name, name, f"{name} のブレイクアウトルーム", db.now(), hid, tid),
    )
    return tid


@app.get("/api/admin/teams")
async def admin_teams(user: dict = Depends(require_admin)):
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT t.*, (SELECT COUNT(*) FROM participants p WHERE p.team_id = t.id) AS members
               FROM teams t WHERE t.hackathon_id = ? ORDER BY t.id""",
            (user["hackathon_id"],),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/admin/teams")
async def admin_create_team(body: TeamIn, user: dict = Depends(require_admin)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "チーム名を入力してください")
    with db.get_db() as conn:
        tid = _create_team(conn, user["hackathon_id"], name)
    return {"ok": True, "id": tid}


@app.delete("/api/admin/teams/{team_id}")
async def admin_delete_team(team_id: int, user: dict = Depends(require_admin)):
    with db.get_db() as conn:
        team = conn.execute(
            "SELECT * FROM teams WHERE id = ? AND hackathon_id = ?",
            (team_id, user["hackathon_id"]),
        ).fetchone()
        if not team:
            raise HTTPException(404, "チームが見つかりません")
        conn.execute("UPDATE participants SET team_id = NULL WHERE team_id = ?", (team_id,))
        conn.execute("DELETE FROM rooms WHERE team_id = ?", (team_id,))
        conn.execute("DELETE FROM votes WHERE work_id IN (SELECT id FROM works WHERE team_id = ?)", (team_id,))
        conn.execute("DELETE FROM works WHERE team_id = ?", (team_id,))
        conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))
    return {"ok": True}


# ---------------------------------------------------------------- 管理: 名簿

class ParticipantIn(BaseModel):
    email: EmailStr
    name: str = ""
    is_admin: bool = False
    team_id: int | None = None


@app.get("/api/admin/participants")
async def admin_list_participants(user: dict = Depends(require_admin)):
    hid = user["hackathon_id"]
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT p.*, t.name AS team_name FROM participants p
               LEFT JOIN teams t ON t.id = p.team_id
               WHERE p.hackathon_id = ? ORDER BY p.email""",
            (hid,))]
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


@app.post("/api/admin/participants")
async def admin_add_participant(body: ParticipantIn, user: dict = Depends(require_admin)):
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO participants(hackathon_id, email, name, team_id, is_admin, created_at)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(hackathon_id, email)
               DO UPDATE SET name = excluded.name, team_id = excluded.team_id, is_admin = excluded.is_admin""",
            (user["hackathon_id"], body.email.lower().strip(), body.name.strip(),
             body.team_id, int(body.is_admin), db.now()),
        )
    return {"ok": True}


class ParticipantPatch(BaseModel):
    name: str | None = None
    is_admin: bool | None = None
    team_id: int | None = None
    clear_team: bool = False


@app.patch("/api/admin/participants/{email}")
async def admin_update_participant(email: str, body: ParticipantPatch, user: dict = Depends(require_admin)):
    email = email.lower().strip()
    hid = user["hackathon_id"]
    if body.is_admin is False and email == user["email"]:
        raise HTTPException(400, "自分自身の管理者権限は外せません")
    with db.get_db() as conn:
        if not conn.execute(
            "SELECT 1 FROM participants WHERE hackathon_id = ? AND email = ?", (hid, email)
        ).fetchone():
            raise HTTPException(404, "参加者が見つかりません")
        if body.name is not None:
            conn.execute("UPDATE participants SET name = ? WHERE hackathon_id = ? AND email = ?",
                         (body.name.strip(), hid, email))
        if body.is_admin is not None:
            conn.execute("UPDATE participants SET is_admin = ? WHERE hackathon_id = ? AND email = ?",
                         (int(body.is_admin), hid, email))
        if body.clear_team:
            conn.execute("UPDATE participants SET team_id = NULL WHERE hackathon_id = ? AND email = ?",
                         (hid, email))
        elif body.team_id is not None:
            conn.execute("UPDATE participants SET team_id = ? WHERE hackathon_id = ? AND email = ?",
                         (body.team_id, hid, email))
    return {"ok": True}


@app.post("/api/admin/participants/csv")
async def admin_upload_csv(file: UploadFile, user: dict = Depends(require_admin)):
    """CSV一括登録。列: email,name,team,admin (ヘッダー行は自動判定、チームは自動作成)"""
    hid = user["hackathon_id"]
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    count = 0
    with db.get_db() as conn:
        team_ids = {t["name"]: t["id"] for t in conn.execute(
            "SELECT id, name FROM teams WHERE hackathon_id = ?", (hid,))}
        for row in reader:
            if not row or not row[0].strip():
                continue
            email = row[0].strip().lower()
            if "@" not in email:
                continue
            name = row[1].strip() if len(row) > 1 else ""
            team_name = row[2].strip() if len(row) > 2 else ""
            is_admin = 1 if len(row) > 3 and row[3].strip().lower() in ("1", "true", "admin", "yes") else 0
            team_id = None
            if team_name:
                if team_name not in team_ids:
                    team_ids[team_name] = _create_team(conn, hid, team_name)
                team_id = team_ids[team_name]
            conn.execute(
                """INSERT INTO participants(hackathon_id, email, name, team_id, is_admin, created_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(hackathon_id, email)
                   DO UPDATE SET name = excluded.name, team_id = excluded.team_id, is_admin = excluded.is_admin""",
                (hid, email, name, team_id, is_admin, db.now()),
            )
            count += 1
    return {"ok": True, "imported": count}


class EmailList(BaseModel):
    emails: list[EmailStr]


@app.post("/api/admin/participants/send-links")
async def admin_send_links(body: EmailList, user: dict = Depends(require_admin)):
    hid = user["hackathon_id"]
    with db.get_db() as conn:
        rows = conn.execute(
            f"""SELECT email, name FROM participants
                WHERE hackathon_id = ? AND email IN ({','.join('?' * len(body.emails))})""",
            [hid] + [e.lower().strip() for e in body.emails],
        ).fetchall() if body.emails else []
    sent, failed = 0, []
    for r in rows:
        try:
            await issue_login_link(r["email"], r["name"])
            sent += 1
        except Exception:
            failed.append(r["email"])
    return {"ok": True, "sent": sent, "failed": failed}


@app.post("/api/admin/participants/bulk-delete")
async def admin_bulk_delete(body: EmailList, user: dict = Depends(require_admin)):
    hid = user["hackathon_id"]
    emails = [e.lower().strip() for e in body.emails if e.lower().strip() != user["email"]]
    with db.get_db() as conn:
        for email in emails:
            conn.execute("DELETE FROM participants WHERE hackathon_id = ? AND email = ?", (hid, email))
            conn.execute("DELETE FROM sessions WHERE email = ?", (email,))
    return {"ok": True, "deleted": len(emails)}


@app.get("/api/admin/participants/export.csv")
async def admin_export_csv(user: dict = Depends(require_admin)):
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT p.email, p.name, t.name AS team, p.is_admin FROM participants p
               LEFT JOIN teams t ON t.id = p.team_id
               WHERE p.hackathon_id = ? ORDER BY p.email""",
            (user["hackathon_id"],),
        ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "name", "team", "admin"])
    for r in rows:
        w.writerow([r["email"], r["name"], r["team"] or "", "admin" if r["is_admin"] else ""])
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=participants.csv"},
    )


@app.delete("/api/admin/participants/{email}")
async def admin_delete_participant(email: str, user: dict = Depends(require_admin)):
    email = email.lower().strip()
    if email == user["email"]:
        raise HTTPException(400, "自分自身は削除できません")
    with db.get_db() as conn:
        conn.execute("DELETE FROM participants WHERE hackathon_id = ? AND email = ?",
                     (user["hackathon_id"], email))
        conn.execute("DELETE FROM sessions WHERE email = ?", (email,))
    return {"ok": True}


# ---------------------------------------------------------------- 管理: ルーム

class RoomIn(BaseModel):
    title: str
    description: str = ""
    auto_record: bool = True


@app.post("/api/admin/rooms")
async def admin_create_room(body: RoomIn, user: dict = Depends(require_admin)):
    slug = re.sub(r"[^a-z0-9-]+", "-", body.title.lower()).strip("-") or f"room-{secrets.token_hex(3)}"
    with db.get_db() as conn:
        if conn.execute("SELECT 1 FROM rooms WHERE name = ?", (slug,)).fetchone():
            slug = f"{slug}-{secrets.token_hex(2)}"
        conn.execute(
            """INSERT INTO rooms(name, title, description, auto_record, created_at, hackathon_id, kind)
               VALUES(?, ?, ?, ?, ?, ?, 'free')""",
            (slug, body.title.strip(), body.description.strip(), int(body.auto_record),
             db.now(), user["hackathon_id"]),
        )
    return {"ok": True, "name": slug}


@app.delete("/api/admin/rooms/{room_name}")
async def admin_delete_room(room_name: str, user: dict = Depends(require_admin)):
    await _stop_room_recordings(room_name)
    with db.get_db() as conn:
        room = conn.execute("SELECT kind FROM rooms WHERE name = ?", (room_name,)).fetchone()
        if room and room["kind"] in ("hall", "team"):
            raise HTTPException(400, "全体会場とチームルームはここからは削除できません")
        conn.execute("DELETE FROM rooms WHERE name = ?", (room_name,))
    return {"ok": True}


# ---------------------------------------------------------------- 管理: 進行 (お知らせ/タイマー/配置/発表/投票/結果)

async def _hackathon_rooms(hid: int) -> list[str]:
    with db.get_db() as conn:
        return [r["name"] for r in conn.execute(
            "SELECT name FROM rooms WHERE hackathon_id = ?", (hid,))]


class AnnounceIn(BaseModel):
    text: str


@app.post("/api/admin/announce")
async def admin_announce(body: AnnounceIn, user: dict = Depends(require_admin)):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "メッセージを入力してください")
    rooms = await _hackathon_rooms(user["hackathon_id"])
    await lk.broadcast_data(rooms, {"type": "announce", "text": text})
    return {"ok": True}


class TimerIn(BaseModel):
    seconds: int
    label: str = ""


@app.post("/api/admin/timer")
async def admin_set_timer(body: TimerIn, user: dict = Depends(require_admin)):
    hid = user["hackathon_id"]
    if body.seconds < 10 or body.seconds > 24 * 3600:
        raise HTTPException(400, "タイマーは10秒〜24時間で設定してください")
    with db.get_db() as conn:
        conn.execute("UPDATE hackathons SET timer_end_at = ?, timer_label = ? WHERE id = ?",
                     (db.now() + body.seconds, body.label.strip(), hid))
    await lk.broadcast_data(await _hackathon_rooms(hid), {"type": "timer"})
    return {"ok": True}


@app.delete("/api/admin/timer")
async def admin_clear_timer(user: dict = Depends(require_admin)):
    hid = user["hackathon_id"]
    with db.get_db() as conn:
        conn.execute("UPDATE hackathons SET timer_end_at = NULL, timer_label = '' WHERE id = ?", (hid,))
    await lk.broadcast_data(await _hackathon_rooms(hid), {"type": "timer"})
    return {"ok": True}


class PlacementIn(BaseModel):
    mode: str  # free / hall / teams


@app.post("/api/admin/placement")
async def admin_placement(body: PlacementIn, user: dict = Depends(require_admin)):
    """ワンボタン: hall=全員を全体会場へ召集 / teams=全員をチームルームへ送出 / free=自由"""
    if body.mode not in ("free", "hall", "teams"):
        raise HTTPException(400, "mode は free / hall / teams のいずれか")
    hid = user["hackathon_id"]
    with db.get_db() as conn:
        conn.execute("UPDATE hackathons SET placement = ? WHERE id = ?", (body.mode, hid))
    await lk.broadcast_data(await _hackathon_rooms(hid), {"type": "move", "mode": body.mode})
    return {"ok": True}


class PresStart(BaseModel):
    seconds: int = 300


@app.post("/api/admin/presentation/start")
async def admin_pres_start(body: PresStart, user: dict = Depends(require_admin)):
    hid = user["hackathon_id"]
    if body.seconds < 30 or body.seconds > 3600:
        raise HTTPException(400, "発表時間は30秒〜60分で設定してください")
    with db.get_db() as conn:
        teams = [t["id"] for t in conn.execute(
            "SELECT id FROM teams WHERE hackathon_id = ? ORDER BY id", (hid,))]
        if not teams:
            raise HTTPException(400, "チームがありません")
        random.shuffle(teams)
        conn.execute(
            """UPDATE hackathons SET pres_active = 1, pres_order = ?, pres_index = 0,
               pres_seconds = ?, pres_end_at = ? WHERE id = ?""",
            (json.dumps(teams), body.seconds, db.now() + body.seconds, hid),
        )
        first = conn.execute("SELECT name FROM teams WHERE id = ?", (teams[0],)).fetchone()
    rooms = await _hackathon_rooms(hid)
    await lk.broadcast_data(rooms, {"type": "announce",
                                    "text": f"🎤 発表モード開始!最初の発表: {first['name']}"})
    await lk.broadcast_data(rooms, {"type": "pres"})
    return {"ok": True}


@app.post("/api/admin/presentation/next")
async def admin_pres_next(user: dict = Depends(require_admin)):
    async with _pres_lock:
        await _advance_presentation(user["hackathon_id"])
    return {"ok": True}


@app.post("/api/admin/presentation/stop")
async def admin_pres_stop(user: dict = Depends(require_admin)):
    hid = user["hackathon_id"]
    with db.get_db() as conn:
        conn.execute("UPDATE hackathons SET pres_active = 0, pres_end_at = NULL WHERE id = ?", (hid,))
    await lk.broadcast_data(await _hackathon_rooms(hid), {"type": "pres"})
    return {"ok": True}


class WorksSettings(BaseModel):
    anonymous: bool | None = None
    voting_open: bool | None = None


@app.post("/api/admin/works-settings")
async def admin_works_settings(body: WorksSettings, user: dict = Depends(require_admin)):
    hid = user["hackathon_id"]
    with db.get_db() as conn:
        if body.anonymous is not None:
            conn.execute("UPDATE hackathons SET works_anonymous = ? WHERE id = ?",
                         (int(body.anonymous), hid))
        if body.voting_open is not None:
            conn.execute("UPDATE hackathons SET voting_open = ? WHERE id = ?",
                         (int(body.voting_open), hid))
    if body.voting_open:
        await lk.broadcast_data(await _hackathon_rooms(hid),
                                {"type": "announce", "text": "🗳 投票が始まりました!ロビーの作品ギャラリーから投票してください"})
    return {"ok": True}


@app.get("/api/admin/works")
async def admin_works(user: dict = Depends(require_admin)):
    with db.get_db() as conn:
        return _standings(conn, user["hackathon_id"])


@app.post("/api/admin/reveal/next")
async def admin_reveal_next(user: dict = Depends(require_admin)):
    """結果発表を1段階進める(3位→2位→1位)。ドラムロールは各クライアントで再生"""
    hid = user["hackathon_id"]
    with db.get_db() as conn:
        h = conn.execute("SELECT * FROM hackathons WHERE id = ?", (hid,)).fetchone()
        top = _standings(conn, hid)[:3]
        if not top:
            raise HTTPException(400, "作品がありません")
        if h["reveal_stage"] >= len(top):
            raise HTTPException(400, "すべて発表済みです")
        stage = h["reveal_stage"] + 1
        conn.execute("UPDATE hackathons SET reveal_stage = ? WHERE id = ?", (stage, hid))
        w = top[len(top) - stage]
        rank = len(top) - stage + 1
    await lk.broadcast_data(
        await _hackathon_rooms(hid),
        {"type": "reveal", "rank": rank, "title": w["title"],
         "team": w["team_name"], "votes": w["votes"]},
    )
    return {"ok": True, "rank": rank}


@app.post("/api/admin/reveal/reset")
async def admin_reveal_reset(user: dict = Depends(require_admin)):
    with db.get_db() as conn:
        conn.execute("UPDATE hackathons SET reveal_stage = 0 WHERE id = ?", (user["hackathon_id"],))
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
        recs = [dict(r) for r in conn.execute(
            """SELECT rec.*, ro.title AS room_title, ro.kind AS room_kind
               FROM recordings rec LEFT JOIN rooms ro ON ro.name = rec.room_name
               ORDER BY rec.started_at DESC"""
        )]
    for r in recs:
        path = config.RECORDINGS_DIR / r["filename"]
        r["size_mb"] = round(path.stat().st_size / 1e6, 1) if path.exists() else None
        r["download_url"] = (
            f"/api/admin/recordings/{r['filename']}/download?t={_make_download_token(r['filename'])}"
        )
    return recs


@app.get("/api/admin/recordings/{filename}/download")
async def admin_download_recording(filename: str, request: Request, t: str = ""):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "不正なファイル名です")
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
    body = (await request.body()).decode()
    auth = request.headers.get("Authorization", "")
    try:
        event = webhook_receiver.receive(body, auth)
    except Exception as e:
        log.warning("webhook 検証失敗: %s", e)
        raise HTTPException(401, "invalid webhook")

    if event.event in ("room_started", "participant_joined"):
        await _handle_auto_record(event.room.name)

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
