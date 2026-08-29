# -*- coding: utf-8 -*-
"""#61（2026-08-30）：人设「应用到…」多账号枚举 —— 静态接线门禁。

钧实锤：弹窗只能看到第一个登录的 Telegram 账号。根因＝枚举只读
``TelegramAccountRegistry.from_config``（config telegram.accounts），而桌面 QR
登录的多账号活在运行时注册表 ``platform_accounts``（编排器真相），config 里
根本没有。修复＝status 枚举合并运行时注册表 + assign 端点回落写
``meta.persona_ids``。

路由函数是闭包（无法轻量单测），本门禁按源码钉三条载荷不变量——谁把合并
删了/把 merge_meta 改回整块替换，这里先红。运行时 meta 解析与 upsert 合并
语义本体由 test_account_registry / parse_persona_ids 各自的门禁守。
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1]
       / "src" / "web" / "routes" / "persona_routes.py").read_text(
           encoding="utf-8")


def test_status_enum_merges_runtime_registry():
    """/api/personas/status 的 TG 枚举必须合并运行时注册表（QR 多账号来源）。"""
    assert 'get_account_registry().list("telegram")' in SRC
    assert "parse_persona_ids" in SRC
    # 合并段必须在 tg_accounts 建表之后、mrpa 段之前（顺序换了=合并进错清单）
    i_merge = SRC.index('get_account_registry().list("telegram")')
    i_tg = SRC.index("tg_accounts: list = []")
    i_mrpa = SRC.index("mrpa_accounts: list = []")
    assert i_tg < i_merge < i_mrpa


def test_status_enum_drops_credless_default_when_runtime_exists():
    """config 回落的无凭证 default 占位：运行时有真账号时必须丢弃——
    否则桌面用户永远看到一个既不能发也不该绑的幽灵「default」行。"""
    assert "_tg_default_unconfigured" in SRC
    assert re.search(
        r"if _rt_rows and _tg_default_unconfigured:", SRC), \
        "default 占位的丢弃条件（有运行时账号才丢）被改动"


def test_tg_assign_falls_back_to_registry_with_merge_meta():
    """assign-profile 对运行时账号写 meta 必须 merge_meta=True——整块替换会把
    session_string 等键抹掉（2026-07-23 baileys 同族事故），这是硬红线。"""
    seg = SRC[SRC.index("api_tg_assign_profile"):]
    seg = seg[:seg.index("api_mrpa_assign_profile")]
    assert 'get("telegram", account_id)' in seg
    assert "merge_meta=True" in seg
    # 注册表回落必须先于 default 扁平槽（具体 id 绝不落进 default 槽）
    i_reg = seg.index("merge_meta=True")
    i_flat = seg.index('tg_cfg["persona_ids"] = pids')
    assert i_reg < i_flat
    # 绑定与清除都写双键（persona_ids + persona_id：两类读取方各认一个）
    assert '"persona_ids": pids' in seg
    assert '"persona_id": (profile_id or "")' in seg
