# -*- coding: utf-8 -*-
"""有效档位单一事实源门禁（effective_automation P0，2026-08-07）。

守三件事：

1. **封顶语义**（纯函数）：平台 / 业务线 / 冷启动预热三层的判定、回落与
   折叠只降不升——与 B 线旧内联实现逐层等价（重构不许变行为）。
2. **四消费方接线**（静态）：B 线拟稿链、A 线档位闸、GET /automation 路由、
   why_no_reply CLI 必须都消费同一实现——「对人展示的口径」与「护栏行为」
   分叉是比不可见更糟的缺陷（approve_blocked 徽标同一哲学）。
3. **前端词条**：胶囊引用的 inbox.effcap.* 键 zh/en 双语齐备（模板里
   label 键是拼接引用，全库静态键门禁扫不到，这里显式钉住）。
"""
from __future__ import annotations

import time
from pathlib import Path

from src.inbox.effective_automation import (
    apply_mode_caps,
    business_line_ceilings_from_config,
    compute_mode_caps,
    effective_automation,
    platform_ceilings_from_config,
    serialize_caps,
)

NOW = 1_700_000_000.0
HOUR = 3600.0
SRC = Path(__file__).resolve().parents[1] / "src"


def _caps(**kw):
    """便捷封装：默认注入 business_line/connected_at，保持测试密闭
    （不触碰注册表单例/账号接入登记，见 conftest 隔离纪律）。"""
    kw.setdefault("platform", "telegram")
    kw.setdefault("account_id", "acct1")
    kw.setdefault("config", {})
    kw.setdefault("now", NOW)
    kw.setdefault("business_line", "")
    kw.setdefault("connected_at", NOW - 1000 * HOUR)
    return compute_mode_caps(**kw)


# ── 1. 封顶语义 ─────────────────────────────────────────────────────────

def test_old_account_no_caps():
    assert _caps() == []


def test_warmup_caps_new_account_with_until_ts():
    caps = _caps(connected_at=NOW - 5 * HOUR)
    assert [c.layer for c in caps] == ["warmup"]
    assert caps[0].ceiling == "review"
    # until_ts = connected_at + 72h（前端倒计时依据）
    assert abs(caps[0].until_ts - (NOW - 5 * HOUR + 72 * HOUR)) < 1.0


def test_warmup_unknown_connected_at_never_caps():
    """核心非回归（与 outbound_gate 同语义）：判不出账号年龄 → 绝不封顶。"""
    assert _caps(connected_at=0.0) == []
    assert _caps(connected_at=-1.0) == []


def test_warmup_switch_off_disables_cap():
    cfg = {"companion": {"proactive_topic": {"cold_start": {
        "warmup_review": False}}}}
    assert _caps(config=cfg, connected_at=NOW - 1 * HOUR) == []


def test_business_line_builtin_default_translation():
    caps = _caps(business_line="translation")
    assert [(c.layer, c.ceiling) for c in caps] == [("business_line", "review")]
    assert caps[0].detail == "translation"


def test_business_line_explicit_empty_map_disables():
    cfg = {"inbox": {"auto_draft": {"business_line_modes": {}}}}
    assert _caps(config=cfg, business_line="translation") == []


def test_business_line_unlabeled_account_no_cap():
    assert _caps(business_line="") == []


def test_platform_cap_from_live_config():
    cfg = {"inbox": {"auto_draft": {"platform_modes": {"messenger": "review"}}}}
    caps = _caps(platform="messenger", config=cfg)
    assert [(c.layer, c.ceiling) for c in caps] == [("platform", "review")]
    assert _caps(platform="telegram", config=cfg) == []


def test_platform_cap_fallback_snapshot_semantics():
    # 键缺席 → 用快照；显式 {} → 运营清空，快照失效（与 B 线活读语义逐字一致）
    snap = {"telegram": "review"}
    assert platform_ceilings_from_config({}, snap) == snap
    assert platform_ceilings_from_config(
        {"inbox": {"auto_draft": {"platform_modes": {}}}}, snap) == {}
    assert platform_ceilings_from_config(None, snap) == snap


def test_business_line_ceilings_priority_chain():
    # 活读 > 快照 > 内置默认
    live = {"inbox": {"auto_draft": {"business_line_modes": {"x": "manual"}}}}
    assert business_line_ceilings_from_config(live, {"y": "review"}) == {
        "x": "manual"}
    assert business_line_ceilings_from_config({}, {"y": "review"}) == {
        "y": "review"}
    assert business_line_ceilings_from_config({}, None) == {
        "translation": "review"}


def test_apply_caps_only_downgrades_and_reports_applied():
    caps = _caps(connected_at=NOW - 5 * HOUR)  # warmup→review
    mode, applied = apply_mode_caps("auto_ai", caps)
    assert mode == "review" and [c.layer for c in applied] == ["warmup"]
    # manual 不会被抬升；review 撞 review 不算生效
    assert apply_mode_caps("manual", caps) == ("manual", [])
    assert apply_mode_caps("review", caps) == ("review", [])


def test_apply_caps_first_effective_layer_wins():
    cfg = {"inbox": {"auto_draft": {"platform_modes": {"telegram": "review"}}}}
    caps = _caps(config=cfg, connected_at=NOW - 5 * HOUR)
    assert [c.layer for c in caps] == ["platform", "warmup"]
    mode, applied = apply_mode_caps("auto_ai", caps)
    assert mode == "review"
    assert [c.layer for c in applied] == ["platform"]  # 主因=先命中的层


# ── 2. resolver 出口（API / CLI 共用） ──────────────────────────────────

class _FakeStore:
    def __init__(self, mode=None):
        self._mode = mode

    def get_automation_mode_if_set(self, conversation_id):
        return self._mode


def test_effective_automation_explicit_mode_wins():
    out = effective_automation(
        _FakeStore("manual"), {}, conversation_id="c1", platform="telegram",
        account_id="a", now=NOW, business_line="", connected_at=NOW - 5 * HOUR)
    assert out["mode"] == "manual" and out["source"] == "explicit"
    # manual 已低于 review 封顶 → 无生效封顶，但 caps_all 仍可见（排障）
    assert out["effective_mode"] == "manual" and out["caps"] == []
    assert [c["layer"] for c in out["caps_all"]] == ["warmup"]


def test_effective_automation_global_auto_ai_capped_by_warmup():
    out = effective_automation(
        _FakeStore(None), {}, conversation_id="c1", platform="telegram",
        account_id="a", now=NOW, business_line="", connected_at=NOW - 5 * HOUR)
    assert out["mode"] == "auto_ai" and out["source"] == "global"
    assert out["effective_mode"] == "review"
    assert [c["layer"] for c in out["caps"]] == ["warmup"]


def test_effective_automation_base_mode_given_skips_store():
    out = effective_automation(
        None, {}, conversation_id="c1", platform="telegram", account_id="a",
        now=NOW, base_mode="auto_ai", business_line="translation",
        connected_at=NOW - 1000 * HOUR)
    assert out["source"] == "given"
    assert out["effective_mode"] == "review"
    assert [c["layer"] for c in out["caps"]] == ["business_line"]


def test_serialize_caps_json_safe():
    caps = _caps(connected_at=NOW - 5 * HOUR)
    ser = serialize_caps(caps)
    assert ser and set(ser[0]) == {"layer", "ceiling", "detail", "until_ts"}
    import json
    json.dumps(ser)


# ── 3. 四消费方接线（静态；防「写了模块没接线」的安全闸经典失效形态）────

def test_wired_b_line_autodraft():
    text = (SRC / "inbox" / "autodraft_helpers.py").read_text("utf-8")
    assert "compute_mode_caps" in text, "B 线未消费 effective_automation"
    # 封顶必须在 companion 双轨互斥判定之前（否则 A 线不让位、System Z 也不
    # 拟稿 = 198 那种「两边都让、无人拟稿」的静默丢回复）
    assert text.index("apply_mode_caps") < text.index(
        "allows_direct_autosend"), "档位封顶必须早于双轨互斥判定"


def test_wired_a_line_telegram_client():
    text = (SRC / "client" / "telegram_client.py").read_text("utf-8")
    assert "compute_mode_caps" in text, (
        "A 线档位闸未接封顶——companion 架构下新号预热期会照样直发"
        "（两种部署架构行为分叉，2026-08-04 挂账 P0）")
    assert "automation_capped" in text, "封顶让位应有独立计数（gate_stats）"


def test_wired_guard_budget_predicate():
    text = (SRC / "inbox" / "peer_bot_guard.py").read_text("utf-8")
    assert "compute_mode_caps" in text, (
        "守卫预算预判未折叠封顶——预热期 A 线不回但预算每条 +1，"
        "72h 能把日预算烧成幻影计数")


def test_wired_api_route():
    text = (SRC / "web" / "routes"
            / "unified_inbox_stored_read_routes.py").read_text("utf-8")
    assert "effective_automation" in text
    assert '"effective"' in text, "GET /automation 未携带 effective 段"


def test_wired_frontend_chip():
    tpl = (SRC / "web" / "templates" / "unified_inbox.html").read_text("utf-8")
    assert "mode-eff-chip" in tpl and "_renderEffChip" in tpl
    # 渲染函数必须消费 /automation 的 effective 段（旧后端无该段=隐藏，自洽）
    assert "d.effective" in tpl or "d&&d.effective" in tpl


def test_wired_cli_tool_read_only():
    p = Path(__file__).resolve().parents[1] / "tools" / "why_no_reply.py"
    text = p.read_text("utf-8")
    assert "effective_automation" in text
    assert "mode=ro" in text, "排障 CLI 必须只读打开生产库"
    # 禁真实导入/实例化（docstring 提及不算）：构造 InboxStore 会跑
    # migration = 对生产库写事务
    assert "from src.inbox.store import" not in text
    assert "InboxStore(" not in text


def test_chain_integrity_effective_module():
    """resolver 自身必须真的串到三层判定源（防重构后变成空壳）。"""
    text = (SRC / "inbox" / "effective_automation.py").read_text("utf-8")
    for symbol in ("automation_ceiling", "resolve_account_connected_at",
                   "cached_business_line", "cap_automation_mode"):
        assert symbol in text, f"effective_automation 丢失判定源 {symbol}"


# ── 4. 前端词条（拼接键，全库静态键门禁扫不到，这里显式钉住）────────────

def test_effcap_i18n_keys_bilingual():
    from src.web.i18n_packs import collect_packs
    pzh, pen, _ = collect_packs()
    for key in ("inbox.effcap.warmup", "inbox.effcap.warmup_t",
                "inbox.effcap.platform", "inbox.effcap.platform_t",
                "inbox.effcap.business_line", "inbox.effcap.business_line_t",
                "inbox.effcap.generic", "inbox.effcap.generic_t"):
        assert key in pzh, f"zh 缺 {key}"
        assert key in pen, f"en 缺 {key}"


# ── 5. 行为等价抽查：resolver 折叠结果 == B 线逐层 cap 的旧算法 ─────────

def test_parity_with_legacy_inline_order():
    from src.inbox.drafts import cap_automation_mode

    cfg = {"inbox": {"auto_draft": {
        "platform_modes": {"messenger": "review"},
        "business_line_modes": {"translation": "review"},
    }}}
    for base in ("auto_ai", "review", "multi_choice", "manual"):
        for plat in ("telegram", "messenger"):
            for bl in ("", "translation"):
                for conn in (0.0, NOW - 5 * HOUR, NOW - 100 * HOUR):
                    caps = compute_mode_caps(
                        platform=plat, account_id="a", config=cfg, now=NOW,
                        business_line=bl, connected_at=conn)
                    got, _ = apply_mode_caps(base, caps)
                    # 旧算法：平台 → 业务线 → 预热逐层 cap_automation_mode
                    want = base
                    pm = cfg["inbox"]["auto_draft"]["platform_modes"].get(plat)
                    if pm:
                        want = cap_automation_mode(want, pm)
                    if bl:
                        blc = cfg["inbox"]["auto_draft"][
                            "business_line_modes"].get(bl)
                        if blc:
                            want = cap_automation_mode(want, blc)
                    from src.inbox.outbound_gate import (
                        automation_ceiling,
                        resolve_cold_start_cfg,
                    )
                    warm = automation_ceiling(
                        conn, NOW, resolve_cold_start_cfg(cfg))
                    if warm:
                        want = cap_automation_mode(want, warm)
                    assert got == want, (
                        f"resolver 与旧内联算法分叉: base={base} plat={plat} "
                        f"bl={bl} conn={conn}: {got} != {want}")
