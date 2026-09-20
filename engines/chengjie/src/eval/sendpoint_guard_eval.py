# -*- coding: utf-8 -*-
"""出站收口点守卫评测（实施91 · #105 呼格 / #106 铆定语言，确定性零网络）。

与 ``lang_mix_eval``（#97 混语面）合成收口点三连的完整验收网：

- **#105 呼格金标**（0831 10:03 实锤）：英文客户 Crisceya 收到的问候**语音**
  念稿喊「baba」——右栏跨平台档案明记「偏好被叫 babe」，数据是对的，守卫
  没罩语音链。金标断言：档案双向称呼（call_peer=babe / peer_calls_you=baba）
  在场时，呼格位置的 baba 必须纠回 babe；语音链入口（presynth）同样成立。
- **#96 呼格金标**（0830 21:23 实锤）：AI 拿我方人设名当客户称呼
  （「…judge it with me, Steven.」）——人设名呼格必须剥除（peer 名未知照判）。
- **#106 铆定金标**（0831 10:40 实锤）：会话铆「发→英」，图片轮三连纯中文
  直发——铆定语言 × 文本文字系统冲突必须被检出；合法形态（已是铆定语言 /
  同文字系统差异）绝不误报。

金标句全部来自工单原图（840/863/869），改任何一条先读工单 #97/#105/#106。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# #105 原句（原图 863 语音念稿）；档案：call_peer=babe / peer_calls_you=baba
INCIDENT_105_VOICE = ("Seven days with you, and I can't imagine "
                      "my mornings without you, baba.")
# #96 原句（原图 833 出站）；人设名 Steven 被安到客户头上
INCIDENT_96_TEXT = "…wish you were there to judge it with me, Steven."
# #106 原句（原图 869，铆「发→英」仍纯中文三连之一）
INCIDENT_106_ZH = "哈哈，这构图还挺有味道的，那个戴墨镜的男士是主角吗？"
# #154 原句（0903 backend.log 14:50:35/14:50:45/14:58:15/14:58:24，会话
# telegram:7331682688:8852939166 铆 en，A 线直发日语四条）。13:11:09 同会话
# sendpoint 还在按 pin=en 改写，14:50 后 pin 读空——铆定被前端偏好同步清了。
INCIDENT_154_JA = [
    "うん、ちゃんと休むよ。",
    "風は任せて、いい夢を届けるからね。 おやすみ🌙",
    "うん、ちゃんと目閉じるね。",
    "おやすみ、また明日ね🌙",
]


@dataclass
class VocativeSample:
    text: str
    names: Dict[str, Any]
    must_not_contain: List[str] = field(default_factory=list)
    must_contain: List[str] = field(default_factory=list)
    expect_unchanged: bool = False
    note: str = ""


VOCATIVE_GOLDENS: List[VocativeSample] = [
    VocativeSample(
        INCIDENT_105_VOICE,
        {"call_peer": "babe", "peer_calls_you": "baba", "self_names": []},
        must_not_contain=["baba"], must_contain=["babe"],
        note="#105 0831 10:03 语音念稿实锤（互换守卫）"),
    VocativeSample(
        "Good morning baba, did you sleep well?",
        {"call_peer": "babe", "peer_calls_you": "baba", "self_names": []},
        must_not_contain=["baba"], must_contain=["babe"],
        note="#24 0830 原案句形（问候+称呼）"),
    VocativeSample(
        # 档案只配了 call_peer（peer_calls_you 空）→ 近形纠正兜住
        "Miss you baba, come back soon.",
        {"call_peer": "babe", "peer_calls_you": "", "self_names": []},
        must_not_contain=["baba"], must_contain=["babe"],
        note="#105 补强：单字段档案走 call_peer 近形纠正"),
    VocativeSample(
        INCIDENT_96_TEXT,
        {"call_peer": "", "peer_calls_you": "", "self_names": ["Steven"]},
        must_not_contain=["Steven"],
        note="#96 0830 21:23 实锤（人设名当客户呼格，peer 未知照判）"),
    # ── 反例（绝不误伤） ──
    VocativeSample(
        "你叫我 baba 的时候好可爱",
        {"call_peer": "babe", "peer_calls_you": "baba", "self_names": []},
        expect_unchanged=True,
        note="元语句（讨论称呼本身）整句放行"),
    VocativeSample(
        "I made baba ganoush for dinner, want the recipe?",
        {"call_peer": "babe", "peer_calls_you": "baba", "self_names": []},
        expect_unchanged=True,
        note="非呼格位置的普通词——不动"),
    VocativeSample(
        "Good night babe, sweet dreams.",
        {"call_peer": "babe", "peer_calls_you": "baba", "self_names": []},
        expect_unchanged=True,
        note="已是正确爱称——不动"),
]


@dataclass
class LangPinSample:
    text: str
    pin: str
    expect_conflict: bool
    note: str = ""


LANG_PIN_GOLDENS: List[LangPinSample] = [
    LangPinSample(INCIDENT_106_ZH, "en", True,
                  "#106 0831 10:40 实锤：铆英发纯中文"),
    LangPinSample("那个戴墨镜的男士是主角吗？", "en", True,
                  "#106 同轮第二条"),
    LangPinSample("一副“我很酷”的样子。", "en", True,
                  "#106 同轮第三条"),
    LangPinSample("Haha, this composition has real character!", "en", False,
                  "已是铆定语言——不动"),
    LangPinSample("这张照片拍得真好看，构图很讲究", "zh", False,
                  "铆中文发中文——不动"),
    LangPinSample("一起加油", "zh-tw", False,
                  "zh 家族变体差异不在收口点判（翻译层管字形，实施89）"),
    LangPinSample("OK", "en", False, "短文本不判"),
    LangPinSample("Long English sentence with no CJK at all here.",
                  "zh", True, "反向：铆中文发纯英长句"),
] + [
    LangPinSample(_s, "en", True,
                  "#154 0903 A 线直发日语（铆 en 被程序清空后漏网）")
    for _s in INCIDENT_154_JA
]


def evaluate_sendpoint_guard(
    voc_samples: Optional[List[VocativeSample]] = None,
    pin_samples: Optional[List[LangPinSample]] = None,
) -> Dict[str, Any]:
    """跑收口点呼格/铆定评测；返回逐项结果 + passed（全对才过）。

    呼格金标同时过**文本收口**（sendpoint_vocative_pass）与**语音收口**
    （presynth 同一函数族）——#105 的击穿正是「文字面修了、语音面裸奔」，
    两面各自独立断言。铆定金标只验确定性冲突检出（翻译修正端到端属
    translate_outbound_text 既有门禁，不在这里重造）。
    """
    from src.ai.sendpoint_guard import (
        pin_script_conflict, sendpoint_vocative_pass)

    errors: List[Dict[str, Any]] = []
    rows_v = voc_samples if voc_samples is not None else VOCATIVE_GOLDENS
    correct_v = 0
    for s in rows_v:
        out, _meta = sendpoint_vocative_pass(s.text, s.names)
        ok = True
        if s.expect_unchanged and out != s.text:
            ok = False
            errors.append({"layer": "vocative", "text": s.text[:60],
                           "got": out[:60], "note": s.note,
                           "why": "合法文本被误伤"})
        for bad in s.must_not_contain:
            if bad in out:
                ok = False
                errors.append({"layer": "vocative", "text": s.text[:60],
                               "got": out[:60], "note": s.note,
                               "why": f"坏呼格「{bad}」仍在出站文本"})
        for good in s.must_contain:
            if good not in out:
                ok = False
                errors.append({"layer": "vocative", "text": s.text[:60],
                               "got": out[:60], "note": s.note,
                               "why": f"应纠正为「{good}」未出现"})
        if ok:
            correct_v += 1

    rows_p = pin_samples if pin_samples is not None else LANG_PIN_GOLDENS
    correct_p = 0
    for p in rows_p:
        got = pin_script_conflict(p.text, p.pin)
        if got == p.expect_conflict:
            correct_p += 1
        else:
            errors.append({"layer": "lang_pin", "text": p.text[:60],
                           "got": str(got), "note": p.note,
                           "why": ("漏检铆定语言冲突" if p.expect_conflict
                                   else "无冲突被误报")})

    n = len(rows_v) + len(rows_p)
    correct = correct_v + correct_p
    passed = n > 0 and correct == n
    return {
        "summary": {"total": n, "correct": correct,
                    "vocative": f"{correct_v}/{len(rows_v)}",
                    "lang_pin": f"{correct_p}/{len(rows_p)}",
                    "accuracy": round(correct / n, 3) if n else 0.0},
        "errors": errors,
        "passed": passed,
    }


def format_sendpoint_guard_report(report: Dict[str, Any]) -> str:
    m = report["summary"]
    lines = [
        "=== 出站收口点守卫评测报告（#105/#106） ===",
        f"样本: {m['total']}  合格: {m['correct']}  呼格: {m['vocative']}  "
        f"铆定: {m['lang_pin']}  "
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
    "VocativeSample", "LangPinSample",
    "VOCATIVE_GOLDENS", "LANG_PIN_GOLDENS",
    "INCIDENT_105_VOICE", "INCIDENT_96_TEXT", "INCIDENT_106_ZH",
    "INCIDENT_154_JA",
    "evaluate_sendpoint_guard", "format_sendpoint_guard_report",
]
