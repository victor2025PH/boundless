# -*- coding: utf-8 -*-
"""P11c 批量 dry-run 矩阵 — 扫不同 (intent, persona, peer_type, contact)
组合跑 B 的 LLM-in-the-loop 链路, 发现 silent failure + 建立输出基线。

动机: P11b 真机 dry-run 一次就发现 UserProfile 签名 bug (生产 auto_reply
从未真正生成 reply)。类似的 silent failure 可能藏在其他代码路径里, 本
工具系统性覆盖所有组合。

测试矩阵 (笛卡尔积):
  * incoming 类型: opening / buying / referral_ask / objection / smalltalk / cold / closing
  * persona_key:   jp_female_midlife / it_male_midlife / us_female_midlife / ""
  * peer_type:     friend / stranger
  * has_contact:   True / False

默认 7 × 4 × 2 × 2 = 112 组合, 每个跑一次 run_dryrun (P11b), 聚合统计:
  * 每组合的 intent / gate level / decision / error
  * 各 intent 触发率 / 各 decision 分布
  * 新发现的 silent failure (error 非空)

用法:
    python scripts/messenger_batch_dryrun.py --device <did>
    python scripts/messenger_batch_dryrun.py --device <did> --no-llm
    python scripts/messenger_batch_dryrun.py --device <did> --sample 20
    python scripts/messenger_batch_dryrun.py --device <did> --json > report.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("batch_dryrun")


GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BLUE = "\033[34m"
CYAN = "\033[36m"
RESET = "\033[0m"
BOLD = "\033[1m"

if os.environ.get("NO_COLOR") or (sys.platform == "win32" and
                                    not os.environ.get("ANSICON")):
    try:
        os.system("")
    except Exception:
        GREEN = YELLOW = RED = BLUE = CYAN = RESET = BOLD = ""


# ─────────────────────────────────────────────────────────────────────────────
# 测试样本 — 每种意图的代表性 incoming (多语言 + 多 persona 预期)
# ─────────────────────────────────────────────────────────────────────────────

INCOMING_SAMPLES: List[Tuple[str, str, str]] = [
    # (expected_intent, lang_hint, text)
    # opening 样本删除 — 矩阵测试永远 seed history, peer_turns 非空必然不返
    # opening; opening 的 unit test 在 test_chat_intent.py 覆盖
    ("buying",       "ja", "値段教えてください"),
    ("buying",       "en", "How much does it cost?"),
    ("buying",       "it", "quanto costa"),
    ("buying",       "zh", "多少钱?"),
    ("referral_ask", "ja", "LINEでも連絡できますか?"),
    ("referral_ask", "en", "Do you have WhatsApp?"),
    ("referral_ask", "it", "qual è il tuo numero"),
    ("referral_ask", "zh", "加你微信"),
    ("closing",      "ja", "またね、おやすみ"),
    ("closing",      "en", "bye, see you later"),
    ("closing",      "it", "ciao a presto"),
    ("cold",         "",   "ok"),
    ("cold",         "",   "😀😀"),
    ("cold",         "zh", "嗯"),
    ("smalltalk",    "ja", "今日はいい天気ですね"),
    ("smalltalk",    "en", "hope you are doing well"),
    ("smalltalk",    "it", "oggi è stata una bella giornata"),  # 避开 "ciao" (与 closing 歧义)
    ("interest",     "en", "tell me more about your service"),
    ("objection",    "en", "honestly I'm a bit skeptical about this"),
]

PERSONAS = [
    "jp_female_midlife",
    "it_male_midlife",
    "us_female_midlife",
    "",  # 无 persona
]

PEER_TYPES = ["friend", "stranger"]

CONTACT_OPTS: List[Tuple[str, str]] = [
    # (label, contact_string)
    ("line",     "line:abc123"),
    ("wa",       "wa:+81900000"),
    ("none",     ""),
]


# ─────────────────────────────────────────────────────────────────────────────
# 单次组合运行 (复用 P11b)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CaseResult:
    case_id: str
    expected_intent: str
    lang_hint: str
    incoming: str
    persona_key: str
    peer_type: str
    contact_label: str
    # 输出
    actual_intent: str = ""
    intent_source: str = ""
    gate_level: str = ""
    decision: str = ""
    has_llm_reply: bool = False
    llm_reply_preview: str = ""
    final_reply_preview: str = ""
    errors: List[str] = field(default_factory=list)
    duration_ms: int = 0
    # 断言
    intent_match: Optional[bool] = None  # vs expected_intent

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def run_matrix_cases(device_id: str, peer_name: str,
                     use_llm: bool = False,
                     sample_limit: Optional[int] = None) -> List[CaseResult]:
    """跑整个矩阵, 返 CaseResult 列表。

    peer_name 固定为一个测试 peer (临时 seed history 让 peer_turns 非空,
    避免所有 case 都被判 opening)。
    """
    import time
    from scripts.messenger_production_dryrun import run_dryrun

    # seed 一点 history 让 chat_memory 的 peer_turns 非空
    # 否则所有 incoming 都被 rule 判为 opening
    try:
        from src.host.fb_store import record_inbox_message
        # 仅种 1 outgoing + 1 incoming 当 "前置轮",  preset_key 隔离便于 cleanup
        record_inbox_message(device_id, peer_name,
                             direction="outgoing",
                             message_text="(batch_dryrun prior greeting)",
                             peer_type="friend",
                             preset_key="batch_dryrun")
        record_inbox_message(device_id, peer_name,
                             direction="incoming",
                             message_text="(batch_dryrun prior incoming)",
                             peer_type="friend",
                             preset_key="batch_dryrun")
    except Exception as e:
        log.warning("seed history 失败: %s", e)

    cases: List[CaseResult] = []
    case_idx = 0
    for (expected, lang, text) in INCOMING_SAMPLES:
        for persona in PERSONAS:
            for peer_type in PEER_TYPES:
                for (contact_label, contact) in CONTACT_OPTS:
                    case_idx += 1
                    if sample_limit and case_idx > sample_limit:
                        break
                    cid = (f"C{case_idx:03d}"
                           f"[{expected}/{lang or '-'}/{persona or '_'}/"
                           f"{peer_type}/{contact_label}]")
                    r = CaseResult(
                        case_id=cid, expected_intent=expected,
                        lang_hint=lang, incoming=text,
                        persona_key=persona, peer_type=peer_type,
                        contact_label=contact_label,
                    )
                    t0 = time.time()
                    try:
                        d = run_dryrun(
                            device_id=device_id, peer_name=peer_name,
                            incoming_text=text, from_inbox=False,
                            referral_contact=contact,
                            persona_key=persona,
                            use_llm=use_llm,
                            peer_type=peer_type,
                        )
                        r.actual_intent = d.intent
                        r.intent_source = d.intent_source
                        r.gate_level = d.gate_level
                        r.decision = d.decision
                        r.has_llm_reply = bool(d.llm_reply_text)
                        r.llm_reply_preview = (d.llm_reply_text or "")[:80]
                        r.final_reply_preview = (d.final_reply or "")[:80]
                        r.errors = list(d.errors)
                        # intent 匹配检查 (opening 样本跳过 — peer_turns 非空
                        # 所以永远不会返 opening, 由 rule-first 规则决定)
                        if expected != "opening":
                            r.intent_match = (d.intent == expected)
                    except Exception as e:
                        r.errors.append(f"run_dryrun 抛异常: {str(e)[:100]}")
                    r.duration_ms = int((time.time() - t0) * 1000)
                    cases.append(r)
                if sample_limit and case_idx >= sample_limit:
                    break
            if sample_limit and case_idx >= sample_limit:
                break
        if sample_limit and case_idx >= sample_limit:
            break

    # cleanup seed
    try:
        from src.host.database import _connect
        with _connect() as conn:
            conn.execute("DELETE FROM facebook_inbox_messages"
                         " WHERE preset_key='batch_dryrun'")
    except Exception:
        pass

    return cases


# ─────────────────────────────────────────────────────────────────────────────
# 聚合统计
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MatrixReport:
    total_cases: int = 0
    intent_distribution: Dict[str, int] = field(default_factory=dict)
    intent_match_rate: float = 0.0
    intent_mismatches: List[str] = field(default_factory=list)
    decision_distribution: Dict[str, int] = field(default_factory=dict)
    gate_level_distribution: Dict[str, int] = field(default_factory=dict)
    errors_by_case: List[Tuple[str, List[str]]] = field(default_factory=list)
    cases_with_reply: int = 0
    cases_with_final_reply: int = 0
    avg_duration_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def aggregate(cases: List[CaseResult]) -> MatrixReport:
    r = MatrixReport(total_cases=len(cases))
    if not cases:
        return r
    match_n = match_total = 0
    for c in cases:
        r.intent_distribution[c.actual_intent] = \
            r.intent_distribution.get(c.actual_intent, 0) + 1
        r.decision_distribution[c.decision] = \
            r.decision_distribution.get(c.decision, 0) + 1
        r.gate_level_distribution[c.gate_level] = \
            r.gate_level_distribution.get(c.gate_level, 0) + 1
        if c.has_llm_reply:
            r.cases_with_reply += 1
        if c.final_reply_preview:
            r.cases_with_final_reply += 1
        if c.errors:
            r.errors_by_case.append((c.case_id, c.errors))
        if c.intent_match is not None:
            match_total += 1
            if c.intent_match:
                match_n += 1
            else:
                r.intent_mismatches.append(
                    f"{c.case_id} expected={c.expected_intent} "
                    f"actual={c.actual_intent}")
    r.intent_match_rate = (round(match_n / match_total, 3)
                            if match_total else 0.0)
    r.avg_duration_ms = int(sum(c.duration_ms for c in cases) / len(cases))
    return r


# ─────────────────────────────────────────────────────────────────────────────
# 渲染
# ─────────────────────────────────────────────────────────────────────────────

def render(cases: List[CaseResult], report: MatrixReport) -> str:
    lines: List[str] = []
    lines.append(f"\n{BOLD}=== Batch Dry-Run Matrix ==={RESET}")
    lines.append(f"total cases: {report.total_cases}  "
                  f"avg_duration: {report.avg_duration_ms}ms")
    lines.append("")

    lines.append(f"{BOLD}## Intent 识别准确率{RESET}")
    rate = report.intent_match_rate
    rate_color = GREEN if rate >= 0.8 else (YELLOW if rate >= 0.5 else RED)
    lines.append(f"  match_rate: {rate_color}{rate:.0%}{RESET}")
    if report.intent_mismatches:
        lines.append(f"  {YELLOW}mismatches:{RESET}")
        for m in report.intent_mismatches[:15]:
            lines.append(f"    • {m}")
        if len(report.intent_mismatches) > 15:
            lines.append(f"    ... 省略 {len(report.intent_mismatches) - 15} 条")

    lines.append("")
    lines.append(f"{BOLD}## Intent 分布{RESET}")
    for intent, n in sorted(report.intent_distribution.items(),
                             key=lambda x: -x[1]):
        lines.append(f"  {intent:15s} {n:3d}")

    lines.append("")
    lines.append(f"{BOLD}## Decision 分布{RESET}")
    for dec, n in sorted(report.decision_distribution.items(),
                          key=lambda x: -x[1]):
        dec_color = {"wa_referral": RED, "reply": GREEN,
                     "skip": YELLOW}.get(dec, "")
        lines.append(f"  {dec_color}{dec:15s}{RESET} {n:3d}")

    lines.append("")
    lines.append(f"{BOLD}## Gate Level 分布{RESET}")
    for lvl, n in sorted(report.gate_level_distribution.items(),
                          key=lambda x: -x[1]):
        c = {"hard_allow": GREEN, "hard_block": RED,
             "soft_pass": GREEN, "soft_fail": YELLOW}.get(lvl, "")
        lines.append(f"  {c}{lvl:15s}{RESET} {n:3d}")

    lines.append("")
    lines.append(f"{BOLD}## LLM Reply 生成率{RESET}")
    llm_rate = (report.cases_with_reply / report.total_cases
                 if report.total_cases else 0.0)
    rc = GREEN if llm_rate >= 0.8 else (YELLOW if llm_rate >= 0.3 else RED)
    lines.append(f"  has_llm_reply: {rc}{report.cases_with_reply}/{report.total_cases} ({llm_rate:.0%}){RESET}")
    lines.append(f"  has_final_reply: {report.cases_with_final_reply}/{report.total_cases}")

    if report.errors_by_case:
        lines.append("")
        lines.append(f"{BOLD}{RED}## Errors ({len(report.errors_by_case)}){RESET}")
        for cid, errs in report.errors_by_case[:15]:
            lines.append(f"  {cid}:")
            for e in errs[:3]:
                lines.append(f"    {RED}•{RESET} {e}")
        if len(report.errors_by_case) > 15:
            lines.append(f"  ... 省略 {len(report.errors_by_case) - 15} 条")

    # 典型 case 输出 sample
    lines.append("")
    lines.append(f"{BOLD}## Sample Cases (前 5 条){RESET}")
    for c in cases[:5]:
        lines.append(f"  {c.case_id}")
        lines.append(f"    intent: {c.actual_intent}({c.intent_source}) → "
                      f"gate: {c.gate_level} → decision: {c.decision}")
        if c.final_reply_preview:
            lines.append(f"    final: {c.final_reply_preview[:70]}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="B Messenger 批量 dry-run 矩阵 — 发现 silent failure")
    parser.add_argument("--device", "-d", required=True, type=str,
                        help="adb device_id (required — 即使不 tap 也用 _did 验证)")
    parser.add_argument("--peer", default="BatchDryrunTestPeer",
                        help="测试用 peer name (本工具会 seed 临时 history)")
    parser.add_argument("--sample", type=int, default=None,
                        help="只跑前 N 组合 (默认跑全部 ~336)")
    parser.add_argument("--no-llm", action="store_true",
                        help="跳过 LLM (只验 memory + intent + gate)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    if args.no_color:
        global GREEN, YELLOW, RED, BLUE, CYAN, RESET, BOLD
        GREEN = YELLOW = RED = BLUE = CYAN = RESET = BOLD = ""

    cases = run_matrix_cases(
        device_id=args.device, peer_name=args.peer,
        use_llm=not args.no_llm, sample_limit=args.sample,
    )
    report = aggregate(cases)

    if args.json:
        out = {"report": report.to_dict(),
               "cases": [c.to_dict() for c in cases]}
        print(json.dumps(out, ensure_ascii=False, indent=2,
                          default=str))
        return 0

    print(render(cases, report))
    # exit code: errors_by_case > 0 → 1
    return 1 if report.errors_by_case else 0


if __name__ == "__main__":
    sys.exit(main())
