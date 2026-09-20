"""「影响全自动回复的因素」注册表 + 聚合健康端点门禁（实施56，2026-08-22）。

三层覆盖：
- 防再散映射：``delivery_block._KNOWN_DOMAINS`` 每个拦截域必须在
  ``autoreply_factors.DOMAIN_FACTOR`` 有归属，且归属键都在 ``FACTOR_KEYS``——
  以后新增拦截域时这里先红，逼着同步接进总控台（本主线的存在理由）；
- 纯函数逐因素状态机：ok/warn/bad/na 与 fix 描述符（修复动作全部指向**既有**
  写入口——preset/settings/feature/anchor/link 五种 kind，不出现新写端点语义）；
- 路由端到端：``GET /api/reply-settings/health`` 数据源缺席时按保守判定降级、
  绝不 500（它是总控卡数据源，挂了坐席连「为什么不回」都看不见）。

不触网、不写仓库 config（fake config_manager 落 tmp_path；quota 走无令牌早返路径）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.autoreply_factors import (
    DOMAIN_FACTOR,
    FACTOR_KEYS,
    collect_autoreply_health,
)


def _factors_by_key(out):
    return {f["key"]: f for f in out["factors"]}


# ── 防再散映射门禁 ───────────────────────────────────────────────────────────


def test_domain_factor_covers_all_known_block_domains():
    from src.ops.delivery_block import _KNOWN_DOMAINS
    missing = [d for d in _KNOWN_DOMAINS if d not in DOMAIN_FACTOR]
    assert not missing, (
        f"delivery_block 拦截域 {missing} 没有在 autoreply_factors.DOMAIN_FACTOR "
        "登记归属——新增拦截域必须同步接进「影响全自动的因素」总控清单，"
        "否则又会长出一个用户找不到的散点")
    bad = [k for k in DOMAIN_FACTOR.values() if k not in FACTOR_KEYS]
    assert not bad, f"DOMAIN_FACTOR 指向了不存在的因素键: {bad}"


def test_all_emitted_factors_are_registered():
    out = collect_autoreply_health({})
    keys = [f["key"] for f in out["factors"]]
    assert keys == list(FACTOR_KEYS), "输出顺序/键集必须与注册表一致（UI 依赖顺序）"
    assert len(set(keys)) == len(keys)


# ── 发送链（chat）─────────────────────────────────────────────────────────────


def _cfg_watching():
    return {"inbox": {"l2_autosend": {"enabled": True, "deliver": True}}}


def test_chat_factor_states():
    by = _factors_by_key(collect_autoreply_health(_cfg_watching()))
    assert by["chat"]["state"] == "ok" and by["chat"]["code"] == "watching"
    by = _factors_by_key(collect_autoreply_health(
        {"inbox": {"l2_autosend": {"enabled": True, "deliver": False}}}))
    assert by["chat"]["state"] == "warn" and by["chat"]["code"] == "suggest"
    by = _factors_by_key(collect_autoreply_health({}))
    assert by["chat"]["state"] == "bad" and by["chat"]["code"] == "off"


def test_chat_factor_recent_blocks_downgrade_to_warn():
    by = _factors_by_key(collect_autoreply_health(
        _cfg_watching(), blocks={"active": True, "by_domain": {"chat": 2}}))
    assert by["chat"]["state"] == "warn"
    assert by["chat"]["code"] == "blocks" and by["chat"]["n"] == 2


# ── 图片 / 语音理解 ───────────────────────────────────────────────────────────


def test_vision_factor_disabled_has_media_fix():
    by = _factors_by_key(collect_autoreply_health({"vision": {"enabled": False}}))
    v = by["vision"]
    assert v["state"] == "bad" and v["code"] == "disabled"
    assert v["fix"] == {"kind": "preset", "name": "understand_all"}


def test_vision_factor_enabled_but_no_backend():
    by = _factors_by_key(collect_autoreply_health({"vision": {"enabled": True}}))
    v = by["vision"]
    assert v["state"] == "bad" and v["code"] == "needs_backend"
    assert v["fix"]["kind"] == "preset"


def test_vision_factor_ready_then_ok_and_blocks_warn():
    cfg = {"vision": {"enabled": True, "provider": "openai_compatible",
                      "base_url": "http://192.168.0.9:11434/v1"}}
    by = _factors_by_key(collect_autoreply_health(cfg))
    assert by["vision"]["state"] == "ok"
    by = _factors_by_key(collect_autoreply_health(
        cfg, blocks={"active": True, "by_domain": {"vision": 3}}))
    assert by["vision"]["state"] == "warn" and by["vision"]["n"] == 3


def test_asr_factor_states():
    by = _factors_by_key(collect_autoreply_health(
        {"voice_recognition": {"enabled": False}}))
    assert by["asr"]["state"] == "bad" and by["asr"]["code"] == "disabled"
    assert by["asr"]["fix"] == {"kind": "preset", "name": "understand_all"}
    cfg = {"voice_recognition": {
        "enabled": True, "provider": "openai_compatible",
        "base_url": "http://192.168.0.9:8765/v1"}}
    by = _factors_by_key(collect_autoreply_health(cfg))
    assert by["asr"]["state"] == "ok"


def test_voice_out_factor_only_surfaces_on_blocks():
    by = _factors_by_key(collect_autoreply_health({}))
    assert by["voice_out"]["state"] == "na"
    by = _factors_by_key(collect_autoreply_health(
        {}, blocks={"active": True, "by_domain": {"voice": 1}}))
    assert by["voice_out"]["state"] == "warn" and by["voice_out"]["n"] == 1


# ── 短消息门槛 / 出站翻译 ─────────────────────────────────────────────────────


def test_short_msg_factor():
    by = _factors_by_key(collect_autoreply_health({}))
    assert by["short_msg"]["state"] == "ok" and by["short_msg"]["code"] == "any_len"
    by = _factors_by_key(collect_autoreply_health(
        {"inbox": {"auto_draft": {"min_text_len": 20}}}))
    s = by["short_msg"]
    assert s["state"] == "warn" and s["n"] == 20
    # 修复动作＝reply-settings 白名单 POST 把门槛归零（既有写入口）
    assert s["fix"] == {"kind": "settings",
                        "changes": {"inbox.auto_draft.min_text_len": 0}}


def test_translate_factor():
    by = _factors_by_key(collect_autoreply_health({}))
    t = by["translate"]
    assert t["state"] == "warn" and t["code"] == "off"
    assert t["fix"] == {"kind": "feature",
                        "key": "inbox.l2_autosend.translate.enabled"}
    by = _factors_by_key(collect_autoreply_health(
        {"inbox": {"l2_autosend": {"translate": {"enabled": True}}}}))
    assert by["translate"]["state"] == "ok"


def test_translate_fix_key_is_registered_feature():
    """修复按钮投功能总览 toggle——键必须真在注册表且可见，否则按钮点了 404。"""
    from src.utils.feature_registry import by_key
    f = by_key("inbox.l2_autosend.translate.enabled")
    assert f is not None and f.show and f.cls != "C"


# ── 额度守卫 / 托管额度 ───────────────────────────────────────────────────────


def _guard_cfg(limit=10):
    return {"inbox": {"peer_bot_guard": {
        "enabled": True, "daily_reply_budget": limit}}}


def test_budget_factor_unlimited_when_guard_off():
    by = _factors_by_key(collect_autoreply_health({}))
    assert by["budget"]["state"] == "ok" and by["budget"]["code"] == "unlimited"


def test_budget_factor_rows_missing_reports_config_only():
    by = _factors_by_key(collect_autoreply_health(_guard_cfg()))
    b = by["budget"]
    assert b["state"] == "ok" and b["code"] == "on" and b["limit"] == 10


def test_budget_factor_exhausted_and_near():
    rows = [{"used": 10, "relieved": 0}, {"used": 8, "relieved": 0},
            {"used": 1, "relieved": 0}]
    by = _factors_by_key(collect_autoreply_health(_guard_cfg(), budget_rows=rows))
    b = by["budget"]
    assert b["state"] == "bad" and b["code"] == "exhausted" and b["n"] == 1
    assert b["fix"] == {"kind": "anchor", "href": "#rps-sec-guard"}
    by = _factors_by_key(collect_autoreply_health(
        _guard_cfg(), budget_rows=[{"used": 8, "relieved": 0}]))
    assert by["budget"]["state"] == "warn" and by["budget"]["code"] == "near"
    # 已救济的会话不算触顶（豁免语义与 budget_flags 单点一致）
    by = _factors_by_key(collect_autoreply_health(
        _guard_cfg(), budget_rows=[{"used": 10, "relieved": 1}]))
    assert by["budget"]["state"] == "ok" and by["budget"]["code"] == "headroom"


def test_quota_factor_states():
    by = _factors_by_key(collect_autoreply_health({}))
    assert by["quota"]["state"] == "na"
    by = _factors_by_key(collect_autoreply_health(
        {}, quota={"enabled": True, "budget": 100, "remaining": 62,
                   "exhausted": False}))
    q = by["quota"]
    assert q["state"] == "ok" and q["pct"] == 62
    by = _factors_by_key(collect_autoreply_health(
        {}, quota={"enabled": True, "budget": 100, "remaining": 5,
                   "exhausted": False}))
    assert by["quota"]["state"] == "warn" and by["quota"]["code"] == "low"
    by = _factors_by_key(collect_autoreply_health(
        {}, quota={"enabled": True, "budget": 100, "remaining": 0,
                   "exhausted": True}))
    q = by["quota"]
    assert q["state"] == "bad" and q["code"] == "out"
    assert q["fix"] == {"kind": "link", "href": "/membership"}
    by = _factors_by_key(collect_autoreply_health(
        {}, quota={"enabled": True, "error": "network"}))
    assert by["quota"]["state"] == "warn" and by["quota"]["code"] == "unknown"


# ── 外部会话在线 ─────────────────────────────────────────────────────────────


def test_session_factor_states():
    by = _factors_by_key(collect_autoreply_health({}))
    assert by["session"]["state"] == "na"          # 未注入=不越权担保
    by = _factors_by_key(collect_autoreply_health(
        {}, sessions={"known": False, "unhealthy": {}}))
    assert by["session"]["state"] == "na"          # 纯 TG 协议部署无登记表
    by = _factors_by_key(collect_autoreply_health(
        {}, sessions={"known": True, "unhealthy": {}}))
    assert by["session"]["state"] == "ok"
    by = _factors_by_key(collect_autoreply_health(
        {}, sessions={"known": True, "unhealthy": {
            "messenger:acct1": {"status": "needs_login"},
            "whatsapp:a2": {"status": "logged_out"},   # 运营主动登出≠故障
        }}))
    s = by["session"]
    assert s["state"] == "bad" and s["n"] == 1
    assert s["fix"] == {"kind": "link", "href": "/workspace/channels/messenger"}


# ── 总裁决 ───────────────────────────────────────────────────────────────────


def test_summary_verdict_tiers():
    ready = {
        **_cfg_watching(),
        "vision": {"enabled": True, "provider": "openai_compatible",
                   "base_url": "http://192.168.0.9:11434/v1"},
        "voice_recognition": {"enabled": True, "provider": "openai_compatible",
                              "base_url": "http://192.168.0.9:8765/v1"},
        "inbox": {"l2_autosend": {"enabled": True, "deliver": True,
                                  "translate": {"enabled": True}}},
    }
    assert collect_autoreply_health(ready)["summary"]["verdict"] == "full"
    deg = {**ready, "inbox": {**ready["inbox"],
                              "auto_draft": {"min_text_len": 20}}}
    assert collect_autoreply_health(deg)["summary"]["verdict"] == "degraded"
    attn = {**ready, "vision": {"enabled": False}}
    assert collect_autoreply_health(attn)["summary"]["verdict"] == "attn"
    assert collect_autoreply_health({})["summary"]["verdict"] == "paused"


# ── 路由端到端 ───────────────────────────────────────────────────────────────


class _FakeCM:
    def __init__(self, tmp_path: Path, config: dict):
        self.config_path = str(tmp_path / "config" / "config.yaml")
        Path(self.config_path).parent.mkdir(parents=True, exist_ok=True)
        self.config = config


def _health_client(tmp_path, config):
    from src.web.routes.reply_settings_routes import register_reply_settings_routes

    app = FastAPI()

    async def _noop(request: Request):
        return None

    register_reply_settings_routes(
        app, page_auth=_noop, api_auth=_noop,
        templates=None, config_manager=_FakeCM(tmp_path, config))
    return TestClient(app)


def test_health_route_contract_and_degrades_without_sources(tmp_path):
    """store/quota/会话表全缺席 → 保守判定照常出清单，绝不 500。"""
    from src.ops.delivery_block import reset_for_tests
    reset_for_tests()
    client = _health_client(tmp_path, {
        "inbox": {"l2_autosend": {"enabled": True, "deliver": True}},
        "vision": {"enabled": False},
    })
    d = client.get("/api/reply-settings/health").json()
    assert d["ok"] is True
    by = {f["key"]: f for f in d["factors"]}
    assert set(by) == set(FACTOR_KEYS)
    assert by["chat"]["state"] == "ok"
    assert by["vision"]["state"] == "bad"
    assert by["quota"]["state"] == "na"       # fake CM 无令牌状态文件 → 非托管
    assert d["summary"]["verdict"] == "attn"
    assert "blocks" in d


def test_health_route_reflects_delivery_blocks(tmp_path):
    from src.ops.delivery_block import report_block, reset_for_tests
    reset_for_tests()
    try:
        report_block("vision", reason="enrich_failed", platform="telegram",
                     conversation_id="c1")
        client = _health_client(tmp_path, {
            "inbox": {"l2_autosend": {"enabled": True, "deliver": True}},
            "vision": {"enabled": True, "provider": "openai_compatible",
                       "base_url": "http://192.168.0.9:11434/v1"},
        })
        d = client.get("/api/reply-settings/health").json()
        by = {f["key"]: f for f in d["factors"]}
        assert by["vision"]["state"] == "warn" and by["vision"]["n"] == 1
        assert d["blocks"]["active"] is True
        assert d["blocks"]["by_domain"].get("vision") == 1
    finally:
        reset_for_tests()
