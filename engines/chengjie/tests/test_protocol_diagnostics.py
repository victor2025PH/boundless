"""M6③：协议栈联调自检（readiness 报告）的单元测试。"""

from __future__ import annotations

from src.integrations import account_self_profile as asp
from src.integrations import protocol_bridge as pb
from src.integrations import protocol_diagnostics as pd
from src.integrations import telegram_protocol_login as tpl


def test_static_empty_config_not_ready():
    rep = pd.readiness_static({})
    assert rep["platform_login_enabled"] is False
    assert rep["telegram"]["ready"] is False
    assert rep["whatsapp"]["mode_enabled"] is False
    assert rep["overall_ready"] is False
    assert rep["inbox_ingest"]["sink_registered"] in (True, False)


def test_static_telegram_ready(monkeypatch):
    monkeypatch.setattr(tpl, "is_pyrogram_available", lambda: True)
    pb.register_inbox_sink(lambda m: None)
    try:
        cfg = {
            "platform_login": {"enabled": True,
                               "telegram": {"protocol_enabled": True}},
            "telegram": {"api_id": 123, "api_hash": "abc"},
        }
        rep = pd.readiness_static(cfg)
        assert rep["telegram"]["ready"] is True
        assert rep["telegram"]["credentials"] is True
        assert rep["telegram"]["pyrogram_available"] is True
        assert rep["inbox_ingest"]["sink_registered"] is True
        assert rep["overall_ready"] is True
    finally:
        pb.register_inbox_sink(None)


def test_static_telegram_missing_creds(monkeypatch):
    monkeypatch.setattr(tpl, "is_pyrogram_available", lambda: True)
    cfg = {"platform_login": {"enabled": True,
                              "telegram": {"protocol_enabled": True}}}
    rep = pd.readiness_static(cfg)
    assert rep["telegram"]["ready"] is False
    assert any("api_id" in h for h in rep["telegram"]["hints"])


def test_static_overall_requires_sink(monkeypatch):
    monkeypatch.setattr(tpl, "is_pyrogram_available", lambda: True)
    pb.register_inbox_sink(None)  # 无 sink
    cfg = {
        "platform_login": {"enabled": True,
                           "telegram": {"protocol_enabled": True}},
        "telegram": {"api_id": 1, "api_hash": "x"},
    }
    rep = pd.readiness_static(cfg)
    assert rep["telegram"]["ready"] is True
    assert rep["overall_ready"] is False  # sink 未注册 → 整体未就绪


async def test_readiness_whatsapp_reachable(monkeypatch):
    async def _ok(_cfg):
        return True
    monkeypatch.setattr(pd, "check_whatsapp_reachable", _ok)
    pb.register_inbox_sink(lambda m: None)
    try:
        cfg = {"platform_login": {"enabled": True,
                                  "whatsapp": {"protocol_enabled": True}}}
        rep = await pd.readiness(cfg)
        assert rep["whatsapp"]["service_reachable"] is True
        assert rep["whatsapp"]["ready"] is True
        assert rep["overall_ready"] is True
    finally:
        pb.register_inbox_sink(None)


async def test_readiness_whatsapp_unreachable(monkeypatch):
    async def _no(_cfg):
        return False
    monkeypatch.setattr(pd, "check_whatsapp_reachable", _no)
    pb.register_inbox_sink(lambda m: None)
    try:
        cfg = {"platform_login": {"enabled": True,
                                  "whatsapp": {"protocol_enabled": True}}}
        rep = await pd.readiness(cfg)
        assert rep["whatsapp"]["service_reachable"] is False
        assert rep["whatsapp"]["ready"] is False
        assert rep["overall_ready"] is False
        assert any("不可达" in h for h in rep["whatsapp"]["hints"])
    finally:
        pb.register_inbox_sink(None)


def test_format_report_renders():
    rep = pd.readiness_static({})
    text = pd.format_report(rep)
    assert "协议栈整体就绪" in text
    assert "Telegram protocol" in text
    assert "WhatsApp Baileys" in text


def test_report_carries_four_platform_matrix():
    """LINE / Messenger 也要进报告——只覆盖 TG/WA 的自检答不了「今天能不能接 LINE」。"""
    mtx = pd.readiness_static({}).get("platforms") or {}
    assert {"telegram", "line", "whatsapp", "messenger"} <= set(mtx)
    for plat, pr in mtx.items():
        assert "ready" in pr and pr.get("modes"), plat
        for mode, d in pr["modes"].items():
            assert set(d) >= {"ready", "blockers", "reason_code"}, (plat, mode)


def test_doctor_cli_loads_the_same_config_as_the_app():
    """CLI 必须走 ConfigManager（主配置 + config.local.yaml overlay 深合并）。

    旧实现只 safe_load 主 config.yaml，而**所有运营开关都写在 overlay**：本机 overlay
    开着 whatsapp.protocol_enabled，doctor 却一直报「未启用」。会说谎的诊断比没有诊断
    更糟——它把人引向错误的修复方向。
    （用 ast 静态校验：`scripts.protocol_doctor` 在 import 期就 os.chdir(ROOT)，
    直接 import 会污染整个测试进程的 cwd。）
    """
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts" / "protocol_doctor.py"
           ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_load_config"), None)
    assert fn is not None, "protocol_doctor 里找不到 _load_config"
    body = ast.dump(fn)
    assert "ConfigManager" in body, (
        "_load_config 必须经 ConfigManager 装载（含 config.local.yaml overlay 合并）"
    )


# ── 账号身份就绪度（self_profile）自检 ───────────────────────────────────────

def _sp_stats(**kw):
    """构造 self_profile 计数 dict（默认全 0，按需覆盖）。"""
    base = {"calls": 0, "written": 0, "skipped": 0,
            "avatar_downloaded": 0, "avatar_reused": 0, "errors": 0}
    base.update(kw)
    return base


_SP_ON_CFG = {"accounts": {"self_profile": {"enabled": True}}}


def test_self_profile_flag_off_ready(monkeypatch):
    monkeypatch.setattr(asp, "get_self_profile_stats", lambda: _sp_stats())
    rep = pd._self_profile_report({})
    assert rep["flag_enabled"] is False
    assert rep["avatar_enabled"] is False
    assert rep["ready"] is True  # 未启用不算故障
    assert any("未启用" in h for h in rep["hints"])


def test_self_profile_flag_on_never_triggered(monkeypatch):
    monkeypatch.setattr(asp, "get_self_profile_stats", lambda: _sp_stats())
    rep = pd._self_profile_report(_SP_ON_CFG)
    assert rep["flag_enabled"] is True
    assert rep["ready"] is False
    assert any("从未触发" in h for h in rep["hints"])


def test_self_profile_flag_on_written_ready(monkeypatch):
    monkeypatch.setattr(
        asp, "get_self_profile_stats",
        lambda: _sp_stats(calls=3, written=2, skipped=1))
    rep = pd._self_profile_report(_SP_ON_CFG)
    assert rep["ready"] is True
    assert any("就绪" in h for h in rep["hints"])
    assert any("written=2" in h for h in rep["hints"])


def test_self_profile_flag_on_persistent_errors(monkeypatch):
    monkeypatch.setattr(
        asp, "get_self_profile_stats",
        lambda: _sp_stats(calls=5, errors=5))
    rep = pd._self_profile_report(_SP_ON_CFG)
    assert rep["ready"] is False
    assert any("富集持续失败" in h for h in rep["hints"])
    assert any("errors=5" in h for h in rep["hints"])


def test_self_profile_avatar_hint_when_never_downloaded(monkeypatch):
    monkeypatch.setattr(
        asp, "get_self_profile_stats",
        lambda: _sp_stats(calls=2, written=2))
    cfg = {"accounts": {"self_profile": {"enabled": True, "avatar": True}}}
    rep = pd._self_profile_report(cfg)
    assert rep["ready"] is True
    assert rep["avatar_enabled"] is True
    assert any("头像子开关" in h for h in rep["hints"])


def test_self_profile_stats_read_failure_falls_back(monkeypatch):
    def _boom():
        raise RuntimeError("stats unavailable")
    monkeypatch.setattr(asp, "get_self_profile_stats", _boom)
    rep = pd._self_profile_report(_SP_ON_CFG)
    assert rep["stats"] == _sp_stats()  # 回退空计数 dict
    assert rep["ready"] is False
    assert any("从未触发" in h for h in rep["hints"])


def test_static_self_profile_does_not_affect_overall(monkeypatch):
    """readiness_static 含 self_profile 键，且 overall_ready 不受其就绪度影响。"""
    monkeypatch.setattr(tpl, "is_pyrogram_available", lambda: True)
    monkeypatch.setattr(asp, "get_self_profile_stats", lambda: _sp_stats())
    pb.register_inbox_sink(lambda m: None)
    try:
        base = {
            "platform_login": {"enabled": True,
                               "telegram": {"protocol_enabled": True}},
            "telegram": {"api_id": 1, "api_hash": "x"},
        }
        rep_off = pd.readiness_static(base)
        rep_on = pd.readiness_static(
            {**base, "accounts": {"self_profile": {"enabled": True}}})
        assert "self_profile" in rep_off and "self_profile" in rep_on
        assert rep_off["self_profile"]["ready"] is True   # flag 关 → 无需就绪
        assert rep_on["self_profile"]["ready"] is False   # flag 开但从未富集
        # 增强项不参与整体判定：两种情况 overall_ready 一致且仍为 True
        assert rep_off["overall_ready"] is True
        assert rep_on["overall_ready"] is True
    finally:
        pb.register_inbox_sink(None)


def test_format_report_renders_self_profile(monkeypatch):
    monkeypatch.setattr(asp, "get_self_profile_stats", lambda: _sp_stats())
    text = pd.format_report(pd.readiness_static({}))
    assert "账号身份采集(self_profile)" in text
    assert "未启用账号身份采集" in text
