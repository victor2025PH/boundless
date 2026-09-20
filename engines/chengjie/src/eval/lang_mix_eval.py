# -*- coding: utf-8 -*-
"""出站混语收口评测（#97 实施91，确定性，零网络/零 LLM）。

安全/体验不变量：**拉丁主体夹 CJK 残字的出站文本，经发送收口点后必须零 CJK**。

背景（两代实锤，同一报障人四连报）：

- 0826 ``I'm 我, and I'm not going anywhere.``（#64 首报，修复挂在 A/B 出稿口
  与三条翻译出口）；
- 0830 21:47 ``I'm 我 the one who's still here, still listening.``（#97 击穿单：
  v1.0.63 已带 #64 修复仍出站——主动触达/关怀等 deferred 链不经出稿口、英文
  客户不触发翻译出口，整条 orch.send 直发路径裸奔）。

修后架构（本评测钉住的双层防线）：

1. **出稿口** ``apply_outbound_text_guard``（A 线 process_message / B 线
   generate_inbox_draft，#64 原位不动）；
2. **发送收口点** ``sendpoint_lang_mix_pass``（AccountOrchestrator.send 全部
   自动链 + A 线 sender._send_reply 经 outbound_quality_pass）——无论文本从
   哪条链来、中途被谁改写，出门前必过这一道。

两层都吃同一批金标：hard 样本两层各自独立过必须零 CJK 残字；legit 样本
（中文主体夹品牌词/引用整句英文/人手短语）必须一字不动——误伤与漏拦同罪。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


@dataclass
class LangMixSample:
    text: str
    expect: str          # "strip"=必须零 CJK 出站 / "keep"=必须一字不动
    note: str = ""


# 金标：strip 样本 = 实锤事故句 + 同形变体；keep 样本 = 历史上明确「刻意不拦」
# 的合法形态（改判其中任何一条前先读 outbound_text_guard 头部设计原则）。
GOLDEN_SAMPLES: List[LangMixSample] = [
    # ── 必须剥除（拉丁主体夹 CJK 残字） ──
    LangMixSample(
        "I'm 我 the one who's still here, still listening.",
        "strip", "#97 0830 21:47 击穿实锤（v1.0.63 修复后新产生）"),
    LangMixSample(
        "But I'm 我 actually curious, not just making small talk.",
        "strip", "#97 0831 10:55 第五例（修复空窗期，v1.0.64 环境）"),
    LangMixSample(
        "I'm 我, and I'm not going anywhere.",
        "strip", "#64 0826 首报实锤"),
    LangMixSample(
        "Good morning 亲爱的! How did you sleep last night?",
        "strip", "英文会话夹中文称呼——同病词形变体"),
    LangMixSample(
        "Sure 好的, I'll send it over tomorrow after work.",
        "strip", "英文会话夹中文应答词"),
    LangMixSample(
        "That sounds great 呢, can't wait to see you there!",
        "strip", "英文句夹中文语气助词"),
    # ── 必须原样（合法混写/守卫刻意不动的形态） ──
    LangMixSample(
        "我买了新手机（iPhone），OK 吧？",
        "keep", "中文主体夹品牌词/短英文——合法"),
    LangMixSample(
        "im 我",
        "keep", "剥后过短安全阀（测试钉死的旧行为）——保留原文"),
    LangMixSample(
        "老师今天让我们把这句英文抄十遍背下来：Practice makes perfect and "
        "never give up！我抄到手都酸了哈哈",
        "keep", "中文主体引用整句英文（语言教学场景）——只观测不动手"),
    LangMixSample(
        "See you tomorrow, take care!",
        "keep", "纯英文"),
    LangMixSample(
        "明天见，路上小心呀",
        "keep", "纯中文"),
    LangMixSample(
        "下载链接 https://example.com/智聊/setup.exe 记得查收哦",
        "keep", "URL 豁免（链接里的字符不参与统计）"),
]


def _cjk_count(text: str) -> int:
    return len(_CJK_RE.findall(text or ""))


def evaluate_lang_mix(
    samples: Optional[List[LangMixSample]] = None,
) -> Dict[str, Any]:
    """跑出站混语收口评测；返回逐层结果 + passed（全对才过——确定性不变量）。

    两层独立断言：``sendpoint``＝发送收口点（#97 新防线，全链最后一站）；
    ``draftgate``＝出稿口（#64 原防线，lang_mix 单项）。strip 样本要求该层
    输出零 CJK；keep 样本要求逐字不变。
    """
    from src.ai.outbound_text_guard import (
        apply_outbound_text_guard, sendpoint_lang_mix_pass)

    rows = samples if samples is not None else GOLDEN_SAMPLES
    errors: List[Dict[str, Any]] = []
    correct = 0
    for s in rows:
        sp_out, sp_act = sendpoint_lang_mix_pass(s.text)
        dg_out, _meta = apply_outbound_text_guard(
            s.text, {"enabled": True, "monologue": False, "lang_mix": True,
                     "unfounded_recall": False, "recall_grounding": False,
                     "apology_dedup": False})
        ok = True
        if s.expect == "strip":
            if _cjk_count(sp_out) > 0:
                ok = False
                errors.append({"layer": "sendpoint", "text": s.text[:50],
                               "got": sp_out[:50], "act": sp_act,
                               "note": s.note, "why": "收口点出站仍带 CJK 残字"})
            if _cjk_count(dg_out) > 0:
                ok = False
                errors.append({"layer": "draftgate", "text": s.text[:50],
                               "got": dg_out[:50], "note": s.note,
                               "why": "出稿口出站仍带 CJK 残字"})
        else:
            if sp_out != s.text:
                ok = False
                errors.append({"layer": "sendpoint", "text": s.text[:50],
                               "got": sp_out[:50], "act": sp_act,
                               "note": s.note, "why": "合法文本被误伤"})
            if dg_out != s.text:
                ok = False
                errors.append({"layer": "draftgate", "text": s.text[:50],
                               "got": dg_out[:50], "note": s.note,
                               "why": "合法文本被误伤"})
        if ok:
            correct += 1

    n = len(rows)
    passed = n > 0 and correct == n
    return {
        "summary": {"total": n, "correct": correct,
                    "accuracy": round(correct / n, 3) if n else 0.0},
        "errors": errors,
        "passed": passed,
    }


def format_lang_mix_report(report: Dict[str, Any]) -> str:
    m = report["summary"]
    lines = [
        "=== 出站混语收口评测报告（#97） ===",
        f"样本: {m['total']}  全层合格: {m['correct']}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    if report["errors"]:
        lines.append(f"违例 {len(report['errors'])} 处:")
        for e in report["errors"][:20]:
            lines.append(
                f"  - [{e['layer']}] 「{e['text']}」→「{e['got']}」 "
                f"{e['why']}（{e['note']}）")
    return "\n".join(lines)


__all__ = [
    "LangMixSample", "GOLDEN_SAMPLES",
    "evaluate_lang_mix", "format_lang_mix_report",
]
