"""获客增长环（goals 第二阶段）门禁：自动建目标 + 画像 LLM 轨 + 成交闭环。

覆盖四条新链路：
- **auto_create**（service.maybe_auto_create_goal + build_block_for_chat 接线）：
  人设 allowlist 必填 / 群聊不建 / 平台过滤 / 幂等（建过任何目标不再建）/
  每日预算（进库计数）/ 首条消息当轮建成即出块。
- **profile_llm**（摘录式 LLM 补槽）：解析（栅栏/坏 JSON/未知槽位）、
  摘录接地（编造必丢、数字组、CJK bigram 容差）、run_llm_capture 端到端
  只填空槽、schedule 门控（关/冷却/预算/无缺口）。
- **成交闭环**：目录链接 ref 归因（product_link/_append_ref）、
  order-hook 路由（token 恒时鉴权/幂等/未匹配 200/结算 done 推满里程碑）、
  坐席「标成交」status action=done。
- **order_pull**（引擎拉官网订单，NAT 后部署的实际成交通道）：配置门控
  （缺 key/url 零 HTTP）、响应过滤（只认 ref+paid/activated、裁掉 PII）、
  结算+双层幂等（进程 _SEEN + 事件级）、网络软失败、watchdog 接线
  （goals 总闸/节流窗）。
- **store 新查询**：has_any_goal（含终态）/ count_created_by_since。
- **link_guard**（P4 链接纪律出站守卫）：纯函数分档剥离矩阵（order 放行 /
  cs·roi 剥下单链留试算器 / 空档剥全部本域、别家域名不碰、整条是链接回退、
  标签行清扫）+ skill_manager 接线（读后即焚 / 开关 / 无暂存零影响 / 计数）。
- **成交反哺选品**（P4）：store.sold_plan_counts（窗口/手动不计）→
  sold_boost_map（plan 折产品）→ pick_products 痛点同分裁决（销量绝不
  盖过对症）→ service TTL 缓存。
- **留存/续费环**（P5）：settle_order_ref/手动标成交 → maybe_create_retention_goal
  自动起 30 天 retention_expand 周期（product_id 按上单 plan 反查目录继承、
  base_goal 链路可溯）；续费单结算留存目标再起下一周期（链式）；开关门控 /
  会话已有活跃目标不叠 / stats 计数。
- **年付周期自适应 + 流失挽回环**（P6）：retention_days_for_period（订单
  period → 留存天数：annual=365，period_days 可覆写，回落 rc.days→模板默认）
  经 order_pull period 透传端到端生效；run_winback_scan（留存 expired 冷却后
  自动起低频挽回目标：窗口 [cooldown,max_age] / 一次流失只挽回一次 / 静默
  active 顺手清扫落库 / 每日预算 / 开关门控）+ watchdog 接线。
- **回流再转化 + note 进 prompt**（P7）：maybe_spawn_reconvert（挽回目标 done
  =流失客户回话 → 顺势起短周期转化目标，产品/plan 继承，只对 winback_auto
  触发）挂 refresh_goal 转态钩子 + 手动标 done 路由；goal block 新增【背景】行
  （params.note 首次进 LLM prompt——挽回/回流语境此前只在坐席 UI 可见），
  超长最先丢弃、意图行截断不再错位。
- **流失原因结构化**（P8）：profile_slots 新增 lifecycle 采集专用轨
  churn_reason（不进 TRACKS → 不计填充率/不进 LLM 全轨缺口枚举/摸底提示，
  新客户永远不会被问「为什么没续」）；capture_churn_reason 分类词表
  （太贵/没用起来/效果不佳/出了问题/换了别家/业务变动/预算紧张）+
  retention 会话续费锚词档（复合词防「连续」误触）；service 按
  goal.created_by ∈ {retention,winback,reconvert}_auto 门控采集（独立于
  profile_slots 能力位——winback 模板没有 bant 摸底）；三处生命周期建目标
  把在档原因织进 note（挽回「TA说过原因」/再转化「别再踩同一个坑」/
  新留存周期「提前留意」）；stats churn_captured（首采计数）。
- **客户档案行 + 对症应对策略**(P9)：facts_line（已采画像原值首次进 prompt
  ——此前只驱动选品；注册表序/单值截断/槽数+字符双预算/兼容裸字符串单元），
  goal block 新增【客户档案】行（溢出梯队最先让位，背景/纪律保住）；
  churn_strategy_hint（7 类流失原因 → 应对话术方向，纯策略不涉定价权限，
  与 _CHURN_PATTERNS 词表齐平有门禁）经 goal_view_block note_suffix 拼进
  【背景】（按当轮画像现值动态拼——采集常发生在目标创建后，静态 note 会错过；
  无 note 时独立成行）；service 画像常载（挽回等无 slots 模板也见档案），
  策略仍按 created_by 生命周期门控（新客户块绝不冒出「应对流失」语义）。
- **开闸就绪度**(P10)：growth_readiness 聚合总闸/自动建/人设 allowlist/
  人设在档/账号绑定/目录/订单通道/留存环 → status
  （disabled/waiting_bind/partial/ready）+ blockers + hints；
  ``GET /api/goals/readiness``（goals 关也 200）+ ops 卡首行置顶，
  零流量也能看见「待绑账号」卡点。
- **流失选品转向 + 待绑可操作化**(P11)：churn_offer_steer（太贵→entry+roi /
  信任问题→cs，**不发明折扣**）→ pick_cta(cta_bias) / product_link(plan_pref)
  / build_catalog_block 文案分叉；service 生命周期门控接线 + stats
  churn_steered；readiness.bind_candidates（online 优先 + hint 写明换绑目标）
  + ops 卡「可换绑」行。
- **校准读数 + 流失×成交矩阵**(P12)：outcome_report.churn_outcomes（生命周期
  终态 JOIN 画像主因，won=order:/manual:）；growth_calibration 出
  priority/hints/personas_href；readiness 附 calibration + 候选 href
  （/personas#profile=）；winback/reconvert note 织入门档 plan key 预览。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.companion.goals import profile_llm as pl
from src.companion.goals import site_catalog as sc
from src.companion.goals.service import (
    build_block_for_chat,
    maybe_auto_create_goal,
)
from src.companion.goals.store import GoalStore, reset_goal_store
from src.web.routes.goal_routes import register_goal_routes

NOW = 1_800_000_000.0
CONV = "telegram:a1:100"
HOOK_TOKEN = "sekret-hook-token"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    import src.integrations.protocol_bridge as pb
    import src.utils.companion_context as cc
    monkeypatch.setattr(cc, "_REL_PROVIDERS", {})
    monkeypatch.setattr(pb, "_inbox_store_getter", None)
    from src.companion.goals import offers as _offers
    reset_goal_store()
    pl.reset_state()
    _offers.reset_citations()
    yield
    reset_goal_store()
    pl.reset_state()
    _offers.reset_citations()


@pytest.fixture()
def mem_store():
    s = GoalStore(":memory:")
    yield s
    s.close()


@pytest.fixture()
def su_wan_persona():
    """注册 su_wan 人设进 PersonaManager 单例；测后复位防串味。"""
    from src.utils.persona_manager import PersonaManager
    pm = PersonaManager.get_instance()
    pm.upsert_profile("su_wan", {"id": "su_wan", "name": "苏婉"},
                      _track_history=False)
    yield pm
    PersonaManager.reset()


# ── store 新查询 ─────────────────────────────────────────────────────────────

def test_has_any_goal_counts_terminal_too(mem_store):
    assert mem_store.has_any_goal(conversation_id=CONV) is False
    g = mem_store.create_goal(
        conversation_id=CONV, platform="telegram", account_id="a1",
        chat_key="100", template="acquire_and_convert", now=NOW)
    assert mem_store.has_any_goal(conversation_id=CONV) is True
    # platform+chat_key 口径同样命中
    assert mem_store.has_any_goal(platform="telegram", chat_key="100") is True
    # 终态后仍算「建过」（幂等闸的语义就是含终态）
    mem_store.update_goal_fields(g["goal_id"], status="cancelled")
    assert mem_store.has_any_goal(conversation_id=CONV) is True
    assert mem_store.has_any_goal(conversation_id="telegram:a1:999") is False


def test_count_created_by_since_filters(mem_store):
    mem_store.create_goal(conversation_id="c1", platform="telegram",
                          chat_key="1", template="custom",
                          created_by="auto_create", now=NOW)
    mem_store.create_goal(conversation_id="c2", platform="telegram",
                          chat_key="2", template="custom",
                          created_by="auto_create", now=NOW + 100)
    mem_store.create_goal(conversation_id="c3", platform="telegram",
                          chat_key="3", template="custom",
                          created_by="tester", now=NOW + 200)
    assert mem_store.count_created_by_since("auto_create", NOW - 1) == 2
    assert mem_store.count_created_by_since("auto_create", NOW + 50) == 1
    assert mem_store.count_created_by_since("tester", NOW - 1) == 1
    assert mem_store.count_created_by_since("nobody", 0) == 0


# ── auto_create ──────────────────────────────────────────────────────────────

def _ac_cfg(**extra):
    ac = {"enabled": True, "personas": ["su_wan"], "max_per_day": 20}
    ac.update(extra)
    return {"companion": {"goals": {
        "enabled": True, "db_path": ":memory:", "auto_create": ac}}}


def _uc(pid="su_wan", **extra):
    uc = {"account_persona_id": pid, "chat_id": "100"}
    uc.update(extra)
    return uc


class TestAutoCreate:
    def test_disabled_returns_none(self, mem_store, su_wan_persona):
        cfg = {"companion": {"goals": {"enabled": True}}}
        assert maybe_auto_create_goal(
            mem_store, cfg, platform="telegram", chat_key="100",
            account_id="a1", user_context=_uc(), now=NOW) is None

    def test_empty_personas_allowlist_never_creates(self, mem_store,
                                                    su_wan_persona):
        assert maybe_auto_create_goal(
            mem_store, _ac_cfg(personas=[]), platform="telegram",
            chat_key="100", account_id="a1", user_context=_uc(),
            now=NOW) is None

    def test_persona_mismatch_skips(self, mem_store, su_wan_persona):
        assert maybe_auto_create_goal(
            mem_store, _ac_cfg(), platform="telegram", chat_key="100",
            account_id="a1", user_context=_uc(pid="other_gal"),
            now=NOW) is None

    def test_group_chat_never_creates(self, mem_store, su_wan_persona):
        assert maybe_auto_create_goal(
            mem_store, _ac_cfg(), platform="telegram", chat_key="100",
            account_id="a1", user_context=_uc(is_group=True), now=NOW) is None

    def test_platform_filter(self, mem_store, su_wan_persona):
        cfg = _ac_cfg(platforms=["whatsapp"])
        assert maybe_auto_create_goal(
            mem_store, cfg, platform="telegram", chat_key="100",
            account_id="a1", user_context=_uc(), now=NOW) is None
        g = maybe_auto_create_goal(
            mem_store, cfg, platform="whatsapp", chat_key="100",
            account_id="a1", user_context=_uc(), now=NOW)
        assert g is not None and g["platform"] == "whatsapp"

    def test_creates_with_synthesized_conversation_id(self, mem_store,
                                                      su_wan_persona):
        g = maybe_auto_create_goal(
            mem_store, _ac_cfg(), platform="telegram", chat_key="100",
            account_id="a1", user_context=_uc(), now=NOW)
        assert g is not None
        assert g["conversation_id"] == "telegram:a1:100"
        assert g["template"] == "acquire_and_convert"
        assert g["autonomy"] == "auto"
        assert g["created_by"] == "auto_create"
        assert g["deadline_ts"] > NOW          # 模板 default_days 兑入

    def test_idempotent_even_after_terminal(self, mem_store, su_wan_persona):
        g = maybe_auto_create_goal(
            mem_store, _ac_cfg(), platform="telegram", chat_key="100",
            account_id="a1", user_context=_uc(), now=NOW)
        assert g is not None
        assert maybe_auto_create_goal(
            mem_store, _ac_cfg(), platform="telegram", chat_key="100",
            account_id="a1", user_context=_uc(), now=NOW) is None
        mem_store.update_goal_fields(g["goal_id"], status="cancelled")
        assert maybe_auto_create_goal(
            mem_store, _ac_cfg(), platform="telegram", chat_key="100",
            account_id="a1", user_context=_uc(), now=NOW) is None

    def test_daily_budget_caps(self, mem_store, su_wan_persona):
        cfg = _ac_cfg(max_per_day=1)
        assert maybe_auto_create_goal(
            mem_store, cfg, platform="telegram", chat_key="100",
            account_id="a1", user_context=_uc(), now=NOW) is not None
        assert maybe_auto_create_goal(
            mem_store, cfg, platform="telegram", chat_key="200",
            account_id="a1", user_context=_uc(chat_id="200"), now=NOW) is None

    def test_build_block_auto_creates_and_injects_first_turn(
            self, su_wan_persona):
        """新好友首条消息：自动建目标 + 当轮即出目标块（获客开场有方向）。"""
        cfg_obj = SimpleNamespace(config=_ac_cfg(), config_path=None)
        block = build_block_for_chat(
            cfg_obj, platform="telegram", chat_key="100", account_id="a1",
            user_context=_uc(), chain="reply", inbound_text="你好，在吗？",
            now=time.time())
        assert block is not None and "【工作目标】" in block
        from src.companion.goals.service import get_configured_store
        store = get_configured_store(cfg_obj.config, None)
        g = store.find_active_goal(conversation_id="telegram:a1:100")
        assert g is not None and g["created_by"] == "auto_create"

    def test_build_block_no_autocreate_without_inbound_text(
            self, su_wan_persona):
        cfg_obj = SimpleNamespace(config=_ac_cfg(), config_path=None)
        block = build_block_for_chat(
            cfg_obj, platform="telegram", chat_key="300", account_id="a1",
            user_context=_uc(chat_id="300"), chain="proactive",
            inbound_text="", now=time.time())
        assert block is None
        from src.companion.goals.service import get_configured_store
        store = get_configured_store(cfg_obj.config, None)
        assert store.find_active_goal(
            conversation_id="telegram:a1:300") is None


# ── profile_llm：解析 / 接地 ────────────────────────────────────────────────

def test_parse_extraction_strips_fences_and_filters():
    raw = '```json\n{"need": "客服回不过来", "bogus": "x", "budget": 200}\n```'
    out = pl.parse_extraction(raw)
    assert out == {"need": "客服回不过来", "budget": "200"}
    assert pl.parse_extraction("前置废话 {\"name\": \"阿龙\"} 后缀") == \
        {"name": "阿龙"}
    assert pl.parse_extraction("not json at all") == {}
    assert pl.parse_extraction('["array"]') == {}
    assert pl.parse_extraction("") == {}
    assert pl.parse_extraction('{"need": ""}') == {}


def test_ground_extracted_extractive_only():
    text = "我们店里3个客服都回不过来，主要做民宿代运营的"
    ok = pl.ground_extracted(text, {
        "need": "客服回不过来",          # bigram 容差（原文有「都」）
        "team_size": "3个客服",          # 数字组命中
        "occupation": "民宿代运营",      # 直接子串
        "budget": "预算五万",            # 编造 → 必丢
        "location": "马尼拉",            # 编造 → 必丢
    })
    assert set(ok) == {"need", "team_size", "occupation"}


def test_ground_extracted_rejects_paraphrase_inventions():
    # 语义对但不是摘录（整句改写）→ 拒。宁可漏采不错采。
    text = "唉最近生意不太行"
    assert pl.ground_extracted(text, {"need": "获客困难客户少"}) == {}


def test_build_extract_prompt_lists_missing_only():
    miss = [{"key": "need", "label_zh": "业务痛点", "ask_zh": "生意上最头疼什么"},
            {"key": "budget", "label_zh": "预算档", "ask_zh": "愿意花多少"}]
    p = pl.build_extract_prompt("你好", miss)
    assert "need" in p and "budget" in p and "禁止推断" in p
    assert "<<<你好>>>" in p


# ── profile_llm：执行 / 调度 ────────────────────────────────────────────────

class _FakeAI:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    async def chat(self, prompt):
        self.calls += 1
        return self.reply


async def test_run_llm_capture_grounds_and_fills(mem_store):
    ai = _FakeAI('{"need": "客服回不过来", "budget": "编造的五万",'
                 ' "occupation": "民宿代运营"}')
    miss = [{"key": "need"}, {"key": "budget"}, {"key": "occupation"}]
    n = await pl.run_llm_capture(
        ai, mem_store, platform="telegram", chat_key="100",
        text="我们客服都回不过来，做民宿代运营的", missing=miss, now=NOW)
    assert n == 2
    prof = mem_store.get_customer_profile("telegram", "100")
    assert prof["fields"]["need"]["v"] == "客服回不过来"
    assert prof["fields"]["need"]["src"] == "llm_pending"
    assert prof["fields"]["occupation"]["v"] == "民宿代运营"
    assert "budget" not in prof["fields"]          # 编造被接地毙掉


async def test_run_llm_capture_never_overwrites_agent(mem_store):
    mem_store.upsert_customer_profile(
        "telegram", "100", {"need": "坐席手录的痛点"},
        source="agent", overwrite=True)
    ai = _FakeAI('{"need": "客服回不过来"}')
    await pl.run_llm_capture(
        ai, mem_store, platform="telegram", chat_key="100",
        text="客服回不过来", missing=[{"key": "need"}], now=NOW)
    prof = mem_store.get_customer_profile("telegram", "100")
    assert prof["fields"]["need"]["v"] == "坐席手录的痛点"


async def test_run_llm_capture_bad_output_is_zero(mem_store):
    n = await pl.run_llm_capture(
        _FakeAI("完全不是JSON"), mem_store, platform="telegram",
        chat_key="100", text="随便说说", missing=[{"key": "need"}], now=NOW)
    assert n == 0
    assert mem_store.get_customer_profile("telegram", "100") is None


async def test_schedule_gates_disabled_cooldown_budget(mem_store):
    ai = _FakeAI('{"need": "客服回不过来"}')
    text = "我们客服都回不过来啊"
    # 未启用 → False
    assert pl.schedule_llm_capture(
        ai, mem_store, {"companion": {"goals": {}}},
        platform="telegram", chat_key="1", text=text) is False
    cfg = {"companion": {"goals": {"profile_llm": {
        "enabled": True, "cooldown_min": 30, "daily_budget": 2}}}}
    # 首次 → 调度成功
    assert pl.schedule_llm_capture(
        ai, mem_store, cfg, platform="telegram", chat_key="1",
        text=text, now=NOW) is True
    # 时间冷却只在**摸底完成后**生效（BANT 已填够）——摸底期同会话连发是刻意的，
    # 见 test_schedule_discovery_allows_consecutive_captures
    _filled = {k: {"v": "x"} for k in
               ("need", "channel", "team_size", "budget", "authority", "timeline")}
    assert pl.schedule_llm_capture(
        ai, mem_store, cfg, platform="telegram", chat_key="1",
        text=text, fields=_filled, now=NOW + 60) is False
    # 预算 2：第二个会话占掉 → 第三个会话拒
    assert pl.schedule_llm_capture(
        ai, mem_store, cfg, platform="telegram", chat_key="2",
        text=text, now=NOW) is True
    assert pl.schedule_llm_capture(
        ai, mem_store, cfg, platform="telegram", chat_key="3",
        text=text, now=NOW) is False
    # 放两拍让 fire-and-forget 任务跑完（不留悬挂任务噪声）
    await asyncio.sleep(0.05)
    assert ai.calls == 2
    prof = mem_store.get_customer_profile("telegram", "1")
    assert prof["fields"]["need"]["src"] == "llm_pending"


def test_looks_worth_extracting_skips_greetings():
    """名额有限：纯寒暄不值得烧一次 LLM（本次事故就是被开场白吃掉的）。"""
    for junk in ("在吗", "在吗？", "你好", "你好呀", "hi", "Hello", "嗨~", "？？", "  "):
        assert pl.looks_worth_extracting(junk) is False, junk
    # 实录里携带 BANT 的那句必须放行
    assert pl.looks_worth_extracting(
        "我做跨境电商，Telegram和WhatsApp客服天天爆，三个客服都快忙吐了，"
        "一个月预算就几百美金")
    # 非寒暄一律放行——刻意不叠长度/关键词启发：更严的版本会把下面这类
    # 真痛点描述挡在门外（漏采才是本次事故本体，多烧一次小调用无所谓）
    assert pl.looks_worth_extracting("团队就3个人")
    assert pl.looks_worth_extracting("我是老板说了算")
    assert pl.looks_worth_extracting("客服人手不够消息老是回不过来很头疼")
    assert pl.looks_worth_extracting("其实我这边是帮人代运营民宿的")


def test_in_discovery_tracks_bant_fill():
    cfg: dict = {}
    assert pl.in_discovery(cfg, None) is True
    assert pl.in_discovery(cfg, {}) is True
    filled = {k: {"v": "x"} for k in
              ("need", "channel", "team_size", "budget", "authority", "timeline")}
    assert pl.in_discovery(cfg, filled) is False
    # 只填一半 → 仍在摸底期
    assert pl.in_discovery(cfg, {"need": {"v": "客服"}}) is True


async def test_schedule_discovery_allows_consecutive_captures(mem_store):
    """摸底期按「每会话次数」放行，不被时间冷却挡住。

    2026-07-28 实录事故复现：10 轮对话 90 秒内跑完，30min 时间冷却让
    LLM 摘录轨只在**第 1 轮寒暄**上开过一次火，紧接着那句携带全部 BANT 的话
    被冷却挡死 → 结构化画像常年空、bant_fill 闸永远抬不起来。
    """
    pl.reset_state()
    ai = _FakeAI('{"need": "客服回不过来"}')
    cfg = {"companion": {"goals": {"profile_llm": {
        "enabled": True, "cooldown_min": 30, "daily_budget": 50,
        "discovery_max_per_conv": 3}}}}
    kw = dict(platform="telegram", chat_key="dup")
    # 寒暄不占名额
    assert pl.schedule_llm_capture(
        ai, mem_store, cfg, text="在吗", now=NOW, **kw) is False
    # 摸底期连续三条有内容的消息都放行（旧行为只会放行第一条）
    for i in range(3):
        assert pl.schedule_llm_capture(
            ai, mem_store, cfg, text=f"我们客服都回不过来啊，第{i}轮说的",
            now=NOW + i, **kw) is True
    # 超过每会话上限 → 拒（成本仍有界）
    assert pl.schedule_llm_capture(
        ai, mem_store, cfg, text="我们客服都回不过来啊，再补一句",
        now=NOW + 5, **kw) is False
    await asyncio.sleep(0.05)


async def test_schedule_discovery_respects_global_budget(mem_store):
    """摸底期放宽的是**每会话**闸，全局每日预算依旧封顶。"""
    pl.reset_state()
    ai = _FakeAI("{}")
    cfg = {"companion": {"goals": {"profile_llm": {
        "enabled": True, "daily_budget": 2, "discovery_max_per_conv": 9}}}}
    text = "我们客服都回不过来啊"
    assert pl.schedule_llm_capture(
        ai, mem_store, cfg, platform="telegram", chat_key="g", text=text,
        now=NOW) is True
    assert pl.schedule_llm_capture(
        ai, mem_store, cfg, platform="telegram", chat_key="g", text=text,
        now=NOW + 1) is True
    assert pl.schedule_llm_capture(
        ai, mem_store, cfg, platform="telegram", chat_key="g", text=text,
        now=NOW + 2) is False          # 预算 2 用尽
    await asyncio.sleep(0.05)


async def test_schedule_skips_short_text_and_full_profile(mem_store):
    cfg = {"companion": {"goals": {"profile_llm": {"enabled": True}}}}
    ai = _FakeAI("{}")
    assert pl.schedule_llm_capture(
        ai, mem_store, cfg, platform="telegram", chat_key="1",
        text="嗯嗯", now=NOW) is False                    # 短文本
    full = {s: "x" for s in (
        "name", "location", "occupation", "age", "interests", "need",
        "channel", "team_size", "budget", "authority", "timeline")}
    assert pl.schedule_llm_capture(
        ai, mem_store, cfg, platform="telegram", chat_key="1",
        text="这条够长了吧一二三四五", fields={k: {"v": v} for k, v in
                                              full.items()},
        now=NOW) is False                                 # 无缺口
    assert ai.calls == 0


# ── 成交闭环：目录 ref 归因 ─────────────────────────────────────────────────

def _tiny_catalog():
    return {
        "site": {"base_url": "https://x.cc", "order_path": "/order"},
        "products": [
            {"id": "chatx", "name_zh": "智聊", "pitch_zh": "AI客服",
             "price_from": "$58/月", "pains": ["客服"],
             "plans": [{"key": "a-team", "price": "$198/月", "hot": True}]},
        ],
    }


def test_product_link_ref_appended_and_encoded():
    cat = _tiny_catalog()
    u = sc.product_link(cat["site"], cat["products"][0], ref="telegram:a1:100")
    assert u == "https://x.cc/order?plan=a-team&ref=telegram%3Aa1%3A100"
    # 无 ref → 纯净链接（旧行为不变）
    assert sc.product_link(cat["site"], cat["products"][0]) == \
        "https://x.cc/order?plan=a-team"
    # 无 plan 的产品回落 url/首页也挂 ref（? 分隔符）
    prod2 = {"id": "y", "url": "https://x.cc/lp"}
    assert sc.product_link(cat["site"], prod2, ref="c") == \
        "https://x.cc/lp?ref=c"


def test_catalog_block_direct_carries_ref():
    cat = _tiny_catalog()
    prods = sc.pick_products(cat, {}, limit=1)
    direct = sc.build_catalog_block(
        prods, push_level="direct", site=cat["site"],
        link_ref="telegram:a1:100")
    assert "ref=telegram%3Aa1%3A100" in direct
    soft = sc.build_catalog_block(
        prods, push_level="soft", site=cat["site"],
        link_ref="telegram:a1:100")
    assert "ref=" not in soft                     # soft 本就不发链接


# ── P3：CTA 分级 + fragment 归因 + 成交归因聚合 ─────────────────────────────

def _cta_site():
    return {"base_url": "https://x.cc", "order_path": "/order",
            "calc_url": "https://x.cc/#calc", "support_contact": "TG @s"}


def test_append_ref_fragment_aware():
    """锚点链接的 ?ref 必须插在 # 之前——拼末尾会整段落进 fragment，
    服务端与前端 location.search 都读不到（真机踩过的归因断链）。"""
    prod = {"id": "y", "url": "https://x.cc/#autochat"}
    u = sc.product_link(_cta_site(), prod, ref="c1")
    assert u == "https://x.cc/?ref=c1#autochat"
    # 已带 query 的锚点链接 → & 续接，fragment 仍殿后
    prod2 = {"id": "z", "url": "https://x.cc/lp?a=1#sec"}
    assert sc.product_link({}, prod2, ref="c2") == \
        "https://x.cc/lp?a=1&ref=c2#sec"


def test_pick_cta_matrix():
    site = _cta_site()
    # direct：资质未知（旧调用）→ order（向后兼容）；资质够 → order
    assert sc.pick_cta(push_level="direct", site=site) == "order"
    assert sc.pick_cta(push_level="direct", bant_fill=0.5, site=site) == "order"
    # direct：BANT 明显没聊透 + 有客服号 → cs；没客服号回落 order
    assert sc.pick_cta(push_level="direct", bant_fill=0.2, site=site) == "cs"
    assert sc.pick_cta(push_level="direct", bant_fill=0.2,
                       site={"base_url": "https://x.cc"}) == "order"
    # soft：种草段（m>=2）且有试算器 → roi；早期/无试算器 → 空
    assert sc.pick_cta(push_level="soft", milestone_idx=2, site=site) == "roi"
    assert sc.pick_cta(push_level="soft", milestone_idx=1, site=site) == ""
    assert sc.pick_cta(push_level="soft", milestone_idx=3,
                       site={"base_url": "https://x.cc"}) == ""
    assert sc.pick_cta(push_level="none", milestone_idx=4, site=site) == ""


def test_catalog_block_cs_tier_no_order_links():
    cat = _tiny_catalog()
    prods = sc.pick_products(cat, {}, limit=1)
    blk = sc.build_catalog_block(
        prods, push_level="direct", site=_cta_site(),
        link_ref="cv1", bant_fill=0.2)
    assert "下单：" not in blk and "/order?plan=" not in blk
    assert "TG @s" in blk                          # 客服收口
    assert "不代收任何款项" in blk                  # 安全红线仍在
    assert "ref=cv1" in blk                        # 试算器辅助链接仍带归因


def test_catalog_block_roi_tier_calc_link_with_ref():
    cat = _tiny_catalog()
    prods = sc.pick_products(cat, {}, limit=1)
    blk = sc.build_catalog_block(
        prods, push_level="soft", site=_cta_site(),
        link_ref="cv2", milestone_idx=2)
    assert "试算器" in blk
    assert "https://x.cc/?ref=cv2#calc" in blk     # query 在 fragment 前
    assert "下单：" not in blk and "不发下单链接" in blk
    assert "不许拿它承诺收益" in blk                # 试算红线
    # 显式 cta 参数优先于内部推导
    blk2 = sc.build_catalog_block(
        prods, push_level="soft", site=_cta_site(), milestone_idx=2, cta="")
    assert "试算器" not in blk2


def test_stats_catalog_cta_tiers_and_prom():
    from src.companion.goals.stats import GoalStats
    st = GoalStats()
    st.record_catalog_injected("order")
    st.record_catalog_injected("cs")
    st.record_catalog_injected("roi")
    st.record_catalog_injected()                   # 纯种草（无分级）
    d = st.dump()
    assert d["catalog_injected"] == 4
    assert d["catalog_cta"] == {"order": 1, "cs": 1, "roi": 1}
    prom = st.dump_prom()
    assert 'goals_catalog_cta_total{tier="order"} 1' in prom
    assert 'goals_catalog_cta_total{tier="cs"} 1' in prom
    st.reset()
    assert st.dump()["catalog_cta"] == {"order": 0, "cs": 0, "roi": 0}


def test_outcome_report_orders_by_plan(mem_store):
    """成交归因看板：order-hook/order_pull 结算的单按 plan 拆分；
    手动「标成交」（result=manual:agent）刻意不计入。"""
    from src.companion.goals.service import settle_order_ref
    now = time.time()
    for i, plan in enumerate(("a-team", "a-team", "t-team")):
        g = mem_store.create_goal(
            conversation_id=f"telegram:a1:9{i}", platform="telegram",
            account_id="a1", chat_key=f"9{i}",
            template="acquire_and_convert", deadline_days=10)
        res = settle_order_ref(
            mem_store, ref=f"telegram:a1:9{i}",
            order_id=f"BX-{i}", plan=plan, now=now)
        assert res["matched"] and res["updated"]
    g4 = mem_store.create_goal(
        conversation_id="telegram:a1:94", platform="telegram",
        account_id="a1", chat_key="94",
        template="acquire_and_convert", deadline_days=10)
    mem_store.update_goal_fields(
        g4["goal_id"], status="done", done_at=now,
        result="manual:agent")                     # 手动成交不进归因
    rep = mem_store.outcome_report(now - 60, now=now)
    assert rep["orders_by_plan"] == {"a-team": 2, "t-team": 1}
    assert rep["orders_settled"] == 3


# ── 成交闭环：order-hook 路由 + 手动标成交 ──────────────────────────────────

def _build_client(enabled=True, **goals_extra):
    goals = {"enabled": enabled, "db_path": ":memory:"}
    goals.update(goals_extra)
    cfg = {"companion": {"goals": goals}}
    sess = {"role": "", "user": "tester"}
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = dict(sess)

    register_goal_routes(
        app, auth_dep, SimpleNamespace(config=cfg, config_path=None))
    return TestClient(app), sess


def _hook_client(**hook_extra):
    hook = {"enabled": True, "token": HOOK_TOKEN}
    hook.update(hook_extra)
    return _build_client(order_hook=hook)


def _mk_goal(client, conv=CONV, template="acquire_and_convert"):
    r = client.post("/api/goals", json={
        "template": template, "conversation_id": conv})
    assert r.status_code == 200
    return r.json()["goal"]


class TestOrderHook:
    def test_hook_disabled_403(self):
        client, _ = _build_client()          # goals 开但 hook 没开
        r = client.post("/api/goals/order-hook",
                        json={"ref": CONV},
                        headers={"X-Goals-Token": HOOK_TOKEN})
        assert r.status_code == 403

    def test_bad_or_missing_token_403(self):
        client, _ = _hook_client()
        assert client.post("/api/goals/order-hook",
                           json={"ref": CONV}).status_code == 403
        assert client.post(
            "/api/goals/order-hook", json={"ref": CONV},
            headers={"X-Goals-Token": "wrong"}).status_code == 403
        # 空 token 配置 → 永拒（哪怕对方也送空）
        client2, _ = _hook_client(token="")
        assert client2.post(
            "/api/goals/order-hook", json={"ref": CONV, "token": ""},
        ).status_code == 403

    def test_missing_ref_400(self):
        client, _ = _hook_client()
        r = client.post("/api/goals/order-hook", json={},
                        headers={"X-Goals-Token": HOOK_TOKEN})
        assert r.status_code == 400

    def test_unmatched_ref_is_200_not_matched(self):
        client, _ = _hook_client()
        r = client.post("/api/goals/order-hook",
                        json={"ref": "telegram:a1:nobody"},
                        headers={"X-Goals-Token": HOOK_TOKEN})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "matched": False}

    def test_order_settles_goal_done_and_idempotent(self):
        client, _ = _hook_client()
        g = _mk_goal(client)
        r = client.post("/api/goals/order-hook",
                        json={"ref": f"chat:{CONV}", "order_id": "ord-1",
                              "plan": "autochat-team"},
                        headers={"X-Goals-Token": HOOK_TOKEN})
        assert r.status_code == 200
        d = r.json()
        assert d["matched"] is True and d["goal_id"] == g["goal_id"]
        detail = client.get(f"/api/goals/{g['goal_id']}").json()
        gv = detail["goal"]
        assert gv["status"] == "done"
        assert gv["milestone_idx"] == 4          # 5 里程碑模板推满
        assert gv["result"].startswith("order:autochat-team")
        # 同 order_id 重放 → dup，不再改状态
        r2 = client.post("/api/goals/order-hook",
                         json={"ref": CONV, "order_id": "ord-1"},
                         headers={"X-Goals-Token": HOOK_TOKEN})
        # 目标已 done（不再 active）→ 匹配不到活跃目标 → matched False 也可接受；
        # 语义要点：绝不 500、绝不重复结算。
        assert r2.status_code == 200

    def test_token_in_body_accepted(self):
        client, _ = _hook_client()
        _mk_goal(client, conv="telegram:a1:200")
        r = client.post("/api/goals/order-hook",
                        json={"ref": "telegram:a1:200", "token": HOOK_TOKEN})
        assert r.status_code == 200 and r.json()["matched"] is True

    def test_bearer_header_accepted(self):
        """Authorization: Bearer <token> 是推荐形态——除路由鉴权外，它在完整
        app 里还天然通过 CSRF 中间件的 Bearer 豁免口（见 security 套件）。"""
        client, _ = _hook_client()
        _mk_goal(client, conv="telegram:a1:300")
        r = client.post(
            "/api/goals/order-hook", json={"ref": "telegram:a1:300"},
            headers={"Authorization": f"Bearer {HOOK_TOKEN}"})
        assert r.status_code == 200 and r.json()["matched"] is True
        # 错的 Bearer 仍拒
        r2 = client.post(
            "/api/goals/order-hook", json={"ref": "telegram:a1:300"},
            headers={"Authorization": "Bearer wrong"})
        assert r2.status_code == 403


class TestManualWon:
    def test_active_done_via_status_action(self):
        client, _ = _build_client()
        g = _mk_goal(client)
        r = client.post(f"/api/goals/{g['goal_id']}/status",
                        json={"action": "done"})
        assert r.status_code == 200
        gv = r.json()["goal"]
        assert gv["status"] == "done"
        assert gv["milestone_idx"] == 4
        assert gv["result"] == "manual:agent"

    def test_paused_done_allowed_and_done_is_terminal(self):
        client, _ = _build_client()
        g = _mk_goal(client, conv="telegram:a1:300")
        gid = g["goal_id"]
        assert client.post(f"/api/goals/{gid}/status",
                           json={"action": "pause"}).status_code == 200
        assert client.post(f"/api/goals/{gid}/status",
                           json={"action": "done"}).status_code == 200
        # 终态后任何操作 → 400
        assert client.post(f"/api/goals/{gid}/status",
                           json={"action": "pause"}).status_code == 400

    def test_viewer_cannot_mark_done(self):
        client, sess = _build_client()
        g = _mk_goal(client, conv="telegram:a1:400")
        sess["role"] = "viewer"
        r = client.post(f"/api/goals/{g['goal_id']}/status",
                        json={"action": "done"})
        assert r.status_code == 403


# ── order_pull（引擎拉官网订单——NAT 后部署的实际成交通道） ────────────────────

def _pull_cfg(**over):
    base = {"enabled": True, "site_url": "https://bd2026.cc",
            "admin_key": "adm-key", "interval_min": 10}
    base.update(over)
    return {"companion": {"goals": {"enabled": True, "order_pull": base}}}


def _order(oid, ref, status="paid", plan="autochat-team", **extra):
    o = {"id": oid, "ref": ref, "status": status, "plan": plan,
         "contact": "@someone", "fingerprint": "FP"}  # PII 字段供裁剪断言
    o.update(extra)
    return o


class TestOrderPull:
    @pytest.fixture(autouse=True)
    def _clean_seen(self):
        from src.companion.goals import order_pull as op
        op.reset_seen()
        yield
        op.reset_seen()

    def test_cfg_defaults_and_gates(self, mem_store):
        from src.companion.goals import order_pull as op
        cfg = op.resolve_pull_cfg({})
        assert cfg["enabled"] is False and cfg["interval_min"] == 10
        # enabled 但缺 key / 缺 url → skipped，一次 HTTP 都不发
        calls = []
        for over in ({"admin_key": ""}, {"site_url": ""}, {"enabled": False}):
            s = op.pull_and_settle(
                mem_store, _pull_cfg(**over),
                http_get=lambda *a, **k: calls.append(1))
            assert s["skipped"] == 1
        assert calls == []

    def test_extract_filters_and_strips_pii(self):
        from src.companion.goals.order_pull import extract_ref_orders
        payload = {"ok": True, "orders": [
            _order("AH-1", CONV, "paid"),
            _order("AH-2", CONV, "activated"),
            _order("AH-3", CONV, "pending"),      # 没付款不算成交
            _order("AH-4", CONV, "cancelled"),
            _order("AH-5", "", "paid"),           # 自然流量无归因
            "not-a-dict",
        ]}
        out = extract_ref_orders(payload)
        assert [o["id"] for o in out] == ["AH-1", "AH-2"]
        for o in out:  # 字段裁剪：绝不带出 contact 等 PII（period=计费周期，非 PII）
            assert set(o) == {"id", "ref", "plan", "period", "status"}
        assert extract_ref_orders({"orders": "junk"}) == []
        assert extract_ref_orders(None) == []

    def test_pull_settles_goal_and_dedupes(self, mem_store):
        from src.companion.goals import order_pull as op
        g = mem_store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="100", template="acquire_and_convert", now=NOW)
        seen_headers = {}

        def fake_get(url, headers, timeout):
            seen_headers.update(headers)
            assert url == "https://bd2026.cc/api/admin/orders"
            return {"ok": True, "count": 3, "orders": [
                _order("AH-1", CONV, "paid"),
                _order("AH-2", "telegram:a1:nobody", "paid"),  # 无对应目标
                _order("AH-3", CONV, "pending"),
            ]}

        s = op.pull_and_settle(mem_store, _pull_cfg(), http_get=fake_get,
                               now=NOW)
        assert seen_headers == {"x-setup-key": "adm-key"}
        assert s["pulled"] == 3 and s["candidates"] == 2
        assert s["settled"] == 1 and s["unmatched"] == 1
        gv = mem_store.get_goal(g["goal_id"])
        assert gv["status"] == "done"
        assert gv["milestone_idx"] == 4
        assert gv["result"].startswith("order:autochat-team:AH-1")
        # 第二轮：进程内 _SEEN 直接跳过（零结算调用）
        s2 = op.pull_and_settle(mem_store, _pull_cfg(), http_get=fake_get,
                                now=NOW)
        assert s2["settled"] == 0 and s2["dup"] == 0
        # 清 _SEEN（模拟重启）：事件层幂等兜底——paid 单已 done 不再活跃 →
        # unmatched，绝不重复结算/重复计 done
        op.reset_seen()
        s3 = op.pull_and_settle(mem_store, _pull_cfg(), http_get=fake_get,
                                now=NOW)
        assert s3["settled"] == 0

    def test_pull_network_error_soft(self, mem_store):
        from src.companion.goals import order_pull as op

        def boom(url, headers, timeout):
            raise RuntimeError("net down")

        s = op.pull_and_settle(mem_store, _pull_cfg(), http_get=boom)
        assert s["errors"] == 1 and s["settled"] == 0

    def test_watchdog_check_gated_and_throttled(self, monkeypatch, mem_store):
        from src.companion.goals import order_pull as op
        from src.inbox.health_watchdog import HealthWatchdog
        calls = []
        monkeypatch.setattr(
            op, "pull_and_settle",
            lambda store, cfg, **k: calls.append(1) or
            {"settled": 1, "errors": 0})
        monkeypatch.setattr(
            "src.companion.goals.service.get_configured_store",
            lambda *a, **k: mem_store)
        cm = SimpleNamespace(config=_pull_cfg(), config_path=None)
        wd = HealthWatchdog(app=SimpleNamespace(), config_manager=cm)
        wd._check_goal_order_pull(now=NOW)
        assert calls == [1]
        assert wd.total_goal_orders_settled == 1
        # 节流窗内第二次 tick 不拉
        wd._check_goal_order_pull(now=NOW + 60)
        assert calls == [1]
        # 过节流窗再拉
        wd._check_goal_order_pull(now=NOW + 601)
        assert calls == [1, 1]
        # 配置关 / goals 总闸关 → 完全不拉
        cm2 = SimpleNamespace(config={}, config_path=None)
        wd2 = HealthWatchdog(app=SimpleNamespace(), config_manager=cm2)
        wd2._check_goal_order_pull(now=NOW)
        cfg_off = _pull_cfg()
        cfg_off["companion"]["goals"]["enabled"] = False
        cm3 = SimpleNamespace(config=cfg_off, config_path=None)
        wd3 = HealthWatchdog(app=SimpleNamespace(), config_manager=cm3)
        wd3._check_goal_order_pull(now=NOW)
        assert calls == [1, 1]


# ── link_guard（P4 链接纪律出站守卫：prompt 叮嘱之外的确定性兜底） ──────────────

SITE = "https://x.cc"
ORDER_LINK = "https://x.cc/order?plan=a-team&ref=abc"
CALC_LINK = "https://x.cc/?ref=abc#autochat"
FOREIGN = "https://example.com/page"


def _sanitize(text, cta):
    from src.companion.goals.link_guard import sanitize_goal_links
    return sanitize_goal_links(text, cta=cta, base_url=SITE)


class TestLinkGuardPure:
    def test_order_tier_passes_everything(self):
        t = f"下单：{ORDER_LINK} 试算 {CALC_LINK}"
        out, n = _sanitize(t, "order")
        assert n == 0 and out == t

    def test_empty_tier_strips_all_site_links_keeps_foreign(self):
        t = (f"你先看看 {FOREIGN} 哈\n"
             f"想要的话下单：{ORDER_LINK}\n"
             f"再算算 {CALC_LINK} 好不好")
        out, n = _sanitize(t, "")
        assert n == 2
        assert "x.cc" not in out
        assert FOREIGN in out                       # 别家域名不碰
        assert "下单" not in out or "下单：" not in out   # 引导标签一并清扫

    def test_cs_roi_tier_strips_order_keeps_calc(self):
        t = f"帮你算过 {CALC_LINK}，直接拍 {ORDER_LINK} 也行"
        for tier in ("cs", "roi"):
            out, n = _sanitize(t, tier)
            assert n == 1
            assert CALC_LINK in out                 # 站内非下单链放行
            assert "/order?plan=" not in out

    def test_label_line_husk_removed(self):
        t = f"这个真的适合你\n下单链接：{ORDER_LINK}\n慢慢看～"
        out, n = _sanitize(t, "")
        assert n == 1
        assert out == "这个真的适合你\n慢慢看～"       # 整行只剩标签 → 整行删

    def test_whole_reply_is_link_falls_back_to_original(self):
        out, n = _sanitize(ORDER_LINK, "")
        assert n == 0 and out == ORDER_LINK          # 宁可漏拦不吃回复

    def test_no_base_url_or_empty_text_noop(self):
        from src.companion.goals.link_guard import sanitize_goal_links
        out, n = sanitize_goal_links("看 https://x.cc/order?plan=x", cta="",
                                     base_url="")
        assert n == 0
        assert sanitize_goal_links("", cta="", base_url=SITE) == ("", 0)

    def test_subdomain_counts_as_same_site(self):
        out, n = _sanitize("看 https://www.x.cc/order?plan=a 吧", "")
        assert n == 1 and "x.cc" not in out


class TestLinkGuardWiring:
    """skill_manager._apply_goal_link_guard：unbound 调用 + 假 self（与本仓
    其他 SkillManager 纯方法测试同法，不起重型实例）。"""

    @staticmethod
    def _fake_self(enabled=True):
        import logging
        return SimpleNamespace(
            config=SimpleNamespace(config={"companion": {"goals": {
                "link_guard": {"enabled": enabled}}}}),
            logger=logging.getLogger("test.linkguard"),
        )

    @classmethod
    def _apply(cls, reply, uc, enabled=True):
        from src.skills.skill_manager import SkillManager
        return SkillManager._apply_goal_link_guard(
            cls._fake_self(enabled), reply, uc)

    def test_strips_and_burns_stash_and_counts(self):
        from src.companion.goals.stats import get_goal_stats
        get_goal_stats().reset()
        uc = {"_goal_cta": {"cta": "", "base_url": SITE,
                            "order_path": "/order"}}
        out = self._apply(f"拍这里 {ORDER_LINK} 哈", uc)
        assert "x.cc" not in out
        assert "_goal_cta" not in uc                 # 读后即焚
        assert get_goal_stats().dump()["link_stripped"] == 1

    def test_no_stash_means_untouched(self):
        t = f"看看 {ORDER_LINK}"
        assert self._apply(t, {}) == t               # 非目标会话零影响

    def test_kill_switch_respected_but_stash_still_burned(self):
        uc = {"_goal_cta": {"cta": "", "base_url": SITE}}
        t = f"看看 {ORDER_LINK}"
        assert self._apply(t, uc, enabled=False) == t
        assert "_goal_cta" not in uc                 # 关守卫也不留残留

    def test_order_tier_allows_links(self):
        uc = {"_goal_cta": {"cta": "order", "base_url": SITE}}
        t = f"直接拍 {ORDER_LINK}"
        assert self._apply(t, uc) == t


# ── 成交反哺选品（P4：近窗真卖动的 SKU 做痛点同分裁决） ─────────────────────────

def _cat2():
    """双产品目录：chatx 主打客服痛点，lingox 主打翻译痛点。"""
    return {
        "site": {"base_url": "https://x.cc", "order_path": "/order"},
        "products": [
            {"id": "chatx", "name_zh": "智聊", "pains": ["客服", "回复不过来"],
             "plans": [{"key": "a-team", "hot": True}]},
            {"id": "lingox", "name_zh": "通译", "pains": ["翻译"],
             "plans": [{"key": "b-basic", "hot": True},
                       {"key": "b-pro"}]},
        ],
    }


class TestSoldBoost:
    def test_store_sold_plan_counts_window_and_manual_excluded(self, mem_store):
        def _done(conv, result, done_at):
            g = mem_store.create_goal(
                conversation_id=conv, platform="telegram", account_id="a1",
                chat_key=conv.rsplit(":", 1)[-1],
                template="acquire_and_convert", now=done_at - 100)
            mem_store.update_goal_fields(
                g["goal_id"], status="done", result=result, done_at=done_at)
        _done("telegram:a1:s1", "order:a-team:AH-1", NOW - 5 * 86400)
        _done("telegram:a1:s2", "order:a-team:AH-2", NOW - 10 * 86400)
        _done("telegram:a1:s3", "order:b-basic:AH-3", NOW - 20 * 86400)
        _done("telegram:a1:s4", "order:a-team:OLD-1", NOW - 200 * 86400)  # 窗外
        _done("telegram:a1:s5", "manual:agent", NOW - 3 * 86400)          # 手动不计
        counts = mem_store.sold_plan_counts(days=90, now=NOW)
        assert counts == {"a-team": 2, "b-basic": 1}

    def test_sold_boost_map_folds_plans_to_products(self):
        cat = _cat2()
        m = sc.sold_boost_map(cat, {"a-team": 2, "b-basic": 3, "b-pro": 1,
                                    "ghost": 9})
        assert m == {"chatx": 2, "lingox": 4}        # 同产品多 plan 求和；未知 plan 忽略
        assert sc.sold_boost_map(cat, {}) == {}

    def test_pick_products_sold_breaks_tie_but_never_beats_pain_match(self):
        cat = _cat2()
        # 画像全空（全 0 分）→ 销量裁决：lingox 卖得多排前
        got = sc.pick_products(cat, {}, sold={"lingox": 5})
        assert [p["id"] for p in got][0] == "lingox"
        # 无销量数据 → 目录声明序（chatx 在前，旧行为不变）
        got0 = sc.pick_products(cat, {})
        assert [p["id"] for p in got0][0] == "chatx"
        # 痛点命中 chatx（回复不过来）→ 即使 lingox 销量高也不越位
        fields = {"need": {"v": "客服回复不过来"}}
        got2 = sc.pick_products(cat, fields, sold={"lingox": 99})
        assert [p["id"] for p in got2][0] == "chatx"

    def test_service_cached_counts_ttl(self, mem_store):
        from src.companion.goals import service as svc
        svc._SOLD_CACHE.clear()
        calls = []
        real = mem_store.sold_plan_counts
        mem_store.sold_plan_counts = (          # type: ignore[method-assign]
            lambda **k: calls.append(1) or real(**k))
        try:
            svc.sold_plan_counts_cached(mem_store, now=NOW)
            svc.sold_plan_counts_cached(mem_store, now=NOW + 60)
            assert calls == [1]                  # TTL 内命中缓存
            svc.sold_plan_counts_cached(mem_store, now=NOW + 400)
            assert calls == [1, 1]               # 过期重读
        finally:
            mem_store.sold_plan_counts = real    # type: ignore[method-assign]
            svc._SOLD_CACHE.clear()


# ── 留存/续费环（P5：成交即起下一周期，LTV 后半场） ─────────────────────────────

def _ret_cfg(enabled=True, catalog_path="", **extra):
    rc = {"enabled": enabled}
    rc.update(extra)
    goals = {"enabled": True, "db_path": ":memory:", "retention": rc}
    if catalog_path:
        goals["catalog"] = {"path": catalog_path}
    return {"companion": {"goals": goals}}


class TestRetentionLoop:
    def _acquire_goal(self, mem_store, conv=CONV):
        return mem_store.create_goal(
            conversation_id=conv, platform="telegram", account_id="a1",
            chat_key=conv.rsplit(":", 1)[-1],
            template="acquire_and_convert", priority=2, now=NOW)

    def test_order_settle_spawns_next_cycle(self, mem_store):
        from src.companion.goals import service as svc
        g = self._acquire_goal(mem_store)
        res = svc.settle_order_ref(
            mem_store, ref=CONV, order_id="o1", plan="a-team",
            now=NOW + 86400, cfg_root=_ret_cfg())
        assert res["updated"] is True
        nxt = mem_store.find_active_goal(conversation_id=CONV)
        assert nxt is not None
        assert nxt["template"] == "retention_expand"
        assert nxt["created_by"] == "retention_auto"
        p = nxt.get("params") or {}
        assert p.get("last_plan") == "a-team"
        assert p.get("base_goal") == g["goal_id"]
        # 周期天数默认 30（deadline = 结算时刻 + 30d）
        assert abs(float(nxt["deadline_ts"]) - (NOW + 86400 + 30 * 86400)) < 5

    def test_disabled_or_no_cfg_no_spawn(self, mem_store):
        from src.companion.goals import service as svc
        self._acquire_goal(mem_store, conv="telegram:a1:201")
        # retention 关
        svc.settle_order_ref(mem_store, ref="telegram:a1:201", order_id="o2",
                             plan="a-team", now=NOW, cfg_root=_ret_cfg(False))
        assert mem_store.find_active_goal(
            conversation_id="telegram:a1:201") is None
        # cfg_root 未传（旧调用方口径）→ 同样零副作用
        self._acquire_goal(mem_store, conv="telegram:a1:202")
        svc.settle_order_ref(mem_store, ref="telegram:a1:202", order_id="o3",
                             plan="a-team", now=NOW)
        assert mem_store.find_active_goal(
            conversation_id="telegram:a1:202") is None

    def test_renewal_chains_and_inherits_product(self, mem_store, tmp_path):
        """续费单结算留存目标 → 再起下一周期；product_id 按上单 plan 反查目录。"""
        from src.companion.goals import service as svc
        cat = tmp_path / "cat.yaml"
        cat.write_text(
            "site: {base_url: 'https://x.cc'}\n"
            "products:\n"
            "  - id: chatx\n"
            "    plans: [{key: a-team}]\n"
            "  - id: lingox\n"
            "    plans: [{key: b-basic}]\n", encoding="utf-8")
        cfg = _ret_cfg(catalog_path=str(cat))
        self._acquire_goal(mem_store)
        svc.settle_order_ref(mem_store, ref=CONV, order_id="o1", plan="a-team",
                             now=NOW, cfg_root=cfg)
        r1 = mem_store.find_active_goal(conversation_id=CONV)
        assert (r1.get("params") or {}).get("product_id") == "chatx"
        # 续费（第二单）→ 结算 r1 → 自动起第二周期，继承新单 plan 的产品
        res2 = svc.settle_order_ref(mem_store, ref=CONV, order_id="o2",
                                    plan="b-basic", now=NOW + 25 * 86400,
                                    cfg_root=cfg)
        assert res2["updated"] is True and res2["goal_id"] == r1["goal_id"]
        r2 = mem_store.find_active_goal(conversation_id=CONV)
        assert r2 is not None and r2["goal_id"] != r1["goal_id"]
        assert r2["template"] == "retention_expand"
        assert (r2.get("params") or {}).get("product_id") == "lingox"
        assert (r2.get("params") or {}).get("base_goal") == r1["goal_id"]

    def test_no_spawn_when_active_goal_exists(self, mem_store):
        """会话仍有别的活跃目标 → 不叠目标（单活跃口径优先）。"""
        from src.companion.goals import service as svc
        base = self._acquire_goal(mem_store)
        mem_store.update_goal_fields(base["goal_id"], status="done",
                                     result="order:a-team:o9")
        other = mem_store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="100", template="custom", now=NOW + 10)
        out = svc.maybe_create_retention_goal(
            mem_store, _ret_cfg(), base, plan="a-team", now=NOW + 20)
        assert out is None
        assert mem_store.find_active_goal(
            conversation_id=CONV)["goal_id"] == other["goal_id"]

    def test_stats_counter_and_prom(self, mem_store):
        from src.companion.goals import service as svc
        from src.companion.goals.stats import GoalStats, get_goal_stats
        st = get_goal_stats()
        before = st.retention_created
        self._acquire_goal(mem_store, conv="telegram:a1:301")
        svc.settle_order_ref(mem_store, ref="telegram:a1:301", order_id="o5",
                             plan="a-team", now=NOW, cfg_root=_ret_cfg())
        assert st.retention_created == before + 1
        s2 = GoalStats()
        s2.record_retention_created()
        assert s2.dump()["retention_created"] == 1
        assert "goals_retention_created_total 1" in s2.dump_prom()

    def test_manual_won_route_spawns_retention(self):
        """坐席手动标成交与订单回流同权：done 后自动起留存周期。"""
        client, _ = _build_client(retention={"enabled": True})
        g = _mk_goal(client, conv="telegram:a1:401")
        r = client.post(f"/api/goals/{g['goal_id']}/status",
                        json={"action": "done"})
        assert r.status_code == 200
        goals = client.get("/api/goals", params={"status": "active"}).json()
        mine = [v for v in goals["goals"]
                if v.get("conversation_id") == "telegram:a1:401"]
        assert len(mine) == 1
        assert mine[0]["template"] == "retention_expand"

    def test_manual_won_non_catalog_template_no_retention(self):
        """没卖东西的模板（无 catalog 能力）手动标成交 → 不 spawn 续费周期。"""
        client, _ = _build_client(retention={"enabled": True})
        g = _mk_goal(client, conv="telegram:a1:402",
                     template="relationship_stage")
        assert client.post(f"/api/goals/{g['goal_id']}/status",
                           json={"action": "done"}).status_code == 200
        goals = client.get("/api/goals", params={"status": "active"}).json()
        assert [v for v in goals["goals"]
                if v.get("conversation_id") == "telegram:a1:402"] == []

    def test_replayed_order_is_dup_never_fakes_renewal(self, mem_store):
        """P5 幂等升级的靶子：留存目标 spawn 后同一旧单重放（order_pull
        paid→activated 二次出现 / 重启后 _SEEN 清空）必须判 dup——
        修复前它会匹配到新的留存目标 → 假结算 done → 再 spawn 假周期。"""
        from src.companion.goals import service as svc
        self._acquire_goal(mem_store)
        svc.settle_order_ref(mem_store, ref=CONV, order_id="o1", plan="a-team",
                             now=NOW, cfg_root=_ret_cfg())
        r1 = mem_store.find_active_goal(conversation_id=CONV)
        assert r1["template"] == "retention_expand"
        res = svc.settle_order_ref(mem_store, ref=CONV, order_id="o1",
                                   plan="a-team", now=NOW + 3600,
                                   cfg_root=_ret_cfg())
        assert res["dup"] is True and res["updated"] is False
        still = mem_store.find_active_goal(conversation_id=CONV)
        assert still["goal_id"] == r1["goal_id"]        # 留存目标未被假结算
        assert str(still["status"]) == "active"

    def test_order_event_exists_conversation_scope(self, mem_store):
        g = mem_store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="100", template="acquire_and_convert", now=NOW)
        mem_store.add_event(g["goal_id"], "order", "ord-77|a-team")
        assert mem_store.order_event_exists(
            order_id="ord-77", conversation_id=CONV) is True
        # platform+chat_key 回落口径同样命中；未知单/空单号不命中
        assert mem_store.order_event_exists(
            order_id="ord-77", platform="telegram", chat_key="100") is True
        assert mem_store.order_event_exists(
            order_id="ord-99", conversation_id=CONV) is False
        assert mem_store.order_event_exists(
            order_id="", conversation_id=CONV) is False

    def test_item_label_inherited_into_title(self, mem_store, tmp_path):
        """留存目标标题自带产品名（goal_view 的 item_label 拼接约定）——
        30 天后上下文窗滚走，goal block 得自证 TA 买过什么。"""
        from src.companion.goals import service as svc
        cat = tmp_path / "cat.yaml"
        cat.write_text(
            "site: {base_url: 'https://x.cc'}\n"
            "products:\n"
            "  - id: chatx\n"
            "    name_zh: 智聊 ChatX\n"
            "    plans: [{key: a-team}]\n", encoding="utf-8")
        self._acquire_goal(mem_store)
        svc.settle_order_ref(mem_store, ref=CONV, order_id="o1", plan="a-team",
                             now=NOW, cfg_root=_ret_cfg(
                                 catalog_path=str(cat)))
        r1 = mem_store.find_active_goal(conversation_id=CONV)
        assert (r1.get("params") or {}).get("item_label") == "智聊 ChatX"
        view = svc.goal_view(r1)
        assert "智聊 ChatX" in view["title"]


# ── P6：年付周期自适应 + 流失挽回环 ──────────────────────────────────────────

_DAY = 86400.0


def _wb_cfg(enabled=True, **wb_extra):
    wb = {"enabled": enabled, "cooldown_days": 60, "max_age_days": 180,
          "days": 14, "max_per_day": 10}
    wb.update(wb_extra)
    return {"companion": {"goals": {
        "enabled": True, "db_path": ":memory:",
        "retention": {"enabled": True, "winback": wb},
    }}}


class TestPeriodDays:
    def test_pure_mapping_and_fallbacks(self):
        from src.companion.goals.service import retention_days_for_period
        tmpl = {"default_days": 30}
        assert retention_days_for_period({}, "annual", tmpl) == 365
        assert retention_days_for_period({}, "monthly", tmpl) == 30
        assert retention_days_for_period({}, "ANNUAL ", tmpl) == 365  # 规范化
        # 未知周期/无周期 → rc.days → 模板默认
        assert retention_days_for_period({"days": 45}, "weekly", tmpl) == 45
        assert retention_days_for_period({"days": 45}, "", tmpl) == 45
        assert retention_days_for_period({}, "", tmpl) == 30
        assert retention_days_for_period({}, "", None) == 30
        # config 覆写优先于内置表
        rc = {"period_days": {"annual": 400}, "days": 30}
        assert retention_days_for_period(rc, "annual", tmpl) == 400
        # 覆写表脏值忽略、其余键仍认内置
        rc2 = {"period_days": {"annual": "x"}}
        assert retention_days_for_period(rc2, "annual", tmpl) == 365
        assert retention_days_for_period(rc2, "monthly", tmpl) == 30

    def test_annual_order_spawns_365d_cycle(self, mem_store):
        from src.companion.goals import service as svc
        mem_store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="100", template="acquire_and_convert", now=NOW)
        svc.settle_order_ref(
            mem_store, ref=CONV, order_id="oy1", plan="a-team",
            period="annual", now=NOW, cfg_root=_ret_cfg())
        nxt = mem_store.find_active_goal(conversation_id=CONV)
        assert nxt is not None and nxt["template"] == "retention_expand"
        assert (nxt.get("params") or {}).get("last_period") == "annual"
        assert abs(float(nxt["deadline_ts"]) - (NOW + 365 * _DAY)) < 5

    def test_order_pull_passes_period_through(self, mem_store):
        """官网订单 period 经 order_pull → settle → 留存周期端到端生效。"""
        from src.companion.goals import order_pull as op
        op.reset_seen()
        mem_store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="100", template="acquire_and_convert", now=NOW)
        orders = op.extract_ref_orders({"orders": [
            _order("oy2", CONV, status="paid", period="Annual")]})
        assert orders and orders[0]["period"] == "annual"   # 规范化+透传
        cfg = _pull_cfg()
        cfg["companion"]["goals"]["retention"] = {"enabled": True}
        op.pull_and_settle(
            mem_store, cfg, now=NOW,
            http_get=lambda *a, **k: {"orders": [
                _order("oy2", CONV, status="paid", period="annual")]})
        nxt = mem_store.find_active_goal(conversation_id=CONV)
        assert nxt is not None
        assert abs(float(nxt["deadline_ts"]) - (NOW + 365 * _DAY)) < 5


class TestWinbackLoop:
    def _churned(self, mem_store, conv=CONV, *, deadline_days_ago=70.0,
                 status="expired", plan="a-team", now=NOW):
        """落一个 deadline 在 now-N 天的留存目标（默认已 settle 成 expired）。"""
        created = now - (deadline_days_ago + 30) * _DAY
        g = mem_store.create_goal(
            conversation_id=conv, platform="telegram", account_id="a1",
            chat_key=conv.rsplit(":", 1)[-1], template="retention_expand",
            deadline_days=30, params={"last_plan": plan},
            created_by="retention_auto", now=created)
        if status != "active":
            mem_store.update_goal_fields(g["goal_id"], status=status)
        return mem_store.get_goal(g["goal_id"])

    def test_store_overdue_window_and_created_after(self, mem_store):
        g1 = self._churned(mem_store, "telegram:a1:301",
                           deadline_days_ago=70)      # 窗口内
        self._churned(mem_store, "telegram:a1:302",
                      deadline_days_ago=200)           # 太陈年
        self._churned(mem_store, "telegram:a1:303",
                      deadline_days_ago=30)            # 冷却未满
        g4 = self._churned(mem_store, "telegram:a1:304",
                           deadline_days_ago=90, status="active")  # 静默未结算
        rows = mem_store.list_overdue_goals(
            template="retention_expand",
            before_ts=NOW - 60 * _DAY, after_ts=NOW - 180 * _DAY)
        ids = {r["goal_id"] for r in rows}
        assert ids == {g1["goal_id"], g4["goal_id"]}   # active+expired 都算
        # has_goal_created_after：流失点之后建过目标 → True
        dl = float(g1["deadline_ts"])
        assert mem_store.has_goal_created_after(
            dl, conversation_id="telegram:a1:301") is False
        mem_store.create_goal(
            conversation_id="telegram:a1:301", platform="telegram",
            chat_key="301", template="custom", now=NOW - 10 * _DAY)
        assert mem_store.has_goal_created_after(
            dl, conversation_id="telegram:a1:301") is True
        # platform+chat_key 回落口径
        assert mem_store.has_goal_created_after(
            dl, platform="telegram", chat_key="301") is True

    def test_spawns_after_cooldown_once(self, mem_store):
        from src.companion.goals import service as svc
        from src.companion.goals.stats import GoalStats, get_goal_stats
        st = get_goal_stats()
        before = st.winback_created
        src_goal = self._churned(mem_store)
        s = svc.run_winback_scan(mem_store, _wb_cfg(), now=NOW)
        assert s["created"] == 1 and s["candidates"] == 1
        wb = mem_store.find_active_goal(conversation_id=CONV)
        assert wb is not None
        assert wb["template"] == "engagement_reactivate"
        assert wb["created_by"] == "winback_auto"
        p = wb.get("params") or {}
        assert p.get("base_goal") == src_goal["goal_id"]
        assert "a-team" in str(p.get("note") or "")
        assert abs(float(wb["deadline_ts"]) - (NOW + 14 * _DAY)) < 5
        assert st.winback_created == before + 1
        s2i = GoalStats()
        s2i.record_winback_created()
        assert s2i.dump()["winback_created"] == 1
        assert "goals_winback_created_total 1" in s2i.dump_prom()
        # 幂等：再扫不重复挽回（流失点之后已有挽回目标）
        s2 = svc.run_winback_scan(mem_store, _wb_cfg(), now=NOW + _DAY)
        assert s2["created"] == 0 and s2["skipped"] >= 1
        # 挽回目标自己过期后也不再追第二次（一次流失只挽回一次）
        mem_store.update_goal_fields(wb["goal_id"], status="expired")
        s3 = svc.run_winback_scan(mem_store, _wb_cfg(), now=NOW + 2 * _DAY)
        assert s3["created"] == 0

    def test_window_gates(self, mem_store):
        from src.companion.goals import service as svc
        # 冷却未满（30 天前流失 < 60 天冷却）→ 不挽回
        self._churned(mem_store, "telegram:a1:311", deadline_days_ago=30)
        # 陈年流失（200 天前 > 180 上限）→ 功能首开不群发
        self._churned(mem_store, "telegram:a1:312", deadline_days_ago=200)
        s = svc.run_winback_scan(mem_store, _wb_cfg(), now=NOW)
        assert s["candidates"] == 0 and s["created"] == 0

    def test_sweeps_silent_active_then_winback(self, mem_store):
        """静默会话到期目标还挂 active → 扫描先结算落库（expired 进流失
        报表），同轮直接接挽回——settle-on-read 盲区补齐。"""
        from src.companion.goals import service as svc
        g = self._churned(mem_store, status="active")
        assert str(g["status"]) == "active"
        s = svc.run_winback_scan(mem_store, _wb_cfg(), now=NOW)
        assert s["swept"] == 1 and s["created"] == 1
        settled = mem_store.get_goal(g["goal_id"])
        assert settled["status"] == "expired"          # 诚实流失落库
        wb = mem_store.find_active_goal(conversation_id=CONV)
        assert wb is not None and wb["created_by"] == "winback_auto"

    def test_skips_when_goal_after_churn_or_active(self, mem_store):
        from src.companion.goals import service as svc
        # 流失点之后已有新目标（迟到续费起的新周期，已终态）→ 不挽回
        g = self._churned(mem_store, "telegram:a1:321")
        late = mem_store.create_goal(
            conversation_id="telegram:a1:321", platform="telegram",
            chat_key="321", template="acquire_and_convert",
            now=NOW - 5 * _DAY)
        mem_store.update_goal_fields(late["goal_id"], status="done")
        # 另一会话：当前有活跃目标 → 不叠加
        self._churned(mem_store, "telegram:a1:322")
        mem_store.create_goal(
            conversation_id="telegram:a1:322", platform="telegram",
            chat_key="322", template="custom",
            now=float(g["deadline_ts"]) - 40 * _DAY)   # 流失点之前就建的活跃目标
        s = svc.run_winback_scan(mem_store, _wb_cfg(), now=NOW)
        assert s["created"] == 0 and s["skipped"] == 2

    def test_daily_budget_caps(self, mem_store):
        from src.companion.goals import service as svc
        self._churned(mem_store, "telegram:a1:331", deadline_days_ago=75)
        self._churned(mem_store, "telegram:a1:332", deadline_days_ago=70)
        s = svc.run_winback_scan(mem_store, _wb_cfg(max_per_day=1), now=NOW)
        assert s["created"] == 1 and s["budget_hit"] is True

    def test_disabled_or_gate_off_noop(self, mem_store):
        from src.companion.goals import service as svc
        self._churned(mem_store)
        assert svc.run_winback_scan(
            mem_store, _wb_cfg(False), now=NOW)["created"] == 0
        cfg = _wb_cfg()
        cfg["companion"]["goals"]["enabled"] = False
        assert svc.run_winback_scan(mem_store, cfg, now=NOW)["created"] == 0

    def test_watchdog_wiring_gated_and_throttled(self, monkeypatch, mem_store):
        from src.companion.goals import service as svc
        from src.inbox.health_watchdog import HealthWatchdog
        calls = []
        monkeypatch.setattr(
            svc, "run_winback_scan",
            lambda store, cfg, **k: calls.append(1) or
            {"candidates": 0, "swept": 0, "created": 0, "skipped": 0,
             "budget_hit": False})
        monkeypatch.setattr(
            "src.companion.goals.service.get_configured_store",
            lambda *a, **k: mem_store)
        cm = SimpleNamespace(config=_wb_cfg(), config_path=None)
        wd = HealthWatchdog(app=SimpleNamespace(), config_manager=cm)
        wd._check_goal_winback(now=NOW)
        assert calls == [1]
        wd._check_goal_winback(now=NOW + 60)       # 节流窗内不扫
        assert calls == [1]
        wd._check_goal_winback(now=NOW + 3601)     # 过窗再扫
        assert calls == [1, 1]
        # winback 关 / goals 总闸关 → 完全不扫
        cm2 = SimpleNamespace(config=_wb_cfg(False), config_path=None)
        HealthWatchdog(app=SimpleNamespace(),
                       config_manager=cm2)._check_goal_winback(now=NOW)
        cfg_off = _wb_cfg()
        cfg_off["companion"]["goals"]["enabled"] = False
        cm3 = SimpleNamespace(config=cfg_off, config_path=None)
        HealthWatchdog(app=SimpleNamespace(),
                       config_manager=cm3)._check_goal_winback(now=NOW)
        assert calls == [1, 1]


# ── P7：回流再转化 + note 进 prompt ──────────────────────────────────────────

def _rcv_cfg(enabled=True, **rcv_extra):
    rcv = {"enabled": enabled, "days": 10}
    rcv.update(rcv_extra)
    return {"companion": {"goals": {
        "enabled": True, "db_path": ":memory:",
        "retention": {"enabled": True,
                      "winback": {"enabled": True, "reconvert": rcv}},
    }}}


def _signals(**kw):
    from src.companion.goals.signals import GoalSignals
    return GoalSignals(**kw)


class TestReconvertLoop:
    def _winback_goal(self, store, conv=CONV, *, plan="a-team", now=NOW):
        return store.create_goal(
            conversation_id=conv, platform="telegram", account_id="a1",
            chat_key=conv.rsplit(":", 1)[-1], template="engagement_reactivate",
            deadline_days=14,
            params={"note": "老客户流失挽回：曾购 a-team，上周期未续费",
                    "last_plan": plan, "base_goal": "g-src"},
            created_by="winback_auto", now=now)

    def test_refresh_done_spawns_reconvert(self, monkeypatch, mem_store):
        """端到端：流失客户回话 → refresh 结算挽回 done → 自动起再转化目标。"""
        from src.companion.goals import service as svc
        from src.companion.goals.stats import GoalStats, get_goal_stats
        st = get_goal_stats()
        before = st.reconvert_created
        g = self._winback_goal(mem_store)
        # 对方开口（last_inbound > start）→ engagement_reactivate done:replied
        monkeypatch.setattr(
            svc, "collect_signals",
            lambda **kw: _signals(now=NOW + _DAY,
                                  last_inbound_ts=NOW + _DAY - 60))
        out = svc.refresh_goal(mem_store, _rcv_cfg(), g, now=NOW + _DAY)
        assert str(out["goal"]["status"]) == "done"
        nxt = mem_store.find_active_goal(conversation_id=CONV)
        assert nxt is not None
        assert nxt["template"] == "acquire_and_convert"
        assert nxt["created_by"] == "reconvert_auto"
        p = nxt.get("params") or {}
        assert p.get("last_plan") == "a-team"
        assert p.get("base_goal") == g["goal_id"]
        assert "回流老客户" in str(p.get("note") or "")
        assert abs(float(nxt["deadline_ts"]) - (NOW + _DAY + 10 * _DAY)) < 5
        assert st.reconvert_created == before + 1
        s2 = GoalStats()
        s2.record_reconvert_created()
        assert s2.dump()["reconvert_created"] == 1
        assert "goals_reconvert_created_total 1" in s2.dump_prom()

    def test_agent_created_reactivate_done_no_reconvert(self, monkeypatch,
                                                        mem_store):
        """坐席手工建的普通唤回完成 → 不自动转卖（关系目标≠销售目标）。"""
        from src.companion.goals import service as svc
        g = mem_store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="100", template="engagement_reactivate",
            deadline_days=14, created_by="agent:tester", now=NOW)
        monkeypatch.setattr(
            svc, "collect_signals",
            lambda **kw: _signals(now=NOW + _DAY,
                                  last_inbound_ts=NOW + _DAY - 60))
        out = svc.refresh_goal(mem_store, _rcv_cfg(), g, now=NOW + _DAY)
        assert str(out["goal"]["status"]) == "done"
        assert mem_store.find_active_goal(conversation_id=CONV) is None

    def test_disabled_or_failed_no_reconvert(self, monkeypatch, mem_store):
        from src.companion.goals import service as svc
        # 开关关：done 也不 spawn
        g = self._winback_goal(mem_store, "telegram:a1:501")
        monkeypatch.setattr(
            svc, "collect_signals",
            lambda **kw: _signals(now=NOW + _DAY,
                                  last_inbound_ts=NOW + _DAY - 60))
        svc.refresh_goal(mem_store, _rcv_cfg(False), g, now=NOW + _DAY)
        assert mem_store.find_active_goal(
            conversation_id="telegram:a1:501") is None
        # 挽回失败（到期没回话 → failed）：人没回来，绝不追转化
        g2 = self._winback_goal(mem_store, "telegram:a1:502")
        monkeypatch.setattr(
            svc, "collect_signals", lambda **kw: _signals(now=NOW + 20 * _DAY))
        out = svc.refresh_goal(mem_store, _rcv_cfg(), g2, now=NOW + 20 * _DAY)
        assert str(out["goal"]["status"]) == "failed"
        assert mem_store.find_active_goal(
            conversation_id="telegram:a1:502") is None

    def test_direct_call_skips_when_active_goal_exists(self, mem_store):
        from src.companion.goals import service as svc
        g = self._winback_goal(mem_store, "telegram:a1:503")
        mem_store.update_goal_fields(g["goal_id"], status="done")
        g = mem_store.get_goal(g["goal_id"])
        other = mem_store.create_goal(
            conversation_id="telegram:a1:503", platform="telegram",
            chat_key="503", template="custom", now=NOW)
        assert svc.maybe_spawn_reconvert(mem_store, _rcv_cfg(), g) is None
        # 清掉活跃目标后可以 spawn（同一 done 目标语义上只会走到这一次）
        mem_store.update_goal_fields(other["goal_id"], status="cancelled")
        spawned = svc.maybe_spawn_reconvert(mem_store, _rcv_cfg(), g, now=NOW)
        assert spawned is not None and spawned["created_by"] == "reconvert_auto"

    def test_manual_done_route_spawns_reconvert(self):
        """坐席在 UI 把挽回目标标 done（对方回来了）→ 与 settle 路径同权。"""
        from src.companion.goals.store import get_goal_store
        client, _ = _build_client(
            retention={"enabled": True,
                       "winback": {"enabled": True,
                                   "reconvert": {"enabled": True}}})
        store = get_goal_store(":memory:")
        g = store.create_goal(
            conversation_id="telegram:a1:504", platform="telegram",
            account_id="a1", chat_key="504",
            template="engagement_reactivate", deadline_days=14,
            params={"last_plan": "a-team"},
            created_by="winback_auto", now=NOW)
        r = client.post(f"/api/goals/{g['goal_id']}/status",
                        json={"action": "done"})
        assert r.status_code == 200
        nxt = store.find_active_goal(conversation_id="telegram:a1:504")
        assert nxt is not None
        assert nxt["template"] == "acquire_and_convert"
        assert nxt["created_by"] == "reconvert_auto"

    def test_reconvert_won_reenters_retention(self, mem_store):
        """回流客户再成交 → settle_order_ref 结算再转化目标 → 留存周期重开
        ——生命周期闭环的回环点。"""
        from src.companion.goals import service as svc
        g = self._winback_goal(mem_store, "telegram:a1:505")
        mem_store.update_goal_fields(g["goal_id"], status="done")
        spawned = svc.maybe_spawn_reconvert(
            mem_store, _rcv_cfg(), mem_store.get_goal(g["goal_id"]), now=NOW)
        assert spawned is not None
        res = svc.settle_order_ref(
            mem_store, ref="telegram:a1:505", order_id="o-re1",
            plan="a-team", period="monthly", now=NOW + 3 * _DAY,
            cfg_root=_ret_cfg())
        assert res["updated"] is True
        ret = mem_store.find_active_goal(conversation_id="telegram:a1:505")
        assert ret is not None and ret["template"] == "retention_expand"


class TestGoalBlockNote:
    def test_note_line_rendered_and_capped(self):
        from src.companion.goals.context_block import build_goal_block
        blk = build_goal_block(
            title="沉默唤回", milestone_label="轻触探温", milestone_idx=0,
            day_index=1, total_days=14, intent="轻量打个招呼",
            context_note="老客户流失挽回：曾购 智聊 ChatX，上周期未续费")
        assert "【背景】老客户流失挽回" in blk
        # 80 字截断
        blk2 = build_goal_block(
            title="t", milestone_label="m", milestone_idx=0, day_index=1,
            total_days=0, context_note="很长" * 100)
        line = [ln for ln in blk2.split("\n") if ln.startswith("【背景】")][0]
        assert len(line) <= 4 + 80 + 1

    def test_note_dropped_first_on_overflow_and_intent_intact(self):
        from src.companion.goals.context_block import build_goal_block
        blk = build_goal_block(
            title="回流再转化", milestone_label="收口", milestone_idx=3,
            day_index=9, total_days=10, intent="意图" * 30,
            push_level="direct", context_note="背景" * 40,
            profile_gap="预算档", max_chars=220)
        assert "【背景】" not in blk          # 超长最先丢背景行
        assert blk.startswith("【工作目标】")
        assert "【推进纪律】" in blk          # 安全语义行恒在
        assert len(blk) <= 220

    def test_view_params_note_reaches_block(self, mem_store):
        """端到端：winback 目标的 note 经 goal_view → goal_view_block 进 prompt
        ——P7 前它只在坐席 UI 可见，LLM 拿不到「老客户曾购」语境。"""
        from src.companion.goals import service as svc
        from src.companion.goals.context_block import goal_view_block
        g = mem_store.create_goal(
            conversation_id=CONV, platform="telegram", chat_key="100",
            template="engagement_reactivate", deadline_days=14,
            params={"note": "老客户流失挽回：曾购 a-team，上周期未续费"},
            created_by="winback_auto", now=NOW)
        view = svc.goal_view(g, now=NOW)
        blk = goal_view_block(view)
        assert "【背景】老客户流失挽回" in blk and "a-team" in blk


# ── P8：流失原因结构化（lifecycle 采集轨 + note 对症）────────────────────────

def _lc_cfg():
    """lifecycle 会话通用最小配置（goals 开 + :memory:）。"""
    return {"companion": {"goals": {"enabled": True, "db_path": ":memory:"}}}


class TestChurnReason:
    # ── 纯函数：分类词表 + 锚词档 ────────────────────────────────────────────
    def test_capture_patterns(self):
        from src.companion.goals.profile_slots import capture_churn_reason
        assert capture_churn_reason("主要是当时觉得太贵了") == "太贵"
        assert capture_churn_reason("后来没时间用，基本闲置") == "没用起来"
        assert capture_churn_reason("感觉没什么效果就停了") == "效果不佳"
        assert capture_churn_reason("老是掉线，不太稳定") == "出了问题"
        assert capture_churn_reason("我们换了别家工具") == "换了别家"
        assert capture_churn_reason("生意不做了，准备回国发展") == "业务变动"
        assert capture_churn_reason("公司在降本，预算砍了") == "预算紧张"
        # 多类命中合并（顿号连接、≤3 类）
        assert capture_churn_reason("太贵了而且老是报错") == "太贵、出了问题"
        # 无命中 / 空 / 超长 → ""
        assert capture_churn_reason("今天天气不错") == ""
        assert capture_churn_reason("") == ""
        assert capture_churn_reason("贵" * 3000) == ""

    def test_anchor_gate_for_retention_context(self):
        from src.companion.goals.profile_slots import capture_churn_reason
        # 留存会话是日常闲聊：没有续费语境锚词 → 不采（餐厅贵≠产品贵）
        assert capture_churn_reason(
            "这家餐厅也太贵了吧", require_anchor=True) == ""
        assert capture_churn_reason(
            "太贵了，先不续了", require_anchor=True) == "太贵"
        assert capture_churn_reason(
            "到期就没再开了，用不起", require_anchor=True) == "太贵"
        # 免锚档（挽回/回流会话本身就在聊流失）不受影响
        assert capture_churn_reason("这也太贵了") == "太贵"

    def test_lifecycle_slot_isolated_from_funnel(self):
        """churn_reason 不进全轨缺口枚举（LLM 抽取）/bant 缺口/填充率——
        新客户永远不会被当成「该问为什么没续」。"""
        from src.companion.goals.profile_slots import (
            fill_rates,
            get_slot,
            missing_slots,
        )
        assert get_slot("churn_reason") is not None      # 注册表可见（UI/入库）
        keys_all = {m["key"] for m in missing_slots({}, track="", limit=99)}
        assert "churn_reason" not in keys_all            # LLM 全轨枚举不含
        keys_bant = {m["key"] for m in missing_slots({}, track="bant", limit=99)}
        assert "churn_reason" not in keys_bant           # 摸底缺口不含
        lc = missing_slots({}, track="lifecycle", limit=9)
        assert [m["key"] for m in lc] == ["churn_reason"]  # 显式点名可及
        # 填充率双轨不受 lifecycle 槽影响
        filled = {"churn_reason": {"v": "太贵", "src": "auto", "ts": NOW}}
        fr = fill_rates(filled)
        assert fr["relation"] == 0.0 and fr["bant"] == 0.0

    # ── service 接线：按 created_by 门控 ────────────────────────────────────
    def _lifecycle_goal(self, store, created_by, template, conv=CONV):
        return store.create_goal(
            conversation_id=conv, platform="telegram", account_id="a1",
            chat_key=conv.rsplit(":", 1)[-1], template=template,
            deadline_days=14, created_by=created_by, now=NOW)

    def _churn_value(self, store, chat_key="100"):
        prof = store.get_customer_profile("telegram", chat_key)
        cell = ((prof or {}).get("fields") or {}).get("churn_reason")
        return str((cell or {}).get("v") or "") if isinstance(cell, dict) else ""

    def test_winback_conversation_captures_no_anchor(self):
        from src.companion.goals.service import (
            build_block_for_chat,
            get_configured_store,
        )
        from src.companion.goals.stats import get_goal_stats
        st = get_goal_stats()
        before = st.churn_captured
        cfg_obj = SimpleNamespace(config=_lc_cfg(), config_path=None)
        store = get_configured_store(cfg_obj.config, None)
        self._lifecycle_goal(store, "winback_auto", "engagement_reactivate")
        build_block_for_chat(
            cfg_obj, platform="telegram", chat_key="100", account_id="a1",
            user_context={}, chain="reply",
            inbound_text="主要是当时觉得太贵了", now=NOW + 60)
        assert self._churn_value(store) == "太贵"
        assert st.churn_captured == before + 1
        # 二次命中不覆盖已有值、不重复计数（auto 只填空）
        build_block_for_chat(
            cfg_obj, platform="telegram", chat_key="100", account_id="a1",
            user_context={}, chain="reply",
            inbound_text="后来也没怎么用起来", now=NOW + 120)
        assert self._churn_value(store) == "太贵"
        assert st.churn_captured == before + 1

    def test_retention_conversation_requires_anchor(self):
        from src.companion.goals.service import (
            build_block_for_chat,
            get_configured_store,
        )
        cfg_obj = SimpleNamespace(config=_lc_cfg(), config_path=None)
        store = get_configured_store(cfg_obj.config, None)
        self._lifecycle_goal(store, "retention_auto", "retention_expand")
        build_block_for_chat(
            cfg_obj, platform="telegram", chat_key="100", account_id="a1",
            user_context={}, chain="reply",
            inbound_text="这家餐厅也太贵了吧", now=NOW + 60)
        assert self._churn_value(store) == ""          # 日常吐槽不采
        build_block_for_chat(
            cfg_obj, platform="telegram", chat_key="100", account_id="a1",
            user_context={}, chain="reply",
            inbound_text="太贵了，先不续了", now=NOW + 120)
        assert self._churn_value(store) == "太贵"      # 带续费锚词才采

    def test_acquire_conversation_never_captures(self):
        """普通获客会话（auto_create/坐席建）说「太贵不续」≠流失原因——
        created_by 门控保证只有生命周期目标会话才采。"""
        from src.companion.goals.service import (
            build_block_for_chat,
            get_configured_store,
        )
        cfg_obj = SimpleNamespace(config=_lc_cfg(), config_path=None)
        store = get_configured_store(cfg_obj.config, None)
        self._lifecycle_goal(store, "auto_create", "acquire_and_convert")
        build_block_for_chat(
            cfg_obj, platform="telegram", chat_key="100", account_id="a1",
            user_context={}, chain="reply",
            inbound_text="太贵了，先不续了", now=NOW + 60)
        assert self._churn_value(store) == ""

    # ── note 对症：三处生命周期建目标织入原因 ───────────────────────────────
    def test_winback_note_includes_captured_reason(self, mem_store):
        from src.companion.goals import service as svc
        created = NOW - 100 * _DAY
        g = mem_store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="100", template="retention_expand", deadline_days=30,
            params={"last_plan": "a-team"}, created_by="retention_auto",
            now=created)
        mem_store.update_goal_fields(g["goal_id"], status="expired")
        mem_store.upsert_customer_profile(
            "telegram", "100", {"churn_reason": "太贵"}, now=NOW)
        s = svc.run_winback_scan(mem_store, _wb_cfg(), now=NOW)
        assert s["created"] == 1
        wb = mem_store.find_active_goal(conversation_id=CONV)
        note = str((wb.get("params") or {}).get("note") or "")
        assert "TA说过原因：太贵" in note

    def test_reconvert_note_targets_reason(self, mem_store):
        from src.companion.goals import service as svc
        g = mem_store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="100", template="engagement_reactivate",
            deadline_days=14, params={"last_plan": "a-team"},
            created_by="winback_auto", now=NOW)
        mem_store.update_goal_fields(g["goal_id"], status="done")
        mem_store.upsert_customer_profile(
            "telegram", "100", {"churn_reason": "太贵、没用起来"}, now=NOW)
        spawned = svc.maybe_spawn_reconvert(
            mem_store, _rcv_cfg(), mem_store.get_goal(g["goal_id"]), now=NOW)
        assert spawned is not None
        note = str((spawned.get("params") or {}).get("note") or "")
        assert "太贵、没用起来" in note and "别再踩同一个坑" in note

    def test_retention_params_note_on_rebuy(self, mem_store):
        """曾流失（原因在档）的客户再成交 → 新留存周期 note 提前预警。"""
        from src.companion.goals import service as svc
        mem_store.upsert_customer_profile(
            "telegram", "100", {"churn_reason": "效果不佳"}, now=NOW)
        mem_store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="100", template="acquire_and_convert", now=NOW)
        svc.settle_order_ref(
            mem_store, ref=CONV, order_id="o-p8", plan="a-team",
            now=NOW, cfg_root=_ret_cfg())
        ret = mem_store.find_active_goal(conversation_id=CONV)
        assert ret is not None and ret["template"] == "retention_expand"
        note = str((ret.get("params") or {}).get("note") or "")
        assert "效果不佳" in note and "提前留意" in note
        # 无档客户 → 不带 note（不给 LLM 编造空间）
        mem_store.create_goal(
            conversation_id="telegram:a1:601", platform="telegram",
            account_id="a1", chat_key="601",
            template="acquire_and_convert", now=NOW)
        svc.settle_order_ref(
            mem_store, ref="telegram:a1:601", order_id="o-p8b",
            plan="a-team", now=NOW, cfg_root=_ret_cfg())
        r2 = mem_store.find_active_goal(conversation_id="telegram:a1:601")
        assert "note" not in (r2.get("params") or {})

    def test_stats_dump_and_prom(self):
        from src.companion.goals.stats import GoalStats
        st = GoalStats()
        st.record_churn_captured()
        assert st.dump()["churn_captured"] == 1
        assert "goals_churn_captured_total 1" in st.dump_prom()
        st.reset()
        assert st.dump()["churn_captured"] == 0

    def test_outcome_report_churn_breakdown(self, mem_store):
        """报表出流失原因分布（逐标签拆分、按量排序、窗口按槽位 ts）——
        「都嫌贵」调价、「都没用起来」补 onboarding 的直接读数面。"""
        mem_store.upsert_customer_profile(
            "telegram", "701", {"churn_reason": "太贵、没用起来"}, now=NOW)
        mem_store.upsert_customer_profile(
            "telegram", "702", {"churn_reason": "太贵"}, now=NOW)
        mem_store.upsert_customer_profile(          # 窗口外的老采集不计
            "telegram", "703", {"churn_reason": "换了别家"},
            now=NOW - 90 * _DAY)
        mem_store.upsert_customer_profile(          # 无 churn 槽的画像不计
            "telegram", "704", {"need": "获客难"}, now=NOW)
        rep = mem_store.outcome_report(NOW - 30 * _DAY, now=NOW)
        assert rep["churn_reasons"] == {"太贵": 2, "没用起来": 1}
        assert list(rep["churn_reasons"])[0] == "太贵"   # 按量降序
        # 空库骨架键恒在（前端零判空负担）
        empty = GoalStore(":memory:").outcome_report(0)
        assert "churn_reasons" in empty and "churn_outcomes" in empty


# ── P9：客户档案行进 prompt + 流失原因对症应对策略 ──────────────────────────

class TestProfileFactsAndStrategy:
    # ── facts_line 纯函数 ────────────────────────────────────────────────────
    def test_facts_line_registry_order_and_format(self):
        from src.companion.goals.profile_slots import facts_line
        assert facts_line({}) == ""
        assert facts_line(None) == ""
        f = {
            "budget": {"v": "500刀", "src": "auto", "ts": NOW},
            "name": {"v": "阿龙", "src": "auto", "ts": NOW},
            "need": {"v": "客服人手不够", "src": "agent", "ts": NOW},
        }
        line = facts_line(f)
        assert "称呼:阿龙" in line and "业务痛点:客服人手不够" in line
        assert "预算档:500刀" in line and "｜" in line
        # 注册表顺序：relation(name) 在 bant(need/budget) 前
        assert line.index("称呼") < line.index("业务痛点") < line.index("预算档")
        # 裸字符串单元（旧形状容错）也认
        assert facts_line({"name": "老王"}) == "称呼:老王"

    def test_facts_line_budgets(self):
        from src.companion.goals.profile_slots import facts_line
        # 单值截 14 字
        long_v = {"need": {"v": "获" * 40, "src": "auto", "ts": NOW}}
        assert facts_line(long_v) == "业务痛点:" + "获" * 14
        # 槽数上限
        many = {k: {"v": "有", "src": "a", "ts": NOW}
                for k in ("name", "location", "occupation", "interests",
                          "need", "channel")}
        assert facts_line(many, limit=3).count("｜") == 2
        # 整行字符预算：装不下的槽直接停
        assert len(facts_line(many, max_chars=24)) <= 24
        # lifecycle 槽也进档案（在档=老客户，语境重要）
        assert facts_line(
            {"churn_reason": {"v": "太贵", "src": "auto", "ts": NOW}},
        ) == "流失原因:太贵"

    # ── churn_strategy_hint 纯函数 ──────────────────────────────────────────
    def test_churn_strategy_hint(self):
        from src.companion.goals.profile_slots import (
            _CHURN_PATTERNS,
            _CHURN_STRATEGY,
            churn_strategy_hint,
        )
        assert "性价比" in churn_strategy_hint("太贵")
        # 多标签取主因（首标签）
        assert churn_strategy_hint("太贵、没用起来") == churn_strategy_hint("太贵")
        assert churn_strategy_hint("没听过的标签") == ""
        assert churn_strategy_hint("") == ""
        # 词表齐平：每个可采标签都有应对策略（新增 pattern 忘配策略即红）
        assert {lab for lab, _ in _CHURN_PATTERNS} == set(_CHURN_STRATEGY)
        # 策略短句纪律（背景行 80 字预算内要装下 note+应对）
        assert all(len(v) <= 26 for v in _CHURN_STRATEGY.values())

    # ── 块渲染：档案行 + 溢出梯队 + note_suffix ─────────────────────────────
    def test_block_renders_facts_line(self):
        from src.companion.goals.context_block import build_goal_block
        block = build_goal_block(
            title="获客转化：阿龙", milestone_label="摸底",
            milestone_idx=1, day_index=2, total_days=10,
            intent="顺势聊聊生意近况",
            profile_facts="称呼:阿龙｜业务痛点:客服人手")
        assert "【客户档案】称呼:阿龙｜业务痛点:客服人手" in block
        # 档案行位于背景/意图之间的固定位（背景缺省时紧跟标题行）
        lines = block.split("\n")
        assert lines[1].startswith("【客户档案】")

    def test_block_overflow_drops_facts_first(self):
        from src.companion.goals.context_block import build_goal_block
        kw = dict(
            title="获客转化", milestone_label="摸底", milestone_idx=1,
            day_index=2, total_days=10,
            context_note="回流老客户：曾购 智聊，当初因「太贵」没续",
            profile_facts="称呼:阿龙｜坐标:马尼拉｜职业/生意:民宿｜预算档:500刀",
            intent="顺势聊聊生意近况，别急着推",
        )
        full = build_goal_block(max_chars=500, **kw)
        assert "【客户档案】" in full and "【背景】" in full
        # 收紧预算 → 档案行最先让位，背景（战略语境）保留
        tight = build_goal_block(max_chars=len(full) - 10, **kw)
        assert "【客户档案】" not in tight
        assert "【背景】" in tight and "【推进纪律】" in tight

    def _view(self, note=""):
        return {
            "title": "流失挽回：阿龙", "milestone_label": "重新联系",
            "milestone_idx": 0, "day_index": 1, "total_days": 14,
            "today": {"intent": "轻松问候", "push_level": "soft"},
            "milestones": [{"id": "a"}, {"id": "b"}],
            "params": {"note": note} if note else {},
        }

    def test_view_block_note_suffix_merges(self):
        from src.companion.goals.context_block import goal_view_block
        # 有 note → 分号衔接
        block = goal_view_block(
            self._view(note="老客户流失挽回：曾购 a-team"),
            note_suffix="应对：聊性价比与入门档位")
        assert "【背景】老客户流失挽回：曾购 a-team；应对：聊性价比与入门档位" in block
        # 无 note → 后缀独立成背景行
        block2 = goal_view_block(self._view(), note_suffix="应对：聊性价比与入门档位")
        assert "【背景】应对：聊性价比与入门档位" in block2
        # 无后缀 → 行为不变
        assert "【背景】" not in (goal_view_block(self._view()) or "")

    # ── service e2e：档案+策略进真实注入块 ─────────────────────────────────
    def test_service_block_carries_facts_and_strategy(self):
        """留存会话：画像在档（称呼+上周期流失原因）→ 目标块同时出
        【客户档案】与【背景】应对策略——策略按画像现值动态拼，
        不依赖目标创建时是否写过 note。"""
        from src.companion.goals.service import (
            build_block_for_chat,
            get_configured_store,
        )
        cfg_obj = SimpleNamespace(config=_lc_cfg(), config_path=None)
        store = get_configured_store(cfg_obj.config, None)
        store.upsert_customer_profile(
            "telegram", "100",
            {"name": "阿龙", "churn_reason": "太贵"}, now=NOW)
        store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="100", template="retention_expand",
            deadline_days=30, created_by="retention_auto", now=NOW)
        block = build_block_for_chat(
            cfg_obj, platform="telegram", chat_key="100", account_id="a1",
            user_context={}, chain="reply",
            inbound_text="最近还行啊", now=NOW + 60)
        assert block and "称呼:阿龙" in block and "流失原因:太贵" in block
        assert "应对：" in block and "性价比" in block

    def test_service_acquire_gets_facts_but_no_strategy(self):
        """普通获客会话：档案事实照进（个性化），但应对策略是生命周期
        专属（created_by 门控）——新客户块里绝不冒出「应对流失」语义。"""
        from src.companion.goals.service import (
            build_block_for_chat,
            get_configured_store,
        )
        cfg_obj = SimpleNamespace(config=_lc_cfg(), config_path=None)
        store = get_configured_store(cfg_obj.config, None)
        store.upsert_customer_profile(
            "telegram", "100", {"name": "阿龙", "churn_reason": "太贵"},
            now=NOW)
        store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="100", template="acquire_and_convert",
            created_by="auto_create", now=NOW)
        block = build_block_for_chat(
            cfg_obj, platform="telegram", chat_key="100", account_id="a1",
            user_context={}, chain="reply",
            inbound_text="你好呀", now=NOW + 60)
        assert block and "称呼:阿龙" in block
        assert "应对：" not in block


# ── P10：开闸就绪度（开关齐了但没绑账号 = 最常见卡点）────────────────────

class TestGrowthReadiness:
    def test_disabled_and_waiting_bind(self, su_wan_persona, monkeypatch):
        from src.companion.goals.readiness import growth_readiness
        # 总闸关
        off = growth_readiness({"companion": {"goals": {"enabled": False}}})
        assert off["status"] == "disabled" and off["ready"] is False
        assert "goals_disabled" in off["blockers"]
        # 自动建开 + allowlist 有人设，但无账号绑定 → waiting_bind
        monkeypatch.setattr(
            "src.companion.goals.readiness._bound_accounts",
            lambda *a, **k: [])
        monkeypatch.setattr(
            "src.companion.goals.readiness._catalog_ok", lambda *_: True)
        cfg = {"companion": {"goals": {
            "enabled": True,
            "auto_create": {"enabled": True, "personas": ["su_wan"]},
            "order_hook": {"enabled": True, "token": "t"},
            "retention": {"enabled": True,
                          "winback": {"enabled": True,
                                      "reconvert": {"enabled": True}}},
        }}}
        wb = growth_readiness(cfg)
        assert wb["status"] == "waiting_bind"
        assert "no_account_bound" in wb["blockers"]
        assert any("绑定" in h for h in wb["hints"])
        assert wb["checks"]["persona_exists"]["su_wan"] is True

    def test_ready_when_bound_and_channels_ok(self, su_wan_persona, monkeypatch):
        from src.companion.goals.readiness import growth_readiness
        monkeypatch.setattr(
            "src.companion.goals.readiness._bound_accounts",
            lambda *a, **k: [{"platform": "telegram", "account_id": "a1",
                              "persona_id": "su_wan"}])
        monkeypatch.setattr(
            "src.companion.goals.readiness._catalog_ok", lambda *_: True)
        cfg = {"companion": {"goals": {
            "enabled": True,
            "auto_create": {"enabled": True, "personas": ["su_wan"]},
            "order_pull": {"enabled": True, "site_url": "https://bd2026.cc",
                           "admin_key": "k"},
            "retention": {"enabled": True,
                          "winback": {"enabled": True,
                                      "reconvert": {"enabled": True}}},
        }}}
        r = growth_readiness(cfg)
        assert r["status"] == "ready" and r["ready"] is True
        assert r["acquire_ready"] is True
        assert r["checks"]["bound_count"] == 1
        assert r["blockers"] == []

    def test_partial_without_retention(self, su_wan_persona, monkeypatch):
        """获客主链齐、留存未开 → partial（能卖但没 LTV 环）。"""
        from src.companion.goals.readiness import growth_readiness
        monkeypatch.setattr(
            "src.companion.goals.readiness._bound_accounts",
            lambda *a, **k: [{"platform": "telegram", "account_id": "a1",
                              "persona_id": "su_wan"}])
        monkeypatch.setattr(
            "src.companion.goals.readiness._catalog_ok", lambda *_: True)
        cfg = {"companion": {"goals": {
            "enabled": True,
            "auto_create": {"enabled": True, "personas": ["su_wan"]},
            "order_hook": {"enabled": True, "token": "t"},
        }}}
        r = growth_readiness(cfg)
        assert r["status"] == "partial" and r["acquire_ready"] is True
        assert r["ready"] is False
        assert any("留存" in h for h in r["hints"])

    def test_empty_allowlist_and_missing_persona(self, monkeypatch):
        from src.companion.goals.readiness import growth_readiness
        monkeypatch.setattr(
            "src.companion.goals.readiness._bound_accounts",
            lambda *a, **k: [])
        # allowlist 空
        r = growth_readiness({"companion": {"goals": {
            "enabled": True,
            "auto_create": {"enabled": True, "personas": []}}}})
        assert "personas_allowlist_empty" in r["blockers"]
        # 人设不在档
        monkeypatch.setattr(
            "src.companion.goals.readiness._persona_exists", lambda *_: False)
        r2 = growth_readiness({"companion": {"goals": {
            "enabled": True,
            "auto_create": {"enabled": True,
                            "personas": ["ghost_gal"]}}}})
        assert any(b.startswith("persona_missing") for b in r2["blockers"])

    def test_route_returns_even_when_goals_disabled(self):
        """goals 关也 200——正是「为什么跑不起来」的答案，不走 403。"""
        client, _ = _build_client(enabled=False)
        r = client.get("/api/goals/readiness")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "disabled"
        assert "goals_disabled" in d["blockers"]
        # goals 开时同形
        client2, _ = _build_client(
            auto_create={"enabled": True, "personas": ["su_wan"]})
        r2 = client2.get("/api/goals/readiness")
        assert r2.status_code == 200
        assert "checks" in r2.json() and "bound_count" in r2.json()["checks"]


# ── P11：流失原因驱动选品/CTA + 待绑候选可操作化 ─────────────────────────

class TestChurnOfferSteer:
    def test_steer_matrix_no_invented_discount(self):
        from src.companion.goals.profile_slots import churn_offer_steer
        # 价格敏感 → 入门档 + soft 期提前试算
        assert churn_offer_steer("太贵") == {
            "plan_pref": "entry", "cta_bias": "roi"}
        assert churn_offer_steer("预算紧张、太贵")["plan_pref"] == "entry"
        # 信任问题 → 客服收口（不甩自助下单）
        assert churn_offer_steer("没用起来") == {
            "plan_pref": "default", "cta_bias": "cs"}
        assert churn_offer_steer("出了问题")["cta_bias"] == "cs"
        # 未知/空 → 不改行为
        assert churn_offer_steer("") == {"plan_pref": "", "cta_bias": ""}
        assert churn_offer_steer("随便说的")["plan_pref"] == ""

    def test_pick_cta_bias_roi_early_and_cs_override(self):
        site = {
            "support_contact": "@cs",
            "calc_url": "https://bd2026.cc/#calc",
        }
        # soft + 早期里程碑：无 bias 不给 roi；bias=roi 提前给
        assert sc.pick_cta(push_level="soft", milestone_idx=0, site=site) == ""
        assert sc.pick_cta(push_level="soft", milestone_idx=0, site=site,
                           cta_bias="roi") == "roi"
        # direct + 资质够：bias=cs 改客服；bias=roi 仍 order（换档由 plan_pref）
        assert sc.pick_cta(push_level="direct", bant_fill=0.8, site=site,
                           cta_bias="cs") == "cs"
        assert sc.pick_cta(push_level="direct", bant_fill=0.8, site=site,
                           cta_bias="roi") == "order"

    def test_entry_plan_deep_link_and_catalog_copy(self):
        cat = sc.load_catalog(sc.DEFAULT_CATALOG_PATH)
        chatx = next(p for p in cat["products"] if p.get("id") == "chatx")
        hot = sc.product_link(cat["site"], chatx)
        entry = sc.product_link(cat["site"], chatx, plan_pref="entry")
        assert "autochat-team" in hot  # hot 默认
        assert "autochat-entry" in entry
        assert "discount" not in entry.lower()
        # 文案分叉：价格敏感提醒 + 信任问题客服口吻
        blk_entry = sc.build_catalog_block(
            [chatx], push_level="direct", site=cat["site"],
            cta="order", plan_pref="entry")
        assert blk_entry and "入门档" in blk_entry and "折扣" in blk_entry
        blk_cs = sc.build_catalog_block(
            [chatx], push_level="direct", site=cat["site"],
            cta="cs", cta_bias="cs")
        assert blk_cs and "信任" in blk_cs
        assert "资质还没聊透" not in blk_cs

    def test_roi_bias_keeps_order_despite_low_bant(self):
        """价格敏感 + direct：即使 BANT 不足也不改 cs（换入门档自助）。"""
        site = {
            "support_contact": "@cs",
            "calc_url": "https://bd2026.cc/#calc",
        }
        assert sc.pick_cta(push_level="direct", bant_fill=0.1, site=site,
                           cta_bias="roi") == "order"
        assert sc.pick_cta(push_level="direct", bant_fill=0.1, site=site) == "cs"

    def test_service_steers_on_lifecycle_goal(self, monkeypatch):
        """留存会话 + churn_reason=太贵 → direct 日入门档深链 + steered 计数。"""
        from src.companion.goals.service import (
            build_block_for_chat,
            get_configured_store,
        )
        from src.companion.goals.stats import get_goal_stats

        cfg_obj = SimpleNamespace(config=_lc_cfg(), config_path=None)
        store = get_configured_store(cfg_obj.config, None)
        get_goal_stats().reset()
        store.upsert_customer_profile(
            "telegram", "100",
            {"churn_reason": "太贵", "need": "客服回不过来"}, now=NOW)
        g = store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="100", template="retention_expand",
            params={"product_id": "chatx"},
            deadline_days=30, created_by="retention_auto", now=NOW)
        assert g is not None

        def _force_direct(store_, cfg_root, goal, **kw):
            g2 = dict(goal)
            g2["status"] = "active"
            g2["milestone_idx"] = 3
            return {
                "hold": None,
                "goal": g2,
                "action": {
                    "action_id": "steer-a1", "status": "planned",
                    "push_level": "direct", "intent": "续费收口",
                },
            }

        monkeypatch.setattr(
            "src.companion.goals.service.refresh_goal", _force_direct)
        monkeypatch.setattr(
            "src.companion.goals.store.GoalStore.mark_action",
            lambda self, *a, **k: True)
        uc: dict = {}
        block = build_block_for_chat(
            cfg_obj, platform="telegram", chat_key="100", account_id="a1",
            conversation_id=CONV, user_context=uc, chain="reply",
            inbound_text="再看看价格吧", now=NOW + 60)
        assert block and "autochat-entry" in block
        assert "入门档" in block
        assert get_goal_stats().dump()["churn_steered"] >= 1
        assert isinstance(uc.get("_goal_cta"), dict)
        assert uc["_goal_cta"].get("cta") == "order"


class TestBindCandidates:
    def test_candidates_online_first_and_hint(self, monkeypatch):
        from src.companion.goals.readiness import growth_readiness

        monkeypatch.setattr(
            "src.companion.goals.readiness._bound_accounts",
            lambda *a, **k: [])
        monkeypatch.setattr(
            "src.companion.goals.readiness._catalog_ok", lambda *_: True)
        monkeypatch.setattr(
            "src.companion.goals.readiness._iter_account_personas",
            lambda *_: [
                {"platform": "telegram", "account_id": "old", "persona_id": "lin_xiaoyu",
                 "status": "offline", "label": "旧号"},
                {"platform": "telegram", "account_id": "live", "persona_id": "chen_mo",
                 "status": "online", "label": "在线号"},
                {"platform": "telegram", "account_id": "sw", "persona_id": "su_wan",
                 "status": "online", "label": "已是苏婉"},
            ])
        cfg = {"companion": {"goals": {
            "enabled": True,
            "auto_create": {"enabled": True, "personas": ["su_wan"]},
            "order_hook": {"enabled": True, "token": "t"},
        }}}
        r = growth_readiness(cfg)
        assert r["status"] == "waiting_bind"
        cands = r["checks"]["bind_candidates"]
        # su_wan 已绑的不进候选；online 排前
        assert [c["account_id"] for c in cands] == ["live", "old"]
        assert cands[0]["suggest"] == "su_wan"
        assert any("在线号" in h and "su_wan" in h for h in r["hints"])

    def test_no_candidates_when_already_bound(self, monkeypatch):
        from src.companion.goals.readiness import growth_readiness
        monkeypatch.setattr(
            "src.companion.goals.readiness._bound_accounts",
            lambda *a, **k: [{"platform": "telegram", "account_id": "a1",
                              "persona_id": "su_wan"}])
        monkeypatch.setattr(
            "src.companion.goals.readiness._catalog_ok", lambda *_: True)
        monkeypatch.setattr(
            "src.companion.goals.readiness._iter_account_personas",
            lambda *_: [
                {"platform": "telegram", "account_id": "a1", "persona_id": "su_wan",
                 "status": "online", "label": "a1"},
            ])
        r = growth_readiness({"companion": {"goals": {
            "enabled": True,
            "auto_create": {"enabled": True, "personas": ["su_wan"]},
            "order_hook": {"enabled": True, "token": "t"},
            "retention": {"enabled": True,
                          "winback": {"enabled": True,
                                      "reconvert": {"enabled": True}}},
        }}})
        assert r["status"] == "ready"
        assert r["checks"]["bind_candidates"] == []


# ── P12：流失×成交矩阵 + 校准建议 + 挽回 note 入门预览 ───────────────────

class TestChurnOutcomesAndCalibration:
    def test_churn_outcomes_won_vs_lost(self, mem_store):
        """生命周期终态 JOIN 画像主因：won 只认 order:/manual:。"""
        mem_store.upsert_customer_profile(
            "telegram", "801", {"churn_reason": "太贵"}, now=NOW)
        mem_store.upsert_customer_profile(
            "telegram", "802", {"churn_reason": "太贵"}, now=NOW)
        mem_store.upsert_customer_profile(
            "telegram", "803", {"churn_reason": "没用起来"}, now=NOW)
        # 太贵：1 真成交 + 1 expired
        g1 = mem_store.create_goal(
            conversation_id="telegram:a1:801", platform="telegram",
            account_id="a1", chat_key="801",
            template="acquire_and_convert",
            created_by="reconvert_auto", now=NOW)
        mem_store.update_goal_fields(
            g1["goal_id"], status="done", done_at=NOW + 10,
            result="order:autochat-entry:o1")
        g2 = mem_store.create_goal(
            conversation_id="telegram:a1:802", platform="telegram",
            account_id="a1", chat_key="802",
            template="retention_expand",
            created_by="retention_auto", now=NOW)
        mem_store.update_goal_fields(
            g2["goal_id"], status="expired", done_at=NOW + 20)
        # 没用起来：winback done（回话）≠ won
        g3 = mem_store.create_goal(
            conversation_id="telegram:a1:803", platform="telegram",
            account_id="a1", chat_key="803",
            template="engagement_reactivate",
            created_by="winback_auto", now=NOW)
        mem_store.update_goal_fields(
            g3["goal_id"], status="done", done_at=NOW + 30,
            result="replied")
        rep = mem_store.outcome_report(NOW - 1, now=NOW + 40)
        out = rep["churn_outcomes"]
        assert out["太贵"]["n"] == 2 and out["太贵"]["won"] == 1
        assert out["太贵"]["expired"] == 1
        assert abs(out["太贵"]["won_rate"] - 0.5) < 0.01
        assert out["没用起来"]["done"] == 1 and out["没用起来"]["won"] == 0

    def test_calibration_bind_priority_and_price_hint(self):
        from src.companion.goals.calibration import growth_calibration
        ready = {
            "status": "waiting_bind",
            "blockers": ["no_account_bound"],
            "checks": {
                "personas_allowlist": ["su_wan"],
                "bind_candidates": [{
                    "label": "在线号", "account_id": "live",
                    "suggest": "su_wan",
                    "href": "/personas#profile=su_wan",
                }],
            },
            "hints": [],
        }
        cal = growth_calibration(readiness=ready)
        assert cal["priority"] == "bind"
        assert "在线号" in cal["hints"][0]
        assert cal["personas_href"] == "/personas#profile=su_wan"
        # 价格主因低成交 → churn_price（引擎不发明折扣）
        cal2 = growth_calibration(
            readiness={"status": "ready", "checks": {}, "blockers": [],
                       "hints": []},
            report={"churn_outcomes": {
                "太贵": {"n": 8, "done": 1, "failed": 2, "expired": 5,
                         "won": 0, "done_rate": 0.125, "won_rate": 0.0},
            }},
            stats={"churn_captured": 8, "churn_steered": 3,
                   "catalog_injected": 10, "auto_created": 1,
                   "injected": {"total": 5}},
        )
        assert cal2["priority"] == "churn_price"
        assert cal2["focus"] == "太贵"
        assert any("不发明折扣" in h for h in cal2["hints"])

    def test_steer_note_suffix_entry_key(self):
        from src.companion.goals.service import _steer_note_suffix
        note = _steer_note_suffix("太贵", {}, product_id="chatx")
        assert "入门档" in note and "autochat-entry" in note
        assert "折扣" in note
        assert "客服收口" in _steer_note_suffix("没用起来", {})
        assert _steer_note_suffix("", {}) == ""

    def test_winback_note_carries_entry_preview(self, mem_store):
        from src.companion.goals import service as svc
        mem_store.upsert_customer_profile(
            "telegram", "100", {"churn_reason": "太贵"}, now=NOW)
        # 与 TestWinbackLoop._churned 同口径：deadline 在冷却窗外
        created = NOW - (70 + 30) * _DAY
        mem_store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="100", template="retention_expand",
            deadline_days=30,
            params={"last_plan": "autochat-team", "product_id": "chatx"},
            created_by="retention_auto", now=created)
        g = mem_store.find_active_goal(conversation_id=CONV)
        mem_store.update_goal_fields(g["goal_id"], status="expired")
        s = svc.run_winback_scan(mem_store, _wb_cfg(), now=NOW)
        assert s["created"] == 1
        wb = mem_store.find_active_goal(conversation_id=CONV)
        assert wb is not None and wb["created_by"] == "winback_auto"
        note = str((wb.get("params") or {}).get("note") or "")
        assert "太贵" in note and "autochat-entry" in note
        assert "不承诺折扣" in note

    def test_readiness_route_includes_calibration(self):
        client, _ = _build_client(
            auto_create={"enabled": True, "personas": ["su_wan"]})
        r = client.get("/api/goals/readiness")
        assert r.status_code == 200
        d = r.json()
        assert "calibration" in d
        assert "priority" in d["calibration"]
        assert d.get("personas_href", "").startswith("/personas")


# ── P13：运营授权活动（引擎只转述）+ 周审 CLI ─────────────────────────────

def _offer_catalog(**over):
    base = {
        "id": "entry-trial",
        "enabled": True,
        "authorized_by": "victor",
        # NOW(1.8e9) ≈ 2027-01；有效期取远期，测的是护栏不是日历
        "valid_until": "2030-12-31",
        "label_zh": "入门版首月体验价",
        "url": "https://bd2026.cc/order?plan=autochat-entry",
        "plan_key": "autochat-entry",
        "product_id": "chatx",
        "for_churn": ["太贵", "预算紧张"],
    }
    base.update(over)
    return {"site": {"base_url": "https://bd2026.cc", "order_path": "/order"},
            "offers": [base]}


class TestAuthorizedOffers:
    def test_guards_reject_unauthorized_expired_and_offsite(self):
        from src.companion.goals import offers as om
        now = 1_800_000_000.0
        assert len(om.active_offers(_offer_catalog(), now=now)) == 1
        # 未显式 enabled / 无批准人 / 过期 / 站外链接：任一 → 整条忽略
        assert om.active_offers(_offer_catalog(enabled=False), now=now) == []
        assert om.active_offers(_offer_catalog(authorized_by=""), now=now) == []
        assert om.active_offers(
            _offer_catalog(valid_until="2020-01-01"), now=now) == []
        assert om.active_offers(_offer_catalog(valid_until=""), now=now) == []
        assert om.active_offers(
            _offer_catalog(url="https://evil.example/order"), now=now) == []
        # 目录没 site.base_url → 无从判定同域 → 不放行
        assert om.active_offers({"offers": [{"enabled": True}]}, now=now) == []

    def test_pick_matches_churn_and_product(self):
        from src.companion.goals import offers as om
        now = 1_800_000_000.0
        cat = _offer_catalog()
        assert om.pick_offer(cat, churn_reason="太贵", now=now)["id"] == "entry-trial"
        # 复合原因取主因；不相关主因 / 空原因 → None（不硬塞活动）
        assert om.pick_offer(cat, churn_reason="太贵、出了问题", now=now) is not None
        assert om.pick_offer(cat, churn_reason="没用起来", now=now) is None
        assert om.pick_offer(cat, churn_reason="", now=now) is None
        # 活动限定产品与当前主推不符 → 不引用
        assert om.pick_offer(
            cat, churn_reason="太贵", product_id="lingox", now=now) is None

    def test_block_line_only_links_on_order_day(self):
        from src.companion.goals import offers as om
        offer = om.pick_offer(_offer_catalog(), churn_reason="太贵",
                              now=1_800_000_000.0)
        order_line = om.offer_block_line(offer, cta="order")
        assert "https://bd2026.cc/order?plan=autochat-entry" in order_line
        assert "不许加码" in order_line
        cs_line = om.offer_block_line(offer, cta="cs")
        assert "http" not in cs_line and "官方客服" in cs_line
        # soft/roi 档不提活动（提了链接也会被 link_guard 剥 → 空头承诺）
        assert om.offer_block_line(offer, cta="roi") == ""
        assert om.offer_block_line(None, cta="order") == ""

    def test_catalog_block_cites_offer_once(self):
        offer = {"id": "x", "label": "入门首月体验价",
                 "url": "https://bd2026.cc/order?plan=autochat-entry"}
        prods = [{"id": "chatx", "name_zh": "智聊", "pitch_zh": "AI 客服",
                  "plans": [{"key": "autochat-entry", "name_zh": "入门版"}]}]
        block = sc.build_catalog_block(
            prods, push_level="direct",
            site={"base_url": "https://bd2026.cc", "order_path": "/order"},
            cta="order", plan_pref="entry", cta_bias="roi", offer=offer)
        assert block.count("入门首月体验价") == 1
        assert "运营已授权" in block
        # 无活动时行为与 P11 完全一致（不出现活动语义）
        plain = sc.build_catalog_block(
            prods, push_level="direct",
            site={"base_url": "https://bd2026.cc", "order_path": "/order"},
            cta="order", plan_pref="entry", cta_bias="roi")
        assert "授权" not in plain

    def test_service_cites_offer_on_lifecycle_direct_day(self, monkeypatch):
        """留存会话 + 太贵 + 目录有在效活动 → 块里出活动行 + offer_cited 计数。"""
        from src.companion.goals import site_catalog as sc_mod
        from src.companion.goals.service import (
            build_block_for_chat,
            get_configured_store,
        )
        from src.companion.goals.stats import get_goal_stats

        cat = _offer_catalog()
        cat["products"] = [{
            "id": "chatx", "name_zh": "智聊", "pitch_zh": "AI 客服成交引擎",
            "pains": ["客服"],
            "plans": [{"key": "autochat-entry", "name_zh": "入门版"},
                      {"key": "autochat-team", "name_zh": "团队版"}],
        }]
        monkeypatch.setattr(sc_mod, "load_catalog", lambda *a, **k: cat)

        cfg_obj = SimpleNamespace(config=_lc_cfg(), config_path=None)
        store = get_configured_store(cfg_obj.config, None)
        get_goal_stats().reset()
        store.upsert_customer_profile(
            "telegram", "100",
            {"churn_reason": "太贵", "need": "客服回不过来"}, now=NOW)
        store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="100", template="retention_expand",
            params={"product_id": "chatx"},
            deadline_days=30, created_by="retention_auto", now=NOW)

        def _force_direct(store_, cfg_root, goal, **kw):
            g2 = dict(goal)
            g2["status"] = "active"
            g2["milestone_idx"] = 3
            return {"hold": None, "goal": g2,
                    "action": {"action_id": "off-a1", "status": "planned",
                               "push_level": "direct", "intent": "续费收口"}}

        monkeypatch.setattr(
            "src.companion.goals.service.refresh_goal", _force_direct)
        monkeypatch.setattr(
            "src.companion.goals.store.GoalStore.mark_action",
            lambda self, *a, **k: True)
        block = build_block_for_chat(
            cfg_obj, platform="telegram", chat_key="100", account_id="a1",
            conversation_id=CONV, user_context={}, chain="reply",
            inbound_text="还是有点贵", now=NOW + 60)
        assert block and "入门版首月体验价" in block
        assert get_goal_stats().dump()["offer_cited"] == 1
        # 同目标同天第二条消息 → 不再复读活动（价格信息提一次就够），
        # 但目录块本身照常注入
        block2 = build_block_for_chat(
            cfg_obj, platform="telegram", chat_key="100", account_id="a1",
            conversation_id=CONV, user_context={}, chain="reply",
            inbound_text="有没有便宜点的", now=NOW + 120)
        assert block2 and "入门版首月体验价" not in block2
        assert "【可推荐产品" in block2
        assert get_goal_stats().dump()["offer_cited"] == 1
        # 次日窗口重新可提
        block3 = build_block_for_chat(
            cfg_obj, platform="telegram", chat_key="100", account_id="a1",
            conversation_id=CONV, user_context={}, chain="reply",
            inbound_text="再想想", now=NOW + 86400 + 60)
        assert block3 and "入门版首月体验价" in block3

    def test_no_offer_for_non_lifecycle_conversation(self, monkeypatch):
        """获客新客户会话没有 churn_reason → 永远不引用活动。"""
        from src.companion.goals import offers as om
        assert om.pick_offer(_offer_catalog(), churn_reason="") is None

    def test_calibration_reads_offer_state(self):
        from src.companion.goals.calibration import growth_calibration
        rep = {"churn_outcomes": {
            "太贵": {"n": 6, "won": 0, "won_rate": 0.0, "expired": 6}}}
        # 无授权活动 → 提示去 offers 段登记
        cal = growth_calibration(
            readiness={"status": "ready", "checks": {}, "blockers": [],
                       "hints": []},
            report=rep, stats={})
        assert cal["priority"] == "churn_price"
        assert any("offers" in h for h in cal["hints"])
        # 有活动但零引用 → 指向覆盖面/档位排查
        cal2 = growth_calibration(
            readiness={"status": "ready", "blockers": [], "hints": [],
                       "checks": {"offers": {"active": 1, "soonest_id": "e1",
                                             "soonest_days": 2}}},
            report=rep, stats={"offer_cited": 0})
        assert any("一次都没被引用" in h for h in cal2["hints"])
        # 快到期 → 续期提醒
        assert any("天到期" in h for h in cal2["hints"])

    # ── P25/P2：期限调整 × 终态 → 校准建议（结局证据优先且与占比互斥）──────

    @staticmethod
    def _de_rep(sh, un, total_n, ext=None):
        """组 deadline_edits 报表片段（sh/un/ext=队列 dict；total_n=模板终态数）。"""
        zero = {"n": 0, "done": 0, "failed": 0, "expired": 0,
                "cancelled": 0, "done_rate": 0.0}
        return {
            "by_template": {"acquire_and_convert": {"n": total_n}},
            "deadline_edits": {"by_template": {"acquire_and_convert": {
                "edited_n": sh.get("n", 0) + (ext or {}).get("n", 0),
                "shortened": sh, "extended": ext or dict(zero),
                "unedited": un,
            }}},
        }

    @staticmethod
    def _cal(rep):
        from src.companion.goals.calibration import growth_calibration
        # stats 给非零进程计数：绕开「从没触发过」的 traffic 分支噪声
        return growth_calibration(
            readiness={"status": "ready", "checks": {}, "blockers": [],
                       "hints": []},
            report=rep, stats={"auto_created": 1, "injected": {"total": 1}})

    def test_calibration_deadline_share_hint(self):
        # 结局样本不足（organic <5）→ 退占比证据：30%+ 被加急 → 下调建议
        rep = self._de_rep(
            sh={"n": 4, "done": 1, "failed": 1, "expired": 0,
                "cancelled": 2, "done_rate": 0.5},
            un={"n": 6, "done": 2, "failed": 1, "expired": 0,
                "cancelled": 3, "done_rate": 0.667},
            total_n=10)
        cal = self._cal(rep)
        assert any("被人工加急" in h and "default_days" in h
                   for h in cal["hints"])

    def test_calibration_deadline_outcome_worse_beats_share(self):
        # 两队列 organic ≥5 且加急差 15pt+ → 出「反而更低」并抑制下调建议
        rep = self._de_rep(
            sh={"n": 8, "done": 2, "failed": 3, "expired": 3,
                "cancelled": 0, "done_rate": 0.25},
            un={"n": 12, "done": 7, "failed": 3, "expired": 2,
                "cancelled": 0, "done_rate": 0.583},
            total_n=20)
        cal = self._cal(rep)
        assert any("达成率反而更低" in h for h in cal["hints"])
        assert not any("考虑下调模板 default_days" in h for h in cal["hints"])

    def test_calibration_deadline_outcome_better(self):
        # 加急达成率显著更高 → 「默认节奏偏保守」实证建议
        rep = self._de_rep(
            sh={"n": 10, "done": 8, "failed": 1, "expired": 1,
                "cancelled": 0, "done_rate": 0.8},
            un={"n": 10, "done": 5, "failed": 3, "expired": 2,
                "cancelled": 0, "done_rate": 0.5},
            total_n=20)
        cal = self._cal(rep)
        assert any("达成率更高" in h and "default_days" in h
                   for h in cal["hints"])

    def test_calibration_deadline_extend_hint(self):
        # 延期占比 ≥30% → 默认节奏偏快提示
        rep = self._de_rep(
            sh={"n": 0, "done": 0, "failed": 0, "expired": 0,
                "cancelled": 0, "done_rate": 0.0},
            un={"n": 6, "done": 3, "failed": 2, "expired": 1,
                "cancelled": 0, "done_rate": 0.5},
            total_n=10,
            ext={"n": 4, "done": 2, "failed": 1, "expired": 1,
                 "cancelled": 0, "done_rate": 0.5})
        cal = self._cal(rep)
        assert any("被人工延期" in h and "上调" in h for h in cal["hints"])

    def test_calibration_deadline_small_sample_silent(self):
        # 模板终态 <8 → 占比证据不出手（小样本读数只看不判）
        rep = self._de_rep(
            sh={"n": 2, "done": 1, "failed": 1, "expired": 0,
                "cancelled": 0, "done_rate": 0.5},
            un={"n": 3, "done": 2, "failed": 1, "expired": 0,
                "cancelled": 0, "done_rate": 0.667},
            total_n=5)
        cal = self._cal(rep)
        assert not any(("加急" in h or "延期" in h) for h in cal["hints"])

    def test_bind_hint_shortens_long_account_ids(self):
        """无昵称账号 label＝一长串平台 id；hint/ops 用掐头留尾短名（可人工核对）。"""
        from src.companion.goals.calibration import growth_calibration
        from src.companion.goals.readiness import _short_label
        long_id = "Uk4bhjJ4-HC2jhfuF0CwF4LlyIJ_WgLnScdIt1OIkQOg"
        short = _short_label(long_id, platform="messenger")
        assert short.startswith("messenger:Uk4bhj") and short.endswith("kQOg")
        assert len(short) < 30
        assert _short_label("小号A", platform="telegram") == "telegram:小号A"
        cal = growth_calibration(readiness={
            "status": "waiting_bind", "blockers": ["no_account_bound"],
            "hints": [],
            "checks": {"personas_allowlist": ["su_wan"],
                       "bind_candidates": [{"short": short,
                                            "account_id": long_id,
                                            "suggest": "su_wan"}]},
        })
        assert short in cal["hints"][0] and long_id not in cal["hints"][0]

    def test_readiness_exposes_offers_summary(self, monkeypatch):
        from src.companion.goals import readiness as rd
        from src.companion.goals import site_catalog as sc_mod
        monkeypatch.setattr(
            sc_mod, "load_catalog", lambda *a, **k: _offer_catalog())
        summary = rd._offers_summary({})
        assert summary["active"] == 1 and summary["soonest_id"] == "entry-trial"
        assert summary["soonest_days"] >= 0

    def test_service_stashes_offer_allowlist_for_guard(self, monkeypatch):
        """注入侧把「授权活动 + 目录卖点」暂存进 _goal_cta，供出站守卫当白名单。"""
        from src.companion.goals import site_catalog as sc_mod
        from src.companion.goals.service import (
            build_block_for_chat,
            get_configured_store,
        )

        cat = _offer_catalog()
        cat["products"] = [{
            "id": "chatx", "name_zh": "智聊", "pitch_zh": "新客7天免费试用",
            "pains": ["客服"],
            "plans": [{"key": "autochat-entry", "name_zh": "入门版"}],
        }]
        monkeypatch.setattr(sc_mod, "load_catalog", lambda *a, **k: cat)
        cfg_obj = SimpleNamespace(config=_lc_cfg(), config_path=None)
        store = get_configured_store(cfg_obj.config, None)
        store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="200", template="retention_expand",
            params={"product_id": "chatx"},
            deadline_days=30, created_by="retention_auto", now=NOW)
        uc: dict = {}
        build_block_for_chat(
            cfg_obj, platform="telegram", chat_key="200", account_id="a1",
            conversation_id=CONV, user_context=uc, chain="reply",
            inbound_text="在吗", now=NOW + 60)
        allow = (uc.get("_goal_cta") or {}).get("offer_texts") or []
        assert any("首月体验价" in x for x in allow)
        assert any("7天免费试用" in x for x in allow)
        # 白名单生效：目录里真写的试用不被守卫剥
        from src.companion.goals.offer_guard import sanitize_offer_claims
        keep = "新客有7天免费试用，先跑跑看效果"
        assert sanitize_offer_claims(keep, allowed_texts=allow)[1] == 0

    def test_proactive_goal_opener_guarded(self):
        """目标桥开场也过优惠守卫（文案/配图配文/语音稿同源一处收口）。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "src" / "companion"
               / "proactive_topic.py").read_text(encoding="utf-8")
        assert "def _guard_offer_text(" in src
        assert 'if (plan or {}).get("_goal_action_id"):' in src
        assert "text = _guard_offer_text(text, plan)" in src
        # 剥离位置必须在三条发送分支之前（照片/语音/文本都取同一个 text）
        i_guard = src.index("text = _guard_offer_text(text, plan)")
        assert i_guard < src.index("if _photo_plan and await _try_send_photo(")
        assert i_guard < src.index("if await _try_send_voice(plan, text):")

    def test_traction_claims_stripped_and_boundaries(self):
        """P15：带数字的社会证明（编客户数）必剥；汉字数量词/无数字/百分比刻意不抓。"""
        from src.companion.goals.offer_guard import (
            find_offer_claims,
            sanitize_offer_claims,
        )
        strip = [
            "我们已经有500多家商家在用了",
            "已服务2000+客户，口碑都不错",
            "It's trusted by 800+ businesses worldwide",
            "帮了30个老板搞定客服",
        ]
        for txt in strip:
            out, n, hits = sanitize_offer_claims(txt)
            assert n >= 1, txt
            assert hits[0] not in out
        assert find_offer_claims("已经有500家客户在用")[0][0] == "traction"
        keep = [
            "用的人挺多的，反馈都还不错",          # 无数字
            "我已经帮三个朋友装好了",              # 汉字数量词（低置信边界）
            "超过30%的客户会先问价格",             # 百分比统计不是客户数
            "我服务了2年多的老客户",               # 年限不是客户数
        ]
        for txt in keep:
            assert sanitize_offer_claims(txt)[1] == 0, txt
        # 运营登记进 claims.texts 的真实数据放行
        allowed = ["已服务 2000+ 客户（官网案例页可查）"]
        assert sanitize_offer_claims(
            "我们已服务2000+客户", allowed_texts=allowed)[1] == 0

    def test_strip_samples_and_source_observability(self):
        """P15：剥离样本 Top-N + 链路归属进 stats（运营看「在编什么」）。"""
        from src.companion.goals.stats import get_goal_stats
        st = get_goal_stats()
        st.reset()
        st.record_offer_claim_stripped(1, samples=["打8折"], source="chat")
        st.record_offer_claim_stripped(2, samples=["打8折", "立减500"],
                                       source="proactive")
        d = st.dump()
        assert d["offer_claims_stripped"] == 3
        assert d["offer_strip_samples"]["打8折"] == 2
        assert d["offer_strip_samples"]["立减500"] == 1
        assert d["offer_strip_by_source"] == {"chat": 1, "proactive": 2}
        prom = st.dump_prom()
        assert 'goals_offer_strip_by_source_total{source="chat"} 1' in prom
        assert 'goals_offer_strip_by_source_total{source="proactive"} 2' in prom
        # distinct 上限：灌 40 个不同片段只留 24 个桶
        for i in range(40):
            st.record_offer_claim_stripped(1, samples=[f"frag{i}"])
        assert len(st.dump()["offer_strip_samples"]) <= 24 + 2
        # 接线：skill_manager 守卫真剥时带 samples/source
        import logging
        from src.skills.skill_manager import _guard_offer_claims
        st.reset()
        out = _guard_offer_claims(
            "我还能给你打个8折", {"offer_texts": []},
            cfg_root={"companion": {"goals": {}}},
            logger=logging.getLogger("t"))
        assert "8折" not in out
        d2 = st.dump()
        assert d2["offer_strip_by_source"].get("chat") == 1
        assert any("8折" in k for k in d2["offer_strip_samples"])

    def test_readiness_first_run_watch(self, monkeypatch):
        """P15：已绑定但从没诞生过自动目标 → 库口径 hint（跨重启稳）。"""
        from src.companion.goals import readiness as rd
        from src.companion.goals.calibration import growth_calibration
        from src.companion.goals.service import get_configured_store

        cfg = {"companion": {"goals": {
            "enabled": True, "db_path": ":memory:",
            "auto_create": {"enabled": True, "personas": ["su_wan"]},
        }}}
        monkeypatch.setattr(rd, "_persona_exists", lambda pid: True)
        monkeypatch.setattr(rd, "_iter_account_personas", lambda c: [
            {"platform": "line", "account_id": "U1", "persona_id": "su_wan",
             "status": "online", "label": "U1"}])
        r = rd.growth_readiness(cfg)
        assert r["checks"]["bound_count"] == 1
        assert r["checks"]["auto_goals_ever"] == 0
        assert any("自动获客目标" in h for h in r["hints"])
        # 校准同口径：库说从没触发 → traffic 提示（无视进程计数）
        cal = growth_calibration(readiness=r, stats={"auto_created": 5})
        assert cal["priority"] == "traffic"
        assert any("自动获客目标" in h for h in cal["hints"])
        # 首个自动目标诞生后：hint 消失、计数可见
        store = get_configured_store(cfg, None)
        store.create_goal(
            conversation_id="line:U1:900", platform="line", account_id="U1",
            chat_key="900", template="acquire_and_convert",
            created_by="auto_create", now=NOW)
        r2 = rd.growth_readiness(cfg)
        assert r2["checks"]["auto_goals_ever"] == 1
        assert not any("还没有自动获客目标" in h for h in r2["hints"])
        cal2 = growth_calibration(readiness=r2, stats={})
        assert cal2["priority"] != "traffic"

    def test_ops_card_one_click_rebind_wired(self):
        """ops 卡「立即换绑」按钮 → 复用人设页已有的账号换绑路由（写权限+审计）。"""
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        html = (root / "src" / "web" / "templates" / "ops_overview.html").read_text(
            encoding="utf-8")
        assert "function goalBindNow(" in html
        assert "goalBindNow(\\'" in html          # 候选行真挂了按钮
        assert "'/api/persona/account-persona'" in html
        routes = (root / "src" / "web" / "routes" / "persona_routes.py").read_text(
            encoding="utf-8")
        assert '"/api/persona/account-persona"' in routes
        # 该路由必须仍带写权限校验 + 审计（换绑是可追责操作）
        assert "_check_write_role(request)" in routes
        assert "account_persona_set" in routes

    def test_offer_guard_no_false_alarm_on_benign_chat(self):
        """日常闲聊/正常带货话术零命中——守卫宁可漏拦也不能改人话。"""
        from src.companion.goals.offer_guard import sanitize_offer_claims
        benign = [
            "今天天气好好呀，你那边下雨了吗",
            "官网自助下单就行，链接我发你 https://bd2026.cc/order",
            "入门版一个月几十块，团队版按坐席算",
            "我一个月大概接三四十个咨询",
            "这事挺折腾的，你别急",
            "免费的东西我一般不太信，你说呢",
            "先用起来看看效果，划不划算你自己算",
            "价格在官网都写着，我给你发试算器",
        ]
        for txt in benign:
            out, n, hits = sanitize_offer_claims(txt)
            assert n == 0 and out == txt, (txt, hits)

    def test_offer_guard_strips_invented_discounts(self):
        """P14：LLM 自己编的折扣/券码/赠送 → 剥掉那一小句（其余原样留）。"""
        from src.companion.goals.offer_guard import sanitize_offer_claims
        cases = [
            "这个我可以给你打个8折，你看行不行",
            "报你个内部价，立减200，今天下单就行",
            "优惠码 SUWAN50 报我名字就有",
            "先免费用一个月，喜欢再付钱",
            "我帮你申请 30% off 好不好",
        ]
        for txt in cases:
            out, n, hits = sanitize_offer_claims(txt)
            assert n >= 1, txt
            assert hits and hits[0]
            # 承诺片段不得留在输出里
            assert hits[0] not in out, txt

    def test_offer_guard_keeps_refusals_and_authorized(self):
        """婉拒语气 + 授权活动原文 → 放行（剥了反而毁掉合规回答）。"""
        from src.companion.goals.offer_guard import sanitize_offer_claims
        keep = [
            "我这边暂时没有折扣哦，官网统一价",
            "打折这事我说了不算，得问官方客服",
            "价格我不能私自改，不提供额外优惠",
        ]
        for txt in keep:
            out, n, _ = sanitize_offer_claims(txt)
            assert n == 0 and out == txt, txt
        # 授权活动文案里的表述 → 放行
        allowed = ["入门版首月体验价 立减200"]
        out, n, _ = sanitize_offer_claims(
            "官网现在有活动：立减200，你要不要看看",
            allowed_texts=allowed)
        assert n == 0
        # 同一会话里编一个更大的数字仍然被剥
        out2, n2, _ = sanitize_offer_claims(
            "我给你争取立减500吧", allowed_texts=allowed)
        assert n2 == 1 and "立减500" not in out2

    def test_freebie_days_parses_qty_and_unit(self):
        """免费时长解析：数字/中文数量词 × 天/周/月 → 天数（认不出 → None）。"""
        from src.companion.goals.offer_guard import freebie_days
        assert freebie_days("免费试用 7 天") == 7
        assert freebie_days("免费试用7天") == 7
        assert freebie_days("免费用一周") == 7
        assert freebie_days("送你两周") == 14
        assert freebie_days("免费用一个月") == 30
        assert freebie_days("送你半个月") == 15
        assert freebie_days("免费用十天") == 10
        assert freebie_days("免费用十五天") == 15
        assert freebie_days("free 7 days") == 7
        assert freebie_days("free 2 weeks") == 14
        # 认不出的一律 None → 调用方按未授权处理（宁可剥不可错放）
        assert freebie_days("免费送你") is None
        assert freebie_days("打个8折") is None

    def test_offer_guard_authorized_trial_days_pass(self):
        """授权免费时长 → 各种措辞都放行；未授权时长照剥。

        2026-07-28 实录假阳：客户端真有「注册领 7 天完整版」，但子串白名单要求
        逐字命中 → 「免费领 7 天」放行而「免费试用 7 天」被剥，甚至整条换成
        「我这边不能私自给折扣」＝对客户否认一个真实存在的试用，且正好发生在
        「能不能先试试」这个成交必经异议上。授权的是事实（天数）不是措辞。
        """
        from src.companion.goals.offer_guard import sanitize_offer_claims
        # 7 天已授权 → 措辞随便怎么变都不该被剥
        for txt in [
            "可以先免费试用 7 天，2.5 万字符够你跑一轮",
            "免费试用7天",
            "免费用 7 天完整版试试",
            "送你 7 天完整版",
            "注册就能免费领 7 天完整版",
            "免费用一周看看效果",          # 一周 ≡ 7 天，同一事实
            "you can claim a free 7 days full version after signing up",
        ]:
            out, n, hits = sanitize_offer_claims(
                txt, allowed_free_days=[7])
            assert n == 0 and out == txt, (txt, hits)
        # 未授权时长仍是编的 → 照剥
        for txt in [
            "免费试用 14 天，不满意不用付钱",
            "免费用一个月",
            "送你半个月",
            "免费用两周",
            "free 14 days trial for you",
        ]:
            out, n, hits = sanitize_offer_claims(txt, allowed_free_days=[7])
            assert n >= 1, txt
            assert hits[0] not in out, txt
        # 没登记授权时长 → 回到全拦（旧行为，不因新参数放松）
        assert sanitize_offer_claims("免费试用 7 天")[1] == 1
        # 时长授权只放行赠送类，不误开折扣/券码的口子
        for txt in ["给你打个8折", "优惠码 SUWAN50 直接减", "立减200"]:
            assert sanitize_offer_claims(txt, allowed_free_days=[7])[1] >= 1, txt

    def test_catalog_claims_feed_guard_allowlist(self):
        """目录 claims 段 → 白名单文本 + 授权时长（运营登记事实的单一入口）。"""
        from src.companion.goals.offers import (
            allowlist_texts,
            authorized_free_days,
        )
        cat = {
            "products": [{"id": "chatx", "pitch_zh": "AI 客服成交引擎"}],
            "claims": {"free_days": [7],
                       "texts": ["注册领 7 天完整版 · 2.5 万字符"]},
        }
        assert authorized_free_days(cat) == [7]
        texts = allowlist_texts(cat)
        assert any("2.5 万字符" in t for t in texts)
        assert any("AI 客服成交引擎" in t for t in texts)
        # 单值/脏值/缺段都不许抛（守卫层挂了不能拖垮聊天）
        assert authorized_free_days({"claims": {"free_days": 7}}) == [7]
        assert authorized_free_days({"claims": {"free_days": ["x", 0, -3]}}) == []
        assert authorized_free_days({}) == []
        assert authorized_free_days(None) == []
        assert allowlist_texts({"claims": "坏形状"}) == []

    def test_production_catalog_authorizes_only_real_trial(self):
        """出厂目录只授权客户端真给得出的事实；按天试用已不存在，不得再授权。

        2026-08-11 免费档升级：按天试用（旧 7 天档）→ 100 万字符 · 无期限。
        目录 free_days 必须清空——否则苏婉还在承诺一个已下线的「免费试用 7 天」
        （比守卫误剥更贵的事故：客户装完发现根本没有按天试用）。14 天试用属
        AvatarHub 线，同样不得混入。字符量类事实（「免费 100 万字符」）不含
        天/周/月单位，守卫的 freebie 模式天然不抓，无需授权也不会被剥。
        """
        from pathlib import Path

        import yaml

        from src.companion.goals.offer_guard import sanitize_offer_claims
        from src.companion.goals.offers import (
            allowlist_texts,
            authorized_free_days,
        )
        cat = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "config"
             / "site_catalog.yaml").read_text(encoding="utf-8")) or {}
        days = authorized_free_days(cat)
        assert days == [], "免费档已无按天试用：free_days 必须留空，别再授权 7/14 天"
        allow = allowlist_texts(cat)
        assert any("100 万字符" in t for t in allow), "新免费档事实必须登记进 texts"
        kw = {"allowed_texts": allow, "allowed_free_days": days}
        # 旧 7 天话术现在是编的 → 必须被剥（正是本次升级要防的穿帮）
        assert sanitize_offer_claims("可以先免费试用 7 天", **kw)[1] == 1
        assert sanitize_offer_claims("免费试用 14 天", **kw)[1] == 1
        assert sanitize_offer_claims("给你打个8折", **kw)[1] == 1
        # 字符量类事实不触发时长/折扣模式，原样放行
        assert sanitize_offer_claims("注册就能免费领 100 万字符", **kw)[1] == 0

    def test_offer_guard_replaces_all_promise_reply(self):
        """整条就是一句承诺 → 换合规话术（回退原文＝没守，正是事故形态）。"""
        from src.companion.goals.offer_guard import (
            compliant_fallback,
            sanitize_offer_claims,
        )
        out, n, _ = sanitize_offer_claims("给你打个8折")
        assert n == 1 and "8折" not in out
        assert out == compliant_fallback("给你打个8折")
        # 英文会话拿英文兜底
        out_en, n_en, _ = sanitize_offer_claims("I can give you 30% off")
        assert n_en == 1 and "%" not in out_en and "official site" in out_en
        # 多句时只掉命中句，其余保留且不留悬空标点
        out2, n2, _ = sanitize_offer_claims(
            "这套我自己也在用，可以给你打个8折，效果真的不错")
        assert n2 == 1
        assert "我自己也在用" in out2 and "效果真的不错" in out2
        assert "8折" not in out2 and "，，" not in out2
        # 纯闲聊零命中零改写
        from src.companion.goals.offer_guard import find_offer_claims
        assert sanitize_offer_claims("今天好累啊")[1] == 0
        assert find_offer_claims("今天好累啊") == []
        assert find_offer_claims("给你打个8折")[0][0] == "discount"

    def test_skill_manager_wires_offer_guard(self):
        """接线：出站链路读 _goal_cta 时先过优惠守卫，再过链接守卫 + 计数。"""
        import logging

        from src.companion.goals.stats import get_goal_stats
        from src.skills.skill_manager import SkillManager

        get_goal_stats().reset()
        uc = {"_goal_cta": {"cta": "order",
                            "base_url": "https://bd2026.cc",
                            "order_path": "/order",
                            "offer_texts": []}}
        fake_self = SimpleNamespace(
            config=SimpleNamespace(config={"companion": {"goals": {}}}),
            logger=logging.getLogger("test-offer-guard"))
        out = SkillManager._apply_goal_link_guard(
            fake_self,
            "官网自助下单：https://bd2026.cc/order?plan=autochat-entry，"
            "我还能给你打个8折", uc)
        assert "8折" not in out
        assert "bd2026.cc/order" in out          # order 档链接照常放行
        assert get_goal_stats().dump()["offer_claims_stripped"] == 1
        assert "_goal_cta" not in uc              # 读后即焚不变

    def test_skill_manager_passes_authorized_free_days(self):
        """接线：暂存的 offer_free_days 必须透传给守卫，否则真试用照样被剥。"""
        import logging

        from src.companion.goals.stats import get_goal_stats
        from src.skills.skill_manager import SkillManager

        get_goal_stats().reset()
        fake_self = SimpleNamespace(
            config=SimpleNamespace(config={"companion": {"goals": {}}}),
            logger=logging.getLogger("test-offer-guard-days"))
        uc = {"_goal_cta": {"cta": "cs", "base_url": "https://bd2026.cc",
                            "order_path": "/order", "offer_texts": [],
                            "offer_free_days": [7]}}
        out = SkillManager._apply_goal_link_guard(
            fake_self, "可以先免费试用 7 天，2.5 万字符够跑一轮", uc)
        assert "免费试用 7 天" in out
        assert get_goal_stats().dump()["offer_claims_stripped"] == 0
        # 同一档位下未授权时长仍被剥
        uc2 = {"_goal_cta": dict(uc.get("_goal_cta") or {},
                                 offer_free_days=[7])}
        out2 = SkillManager._apply_goal_link_guard(
            fake_self, "先免费用一个月，喜欢再付钱", uc2)
        assert "免费用一个月" not in out2
        assert get_goal_stats().dump()["offer_claims_stripped"] == 1

    def test_service_stashes_authorized_free_days(self, monkeypatch):
        """注入侧把目录 claims.free_days 一并暂存（守卫读的是 _goal_cta）。"""
        from src.companion.goals import site_catalog as sc_mod
        from src.companion.goals.service import (
            build_block_for_chat,
            get_configured_store,
        )

        cat = _offer_catalog()
        cat["products"] = [{
            "id": "chatx", "name_zh": "智聊", "pitch_zh": "AI 客服成交引擎",
            "pains": ["客服"],
            "plans": [{"key": "autochat-entry", "name_zh": "入门版"}],
        }]
        cat["claims"] = {"free_days": [7], "texts": ["注册领 7 天完整版"]}
        monkeypatch.setattr(sc_mod, "load_catalog", lambda *a, **k: cat)
        cfg_obj = SimpleNamespace(config=_lc_cfg(), config_path=None)
        store = get_configured_store(cfg_obj.config, None)
        store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="201", template="retention_expand",
            params={"product_id": "chatx"},
            deadline_days=30, created_by="retention_auto", now=NOW)
        uc: dict = {}
        build_block_for_chat(
            cfg_obj, platform="telegram", chat_key="201", account_id="a1",
            conversation_id=CONV, user_context=uc, chain="reply",
            inbound_text="在吗", now=NOW + 60)
        cta = uc.get("_goal_cta") or {}
        assert cta.get("offer_free_days") == [7]
        assert any("注册领 7 天完整版" in x
                   for x in (cta.get("offer_texts") or []))

    def test_proactive_opener_passes_free_days(self):
        """主动开场守卫同口径带上授权时长（两条出站链不许一宽一严）。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "src" / "companion"
               / "proactive_topic.py").read_text(encoding="utf-8")
        i = src.index("def _guard_offer_text(")
        body = src[i:i + 1600]
        assert "allowed_free_days=offers_mod.authorized_free_days(" in body
        assert "allowed_texts=offers_mod.allowlist_texts(" in body

    def test_review_cli_renders_and_trend_row(self):
        from scripts.growth_review import render, trend_row
        snap = {
            "base": "http://127.0.0.1:18799", "days": 30, "ts": NOW,
            "readiness": {
                "status": "ready", "blockers": [],
                "checks": {"bound_count": 1,
                           "offers": {"active": 1, "soonest_id": "e1",
                                      "soonest_days": 9}},
                "calibration": {"priority": "churn_price", "focus": "太贵",
                                "hints": ["把活动登记到 offers 段"]},
            },
            "report": {
                "totals": {"n": 4, "done": 1, "done_rate": 0.25,
                           "avg_days_to_done": 3.2},
                "churn_outcomes": {
                    "太贵": {"n": 3, "won": 1, "won_rate": 0.333}},
            },
        }
        text = render(snap)
        assert "churn_price" in text and "太贵" in text and "授权活动" in text
        row = trend_row(snap)
        assert row["priority"] == "churn_price" and row["n"] == 4
        assert row["churn"]["太贵"]["won"] == 1
        # 老实例无 stats 段 → funnel 空 dict，渲染不出漏斗行（向后兼容）
        assert row["funnel"] == {} and "漏斗（自" not in text
        # 实例没起时如实报错，不编数字
        err = render({"base": "x", "days": 7, "ts": NOW,
                      "readiness": {"_error": "conn refused"},
                      "report": {"_error": "conn refused"}})
        assert "读取失败" in err

    def test_review_funnel_section_and_trend_funnel(self):
        """P16：readiness.stats → 周审漏斗行 + 出站纪律行 + 趋势 funnel 段。"""
        from scripts.growth_review import render, trend_row
        snap = {
            "base": "http://127.0.0.1:18799", "days": 7, "ts": NOW,
            "readiness": {
                "status": "ready", "blockers": [],
                "checks": {"bound_count": 1},
                "calibration": {"priority": "healthy", "hints": []},
                "stats": {
                    "since": NOW - 3600,
                    "auto_created": 13,
                    "beats": {"planned": 9, "hold_emotion": 1,
                              "hold_silent": 2},
                    "injected": {"draft": 4, "reply": 5, "proactive": 1,
                                 "total": 10},
                    "catalog_injected": 3,
                    "profile_captured": 6,
                    "link_stripped": 1,
                    "offer_claims_stripped": 2,
                    "offer_strip_samples": {"打8折": 2},
                    "offer_strip_by_persona": {"su_wan": 2},
                    "offer_cited": 1,
                },
            },
            "report": {"totals": {"n": 0}},
        }
        text = render(snap)
        assert "自动建 13" in text and "拍 9" in text and "注入 10" in text
        assert "画像槽 6" in text
        assert "折扣守卫剥离 2" in text and "打8折×2" in text
        assert "su_wan×2" in text and "授权活动引用 1" in text
        row = trend_row(snap)
        assert row["funnel"]["auto_created"] == 13
        assert row["funnel"]["offer_claims_stripped"] == 2
        assert row["stats_since"] == NOW - 3600

    def test_review_p23_winrate_and_instruction_funnel(self):
        """P23：win-rate 表（含小样本标注）+ 指令拟稿/画像填充漏斗 + 抽检样本。"""
        from scripts.growth_review import render, trend_row
        snap = {
            "base": "http://127.0.0.1:18799", "days": 7, "ts": NOW,
            "readiness": {"status": "ready", "blockers": [],
                          "checks": {"bound_count": 1},
                          "calibration": {"priority": "healthy", "hints": []}},
            "report": {
                "totals": {"n": 8, "done": 5, "done_rate": 0.625,
                           "won": 3, "won_rate": 0.375,
                           "avg_days_to_done": 4.0},
                "by_template": {
                    "acquire_and_convert": {
                        "n": 6, "done": 4, "failed": 1, "expired": 1,
                        "cancelled": 0, "done_rate": 0.667,
                        "won": 3, "won_rate": 0.5},
                    "winback": {
                        "n": 2, "done": 1, "failed": 1, "expired": 0,
                        "cancelled": 0, "done_rate": 0.5,
                        "won": 0, "won_rate": 0.0},
                },
                "drive_draft": {"total": 7,
                                "by_source": {"beat": 4, "slot": 2, "hero": 1}},
                "profile_fills": {"total": 5,
                                  "by_src": {"agent": 3, "auto": 2},
                                  "by_track": {"relation": 3, "bant": 2}},
            },
            "samples": {"ok": True, "n": 1, "samples": [{
                "ts": NOW - 60, "source": "beat", "mode": "reply",
                "instruction": "今日工作意图：摸痛点",
                "reply": "老板最近店里忙不忙呀？"}]},
        }
        text = render(snap)
        assert "won=3" in text and "won_rate=38%" in text
        assert "acquire_and_convert" in text and "won_rate=50%" in text
        assert "⚠样本不足" in text            # winback organic=2 < 5
        assert "指令生成：7" in text and "beat×4" in text
        assert "画像槽填充：5" in text and "bant×2" in text
        assert "指令遵循抽检" in text and "摸痛点" in text
        row = trend_row(snap)
        assert row["won"] == 3 and row["drive_draft"] == 7
        assert row["profile_fills"] == 5

    def test_review_p23_zero_usage_judgment(self):
        """指令拟稿 0 使用要给判词（新链路无人用=发现性问题，不是没数据）；
        有拟稿但 bant 零填充也要点名（问了没采到）。"""
        from scripts.growth_review import render
        base = {
            "base": "http://127.0.0.1:18799", "days": 7, "ts": NOW,
            "readiness": {"status": "ready", "blockers": [],
                          "checks": {"bound_count": 1},
                          "calibration": {"priority": "healthy", "hints": []}},
        }
        snap0 = dict(base, report={
            "totals": {"n": 0},
            "drive_draft": {"total": 0, "by_source": {}},
            "profile_fills": {"total": 0, "by_src": {}, "by_track": {}}})
        text0 = render(snap0)
        assert "指令生成：0" in text0 and "查坐席是否知道" in text0
        snap1 = dict(base, report={
            "totals": {"n": 0},
            "drive_draft": {"total": 3, "by_source": {"slot": 3}},
            "profile_fills": {"total": 2, "by_src": {"agent": 2},
                              "by_track": {"relation": 2}}})
        text1 = render(snap1)
        assert "bant 轨零填充" in text1

    def test_readiness_route_exposes_stats(self, monkeypatch):
        """路由契约：/api/goals/readiness 响应带 stats 段（周审 CLI 的数据源）。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "src" / "web" / "routes"
               / "goal_routes.py").read_text(encoding="utf-8")
        assert 'snap["stats"] = stats_dump' in src
        # stats 在 calibration 之后挂上（同一份 dump，两处不重复取）
        assert src.index("growth_calibration(") < src.index(
            'snap["stats"] = stats_dump')

    def test_strip_persona_attribution_wired(self):
        """P16：_goal_cta 带 persona_id → 守卫按人设分桶计数。"""
        import logging

        from src.companion.goals.stats import get_goal_stats
        from src.skills.skill_manager import _guard_offer_claims

        st = get_goal_stats()
        st.reset()
        out = _guard_offer_claims(
            "我还能给你打个8折",
            {"offer_texts": [], "persona_id": "su_wan"},
            cfg_root={"companion": {"goals": {}}},
            logger=logging.getLogger("t"))
        assert "8折" not in out
        d = st.dump()
        assert d["offer_strip_by_persona"] == {"su_wan": 1}
        # 人设桶有 cap：灌 30 个不同人设只留 16 个桶
        for i in range(30):
            st.record_offer_claim_stripped(1, persona=f"p{i}")
        assert len(st.dump()["offer_strip_by_persona"]) <= 17

    def test_service_stash_carries_persona(self, monkeypatch):
        """注入侧暂存带账号生效人设（best-effort；解析失败留空不阻断）。"""
        from src.companion.goals import service as svc_mod
        from src.companion.goals import site_catalog as sc_mod
        from src.companion.goals.service import (
            build_block_for_chat,
            get_configured_store,
        )

        cat = _offer_catalog()
        cat["products"] = [{
            "id": "chatx", "name_zh": "智聊", "pitch_zh": "AI 客服",
            "pains": ["客服"],
            "plans": [{"key": "autochat-entry", "name_zh": "入门版"}],
        }]
        monkeypatch.setattr(sc_mod, "load_catalog", lambda *a, **k: cat)
        monkeypatch.setattr(
            svc_mod, "_account_persona", lambda cfg, p, a: "su_wan")
        cfg_obj = SimpleNamespace(config=_lc_cfg(), config_path=None)
        store = get_configured_store(cfg_obj.config, None)
        store.create_goal(
            conversation_id=CONV, platform="telegram", account_id="a1",
            chat_key="300", template="retention_expand",
            params={"product_id": "chatx"},
            deadline_days=30, created_by="retention_auto", now=NOW)
        uc: dict = {}
        build_block_for_chat(
            cfg_obj, platform="telegram", chat_key="300", account_id="a1",
            conversation_id=CONV, user_context=uc, chain="reply",
            inbound_text="在吗", now=NOW + 60)
        assert (uc.get("_goal_cta") or {}).get("persona_id") == "su_wan"
