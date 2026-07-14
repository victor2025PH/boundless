# -*- coding: utf-8 -*-
"""P2 重启后在线回归：lib 字段 / RVC 别名 / DFM 中文名回填 / 声音库新名 全链路自检。"""
import io
import json
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
HUB = "http://127.0.0.1:9000"
fails = []


def check(name, ok, detail=""):
    print(f"  [{'OK' if ok else 'NG'}] {name} {detail}")
    if not ok:
        fails.append(name)


def get(path):
    return json.loads(urllib.request.urlopen(HUB + path, timeout=15).read())


h = get("/health")
check("Hub /health ok", bool(h.get("ok", True)), f"active={h.get('active_profile')}")

p = get("/profiles")["profiles"]
check("VL1 /profiles 带 lib 字段", all("lib" in x for x in p),
      str({x.get("lib") for x in p}))

r = get("/rvc/models")
al = r.get("aliases") or {}
check("P2 /rvc/models 带 9 条别名", len(al) == 9, f"n={len(al)}")

ra = get("/api/rvc_assets")["assets"]
withal = [a for a in ra if a.get("alias")]
check("P2 /api/rvc_assets 带别名", len(withal) == 9,
      f"{len(withal)}/9 例:{withal[0]['alias'] if withal else '-'}")

d = get("/api/dfm/models?live_only=0")
models = d.get("models") or []
cn_cov = sum(1 for m in models if m.get("cn"))
check("P2 DFM 清单 cn 覆盖", d.get("engine_up") and cn_cov == len(models),
      f"{cn_cov}/{len(models)} engine_up={d.get('engine_up')}")
files = {m.get("model") for m in models}
_new3 = {"Bryan_Greynolds.dfm", "Jackie_Chan.dfm", "Keanu_Reeves_320.dfm"}
# 信息项不计罪：/model/available 来自生产引擎(.104)自己的库存，官方 3 款尚未推送过去
# （P3 任务：推模型+跑体检）。在与不在都只播报，不判 FAIL。
print(f"  [--] 官方3款在引擎清单: {len(_new3 & files)}/3（不在=生产机未同步，属 P3 推送范围）")

vp = get("/api/voicepack")
rows = vp.get("rows") or []
numbered = [x["name"] for x in rows if __import__("re").search(r"\d{3,}", x.get("name") or "")]
check("VN1 声音库显示名无编号", not numbered, str(numbered[:4]))
noted = sum(1 for x in rows if x.get("note"))
check("VN1 声音库 note 字段透出", noted > 150, f"note={noted}/{len(rows)}")

print("RESULT:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(1 if fails else 0)
