# -*- coding: utf-8 -*-
"""授权分层运维手册（docs/授权分层与功能锁运维手册.md）关键事实一致性门禁。

哲学：手册是手写的操作叙事（生成器换不来），但**手册里的硬事实不许说谎**——
档位注册表、配置键名、指标锚点变了而手册没跟上，这里先红。只钉「读者会照着
操作/照着查」的锚点，不钉措辞（措辞自由，事实从严）。
"""

from pathlib import Path

_DOC = Path(__file__).resolve().parents[1] / "docs" / "授权分层与功能锁运维手册.md"


def _text() -> str:
    assert _DOC.exists(), f"运维手册缺失: {_DOC}"
    return _DOC.read_text(encoding="utf-8")


def test_doc_pins_workflows_tier():
    """手册写的 workflows 档位必须等于注册表现值（改档必须同步手册）。"""
    from src.licensing.feature_gate import FEATURE_MIN_PLAN
    tier = FEATURE_MIN_PLAN.get("workflows")
    assert tier, "workflows 不在 FEATURE_MIN_PLAN——手册与注册表同时失效"
    assert f"workflows={tier}" in _text(), (
        f"手册的档位事实过期：应写 workflows={tier}（注册表现值）")


def test_doc_mentions_all_plans_in_order():
    from src.licensing.feature_gate import PLAN_ORDER
    text = _text()
    for plan in PLAN_ORDER:
        assert plan in text, f"手册缺档位名 {plan}"


def test_doc_pins_config_keys():
    """读者会照抄的三个配置键：拼错一个，客户机就配不出来。"""
    text = _text()
    for key in ("licensing.feature_gate.enabled", "plan_override",
                "inbox.workflows.enabled", "licensing.shop_url"):
        assert key in text, f"手册缺配置键 {key}"


def test_doc_pins_metric_anchors():
    """三个读数锚点 + 两个读出面（照着查的路径必须真实存在于代码）。"""
    text = _text()
    for anchor in ("feature_lock", "rec_follow", "attr_reply_rate",
                   "/api/workspace/metrics", "/api/workspace/chain-funnel",
                   "feature_lock_hits_"):
        assert anchor in text, f"手册缺指标锚点 {anchor}"


def test_doc_pins_lock_ux_anchors():
    """锁定态叙事的可验证锚点：来源参数 + 升级页 + 双映射文件名。"""
    text = _text()
    for anchor in ("?from=", "/membership", "GOAL_CHAIN_REC",
                   "sidebar-chrome.js", "workflow_starter.py",
                   "FEATURE_MIN_PLAN"):
        assert anchor in text, f"手册缺锚点 {anchor}"
