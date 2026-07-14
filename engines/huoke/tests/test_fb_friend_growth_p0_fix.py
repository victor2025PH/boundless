"""P0 修复回归测试：好友打招呼任务"虚假成功"

覆盖范围：
1. _validate_preset_inputs — 缺失/已填/persona 兜底/空字符串
2. friend_growth & group_hunter preset 必填字段声明
3. executor facebook_extract_members:
   - group_name 为空 → 立即失败 (False, msg, outcome=missing_param)
   - group_name 给定但 0 结果 → 失败 (False, msg, outcome=zero_result)
   - 正常路径 → 成功 (True, "", outcome=ok, count>0)
"""
from __future__ import annotations

import pytest


# ────────────────────────────────────────────────────────────
# 1. _validate_preset_inputs 单元测试
# ────────────────────────────────────────────────────────────

def _get_validator_and_presets():
    from src.host.routers.facebook import (
        _validate_preset_inputs,
        FB_FLOW_PRESETS,
    )
    return _validate_preset_inputs, FB_FLOW_PRESETS


def _preset(key: str) -> dict:
    _, presets = _get_validator_and_presets()
    return next(p for p in presets if p["key"] == key)


def test_friend_growth_preset_declares_required_inputs():
    """friend_growth preset 必须声明 needs_input + input_schema，
    防止"一键启动"创建无群组、无话术的空跑任务（历史 bug 根因）。"""
    p = _preset("friend_growth")
    assert "target_groups" in p["needs_input"]
    assert "verification_note" in p["needs_input"]
    assert "greeting" in p["needs_input"]
    schema = p["input_schema"]
    assert schema["target_groups"]["required"] is True
    assert schema["verification_note"]["required"] is True
    assert schema["greeting"]["required"] is True


def test_group_hunter_preset_declares_target_groups():
    p = _preset("group_hunter")
    assert "target_groups" in p["needs_input"]
    assert p["input_schema"]["target_groups"]["required"] is True


def _extract_step_params(preset_key: str) -> dict:
    p = _preset(preset_key)
    step = next(s for s in p["steps"] if s["type"] == "facebook_extract_members")
    return step["params"]


def _friend_growth_step_params() -> dict:
    p = _preset("friend_growth")
    assert len(p["steps"]) == 1
    step = p["steps"][0]
    assert step["type"] == "facebook_group_member_greet"
    return step["params"]


def test_friend_growth_uses_single_closed_loop_task():
    """好友打招呼是一个业务闭环，不能再拆成提取成员 + 剧本两个独立任务。

    2026-05-03: member_sources 从 ['mutual_members', 'contributors'] 改为
    ['feed_authors'] — 真机 21 轮迭代证实新版 FB 已关闭非管理员的 Members
    列表入口, 旧两池长期 yielded=0; feed_authors 法从帖子作者抽候选,
    绕过权限限制. 测试更新到反映此业务转向.
    """
    params = _friend_growth_step_params()
    assert params["steps"] == ["extract_members", "add_friends"]
    assert params["send_greeting_inline"] is True
    assert params["require_outreach_goal"] is True
    assert params["member_sources"] == ["feed_authors"]


def test_group_hunter_extracts_by_discovery_and_auto_join():
    """宽关键词场景必须先发现群组并尝试入群，不能把关键词当精确群名空跑。"""
    params = _extract_step_params("group_hunter")
    assert params["broad_keyword"] is True
    assert params["discover_groups"] is True
    assert params["auto_join_groups"] is True
    assert params["join_if_needed"] is True
    assert params["skip_visited"] is True
    assert params["max_groups"] > 1
    assert params["max_groups_to_extract"] == params["max_groups"]
    assert params["max_members_per_group"] == params["max_members"]


def test_friend_growth_closed_loop_discovers_groups_and_auto_joins():
    params = _friend_growth_step_params()
    assert params["broad_keyword"] is True
    assert params["discover_groups"] is True
    assert params["auto_join_groups"] is True
    assert params["join_if_needed"] is True
    assert params["skip_visited"] is True
    assert params["max_groups"] > 1
    assert params["max_groups_to_extract"] == params["max_groups"]
    assert params["max_members_per_group"] == params["max_members"]


def test_validate_returns_missing_when_all_empty():
    validate, _ = _get_validator_and_presets()
    p = _preset("friend_growth")
    missing = validate(p, provided={}, persona_ctx={})
    fields = {m["field"] for m in missing}
    assert "target_groups" in fields
    assert "verification_note" in fields
    assert "greeting" in fields


def test_validate_passes_when_all_filled():
    validate, _ = _get_validator_and_presets()
    p = _preset("friend_growth")
    missing = validate(p, provided={
        "target_groups": ["ママ友サークル"],
        "verification_note": "您好🌸看到我们都在 XX 群",
        "greeting": "请多多指教",
    }, persona_ctx={})
    assert missing == []


def test_validate_target_groups_persona_fallback():
    """target_groups 声明 fallback_from=persona.seed_group_keywords：
    当 persona 配了 seeds 时即使 body 没传也算已填。"""
    validate, _ = _get_validator_and_presets()
    p = _preset("group_hunter")
    missing = validate(p, provided={}, persona_ctx={
        "seed_group_keywords": ["ママ友サークル", "アラフィフ ヨガ"],
    })
    # group_hunter 只要求 target_groups，已被 persona 兜底
    fields = {m["field"] for m in missing}
    assert "target_groups" not in fields


def test_validate_empty_string_treated_as_missing():
    """空白字符串不算已填（修复历史上空 verification_note 也派发任务的问题）。"""
    validate, _ = _get_validator_and_presets()
    p = _preset("friend_growth")
    missing = validate(p, provided={
        "target_groups": ["g1"],
        "verification_note": "   ",   # 全空白
        "greeting": "",
    }, persona_ctx={"seed_group_keywords": ["g1"]})
    fields = {m["field"] for m in missing}
    assert "verification_note" in fields
    assert "greeting" in fields


def test_validate_empty_list_treated_as_missing():
    validate, _ = _get_validator_and_presets()
    p = _preset("friend_growth")
    missing = validate(p, provided={
        "target_groups": [],
        "verification_note": "v",
        "greeting": "g",
    }, persona_ctx={})
    fields = {m["field"] for m in missing}
    assert "target_groups" in fields


# ────────────────────────────────────────────────────────────
# 2. executor facebook_extract_members 行为测试（mock fb 模块）
# ────────────────────────────────────────────────────────────

class _FakeFB:
    """最小化 fb 模块替身：可控 extract_group_members 返回值。"""
    def __init__(self, members):
        self._members = members
        self.called_with = None

    def extract_group_members(self, **kwargs):
        self.called_with = kwargs
        return list(self._members)


def _run_extract(fb, params: dict):
    """直接调用 executor 中 facebook_extract_members 的等价分支。

    我们不实际通过 task_dispatch（涉及大量初始化），而是复刻 executor 中
    facebook_extract_members 区块的逻辑，确保契约一致。
    历史上这块代码是 21 行；如果 executor 改动需同步本测试。
    """
    # 复刻自 src/host/executor.py: facebook_extract_members 分支
    if not hasattr(fb, "extract_group_members"):
        return False, "facebook.extract_group_members 尚未实现", None
    group_name = (params.get("group_name") or "").strip()
    if not group_name:
        return False, (
            "缺少必填参数 group_name（目标群组名）。请在启动方案时填写群组列表，"
            "或在 persona 的 seed_group_keywords 中配置默认值。"
        ), {"members": [], "count": 0, "outcome": "missing_param:group_name"}
    members = fb.extract_group_members(
        group_name=group_name,
        max_members=int(params.get("max_members", 30)),
        use_llm_scoring=bool(params.get("use_llm_scoring", False)),
        target_country=params.get("target_country", ""),
        device_id="test-device",
        persona_key=params.get("persona_key") or None,
        phase=None,
    )
    count = len(members or [])
    if count == 0:
        return False, (
            f"未提取到任何成员（group={group_name!r}）。可能原因：① 进群失败 "
            f"② FB 改版导致成员卡选择器失效 ③ 群组开启隐私 / 被删除。"
        ), {"members": [], "count": 0,
            "outcome": "automation_extract_zero_after_enter",
            "group_name": group_name}
    return True, "", {"members": members, "count": count, "outcome": "ok",
                      "group_name": group_name}


def test_extract_empty_group_name_fails_fast():
    """历史 bug：group_name 为空时 executor 返回 True (虚假成功)。
    现在应立即 False + outcome=missing_param。"""
    fb = _FakeFB(members=[])
    ok, msg, result = _run_extract(fb, {"max_members": 20})
    assert ok is False
    assert "group_name" in msg
    assert result["outcome"] == "missing_param:group_name"
    # 关键：fb.extract_group_members 不应被调用（早退）
    assert fb.called_with is None


def test_extract_zero_result_treated_as_failure():
    """0 提取也应判失败 — outcome=automation_extract_zero_after_enter (P1.5 细化)
    + members=[] + count=0"""
    fb = _FakeFB(members=[])
    ok, msg, result = _run_extract(fb, {"group_name": "ママ友サークル"})
    assert ok is False
    assert "未提取到任何成员" in msg
    # P1.5 (2026-04-30): outcome 从 zero_result 细化为 automation_extract_zero_after_enter，
    # 让前端徽章走 forensics 现场查看路径（紫色 📸 徽章）而非重配置 dialog（参数没问题）
    assert result["outcome"] == "automation_extract_zero_after_enter"
    assert result["outcome"].startswith("automation_")  # 前缀契约
    assert result["count"] == 0
    assert result["group_name"] == "ママ友サークル"


def test_extract_normal_path_success():
    fb = _FakeFB(members=[
        {"name": "山田花子"}, {"name": "佐藤美咲"}, {"name": "鈴木恵子"},
    ])
    ok, msg, result = _run_extract(fb, {
        "group_name": "ママ友サークル", "max_members": 20,
        "persona_key": "jp_female_midlife", "target_country": "JP",
    })
    assert ok is True
    assert msg == ""
    assert result["count"] == 3
    assert result["outcome"] == "ok"
    assert fb.called_with["group_name"] == "ママ友サークル"
    assert fb.called_with["max_members"] == 20


def test_extract_whitespace_only_group_name_fails():
    """空白字符串也应判失败（防止前端传 ' ' 跳过校验）。"""
    fb = _FakeFB(members=[])
    ok, msg, result = _run_extract(fb, {"group_name": "   "})
    assert ok is False
    assert result["outcome"] == "missing_param:group_name"


# ────────────────────────────────────────────────────────────
# 2b. friend_growth 闭环完成语义
# ────────────────────────────────────────────────────────────

class _CampaignFakeFB:
    def extract_group_members(self, **kwargs):
        return [{"name": "山田花子", "source_section": kwargs.get("member_source", "")}]

    def add_friend_and_greet(self, *args, **kwargs):
        return {"add_friend_ok": False, "greet_ok": False,
                "greet_skipped_reason": "add_friend_failed"}


def test_campaign_outreach_goal_not_met_is_failure(monkeypatch):
    """闭环任务没有达到本次触达数量时不能再显示 completed。"""
    import src.host.executor as ex
    import src.host.fb_add_friend_gate as gate

    monkeypatch.setattr(ex.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(gate, "check_add_friend_gate",
                        lambda *_a, **_kw: (None, {}))

    ok, msg, result = ex._run_facebook_campaign(
        _CampaignFakeFB(),
        "D1",
        {
            "steps": ["extract_members", "add_friends"],
            "target_groups": ["ママ友"],
            "verification_note": "同じグループで拝見しました",
            "greeting": "よろしくお願いします",
            "max_friends_per_run": 1,
            "require_outreach_goal": True,
            "disable_ai_per_target_message": True,
        },
    )

    assert ok is False
    assert "未达成" in msg
    assert result["outcome"] == "outreach_goal_not_met"
    assert result["outreach_goal"] == 1
    assert result["friend_requests_sent"] == 0


# ────────────────────────────────────────────────────────────
# 3. P1 Sprint C: campaign_run.add_friends verification_note 校验
# ────────────────────────────────────────────────────────────

def _run_add_friends_step(params, has_targets=True):
    """复刻 executor._run_facebook_campaign 中 step=='add_friends' 的关键校验路径。

    历史 bug：require_verification_note=True 但 verification_note='' 时仍发起请求
    → FB 把"无验证语 + 短时高频"判为机器人。Sprint C 修复：硬跳过 + 标记 outcome。
    """
    targets = params.get("add_friend_targets") or (
        [{"name": "u1"}, {"name": "u2"}] if has_targets else []
    )
    if not targets:
        return {"step": "add_friends", "error": "no_targets_upstream_zero_members"}
    note = (params.get("verification_note") or "").strip()
    if not note and bool(params.get("require_verification_note", False)):
        return {
            "step": "add_friends",
            "error": "missing_verification_note",
            "meta": {
                "hint": "preset 声明 require_verification_note=True 但 verification_note 为空",
                "outcome": "missing_param:verification_note",
            },
        }
    return {"step": "add_friends", "ok": True, "sent": len(targets)}


def test_campaign_add_friends_blocks_when_note_empty_and_required():
    """Sprint C: 必填验证语为空 → 步骤跳过 + outcome=missing_param:verification_note"""
    res = _run_add_friends_step({
        "add_friend_targets": [{"name": "山田花子"}],
        "verification_note": "",
        "require_verification_note": True,
    })
    assert res["error"] == "missing_verification_note"
    assert res["meta"]["outcome"] == "missing_param:verification_note"


def test_campaign_add_friends_blocks_when_note_whitespace():
    """Sprint C: 全空白也判空（与 P0 group_name 同语义）"""
    res = _run_add_friends_step({
        "add_friend_targets": [{"name": "x"}],
        "verification_note": "   \n   ",
        "require_verification_note": True,
    })
    assert res["error"] == "missing_verification_note"


def test_campaign_add_friends_passes_when_note_filled():
    res = _run_add_friends_step({
        "add_friend_targets": [{"name": "x"}],
        "verification_note": "您好🌸",
        "require_verification_note": True,
    })
    assert res.get("ok") is True
    assert res["sent"] == 1


def test_campaign_add_friends_allows_empty_when_not_required():
    """preset 没声明 require_verification_note=True 时，空 note 是允许的
    （automation 层会按 persona 兜底生成）。"""
    res = _run_add_friends_step({
        "add_friend_targets": [{"name": "x"}],
        "verification_note": "",
        "require_verification_note": False,
    })
    assert res.get("ok") is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
