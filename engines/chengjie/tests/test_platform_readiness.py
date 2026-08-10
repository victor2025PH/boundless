"""接入方式诊断门禁：「为什么不能用」必须说得准、且永远说得出。

事故背景（P1 前的实况）：`list_modes` 的不可用原因来自一张静态表，只有
`not_enabled` / `needs_server_setup` 两个值。于是三种处置完全不同的故障
在界面上长得一模一样——

- LINE 开了 protocol_enabled 但没装 okline（该去装依赖，不是去翻开关）
- Telegram 装了 pyrogram 但没填 api_id/api_hash（该去填凭据）
- WhatsApp 全开且 provider 已注册，但 Baileys sidecar 没起（该去起服务）

最后一条更糟：它 `available=true`，界面挂着「推荐」角标，用户点下去等二维码，
等来一句 service_down。

本门禁钉住两条不变量：
1. **准**——每种故障给出各自的原因码与可插值参数；
2. **兜底**——只要不可用，就必须给得出原因（绝不出现「点不动且不解释」）。
"""
from __future__ import annotations

import pytest

from src.integrations import line_protocol_login as lpl
from src.integrations import platform_readiness as pr
from src.integrations import telegram_protocol_login as tpl


def _codes(diag) -> list:
    return [b["code"] for b in diag["blockers"]]


def _params(diag, code) -> dict:
    for b in diag["blockers"]:
        if b["code"] == code:
            return b.get("params") or {}
    return {}


ON = {"platform_login": {"enabled": True}}


def _cfg(**platform_login) -> dict:
    base = {"enabled": True}
    base.update(platform_login)
    return {"platform_login": base}


# ── 全局闸 / device ────────────────────────────────────────────────────────

def test_login_disabled_blocks_everything():
    cfg = {"platform_login": {"enabled": False}}
    d = pr.diagnose_mode("telegram", "device", cfg, provider_registered=True)
    assert d["ready"] is False
    assert pr.BLOCK_LOGIN_DISABLED in _codes(d)


def test_device_mode_is_always_ready():
    d = pr.diagnose_mode("line", "device", ON, provider_registered=True)
    assert d["ready"] is True
    assert d["blockers"] == []


# ── Telegram ──────────────────────────────────────────────────────────────

def test_telegram_flag_off(monkeypatch):
    monkeypatch.setattr(tpl, "is_pyrogram_available", lambda: True)
    cfg = _cfg(telegram={"protocol_enabled": False},
               orchestrator_enabled=True)
    cfg["telegram"] = {"api_id": 1, "api_hash": "x"}
    d = pr.diagnose_mode("telegram", "protocol", cfg, provider_registered=False)
    assert _codes(d) == [pr.BLOCK_NOT_ENABLED]


def test_telegram_dep_missing_is_not_reported_as_not_enabled(monkeypatch):
    """开关明明开着却报「未启用」——正是这次要消灭的误导。"""
    monkeypatch.setattr(tpl, "is_pyrogram_available", lambda: False)
    cfg = _cfg(telegram={"protocol_enabled": True}, orchestrator_enabled=True)
    cfg["telegram"] = {"api_id": 1, "api_hash": "x"}
    d = pr.diagnose_mode("telegram", "protocol", cfg, provider_registered=False)
    assert _codes(d) == [pr.BLOCK_DEP_MISSING]
    assert _params(d, pr.BLOCK_DEP_MISSING)["dep"] == "pyrogram"


def test_telegram_creds_missing(monkeypatch):
    monkeypatch.setattr(tpl, "is_pyrogram_available", lambda: True)
    cfg = _cfg(telegram={"protocol_enabled": True}, orchestrator_enabled=True)
    d = pr.diagnose_mode("telegram", "protocol", cfg, provider_registered=False)
    assert _codes(d) == [pr.BLOCK_CREDS_MISSING]


def test_telegram_credpool_replaces_own_credentials(monkeypatch):
    """开了中央凭据池就不该再报缺 api_id——那正是「新用户免申请」的设计。"""
    monkeypatch.setattr(tpl, "is_pyrogram_available", lambda: True)
    cfg = _cfg(telegram={"protocol_enabled": True,
                         "credpool": {"enabled": True}},
               orchestrator_enabled=True)
    d = pr.diagnose_mode("telegram", "protocol", cfg, provider_registered=True)
    assert d["ready"] is True
    assert d["blockers"] == []


# ── LINE ──────────────────────────────────────────────────────────────────

def test_line_missing_okline_says_dep_missing(monkeypatch):
    monkeypatch.setattr(lpl, "is_okline_available", lambda: False)
    cfg = _cfg(line={"protocol_enabled": True}, orchestrator_enabled=True)
    d = pr.diagnose_mode("line", "protocol", cfg, provider_registered=False)
    assert _codes(d) == [pr.BLOCK_DEP_MISSING]
    p = _params(d, pr.BLOCK_DEP_MISSING)
    assert p["dep"] == "okline" and "pip install" in p["install"]


def test_line_flag_off_and_dep_missing_reports_both(monkeypatch):
    """两件事都没做就都说出来，别让运维修完一个再来一轮。"""
    monkeypatch.setattr(lpl, "is_okline_available", lambda: False)
    cfg = _cfg(line={"protocol_enabled": False}, orchestrator_enabled=True)
    d = pr.diagnose_mode("line", "protocol", cfg, provider_registered=False)
    assert set(_codes(d)) == {pr.BLOCK_NOT_ENABLED, pr.BLOCK_DEP_MISSING}


def test_line_ready_when_dep_present(monkeypatch):
    monkeypatch.setattr(lpl, "is_okline_available", lambda: True)
    cfg = _cfg(line={"protocol_enabled": True}, orchestrator_enabled=True)
    d = pr.diagnose_mode("line", "protocol", cfg, provider_registered=True)
    assert d["ready"] is True and d["blockers"] == []


# ── sidecar 平台（WhatsApp / Messenger）────────────────────────────────────

def test_whatsapp_sidecar_down_is_caught_before_the_click():
    cfg = _cfg(whatsapp={"protocol_enabled": True,
                         "baileys_url": "http://127.0.0.1:8790"},
               orchestrator_enabled=True)
    d = pr.diagnose_mode("whatsapp", "protocol", cfg,
                         provider_registered=True, service_ok=False)
    assert d["ready"] is False
    assert _codes(d) == [pr.BLOCK_SERVICE_DOWN]
    assert _params(d, pr.BLOCK_SERVICE_DOWN)["url"].endswith("8790")


def test_whatsapp_unknown_reachability_does_not_block():
    """没探过 ≠ 挂了。静态层（不触网）不许把未知说成故障。"""
    cfg = _cfg(whatsapp={"protocol_enabled": True}, orchestrator_enabled=True)
    d = pr.diagnose_mode("whatsapp", "protocol", cfg,
                         provider_registered=True, service_ok=None)
    assert d["ready"] is True


def test_messenger_flag_off_is_needs_server_setup_not_not_enabled():
    """Messenger 不是「打开开关就能用」，还要在服务器上人工登录一次。"""
    cfg = _cfg(messenger={"web_enabled": False}, orchestrator_enabled=True)
    d = pr.diagnose_mode("messenger", "web", cfg, provider_registered=False)
    assert _codes(d) == [pr.BLOCK_NEEDS_SERVER_SETUP]


def test_messenger_service_down():
    cfg = _cfg(messenger={"web_enabled": True,
                          "web_url": "http://127.0.0.1:8791"},
               orchestrator_enabled=True)
    d = pr.diagnose_mode("messenger", "web", cfg,
                         provider_registered=True, service_ok=False)
    assert _codes(d) == [pr.BLOCK_SERVICE_DOWN]
    assert _params(d, pr.BLOCK_SERVICE_DOWN)["svc"] == "messenger-web"


# ── 兜底不变量 ────────────────────────────────────────────────────────────

def test_unimplemented_mode_still_explains():
    """压根没实现的组合必须给得出原因，且码是 not_implemented 而非 not_enabled。

    2026-08-11 分码：not_enabled 的处置文案是「翻开关/填凭据」，对「功能不存在」
    全是空头支票（实录：用户照指引找一个不存在的表单）。「没做」必须诚实说没做，
    前端据此隐藏「重新检测」并给「改用可用方式」直达键。
    """
    for platform, mode in (("telegram", "web"), ("whatsapp", "web")):
        d = pr.diagnose_mode(platform, mode, ON, provider_registered=False)
        assert d["ready"] is False
        assert _codes(d) == [pr.BLOCK_NOT_IMPLEMENTED]
        assert d["reason_code"] == pr.BLOCK_NOT_IMPLEMENTED


@pytest.mark.parametrize("platform,mode", [
    ("telegram", "protocol"), ("telegram", "web"),
    ("line", "protocol"), ("whatsapp", "protocol"),
    ("whatsapp", "web"), ("messenger", "web"),
])
def test_never_unavailable_without_a_reason(platform, mode, monkeypatch):
    monkeypatch.setattr(tpl, "is_pyrogram_available", lambda: False)
    monkeypatch.setattr(lpl, "is_okline_available", lambda: False)
    d = pr.diagnose_mode(platform, mode, ON, provider_registered=False)
    hard = [b for b in d["blockers"] if b["severity"] == pr.SEV_BLOCK]
    assert hard, f"{platform}/{mode} 不可用却没给原因"
    assert d["reason_code"] == hard[0]["code"]


def test_unknown_registration_does_not_fabricate_a_problem(monkeypatch):
    """CLI / 后台进程不注册 provider —— 那里 provider_registered 无从得知（None）。

    早期版本据「未注册」兜底报 not_enabled，于是 protocol_doctor 把依赖齐全、开关已开
    的 Telegram 报成「未启用」——正是本模块要消灭的那种误导，却由本模块自己造了一遍。
    """
    monkeypatch.setattr(tpl, "is_pyrogram_available", lambda: True)
    cfg = _cfg(telegram={"protocol_enabled": True}, orchestrator_enabled=True)
    cfg["telegram"] = {"api_id": 1, "api_hash": "x"}
    d = pr.diagnose_mode("telegram", "protocol", cfg)          # 不传 provider_registered
    assert d["ready"] is True and d["blockers"] == []


def test_known_unregistered_reports_provider_unavailable(monkeypatch):
    """web 进程里明确知道没装载：开关依赖全对却起不来 → 提示重启，而不是「未启用」。"""
    monkeypatch.setattr(tpl, "is_pyrogram_available", lambda: True)
    cfg = _cfg(telegram={"protocol_enabled": True}, orchestrator_enabled=True)
    cfg["telegram"] = {"api_id": 1, "api_hash": "x"}
    d = pr.diagnose_mode("telegram", "protocol", cfg, provider_registered=False)
    assert _codes(d) == [pr.BLOCK_PROVIDER_UNAVAILABLE]
    assert d["ready"] is False


def test_diagnosis_never_raises_on_garbage_config():
    """诊断失败也绝不能把登录弹窗弄崩。"""
    for bad in ({}, {"platform_login": None}, {"platform_login": {"telegram": None}}):
        d = pr.diagnose_mode("telegram", "protocol", bad, provider_registered=False)
        assert isinstance(d["blockers"], list)


# ── 编排器警告（能扫上但不会常驻在线）──────────────────────────────────────

def test_orchestrator_off_is_warn_not_block(monkeypatch):
    monkeypatch.setattr(lpl, "is_okline_available", lambda: True)
    cfg = _cfg(line={"protocol_enabled": True}, orchestrator_enabled=False)
    d = pr.diagnose_mode("line", "protocol", cfg, provider_registered=True)
    assert d["ready"] is True          # 不阻断
    assert _codes(d) == [pr.WARN_ORCHESTRATOR_OFF]
    assert d["reason_code"] == ""      # warn 不占 reason_code


# ── 探测目标选择 ──────────────────────────────────────────────────────────

def test_probe_targets_skip_disabled_services():
    """没开的功能不去探它的端口：白等一次超时，还会误报成「服务挂了」。"""
    assert pr.service_probe_targets(_cfg()) == {}
    t = pr.service_probe_targets(_cfg(whatsapp={"protocol_enabled": True}))
    assert set(t) == {"whatsapp"} and t["whatsapp"].startswith("http")
    t2 = pr.service_probe_targets(
        _cfg(whatsapp={"protocol_enabled": True}, messenger={"web_enabled": True}))
    assert set(t2) == {"whatsapp", "messenger"}


# ── 整平台聚合 ────────────────────────────────────────────────────────────

def test_diagnose_platform_ready_if_any_mode_works(monkeypatch):
    monkeypatch.setattr(lpl, "is_okline_available", lambda: False)
    cfg = _cfg(line={"protocol_enabled": True}, orchestrator_enabled=True)
    out = pr.diagnose_platform(
        "line", ["protocol", "device"], cfg,
        provider_registered_fn=lambda p, m: m == "device")
    assert out["ready"] is True                       # device 兜底
    assert out["modes"]["protocol"]["ready"] is False
    assert out["modes"]["device"]["ready"] is True
