# -*- coding: utf-8 -*-
"""P2-198：web 请求检查点触发配置热重载（桌面版的唯一热重载通道）。

背景：`check_and_hot_reload` 有两个触发点——Telegram 消息循环 + web 中间件
检查点。生产机（有 TG 循环）永远兜得住，**桌面版没有 TG 循环**，全靠中间件；
它坏了的症状是「改 config.local.yaml 永不生效，必须重启」——正是 198 实测
（2026-07-31 两次改 overlay + 打请求，日志零反应）。本测试在进程内钉死这条链：
建真 app → 请求 → 改 overlay → 过节流窗 → 请求 → 配置必须已更新。
"""
from __future__ import annotations

import asyncio

import pytest
import yaml
from fastapi.testclient import TestClient

from src.utils.config_manager import ConfigManager
from src.web.admin import create_app


@pytest.fixture
def cm_app(tmp_path):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(
        yaml.dump({"greeting": ["hi"]}), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump({"channels": {}}), encoding="utf-8")
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cm.load())
    finally:
        loop.close()
    return cm, create_app(cm), tmp_path


def test_web_checkpoint_reloads_overlay_change(cm_app):
    cm, app, tmp_path = cm_app
    client = TestClient(app)

    # 第一发请求：走一遍中间件（建立 _last_hot_reload_check 基线）
    r = client.get("/login")
    assert r.status_code == 200

    # 改 overlay（新增无害键）
    overlay = tmp_path / "config.local.yaml"
    overlay.write_text(
        yaml.dump({"companion": {"quality_trend": {"enabled": False}}},
                  allow_unicode=True),
        encoding="utf-8")
    assert (
        (cm.config.get("companion") or {}).get("quality_trend") is None
    ), "写盘不应立即生效（尚未过检查点）"

    # 绕过 30s 节流窗（测试不真等半分钟）
    cm._last_hot_reload_check = 0.0

    r = client.get("/login")
    assert r.status_code == 200

    qt = ((cm.config.get("companion") or {}).get("quality_trend") or {})
    assert qt.get("enabled") is False, (
        "web 请求检查点未触发热重载——桌面版（无 Telegram 循环）改配置将永不生效"
    )


def test_web_checkpoint_respects_throttle(cm_app):
    """节流窗内的请求不应触发重载（防每请求做文件 IO）。"""
    cm, app, tmp_path = cm_app
    client = TestClient(app)
    client.get("/login")

    overlay = tmp_path / "config.local.yaml"
    overlay.write_text(
        yaml.dump({"companion": {"quality_trend": {"enabled": False}}},
                  allow_unicode=True),
        encoding="utf-8")
    # 刚检查过（load 后首个请求已跑）→ 窗口内再请求不重载
    cm._last_hot_reload_check = __import__("time").time()
    client.get("/login")
    assert ((cm.config.get("companion") or {}).get("quality_trend") is None)
