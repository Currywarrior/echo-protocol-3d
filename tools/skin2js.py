"""把 skins/ 裡的 .glb 造型模型包成 skins/skins.js，讓 file:// 開的遊戲也讀得到。

Chrome 在 file:// 底下不准 fetch 旁邊的 .glb，但 <script src> 可以，所以轉成 base64 塞進 JS。
skins/ 整個不進版控（造型是別人的設計，只在本機玩）。

用法：python tools/skin2js.py
檔名規則：skins/<槍模 key>.glb，例如 skins/val_vandal.glb；同名的 .json 放對齊參數（可省略，用 DEFAULT_FIT）。
  muzzle  模型原本槍口朝哪個軸（"+x" "-x" "+z" "-z"）
  len     擺進遊戲後的總長（槍口到槍托，公尺）
  front   槍口的 z（遊戲的槍模槍口朝 -z）
  top     最高點的 y（瞄具頂端，對齊原本槍模的準星高度，開鏡才看得到前方；對齊最低點的話彈匣會把整把槍頂高、槍托擋住視線）
"""
import base64, json, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKINS = ROOT / "skins"
DEFAULT_FIT = {"muzzle": "+x", "len": 1.24, "front": -0.77, "top": 0.098}

out = {}
for glb in sorted(SKINS.glob("*.glb")):
    cfg = glb.with_suffix(".json")
    fit = dict(DEFAULT_FIT, **(json.loads(cfg.read_text("utf-8")) if cfg.exists() else {}))
    out[glb.stem] = {"glb": base64.b64encode(glb.read_bytes()).decode("ascii"), "fit": fit}
    print(f"{glb.name}: {glb.stat().st_size/1e6:.1f} MB  fit={fit}")

(SKINS / "skins.js").write_text("window.ECHO_SKINS = " + json.dumps(out) + ";\n", "utf-8")
print(f"寫出 skins/skins.js（{len(out)} 個模型）")
