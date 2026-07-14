# -*- coding: utf-8 -*-
"""P1-4 纠错自学习单元探针：不碰真实 glossary/台账(全部重定向临时文件)。
验证 ①词级修正对抽取(jieba 扩词边界+拼音过滤) ②计数→阈值自动采纳→热词可见。
退出码 0=全过 1=有失败。"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ.setdefault("INTERP_STREAM_ASR", "0")   # 探针不连流式 ASR
import live_interpreter as li

FAIL = 0


def ok(cond, name, extra=""):
    global FAIL
    print(("  OK: " if cond else "  FAIL: ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAIL = 1


print("== ① 修正对抽取 ==")
p1 = li._ger_learn_pairs("我们请了一位通译帮忙", "我们请了一位同译帮忙")
ok(any(b == "同译" for _, b in p1), "同音单字替换扩成词", str(p1))
p2 = li._ger_learn_pairs("你好林文洁经理", "你好林文杰经理")
ok(any("文杰" in b for _, b in p2), "人名同音字纠错被抽取", str(p2))
p3 = li._ger_learn_pairs("the price is fourty dollars", "the price is forty dollars")
ok(any(b.strip() == "forty" for _, b in p3), "拉丁词纠错被抽取", str(p3))
p4 = li._ger_learn_pairs("今天天气很好", "今天天气很好")
ok(p4 == [], "无差异不产出", str(p4))
p5 = li._ger_learn_pairs("这个方案不行", "这个方案可以")   # 语义改写(非同音) 不该学
ok(not any(b == "可以" for _, b in p5), "非同音替换被拼音闸拒绝", str(p5))

print("== ② 台账计数与自动采纳(临时文件) ==")
tmpdir = tempfile.mkdtemp(prefix="p1learn_")
li._GER_LEARN_PATH = os.path.join(tmpdir, "ger_learned.json")
gl_tmp = os.path.join(tmpdir, "glossary.json")
with open(gl_tmp, "w", encoding="utf-8") as f:
    json.dump({"zh->en": [{"src": "既有词", "dst": "existing"}]}, f, ensure_ascii=False)
li._GLOSSARY_PATH = gl_tmp
li._GER_LEARN_ON = True
li._GER_LEARN_ADOPT = 2

li._ger_learn_note("你好林文洁经理", "你好林文杰经理", "zh")
store = li._ger_learn_load()
e = next((v for v in store.values() if "文杰" in v["right"]), None)
ok(e is not None and e["n"] == 1 and not e["adopted"], "第 1 次只记账不采纳", str(e))

li._ger_learn_note("请转告林文洁一声", "请转告林文杰一声", "zh")
store = li._ger_learn_load()
e = next((v for v in store.values() if "文杰" in v["right"]), None)
ok(e is not None and e["n"] == 2 and e["adopted"], "第 2 次达阈值自动采纳", str(e))

with open(gl_tmp, encoding="utf-8") as f:
    gl = json.load(f)
star = gl.get("*") or []
ok(any(it.get("src") == e["right"] and it.get("dst") == e["right"] for it in star),
   "词表 '*' 语向出现恒等映射", json.dumps(star, ensure_ascii=False))

comp = li._glossary_load(force=True)
ok(any(s == e["right"] for (s, d, _l) in comp.get("*", [])), "编译后词表含新词")
hp = li._asr_hotwords("zh")   # 采纳时已 force 重载→ver 跳→热词缓存自动失效
ok(e["right"] in (hp or ""), "新词进入 ASR 热词 prompt", (hp or "")[:80])

li._ger_learn_note("请转告林文洁一声", "请转告林文杰一声", "zh")
store = li._ger_learn_load()
e2 = next((v for v in store.values() if "文杰" in v["right"]), None)
ok(e2["n"] == 3 and e2["adopted"], "已采纳不重复写词表", str(e2))
with open(gl_tmp, encoding="utf-8") as f:
    gl2 = json.load(f)
ok(len(gl2.get("*") or []) == len(star), "词表条数未膨胀")

print("== ③ 已在词表的不再采纳 ==")
li._ger_learn_note("既有词写错了叫既有磁", "既有磁写错了叫既有词", "zh")  # right=既有词 已存在
store = li._ger_learn_load()
e3 = next((v for v in store.values() if v["right"] == "既有词"), None)
if e3:
    li._ger_learn_note("大家说既有磁", "大家说既有词", "zh")
    store = li._ger_learn_load()
    e3 = next((v for v in store.values() if v["right"] == "既有词"), None)
    ok(not e3.get("adopted"), "词表已含该词→不重复采纳", str(e3))
else:
    print("  SKIP: 该组 diff 未抽出修正对(可接受)")

print()
print("ALL PASS" if FAIL == 0 else "HAS FAIL")
sys.exit(FAIL)
