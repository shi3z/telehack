#!/usr/bin/env python
"""ハッカソン完全自動デモシナリオ

生成した顔の挨拶動画をカメラ映像として持つ仮想参加者6人+主催者MCで、
ハッカソンを最初から最後まで自動進行する:

  1. 全体会場に集合して挨拶
  2. 主催者からテーマ発表(MCのカメラを全ルーム配信+お知らせ)
  3. ディスカッション (5分、チームルームで共有タイマー)
  4. 開発タイム (5分、各チームが作品URLを登録)
  5. 発表モード (各チーム2分、自動送り)
  6. クロージング

全ルームは自動録画されるので、終了後に管理画面の録画タブから
「本物のハッカソンの雰囲気」をMP4で確認できる。

実行:
  ../.venv/bin/python scenario.py --fast   # 各フェーズ約1分の短縮版
  ../.venv/bin/python scenario.py          # 本番タイム(ディスカッション/開発 各5分)

前提: gen_faces.py で assets/*.y4m を生成済みであること
"""
import asyncio
import json
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

from util import BASE, inject_session, active_hackathon_id

FAST = "--fast" in sys.argv
ASSETS = Path(__file__).resolve().parent / "assets"
SHOTS = ASSETS / "scenario-shots"
ADMIN_EMAIL = "shi3z@zelpm.com"
THEME = "AIで日常をハックせよ"

DUR = (
    {"greet": 40, "theme": 35, "discuss": 60, "dev": 60, "pres": 40}
    if FAST else
    {"greet": 60, "theme": 45, "discuss": 300, "dev": 300, "pres": 120}
)

BOTS = [  # (asset, email, 表示名, チーム)
    ("p1", "kaito@example.com",  "拓海",  "チームアルファ"),
    ("p2", "misaki@example.com", "美咲",  "チームアルファ"),
    ("p3", "kenta@example.com",  "健太",  "チームアルファ"),
    ("p4", "hina@example.com",   "陽菜",  "チームブラボー"),
    ("p5", "ren@example.com",    "蓮",    "チームブラボー"),
    ("p6", "ayaka@example.com",  "彩花",  "チームブラボー"),
]
MC = ("mc", "mc@example.com", "主催者MC")

WORKS = {
    "チームアルファ": ("AI献立ハッカー", "https://example.com/ai-kondate"),
    "チームブラボー": ("サボり検知くん", "https://example.com/saboriken"),
}


async def api(ctx, method, path, data=None):
    r = await ctx.request.fetch(BASE + path, method=method,
                                data=json.dumps(data) if data is not None else None,
                                headers={"Content-Type": "application/json"})
    try:
        return r.status, await r.json()
    except Exception:
        return r.status, None


async def launch_bot(p, asset, email):
    """1ボット=1ブラウザ(フェイクカメラ映像を個別指定するため)"""
    args = [
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        f"--use-file-for-fake-audio-capture={ASSETS/'silence.wav'}",
    ]
    y4m = ASSETS / f"{asset}.y4m"
    if y4m.exists():
        args.append(f"--use-file-for-fake-video-capture={y4m}")
    browser = await p.chromium.launch(args=args)
    ctx = await browser.new_context(viewport={"width": 1100, "height": 700},
                                    permissions=["camera", "microphone"])
    await ctx.add_cookies([
        {"name": "th_session", "value": inject_session(email, ttl=6 * 3600), "url": BASE}])
    page = await ctx.new_page()
    return browser, ctx, page


async def countdown(sec, label):
    for remain in range(sec, 0, -10):
        print(f"    … {label} 残り{remain}秒")
        await asyncio.sleep(min(10, remain))


async def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    async with async_playwright() as p:
        # ---- 管理者 ----
        admin_browser = await p.chromium.launch()
        admin = await admin_browser.new_context()
        await admin.add_cookies([
            {"name": "th_session", "value": inject_session(ADMIN_EMAIL), "url": BASE}])

        # ---- デモハッカソンを作成してアクティブ化 (過去のデモは削除、録画は残る) ----
        print("== セットアップ ==")
        await api(admin, "POST", "/api/admin/hackathons", {"name": "デモハッカソン"})
        hid = active_hackathon_id()
        _, hlist = await api(admin, "GET", "/api/admin/hackathons")
        for h in hlist or []:
            if h["name"] == "デモハッカソン" and not h["active"]:
                await api(admin, "DELETE", f"/api/admin/hackathons/{h['id']}")
        team_ids, team_rooms = {}, {}
        for tname in ("チームアルファ", "チームブラボー"):
            st, r = await api(admin, "POST", "/api/admin/teams", {"name": tname})
            team_ids[tname] = r["id"]
        _, tlist = await api(admin, "GET", "/api/admin/teams")
        for t in tlist:
            team_rooms[t["name"]] = t["room_name"]
        for asset, email, name, team in BOTS:
            await api(admin, "POST", "/api/admin/participants",
                      {"email": email, "name": name, "team_id": team_ids[team]})
        await api(admin, "POST", "/api/admin/participants",
                  {"email": MC[1], "name": MC[2], "is_admin": True})
        print(f"  ハッカソン#{hid} / 2チーム / 参加者6+MC を登録")

        # ---- 参加者ボット起動 → 全体会場へ ----
        print("== 1. 集合・挨拶 ==")
        await api(admin, "POST", "/api/admin/placement", {"mode": "hall"})
        hall = f"hall-{hid}"
        bots = []
        for asset, email, name, team in BOTS:
            b, c, pg = await launch_bot(p, asset, email)
            await pg.goto(f"{BASE}/room.html?room={hall}")
            bots.append((b, c, pg, name))
            print(f"  {name} が全体会場に入室")
            await asyncio.sleep(1.2)

        # 全体会場の録画を開始(全篇を記録)
        await asyncio.sleep(5)
        st, _ = await api(admin, "POST", f"/api/admin/rooms/{hall}/record/start")
        print(f"  全体会場の録画開始 ({'OK' if st == 200 else 'NG'})")
        await api(admin, "POST", "/api/admin/announce",
                  {"text": f"ようこそ『デモハッカソン』へ!まもなく主催者からテーマ発表です"})
        await asyncio.sleep(DUR["greet"] - 5)
        await bots[0][2].screenshot(path=str(SHOTS / "1-greeting.png"))

        # ---- 2. 主催者からテーマ発表 (MCのカメラをステージ配信) ----
        print("== 2. テーマ発表 (主催者) ==")
        mc_browser, mc_ctx, mc_page = await launch_bot(p, MC[0], MC[1])
        await mc_page.goto(f"{BASE}/stage.html")
        await mc_page.wait_for_selector("#camBtn", timeout=10000)
        await asyncio.sleep(2)
        await mc_page.click("#camBtn")
        await mc_page.click("#micBtn")
        await asyncio.sleep(3)
        await api(admin, "POST", "/api/admin/announce",
                  {"text": f"🎤 テーマ発表:『{THEME}』!制限時間内に作品を作って発表してください"})
        await asyncio.sleep(DUR["theme"])
        await bots[0][2].screenshot(path=str(SHOTS / "2-theme.png"))
        await mc_page.click("#endBtn")   # 配信終了

        # ---- 3. ディスカッション ----
        print(f"== 3. ディスカッション ({DUR['discuss']}秒) ==")
        await api(admin, "POST", "/api/admin/timer",
                  {"seconds": DUR["discuss"], "label": "ディスカッション"})
        await api(admin, "POST", "/api/admin/placement", {"mode": "teams"})
        await asyncio.sleep(15)
        await bots[0][2].screenshot(path=str(SHOTS / "3-discussion.png"))
        await countdown(DUR["discuss"] - 15, "ディスカッション")

        # ---- 4. 開発タイム ----
        print(f"== 4. 開発タイム ({DUR['dev']}秒) ==")
        await api(admin, "POST", "/api/admin/timer",
                  {"seconds": DUR["dev"], "label": "開発タイム"})
        await api(admin, "POST", "/api/admin/announce",
                  {"text": "🛠 開発タイム開始!作品ができたらロビーからURLを登録してください"})
        # 中盤で各チームが作品を登録
        await asyncio.sleep(DUR["dev"] // 2)
        for i in (0, 3):   # 各チーム代表が登録
            asset, email, name, team = BOTS[i]
            title, url = WORKS[team]
            st, _ = await api(bots[i][1], "POST", "/api/works", {"title": title, "url": url})
            print(f"  {name} が作品『{title}』を登録 ({'OK' if st == 200 else 'NG'})")
        await countdown(DUR["dev"] - DUR["dev"] // 2, "開発タイム")

        # ---- 5. 発表モード ----
        print(f"== 5. 発表モード (各チーム{DUR['pres']}秒、自動送り) ==")
        await api(admin, "DELETE", "/api/admin/timer")
        await api(admin, "POST", "/api/admin/placement", {"mode": "hall"})
        await asyncio.sleep(12)   # 全員が会場へ戻るのを待つ
        await api(admin, "POST", "/api/admin/presentation/start", {"seconds": DUR["pres"]})
        await asyncio.sleep(15)
        await bots[5][2].screenshot(path=str(SHOTS / "4-presentation.png"))
        # 発表が終わるまで待機(自動送り)
        deadline = time.time() + DUR["pres"] * 2 + 60
        while time.time() < deadline:
            st, s = await api(admin, "GET", "/api/state")
            pr = s.get("presentation") if s else None
            if not pr:
                break
            print(f"    … 発表中: {pr['current_team_name']} ({pr['index']+1}/{len(pr['order'])})")
            await asyncio.sleep(10)
        print("  全チームの発表が終了")

        # ---- 6. クロージング ----
        print("== 6. クロージング ==")
        await api(admin, "POST", "/api/admin/announce",
                  {"text": "🎉 デモハッカソン終了!お疲れさまでした。録画は管理画面からどうぞ"})
        await asyncio.sleep(8)
        await bots[0][2].screenshot(path=str(SHOTS / "5-closing.png"))
        await api(admin, "POST", f"/api/admin/rooms/{hall}/record/stop")
        await api(admin, "POST", "/api/admin/placement", {"mode": "free"})

        # ---- 退出 ----
        for b, c, pg, name in bots:
            await b.close()
        await mc_browser.close()
        await admin_browser.close()

    print()
    print("=" * 60)
    print(f" シナリオ完了 ({(time.time()-t0)/60:.1f}分)")
    print(f" ・スクリーンショット: {SHOTS}/")
    print(" ・録画: 管理画面 → 録画タブ (全体会場+各チームルーム)")
    print("   数十秒後にMP4が確定します。録画は削除されません。")
    print(" ・『デモハッカソン』がアクティブのままです。名簿コンソールで")
    print("   元のハッカソンに切り替えられます。")
    print("=" * 60)


asyncio.run(main())
