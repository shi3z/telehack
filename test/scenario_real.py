#!/usr/bin/env python
"""リアル・ハッカソン完全自動シナリオ (実アプリ付き)

3チーム×2名のAI参加者+主催者MCが、実際に動く3つの作品アプリとともに
ハッカソンを開会から表彰式まで完全自動で進行する。

■ 進行台本
  0. 開場・集合      全員が全体会場へ。挨拶(全篇録画開始)
  1. 開会式         MCがステージ登壇しテーマ発表『AIで日常をハックせよ』
  2. チーム作戦会議   チームルームへ送出、自己紹介・役割分担
  3. アイデアソン     共有タイマーでディスカッション(中間アナウンスあり)
  4. 開発スプリント   タイマー、作品URL提出、締切アナウンス
  5. 試遊会         各チームが他チームの作品を実際に操作(画面+顔を自動録画)
  6. 成果発表会      全員召集、発表モード(ランダム順・自動送り)
  7. 投票タイム      全員が他チームの作品へ投票
  8. 表彰式         3位→2位→1位をドラムロールで発表(MC再登壇)
  9. 閉会

■ 作品 (server/static/apps/ に実装済みの本物のWebアプリ)
  チームアルファ   : AI献立ハッカー   /apps/ai-kondate/
  チームブラボー   : サボり検知くん   /apps/sabori/
  チームチャーリー : ほめ日記AI      /apps/homenikki/

実行:
  ../.venv/bin/python scenario_real.py --fast   # 短縮版(約8分)
  ../.venv/bin/python scenario_real.py          # 本番(約20分)
"""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

from util import BASE, inject_session, active_hackathon_id

FAST = "--fast" in sys.argv
ASSETS = Path(__file__).resolve().parent / "assets"
SHOTS = ASSETS / "scenario-real-shots"
ADMIN_EMAIL = "shi3z@zelpm.com"
PUBLIC = "https://219-104-122-252.sslip.io"
THEME = "AIで日常をハックせよ"

D = (
    {"gather": 25, "opening": 25, "strategy": 30, "ideathon": 45, "dev": 50,
     "pres": 35, "vote": 20, "reveal": 13, "closing": 12}
    if FAST else
    {"gather": 40, "opening": 45, "strategy": 60, "ideathon": 300, "dev": 300,
     "pres": 90, "vote": 40, "reveal": 15, "closing": 15}
)

TEAMS = ["チームアルファ", "チームブラボー", "チームチャーリー"]
BOTS = [  # (asset, email, 表示名, チーム)
    ("p1", "kaito@example.com",  "拓海", "チームアルファ"),
    ("p2", "misaki@example.com", "美咲", "チームアルファ"),
    ("p3", "kenta@example.com",  "健太", "チームブラボー"),
    ("p4", "hina@example.com",   "陽菜", "チームブラボー"),
    ("p5", "ren@example.com",    "蓮",   "チームチャーリー"),
    ("p6", "ayaka@example.com",  "彩花", "チームチャーリー"),
]
MC = ("mc", "mc@example.com", "主催者MC")

WORKS = {
    "チームアルファ":   ("AI献立ハッカー", f"{PUBLIC}/apps/ai-kondate/"),
    "チームブラボー":   ("サボり検知くん", f"{PUBLIC}/apps/sabori/"),
    "チームチャーリー": ("ほめ日記AI",    f"{PUBLIC}/apps/homenikki/"),
}
# 投票先 (アルファ3票 / ブラボー2票 / チャーリー1票 → 明確な順位がつく)
VOTES = {"拓海": "チームブラボー", "美咲": "チームチャーリー",
         "健太": "チームアルファ", "陽菜": "チームアルファ",
         "蓮": "チームアルファ", "彩花": "チームブラボー"}


async def api(ctx, method, path, data=None):
    r = await ctx.request.fetch(BASE + path, method=method,
                                data=json.dumps(data) if data is not None else None,
                                headers={"Content-Type": "application/json"})
    try:
        return r.status, await r.json()
    except Exception:
        return r.status, None


async def announce(admin, text):
    await api(admin, "POST", "/api/admin/announce", {"text": text})
    print(f"  📢 {text}")


class Bot:
    def __init__(self, asset, email, name, team):
        self.asset, self.email, self.name, self.team = asset, email, name, team
        self.browser = self.ctx = self.page = None

    async def launch(self, p):
        # 声: 生成動画から抽出した本人の音声をループ再生(なければ無音)
        voice = ASSETS / f"{self.asset}.wav"
        audio = voice if voice.exists() else ASSETS / "silence.wav"
        args = [
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
            "--auto-select-desktop-capture-source=Entire screen",
            f"--use-file-for-fake-audio-capture={audio}",
        ]
        y4m = ASSETS / f"{self.asset}.y4m"
        if y4m.exists():
            args.append(f"--use-file-for-fake-video-capture={y4m}")
        self.browser = await p.chromium.launch(args=args)
        self.ctx = await self.browser.new_context(
            viewport={"width": 1100, "height": 700}, permissions=["camera", "microphone"])
        await self.ctx.add_cookies([
            {"name": "th_session", "value": inject_session(self.email, ttl=6 * 3600), "url": BASE}])
        self.page = await self.ctx.new_page()

    async def goto_room(self, room):
        await self.page.goto(f"{BASE}/room.html?room={room}")


def ensure_xvfb():
    """実画面キャプチャ用の仮想ディスプレイを起動する"""
    if not Path("/tmp/.X99-lock").exists():
        subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1280x800x24"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)


async def playtest(p, bot: Bot, work_id: int, actions, seconds: int):
    """他チームの作品を play.html で実際に操作する(実画面+顔が自動録画される)

    ヘッドレスでは画面キャプチャがテストパターンになるため、
    Xvfb 上のヘッドありブラウザで本物の画面を録る。
    """
    ensure_xvfb()
    voice = ASSETS / f"{bot.asset}.wav"
    args = [
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={voice if voice.exists() else ASSETS/'silence.wav'}",
        "--auto-select-desktop-capture-source=Entire screen",
        "--window-size=1280,800", "--window-position=0,0",
        "--disable-features=Translate", "--lang=ja",
    ]
    y4m = ASSETS / f"{bot.asset}.y4m"
    if y4m.exists():
        args.append(f"--use-file-for-fake-video-capture={y4m}")
    browser = await p.chromium.launch(headless=False, env={"DISPLAY": ":99"}, args=args)
    try:
        ctx = await browser.new_context(viewport={"width": 1260, "height": 740},
                                        permissions=["camera", "microphone"])
        await ctx.add_cookies([
            {"name": "th_session", "value": inject_session(bot.email), "url": BASE}])
        pg = await ctx.new_page()
        await pg.goto(f"{BASE}/play.html?work={work_id}&capture=screen")
        await pg.wait_for_selector("#startBtn", timeout=10000)
        await pg.click("#startBtn")
        try:
            await pg.wait_for_selector("#recBadge:visible", timeout=15000)
        except Exception:
            print(f"  ⚠ {bot.name} のプレイ録画が開始できませんでした")
            return
        frame = pg.frame_locator("#gameFrame")
        try:
            await actions(frame, pg)
        except Exception as e:
            print(f"  ⚠ {bot.name} のアプリ操作でエラー: {str(e)[:80]}")
        await asyncio.sleep(max(3, seconds - 20))
        try:
            await pg.evaluate("() => window.playRoom && window.playRoom.disconnect()")
        except Exception:
            pass
        await asyncio.sleep(1)
    finally:
        await browser.close()
    print(f"  🎮 {bot.name} の試遊おわり(実画面を録画済み)")


# --- 各アプリのリアルな操作スクリプト ---
async def play_kondate(f, pg):
    await f.locator("#ing").fill("卵, キャベツ, 豚肉")
    await asyncio.sleep(2)
    await f.locator("#go").click()
    await asyncio.sleep(4)
    await f.locator(".chip").first.click()
    await asyncio.sleep(1)
    await f.locator("#go").click()


async def play_sabori(f, pg):
    await f.locator("#btn").click()          # 集中開始
    await asyncio.sleep(6)
    await f.locator("body").evaluate("() => window.dispatchEvent(new Event('blur'))")  # サボり発生!
    await asyncio.sleep(5)
    await f.locator("#btn").click()          # 休憩


async def play_homenikki(f, pg):
    await f.locator("#diary").fill("ハッカソンでバグが直せなかったけど、チームのみんなと最後まで粘って作品を完成させた!")
    await asyncio.sleep(2)
    await f.locator("button").first.click()
    await asyncio.sleep(4)
    await f.locator("#diary").fill("明日は発表。緊張するけど頑張る")
    await f.locator("button").first.click()


PLAYTESTS = [  # (プレイする人のindex, 遊ぶ作品のチーム, 操作)
    (2, "チームアルファ",   play_kondate),   # 健太 → AI献立ハッカー
    (4, "チームブラボー",   play_sabori),    # 蓮 → サボり検知くん
    (1, "チームチャーリー", play_homenikki), # 美咲 → ほめ日記AI
]


async def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    async with async_playwright() as p:
        admin_browser = await p.chromium.launch()
        admin = await admin_browser.new_context()
        await admin.add_cookies([
            {"name": "th_session", "value": inject_session(ADMIN_EMAIL), "url": BASE}])

        # ---- セットアップ ----
        print("== セットアップ ==")
        await api(admin, "POST", "/api/admin/hackathons", {"name": "デモハッカソン"})
        hid = active_hackathon_id()
        _, hlist = await api(admin, "GET", "/api/admin/hackathons")
        for h in hlist or []:
            if h["name"] == "デモハッカソン" and not h["active"]:
                await api(admin, "DELETE", f"/api/admin/hackathons/{h['id']}")
        team_ids = {}
        for tname in TEAMS:
            st, r = await api(admin, "POST", "/api/admin/teams", {"name": tname})
            team_ids[tname] = r["id"]
        _, tlist = await api(admin, "GET", "/api/admin/teams")
        team_rooms = {t["name"]: t["room_name"] for t in tlist}
        bots = [Bot(*b) for b in BOTS]
        for b in bots:
            await api(admin, "POST", "/api/admin/participants",
                      {"email": b.email, "name": b.name, "team_id": team_ids[b.team]})
        await api(admin, "POST", "/api/admin/participants",
                  {"email": MC[1], "name": MC[2], "is_admin": True})
        print(f"  ハッカソン#{hid}: 3チーム6名+MC登録、作品アプリ3本は /apps/ に配備済み")

        # ---- 0. 開場・集合 ----
        print("== 0. 開場・集合 ==")
        await api(admin, "POST", "/api/admin/placement", {"mode": "hall"})
        hall = f"hall-{hid}"
        for b in bots:
            await b.launch(p)
            await b.goto_room(hall)
            print(f"  {b.name}({b.team}) が入場")
            await asyncio.sleep(1.0)
        await asyncio.sleep(5)
        await api(admin, "POST", f"/api/admin/rooms/{hall}/record/start")
        await announce(admin, "ようこそ『デモハッカソン』へ!まもなく開会式がはじまります")
        await asyncio.sleep(D["gather"])
        await bots[0].page.screenshot(path=str(SHOTS / "0-gather.png"))

        # ---- 1. 開会式 (MCステージ登壇・テーマ発表) ----
        print("== 1. 開会式 ==")
        mc = Bot(MC[0], MC[1], MC[2], None)
        await mc.launch(p)
        await mc.page.goto(f"{BASE}/stage.html")
        await mc.page.wait_for_selector("#camBtn", timeout=10000)
        await asyncio.sleep(2)
        await mc.page.click("#camBtn")
        await mc.page.click("#micBtn")
        await asyncio.sleep(3)
        await announce(admin, f"🎤 開会式:テーマ発表 —『{THEME}』!")
        await asyncio.sleep(4)
        await announce(admin, "本日の流れ: 作戦会議 → アイデアソン → 開発 → 試遊会 → 発表 → 表彰式")
        await asyncio.sleep(D["opening"])
        await bots[0].page.screenshot(path=str(SHOTS / "1-opening.png"))
        await mc.page.click("#endBtn")

        # ---- 2. チーム作戦会議 ----
        print("== 2. チーム作戦会議 ==")
        await api(admin, "POST", "/api/admin/timer",
                  {"seconds": D["strategy"] + D["ideathon"], "label": "作戦会議+アイデアソン"})
        await api(admin, "POST", "/api/admin/placement", {"mode": "teams"})
        await announce(admin, "チームルームで自己紹介と役割分担をどうぞ!")
        await asyncio.sleep(D["strategy"])
        await bots[2].page.screenshot(path=str(SHOTS / "2-strategy.png"))

        # ---- 3. アイデアソン ----
        print("== 3. アイデアソン ==")
        await announce(admin, "💡 アイデアソン開始!テーマに沿ってアイデアを出し切りましょう")
        await asyncio.sleep(D["ideathon"] / 2)
        await announce(admin, "⏳ アイデアソン残り半分!そろそろ方向性を固めましょう")
        await asyncio.sleep(D["ideathon"] / 2)
        await bots[4].page.screenshot(path=str(SHOTS / "3-ideathon.png"))

        # ---- 4. 開発スプリント ----
        print("== 4. 開発スプリント ==")
        await api(admin, "POST", "/api/admin/timer", {"seconds": D["dev"], "label": "開発スプリント"})
        await announce(admin, "🛠 開発スプリント開始!完成したらロビーから作品URLを提出してください")
        await asyncio.sleep(D["dev"] * 0.4)
        for b in (bots[0], bots[2], bots[4]):   # 各チーム代表が提出
            title, url = WORKS[b.team]
            st, _ = await api(b.ctx, "POST", "/api/works", {"title": title, "url": url})
            await announce(admin, f"✅ {b.team} が作品『{title}』を提出しました!")
            await asyncio.sleep(2)
        await asyncio.sleep(D["dev"] * 0.4)
        await announce(admin, "🚨 まもなく開発締切です!最終コミットを!")
        await asyncio.sleep(D["dev"] * 0.2)
        await announce(admin, "🔔 開発終了!お疲れさまでした")
        await bots[0].page.screenshot(path=str(SHOTS / "4-dev.png"))

        # ---- 5. 試遊会 ----
        print("== 5. 試遊会 ==")
        await api(admin, "DELETE", "/api/admin/timer")
        await announce(admin, "🎮 試遊会!他チームの作品を実際に遊んでみましょう(プレイの様子は録画されます)")
        st, works_list = await api(admin, "GET", "/api/admin/works")
        wid = {w["team_name"]: w["id"] for w in works_list}
        for idx, team, actions in PLAYTESTS:
            await playtest(p, bots[idx], wid[team], actions, 40 if not FAST else 30)
        await asyncio.sleep(2)

        # ---- 6. 成果発表会 ----
        print("== 6. 成果発表会 ==")
        await api(admin, "POST", "/api/admin/placement", {"mode": "hall"})
        await announce(admin, "📣 全員全体会場へ!成果発表会をはじめます")
        await asyncio.sleep(12)
        await api(admin, "POST", "/api/admin/presentation/start", {"seconds": D["pres"]})
        await asyncio.sleep(15)
        await bots[5].page.screenshot(path=str(SHOTS / "5-present.png"))
        deadline = time.time() + D["pres"] * 4 + 90
        while time.time() < deadline:
            st, s = await api(admin, "GET", "/api/state")
            pr = (s or {}).get("presentation")
            if not pr:
                break
            print(f"  🎤 発表中: {pr['current_team_name']} ({pr['index']+1}/{len(pr['order'])})")
            await asyncio.sleep(10)
        print("  全チームの発表終了")

        # ---- 7. 投票タイム ----
        print("== 7. 投票タイム ==")
        await api(admin, "POST", "/api/admin/works-settings", {"voting_open": True})
        await asyncio.sleep(5)
        for b in bots:
            target = VOTES[b.name]
            st, _ = await api(b.ctx, "POST", f"/api/works/{wid[target]}/vote")
            print(f"  🗳 {b.name} → {target} ({'OK' if st == 200 else 'NG'})")
            await asyncio.sleep(1)
        await asyncio.sleep(D["vote"])
        await api(admin, "POST", "/api/admin/works-settings", {"voting_open": False})
        await announce(admin, "投票締切!集計に入ります…")

        # ---- 8. 表彰式 ----
        print("== 8. 表彰式 ==")
        await mc.page.goto(f"{BASE}/stage.html")
        await mc.page.wait_for_selector("#camBtn", timeout=10000)
        await asyncio.sleep(1)
        await mc.page.click("#camBtn")
        await asyncio.sleep(2)
        await announce(admin, "🏆 表彰式!結果発表です。ドラムロール!")
        for i in range(3):
            await asyncio.sleep(3)
            st, r = await api(admin, "POST", "/api/admin/reveal/next")
            if st == 200:
                print(f"  🥁 第{r['rank']}位 発表!")
            await asyncio.sleep(D["reveal"])
            if i == 1:
                await bots[0].page.screenshot(path=str(SHOTS / "6-awards.png"))
        await mc.page.click("#endBtn")

        # ---- 9. 閉会 ----
        print("== 9. 閉会 ==")
        await announce(admin, "🎉 デモハッカソン閉会!全記録は録画タブからご覧いただけます。お疲れさまでした!")
        await asyncio.sleep(D["closing"])
        await bots[0].page.screenshot(path=str(SHOTS / "7-closing.png"))
        await api(admin, "POST", f"/api/admin/rooms/{hall}/record/stop")
        await api(admin, "POST", "/api/admin/placement", {"mode": "free"})

        for b in bots:
            await b.browser.close()
        await mc.browser.close()
        await admin_browser.close()

    print()
    print("=" * 62)
    print(f" リアルシナリオ完了 ({(time.time()-t0)/60:.1f}分)")
    print(f" ・作品アプリ: {PUBLIC}/apps/ai-kondate/ ほか3本(実際に遊べます)")
    print(f" ・スクリーンショット: {SHOTS}/")
    print(" ・録画: 全体会場(全篇) + 各チームルーム + 試遊プレイ3本")
    print(" ・結果: 1位 チームアルファ『AI献立ハッカー』(3票)")
    print("=" * 62)


if __name__ == "__main__":
    asyncio.run(main())
