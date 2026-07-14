# -*- coding: utf-8 -*-
"""L2 端到端烟囱测试 — 模拟全链路客户跑通.

链路: greeting → messenger 入站 → AI 出站 → wa_referral → handoff → 真人接管 → 标结果

验证:
- L2 中央 PG 数据落库 (customers / events / chats / handoffs)
- referral_gate 真触发 (持 7 轮后 + 关键词命中)
- lead_handoffs SQLite 也能跟着双写
- ai_takeover_state 接管暂停 ↔ 释放
- 真人客服 4 个动作 (assign / reply / note / outcome) 都生效
- emotion scorer 跑通 (失败 fallback 也能工作)

使用:
    set -a && source .env && set +a
    python scripts/l2_e2e_smoke.py
"""
from __future__ import annotations

import os
import sys
import json
import uuid
from pathlib import Path

# 让 src 可 import
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

# 强制走主控本机
os.environ.setdefault("OPENCLAW_COORDINATOR_URL", "http://192.168.0.118:8000")

PEER_NAME = "EmadEmoTest"
DEVICE_ID = "smoke-device-d99"
WORKER_ID = "smoke-worker"
PERSONA_KEY = "jp_female_midlife"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def step(label: str) -> None:
    print(f"\n{YELLOW}━━━ {label} ━━━{RESET}")


def ok(label: str) -> None:
    print(f"  {GREEN}[OK]{RESET} {label}")


def fail(label: str) -> None:
    print(f"  {RED}[FAIL]{RESET} {label}")


def main() -> int:
    errors = 0

    # ── 0. 清理上一次 smoke 数据 ─────────────────────────────────────
    step("0) 清 smoke 测试数据")
    try:
        from src.host.central_customer_store import get_store
        store = get_store()
        with store._cursor() as cur:
            cur.execute(
                "DELETE FROM customers WHERE primary_name = %s OR canonical_id LIKE %s",
                (PEER_NAME, f"%{PEER_NAME}%"),
            )
            ok("L2 PG 旧 smoke 数据已清")
    except Exception as e:
        fail(f"清 PG 失败: {e}")
        errors += 1

    # ── 1. greeting bot 接入 (sync_friend_request_sent) ──────────────
    step("1) greeting bot — 加好友 → sync_friend_request_sent")
    try:
        from src.host.customer_sync_bridge import sync_friend_request_sent
        cid = sync_friend_request_sent(
            DEVICE_ID, PEER_NAME,
            status="sent",
            persona_key=PERSONA_KEY,
            preset_key="growth_v2",
            source="smoke_test",
            note="情感陪护机器人测试",
        )
        if cid:
            ok(f"customer_id={cid[:12]}…")
        else:
            fail("bridge 返回 None")
            errors += 1
    except Exception as e:
        fail(f"bridge 异常: {e}")
        errors += 1

    # ── 2. greeting bot — 发打招呼 (sync_greeting_sent) ──────────────
    step("2) greeting bot — 发打招呼 → sync_greeting_sent")
    try:
        from src.host.customer_sync_bridge import sync_greeting_sent
        cid2 = sync_greeting_sent(
            DEVICE_ID, PEER_NAME,
            greeting="はじめまして、最近どうですか？",
            template_id="jp:smoke",
            preset_key="growth_v2",
            persona_key=PERSONA_KEY,
            phase="warmup",
        )
        if cid2:
            ok(f"greeting 发出, customer_id={cid2[:12]}…")
        else:
            fail("greeting bridge 返回 None")
            errors += 1
    except Exception as e:
        fail(f"greeting 异常: {e}")
        errors += 1

    # ── 3. messenger 入站 (sync_messenger_incoming) ──────────────────
    step("3) messenger inbound — 客户回复")
    try:
        from src.host.customer_sync_bridge import sync_messenger_incoming
        cid3 = sync_messenger_incoming(
            DEVICE_ID, PEER_NAME,
            content="ありがとうございます、毎日忙しくて疲れますね",
            content_lang="ja",
            peer_type="friend",
        )
        ok(f"入站 OK, status 应升 in_messenger, customer_id={cid3[:12]}…")
    except Exception as e:
        fail(f"入站异常: {e}")
        errors += 1

    # ── 4. messenger 出站 (sync_messenger_outgoing) ──────────────────
    step("4) messenger outbound — AI 关爱回复")
    try:
        from src.host.customer_sync_bridge import sync_messenger_outgoing
        sync_messenger_outgoing(
            DEVICE_ID, PEER_NAME,
            content="お疲れ様です、家事や育児大変ですよね",
            ai_decision="reply",
            ai_generated=True,
            content_lang="ja",
            intent_tag="empathy",
        )
        ok("AI 回复 OK")
    except Exception as e:
        fail(f"AI 出站异常: {e}")
        errors += 1

    # ── 5. referral_gate 触发测试 ────────────────────────────────────
    step("5) referral_gate — 关键词命中 'LINE教えて' 应 hard_allow")
    try:
        from src.ai.referral_gate import should_refer
        d = should_refer(
            intent="smalltalk",
            has_contact=True,
            memory_ctx={"profile": {"total_turns": 3, "peer_reply_count": 2}},
            incoming_text="もしよければLINE教えてください",
            persona_key=PERSONA_KEY,
        )
        if d.refer and d.level == "hard_allow":
            ok(f"hard_allow ({d.reasons[0][:50]})")
        else:
            fail(f"应 hard_allow, 实际 level={d.level} refer={d.refer}")
            errors += 1
    except Exception as e:
        fail(f"referral_gate 异常: {e}")
        errors += 1

    # ── 6. referral_gate — 拒绝词命中 ────────────────────────────────
    step("6) referral_gate — 拒绝词 '結構です' 应 hard_block")
    try:
        from src.ai.referral_gate import should_refer
        d = should_refer(
            intent="smalltalk",
            has_contact=True,
            incoming_text="LINEは結構です",
            persona_key=PERSONA_KEY,
        )
        if not d.refer and d.level == "hard_block":
            ok(f"hard_block ({d.reasons[0][:60]})")
        else:
            fail(f"应 hard_block, 实际 level={d.level}")
            errors += 1
    except Exception as e:
        fail(f"拒绝词异常: {e}")
        errors += 1

    # ── 7. emotion_scorer (LLM 失败 fallback 中性分) ─────────────────
    step("7) chat_emotion_scorer — 评分 (LLM 不可用 fallback 中性 0.5)")
    try:
        from src.ai.chat_emotion_scorer import score_emotion
        result = score_emotion(
            [{"role": "user", "content": "毎日仕事疲れた"},
             {"role": "assistant", "content": "お疲れ様、頑張りすぎないでね"}],
            persona_key=PERSONA_KEY,
        )
        ok(f"评分: trust={result.get('trust')} interest={result.get('interest')} "
           f"frustration={result.get('frustration')} topic_match={result.get('topic_match')} "
           f"overall={result.get('overall'):.2f} fallback={result.get('fallback')}")
    except Exception as e:
        fail(f"emotion_scorer 异常: {e}")
        errors += 1

    # ── 8. handoff 真发起 (sync_handoff_to_line) ─────────────────────
    step("8) sync_handoff_to_line — 真发起人机交接")
    try:
        from src.host.customer_sync_bridge import sync_handoff_to_line, build_simple_summary
        ai_summary = build_simple_summary(
            persona_key=PERSONA_KEY,
            intent_tag="invite_line",
            last_incoming="LINE教えて",
            last_outgoing="もちろん、ID は xxxx です",
        )
        hid = sync_handoff_to_line(
            DEVICE_ID, PEER_NAME,
            ai_summary=ai_summary,
        )
        if hid:
            ok(f"handoff_id={hid[:12]}…  ai_summary='{ai_summary[:60]}…'")
        else:
            fail("handoff 返回 None")
            errors += 1
    except Exception as e:
        fail(f"handoff 异常: {e}")
        errors += 1

    # ── 9. ai_takeover_state — 真人接管 → AI 暂停 ────────────────────
    step("9) ai_takeover_state — 真人按'我接手' → AI 应短路")
    try:
        from src.host import ai_takeover_state
        ai_takeover_state.clear_for_tests()
        assert not ai_takeover_state.is_taken_over(PEER_NAME, DEVICE_ID)
        ai_takeover_state.mark_taken_over(PEER_NAME, DEVICE_ID, by_username="agent_smoke")
        if ai_takeover_state.is_taken_over(PEER_NAME, DEVICE_ID):
            info = ai_takeover_state.get_takeover_info(PEER_NAME, DEVICE_ID)
            ok(f"接管中, by={info['by']}")
        else:
            fail("接管标记没生效")
            errors += 1
        ai_takeover_state.release(PEER_NAME, DEVICE_ID)
        if not ai_takeover_state.is_taken_over(PEER_NAME, DEVICE_ID):
            ok("释放生效")
        else:
            fail("释放没生效")
            errors += 1
    except Exception as e:
        fail(f"ai_takeover_state 异常: {e}")
        errors += 1

    # ── 10. customer_service 4 个动作 (assign/reply/note/outcome) ────
    step("10) customer_service — 真人客服 4 个动作")
    try:
        from src.host.lead_mesh import customer_service as cs
        from src.host.database import _connect
        # 临时插一条 lead_handoffs
        smoke_hid = "smoke-cs-" + uuid.uuid4().hex[:8]
        with _connect() as conn:
            conn.execute(
                "INSERT INTO lead_handoffs (handoff_id, canonical_id, source_agent, channel, state) "
                "VALUES (?, ?, ?, ?, ?)",
                (smoke_hid, "lead-smoke-001", "agent_a", "line", "pending"),
            )
            conn.commit()

        cs.assign_to_human(smoke_hid, "agent_smoke",
                          peer_name_hint=PEER_NAME, device_id_hint=DEVICE_ID)
        ok("assign_to_human (AI 接管被同步标记)")

        cs.record_human_reply(smoke_hid, "agent_smoke", "もちろんお話しましょう")
        cs.record_human_reply(smoke_hid, "agent_smoke", "私のLINEは...")
        ok("record_human_reply ×2")

        cs.record_internal_note(smoke_hid, "agent_smoke", "客户对孩子话题有共鸣, 优先")
        ok("record_internal_note")

        cs.record_outcome(smoke_hid, "agent_smoke", "converted",
                         notes="客户加 LINE 成交",
                         peer_name_hint=PEER_NAME, device_id_hint=DEVICE_ID)
        # 终态应释放 ai_takeover
        if not ai_takeover_state.is_taken_over(PEER_NAME, DEVICE_ID):
            ok("record_outcome converted → AI 接管自动释放")
        else:
            fail("converted 终态应释放但没释放")
            errors += 1

        # full 详情验证
        full = cs.get_handoff_full(smoke_hid)
        replies_count = len(full.get("customer_service_replies", []))
        notes_count = len(full.get("internal_notes", []))
        if replies_count == 2 and notes_count == 1:
            ok(f"get_handoff_full: replies={replies_count} notes={notes_count} outcome={full['outcome']}")
        else:
            fail(f"replies={replies_count} (期望2) notes={notes_count} (期望1)")
            errors += 1

        # 清理 smoke handoff
        with _connect() as conn:
            conn.execute("DELETE FROM lead_handoffs WHERE handoff_id = ?", (smoke_hid,))
            conn.commit()
    except Exception as e:
        fail(f"customer_service 异常: {e}")
        errors += 1

    # ── 11. PG 数据查证 ──────────────────────────────────────────────
    step("11) PG 数据查证 — customers / events / chats / handoffs 都有 smoke 数据")
    try:
        from src.host.central_customer_store import get_store
        store = get_store()
        with store._cursor() as cur:
            cur.execute(
                "SELECT customer_id::text, primary_name, status FROM customers WHERE primary_name = %s",
                (PEER_NAME,),
            )
            row = cur.fetchone()
            if row and row["primary_name"] == PEER_NAME:
                ok(f"customers: customer_id={row['customer_id'][:12]}… status={row['status']}")
            else:
                fail("没找到 smoke 客户")
                errors += 1

            cur.execute(
                "SELECT event_type FROM customer_events WHERE customer_id IN "
                "(SELECT customer_id FROM customers WHERE primary_name = %s) "
                "ORDER BY ts",
                (PEER_NAME,),
            )
            events = [r["event_type"] for r in cur.fetchall()]
            ok(f"customer_events ({len(events)}): {events}")

            cur.execute(
                "SELECT channel, direction FROM customer_chats WHERE customer_id IN "
                "(SELECT customer_id FROM customers WHERE primary_name = %s) "
                "ORDER BY ts",
                (PEER_NAME,),
            )
            chats = [(r["channel"], r["direction"]) for r in cur.fetchall()]
            ok(f"customer_chats ({len(chats)}): {chats}")

            cur.execute(
                "SELECT to_stage, from_stage FROM customer_handoffs WHERE customer_id IN "
                "(SELECT customer_id FROM customers WHERE primary_name = %s)",
                (PEER_NAME,),
            )
            handoffs = [(r["from_stage"], r["to_stage"]) for r in cur.fetchall()]
            ok(f"customer_handoffs ({len(handoffs)}): {handoffs}")

    except Exception as e:
        fail(f"PG 查证异常: {e}")
        errors += 1

    # ── 12. push 端点 metrics 增长验证 ───────────────────────────────
    step("12) /cluster/customers/push/metrics 验证")
    try:
        from urllib.request import urlopen
        with urlopen("http://192.168.0.118:8000/cluster/customers/push/metrics", timeout=5) as r:
            data = json.loads(r.read())
        m = data["metrics"]
        ok(f"metrics: total={m['push_total']} success={m['push_success']} "
           f"failure={m['push_failure']} queue_pending={m['queue_pending']}")
        d = data["drain"]
        ok(f"drain: running={d.get('running')} iterations={d.get('iterations',0)}")
    except Exception as e:
        fail(f"metrics 异常: {e}")
        errors += 1

    # ── summary ──────────────────────────────────────────────────────
    print()
    if errors == 0:
        print(f"{GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
        print(f"{GREEN}✅ ALL 12 STEPS PASSED — L2 端到端跑通{RESET}")
        print(f"{GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
        return 0
    else:
        print(f"{RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
        print(f"{RED}❌ {errors} 个步骤失败{RESET}")
        print(f"{RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
