# -*- coding: utf-8 -*-
"""AvatarHub 救援链（计划任务）活性探测门禁（2026-08-14 事故沉淀）。

事故：7852 掉线 9 小时无自愈——EmotionTTS_Boot / EmotionTTSWatchdog 都被
Disabled（集群代码模式联动停用后没恢复），而 avatar_voice_alert 只说
「服务掉线」，隐含「看门狗会拉起来」的误导。本门禁钉住：
  - XML 解析只认 Settings/Enabled（Trigger 里的同名标签不得混入）；
  - broken 清单只收 disabled/missing（unknown=探测失败不构成断言）；
  - watchdog down 告警携带 rescue_broken；hang 告警不探（服务活着）；
  - webhook 文案在救援链断裂时把「等看门狗」指引换成「需人工恢复任务」。
"""
from __future__ import annotations

import time

from src.ai.avatar_voice_rescue import (
    DEFAULT_RESCUE_TASKS,
    broken_rescue_tasks,
    parse_task_enabled,
    resolve_rescue_task_names,
)

_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _task_xml(settings_enabled: str = "", trigger_enabled: str = "true") -> str:
    """schtasks /XML 形状的最小夹具（带真实命名空间）。"""
    settings_line = (
        f"<Enabled>{settings_enabled}</Enabled>" if settings_enabled else "")
    return (
        f'<Task xmlns="{_NS}">'
        f"<Triggers><CalendarTrigger>"
        f"<Enabled>{trigger_enabled}</Enabled>"
        f"</CalendarTrigger></Triggers>"
        f"<Settings>{settings_line}<Hidden>false</Hidden></Settings>"
        f"</Task>"
    )


# ── parse_task_enabled ───────────────────────────────────────────────────────
def test_parse_enabled_true_false():
    assert parse_task_enabled(_task_xml("true")) is True
    assert parse_task_enabled(_task_xml("false")) is False
    assert parse_task_enabled(_task_xml("FALSE")) is False


def test_parse_settings_without_enabled_defaults_true():
    # Settings 无 Enabled 子节点 = 计划任务默认启用
    assert parse_task_enabled(_task_xml("")) is True


def test_parse_trigger_enabled_not_confused_with_settings():
    # Trigger 的 Enabled=false（触发器停用）≠ 任务本体停用——不得误判
    assert parse_task_enabled(_task_xml("true", trigger_enabled="false")) is True


def test_parse_bad_xml_and_missing_settings_return_none():
    assert parse_task_enabled("") is None
    assert parse_task_enabled("<not-xml") is None
    assert parse_task_enabled(f'<Task xmlns="{_NS}"><Actions/></Task>') is None


# ── broken_rescue_tasks ──────────────────────────────────────────────────────
def test_broken_list_only_disabled_and_missing_sorted():
    states = {
        "EmotionTTSWatchdog": "disabled",
        "EmotionTTS_Boot": "missing",
        "SomethingElse": "enabled",
        "Flaky": "unknown",
    }
    # ASCII 序：'W'(87) < '_'(95) → Watchdog 排前
    assert broken_rescue_tasks(states) == ["EmotionTTSWatchdog", "EmotionTTS_Boot"]
    assert broken_rescue_tasks({}) == []
    assert broken_rescue_tasks(None) == []


# ── resolve_rescue_task_names ────────────────────────────────────────────────
def test_resolve_task_names_default_override_and_optout():
    assert resolve_rescue_task_names({}) == list(DEFAULT_RESCUE_TASKS)
    assert resolve_rescue_task_names(None) == list(DEFAULT_RESCUE_TASKS)
    cfg = {"avatar_voice": {"rescue_tasks": ["MyBoot", " MyDog "]}}
    assert resolve_rescue_task_names(cfg) == ["MyBoot", "MyDog"]
    # 显式空列表 = 关闭探测（无这两个任务的部署形态的逃生门）
    assert resolve_rescue_task_names({"avatar_voice": {"rescue_tasks": []}}) == []
    # 坏形回默认
    assert resolve_rescue_task_names(
        {"avatar_voice": {"rescue_tasks": "oops"}}) == list(DEFAULT_RESCUE_TASKS)


# ── watchdog 接线：down 告警携带 rescue_broken ───────────────────────────────
class _FakeBus:
    def __init__(self):
        self.events = []

    def publish(self, etype, data):
        self.events.append((etype, data))


def _watchdog(cfg: dict):
    from src.inbox.health_watchdog import HealthWatchdog

    class _CM:
        config = cfg

    class _App:
        class state:
            pass

    return HealthWatchdog(app=_App(), config_manager=_CM())


def test_watchdog_down_alert_carries_rescue_broken(monkeypatch):
    from src.inbox import health_watchdog as hw

    bus = _FakeBus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    monkeypatch.setattr(
        "src.ai.avatar_voice_rescue.probe_rescue_tasks",
        lambda names, **kw: {n: "disabled" for n in names})
    down = {"reachable": False, "models_loaded": False,
            "url": "http://127.0.0.1:7852/health", "error": "conn refused"}
    monkeypatch.setattr(hw, "probe_avatar_voice", lambda cfg, **kw: down)

    wd = _watchdog({"health_watchdog": {}})
    t0 = time.time()
    wd._check_avatar_voice(now=t0)               # 建立 down_since
    wd._check_avatar_voice(now=t0 + 31 * 60)     # ≥30min → 首提
    assert len(bus.events) == 1
    etype, data = bus.events[0]
    assert etype == "avatar_voice_alert"
    assert data["rescue_broken"] == sorted(DEFAULT_RESCUE_TASKS)


def test_watchdog_down_alert_remote_target_skips_rescue_probe(monkeypatch):
    # 探针目标在远端主机（如 140:7852）→ 本机 schtasks 与它无关：告警照发，
    # 但绝不点名本机救援任务（本地 TTS 退役后 Boot/Watchdog 停用是刻意状态）
    from src.inbox import health_watchdog as hw

    bus = _FakeBus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)

    # 注意：不能用「mock 里 raise」验证——生产代码把探测包在 try/except 里，
    # 异常会被吞成 rescue_broken=[]，坏实现也能骗绿。用调用记录当铁证。
    called: list = []

    def _record_probe(names, **kw):
        called.append(list(names))
        return {n: "disabled" for n in names}

    monkeypatch.setattr(
        "src.ai.avatar_voice_rescue.probe_rescue_tasks", _record_probe)
    down = {"reachable": False, "models_loaded": False,
            "url": "http://192.168.0.140:7852/health", "error": "conn refused"}
    monkeypatch.setattr(hw, "probe_avatar_voice", lambda cfg, **kw: down)

    wd = _watchdog({"health_watchdog": {}})
    t0 = time.time()
    wd._check_avatar_voice(now=t0)
    wd._check_avatar_voice(now=t0 + 31 * 60)
    assert called == []            # 本机任务探测不得发生
    assert len(bus.events) == 1    # 告警本体照发
    assert bus.events[0][1]["rescue_broken"] == []


def test_avatar_probe_host_is_local():
    from src.inbox.health_watchdog import avatar_probe_host_is_local
    assert avatar_probe_host_is_local("http://127.0.0.1:7852/health")
    assert avatar_probe_host_is_local("http://localhost:7852/health")
    assert not avatar_probe_host_is_local("http://192.168.0.140:7852/health")
    assert not avatar_probe_host_is_local("")
    assert not avatar_probe_host_is_local(None)


def test_watchdog_down_alert_rescue_probe_failure_is_silent(monkeypatch):
    # 探测抛异常 → rescue_broken 空列表，告警本体照发（探测失败绝不拦告警）
    from src.inbox import health_watchdog as hw

    bus = _FakeBus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)

    def _boom(*a, **kw):
        raise RuntimeError("no schtasks here")

    monkeypatch.setattr("src.ai.avatar_voice_rescue.probe_rescue_tasks", _boom)
    down = {"reachable": False, "models_loaded": False,
            "url": "http://127.0.0.1:7852/health", "error": "conn refused"}
    monkeypatch.setattr(hw, "probe_avatar_voice", lambda cfg, **kw: down)

    wd = _watchdog({"health_watchdog": {}})
    t0 = time.time()
    wd._check_avatar_voice(now=t0)
    wd._check_avatar_voice(now=t0 + 31 * 60)
    assert len(bus.events) == 1
    assert bus.events[0][1]["rescue_broken"] == []


# ── webhook 文案 ─────────────────────────────────────────────────────────────
def test_webhook_message_rescue_broken_overrides_fix_line():
    from src.inbox.webhook_notifier import _build_message

    t, x = _build_message("avatar_voice_alert", {
        "reachable": False, "down_minutes": 540,
        "url": "http://127.0.0.1:7852/health", "error": "conn refused",
        "rescue_broken": ["EmotionTTS_Boot", "EmotionTTSWatchdog"]})
    assert "掉线" in t
    # 告警卡 v2 文案（c26afdc6 运维群降噪线）：「救援链已停用 … /ENABLE」改为
    # 「自动救援已停用 … 重新启用这些任务」；断言跟文案走，语义不变——
    # 救援链死时必须点名任务并指引恢复任务本体
    assert "自动救援已停用" in x
    assert "EmotionTTS_Boot" in x and "EmotionTTSWatchdog" in x
    assert "重新启用" in x
    # 默认「先等自动拉起 / 手动拉起计划任务」指引必须被替换（它此时是误导）
    assert "请检查 emotion_tts 服务或手动拉起计划任务" not in x
    assert "先等自动拉起" not in x

    # 救援链正常（空列表）→ 旧指引原样保留
    t2, x2 = _build_message("avatar_voice_alert", {
        "reachable": False, "down_minutes": 65,
        "url": "http://127.0.0.1:7852/health", "error": "conn refused",
        "rescue_broken": []})
    assert "EmotionTTS_Boot" in x2 and "救援链已停用" not in x2
