# -*- coding: utf-8 -*-
"""小智四层作答的**实例验收**（2026-08-29 P0）。

pytest 用假 LLM 证「分流代码正确」，这里用**真模型**证「分流判断正确」——
两者证的不是一回事：prompt 写得对不对、模型听不听话，只有真打才知道。
首跑就抓到一条：「帮我写一句给客户的问候语」被判成产品问题拒答了，因为原
prompt 用「与本产品无关」做判据，而这句话里有「客户」。判据已改成「是在问
产品本身，还是让我帮你做事」。

只读：只打问答端点，不写业务数据（会在 qa_log 落台账行，那本就是问答账本）。
token 从实例配置读，绝不打印。

用法：
    python tools/verify_assistant_tiers.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

import httpx  # noqa: E402
import yaml  # noqa: E402

BASE = "http://127.0.0.1:18799"
CFG = r"D:\chengjie-instances\zhiliao\data\config\config.local.yaml"
with open(CFG, encoding="utf-8") as _f:
    TOKEN = str((yaml.safe_load(_f) or {}).get("web_admin", {})
                .get("auth_token") or "")
CASES = [
    ("怎么发语音", "doc", "有文档命中：应引用条目"),
    ("支持抖音吗", "product", "能力边界：产品事实卡"),
    ("支持微信吗", "product", "能力边界：产品事实卡"),
    ("帮我写一句给客户的问候语", "general", "通用知识：应标非产品文档"),
    ("红烧肉怎么做才好吃", "general", "完全无关：通用或拒答均可接受"),
    # 乱码：模型礼貌反问「是想测试还是有别的问题」比「没找到依据」有用，
    # 且乱码本不该计入 no_hit（那个数的语义是「该补语料」）——两种都算对。
    ("qqxyzzy foobar zzzz", "*", "纯乱码：反问或拒答均可接受"),
]


def ask(c, q):
    with c.stream("POST", f"{BASE}/api/assistant/query",
                  headers={"Authorization": "Bearer " + TOKEN},
                  json={"q": q, "page": "/workspace", "lang": "zh"},
                  timeout=90) as r:
        if r.status_code != 200:
            return {"http": r.status_code}
        text, done = "", {}
        for line in r.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except Exception:
                continue
            if ev.get("ev") == "delta":
                text += ev.get("text") or ""
            elif ev.get("ev") == "done":
                done = ev
        return {"text": text, "basis": done.get("basis"),
                "answered": done.get("answered"), "ms": done.get("ms")}


if not TOKEN:
    print("NO_TOKEN in instance config")
    sys.exit(2)

with httpx.Client(follow_redirects=True, timeout=90) as c:
    ok = 0
    for q, want, why in CASES:
        r = ask(c, q)
        basis = r.get("basis")
        body = re.sub(r"\s+", " ", (r.get("text") or ""))[:70]
        if want == "*":
            hit = basis in ("general", "none")
        elif want:
            hit = basis == want
        else:
            hit = r.get("answered") is False
        # 「红烧肉」允许 general 或拒答两种正确处置
        if q.startswith("红烧肉") and (basis == "general"
                                    or r.get("answered") is False):
            hit = True
        ok += 1 if hit else 0
        print(f"[{'PASS' if hit else 'FAIL'}] {q}")
        print(f"       basis={basis} answered={r.get('answered')} "
              f"{r.get('ms')}ms  期望={want or '拒答'}  ({why})")
        print(f"       {body}")
    print(f"\n{ok}/{len(CASES)} 通过")
