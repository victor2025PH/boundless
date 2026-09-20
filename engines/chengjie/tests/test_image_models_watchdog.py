# -*- coding: utf-8 -*-
"""出图模型「被删」哨兵门禁（P3 2026-08-22）。

事故：176 的 ComfyUI 模型目录被整树清空 → 所有现场生图 400，从删除到被发现
隔了数小时（唯一暴露面=坐席面板里一段被截断的乱码）。本哨兵把「模型清单
从有到无」变成主动告警。重点覆盖**不该告警**的路径（新装机/不可达/关闸）
——误报会让运维把它当狼来了。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from src.inbox import health_watchdog as hw
from src.web.routes import image_gen_routes as igr

_ARGS = ["python", "tools/comfy_infer.py", "--url", "http://192.168.0.176:8188",
         "--prompt", "{prompt}", "--out", "{out}"]


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


def _wd(monkeypatch, models, *, enabled=True, selfie_on=True):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    monkeypatch.setattr(igr, "probe_comfy_models", lambda url: models)
    conf: Dict[str, Any] = {
        "health_watchdog": {"image_models_watch": {"enabled": enabled}},
        "companion": {"selfie": {"enabled": selfie_on,
                                 "provider": {"command_args": list(_ARGS)}}},
    }
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._config_manager = SimpleNamespace(config=conf)
    return w, bus


_OK = {"ckpts": ["flux1-dev-fp8.safetensors"], "unets": []}
_GONE = {"ckpts": [], "unets": []}


def test_never_seen_ok_stays_silent(monkeypatch):
    """新装机/从未部署：清单空也不告警（否则 dev 环境天天误报）。"""
    w, bus = _wd(monkeypatch, _GONE)
    w._check_image_models(now=1000.0)
    assert bus.events == []


def test_vanish_after_seen_ok_alerts_once_then_reminds(monkeypatch):
    w, bus = _wd(monkeypatch, _OK)
    w._check_image_models(now=1000.0)          # 见过好状态
    assert bus.events == []
    monkeypatch.setattr(igr, "probe_comfy_models", lambda url: _GONE)
    w._check_image_models(now=2000.0)          # 消失 → 首提
    assert len(bus.events) == 1
    name, payload = bus.events[0]
    assert name == "image_models_alert"
    assert payload["ckpts"] == 0 and payload["reminder"] is False
    assert payload["rate_key"] == "image_models:remind"
    w._check_image_models(now=2600.0)          # interval 内不重提
    assert len(bus.events) == 1
    w._check_image_models(now=2000.0 + 241 * 60)  # 超 interval → 重提
    assert len(bus.events) == 2
    assert bus.events[1][1]["reminder"] is True


def test_recovery_emits_notice_and_resets(monkeypatch):
    w, bus = _wd(monkeypatch, _OK)
    w._check_image_models(now=1000.0)
    monkeypatch.setattr(igr, "probe_comfy_models", lambda url: _GONE)
    w._check_image_models(now=2000.0)
    monkeypatch.setattr(igr, "probe_comfy_models", lambda url: _OK)
    w._check_image_models(now=3000.0)
    names = [n for n, _ in bus.events]
    assert names == ["image_models_alert", "image_models_alert"]
    assert bus.events[-1][1].get("recovered") is True
    # 未告警状态下的正常巡检不再发任何事件
    w._check_image_models(now=4000.0)
    assert len(bus.events) == 2


def test_unreachable_probe_is_silent(monkeypatch):
    """整机不可达 ≠ 模型被删（服务活性归 176 侧看门狗），本哨兵不猜不报。"""
    w, bus = _wd(monkeypatch, _OK)
    w._check_image_models(now=1000.0)
    monkeypatch.setattr(igr, "probe_comfy_models", lambda url: None)
    w._check_image_models(now=2000.0)
    assert bus.events == []


def test_disabled_switch_and_selfie_off_silent(monkeypatch):
    w, bus = _wd(monkeypatch, _GONE, enabled=False)
    w._imgm_seen_ok = True
    w._check_image_models(now=1000.0)
    assert bus.events == []
    w2, bus2 = _wd(monkeypatch, _GONE, selfie_on=False)
    w2._imgm_seen_ok = True
    w2._check_image_models(now=1000.0)
    assert bus2.events == []


def test_no_command_args_silent(monkeypatch):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    conf = {"health_watchdog": {}, "companion": {"selfie": {"enabled": True,
                                                            "provider": {}}}}
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._config_manager = SimpleNamespace(config=conf)
    w._check_image_models(now=1000.0)
    assert bus.events == []
