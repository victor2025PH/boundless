# -*- coding: utf-8 -*-
"""出站事实守卫的**覆盖面**门禁（2026-07-28 预检实锤的漏洞）。

事故本体：``_apply_goal_link_guard`` 原先第一行就是
``info = pop("_goal_cta"); if not isinstance(info, dict): return reply``——
而那份暂存只有**建了漏斗目标**的会话才有。``companion.goals.auto_create`` 带
日预算（``max_per_day``，本机 20），预算一打满，新会话全部拿不到暂存，于是
「报价与目录不符 / 试用时长编造 / gated 线泄漏 / 未授权折扣」这些**与漏斗目标
毫无关系的商业事实**守卫一起静默失效。

实测证据（6 场 × 8 轮预检，仅第 1 场赶在预算内建了目标）：
- 有目标那场：0 缺陷
- 其余 5 场：七折 / 年付85折 / 14 天试用 / 免费图片换脸 / 免费一个月 全出街

本门禁钉两条不变量：
1. **无目标会话也必须守事实**（回归即红）。
2. **无目标会话用保守口径**（``product_context=False``）——否则纯陪聊里人设
   自家生意报价（「芒果干大包 450 比索」）会被当越权报价剥掉。误伤人设日常
   对话比漏抓一次越权报价更糟：前者天天发生。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import pytest

from src.skills.skill_manager import (
    _guard_offer_claims,
    _guard_outbound_claims,
)

# 与生产 site_catalog 同口径的最小事实集：ChatX 团队版 $198 / 入门 $58，
# 授权免费时长只有 7 天（14 天属另一条产品线，不该由苏婉承诺）。
FACTS: Dict[str, Any] = {
    "offer_texts": ["注册领 7 天完整版 · 2.5 万字符，绑定本机使用"],
    "offer_free_days": [7.0],
    "catalog_prices": [198.0, 58.0],
}


class _Cfg:
    """最小 ConfigManager 替身（守卫只读 config / config_path）。"""

    def __init__(self, root: Optional[Dict[str, Any]] = None):
        self.config = root if root is not None else {}
        self.config_path = None


class _SM:
    """只借 SkillManager 的 ``_apply_goal_link_guard``（不构造整个引擎）。"""

    def __init__(self, cfg_root: Optional[Dict[str, Any]] = None):
        self.config = _Cfg(cfg_root)
        self.logger = logging.getLogger("test.guard_coverage")

    from src.skills.skill_manager import (  # type: ignore[misc]
        SkillManager as _Real,
    )
    _apply_goal_link_guard = _Real._apply_goal_link_guard


@pytest.fixture()
def sm(monkeypatch):
    """无目标会话走目录事实兜底 → patch 掉真实读盘，注入 FACTS。"""
    import src.companion.goals.service as svc
    monkeypatch.setattr(svc, "catalog_guard_facts",
                        lambda *a, **k: dict(FACTS))
    return _SM()


# ── 不变量 1：无目标会话也守事实 ────────────────────────────────────────
@pytest.mark.parametrize("text,must_go", [
    ("官网有14天客户端试用，装上就能跑一遍你的业务流", "14天"),
    ("那我私下给你个优惠码，年付85折，别往外说", "85折"),
    ("官网bd2026.cc有免费图片换脸，你上传产品图试试效果", "换脸"),
    ("这样吧，免费一个月给你试，效果好再付", "免费一个月"),
])
def test_no_goal_context_still_guards(sm, text, must_go):
    """预检实录原句：无 ``_goal_cta`` 时守卫必须仍然处置。"""
    uc: Dict[str, Any] = {}          # 刻意不放 _goal_cta
    out = sm._apply_goal_link_guard(text, uc, log_prefix="[t]")
    assert must_go not in out, f"无目标会话漏抓：{must_go!r} 仍在 {out!r}"


def test_no_goal_context_keeps_authorized_trial(sm):
    """真实授权的 7 天试用不许被当成编的剥掉（守卫按事实不按措辞）。"""
    text = "注册领 7 天完整版，2.5 万字符，绑定本机使用"
    out = sm._apply_goal_link_guard(text, {}, log_prefix="[t]")
    assert "7 天" in out


# ── 不变量 2：无目标会话用保守口径，不误伤人设自家生意 ──────────────────
def test_no_goal_context_does_not_flag_persona_own_prices(sm):
    """纯陪聊里人设自家报价无产品/套餐词 → 必须原样放行。

    这是 ``product_context`` 不能在无目标会话恒 True 的原因：那会放宽
    「产品词同句」要求，把「450比索」当成我方越权报价。
    """
    text = "我店里芒果干大包450比索，小包250，都是自己晒的"
    out = sm._apply_goal_link_guard(text, {}, log_prefix="[t]")
    assert out == text


def test_no_goal_context_does_not_flag_roi_talk(sm):
    """ROI 话术谈的是**客户的钱**，不是我方报价 → 放行。"""
    text = "你三个客服一个月工资就4500块，一个月能帮你省下2000块人力成本"
    out = sm._apply_goal_link_guard(text, {}, log_prefix="[t]")
    assert out == text


# 陪聊里第三方折扣：措辞轴（N折）不问「谁在打折」，扩到全会话后会把这类句子
# 剥成半截。覆盖扩大当天实测 3/12 误报 → 加 mentions_our_commerce 判据后归零。
# 陪聊质量是主业，误伤这类句子比漏抓一次越权折扣更贵：前者天天发生。
@pytest.mark.parametrize("text", [
    "楼下那家奶茶店今天打八折，我下班去买一杯",
    "我看中的裙子五折了，终于降价啦",
    "超市晚上八点以后面包都七折，我常去捡漏",
    "双十一我买猫粮打了个折，省了两百多",
    "我上个月机票打折才买的，两千多",
    "下次你来我请你喝咖啡，免费的哈哈",
    "我送你一包芒果干，不要钱",
    "咖啡一杯120比索，房租一个月两万五，压力挺大的",
])
def test_no_goal_context_leaves_companion_chat_alone(sm, text):
    out = sm._apply_goal_link_guard(text, {}, log_prefix="[t]")
    assert out == text, f"陪聊被误伤：{text!r} → {out!r}"


@pytest.mark.parametrize("text,must_go", [
    ("我给你打个八折吧，就当交个朋友", "八折"),
    ("七折按年付给我这边确实给不了，年付85折已经是公司底价了", "85折"),
    ("我给你个优惠码 SAVE20，别往外说", "SAVE20"),
])
def test_no_goal_context_still_catches_our_own_discounts(sm, text, must_go):
    """收窄判据不许把**我方**折扣承诺一起放过（否则修误报变成了拆守卫）。"""
    out = sm._apply_goal_link_guard(text, {}, log_prefix="[t]")
    assert must_go not in out, f"我方折扣漏抓：{must_go!r} 仍在 {out!r}"


@pytest.mark.parametrize("text", [
    "我私下给你个优惠码，别往外说",
    "关注我们Telegram频道再进交流群，就有专属折扣码解锁",
])
def test_bare_coupon_promise_is_out_of_deterministic_scope(sm, text):
    """已知边界（如实钉住，不是漏洞）：**无码**的券码承诺措辞轴不剥。

    ``offer_guard`` 的券码轴要求「优惠码 + 真码」才判——「关注频道就有折扣码」
    是否属实取决于运营当下有没有在跑频道促销，目录里无从核对；确定性守卫在这里
    宁可放行，误剥一句真实的引流话术比漏一句模糊承诺更贵。真要收口应走目录
    登记（``claims.texts`` 白名单化频道促销口径）而不是加正则猜。
    """
    out = sm._apply_goal_link_guard(text, {}, log_prefix="[t]")
    assert out == text


@pytest.mark.parametrize("text,expected", [
    # 第三方 / 陪聊 → 不是我方商业事项
    ("楼下那家奶茶店今天打八折", False),
    ("我看中的裙子五折了", False),
    ("今天客人特别多，累死了", False),
    ("下次你来我请你喝咖啡，免费的哈哈", False),
    # 我方给出动作
    ("我给你打个八折吧", True),
    ("我私下给你个优惠码", True),
    # 我方商业语境（无第二人称的定价陈述——预检里漏抓的那类）
    ("年付85折已经是公司底价了", True),
    ("官网有14天客户端试用", True),
    ("注册领 7 天完整版，2.5 万字符", True),
    # 产品名
    ("算下来智聊团队版一个月168就够了", True),
])
def test_mentions_our_commerce_discriminator(text, expected):
    """判据本体（纯函数）：15 例校准全对时才敢拿它当措辞轴的开关。"""
    from src.companion.goals.claim_guard import mentions_our_commerce
    assert mentions_our_commerce(text) is expected, text


# ── 目标会话行为不变（零回归） ──────────────────────────────────────────
def test_goal_context_unchanged_and_cross_turn_still_strict():
    """有 ``_goal_cta`` → 跨轮口径仍生效（产品词在上一轮也抓得到 168）。"""
    sm2 = _SM()
    uc = {"_goal_cta": dict(FACTS, cta="", base_url="", order_path="/order")}
    out = sm2._apply_goal_link_guard(
        "算下来一个月也就168，比你请人便宜多了", uc, log_prefix="[t]")
    assert "168" not in out


def test_goal_context_stash_is_consumed():
    """``_goal_cta`` 读后即焚（老不变量，别在改覆盖面时弄丢）。"""
    sm2 = _SM()
    uc = {"_goal_cta": dict(FACTS, cta="", base_url="", order_path="/order")}
    sm2._apply_goal_link_guard("随便说点什么", uc, log_prefix="[t]")
    assert "_goal_cta" not in uc


# ── 兜底安全性：目录空 / 兜底不可用 → 零影响 ────────────────────────────
def test_empty_catalog_is_noop(monkeypatch):
    import src.companion.goals.service as svc
    monkeypatch.setattr(svc, "catalog_guard_facts", lambda *a, **k: {
        "offer_texts": [], "offer_free_days": [], "catalog_prices": []})
    sm2 = _SM()
    text = "官网有14天客户端试用"
    assert sm2._apply_goal_link_guard(text, {}, log_prefix="[t]") == text


def test_fallback_exception_is_noop(monkeypatch):
    """兜底自身抛异常绝不能吃掉回复（守卫故障 ≠ 不许说话）。"""
    import src.companion.goals.service as svc

    def _boom(*a, **k):
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(svc, "catalog_guard_facts", _boom)
    sm2 = _SM()
    text = "官网有14天客户端试用"
    assert sm2._apply_goal_link_guard(text, {}, log_prefix="[t]") == text


def test_kill_switch_disables_claim_guard(sm):
    """``claim_guard.enabled=false`` → 事实轴整体让位（应急回退口）。

    用 ``sm`` fixture 只为借它对 ``catalog_guard_facts`` 的 patch：证明「事实
    白名单确实到位、却因开关关掉而不处置」，而不是因为读不到目录才没动。
    """
    assert sm is not None
    sm3 = _SM({"companion": {"goals": {"claim_guard": {"enabled": False}}}})
    text = "官网有14天客户端试用，装上就能跑"
    out = sm3._apply_goal_link_guard(text, {}, log_prefix="[t]")
    assert "14天" in out          # 事实轴让位（offer_guard 开关独立，不受影响）


def test_product_context_param_respects_kill_switch():
    """``price_cross_turn=false`` → 即使有目标也回单条口径（不看跨轮）。"""
    cfg = {"companion": {"goals": {"claim_guard": {"price_cross_turn": False}}}}
    out = _guard_outbound_claims(
        "算下来一个月也就168", dict(FACTS), cfg_root=cfg,
        logger=None, log_prefix="", product_context=True)
    assert "168" in out       # 无产品词同句 + 跨轮关 → 不校验


def test_offer_guard_signature_accepts_facts_dict():
    """``_guard_offer_claims`` 只读 info 里两把白名单键（契约钉）。"""
    out = _guard_offer_claims(
        "给你打个八折吧", dict(FACTS), cfg_root={},
        logger=None, log_prefix="")
    assert "八折" not in out


def test_facts_keys_match_goal_cta_contract():
    """目录事实兜底的键名必须与 ``_goal_cta`` 一致，否则守卫读到空白名单
    却「看起来在跑」——静默失效比报错更贵。"""
    from src.companion.goals.service import catalog_guard_facts
    keys = set(catalog_guard_facts({}, None).keys())
    assert {"offer_texts", "offer_free_days", "catalog_prices"} <= keys
