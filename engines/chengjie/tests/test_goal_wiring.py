"""营销目标接线门禁：skill_manager 双链注入 / ai_client 消费 / proactive 桥挂点 /
metrics 并入。轻量绑定（免全量 init，与 test_bazi_wiring 同模式），全部离线。"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest  # noqa: F401  （asyncio_mode=auto 跑本文件的 async 用例）

from src.companion.goals.store import get_goal_store, reset_goal_store

_SMcls = __import__(
    "src.skills.skill_manager", fromlist=["SkillManager"]
).SkillManager

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


class _SM:
    _inject_goal_context = _SMcls._inject_goal_context

    def __init__(self, *, goals_cfg=None):
        comp = {}
        if goals_cfg is not None:
            comp["goals"] = goals_cfg
        self.config = SimpleNamespace(
            config={"companion": comp}, config_path=None)
        self.logger = logging.getLogger("test_goal_wiring")


_ON = {"enabled": True, "db_path": ":memory:"}


def _seed_goal(autonomy: str = "suggest"):
    """向单例 store 建一条 telegram/c1 的活跃目标。"""
    store = get_goal_store(":memory:")
    goal = store.create_goal(
        conversation_id="telegram:default:c1", platform="telegram",
        account_id="default", chat_key="c1", template="relationship_intimacy",
        params={"target_score": 55}, autonomy=autonomy, deadline_days=30)
    assert goal is not None
    return store, goal


def setup_function(_fn):
    reset_goal_store()


def teardown_function(_fn):
    reset_goal_store()


# ── skill_manager 注入 ────────────────────────────────────────────────────────

def test_inject_disabled_noop_and_clears_stale():
    sm = _SM(goals_cfg={"enabled": False})
    ctx = {"_goal_block": "残留"}
    sm._inject_goal_context(ctx, platform="telegram", chat_key="c1")
    assert "_goal_block" not in ctx


def test_inject_active_goal_gets_block():
    _seed_goal()
    sm = _SM(goals_cfg=_ON)
    ctx = {}
    sm._inject_goal_context(ctx, platform="telegram", chat_key="c1",
                            conversation_id="telegram:default:c1")
    blk = ctx.get("_goal_block") or ""
    assert "【工作目标】" in blk
    assert "【推进纪律】" in blk


def test_inject_no_goal_noop():
    sm = _SM(goals_cfg=_ON)
    ctx = {}
    sm._inject_goal_context(ctx, platform="telegram", chat_key="nobody")
    assert "_goal_block" not in ctx


def test_inject_observe_autonomy_silent():
    _seed_goal(autonomy="observe")
    sm = _SM(goals_cfg=_ON)
    ctx = {}
    sm._inject_goal_context(ctx, platform="telegram", chat_key="c1")
    assert "_goal_block" not in ctx


def test_inject_negative_emotion_holds():
    _seed_goal()
    sm = _SM(goals_cfg=_ON)
    ctx = {"user_emotion_hint": "sad"}
    sm._inject_goal_context(ctx, platform="telegram", chat_key="c1")
    assert "_goal_block" not in ctx


def test_inject_empty_platform_defaults_telegram():
    """A 线 process_message 里 user_context.platform 可能为空 → 按 telegram 兜底命中。"""
    _seed_goal()
    sm = _SM(goals_cfg=_ON)
    ctx = {}
    sm._inject_goal_context(ctx, platform="", chat_key="c1")
    assert "【工作目标】" in (ctx.get("_goal_block") or "")


def test_inject_marks_action_consumed():
    store, goal = _seed_goal()
    sm = _SM(goals_cfg=_ON)
    ctx = {}
    sm._inject_goal_context(ctx, platform="telegram", chat_key="c1",
                            chain="draft")
    assert "_goal_block" in ctx
    from src.companion.goals.planner import day_key
    action = store.get_action(goal["goal_id"], day_key())
    assert action is not None and action["status"] == "consumed"
    assert action["detail"] == "draft"


def test_inject_account_id_precise_match():
    """多账号同 peer（同 chat_key 不同协议号）各有目标：A 线显式带 account_id
    时精确命中本账号那条，防串号（2026-07-27 硬化：_handle_message_guarded
    透传 _acct_id）。"""
    store = get_goal_store(":memory:")
    for acct, tmpl in (("a1", "relationship_intimacy"),
                       ("a2", "conversion_unlock")):
        g = store.create_goal(
            conversation_id=f"telegram:{acct}:c9", platform="telegram",
            account_id=acct, chat_key="c9", template=tmpl,
            autonomy="suggest", deadline_days=30)
        assert g is not None
    sm = _SM(goals_cfg=_ON)
    ctx = {}
    sm._inject_goal_context(ctx, platform="telegram", chat_key="c9",
                            account_id="a2")
    blk = ctx.get("_goal_block") or ""
    assert "【工作目标】" in blk
    # 精确命中 a2 的 conversion_unlock（不是宽匹配捞到的 a1 那条）：
    # settle-on-read 只会给被命中的目标建当日拍。
    from src.companion.goals.planner import day_key
    goal_a2 = store.find_active_goal(
        platform="telegram", chat_key="c9", account_id="a2")
    assert goal_a2 is not None and goal_a2["template"] == "conversion_unlock"
    assert store.get_action(goal_a2["goal_id"], day_key()) is not None


# ── ai_client 消费 ────────────────────────────────────────────────────────────

def test_ai_client_consumes_goal_block():
    from src.ai.ai_client import AIClient

    class _Cfg:
        config_path = None
        config = {"web_admin": {"site_name": "T"}, "ai": {}}

        def get_ai_config(self):
            return {}

    client = AIClient(_Cfg())
    out = client._build_context_prompt({
        "channel": "telegram",
        "_goal_block": "【工作目标】沉默唤回 · 里程碑 1/4 轻触探温\n【推进纪律】自然融入",
    })
    assert "【工作目标】" in out
    out2 = client._build_context_prompt({"channel": "telegram"})
    assert "【工作目标】" not in out2


# ── 双链调用点 + proactive 桥挂点 + metrics 并入（源码级 wireup 断言）──────────

def test_skill_manager_both_chains_call_inject():
    src = (_SRC_ROOT / "skills" / "skill_manager.py").read_text(
        encoding="utf-8", errors="ignore")
    assert src.count("self._inject_goal_context(") >= 2, (
        "A 线 process_message 与 B 线 generate_inbox_draft 都必须注入 _goal_block")
    # B 线必须在 bazi(3c) 之后注入（service 读 _bazi_block 判定 suppress_push）
    idx_bazi_b = src.rfind("self._inject_bazi_context(")
    idx_goal_b = src.rfind("self._inject_goal_context(")
    assert idx_goal_b > idx_bazi_b > 0


def test_proactive_topic_bridge_wired():
    src = (_SRC_ROOT / "companion" / "proactive_topic.py").read_text(
        encoding="utf-8", errors="ignore")
    assert "augment_plan_with_goal(" in src, "主动开场发送前必须过目标桥增广"
    assert "on_proactive_sent(" in src, "发送成功回执必须标记今日拍已发出"
    # 增广必须在真发路径 _send 内、且先于其文案生成（意图要进 directive 才影响
    # 开场内容）；预览端点（generate-only）刻意不过桥——预览不消耗当日拍。
    idx_send = src.find("async def _send(plan):")
    assert idx_send > 0
    idx_aug = src.find("augment_plan_with_goal(", idx_send)
    idx_gen = src.find("await _gen_text(", idx_send)
    assert idx_send < idx_aug < idx_gen, "桥增广必须在 _send 内且先于文案生成"
    # 预览路径（第一处 _gen_text 调用）之前不应有桥调用
    idx_gen_preview = src.find("await _gen_text(")
    assert src.find("augment_plan_with_goal(") > idx_gen_preview


def test_metrics_and_prometheus_wired():
    src = (_SRC_ROOT / "web" / "routes" / "drafts_routes.py").read_text(
        encoding="utf-8", errors="ignore")
    assert 'metrics["goals"]' in src
    assert src.count("get_goal_stats().dump_prom()") == 1


def test_ai_client_source_consumes_goal_block():
    src = (_SRC_ROOT / "ai" / "ai_client.py").read_text(
        encoding="utf-8", errors="ignore")
    assert '_goal_block' in src


# ── P25（2026-08-05）：注入观测元数据 _goal_inject_meta ──────────────────────
# 「设了目标为什么没切入」此前是黑箱——六个静默早退分支全都不留痕。
# 现在每条判定路径都写 ctx["_goal_inject_meta"]，生成链透传成 API goal_applied。

def test_meta_reason_disabled():
    sm = _SM(goals_cfg={"enabled": False})
    ctx = {"_goal_inject_meta": {"stale": True}}
    sm._inject_goal_context(ctx, platform="telegram", chat_key="c1")
    meta = ctx.get("_goal_inject_meta")
    assert meta == {"injected": False, "reason": "disabled"}  # 残留已被换新


def test_meta_reason_no_goal():
    sm = _SM(goals_cfg=_ON)
    ctx = {}
    sm._inject_goal_context(ctx, platform="telegram", chat_key="nobody")
    assert ctx["_goal_inject_meta"]["reason"] == "no_goal"
    assert ctx["_goal_inject_meta"]["injected"] is False


def test_meta_reason_observe():
    _seed_goal(autonomy="observe")
    sm = _SM(goals_cfg=_ON)
    ctx = {}
    sm._inject_goal_context(ctx, platform="telegram", chat_key="c1")
    meta = ctx["_goal_inject_meta"]
    assert meta["reason"] == "observe" and meta["injected"] is False
    assert meta.get("goal_id")


def test_meta_reason_hold_on_negative_emotion():
    _seed_goal()
    sm = _SM(goals_cfg=_ON)
    ctx = {"user_emotion_hint": "sad"}
    sm._inject_goal_context(ctx, platform="telegram", chat_key="c1")
    meta = ctx["_goal_inject_meta"]
    assert meta["reason"] == "hold" and meta["injected"] is False
    assert meta.get("hold_reason")


def test_meta_injected_carries_beat_fields():
    _seed_goal()
    sm = _SM(goals_cfg=_ON)
    ctx = {}
    sm._inject_goal_context(ctx, platform="telegram", chat_key="c1",
                            conversation_id="telegram:default:c1")
    assert "_goal_block" in ctx
    meta = ctx["_goal_inject_meta"]
    assert meta["injected"] is True and meta["reason"] == ""
    assert meta.get("goal_id") and meta.get("push_level")
    assert "intent" in meta and "milestone_idx" in meta


# ── P25：工坊「开新话题」链接入目标（此前唯一完全不消费目标的生成链）──────────

def _seed_custom_goal(chat_key: str = "c1", note: str = "获取客户年龄、职业"):
    store = get_goal_store(":memory:")
    goal = store.create_goal(
        conversation_id=f"telegram:default:{chat_key}", platform="telegram",
        account_id="default", chat_key=chat_key, template="custom",
        params={"note": note}, autonomy="suggest", deadline_days=14)
    assert goal is not None
    return store, goal


class _OpenerAI:
    """回显 directive + 捕获 ctx 的 opener 假 AI。"""

    def __init__(self):
        self.calls = []

    async def generate_reply_with_intent(self, *, user_message, intent,
                                         user_context, **_kw):
        self.calls.append({"msg": user_message, "intent": intent,
                           "ctx": dict(user_context)})
        return "开场白正文"


class _OpenerSM(_SM):
    """带 ai_client + 守卫桩的 SM 替身（_inject_goal_context 用真实现）。"""

    def __init__(self, *, goals_cfg=None):
        super().__init__(goals_cfg=goals_cfg)
        self.ai_client = _OpenerAI()
        self.guard_calls = []

    def _apply_goal_link_guard(self, reply, user_context, *, log_prefix=""):
        self.guard_calls.append(log_prefix)
        return reply


def _opener_app(sm):
    return SimpleNamespace(state=SimpleNamespace(
        skill_manager=sm, ai_client=None, telegram_client=None,
        config_manager=None, translation_service=None))


async def test_opener_injects_goal_block_and_steers_directive():
    _seed_custom_goal()
    sm = _OpenerSM(goals_cfg=_ON)
    from src.inbox.persona_reply import generate_topic_opener
    out = await generate_topic_opener(
        app=_opener_app(sm), platform="telegram", chat_key="c1",
        history=[], conversation_id="telegram:default:c1")
    assert out["ok"] is True
    call = sm.ai_client.calls[-1]
    # 今日意图（custom 模板代入 note）必须驱动开场指令，angle 降为备选
    assert "获取客户年龄、职业" in call["msg"]
    assert "工作目标" in call["msg"]
    assert "备选切入点" in call["msg"]
    # 系统侧 _goal_block 与回复链同源注入
    assert "【工作目标】" in (call["ctx"].get("_goal_block") or "")
    # 观测元数据透传到 API 出参（custom 新曲线首段=soft）
    ga = out["goal_applied"]
    assert ga["injected"] is True and ga["push_level"] == "soft"
    assert "获取客户年龄、职业" in ga["intent"]
    # 出站商业事实守卫与拟稿链配平（catalog 目录/价格措辞同一道闸）
    assert sm.guard_calls, "opener 生成后必须过 _apply_goal_link_guard"


async def test_opener_without_goal_keeps_plain_directive():
    sm = _OpenerSM(goals_cfg=_ON)
    from src.inbox.persona_reply import generate_topic_opener
    out = await generate_topic_opener(
        app=_opener_app(sm), platform="telegram", chat_key="nobody",
        history=[], conversation_id="telegram:default:nobody")
    assert out["ok"] is True
    call = sm.ai_client.calls[-1]
    assert "今天的切入点参考" in call["msg"]      # 旧行为逐字保留
    assert "进行中的工作目标" not in call["msg"]
    assert out["goal_applied"]["reason"] == "no_goal"


async def test_opener_observe_goal_stays_silent():
    _seed_goal(autonomy="observe")
    sm = _OpenerSM(goals_cfg=_ON)
    from src.inbox.persona_reply import generate_topic_opener
    out = await generate_topic_opener(
        app=_opener_app(sm), platform="telegram", chat_key="c1",
        history=[], conversation_id="telegram:default:c1")
    call = sm.ai_client.calls[-1]
    assert "进行中的工作目标" not in call["msg"]
    assert (call["ctx"].get("_goal_block") or "") == ""
    assert out["goal_applied"]["reason"] == "observe"


def test_opener_directive_pure_function_goal_variants():
    from src.inbox.persona_reply import build_opener_directive
    plain = build_opener_directive(angle="聊聊天气", reply_lang="zh")
    soft = build_opener_directive(
        angle="聊聊天气", reply_lang="zh",
        goal_intent="自然了解TA的职业", goal_push="soft", goal_title="摸底")
    direct = build_opener_directive(
        angle="聊聊天气", reply_lang="zh",
        goal_intent="自然了解TA的职业", goal_push="direct", goal_title="摸底")
    none_push = build_opener_directive(
        angle="聊聊天气", reply_lang="zh",
        goal_intent="自然了解TA的职业", goal_push="none", goal_title="摸底")
    assert "自然了解TA的职业" in soft and "「摸底」" in soft
    assert "聊聊天气" in soft                     # angle 仍作备选在场
    assert "绝不生硬转折" in soft
    assert "可以比较直接" in direct
    # none 力度（今天只陪伴）与无目标输出逐字一致——目标方向只字不进指令
    assert none_push == plain
    # 模板壳禁令与单问纪律在所有变体恒在
    for d in (plain, soft, direct):
        assert "好久没联系" in d and "最多问一个问题" in d


def test_opener_and_draft_chain_source_wireup():
    """源码级钉住：opener 产线经同一注入口（chain=opener）、返回带 goal_applied、
    直连回落路径也注入（统一引擎故障时目标不再静默消失）、拟稿返回透传元数据。"""
    src_pr = (_SRC_ROOT / "inbox" / "persona_reply.py").read_text(
        encoding="utf-8", errors="ignore")
    assert 'chain="opener"' in src_pr
    assert src_pr.count('"goal_applied"') >= 2       # opener + reply 两处出参
    assert src_pr.count("sm._inject_goal_context(") >= 2   # opener + direct 回落
    assert src_pr.count("_apply_goal_link_guard(") >= 2    # 两处守卫配平
    src_sm = (_SRC_ROOT / "skills" / "skill_manager.py").read_text(
        encoding="utf-8", errors="ignore")
    assert '"goal_applied": user_context.get("_goal_inject_meta")' in src_sm


# ── P26（2026-08-05）：摸底模板端到端（槽位缺口→采集→推进→达成）──────────────

def _seed_discovery_goal(chat_key: str = "d1", slots: str = "age,occupation"):
    store = get_goal_store(":memory:")
    goal = store.create_goal(
        conversation_id=f"telegram:default:{chat_key}", platform="telegram",
        account_id="default", chat_key=chat_key, template="profile_discovery",
        params={"slots": slots}, autonomy="suggest", deadline_days=10)
    assert goal is not None
    return store, goal


def test_discovery_gap_from_day_one_single_slot():
    """建目标当天（里程碑 0）缺口就并进今日意图（P27 合流），且每轮只带一个
    （勾选序第一缺口）；独立【画像缺口】行取消防同块重复。"""
    _seed_discovery_goal()
    sm = _SM(goals_cfg=_ON)
    ctx = {}
    sm._inject_goal_context(ctx, platform="telegram", chat_key="d1",
                            conversation_id="telegram:default:d1")
    blk = ctx.get("_goal_block") or ""
    assert "本轮顺势了解" in blk               # 缺口在今日意图行内
    assert "哪个年龄段" in blk                 # 首缺口 = 勾选序第一个（age）
    assert "【画像缺口】" not in blk           # 合流后独立行取消
    assert "做什么生意/工作" not in blk        # 单缺口：一次只带一件
    meta = ctx["_goal_inject_meta"]
    assert meta["injected"] is True and meta["profile_gap"]
    # meta.intent 携带缺口 → opener 转向与徽章都拿到「本轮问什么」
    assert "哪个年龄段" in meta["intent"]


def test_discovery_filled_slot_switches_gap_and_advances():
    """客户说了年龄（槽位入库）→ 缺口切到下一项、里程碑/进度随填充率推进。"""
    store, goal = _seed_discovery_goal(chat_key="d2")
    store.upsert_customer_profile(
        "telegram", "d2", {"age": "28岁"}, source="auto")
    sm = _SM(goals_cfg=_ON)
    ctx = {}
    sm._inject_goal_context(ctx, platform="telegram", chat_key="d2",
                            conversation_id="telegram:default:d2")
    blk = ctx.get("_goal_block") or ""
    assert "做什么生意/工作" in blk
    assert "哪个年龄段" not in blk
    meta = ctx["_goal_inject_meta"]
    assert int(meta["milestone_idx"]) >= 1     # fill 0.5 推进（day1 相位封顶 1 段）
    g = store.get_goal(goal["goal_id"])
    assert float(g["progress"]) >= 0.45        # 填充率 0.5×0.95 直接进进度条


def test_discovery_all_slots_filled_completes_goal():
    """勾选槽位全部聊出来 → settle-on-read 自动标「已达成」，本轮不再注入。"""
    store, goal = _seed_discovery_goal(chat_key="d3")
    store.upsert_customer_profile(
        "telegram", "d3", {"age": "28岁", "occupation": "律师"}, source="auto")
    sm = _SM(goals_cfg=_ON)
    ctx = {}
    sm._inject_goal_context(ctx, platform="telegram", chat_key="d3",
                            conversation_id="telegram:default:d3")
    assert "_goal_block" not in ctx            # 目标已达成，不再推进
    assert ctx["_goal_inject_meta"]["reason"] == "inactive"
    g = store.get_goal(goal["goal_id"])
    assert g["status"] == "done" and g["result"] == "slots_filled"
    assert float(g["progress"]) == 1.0


def test_discovery_without_slots_never_autocompletes():
    """slots 全非法/为空 → selected_fill=-1（信号缺失）→ 绝不自动达成。"""
    store, goal = _seed_discovery_goal(chat_key="d4", slots="bogus,churn_reason")
    sm = _SM(goals_cfg=_ON)
    ctx = {}
    sm._inject_goal_context(ctx, platform="telegram", chat_key="d4",
                            conversation_id="telegram:default:d4")
    g = store.get_goal(goal["goal_id"])
    assert g["status"] == "active"


def test_age_capture_positive_and_traps():
    """age 槽采集：明确自述才采；日期/楼层/裸数字/越界/分钟一律不采。"""
    from src.companion.goals.profile_slots import capture_from_text
    assert ("age", "28岁") in capture_from_text("我今年28岁啦")
    assert ("age", "28岁") in capture_from_text("我28岁")
    assert ("age", "28岁") in capture_from_text("我今年28了")
    assert ("age", "30岁") in capture_from_text("I'm 30 years old")
    assert ("age", "35岁") in capture_from_text("I am 35 years old")
    assert ("age", "90后") in capture_from_text("我是90后哈哈")
    traps = ("我今年28号出发", "我住28楼", "我今年28",
             "我8岁就会游泳了", "I am 28 minutes away", "房间号是90后面那间")
    for t in traps:
        assert not [kv for kv in capture_from_text(t) if kv[0] == "age"], t


def test_selected_slots_pure_functions():
    from src.companion.goals.profile_slots import (
        gap_hint,
        missing_slots,
        parse_selected_slots,
        selected_fill_rate,
    )
    assert parse_selected_slots("age, occupation、bogus location") == [
        "age", "occupation", "location"]
    # 去重 + lifecycle 专用槽（churn_reason）拒收
    assert parse_selected_slots(["AGE", "age", "churn_reason"]) == ["age"]
    assert parse_selected_slots("") == []
    fields = {"age": {"v": "28岁"}}
    assert selected_fill_rate(fields, ["age", "occupation"]) == 0.5
    assert selected_fill_rate(fields, []) == -1.0
    assert selected_fill_rate({}, None) == -1.0
    miss = missing_slots(fields, include=["age", "occupation"], limit=5)
    assert [m["key"] for m in miss] == ["occupation"]
    assert gap_hint(fields, include=["age", "occupation"], limit=1) == \
        "做什么生意/工作"
    assert gap_hint({"age": {"v": "28岁"}, "occupation": {"v": "律师"}},
                    include=["age", "occupation"]) == ""


def test_goal_hold_on_manual_negative_mood_via_inbox_store():
    """P26：坐席手动「情绪低落」标注（conv_meta）→ 目标让路。此前注入口
    不传 inbox_store，该判据在所有生成链都是死路。"""
    import time as _time
    _seed_goal()

    class _FakeInbox:
        def get_conv_meta(self, cid):
            return {"mood_manual": "情绪低落",
                    "mood_manual_ts": _time.time(),
                    "conv_tags": ["情绪低落"]}

    from src.companion.goals.service import build_block_for_chat
    cfg = SimpleNamespace(config={"companion": {"goals": dict(_ON)}},
                          config_path=None)
    ctx: dict = {}
    block = build_block_for_chat(
        cfg, platform="telegram", chat_key="c1",
        conversation_id="telegram:default:c1",
        user_context=ctx, inbox_store=_FakeInbox())
    assert block is None
    assert ctx["_goal_inject_meta"]["reason"] == "hold"


def test_inject_resolves_inbox_store_source():
    """源码级：注入口经协议桥单例补 inbox_store（四条链一处生效）。"""
    src = (_SRC_ROOT / "skills" / "skill_manager.py").read_text(
        encoding="utf-8", errors="ignore")
    idx = src.find("def _inject_goal_context")
    assert idx > 0
    seg = src[idx:idx + 3000]
    assert "get_inbox_store" in seg and "inbox_store=_ibx" in seg


# ── P27（2026-08-05）：意图×缺口合流 + 主动桥同源 + LLM 摘录聚焦 ─────────────

def test_merged_beat_intent_pure():
    from src.companion.goals.service import merged_beat_intent
    out = merged_beat_intent("先把互动热起来", "大概哪个年龄段")
    assert out.startswith("先把互动热起来；")
    assert "本轮顺势了解：大概哪个年龄段" in out and "一次只问这一件" in out
    assert merged_beat_intent("原意图", "") == "原意图"      # 无缺口原样
    assert "本轮顺势了解" in merged_beat_intent("", "职业")   # 无意图仍给方向


def test_discovery_gap_for_goal_semantics():
    from src.companion.goals.service import discovery_gap_for_goal
    from src.companion.goals.templates import get_template
    store, goal = _seed_discovery_goal(chat_key="d5")
    tpl = get_template("profile_discovery")
    assert "哪个年龄段" in discovery_gap_for_goal(store, tpl, goal)
    # 填满 → 无缺口；非合流模板（custom）→ 恒空
    store.upsert_customer_profile(
        "telegram", "d5", {"age": "28岁", "occupation": "律师"}, source="auto")
    assert discovery_gap_for_goal(store, tpl, goal) == ""
    assert discovery_gap_for_goal(
        store, get_template("custom"), goal) == ""


def test_bridge_merges_discovery_gap_into_directive():
    """主动桥与注入链同源：auto 档摸底目标的主动开场 directive 必须带
    「本轮顺势了解：<缺口>」；非摸底模板保持原始拍意图不动。"""
    from src.companion.goals.bridge import augment_plan_with_goal
    store = get_goal_store(":memory:")
    g1 = store.create_goal(
        conversation_id="telegram:default:b1", platform="telegram",
        account_id="default", chat_key="b1", template="profile_discovery",
        params={"slots": "age,occupation"}, autonomy="auto",
        deadline_days=10)
    g2 = store.create_goal(
        conversation_id="telegram:default:b2", platform="telegram",
        account_id="default", chat_key="b2", template="custom",
        params={"note": "推广新品"}, autonomy="auto", deadline_days=14)
    assert g1 and g2
    cfg_root = {"companion": {"goals": {
        "enabled": True, "db_path": ":memory:",
        "bridge": {"enabled": True}}}}
    plan1 = {"conversation_id": "telegram:default:b1",
             "platform": "telegram", "account_id": "default",
             "chat_key": "b1", "directive": "旧指令"}
    augment_plan_with_goal(cfg_root, None, plan1)
    assert "【工作目标衔接】" in plan1["directive"]
    assert "本轮顺势了解" in plan1["directive"]
    assert "哪个年龄段" in plan1["directive"]
    plan2 = {"conversation_id": "telegram:default:b2",
             "platform": "telegram", "account_id": "default",
             "chat_key": "b2", "directive": ""}
    augment_plan_with_goal(cfg_root, None, plan2)
    assert "【工作目标衔接】" in plan2["directive"]
    assert "本轮顺势了解" not in plan2["directive"]   # 非摸底不合流


async def test_profile_llm_include_narrows_prompt():
    """P27：摸底目标的 LLM 摘录只问勾选缺口（全 11 槽提示词=误摘面更大）。"""
    import asyncio
    from src.companion.goals import profile_llm as pl
    pl.reset_state()

    class _CapAI:
        def __init__(self):
            self.prompts = []

        async def chat(self, prompt):
            self.prompts.append(prompt)
            return "{}"

    ai = _CapAI()
    store = get_goal_store(":memory:")
    cfg = {"companion": {"goals": {"profile_llm": {"enabled": True}}}}
    ok = pl.schedule_llm_capture(
        ai, store, cfg, platform="telegram", chat_key="p1",
        text="我这边是帮人代运营民宿的，白天基本都在店里",
        fields={}, include=["age", "occupation"])
    assert ok is True
    for _ in range(4):                      # 让 fire-and-forget 任务跑完
        await asyncio.sleep(0)
    assert ai.prompts, "LLM 摘录任务未执行"
    p = ai.prompts[-1]
    assert "age" in p and "occupation" in p
    assert "team_size" not in p and "budget" not in p
    pl.reset_state()
