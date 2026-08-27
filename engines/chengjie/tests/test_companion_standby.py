"""P1-2「AI 值守」三档姿态纯函数 + 与护栏组合契约（零 IO）。"""

from __future__ import annotations

from src.companion.standby_mode import (
    STANDBY_MODES, build_standby_plan, infer_standby_mode, is_standby_mode,
    standby_options,
)
from src.companion.capability_toggle import check_toggle


def _plan_map(mode):
    """把计划压成 {(key,field): value} 便于断言。"""
    return {(it["key"], it["field"]): it["value"] for it in build_standby_plan(mode)}


# ── build_standby_plan：三档 → 两键目标态 ────────────────────────────────────

def test_off_plan_turns_both_off():
    m = _plan_map("off")
    assert m[("l2_autosend_worker", "enabled")] is False
    assert m[("l2_autosend_deliver", "enabled")] is False


def test_suggest_plan_worker_on_deliver_off():
    m = _plan_map("suggest")
    assert m[("l2_autosend_worker", "enabled")] is True
    assert m[("l2_autosend_deliver", "enabled")] is False


def test_watching_plan_both_on():
    m = _plan_map("watching")
    assert m[("l2_autosend_worker", "enabled")] is True
    assert m[("l2_autosend_deliver", "enabled")] is True


def test_watching_enables_send_gate_safeguard():
    """优化：值守中一键即得「受保护的自动回复」——自动开出站安全闸。"""
    m = _plan_map("watching")
    assert m[("companion_send_gate", "enabled")] is True


def test_watching_gate_up_before_deliver_armed():
    """送闸 send-gate(safeguard) 必须在 critical deliver 之前开——先立闸再武装真发。"""
    order = [(it["key"], it["value"]) for it in build_standby_plan("watching")]
    keys = [k for k, v in order if v is True]
    assert keys.index("companion_send_gate") < keys.index("l2_autosend_deliver")


def test_off_and_suggest_do_not_touch_send_gate():
    """安全闸黏住：off/suggest 绝不去动 send-gate（避免误关一个纯护栏）。"""
    for mode in ("off", "suggest"):
        keys = {it["key"] for it in build_standby_plan(mode)}
        assert "companion_send_gate" not in keys


def test_watching_order_worker_before_deliver():
    """执行序：worker(低风险) 必须在 deliver(critical 主开关) 之前开，避免中途自相矛盾。"""
    plan = build_standby_plan("watching")
    order = [it["key"] for it in plan if it["value"] is True]
    assert order.index("l2_autosend_worker") < order.index("l2_autosend_deliver")


def test_off_order_deliver_before_worker():
    """关闭序：先关 deliver(真发) 再关 worker，任一时刻都不会出现 deliver 开而 worker 关。"""
    plan = build_standby_plan("off")
    order = [it["key"] for it in plan]
    assert order.index("l2_autosend_deliver") < order.index("l2_autosend_worker")


def test_unknown_mode_returns_none():
    assert build_standby_plan("bogus") is None
    assert not is_standby_mode("bogus")
    assert is_standby_mode("watching")


def test_standby_plan_only_touches_autosend_axis():
    """值守只治理反应式 autosend 轴（worker/deliver + 值守中的 send-gate）——
    绝不隐式动 proactive/voice/翻译等其它风险轴能力。"""
    for mode in STANDBY_MODES:
        keys = {it["key"] for it in build_standby_plan(mode)}
        assert keys <= {"l2_autosend_worker", "l2_autosend_deliver", "companion_send_gate"}


# ── infer_standby_mode：反推当前档 ───────────────────────────────────────────

def _cfg(worker, deliver):
    return {"inbox": {"l2_autosend": {"enabled": worker, "deliver": deliver}}}


def test_infer_all_states():
    assert infer_standby_mode(_cfg(False, False)) == "off"
    assert infer_standby_mode(_cfg(True, False)) == "suggest"
    assert infer_standby_mode(_cfg(True, True)) == "watching"
    assert infer_standby_mode(_cfg(False, True)) == "custom"   # deliver 开而 worker 关＝矛盾
    assert infer_standby_mode({}) == "off"                      # 空 config = 全关


def test_options_ordered_low_to_high():
    opts = standby_options()
    assert [o["mode"] for o in opts] == ["off", "suggest", "watching"]
    assert all(o["label"] for o in opts)


# ── 与护栏组合契约：值守中真发仍受双 opt-in 约束（护栏不被姿态绕过）─────────────

def test_watching_deliver_blocked_without_auto_ai():
    """全局默认档非全自动且无 auto_ai 会话 → check_toggle 仍拦 deliver。

    2026-08-22 起需显式钉 automation_mode=review 才触发旧判据——值守「watching」
    捆绑写入会先把全局档落 auto_ai（standby_extras），届时本闸自然放行；
    这里守的是「档位没跟上时护栏不放水」的兜底语义。"""
    cfg = _cfg(True, False)          # worker 已开（值守计划会先开 worker）
    cfg.setdefault("inbox", {})["auto_draft"] = {"automation_mode": "review"}
    chk = check_toggle(cfg, {}, "l2_autosend_deliver", "enabled", True)
    assert chk["allowed"] is False
    assert "auto_ai" in chk["reason"] or "全自动" in chk["reason"]


def test_watching_bundle_semantics():
    """P1 捆绑语义（2026-08-22）：三档 extras/对齐目标的契约钉。"""
    from src.companion.standby_mode import standby_align_target, standby_extras

    w = {e["path"]: e["value"] for e in standby_extras("watching")}
    assert w["inbox.auto_draft.automation_mode"] == "auto_ai"
    assert w["inbox.auto_draft.bootstrap_automation_mode"] is True
    s = {e["path"]: e["value"] for e in standby_extras("suggest")}
    assert s["inbox.auto_draft.automation_mode"] == "review"
    o = {e["path"]: e["value"] for e in standby_extras("off")}
    assert o["inbox.auto_draft.automation_mode"] == "manual"
    assert standby_extras("nope") == []
    assert standby_align_target("watching") == "auto_ai"
    assert standby_align_target("suggest") == "review"
    assert standby_align_target("off") is None   # off 刻意不批量覆写档位现场


def test_align_existing_conversations_scoped_to_system_sources(tmp_path):
    """存量对齐只动 bootstrap/standby 来源的行；人的显式设置/接管/守卫降档
    绝不覆盖；升 auto_ai 时群会话跳过（confirm_group 铁律）。"""
    from src.inbox.store import InboxStore
    from src.companion.standby_mode import align_existing_conversations

    store = InboxStore(tmp_path / "align.db")
    store.set_automation_mode("telegram:a:p1", "review", source="bootstrap")
    store.set_automation_mode("telegram:a:p2", "review", source="human")
    store.set_automation_mode("telegram:a:p3", "manual", source="takeover")
    store.set_automation_mode("telegram:a:p4", "review", source="standby")
    # 群会话（telegram 负 id）+ bootstrap 来源：升 auto_ai 必须跳过
    store.set_automation_mode("telegram:a:-100888", "review", source="bootstrap")
    n = align_existing_conversations(store, "auto_ai")
    assert n == 2
    assert store.get_automation_mode_if_set("telegram:a:p1") == "auto_ai"
    assert store.get_automation_mode_if_set("telegram:a:p2") == "review"    # human 保留
    assert store.get_automation_mode_if_set("telegram:a:p3") == "manual"    # takeover 保留
    assert store.get_automation_mode_if_set("telegram:a:p4") == "auto_ai"
    assert store.get_automation_mode_if_set("telegram:a:-100888") == "review"  # 群跳过
    # 降 review 无群限制：两条 auto_ai（现 source=standby）回 review
    n2 = align_existing_conversations(store, "review")
    assert n2 == 2
    assert store.get_automation_mode_if_set("telegram:a:p1") == "review"
    # 无该读口的旧 store → 0（优雅退化）
    class _Old:
        pass
    assert align_existing_conversations(_Old(), "auto_ai") == 0
    assert align_existing_conversations(None, "auto_ai") == 0
    assert align_existing_conversations(store, "bogus") == 0


def test_watching_deliver_allowed_with_auto_ai_and_gate():
    cfg = {"inbox": {"l2_autosend": {"enabled": True, "deliver": False}},
           "companion_send_gate": {"enabled": True}}
    modes = {"c1": "auto_ai"}
    chk = check_toggle(cfg, modes, "l2_autosend_deliver", "enabled", True)
    assert chk["allowed"] is True
    assert not chk.get("warn")


def test_watching_deliver_warns_without_send_gate():
    """有 auto_ai 但 send-gate 未开 → 放行但 warn（裸奔提示），护栏语义保持。"""
    cfg = _cfg(True, False)
    modes = {"c1": "auto_ai"}
    chk = check_toggle(cfg, modes, "l2_autosend_deliver", "enabled", True)
    assert chk["allowed"] is True
    assert chk.get("warn") is True
