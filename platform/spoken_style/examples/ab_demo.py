# -*- coding: utf-8 -*-
"""spoken_style 效果对照 demo（同时是接入姿势的活文档）。

用法（任何有 httpx 的 Python 3.10+）：
    python ab_demo.py                     # 默认输入集 → ../docs/AB_SAMPLES.md 对照表
    python ab_demo.py --ask "你周末一般干嘛"   # 单条提问，终端直接看三版对照
    python ab_demo.py --no-rewrite        # 跳过 L4（没有本地小模型端点时）

三版对照 = 智聊现状(裸 system) vs 挂包(L1+L2+L3) vs 挂包+L4 口语化改写。
LLM 端点用 CONV_COLLOQ_LLM / CONV_COLLOQ_MODEL（缺省 .173 qwen14b，改写层同一后端）。
接入正式管线时照 _styled_reply() 抄挂点即可——它就是 README「快速接入」三步的实跑版。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

_PKG_PARENT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PKG_PARENT))

import httpx

import spoken_style as ss

LLM_URL = os.environ.get("CONV_COLLOQ_LLM",
                         "http://192.168.0.173:11434/v1/chat/completions")
LLM_MODEL = os.environ.get("CONV_COLLOQ_MODEL", "qwen14b-fallback")

# 覆盖五类输入意图：寒暄 / 附和 / 闲聊 / 知识问答 / 故事（附和与故事会走不同尾注分支）
DEFAULT_INPUTS = [
    "早啊",
    "哈哈哈",
    "你周末一般都干嘛呀",
    "咖啡喝多了对身体不好吗",
    "给我讲讲你小时候印象最深的一件事",
]

BARE_SYSTEM = "你是一个智能助手，请认真回答用户的问题。"


def _llm(system: str, user: str, max_tokens: int = 400) -> str:
    r = httpx.post(LLM_URL, json={
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.7, "max_tokens": max_tokens, "stream": False,
    }, timeout=60)
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()


def _bare_reply(user_text: str) -> str:
    return _llm(BARE_SYSTEM, user_text)


def _styled_reply(user_text: str, card: dict, role: str) -> dict:
    """README「快速接入」三步的实跑版——接正式管线照这里抄。"""
    # ① 稳定 system（会话级只拼一次；业务自己的 system 拼在最前）
    sys_prompt = BARE_SYSTEM + "\n\n" + "\n\n".join(
        ss.system_blocks(persona_card=card, role=role, laugh=False, emotion_tags=True))
    # ② 轮变尾注：故事/续讲意图升到档 3，其余档 2
    level = 3 if ss.naturalness.STORY_INTENT_RE.search(user_text) else 2
    tail = ss.turn_tail_hint(user_text, level=level)
    raw = _llm(sys_prompt, user_text + tail)
    # ③ 出口清洁：字幕/TTS 都用 clean 后文本；emotion 直接喂 CosyVoice /v1/tts/clone
    return ss.clean_reply(raw)


def _rewrite(role: str, text: str) -> str | None:
    """L4：与生产同一入口（事实锁/超时直通都在里面）。
    生产管线（async 环境）用 build_rewrite_fn 返回的 async fn；
    demo 是同步脚本，旗标开了就 asyncio.run 包一层，没开走同步调试口。"""
    import asyncio
    fn = ss.colloquial_rewrite.build_rewrite_fn(role)
    if fn is None:                       # 旗标没开时给 demo 兜底：直接走同步调试口
        info = ss.colloquial_rewrite.rewrite_sync(role, text, debug=True)
        return info["out"]
    return asyncio.run(fn(text, first=True))


def run(inputs: list[str], do_rewrite: bool, out_path: Path | None) -> None:
    card = ss.load_persona(Path(__file__).parent.parent / "data" / "personas" / "Mizuki.json")
    role = "美月"
    rows = []
    for q in inputs:
        print(f"—— 输入：{q}", flush=True)
        bare = _bare_reply(q)
        styled = _styled_reply(q, card, role)
        rw = _rewrite(role, styled["text"]) if do_rewrite else None
        rows.append((q, bare, styled, rw))
        print(f"   裸: {bare[:60]}")
        print(f"   挂包[{styled['emotion'] or '–'}]: {styled['text'][:60]}")
        if do_rewrite:
            print(f"   +改写: {(rw or '(直通)')[:60]}")

    if out_path is None:
        return
    lines = [
        "# 挂包前后对照样本（自动生成，人眼验收用）", "",
        f"- 生成：{date.today()} · 模型 `{LLM_MODEL}` @ `{LLM_URL.split('//')[1].split('/')[0]}`",
        "- 三版 = 裸 system（智聊现状）/ 挂包 L1+L2+L3（人设卡+说话指纹+说话稿档位+出口清洁）"
        "/ 再加 L4 口语化改写（事实锁把关，None=直通）",
        "- 温度 0.7，逐次生成会有波动；看的是**语域差**（书面腔 vs 嘴里的话），不是逐字复现", "",
    ]
    for q, bare, styled, rw in rows:
        lines += [f"## 「{q}」", "",
                  f"**裸 LLM**：{bare}", "",
                  f"**挂包**（情绪 `{styled['emotion'] or '无'}`）：{styled['text']}", ""]
        if rw is not None or do_rewrite:
            lines += [f"**挂包+L4 改写**：{rw or '（事实锁/短句直通，用挂包版）'}", ""]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n对照表已写入 {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ask", help="单条提问（不落盘）")
    ap.add_argument("--no-rewrite", action="store_true", help="跳过 L4 改写")
    ap.add_argument("--out", default=str(Path(__file__).parent.parent / "docs" / "AB_SAMPLES.md"))
    a = ap.parse_args()
    if a.ask:
        run([a.ask], not a.no_rewrite, None)
    else:
        run(DEFAULT_INPUTS, not a.no_rewrite, Path(a.out))
