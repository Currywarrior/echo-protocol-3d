#!/usr/bin/env python3
"""一行指令跑完整回歸：python tools/regress.py

需要 Python 版 playwright（pip install playwright；沒有瀏覽器就 python -m playwright install chromium）。
全部用固定步長推進（__dbg.step），不依賴真實幀率，GPU 忙或用軟體算繪時結果一樣。
任何一項失敗、或頁面出現 pageerror，結束碼就不是 0。

檢查項目：
  maps        每一張地圖（主選單的地圖清單全部）都能載入、APEX 生存開局、VALORANT 死鬥開局 5 隻敵人
  val-guns    VALORANT 訓練場：19 把槍逐把買、開火、換彈
  apex-guns   APEX 訓練場：三把槍開火
  dm          VALORANT 死鬥：開局 5 隻、擊殺後 1.5 秒補回、玩家倒地 1.5 秒後重生
  range-test  VALORANT 訓練場的射擊測驗：能開始、30 隻打完後結束
另外 --shots DIR 會在每張地圖與 --shot-map 的幾個位置拍截圖。

選項：
  --only maps,dm     只跑指定項目
  --shots DIR        截圖存到 DIR
  --shot-map ID      多拍幾個位置的地圖（預設 stonegate）
  --dm-map ID        死鬥測試用的地圖（預設 stonegate）
  --gpu              用本機 Chrome 與 GPU（Windows：channel="chrome"、--use-angle=d3d11）；預設是 Chromium + SwiftShader
  --executable PATH  指定瀏覽器執行檔
  --three PATH       連不到 cdnjs 時，用本機的 three.min.js（r147，例如 npm pack three@0.147.0 解出來的 build/three.min.js）
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_test_copy import make_copy, serve  # noqa: E402

TESTS = ["maps", "val-guns", "apex-guns", "dm", "range-test"]
DT = 1 / 60
THREE_URL = "https://cdnjs.cloudflare.com/ajax/libs/three.js/0.147.0/three.min.js"
# --shot-map 多拍的位置（像素座標；angle 0 朝 +x（東）、-π/2 朝北）。新地圖要多拍就在這裡加
PI = 3.14159265
SHOT_VIEWS = {
    "stonegate": [
        {"name": "atk-spawn", "x": 1200, "y": 2300, "angle": -PI / 2},
        {"name": "a-main", "x": 300, "y": 1950, "angle": -PI / 2},
        {"name": "a-site", "x": 900, "y": 1050, "angle": -2.4},
        {"name": "mid", "x": 1300, "y": 1450, "angle": -0.9},
        {"name": "b-site", "x": 2700, "y": 1150, "angle": -2.3},
        {"name": "def-spawn", "x": 1500, "y": 250, "angle": PI / 2},
    ],
}


class Run:
    def __init__(self, page):
        self.page = page
        self.results = []
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))

    def js(self, code, arg=None):
        return self.page.evaluate(code, arg)

    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok), detail))
        print(("  PASS " if ok else "  FAIL ") + name + (" · " + detail if detail else ""), flush=True)
        return ok

    # ── 操作 ──
    def rules(self, r):
        self.js("r => { const b = document.querySelector(`.rules button[data-rules='${r}']`); b.click(); }", r)

    def select(self, which, value):
        self.js("""([w, v]) => { const el = __dbg[w]; el.value = v; el.dispatchEvent(new Event('change')); }""", [which, value])

    def start(self, rules, mode, map_id):
        """選玩法、模式、地圖後開始；立刻暫停 rAF 迴圈，之後只用 step 推進"""
        self.rules(rules)
        self.select("modeSelect", mode)
        self.select("mapSelect", map_id)
        self.js("""() => { __dbg.startGame(); __dbg.overPause.hidden = false; __dbg.setLocked(true); }""")

    def step(self, seconds):
        n = max(1, round(seconds / DT))
        self.js("n => __dbg.step(n, 1/60)", n)

    def map_ids(self):
        return self.js("() => [...__dbg.mapSelect.options].map(o => o.value)")


def t_maps(run, a):
    for mid in run.map_ids():
        errs = len(run.errors)
        run.start("apex", "survival", mid)
        run.step(0.5)
        info = run.js("""() => { const G = __dbg.getG(), m = __dbg.activeMap();
            return {name:m.name, walls:__dbg.WALLS().length, want:__dbg.MAPS[__dbg.mapSelect.value] === m}; }""")
        run.check(f"maps/{mid} apex 載入", info["want"] and info["walls"] > 0 and len(run.errors) == errs,
                  f"{info['name']} · {info['walls']} 面牆")
        run.start("valorant", "survival", mid)
        run.step(0.2)
        n = run.js("() => __dbg.getG().bots.length")
        run.check(f"maps/{mid} 死鬥開局 5 隻", n == 5 and len(run.errors) == errs, f"{n} 隻")


def t_val_guns(run, a):
    run.start("valorant", "range", "atrium")
    run.js("() => { __dbg.RANGE.infinite = 0; }")
    keys = run.js("() => Object.keys(__dbg.GUNS).filter(k => __dbg.GUNS[k].val && !__dbg.GUNS[k].val.melee)")
    run.check("val-guns 槍數", len(keys) == 19, f"{len(keys)} 把")
    for k in keys:
        run.js("k => __dbg.valBuyGun(k)", k)
        run.step(2.0)
        r = run.js("""k => { const G = __dbg.getG(), p = G.player;
            const s0 = G.shots, m0 = p.mags[k];
            __dbg.keys.add('Mouse0'); __dbg.inputDown('Mouse0');
            for (let i = 0; i < 90 && G.shots === s0; i++) __dbg.step(1, 1/60);
            __dbg.keys.delete('Mouse0');
            __dbg.step(30, 1/60);
            const fired = G.shots > s0 && p.mags[k] < m0, m1 = p.mags[k];
            __dbg.startReload();
            const started = p.reloading > 0;
            __dbg.step(Math.ceil((__dbg.GUNS[k].reload + 0.5) * 60), 1/60);
            return {gun:p.gun, fired, m0, m1, started, full:p.mags[k] === __dbg.GUNS[k].mag, mag:p.mags[k]}; }""", k)
        run.check(f"val-guns/{k}", r["gun"] == k and r["fired"] and r["started"] and r["full"],
                  f"彈匣 {r['m0']}→{r['m1']}→{r['mag']}")


def t_apex_guns(run, a):
    run.start("apex", "range", "atrium")
    for k in ["vector", "lance", "breach"]:
        run.js("k => __dbg.swapGun(k)", k)
        run.step(1.5)
        r = run.js("""k => { const G = __dbg.getG(), p = G.player, s0 = G.shots, m0 = p.mags[k];
            __dbg.keys.add('Mouse0'); __dbg.inputDown('Mouse0');
            for (let i = 0; i < 90 && G.shots === s0; i++) __dbg.step(1, 1/60);
            __dbg.keys.delete('Mouse0'); __dbg.step(20, 1/60);
            return {gun:p.gun, shots:G.shots - s0, m0, m1:p.mags[k]}; }""", k)
        run.check(f"apex-guns/{k}", r["gun"] == k and r["shots"] > 0 and r["m1"] < r["m0"],
                  f"{r['shots']} 發 · 彈匣 {r['m0']}→{r['m1']}")


def t_dm(run, a):
    run.start("valorant", "survival", a.dm_map)
    run.js("() => { __dbg.getG().player.iframe = 1e9; }")
    run.step(0.1)
    n0 = run.js("() => __dbg.getG().bots.length")
    run.check("dm 開局 5 隻", n0 == 5, f"{n0} 隻")
    r = run.js("""() => { const G = __dbg.getG(); G.bots[0].hp = 0; __dbg.step(3, 1/60);
        const after = G.bots.length, kills = G.dm.kills;
        __dbg.step(Math.round(1.3*60), 1/60); const early = G.bots.length;
        __dbg.step(Math.round(0.4*60), 1/60); const late = G.bots.length;
        return {after, kills, early, late}; }""")
    run.check("dm 擊殺後補回", r["after"] == 4 and r["kills"] == 1 and r["early"] == 4 and r["late"] == 5,
              f"擊殺後 {r['after']} 隻、1.3 秒 {r['early']} 隻、1.7 秒 {r['late']} 隻")
    r = run.js("""() => { const G = __dbg.getG(), p = G.player; p.iframe = 0; p.hp = 0;
        __dbg.step(1, 1/60); const down = G.mode;
        __dbg.step(Math.round(1.3*60), 1/60); const mid = G.mode;
        __dbg.step(Math.round(0.3*60), 1/60);
        return {down, mid, after:G.mode, hp:p.hp, deaths:G.dm.deaths}; }""")
    run.check("dm 倒地 1.5 秒後重生", r["down"] == "down" and r["mid"] == "down" and r["after"] == "fight" and r["hp"] == 100,
              f"{r['down']} → 1.3 秒 {r['mid']} → 1.6 秒 {r['after']}（血 {r['hp']}、死亡 {r['deaths']}）")


def t_range_test(run, a):
    run.start("valorant", "range", "atrium")
    r = run.js("""() => { const G = __dbg.getG(); __dbg.inputDown('KeyG');
        const st = G.rangeTest; if (!st) return {started:false};
        const phase0 = st.phase;
        __dbg.step(Math.round(3.2*60), 1/60);
        const phase1 = st.phase, mapName = __dbg.activeMap().name;
        let it = 0;
        while (G.rangeTest && it++ < 20000){
          if (st.current && st.current.hp > 0) st.current.hp = 0;
          __dbg.step(3, 1/60);
        }
        return {started:true, phase0, phase1, mapName, done:!G.rangeTest, kills:st.kills, missed:st.missed, index:st.index}; }""")
    run.check("range-test 開始", r.get("started") and r["phase0"] == "countdown" and r["phase1"] == "active",
              f"{r.get('mapName')} · {r.get('phase0')} → {r.get('phase1')}")
    run.check("range-test 結束", r.get("done") and r["index"] == 30 and r["kills"] + r["missed"] == 30,
              f"{r.get('kills')} 殺 / 漏 {r.get('missed')}")


def shots(run, a):
    os.makedirs(a.shots, exist_ok=True)
    # 截圖時藏起暫停畫面：邏輯上仍是暫停（rAF 迴圈不推進），畫面照畫
    run.page.add_style_tag(content="#overPause{display:none!important}")
    views = {mid: [None] for mid in run.map_ids()}
    extra = SHOT_VIEWS.get(a.shot_map)
    if extra:
        views[a.shot_map] = [None] + extra
    for mid, vs in views.items():
        run.start("apex", "range", mid)
        run.step(0.3)
        for i, v in enumerate(vs):
            if v:
                run.js("""v => { const G = __dbg.getG(), p = G.player; p.x = v.x; p.y = v.y; p.vx = p.vy = 0;
                    __dbg.setYaw(__dbg.yawOf(v.angle)); __dbg.setPitch(v.pitch || 0); __dbg.step(2, 1/60); }""", v)
            run.js("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
            name = f"{mid}.png" if not v else f"{mid}-{v.get('name', i)}.png"
            run.page.screenshot(path=os.path.join(a.shots, name))
            print("  shot " + name, flush=True)


def launch(pw, a):
    if a.gpu:
        args = ["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist"] if sys.platform == "win32" else ["--enable-gpu", "--ignore-gpu-blocklist"]
        return pw.chromium.launch(channel="chrome", headless=not a.headed, args=args)
    args = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
    kw = {"executable_path": a.executable} if a.executable else {}
    try:
        return pw.chromium.launch(headless=not a.headed, args=args, **kw)
    except Exception:
        # playwright 版本和預裝的瀏覽器版本對不上時（雲端環境常見），改用 PLAYWRIGHT_BROWSERS_PATH 裡的 chromium
        alt = os.path.join(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""), "chromium")
        if a.executable or not os.path.isfile(alt):
            raise
        return pw.chromium.launch(headless=not a.headed, args=args, executable_path=alt)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=",".join(TESTS))
    ap.add_argument("--shots")
    ap.add_argument("--shot-map", default="stonegate")
    ap.add_argument("--dm-map", default="stonegate")
    ap.add_argument("--gpu", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--executable")
    ap.add_argument("--three", help="本機的 three.min.js（r147）；也可以用環境變數 ECHO3D_THREE")
    a = ap.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("需要 playwright：pip install playwright（沒有瀏覽器再跑 python -m playwright install chromium）")
        return 2
    only = [t for t in a.only.split(",") if t]
    bad = [t for t in only if t not in TESTS]
    if bad:
        print("不認得的項目：" + ", ".join(bad) + "（可選：" + ", ".join(TESTS) + "）")
        return 2
    d, _ = make_copy()
    srv, url = serve(d)
    t0 = time.time()
    with sync_playwright() as pw:
        browser = launch(pw, a)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        run = Run(page)
        three = a.three or os.environ.get("ECHO3D_THREE")
        if three:
            # 連不到 cdnjs 的環境：把 CDN 網址接到本機的同一版 three.min.js，字型給空的樣式表
            page.route(THREE_URL, lambda r: r.fulfill(path=three, content_type="application/javascript"))
            page.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(body="", content_type="text/css"))
        page.goto(url)
        page.wait_for_function("() => window.__dbg && __dbg.getG()", timeout=120000)
        fns = {"maps": t_maps, "val-guns": t_val_guns, "apex-guns": t_apex_guns, "dm": t_dm, "range-test": t_range_test}
        for t in only:
            print(f"[{t}]", flush=True)
            try:
                fns[t](run, a)
            except Exception as e:  # 測試程式本身出錯也算失敗，繼續跑其他項目
                run.check(f"{t} 執行", False, repr(e)[:300])
        if a.shots:
            print("[shots]", flush=True)
            shots(run, a)
        run.check("沒有 pageerror", not run.errors, "; ".join(run.errors[:5]))
        browser.close()
    srv.shutdown()
    fails = [r for r in run.results if not r[1]]
    print(f"\n{len(run.results) - len(fails)}/{len(run.results)} 通過 · {time.time() - t0:.0f} 秒")
    for name, _, detail in fails:
        print("  FAIL " + name + (" · " + detail if detail else ""))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
