# -*- coding: utf-8 -*-
"""小智动作注册表门禁（实施58 P1，2026-08-23）。

重点覆盖「不该发生」的路径：未知动作/坏参数必拒、goto 白名单外必拒、
坐席角色摸不到 L2、确认 token 单次核销+过期+跨用户必拒、undo 精确回写、
审计落盘。全部纯核心（FakeCM 注入），零 web/零实例依赖。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.assistant import actions as act


class FakeCM:
    """set_overlay_flag 语义仿真：点分路径写进嵌套 dict，返回 (True, '')。"""

    def __init__(self, tmp: Path, config=None):
        self.config = config if config is not None else {}
        self.config_path = str(tmp / "config.yaml")
        self.calls = []

    def set_overlay_flag(self, path, value):
        self.calls.append((path, value))
        cur = self.config
        parts = str(path).split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
        return True, ""


class BrokenCM(FakeCM):
    def set_overlay_flag(self, path, value):
        return False, "disk full"


@pytest.fixture(autouse=True)
def _clean_state():
    act._CONFIRMS.clear()
    act._UNDOS.clear()
    yield
    act._CONFIRMS.clear()
    act._UNDOS.clear()


# ── 注册表结构 ────────────────────────────────────────────────────────────
def test_catalog_bilingual_and_structure():
    for aid, spec in act.ACTIONS.items():
        assert spec["level"] in ("L0", "L1", "L2"), aid
        for k in ("label_zh", "label_en", "desc_zh", "desc_en"):
            assert str(spec.get(k) or "").strip(), f"{aid} 缺 {k}"
        if spec["level"] == "L2" and spec["kind"] == "settings":
            assert spec.get("fields"), f"L2 settings 动作 {aid} 必须声明 fields 白名单"
    zh = {a["id"]: a for a in act.catalog("master", "zh")}
    en = {a["id"]: a for a in act.catalog("master", "en")}
    assert set(zh) == set(en) == set(act.ACTIONS)
    assert zh["goto_page"]["label"] != en["goto_page"]["label"]


def test_agent_role_cannot_see_or_run_l2():
    assert act.allowed_levels_for_role("agent") == ("L0", "L1")
    ids = {a["id"] for a in act.catalog("agent", "zh")}
    assert "set_reply_delay" not in ids and "toggle_voice_reply" not in ids
    assert "diagnose_autoreply" in ids and "goto_page" in ids


# ── plan 校验 ────────────────────────────────────────────────────────────
def test_unknown_action_rejected():
    p = act.plan_action("rm_rf_everything", {}, {})
    assert p["ok"] is False and p["error"] == "unknown_action"


def test_goto_whitelist():
    ok = act.plan_action("goto_page", {"path": "/reply-settings"}, {})
    assert ok["ok"] and ok["goto"] == "/reply-settings"
    bad = act.plan_action("goto_page", {"path": "/api/admin/anything"}, {})
    assert bad["ok"] is False and bad["error"] == "path_not_allowed"
    custom = act.plan_action("goto_page", {"path": "/x"}, {},
                             nav_paths={"/x"})
    assert custom["ok"] and custom["goto"] == "/x"


def test_delay_validation_paths():
    assert act.plan_action("set_reply_delay", {"min_sec": 3}, {})["error"] == \
        "bad_params"  # 缺 max_sec
    assert act.plan_action("set_reply_delay",
                           {"min_sec": 10, "max_sec": 5}, {})["error"] == \
        "bad_params"  # min > max
    assert act.plan_action("set_reply_delay",
                           {"min_sec": -1, "max_sec": 5}, {})["error"] == \
        "bad_params"  # 越界
    assert act.plan_action("set_reply_delay",
                           {"min_sec": "3", "max_sec": 8}, {})["error"] == \
        "bad_params"  # 类型
    ok = act.plan_action("set_reply_delay", {"min_sec": 3, "max_sec": 8},
                         {"inbox": {"l2_autosend": {"deliver_delay":
                                                    {"min_sec": 1}}}})
    assert ok["ok"] and ok["level"] == "L2"
    d = {x["path"]: x for x in ok["diff"]}
    assert d["inbox.l2_autosend.deliver_delay.min_sec"]["old"] == 1
    assert d["inbox.l2_autosend.deliver_delay.max_sec"]["old"] == 0  # 缺省快照


def test_toggle_param_must_be_bool():
    assert act.plan_action("toggle_voice_reply", {"enabled": "yes"},
                           {})["error"] == "bad_params"
    ok = act.plan_action("toggle_voice_reply", {"enabled": True}, {})
    assert ok["ok"] and ok["diff"][0]["old"] is False  # FIELDS 缺省


# ── 2026-08-23 动作池扩容：新参数类型 + 人话标签 ─────────────────────────
def test_enum_field_rejects_bad_choice():
    assert act.plan_action("set_automation_mode", {"mode": "yolo"},
                           {})["error"] == "bad_params"
    assert act.plan_action("set_automation_mode", {"mode": 3},
                           {})["error"] == "bad_params"
    ok = act.plan_action("set_automation_mode", {"mode": "review"}, {})
    assert ok["ok"] and ok["diff"][0]["new"] == "review"


def test_text_field_maxlen_and_empty_clear():
    long = "x" * 121
    assert act.plan_action("set_tone_hint", {"hint": long},
                           {})["error"] == "bad_params"
    ok = act.plan_action("set_tone_hint", {"hint": ""}, {})
    assert ok["ok"] and ok["diff"][0]["new"] == ""


def test_platform_cap_path_templating_and_whitelist():
    bad = act.plan_action("set_platform_automation_cap",
                          {"platform": "wechat", "mode": "review"}, {})
    assert bad["error"] == "bad_params"
    missing = act.plan_action("set_platform_automation_cap",
                              {"mode": "review"}, {})
    assert missing["error"] == "bad_params"
    ok = act.plan_action(
        "set_platform_automation_cap",
        {"platform": "messenger", "mode": "review"},
        {"inbox": {"auto_draft": {"platform_modes": {"messenger": "auto_ai"}}}})
    assert ok["ok"]
    assert ok["diff"][0]["path"] == "inbox.auto_draft.platform_modes.messenger"
    assert ok["diff"][0]["old"] == "auto_ai"
    assert ok["clean_params"] == {"platform": "messenger", "mode": "review"}


def test_diff_carries_human_labels_both_langs():
    zh = act.plan_action("set_automation_mode", {"mode": "auto_ai"},
                         {"inbox": {"auto_draft":
                                    {"automation_mode": "review"}}})
    d = zh["diff"][0]
    assert d["label"] == "回复档位"
    assert d["old_h"] == "AI拟稿·人审后发" and d["new_h"] == "全自动"
    en = act.plan_action("set_automation_mode", {"mode": "auto_ai"},
                         {}, lang="en")
    assert en["diff"][0]["label"] == "Reply mode"
    assert en["diff"][0]["new_h"] == "fully automatic"
    b = act.plan_action("toggle_selfie", {"enabled": True}, {})
    assert b["diff"][0]["old_h"] == "关" and b["diff"][0]["new_h"] == "开"


def test_all_l2_fields_have_bilingual_labels():
    """老板令：确认卡不给用户看 config 路径——每个 L2 字段必须带双语标签。"""
    for aid, spec in act.ACTIONS.items():
        for f in spec.get("fields", []):
            assert str(f.get("label_zh") or "").strip(), f"{aid} 字段缺 label_zh"
            assert str(f.get("label_en") or "").strip(), f"{aid} 字段缺 label_en"
            if "{platform}" in str(f.get("path")):
                assert f.get("platforms"), f"{aid} 模板路径缺 platforms 白名单"


def test_all_l2_actions_declare_home_in_core_nav():
    """每个 L2 动作必须登记所在页面 home 且落在 CORE_NAV_PATHS 兜底集内——
    「带你去操作」的确定性导航（_maybe_insert_home_nav）靠它；home 不在
    兜底集＝nav_schema 提取失败时导航静默消失（正是本次事故的形态）。"""
    for aid, spec in act.ACTIONS.items():
        # 只有 L2 **settings**（写 config）动作需要 home 页；L2 runner 动作在受控机
        # 上执行、不插本地 home-nav（_maybe_insert_home_nav 已跳过 runner 步）。
        is_l2_settings = spec["level"] == "L2" and spec["kind"] == "settings"
        if not is_l2_settings:
            assert "home" not in spec, f"{aid} 非 L2-settings 不该有 home"
            continue
        home = str(spec.get("home") or "")
        assert home.startswith("/"), f"L2 settings 动作 {aid} 缺 home 页面登记"
        assert home in act.CORE_NAV_PATHS, (
            f"{aid} 的 home={home} 不在 CORE_NAV_PATHS 兜底集")


def test_action_platforms_match_pacing_registry():
    from src.inbox.reply_pacing_settings import PLATFORMS

    assert tuple(act.ACTION_PLATFORMS) == tuple(PLATFORMS), (
        "ACTION_PLATFORMS 与 reply_pacing_settings.PLATFORMS 漂移——"
        "两表必须同键域（本模块零依赖故本地镜像，靠本门禁钉住）"
    )


# ── P1：任务历史 + 撤销中心 ──────────────────────────────────────────────
def test_audit_carries_undo_id_and_history_reads_back(tmp_path):
    cm = FakeCM(tmp_path)
    plan = act.plan_action("toggle_voice_reply", {"enabled": True}, cm.config)
    res = act.apply_plan(plan, cm, "boss")
    assert res["undo_id"]
    audit = Path(cm.config_path).parent / "assistant_actions_audit.jsonl"
    rec = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert rec["undo_id"] == res["undo_id"]

    assert act.has_undo(res["undo_id"]) is True
    hist = act.read_history(cm)
    assert hist and hist[0]["op"] == "apply"
    assert hist[0]["undo_id"] == res["undo_id"]

    act.apply_undo(res["undo_id"], cm, "boss")
    assert act.has_undo(res["undo_id"]) is False
    hist2 = act.read_history(cm)
    assert hist2[0]["op"] == "undo"  # 新→旧排序
    assert hist2[1]["op"] == "apply"


def test_read_history_skips_garbage_and_limits(tmp_path):
    cm = FakeCM(tmp_path)
    audit = Path(cm.config_path).parent / "assistant_actions_audit.jsonl"
    lines = ["not json", '{"op": "noise"}']
    for i in range(5):
        lines.append(json.dumps({"op": "apply", "actor": "a", "ts": i,
                                 "action": "toggle_voice_reply",
                                 "path": "inbox.l2_autosend.voice.enabled",
                                 "old": False, "new": True}))
    audit.write_text("\n".join(lines), encoding="utf-8")
    hist = act.read_history(cm, limit=3)
    assert len(hist) == 3 and all(h["op"] == "apply" for h in hist)
    assert act.read_history(FakeCM(tmp_path / "empty")) == []


def test_describe_path_exact_and_template():
    label, spec = act.describe_path("inbox.l2_autosend.voice.enabled", "zh")
    assert label == "自动语音回复" and spec is not None
    label_en, _ = act.describe_path("inbox.l2_autosend.voice.enabled", "en")
    assert label_en == "Auto voice replies"
    tpl, spec2 = act.describe_path(
        "inbox.auto_draft.platform_modes.messenger", "zh")
    assert "messenger" in tpl and spec2 is not None
    assert act.humanize_value(
        "inbox.auto_draft.platform_modes.messenger", "review", "zh") == \
        "AI拟稿·人审后发"
    unknown, none_spec = act.describe_path("a.b.c", "zh")
    assert none_spec is None and unknown


# ── P2：L0 查询摘要器（路由取快照、纯函数出人话一句）──────────────────
def test_summarize_ai_status_variants():
    assert "读不到" in act.summarize_ai_status(None, "zh")
    ok = act.summarize_ai_status({"degraded": False, "mode": "primary"}, "zh")
    assert ok.startswith("AI 正常") and "主链在岗" in ok
    deg = act.summarize_ai_status({"degraded": True, "mode": "local"}, "zh")
    assert deg.startswith("AI 降级中") and "本地兜底" in deg
    en = act.summarize_ai_status({"degraded": True, "mode": "pool"}, "en")
    assert en.startswith("AI degraded") and "backup key" in en


def test_summarize_reply_budget_variants():
    off = act.summarize_reply_budget(0, 0, 0, {"enabled": False}, "zh")
    assert "未开启" in off
    # enabled 但 budget=0 = YAML 不限额语义，同样按未开启口径说话
    off2 = act.summarize_reply_budget(
        3, 1, 0, {"enabled": True, "daily_reply_budget": 0}, "zh")
    assert "未开启" in off2
    idle = act.summarize_reply_budget(
        0, 0, 0, {"enabled": True, "daily_reply_budget": 500}, "zh")
    assert "还没有会话用到" in idle and "500" in idle
    busy = act.summarize_reply_budget(
        7, 2, 1, {"enabled": True, "daily_reply_budget": 500}, "zh")
    assert "7 个会话" in busy and "2 个触顶" in busy and "1 个已豁免" in busy
    en = act.summarize_reply_budget(
        7, 0, 0, {"enabled": True, "daily_reply_budget": 500}, "en")
    assert "none capped" in en


def test_summarize_platform_sessions_variants():
    assert "没有外部平台会话" in act.summarize_platform_sessions(None, "zh")
    ok = act.summarize_platform_sessions(
        {"sessions": {"a": {}, "b": {}}, "unhealthy": []}, "zh")
    assert "全部平台会话健康" in ok and "2" in ok
    bad = act.summarize_platform_sessions(
        {"sessions": {"a": {}, "b": {}}, "unhealthy": ["messenger:x"]}, "zh")
    assert "1 条会话不健康" in bad and "messenger:x" in bad
    en = act.summarize_platform_sessions(
        {"sessions": {"a": {}}, "unhealthy": ["m:x"]}, "en")
    assert "unhealthy" in en


def test_query_actions_registered_as_l0():
    for aid in ("query_ai_status", "query_reply_budget",
                "query_platform_health"):
        assert act.ACTIONS[aid]["level"] == "L0"
        assert act.ACTIONS[aid]["kind"] == "query"
        p = act.plan_action(aid, {}, {})
        assert p["ok"] and p["kind"] == "query"


def test_new_action_paths_are_registered_write_surfaces():
    """扩容动作只许写已知写面：reply_pacing_settings.FIELDS 登记过的路径，
    或 companion/translate 家族的既有开关键——防手滑写错键名。"""
    from src.inbox.reply_pacing_settings import FIELDS

    known_extra = {
        "inbox.l2_autosend.translate.enabled",
        "companion.proactive_topic.enabled",
        "companion.proactive_topic.daily_ritual.enabled",
        "companion.selfie.enabled",
    }
    for aid, spec in act.ACTIONS.items():
        for f in spec.get("fields", []):
            path = str(f["path"])
            if "{platform}" in path:
                path = path.rsplit(".", 1)[0]
                assert any(k.startswith(path) for k in FIELDS), (
                    f"{aid} 模板路径 {path} 不在 FIELDS 家族")
                continue
            assert path in FIELDS or path in known_extra, (
                f"{aid} 写了未登记路径 {path}")


# ── 确认 token ───────────────────────────────────────────────────────────
def test_confirm_token_single_use_and_uid_bound():
    plan = act.plan_action("toggle_voice_reply", {"enabled": True}, {})
    tok = act.issue_confirm(plan, "u1")
    assert act.pop_confirm(tok, "u2") is None      # 跨用户必拒
    assert act.pop_confirm(tok, "u1") is None      # 上一行已核销（取走即删）
    tok2 = act.issue_confirm(plan, "u1")
    got = act.pop_confirm(tok2, "u1")
    assert got and got["action"] == "toggle_voice_reply"
    assert act.pop_confirm(tok2, "u1") is None     # 单次核销


def test_confirm_token_expiry(monkeypatch):
    plan = act.plan_action("toggle_voice_reply", {"enabled": True}, {})
    t0 = act._time()
    tok = act.issue_confirm(plan, "u1")
    monkeypatch.setattr(act, "_time", lambda: t0 + act.CONFIRM_TTL_SEC + 5)
    assert act.pop_confirm(tok, "u1") is None


# ── 应用 / 撤销 / 审计 ───────────────────────────────────────────────────
def test_apply_undo_roundtrip_with_audit(tmp_path):
    cm = FakeCM(tmp_path, {"inbox": {"l2_autosend": {"voice":
                                                     {"enabled": False}}}})
    plan = act.plan_action("toggle_voice_reply", {"enabled": True}, cm.config)
    res = act.apply_plan(plan, cm, "boss")
    assert res["ok"] and res["applied"] == ["inbox.l2_autosend.voice.enabled"]
    assert cm.config["inbox"]["l2_autosend"]["voice"]["enabled"] is True
    assert res["undo_id"].startswith("xzu-")

    undo = act.apply_undo(res["undo_id"], cm, "boss")
    assert undo and undo["ok"]
    assert cm.config["inbox"]["l2_autosend"]["voice"]["enabled"] is False
    # 快照单次核销
    assert act.apply_undo(res["undo_id"], cm, "boss") is None

    audit = Path(cm.config_path).parent / "assistant_actions_audit.jsonl"
    lines = [json.loads(x) for x in
             audit.read_text(encoding="utf-8").splitlines()]
    assert [x["op"] for x in lines] == ["apply", "undo"]
    assert lines[0]["actor"] == "boss"
    assert lines[1]["restored"] is False


def test_apply_failure_reported_not_swallowed(tmp_path):
    cm = BrokenCM(tmp_path)
    plan = act.plan_action("toggle_voice_reply", {"enabled": True}, cm.config)
    res = act.apply_plan(plan, cm, "boss")
    assert res["ok"] is False and not res["applied"]
    assert res["failed"][0]["msg"] == "disk full"
    assert res["undo_id"] == ""  # 没写成的东西没有撤销资格


def test_delay_apply_writes_both_fields(tmp_path):
    cm = FakeCM(tmp_path)
    plan = act.plan_action("set_reply_delay", {"min_sec": 3, "max_sec": 8},
                           cm.config)
    res = act.apply_plan(plan, cm, "op")
    assert res["ok"] and len(res["applied"]) == 2
    dd = cm.config["inbox"]["l2_autosend"]["deliver_delay"]
    assert dd["min_sec"] == 3 and dd["max_sec"] == 8
    undo = act.apply_undo(res["undo_id"], cm, "op")
    assert undo["ok"] and len(undo["restored"]) == 2
    assert dd["min_sec"] == 0 and dd["max_sec"] == 0  # 回缺省快照


# ── 切档存量对齐（2026-08-30，「小智说切了档但全自动还在回」事故）─────────
class FakeStore:
    """conversation_settings 显式档位行仿真（list/set/cancel 三口）。"""

    def __init__(self, rows, group_cids=()):
        self.rows = [dict(r) for r in rows]
        self.group_cids = set(group_cids)
        self.set_calls = []
        self.cancelled = []

    def list_automation_mode_rows(self):
        return [dict(r) for r in self.rows]

    def set_automation_mode(self, cid, mode, *, source=""):
        self.set_calls.append((cid, mode, source))
        for r in self.rows:
            if r["conversation_id"] == cid:
                r["automation_mode"] = mode
                r["source"] = source

    def cancel_pending_l2_drafts(self, cid, decided_by=""):
        self.cancelled.append((cid, decided_by))
        return 1

    def get_conversation(self, cid):
        return {"conversation_id": cid,
                "chat_type": "group" if cid in self.group_cids else "private"}

    def modes(self):
        return {r["conversation_id"]: r["automation_mode"] for r in self.rows}


def _stock_rows():
    """zhiliao 实录的缩影：auto_ai 行来源五花八门 + 已是人审/手动的行。"""
    return [
        {"conversation_id": "telegram:a:1", "automation_mode": "auto_ai",
         "source": "bootstrap"},
        {"conversation_id": "telegram:a:2", "automation_mode": "auto_ai",
         "source": "human"},
        {"conversation_id": "telegram:a:3", "automation_mode": "auto_ai",
         "source": ""},                      # 旧代码写的空来源行
        {"conversation_id": "whatsapp:b:g1", "automation_mode": "auto_ai",
         "source": "rearm"},                 # 群（chat_type=group）
        {"conversation_id": "telegram:a:-100999", "automation_mode": "auto_ai",
         "source": "human"},                 # 群（telegram 负 id 启发式）
        {"conversation_id": "telegram:a:5", "automation_mode": "review",
         "source": "standby"},
        {"conversation_id": "telegram:a:6", "automation_mode": "manual",
         "source": "takeover"},
    ]


def test_align_downgrade_covers_every_auto_ai_row_including_human_and_groups():
    """降档=安全方向：显式 auto_ai 行不分来源全部降下来（含群，预览有数），
    manual/review 行绝不动——「关闭全自动」后不允许任何会话还在直发。"""
    store = FakeStore(_stock_rows(), group_cids={"whatsapp:b:g1"})
    pv = act.preview_mode_alignment(store, "review")
    assert pv == {"total": 5, "groups": 2}
    res = act.align_conversation_modes(store, "review")
    assert res["changed"] == 5 and res["groups"] == 2
    assert res["cancelled"] == 5    # 每行顺带作废 pending L2 草稿
    m = store.modes()
    assert m["telegram:a:1"] == m["telegram:a:2"] == m["telegram:a:3"] == \
        m["whatsapp:b:g1"] == m["telegram:a:-100999"] == "review"
    assert m["telegram:a:5"] == "review" and m["telegram:a:6"] == "manual"
    assert all(c[2] == "assistant" for c in store.set_calls)
    assert {c[0] for c in store.cancelled} == {
        "telegram:a:1", "telegram:a:2", "telegram:a:3",
        "whatsapp:b:g1", "telegram:a:-100999"}


def test_align_upgrade_only_system_sources_and_never_groups():
    """升档=风险方向：只抬系统来源（bootstrap/standby/assistant*）的行，
    human/takeover/guard 的显式决定绝不覆盖；群一律跳过。"""
    rows = [
        {"conversation_id": "telegram:a:1", "automation_mode": "review",
         "source": "bootstrap"},
        {"conversation_id": "telegram:a:2", "automation_mode": "review",
         "source": "standby"},
        {"conversation_id": "telegram:a:3", "automation_mode": "review",
         "source": "assistant"},
        {"conversation_id": "telegram:a:4", "automation_mode": "review",
         "source": "human"},
        {"conversation_id": "telegram:a:5", "automation_mode": "manual",
         "source": "guard:x"},
        {"conversation_id": "telegram:a:6", "automation_mode": "review",
         "source": ""},                      # 空来源=证明不了是系统写的
        {"conversation_id": "whatsapp:b:g1", "automation_mode": "review",
         "source": "bootstrap"},             # 群：系统来源也不许抬
    ]
    store = FakeStore(rows, group_cids={"whatsapp:b:g1"})
    res = act.align_conversation_modes(store, "auto_ai")
    assert res["changed"] == 3 and res["cancelled"] == 0
    m = store.modes()
    assert m["telegram:a:1"] == m["telegram:a:2"] == m["telegram:a:3"] == \
        "auto_ai"
    assert m["telegram:a:4"] == "review" and m["telegram:a:5"] == "manual"
    assert m["telegram:a:6"] == "review" and m["whatsapp:b:g1"] == "review"


def test_align_undo_restores_conversation_rows(tmp_path):
    """undo 对称性：overlay 键回旧值 + 对齐过的会话行回对齐前档位。"""
    cm = FakeCM(tmp_path, {"inbox": {"auto_draft":
                                     {"automation_mode": "auto_ai"}}})
    store = FakeStore(_stock_rows(), group_cids={"whatsapp:b:g1"})
    plan = act.plan_action("set_automation_mode", {"mode": "review"},
                           cm.config)
    res = act.apply_plan(plan, cm, "boss")
    ares = act.align_conversation_modes(store, "review", cm=cm, actor="boss")
    act.attach_undo_conversations(res["undo_id"], ares["olds"])

    undo = act.apply_undo(res["undo_id"], cm, "boss", store=store)
    assert undo["ok"] and undo["conv_restored"] == 5
    assert cm.config["inbox"]["auto_draft"]["automation_mode"] == "auto_ai"
    m = store.modes()
    assert m["telegram:a:2"] == "auto_ai"    # human 行也原样回
    assert m["telegram:a:6"] == "manual"     # 没动过的行保持不动
    # 撤销写的行诚实标注来源（不伪造成原 source）
    assert ("telegram:a:2", "auto_ai", "assistant_undo") in store.set_calls
    # 审计带对齐行（op=align_modes 不进 read_history 的 apply/undo 视图）
    audit = Path(cm.config_path).parent / "assistant_actions_audit.jsonl"
    ops = [json.loads(x)["op"] for x in
           audit.read_text(encoding="utf-8").splitlines()]
    assert ops.count("align_modes") == 2     # 一次对齐 + 一次撤销回写
    assert all(h["op"] in ("apply", "undo") for h in act.read_history(cm))


def test_align_noop_paths_are_safe():
    """store 缺席/旧 store 无读口/非法目标档 → 全部零对齐零异常。"""
    assert act.preview_mode_alignment(None, "review") == \
        {"total": 0, "groups": 0}
    assert act.align_conversation_modes(None, "review")["changed"] == 0

    class OldStore:   # 无 list_automation_mode_rows
        pass
    assert act.align_conversation_modes(OldStore(), "review")["changed"] == 0
    store = FakeStore(_stock_rows())
    assert act.align_conversation_modes(store, "yolo")["changed"] == 0
    assert store.set_calls == []
    # undo 快照无 conv_olds / store 缺席时 conv_restored 如实为 0
    assert act.attach_undo_conversations("", []) is None


# ── 2026-08-30 动作池扩容第二批（实施88 P0）──────────────────────────────
def test_hhmm_coerce_normalizes_and_rejects():
    spec = {"type": "hhmm", "param": "start"}
    assert act._coerce(spec, "9:00") == (True, "09:00")      # 归一化零填充
    assert act._coerce(spec, "23:59") == (True, "23:59")
    assert act._coerce(spec, "") == (True, "")               # 空=清除
    assert act._coerce(spec, "24:00")[0] is False
    assert act._coerce(spec, "9:5")[0] is False
    assert act._coerce(spec, "0900")[0] is False
    assert act._coerce(spec, 900)[0] is False


def test_work_hours_half_configured_rejected_overnight_ok():
    half = act.plan_action("set_work_hours", {"start": "09:00", "end": ""},
                           {})
    assert not half["ok"] and half["error"] == "bad_params"
    clear = act.plan_action("set_work_hours", {"start": "", "end": ""}, {})
    assert clear["ok"]
    overnight = act.plan_action("set_work_hours",
                                {"start": "22:00", "end": "06:00"}, {})
    assert overnight["ok"]      # 跨夜班合法


def test_daily_reply_budget_bounds_and_zero_unlimited():
    assert act.plan_action("set_daily_reply_budget", {"n": -1},
                           {})["error"] == "bad_params"
    assert act.plan_action("set_daily_reply_budget", {"n": 1_000_001},
                           {})["error"] == "bad_params"
    zero = act.plan_action("set_daily_reply_budget", {"n": 0}, {})
    assert zero["ok"]           # 0=不限额（FIELDS 语义，不是全拦）
    assert "0" in zero["diff"][0]["label"]  # 语义写在字段标签里防误解


def test_second_batch_actions_present_with_correct_levels():
    expect = {
        "query_version": "L0",
        "ui_act": "L1",
        "set_typing_indicator": "L2",
        "set_mark_read": "L2",
        "toggle_adaptive_delay": "L2",
        "toggle_bubbles": "L2",
        "set_daily_reply_budget": "L2",
        "set_work_hours": "L2",
        "toggle_holding": "L2",
    }
    for aid, lv in expect.items():
        assert aid in act.ACTIONS, f"缺动作 {aid}"
        assert act.ACTIONS[aid]["level"] == lv, f"{aid} 等级漂移"


def test_summarize_version_honest_when_unavailable():
    assert "v1.0.63" in act.summarize_version("1.0.63", "zh")
    assert "读取不到" in act.summarize_version("", "zh")
    assert "unavailable" in act.summarize_version("", "en").lower()
