"""获客转化漏斗（acquire_and_convert）门禁：模板相位 + 画像双轨 + 官网目录桥。

覆盖四层：
- **templates 相位纯函数**：scaled_phase_days 线性缩放 / phase_floor 时间兑底 /
  phase_cap 超前封顶（lookahead=1）。
- **profile_slots 纯函数**：确定性采集（高置信正例 + 防误采反例）、加权填充率、
  缺口序 + gap_hint。
- **store.customer_profiles**：auto 只填空槽、agent 覆盖一切、空串清槽、
  未知键忽略。
- **ledger acquire_and_convert**：bant_fill 推里程碑、第 1 天封顶不许开价、
  天窗兑底、单调不回退、done=末里程碑。
- **site_catalog**：真目录文件可加载（plan 键对齐官网 order-lines）、按画像选品、
  soft 不带链接 / direct 带链接、none 不出块。
- **service 集成**：build_block_for_chat 顺手采画像 → 摸底段出【画像缺口】行 →
  报价段附目录块（bazi 同轮 suppress 不叠加）。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.companion.goals.ledger import settle_goal
from src.companion.goals.planner import day_key
from src.companion.goals.profile_slots import (
    capture_from_text,
    fill_rates,
    gap_hint,
    missing_slots,
    slot_keys,
)
from src.companion.goals.service import build_block_for_chat, refresh_goal
from src.companion.goals.signals import GoalSignals
from src.companion.goals.store import GoalStore, reset_goal_store
from src.companion.goals.templates import (
    get_template,
    milestone_count,
    phase_cap,
    phase_floor,
    scaled_phase_days,
)
from src.companion.goals import site_catalog as sc

NOW = 1_800_000_000.0
_DAY = 86400.0

ENGINE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    import src.integrations.protocol_bridge as pb
    import src.utils.companion_context as cc
    monkeypatch.setattr(cc, "_REL_PROVIDERS", {})
    monkeypatch.setattr(pb, "_inbox_store_getter", None)
    reset_goal_store()
    yield
    reset_goal_store()


@pytest.fixture()
def mem_store():
    s = GoalStore(":memory:")
    yield s
    s.close()


TMPL = get_template("acquire_and_convert") or {}


def _goal(**kw):
    g = {
        "goal_id": "gA", "template": "acquire_and_convert", "status": "active",
        "milestone_idx": 0, "progress": 0.0, "result": "", "params": {},
        "start_ts": NOW, "deadline_ts": NOW + 10 * _DAY,
    }
    g.update(kw)
    return g


def _settle(goal, signals, now=NOW, **kw):
    return settle_goal(
        template_id="acquire_and_convert", template=TMPL, goal=goal,
        signals=signals, now=now, **kw)


# ── templates：相位纯函数 ───────────────────────────────────────────────────

def test_template_registered_with_flags_and_five_milestones():
    assert TMPL and milestone_count(TMPL) == 5
    assert TMPL.get("catalog") is True and TMPL.get("profile_slots") is True
    assert len(TMPL["push_curve"]) == 5
    assert set(TMPL["intents"].keys()) == {0, 1, 2, 3, 4}


def test_scaled_phase_days_identity_and_linear_scale():
    assert scaled_phase_days(TMPL, 10) == [2, 4, 7, 9, 10]
    assert scaled_phase_days(TMPL, 20) == [4, 8, 14, 18, 20]
    # 未声明 phase_days 的存量模板 → []（零行为变更）
    assert scaled_phase_days(get_template("conversion_unlock") or {}, 14) == []


def test_phase_floor_advances_on_window_pass():
    pd = [2.0, 4.0, 7.0, 9.0, 10.0]
    assert phase_floor(pd, 0.5) == 0
    assert phase_floor(pd, 2.5) == 1          # 破冰窗已过 → 该摸底了
    assert phase_floor(pd, 7.5) == 3          # 种草窗已过 → 该报价了
    assert phase_floor(pd, 9.5) == 4
    assert phase_floor(pd, 99.0) == 4         # 最后一段边界=deadline，不越界
    assert phase_floor([], 5.0) == 0


def test_phase_cap_allows_one_segment_lookahead():
    pd = [2.0, 4.0, 7.0, 9.0, 10.0]
    assert phase_cap(pd, 0.5) == 1            # 第 1 天最多推进到「摸底」
    assert phase_cap(pd, 3.0) == 2
    assert phase_cap(pd, 8.0) == 4
    assert phase_cap(pd, 0.5, lookahead=0) == 0
    assert phase_cap([], 0.5) >= 4            # 无相位声明 → 不封顶


# ── profile_slots：采集正反例 ───────────────────────────────────────────────

def test_capture_positive_bant_signals():
    got = dict(capture_from_text(
        "我们店里3个客服都回不过来，预算大概200美金一个月，下个月就想用起来"))
    assert "回复不过来" in got["need"]
    assert got["team_size"] == "3人"
    assert got["budget"].startswith("200")
    assert got["timeline"] == "下个月"


def test_capture_positive_relation_and_channel():
    got = dict(capture_from_text(
        "叫我阿龙就行，我在宿务，我是做民宿的，客户主要用whatsapp联系"))
    assert got["name"] == "阿龙"
    assert got["location"] == "宿务"
    assert got["channel"] == "whatsapp"
    assert "民宿" in got["occupation"]


def test_capture_authority_both_polarities():
    assert dict(capture_from_text("这个我说了算，我就是老板"))["authority"] == "拍板人"
    assert dict(capture_from_text("我得跟合伙人商量一下"))["authority"] == "需上报"


def test_capture_rejects_low_confidence():
    # 「我是觉得…」不是自称；裸数字不是预算；无团队语境的数字不是规模
    assert "name" not in dict(capture_from_text("我是觉得这样挺好的"))
    assert "budget" not in dict(capture_from_text("那个东西 200 就能买到"))
    assert "team_size" not in dict(capture_from_text("等我3天后回来"))
    assert capture_from_text("") == []
    assert capture_from_text("x" * 3000) == []


def test_fill_rates_weighted_and_missing_order():
    fields = {"need": {"v": "客服人手"}, "budget": {"v": "200美金"},
              "team_size": {"v": "3人"}}
    rates = fill_rates(fields)
    assert rates["bant"] == pytest.approx(6 / 11, abs=0.01)   # 3+2+1 / 11
    assert rates["relation"] == 0.0
    miss = [m["key"] for m in missing_slots(fields, track="bant", limit=6)]
    assert miss == ["channel", "authority", "timeline"]
    hint = gap_hint(fields)
    assert "平台" in hint                       # channel 的 ask_zh
    assert len(slot_keys("bant")) == 6 and len(slot_keys("relation")) == 4


# ── store：customer_profiles 写入语义 ───────────────────────────────────────

def test_profile_auto_fills_empty_only(mem_store):
    p1 = mem_store.upsert_customer_profile(
        "telegram", "u1", {"need": "客服人手"}, source="auto", now=NOW)
    assert p1["fields"]["need"]["v"] == "客服人手"
    assert p1["fields"]["need"]["src"] == "auto"
    # auto 再来不同值 → 不覆盖已有
    p2 = mem_store.upsert_customer_profile(
        "telegram", "u1", {"need": "别的"}, source="auto", now=NOW + 1)
    assert p2["fields"]["need"]["v"] == "客服人手"


def test_profile_agent_overwrites_and_clears(mem_store):
    mem_store.upsert_customer_profile(
        "telegram", "u2", {"need": "机器猜的"}, source="auto", now=NOW)
    p = mem_store.upsert_customer_profile(
        "telegram", "u2", {"need": "人工核实", "budget": "500刀"},
        source="agent", overwrite=True, now=NOW + 1)
    assert p["fields"]["need"]["v"] == "人工核实"
    assert p["fields"]["need"]["src"] == "agent"
    # agent 之后 auto 不得覆盖
    p = mem_store.upsert_customer_profile(
        "telegram", "u2", {"need": "又猜"}, source="auto", now=NOW + 2)
    assert p["fields"]["need"]["v"] == "人工核实"
    # 空串 = 清槽
    p = mem_store.upsert_customer_profile(
        "telegram", "u2", {"budget": ""}, source="agent", overwrite=True,
        now=NOW + 3)
    assert "budget" not in p["fields"]


def test_profile_ignores_unknown_keys_and_missing_row(mem_store):
    assert mem_store.get_customer_profile("telegram", "nobody") is None
    p = mem_store.upsert_customer_profile(
        "telegram", "u3", {"evil_key": "x", "name": "老王"}, source="auto")
    assert "evil_key" not in p["fields"] and p["fields"]["name"]["v"] == "老王"


# ── ledger：acquire_and_convert 结算 ────────────────────────────────────────

def test_ledger_day1_cap_blocks_offer_even_with_hot_signals():
    sig = GoalSignals(now=NOW, intimacy=80.0, funnel_stage="engaged",
                      last_inbound_ts=NOW + 60)
    sig.extras["bant_fill"] = 1.0
    res = _settle(_goal(), sig, now=NOW + 0.5 * _DAY)
    assert res["milestone_idx"] == 1          # 信号全热也只许超前一段


def test_ledger_bant_drives_qualify_and_rapport():
    g = _goal()
    sig = GoalSignals(now=NOW, intimacy=35.0, last_inbound_ts=NOW + 60)
    sig.extras["bant_fill"] = 0.8
    res = _settle(g, sig, now=NOW + 5 * _DAY)   # 第 6 天：cap=3
    assert res["milestone_idx"] == 3            # 画像够+关系热 → 该报价了


def test_ledger_phase_floor_forces_progress_without_signals():
    res = _settle(_goal(), GoalSignals(now=NOW), now=NOW + 7.5 * _DAY)
    assert res["milestone_idx"] == 3            # 零信号也按天窗兑底到报价段


def test_ledger_monotonic_and_direct_close():
    g = _goal(milestone_idx=3)
    sig = GoalSignals(now=NOW)                  # 信号全冷
    res = _settle(g, sig, now=NOW + 2 * _DAY)
    assert res["milestone_idx"] == 3            # 封顶不回退已达成
    res = _settle(g, sig, now=NOW + 9.5 * _DAY, direct_beat_engaged=True)
    assert res["milestone_idx"] == 4            # 开价证据 → 收口段


def test_ledger_done_via_entitlement_hits_last_milestone():
    g = _goal(params={"item_id": "chatx_seat"})
    sig = GoalSignals(now=NOW, entitlement={"unlocked": ["chatx_seat"]})
    res = _settle(g, sig, now=NOW + 1 * _DAY)
    assert res["status"] == "done" and res["milestone_idx"] == 4
    assert res["progress"] == 1.0


def test_ledger_expired_after_deadline():
    res = _settle(_goal(), GoalSignals(now=NOW), now=NOW + 11 * _DAY)
    assert res["status"] == "expired"


# ── site_catalog ────────────────────────────────────────────────────────────

def test_real_catalog_file_loads_with_website_plan_keys():
    data = sc.load_catalog(str(ENGINE_ROOT / "config" / "site_catalog.yaml"))
    ids = {p["id"] for p in data["products"]}
    assert {"chatx", "lingox"} <= ids
    plan_keys = {pl["key"] for p in data["products"] for pl in p["plans"]}
    # 与 website/lib/order-lines.ts 的 offer id 对齐（改那边要同步这里）
    assert {"autochat-entry", "autochat-team", "autochat-flagship",
            "translate-team", "translate-pro", "translate-charpack"} <= plan_keys
    assert data["site"]["base_url"].startswith("https://")


def _tiny_catalog():
    return {
        "site": {"base_url": "https://x.cc", "order_path": "/order",
                 "calc_url": "https://x.cc/#calc", "support_contact": "TG @s"},
        "products": [
            {"id": "chatx", "name_zh": "智聊", "pitch_zh": "AI客服",
             "price_from": "$58/月",
             "pains": ["客服", "回不过来"],
             "plans": [{"key": "a-entry", "price": "$58/月"},
                       {"key": "a-team", "price": "$198/月", "hot": True}]},
            {"id": "lingox", "name_zh": "通译", "pitch_zh": "聊天翻译",
             "price_from": "$99/月",
             "pains": ["翻译", "语言"],
             "plans": [{"key": "t-team", "price": "$99/月", "hot": True}]},
        ],
    }


def test_pick_products_matches_profile_pain():
    cat = _tiny_catalog()
    fields = {"need": {"v": "客服人手、回复不过来"}}
    picks = sc.pick_products(cat, fields, limit=2)
    assert picks[0]["id"] == "chatx"
    # 翻译痛点 → lingox 排前
    picks = sc.pick_products(cat, {"need": {"v": "语言不通"}}, limit=2)
    assert picks[0]["id"] == "lingox"
    # 画像空 → 按目录声明序
    assert sc.pick_products(cat, {}, limit=1)[0]["id"] == "chatx"
    # 钉死主推优先于画像
    picks = sc.pick_products(cat, fields, pinned="lingox", limit=2)
    assert picks[0]["id"] == "lingox"


def test_product_link_prefers_hot_plan():
    cat = _tiny_catalog()
    assert sc.product_link(cat["site"], cat["products"][0]) == \
        "https://x.cc/order?plan=a-team"


def test_catalog_block_levels():
    cat = _tiny_catalog()
    prods = sc.pick_products(cat, {}, limit=2)
    assert sc.build_catalog_block(prods, push_level="none") is None
    soft = sc.build_catalog_block(prods, push_level="soft", site=cat["site"])
    assert "不发链接" in soft and "https://x.cc/order" not in soft
    direct = sc.build_catalog_block(prods, push_level="direct", site=cat["site"])
    assert "https://x.cc/order?plan=a-team" in direct
    assert "试算" in direct and "不代收任何款项" in direct
    assert sc.build_catalog_block([], push_level="direct") is None


# ── service 集成：采集 → 缺口行 → 目录块 ────────────────────────────────────

def _cfg_obj(tmp_path):
    import yaml
    cat_file = tmp_path / "cat.yaml"
    cat_file.write_text(
        yaml.safe_dump(_tiny_catalog(), allow_unicode=True), encoding="utf-8")
    return SimpleNamespace(
        config={"companion": {"goals": {
            "enabled": True, "db_path": ":memory:",
            "catalog": {"path": str(cat_file)},
        }}},
        config_path=None)


def _mk_acquire_goal(cfg, chat_key="900"):
    from src.companion.goals.service import get_configured_store
    store = get_configured_store(cfg.config, None)
    conv = f"telegram:a1:{chat_key}"
    goal = store.create_goal(
        conversation_id=conv, platform="telegram", account_id="a1",
        chat_key=chat_key, template="acquire_and_convert", deadline_days=10)
    assert goal is not None
    return store, goal


def test_service_captures_profile_and_shows_gap(tmp_path):
    now = time.time()
    cfg = _cfg_obj(tmp_path)
    store, goal = _mk_acquire_goal(cfg, "901")
    block = build_block_for_chat(
        cfg, platform="telegram", chat_key="901", account_id="a1",
        conversation_id="telegram:a1:901",
        inbound_text="我们店里3个客服都回不过来，预算大概200美金一个月",
        now=now)
    assert block is not None
    prof = store.get_customer_profile("telegram", "901")
    assert prof["fields"]["team_size"]["v"] == "3人"       # 顺手采集已落库
    # bant 6/11≥0.5 → 摸底里程碑（第 1 天 cap=1）；缺口行出现且不含已采槽位
    g = store.get_goal(goal["goal_id"])
    assert g["milestone_idx"] == 1
    assert "【画像缺口】" in block
    assert "平台" in block                                  # channel 缺口在列


def test_service_attaches_catalog_at_offer_stage(tmp_path):
    now = time.time()
    cfg = _cfg_obj(tmp_path)
    store, goal = _mk_acquire_goal(cfg, "902")
    # BANT 资质过线（need 3 + budget 2 = 5/11 ≥ 0.34）→ direct 走自助下单档
    store.upsert_customer_profile(
        "telegram", "902",
        {"need": "客服人手、回复不过来", "budget": "200美金/月"}, source="auto")
    late = now + 8 * _DAY                       # 第 9 天：floor=3 → direct
    block = build_block_for_chat(
        cfg, platform="telegram", chat_key="902", account_id="a1",
        conversation_id="telegram:a1:902", now=late)
    assert block is not None
    assert "【可推荐产品" in block and "智聊" in block
    assert "https://x.cc/order?plan=a-team" in block        # direct 段带深链
    assert "【推进纪律】" in block                           # 目标纪律行仍在


def test_service_direct_unqualified_routes_to_cs(tmp_path):
    """P3 CTA 分级：direct 但 BANT 没聊透（只有痛点 3/11 < 0.34）→ 不甩
    自助下单链，改引官方客服人工收口（目录 site.support_contact）。"""
    now = time.time()
    cfg = _cfg_obj(tmp_path)
    store, goal = _mk_acquire_goal(cfg, "906")
    store.upsert_customer_profile(
        "telegram", "906", {"need": "客服人手、回复不过来"}, source="auto")
    late = now + 8 * _DAY
    block = build_block_for_chat(
        cfg, platform="telegram", chat_key="906", account_id="a1",
        conversation_id="telegram:a1:906", now=late)
    assert block is not None
    assert "【可推荐产品" in block
    assert "https://x.cc/order?plan=" not in block   # 资质缺 → 无下单深链
    assert "TG @s" in block                          # 引客服收口
    assert "预算/团队规模" in block                   # 先聊清资质的纪律行


def test_service_suppresses_catalog_when_bazi_active(tmp_path):
    now = time.time()
    cfg = _cfg_obj(tmp_path)
    _mk_acquire_goal(cfg, "903")
    late = now + 8 * _DAY
    block = build_block_for_chat(
        cfg, platform="telegram", chat_key="903", account_id="a1",
        conversation_id="telegram:a1:903",
        user_context={"_bazi_block": "【命理】x"}, now=late)
    assert block is not None
    assert "【可推荐产品" not in block           # 同轮已有变现引导 → 不叠加


def test_service_no_catalog_for_templates_without_flag(tmp_path):
    now = time.time()
    cfg = _cfg_obj(tmp_path)
    from src.companion.goals.service import get_configured_store
    store = get_configured_store(cfg.config, None)
    store.create_goal(
        conversation_id="telegram:a1:904", platform="telegram",
        account_id="a1", chat_key="904", template="conversion_unlock",
        deadline_days=14)
    block = build_block_for_chat(
        cfg, platform="telegram", chat_key="904", account_id="a1",
        conversation_id="telegram:a1:904",
        inbound_text="预算200美金", now=now)
    assert block is not None
    assert "【可推荐产品" not in block
    # 无 profile_slots 声明 → 不采集
    assert store.get_customer_profile("telegram", "904") is None


def test_refresh_goal_reads_profile_into_extras(tmp_path):
    now = time.time()
    cfg = _cfg_obj(tmp_path)
    store, goal = _mk_acquire_goal(cfg, "905")
    store.upsert_customer_profile(
        "telegram", "905",
        {"need": "获客难", "budget": "500刀", "channel": "whatsapp",
         "timeline": "下个月"}, source="agent", overwrite=True)
    res = refresh_goal(store, cfg.config, goal,
                       now=now + 5 * _DAY)      # bant 8/11 → 第 6 天可到报价段
    assert res["goal"]["milestone_idx"] >= 2


# ── service：链接纪律档位暂存（P4 出站守卫的注入侧）────────────────────────────

def test_service_stashes_order_cta_for_link_guard(tmp_path):
    """direct+资质过线日：uc 落 _goal_cta 且档位升为 order（守卫放行下单链）。"""
    now = time.time()
    cfg = _cfg_obj(tmp_path)
    store, goal = _mk_acquire_goal(cfg, "910")
    store.upsert_customer_profile(
        "telegram", "910",
        {"need": "客服人手、回复不过来", "budget": "200美金/月"}, source="auto")
    uc: dict = {}
    block = build_block_for_chat(
        cfg, platform="telegram", chat_key="910", account_id="a1",
        conversation_id="telegram:a1:910", user_context=uc,
        now=now + 8 * _DAY)
    assert block is not None
    info = uc.get("_goal_cta")
    assert isinstance(info, dict) and info["cta"] == "order"
    assert info["base_url"] == "https://x.cc"
    assert info["order_path"] == "/order"


def test_service_stash_stays_conservative_on_bazi_suppress(tmp_path):
    """bazi 同轮让位：目录块不出，但暂存仍在且保持保守档 ""（守卫剥全部本域链
    ——变现让位的那轮更不该出现带货链接）。"""
    now = time.time()
    cfg = _cfg_obj(tmp_path)
    _mk_acquire_goal(cfg, "911")
    uc: dict = {"_bazi_block": "【命理】x"}
    block = build_block_for_chat(
        cfg, platform="telegram", chat_key="911", account_id="a1",
        conversation_id="telegram:a1:911", user_context=uc,
        now=now + 8 * _DAY)
    assert block is not None and "【可推荐产品" not in block
    info = uc.get("_goal_cta")
    assert isinstance(info, dict) and info["cta"] == ""


def test_service_no_stash_for_templates_without_catalog(tmp_path):
    """无 catalog 声明的模板：不落暂存（守卫对这类会话零参与）。"""
    now = time.time()
    cfg = _cfg_obj(tmp_path)
    from src.companion.goals.service import get_configured_store
    store = get_configured_store(cfg.config, None)
    store.create_goal(
        conversation_id="telegram:a1:912", platform="telegram",
        account_id="a1", chat_key="912", template="conversion_unlock",
        deadline_days=14)
    uc: dict = {}
    block = build_block_for_chat(
        cfg, platform="telegram", chat_key="912", account_id="a1",
        conversation_id="telegram:a1:912", user_context=uc, now=now)
    assert block is not None
    assert "_goal_cta" not in uc
