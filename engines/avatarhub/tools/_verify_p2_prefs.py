# -*- coding: utf-8 -*-
"""P2-1 设备偏好闭环实测（对着运行中的 hub 打真实请求）：
   ① 无偏好基线 → ② 保存真实设备偏好=选择被尊重 → ③ 伪造缺席设备=粘性回退+标记
   → ④ 显式空串=跟随系统默认 → ⑤ 清场恢复原状。任何一步失败退出码 1。"""
import io
import json
import os
import sys

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HUB = "http://127.0.0.1:9000"
PREFS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audio_prefs.json")
FAIL = []


def check(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FAIL.append(msg)


print("① 基线：/rvc/devices 带 prefs 字段")
d = requests.get(f"{HUB}/rvc/devices", timeout=20).json()
check(d.get("ok") is True, f"devices ok (source={d.get('source')})")
pf = d.get("prefs")
check(isinstance(pf, dict), f"prefs 字段在: {json.dumps(pf, ensure_ascii=False)[:120]}")
pick_in = ((d.get("inputs_ex") or {}).get("pick") or {})
pick_out = ((d.get("outputs_ex") or {}).get("pick") or {})
print(f"   推荐输入={pick_in.get('label')} / 推荐输出={pick_out.get('label')}")

# 找一个「非推荐」的真实在线输入设备来当用户手选（更能证明 pick 被偏好压过）
devs_in = (d.get("inputs_ex") or {}).get("devices") or []
alt = next((e for e in devs_in if e["value"] != pick_in.get("value") and not e.get("danger") and not e.get("hidden")), None)
chosen = alt or next((e for e in devs_in if e["value"] == pick_in.get("value")), None)
check(chosen is not None, f"选做偏好的真实设备: {chosen and chosen['label']} ({chosen and chosen['value']})")

print("② 保存真实偏好 → 自动挑选尊重它")
r = requests.post(f"{HUB}/api/audio/prefs", json={"input": chosen["value"]}, timeout=10).json()
check(r.get("ok") is True, f"保存偏好 ok: {json.dumps(r.get('prefs'), ensure_ascii=False)[:120]}")
d2 = requests.get(f"{HUB}/rvc/devices", timeout=20).json()
pf2 = d2.get("prefs") or {}
check(pf2.get("input_set") and pf2.get("input_present"), f"prefs 回读: set={pf2.get('input_set')} present={pf2.get('input_present')} label={pf2.get('input_label')}")
a = requests.get(f"{HUB}/rvc/auto_devices", timeout=20).json()
check(a.get("input") == (pf2.get("input_canon") or chosen["value"]),
      f"auto_devices.input=偏好设备: {a.get('input_label')} ({a.get('input')})")
check(a.get("input_reason") == "你上次选定的设备", f"reason={a.get('input_reason')}")

print("③ 伪造缺席设备 → 粘性回退 + 缺席标记（偏好不被改写）")
ghost = "麦克风 (Ghost USB Device) (MME)"
requests.post(f"{HUB}/api/audio/prefs", json={"input": ghost}, timeout=10)
d3 = requests.get(f"{HUB}/rvc/devices", timeout=20).json()
pf3 = d3.get("prefs") or {}
check(pf3.get("input_set") and not pf3.get("input_present"),
      f"缺席识别: present={pf3.get('input_present')} label={pf3.get('input_label')}")
a3 = requests.get(f"{HUB}/rvc/auto_devices", timeout=20).json()
check(a3.get("input") == pick_in.get("value"),
      f"回退到推荐: {a3.get('input_label')} ({a3.get('input')})")
miss = a3.get("input_pref_missing") or {}
check(miss.get("value") == ghost, f"缺席标记带原偏好: {json.dumps(miss, ensure_ascii=False)}")
raw = json.loads(open(PREFS_FILE, encoding="utf-8").read())
check(raw.get("input") == ghost, "偏好文件未被回退改写（粘性=插回可自动恢复）")

print("④ 显式空串 = 跟随系统默认（不强塞推荐）")
requests.post(f"{HUB}/api/audio/prefs", json={"input": ""}, timeout=10)
a4 = requests.get(f"{HUB}/rvc/auto_devices", timeout=20).json()
check(not a4.get("input") and a4.get("input_label") == "跟随系统默认",
      f"input={a4.get('input')} label={a4.get('input_label')} reason={a4.get('input_reason')}")

print("⑤ 输出侧偏好 + 清场恢复")
if pick_out.get("value"):
    requests.post(f"{HUB}/api/audio/prefs", json={"output": pick_out["value"]}, timeout=10)
    a5 = requests.get(f"{HUB}/rvc/auto_devices", timeout=20).json()
    check(a5.get("output") == pick_out["value"] and a5.get("output_reason") == "你上次选定的设备",
          f"输出偏好生效: {a5.get('output_label')}")
try:
    os.remove(PREFS_FILE)
    print("  ✓ 已删除 audio_prefs.json（恢复无偏好初始态）")
except FileNotFoundError:
    pass
a6 = requests.get(f"{HUB}/rvc/auto_devices", timeout=20).json()
check(a6.get("input") == pick_in.get("value"), f"清场后回到纯推荐: {a6.get('input_label')}")

print("\n" + ("❌ FAIL: " + " | ".join(FAIL) if FAIL else "✅ P2-1 偏好闭环全部通过"))
sys.exit(1 if FAIL else 0)
