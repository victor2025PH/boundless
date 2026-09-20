"""打包能力对账单门禁（实施49 P0-5，2026-08-20）。

内测反馈里反复出现同一形态：**演示机有、客户包里没有，而界面上没有任何交代**
（B1「语音克隆点了没反应」是标本）。既有两条门禁只管住了配置层的两端——
``test_desktop_seed_visibility``（A 类必须开 / C 类禁入种子）与
``test_seed_switch_upgrade_coverage``（种子里开着的开关必须有升级路径），
中间那段「**没开的那些，客户在界面上会遇到什么**」一直没人守：功能可以静静地
关着，UI 可以静静地留着入口，两边各自「正确」，合起来就是死按钮。

本门禁把这段补上：客户打包视图下**每一个不为 on 的注册能力**，都必须在下面的
对账单里声明一种处置，且处置本身可被代码验证——

- ``hosted_replacement``：本地引擎不给，但托管网关顶上（客户实际有这个能力）。
  证据＝替代标记键在客户视图里为真（如 ``avatar_voice._hosted_auto``）。
- ``ui_unavailable``：功能总览里**看得见的锁**——客户能发现它、并被告知缺什么
  或为什么没开放。证据＝``show=True`` 且 C 类有 reason 码 / B 类有依赖码或本就
  可直接开。
- ``ui_hidden``：UI 一个入口都不露。证据＝``show=False``；若该能力还占着侧栏
  页面，另由 nav 断言钉住客户档下那条路径确实不出现（死入口正是本门禁的靶心）。

**未登记即红**（双向）：新增能力没给处置 → 红；能力已开或已下架而登记还赖着
→ 红（陈旧台账比没有台账更误导）。判据全部纯函数读配置，零网络零重启风险。

客户视图＝随包种子 + A 类基线补齐（``ConfigManager._ensure_baseline`` 的口径），
刻意**不叠** ``config.local.yaml``：客户机上没有那层，演示机的 overlay 正是本
门禁要防的东西。profile（``config/profiles/*.yaml``）经核实不改动注册表键集，
故不参与合成。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

from src.utils.feature_registry import (
    FEATURES,
    Feature,
    as_nested,
    baseline_patch,
    by_key,
    dig,
    feature_state,
)

ENGINE_ROOT = Path(__file__).resolve().parent.parent
SEED = ENGINE_ROOT / "config" / "config.desktop.min.yaml"

HOSTED = "hosted_replacement"
VISIBLE_LOCK = "ui_unavailable"
HIDDEN = "ui_hidden"
_KINDS = (HOSTED, VISIBLE_LOCK, HIDDEN)


#: 能力键 → (处置, 证据键, 人话理由)。证据键仅 hosted_replacement 用。
#: 改动这张表＝对客户的交付承诺变了，请连同 docs/实施49 一起更新。
_PACKAGED_CAPABILITY_LEDGER: Dict[str, Tuple[str, str, str]] = {
    # ── 托管替代：本地引擎不随包，官网网关顶上，客户实际有这个能力 ──────────
    "avatar_voice.enabled": (
        HOSTED, "avatar_voice._hosted_auto",
        "本地 TTS 集群不随包；持设备令牌的部署由 hosted_gateway 运行时接官方"
        "语音网关（内测 B1「语音克隆锁着」的正解是托管顶上，不是解锁本地引擎）"),

    # ── 看得见的锁：功能总览里可发现，并告知缺什么 / 为什么没开放 ───────────
    "memory.vector.enabled": (
        VISIBLE_LOCK, "", "向量召回要嵌入端点，客户没配时总览显示缺依赖 embedding"),
    "translation.engines.confidence_switch.enabled": (
        VISIBLE_LOCK, "", "置信度切换要至少两个翻译引擎，总览显示缺依赖"),
    "inbox.l2_autosend.translate.enabled": (
        VISIBLE_LOCK, "", "出站自动翻译零依赖，总览里可直接开（默认关是安全选择）"),
    "personas.quiz.enabled": (
        VISIBLE_LOCK, "", "人设自测零依赖，总览里可直接开"),
    "accounts.profile_push.enabled": (
        VISIBLE_LOCK, "", "账号资料下发零依赖，总览里可直接开"),
    "companion.selfie.enabled": (
        VISIBLE_LOCK, "", "相册/出图绑本机集群，总览显示未开放原因"),
    "companion.proactive_topic.enabled": (
        VISIBLE_LOCK, "", "主动触达属账号风险行为，总览显示未开放原因"),
    "companion.bazi.enabled": (
        VISIBLE_LOCK, "", "命理技能待产品拍板，总览显示未开放原因"),

    # ── 完全隐藏：客户界面零入口（含侧栏页面，另有 nav 断言）─────────────────
    "monetization.enabled": (
        HIDDEN, "", "变现未开＝空页，nav 按 monetization.enabled 过滤掉「客户营收」"),
    "line_rpa.enabled": (
        HIDDEN, "", "真机 RPA 不随包；侧栏五项跟 ui_visibility.matrix_nav（默认隐藏）"),
    "messenger_rpa.enabled": (
        HIDDEN, "", "同上，真机矩阵导航默认隐藏"),
    "whatsapp_rpa.enabled": (
        HIDDEN, "", "同上，真机矩阵导航默认隐藏"),
    "realtime_voice.enabled": (
        HIDDEN, "", "实时语音绑本机集群，无 UI 入口"),
    "speech_emotion.enabled": (
        HIDDEN, "", "声学情绪绑本机 GPU，纯后台增强、本就无 UI 入口"),
    "ai.fallback.enabled": (
        HIDDEN, "", "本地 LLM 兜底绑本机集群，纯后台容灾、无 UI 入口"),
    "ops.gpu_watermark.enabled": (
        HIDDEN, "", "LAN GPU 水位是自建集群运维项，客户无此拓扑、ops 卡整卡隐藏"),
    "ops.cloud_credentials.enabled": (
        HIDDEN, "", "云端凭证体检针对自备 Key 的自建部署，托管客户无此面"),
    # inbox.l2_autosend.deliver：2026-08-22 拍板 C→B 且种子出厂即开（全自动
    # 开箱）——客户视图里已是 on，无需处置行（stale 门禁会点名多余登记）。
}

#: 占着侧栏页面的隐藏项 → 客户档下**不得出现**的路径（死入口靶心）。
_HIDDEN_NAV_PATHS: Dict[str, str] = {
    "monetization.enabled": "/monetization",
    "line_rpa.enabled": "/workspace/channels/line",
    "messenger_rpa.enabled": "/workspace/channels/messenger",
    "whatsapp_rpa.enabled": "/workspace/channels/whatsapp",
}


# ── 客户打包视图 ─────────────────────────────────────────────────────────────

def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _customer_cfg() -> Dict[str, Any]:
    cfg = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    assert isinstance(cfg, dict) and cfg, "种子必须是非空 YAML mapping"
    return _deep_merge(cfg, as_nested(baseline_patch(cfg)))


def _not_on(cfg: Dict[str, Any]) -> List[Feature]:
    return [f for f in FEATURES if feature_state(f, cfg) != "on"]


# ── 处置判据（纯函数，供门禁与自证共用）────────────────────────────────────

def _visible_lock_defect(f: Feature, cfg: Dict[str, Any]) -> str:
    """看得见的锁：能被发现 + 能被解释。返回缺陷描述，空串=合格。"""
    if not f.show:
        return "show=False——总览里根本看不见，客户无从发现，应改登记为 ui_hidden"
    if f.cls == "C" and not f.reason:
        return "C 类缺 reason 码——总览只能显示一把没有说明的锁"
    if f.cls == "B" and not f.requires and feature_state(f, cfg) != "available":
        return "B 类既没有依赖码也不可直接开——总览无法告诉客户下一步做什么"
    return ""


def _hidden_defect(f: Feature) -> str:
    if f.show:
        return "show=True——总览里露着，与「完全隐藏」的登记自相矛盾"
    return ""


def _hosted_defect(evidence: str, cfg: Dict[str, Any]) -> str:
    if not evidence:
        return "hosted_replacement 必须给证据键（托管替代的标记）"
    if not bool(dig(cfg, evidence)):
        return f"证据键 {evidence} 在客户视图里不为真——托管替代并没有真的到货"
    return ""


# ── 门禁 ─────────────────────────────────────────────────────────────────────

def test_PACKAGED_CAPABILITY_LEDGER_kinds_are_valid():
    bad = {k: v[0] for k, v in _PACKAGED_CAPABILITY_LEDGER.items() if v[0] not in _KINDS}
    assert not bad, f"非法处置类型：{bad}（合法值 {_KINDS}）"
    assert all(v[2].strip() for v in _PACKAGED_CAPABILITY_LEDGER.values()), "每条登记必须写人话理由"


def test_every_not_on_capability_is_dispositioned():
    """未登记即红：客户拿不到的能力，必须先想清楚界面上怎么交代。"""
    cfg = _customer_cfg()
    missing = {f.key: f.cls for f in _not_on(cfg) if f.key not in _PACKAGED_CAPABILITY_LEDGER}
    assert not missing, (
        "以下能力在客户打包视图里不为 on，却没在对账单里声明处置——\n"
        "客户会遇到「功能不存在且无任何说明」（内测 B1 同型）。请三选一登记：\n"
        f"  hosted_replacement（托管顶上）/ ui_unavailable（看得见的锁）/ ui_hidden（零入口）\n"
        + "\n".join(f"  {k}（{c} 类）" for k, c in sorted(missing.items())))


def test_PACKAGED_CAPABILITY_LEDGER_has_no_stale_rows():
    """陈旧台账比没有台账更误导：能力已开或已下架，登记必须同步删。"""
    cfg = _customer_cfg()
    universe = {f.key for f in _not_on(cfg)}
    stale = {}
    for key in _PACKAGED_CAPABILITY_LEDGER:
        f = by_key(key)
        if f is None:
            stale[key] = "该键已不在 feature_registry 里（能力下架？键改名？）"
        elif key not in universe:
            stale[key] = f"客户视图里已是 on（state={feature_state(f, cfg)}），无需处置"
    assert not stale, "对账单有陈旧条目：\n" + "\n".join(
        f"  {k} —— {why}" for k, why in sorted(stale.items()))


def test_dispositions_hold_up():
    """逐条验处置：登记说的那种交代，代码里必须真的成立。"""
    cfg = _customer_cfg()
    defects = {}
    for key, (kind, evidence, _why) in _PACKAGED_CAPABILITY_LEDGER.items():
        f = by_key(key)
        if f is None:
            continue                      # 由 stale 门禁点名，不重复报
        if kind == VISIBLE_LOCK:
            d = _visible_lock_defect(f, cfg)
        elif kind == HIDDEN:
            d = _hidden_defect(f)
        else:
            d = _hosted_defect(evidence, cfg)
        if d:
            defects[key] = f"[{kind}] {d}"
    assert not defects, "处置与代码实况不符：\n" + "\n".join(
        f"  {k} —— {d}" for k, d in sorted(defects.items()))


def test_hidden_capabilities_leave_no_nav_entry():
    """占着页面的隐藏项：客户档下侧栏/命令面板不得出现那条路径。

    这是本门禁唯一的「真死入口」断言——能力关着而入口留着，点进去是空页，
    正是内测反馈里最败好感的一类。
    """
    from src.web.nav_schema import NAV_ITEMS, get_nav_context

    cfg = _customer_cfg()
    ctx = get_nav_context(cfg)

    def _paths(items) -> List[str]:
        out = []
        for it in items or []:
            if isinstance(it, dict):
                p = it.get("path")
            else:
                p = (NAV_ITEMS.get(it) or {}).get("path")
            if p:
                out.append(p)
        return out

    seen = set()
    for g in ctx.get("nav_groups") or []:
        seen.update(_paths(g.get("items")))
    for bucket in ("nav_simple_core", "nav_simple_more",
                   "nav_matrix_items", "nav_cmd_items"):
        seen.update(_paths(ctx.get(bucket)))

    leaked = {k: p for k, p in _HIDDEN_NAV_PATHS.items() if p in seen}
    assert not leaked, (
        "以下能力在客户档里关着，导航却留着入口（点进去＝空页）：\n"
        + "\n".join(f"  {k} → {p}" for k, p in sorted(leaked.items())))


def test_nav_probe_detects_a_leak():
    """探测器有效性自证：把 matrix_nav 打开，上面那条断言必须能抓到入口回归。"""
    from src.web.nav_schema import NAV_ITEMS, get_nav_context

    cfg = _customer_cfg()
    cfg.setdefault("ui_visibility", {})["matrix_nav"] = True
    ctx = get_nav_context(cfg)
    paths = set()
    for g in ctx.get("nav_groups") or []:
        for it in g.get("items") or []:
            p = it.get("path") if isinstance(it, dict) else (
                NAV_ITEMS.get(it) or {}).get("path")
            if p:
                paths.add(p)
    assert "/workspace/channels/line" in paths, (
        "matrix_nav 打开后 LINE 入口仍不出现——nav 断言探了个寂寞，判据需重写")


def test_disposition_predicates_reject_bad_shapes():
    """判据自证：三个处置的缺陷检测器对反例必须点名（防判据写成恒真）。"""
    cfg = _customer_cfg()
    hidden_but_shown = Feature(key="x.y", cls="C", slug="_t1", note="t",
                               reason="lan", show=True)
    assert _hidden_defect(hidden_but_shown), "露着的功能不该通过 ui_hidden"
    lock_without_reason = Feature(key="x.y", cls="C", slug="_t2", note="t",
                                  reason="", show=True)
    # 注册表 _validate 不允许 C 类空 reason 入表，这里只测判据本身。
    assert _visible_lock_defect(lock_without_reason, cfg), "无 reason 的锁不该通过"
    assert _hosted_defect("nope.not.here", cfg), "不存在的证据键不该通过 hosted"
    assert not _hosted_defect("avatar_voice._hosted_auto", cfg), (
        "真实存在的托管标记应当通过——判据过严会逼人乱登记")

