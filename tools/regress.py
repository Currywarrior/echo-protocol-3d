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
  defuse      VALORANT 爆破：5 對 5 開局、購買階段屏障擋得住（結束後放行）、全滅計分與發錢、下包、引爆、
              打滿 12 回合換邊（錢回 800、武器清空）、拆包（拆到一半的存檔點）、13 勝結束、12:12 延長賽要領先 2 回合
  defuse-match（不在預設清單，要用 --only 指定）VALORANT 爆破：電腦 4 對 5 自己打完一整場，印出每回合結果
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

TESTS = ["maps", "val-guns", "apex-guns", "dm", "range-test", "defuse"]
EXTRA_TESTS = ["defuse-match"]      # 很慢（整場約幾十萬步），要用 --only 指定
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


# ── 爆破 ──
# 共用的 JS：跳過購買階段、讓某一隊全滅、推進到下一回合
SD_JS = """
window.__sd = {
  G: () => __dbg.getG(), sd: () => __dbg.getG().sd,
  skipBuy(){ const sd = this.sd(); if (sd.phase === 'buy'){ sd.timer = 0.001; __dbg.step(2, 1/60); } },
  wipe(team){ const G = this.G(); for (const b of G.bots) if (b.team === team) b.hp = 0; if (team === 0) G.player.hp = 0; __dbg.step(3, 1/60); },
  nextRound(){ const sd = this.sd(); if (sd.phase === 'end'){ sd.timer = 0.001; __dbg.step(2, 1/60); } },
  calm(){ for (const b of this.G().bots) b.cool = 1e9; },           // 電腦不開槍（只測規則）
  // 包點裡一個走得到的點
  siteSpot(key){ const r = __dbg.activeMap().sites[key];
    for (let j = 0.5; j < 1; j += 0.05) for (let i = 0; i < 12; i++){
      const x = r.x + r.w*(0.5 + (i%2 ? 1 : -1)*(i/24)*j), y = r.y + r.h*j;
      if (!__dbg.navBlocked(x, y) && __dbg.sdSiteAt(x, y)) return {x, y}; }
    return {x:r.x + r.w/2, y:r.y + r.h/2}; },
  hold(code, sec){ __dbg.keys.add(code); __dbg.step(Math.round(sec*60), 1/60); __dbg.keys.delete(code); },
};
"""


def t_defuse(run, a):
    run.start("valorant", "defuse", a.dm_map)
    run.js(SD_JS)
    r = run.js("""() => { const G = __sd.G(), sd = G.sd;
        return {ok:!!sd, phase:sd && sd.phase, t0:G.bots.filter(b => b.team === 0).length,
                t1:G.bots.filter(b => b.team === 1).length, atk:sd.atk, credits:G.credits, timer:sd.timer, map:__dbg.activeMap().name}; }""")
    run.check("defuse 開局 5 對 5", r["ok"] and r["phase"] == "buy" and r["t0"] == 4 and r["t1"] == 5 and r["credits"] == 800
              and abs(r["timer"] - 45) < 0.1, f"{r['map']} · 隊友 {r['t0']}+玩家、敵人 {r['t1']} · {r['phase']} {r['timer']:.1f} 秒 · ¤{r['credits']}")
    # 屏障：往自己那一側的每道屏障走 1.5 秒，購買階段過不去；回合開始後同一道屏障走得過去
    walk = """(after) => { const G = __sd.G(), p = G.player, sd = G.sd, m = __dbg.activeMap();
        const side = sd.atk === 0 ? 'atk' : 'def', sp = side === 'atk' ? m.spawnsAtk : m.spawnsDef;
        const c = sp.reduce((q, s) => ({x:q.x + s.x/sp.length, y:q.y + s.y/sp.length}), {x:0, y:0});
        const out = [];
        for (const w of m.barriers.filter(w => w.team === side)){
          const bx = w.x + w.w/2, by = w.y + w.h/2, nx = w.w < w.h;
          const dir = nx ? Math.sign(bx - c.x) : Math.sign(by - c.y);
          p.x = nx ? bx - dir*45 : bx; p.y = nx ? by : by - dir*45; p.vx = p.vy = 0;
          __dbg.setYaw(__dbg.yawOf(nx ? (dir > 0 ? 0 : Math.PI) : (dir > 0 ? Math.PI/2 : -Math.PI/2))); __dbg.setPitch(0);
          __sd.hold('KeyW', 1.5);
          const crossed = nx ? (dir > 0 ? p.x > w.x + w.w : p.x < w.x) : (dir > 0 ? p.y > w.y + w.h : p.y < w.y);
          out.push({crossed, x:Math.round(p.x), y:Math.round(p.y)});
        }
        return {out, phase:sd.phase}; }"""
    r = run.js(walk)
    run.check("defuse 購買階段屏障擋住", r["phase"] == "buy" and r["out"] and not any(o["crossed"] for o in r["out"]),
              f"{len(r['out'])} 道 · " + ", ".join(f"({o['x']},{o['y']})" for o in r["out"]))
    run.js("() => __sd.skipBuy()")
    r = run.js(walk)
    run.check("defuse 回合開始後屏障放行", r["phase"] == "live" and all(o["crossed"] for o in r["out"]),
              ", ".join(str(o["crossed"]) for o in r["out"]))
    # 全滅計分：敵隊全滅 → 1:0，勝方 +3000，敗方 +1900
    r = run.js("""() => { const G = __sd.G(), sd = G.sd, c0 = G.credits, q = sd.roster.find(q => q.team === 1), e0 = q.credits;
        __sd.wipe(1);
        return {phase:sd.phase, score:sd.score.slice(), dc:G.credits - c0, de:q.credits - e0, why:sd.last && sd.last.reason}; }""")
    run.check("defuse 全滅計分與發錢", r["phase"] == "end" and r["score"] == [1, 0] and r["dc"] == 3000 and r["de"] == 1900,
              f"{r['score']} · {r['why']} · 我方 +{r['dc']}、敵方 +{r['de']}")
    r = run.js("""() => { const sd = __sd.sd(); __dbg.step(Math.round((__dbg.SD.endTime + 0.2)*60), 1/60);
        return {round:sd.round, phase:sd.phase, timer:sd.timer, n:__sd.G().bots.length}; }""")
    run.check("defuse 下一回合", r["round"] == 2 and r["phase"] == "buy" and abs(r["timer"] - 30) < 0.5 and r["n"] == 9,
              f"第 {r['round']} 回合 · {r['phase']} {r['timer']:.1f} 秒 · {r['n']} 隻")
    # 下包：玩家拿包、到包點按住互動 4 秒；3.9 秒時還沒下好，放開就從頭算
    r = run.js("""() => { const G = __sd.G(), sd = G.sd, p = G.player; __sd.skipBuy(); __sd.calm(); p.iframe = 1e9;
        sd.spike = {state:'carried', carrier:p, x:p.x, y:p.y};
        const key = Object.keys(__dbg.activeMap().sites)[0], s = __sd.siteSpot(key);
        p.x = s.x; p.y = s.y; p.vx = p.vy = 0; __dbg.step(2, 1/60);
        __sd.hold('KeyE', 2.0); const partial = sd.spike.state; __dbg.step(2, 1/60); const reset = sd.act === null;
        __sd.hold('KeyE', 3.9); const early = sd.spike.state;
        __sd.hold('KeyE', 0.2);
        return {partial, reset, early, state:sd.spike.state, phase:sd.phase, timer:sd.timer, site:sd.spike.site, key}; }""")
    run.check("defuse 下包 4 秒", r["partial"] == "carried" and r["reset"] and r["early"] == "carried" and r["state"] == "planted"
              and r["phase"] == "planted" and abs(r["timer"] - 45) < 0.5 and r["site"] == r["key"],
              f"{r['state']} @ {r['site']} · 倒數 {r['timer']:.1f}")
    # 引爆：45 秒到 → 進攻方勝，進攻方額外 +300
    r = run.js("""() => { const G = __sd.G(), sd = G.sd, c0 = G.credits;
        sd.timer = 0.01; __dbg.step(2, 1/60);
        return {phase:sd.phase, score:sd.score.slice(), why:sd.last && sd.last.reason, dc:G.credits - c0}; }""")
    run.check("defuse 引爆", r["phase"] == "end" and r["score"] == [2, 0] and r["why"] == "detonate" and r["dc"] == 3300,
              f"{r['score']} · {r['why']} · +{r['dc']}")
    # 換邊：第 12 回合打完 → 第 13 回合攻守互換、錢回 800、武器清空
    r = run.js("""() => { const G = __sd.G(), sd = G.sd, p = G.player; __sd.nextRound(); __dbg.valBuyGun('v_vandal');
        const atk0 = sd.atk, gun = p.slots.primary; sd.round = 12; sd.score = [7, 4]; __sd.skipBuy(); __sd.wipe(1); __sd.nextRound();
        return {round:sd.round, atk0, atk:sd.atk, credits:G.credits, bots:sd.roster.map(q => q.credits), gun, prim:p.slots.primary, phase:sd.phase,
                timer:sd.timer, score:sd.score.slice()}; }""")
    run.check("defuse 12 回合後換邊", r["round"] == 13 and r["atk"] != r["atk0"] and r["credits"] == 800 and all(c <= 800 for c in r["bots"])
              and r["gun"] == "v_vandal" and r["prim"] is None and abs(r["timer"] - 45) < 0.5,
              f"第 {r['round']} 回合 · 進攻方 {r['atk0']}→{r['atk']} · ¤{r['credits']} · 主武器 {r['gun']}→{r['prim']} · {r['score']}")
    # 拆包：玩家是防守方；敵方下包後按住 4 秒放開，存到一半；再按 3.6 秒拆完
    r = run.js("""() => { const G = __sd.G(), sd = G.sd, p = G.player; __sd.skipBuy(); __sd.calm(); p.iframe = 1e9;
        const key = Object.keys(__dbg.activeMap().sites)[0], s = __sd.siteSpot(key);
        const e = G.bots.find(b => b.team === sd.atk); e.x = s.x; e.y = s.y; __dbg.sdPlant(e);
        const planted = sd.spike.state; p.x = s.x + 20; p.y = s.y; p.vx = p.vy = 0;
        __sd.hold('KeyE', 4.0); __dbg.step(2, 1/60);
        const save = sd.defuseSave, mid = sd.spike.state, act = sd.act;
        __sd.hold('KeyE', 3.4); const early = sd.spike.state;
        __sd.hold('KeyE', 0.3);
        return {planted, save, mid, act:!!act, early, state:sd.spike.state, phase:sd.phase, why:sd.last && sd.last.reason, score:sd.score.slice()}; }""")
    run.check("defuse 拆包 7 秒（一半存檔）", r["planted"] == "planted" and r["save"] == 3.5 and r["mid"] == "planted" and not r["act"]
              and r["early"] == "planted" and r["state"] == "defused" and r["why"] == "defuse" and r["score"] == [9, 4],
              f"存檔 {r['save']} · {r['state']} · {r['score']}")
    # 13 勝結束
    r = run.js("""() => { const G = __sd.G(), sd = G.sd; __sd.nextRound(); sd.score = [12, 9]; __sd.skipBuy(); __sd.wipe(1); __sd.nextRound();
        return {over:sd.over, winner:sd.winner, mode:G.mode, score:sd.score.slice()}; }""")
    run.check("defuse 13 勝結束", r["over"] and r["winner"] == 0 and r["mode"] == "dead" and r["score"] == [13, 9], f"{r['score']} · {r['mode']}")
    # 延長賽：12:12 之後每回合換邊、每人 5000，13:12 不結束、14:12 才結束
    run.start("valorant", "defuse", a.dm_map)
    run.js(SD_JS)
    r = run.js("""() => { const G = __sd.G(), sd = G.sd, res = [];
        sd.round = 24; sd.score = [12, 11]; __sd.skipBuy(); __sd.wipe(0); const a24 = sd.atk; __sd.nextRound();
        res.push({round:sd.round, atk:sd.atk, c:G.credits, cb:sd.roster.map(q => q.credits), over:sd.over, score:sd.score.slice()});
        __sd.skipBuy(); __sd.wipe(1); __sd.nextRound();
        res.push({round:sd.round, atk:sd.atk, c:G.credits, over:sd.over, score:sd.score.slice()});
        __sd.skipBuy(); __sd.wipe(1); __sd.nextRound();
        res.push({over:sd.over, winner:sd.winner, score:sd.score.slice()});
        return {a24, res}; }""")
    o = r["res"]
    run.check("defuse 延長賽", o[0]["round"] == 25 and o[0]["atk"] != r["a24"] and o[0]["c"] == 5000 and all(c <= 5000 for c in o[0]["cb"])
              and not o[0]["over"] and o[1]["round"] == 26 and o[1]["atk"] != o[0]["atk"] and not o[1]["over"] and o[1]["score"] == [13, 12]
              and o[2]["over"] and o[2]["winner"] == 0 and o[2]["score"] == [14, 12],
              f"12:12 → 第 25 回合 ¤{o[0]['c']} · {o[1]['score']} 未結束 · {o[2]['score']} 結束")


def t_defuse_match(run, a):
    """電腦自己打完一整場：玩家每回合開場就退場（4 對 5），固定步長 1/30 秒"""
    run.start("valorant", "defuse", a.dm_map)
    run.js(SD_JS)
    r = run.js("""() => { const G = __sd.G(), sd = G.sd, log = [];
        let steps = 0, lastRound = 0;
        while (!sd.over && steps < 2e6){
          if (sd.round !== lastRound && sd.phase === 'buy'){ lastRound = sd.round; G.player.out = true; G.player.hp = 0; if (sd.spike.carrier === G.player){ const b = G.bots.find(b => b.team === sd.atk); if (b) sd.spike.carrier = b; } }
          const before = sd.history.length;
          __dbg.step(1, 1/30); steps++;
          if (sd.history.length !== before){ const h = sd.history[sd.history.length - 1]; log.push(h.round + ':' + (h.win === h.atk ? 'ATK' : 'DEF') + '(' + h.reason + ')'); }
        }
        return {over:sd.over, score:sd.score.slice(), winner:sd.winner, rounds:sd.history.length, steps, log, t:G.t}; }""")
    print("  " + " ".join(r["log"]), flush=True)
    run.check("defuse-match 打完一整場", r["over"] and max(r["score"]) >= 13,
              f"比分 {r['score'][0]}:{r['score'][1]} · {r['rounds']} 回合 · 遊戲時間 {r['t']/60:.1f} 分")


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
    bad = [t for t in only if t not in TESTS + EXTRA_TESTS]
    if bad:
        print("不認得的項目：" + ", ".join(bad) + "（可選：" + ", ".join(TESTS + EXTRA_TESTS) + "）")
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
        fns = {"maps": t_maps, "val-guns": t_val_guns, "apex-guns": t_apex_guns, "dm": t_dm, "range-test": t_range_test,
               "defuse": t_defuse, "defuse-match": t_defuse_match}
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
