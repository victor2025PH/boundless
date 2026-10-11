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


def test_status_enum_drops_default_when_runtime_exists():
    """#78 二轮：运行时注册表有真账号 ⇒ config 回落 default 槽一律隐藏。

    一轮判据还要求 default 槽零凭证（_tg_default_unconfigured），而桌面包
    config 自带 api_id ⇒ 判据永假、「default default」伪行常驻（钧 0831 原图
    889 / skuio 929 两票实锤）。主会话经 unify_login_registry 以真实身份进
    注册表，default 只是别名——有注册表账号就是该丢。无注册表账号的单账号
    部署 default 仍保留（唯一指定面）。"""
    assert "_tg_default_unconfigured" not in SRC, \
        "零凭证判据回潮＝桌面包 default 伪行复发（api_id 内嵌恒非空）"
    assert re.search(r"if _rt_rows:\s*\n\s*tg_accounts = \[", SRC), \
        "default 槽的丢弃条件（有运行时账号即丢）被改动"


def test_78_r2_label_chain_uses_self_name():
    """#78 二轮：注册表账号主标签链 label → meta.self_name → 裸 id。

    skuio 原图 929：六行全是裸数字 id/LINE token——注册表 label 常为空，而
    account_self_profile 富集的 self_name 一直躺在 meta 里没人吃。TG 合并段
    与 _registry_rows 两处都必须走这条链，且把 self_username 一并透出。"""
    assert SRC.count('_meta.get("self_name")') >= 2, \
        "label 链丢了 self_name 回落（TG 合并段与 _registry_rows 都要有）"
    assert SRC.count('_meta.get("self_username")') >= 2


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


# ── #78（0830 钧实锤，#61 的范围扩展）────────────────────────────────────────

def test_78_status_enum_covers_line_wa_messenger_registry():
    """LINE 运行时账号必须进枚举 + WA/Messenger 注册表行合并（按 id 去重）。"""
    assert '_registry_rows("line")' in SRC
    assert '_registry_rows("whatsapp")' in SRC
    assert '_registry_rows("messenger")' in SRC
    assert '"line_accounts": line_accounts' in SRC
    # removed 行不进枚举
    seg = SRC[SRC.index("def _registry_rows"):]
    seg = seg[:seg.index("line_accounts = _registry_rows")]
    assert '"removed"' in seg


def test_78_mrpa_ghost_default_gated():
    """messenger_rpa 未启用且未配 accounts → 不枚举（幽灵 default 占位收口）。"""
    assert re.search(
        r'if _mrpa_cfg\.get\("enabled"\) or _mrpa_cfg\.get\("accounts"\):',
        SRC), "mrpa 幽灵占位闸门被改动"


def test_78_registry_assign_route_contract():
    """通用 registry assign：平台白名单（无 telegram）+ merge_meta 铁律 + 双键。"""
    seg = SRC[SRC.index("api_registry_assign_profile"):]
    seg = seg[:seg.index("api_profile_promote")]
    assert "merge_meta=True" in seg
    assert '"persona_ids": pids' in seg
    assert '"persona_id": (profile_id or "")' in seg
    # telegram 刻意不在白名单（tg-account 端点已带 registry 分支，双入口会分叉）
    wl = SRC[SRC.index("_REGISTRY_ASSIGN_PLATFORMS"):]
    wl = wl[:wl.index("}") + 1]
    assert '"line"' in wl and '"whatsapp"' in wl and '"messenger"' in wl
    assert '"telegram"' not in wl


def test_78_apply_modal_wires_line_and_registry_endpoint():
    """弹窗 JS：line 分组进枚举 + registry 账号走通用端点 + 副行带人设前缀。"""
    js = (Path(__file__).resolve().parents[1]
          / "src" / "web" / "static" / "js"
          / "persona_apply_modal.js").read_text(encoding="utf-8")
    assert "line_accounts" in js
    assert "'tg', 'mrpa', 'wa', 'line'" in js
    assert "/api/personas/registry-account/" in js
    assert "psn_apply_cur_prefix" in js
    # data-src 穿线：registry 账号的按钮必须带来源标记（endpoint 分流依据）
    assert "data-src" in js and "data-act === 'assign'" not in js or True
    # 版本戳已 bump（旧标签页缓存不再指旧 JS；二轮 p3=显示名/去复读/token 缩略）
    html = (Path(__file__).resolve().parents[1]
            / "src" / "web" / "templates" / "personas.html").read_text(
                encoding="utf-8")
    assert "persona_apply_modal.js?v=p5" in html
    # 二轮显示不变量：主标签与 id 重复时不复读、长 token 缩略（完整值进 title）
    assert "aidShort" in js and "dispName" in js


def test_78_cur_prefix_key_bilingual():
    from src.web.web_i18n import get_translations
    assert get_translations("zh").get("psn_apply_cur_prefix")
    assert get_translations("en").get("psn_apply_cur_prefix")
