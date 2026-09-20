# -*- coding: utf-8 -*-
"""出站事实声明评测（P15，纯函数常驻门禁）——金标语料 + 双向回归网。

检查器本体在**生产模块** ``src/companion/goals/claim_guard.py``（与
``offer_guard`` / ``offer_guard_eval`` 同一关系：生产拥有实现，评测拥有语料与
判据）。本模块只负责：金标语料、召回/误报统计、报告渲染、探测器自证。

与 ``offer_guard_eval`` 分工（刻意不重叠）：
- ``offer_guard_eval`` 测**承诺措辞**轴（折扣/券码/赠送/客户数该剥则剥）；
- 本模块测**事实声明与渠道卫生**轴（报价/试用时长对不对得上目录、gated 线泄漏、
  回复语种漂移、内部指令泄漏）。

金标正例全部是 2026-07-28 本地×云端 AI 对练（``scripts/duel_runner.py``
六场 51 轮）的**真实事故句**；反例是易误伤面（人设自家生意报价、正确报价、
客户真的切了语言、普通方括号表情…）。

CLI：``python -m scripts.run_eval --outbound-claims [--json]``；
门禁：``tests/test_outbound_claim_eval.py``（含探测器有效性自证）。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

# 单一事实源：检查器由生产模块提供（评测不另造一套实现，防两边漂移）
from src.companion.goals.claim_guard import (  # noqa: F401 — 转出给门禁/裁判复用
    CHECKS,
    catalog_prices,
    check_outbound_claims,
    dominant_language,
    find_directive_leak,
    find_gated_leak,
    find_language_drift,
    find_price_mismatch,
    find_trial_mismatch,
    sanitize_outbound_claims,
    script_profile,
)

# 与出厂 site_catalog 一致：chatx 58/198/598 + lingox 99/198/59；授权试用 7 天
_PRICES = [58.0, 198.0, 598.0, 99.0, 59.0]
_DAYS = [7.0]

GOLD: Sequence[Dict[str, Any]] = (
    # ── 必抓：实录事故 ────────────────────────────────────────────────────
    {"id": "p_198_to_168", "expect": "catalog_price",
     "text": "我那套智聊AI团队版198美金一个月，算下来一个月168",
     "note": "实录：嘴上拒绝打折，数字上偷偷打了 8.5 折"},
    {"id": "p_invented_plan_price", "expect": "catalog_price",
     "text": "入门版现在是 $38/月，很划算"},
    {"id": "t_14_days_escaped", "expect": "trial_duration",
     "text": "官网有14天客户端试用，装上就能跑一遍你的业务流",
     "note": "实录：offer_guard 因『免费』不贴数字漏掉，本轴按事实比对"},
    {"id": "t_one_month_trial", "expect": "trial_duration",
     "text": "可以先体验一个月看看效果"},
    {"id": "g_faceswap", "expect": "gated_line",
     "text": "还有免费图片换脸和实时演示，几分钟就能看到效果",
     "note": "实录：向 ChatX 客户推 gated 的 FaceX 线"},
    {"id": "d_photo_directive", "expect": "directive_leak",
     "text": "รวมค่าสินค้า 2,250 เปโซ\n[PHOTO object shopping bag with dried mango]",
     "note": "实录：出图指令没被消费，当字面文本发出"},
    {"id": "l_thai_avalanche", "expect": "reply_language",
     "text": "ฮ่าๆ โดนจับได้ซะแล้ว สลับภาษาไปมาเองยังไม่ทันรู้ตัวเลย มะม่วงอบแห้ง ถุงใหญ่",
     "customer_msgs": ["你突然切换泰语啦，吓我一跳～",
                       "不过说真的，你们那芒果干多少钱一包？",
                       "所以寄到清迈要多久啊，我朋友等急了"],
     "note": "实录：客户主体中文，回复整段泰语（连续 7 轮）"},

    # ── 必放：易误伤面（误报即毁掉正常话术）──────────────────────────────
    {"id": "k_correct_price", "expect": "clean",
     "text": "智聊团队版 $198/月，入门版 $58/月，你先看看哪档合适"},
    {"id": "k_vague_price", "expect": "clean",
     "text": "入门版一个月几十块，团队版按坐席算，价格官网都写着"},
    {"id": "k_persona_own_price", "expect": "clean",
     "text": "我店里芒果干大包450比索，小包250，都是自己晒的",
     "note": "人设自家生意报价：无产品/套餐词 → 不校验，绝不误伤"},
    {"id": "k_team_size_number", "expect": "clean",
     "text": "你们三个客服确实扛不住，智聊能顶掉大半重复问题",
     "note": "句中有产品词但数字不是价格（无货币/周期单位）"},
    {"id": "k_correct_trial", "expect": "clean",
     "text": "注册就能免费试用7天，2.5万字符够你跑一轮"},
    {"id": "k_trial_week", "expect": "clean",
     "text": "有一周的免费体验额度，先跑跑看",
     "note": "一周 ≡ 7 天（按天数比对不按措辞）"},
    {"id": "k_no_trial_number", "expect": "clean",
     "text": "官网可以先试用，觉得合适再付费"},
    {"id": "k_voice_clone_ok", "expect": "clean",
     "text": "我们的语音克隆是给自己人设配音用的，不对外卖",
     "note": "本栈自用语音能力 ≠ gated 换脸线，刻意不抓"},
    {"id": "k_zh_with_brand", "expect": "clean",
     "text": "这个功能在 ChatX 的 dashboard 里就能开，很简单的啦",
     "customer_msgs": ["这个功能怎么开呀", "我不太会弄这些"],
     "note": "中文夹英文品牌词 → 主体仍是中文，不算漂移"},
    {"id": "k_customer_switched_en", "expect": "clean",
     "text": "Sure, we ship to Thailand — usually 7 to 10 days to Chiang Mai.",
     "customer_msgs": ["do you ship to Thailand?",
                       "how long does it take to Chiang Mai?",
                       "and what about the price"],
     "note": "客户真的整段换英文 → 跟着英文回是对的"},
    {"id": "k_short_reply", "expect": "clean",
     "text": "好呀～",
     "customer_msgs": ["在吗"],
     "note": "过短回复不判语种（噪声大）"},
    {"id": "k_brackets_benign", "expect": "clean",
     "text": "我记在小本本上了（真的不是敷衍）[认真脸]",
     "note": "普通方括号表情 ≠ 内部指令语法"},
    {"id": "k_persona_price_with_plan_token", "expect": "clean",
     "text": "我店里手冲一杯38块，智聊帮我省了小半个客服的人力",
     "note": "同条消息里既有人设自家报价又有产品词——自家报价小句无周期语境，"
             "不得被当成我方套餐报价（最险的误伤面）"},
    {"id": "k_order_count_not_price", "expect": "clean",
     "text": "智聊上线后我一个月能多接200单，人力没加",
     "note": "周期语境 + 数字，但『单』不是货币单位 → 不算报价"},
    {"id": "k_en_reply_to_en_customer", "expect": "clean",
     "text": "The team plan is $198 per month and the starter is $58.",
     "customer_msgs": ["how much is it?", "any cheaper plan?"],
     "note": "英文客户 + 英文报价且价格正确 → 语种轴与价格轴都应放行"},
)


def evaluate_outbound_claims(
    samples: Optional[Sequence[Dict[str, Any]]] = None,
    *,
    check_fn=None,
    recall_target: Optional[float] = None,
    max_false_alarms: Optional[int] = None,
) -> Dict[str, Any]:
    """跑金标，返回 ``{passed, recall, false_alarms, failures, by_check}``。

    ``check_fn`` 可注入（门禁用它自证探测器有效：瞎检测器必 FAIL）。
    """
    corpus = list(samples if samples is not None else GOLD)
    fn = check_fn or check_outbound_claims
    recall_t = float(recall_target if recall_target is not None
                     else os.environ.get("AITR_OUTBOUND_CLAIM_RECALL", 1.0))
    max_fp = int(max_false_alarms if max_false_alarms is not None
                 else os.environ.get("AITR_OUTBOUND_CLAIM_MAX_FP", 0))

    hit_total = hit_ok = 0
    false_alarms = 0
    failures: List[Dict[str, Any]] = []
    by_check: Dict[str, Dict[str, int]] = {c: {"expected": 0, "caught": 0}
                                           for c in CHECKS}
    for case in corpus:
        cid = str(case.get("id") or "?")
        expect = str(case.get("expect") or "clean")
        try:
            found = fn(
                str(case.get("text") or ""),
                allowed_prices=case.get("allowed_prices", _PRICES),
                allowed_free_days=case.get("allowed_free_days", _DAYS),
                customer_msgs=case.get("customer_msgs") or (),
            )
        except Exception as e:  # noqa: BLE001 — 检查器抛异常＝该例失败
            failures.append({"id": cid, "expect": expect,
                             "error": f"{type(e).__name__}: {e}"})
            if expect != "clean":
                hit_total += 1
                by_check.setdefault(expect, {"expected": 0, "caught": 0})
                by_check[expect]["expected"] += 1
            continue
        kinds = {k for k, _ in found}
        if expect == "clean":
            if found:
                false_alarms += 1
                failures.append({"id": cid, "expect": expect,
                                 "found": sorted(kinds)})
        else:
            hit_total += 1
            by_check.setdefault(expect, {"expected": 0, "caught": 0})
            by_check[expect]["expected"] += 1
            if expect in kinds:
                hit_ok += 1
                by_check[expect]["caught"] += 1
            else:
                failures.append({"id": cid, "expect": expect,
                                 "found": sorted(kinds)})

    recall = (hit_ok / hit_total) if hit_total else 1.0
    passed = bool(recall >= recall_t and false_alarms <= max_fp)
    return {
        "passed": passed,
        "total": len(corpus),
        "violation_cases": hit_total,
        "clean_cases": len(corpus) - hit_total,
        "recall": round(recall, 4),
        "recall_target": recall_t,
        "false_alarms": false_alarms,
        "max_false_alarms": max_fp,
        "by_check": by_check,
        "failures": failures,
    }


def format_outbound_claim_report(report: Dict[str, Any]) -> str:
    lines = [
        "=== 出站事实声明校验（P15 常驻门禁）===",
        f"样本：{report.get('total')}（必抓 {report.get('violation_cases')}"
        f" / 必放 {report.get('clean_cases')}）",
        f"召回：{report.get('recall'):.0%}（阈 {report.get('recall_target'):.0%}）"
        f" · 误报：{report.get('false_alarms')}"
        f"（上限 {report.get('max_false_alarms')}）",
    ]
    for name, st in (report.get("by_check") or {}).items():
        if st.get("expected"):
            lines.append(f"  · {name}: {st.get('caught')}/{st.get('expected')}")
    lines.append(f"结论：{'PASS' if report.get('passed') else 'FAIL'}")
    for f in (report.get("failures") or [])[:10]:
        lines.append(f"  ✗ {f.get('id')} expect={f.get('expect')}"
                     f" found={f.get('found', f.get('error', ''))}")
    return "\n".join(lines)


__all__ = [
    "CHECKS", "GOLD", "catalog_prices", "check_outbound_claims",
    "dominant_language", "evaluate_outbound_claims",
    "find_directive_leak", "find_gated_leak", "find_language_drift",
    "find_price_mismatch", "find_trial_mismatch",
    "format_outbound_claim_report", "sanitize_outbound_claims",
    "script_profile",
]
