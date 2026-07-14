# -*- coding: utf-8 -*-
"""Smoke runner 的 meta 测试: 保证 scripts/messenger_workflow_smoke.py 跑起来
不崩 + 在当前分支 (B P7 栈顶) 达到预期的 PASS/SKIP 分布。

本测试是 "测试的测试" — 保护 smoke runner 本身不回归。实际业务验证
由 smoke 各 step 完成,不在这里重复。
"""
from __future__ import annotations

import os

import pytest

# P2-⑫: spawn 子 Python 进程时强制 UTF-8 防 Windows cp936 emoji 解码挂.
_UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def test_smoke_runs_without_fail(tmp_db):
    """跑 smoke runner 不应有 FAIL, SKIP 可接受 (依赖分支未 merge 正常)。

    tmp_db fixture 用 conftest 的隔离 sqlite; smoke runner 自己又会切一个
    临时 DB path, 两者互不干扰。
    """
    from scripts.messenger_workflow_smoke import run_smoke
    results, exit_code = run_smoke()
    fail_steps = [r for r in results if r.status == "FAIL"]
    assert fail_steps == [], \
        f"smoke FAIL steps: {[(r.name, r.reason) for r in fail_steps]}"
    assert exit_code == 0


def test_smoke_core_steps_pass(tmp_db):
    """即使 A 的 Phase 5 / B 的 P0/P1 未 merge, 核心步骤应仍然 PASS:
    setup/seed_leads/send_frs/accept/send_greetings/peers_reply/
    chat_memory/intent/gate/wa_referral/funnel/teardown 这 12 步
    是 B P7 栈顶必然可用的。"""
    from scripts.messenger_workflow_smoke import run_smoke
    results, _ = run_smoke()
    by_name = {r.name: r for r in results}

    # 这 8 步必须 PASS (本分支代码齐)
    must_pass = [
        "01_setup_tmp_db", "02_seed_leads_A", "03_a_send_friend_requests",
        "05_peers_accept_friend", "06_a_send_greetings",
        "07_peers_reply_incoming", "09_b_chat_memory",
        "10_b_intent_classify", "11_b_referral_gate",
        "12_b_wa_referral_sent", "13_verify_funnel_metrics",
        "15_teardown",
    ]
    for name in must_pass:
        assert by_name[name].status == "PASS", \
            f"{name} should PASS, got {by_name[name].status} " \
            f"({by_name[name].reason})"


def test_smoke_funnel_metrics_make_sense(tmp_db):
    """漏斗指标应串联: friend_request=3 → accepted=3 → greetings=3 →
    inbox_incoming=3 → outgoing_replies 含 3 greetings + 2 wa_referrals
    → wa_referrals=2。"""
    from scripts.messenger_workflow_smoke import run_smoke
    results, _ = run_smoke()
    funnel_step = next(r for r in results
                       if r.name == "13_verify_funnel_metrics")
    assert funnel_step.status == "PASS"
    d = funnel_step.data
    assert d.get("stage_friend_request_sent") == 3
    assert d.get("stage_friend_accepted") == 3
    assert d.get("stage_greetings_sent") == 3
    assert d.get("stage_inbox_incoming") == 3
    assert d.get("stage_wa_referrals") == 2
    # outgoing_replies = greetings + wa_referrals = 5
    assert d.get("stage_outgoing_replies", 0) >= 5


def test_smoke_intents_classified_correctly(tmp_db):
    """3 个模拟对方消息的 intent 分类:
    - alice "LINEでも連絡" → referral_ask
    - carol "how much" → buying
    - bob "ciao come stai" → (closing/smalltalk 都可接受, 验 alice/carol 即可)
    """
    from scripts.messenger_workflow_smoke import run_smoke
    results, _ = run_smoke()
    intent_step = next(r for r in results
                       if r.name == "10_b_intent_classify")
    assert intent_step.status == "PASS"
    intents = intent_step.data["intents"]
    assert intents["alice.yamada"] == "referral_ask"
    assert intents["carol.smith"] == "buying"


def test_smoke_gate_decisions(tmp_db):
    """referral_gate: alice/carol 应走 hard_allow, bob 走 soft_*。"""
    from scripts.messenger_workflow_smoke import run_smoke
    results, _ = run_smoke()
    gate_step = next(r for r in results if r.name == "11_b_referral_gate")
    assert gate_step.status == "PASS"
    d = gate_step.data
    assert d["alice.yamada"] == "hard_allow"
    assert d["carol.smith"] == "hard_allow"
    assert d["bob.rossi"].startswith("soft_")


def test_smoke_no_color_flag():
    """--no-color flag 不应报错 (CI 场景)。"""
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    r = subprocess.run(
        [sys.executable, str(repo / "scripts" / "messenger_workflow_smoke.py"),
         "--no-color"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=_UTF8_ENV, timeout=60,
    )
    # 只验证能跑起来, 结果细节由其他 test 覆盖
    assert r.returncode in (0, 1), f"unexpected exit: {r.returncode}"
    # 输出里应该有 Summary
    assert "Summary" in r.stdout or "Summary" in r.stderr
