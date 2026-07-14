# -*- coding: utf-8 -*-
"""P11b 半监督真机 dry-run (B 侧 LLM-in-the-loop 验证)。

和 live_smoke / workflow_smoke 互补:
  * workflow_smoke = 数据层集成测试 (不碰设备 / 不调 LLM)
  * live_smoke     = 真机代码跑通性验证 (只读 UI, 不调 LLM)
  * **dryrun**     = 真实 LLM + gate + memory 链路验证, 但**不实际 send**
                      到 Messenger (避免误发给真人)

典型用法 (半监督):
  1. 用户在真机 Messenger 里让另一账号发一条测试消息给目标 peer
  2. 跑 live_smoke 的 inbox step 让 B 读到 incoming 写进 DB
  3. 跑本工具 dryrun — 完整跑 memory + intent + LLM + gate, 输出 B 会
     生成什么 reply + 决策信息, 但**不实际发消息**

这让真人一眼看到:
  * LLM 生成的 reply 质量
  * intent 是否判对
  * gate 是否按预期触发 wa_referral
  * L3 facts 是否融入 prompt

不主动改 Messenger UI (不进对话、不 type、不 tap send), **零骚扰真人**。

用法:
    # 1. 从 DB 最近一条 incoming dry-run
    python scripts/messenger_production_dryrun.py \\
        --device <did> --peer Alice --from-inbox

    # 2. 显式指定 incoming 文本
    python scripts/messenger_production_dryrun.py \\
        --device <did> --peer Alice --incoming "LINEでも連絡できますか?"

    # 3. 跳过 LLM (只跑 memory + intent + gate)
    python scripts/messenger_production_dryrun.py \\
        --device <did> --peer Alice --from-inbox --no-llm

    # 4. 带引流渠道测试
    python scripts/messenger_production_dryrun.py \\
        --device <did> --peer Alice --from-inbox \\
        --referral-contact "line:abc123" \\
        --persona jp_female_midlife
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dryrun")


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
# Result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DryRunResult:
    device_id: str = ""
    peer_name: str = ""
    incoming_text: str = ""
    # memory
    history_turns: int = 0
    profile_summary: Dict[str, Any] = field(default_factory=dict)
    facts: Dict[str, Any] = field(default_factory=dict)
    hint_text_len: int = 0
    should_block_referral: bool = False
    # intent
    intent: str = ""
    intent_source: str = ""
    intent_confidence: float = 0.0
    intent_reason: str = ""
    # llm
    llm_called: bool = False
    llm_reply_text: str = ""
    llm_referral_score: float = 0.0
    # gate
    gate_level: str = ""
    gate_score: int = 0
    gate_threshold: int = 0
    gate_reasons: List[str] = field(default_factory=list)
    # final
    decision: str = ""
    final_reply: str = ""
    referral_channel: str = ""
    # 错误
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# DB 查询辅助
# ─────────────────────────────────────────────────────────────────────────────

def fetch_latest_incoming(device_id: str, peer_name: str) -> Optional[str]:
    """从 facebook_inbox_messages 取该 peer 最近一条 incoming 消息。"""
    try:
        from src.host.database import _connect
    except Exception as e:
        log.error("DB import 失败: %s", e)
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT message_text FROM facebook_inbox_messages"
                " WHERE device_id=? AND peer_name=? AND direction='incoming'"
                " AND COALESCE(message_text,'')<>''"
                " ORDER BY id DESC LIMIT 1",
                (device_id, peer_name),
            ).fetchone()
        return row[0] if row else None
    except Exception as e:
        log.error("查 incoming 失败: %s", e)
        return None


def fetch_lead_score(peer_name: str) -> int:
    """查 leads.store 拿 lead_score (P1 F5 fuzzy 机制)。"""
    try:
        from src.app_automation.facebook import FacebookAutomation
        _, score = FacebookAutomation._lookup_lead_score(peer_name)
        return int(score or 0)
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# 流程
# ─────────────────────────────────────────────────────────────────────────────

def run_dryrun(device_id: str, peer_name: str,
               incoming_text: str = "",
               from_inbox: bool = False,
               referral_contact: str = "",
               persona_key: str = "",
               use_llm: bool = True,
               peer_type: str = "friend") -> DryRunResult:
    """完整 dry-run: memory → intent → LLM → gate → 决策。不实际 send。"""
    r = DryRunResult(device_id=device_id, peer_name=peer_name)

    # ── 0. 取 incoming ─────────────────────────────────────────────
    if not incoming_text and from_inbox:
        fetched = fetch_latest_incoming(device_id, peer_name)
        if not fetched:
            r.errors.append("DB 里该 peer 无 incoming 消息")
            return r
        incoming_text = fetched
    if not incoming_text:
        r.errors.append("未提供 incoming_text 且未 --from-inbox")
        return r
    r.incoming_text = incoming_text

    # ── 1. Memory (P3 + P10) ────────────────────────────────────────
    try:
        from src.ai.chat_memory import build_context_block
        memory_ctx = build_context_block(device_id, peer_name,
                                          history_limit=5)
        r.history_turns = len(memory_ctx.get("history", []))
        profile = memory_ctx.get("profile", {})
        r.profile_summary = {
            "total_turns": profile.get("total_turns", 0),
            "peer_reply_count": profile.get("peer_reply_count", 0),
            "bot_reply_count": profile.get("bot_reply_count", 0),
            "greeting_count": profile.get("greeting_count", 0),
            "language_pref": profile.get("language_pref", ""),
            "referral_attempts": profile.get("referral_attempts", 0),
            "referral_got_reply": profile.get("referral_got_reply", False),
        }
        r.facts = memory_ctx.get("facts", {}) or {}
        r.hint_text_len = len(memory_ctx.get("hint_text", ""))
        r.should_block_referral = bool(memory_ctx.get("should_block_referral",
                                                        False))
    except Exception as e:
        r.errors.append(f"memory 失败: {str(e)[:80]}")
        memory_ctx = {"hint_text": "", "history": [], "profile": {},
                       "facts": {}, "should_block_referral": False}

    # ── 2. Intent (P4) ─────────────────────────────────────────────
    try:
        from src.ai.chat_intent import classify_intent
        intent_result = classify_intent(
            incoming_text,
            history=memory_ctx.get("history", []),
            lang_hint=persona_key.split("_")[0] if persona_key else "",
            use_llm_fallback=use_llm,
        )
        r.intent = intent_result.intent
        r.intent_source = intent_result.source
        r.intent_confidence = intent_result.confidence
        r.intent_reason = intent_result.reason
    except Exception as e:
        r.errors.append(f"intent 失败: {str(e)[:80]}")
        r.intent = "smalltalk"

    # ── 3. LLM (ChatBrain) ─────────────────────────────────────────
    ref_score = 0.0
    llm_reply = ""
    if use_llm:
        try:
            from src.ai.chat_brain import ChatBrain, UserProfile
            brain = ChatBrain.get_instance()
            profile_obj = UserProfile(username=peer_name,
                                       bio="", source="dryrun")
            style_hint_parts: List[str] = []
            if memory_ctx.get("hint_text"):
                style_hint_parts.append(memory_ctx["hint_text"])
            # 加 intent hint
            try:
                from src.ai.chat_intent import format_intent_for_llm_hint
                ih = format_intent_for_llm_hint(intent_result)
                if ih:
                    style_hint_parts.append(ih)
            except Exception:
                pass
            ab_style_hint = "\n\n".join(style_hint_parts).strip()
            target_lang = ""
            try:
                from src.host.fb_target_personas import get_persona_display
                disp = get_persona_display(persona_key) if persona_key else {}
                target_lang = str(disp.get("language") or "").strip()
            except Exception:
                pass
            result = brain.generate_reply(
                lead_id=peer_name,
                incoming_message=incoming_text,
                profile=profile_obj,
                platform="facebook",
                target_language=target_lang,
                contact_info=referral_contact,
                source="dryrun",
                ab_style_hint=ab_style_hint,
            )
            r.llm_called = True
            if result and getattr(result, "message", None):
                llm_reply = result.message
                r.llm_reply_text = llm_reply
                ref_score = float(getattr(result, "referral_score", 0.0) or 0.0)
                r.llm_referral_score = ref_score
        except Exception as e:
            r.errors.append(f"LLM 失败: {str(e)[:80]}")
    else:
        llm_reply = "[DRY no-llm] 占位 reply"

    # ── 4. Gate (P5) ───────────────────────────────────────────────
    has_contact = bool(referral_contact)
    try:
        from src.ai.referral_gate import should_refer
        gate_cfg = None
        if peer_type == "stranger":
            gate_cfg = {"min_turns": 5, "min_peer_replies": 3,
                        "score_threshold": 4, "refer_cooldown_hours": 6}
        gate = should_refer(
            intent=r.intent, ref_score=ref_score,
            memory_ctx=memory_ctx,
            lead_score=fetch_lead_score(peer_name),
            has_contact=has_contact, config=gate_cfg,
        )
        r.gate_level = gate.level
        r.gate_score = gate.score
        r.gate_threshold = gate.threshold
        r.gate_reasons = list(gate.reasons)
        r.decision = "wa_referral" if gate.refer else ("reply" if llm_reply else "skip")
    except Exception as e:
        r.errors.append(f"gate 失败: {str(e)[:80]}")
        r.decision = "reply" if llm_reply else "skip"

    # ── 5. Referral snippet 覆写 ───────────────────────────────────
    final_reply = llm_reply
    if r.decision == "wa_referral" and referral_contact:
        try:
            from src.host.fb_referral_contact import (
                pick_referral_for_persona, parse_referral_channels,
            )
            channel_map = parse_referral_channels(referral_contact)
            val, channel = pick_referral_for_persona(channel_map, persona_key)
            r.referral_channel = channel
            if val:
                try:
                    from src.app_automation.fb_content_assets import (
                        get_referral_snippet,
                    )
                    snippet = get_referral_snippet(channel, val,
                                                    persona_key=persona_key)
                    if snippet:
                        final_reply = snippet
                except Exception:
                    pass
        except Exception as e:
            r.errors.append(f"referral_snippet 失败: {str(e)[:80]}")

    r.final_reply = final_reply
    return r


# ─────────────────────────────────────────────────────────────────────────────
# 渲染
# ─────────────────────────────────────────────────────────────────────────────

def _section(title: str) -> str:
    return f"\n{BOLD}{CYAN}━━━ {title} ━━━{RESET}"


def render(r: DryRunResult) -> str:
    lines: List[str] = []
    lines.append(f"\n{BOLD}=== Messenger Dry-Run ==={RESET}")
    lines.append(f"device={r.device_id}  peer={r.peer_name}\n")

    lines.append(_section("输入"))
    lines.append(f"  incoming: {BLUE}{r.incoming_text[:200]}{RESET}")

    lines.append(_section("Memory (P3 + P10)"))
    lines.append(f"  history_turns: {r.history_turns}")
    for k, v in r.profile_summary.items():
        lines.append(f"  profile.{k}: {v}")
    lines.append(f"  facts: {r.facts if r.facts else '(无, Phase 5 未 merge 或无 extracted)'}")
    lines.append(f"  hint_text_len: {r.hint_text_len}")
    if r.should_block_referral:
        lines.append(f"  {YELLOW}should_block_referral: True{RESET} (上次引流对方未回)")

    lines.append(_section("Intent (P4)"))
    intent_color = {"buying": RED, "referral_ask": RED,
                    "interest": YELLOW, "cold": YELLOW}.get(r.intent, GREEN)
    lines.append(f"  intent: {intent_color}{r.intent}{RESET} "
                  f"(confidence={r.intent_confidence:.2f}, source={r.intent_source})")
    if r.intent_reason:
        lines.append(f"  reason: {r.intent_reason}")

    lines.append(_section("LLM Reply Generated"))
    if r.llm_called:
        lines.append(f"  referral_score: {r.llm_referral_score:.2f}")
        lines.append(f"  reply text:")
        for line in (r.llm_reply_text or "(空)").splitlines():
            lines.append(f"    {BLUE}│{RESET} {line}")
    else:
        lines.append(f"  {YELLOW}(跳过 LLM --no-llm){RESET}")

    lines.append(_section("Gate (P5)"))
    level_color = {"hard_allow": GREEN, "hard_block": RED,
                   "soft_pass": GREEN, "soft_fail": YELLOW}.get(r.gate_level, "")
    lines.append(f"  level: {level_color}{r.gate_level}{RESET}")
    lines.append(f"  score: {r.gate_score}/{r.gate_threshold}")
    for reason in r.gate_reasons[:6]:
        lines.append(f"  • {reason}")

    lines.append(_section("Final Decision"))
    decision_color = {"wa_referral": RED, "reply": GREEN,
                      "skip": YELLOW}.get(r.decision, "")
    lines.append(f"  decision: {decision_color}{r.decision}{RESET}")
    if r.referral_channel:
        lines.append(f"  referral_channel: {r.referral_channel}")
    lines.append(f"  {BOLD}final reply (would be sent if not dry):{RESET}")
    for line in (r.final_reply or "(无)").splitlines():
        lines.append(f"    {GREEN}│{RESET} {line}")

    if r.errors:
        lines.append(_section(f"{RED}Errors ({len(r.errors)}){RESET}"))
        for e in r.errors:
            lines.append(f"  {RED}•{RESET} {e}")

    lines.append(f"\n{BOLD}{YELLOW}[DRY-RUN] 上述 reply 未实际发送到 Messenger{RESET}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Messenger 半监督 LLM-in-the-loop dry-run (不实际 send)")
    parser.add_argument("--device", "-d", required=True, type=str,
                        help="adb device_id")
    parser.add_argument("--peer", "-p", required=True, type=str,
                        help="peer_name (FB 显示名, 和 DB peer_name 列精确匹配)")
    parser.add_argument("--incoming", "-i", type=str, default="",
                        help="显式指定 incoming 文本; 不给则用 --from-inbox")
    parser.add_argument("--from-inbox", action="store_true",
                        help="从 DB 取最近一条 incoming")
    parser.add_argument("--referral-contact", type=str, default="",
                        help="引流 contact (如 'line:abc123')")
    parser.add_argument("--persona", "--persona-key", type=str, default="",
                        help="persona_key (如 jp_female_midlife)")
    parser.add_argument("--peer-type", choices=("friend", "stranger"),
                        default="friend",
                        help="friend=主 inbox gate; stranger=MR 保守 gate")
    parser.add_argument("--no-llm", action="store_true",
                        help="跳过 LLM, 用占位 reply (只验 memory/intent/gate)")
    parser.add_argument("--json", action="store_true",
                        help="输出 JSON 供程序消费")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    if args.no_color:
        global GREEN, YELLOW, RED, BLUE, CYAN, RESET, BOLD
        GREEN = YELLOW = RED = BLUE = CYAN = RESET = BOLD = ""

    if not args.incoming and not args.from_inbox:
        parser.error("--incoming 或 --from-inbox 至少给一个")

    result = run_dryrun(
        device_id=args.device,
        peer_name=args.peer,
        incoming_text=args.incoming,
        from_inbox=args.from_inbox,
        referral_contact=args.referral_contact,
        persona_key=args.persona,
        use_llm=not args.no_llm,
        peer_type=args.peer_type,
    )

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render(result))

    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
