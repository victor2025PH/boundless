# -*- coding: utf-8 -*-
"""L2 数据模拟器 — 生成 30 个真实场景的虚拟客户全链路数据.

目的:
    让 L3 看板 / 主控 PG 有真实数据, victor 打开看到完整漏斗:
    in_funnel → in_messenger → in_line → accepted_by_human → converted/lost

不模拟物理 UI, 直接调 customer_sync_bridge + customer_service 函数.
跟真业务的 L2 数据 schema 完全一致, 真业务上线时能无缝替换.

每个客户的故事都不同:
- 30% 客户 high engaged → handoff → 真人接管 → 一半成交一半流失
- 40% 客户 medium → 在 messenger 聊但还没引流
- 20% 客户 low → 加好友刚发了打招呼客户没回
- 10% 客户 rejected → 拒绝词命中触发 7 天冷却
"""
from __future__ import annotations

import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

os.environ.setdefault("OPENCLAW_COORDINATOR_URL", "http://192.168.0.118:8000")

# ── 30 个虚拟客户名 (日本中年女性 persona) ──────────────────────────
JP_FEMALE_NAMES = [
    "Yumi Tanaka", "Sakura Watanabe", "Hiroko Yamamoto", "Kazuko Suzuki",
    "Mariko Kobayashi", "Junko Sato", "Akiko Ito", "Naoko Nakamura",
    "Reiko Inoue", "Tomoko Kimura", "Etsuko Matsumoto", "Kyoko Yoshida",
    "Yoko Hayashi", "Michiko Yamada", "Setsuko Mori", "Hatsuko Hashimoto",
    "Tamiko Saito", "Sumiko Goto", "Fumiko Okada", "Ikuko Murakami",
    "Mieko Aoki", "Tomoko Fujita", "Shizuko Nishimura", "Keiko Oda",
    "Hisako Nakamoto", "Akane Otsuka", "Mayumi Hashizume", "Chizuko Komori",
    "Naomi Takagi", "Yoshiko Saeki",
]

WORKER_IDS = ["worker-w03", "worker-175", "worker-coordinator"]
DEVICES_BY_WORKER = {
    "worker-w03": [f"W03-DEV-{i:03d}" for i in range(15)],
    "worker-175": [f"W175-DEV-{i:03d}" for i in range(15)],
    "worker-coordinator": ["4HUSIB4TBQC69TJZ", "CACAVKLNU8SGO74D", "IJ8HZLORS485PJWW"],
}

# Greeting / chat 模板 (日本男性关爱型, 模拟 ChatBrain 输出)
GREETING_TEMPLATES = [
    "はじめまして、最近どうですか？",
    "こんにちは、お元気ですか？",
    "週末はゆっくり過ごせましたか？",
    "毎日お疲れ様です、無理しないでくださいね",
]

INCOMING_TEMPLATES_HIGH = [
    "ありがとうございます、最近少し疲れています",
    "そうですね、子供のことで悩んでいて...",
    "お話できて嬉しいです",
    "今度LINEでお話しませんか？",
    "もっと色々聞いてもらえたら嬉しいです",
]

INCOMING_TEMPLATES_MEDIUM = [
    "こんばんは",
    "ありがとうございます",
    "そうですね",
    "毎日忙しいです",
]

INCOMING_TEMPLATES_LOW = ["…", "嗯", "えっと"]

INCOMING_REJECTED = [
    "LINEは結構です",
    "興味ないです",
    "やめてください",
]


def step(label: str) -> None:
    print(f"\n━━━ {label} ━━━")


def ok(label: str) -> None:
    print(f"  [OK] {label}")


def info(label: str) -> None:
    print(f"  [..] {label}")


def simulate_customer(
    name: str,
    scenario: str,  # "high" / "medium" / "low" / "rejected"
    worker_id: str,
    device_id: str,
) -> dict:
    """模拟一个客户的全链路."""
    from src.host.customer_sync_bridge import (
        sync_friend_request_sent,
        sync_greeting_sent,
        sync_messenger_incoming,
        sync_messenger_outgoing,
        sync_handoff_to_line,
        build_simple_summary,
    )
    # 直接走 sync 模式 upsert 让 customer 先真进 PG, 避免 fire_and_forget
    # 异步还没跑完 sync_handoff_to_line 外键约束失败
    from src.host.central_push_client import upsert_customer
    canonical_source = "facebook_name"
    canonical_id_str = f"{device_id}::{name}"
    upsert_customer(
        canonical_id=canonical_id_str,
        canonical_source=canonical_source,
        primary_name=name,
        worker_id=worker_id,
        device_id=device_id,
        ai_profile={"persona_key": "jp_female_midlife"},
        fire_and_forget=False,  # sync 等 PG 真接到
    )
    import os as _os
    _os.environ["OPENCLAW_COORDINATOR_URL"] = "http://192.168.0.118:8000"
    _os.environ.pop("OPENCLAW_API_KEY", None)

    # 让 worker_id 真生效 (bridge 调 _safe_worker_id 会读 cluster.yaml,
    # 我们 override sync 函数的 worker_id 参数走 _ensure_customer 路径)
    import src.host.customer_sync_bridge as _bridge
    original = _bridge._safe_worker_id
    _bridge._safe_worker_id = lambda: worker_id

    try:
        # 1. 加好友
        cid = sync_friend_request_sent(
            device_id, name,
            status="sent",
            persona_key="jp_female_midlife",
            preset_key="growth_v2",
            source="simulated_data",
        )

        # 2. 发打招呼
        greeting = random.choice(GREETING_TEMPLATES)
        sync_greeting_sent(
            device_id, name,
            greeting=greeting,
            template_id=f"jp:{random.randint(1, 10)}",
            preset_key="growth_v2",
            persona_key="jp_female_midlife",
            phase="warmup",
        )

        if scenario == "low":
            # low: 加好友 + 打招呼后客户没回, 卡在 in_funnel
            return {"customer_id": cid, "name": name, "scenario": scenario,
                    "status": "in_funnel"}

        # 3. 客户回了消息 (incoming)
        if scenario == "rejected":
            # rejected: 客户拒绝引流
            for _ in range(2):
                sync_messenger_incoming(
                    device_id, name,
                    content=random.choice(INCOMING_TEMPLATES_LOW),
                    content_lang="ja", peer_type="friend",
                )
                sync_messenger_outgoing(
                    device_id, name,
                    content="お疲れ様です、家族とゆっくり過ごせましたか？",
                    ai_decision="reply", ai_generated=True, content_lang="ja",
                )
            # 拒绝词命中
            sync_messenger_incoming(
                device_id, name,
                content=random.choice(INCOMING_REJECTED),
                content_lang="ja",
            )
            return {"customer_id": cid, "name": name, "scenario": scenario,
                    "status": "in_messenger_rejected"}

        # medium: 在 messenger 聊几轮, 没引流
        # high: 聊 7+ 轮 → 发引流话术 → handoff → 真人接管
        n_turns = random.randint(3, 5) if scenario == "medium" else random.randint(7, 12)
        templates = INCOMING_TEMPLATES_MEDIUM if scenario == "medium" else INCOMING_TEMPLATES_HIGH
        for _ in range(n_turns):
            sync_messenger_incoming(
                device_id, name,
                content=random.choice(templates),
                content_lang="ja", peer_type="friend",
            )
            sync_messenger_outgoing(
                device_id, name,
                content=random.choice([
                    "そうなんですね、頑張りすぎないでくださいね",
                    "それは大変ですね、私も応援していますよ",
                    "今度ゆっくりお話しましょう",
                    "毎日お疲れ様です",
                ]),
                ai_decision="reply", ai_generated=True, content_lang="ja",
                intent_tag="empathy",
            )

        if scenario == "medium":
            return {"customer_id": cid, "name": name, "scenario": scenario,
                    "status": "in_messenger"}

        # high: 发起 handoff
        ai_summary = build_simple_summary(
            persona_key="jp_female_midlife",
            intent_tag="invite_line_high_trust",
            last_incoming="今度LINEでお話しませんか？",
            last_outgoing="もちろんです、私の LINE ID は xxxx です",
        )
        hid = sync_handoff_to_line(
            device_id, name,
            ai_summary=ai_summary,
        )

        return {"customer_id": cid, "name": name, "scenario": scenario,
                "status": "in_line", "handoff_id": hid, "device_id": device_id}
    finally:
        _bridge._safe_worker_id = original


def simulate_human_takeover(
    customer_data: dict,
    outcome: str,  # "converted" / "lost" / "pending_followup"
) -> None:
    """对 high 场景的客户, 模拟真人接管 + 标记结果."""
    if customer_data["scenario"] != "high" or "handoff_id" not in customer_data:
        return
    # 主控 customer_handoffs (PG) 直接 accept + complete
    try:
        from src.host.central_customer_store import get_store
        store = get_store()
        agent_username = random.choice(["agent_001", "agent_002", "agent_003"])
        ok_acc = store.accept_handoff(customer_data["handoff_id"], agent_username)
        ok_done = store.complete_handoff(customer_data["handoff_id"], outcome)
        info(f"  接管 by {agent_username} → {outcome}")
    except Exception as e:
        info(f"  接管失败: {e}")


def main() -> int:
    random.seed(42)
    step(f"L2 数据模拟器 - 生成 {len(JP_FEMALE_NAMES)} 客户全链路")

    scenarios = (
        ["high"] * 9 +     # 30% 高意向 → handoff → 接管 → 一半成交一半流失
        ["medium"] * 12 +  # 40% 在 messenger 聊
        ["low"] * 6 +      # 20% 加好友后没回
        ["rejected"] * 3   # 10% 拒绝
    )
    random.shuffle(scenarios)
    assert len(scenarios) == len(JP_FEMALE_NAMES)

    customers = []
    for i, name in enumerate(JP_FEMALE_NAMES):
        scenario = scenarios[i]
        worker_id = random.choice(WORKER_IDS)
        device_id = random.choice(DEVICES_BY_WORKER[worker_id])
        result = simulate_customer(name, scenario, worker_id, device_id)
        customers.append(result)
        info(f"#{i+1:02d} [{scenario:9s}] {name} → {result.get('status', '?')} "
             f"(worker={worker_id} device={device_id[:14]})")
        # 错开点时间, 避免 deterministic UUID 冲突 (虽然 name 不同就不会冲)
        time.sleep(0.05)

    step("对 high 场景的客户做真人接管 + 标记结果")
    high_customers = [c for c in customers if c["scenario"] == "high"]
    for i, c in enumerate(high_customers):
        # 一半成交, 一半流失, 极少 pending
        if i < len(high_customers) // 2:
            outcome = "converted"
        elif i < int(len(high_customers) * 0.85):
            outcome = "lost"
        else:
            outcome = "pending_followup"
        info(f"#{i+1:02d} {c['name']} → {outcome}")
        simulate_human_takeover(c, outcome)

    step("等待异步 push 完成 (fire_and_forget executor)")
    time.sleep(5)
    ok("异步 push 完成")

    step("查 PG 验证最终数据")
    try:
        from src.host.central_customer_store import get_store
        store = get_store()
        with store._cursor() as cur:
            cur.execute("SELECT status, COUNT(*) AS n FROM customers "
                       "WHERE primary_name = ANY(%s::text[]) "
                       "GROUP BY status ORDER BY n DESC",
                       (JP_FEMALE_NAMES,))
            for r in cur.fetchall():
                ok(f"status={r['status']:20s} {r['n']} 客户")

            cur.execute("SELECT event_type, COUNT(*) AS n FROM customer_events "
                       "WHERE customer_id IN ("
                       " SELECT customer_id FROM customers WHERE primary_name = ANY(%s::text[])"
                       ") GROUP BY event_type ORDER BY n DESC",
                       (JP_FEMALE_NAMES,))
            print()
            print("  事件统计:")
            for r in cur.fetchall():
                print(f"    {r['event_type']:30s} {r['n']:4d}")

            cur.execute("SELECT outcome, COUNT(*) AS n FROM customer_handoffs "
                       "WHERE customer_id IN ("
                       " SELECT customer_id FROM customers WHERE primary_name = ANY(%s::text[])"
                       ") GROUP BY outcome ORDER BY n DESC",
                       (JP_FEMALE_NAMES,))
            print()
            print("  Handoff 结果:")
            for r in cur.fetchall():
                print(f"    {r['outcome'] or '(pending 接管)':25s} {r['n']:4d}")
    except Exception as e:
        info(f"PG 查证异常: {e}")

    step(f"完成 - 模拟生成 {len(customers)} 个客户")
    return 0


if __name__ == "__main__":
    sys.exit(main())
