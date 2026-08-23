#!/usr/bin/env python
"""仮想参加者の顔と挨拶動画を生成する

1. klein (FLUX.2) でWebカメラ風の参加者ポートレートを生成
2. oracle に顔をアップロードし、挨拶している動画を生成
3. ffmpeg で Chromium フェイクカメラ用の y4m に変換 → assets/ に保存

実行:  ../.venv/bin/python gen_faces.py
"""
import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

KLEIN = "http://100.104.204.50:8020"
ORACLE = "http://tsuginosuke:8878"
ASSETS = Path(__file__).resolve().parent / "assets"

PEOPLE = [
    ("p1", "webcam view of a young japanese man in his 20s wearing a hoodie, sitting at a desk with a laptop, hackathon venue with posters in the background, natural indoor lighting, looking at the camera, photorealistic, casual"),
    ("p2", "webcam view of a japanese woman in her 20s with glasses and a ponytail, sitting at a desk with an energy drink and laptop, hackathon venue background, looking at the camera, photorealistic"),
    ("p3", "webcam view of a japanese man in his 30s with a beard wearing a black t-shirt, headphones around his neck, sitting at a cluttered desk, hackathon venue background, looking at the camera, photorealistic"),
    ("p4", "webcam view of a young japanese woman with short hair wearing a denim jacket, sitting at a desk with sticky notes on the wall behind, hackathon venue, looking at the camera, photorealistic"),
    ("p5", "webcam view of a japanese man in his 20s with dyed silver hair wearing a parka, mechanical keyboard on the desk, hackathon venue background, looking at the camera, photorealistic"),
    ("p6", "webcam view of a japanese woman in her 30s in a casual shirt, holding a coffee mug, whiteboard with diagrams behind her, hackathon venue, looking at the camera, photorealistic"),
    ("mc", "webcam view of an energetic japanese man in his 40s wearing a staff t-shirt and lanyard, standing in front of a hackathon stage banner, smiling at the camera, event organizer, photorealistic"),
]

GREETING = "この人物がWebカメラに向かって笑顔で軽く手を振って挨拶し、そのあと自然にうなずきながら話している。カメラは固定、背景はそのまま、Webカメラ映像風"


def jpost(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def jget(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())


def fetch(url) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def gen_faces():
    ASSETS.mkdir(exist_ok=True)
    tasks = {}
    for name, prompt in PEOPLE:
        if (ASSETS / f"{name}.png").exists():
            print(f"  {name}.png は生成済み、スキップ")
            continue
        r = jpost(f"{KLEIN}/generate", {
            "prompt": prompt, "model": "klein",
            "width": 640, "height": 480,
            "num_inference_steps": 4, "seed": hash(name) % 10**9,
        })
        tasks[name] = r["task_id"]
        print(f"  {name}: 画像生成キュー投入 ({r['task_id'][:8]})")
    while tasks:
        time.sleep(3)
        for name, tid in list(tasks.items()):
            s = jget(f"{KLEIN}/status/{tid}")
            if s["status"] == "completed":
                (ASSETS / f"{name}.png").write_bytes(fetch(f"{KLEIN}/download/{tid}"))
                print(f"  ✅ {name}.png 完成")
                del tasks[name]
            elif s["status"] == "failed":
                print(f"  ❌ {name} 失敗: {s.get('error', '')[:100]}")
                del tasks[name]


def gen_videos():
    """顔をoracleにアップロードし、挨拶動画を生成する"""
    jobs = {}
    for name, _ in PEOPLE:
        if (ASSETS / f"{name}.mp4").exists():
            print(f"  {name}.mp4 は生成済み、スキップ")
            continue
        img = ASSETS / f"{name}.png"
        if not img.exists():
            continue
        ref_name = f"telehack_{name}.png"
        jpost(f"{ORACLE}/api/refs/upload",
              {"name": ref_name, "data_b64": base64.b64encode(img.read_bytes()).decode()})
        r = jpost(f"{ORACLE}/api/h3/nlgen", {
            "text": GREETING, "n": 1, "repeat": 1,
            "mode": "raw", "refs": [ref_name],
            "ref_videos": None, "ref_audios": None,
        })
        cut = r["cuts"][0]["cut"]
        jobs[name] = cut
        print(f"  {name}: 動画生成キュー投入 (cut {cut})")

    deadline = time.time() + 30 * 60
    while jobs and time.time() < deadline:
        time.sleep(20)
        try:
            prog = jget(f"{ORACLE}/api/h3/progress")
        except Exception:
            continue
        st = {c["id"]: c for c in prog["cuts"]}
        for name, cut in list(jobs.items()):
            c = st.get(cut) or st.get(str(cut))
            if not c:
                continue
            if c["status"] == "done":
                info = jget(f"{ORACLE}/api/h3/retake_info/{cut}")
                takes = info.get("takes") or {}
                if not takes:
                    continue
                kf = list(takes.keys())[0]
                t = sorted(takes[kf])[-1]
                pad = str(cut).zfill(4)
                data = fetch(f"{ORACLE}/media/cut{pad}/kf{kf}_t{t}.mp4")
                (ASSETS / f"{name}.mp4").write_bytes(data)
                print(f"  ✅ {name}.mp4 完成 ({len(data)/1e6:.1f}MB)")
                del jobs[name]
            else:
                print(f"  … {name}: {c['status']}")
    for name in jobs:
        print(f"  ⚠ {name} は時間内に完成しませんでした")


def to_y4m():
    """Chromium の --use-file-for-fake-video-capture 用に y4m へ変換"""
    for name, _ in PEOPLE:
        mp4, y4m = ASSETS / f"{name}.mp4", ASSETS / f"{name}.y4m"
        if not mp4.exists() or y4m.exists():
            continue
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4),
             "-vf", "scale=640:480:force_original_aspect_ratio=decrease,pad=640:480:-1:-1,fps=15",
             "-pix_fmt", "yuv420p", str(y4m)],
            check=True)
        print(f"  ✅ {name}.y4m ({y4m.stat().st_size/1e6:.0f}MB)")


if __name__ == "__main__":
    print("== 1/3 顔画像を生成 (klein) ==")
    gen_faces()
    print("== 2/3 挨拶動画を生成 (oracle) ==")
    gen_videos()
    print("== 3/3 y4m 変換 ==")
    to_y4m()
    print("完了:", sorted(p.name for p in ASSETS.glob('*.y4m')))
