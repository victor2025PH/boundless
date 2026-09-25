"""裸 fetch ratchet 门禁（P0 前端优化，2026-07-23）。

背景：全站模板 55 个文件共 613 处裸 ``fetch(`` 调用，无统一超时/重试/遥测。
2026-07-23 实锤事故：重启窗口的半开连接让 fetch **永不 settle** → 悬死 promise
卡死轮询管线，「重连中」横幅永久冻结（服务端早已恢复）。统一层
``window.apiFetch``（``_api_fetch.html``，workspace_base 头部 include）提供
默认 20s 硬超时 + 网络失败遥测 + 可选重试，语义与原生 fetch 同构。

本门禁与 ``test_route_response_cjk_ledger_ratchet`` 同款「非增天花板」纪律：

1. 任何模板的裸 fetch 计数**不得超过**账本天花板——新代码一律走
   ``apiFetch(url, init, {timeoutMs})``（或页内既有的 ``_fetchWithTimeout``）。
2. 计数下降后必须同步调低账本（防账本陈旧、让迁移进度可见）。
3. ``_api_fetch.html`` 基建自身与 workspace_base 的挂载点受契约保护。

计数口径：正则 ``(?<![A-Za-z0-9_$.])fetch\\s*\\(``——``apiFetch(`` /
``_fetchWithTimeout(`` 等包装名因前导字符天然不命中；注释里提到 fetch 但不带
调用括号的行不计。
"""
from __future__ import annotations

import re
from pathlib import Path

TEMPLATES_ROOT = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"

_BARE_FETCH_RX = re.compile(r"(?<![A-Za-z0-9_$.])fetch\s*\(")

# ── 账本：文件 → 裸 fetch 天花板（只许降不许升） ──
# 2026-07-23 基线 613 → P1-1 GET 批迁 318 → P2-1 写路径批迁 256（60s 预算）后余 39
# → 2026-08-07 两线接力批清（unified_inbox 13 处 + 全站其余 21 处）后余 5。
# 原「长操作端点统一超时=回归风险」的欠账已按端点显式预算兑现：普通写 60s、
# 上传/拉媒体 120s、OCR/VLM 翻译 180s、文档翻译/大上传 5min、backfill/bulk-autosend 10min；
# 自带 signal 的生命周期中断（rpa_overview lint 取消）由 apiFetch race 语义原样承接。
# 未列出的文件天花板=0：新模板必须走 apiFetch(url, init, {timeoutMs})。
_BARE_FETCH_CEILINGS = {
    # 统一层自身的原生 fetch 调用（_oneAttempt 两处 + 老浏览器透传一处）——豁免基线，
    # 全站唯一允许长期保留裸 fetch 的文件。
    "_api_fetch.html": 3,
    # 2026-09-25：personas.html 裸 fetch 已归零（实测 0），账本下调锁死。
    # 独立整页（布局链无 _api_fetch.html）+ 实时通话链自带 signal 的超时包装——
    # 结构性豁免：为一处迁移给独立页挂统一层，收益低于回归风险，刻意保留。
    "voice_call.html": 1,
    # 群成员提取管理台：独立自包含页（不 extends base.html、无 _api_fetch.html include），
    # 全页仅 api() 一处原生 fetch（同源 + 内建 CSRF 头 + JSON 解析）。同 voice_call 结构性豁免。
    "tg_members.html": 1,
}


def _scan_counts() -> dict:
    counts = {}
    for p in sorted(TEMPLATES_ROOT.rglob("*.html")):
        rel = p.relative_to(TEMPLATES_ROOT).as_posix()
        n = len(_BARE_FETCH_RX.findall(p.read_text(encoding="utf-8", errors="replace")))
        if n:
            counts[rel] = n
    return counts


def test_bare_fetch_not_above_ceiling():
    """新增裸 fetch 即红：请改用 window.apiFetch(url, init, {timeoutMs})。"""
    counts = _scan_counts()
    offenders = []
    for rel, n in counts.items():
        ceiling = _BARE_FETCH_CEILINGS.get(rel, 0)
        if n > ceiling:
            offenders.append(f"{rel}: {n} > ceiling {ceiling}")
    assert not offenders, (
        "模板新增了裸 fetch 调用（超出 ratchet 天花板）。新代码请走统一层 "
        "window.apiFetch(url, init, {timeoutMs})（_api_fetch.html，含超时/遥测/重试），"
        "或该页既有的 _fetchWithTimeout 包装：\n  " + "\n  ".join(offenders)
    )


def test_bare_fetch_ledger_not_stale():
    """计数下降后账本必须同步调低——迁移进度可见，防天花板虚高被回填。"""
    counts = _scan_counts()
    stale = []
    for rel, ceiling in _BARE_FETCH_CEILINGS.items():
        actual = counts.get(rel, 0)
        if actual < ceiling:
            stale.append(f"{rel}: actual {actual} < ceiling {ceiling}（请把账本降到 {actual}）")
    assert not stale, (
        "裸 fetch 账本陈旧（实际计数已低于天花板），请在 "
        "tests/test_template_bare_fetch_ratchet.py 同步调低：\n  " + "\n  ".join(stale)
    )


# P1-1 起 apiFetch 挂载面扩展到全部根布局（base 家族 + 独立整页）。
# 列出的每个文件必须 include _api_fetch.html——漏挂＝该家族所有页面 ReferenceError 全灭。
# ops_overview.html 2026-08-03 P1 挂壳后 extends base.html（挂载随壳继承，经
# _resolve_root 的 extends 链解析自动纳入 base 家族口径），从根清单除名。
_WIRED_ROOTS = (
    "workspace_base.html",
    "base.html",
    "ops/contacts.html",
    "ops/merge_reviews.html",
    "ops/mobile_handoffs.html",
)


def test_api_fetch_foundation_wired():
    """基建契约：_api_fetch.html 必须定义 apiFetch/wsBanner，且各根布局头部挂载。"""
    partial = (TEMPLATES_ROOT / "_api_fetch.html").read_text(encoding="utf-8")
    assert "window.apiFetch" in partial, "_api_fetch.html 丢失 window.apiFetch 定义"
    assert "window.wsBanner" in partial, "_api_fetch.html 丢失 window.wsBanner 仲裁器"
    for root in _WIRED_ROOTS:
        txt = (TEMPLATES_ROOT / root).read_text(encoding="utf-8")
        assert '{% include "_api_fetch.html" %}' in txt, (
            f"{root} 未挂载 _api_fetch.html（该布局家族页面的 apiFetch 调用会全部 ReferenceError）"
        )
    base = (TEMPLATES_ROOT / "workspace_base.html").read_text(encoding="utf-8")
    idx_i18n = base.find('{% include "_i18n_bootstrap.html" %}')
    idx_api = base.find('{% include "_api_fetch.html" %}')
    assert 0 <= idx_i18n < idx_api, "_api_fetch.html 必须在 _i18n_bootstrap 之后、body 脚本之前挂载"


_EXTENDS_RX = re.compile(r"{%-?\s*extends\s+['\"]([^'\"]+)['\"]")
_API_FETCH_CALL_RX = re.compile(r"(?<![A-Za-z0-9_$.])apiFetch\s*\(")


def _resolve_root(rel: str, cache: dict) -> str:
    """沿 extends 链找到根布局（无 extends 即自身；循环防御截断）。"""
    seen = []
    cur = rel
    while cur not in seen:
        seen.append(cur)
        if cur in cache:
            parent = cache[cur]
        else:
            p = TEMPLATES_ROOT / cur
            m = _EXTENDS_RX.search(p.read_text(encoding="utf-8", errors="replace")) if p.exists() else None
            parent = m.group(1) if m else None
            cache[cur] = parent
        if not parent:
            return cur
        cur = parent
    return cur


def test_api_fetch_reachable_wherever_used():
    """挂载保障：任何用了 apiFetch( 的模板，其根布局（或自身）必须挂载 _api_fetch.html。

    防两类回归：新页面 extends 了未挂载的布局就用 apiFetch；或有人把根布局里的
    include 挪走——静态即红，别等运行时 ReferenceError 全页瘫痪。"""
    cache: dict = {}
    offenders = []
    for p in sorted(TEMPLATES_ROOT.rglob("*.html")):
        rel = p.relative_to(TEMPLATES_ROOT).as_posix()
        # 下划线开头＝partial（被 include 进已挂载的宿主文档，自身无布局链），
        # 由宿主的根布局保证可达（如 _rpa_shared_scripts 的宿主 RPA 页均 extends base）。
        if p.name.startswith("_"):
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        if not _API_FETCH_CALL_RX.search(txt):
            continue
        if '{% include "_api_fetch.html" %}' in txt:
            continue
        root = _resolve_root(rel, cache)
        root_txt = (TEMPLATES_ROOT / root).read_text(encoding="utf-8", errors="replace") \
            if (TEMPLATES_ROOT / root).exists() else ""
        if '{% include "_api_fetch.html" %}' not in root_txt:
            offenders.append(f"{rel}（根布局 {root} 未挂载）")
    assert not offenders, (
        "以下模板调用了 apiFetch 但其布局链上没有挂载 _api_fetch.html，"
        "运行时必抛 ReferenceError：\n  " + "\n  ".join(offenders)
    )
