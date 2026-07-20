# -*- coding: utf-8 -*-
"""按域拆分的 i18n 词条包(P4 词条治理机制化)。

背景:`web_i18n.py` 是 16k 行的单体字典,多条工作流并行往里加词条时反复发生
"后写覆盖先写"的丢失更新(2026-07-20 实测踩中)。本包提供增量拆分机制:

- **新增词条一律进本包**(按域建小文件),不再直接改 `web_i18n.py` 的
  `_TRANSLATIONS` 字面量;存量词条在无并行编辑窗口时逐域迁移。
- 每个 pack 模块暴露 ``ZH`` 与 ``EN``(key 一一对应);可选 ``VI``(越南语子集,
  可仅含 override 键,不要求与 ZH/EN 同键)。
- `web_i18n.get_translations()` 返回 **单体 + 全部 pack 合并**后的字典,
  对所有消费方(模板/JS 词表/tr())透明。
- 冲突规则:pack 之间、pack 与单体之间 **禁止同 key**(collect_packs 对
  pack 间冲突直接抛错;pack×单体冲突由门禁
  ``test_i18n_packs_bilingual_and_no_collision`` 拦截)。
- 热更新:web_i18n 的 `_maybe_reload` 同时监视本包目录 mtime,改 pack 文件
  与改单体一样即时生效(fail-safe:异常保留旧字典)。

新建 pack:在本目录加 ``<domain>.py``,定义 ``ZH``/``EN``(及可选 ``VI``),无需注册。
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Dict, Iterable, Tuple

_PKG_DIR = Path(__file__).resolve().parent


def iter_pack_names() -> Iterable[str]:
    """本包内全部 pack 模块名(不含下划线开头)。"""
    for m in pkgutil.iter_modules([str(_PKG_DIR)]):
        if not m.name.startswith("_"):
            yield m.name


def pack_files() -> list:
    """全部 pack 源文件路径(供热重载 mtime 监视)。"""
    return sorted(_PKG_DIR.glob("[!_]*.py"))


def collect_packs(force_reload: bool = False) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    """合并全部 pack,返回 (zh, en, vi)。

    ZH/EN:pack 之间同 key 冲突 → 直接 ValueError(fail fast)。
    VI:独立收集,pack 可仅定义 VI(override 键);VI 之间同 key 冲突同样 fail fast。
    force_reload=True 时逐模块 importlib.reload(热更新路径用)。
    """
    zh: Dict[str, str] = {}
    en: Dict[str, str] = {}
    vi: Dict[str, str] = {}
    owner: Dict[str, str] = {}
    vi_owner: Dict[str, str] = {}
    for name in iter_pack_names():
        mod = importlib.import_module(f"{__name__}.{name}")
        if force_reload:
            mod = importlib.reload(mod)
        mzh = getattr(mod, "ZH", {}) or {}
        men = getattr(mod, "EN", {}) or {}
        mvi = getattr(mod, "VI", {}) or {}
        for k in set(mzh) | set(men):
            if k in owner:
                raise ValueError(
                    f"i18n pack 冲突: key {k!r} 同时定义于 {owner[k]} 与 {name}")
            owner[k] = name
        for k in mvi:
            if k in vi_owner:
                raise ValueError(
                    f"i18n pack VI 冲突: key {k!r} 同时定义于 {vi_owner[k]} 与 {name}")
            vi_owner[k] = name
        zh.update(mzh)
        en.update(men)
        vi.update(mvi)
    return zh, en, vi
