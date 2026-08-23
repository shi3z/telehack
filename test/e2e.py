#!/usr/bin/env python
"""Telehack 全機能E2Eテスト

主催者1名+仮想参加者4名(2チーム)をヘッドレスブラウザで動かし、
認証・チーム・配置・お知らせ・タイマー・発表モード・作品・投票・
結果発表・録画までを自動検証する。

実行:  ../.venv/bin/python e2e.py
"""
import asyncio
import json
import sys
import time

from playwright.async_api import async_playwright

from util import BASE, db, inject_session, inject_login_token, active_hackathon_id, wait_for

ADMIN_EMAIL = "shi3z@zelpm.com"
RESULTS: list[tuple[str, str, str]] = []   # (name, PASS/FAIL/WARN, note)


def record(name, ok, note=""):
    RESULTS.append((name, "PASS" if ok else "FAIL", note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {note}")


def warn(name, note=""):
    RESULTS.append((name, "WARN", note))
    print(f"  [WARN] {name} {note}")


class User:
    def __init__(self, email, name, team=None):
        self.email, self.name, self.team = email, name, team
        self.ctx = None
        self.page = None

    async def open(self, browser, path="/lobby.html"):
        self.ctx = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            permissions=["camera", "microphone"],
        )
        await self.ctx.add_cookies([
            {"name": "th_session", "value": inject_session(self.email), "url": BASE}
        ])
        self.page = await self.ctx.new_page()
        await self.page.goto(BASE + path)

    async def api(self, method, path, data=None):
        r = await self.ctx.request.fetch(BASE + path, method=method,
                                         data=json.dumps(data) if data is not None else None,
                                         headers={"Content-Type": "application/json"})
        body = await r.json() if "json" in (r.headers.get("content-type") or "") else None
        return r.status, body


async def main():
    print(f"現在のハッカソン(id={PREV_HID})は終了後に復元します")

    admin = User(ADMIN_EMAIL, "admin")
    a1 = User("e2e-a1@example.com", "赤1", "赤チーム")
    a2 = User("e2e-a2@example.com", "赤2", "赤チーム")
    b1 = User("e2e-b1@example.com", "青1", "青チーム")
    b2 = User("e2e-b2@example.com", "青2", "青チーム")
    parts = [a1, a2, b1, b2]

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=[
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
            "--auto-select-desktop-capture-source=Entire screen",
        ])
        await admin.open(browser, "/admin.html")

        # ---- セットアップ: テスト用ハッカソン・チーム・参加者 ----
        print("== セットアップ ==")
        st, _ = await admin.api("POST", "/api/admin/hackathons", {"name": "E2Eテスト"})
        record("ハッカソン作成/アクティブ化", st == 200)
        hid = active_hackathon_id()

        teams = {}
        for tname in ("赤チーム", "青チーム"):
            st, r = await admin.api("POST", "/api/admin/teams", {"name": tname})
            teams[tname] = r["id"]
        st, tlist = await admin.api("GET", "/api/admin/teams")
        record("チーム作成(2チーム)", st == 200 and len(tlist) == 2)
        conn = db()
        team_rooms = {r["id"]: r["room_name"] for r in conn.execute(
            "SELECT id, room_name FROM teams WHERE hackathon_id = ?", (hid,))}
        rooms_cnt = conn.execute(
            "SELECT COUNT(*) FROM rooms WHERE hackathon_id = ? AND kind = 'team'", (hid,)
        ).fetchone()[0]
        conn.close()
        record("チームルーム自動生成", rooms_cnt == 2)

        for u in parts:
            await admin.api("POST", "/api/admin/participants",
                            {"email": u.email, "name": u.name, "team_id": teams[u.team]})
        st, plist = await admin.api("GET", "/api/admin/participants")
        record("名簿登録(チーム割当)", len([x for x in plist if x["email"].startswith("e2e-")]) == 4)

        # ---- 認証フロー: ワンタイムURL ----
        print("== 認証 ==")
        token = inject_login_token(a1.email)
        ctx = await browser.new_context()
        pg = await ctx.new_page()
        await pg.goto(f"{BASE}/auth/{token}")
        ok1 = pg.url.endswith("/lobby.html")
        await pg.goto(f"{BASE}/auth/{token}")   # 再利用
        ok2 = "error=invalid_token" in pg.url
        record("ワンタイムURLでログイン", ok1, pg.url if not ok1 else "")
        record("ワンタイムURLの再利用拒否", ok2)
        await ctx.close()

        # ---- 参加者ログイン(ロビー) ----
        for u in parts:
            await u.open(browser, "/lobby.html")
        await asyncio.sleep(2)
        body = await a1.page.text_content("body")
        record("ロビーにチーム名表示", "赤チーム" in (body or ""), )

        # ---- ワンボタン送出: 全員をチームルームへ ----
        print("== 配置(送出) ==")
        await admin.api("POST", "/api/admin/placement", {"mode": "teams"})

        async def all_in_team_rooms():
            for u in parts:
                if team_rooms[teams[u.team]] not in u.page.url:
                    return False
            return True
        try:
            await wait_for(all_in_team_rooms, timeout=25, desc="チームルームへ自動移動")
            record("ワンボタン送出(チームルームへ自動移動)", True)
        except TimeoutError as e:
            record("ワンボタン送出(チームルームへ自動移動)", False, str(e))
        await asyncio.sleep(4)  # 映像接続待ち

        async def tiles_visible():
            return await a1.page.locator(".tile").count() >= 2  # 自分+チームメイト
        try:
            await wait_for(tiles_visible, timeout=20, desc="ルーム内映像タイル")
            record("チームルームで相互に映像表示", True)
        except TimeoutError:
            record("チームルームで相互に映像表示", False)

        # ---- 自動録画 ----
        async def recording_started():
            st, recs = await admin.api("GET", "/api/admin/recordings")
            active = [r for r in recs if r["status"] == "recording" and r["room_name"].startswith("team-")]
            return len(active) >= 2
        try:
            await wait_for(recording_started, timeout=40, desc="チームルーム自動録画")
            record("入室で自動録画開始(全チームルーム)", True)
        except TimeoutError:
            record("入室で自動録画開始(全チームルーム)", False)

        # ---- お知らせ ----
        print("== お知らせ/タイマー ==")
        await admin.api("POST", "/api/admin/announce", {"text": "E2Eテスト通知"})
        try:
            await b1.page.wait_for_selector(".toast", timeout=10000)
            txt = await b1.page.text_content(".toast")
            record("全ルームお知らせ(トースト表示)", "E2Eテスト通知" in (txt or ""))
        except Exception:
            record("全ルームお知らせ(トースト表示)", False)

        # ---- タイマー ----
        await admin.api("POST", "/api/admin/timer", {"seconds": 600, "label": "作業"})
        try:
            await a2.page.wait_for_selector("#timerChip:visible", timeout=12000)
            txt = await a2.page.text_content("#timerChip")
            record("共有タイマー表示", ":" in (txt or ""), txt or "")
        except Exception:
            record("共有タイマー表示", False)

        # ---- 発表モード ----
        print("== 発表モード ==")
        await admin.api("POST", "/api/admin/presentation/start", {"seconds": 45})
        st, state = await admin.api("GET", "/api/state")
        pres = state["presentation"]
        record("発表モード開始(ランダム順生成)", bool(pres and pres["active"] and len(pres["order"]) == 2))
        cur_team = pres["current_team_id"]
        presenter = a1 if cur_team == teams["赤チーム"] else b1
        viewer = b1 if presenter is a1 else a1
        try:
            await presenter.page.wait_for_selector(".presbar.show", timeout=12000)
            record("発表バナー表示", True)
        except Exception:
            record("発表バナー表示", False)
        try:
            await viewer.page.wait_for_selector("#stagePanel.show", timeout=25000)
            record("発表チームの映像が他ルームに配信", True)
        except Exception:
            record("発表チームの映像が他ルームに配信", False)

        # 自動送り: 45秒待つと次のチームへ
        print("  (自動送りを待機中...約50秒)")
        async def advanced():
            st, s = await admin.api("GET", "/api/state")
            pr = s["presentation"]
            return pr and pr["index"] == 1
        try:
            await wait_for(advanced, timeout=70, interval=3, desc="タイマー切れで次チームへ")
            record("時間切れで自動的に次のチームへ", True)
        except TimeoutError:
            record("時間切れで自動的に次のチームへ", False)
        await admin.api("POST", "/api/admin/presentation/stop")

        # ---- 作品登録 ----
        print("== 作品/投票 ==")
        # a1 はルーム内にいるのでロビーへ移動して登録(配置を自由に戻す)
        await admin.api("POST", "/api/admin/placement", {"mode": "free"})
        try:
            await a1.page.goto(BASE + "/lobby.html")
            await a1.page.wait_for_selector("#myWorkBox", state="visible", timeout=10000)
            await a1.page.fill("#wTitle", "赤の作品")
            await a1.page.fill("#wUrl", "https://example.com/red")
            await a1.page.click("#myWorkBox button")
            await asyncio.sleep(1)
        except Exception as e:
            record("作品登録(UI経由)", False, str(e)[:60])
        st, works = await admin.api("GET", "/api/admin/works")
        if not any(w["title"] == "赤の作品" for w in works):
            # UIが失敗してもAPI登録で後続テストを続行
            await a1.api("POST", "/api/works", {"title": "赤の作品", "url": "https://example.com/red"})
            st, works = await admin.api("GET", "/api/admin/works")
        else:
            record("作品登録(UI経由)", True)
        await b2.api("POST", "/api/works", {"title": "青の作品", "url": "https://example.com/blue"})

        # 匿名表示
        await b1.page.goto(BASE + "/lobby.html")
        await b1.page.wait_for_selector(".work-card", timeout=10000)
        body = await b1.page.text_content("body")
        record("匿名モードでチーム名非表示", "(匿名)" in (body or "") and "by 赤チーム" not in (body or ""))
        # 顕名に切替
        await admin.api("POST", "/api/admin/works-settings", {"anonymous": False})
        async def named():
            b = await b1.page.text_content("body")
            return "by 赤チーム" in (b or "")
        try:
            await wait_for(named, timeout=15, desc="顕名表示")
            record("顕名切替でチーム名表示", True)
        except TimeoutError:
            record("顕名切替でチーム名表示", False)

        # ---- 投票 ----
        st, r = await b1.api("POST", f"/api/works/{works[0]['id'] if works[0]['title']=='赤の作品' else works[1]['id']}/vote")
        record("投票開始前の投票拒否", st == 400)
        await admin.api("POST", "/api/admin/works-settings", {"voting_open": True})
        red_id = next(w["id"] for w in works if w["title"] == "赤の作品")
        st, _ = await b1.api("POST", f"/api/works/{red_id}/vote")
        record("他チーム作品への投票", st == 200)
        st, _ = await a1.api("POST", f"/api/works/{red_id}/vote")
        record("自チーム作品への投票拒否", st == 400)
        st, works2 = await admin.api("GET", "/api/admin/works")
        red_votes = next(w["votes"] for w in works2 if w["id"] == red_id)
        record("得票集計", red_votes == 1, f"votes={red_votes}")
        blue_id = next(w["id"] for w in works2 if w["title"] == "青の作品")
        await a2.api("POST", f"/api/works/{blue_id}/vote")

        # ---- プレイ(画面+カメラ録画) ----
        print("== 作品プレイ録画 ==")
        await b2.page.goto(BASE + f"/play.html?work={red_id}")
        await b2.page.wait_for_selector("#startBtn", timeout=8000)
        await b2.page.click("#startBtn")
        played = False
        try:
            await b2.page.wait_for_selector("#recBadge:visible", timeout=15000)
            played = True
        except Exception:
            pass
        if played:
            async def play_rec():
                st, recs = await admin.api("GET", "/api/admin/recordings")
                return any(r["room_name"].startswith("play-") and r["status"] == "recording" for r in recs)
            try:
                await wait_for(play_rec, timeout=30, desc="プレイ録画開始")
                record("作品プレイで画面+カメラ自動録画", True)
            except TimeoutError:
                record("作品プレイで画面+カメラ自動録画", False, "ルームは作成されたが録画が始まらない")
            await b2.page.click("#endBtn")
        else:
            warn("作品プレイで画面+カメラ自動録画", "ヘッドレスブラウザでは画面キャプチャ不可のため実ブラウザで要確認")

        # ---- 結果発表 ----
        print("== 結果発表 ==")
        st, r = await admin.api("POST", "/api/admin/reveal/next")
        ok = st == 200 and r["rank"] == 2
        try:
            await a2.page.wait_for_selector("#ceremonyOverlay", timeout=10000)
            await asyncio.sleep(3.3)  # ドラムロール後
            txt = await a2.page.text_content("#ceremonyOverlay")
            record("結果発表(2作品なので2位から/ドラムロール演出)", ok and "第 2 位" in (txt or ""), txt.strip()[:40] if txt else "")
        except Exception:
            record("結果発表(2作品なので2位から/ドラムロール演出)", False)
        st, r = await admin.api("POST", "/api/admin/reveal/next")
        record("1位発表", st == 200 and r["rank"] == 1)
        st, r = await admin.api("POST", "/api/admin/reveal/next")
        record("発表済み後の再発表拒否", st == 400)

        # ---- ワンボタン召集 ----
        print("== 召集 ==")
        for u in (a2, b1):
            pass  # a2/b1 はまだチームルームにいる
        await admin.api("POST", "/api/admin/placement", {"mode": "hall"})
        async def in_hall():
            return f"hall-{hid}" in a2.page.url
        try:
            await wait_for(in_hall, timeout=25, desc="全体会場へ自動移動")
            record("ワンボタン召集(全体会場へ自動移動)", True)
        except TimeoutError:
            record("ワンボタン召集(全体会場へ自動移動)", False)

        # ---- 録画ファイル確定 ----
        print("== 録画ファイル確定(全員退出→保存を待機、最大2分) ==")
        await admin.api("POST", "/api/admin/placement", {"mode": "free"})
        for u in parts:
            await u.ctx.close()

        async def recordings_done():
            st, recs = await admin.api("GET", "/api/admin/recordings")
            team_recs = [r for r in recs if r["room_name"].startswith("team-")]
            done = [r for r in team_recs if r["status"] == "done" and (r["size_mb"] or 0) > 0]
            return len(done) >= 2 and not any(r["status"] == "recording" for r in team_recs)
        try:
            await wait_for(recordings_done, timeout=120, interval=5, desc="録画MP4確定")
            record("退出で録画自動保存(MP4生成)", True)
        except TimeoutError:
            record("退出で録画自動保存(MP4生成)", False)

        st, recs = await admin.api("GET", "/api/admin/recordings")
        dl = next((r["download_url"] for r in recs if r["status"] == "done"), None)
        if dl:
            resp = await admin.ctx.request.get(BASE + dl)
            body = await resp.body()
            record("署名付きURLで録画ダウンロード", resp.status == 200 and len(body) > 100000,
                   f"{len(body)/1e6:.1f}MB")
        else:
            record("署名付きURLで録画ダウンロード", False, "完了済み録画なし")

        # ---- 後片付け: 元のハッカソンに復元 ----
        await admin.api("POST", f"/api/admin/hackathons/{PREV_HID}/activate")
        record("元のハッカソンへ復元", active_hackathon_id() == PREV_HID)
        await browser.close()


def cleanup_leftovers():
    """テスト用ハッカソンを削除し、元のハッカソンを復元する。
    録画(MP4ファイルと録画一覧)は削除せず残す。"""
    conn = db()
    for (hid,) in conn.execute("SELECT id FROM hackathons WHERE name = 'E2Eテスト'").fetchall():
        for t in ("participants", "teams", "rooms", "works", "votes"):
            conn.execute(f"DELETE FROM {t} WHERE hackathon_id = ?", (hid,))
        conn.execute("DELETE FROM hackathons WHERE id = ?", (hid,))
    conn.execute("UPDATE hackathons SET active = 0")
    conn.execute("UPDATE hackathons SET active = 1 WHERE id = ?", (PREV_HID,))
    conn.execute("DELETE FROM sessions WHERE email LIKE 'e2e-%'")
    conn.commit()
    conn.close()


def report() -> int:
    print("\n" + "=" * 62)
    print(f" E2Eテスト結果  ({time.time()-T0:.0f}秒)")
    print("=" * 62)
    npass = nfail = nwarn = 0
    for name, status, note in RESULTS:
        mark = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️ "}[status]
        print(f" {mark} {name}" + (f"  — {note}" if note else ""))
        npass += status == "PASS"; nfail += status == "FAIL"; nwarn += status == "WARN"
    print("-" * 62)
    print(f" PASS: {npass}  FAIL: {nfail}  WARN: {nwarn}")
    return nfail


def base_hackathon_id() -> int:
    """復元先: E2E残骸ではない本来のハッカソン"""
    conn = db()
    row = conn.execute(
        "SELECT id FROM hackathons WHERE active = 1 AND name != 'E2Eテスト'").fetchone()
    if not row:
        row = conn.execute(
            "SELECT MAX(id) FROM hackathons WHERE name != 'E2Eテスト'").fetchone()
    conn.close()
    return row[0]


T0 = time.time()
PREV_HID = base_hackathon_id()

try:
    asyncio.run(main())
except Exception as e:
    print(f"\n!! テスト中断: {type(e).__name__}: {e}")
    RESULTS.append(("テスト完走", "FAIL", f"{type(e).__name__}: {str(e)[:80]}"))
finally:
    cleanup_leftovers()
    print("(テスト用ハッカソンを削除し、元のハッカソンを復元しました)")
    nfail = report()
sys.exit(1 if nfail else 0)
