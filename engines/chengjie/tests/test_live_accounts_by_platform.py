# -*- coding: utf-8 -*-
"""#126（2026-09-01 skuio）：向导「已接 N 个账号」/ 头部「能收发 X/7」计数源。

向导 Telegram 行常年显「已接 1 个账号」而机器实登多号——与人设「应用到」弹窗
（#61/#78）同族的账号枚举病换了个消费面。真相单一源＝运行时账号注册表
``platform_accounts``。本文件钉死 ``live_accounts_by_platform`` 的三条口径：
多账号全计、removed 不计、offline（登出）不计（登录登出实时跟随）。
"""
from __future__ import annotations

import pytest

from src.integrations import account_registry as ar
from src.integrations.account_registry import (
    AccountRegistry, live_accounts_by_platform,
)


@pytest.fixture
def reg(tmp_path, monkeypatch):
    """独立注册表 + 把模块单例指向它（live_* 内部走 get_account_registry）。"""
    r = AccountRegistry(tmp_path / "acc.db")
    monkeypatch.setattr(ar, "_registry", r)
    return r


def test_counts_all_online_accounts_per_platform(reg):
    """#126 主场景：5 个 TG 账号在线 ⇒ telegram 计 5（不是打包主会话的 1）。"""
    for i in range(5):
        reg.upsert("telegram", f"tg{i}", mode="protocol", status="online")
    reg.upsert("line", "line1", mode="protocol", status="online")
    by = live_accounts_by_platform()
    assert by["telegram"] == 5
    assert by["line"] == 1


def test_removed_never_counted(reg):
    """软删账号（status=removed）不计入。"""
    reg.upsert("telegram", "a", status="online")
    reg.upsert("telegram", "b", status="online")
    reg.remove("telegram", "b")  # → status=removed
    assert live_accounts_by_platform()["telegram"] == 1


def test_offline_excluded_by_default(reg):
    """登录登出实时跟随：登出（offline）即时 -1。"""
    reg.upsert("telegram", "a", status="online")
    reg.upsert("telegram", "b", status="online")
    by = live_accounts_by_platform()
    assert by["telegram"] == 2
    reg.set_status("telegram", "b", "offline")
    by2 = live_accounts_by_platform()
    assert by2["telegram"] == 1


def test_pending_counted_as_live(reg):
    """pending（登录在途/待编排器拉起）算活跃——与聊天页活跃账号定义同口径。"""
    reg.upsert("telegram", "a", status="online")
    reg.upsert("telegram", "b", status="pending")
    assert live_accounts_by_platform()["telegram"] == 2


def test_exclude_offline_false_keeps_offline(reg):
    """显式 exclude_offline=False：offline 也计（供「所有在册账号」类用途）。"""
    reg.upsert("telegram", "a", status="online")
    reg.upsert("telegram", "b", status="offline")
    assert live_accounts_by_platform(exclude_offline=False)["telegram"] == 2
    assert live_accounts_by_platform()["telegram"] == 1


def test_read_failure_returns_empty(monkeypatch):
    """取数失败一律返回空 dict（调用方回落纯配置口径，绝不报错）。"""
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(ar, "get_account_registry", _boom)
    assert live_accounts_by_platform() == {}
