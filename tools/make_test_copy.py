#!/usr/bin/env python3
"""從 index.html 產生測試副本，並（可選）用 http.server 提供服務。

測試副本在 "applyAtmo();\\nlayout();\\nG = newGame();" 前面掛 window.__dbg，
讓 Playwright 可以直接讀寫遊戲狀態、按鍵、以固定步長推進 update。
Playwright 不能開 file://，所以要用 http 服務。

用法：
  python tools/make_test_copy.py                 # 產生副本到暫存資料夾，印出路徑
  python tools/make_test_copy.py --out DIR       # 產生到指定資料夾
  python tools/make_test_copy.py --serve 8123    # 產生並在 8123 埠提供服務（Ctrl+C 結束）

被 tools/regress.py 匯入使用：make_copy() 與 serve()。
"""
import argparse
import functools
import http.server
import os
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANCHOR = "applyAtmo();\nlayout();\nG = newGame();"

# 掛在遊戲 IIFE 裡面，所以拿得到 let / const 宣告的區域變數。
# 開場自動全螢幕要關掉（SET.autoFs = 0），否則尺寸一直變、滑鼠鎖定一直被解除。
# step 以固定步長推進：依賴真實時間的測試在 GPU 忙的時候會全面失準（AGENTS.md）
HOOK = r"""
// ── 測試副本專用（tools/make_test_copy.py 產生，不要提交）──
SET.autoFs = 0;
window.__dbg = {
  getG: () => G, keys, update: (dt) => update(dt),
  step: (n, dt) => { for (let i = 0; i < n; i++){ update(dt); sync(dt); updateViewModel(dt); wheelTap.clear(); } },
  setLocked: (v) => { locked = !!v; },
  setYaw: (v) => { yaw = v; }, setPitch: (v) => { pitch = v; },
  getYaw: () => yaw, yawOf,
  startGame, backToMenu, overPause, overStart, camera, scene,
  useMap, MAPS, activeMap: () => activeMap, WALLS: () => WALLS, SPAWNS: () => SPAWNS,
  GUNS, VAL_GUNS, RANGE, DM, RULES: () => RULES, gun: () => gun(),
  swapGun, startReload, valBuyGun, beginRangeTest, inputDown, held,
  mapSelect, modeSelect, navBlocked, hasLOS, spotFree,
  SD, sdPlant, sdSiteAt, sdAliveN, sdMapFor,
};
"""


def make_copy(out_dir=None, src=None):
    """寫出測試副本，回傳 (資料夾, 檔案路徑)。"""
    src = src or os.path.join(ROOT, "index.html")
    with open(src, encoding="utf-8") as f:
        html = f.read()
    n = html.count(ANCHOR)
    if n != 1:
        raise SystemExit(f"make_test_copy: 找到 {n} 處掛點（應該剛好 1 處）：{ANCHOR!r}")
    html = html.replace(ANCHOR, HOOK.strip("\n") + "\n" + ANCHOR, 1)
    out_dir = out_dir or tempfile.mkdtemp(prefix="echo3d-test-")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_dir, path


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(directory, port=0):
    """在背景執行緒提供 directory，回傳 (server, url)。port=0 由系統挑空的埠。"""
    handler = functools.partial(_Quiet, directory=directory)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/index.html"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", help="來源 HTML（預設 repo 根目錄的 index.html）")
    ap.add_argument("--out", help="輸出資料夾（預設暫存資料夾）")
    ap.add_argument("--serve", type=int, metavar="PORT", help="產生後在這個埠提供服務，直到 Ctrl+C")
    a = ap.parse_args()
    d, path = make_copy(a.out, a.src)
    print(path)
    if a.serve is not None:
        srv, url = serve(d, a.serve)
        print(url, flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
