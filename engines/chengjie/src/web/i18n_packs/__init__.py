# -*- coding: utf-8 -*-
"""按域拆分的 i18n 词条包(P4 词条治理机制化)。

背景:`web_i18n.py` 是 16k 行的单体字典,多条工作流并行往里加词条时反复发生
"后写覆盖先写"的丢失更新(2026-07-20 实测踩中)。本包提供增量拆分机制:

- **新增词条一律进本包**(按域建小文件),不再直接改 `web_i18n.py` 的
  `_TRANSLATIONS` 字面量;存量词条在无并行编辑窗口时逐域迁移。
- 每个 pack 模块暴露 ``ZH`` 与 ``EN``(key 一一对应);可选扩展语言 dict
  (``VI``/``TH``/``ID``,见 ``EXTRA_LANGS``——子集 override 键,不要求与 ZH/EN 同键)。
- `web_i18n.get_translations()` 返回 **单体 + 全部 pack 合并**后的字典,
  对所有消费方(模板/JS 词表/tr())透明。扩展语言 = ``{**en合并, **override}``,
  缺键自动回落英文。
- 冲突规则:pack 之间、pack 与单体之间 **禁止同 key**(collect 对 pack 间冲突
  直接抛错;pack×单体冲突由门禁 ``test_i18n_packs_bilingual_and_no_collision``
  拦截)。扩展语言各自独立判重(同语言两个 pack 定义同 key = fail fast)。
- 热更新:web_i18n 的 `_maybe_reload` 同时监视本包目录 mtime,改 pack 文件
  与改单体一样即时生效(fail-safe:异常保留旧字典)。

新建 pack:在本目录加 ``<domain>.py``,定义 ``ZH``/``EN``(及可选扩展语言 dict),
无需注册。**新增 UI 语言**(xlate P3 起表驱动):``EXTRA_LANGS`` 加语言码 →
建 ``<lang>_<domain>.py`` 词包(暴露同名大写 dict) → ``UI_LOCALES`` 补 locale;
中间件/set_lang/bundle 路由的白名单都消费 ``UI_LANGS`` 单一事实源,无需逐处改。
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Dict, Iterable, Tuple

_PKG_DIR = Path(__file__).resolve().parent

# ── UI 语言单一事实源（xlate P3，2026-08-16）──────────────────────────────────
# EXTRA_LANGS：完整双语（zh/en）之外的「en 底 + 覆盖」扩展语言，按覆盖度渐进补词。
# 消费方：web_i18n 合并视图、admin.py 中间件与 /set_lang、web_user_store 偏好落库、
# i18n_bundle_routes 白名单/locale——此前四处 ("zh","en","vi") 字面量各自为政，
# th/id 若逐处手改必漂移，收口进本表。
EXTRA_LANGS: Tuple[str, ...] = ("vi", "th", "id")
UI_LANGS: Tuple[str, ...] = ("zh", "en") + EXTRA_LANGS
UI_LOCALES: Dict[str, str] = {
    "zh": "zh-CN", "en": "en-US", "vi": "vi-VN", "th": "th-TH", "id": "id-ID",
}


def iter_pack_names() -> Iterable[str]:
    """本包内全部 pack 模块名(不含下划线开头)。"""
    for m in pkgutil.iter_modules([str(_PKG_DIR)]):
        if not m.name.startswith("_"):
            yield m.name


def pack_files() -> list:
    """全部 pack 源文件路径(供热重载 mtime 监视)。"""
    return sorted(_PKG_DIR.glob("[!_]*.py"))


def collect_all(force_reload: bool = False) -> Tuple[
    Dict[str, str], Dict[str, str], Dict[str, Dict[str, str]],
]:
    """合并全部 pack,返回 ``(zh, en, extras)``,extras=``{lang: override}``。

    ZH/EN:pack 之间同 key 冲突 → 直接 ValueError(fail fast)。
    扩展语言:按 ``EXTRA_LANGS`` 表逐语言独立收集/判重(pack 可只定义某语言的
    override dict;同语言跨 pack 同 key 同样 fail fast)。
    force_reload=True 时逐模块 importlib.reload(热更新路径用)。
    """
    zh: Dict[str, str] = {}
    en: Dict[str, str] = {}
    extras: Dict[str, Dict[str, str]] = {lg: {} for lg in EXTRA_LANGS}
    owner: Dict[str, str] = {}
    extra_owner: Dict[str, Dict[str, str]] = {lg: {} for lg in EXTRA_LANGS}
    for name in iter_pack_names():
        mod = importlib.import_module(f"{__name__}.{name}")
        if force_reload:
            mod = importlib.reload(mod)
        mzh = getattr(mod, "ZH", {}) or {}
        men = getattr(mod, "EN", {}) or {}
        for k in set(mzh) | set(men):
            if k in owner:
                raise ValueError(
                    f"i18n pack 冲突: key {k!r} 同时定义于 {owner[k]} 与 {name}")
            owner[k] = name
        zh.update(mzh)
        en.update(men)
        for lg in EXTRA_LANGS:
            mx = getattr(mod, lg.upper(), {}) or {}
            own = extra_owner[lg]
            for k in mx:
                if k in own:
                    raise ValueError(
                        f"i18n pack {lg.upper()} 冲突: key {k!r} 同时定义于 "
                        f"{own[k]} 与 {name}")
                own[k] = name
            extras[lg].update(mx)
    return zh, en, extras


def collect_packs(force_reload: bool = False) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    """兼容出口：返回 ``(zh, en, vi)`` 三元组（vi 首发时的契约，勿改签名）。

    新消费方（web_i18n 合并视图等）请用 :func:`collect_all`（含全部扩展语言）。
    """
    zh, en, extras = collect_all(force_reload)
    return zh, en, extras.get("vi", {})
