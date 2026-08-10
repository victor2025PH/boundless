# -*- coding: utf-8 -*-
"""spoken_style 交付包冒烟（零外网，注入式假 LLM，红绿双向标定）。

用法：python smoke_test.py    （任意有 httpx 的 Python 3.10+ 环境）

覆盖：
  模板层  —— 自然度四档/意图篇幅分支/播报档互斥
  persona —— schema 校验、硬规则不被截断、坏类型必红
  出口层  —— 情绪标记摘净映射、[轻笑]/[呼吸] 剥净计数、残片兜底
  改写层  —— 假 LLM 注入（CONV_COLLOQ_LLM 指向本地 mock）：
             绿=合法改写放行；红=丢数字/自我暴露/问句变陈述 必须被事实锁拦下
红绿双向：断言既验「好样本必过」也验「坏样本必拒」——判据改松会当场红。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent

# ── 假 LLM：OpenAI 兼容 /v1/chat/completions，按输入原文查表回放 ────────
CASES = {
    # 绿：合法口语化（数字/英文/问句/否定全保留）
    "这家店人均 120 块，味道特别好，用的是 WiFi6 路由。":
        "他家人均 120 块诶，味道是真的真的好，用的还是 WiFi6 的路由呢。",
    # 红：数字丢失
    "他昨天跑了 5 公里，用了 30 分钟。":
        "他昨天跑步来着，跑了挺远的，花了半个来小时吧。",
    # 红：自我暴露（LEAK_RE：改写/以下是/输出：…）
    "今天天气不错，我们出去走走吧。":
        "以下是改写结果：今天天儿挺好的，咱出去溜达溜达呗。",
    # 红：问句变陈述（问答方向反转）
    "你昨天到底去没去健身房？":
        "你昨天去健身房了。",
}


class _MockLLM(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        user = next((m["content"] for m in body.get("messages", [])
                     if m.get("role") == "user"), "")
        reply = CASES.get(user.strip(), "嗯……这个嘛，我想想啊。")
        out = {"choices": [{"message": {"content": reply}}]}
        data = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):     # 静音
        pass


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _MockLLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # 必须在 import 包之前设好（colloquial_rewrite 在模块级读 env）
    os.environ["CONV_COLLOQ_LLM"] = f"http://127.0.0.1:{srv.server_port}/v1/chat/completions"
    sys.path.insert(0, str(_PKG_DIR.parent))
    import spoken_style as ss

    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = ""):
        print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail and not ok else ""))
        if not ok:
            fails.append(name)

    print("[1/4] 模板层")
    h0, h1, h2, h3 = (ss.spoken_style_hint(i) for i in range(4))
    check("档0=空串(回退语气段)", h0 == "")
    check("档1=基础说话稿", h1.startswith("【说话稿】") and "思考声" not in h1)
    check("档2含不流利度工程", "思考声" in h2 and len(h2) > len(h1))
    check("档3含故事层", "半句重启" in h3 and len(h3) > len(h2))
    check("附和轮→8~25字", "8～25 字" in ss.reply_style_hint("哈哈哈"))
    check("续讲轮→顺着话头", "顺着刚才的话头" in ss.reply_style_hint("然后呢？"))
    check("普通轮→1~3句", "1～3 句" in ss.reply_style_hint("你周末一般干嘛？"))
    check("附和判定红绿", ss.is_reactive_input("嗯嗯") and not ss.is_reactive_input("给我讲讲你老家的事"))
    check("播报档互斥", ss.turn_tail_hint("随便说说", flavor=0.3) == ss.BROADCAST_HINT)
    check("档0尾注回退语气段", ss.EMO_TEXT_HINT in ss.turn_tail_hint("你好", level=0))

    print("[2/4] persona 层")
    card = ss.load_persona(_PKG_DIR / "data" / "personas" / "Mizuki.json")
    pp = ss.persona_prompt(card)
    check("人设卡组装", pp.startswith("【人设卡】") and "请始终以这个人设的口吻说话" in pp)
    check("硬规则在场", "【必须遵守】" in pp and "没听清" in pp)
    long_card = dict(card, identity="很长的身份描述。" * 40, style="很长的风格。" * 40)
    pl = ss.persona_prompt(ss.persona_sanitize(long_card))
    check("超长卡硬规则不被截断", len(pl) <= 720 and "【必须遵守】" in pl and "没听清" in pl)
    try:
        ss.persona_sanitize({"identity": 123})
        check("坏类型必红", False, "未抛 PersonaError")
    except ss.PersonaError:
        check("坏类型必红", True)
    check("语气词白名单", ss.tone_words({"tone_words": ["啊", "呢", "哼", "呀呀"]}) == ["啊", "呢"])

    print("[3/4] 出口清洁层")
    r = ss.clean_reply("[情绪:开心|中|有点得意] 真的假的？那也太棒了吧[轻笑]。后来啊[呼吸]，我们就回去了。")
    check("情绪标记摘净映射", r["emotion"] == "happy" and r["intensity"] == "中")
    check("副语言剥净", "[" not in r["text"] and "轻笑" not in r["text"] and "呼吸" not in r["text"])
    check("副语言计数", r["paraling"]["laugh"] == 1 and r["paraling"]["breath"] == 1)
    check("呼吸停顿建议", r["pause_extra_ms"] == 250)
    frag, _ = ss.strip_paralinguistic("刚才特别开心[轻")
    check("流式残片兜底", frag == "刚才特别开心")
    blocks = ss.system_blocks(persona_card=card, role="美月", laugh=False)
    check("system 组装",
          len(blocks) == 4 and blocks[0].startswith("【人设卡】")
          and blocks[1].startswith("【说话指纹】")
          and "[呼吸]" in blocks[2] and "情绪标记" in blocks[3])

    print("[4/4] 改写层（假 LLM 注入 + 事实锁红绿）")
    cr = ss.colloquial_rewrite
    srcs = list(CASES)
    g = cr.rewrite_sync("美月", srcs[0], debug=True)
    check("合法改写放行", bool(g["out"]) and "120" in g["out"] and "WiFi6" in g["out"], str(g))
    r1 = cr.rewrite_sync("美月", srcs[1], debug=True)
    check("丢数字必拒", r1["out"] is None and "数字丢失" in r1["why"], str(r1))
    r2 = cr.rewrite_sync("美月", srcs[2], debug=True)
    check("自我暴露必拒", r2["out"] is None and "自我暴露" in r2["why"], str(r2))
    r3 = cr.rewrite_sync("美月", srcs[3], debug=True)
    check("问句变陈述必拒", r3["out"] is None and "问句变陈述" in r3["why"], str(r3))
    check("说话指纹块在场", cr.prompt_block("美月").startswith("【说话指纹】"))

    srv.shutdown()
    print()
    if fails:
        print(f"冒烟失败 {len(fails)} 项: " + "、".join(fails))
        return 1
    print("冒烟全绿（模板/persona/出口/改写 四层，红绿双向标定通过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
