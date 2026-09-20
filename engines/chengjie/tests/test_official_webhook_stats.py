# -*- coding: utf-8 -*-
"""官方渠道 webhook 回调可达性台账门禁（official_webhook_stats，2026-08-07）。

守的断层：「凭证配了、状态绿了、但客户消息永远进不来」——官方 API 渠道是被动
webhook 入站，回调不可达时系统零报错。重点覆盖：

- 判词阶梯纯函数（disabled/not_mounted/never_reached/auth_failing/handshake_only/live）
- 台账跨重启语义（verify 握手是配置期一次性动作，落盘丢了会把健康渠道误判成从未接通）
- 记录函数「绝不抛异常」契约（观测挂了不能影响 webhook 收发主流程）
- 四个 webhook 家族的埋点接线（静态断言，防悄悄退线）
- creds_ok 与各 register_* 的 early-return 条件逐字对齐
- ops 卡三件套（section / loader / 注册表）+ i18n 双语
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.integrations import official_webhook_stats as ows

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path):
    """每例独立台账落盘（叠加 conftest 的 AITR_DATA_DIR 隔离，双保险）。"""
    ows.reset_for_tests(tmp_path / "official_webhook_state.json")
    yield
    ows.reset_for_tests(None)


# ── 判词阶梯 ─────────────────────────────────────────────────────────────────

def _row(**kw):
    row = dict(ows._EMPTY_ROW)
    row.update(kw)
    return row


def test_classify_disabled_and_not_mounted_precede_everything():
    assert ows.classify(enabled=False, creds=False, mounted=False,
                        has_verify=True, row=_row(events_total=9)) == "disabled"
    # 启用但没挂载：哪怕历史上有过事件（上个进程挂过），当前进程就是收不到
    assert ows.classify(enabled=True, creds=True, mounted=False,
                        has_verify=True, row=_row(events_total=9)) == "not_mounted"


def test_classify_live_wins_over_handshake_and_errors():
    row = _row(events_total=3, last_verify_ts=time.time(), error_total=2)
    assert ows.classify(enabled=True, creds=True, mounted=True,
                        has_verify=True, row=row) == "live"


def test_classify_handshake_only_requires_get_verify_platform():
    row = _row(last_verify_ts=time.time())
    assert ows.classify(enabled=True, creds=True, mounted=True,
                        has_verify=True, row=row) == "handshake_only"
    # Zalo（无 GET 握手）不可能出 handshake_only —— last_verify_ts 恒 0，
    # 若数据异常带了值也不采信
    assert ows.classify(enabled=True, creds=True, mounted=True,
                        has_verify=False, row=row) == "never_reached"


def test_classify_auth_failing_vs_never_reached():
    # 有请求到达但没一次成功（verify 失败 / 验签失败）→ auth_failing（公网可达，凭证错）
    assert ows.classify(enabled=True, creds=True, mounted=True, has_verify=True,
                        row=_row(verify_fail_total=2)) == "auth_failing"
    assert ows.classify(enabled=True, creds=True, mounted=True, has_verify=False,
                        row=_row(error_total=1)) == "auth_failing"
    # 零接触 → never_reached（公网 URL/隧道/后台回调没配）
    assert ows.classify(enabled=True, creds=True, mounted=True, has_verify=True,
                        row=_row()) == "never_reached"


# ── 台账语义 ─────────────────────────────────────────────────────────────────

def test_record_and_snapshot_roundtrip_across_reload(tmp_path):
    p = tmp_path / "state.json"
    ows.reset_for_tests(p)
    ows.record_verify("instagram", ok=True)
    ows.record_event("instagram")
    ows.record_event("instagram", n=2)
    ows.record_error("zalo", "bad_signature")
    snap = ows.snapshot()
    assert snap["instagram"]["events_total"] == 3
    assert snap["instagram"]["first_verify_ts"] > 0
    assert snap["zalo"]["error_total"] == 1
    assert snap["zalo"]["last_error_kind"] == "bad_signature"
    # 模拟重启：清内存、同一路径重载 —— 「历史上到达过」必须幸存
    ows.reset_for_tests(p)
    snap2 = ows.snapshot()
    assert snap2["instagram"]["events_total"] == 3
    assert snap2["instagram"]["first_verify_ts"] == snap["instagram"]["first_verify_ts"]
    assert snap2["zalo"]["last_error_kind"] == "bad_signature"


def test_verify_failure_counts_separately_from_success():
    ows.record_verify("messenger", ok=False)
    ows.record_verify("messenger", ok=False)
    snap = ows.snapshot()["messenger"]
    assert snap["verify_fail_total"] == 2
    assert snap["last_verify_ts"] == 0.0          # 失败不算成功握手
    assert snap["last_verify_fail_ts"] > 0


def test_record_functions_never_raise_on_garbage(tmp_path, monkeypatch):
    # 路径指向不可写位置（目录当文件用）也不许抛——webhook 主流程优先
    bad = tmp_path / "dir_as_file"
    bad.mkdir()
    ows.reset_for_tests(bad)   # write 会失败，但必须被吞掉
    ows.record_event("instagram")
    ows.record_verify(None, ok=True)     # 垃圾入参
    ows.record_error("", None)
    assert True   # 走到这里 = 没抛


def test_corrupt_ledger_tolerated(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json", encoding="utf-8")
    ows.reset_for_tests(p)
    assert ows.snapshot() == {}          # 坏文件按空台账，不炸
    ows.record_event("zalo")             # 还能继续记
    assert ows.snapshot()["zalo"]["events_total"] == 1


# ── creds_ok 与 register_* 条件对齐 ──────────────────────────────────────────

def test_creds_ok_mirrors_register_conditions():
    assert ows.creds_ok("instagram", {"instagram": {
        "page_access_token": "t", "app_secret": "s", "verify_token": "v"}})
    assert not ows.creds_ok("instagram", {"instagram": {
        "page_access_token": "t", "app_secret": "s"}})
    assert ows.creds_ok("zalo", {"zalo": {"access_token": "t"}})
    assert not ows.creds_ok("zalo", {"zalo": {}})
    assert ows.creds_ok("messenger", {"facebook_messenger": {
        "page_access_token": "t", "app_secret": "s", "verify_token": "v"}})
    assert ows.creds_ok("whatsapp", {"whatsapp_cloud": {
        "phone_number_id": "1", "access_token": "t",
        "app_secret": "s", "verify_token": "v"}})
    assert not ows.creds_ok("whatsapp", {"whatsapp_cloud": {
        "access_token": "t", "app_secret": "s", "verify_token": "v"}})


# ── collect_status ───────────────────────────────────────────────────────────

class _FakeState:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_collect_status_uses_app_state_mount_truth():
    cfg = {"instagram": {"enabled": True, "page_access_token": "t",
                         "app_secret": "s", "verify_token": "v",
                         "webhook_path": "/ig/webhook"}}
    # 配置看着齐全，但 app.state 没有挂载痕迹（启动后才填的凭证）→ not_mounted
    st = _FakeState()
    out = ows.collect_status(cfg, app_state=st)
    ig = next(p for p in out["platforms"] if p["platform"] == "instagram")
    assert ig["verdict"] == "not_mounted"
    # 挂载真相在 → never_reached（还没人打进来）
    st2 = _FakeState(ig_webhook_path="/ig/webhook")
    out2 = ows.collect_status(cfg, app_state=st2)
    ig2 = next(p for p in out2["platforms"] if p["platform"] == "instagram")
    assert ig2["verdict"] == "never_reached"


def test_collect_status_active_semantics():
    # 无启用渠道 + 无历史 → active=False（ops 卡整卡隐藏）
    out = ows.collect_status({}, app_state=None)
    assert out["active"] is False
    # 有历史（哪怕渠道已被关掉）→ active=True（曾经接过，值得继续展示）
    ows.record_event("zalo")
    out2 = ows.collect_status({}, app_state=None)
    assert out2["active"] is True


def test_collect_status_no_secret_fields():
    cfg = {"zalo": {"enabled": True, "access_token": "SECRET-TOKEN-X",
                    "oa_secret": "SECRET-Y"}}
    out = ows.collect_status(cfg, app_state=None)
    import json as _json
    blob = _json.dumps(out, ensure_ascii=False)
    assert "SECRET-TOKEN-X" not in blob and "SECRET-Y" not in blob


# ── 热挂载平台的配置推断（messenger 2026-08-10 起 register 不再按凭证 early-return）─

def test_collect_status_fallback_hot_mounted_messenger():
    """messenger 路由常驻挂载：app_state=None 的配置推断不得再出 not_mounted。

    「保存了一半凭证 → not_mounted（判词建议等重启）」是热挂载已消灭的状态；
    凭证不齐的真相由 creds_ok=False 表达，入站被拒会另记 no_app_secret 账。
    """
    cfg = {"facebook_messenger": {"enabled": True, "page_access_token": "t"}}
    out = ows.collect_status(cfg, app_state=None)
    fb = next(p for p in out["platforms"] if p["platform"] == "messenger")
    assert fb["creds_ok"] is False
    assert fb["verdict"] == "never_reached"   # 挂着、没人打进来；绝不是 not_mounted
    # 对照组：非热挂载平台（instagram）维持旧口径——凭证不齐＝register 不挂
    cfg2 = {"instagram": {"enabled": True, "page_access_token": "t"}}
    out2 = ows.collect_status(cfg2, app_state=None)
    ig = next(p for p in out2["platforms"] if p["platform"] == "instagram")
    assert ig["verdict"] == "not_mounted"


def test_messenger_no_app_secret_flows_to_auth_failing():
    """入站被 no_app_secret 拒收（热门控新增错误类）→ 台账 error → auth_failing。

    运维在状态里看到的处置指向是「补 App Secret」（last_error_kind），
    而不是误以为公网不可达或被恶意打。
    """
    ows.record_error("messenger", "no_app_secret")
    cfg = {"facebook_messenger": {"enabled": True, "page_access_token": "t"}}
    out = ows.collect_status(cfg, app_state=None)
    fb = next(p for p in out["platforms"] if p["platform"] == "messenger")
    assert fb["verdict"] == "auth_failing"
    assert fb["last_error_kind"] == "no_app_secret"


def test_inferred_mounted_single_source():
    """挂载推断唯一事实源：collect_status 回落与 selfcheck CLI 都必须走它
    （2026-08-10 曾因 CLI 各写一份 `enabled and creds` 而口径漂移）。"""
    assert ows.inferred_mounted("messenger", enabled=True, creds=False) is True
    assert ows.inferred_mounted("messenger", enabled=False, creds=True) is False
    assert ows.inferred_mounted("instagram", enabled=True, creds=False) is False
    assert ows.inferred_mounted("instagram", enabled=True, creds=True) is True


def test_selfcheck_cli_hot_mounted_messenger(tmp_path):
    """CLI 文件口径与模块同源：messenger 半配置不再误判 not_mounted（等重启）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "official_webhook_selfcheck_hotmount",
        _ROOT / "tools" / "official_webhook_selfcheck.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "config.yaml").write_text(
        "facebook_messenger:\n  enabled: true\n  page_access_token: t\n",
        encoding="utf-8")
    rep = mod.check_root(tmp_path)
    fb = next(p for p in rep["platforms"] if p["platform"] == "messenger")
    assert fb["creds_ok"] is False
    assert fb["verdict"] == "never_reached"   # 挂着、没人打进来；不是 not_mounted


# ── 埋点接线（静态，防悄悄退线）──────────────────────────────────────────────

@pytest.mark.parametrize("module,platform,expect_verify", [
    ("src/integrations/instagram_webhook.py", "instagram", True),
    ("src/integrations/zalo_webhook.py", "zalo", False),
    ("src/integrations/facebook_webhook.py", "messenger", True),
    ("src/integrations/whatsapp_cloud.py", "whatsapp", True),
    # QQ 机器人（2026-09-07）：POST op=13 回调验证 + Ed25519 事件验签，无 GET 握手
    ("src/integrations/qq_official.py", "qqbot", False),
])
def test_webhook_modules_wired(module, platform, expect_verify):
    src = (_ROOT / module).read_text(encoding="utf-8")
    assert f'record_event("{platform}")' in src, f"{module} 缺事件埋点"
    assert f'record_error("{platform}", "bad_signature")' in src, f"{module} 缺验签失败埋点"
    if expect_verify:
        assert f'record_verify("{platform}", ok=True)' in src, f"{module} 缺握手成功埋点"
        assert f'record_verify("{platform}", ok=False)' in src, f"{module} 缺握手失败埋点"


def test_platform_blocks_cover_all_wired_platforms():
    """埋点用的平台键必须都在 PLATFORM_CONFIG_BLOCKS（否则 collect 永远不展示它）。"""
    assert set(ows.PLATFORM_CONFIG_BLOCKS) == {"instagram", "zalo", "messenger", "whatsapp", "qqbot"}
    assert set(ows.HAS_GET_VERIFY) == set(ows.PLATFORM_CONFIG_BLOCKS)


# ── 路由 + ops 卡三件套 + i18n ───────────────────────────────────────────────

def test_route_registered_in_account_routes():
    src = (_ROOT / "src/web/routes/unified_inbox_account_routes.py").read_text(
        encoding="utf-8")
    assert '"/api/admin/official-webhook-status"' in src
    assert "collect_status(cfg, app_state=app.state)" in src


def test_ops_card_triple_registered():
    """卡片三件套：section 声明 / loader 读数据源 / 注册表登记（缺一=静默缺陷）。"""
    src = (_ROOT / "src/web/templates/ops_overview.html").read_text(encoding="utf-8")
    assert 'id="officialWhSection"' in src
    assert "async function loadOfficialWebhooks()" in src
    assert "/api/admin/official-webhook-status" in src
    assert "anchor:'officialWhKpis'" in src
    # 站内惯例：端点 404 / 无启用渠道 → 整卡隐藏
    assert "if(!d||!d.ok||!d.active){ opsHideCardEl(sec, 'offwh', 'disabled'); return; }" in src


def test_i18n_keys_bilingual():
    from src.web.i18n_packs.official_webhook_ops import EN, ZH

    keys = ("ov2_s_offwh", "ov2_ow_sub",
            "ov2_ow_verdict_live", "ov2_ow_verdict_handshake_only",
            "ov2_ow_verdict_auth_failing", "ov2_ow_verdict_never_reached",
            "ov2_ow_verdict_not_mounted", "ov2_ow_verdict_disabled",
            "ov2_ow_events", "ov2_ow_last_event", "ov2_ow_last_verify",
            "ov2_ow_errors", "ov2_ow_verify_fails", "ov2_ow_path", "ov2_ow_never",
            "ov2_ow_hint_never_reached", "ov2_ow_hint_handshake_only",
            "ov2_ow_hint_auth_failing", "ov2_ow_hint_not_mounted")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


# ── 接入向导内嵌回调状态条（setup_wizard.html，2026-08-07 消费面 #2）──────────

def test_setup_wizard_reach_strip_wired():
    """向导接线三件套：状态条容器 / 轮询器 / 数据源；缺一＝配后台时反馈静默失踪。

    另钉两个行为不变量：路由未装载时退 30s 慢轮询（等重启期间不能高频打 404）、
    仅 REACH_PLATS 四个官方 webhook 渠道注入（LINE official 无 webhook 入站，
    给它挂状态条＝永远「从未到达」的假故障）。
    """
    src = (_ROOT / "src/web/templates/setup_wizard.html").read_text(encoding="utf-8")
    assert 'data-reach-plat=' in src
    assert "async function swReachTick()" in src
    assert "/api/admin/official-webhook-status" in src
    assert "_swReachEvery=30000" in src           # 404 → 慢轮询
    # 2026-09-07 QQ 机器人加入（Webhook 模式有回调；WS 模式事件同记「到达」台账）
    assert "REACH_PLATS={instagram:1,zalo:1,messenger:1,whatsapp:1,qqbot:1}" in src
    # 判词渲染必须覆盖全部非 disabled 判词（漏一个=该状态下条子空白）
    for v in ("live", "handshake_only", "auth_failing", "not_mounted"):
        assert f"'{v}'" in src, f"缺判词分支 {v}"


def test_setup_wizard_reach_i18n_bilingual():
    from src.web.i18n_packs.setup_channels import EN, ZH

    keys = ("sw_reach_title", "sw_reach_live", "sw_reach_handshake",
            "sw_reach_never", "sw_reach_auth", "sw_reach_not_mounted",
            "sw_reach_path")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


# ── 进程外自检 CLI（tools/official_webhook_selfcheck.py，文件口径）────────────

def test_selfcheck_cli_file_scope(tmp_path):
    """CLI 的 check_root 纯文件口径：读实例根 config+台账，判词与模块同源。

    数据根解析必须走 _data_root 契约而非 CWD 猜（protocol_doctor 的引擎根
    CWD 病是前车之鉴）——这里用显式 root 验证纯函数部分。
    """
    import importlib.util
    import json
    spec = importlib.util.spec_from_file_location(
        "official_webhook_selfcheck",
        _ROOT / "tools" / "official_webhook_selfcheck.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "config.yaml").write_text(
        "instagram:\n  enabled: true\n  page_access_token: t\n"
        "  app_secret: s\n  verify_token: v\n", encoding="utf-8")
    # 无台账：enabled+creds → 文件口径 mounted 推断 → never_reached
    rep = mod.check_root(tmp_path)
    ig = next(p for p in rep["platforms"] if p["platform"] == "instagram")
    assert ig["verdict"] == "never_reached"
    assert rep["attention"] == ["instagram"]
    # 有台账（曾握手+来过事件）→ live；attention 清空
    (cfgdir / "official_webhook_state.json").write_text(
        json.dumps({"instagram": {"events_total": 5,
                                  "last_event_ts": time.time(),
                                  "last_verify_ts": time.time()}}),
        encoding="utf-8")
    rep2 = mod.check_root(tmp_path)
    ig2 = next(p for p in rep2["platforms"] if p["platform"] == "instagram")
    assert ig2["verdict"] == "live"
    assert rep2["attention"] == []
    # 坏台账软失败：不抛、按空台账
    (cfgdir / "official_webhook_state.json").write_text("{bad", encoding="utf-8")
    rep3 = mod.check_root(tmp_path)
    assert rep3["platforms"]
