#!/usr/bin/env python3
"""bulletInWall 的耗時：每張圖在地圖範圍內取固定的一組隨機點（離地 0～3m），量每次呼叫幾奈秒。

  python tools/bullet_bench.py                  # 目前的 index.html
  python tools/bullet_bench.py --rev HEAD~1     # 某個 commit 的版本，拿來做改前改後比較

子彈每一小步（約 21cm）都呼叫一次，tools/perf.py 只量畫面那一趟看不到它，所以另外量。
"""
import argparse
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_test_copy import ROOT, make_copy, serve  # noqa: E402
from regress import THREE_URL, launch  # noqa: E402

EXTRA = "mapSelect, modeSelect, navBlocked, hasLOS, spotFree,"
BENCH_JS = r"""
(N) => {
  const D = __dbg, W = D.W(), H = D.H();
  let sd = 777;
  const R = () => (sd = (sd*1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
  const pts = new Float64Array(N*3);
  for (let i = 0; i < N; i++){ pts[i*3] = R()*W; pts[i*3+1] = R()*H; pts[i*3+2] = R()*3; }
  D.bulletInWall(1, 1, 1);                         // 第一次呼叫會建格子，不算在內
  let best = 1e9, hits = 0;
  for (let rep = 0; rep < 5; rep++){
    const t0 = performance.now(); hits = 0;
    for (let i = 0; i < N; i++) if (D.bulletInWall(pts[i*3], pts[i*3+1], pts[i*3+2])) hits++;
    best = Math.min(best, performance.now() - t0);
  }
  return {ns: best*1e6/N, hits, walls: D.WALLS().length};
}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rev", help="量某個 commit 的 index.html")
    ap.add_argument("--maps", help="逗號分隔的地圖 id（預設主選單全部）")
    ap.add_argument("--n", type=int, default=200000)
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
    with open(path, encoding="utf-8") as f:
        html = f.read().replace(EXTRA, EXTRA + " bulletInWall, W: () => W, H: () => H,", 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    srv, url = serve(d)
    with sync_playwright() as pw:
        browser = launch(pw, a)
        page = browser.new_page(viewport={"width": 640, "height": 360})
        three = a.three or os.environ.get("ECHO3D_THREE")
        if three:
            page.route(THREE_URL, lambda r: r.fulfill(path=three, content_type="application/javascript"))
            page.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(body="", content_type="text/css"))
        page.goto(url, timeout=600000)
        page.wait_for_function("() => window.__dbg && __dbg.getG()", timeout=180000)
        ids = a.maps.split(",") if a.maps else page.evaluate("() => [...__dbg.mapSelect.options].map(o => o.value)")
        print(f"{'地圖':16s} {'牆':>5s} {'ns/次':>8s} {'擋下':>7s}")
        for mid in ids:
            page.evaluate("mid => { const el = __dbg.mapSelect; el.value = mid; el.dispatchEvent(new Event('change')); }", mid)
            page.evaluate("() => { __dbg.startGame(); __dbg.overPause.hidden = false; __dbg.setLocked(true); __dbg.step(2, 1/60); }")
            name = page.evaluate("() => __dbg.activeMap().name")
            r = page.evaluate(BENCH_JS, a.n)
            print(f"{name:16s} {r['walls']:5d} {r['ns']:8.1f} {r['hits']:7d}", flush=True)
        browser.close()
    srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
