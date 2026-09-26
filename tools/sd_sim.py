#!/usr/bin/env python3
"""爆破模式的電腦對電腦模擬：玩家不介入，統計戰術選擇、下包率、攻守勝率、卡住與來回反轉。

  python tools/sd_sim.py                         # 目前的 index.html，預設 24 回合
  python tools/sd_sim.py --rounds 30 --seed 7    # 回合數與亂數種子（頁面的 Math.random 換成固定種子的 LCG）
  python tools/sd_sim.py --rev HEAD~1            # 某個 commit 的版本（git show 出來量），拿來做改前改後對照
  python tools/sd_sim.py --three PATH            # 連不到 cdnjs 時用本機的 three.min.js r147

玩家每回合開場就退場，另一隊也抽掉一隻 GRUNT（不算陣亡、不影響經濟），變成 4 對 4，攻守才公平。
購買階段直接跳過（電腦在購買階段本來就不動），之後用固定步長 1/60 秒推進到回合結束。

卡住與反轉的定義和 shots/dm-ai-sim.js 一致：
  反轉：每 0.1 秒取樣一次位移，前後兩段都 ≥ 10cm、相隔 ≤ 0.6 秒、方向差 > 150°
  卡住：不重疊的 2 秒區間，淨位移 < 0.8m，而且這段時間平均油門（vK，要走的比例）> 0.5——
        想走卻沒走動；刻意站定（架槍、下包、急停）時油門是 0，不算
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_test_copy import ROOT, make_copy, serve  # noqa: E402
from regress import THREE_URL, Run, launch  # noqa: E402

# 固定種子的 Math.random：改前改後用同一串亂數開局（之後會因行為不同而分岔）
SEED_JS = """(seed => { let s = seed >>> 0; Math.random = () => (s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 4294967296; })(%d);"""

ROUND_JS = r"""
() => {
  const G = __dbg.getG(), sd = G.sd, p = G.player, DT = 1/60;
  // 開場：玩家退場、另一隊抽掉一隻 GRUNT（4 對 4）
  p.out = true; p.hp = 0; p.x = p.y = -1e4;
  const bench = G.bots.find(b => b.team === 1 && b.type === 'grunt');
  if (bench){ G.bots.splice(G.bots.indexOf(bench), 1); bench.obj.visible = false; }
  if (sd.spike.carrier === p || sd.spike.carrier === bench){
    const b = G.bots.find(b => b.team === sd.atk); sd.spike.carrier = b; sd.spike.x = b.x; sd.spike.y = b.y;
  }
  if (sd.phase === 'buy'){ sd.timer = 0.001; __dbg.step(1, DT); }
  const plan = sd.plan ? {kind:sd.plan.kind, site:sd.plan.site} : {kind:'legacy', site:sd.target};
  const roles = G.bots.filter(b => b.team === sd.atk).map(b => b.sdRole || '-');
  const st = new Map();
  for (const b of G.bots) st.set(b, {sx:b.x, sy:b.y, prev:null, prevT:-9, wx:b.x, wy:b.y, vk:0, n:0});
  let rev = 0, stuck = 0, stuckAt = [], frame = 0, plantedAt = null;
  const r0 = sd.round;
  while (sd.phase !== 'end' && sd.round === r0 && frame < 60*200){
    __dbg.step(1, DT); frame++;
    if (sd.planted && plantedAt === null) plantedAt = frame*DT;
    const t = frame*DT;
    for (const b of G.bots){
      if (b.hp <= 0) continue;
      const s = st.get(b);
      s.vk += b.vK || 0; s.n++;
      if (frame % 6 === 0){
        const dx = b.x - s.sx, dy = b.y - s.sy, d = Math.hypot(dx, dy);
        if (d > 1){
          const now = {x:dx/d, y:dy/d, len:d};
          if (s.prev && s.prev.len >= 3.3 && d >= 3.3 && t - s.prevT <= 0.6 && s.prev.x*now.x + s.prev.y*now.y < Math.cos(150*Math.PI/180)) rev++;
          s.prev = now; s.prevT = t;
        }
        s.sx = b.x; s.sy = b.y;
      }
      if (frame % 120 === 0){
        if (s.n && Math.hypot(b.x - s.wx, b.y - s.wy) < 0.8/0.03 && s.vk/s.n > 0.5){
          stuck++;
          if (stuckAt.length < 12) stuckAt.push({t:+t.toFixed(1), team:b.team === sd.atk ? 'A' : 'D', type:b.type, x:Math.round(b.x), y:Math.round(b.y),
            key:b.sdKey, spot:b.sdSpot && [Math.round(b.sdSpot.x), Math.round(b.sdSpot.y)], see:b.see > 0, chase:!!b.chase,
            last:[Math.round(b.lastX), Math.round(b.lastY)], pathFail:!!b.pathFail, stuckT:+(b.stuck || 0).toFixed(1), cov:!!b.cov});
        }
        s.wx = b.x; s.wy = b.y; s.vk = 0; s.n = 0;
      }
    }
  }
  const h = sd.history[sd.history.length - 1];
  const out = {round:r0, atk:sd.atk, plan, roles, planted:sd.planted, plantedAt, plantSite:sd.spike.site || null,
    win:h ? (h.win === h.atk ? 'ATK' : 'DEF') : '?', reason:h ? h.reason : '?', secs:+(frame*DT).toFixed(1), rev, stuck, stuckAt,
    alive:[sdN(sd.atk), sdN(1 - sd.atk)]};
  function sdN(team){ return G.bots.filter(b => b.team === team && b.hp > 0).length; }
  // 下一回合（跳過結束展示）
  if (!sd.over){ sd.timer = 0.001; __dbg.step(1, DT); }
  return out;
}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rev", help="量某個 commit 的 index.html")
    ap.add_argument("--rounds", type=int, default=24)
    ap.add_argument("--seed", type=int, default=20260926)
    ap.add_argument("--map", default="stonegate")
    ap.add_argument("--json", help="結果另存 JSON")
    ap.add_argument("--trace", action="store_true", help="印出每次卡住的位置與當時的目標")
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
    d, _ = make_copy(src=src)
    srv, url = serve(d)
    rows = []
    with sync_playwright() as pw:
        browser = launch(pw, a)
        page = browser.new_page(viewport={"width": 640, "height": 360})
        run = Run(page)
        page.add_init_script(SEED_JS % a.seed)
        three = a.three or os.environ.get("ECHO3D_THREE")
        if three:
            page.route(THREE_URL, lambda r: r.fulfill(path=three, content_type="application/javascript"))
            page.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(body="", content_type="text/css"))
        page.goto(url, timeout=300000)
        page.wait_for_function("() => window.__dbg && __dbg.getG()", timeout=120000)
        run.start("valorant", "defuse", a.map)
        while len(rows) < a.rounds:
            if run.js("() => __dbg.getG().sd.over"):
                run.start("valorant", "defuse", a.map)
            r = run.js(ROUND_JS)
            rows.append(r)
            print(f"  R{r['round']:>2} atk={r['atk']} {r['plan']['kind']:>6}→{r['plan']['site']}  roles={','.join(r['roles'])}  "
                  f"planted={'Y@' + str(r['plantSite']) + ' ' + str(round(r['plantedAt'] or 0)) + 's' if r['planted'] else 'N':<10} "
                  f"{r['win']}({r['reason']}) {r['secs']}s  alive={r['alive']}  rev={r['rev']} stuck={r['stuck']}", flush=True)
            if a.trace:
                for e in r.get("stuckAt", []):
                    print("      stuck " + json.dumps(e, ensure_ascii=False), flush=True)
        if run.errors:
            print("pageerror: " + "; ".join(run.errors[:5]))
        browser.close()
    srv.shutdown()
    n = len(rows)
    kinds = {}
    for r in rows:
        k = r["plan"]["kind"] + "→" + str(r["plan"]["site"])
        kinds[k] = kinds.get(k, 0) + 1
    planted = sum(r["planted"] for r in rows)
    atk = sum(r["win"] == "ATK" for r in rows)
    reasons = {}
    for r in rows:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    secs = sum(r["secs"] for r in rows)
    rev = sum(r["rev"] for r in rows)
    stuck = sum(r["stuck"] for r in rows)
    print(f"\n{n} 回合 · 版本 {a.rev or '目前'} · 種子 {a.seed}")
    print("  戰術：" + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    print(f"  下包：{planted}/{n}（{100*planted/n:.0f}%）")
    print(f"  勝率：進攻 {atk}/{n}（{100*atk/n:.0f}%）· 防守 {n-atk}/{n}（{100*(n-atk)/n:.0f}%）")
    print("  結束原因：" + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
    print(f"  回合時間合計 {secs:.0f} 秒 · 反轉 {rev}（每分鐘 {60*rev/secs:.2f}）· 卡住 2 秒區間 {stuck}（每分鐘 {60*stuck/secs:.2f}）")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
    return 1 if run.errors else 0


if __name__ == "__main__":
    sys.exit(main())
