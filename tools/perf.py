#!/usr/bin/env python3
"""STONEGATE 每幀耗時：VALORANT 死鬥（5 隻敵人在場，太陽陰影每幀重算），1920x1080，固定幾個位置各量幾幀。

  python tools/perf.py                          # 目前的 index.html
  python tools/perf.py --rev HEAD~3             # 某個 commit 的版本（git show 出來量），拿來做改前改後比較
  python tools/perf.py --gpu                    # Leo 的 Windows：本機 Chrome + d3d11（預設 Chromium + SwiftShader）
  python tools/perf.py --three PATH             # 連不到 cdnjs 時用本機的 three.min.js r147

每個位置印出：
  total  畫一幀加等 GPU 畫完（readPixels 同步）的中位數，ms
  cpu    只算 JS 與送出指令的時間（計時前先同步一次，不含等 GPU），ms
  calls / tris  這一幀（含陰影那一趟）的 draw call 與三角形數
SwiftShader 是 CPU 上的軟體算繪，total 比實機慢上百倍、而且三角形與像素的比重偏高，只能拿來比相對變化；
實機的數字要用 --gpu 在 Leo 的電腦上量。
"""
import argparse
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_test_copy import ROOT, make_copy, serve  # noqa: E402
from regress import THREE_URL, launch  # noqa: E402

PI = 3.14159265
# (名稱, x, y, 朝向, 俯仰)：像素座標，朝向 0 是東、-π/2 是北
VIEWS = [
    ("atk-spawn", 1200, 2300, -PI / 2, 0), ("a-main", 300, 1950, -PI / 2, 0), ("a-site", 900, 1050, -2.4, 0),
    ("mid", 1300, 1450, -0.9, 0), ("b-site", 2700, 1150, -2.3, 0), ("b-main", 2400, 1950, -PI / 2 - 0.3, 0),
    ("def-spawn", 1500, 250, PI / 2, 0), ("street-up", 1150, 1850, -PI / 2, 0.35),
]
EXTRA = "mapSelect, modeSelect, navBlocked, hasLOS, spotFree,"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rev", help="量某個 commit 的 index.html")
    ap.add_argument("--frames", type=int, default=6)
    ap.add_argument("--gpu", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--executable")
    ap.add_argument("--three")
    a = ap.parse_args()
    from playwright.sync_api import sync_playwright
    src = None
    if a.rev:
        html = subprocess.run(["git", "show", f"{a.rev}:index.html"], cwd=ROOT, capture_output=True, check=True).stdout
        fd, src = tempfile.mkstemp(suffix=".html")
        os.write(fd, html)
        os.close(fd)
    d, path = make_copy(src=src)
    # 量測要直接呼叫 renderPosted 與 renderer：掛在 __dbg 上
    with open(path, encoding="utf-8") as f:
        html = f.read().replace(EXTRA, EXTRA + " renderer, renderPosted, drawUI,", 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    srv, url = serve(d)
    with sync_playwright() as pw:
        browser = launch(pw, a)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        three = a.three or os.environ.get("ECHO3D_THREE")
        if three:
            page.route(THREE_URL, lambda r: r.fulfill(path=three, content_type="application/javascript"))
            page.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(body="", content_type="text/css"))
        page.goto(url, timeout=600000)
        page.wait_for_function("() => window.__dbg && __dbg.getG()", timeout=600000)
        page.evaluate("() => document.querySelector(`.rules button[data-rules='valorant']`).click()")
        for w, v in (("modeSelect", "survival"), ("mapSelect", "stonegate")):
            page.evaluate("([w, v]) => { const el = __dbg[w]; el.value = v; el.dispatchEvent(new Event('change')); }", [w, v])
        page.evaluate("() => { __dbg.startGame(); __dbg.overPause.hidden = false; __dbg.setLocked(true); __dbg.getG().player.iframe = 1e9; __dbg.step(20, 1/60); }")
        print(f"{'位置':12s} {'total':>8s} {'cpu':>6s} {'calls':>6s} {'tris':>8s}")
        for n, x, y, ang, pit in VIEWS:
            page.evaluate("""v => { const p = __dbg.getG().player; p.x = v[0]; p.y = v[1]; p.vx = p.vy = 0;
                __dbg.setYaw(__dbg.yawOf(v[2])); __dbg.setPitch(v[3]); __dbg.step(3, 1/60); }""", [x, y, ang, pit])
            r = page.evaluate("""N => { const R = __dbg.renderer, gl = R.getContext(), px = new Uint8Array(4);
                const sync = () => gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
                const draw = () => { R.shadowMap.needsUpdate = true; __dbg.renderPosted(); __dbg.drawUI(); };
                draw(); sync(); draw(); sync();
                const tot = [], cpu = [];
                for (let i=0;i<N;i++){ const t0 = performance.now(); draw(); sync(); tot.push(performance.now() - t0); }
                for (let i=0;i<N;i++){ sync(); const t0 = performance.now(); draw(); cpu.push(performance.now() - t0); }
                sync();
                R.info.autoReset = false; R.info.reset(); draw(); const calls = R.info.render.calls, tris = R.info.render.triangles; R.info.autoReset = true;
                const med = a => a.sort((p, q) => p - q)[a.length >> 1];
                return {total:med(tot), cpu:med(cpu), calls, tris}; }""", a.frames)
            print(f"{n:12s} {r['total']:8.1f} {r['cpu']:6.1f} {r['calls']:6d} {r['tris']:8d}", flush=True)
        browser.close()
    if errs:
        print("pageerror:", errs[:3])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
