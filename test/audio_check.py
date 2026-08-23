#!/usr/bin/env python
"""音声経路のE2E検証

1. 声つきボット(話者)がルームに入室
2. 受信ボットが同室で、届いたリモート音声トラックの実音量を WebAudio で計測
3. そのルームを録画し、MP4の音量を ffmpeg volumedetect で計測

実行:  ../.venv/bin/python audio_check.py
"""
import asyncio
import json
import subprocess
import time
from pathlib import Path

from playwright.async_api import async_playwright

from util import BASE, inject_session, active_hackathon_id

ASSETS = Path(__file__).resolve().parent / "assets"
REC = Path(__file__).resolve().parent.parent / "recordings"
ADMIN = "shi3z@zelpm.com"


async def api(ctx, method, path, data=None):
    r = await ctx.request.fetch(BASE + path, method=method,
                                data=json.dumps(data) if data is not None else None,
                                headers={"Content-Type": "application/json"})
    try:
        return r.status, await r.json()
    except Exception:
        return r.status, None


async def main():
    ok = True
    async with async_playwright() as p:
        admin_b = await p.chromium.launch()
        admin = await admin_b.new_context()
        await admin.add_cookies([{"name": "th_session", "value": inject_session(ADMIN), "url": BASE}])
        hid = active_hackathon_id()
        room_name = f"hall-{hid}"

        # 話者: p1 の声を持つボット
        speaker_b = await p.chromium.launch(args=[
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
            f"--use-file-for-fake-video-capture={ASSETS/'p1.y4m'}",
            f"--use-file-for-fake-audio-capture={ASSETS/'p1.wav'}"])
        await api(admin, "POST", "/api/admin/participants",
                  {"email": "spk@example.com", "name": "話者", "is_admin": False})
        await api(admin, "POST", "/api/admin/participants",
                  {"email": "lsn@example.com", "name": "受信", "is_admin": False})
        sctx = await speaker_b.new_context(permissions=["camera", "microphone"])
        await sctx.add_cookies([{"name": "th_session", "value": inject_session("spk@example.com"), "url": BASE}])
        spg = await sctx.new_page()
        await spg.goto(f"{BASE}/room.html?room={room_name}")

        # 受信者
        listener_b = await p.chromium.launch(args=[
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
            "--autoplay-policy=no-user-gesture-required"])
        lctx = await listener_b.new_context(permissions=["camera", "microphone"])
        await lctx.add_cookies([{"name": "th_session", "value": inject_session("lsn@example.com"), "url": BASE}])
        lpg = await lctx.new_page()
        await lpg.goto(f"{BASE}/room.html?room={room_name}")
        await asyncio.sleep(8)

        # 録画開始
        await api(admin, "POST", f"/api/admin/rooms/{room_name}/record/start")

        # 受信側で音量を計測
        result = await lpg.evaluate("""async () => {
            const room = window.thRoom;
            if (!room) return {error: 'thRoom未公開'};
            const tracks = [];
            room.remoteParticipants.forEach(p =>
                p.audioTrackPublications.forEach(pub => { if (pub.track) tracks.push(pub.track.mediaStreamTrack); }));
            if (!tracks.length) return {tracks: 0};
            const ctx = new AudioContext();
            await ctx.resume();
            const src = ctx.createMediaStreamSource(new MediaStream([tracks[0]]));
            const an = ctx.createAnalyser();
            src.connect(an);
            const buf = new Float32Array(an.fftSize);
            let peak = 0;
            for (let i = 0; i < 30; i++) {
                an.getFloatTimeDomainData(buf);
                for (const v of buf) peak = Math.max(peak, Math.abs(v));
                await new Promise(r => setTimeout(r, 100));
            }
            return {tracks: tracks.length, peak};
        }""")
        print("受信側計測:", result)
        if result.get("peak", 0) > 0.01:
            print("✅ ライブ音声: 受信側に実音声が届いています (peak %.3f)" % result["peak"])
        else:
            print("❌ ライブ音声: 音が届いていません")
            ok = False

        await asyncio.sleep(15)  # 録画に音声を含める
        await api(admin, "POST", f"/api/admin/rooms/{room_name}/record/stop")
        await speaker_b.close()
        await listener_b.close()

        # 録画確定を待って音量計測
        print("録画の確定を待機中…")
        filename = None
        for _ in range(24):
            await asyncio.sleep(5)
            st, recs = await api(admin, "GET", "/api/admin/recordings")
            r0 = next((r for r in recs if r["room_name"] == room_name), None)
            if r0 and r0["status"] == "done":
                filename = r0["filename"]
                break
        if filename:
            out = subprocess.run(
                ["ffmpeg", "-i", str(REC / filename), "-af", "volumedetect", "-f", "null", "-"],
                capture_output=True, text=True).stderr
            mean = [l for l in out.splitlines() if "mean_volume" in l]
            print("録画音量:", mean[0].split("]")[-1].strip() if mean else "?")
            db = float(mean[0].split("mean_volume:")[1].split("dB")[0]) if mean else -99
            if db > -50:
                print(f"✅ 録画音声: MP4に音声が記録されています ({db} dB)")
            else:
                print(f"❌ 録画音声: MP4がほぼ無音です ({db} dB)")
                ok = False
        else:
            print("❌ 録画が確定しませんでした")
            ok = False

        # 後片付け
        await api(admin, "POST", "/api/admin/participants/bulk-delete",
                  {"emails": ["spk@example.com", "lsn@example.com"]})
        await admin_b.close()
    print("\n結果:", "✅ 音声経路はすべて正常" if ok else "❌ 問題あり")


asyncio.run(main())
