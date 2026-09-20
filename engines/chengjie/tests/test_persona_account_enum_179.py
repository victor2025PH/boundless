# -*- coding: utf-8 -*-
"""#179（2026-09-05 钧原图 1186）：「应用到 → 账号默认」出现 TG「default —」幽灵行。

#61 把运行时注册表并进枚举后，占位账号 ``default``（无 label / 非真登录）被当
真账号列出。修法＝合并处两道过滤（`persona_routes.registry_row_is_placeholder`
+ `config_default_account_is_live`）：注册表占位行不进枚举；config 回退槽
``default`` 只在协议客户端真会用它登录（非桌面 + 真凭证）时才列。

验收（指令 §5）：注册表含 ``default`` 占位 + 两个真号 → 列表只出两个真号。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.web.routes.persona_routes import (
    config_default_account_is_live,
    registry_row_is_placeholder,
)

SRC = (Path(__file__).resolve().parents[1]
       / "src" / "web" / "routes" / "persona_routes.py").read_text(
           encoding="utf-8")


def _row(aid, label="", status="pending", last_online_at=0, meta=None):
    return {"platform": "telegram", "account_id": aid, "label": label,
            "status": status, "last_online_at": last_online_at,
            "meta": meta or {}}


# ── 占位行判据 ────────────────────────────────────────────────────────────────

def test_default_alias_row_is_placeholder_even_when_online():
    """default 别名槽恒占位（哪怕 N5 同步把它标成 online）。"""
    assert registry_row_is_placeholder(_row("default", label="default", status="online"))
    assert registry_row_is_placeholder(_row("", status="online"))
    assert registry_row_is_placeholder(_row("  "))


def test_unlabeled_never_online_pending_row_is_placeholder():
    assert registry_row_is_placeholder(_row("tg-8f2a", status="pending"))
    assert registry_row_is_placeholder(_row("tg-8f2a", status="offline", last_online_at=0))


@pytest.mark.parametrize("row", [
    _row("6834964252", label="钧", status="online", last_online_at=1.7e9),
    _row("6834964252", label="", status="online"),                       # 在线裸 id
    _row("6834964252", label="", status="offline", last_online_at=1.7e9),  # 曾上线的离线号
    _row("6834964252", label="", status="pending", meta={"self_name": "steven"}),  # 有昵称
])
def test_real_accounts_are_not_placeholders(row):
    """真号哪怕此刻离线 / 没运营 label，只要曾上线或有昵称一律保留。"""
    assert not registry_row_is_placeholder(row)


def test_non_dict_row_is_placeholder():
    assert registry_row_is_placeholder(None)
    assert registry_row_is_placeholder("default")


# ── config default 槽是否算真账号 ─────────────────────────────────────────────

_REAL_TG = {"api_id": "12345", "api_hash": "abcd" * 8, "phone_number": "+8613800000000"}


def test_config_default_live_only_for_real_creds_non_desktop(monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    assert config_default_account_is_live(_REAL_TG, {"telegram": _REAL_TG})
    # 桌面包（config app.desktop_mode）→ 协议客户端不起 → 不是真账号
    assert not config_default_account_is_live(
        _REAL_TG, {"telegram": _REAL_TG, "app": {"desktop_mode": True}})
    # 桌面包（env）
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    assert not config_default_account_is_live(_REAL_TG, {"telegram": _REAL_TG})


def test_config_default_not_live_when_creds_missing_or_placeholder(monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    assert not config_default_account_is_live({}, {})
    assert not config_default_account_is_live({"api_id": "1", "api_hash": "x"}, {})  # 无手机号
    assert not config_default_account_is_live(
        {"api_id": "YOUR_API_ID", "api_hash": "YOUR_API_HASH",
         "phone_number": "+86100"}, {})
    assert not config_default_account_is_live(None, None)


# ── 验收：占位 + 两个真号 → 只出两个真号 ──────────────────────────────────────

def test_merge_surfaces_only_real_accounts():
    """复刻 /api/personas/status TG 合并段的去重 + 占位过滤语义。"""
    rows = [
        _row("default", label="default", status="pending"),
        _row("6834964252", label="钧", status="online", last_online_at=1.7e9),
        _row("7000000001", label="", status="online",
             meta={"self_name": "steven", "self_username": "steven_x"}),
    ]
    seen = set()
    out = []
    for r in rows:
        aid = str(r.get("account_id") or "").strip()
        if not aid or aid in seen or registry_row_is_placeholder(r):
            continue
        out.append(aid)
    assert out == ["6834964252", "7000000001"]


# ── 静态接线：两道过滤真的挂在路由合并段里 ────────────────────────────────────

def test_status_route_wires_both_filters():
    seg = SRC[SRC.index('@app.get("/api/personas/status")'):]
    seg = seg[:seg.index("mrpa_accounts: list = []")]
    assert "config_default_account_is_live(_tg_cfg, _cfg_obj)" in seg
    assert re.search(r"if acc\.is_default and not _cfg_default_live:\s*\n\s*continue", seg)
    assert re.search(r"if registry_row_is_placeholder\(_row\):\s*\n\s*continue", seg)
    # 占位过滤必须在 append 之前（放在后面＝先渲染再删，等于没过滤）
    assert seg.index("registry_row_is_placeholder(_row)") < seg.index('"source": "registry"')
