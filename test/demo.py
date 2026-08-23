#!/usr/bin/env python
"""Telehack 仮想参加者シミュレーター

一人でも全機能を試せるように、フェイクカメラ付きの仮想参加者を常駐させる。
仮想参加者は自分のチームルームに入室し、召集/送出・発表モード・お知らせ等の
指示にブラウザ上の本物の参加者と同じように反応する。

使い方:
  ../.venv/bin/python demo.py            # 6人 (2チームに分かれる)
  ../.venv/bin/python demo.py 10         # 10人
  ../.venv/bin/python demo.py 6 --works  # 各チームがデモ作品も登録する

Ctrl+C で終了(仮想参加者は名簿から削除される)
"""
import asyncio
import json
import sys

from playwright.async_api import async_playwright

from util import BASE, db, inject_session, active_hackathon_id

N = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 6
REGISTER_WORKS = "--works" in sys.argv
ADMIN_EMAIL = "shi3z@zelpm.com"
NAMES = ["さくら", "たろう", "はなこ", "けん", "ゆい", "そら", "れん", "みお", "ひろ", "あおい",
         "りく", "いち", "ふた", "みつ", "よん"]


async def api(ctx, method, path, data=None):
    r = await ctx.request.fetch(BASE + path, method=method,
                                data=json.dumps(data) if data is not None else None,
                                headers={"Content-Type": "application/json"})
    try:
        return r.status, await r.json()
    except Exception:
        return r.status, None


async def main():
    hid = active_hackathon_id()
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=[
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
        ])
        # 管理APIセッション
        admin_ctx = await browser.new_context()
        await admin_ctx.add_cookies([
            {"name": "th_session", "value": inject_session(ADMIN_EMAIL), "url": BASE}])

        # チームがなければデモチームを作る
        st, teams = await api(admin_ctx, "GET", "/api/admin/teams")
        if st != 200:
            print("管理者セッションを作れませんでした。ADMIN_EMAILS を確認してください。")
            return
        if not teams:
            for name in ("デモチームA", "デモチームB"):
                await api(admin_ctx, "POST", "/api/admin/teams", {"name": name})
            _, teams = await api(admin_ctx, "GET", "/api/admin/teams")
            print(f"チームがなかったため {len(teams)} チームを自動作成しました")

        # 仮想参加者を名簿に登録 (チームへラウンドロビン割当)
        bots = []
        for i in range(N):
            email = f"demo{i+1}@example.com"
            name = f"{NAMES[i % len(NAMES)]}(bot)"
            team = teams[i % len(teams)]
            await api(admin_ctx, "POST", "/api/admin/participants",
                      {"email": email, "name": name, "team_id": team["id"]})
            bots.append((email, name, team))

        # 各仮想参加者: チームルームに入室して常駐
        print(f"仮想参加者 {N} 人を起動中...")
        for email, name, team in bots:
            ctx = await browser.new_context(
                viewport={"width": 960, "height": 600},
                permissions=["camera", "microphone"])
            await ctx.add_cookies([
                {"name": "th_session", "value": inject_session(email, ttl=24 * 3600), "url": BASE}])
            page = await ctx.new_page()
            await page.goto(f"{BASE}/room.html?room={team['room_name']}")
            print(f"  {name} → {team['name']}")
            await asyncio.sleep(0.8)   # 接続を分散

        if REGISTER_WORKS:
            for i, team in enumerate(teams):
                member = next((b for b in bots if b[2]["id"] == team["id"]), None)
                if member:
                    ctx = await browser.new_context()
                    await ctx.add_cookies([
                        {"name": "th_session", "value": inject_session(member[0]), "url": BASE}])
                    await api(ctx, "POST", "/api/works",
                              {"title": f"{team['name']}の作品", "url": f"https://example.com/demo{i}"})
                    await ctx.close()
            print(f"各チームのデモ作品を登録しました")

        print()
        print("=" * 60)
        print(f" 仮想参加者 {N} 人が各チームルームで稼働中です。")
        print(f" ブラウザで {BASE.replace('http://localhost:8800', 'https://219-104-122-252.sslip.io')} の管理画面から")
        print("  ・📢 お知らせ送信 → 全員の画面にトースト表示")
        print("  ・🚪 召集/送出 → 全員が自動移動")
        print("  ・⏱ タイマー / 🎤 発表モード / 🏆 結果発表")
        print(" などを操作して動作を確認してください。")
        print(" 終了: Ctrl+C (仮想参加者は名簿から削除されます)")
        print("=" * 60)

        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            print("\n後片付け中...")
            emails = [b[0] for b in bots]
            await api(admin_ctx, "POST", "/api/admin/participants/bulk-delete", {"emails": emails})
            conn = db()
            conn.execute(f"DELETE FROM works WHERE hackathon_id = ? AND url LIKE 'https://example.com/demo%'", (hid,))
            conn.commit(); conn.close()
            await browser.close()
            print("仮想参加者を削除しました")


try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
