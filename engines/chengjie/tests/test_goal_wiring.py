"""营销目标接线门禁：skill_manager 双链注入 / ai_client 消费 / proactive 桥挂点 /
metrics 并入。轻量绑定（免全量 init，与 test_bazi_wiring 同模式），全部离线。"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

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
