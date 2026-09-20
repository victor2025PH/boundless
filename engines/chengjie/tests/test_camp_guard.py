# -*- coding: utf-8 -*-
"""#147 贬损自家阵营推广——登记表 / 出站守卫 / prompt 块 / 接线 四层门禁。

实录（2026-09-02 群截图 6834964252_1033/_1034，LINE 客户 tisay × Kevin 账号 /
Claire Bennett 人设，全自动档）：客户提到收件箱里的佳士得夏季拍卖推广（我方其他
人设在推：夏季拍卖 + 充值奖励 + 等级），AI 连续四条出站贬损——
  「Those silly top-up bonuses」/「reads like a flashy ad more than anything real」/
  「auction houses don't beg for deposits like that」——客户顺势回「跟垃圾邮件竞争」，
AI 还接「I'll take that as a win」。机制：人设事实层没有「自家在推的活动/产品」白名单，
LLM 反诈直觉对推广素材开火；跨人设商业事实不共享。

修两层：① 人设 prompt 注入「自家在推活动」硬约束块（登记表与 P13/P15 目录守卫同源
＝site_catalog.yaml 的 camp_promotions 段）；② 出站贬损守卫（自家词/机制泛词 + 负面
定性词 → 整句剥离），挂载点与 claim_guard 同（``_apply_goal_link_guard``，A/B 线共用）。
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

from src.companion.goals import offers as offers_mod
from src.companion.goals.claim_guard import (
    camp_fallback,
    find_camp_disparagement,
    sanitize_camp_disparagement,
)

# 与实录同形的登记表（zhiliao 目录默认为空；这里是运营登记后的形态）
CATALOG = {
    "site": {"base_url": "https://bd2026.cc"},
    "products": [
        {"id": "chatx", "name_zh": "智聊 ChatX", "name_en": "ChatX"},
        {"id": "lingox", "name_zh": "通译 LingoX", "name_en": "LingoX"},
    ],
    "offers": [],
    "camp_promotions": [
        {
            "id": "christies-summer-2609", "enabled": True, "authorized_by": "jun",
            "label_zh": "佳士得夏季拍卖·充值奖励活动",
            "label_en": "Christie's summer auction top-up bonus",
            "keywords": ["佳士得", "Christie's", "Christies", "夏季拍卖",
                         "summer auction", "充值奖励", "top-up bonus"],
            "note_zh": "同事账号在推的正规活动，细则以官方公告为准",
        },
        # 护栏反例：未 enabled / 无 authorized_by / 已过期 → 一律忽略
        {"id": "off", "enabled": False, "authorized_by": "x", "label_zh": "关掉的活动"},
        {"id": "noauth", "enabled": True, "label_zh": "没人批的活动"},
        {"id": "expired", "enabled": True, "authorized_by": "x",
         "valid_until": "2020-01-01", "label_zh": "过期活动"},
    ],
}
TERMS = offers_mod.camp_terms(CATALOG)
# 客户本条/近轮：提到了登记活动（跨轮语境的锚）
CTX = ("Got an email from Christie's about their summer auction — top-up "
       "bonuses, tiers, the whole thing. Reads like an ad honestly")

# 实录四句（第四句「I'll take that as a win」无贬损词，刻意作为**不抓**的边界）
INCIDENT = [
    "Those silly top-up bonuses though, who falls for that",
    "It reads like a flashy ad more than anything real.",
    "Real auction houses don't beg for deposits like that.",
]


# ── ① 登记表（offers.camp_promotions / camp_terms）──────────────────────────
def test_camp_promotions_guardrails():
    """enabled 显式 true + authorized_by 留痕 + 未过期，三者缺一即忽略。"""
    rows = offers_mod.camp_promotions(CATALOG)
    assert [r["id"] for r in rows] == ["christies-summer-2609"]
    assert rows[0]["keywords"][0] == "佳士得"
    assert rows[0]["valid_until_ts"] == 0.0      # 未给 valid_until = 长期


def test_camp_promotions_valid_until_honoured():
    cat = {"camp_promotions": [{
        "id": "x", "enabled": True, "authorized_by": "a",
        "valid_until": "2099-12-31", "label_zh": "长期活动"}]}
    assert offers_mod.camp_promotions(cat)
    assert offers_mod.camp_promotions(
        cat, now=time.mktime(time.strptime("2100-01-02", "%Y-%m-%d"))) == []


def test_camp_terms_union_of_three_sources():
    """词表 = 登记活动 label/关键词 + 当日有效 P13 活动文案 + 目录产品名。"""
    low = {t.lower() for t in TERMS}
    assert {"佳士得", "christie's", "top-up bonus", "智聊 chatx", "chatx", "lingox"} <= low
    assert "关掉的活动" not in low and "过期活动" not in low
    cat2 = dict(CATALOG)
    cat2["offers"] = [{
        "id": "o1", "enabled": True, "authorized_by": "v", "valid_until": "2099-01-01",
        "label_zh": "入门版首月体验价", "url": "https://bd2026.cc/order?plan=x"}]
    assert "入门版首月体验价" in offers_mod.camp_terms(cat2)


def test_camp_terms_never_raise_and_drop_short():
    assert offers_mod.camp_terms(None) == []
    assert offers_mod.camp_terms({"camp_promotions": "坏形状", "products": 1}) == []
    cat = {"camp_promotions": [{"id": "a", "enabled": True, "authorized_by": "a",
                                "keywords": ["x", "ok词", "ok词"]}]}
    assert offers_mod.camp_terms(cat) == ["ok词"]   # 单字剔除 + 去重


# ── ② 出站贬损守卫（claim_guard）────────────────────────────────────────────
@pytest.mark.parametrize("sent", INCIDENT)
def test_incident_sentences_caught_with_context(sent):
    """实录三句一句都没点名「佳士得」——靠登记关键词 / 客户近轮语境 + 机制泛词抓到。
    命中句不带句末标点（剥离时标点随句一并摘）。"""
    hits = find_camp_disparagement(sent, camp_terms=TERMS, context_text=CTX)
    assert len(hits) == 1 and hits[0] == sent.strip().rstrip(".")


def test_registered_keyword_sentence_needs_no_context():
    """「top-up bonus」是运营登记的关键词 → 句内直接命中，不依赖语境。"""
    assert find_camp_disparagement(INCIDENT[0], camp_terms=TERMS)


@pytest.mark.parametrize("sent", INCIDENT[1:])
def test_generic_promo_sentences_need_promo_context(sent):
    """只有机制泛词（ad / auction / deposits）的句子：没有语境（客户没提过登记活动）
    → 不抓——AI 帮客户吐槽别家垃圾广告是合法的。"""
    assert find_camp_disparagement(sent, camp_terms=TERMS) == []


def test_named_disparagement_needs_no_context():
    for txt in ("Christie's is basically a scam dressed up in a nice logo",
                "智聊 ChatX 就是个骗局，别信",
                "那个佳士得的充值奖励一看就是割韭菜"):
        assert find_camp_disparagement(txt, camp_terms=TERMS), txt


def test_empty_registry_means_no_judgement():
    """没登记＝不知道谁是自家 → 不判（宁漏勿误伤真反诈提醒）。"""
    assert find_camp_disparagement(INCIDENT[0], camp_terms=[], context_text=CTX) == []
    out, n, _ = sanitize_camp_disparagement(INCIDENT[0], camp_terms=(), context_text=CTX)
    assert (out, n) == (INCIDENT[0], 0)


@pytest.mark.parametrize("txt", [
    "ChatX isn't a scam, it's what I use every day",          # 辩护句
    "佳士得绝不是骗局，他们是正规拍卖行",                          # 辩护句
    "Ugh, my inbox is full of spam again",                    # 无语境的泛词
    "周末的活动取消了，垃圾天气",                                  # 无语境的泛词
    "Yeah that random crypto ad does look like a scam, be careful",  # 骂别家
    "Christie's summer auction sounds fun, are you going?",   # 中性提及
    "I'll take that as a win, haha",                          # 实录第四句：无贬损词
])
def test_no_false_alarm(txt):
    assert find_camp_disparagement(txt, camp_terms=TERMS, context_text=CTX if "Christie" in txt or "win" in txt else "") == [], txt


def test_sanitize_strips_only_the_disparaging_sentences():
    """实录整条回复：三句贬损剥掉，开场白与收尾问句原样留。"""
    reply = ("Haha, the spreadsheet life. Those silly top-up bonuses though. "
             "It reads like a flashy ad more than anything real. "
             "Real auction houses don't beg for deposits like that! Anyway, how was your day?")
    out, n, hits = sanitize_camp_disparagement(reply, camp_terms=TERMS, context_text=CTX)
    assert n == 3 and len(hits) == 3
    assert out == "Haha, the spreadsheet life. Anyway, how was your day?"


def test_sanitize_multiline_keeps_other_lines():
    out, n, _ = sanitize_camp_disparagement(
        "今天好累。\n那个充值奖励活动真蠢，一看就是割韭菜。\n你吃了吗？",
        camp_terms=TERMS, context_text="刚收到佳士得的充值奖励推广")
    assert n == 1 and out == "今天好累。\n你吃了吗？"


def test_sanitize_whole_reply_becomes_neutral_fallback():
    """整条都是贬损 → 中性兜底，绝不回退原文（原文就是那几句）。"""
    out, n, _ = sanitize_camp_disparagement("智聊 ChatX 就是个骗局。", camp_terms=TERMS)
    assert n == 1 and out == camp_fallback("智聊 ChatX 就是个骗局。")
    out_en, n_en, _ = sanitize_camp_disparagement(
        "Christie's is a scam.", camp_terms=TERMS)
    assert n_en == 1 and out_en == camp_fallback("Christie's is a scam.")
    assert "scam" not in out_en.lower()


def test_sentence_splitter_keeps_numbers_and_urls():
    """英文句点只在后跟空白时切句：$1.5 / bd2026.cc 里的点不能把一句切成两半。"""
    txt = "The tier costs $1.5k on bd2026.cc which is a scam-level price for ChatX"
    hits = find_camp_disparagement(txt, camp_terms=TERMS)
    assert hits == [txt]


def test_sanitize_never_raises():
    for bad in (None, "", 123, "x" * 4000):
        out, n, hits = sanitize_camp_disparagement(bad, camp_terms=TERMS, context_text=CTX)
        assert isinstance(out, str) and isinstance(n, int) and isinstance(hits, list)


# ── ③ prompt 块（offers.camp_block / service.build_camp_block_for_chat）─────
def test_camp_block_lists_registered_and_rules():
    blk = offers_mod.camp_block(CATALOG, lang="zh")
    assert blk.startswith("【自家阵营在推活动·硬约束】")
    assert "佳士得夏季拍卖·充值奖励活动" in blk and "官方细则" in blk
    assert "不主动推销" in blk          # 是「别拆台」约束，不是新的带货指令
    assert "智聊 ChatX" not in blk      # 产品不列（非带货会话列产品名=诱发推销）
    en = offers_mod.camp_block(CATALOG, lang="en")
    assert en.startswith("[Our side's live promotions") and "Christie's summer auction" in en


def test_camp_block_empty_registry_is_empty_string():
    assert offers_mod.camp_block({"products": CATALOG["products"]}) == ""
    assert offers_mod.camp_block(None) == ""


def test_build_camp_block_for_chat_modes(tmp_path, monkeypatch):
    import yaml

    from src.companion.goals import service as svc
    from src.companion.goals import site_catalog as sc
    cat_path = tmp_path / "site_catalog.yaml"
    cat_path.write_text(yaml.safe_dump(CATALOG, allow_unicode=True), encoding="utf-8")
    cfg_root = {"companion": {"goals": {"catalog": {"path": str(cat_path)}}}}
    # 缓存按 path 键，换文件即失效；保险起见清一次
    sc._CACHE.update(path="", mtime=-1.0, data=None, checked=0.0)
    assert svc.build_camp_block_for_chat(cfg_root, inbound_text="hello")
    # on_mention：客户没提 → 空；近轮提过 → 有
    cfg_root["companion"]["goals"]["camp_guard"] = {"prompt_mode": "on_mention"}
    assert svc.build_camp_block_for_chat(cfg_root, inbound_text="hello") == ""
    assert svc.build_camp_block_for_chat(
        cfg_root, inbound_text="lol", history_texts=["got mail from Christie's"])
    # prompt 开关关 → 空
    cfg_root["companion"]["goals"]["camp_guard"] = {"prompt": False}
    assert svc.build_camp_block_for_chat(cfg_root, inbound_text="Christie's") == ""


# ── ④ 接线（skill_manager 单一挂载点 = claim_guard 同口，A/B 线共用）──────────
def _fake_sm(cfg=None):
    return SimpleNamespace(
        config=SimpleNamespace(config=cfg or {"companion": {"goals": {}}},
                               config_path=None),
        logger=logging.getLogger("test-camp-guard"))


def test_skill_manager_guard_strips_with_goal_cta():
    from src.companion.goals.stats import get_goal_stats
    from src.skills.skill_manager import SkillManager
    get_goal_stats().reset()
    uc = {
        "_goal_cta": {"cta": "order", "base_url": "https://bd2026.cc",
                      "order_path": "/order", "offer_texts": [], "offer_free_days": [],
                      "catalog_prices": [], "camp_terms": TERMS},
        "last_message": CTX,
    }
    out = SkillManager._apply_goal_link_guard(
        _fake_sm(), "Those silly top-up bonuses though. Anyway how are you?", uc)
    assert "silly" not in out and "how are you" in out
    assert get_goal_stats().dump()["camp_stripped"] == 1
    assert get_goal_stats().dump()["camp_strip_samples"]
    assert "_goal_cta" not in uc


def test_skill_manager_guard_without_goal_uses_catalog_facts(monkeypatch):
    """无目标会话：兜底事实里只有 camp_terms（价格/试用全空）也必须跑贬损守卫。"""
    from src.skills import skill_manager as smmod
    from src.skills.skill_manager import SkillManager
    monkeypatch.setattr(
        "src.companion.goals.service.catalog_guard_facts",
        lambda *_a, **_k: {"offer_texts": [], "offer_free_days": [],
                           "catalog_prices": [], "camp_terms": TERMS})
    uc = {"_conversation_history": [
        {"role": "user", "content": CTX},
        {"role": "assistant", "content": "haha yeah"},
        {"role": "user", "content": "so I'm competing with spam now"},
    ]}
    out = SkillManager._apply_goal_link_guard(
        _fake_sm(), "Real auction houses don't beg for deposits like that. Tea?", uc)
    assert "beg for deposits" not in out and "Tea?" in out
    # 历史里只认客户侧消息作语境：AI 自己提过的自家词不算「客户在谈」
    assert "haha yeah" not in smmod._camp_context_text(uc)
    assert CTX in smmod._camp_context_text(uc)


def test_skill_manager_guard_kill_switch():
    from src.skills.skill_manager import SkillManager
    cfg = {"companion": {"goals": {"camp_guard": {"enabled": False}}}}
    uc = {"_goal_cta": {"cta": "order", "base_url": "https://bd2026.cc",
                        "order_path": "/order", "offer_texts": [], "offer_free_days": [],
                        "catalog_prices": [], "camp_terms": TERMS},
          "last_message": CTX}
    txt = "Those silly top-up bonuses though."
    assert SkillManager._apply_goal_link_guard(_fake_sm(cfg), txt, uc) == txt


def test_inject_and_consume_wiring_source_pins():
    """接线钉死：注入口写 _camp_block、prompt 组装消费它、ContextStore 不落盘。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src"
    sm = (root / "skills" / "skill_manager.py").read_text(encoding="utf-8")
    assert 'user_context["_camp_block"] = _camp' in sm
    assert "_guard_camp_disparagement(" in sm
    ai = (root / "ai" / "ai_client.py").read_text(encoding="utf-8")
    assert 'context.get("_camp_block")' in ai
    cs = (root / "utils" / "context_store.py").read_text(encoding="utf-8")
    assert '"_camp_block"' in cs
    svc = (root / "companion" / "goals" / "service.py").read_text(encoding="utf-8")
    assert svc.count('"camp_terms": _camp_term_list(') == 2   # _goal_cta 暂存 + 兜底事实同源


def test_shipped_catalog_has_registry_section():
    """随仓默认目录带 camp_promotions 段（空表 + 登记样例），运营照抄即用。"""
    from pathlib import Path

    import yaml
    p = Path(__file__).resolve().parents[1] / "config" / "site_catalog.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "camp_promotions" in data and data["camp_promotions"] == []
    assert "# camp_promotions:" in p.read_text(encoding="utf-8")
