# -*- coding: utf-8 -*-
"""小智 Windows 操控 runner 受控机注册表门禁（实施91 P1-0，2026-08-30）。

守「机器是白名单」这条本子系统的第一道结构性防线（改前先读）：
1. 只有配置声明的机器可被操作；LLM/前端提名不在白名单的 id 一律 None。
2. 绝不接受任意 URL（只白名单 id → base_url）；非 http scheme 的配置条目
   被拒（防 file:// 等）。
3. revoke 立即生效、restore 可恢复；token 绝不出现在任何列表返回里。
4. machine_id 严格校验（防路径穿越/审计行污染）。
"""
from __future__ import annotations

import pytest

from src.assistant import runner_pairing as rp


@pytest.fixture(autouse=True)
def _clean():
    rp._reset_for_test()
    yield
    rp._reset_for_test()


_GOOD = [
    {"id": "zhuji", "base_url": "http://127.0.0.1:18760", "label": "主机"},
    {"id": "kouxing", "base_url": "http://192.168.0.198:18760/",
     "label": "视觉机"},
]


def test_only_whitelisted_machines_resolve():
    assert rp.load_machines(_GOOD) == 2
    assert rp.get_machine("zhuji")["base_url"] == "http://127.0.0.1:18760"
    # 尾斜杠归一化
    assert rp.get_machine("kouxing")["base_url"] == \
        "http://192.168.0.198:18760"
    # 不在白名单 → None（LLM 提名野机器打不进来）
    assert rp.get_machine("some-random-box") is None
    assert rp.get_machine("") is None


def test_load_rejects_bad_entries():
    n = rp.load_machines([
        {"id": "ok", "base_url": "http://10.0.0.5:18760"},
        {"id": "bad id!", "base_url": "http://10.0.0.6:18760"},  # 非法 id
        {"id": "nourl"},                                          # 缺 url
        {"id": "evil", "base_url": "file:///etc/passwd"},         # 非 http
        {"id": "ftp", "base_url": "ftp://x"},                     # 非 http
        "not-a-dict",
    ])
    assert n == 1
    assert rp.get_machine("ok") is not None
    for bad in ("bad id!", "nourl", "evil", "ftp"):
        assert rp.get_machine(bad) is None


def test_machine_id_validation():
    assert rp.valid_machine_id("zhuji")
    assert rp.valid_machine_id("box_01-a")
    assert not rp.valid_machine_id("../etc")
    assert not rp.valid_machine_id("a b")
    assert not rp.valid_machine_id("")
    assert not rp.valid_machine_id("x" * 41)


def test_revoke_restore_lifecycle():
    rp.load_machines(_GOOD)
    assert rp.get_machine("zhuji") is not None
    assert rp.revoke("zhuji") is True
    assert rp.get_machine("zhuji") is None       # 踢下线立即不可用
    assert rp.is_revoked("zhuji")
    assert "zhuji" not in rp.machine_ids()
    assert rp.restore("zhuji") is True
    assert rp.get_machine("zhuji") is not None    # 恢复
    # 踢不在白名单的机器 = False（不误报成功）
    assert rp.revoke("ghost") is False
    assert rp.restore("never-revoked") is False


def test_reload_keeps_revoked_state():
    """重配受控机清单不清空运行时踢下线态（跨重配保留）。"""
    rp.load_machines(_GOOD)
    rp.revoke("zhuji")
    rp.load_machines(_GOOD)               # 重新载入同清单
    assert rp.get_machine("zhuji") is None  # 仍被踢
    assert rp.get_machine("kouxing") is not None


def test_list_machines_never_leaks_token():
    """注册表结构里根本不存 token；list 返回也绝不含任何 token 字段。"""
    rp.load_machines([
        {"id": "zhuji", "base_url": "http://127.0.0.1:18760",
         "label": "主机", "token": "SECRET-should-be-ignored"}])
    rows = rp.list_machines()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "zhuji" and row["revoked"] is False
    for k, v in row.items():
        assert "token" not in k.lower()
        assert "SECRET" not in str(v)


def test_actor_suffix_safe():
    assert rp.actor_suffix("zhuji") == "@pc:zhuji"
    # 非法 id 不拼进审计行（防污染）
    assert rp.actor_suffix("../evil") == "@pc:?"
    assert rp.actor_suffix("") == "@pc:?"
