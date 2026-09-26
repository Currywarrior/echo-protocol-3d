#!/usr/bin/env python3
"""子彈與畫面一致性：隨機射線比對「子彈判定（bulletInWall）停在哪」和「畫面上的網格（mapLayer）先擋到哪」。

  python tools/bullet_audit.py                          # 目前的 index.html，全部地圖
  python tools/bullet_audit.py --maps stonegate,town    # 只量部分地圖
  python tools/bullet_audit.py --rev HEAD~1             # 某個 commit 的版本，拿來做改前改後比較
  python tools/bullet_audit.py --detail 12              # 每種落差列出前 12 組造成的網格（材質顏色、幾何、位置）
  python tools/bullet_audit.py --three PATH             # 連不到 cdnjs 時用本機的 three.min.js r147

每張圖從隨機位置（地圖內、不在牆裡、離地 0.3～2.2m）往隨機方向（俯仰 ±0.15 rad）射出，最遠 50m：
  穿過可見實體  畫面上先碰到網格，子彈判定卻還要往後超過 TOL 才停（或根本沒停）。分母是畫面上有碰到東西的射線
  被看不見的擋  子彈判定先停，畫面上那裡卻沒有東西（網格在 TOL 之後或沒有）。分母是子彈判定有停下的射線
碰到地面（0.06m 以下）就結束，兩邊都截在那裡。半透明（opacity < 0.5）、shader 特效（光柱）、只寫深度的陰影代理、線條不算可見實體。
葉片（樹冠的枝葉、葉片卡、花槽的灌木）本來就讓子彈穿過（看起來是鏤空的），不算穿過可見實體；
0.06m 以下的薄層（草皮、鋪面）當成地面。
量測時把 batchStatic 關掉：幾何完全一樣，只是不合批，才能回報是哪個網格。
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
AUDIT_JS = r"""
([N, TOL, seed, detail]) => {
  const D = __dbg, S = D.S, W = D.W(), H = D.H(), T = THREE;
  let sd = seed;
  const R = () => (sd = (sd*1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
  const meshes = [], TREES = D.WALLS().filter(w => w.tree).map(w => [(w.x + w.w/2)*S, (w.y + w.h/2)*S]);
  // 樹冠（樹幹 3m 以上的枝葉）：離樹幹中心 2.5m 內、高 2.7m 以上的命中
  const canopy = (p) => p.y > 2.7 && TREES.some(([tx, tz]) => (p.x - tx)**2 + (p.z - tz)**2 < 2.5*2.5);
  D.mapLayer.updateMatrixWorld(true);
  D.mapLayer.traverse(o => {
    if (!o.isMesh) return;
    for (let p = o; p; p = p.parent) if (!p.visible) return;
    const m = o.material;
    if (Array.isArray(m) || m.colorWrite === false || m.isShaderMaterial || (m.transparent && m.opacity < 0.5)) return;
    meshes.push(o);
  });
  const rc = new T.Raycaster(), org = new T.Vector3(), dir = new T.Vector3();
  const MAX = 50, STEP = 0.05;
  let visHits = 0, pass = 0, stops = 0, ghost = 0, n = 0, tries = 0;
  const passBy = new Map(), ghostBy = new Map();
  const tag = (o) => {
    const m = o.material, g = o.geometry;
    if (!g.boundingBox) g.computeBoundingBox();
    const b = g.boundingBox, sz = [b.max.x-b.min.x, b.max.y-b.min.y, b.max.z-b.min.z].map(v => (v*o.scale.x).toFixed(2));
    return (m.color ? "#" + m.color.getHexString() : m.type) + " " + g.type.replace("Geometry", "") + " " + sz.join("x");
  };
  const note = (map, key, pt) => {
    const e = map.get(key) || map.set(key, {n:0, pts:[]}).get(key);
    e.n++; if (e.pts.length < 3) e.pts.push(pt);
  };
  while (n < N && tries < N*50){
    tries++;
    const x = R()*W, y = R()*H, h = 0.3 + R()*1.9;
    if (D.inWallAt(x, y, h) || D.bulletInWall(x, y, h)) continue;
    const yaw = R()*Math.PI*2, pit = (R()*2 - 1)*0.15;
    const dx = Math.cos(yaw)*Math.cos(pit), dy = Math.sin(yaw)*Math.cos(pit), dh = Math.sin(pit);
    org.set(x*S, h, y*S); dir.set(dx, dh, dy);
    rc.set(org, dir); rc.far = MAX;
    const hits = rc.intersectObjects(meshes, false);
    // 起點不能在可見的網格裡（例如沒有碰撞的裝飾內部）：第一個命中的是背面就換一條
    if (hits.length && hits[0].face && hits[0].face.normal.clone().transformDirection(hits[0].object.matrixWorld).dot(dir) > 0 && !hits[0].object.material.side) continue;
    n++;
    // 地面：0.06m 以下的薄層（鋪面、踢腳石的底）當成地面，兩邊都比到這裡為止
    let dG = dh < 0 ? (h - 0.06)/(-dh) : MAX; dG = Math.min(dG, MAX);
    let dV = MAX + 1, vObj = null;
    for (const hh of hits){
      if (hh.distance > dG) break;
      if (hh.point.y < 0.06) continue;                  // 地面上的薄層（草皮、鋪面）當地面
      const mt = hh.object.material;
      if (mt.alphaTest > 0 || (mt.color && mt.color.getHex() === 0x34503A)) continue;   // 葉片卡、葉簇（樹冠與花槽的灌木）
      // 樹冠：子彈刻意穿過
      if (canopy(hh.point)) continue;
      dV = hh.distance; vObj = hh.object; break;
    }
    let dC = MAX + 1;
    for (let t = STEP; t <= dG; t += STEP){
      if (D.bulletInWall((x*S + dx*t)/S, (y*S + dy*t)/S, h + dh*t)){ dC = t; break; }
    }
    if (dV <= MAX){
      visHits++;
      if (dC > dV + TOL){ pass++; const pt = [(x*S+dx*dV).toFixed(1), (y*S+dy*dV).toFixed(1), (h+dh*dV).toFixed(2)]; note(passBy, tag(vObj), pt.join(",")); }
    }
    if (dC <= MAX){
      stops++;
      if (dV > dC + TOL){ ghost++; const pt = [(x*S+dx*dC).toFixed(1), (y*S+dy*dC).toFixed(1), (h+dh*dC).toFixed(2)]; 
        const X = (x*S + dx*dC)/S, Y = (y*S + dy*dC)/S, Hh = h + dh*dC;
        const hit = [...D.WALLS(), ...(D.SHELLS ? D.SHELLS() : [])].find(w => X > w.x && X < w.x + w.w && Y > w.y && Y < w.y + w.h && Hh >= (w.base || 0) && Hh < (w.ht || 3.2));
        const k = !hit ? "?" : hit.shell ? "shell " + (hit.cyl ? "cyl" : hit.roof ? "roof" : hit.cliffOf ? "cliff" : "box " + [hit.w, hit.h].map(v => (v*S).toFixed(2)).join("x") + " " + hit.base.toFixed(2) + "-" + hit.ht.toFixed(2))
                : "wall " + (hit.style || hit.part || (hit.tree ? "tree" : hit.crate ? "crate" : hit.ramp ? "ramp" : "plain"));
        note(ghostBy, k, pt.join(",")); }
    }
  }
  const top = (m) => [...m.entries()].sort((a, b) => b[1].n - a[1].n).slice(0, detail).map(([k, v]) => [k, v.n, v.pts]);
  return {n, visHits, pass, stops, ghost, passTop: top(passBy), ghostTop: top(ghostBy), meshes: meshes.length};
}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rev", help="量某個 commit 的 index.html")
    ap.add_argument("--maps", help="逗號分隔的地圖 id（預設主選單全部）")
    ap.add_argument("--rays", type=int, default=3000)
    ap.add_argument("--tol", type=float, default=0.25, help="兩邊停下的距離差多少公尺以上才算落差（子彈一步約 0.21m）")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--detail", type=int, default=0)
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
        html = f.read()
    html = html.replace(EXTRA, EXTRA + " mapLayer, bulletInWall, inWallAt, S, W: () => W, H: () => H," + (" SHELLS: () => SHELLS," if "let BGRID" in html else "") + "", 1)
    # 不合批：幾何不變，但每個網格保留自己的材質與尺寸，才回報得出是哪個東西
    html = html.replace("function batchStatic(root){", "function batchStatic(root){ if (window.__noBatch) return;", 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    srv, url = serve(d)
    with sync_playwright() as pw:
        browser = launch(pw, a)
        page = browser.new_page(viewport={"width": 640, "height": 360})
        page.add_init_script("window.__noBatch = true;")
        errs = []
        page.on("pageerror", lambda e: (errs.append(str(e)), print("pageerror: " + str(e)[:300], flush=True)))
        three = a.three or os.environ.get("ECHO3D_THREE")
        if three:
            page.route(THREE_URL, lambda r: r.fulfill(path=three, content_type="application/javascript"))
            page.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(body="", content_type="text/css"))
        page.goto(url, timeout=600000)
        page.wait_for_function("() => window.__dbg && __dbg.getG()", timeout=180000)
        ids = a.maps.split(",") if a.maps else page.evaluate("() => [...__dbg.mapSelect.options].map(o => o.value)")
        print(f"{'地圖':16s} {'射線':>5s} {'穿過可見實體':>14s} {'被看不見的擋':>14s}")
        for mid in ids:
            page.evaluate("mid => { const el = __dbg.mapSelect; el.value = mid; el.dispatchEvent(new Event('change')); }", mid)
            page.evaluate("() => { __dbg.startGame(); __dbg.overPause.hidden = false; __dbg.setLocked(true); __dbg.step(2, 1/60); }")
            name = page.evaluate("() => __dbg.activeMap().name")
            r = page.evaluate(AUDIT_JS, [a.rays, a.tol, a.seed, a.detail])
            pp = 100 * r["pass"] / max(1, r["visHits"])
            gp = 100 * r["ghost"] / max(1, r["stops"])
            print(f"{name:16s} {r['n']:5d} {pp:6.1f}% ({r['pass']:4d}/{r['visHits']:4d}) {gp:6.1f}% ({r['ghost']:4d}/{r['stops']:4d})", flush=True)
            if a.detail:
                for title, key in (("  穿過可見實體（材質 幾何 尺寸m：次數 [x,z,高]）", "passTop"), ("  被看不見的擋", "ghostTop")):
                    if r[key]:
                        print(title)
                        for k, c, pts in r[key]:
                            print(f"    {c:4d}  {k}  {' '.join(pts)}")
        if errs:
            print("pageerror: " + "; ".join(errs[:5]))
        browser.close()
    srv.shutdown()
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
