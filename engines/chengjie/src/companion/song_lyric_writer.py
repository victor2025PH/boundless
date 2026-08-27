# -*- coding: utf-8 -*-
"""专属歌填词器（实施58 P2）：LLM 按聊天素材写 4 句歌词 + 纯函数硬闸。

设计不变量：
- **等长句式硬闸**：每句 6~9 个汉字——2026-08-23 夜实测教训：换词重唱系统性
  吞短句（《晚风》第二句 5 字，甜嗓 5 抽全败在它身上）。闸在词端根治。
- **素材只许来自订单自带的客户原话**（facts）：prompt 明令「只用素材里对方
  亲口说的事」，闸不验语义（LLM 编不编由人审兜底），但验硬红线：敏感词
  （财务/联系方式）零容忍、连续数字≥3 禁（电话号）、真人歌手/歌名禁提示、
  行数/长度/重复行硬性不过。
- 生成 ≤N 次重试，闸间反馈失败原因给 LLM（负样本重写，proactive variety 同款）。
门禁：tests/test_song_lyric_writer.py。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("ai_chat_assistant.song_lyric_writer")

LINE_COUNT = 4
LINE_MIN = 6
LINE_MAX = 9

# 硬红线词表（宁严勿松——歌词是要唱出去的，比文本更难撤回）
_SENSITIVE = (
    "转账", "汇款", "验证码", "银行卡", "密码", "微信号", "加我", "二维码",
    "投资", "充值", "下注", "贷款", "身份证",
)
# 真人歌手/常见歌名词（版权红线的词端保险丝；主防线在 prompt）
_COPYRIGHT_HINTS = ("周杰伦", "邓紫棋", "林俊杰", "七里香", "泡沫", "晴天")


def _cjk(s: str) -> str:
    return "".join(c for c in (s or "") if "\u4e00" <= c <= "\u9fff")


def validate_lyrics(text: str) -> Tuple[bool, str, List[str]]:
    """歌词硬闸（纯函数）。回 (ok, 原因, 规整后的行)。"""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if len(lines) != LINE_COUNT:
        return False, f"line_count:{len(lines)}", lines
    cleaned: List[str] = []
    for ln in lines:
        if re.search(r"\d{3,}", ln):
            return False, "digits", lines
        low = ln.lower()
        for w in _SENSITIVE:
            if w in ln:
                return False, f"sensitive:{w}", lines
        for w in _COPYRIGHT_HINTS:
            if w in ln:
                return False, f"copyright:{w}", lines
        if re.search(r"https?://|www\.", low):
            return False, "url", lines
        n = len(_cjk(ln))
        if not (LINE_MIN <= n <= LINE_MAX):
            return False, f"line_len:{n}", lines
        cleaned.append(ln)
    if len(set(cleaned)) != LINE_COUNT:
        return False, "dup_line", lines
    return True, "ok", cleaned


def build_lyric_prompt(*, persona_name: str, peer_name: str,
                       facts: List[str], retry_feedback: str = "") -> str:
    """填词 prompt（单一入口；worker 与测试同口径）。"""
    facts_block = "\n".join(f"- {f.strip()[:80]}" for f in facts[:6]
                            if str(f or "").strip()) or "-（无素材：写温柔陪伴）"
    name_line = (f"对方叫「{peer_name}」，把这个称呼自然唱进歌里（一次即可）。"
                 if peer_name else "不知道对方名字，不要编造称呼。")
    fb = f"\n上一稿被打回，原因：{retry_feedback}。请修正后重写。\n" \
        if retry_feedback else ""
    return (
        f"你是「{persona_name}」，要给聊天对象写一小段清唱歌词（会真的唱出来）。\n"
        f"{name_line}\n"
        "对方最近亲口说过的话（唯一允许的素材来源，不许编造别的事实）：\n"
        f"{facts_block}\n"
        "硬性要求：\n"
        f"1. 恰好 {LINE_COUNT} 行，每行 {LINE_MIN}~{LINE_MAX} 个汉字，"
        "各行长度尽量一致（这是唱出来的，长短句会唱劈）。\n"
        "2. 只输出歌词本身，不要引号/序号/解释。\n"
        "3. 温柔口语，尽量押韵；可以有画面感。\n"
        "4. 禁止：数字串、金钱/转账/联系方式、真实歌手或歌名、"
        "承诺性的话（明天见/我会永远……）。\n"
        f"{fb}"
    )


def write_custom_lyrics(
    llm_chat: Callable[[str], str], *, persona_name: str, peer_name: str,
    facts: List[str], tries: int = 3,
) -> Tuple[Optional[str], Dict[str, Any]]:
    """LLM 填词 ≤tries 次，闸间带失败原因重写。回 (歌词 or None, meta)。"""
    meta: Dict[str, Any] = {"attempts": []}
    feedback = ""
    for i in range(1, max(1, tries) + 1):
        try:
            raw = str(llm_chat(build_lyric_prompt(
                persona_name=persona_name, peer_name=peer_name,
                facts=facts, retry_feedback=feedback)) or "")
        except Exception as e:  # noqa: BLE001
            meta["attempts"].append({"n": i, "error": str(e)[:120]})
            continue
        ok, why, lines = validate_lyrics(raw)
        meta["attempts"].append({"n": i, "why": why,
                                 "raw": raw[:200]})
        if ok:
            meta["ok_attempt"] = i
            return "\n".join(lines), meta
        feedback = why
    return None, meta


__all__ = ["validate_lyrics", "build_lyric_prompt", "write_custom_lyrics",
           "LINE_COUNT", "LINE_MIN", "LINE_MAX"]
