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
import re
import sys
import types
from pathlib import Path
from typing import Dict, Iterable, Tuple

_PKG_DIR = Path(__file__).resolve().parent

# ── 打包态（PyInstaller frozen）文件优先加载（2026-08-18，桌面增量刷新 P1）────
# 桌面包里本目录以**双份**存在：PYZ 编译副本（import 机制走 FrozenImporter 命中它）
# + <_MEIPASS>/src/web/i18n_packs/*.py 数据文件（build_backend 暂存清洗后 --add-data）。
# refresh_backend_datas 的秒级增量只能替换**数据文件**，故 frozen 态收集改为
# 「磁盘文件存在即 exec 文件」（与 web_i18n 单体热重载同一招式；pack 均为纯数据
# dict，有门禁保证零运行时 import/零函数）——谁改的文件读谁，PYZ 陈旧副本仅作
# exec 失败时的回落。dev 态（非 frozen）路径零改动，仍走 importlib（可 reload、
# 模块身份稳定，被既有门禁与直接 import 消费方依赖）。
# ⚠ 本 __init__.py 自身逻辑始终来自 PYZ——改它必须全量重打
# （refresh_backend_datas 把 `_` 开头文件归为不安全变更，正是为此）。
_FROZEN = bool(getattr(sys, "frozen", False))


def _load_pack_from_file(path: Path):
    """把单个 pack 源文件 exec 进独立命名空间（不碰 sys.modules / 不受 PYZ 影响）。"""
    ns: dict = {"__file__": str(path)}
    code = compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")
    exec(code, ns)  # noqa: S102 - pack 是自家数据文件，与单体热重载同信任级
    return types.SimpleNamespace(**ns)

# ── UI 语言单一事实源（xlate P3，2026-08-16）──────────────────────────────────
# EXTRA_LANGS：完整双语（zh/en）之外的「底语言 + 覆盖」扩展语言，按覆盖度渐进补词。
# 消费方：web_i18n 合并视图、admin.py 中间件与 /set_lang、web_user_store 偏好落库、
# i18n_bundle_routes 白名单/locale——此前四处 ("zh","en","vi") 字面量各自为政，
# th/id 若逐处手改必漂移，收口进本表。
# 语言码必须是合法 Python 标识符片段（pack 加载走 getattr(mod, 码.upper())），
# 故繁体用 "zh_hant" 而非 BCP-47 的 "zh-Hant"；对外 locale 由 UI_LOCALES 提供。
EXTRA_LANGS: Tuple[str, ...] = ("vi", "th", "id", "zh_hant")
UI_LANGS: Tuple[str, ...] = ("zh", "en") + EXTRA_LANGS
UI_LOCALES: Dict[str, str] = {
    "zh": "zh-CN", "en": "en-US", "vi": "vi-VN", "th": "th-TH", "id": "id-ID",
    "zh_hant": "zh-Hant",
}
# 扩展语缺键回落底（zh_hant P2，2026-08-27）：默认英文底；繁体缺键回落**简体**
# ——繁体坐席看简体可读，看英文才是断崖。消费方 web_i18n._merge_views。
# 注：zh_hant 由 scripts/i18n_hant.py 从简体全量转换生成（zh_hant_auto.py，
# ~100% 覆盖），本表是新键在 regen 前的兜底方向，不是常态依赖。
EXTRA_LANG_BASE: Dict[str, str] = {"zh_hant": "zh"}

# zh 家族按 script/地区细分到繁体的子标签（negotiate_ui_lang 消费）
_HANT_SUBTAGS = frozenset({"tw", "hk", "mo", "hant"})
_Q_RE = re.compile(r"q\s*=\s*([0-9.]+)")


def negotiate_ui_lang(accept_language: str) -> str:
    """``Accept-Language`` → UI_LANGS 白名单内最佳匹配；无法匹配返回 ``''``。

    定位（系统语言自动跟随，2026-08-27）：只做中间件语言链的**末级**——
    ``?lang=`` 与 ``ui_lang`` cookie（显式选择/登录回填）永远优先；推断结果
    **不落 cookie 不落库**（跟随而非固化：坐席换系统/浏览器语言界面即跟走，
    显式切换过一次则推断永久让位）。

    匹配规则：按 q 值降序（同 q 保持出现顺序）逐个 tag 尝试——
    zh 家族按子标签细分（TW/HK/MO/Hant → zh_hant，其余 → zh）；
    其他语言取主子标签在 UI_LANGS 里直查（en-GB→en、vi-VN→vi…）。
    """
    s = str(accept_language or "").strip()
    if not s:
        return ""
    cands = []
    for i, part in enumerate(s.split(",")):
        seg = part.strip()
        if not seg:
            continue
        tag, _, params = seg.partition(";")
        q = 1.0
        m = _Q_RE.search(params)
        if m:
            try:
                q = float(m.group(1))
            except ValueError:
                q = 0.0
        cands.append((-q, i, tag.strip().lower().replace("_", "-")))
    cands.sort()
    for _nq, _i, tag in cands:
        if not tag or tag == "*":
            continue
        parts = tag.split("-")
        if parts[0] == "zh":
            return "zh_hant" if _HANT_SUBTAGS.intersection(parts[1:]) else "zh"
        if parts[0] in UI_LANGS:
            return parts[0]
    return ""


def primary_lang_tag(accept_language: str) -> str:
    """Accept-Language 里 q 值最高的**主子标签**（``ja-JP;q=0.9,en;q=0.8`` → ``ja``）。

    消费方：ui_lang_stats「想要却没有」计数——negotiate 落空（default）时记下
    坐席浏览器最想要的语言，ja/ko 等语种扩张立项直接看这个分布，不再拍脑袋。
    与 negotiate_ui_lang 同一解析口径；空/畸形/通配返回 ''。
    """
    s = str(accept_language or "").strip()
    if not s:
        return ""
    best = ("", -1.0, 0)  # (tag, q, -index)：q 降序，同 q 取先出现
    for i, part in enumerate(s.split(",")):
        seg = part.strip()
        if not seg:
            continue
        tag, _, params = seg.partition(";")
        tag = tag.strip().lower().replace("_", "-")
        if not tag or tag == "*":
            continue
        q = 1.0
        m = _Q_RE.search(params)
        if m:
            try:
                q = float(m.group(1))
            except ValueError:
                q = 0.0
        if q > best[1] or (q == best[1] and -i > best[2]):
            best = (tag.split("-")[0], q, -i)
    return best[0][:8]


def iter_pack_names() -> Iterable[str]:
    """本包内全部 pack 模块名(不含下划线开头)。

    frozen 态且数据目录真实存在 → 按磁盘文件枚举（增量刷新新增的 pack 不在 PYZ，
    pkgutil 会漏）；否则（dev / 旧安装包无数据目录）保持 pkgutil 旧行为。
    """
    if _FROZEN and _PKG_DIR.is_dir():
        files = pack_files()
        if files:
            for p in files:
                yield p.stem
            return
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
        mod = None
        if _FROZEN:
            path = _PKG_DIR / f"{name}.py"
            if path.is_file():
                try:
                    mod = _load_pack_from_file(path)
                except Exception:
                    # 数据文件坏（半写/编码损伤）→ 回落 PYZ 编译副本（旧但完好），
                    # 与「收集失败保留旧视图」同哲学：绝不让单个坏文件放倒全站词表。
                    mod = None
        if mod is None:
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
