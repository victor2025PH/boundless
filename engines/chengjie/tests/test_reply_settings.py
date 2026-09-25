"""「自动回复设置」页门禁（P0，2026-08-02）。

覆盖三层：
- 纯函数核心 ``src.inbox.reply_pacing_settings``：白名单校验 / 跨字段规则 /
  嵌套 patch / 有效值快照（含 bootstrap 哨兵语义）/ 热生效分组 / delay 块合并；
- ``AutosendWorker.apply_deliver_delay`` 热更新真的改变取延迟行为；
- 路由端到端：GET 快照契约、POST 校验拒绝 / overlay 落盘 / worker 热更 /
  审计落盘 / needs_restart 诚实分组（worker 缺席时 deliver_delay 必须归 pending）。

不触网、不写仓库 config（fake config_manager 落 tmp_path）。
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.inbox import reply_pacing_settings as rps


# ── 纯函数：sanitize_patch ───────────────────────────────────────


class TestSanitizePatch:
    def test_unknown_field_rejected(self):
        clean, errors = rps.sanitize_patch({"inbox.l2_autosend.enabled": True})
        assert clean == {}
        assert errors == [{"field": "inbox.l2_autosend.enabled",
                           "code": "unknown_field"}]

    def test_master_switches_not_in_whitelist(self):
        # 危险闸门刻意不可写（capability_toggle 辖区），白名单必须不含
        for path in ("inbox.l2_autosend.enabled", "inbox.l2_autosend.deliver",
                     "inbox.auto_draft.enabled"):
            assert path not in rps.FIELDS

    def test_bool_coercion(self):
        clean, errors = rps.sanitize_patch({
            "inbox.l2_autosend.voice.enabled": "true",
            "inbox.l2_autosend.holding.enabled": 0,
        })
        assert errors == []
        assert clean["inbox.l2_autosend.voice.enabled"] is True
        assert clean["inbox.l2_autosend.holding.enabled"] is False

    def test_bad_bool(self):
        _, errors = rps.sanitize_patch({"inbox.l2_autosend.voice.enabled": "maybe"})
        assert errors[0]["code"] == "bad_bool"

    def test_enum_validation(self):
        clean, errors = rps.sanitize_patch({
            "inbox.auto_draft.automation_mode": "Review",
        })
        assert errors == [] and clean["inbox.auto_draft.automation_mode"] == "review"
        _, errors = rps.sanitize_patch({"inbox.auto_draft.automation_mode": "yolo"})
        assert errors[0]["code"] == "bad_enum"

    def test_number_range(self):
        _, errors = rps.sanitize_patch(
            {"inbox.l2_autosend.deliver_delay.min_sec": 999})
        assert errors[0]["code"] == "out_of_range"
        _, errors = rps.sanitize_patch(
            {"inbox.l2_autosend.deliver_delay.min_sec": "abc"})
        assert errors[0]["code"] == "bad_number"
        clean, errors = rps.sanitize_patch(
            {"inbox.l2_autosend.deliver_delay.min_sec": "7"})
        assert errors == [] and clean["inbox.l2_autosend.deliver_delay.min_sec"] == 7

    def test_int_field_truncates_to_int(self):
        clean, _ = rps.sanitize_patch({"inbox.auto_draft.min_text_len": 3.0})
        assert clean["inbox.auto_draft.min_text_len"] == 3
        assert isinstance(clean["inbox.auto_draft.min_text_len"], int)

    # ── P1 连发/秒回两道地板（2026-08-12） ──────────────────────

    def test_pacing_floors_in_whitelist(self):
        # 两键必须在白名单（设置页可写）、随 deliver_delay 块 worker 级热更、
        # 默认 0=关（旧行为）——三点任一漂移即红。
        for path in ("inbox.l2_autosend.deliver_delay.min_gap_sec",
                     "inbox.l2_autosend.deliver_delay.min_residual_sec"):
            spec = rps.FIELDS.get(path)
            assert spec is not None, path
            assert spec["default"] == 0
            assert spec["hot"] == "worker"

    def test_pacing_floors_accept_and_clamp(self):
        clean, errors = rps.sanitize_patch({
            "inbox.l2_autosend.deliver_delay.min_gap_sec": "12",
            "inbox.l2_autosend.deliver_delay.min_residual_sec": 2.5,
        })
        assert errors == []
        assert clean["inbox.l2_autosend.deliver_delay.min_gap_sec"] == 12
        assert clean["inbox.l2_autosend.deliver_delay.min_residual_sec"] == 2.5
        _, errors = rps.sanitize_patch(
            {"inbox.l2_autosend.deliver_delay.min_gap_sec": 999})
        assert errors[0]["code"] == "out_of_range"
        _, errors = rps.sanitize_patch(
            {"inbox.l2_autosend.deliver_delay.min_residual_sec": -1})
        assert errors[0]["code"] == "out_of_range"

    # ── 回复额度守卫（P0-guard，2026-08-12） ────────────────────

    def test_guard_keys_in_whitelist_and_defaults_synced(self):
        # 全部守卫键（P0 基础两键 + P1 高级六键）在白名单且 hot=True（parse_cfg
        # 逐调用现读 config，写 overlay 即生效）；default 必须与消费方 parse_cfg
        # 的缺省一致——FIELDS 契约是「config 缺键时快照展示的值＝真实行为」，
        # 两处漂移这里先红。sweep_legacy 刻意不进 UI（进程级一次性语义）。
        from src.inbox.peer_bot_guard import _DEFAULTS as guard_defaults
        prefix = "inbox.peer_bot_guard."
        for name in ("enabled", "daily_reply_budget", "repeat_streak_n",
                     "instant_reply_sec", "instant_repeat_n", "heuristics",
                     "suspect_threshold", "proactive_filter"):
            spec = rps.FIELDS.get(prefix + name)
            assert spec is not None, name
            assert spec["hot"] is True, name
            assert spec["default"] == guard_defaults[name], name
        assert prefix + "sweep_legacy" not in rps.FIELDS
        # 2026-08-24 老板拍板（B34）：每日额度不设业务上限——lo=0（0=不限额，
        # 与 budget_flags 同语义）、hi=1_000_000（纯技术帽防荒谬输入）。
        bu = rps.FIELDS[prefix + "daily_reply_budget"]
        assert (bu["lo"], bu["hi"]) == (0, 1_000_000)
        # repeat_streak_n 下限 2：n=1 时任何非空入站 streak≥1 → 全量降 review
        # 的必炸脚枪，UI 层必须挡住
        assert rps.FIELDS[prefix + "repeat_streak_n"]["lo"] == 2
        # suspect_threshold 允许 0（观察模式）且 2 位小数（0.05 粒度）
        thr = rps.FIELDS[prefix + "suspect_threshold"]
        assert thr["lo"] == 0 and thr["decimals"] == 2

    def test_guard_adv_clamps_and_decimals(self):
        # 阈值 0.65 必须原样保留（旧 1 位小数口径会磨成 0.6/0.7——decimals=2 的
        # 存在理由）；0=观察模式合法；repeat=1 拒绝（脚枪）；echo 窗半秒步长。
        clean, errors = rps.sanitize_patch({
            "inbox.peer_bot_guard.suspect_threshold": 0.65,
            "inbox.peer_bot_guard.instant_reply_sec": "2.5",
            "inbox.peer_bot_guard.repeat_streak_n": 4,
            "inbox.peer_bot_guard.instant_repeat_n": 3,
            "inbox.peer_bot_guard.heuristics": "off",
            "inbox.peer_bot_guard.proactive_filter": 1,
        })
        assert errors == []
        assert clean["inbox.peer_bot_guard.suspect_threshold"] == 0.65
        assert clean["inbox.peer_bot_guard.instant_reply_sec"] == 2.5
        assert clean["inbox.peer_bot_guard.repeat_streak_n"] == 4
        assert clean["inbox.peer_bot_guard.heuristics"] is False
        assert clean["inbox.peer_bot_guard.proactive_filter"] is True
        clean, errors = rps.sanitize_patch(
            {"inbox.peer_bot_guard.suspect_threshold": 0})
        assert errors == []
        assert clean["inbox.peer_bot_guard.suspect_threshold"] == 0
        _, errors = rps.sanitize_patch(
            {"inbox.peer_bot_guard.repeat_streak_n": 1})
        assert errors[0]["code"] == "out_of_range"
        _, errors = rps.sanitize_patch(
            {"inbox.peer_bot_guard.instant_reply_sec": 0})
        assert errors[0]["code"] == "out_of_range"

    def test_guard_budget_open_range(self):
        # 2026-08-24 老板拍板（B34 落定，内测实录 600 被旧 [5,500] 拒）：
        # 0=不限额从 YAML 专属语义升为 UI 一等公民（页面提示写明语义），
        # 正整数不设业务上限；仅留 1_000_000 技术帽与负数拒绝。
        clean, errors = rps.sanitize_patch(
            {"inbox.peer_bot_guard.daily_reply_budget": 0})
        assert errors == []
        assert clean["inbox.peer_bot_guard.daily_reply_budget"] == 0
        clean, errors = rps.sanitize_patch(
            {"inbox.peer_bot_guard.daily_reply_budget": 600})
        assert errors == []
        assert clean["inbox.peer_bot_guard.daily_reply_budget"] == 600
        _, errors = rps.sanitize_patch(
            {"inbox.peer_bot_guard.daily_reply_budget": -1})
        assert errors[0]["code"] == "out_of_range"
        _, errors = rps.sanitize_patch(
            {"inbox.peer_bot_guard.daily_reply_budget": 1_000_001})
        assert errors[0]["code"] == "out_of_range"
        clean, errors = rps.sanitize_patch({
            "inbox.peer_bot_guard.enabled": "false",
            "inbox.peer_bot_guard.daily_reply_budget": "60",
        })
        assert errors == []
        assert clean["inbox.peer_bot_guard.enabled"] is False
        assert clean["inbox.peer_bot_guard.daily_reply_budget"] == 60

    # ── 账号发送额度 / 防轰炸闸门（companion_send_gate，2026-08-29）────

    def test_sendgate_keys_in_whitelist_and_defaults_synced(self):
        # 老板指令（0829）：这组键收进设置页，支持不得再教用户改 YAML。
        # default 必须与消费方 gate_decision/evaluate 的代码缺省一致——
        # 按**函数签名**对拍（companion_send_gate 无 _DEFAULTS 表），漂移先红。
        import inspect

        from src.skills.companion_send_gate import gate_decision, gate_enabled
        sig = {k: p.default
               for k, p in inspect.signature(gate_decision).parameters.items()}
        prefix = "companion_send_gate."
        for name in ("target_cap", "warmup_start_cap", "warmup_ramp_days",
                     "block_on_red", "reserve_for_manual"):
            spec = rps.FIELDS.get(prefix + name)
            assert spec is not None, name
            assert spec["hot"] is True, name
            assert spec["default"] == sig[name], name
        en = rps.FIELDS[prefix + "enabled"]
        assert en["hot"] is True
        assert en["default"] is gate_enabled(None) is gate_enabled({}) is False
        ex = rps.FIELDS[prefix + "exempt_peers"]
        assert ex["type"] == "str_list" and ex["default"] == []

    def test_sendgate_cap_zero_rejected(self):
        # 闸门语义里 cap=0＝每天 0 条＝全停（与 peer_bot_guard 的 0=不限
        # **相反**）——全停走 Kill-Switch，UI 不给这个脚枪留入口。
        _, errors = rps.sanitize_patch({"companion_send_gate.target_cap": 0})
        assert errors[0]["code"] == "out_of_range"
        _, errors = rps.sanitize_patch(
            {"companion_send_gate.warmup_start_cap": 0})
        assert errors[0]["code"] == "out_of_range"
        # 正常值/技术帽：安装包默认 300、支持建议 500 都必须能存
        clean, errors = rps.sanitize_patch({
            "companion_send_gate.target_cap": 500,
            "companion_send_gate.warmup_start_cap": "100",
            "companion_send_gate.warmup_ramp_days": 3,
            "companion_send_gate.block_on_red": "off",
            "companion_send_gate.reserve_for_manual": 5,
        })
        assert errors == []
        assert clean["companion_send_gate.target_cap"] == 500
        assert clean["companion_send_gate.warmup_start_cap"] == 100
        assert clean["companion_send_gate.block_on_red"] is False

    def test_sendgate_exempt_str_list(self):
        # 白名单：去空白/去重保序、数字条目 str 化、空表=清空；
        # 非列表 / 嵌套 / 超长拒绝（bad_list / too_long）
        clean, errors = rps.sanitize_patch({
            "companion_send_gate.exempt_peers":
                [" 4498639894 ", "user@x", 4498639894, "", None, "user@x"],
        })
        assert errors == []
        assert clean["companion_send_gate.exempt_peers"] == [
            "4498639894", "user@x"]
        clean, errors = rps.sanitize_patch(
            {"companion_send_gate.exempt_peers": []})
        assert errors == [] and clean["companion_send_gate.exempt_peers"] == []
        _, errors = rps.sanitize_patch(
            {"companion_send_gate.exempt_peers": "4498639894"})
        assert errors[0]["code"] == "bad_list"
        _, errors = rps.sanitize_patch(
            {"companion_send_gate.exempt_peers": [["nested"]]})
        assert errors[0]["code"] == "bad_list"
        _, errors = rps.sanitize_patch(
            {"companion_send_gate.exempt_peers": ["x" * 65]})
        assert errors[0]["code"] == "too_long"

    def test_sendgate_ramp_cross_validate(self):
        # 起点 > 目标＝爬坡倒着走（额度随号龄下降），合并视图拦下
        errors = rps.cross_validate(
            {"companion_send_gate.warmup_start_cap": 100}, 
            {"companion_send_gate": {"target_cap": 50}})
        assert errors and errors[0]["code"] == "min_gt_max"
        assert errors[0]["field"] == "companion_send_gate.warmup_start_cap"
        # 同批提交自洽 / 只动无关键不连坐历史脏配置
        assert rps.cross_validate({
            "companion_send_gate.target_cap": 300,
            "companion_send_gate.warmup_start_cap": 100,
        }, {}) == []
        assert rps.cross_validate(
            {"companion_send_gate.enabled": True},
            {"companion_send_gate": {"warmup_start_cap": 100,
                                     "target_cap": 50}}) == []

    def test_sendgate_exempt_effective_values_stringified(self):
        # YAML 手写数字条目（号码不加引号）在快照里统一 str（前端 join 不炸）
        vals = rps.effective_values({
            "companion_send_gate": {"exempt_peers": [4498639894, "a"]}})
        assert vals["companion_send_gate.exempt_peers"] == ["4498639894", "a"]

    # ── 内容与风格全局默认（P0-style） ──────────────────────────

    def test_style_enum_accepts_empty_and_values(self):
        clean, errors = rps.sanitize_patch({
            "ai.reply_defaults.length": "",
            "ai.reply_defaults.emoji_level": "Minimal",
        })
        assert errors == []
        assert clean["ai.reply_defaults.length"] == ""
        assert clean["ai.reply_defaults.emoji_level"] == "minimal"
        _, errors = rps.sanitize_patch({"ai.reply_defaults.length": "epic"})
        assert errors[0]["code"] == "bad_enum"

    def test_style_max_sentences_range(self):
        clean, errors = rps.sanitize_patch(
            {"ai.reply_defaults.max_sentences": "5"})
        assert errors == [] and clean["ai.reply_defaults.max_sentences"] == 5
        _, errors = rps.sanitize_patch(
            {"ai.reply_defaults.max_sentences": 11})
        assert errors[0]["code"] == "out_of_range"

    def test_tone_hint_text_collapses_whitespace(self):
        clean, errors = rps.sanitize_patch(
            {"ai.reply_defaults.tone_hint": "  多用短句\n\t别书面腔  "})
        assert errors == []
        assert clean["ai.reply_defaults.tone_hint"] == "多用短句 别书面腔"

    def test_tone_hint_too_long_rejected(self):
        _, errors = rps.sanitize_patch(
            {"ai.reply_defaults.tone_hint": "长" * 200})
        assert errors[0]["code"] == "too_long"

    def test_tone_hint_empty_clears(self):
        clean, errors = rps.sanitize_patch(
            {"ai.reply_defaults.tone_hint": ""})
        assert errors == [] and clean["ai.reply_defaults.tone_hint"] == ""


# ── 纯函数：cross_validate ───────────────────────────────────────


class TestCrossValidate:
    def test_min_gt_max_rejected(self):
        errs = rps.cross_validate(
            {"inbox.l2_autosend.deliver_delay.min_sec": 30,
             "inbox.l2_autosend.deliver_delay.max_sec": 10}, {})
        assert errs and errs[0]["code"] == "min_gt_max"

    def test_merges_with_existing_config(self):
        # 只提交 min=30，现值 max=10 → 应拦（合并后不自洽）
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {"max_sec": 10}}}}
        errs = rps.cross_validate(
            {"inbox.l2_autosend.deliver_delay.min_sec": 30}, cfg)
        assert errs and errs[0]["code"] == "min_gt_max"

    def test_ok_when_consistent(self):
        assert rps.cross_validate(
            {"inbox.l2_autosend.deliver_delay.min_sec": 3,
             "inbox.l2_autosend.deliver_delay.max_sec": 9}, {}) == []

    def test_untouched_delay_skips(self):
        assert rps.cross_validate(
            {"inbox.l2_autosend.voice.enabled": True}, {}) == []


# ── 纯函数：nested_patch / effective_values / split / merge ──────


class TestHelpers:
    def test_nested_patch(self):
        out = rps.nested_patch({
            "inbox.l2_autosend.deliver_delay.min_sec": 3,
            "inbox.l2_autosend.voice.enabled": True,
        })
        assert out == {"inbox": {"l2_autosend": {
            "deliver_delay": {"min_sec": 3}, "voice": {"enabled": True}}}}

    def test_effective_values_defaults(self):
        vals = rps.effective_values({})
        # bootstrap 哨兵：缺键 + 档位缺省 auto_ai → True（automation_mode.py 真实语义）
        assert vals["inbox.auto_draft.bootstrap_automation_mode"] is True
        assert vals["inbox.auto_draft.automation_mode"] == "auto_ai"
        assert vals["inbox.l2_autosend.mark_read_before_reply"] is True

    def test_bootstrap_follows_mode_when_missing(self):
        cfg = {"inbox": {"auto_draft": {"automation_mode": "review"}}}
        vals = rps.effective_values(cfg)
        assert vals["inbox.auto_draft.bootstrap_automation_mode"] is False

    def test_bootstrap_explicit_wins(self):
        cfg = {"inbox": {"auto_draft": {"automation_mode": "review",
                                        "bootstrap_automation_mode": True}}}
        assert rps.effective_values(cfg)[
            "inbox.auto_draft.bootstrap_automation_mode"] is True

    def test_style_defaults_and_meta(self):
        vals = rps.effective_values({})
        assert vals["ai.reply_defaults.length"] == ""
        assert vals["ai.reply_defaults.max_sentences"] == 0
        assert vals["ai.reply_defaults.emoji_level"] == ""
        assert vals["ai.reply_defaults.tone_hint"] == ""
        meta = rps.field_meta()
        assert meta["ai.reply_defaults.length"]["hot"] is True
        assert meta["ai.reply_defaults.tone_hint"]["maxlen"] == rps.TONE_HINT_MAXLEN
        # 全 hot=True：split 应归 live
        live, pending = rps.split_hot_pending(
            {"ai.reply_defaults.length": "concise",
             "ai.reply_defaults.tone_hint": "x"})
        assert pending == [] and len(live) == 2

    def test_split_hot_pending_worker_applied(self):
        clean = {"inbox.l2_autosend.deliver_delay.min_sec": 3,
                 "inbox.l2_autosend.voice.enabled": True,
                 "inbox.l2_autosend.typing_indicator": False}
        live, pending = rps.split_hot_pending(
            clean, worker_applied={"inbox.l2_autosend.deliver_delay.min_sec"})
        assert "inbox.l2_autosend.deliver_delay.min_sec" in live
        assert "inbox.l2_autosend.voice.enabled" in live
        # typing 属另一热更入口，本次未成功 → 诚实归 pending
        assert pending == ["inbox.l2_autosend.typing_indicator"]

    def test_split_hot_pending_worker_missing(self):
        # worker 热更失败/缺席 → deliver_delay 必须诚实归「重启后生效」
        live, pending = rps.split_hot_pending(
            {"inbox.l2_autosend.deliver_delay.min_sec": 3},
            worker_applied=set())
        assert live == [] and pending == [
            "inbox.l2_autosend.deliver_delay.min_sec"]

    def test_split_hot_pending_per_path_independent(self):
        # 两类热更入口独立成败：flags 成功、delay 失败 → 各归各
        clean = {"inbox.l2_autosend.deliver_delay.min_sec": 3,
                 "inbox.l2_autosend.mark_read_before_reply": False}
        live, pending = rps.split_hot_pending(
            clean, worker_applied={"inbox.l2_autosend.mark_read_before_reply"})
        assert live == ["inbox.l2_autosend.mark_read_before_reply"]
        assert pending == ["inbox.l2_autosend.deliver_delay.min_sec"]

    def test_merged_delay_block_preserves_unmanaged_keys(self):
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {
            "min_sec": 1, "max_sec": 5,
            "persona_overrides": {"a": {"max_sec": 9}}}}}}
        block = rps.merged_delay_block(
            cfg, {"inbox.l2_autosend.deliver_delay.max_sec": 12})
        assert block["max_sec"] == 12
        assert block["min_sec"] == 1
        assert block["persona_overrides"] == {"a": {"max_sec": 9}}


# ── 场景预设档 + 节奏溯源（P1） ─────────────────────────────────


class TestPresets:
    def test_preset_keys_subset_of_whitelist(self):
        # 预设是「组合写入」不是新键——键集越界＝绕过白名单校验
        for pid, kv in rps.PRESETS.items():
            for k in kv:
                assert k in rps.FIELDS, f"{pid}:{k} 不在白名单"

    def test_preset_values_pass_own_sanitize(self):
        for pid, kv in rps.PRESETS.items():
            clean, errors = rps.sanitize_patch(dict(kv))
            assert errors == [], f"{pid} 预设值必须能过自身校验: {errors}"
            assert len(clean) == len(kv)

    def test_presets_never_touch_gates_or_personal_keys(self):
        # 危险闸门（能力看板辖区）与运营个性化项（tone/emoji/句数/语音总开关/缓冲）
        # 都不许进预设——预设换挡不清洗个性化
        personal = {
            "ai.reply_defaults.tone_hint", "ai.reply_defaults.emoji_level",
            "ai.reply_defaults.max_sentences",
            "inbox.l2_autosend.voice.enabled",
            "inbox.l2_autosend.holding.enabled",
        }
        for pid, kv in rps.PRESETS.items():
            for k in kv:
                assert "enabled" not in k and not k.endswith(".deliver"), \
                    f"{pid}:{k} 疑似闸门键"
                assert k not in personal, f"{pid}:{k} 是个性化项"

    def test_match_preset_exact_and_numeric_tolerance(self):
        vals = rps.effective_values({})
        vals.update(rps.PRESETS["natural"])
        assert rps.match_preset(vals) == "natural"
        # 8 == 8.0（sanitize 归一后可能是 float）
        vals["inbox.l2_autosend.deliver_delay.min_sec"] = 3.0
        assert rps.match_preset(vals) == "natural"

    def test_match_preset_none_on_any_diff(self):
        vals = rps.effective_values({})
        vals.update(rps.PRESETS["natural"])
        vals["inbox.l2_autosend.deliver_delay.max_sec"] = 13
        assert rps.match_preset(vals) == ""

    def test_defaults_match_no_preset(self):
        assert rps.match_preset(rps.effective_values({})) == ""


# ── 消息分条「条间节奏」（P0-bub，2026-08-14）────────────────────────────


class TestBubblePacing:
    _P = "inbox.reply_style.bubbles."

    def test_bubble_defaults_synced_with_parse_cfg(self):
        """FIELDS 缺省必须与消费方 parse_bubbles_cfg 真实缺省逐键一致。

        （max_parts 哨兵 None 除外——真实缺省是动态的，由 effective_values
        二次求值，单独有测试钉。）零依赖约束下本地常量与 reply_split 的
        出厂值也在此互钉。
        """
        from src.inbox import reply_split as rsp
        from src.inbox.reply_split import parse_bubbles_cfg
        for mirror, truth in (
            ("BUBBLE_GAP_LO_DEFAULT", "DEFAULT_GAP_SEC_LO"),
            ("BUBBLE_GAP_HI_DEFAULT", "DEFAULT_GAP_SEC_HI"),
            ("BUBBLE_CJK_PER_CHAR_DEFAULT", "DEFAULT_PER_CHAR_SEC"),
            ("BUBBLE_LATIN_PER_CHAR_DEFAULT", "DEFAULT_LATIN_PER_CHAR_SEC"),
            ("BUBBLE_MAX_GAP_DEFAULT", "DEFAULT_MAX_GAP_SEC"),
            ("BUBBLE_TOTAL_BUDGET_DEFAULT", "DEFAULT_TOTAL_BUDGET_SEC"),
            ("BUBBLE_MIN_TOTAL_CHARS_DEFAULT", "DEFAULT_MIN_TOTAL_CHARS"),
        ):
            assert getattr(rps, mirror) == pytest.approx(
                getattr(rsp, truth)), mirror
        assert (rps.BUBBLE_EXPLICIT_NEWLINE_ONLY_DEFAULT
                is rsp.DEFAULT_EXPLICIT_NEWLINE_ONLY is True)
        real = parse_bubbles_cfg({})
        for short in ("enabled", "per_sentence", "gap_sec_lo", "gap_sec_hi",
                      "per_char_sec", "latin_per_char_sec", "max_gap_sec",
                      "total_budget_sec", "explicit_newline_only",
                      "min_total_chars"):
            spec = rps.FIELDS[self._P + short]
            assert spec["default"] == pytest.approx(real[short]), short
            assert spec["hot"] is True, short

    def test_bubble_1075_factory_defaults(self):
        """#210 / D-L3：出厂关 + 仅显式换行才拆 + 短回复门 80；平台覆写表**不许**
        携带条数键（同一人设跨平台形态一致，平台层只调延迟）。"""
        vals = rps.effective_values({})
        assert vals[self._P + "enabled"] is False
        assert vals[self._P + "per_sentence"] is False
        assert vals[self._P + "explicit_newline_only"] is True
        assert vals[self._P + "min_total_chars"] == 80
        assert "max_parts" not in rps.OVERRIDE_EDITABLE_KEYS
        _, errs = rps.sanitize_patch({
            "inbox.l2_autosend.deliver_delay.platform_overrides": {
                "telegram": {"max_parts": 2}}})
        assert errs and errs[0]["code"] == "bad_override_key"
        # 白名单能接受运营显式回算法档 / 调门槛
        clean, errs2 = rps.sanitize_patch({
            self._P + "explicit_newline_only": False,
            self._P + "min_total_chars": 120})
        assert errs2 == []
        assert clean[self._P + "explicit_newline_only"] is False
        assert clean[self._P + "min_total_chars"] == 120

    def test_bubble_max_parts_sentinel(self):
        # 缺键：打包模式缺省 3；逐句模式缺省 5；显式值优先
        assert rps.effective_values({})[self._P + "max_parts"] == 3
        cfg_ps = {"inbox": {"reply_style": {"bubbles": {"per_sentence": True}}}}
        assert rps.effective_values(cfg_ps)[self._P + "max_parts"] == 5
        cfg_ex = {"inbox": {"reply_style": {"bubbles": {
            "per_sentence": True, "max_parts": 4}}}}
        assert rps.effective_values(cfg_ex)[self._P + "max_parts"] == 4

    def test_bubble_sanitize_decimals_and_range(self):
        clean, errors = rps.sanitize_patch({
            self._P + "latin_per_char_sec": 0.075,
            self._P + "per_char_sec": 0.03,
            self._P + "gap_sec_lo": 1.5,
        })
        assert errors == []
        # decimals=3：0.075 不被 1 位小数磨成 0.1
        assert clean[self._P + "latin_per_char_sec"] == pytest.approx(0.075)
        _, err2 = rps.sanitize_patch({self._P + "max_gap_sec": 0.5})
        assert err2 and err2[0]["code"] == "out_of_range"

    def test_bubble_gap_cross_validate_lo_gt_hi(self):
        # 只提交 lo、与现值 hi 冲突：合并视图必须拦（运行时静默抬 hi=失真）
        cfg = {"inbox": {"reply_style": {"bubbles": {"gap_sec_hi": 2.0}}}}
        errors = rps.cross_validate({self._P + "gap_sec_lo": 5.0}, cfg)
        assert errors == [{"field": self._P + "gap_sec_lo",
                           "code": "min_gt_max"}]
        # 同时提交自洽的 lo/hi：放行
        assert rps.cross_validate({
            self._P + "gap_sec_lo": 2.0, self._P + "gap_sec_hi": 5.0}, cfg) == []
        # 未触碰分条键：历史脏配置不拦无关保存
        assert rps.cross_validate(
            {"inbox.auto_draft.min_text_len": 5},
            {"inbox": {"reply_style": {"bubbles": {
                "gap_sec_lo": 9, "gap_sec_hi": 1}}}}) == []

    def test_bubble_keys_all_hot_split(self):
        clean = {self._P + "gap_sec_lo": 1.0, self._P + "enabled": True}
        live, pending = rps.split_hot_pending(clean)
        assert pending == [] and len(live) == 2


class TestExplainPacing:
    def test_all_default_when_empty(self):
        out = rps.explain_pacing({}, "")
        assert out["min_sec"] == {"value": 0, "source": "default"}
        assert out["max_sec"] == {"value": 0, "source": "default"}
        assert out["adaptive"] == {"value": False, "source": "default"}

    def test_global_layer(self):
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {
            "min_sec": 3, "max_sec": 9}}}}
        out = rps.explain_pacing(cfg, "")
        assert out["min_sec"] == {"value": 3, "source": "global"}
        assert out["max_sec"] == {"value": 9, "source": "global"}
        assert out["adaptive"]["source"] == "default"

    def test_persona_override_is_key_level(self):
        # 覆写只给 max → min 仍标 global（键级来源，不整块连坐）
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {
            "min_sec": 3, "max_sec": 9,
            "persona_overrides": {"xiaoya": {"max_sec": 20}}}}}}
        out = rps.explain_pacing(cfg, "xiaoya")
        assert out["min_sec"] == {"value": 3, "source": "global"}
        assert out["max_sec"] == {"value": 20, "source": "persona"}
        assert out["persona_id"] == "xiaoya"

    def test_unknown_persona_falls_back_to_global(self):
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {
            "min_sec": 3, "max_sec": 9,
            "persona_overrides": {"other": {"max_sec": 20}}}}}}
        out = rps.explain_pacing(cfg, "nobody")
        assert out["max_sec"] == {"value": 9, "source": "global"}


class TestChainPacingCoverage:
    """三条发送链的节奏生效自检（2026-08-07：修「滑杆只管部分链、其余秒回且隐形」）。"""

    _SLIDER = {"inbox": {"l2_autosend": {
        "deliver_delay": {"min_sec": 8, "max_sec": 20}}}}

    def test_protocol_active_and_unpaced_flags_warning(self):
        # ChatX 默认档标本：deliver=false + 协议链开 + 滑杆有值但协议自读键未配。
        # 修复后协议链跟随滑杆 → paced=True，不再是隐形秒回。
        cfg = {"protocol_autoreply": {"enabled": True},
               **self._SLIDER}
        cov = rps.chain_pacing_coverage(cfg)
        assert cov["chains"]["protocol"]["active"] is True
        assert cov["chains"]["protocol"]["paced"] is True
        assert cov["chains"]["protocol"]["source"] == \
            "inbox.l2_autosend.deliver_delay"
        assert cov["ok"] is True

    def test_protocol_default_when_nothing_configured(self):
        # 存量节点收口：protocol 开、无 slider 无 own → 兜底出厂默认（8-20s），
        # 不再秒回；banner 与 runtime 同函数，来源标为出厂默认。
        cfg = {"protocol_autoreply": {"enabled": True}}
        cov = rps.chain_pacing_coverage(cfg)
        assert cov["chains"]["protocol"]["active"] is True
        assert cov["chains"]["protocol"]["paced"] is True          # 兜底＝非秒回
        assert cov["chains"]["protocol"]["source"] == "__protocol_default__"
        assert "protocol" not in cov["unpaced_active"]
        assert cov["ok"] is True

    def test_protocol_instant_flagged_only_on_explicit_follow_false(self):
        # 唯一的真秒回＝用户显式 follow:false（明示要即时）→ 自检如实点名（bot-like 警示）。
        cfg = {"protocol_autoreply": {"enabled": True,
                                      "delay": {"follow": False}}}
        cov = rps.chain_pacing_coverage(cfg)
        assert cov["chains"]["protocol"]["active"] is True
        assert cov["chains"]["protocol"]["paced"] is False
        assert "protocol" in cov["unpaced_active"]
        assert cov["ok"] is False

    def test_protocol_inactive_when_deliver_on(self):
        # deliver=true → auto_ai 让位 B 线、其余档人审静音 → 协议链不自动外发。
        cfg = {"protocol_autoreply": {"enabled": True},
               "inbox": {"l2_autosend": {
                   "deliver": True, "enabled": True,
                   "deliver_delay": {"min_sec": 8, "max_sec": 20}}}}
        cov = rps.chain_pacing_coverage(cfg)
        assert cov["chains"]["protocol"]["active"] is False
        assert cov["chains"]["autosend"]["active"] is True
        assert cov["chains"]["autosend"]["paced"] is True
        # 未启用的链不进 unpaced（不逼用户配一条根本不发的链）
        assert cov["ok"] is True

    def test_native_follows_slider_but_own_thinking_delay_wins(self):
        cfg = {"telegram": {"api_id": "12345", "reply_humanize": {
            "thinking_delay": {"min_sec": 2, "max_sec": 9}}},
               **self._SLIDER}
        cov = rps.chain_pacing_coverage(cfg)
        assert cov["chains"]["native_tg"]["active"] is True
        assert cov["chains"]["native_tg"]["source"] == \
            "telegram.reply_humanize.thinking_delay"
        assert cov["chains"]["native_tg"]["max_sec"] == 9

    def test_native_inactive_without_api_id(self):
        # 没有主号凭据 → 原生链不会启动 → 不算需要配节奏的 active 链。
        cov = rps.chain_pacing_coverage(self._SLIDER)
        assert cov["chains"]["native_tg"]["active"] is False
        assert cov["ok"] is True

    # ── diverged 黄档 / followable（2026-08-12）────────────────────
    # diverged＝有节奏但吃独立键（拖滑杆不生效——198「改了没变化」实录的可见化）；
    # followable＝可一键收敛到滑杆的链。语义钉住：看**绑定关系**不看数值。

    def test_native_own_delay_is_diverged_and_followable(self):
        cfg = {"telegram": {"api_id": "12345", "reply_humanize": {
            "thinking_delay": {"min_sec": 2, "max_sec": 9}}},
               **self._SLIDER}
        cov = rps.chain_pacing_coverage(cfg)
        nat = cov["chains"]["native_tg"]
        assert nat["diverged"] is True
        assert nat["followable"] is True
        assert cov["diverged_active"] == ["native_tg"]
        assert cov["ok"] is True  # 黄档不算红：有节奏，只是不随滑杆

    def test_native_same_values_as_slider_still_diverged(self):
        # 值恰好等于滑杆也算 diverged：绑定关系已分叉，下次拖滑杆就不同步。
        cfg = {"telegram": {"api_id": "12345", "reply_humanize": {
            "thinking_delay": {"min_sec": 8, "max_sec": 20}}},
               **self._SLIDER}
        cov = rps.chain_pacing_coverage(cfg)
        assert cov["chains"]["native_tg"]["diverged"] is True

    def test_native_following_slider_not_diverged(self):
        cfg = {"telegram": {"api_id": "12345"}, **self._SLIDER}
        cov = rps.chain_pacing_coverage(cfg)
        nat = cov["chains"]["native_tg"]
        assert nat["diverged"] is False
        assert nat["followable"] is False
        assert cov["diverged_active"] == []

    def test_protocol_default_not_diverged_or_followable(self):
        # 出厂默认兜底＝「都没配」，没有可分叉的对象 → 不标黄不出按钮
        # （标黄会逼用户去动一个本来无害的状态）。
        cfg = {"protocol_autoreply": {"enabled": True}}
        cov = rps.chain_pacing_coverage(cfg)
        proto = cov["chains"]["protocol"]
        assert proto["diverged"] is False
        assert proto["followable"] is False

    def test_protocol_explicit_instant_followable_but_not_diverged(self):
        # 红档 follow:false 秒回：不 paced 故不属黄档，但必须给「改为跟随滑杆」
        # 出路（此前红警告没有任何页面内动作可走）。
        cfg = {"protocol_autoreply": {"enabled": True,
                                      "delay": {"follow": False}},
               **self._SLIDER}
        cov = rps.chain_pacing_coverage(cfg)
        proto = cov["chains"]["protocol"]
        assert proto["paced"] is False
        assert proto["diverged"] is False
        assert proto["followable"] is True

    def test_inactive_chain_never_followable(self):
        # 未启用的链不出收敛按钮（对一条根本不发的链给动作＝噪音）。
        cfg = {"telegram": {"reply_humanize": {
            "thinking_delay": {"min_sec": 2, "max_sec": 9}}}}
        cov = rps.chain_pacing_coverage(cfg)
        assert cov["chains"]["native_tg"]["active"] is False
        assert cov["chains"]["native_tg"]["followable"] is False
        assert cov["chains"]["native_tg"]["diverged"] is False


# ── 纯函数：人设节奏覆写（P1） ───────────────────────────────────

_PO = "inbox.l2_autosend.deliver_delay.persona_overrides"


class TestPersonaOverrides:
    def test_sanitize_accepts_editable_keys(self):
        clean, errors = rps.sanitize_patch({_PO: {
            "xiaoya": {"min_sec": "2", "max_sec": 8.5, "adaptive": "true"}}})
        assert errors == []
        assert clean[_PO]["xiaoya"] == {
            "min_sec": 2, "max_sec": 8.5, "adaptive": True}

    def test_sanitize_rejects_unknown_override_key(self):
        # per_char_sec 等手调键不许经 UI 口提交（防任意键注入）
        _, errors = rps.sanitize_patch({_PO: {
            "p": {"per_char_sec": 0.1}}})
        assert errors and errors[0]["code"] == "bad_override_key"

    def test_sanitize_rejects_bad_persona_id(self):
        _, errors = rps.sanitize_patch({_PO: {"": {"min_sec": 1}}})
        assert errors[0]["code"] == "bad_persona_id"
        _, errors = rps.sanitize_patch({_PO: {"x" * 65: {"min_sec": 1}}})
        assert errors[0]["code"] == "bad_persona_id"

    def test_sanitize_out_of_range_in_override(self):
        _, errors = rps.sanitize_patch({_PO: {"p": {"min_sec": 999}}})
        assert errors[0]["code"] == "out_of_range"

    def test_empty_entry_means_delete(self):
        # UI 清空全部格子＝该人设视同删除（不落空 dict 噪音）
        clean, errors = rps.sanitize_patch({_PO: {"p": {}}})
        assert errors == [] and clean[_PO] == {}

    def test_empty_table_clears_all(self):
        clean, errors = rps.sanitize_patch({_PO: {}})
        assert errors == [] and clean[_PO] == {}

    def test_non_mapping_rejected(self):
        _, errors = rps.sanitize_patch({_PO: [1, 2]})
        assert errors[0]["code"] == "bad_overrides"

    def test_merged_preserves_manual_keys_and_deletes(self):
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {
            "persona_overrides": {
                "keep": {"min_sec": 1, "per_char_sec": 0.2},
                "gone": {"max_sec": 9},
            }}}}}
        out = rps.merged_persona_overrides(
            cfg, {"keep": {"min_sec": 3}, "new": {"max_sec": 6}})
        # UI 三键以提交为准；per_char_sec 手调键按人设保留
        assert out["keep"] == {"min_sec": 3, "per_char_sec": 0.2}
        # 提交表缺席＝删除
        assert "gone" not in out
        assert out["new"] == {"max_sec": 6}

    def test_merged_clears_editable_key_not_resubmitted(self):
        # 运营在 UI 清掉 max_sec 那格 → 现存 max_sec 真被清掉
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {
            "persona_overrides": {"p": {"min_sec": 1, "max_sec": 9}}}}}}
        out = rps.merged_persona_overrides(cfg, {"p": {"min_sec": 2}})
        assert out["p"] == {"min_sec": 2}

    def test_cross_validate_override_min_gt_max(self):
        errs = rps.cross_validate({_PO: {"p": {"min_sec": 30, "max_sec": 5}}}, {})
        assert errs and errs[0]["code"] == "min_gt_max"
        assert errs[0]["field"].endswith(".p")

    def test_cross_validate_override_vs_top_level(self):
        # 覆写只给 min=30，顶层 max=10 → 也要拦
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {"max_sec": 10}}}}
        errs = rps.cross_validate({_PO: {"p": {"min_sec": 30}}}, cfg)
        assert errs and errs[0]["code"] == "min_gt_max"

    def test_cross_validate_override_ok(self):
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {
            "min_sec": 2, "max_sec": 10}}}}
        assert rps.cross_validate({_PO: {"p": {"min_sec": 5}}}, cfg) == []

    def test_effective_values_copies_overrides(self):
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {
            "persona_overrides": {"p": {"min_sec": 1}}}}}}
        vals = rps.effective_values(cfg)
        vals[_PO]["p"]["min_sec"] = 999  # 改快照不得污染 config 活引用
        assert cfg["inbox"]["l2_autosend"]["deliver_delay"][
            "persona_overrides"]["p"]["min_sec"] == 1

    def test_field_meta_exposes_editable_keys(self):
        meta = rps.field_meta()[_PO]
        assert meta["type"] == "delay_overrides"
        assert meta["hot"] == "worker"
        assert meta["editable_keys"] == ["min_sec", "max_sec", "adaptive"]

    def test_replace_paths_contract(self):
        # 各覆写表都是整树替换语义（删掉的人设/平台/账号班表条目必须真被删掉）
        assert set(rps.REPLACE_PATHS) == {
            _PO,
            "inbox.l2_autosend.deliver_delay.platform_overrides",
            "inbox.l2_autosend.platform_humanize",
            "inbox.auto_draft.platform_modes",
            "inbox.l2_autosend.voice.platform_triggers",
            "inbox.work_schedule.accounts",
        }


# ── worker 热更新 ────────────────────────────────────────────────


class TestWorkerHotApply:
    def test_apply_deliver_delay_changes_pick(self):
        from src.inbox.autosend_worker import AutosendWorker
        w = AutosendWorker(draft_service=object(), config={
            "deliver_delay": {"min_sec": 0, "max_sec": 0}})
        assert w._pick_deliver_delay() == 0.0
        w.apply_deliver_delay({"min_sec": 5, "max_sec": 5})
        assert w._pick_deliver_delay() == pytest.approx(5.0)
        w.apply_deliver_delay(None)  # 清空 → 回缺省零延迟
        assert w._pick_deliver_delay() == 0.0

    def test_apply_humanize_flags_and_snapshot(self):
        from src.inbox.autosend_worker import AutosendWorker
        w = AutosendWorker(
            draft_service=object(),
            config={"deliver_delay": {"min_sec": 1, "max_sec": 3},
                    "mark_read_before_reply": True, "typing_indicator": True},
            mark_read_callback=lambda *a: None,
            typing_callback=lambda *a: None)
        snap = w.runtime_pacing_snapshot()
        assert snap["mark_read_enabled"] is True
        assert snap["typing_enabled"] is True
        assert snap["mark_read_capable"] is True
        assert snap["deliver_delay"]["max_sec"] == 3
        # 热关已读、保持 typing（None=不动）
        w.apply_humanize_flags(mark_read=False)
        snap = w.runtime_pacing_snapshot()
        assert snap["mark_read_enabled"] is False
        assert snap["typing_enabled"] is True
        # 热开回来
        w.apply_humanize_flags(mark_read=True, typing=False)
        snap = w.runtime_pacing_snapshot()
        assert snap["mark_read_enabled"] is True
        assert snap["typing_enabled"] is False

    def test_humanize_flags_without_callbacks(self):
        # 回调本体缺席（deliver=false 装配）→ enabled 恒 False（开关开也没用），
        # capable=False 让 UI 能区分「关了」vs「没接」
        from src.inbox.autosend_worker import AutosendWorker
        w = AutosendWorker(draft_service=object(), config={
            "mark_read_before_reply": True, "typing_indicator": True})
        snap = w.runtime_pacing_snapshot()
        assert snap["mark_read_enabled"] is False
        assert snap["mark_read_capable"] is False
        assert snap["typing_enabled"] is False

    def test_config_flags_seed_runtime(self):
        # 构造期 config 关掉的开关，运行时初值应为关（builders always= 旁路后
        # 闸门移进 worker，语义必须与旧行为一致）
        from src.inbox.autosend_worker import AutosendWorker
        w = AutosendWorker(
            draft_service=object(),
            config={"mark_read_before_reply": False, "typing_indicator": False},
            mark_read_callback=lambda *a: None,
            typing_callback=lambda *a: None)
        snap = w.runtime_pacing_snapshot()
        assert snap["mark_read_enabled"] is False
        assert snap["typing_enabled"] is False
        assert snap["mark_read_capable"] is True

    def test_platform_humanize_flag_resolution(self):
        # 平台显式覆写 > 全局开关；平台键大小写归一；全局关也可单平台开
        from src.inbox.autosend_worker import AutosendWorker
        w = AutosendWorker(
            draft_service=object(),
            config={"mark_read_before_reply": True, "typing_indicator": False,
                    "platform_humanize": {
                        "Messenger": {"mark_read": False},
                        "line": {"typing": True}}},
            mark_read_callback=lambda *a: None,
            typing_callback=lambda *a: None)
        assert w._humanize_flag("messenger", "mark_read") is False
        assert w._humanize_flag("telegram", "mark_read") is True
        assert w._humanize_flag("line", "typing") is True     # 全局关、平台开
        assert w._humanize_flag("whatsapp", "typing") is False
        snap = w.runtime_pacing_snapshot()
        assert snap["platform_humanize"]["messenger"] == {"mark_read": False}

    def test_apply_platform_humanize_hot(self):
        from src.inbox.autosend_worker import AutosendWorker
        w = AutosendWorker(draft_service=object(), config={
            "mark_read_before_reply": True})
        assert w._humanize_flag("messenger", "mark_read") is True
        w.apply_platform_humanize({"messenger": {"mark_read": False}})
        assert w._humanize_flag("messenger", "mark_read") is False
        w.apply_platform_humanize(None)   # 清空=全部回跟随全局
        assert w._humanize_flag("messenger", "mark_read") is True

    def test_pick_deliver_delay_platform_override(self):
        from src.inbox.autosend_worker import AutosendWorker
        w = AutosendWorker(draft_service=object(), config={
            "deliver_delay": {
                "min_sec": 0, "max_sec": 0,
                "platform_overrides": {
                    "messenger": {"min_sec": 7, "max_sec": 7}}}})
        assert w._pick_deliver_delay() == 0.0
        assert w._pick_deliver_delay(
            platform="messenger") == pytest.approx(7.0)


# ── 路由端到端 ───────────────────────────────────────────────────


class _FakeConfigManager:
    def __init__(self, tmp_path, config=None, save_ok=True):
        self.config = config or {}
        self.config_path = str(tmp_path / "config.yaml")
        self.save_ok = save_ok
        self.saved_patches = []
        self.saved_replace_paths = []

    def save_overlay_patch(self, patch, *, replace_paths=()):
        if not self.save_ok:
            return False
        self.saved_patches.append(patch)
        self.saved_replace_paths.append(tuple(replace_paths))
        # 模拟热重载 deep-merge：让后续快照读到新值
        def _merge(dst, src):
            for k, v in src.items():
                if isinstance(v, dict) and isinstance(dst.get(k), dict):
                    _merge(dst[k], v)
                else:
                    dst[k] = v
        _merge(self.config, patch)
        # 整树替换路径：删掉的键真被删掉（与生产 replace 语义对齐）
        for rp in replace_paths:
            keys = rp.split(".")
            src, dst = patch, self.config
            for k in keys[:-1]:
                if not isinstance(src, dict) or k not in src:
                    src = None
                    break
                src = src[k]
                dst = dst.setdefault(k, {})
            if isinstance(src, dict) and keys[-1] in src:
                dst[keys[-1]] = src[keys[-1]]
        return True


class _FakeWorker:
    def __init__(self, *, with_flags=False, with_runtime=False):
        self.applied = []
        self.flag_calls = []
        if with_flags:
            self.apply_humanize_flags = self._apply_humanize_flags
        if with_runtime:
            self.runtime_pacing_snapshot = self._runtime_pacing_snapshot

    def apply_deliver_delay(self, block):
        self.applied.append(block)

    def _apply_humanize_flags(self, **kw):
        self.flag_calls.append(kw)

    def _runtime_pacing_snapshot(self):
        return {"deliver_delay": (self.applied[-1] if self.applied else {}),
                "mark_read_enabled": True, "typing_enabled": True,
                "mark_read_capable": True, "typing_capable": True}


def _make_client(tmp_path, *, config=None, worker=None, save_ok=True):
    from fastapi import Request

    from src.web.routes.reply_settings_routes import register_reply_settings_routes
    app = FastAPI()
    cm = _FakeConfigManager(tmp_path, config=config, save_ok=save_ok)

    async def _noop(request: Request):
        return None

    register_reply_settings_routes(
        app, page_auth=_noop, api_auth=_noop,
        templates=None, config_manager=cm)
    if worker is not None:
        app.state.autosend_worker = worker
    return TestClient(app), cm


class TestRoutes:
    def test_get_snapshot_contract(self, tmp_path):
        cfg = {"inbox": {
            "auto_draft": {"automation_mode": "auto_ai"},
            "l2_autosend": {"enabled": True, "deliver": True,
                            "deliver_delay": {"min_sec": 2, "max_sec": 8}},
        }}
        client, _ = _make_client(tmp_path, config=cfg)
        d = client.get("/api/reply-settings").json()
        assert d["ok"] is True
        assert d["values"]["inbox.l2_autosend.deliver_delay.min_sec"] == 2
        assert d["gates"]["worker_enabled"] is True
        assert d["gates"]["deliver"] is True
        meta = d["meta"]["inbox.l2_autosend.deliver_delay.min_sec"]
        assert meta["hot"] == "worker" and meta["lo"] == 0

    def test_get_snapshot_p1_fields(self, tmp_path):
        client, _ = _make_client(tmp_path)
        d = client.get("/api/reply-settings").json()
        assert set(d["presets"]) == {"cautious", "natural", "rapid"}
        # 出货默认（delay 0/0）不匹配任何预设 → 自定义
        assert d["preset_match"] == ""
        # 实测长度：dict（进程内有采样）或 None（读取失败），键必须在
        assert "observed_reply_len" in d

    def test_get_snapshot_preset_match_hits(self, tmp_path):
        cfg = {
            "inbox": {"l2_autosend": {
                # O-1 D（D-O4）：三预设各带 profile，命中判定含档位键（缺 profile = custom → 不匹配）
                "deliver_delay": {"profile": "natural", "min_sec": 3, "max_sec": 12,
                                  "adaptive": True},
                "mark_read_before_reply": True, "typing_indicator": True,
                "voice": {"trigger": "when_peer_voice"},
            },
                # P0-bub 起预设含条间节奏三键，命中判定随之收紧
                # （2026-08-14 换挡：natural=5-10s 时代缺省 3/8/12）
                "reply_style": {"bubbles": {
                    "gap_sec_lo": 3.0, "gap_sec_hi": 8.0, "max_gap_sec": 12,
                }}},
            "ai": {"reply_defaults": {"length": "moderate"}},
        }
        client, _ = _make_client(tmp_path, config=cfg)
        d = client.get("/api/reply-settings").json()
        assert d["preset_match"] == "natural"

    def test_explain_route_requires_ctx(self, tmp_path):
        client, _ = _make_client(tmp_path)
        d = client.get("/api/reply-settings/explain").json()
        assert d["ok"] is False
        assert d["errors"][0]["code"] == "empty"
        assert d["errors"][0]["message"]

    def test_explain_route_contract_minimal_app(self, tmp_path):
        # 最小装配（无 inbox store / 无人设库）：各分区独立 best-effort，
        # 档位回落全局默认、人设空、节奏/风格仍给出带来源的结构。
        cfg = {
            "inbox": {"auto_draft": {"automation_mode": "review"},
                      "l2_autosend": {"deliver_delay": {"min_sec": 3, "max_sec": 9}}},
            "ai": {"reply_defaults": {"length": "concise", "tone_hint": "短句"}},
        }
        client, _ = _make_client(tmp_path, config=cfg)
        d = client.get(
            "/api/reply-settings/explain",
            # chat_key 取生僻值：防其他测试往 PersonaManager 单例塞的会话级
            # 绑定恰好撞键（单例跨测试共享）
            params={"platform": "telegram", "chat_key": "rps-explain-e2e-987654"},
        ).json()
        assert d["ok"] is True
        assert d["conversation_id"]
        assert d["mode"]["value"] == "review"
        assert d["mode"]["source"] == "global"
        assert isinstance(d["persona"], dict)
        assert d["pacing"]["min_sec"]["source"] in ("global", "default")
        # 风格溯源与格式化器同源：无人设绑定 → 全局 concise 生效
        if not d["persona"].get("id"):
            assert d["style"]["length"]["value"] == "concise"
            assert d["style"]["length"]["source"] == "global"
            assert d["style"]["tone_hint"]["source"] == "global"

    def test_post_rejects_bad_payload(self, tmp_path):
        client, cm = _make_client(tmp_path)
        d = client.post("/api/reply-settings", json={}).json()
        assert d["ok"] is False and d["errors"][0]["code"] == "empty"
        d = client.post("/api/reply-settings", json={
            "changes": {"evil.key": 1}}).json()
        assert d["ok"] is False
        assert d["errors"][0]["code"] == "unknown_field"
        assert d["errors"][0]["message"]  # tr() 出了文案
        assert cm.saved_patches == []  # 校验失败绝不落盘

    def test_post_min_gt_max_rejected(self, tmp_path):
        client, cm = _make_client(tmp_path)
        d = client.post("/api/reply-settings", json={"changes": {
            "inbox.l2_autosend.deliver_delay.min_sec": 30,
            "inbox.l2_autosend.deliver_delay.max_sec": 5}}).json()
        assert d["ok"] is False
        assert any(e["code"] == "min_gt_max" for e in d["errors"])
        assert cm.saved_patches == []

    def test_post_saves_and_hot_applies_worker(self, tmp_path):
        worker = _FakeWorker()
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {
            "min_sec": 1, "max_sec": 5, "persona_overrides": {"p": {}}}}}}
        client, cm = _make_client(tmp_path, config=cfg, worker=worker)
        d = client.post("/api/reply-settings", json={"changes": {
            "inbox.l2_autosend.deliver_delay.max_sec": 12,
            "inbox.l2_autosend.voice.enabled": True}}).json()
        assert d["ok"] is True
        # overlay 落盘（嵌套结构）
        assert cm.saved_patches[0]["inbox"]["l2_autosend"][
            "deliver_delay"]["max_sec"] == 12
        # worker 拿到完整合并块（persona_overrides 保留）
        assert worker.applied[0]["max_sec"] == 12
        assert worker.applied[0]["persona_overrides"] == {"p": {}}
        # 热生效分组：两项都 live，无需重启
        assert set(d["applied_live"]) == {
            "inbox.l2_autosend.deliver_delay.max_sec",
            "inbox.l2_autosend.voice.enabled"}
        assert d["needs_restart"] == []
        # 快照回读到新值
        assert d["values"]["inbox.l2_autosend.deliver_delay.max_sec"] == 12

    def test_post_without_worker_delay_pending(self, tmp_path):
        client, _ = _make_client(tmp_path)  # 无 app.state.autosend_worker
        d = client.post("/api/reply-settings", json={"changes": {
            "inbox.l2_autosend.deliver_delay.max_sec": 12}}).json()
        assert d["ok"] is True
        assert d["applied_live"] == []
        assert d["needs_restart"] == ["inbox.l2_autosend.deliver_delay.max_sec"]

    def test_post_restart_only_field(self, tmp_path):
        client, _ = _make_client(tmp_path)
        d = client.post("/api/reply-settings", json={"changes": {
            "inbox.l2_autosend.typing_indicator": False}}).json()
        assert d["ok"] is True
        assert d["needs_restart"] == ["inbox.l2_autosend.typing_indicator"]

    def test_post_save_failed(self, tmp_path):
        client, _ = _make_client(tmp_path, save_ok=False)
        d = client.post("/api/reply-settings", json={"changes": {
            "inbox.l2_autosend.voice.enabled": True}}).json()
        assert d["ok"] is False
        assert d["errors"][0]["code"] == "save_failed"

    def test_post_overrides_end_to_end(self, tmp_path):
        # 覆写整表提交：replace 落盘 + 手调键保留 + 删除生效 + worker 拿完整块
        worker = _FakeWorker()
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {
            "min_sec": 1, "max_sec": 8,
            "persona_overrides": {
                "keep": {"min_sec": 2, "per_char_sec": 0.15},
                "gone": {"max_sec": 5},
            }}}}}
        client, cm = _make_client(tmp_path, config=cfg, worker=worker)
        d = client.post("/api/reply-settings", json={"changes": {
            _PO: {"keep": {"min_sec": 3}, "new": {"max_sec": 6}}}}).json()
        assert d["ok"] is True
        # replace_paths 传到了 save_overlay_patch（否则删除赖着不走）
        assert cm.saved_replace_paths[0] == rps.REPLACE_PATHS
        saved = cm.saved_patches[0]["inbox"]["l2_autosend"][
            "deliver_delay"]["persona_overrides"]
        assert saved["keep"] == {"min_sec": 3, "per_char_sec": 0.15}
        assert "gone" not in saved and saved["new"] == {"max_sec": 6}
        # worker 热更块里的覆写表＝最终整表
        assert worker.applied[0]["persona_overrides"] == saved
        assert d["applied_live"] == [_PO] and d["needs_restart"] == []
        # 快照回读：删除的真没了
        assert "gone" not in d["values"][_PO]

    def test_post_override_min_gt_max_rejected(self, tmp_path):
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {"max_sec": 10}}}}
        client, cm = _make_client(tmp_path, config=cfg)
        d = client.post("/api/reply-settings", json={"changes": {
            _PO: {"p": {"min_sec": 30}}}}).json()
        assert d["ok"] is False
        assert any(e["code"] == "min_gt_max" for e in d["errors"])
        assert cm.saved_patches == []

    def test_post_humanize_hot_applies(self, tmp_path):
        worker = _FakeWorker(with_flags=True)
        client, _ = _make_client(tmp_path, worker=worker)
        d = client.post("/api/reply-settings", json={"changes": {
            "inbox.l2_autosend.mark_read_before_reply": False}}).json()
        assert d["ok"] is True
        assert worker.flag_calls == [{"mark_read": False}]
        assert d["applied_live"] == [
            "inbox.l2_autosend.mark_read_before_reply"]
        assert d["needs_restart"] == []

    def test_post_humanize_pending_when_worker_lacks_api(self, tmp_path):
        # 旧 worker（无 apply_humanize_flags）→ 诚实归重启后生效
        worker = _FakeWorker()
        client, _ = _make_client(tmp_path, worker=worker)
        d = client.post("/api/reply-settings", json={"changes": {
            "inbox.l2_autosend.typing_indicator": False}}).json()
        assert d["ok"] is True
        assert d["needs_restart"] == ["inbox.l2_autosend.typing_indicator"]

    def test_runtime_readback_in_get_and_save(self, tmp_path):
        worker = _FakeWorker(with_runtime=True)
        client, _ = _make_client(tmp_path, worker=worker)
        d = client.get("/api/reply-settings").json()
        assert d["runtime"]["mark_read_enabled"] is True
        d = client.post("/api/reply-settings", json={"changes": {
            "inbox.l2_autosend.deliver_delay.max_sec": 7}}).json()
        assert d["runtime"]["deliver_delay"]["max_sec"] == 7

    def test_runtime_none_without_worker(self, tmp_path):
        client, _ = _make_client(tmp_path)
        assert client.get("/api/reply-settings").json()["runtime"] is None

    def test_audit_written(self, tmp_path):
        client, _ = _make_client(tmp_path)
        client.post("/api/reply-settings", json={
            "actor": "boss",
            "changes": {"inbox.l2_autosend.voice.enabled": True}})
        audit = tmp_path / "reply_settings_audit.jsonl"
        assert audit.exists()
        rec = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
        assert rec["actor"] == "boss"
        entry = rec["changes"]["inbox.l2_autosend.voice.enabled"]
        assert entry["old"] is False and entry["new"] is True


# ── 实测观测段（P2）─────────────────────────────────────────────


class TestObserved:
    @pytest.fixture(autouse=True)
    def _reset_metrics(self):
        from src.integrations import humanize_metrics as hm
        hm.reset()
        yield
        hm.reset()

    def test_get_includes_observed_pacing_and_humanize(self, tmp_path):
        from types import SimpleNamespace

        from src.integrations import humanize_metrics as hm
        hm.record_pacing("autosend/pid1", SimpleNamespace(
            enabled=True, delay=4.0, target=4.5, elapsed=0.5, adaptive=True))
        hm.record_pacing("autosend/pid1", SimpleNamespace(
            enabled=True, delay=6.0, target=6.0, elapsed=0.0, adaptive=False))
        hm.record_read("telegram", True)
        hm.record_typing("telegram", False)

        client, _ = _make_client(tmp_path)  # 无 worker 也要有观测（模块级采集）
        d = client.get("/api/reply-settings").json()
        obs = d["observed"]
        row = obs["pacing"]["autosend/pid1"]
        assert row["count"] == 2
        assert row["avg_delay"] == pytest.approx(5.0)
        assert row["adaptive_count"] == 1
        assert row["max_delay"] == pytest.approx(6.0)
        assert obs["humanize"]["telegram"]["read_ok"] == 1
        assert obs["humanize"]["telegram"]["typing_fail"] == 1

    def test_disabled_pacing_not_sampled(self, tmp_path):
        # enabled=False（未启用延迟配置）不进分布——零配置实例不该出现幻影采样
        from types import SimpleNamespace

        from src.integrations import humanize_metrics as hm
        hm.record_pacing("autosend/pid1", SimpleNamespace(
            enabled=False, delay=0.0, target=0.0, elapsed=0.0, adaptive=False))
        client, _ = _make_client(tmp_path)
        d = client.get("/api/reply-settings").json()
        assert d["observed"]["pacing"] == {}

    def test_post_response_includes_observed(self, tmp_path):
        client, _ = _make_client(tmp_path)
        d = client.post("/api/reply-settings", json={"changes": {
            "inbox.l2_autosend.voice.enabled": True}}).json()
        assert d["ok"] is True
        assert "observed" in d and "pacing" in d["observed"]


# ── 平台专家覆写（P1，2026-08-03）────────────────────────────────

_PLAT_OV = "inbox.l2_autosend.deliver_delay.platform_overrides"
_PH = "inbox.l2_autosend.platform_humanize"
_PM = "inbox.auto_draft.platform_modes"
_PVT = "inbox.l2_autosend.voice.platform_triggers"


class TestPlatformExpert:
    """平台专家覆写：白名单/校验/合并/能力护栏/溯源/键域一致性。"""

    def test_platforms_match_worker_registry(self):
        # PLATFORMS 刻意本地定义（纯函数模块零依赖）。键域必须等于
        # 「WORKERS 平台 ∩ 登录目录 SUPPORTED_PLATFORMS」：进了登录目录却没进
        # 节奏表 → 红。节奏表里出现登录目录没有的名字，由
        # test_reply_pacing_platforms_are_supported_subset 拒绝。
        # douyin / tiktok 已在能力矩阵，尚未进登录目录，因此也不能进 PLATFORMS
        # 或 ACTION_PLATFORMS。这组差集钉死，防静默漏登记。
        from src.integrations.platform_capabilities import WORKERS
        from src.integrations.platform_login import SUPPORTED_PLATFORMS
        worker_plats = {p for p, _m, _mod, _c in WORKERS}
        supported = set(SUPPORTED_PLATFORMS)
        assert set(rps.PLATFORMS) == worker_plats & supported
        assert worker_plats - supported == {"douyin", "tiktok"}

    def test_sanitize_platform_modes(self):
        clean, errors = rps.sanitize_patch({_PM: {"Messenger": "Review"}})
        assert errors == [] and clean[_PM] == {"messenger": "review"}
        _, errors = rps.sanitize_patch({_PM: {"weibo": "review"}})
        assert errors[0]["code"] == "bad_platform"
        _, errors = rps.sanitize_patch({_PM: {"line": "yolo"}})
        assert errors[0]["code"] == "bad_enum"
        # 空值=删除该平台封顶；空表=全部清除
        clean, errors = rps.sanitize_patch({_PM: {"line": ""}})
        assert errors == [] and clean[_PM] == {}
        clean, errors = rps.sanitize_patch({_PM: {}})
        assert errors == [] and clean[_PM] == {}

    def test_sanitize_platform_delay_overrides(self):
        clean, errors = rps.sanitize_patch({_PLAT_OV: {
            "messenger": {"min_sec": "10", "max_sec": 30, "adaptive": 1}}})
        assert errors == []
        assert clean[_PLAT_OV]["messenger"] == {
            "min_sec": 10, "max_sec": 30, "adaptive": True}
        _, errors = rps.sanitize_patch({_PLAT_OV: {"snapchat": {"min_sec": 1}}})
        assert errors[0]["code"] == "bad_platform"
        _, errors = rps.sanitize_patch(
            {_PLAT_OV: {"line": {"per_char_sec": 0.1}}})
        assert errors[0]["code"] == "bad_override_key"

    def test_sanitize_platform_flags(self):
        clean, errors = rps.sanitize_patch({_PH: {
            "whatsapp": {"mark_read": "false", "typing": 1}}})
        assert errors == []
        assert clean[_PH]["whatsapp"] == {"mark_read": False, "typing": True}
        _, errors = rps.sanitize_patch({_PH: {"line": {"selfie": True}}})
        assert errors[0]["code"] == "bad_override_key"
        _, errors = rps.sanitize_patch({_PH: {"vk": {"typing": True}}})
        assert errors[0]["code"] == "bad_platform"
        clean, errors = rps.sanitize_patch({_PH: {"line": {}}})
        assert errors == [] and clean[_PH] == {}   # 条目清空=删除

    def test_voice_platform_triggers_accept_wechat_desktop_bridge_only(self):
        # 2026-09-19 P1：微信电脑副驾只在语音触发表放行；档位封顶/拟人表仍拒（对副驾无意义）
        clean, errors = rps.sanitize_patch({_PVT: {"wechat": "never", "Telegram": "smart"}})
        assert errors == [] and clean[_PVT] == {"wechat": "never", "telegram": "smart"}
        _, errors = rps.sanitize_patch({_PM: {"wechat": "review"}})
        assert errors and errors[0]["code"] == "bad_platform"
        _, errors = rps.sanitize_patch({_PH: {"wechat": {"typing": True}}})
        assert errors and errors[0]["code"] == "bad_platform"
        meta = rps.field_meta()
        assert "wechat" in meta[_PVT]["keys"] and "wechat" not in meta[_PM]["keys"]
        assert "wechat" not in rps.PLATFORMS and rps.VOICE_TRIGGER_PLATFORMS[-1] == "wechat"
        # 消费端：wechat 的覆写真的会改 trigger
        from src.inbox.voice_autosend import effective_voice_block
        vb = effective_voice_block({"trigger": "always", "platform_triggers": {"wechat": "never"}}, "wechat")
        assert vb["trigger"] == "never" and "platform_triggers" not in vb

    def test_sanitize_voice_platform_triggers(self):
        clean, errors = rps.sanitize_patch({_PVT: {"messenger": "Never"}})
        assert errors == [] and clean[_PVT] == {"messenger": "never"}
        _, errors = rps.sanitize_patch({_PVT: {"messenger": "loud"}})
        assert errors[0]["code"] == "bad_enum"

    def test_cross_validate_platform_vs_top(self):
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {"max_sec": 10}}}}
        errs = rps.cross_validate({_PLAT_OV: {"line": {"min_sec": 30}}}, cfg)
        assert errs and errs[0]["code"] == "min_gt_max"
        assert errs[0]["field"].endswith(".line")

    def test_cross_validate_persona_x_platform_conflict(self):
        # 人设 min=30 落在平台 max=10 上 → 运行时 resolve_pacing 按
        # 「min>max=禁用」**静默秒回**——必须在保存时提前拦下
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {
            "min_sec": 1, "max_sec": 60,
            "platform_overrides": {"messenger": {"max_sec": 10}}}}}}
        errs = rps.cross_validate({_PO: {"slowpoke": {"min_sec": 30}}}, cfg)
        assert errs and errs[0]["code"] == "min_gt_max"
        assert "slowpoke@messenger" in errs[0]["field"]

    def test_cross_validate_full_matrix_ok(self):
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {
            "min_sec": 2, "max_sec": 60,
            "platform_overrides": {
                "messenger": {"min_sec": 10, "max_sec": 30}}}}}}
        assert rps.cross_validate({_PO: {"p": {"min_sec": 20}}}, cfg) == []

    def test_cross_validate_untouched_delay_skips_matrix(self):
        # 非节奏键不触发全矩阵校验——历史脏配置不拦无关项的保存
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {
            "min_sec": 30, "max_sec": 5}}}}
        assert rps.cross_validate(
            {"inbox.l2_autosend.voice.enabled": True}, cfg) == []

    def test_merged_scoped_preserves_manual_keys(self):
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {
            "platform_overrides": {
                "line": {"min_sec": 1, "per_char_sec": 0.2},
                "messenger": {"max_sec": 9}}}}}}
        out = rps.merged_scoped_overrides(
            cfg, _PLAT_OV, {"line": {"min_sec": 3}})
        assert out["line"] == {"min_sec": 3, "per_char_sec": 0.2}
        assert "messenger" not in out   # 提交表缺席=删除

    def test_caps_guard_semantics(self):
        caps = {"line": {"typing": {"state": "unsupported", "hard": True},
                         "mark_read": {"state": "ok", "hard": False}}}
        errs = rps.validate_platform_flags_caps(
            {"line": {"typing": True}}, caps)
        assert errs and errs[0]["code"] == "cap_unsupported"
        assert errs[0]["field"].endswith("line.typing")
        # 关掉不支持能力 / 开支持能力 / caps 探针缺席 → 全放行（fail-open）
        assert rps.validate_platform_flags_caps(
            {"line": {"typing": False}}, caps) == []
        assert rps.validate_platform_flags_caps(
            {"line": {"mark_read": True}}, caps) == []
        assert rps.validate_platform_flags_caps(
            {"line": {"typing": True}}, None) == []
        # partial / unknown 不拦（部分模式支持、判不了都不该锁死）
        caps2 = {"telegram": {"typing": {"state": "partial", "hard": False}}}
        assert rps.validate_platform_flags_caps(
            {"telegram": {"typing": True}}, caps2) == []

    def test_explain_pacing_platform_layer(self):
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {
            "min_sec": 3, "max_sec": 9,
            "platform_overrides": {"messenger": {"max_sec": 30}},
            "persona_overrides": {"x": {"min_sec": 5}}}}}}
        out = rps.explain_pacing(cfg, "x", platform="messenger")
        assert out["min_sec"] == {"value": 5, "source": "persona"}
        assert out["max_sec"] == {"value": 30, "source": "platform"}
        assert out["adaptive"]["source"] == "default"
        assert out["platform"] == "messenger"
        # 不带平台 = 旧两层行为（max 落回 global）
        out2 = rps.explain_pacing(cfg, "x")
        assert out2["max_sec"] == {"value": 9, "source": "global"}

    def test_effective_values_and_meta(self):
        vals = rps.effective_values({})
        assert vals[_PM] == {} and vals[_PLAT_OV] == {}
        assert vals[_PH] == {} and vals[_PVT] == {}
        meta = rps.field_meta()
        assert meta[_PM]["hot"] is True
        assert meta[_PM]["choices"] == list(rps.AUTOMATION_MODES)
        assert meta[_PM]["keys"] == list(rps.PLATFORMS)
        assert meta[_PLAT_OV]["hot"] == "worker"
        assert meta[_PLAT_OV]["keys"] == list(rps.PLATFORMS)
        assert meta[_PH]["editable_keys"] == ["mark_read", "typing"]
        assert meta[_PVT]["hot"] is True

    def test_presets_never_touch_platform_layers(self):
        scoped = {_PM, _PLAT_OV, _PH, _PVT}
        for pid, kv in rps.PRESETS.items():
            assert not (scoped & set(kv)), f"{pid} 预设不许写平台层"

    def test_effective_voice_block(self):
        from src.inbox.voice_autosend import effective_voice_block
        vb = {"enabled": True, "trigger": "always",
              "platform_triggers": {"messenger": "never", "line": "bogus"}}
        out = effective_voice_block(vb, "messenger")
        assert out["trigger"] == "never"
        assert "platform_triggers" not in out
        # 非法覆写值忽略 / 未覆写平台 / 无平台 → 全局 trigger
        assert effective_voice_block(vb, "line")["trigger"] == "always"
        assert effective_voice_block(vb, "telegram")["trigger"] == "always"
        assert effective_voice_block(vb, "")["trigger"] == "always"
        assert vb["platform_triggers"]   # 原块不被就地修改

    def test_post_platform_bundle_end_to_end(self, tmp_path, monkeypatch):
        # 能力护栏打桩成确定值（真矩阵因机器而异，e2e 必须确定性）
        import src.integrations.platform_capabilities as pc
        monkeypatch.setattr(
            pc, "humanize_caps_by_platform",
            lambda config=None, **kw: {
                "line": {"typing": {"state": "unsupported", "hard": True},
                         "mark_read": {"state": "ok", "hard": False}}})
        worker = _FakeWorker(with_flags=True)
        applied_ph = []
        worker.apply_platform_humanize = (
            lambda block: applied_ph.append(block))
        cfg = {"inbox": {"l2_autosend": {"deliver_delay": {
            "min_sec": 1, "max_sec": 60,
            "platform_overrides": {
                "line": {"min_sec": 2, "per_char_sec": 0.3}}}}}}
        client, cm = _make_client(tmp_path, config=cfg, worker=worker)
        d = client.post("/api/reply-settings", json={"changes": {
            _PM: {"messenger": "review"},
            _PLAT_OV: {"line": {"min_sec": 5},
                       "messenger": {"min_sec": 10, "max_sec": 30}},
            _PH: {"whatsapp": {"typing": False}},
            _PVT: {"messenger": "never"},
        }}).json()
        assert d["ok"] is True, d
        saved = cm.saved_patches[0]
        assert saved["inbox"]["auto_draft"]["platform_modes"] == {
            "messenger": "review"}
        pov = saved["inbox"]["l2_autosend"]["deliver_delay"][
            "platform_overrides"]
        assert pov["line"] == {"min_sec": 5, "per_char_sec": 0.3}  # 手调键保留
        assert pov["messenger"] == {"min_sec": 10, "max_sec": 30}
        # worker 热更：delay 整块带平台表；platform_humanize 走独立入口
        assert worker.applied[0]["platform_overrides"] == pov
        assert applied_ph == [{"whatsapp": {"typing": False}}]
        # 四项全部即时生效（modes/voice 活读 hot=True；delay/flags 热更成功）
        assert d["needs_restart"] == []
        assert set(d["applied_live"]) == {_PM, _PLAT_OV, _PH, _PVT}
        assert d["values"][_PM] == {"messenger": "review"}

    def test_post_platform_typing_unsupported_rejected(
            self, tmp_path, monkeypatch):
        import src.integrations.platform_capabilities as pc
        monkeypatch.setattr(
            pc, "humanize_caps_by_platform",
            lambda config=None, **kw: {
                "line": {"typing": {"state": "unsupported", "hard": True}}})
        client, cm = _make_client(tmp_path)
        d = client.post("/api/reply-settings", json={"changes": {
            _PH: {"line": {"typing": True}}}}).json()
        assert d["ok"] is False
        assert d["errors"][0]["code"] == "cap_unsupported"
        assert d["errors"][0]["message"]
        assert cm.saved_patches == []   # 护栏拦下绝不落盘

    def test_post_platform_flags_pending_when_worker_lacks_api(self, tmp_path):
        # 旧 worker（无 apply_platform_humanize）→ 诚实归重启后生效
        worker = _FakeWorker()
        client, _ = _make_client(tmp_path, worker=worker)
        d = client.post("/api/reply-settings", json={"changes": {
            _PH: {"whatsapp": {"mark_read": False}}}}).json()
        assert d["ok"] is True
        assert d["needs_restart"] == [_PH]


# ── 工作时间班表（P0-ws，2026-08-04）────────────────────────────

_WS_EN = "inbox.work_schedule.enabled"
_WS_TZ = "inbox.work_schedule.timezone"
_WS_WD = "inbox.work_schedule.default.workdays"
_WS_ST = "inbox.work_schedule.default.start"
_WS_ED = "inbox.work_schedule.default.end"
_WS_AC = "inbox.work_schedule.accounts"


class TestWorkSchedule:
    """工作时间班表键：白名单校验 / 窗口成对规则 / 快照拷贝 / e2e 落盘。"""

    def test_sanitize_basics(self):
        clean, errors = rps.sanitize_patch({
            _WS_EN: "true",
            _WS_ST: "9:5",           # 归一 HH:MM
            _WS_ED: "23:00",
            _WS_WD: [7, 1, 1, 3],    # 去重升序
            _WS_TZ: "Asia/Shanghai",
            "inbox.work_schedule.edge_jitter_min": "15",
            "inbox.work_schedule.crisis_bypass": False,
        })
        assert errors == []
        assert clean[_WS_EN] is True
        assert clean[_WS_ST] == "09:05"
        assert clean[_WS_WD] == [1, 3, 7]
        assert clean[_WS_TZ] == "Asia/Shanghai"
        assert clean["inbox.work_schedule.edge_jitter_min"] == 15

    def test_sanitize_rejects_bad_values(self):
        _, errors = rps.sanitize_patch({_WS_ST: "25:00"})
        assert errors[0]["code"] == "bad_time"
        _, errors = rps.sanitize_patch({_WS_ST: "morning"})
        assert errors[0]["code"] == "bad_time"
        _, errors = rps.sanitize_patch({_WS_WD: [0, 8]})
        assert errors[0]["code"] == "bad_workdays"
        _, errors = rps.sanitize_patch({_WS_WD: "mon"})
        assert errors[0]["code"] == "bad_workdays"
        _, errors = rps.sanitize_patch({_WS_TZ: "Mars/Olympus"})
        assert errors[0]["code"] == "bad_timezone"
        _, errors = rps.sanitize_patch(
            {"inbox.work_schedule.edge_jitter_min": 999})
        assert errors[0]["code"] == "out_of_range"

    def test_sanitize_empty_clears(self):
        # 默认班表的 start/end/timezone 空串合法（=未配置/跟随本地钟）
        clean, errors = rps.sanitize_patch({_WS_ST: "", _WS_ED: "", _WS_TZ: ""})
        assert errors == []
        assert clean[_WS_ST] == "" and clean[_WS_TZ] == ""

    def test_window_pair_rule(self):
        # 只填一半 → work_hours_gate 会 fail-open 静默不生效，保存时必须拦
        errs = rps.cross_validate({_WS_ST: "09:00", _WS_ED: ""}, {})
        assert errs and errs[0]["code"] == "incomplete_window"
        assert errs[0]["field"] == _WS_ED
        # 另一半在现存 config 里 → 合并视图自洽，放行
        cfg = {"inbox": {"work_schedule": {"default": {"end": "23:00"}}}}
        assert rps.cross_validate({_WS_ST: "09:00"}, cfg) == []
        # 同空 / 同有都合法
        assert rps.cross_validate({_WS_ST: "", _WS_ED: ""}, {}) == []
        assert rps.cross_validate({_WS_ST: "09:00", _WS_ED: "23:00"}, {}) == []
        # 未触碰班表键不校验（历史脏配置不拦无关保存）
        cfg2 = {"inbox": {"work_schedule": {"default": {"start": "09:00"}}}}
        assert rps.cross_validate(
            {"inbox.l2_autosend.voice.enabled": True}, cfg2) == []

    def test_sanitize_accounts_table(self):
        clean, errors = rps.sanitize_patch({_WS_AC: {
            "Telegram:acct1": {"start": "10:00", "end": "2:00",
                               "workdays": [6, 5], "timezone": "UTC"},
            "line:vip": {"enabled": "false"},
        }})
        assert errors == []
        assert clean[_WS_AC]["telegram:acct1"] == {
            "start": "10:00", "end": "02:00", "workdays": [5, 6],
            "timezone": "UTC"}
        assert clean[_WS_AC]["line:vip"] == {"enabled": False}

    def test_sanitize_accounts_rejects(self):
        _, errors = rps.sanitize_patch({_WS_AC: {"weibo:a": {"start": "09:00"}}})
        assert errors[0]["code"] == "bad_account_key"
        _, errors = rps.sanitize_patch({_WS_AC: {"telegram": {"start": "09:00"}}})
        assert errors[0]["code"] == "bad_account_key"
        _, errors = rps.sanitize_patch({_WS_AC: {"telegram:": {"start": "09:00"}}})
        assert errors[0]["code"] == "bad_account_key"
        _, errors = rps.sanitize_patch(
            {_WS_AC: {"telegram:a": {"midnight": True}}})
        assert errors[0]["code"] == "bad_override_key"
        # 覆写里 start 空串不合法（想跟随默认就别提交该键）
        _, errors = rps.sanitize_patch({_WS_AC: {"telegram:a": {"start": ""}}})
        assert errors[0]["code"] == "bad_time"
        _, errors = rps.sanitize_patch({_WS_AC: [1]})
        assert errors[0]["code"] == "bad_overrides"

    def test_accounts_empty_entry_means_delete(self):
        clean, errors = rps.sanitize_patch({_WS_AC: {"telegram:a": {}}})
        assert errors == [] and clean[_WS_AC] == {}
        clean, errors = rps.sanitize_patch({_WS_AC: {}})
        assert errors == [] and clean[_WS_AC] == {}
        # timezone 空串=跟随全局 → 不落键；条目因此为空=删除
        clean, errors = rps.sanitize_patch(
            {_WS_AC: {"telegram:a": {"timezone": ""}}})
        assert errors == [] and clean[_WS_AC] == {}

    def test_effective_values_defaults_and_copy(self):
        vals = rps.effective_values({})
        assert vals[_WS_EN] is False
        assert vals[_WS_ST] == "" and vals[_WS_ED] == ""
        assert vals[_WS_WD] == []
        assert vals["inbox.work_schedule.edge_jitter_min"] == 20
        assert vals["inbox.work_schedule.crisis_bypass"] is True
        assert vals["inbox.work_schedule.off_hours.generate_drafts"] is True
        assert vals[_WS_AC] == {}
        # 快照深拷贝：改快照不得污染 config 活引用（含内层 workdays 列表）
        cfg = {"inbox": {"work_schedule": {"accounts": {
            "telegram:a": {"start": "10:00", "workdays": [1, 2]}}}}}
        vals = rps.effective_values(cfg)
        vals[_WS_AC]["telegram:a"]["workdays"].append(9)
        vals[_WS_AC]["telegram:a"]["start"] = "11:00"
        src = cfg["inbox"]["work_schedule"]["accounts"]["telegram:a"]
        assert src["workdays"] == [1, 2] and src["start"] == "10:00"

    def test_field_meta_and_hot(self):
        meta = rps.field_meta()
        assert meta[_WS_EN]["hot"] is True
        assert meta[_WS_AC]["type"] == "schedule_overrides"
        assert meta[_WS_AC]["editable_keys"] == [
            "enabled", "workdays", "start", "end", "timezone"]
        # 全部班表键消费点活读 config → 必须全 hot=True（谎报会误导「需重启」）
        for path, spec in rps.FIELDS.items():
            if path.startswith("inbox.work_schedule."):
                assert spec["hot"] is True, path

    def test_presets_never_touch_schedule(self):
        for pid, kv in rps.PRESETS.items():
            assert not any(k.startswith("inbox.work_schedule.") for k in kv), \
                f"{pid} 预设不许写班表（作息是账号属性，不随场景档切换）"

    def test_post_schedule_end_to_end(self, tmp_path):
        client, cm = _make_client(tmp_path)
        # D-Q1（Q-4 #267）：开启班表必须带时区（红线③：时区类键不得默认服务器本机）
        d = client.post("/api/reply-settings", json={"changes": {
            _WS_EN: True, _WS_TZ: "America/New_York",
            _WS_ST: "09:00", _WS_ED: "23:00",
            _WS_WD: [1, 2, 3, 4, 5, 6, 7],
            _WS_AC: {"telegram:night": {"start": "22:00", "end": "06:00"}},
        }}).json()
        assert d["ok"] is True, d
        saved = cm.saved_patches[0]["inbox"]["work_schedule"]
        assert saved["enabled"] is True
        assert saved["timezone"] == "America/New_York"
        assert saved["default"]["start"] == "09:00"
        assert saved["accounts"]["telegram:night"]["end"] == "06:00"
        # 账号表走整树替换（删掉的账号真被删掉）
        assert _WS_AC in cm.saved_replace_paths[0]
        # 全 hot=True：即时生效，无需重启
        assert d["needs_restart"] == []
        assert set(d["applied_live"]) == {_WS_EN, _WS_TZ, _WS_ST, _WS_ED, _WS_WD, _WS_AC}
        assert d["values"][_WS_EN] is True

    def test_post_enable_without_timezone_rejected(self, tmp_path):
        """D-Q1（Q-4 #267）：班表开启 + 时区空 → tz_required，且什么都不落盘。"""
        client, cm = _make_client(tmp_path)
        d = client.post("/api/reply-settings", json={"changes": {
            _WS_EN: True, _WS_ST: "09:00", _WS_ED: "23:00"}}).json()
        assert d["ok"] is False
        assert d["errors"][0]["code"] == "tz_required"
        assert d["errors"][0]["field"] == _WS_TZ
        assert "{field}" not in d["errors"][0]["message"]
        assert cm.saved_patches == []

    def test_timezone_required_rule(self):
        """cross_validate 合并视图：enabled 真 ∧ tz 空 → 拦；只在触碰 enabled/tz 时校验。"""
        # 本次开启、现值无时区 → 拦
        errs = rps.cross_validate({_WS_EN: True}, {})
        assert errs == [{"field": _WS_TZ, "code": "tz_required"}]
        # 现值已开、本次把时区清空 → 拦（不能把已配好的时区退回「服务器本地」）
        cfg_on = {"inbox": {"work_schedule": {"enabled": True, "timezone": "Asia/Shanghai"}}}
        errs = rps.cross_validate({_WS_TZ: ""}, cfg_on)
        assert errs and errs[0]["code"] == "tz_required"
        # 现值已有时区、本次只开开关 → 放行
        cfg_tz = {"inbox": {"work_schedule": {"timezone": "Asia/Shanghai"}}}
        assert rps.cross_validate({_WS_EN: True}, cfg_tz) == []
        # 同批带时区 → 放行；关闭班表时时区可空
        assert rps.cross_validate({_WS_EN: True, _WS_TZ: "Europe/London"}, {}) == []
        assert rps.cross_validate({_WS_EN: False, _WS_TZ: ""}, {}) == []
        # 存量「已开 + 空时区」的脏配置：不触碰班表键时不拦无关保存
        cfg_dirty = {"inbox": {"work_schedule": {"enabled": True, "timezone": ""}}}
        assert rps.cross_validate({"inbox.auto_draft.min_text_len": 3}, cfg_dirty) == []

    def test_post_incomplete_window_rejected(self, tmp_path):
        client, cm = _make_client(tmp_path)
        d = client.post("/api/reply-settings", json={"changes": {
            _WS_ST: "09:00"}}).json()
        assert d["ok"] is False
        assert d["errors"][0]["code"] == "incomplete_window"
        assert cm.saved_patches == []


# ── 平台能力清单（P0-caps，2026-08-03）──────────────────────────


class TestPlatformCapsSummary:
    """``humanize_caps_by_platform``：按平台收敛 mark_read/typing 的三态语义。

    假矩阵钉纯逻辑（ok / partial / unsupported / unknown + hard 标注），
    真矩阵只做契约烟测（能力细节另有 test_platform_matrix.py 钉住）。
    """

    @staticmethod
    def _row(platform, *, available=True, mark_read=False, typing=False):
        return {"platform": platform, "mode": "x", "available": available,
                "caps": {"mark_read": mark_read, "typing": typing}}

    def test_ok_when_all_modes_support(self):
        from src.integrations.platform_capabilities import (
            humanize_caps_by_platform,
        )
        m = {"telegram:a": self._row("telegram", mark_read=True, typing=True),
             "telegram:b": self._row("telegram", mark_read=True, typing=True)}
        out = humanize_caps_by_platform(matrix=m)
        assert out["telegram"]["mark_read"] == {"state": "ok", "hard": False}
        assert out["telegram"]["typing"] == {"state": "ok", "hard": False}

    def test_partial_when_modes_disagree(self):
        from src.integrations.platform_capabilities import (
            humanize_caps_by_platform,
        )
        m = {"telegram:a": self._row("telegram", mark_read=True, typing=True),
             "telegram:b": self._row("telegram", mark_read=True, typing=False)}
        out = humanize_caps_by_platform(matrix=m)
        assert out["telegram"]["typing"]["state"] == "partial"
        assert out["telegram"]["mark_read"]["state"] == "ok"

    def test_unsupported_with_hard_limit_flag(self):
        # LINE typing 在 HARD_LIMITS 登记（协议层没有端点）→ unsupported + hard
        from src.integrations.platform_capabilities import (
            humanize_caps_by_platform,
        )
        m = {"line:protocol": self._row("line", mark_read=True, typing=False)}
        out = humanize_caps_by_platform(matrix=m)
        assert out["line"]["typing"] == {"state": "unsupported", "hard": True}
        assert out["line"]["mark_read"] == {"state": "ok", "hard": False}

    def test_unknown_when_no_mode_constructible(self):
        # 全部构造不出（缺可选依赖）→ unknown，**不等于不支持**
        from src.integrations.platform_capabilities import (
            humanize_caps_by_platform,
        )
        m = {"messenger:web": self._row("messenger", available=False)}
        out = humanize_caps_by_platform(matrix=m)
        assert out["messenger"]["mark_read"]["state"] == "unknown"
        assert out["messenger"]["typing"]["state"] == "unknown"

    def test_unavailable_mode_does_not_vote(self):
        # 一档构造不出、另一档支持 → 按可构造那档判 ok（缺依赖不投反对票）
        from src.integrations.platform_capabilities import (
            humanize_caps_by_platform,
        )
        m = {"telegram:a": self._row("telegram", available=False),
             "telegram:b": self._row("telegram", mark_read=True, typing=True)}
        out = humanize_caps_by_platform(matrix=m)
        assert out["telegram"]["typing"]["state"] == "ok"

    def test_real_matrix_contract(self):
        # 真矩阵烟测：WORKERS 登记的平台都在场，态落在合法枚举内
        # （worker 构造不出的机器上如实回 unknown，不许崩、不许缺平台）
        from src.integrations.platform_capabilities import (
            humanize_caps_by_platform,
        )
        out = humanize_caps_by_platform({})
        assert {"telegram", "whatsapp", "line", "messenger"} <= set(out)
        for entry in out.values():
            for cap in ("mark_read", "typing"):
                assert entry[cap]["state"] in (
                    "ok", "partial", "unsupported", "unknown")

    def test_route_exposes_platform_caps(self, tmp_path):
        client, _ = _make_client(tmp_path)
        d = client.get("/api/reply-settings").json()
        assert "platform_caps" in d
        pc = d["platform_caps"]
        assert pc is None or isinstance(pc, dict)
        if isinstance(pc, dict) and pc:
            entry = next(iter(pc.values()))
            assert {"mark_read", "typing"} <= set(entry)


# ── i18n pack 契约 ───────────────────────────────────────────────


def test_i18n_pack_bilingual():
    from src.web.i18n_packs import reply_settings_page as pack
    assert set(pack.ZH.keys()) == set(pack.EN.keys())
    assert all(k.startswith("rps_") for k in pack.ZH)
    # 路由用到的错误键必须存在
    for key in ("rps_err_empty", "rps_err_unknown_field", "rps_err_bad_value",
                "rps_err_out_of_range", "rps_err_min_gt_max",
                "rps_err_save_failed", "rps_err_bad_persona", "rps_nav",
                # P1：人设覆写表 + 运行时回读
                "rps_po_title", "rps_po_follow", "rps_po_dup",
                "rps_rt_live", "rps_rt_none",
                # P2：实测观测
                "rps_obs_title", "rps_obs_summary", "rps_obs_empty",
                "rps_obs_native", "rps_obs_hum_title",
                # P0-caps：平台能力清单 + 档位封顶 + 改动明细
                "rps_caps_lbl", "rps_cap_ok", "rps_cap_off", "rps_cap_hard",
                "rps_cap_partial", "rps_cap_unknown", "rps_cap_note",
                "rps_diff_btn", "rps_diff_overrides", "rps_diff_empty",
                # P1：平台专家覆写
                "rps_plat_title", "rps_plat_hint", "rps_plat_prio",
                "rps_nav_plat", "rps_plat_col_cap", "rps_plat_col_voice",
                "rps_plat_cap_none", "rps_plat_unsupported",
                "rps_plat_fn_delay", "rps_plat_fn_flags",
                "rps_diff_platforms", "rps_src_platform",
                "rps_err_bad_platform", "rps_err_cap_unsupported",
                # 2026-08-12：diverged 黄档 + 一键跟随滑杆 + adaptive 体感预览
                "rps_cc_attn_title", "rps_cc_attn_desc", "rps_cc_follow_btn",
                "rps_cc_follow_confirm", "rps_cc_follow_done",
                "rps_cc_follow_fail", "rps_pv_adaptive_obs",
                "rps_pv_adaptive_note",
                # 2026-08-29：额度命名 + 值守关闸确认 + 今日发送量
                "rps_master_confirm_auto_keep_off", "rps_master_confirm_auto_cap",
                "rps_master_gate_kept_off", "rps_guard_title", "rps_sg_title",
                "rps_af_f_budget", "rps_af_f_quota", "rps_guard_q_factory",
                "rps_guard_unlimited", "rps_sg_pending", "rps_sg_today",
                "rps_sg_disabled_note", "rps_nav_quota",
                "rps_dlg_ok", "rps_dlg_cancel"):
        assert key in pack.ZH and key in pack.EN


def test_capability_hints_hold_no_platform_claims():
    """防再烂钉：已读/打字的 hint 不得手写平台名。

    平台支持清单由 ``platform_caps``（capability_matrix 现算）渲染；手写清单
    已实证会过时——旧 hint「当前 Telegram 协议号支持」漏了 WhatsApp/LINE 的
    已读与 WhatsApp 的打字（AGENTS「别信散落的能力注释」同款病）。
    """
    from src.web.i18n_packs import reply_settings_page as pack
    for lang in (pack.ZH, pack.EN):
        for key in ("rps_typing_hint", "rps_markread_hint"):
            txt = lang[key].lower()
            for plat in ("telegram", "line", "whatsapp", "messenger"):
                assert plat not in txt, (
                    f"{key} 手写了平台名 {plat}——会烂掉，改由 platform_caps 渲染")


# ── 一键「改为跟随滑杆」路由（2026-08-12） ─────────────────────────


class TestFollowSliderRoute:
    _CFG = {
        "telegram": {"api_id": "12345", "reply_humanize": {
            "thinking_delay": {"min_sec": 2, "max_sec": 9}}},
        "inbox": {"l2_autosend": {
            "deliver_delay": {"min_sec": 8, "max_sec": 20}}},
    }

    def test_native_converges_to_slider(self, tmp_path):
        import copy
        client, cm = _make_client(tmp_path, config=copy.deepcopy(self._CFG))
        d = client.post("/api/reply-settings/follow-slider",
                        json={"chain": "native_tg"}).json()
        assert d["ok"] is True
        # 三键一起写：overlay 深合并下只写 0/0 会与存量 follow:false 合并成
        # 「显式秒回」——follow:true 必须显式落盘（完整语义）。
        patch = cm.saved_patches[0]
        blk = patch["telegram"]["reply_humanize"]["thinking_delay"]
        assert blk == {"min_sec": 0, "max_sec": 0, "follow": True}
        # 响应快照里该链已改为跟随滑杆（前端拿它就地重绘，无需再拉一次）
        nat = d["chain_coverage"]["chains"]["native_tg"]
        assert nat["source"] == "inbox.l2_autosend.deliver_delay"
        assert nat["diverged"] is False
        assert nat["max_sec"] == 20

    def test_protocol_instant_converges(self, tmp_path):
        # 红档 follow:false 逃生阀 → 按钮同样是出路：写 follow:true 覆掉。
        cfg = {"protocol_autoreply": {"enabled": True,
                                      "delay": {"follow": False}},
               "inbox": {"l2_autosend": {
                   "deliver_delay": {"min_sec": 8, "max_sec": 20}}}}
        client, cm = _make_client(tmp_path, config=cfg)
        d = client.post("/api/reply-settings/follow-slider",
                        json={"chain": "protocol"}).json()
        assert d["ok"] is True
        blk = cm.saved_patches[0]["protocol_autoreply"]["delay"]
        assert blk == {"min_sec": 0, "max_sec": 0, "follow": True}
        proto = d["chain_coverage"]["chains"]["protocol"]
        assert proto["paced"] is True
        assert proto["source"] == "inbox.l2_autosend.deliver_delay"

    def test_unknown_chain_rejected(self, tmp_path):
        client, cm = _make_client(tmp_path)
        for bad in ("autosend", "evil", ""):
            # autosend 本体＝滑杆，没有「跟随自己」的语义 → 同样拒绝
            d = client.post("/api/reply-settings/follow-slider",
                            json={"chain": bad}).json()
            assert d["ok"] is False
            assert d["errors"][0]["code"] == "bad_enum"
        assert cm.saved_patches == []

    def test_save_failure_reported(self, tmp_path):
        import copy
        client, _ = _make_client(
            tmp_path, config=copy.deepcopy(self._CFG), save_ok=False)
        d = client.post("/api/reply-settings/follow-slider",
                        json={"chain": "native_tg"}).json()
        assert d["ok"] is False
        assert d["errors"][0]["code"] == "save_failed"

    def test_audit_written(self, tmp_path):
        import copy
        import json as _json
        client, cm = _make_client(tmp_path, config=copy.deepcopy(self._CFG))
        client.post("/api/reply-settings/follow-slider",
                    json={"chain": "native_tg", "actor": "tester"})
        audit = tmp_path / "reply_settings_audit.jsonl"
        assert audit.exists()
        row = _json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])
        assert row["actor"] == "tester"
        ch = row["changes"]
        assert ch["telegram.reply_humanize.thinking_delay.follow"]["new"] is True
        # 旧值入审计：一键动作可回溯（改回去照着 old 填）
        assert ch["telegram.reply_humanize.thinking_delay.max_sec"]["old"] == 9


class TestBudgetTodayRoute:
    """GET /api/reply-settings/budget-today（P0-guard）契约。

    只读列表：逐行带豁免所需三元组 + budget_flags 同源状态位；store 缺席
    如实 available=false；guard 段回显 parse_cfg 全量（高级参数只读展示）。
    豁免写入口不在本路由（复用收件箱 relief 端点，见 test_reply_budget_route）。
    """
    _CFG = {"inbox": {"peer_bot_guard": {
        "enabled": True, "daily_reply_budget": 3}}}

    def test_no_store_degrades_honestly(self, tmp_path):
        import copy
        client, _ = _make_client(tmp_path, config=copy.deepcopy(self._CFG))
        d = client.get("/api/reply-settings/budget-today").json()
        assert d["ok"] is True and d["available"] is False and d["rows"] == []
        # guard 段＝parse_cfg 解析值（含高级参数），配置卡不因台账缺席瘫痪
        assert d["guard"]["enabled"] is True
        assert d["guard"]["daily_reply_budget"] == 3
        assert "suspect_threshold" in d["guard"]

    def test_rows_flags_order_and_relief_triplet(self, tmp_path):
        import copy

        from src.inbox.models import InboxConversation
        from src.inbox.peer_bot_guard import today_key
        from src.inbox.store import InboxStore
        store = InboxStore(tmp_path / "inbox.db")
        try:
            day = today_key()
            store.upsert_conversation(InboxConversation(
                conversation_id="telegram:acct:peer", platform="telegram",
                account_id="acct", chat_key="peer", display_name="测试客户"))
            for _ in range(3):
                store.bump_auto_reply("telegram:acct:peer", day)   # 触顶
            store.bump_auto_reply("telegram:acct:ok", day)          # 正常
            store.set_budget_relief("telegram:acct:relieved", day)  # 已豁免
            client, _ = _make_client(tmp_path, config=copy.deepcopy(self._CFG))
            client.app.state.inbox_store = store
            d = client.get("/api/reply-settings/budget-today").json()
            assert d["ok"] and d["available"] is True
            rows = {r["conversation_id"]: r for r in d["rows"]}
            # used 降序：触顶行排最前（「今日用量 top1」小字的依据）
            assert d["rows"][0]["conversation_id"] == "telegram:acct:peer"
            capped = rows["telegram:acct:peer"]
            assert capped["exhausted"] and not capped["hard_stopped"]
            assert capped["used"] == 3 and capped["limit"] == 3
            assert capped["title"] == "测试客户"
            # 豁免按钮的三元组必须齐——前端直投收件箱 relief 端点
            assert (capped["platform"], capped["account_id"],
                    capped["chat_key"]) == ("telegram", "acct", "peer")
            ok_row = rows["telegram:acct:ok"]
            assert not ok_row["exhausted"] and ok_row["used"] == 1
            assert ok_row["near"] is False       # P1：行随带 near 预警位
            rel = rows["telegram:acct:relieved"]
            assert rel["relieved"] and not rel["exhausted"]
        finally:
            store.close()

    def test_rows_carry_near_flag(self, tmp_path):
        """P1：≥80% 未触顶的行 near=True——设置页「接近」chip 的数据源。"""
        import copy

        from src.inbox.peer_bot_guard import today_key
        from src.inbox.store import InboxStore
        store = InboxStore(tmp_path / "inbox.db")
        try:
            day = today_key()
            cfg = {"inbox": {"peer_bot_guard": {
                "enabled": True, "daily_reply_budget": 5}}}
            for _ in range(4):                    # 4/5 = 80%
                store.bump_auto_reply("telegram:acct:warm", day)
            client, _ = _make_client(tmp_path, config=copy.deepcopy(cfg))
            client.app.state.inbox_store = store
            d = client.get("/api/reply-settings/budget-today").json()
            row = d["rows"][0]
            assert row["near"] is True and row["exhausted"] is False
        finally:
            store.close()

    def test_guard_disabled_rows_not_exhausted(self, tmp_path):
        """守卫关着：行照常列出（历史用量可见）但永不标 exhausted——
        与 budget_flags.enabled=False 同源，防设置页出幽灵「触顶」。"""
        from src.inbox.peer_bot_guard import today_key
        from src.inbox.store import InboxStore
        store = InboxStore(tmp_path / "inbox.db")
        try:
            day = today_key()
            for _ in range(9):
                store.bump_auto_reply("telegram:acct:busy", day)
            client, _ = _make_client(tmp_path, config={
                "inbox": {"peer_bot_guard": {
                    "enabled": False, "daily_reply_budget": 3}}})
            client.app.state.inbox_store = store
            d = client.get("/api/reply-settings/budget-today").json()
            assert d["guard"]["enabled"] is False
            row = d["rows"][0]
            assert row["used"] == 9
            assert not row["exhausted"] and not row["hard_stopped"]
        finally:
            store.close()


class TestSendgateTodayRoute:
    """GET /api/reply-settings/sendgate-today（P1 2026-08-29）。

    Fake CM 的 config_path=tmp_path/config.yaml → 数据根=tmp_path，
    库落 tmp_path/config/*.db（与 collector 单测同址）。
    """

    def test_no_registry_degrades_honestly(self, tmp_path):
        client, _ = _make_client(tmp_path, config={
            "companion_send_gate": {"enabled": True, "target_cap": 15}})
        d = client.get("/api/reply-settings/sendgate-today").json()
        assert d["ok"] is True and d["available"] is False and d["rows"] == []

    def test_gate_off_usage_without_block(self, tmp_path):
        import sqlite3
        import time
        from src.integrations.account_registry import _DDL as _REG_DDL
        from src.integrations.protocol_autoreply_limits import _SEND_DDL

        now = time.time()
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        con = sqlite3.connect(str(cfg_dir / "account_registry.db"))
        con.executescript(_REG_DDL)
        con.execute(
            "INSERT INTO platform_accounts "
            "(platform, account_id, status, created_at) VALUES (?,?,?,?)",
            ("telegram", "acct1", "online", now - 30 * 86400))
        con.commit()
        con.close()
        scon = sqlite3.connect(str(cfg_dir / "account_sends.db"))
        scon.executescript(_SEND_DDL)
        for i in range(8):
            scon.execute(
                "INSERT INTO account_sends (account_key, ts) VALUES (?,?)",
                ("telegram:acct1", now - i * 60))
        scon.commit()
        scon.close()
        client, _ = _make_client(tmp_path, config={
            "companion_send_gate": {"enabled": False, "target_cap": 15}})
        d = client.get("/api/reply-settings/sendgate-today").json()
        assert d["ok"] and d["available"] is True
        assert d["gate"]["enabled"] is False
        row = d["rows"][0]
        assert row["account"] == "telegram:acct1"
        assert row["used_24h"] == 8
        assert row["auto_verdict"] == "-" and row["manual_verdict"] == "-"


def test_guard_quick_buttons_match_fields_default():
    """快捷档数字必须钉 FIELDS 缺省：出厂 500，不得再出现过期的 rpsGuardQuick(40)。"""
    import re
    from pathlib import Path

    from src.inbox.reply_pacing_settings import FIELDS
    html = (Path(__file__).resolve().parent.parent
            / "src" / "web" / "templates" / "reply_settings.html").read_text(
                encoding="utf-8")
    nums = {int(n) for n in re.findall(r"rpsGuardQuick\((\d+)\)", html)}
    factory = int(FIELDS["inbox.peer_bot_guard.daily_reply_budget"]["default"])
    assert factory == 500
    assert factory in nums
    assert 0 in nums          # 不限额
    assert 40 not in nums


def test_follow_slider_uses_inpage_confirm():
    """「改为跟随滑杆」走页内 rpsConfirm，不再用 window.confirm（会写 overlay，确认必须同款对话框）。"""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent
            / "src" / "web" / "templates" / "reply_settings.html").read_text(
                encoding="utf-8")
    assert "await rpsConfirm(RPS_I18N.ccFollowConfirm" in html
    assert "confirm(RPS_I18N.ccFollowConfirm" not in html


# ── 桌面种子节奏约定门禁（2026-08-12） ─────────────────────────────


def test_desktop_seeds_do_not_pin_native_thinking_delay():
    """桌面种子不得给 A 线钉独立 thinking_delay 值。

    198 实录根因：``config.desktop.internal.yaml`` 种子钉死 2-9s → 装出来的
    节点 A 线永远吃独立键，坐席拖设置页滑杆「改了没变化」。约定＝种子不配值
    （或 max_sec<=0），A 线经 ``resolve_following_delay_block`` 跟随滑杆，
    三条发送链一个滑杆管到底。谁再往种子里写回显式值，这里先红。
    """
    import pathlib

    import yaml
    cfg_dir = pathlib.Path(__file__).resolve().parent.parent / "config"
    for name in ("config.desktop.internal.yaml", "config.desktop.min.yaml"):
        p = cfg_dir / name
        if not p.exists():
            continue
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        td = (((data.get("telegram") or {}).get("reply_humanize") or {})
              .get("thinking_delay") or {})
        try:
            mx = float(td.get("max_sec", 0) or 0)
        except (TypeError, ValueError):
            mx = 0.0
        assert mx <= 0, (
            f"{name} 给 A 线钉了独立 thinking_delay（max_sec={mx}）——"
            "会让设置页滑杆对 A 线失效（198 实录），种子应留空跟随滑杆")
