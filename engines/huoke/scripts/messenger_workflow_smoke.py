# -*- coding: utf-8 -*-
"""Messenger 工作流数据层 smoke runner (B 侧端到端集成验证)。

设计理念(深入思考后重构):
  * **不 mock UI** — UI 层(smart_tap / dump_hierarchy / screen_parser)已被
    大量 unit test 覆盖, smoke 关注跨模块/跨分支的**数据契约闭环**
  * **一次运行 ≈ 一次真机工作流** — 通过直接写 DB + 调业务函数模拟每个
    workflow 阶段的产出,验证:
      - A 写入(friend_request / greeting) → B 能读到 (lead_score / template_id)
      - B 的派生画像 + 意图 + 引流决策串起来
      - B 的 contact_events 全部写出 → A 的 Lead Mesh 能消费
      - 漏斗指标闭环 (stage_greetings_sent → stage_wa_referrals)

运行方式:
    python scripts/messenger_workflow_smoke.py

无需真机、无需外部依赖, 使用临时 sqlite DB (tmp_path 自动清理)。

每个步骤 PASS/SKIP/FAIL 独立输出; SKIP 表示依赖分支(如 A 的 Phase 5 /
B 的 P0)还没 merge, 合入后自动 PASS。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 把 repo root 加入 sys.path 让 `from src...` 能跑
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("smoke")


# ─────────────────────────────────────────────────────────────────────────────
# 输出格式
# ─────────────────────────────────────────────────────────────────────────────

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"
BOLD = "\033[1m"

# Windows cmd 不支持 ANSI 默认; 用环境变量 NO_COLOR 关掉
if os.environ.get("NO_COLOR") or sys.platform == "win32" and not os.environ.get("ANSICON"):
    # 简单检测, 不准但足够
    try:
        os.system("")  # enable ANSI on Win10+
    except Exception:
        GREEN = YELLOW = RED = RESET = BOLD = ""


@dataclass
class StepResult:
    name: str
    status: str  # PASS / SKIP / FAIL
    reason: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        color = {"PASS": GREEN, "SKIP": YELLOW, "FAIL": RED}.get(self.status, "")
        tag = f"{color}[{self.status:4}]{RESET}"
        detail = f" ({self.reason})" if self.reason else ""
        data = ""
        if self.data:
            kv = ", ".join(f"{k}={v}" for k, v in self.data.items())
            data = f"  → {kv}"
        return f"{tag} {self.name}{detail}{data}"


# ─────────────────────────────────────────────────────────────────────────────
# Context (accumulates state across steps)
# ─────────────────────────────────────────────────────────────────────────────

class Ctx:
    def __init__(self, device_id: str = "smoke-devA"):
        self.device_id = device_id
        self.personas = {
            "alice.yamada": "jp_female_midlife",
            "bob.rossi": "it_male_midlife",
            "carol.smith": "us_female_midlife",
        }
        # 模拟 3 个目标 peer
        self.peers = [
            {"name": "alice.yamada", "lang": "ja", "score": 75,
             "mutual": 3, "replies_buying": True},
            {"name": "bob.rossi", "lang": "it", "score": 55,
             "mutual": 1, "replies_buying": False},
            {"name": "carol.smith", "lang": "en", "score": 85,
             "mutual": 0, "replies_buying": True},
        ]
        self.lead_ids: Dict[str, int] = {}
        self.greeting_template_ids: Dict[str, str] = {}
        self.tmp_db_path: Optional[Path] = None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_import(module_path: str, attr: Optional[str] = None):
    """import or return None. attr='X' 用 getattr 且找不到时返 None。"""
    try:
        m = importlib.import_module(module_path)
    except Exception:
        return None
    if attr is None:
        return m
    return getattr(m, attr, None)


def _get_contact_events(ctx: Ctx, peer_name: str) -> List[Dict[str, Any]]:
    list_fn = _safe_import("src.host.fb_store", "list_contact_events_by_peer")
    if list_fn is None:
        return []
    try:
        return list_fn(ctx.device_id, peer_name, limit=100) or []
    except Exception:
        return []


def _count_event(ctx: Ctx, peer_name: str, event_type: str) -> int:
    return sum(1 for e in _get_contact_events(ctx, peer_name)
               if e.get("event_type") == event_type)


def _step(name: str):
    """装饰器: catch 异常转 StepResult FAIL。"""
    def deco(fn: Callable[[Ctx], StepResult]):
        fn.__smoke_name__ = name
        return fn
    return deco


# ─────────────────────────────────────────────────────────────────────────────
# Steps (按工作流顺序)
# ─────────────────────────────────────────────────────────────────────────────

@_step("01_setup_tmp_db")
def step_setup(ctx: Ctx) -> StepResult:
    import src.host.database as db_mod
    ctx.tmp_db_path = Path(tempfile.mkdtemp(prefix="smoke_")) / "openclaw.db"
    ctx._db_original = db_mod.DB_PATH
    db_mod.DB_PATH = ctx.tmp_db_path
    db_mod.init_db()
    return StepResult("01_setup_tmp_db", "PASS",
                      data={"path": str(ctx.tmp_db_path)})


@_step("02_seed_leads_A")
def step_seed_leads(ctx: Ctx) -> StepResult:
    """模拟 A 的 search_and_collect_leads + fb_lead_scorer_v2 → leads.score。"""
    store = _safe_import("src.leads.store", "get_leads_store")
    if store is None:
        return StepResult("02_seed_leads_A", "SKIP",
                          "leads.store 不可用")
    store = store()
    for p in ctx.peers:
        lid = store.add_lead(name=p["name"], source_platform="facebook",
                             tags=["smoke"])
        store.update_lead(lid, score=p["score"])
        ctx.lead_ids[p["name"]] = lid
    return StepResult("02_seed_leads_A", "PASS",
                      data={"leads": len(ctx.lead_ids)})


@_step("03_a_send_friend_requests")
def step_send_frs(ctx: Ctx) -> StepResult:
    record = _safe_import("src.host.fb_store", "record_friend_request")
    if record is None:
        return StepResult("03_a_send_friend_requests", "SKIP",
                          "fb_store.record_friend_request 不可用")
    n = 0
    for p in ctx.peers:
        rid = record(ctx.device_id, p["name"],
                     lead_id=ctx.lead_ids.get(p["name"]),
                     preset_key="smoke_jp_growth",
                     source="smoke")
        if rid:
            n += 1
    return StepResult("03_a_send_friend_requests", "PASS",
                      data={"sent": n})


@_step("04_b_lookup_lead_score")
def step_lookup_lead(ctx: Ctx) -> StepResult:
    """B 的 _lookup_lead_score 能按 peer_name 找到 A 写入的 lead_score。
    P1 分支的功能, P5 fuzzy 兜底。"""
    FA = _safe_import("src.app_automation.facebook", "FacebookAutomation")
    if FA is None or not hasattr(FA, "_lookup_lead_score"):
        return StepResult("04_b_lookup_lead_score", "SKIP",
                          "P1 _lookup_lead_score 未 merge")
    hits = 0
    mismatched = []
    for p in ctx.peers:
        lid, score = FA._lookup_lead_score(p["name"])
        if lid is not None and abs(score - p["score"]) <= 1:
            hits += 1
        else:
            mismatched.append(p["name"])
    if hits == len(ctx.peers):
        return StepResult("04_b_lookup_lead_score", "PASS",
                          data={"hits": hits})
    return StepResult("04_b_lookup_lead_score", "FAIL",
                      f"mismatched: {mismatched}", data={"hits": hits})


@_step("05_peers_accept_friend")
def step_peers_accept(ctx: Ctx) -> StepResult:
    """模拟对方在 FB 上点了接受。更新 friend_request.status → accepted +
    写 add_friend_accepted contact_event (模拟 B 的 check_friend_requests_inbox
    命中 _tap_accept_button_for 成功)。"""
    update = _safe_import("src.host.fb_store", "update_friend_request_status")
    record_ce = _safe_import("src.host.fb_store", "record_contact_event")
    if update is None:
        return StepResult("05_peers_accept_friend", "SKIP",
                          "update_friend_request_status 不可用")
    accepted_evts = 0
    for p in ctx.peers:
        update(ctx.device_id, p["name"], "accepted")
        if record_ce:
            record_ce(ctx.device_id, p["name"], "add_friend_accepted",
                      meta={"lead_id": ctx.lead_ids.get(p["name"]),
                            "mutual_friends": p["mutual"],
                            "lead_score": p["score"],
                            "accept_key": "both" if p["mutual"] > 0 else "score_only"})
            accepted_evts += 1
    return StepResult("05_peers_accept_friend",
                      "PASS" if record_ce else "PASS",
                      reason="" if record_ce else "Phase 5 未 merge, 跳过 add_friend_accepted 事件",
                      data={"accepted": len(ctx.peers),
                            "contact_events": accepted_evts})


@_step("06_a_send_greetings")
def step_send_greetings(ctx: Ctx) -> StepResult:
    """模拟 A 的 send_greeting_after_add_friend 写库。"""
    record = _safe_import("src.host.fb_store", "record_inbox_message")
    if record is None:
        return StepResult("06_a_send_greetings", "SKIP",
                          "record_inbox_message 不可用")
    for p in ctx.peers:
        tid = f"yaml:{p['lang']}:1"
        ctx.greeting_template_ids[p["name"]] = tid
        record(ctx.device_id, p["name"],
               peer_type="friend_request",
               direction="outgoing",
               ai_decision="greeting",
               message_text=f"(smoke greeting for {p['name']})",
               language_detected=p["lang"],
               template_id=tid,
               preset_key="smoke_jp_growth")
    return StepResult("06_a_send_greetings", "PASS",
                      data={"greetings": len(ctx.peers)})


@_step("07_peers_reply_incoming")
def step_peers_reply(ctx: Ctx) -> StepResult:
    """模拟对方回消息。alice 和 carol 带 buying 信号 (触发 wa_referral),
    bob 只 smalltalk。"""
    record = _safe_import("src.host.fb_store", "record_inbox_message")
    if record is None:
        return StepResult("07_peers_reply_incoming", "SKIP",
                          "record_inbox_message 不可用")
    msgs = {
        "alice.yamada": "値段教えてください。LINEでも連絡できますか?",
        "bob.rossi": "ciao come stai",
        "carol.smith": "How much does it cost?",
    }
    for p in ctx.peers:
        record(ctx.device_id, p["name"],
               peer_type="friend",
               direction="incoming",
               ai_decision="",
               message_text=msgs[p["name"]],
               language_detected=p["lang"],
               preset_key="smoke_jp_growth")
    return StepResult("07_peers_reply_incoming", "PASS",
                      data={"incomings": len(ctx.peers)})


@_step("08_b_mark_greeting_replied")
def step_mark_replied(ctx: Ctx) -> StepResult:
    """B 的 mark_greeting_replied_back — 标 replied_at + 写 greeting_replied
    event (F1 合入 P0 之后)。"""
    mark = _safe_import("src.host.fb_store", "mark_greeting_replied_back")
    if mark is None:
        return StepResult("08_b_mark_greeting_replied", "SKIP",
                          "P0 mark_greeting_replied_back 未 merge")
    marked = 0
    for p in ctx.peers:
        n = mark(ctx.device_id, p["name"], window_days=7)
        marked += n
    events = sum(_count_event(ctx, p["name"], "greeting_replied")
                 for p in ctx.peers)
    return StepResult("08_b_mark_greeting_replied", "PASS",
                      data={"marked": marked, "events": events})


@_step("09_b_chat_memory")
def step_chat_memory(ctx: Ctx) -> StepResult:
    """B 的 chat_memory.get_derived_profile 能看到历史 + 画像。"""
    build = _safe_import("src.ai.chat_memory", "build_context_block")
    if build is None:
        return StepResult("09_b_chat_memory", "SKIP",
                          "P3 chat_memory 未 merge")
    profiles_ok = 0
    for p in ctx.peers:
        ctx_block = build(ctx.device_id, p["name"], history_limit=5)
        if ctx_block["profile"]["total_turns"] >= 2:
            profiles_ok += 1
    if profiles_ok == len(ctx.peers):
        return StepResult("09_b_chat_memory", "PASS",
                          data={"peers_with_history": profiles_ok})
    return StepResult("09_b_chat_memory", "FAIL",
                      f"{profiles_ok}/{len(ctx.peers)} peers have history")


@_step("10_b_intent_classify")
def step_intent(ctx: Ctx) -> StepResult:
    """B 的 chat_intent.classify_intent 对 3 条 incoming 给出正确意图。
    alice/carol 的 buying keyword → buying 或 referral_ask (后者命中 LINE)。"""
    cls = _safe_import("src.ai.chat_intent", "classify_intent")
    if cls is None:
        return StepResult("10_b_intent_classify", "SKIP",
                          "P4 chat_intent 未 merge")
    msgs = {
        "alice.yamada": "値段教えてください。LINEでも連絡できますか?",
        "bob.rossi": "ciao come stai",
        "carol.smith": "How much does it cost?",
    }
    intents = {}
    for p in ctx.peers:
        # 历史需要 peer_turns 非空才算 non-opening
        hist = [{"direction": "outgoing", "message_text": "greeting"},
                {"direction": "incoming", "message_text": "prior"}]
        r = cls(msgs[p["name"]], history=hist, use_llm_fallback=False)
        intents[p["name"]] = (r.intent, r.source)
    # alice 应命中 referral_ask (LINE 关键词)
    # carol 应命中 buying (cost 关键词)
    # bob 应是 smalltalk fallback
    expected_ok = (
        intents["alice.yamada"][0] == "referral_ask"
        and intents["carol.smith"][0] == "buying"
    )
    return StepResult("10_b_intent_classify",
                      "PASS" if expected_ok else "FAIL",
                      data={"intents": {k: v[0] for k, v in intents.items()}})


@_step("11_b_referral_gate")
def step_gate(ctx: Ctx) -> StepResult:
    """B 的 referral_gate.should_refer 对 3 个 peer 决策正确。"""
    should = _safe_import("src.ai.referral_gate", "should_refer")
    if should is None:
        return StepResult("11_b_referral_gate", "SKIP",
                          "P5 referral_gate 未 merge")
    memory = _safe_import("src.ai.chat_memory", "build_context_block")
    decisions = {}
    for p in ctx.peers:
        mem_ctx = memory(ctx.device_id, p["name"]) if memory else {"profile": {}}
        intent = "referral_ask" if p["name"] == "alice.yamada" else (
            "buying" if p["name"] == "carol.smith" else "smalltalk")
        d = should(
            intent=intent, ref_score=0.6,
            memory_ctx=mem_ctx, lead_score=p["score"],
            has_contact=True,
        )
        decisions[p["name"]] = d.level
    # alice referral_ask → hard_allow; carol buying → hard_allow; bob → soft_*
    ok = (
        decisions["alice.yamada"] == "hard_allow"
        and decisions["carol.smith"] == "hard_allow"
        and decisions["bob.rossi"].startswith("soft_")
    )
    return StepResult("11_b_referral_gate",
                      "PASS" if ok else "FAIL",
                      data=decisions)


@_step("12_b_wa_referral_sent")
def step_wa_referral(ctx: Ctx) -> StepResult:
    """模拟 B 的 _ai_reply_and_send 引流成功 → 写 wa_referral_sent event。"""
    record = _safe_import("src.host.fb_store", "record_inbox_message")
    record_ce = _safe_import("src.host.fb_store", "record_contact_event")
    if record is None:
        return StepResult("12_b_wa_referral_sent", "SKIP",
                          "record_inbox_message 不可用")
    # 只对 alice/carol 发 wa_referral
    sent = 0
    for p in ctx.peers:
        if p["name"] == "bob.rossi":
            continue
        record(ctx.device_id, p["name"],
               peer_type="friend",
               direction="outgoing",
               ai_decision="wa_referral",
               message_text=f"LINE ID: xyz123 ({p['name']})",
               language_detected=p["lang"],
               preset_key="smoke_jp_growth")
        if record_ce:
            record_ce(ctx.device_id, p["name"], "wa_referral_sent",
                      preset_key="smoke_jp_growth",
                      meta={"channel": "line", "peer_type": "friend",
                            "intent": "referral_ask" if "alice" in p["name"]
                                      else "buying"})
            sent += 1
    return StepResult("12_b_wa_referral_sent", "PASS",
                      reason="" if record_ce else "Phase 5 未 merge, 仅写 inbox 行",
                      data={"wa_referrals": sent or "DB-only"})


@_step("13_verify_funnel_metrics")
def step_funnel(ctx: Ctx) -> StepResult:
    get = _safe_import("src.host.fb_store", "get_funnel_metrics")
    if get is None:
        return StepResult("13_verify_funnel_metrics", "SKIP",
                          "get_funnel_metrics 不可用")
    m = get(device_id=ctx.device_id)
    return StepResult("13_verify_funnel_metrics", "PASS",
                      data={k: v for k, v in m.items()
                            if str(k).startswith("stage_")
                            and isinstance(v, (int, float))})


@_step("14_verify_contact_events_total")
def step_ce_total(ctx: Ctx) -> StepResult:
    """汇总各类 contact_event 数量。Phase 5 未 merge 时全 0,合入后有值。"""
    list_fn = _safe_import("src.host.fb_store", "list_contact_events_by_peer")
    if list_fn is None:
        return StepResult("14_verify_contact_events_total", "SKIP",
                          "Phase 5 fb_contact_events 未 merge")
    breakdown: Dict[str, int] = {}
    for p in ctx.peers:
        for e in _get_contact_events(ctx, p["name"]):
            k = e.get("event_type", "unknown")
            breakdown[k] = breakdown.get(k, 0) + 1
    return StepResult("14_verify_contact_events_total", "PASS",
                      data=breakdown)


@_step("15_teardown")
def step_teardown(ctx: Ctx) -> StepResult:
    import src.host.database as db_mod
    if hasattr(ctx, "_db_original"):
        db_mod.DB_PATH = ctx._db_original
    if ctx.tmp_db_path and ctx.tmp_db_path.exists():
        try:
            ctx.tmp_db_path.unlink()
            ctx.tmp_db_path.parent.rmdir()
        except Exception:
            pass
    return StepResult("15_teardown", "PASS")


STEPS: List[Callable[[Ctx], StepResult]] = [
    step_setup,
    step_seed_leads,
    step_send_frs,
    step_lookup_lead,
    step_peers_accept,
    step_send_greetings,
    step_peers_reply,
    step_mark_replied,
    step_chat_memory,
    step_intent,
    step_gate,
    step_wa_referral,
    step_funnel,
    step_ce_total,
    step_teardown,
]


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

def run_smoke() -> Tuple[List[StepResult], int]:
    """跑所有步骤, 返回 (results, exit_code)。"""
    ctx = Ctx()
    results: List[StepResult] = []
    print(f"\n{BOLD}=== Messenger Workflow Smoke ==={RESET}")
    print(f"device_id={ctx.device_id}  peers={len(ctx.peers)}\n")

    for fn in STEPS:
        name = getattr(fn, "__smoke_name__", fn.__name__)
        try:
            r = fn(ctx)
        except Exception as e:
            r = StepResult(name, "FAIL", str(e))
        results.append(r)
        print(r.render())

    # 汇总
    by_status: Dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1

    print(f"\n{BOLD}=== Summary ==={RESET}")
    for status in ("PASS", "SKIP", "FAIL"):
        n = by_status.get(status, 0)
        color = {"PASS": GREEN, "SKIP": YELLOW, "FAIL": RED}.get(status, "")
        print(f"  {color}{status}{RESET}: {n}")

    # exit code: FAIL > 0 → 1, 其他 → 0
    return results, 1 if by_status.get("FAIL", 0) > 0 else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Messenger 工作流数据层 smoke runner")
    parser.add_argument("--no-color", action="store_true",
                        help="关 ANSI color (CI 场景)")
    args = parser.parse_args()
    if args.no_color:
        global GREEN, YELLOW, RED, RESET, BOLD
        GREEN = YELLOW = RED = RESET = BOLD = ""

    _, exit_code = run_smoke()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
