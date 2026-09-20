# -*- coding: utf-8 -*-
"""学习队列断粮复盘（2026-08-02）门禁。

覆盖四层修复：
1. kb_gate 入池守门纯函数（占位符/问题样式）——防陪聊闲聊与系统占位符灌爆 miss_log；
2. resolve_learner_ai 回落链——telegram 协议客户端下线的实例不再整族假空；
3. DailyLearner AI 可选——stats/审核纯 DB 不被 AI 缺席连坐，run 如实记 ai_unavailable；
4. 素材漏斗计数 + last_run 落盘 + 手动喂料 feed_and_learn。
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.utils.daily_learner import (
    LAST_RUN_META_KEY,
    DailyLearner,
    resolve_learner_ai,
)
from src.utils.kb_gate import (
    is_system_placeholder,
    looks_like_kb_query,
    should_log_kb_miss,
)


# ── 1. 入池守门纯函数 ────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "[语音消息 - 下载失败]",
    "[名片] Wisley",
    "[图片内容] 一张海边的照片",
    "[视频内容] 跳舞视频",
    "[文件] report.pdf",
    "[TRANSLATE:en:abc123] 怎么付款",
])
def test_system_placeholder_detected(text):
    assert is_system_placeholder(text)
    assert not should_log_kb_miss(text)


@pytest.mark.parametrize("text", [
    "怎么充值VIP",          # 疑问词
    "为什么给我发英文",      # 生产实录：真实产品投诉
    "我要退款",              # 求助句式（无问号）
    "请问你们支持哪些支付方式",
    "价格?",                # 问号
    "有没有安卓版",
    "how much is the vip plan",
    "can you send me the invoice",
])
def test_question_like_accepted(text):
    assert looks_like_kb_query(text)
    assert should_log_kb_miss(text)


@pytest.mark.parametrize("text", [
    "可",                    # 生产实录：单字闲聊
    "Jinn",                  # 生产实录：无意义人名
    "",
    "哈哈哈好的",            # 纯附和
    "早安宝贝",              # 陪聊寒暄
    "hello there",           # 英文寒暄（无疑问结构）
])
def test_chitchat_rejected(text):
    assert not looks_like_kb_query(text)
    assert not should_log_kb_miss(text)


def test_chitchat_with_interrogative_is_accepted_by_design():
    """含疑问词的闲聊刻意放行（召回优先，cnt 门槛 + 人工审核兜底）。"""
    assert looks_like_kb_query("来聊聊你中午要吃什么。")


# ── 2. AI 客户端回落链 ────────────────────────────────────────────


def _app_with_state(**kwargs):
    return SimpleNamespace(state=SimpleNamespace(**kwargs))


def test_resolve_ai_prefers_telegram_client():
    tg = SimpleNamespace(ai_client="tg_ai")
    app = _app_with_state(ai_client="state_ai")
    assert resolve_learner_ai(app, tg) == "tg_ai"


def test_resolve_ai_falls_back_to_app_state():
    """zhiliao 实锤形态：telegram_client=None（协议号未配置）→ 取 app.state。"""
    app = _app_with_state(ai_client="state_ai")
    assert resolve_learner_ai(app, None) == "state_ai"


def test_resolve_ai_falls_back_to_skill_manager():
    sm = SimpleNamespace(ai_client="sm_ai")
    app = _app_with_state(skill_manager=sm)
    assert resolve_learner_ai(app, None) == "sm_ai"


def test_resolve_ai_none_when_nothing():
    assert resolve_learner_ai(_app_with_state(), None) is None
    assert resolve_learner_ai(None, None) is None


# ── 3/4. DailyLearner：AI 可选 / 漏斗 / last_run / 喂料 ─────────────


def _mock_kb(tmp_path, miss_rows=None):
    kb = MagicMock()
    kb._db_path = str(tmp_path / "kb.db")
    kb.get_miss_stats = MagicMock(return_value=miss_rows or [])
    kb.list_feedback = MagicMock(return_value=[])
    kb.get_auto_suggestions = MagicMock(return_value=[])
    kb.search = MagicMock(return_value={"entries": []})
    kb.get_meta = MagicMock(return_value=None)
    return kb


def test_stats_works_without_ai(tmp_path):
    """stats/list 是纯 DB 操作，AI 缺席不该 503/假空（zhiliao 一周假空的根因）。"""
    kb = _mock_kb(tmp_path)
    dl = DailyLearner(kb, ai_client=None, db_path=tmp_path / "drafts.db")
    assert dl.ai_ready is False
    s = dl.stats()
    assert s["pending"] == 0 and s["approved"] == 0


def test_attach_ai_late_binding(tmp_path):
    dl = DailyLearner(_mock_kb(tmp_path), None, db_path=tmp_path / "d.db")
    assert not dl.ai_ready
    dl.attach_ai("ai")
    assert dl.ai_ready
    dl.attach_ai(None)  # None 不得清掉已有 AI
    assert dl.ai_ready


def test_collect_funnel_filters(tmp_path):
    """漏斗计数：占位符/非问题样式/低于门槛 各归各位，素材只剩达标问题。"""
    rows = [
        {"query": "怎么充值VIP", "cnt": 3, "last_at": "t"},        # 达标
        {"query": "为什么这么贵", "cnt": 1, "last_at": "t"},        # 低于门槛
        {"query": "[语音消息 - 下载失败]", "cnt": 5, "last_at": "t"},  # 占位符
        {"query": "哈哈哈好的", "cnt": 9, "last_at": "t"},          # 非问题样式
        {"query": "[TRANSLATE:en:x] 标题", "cnt": 2, "last_at": "t"},  # 翻译标记不计入
    ]
    kb = _mock_kb(tmp_path, miss_rows=rows)
    dl = DailyLearner(kb, None, db_path=tmp_path / "d.db")
    materials, funnel = dl.collect_with_funnel(min_miss_count=2)
    assert [m["query"] for m in materials] == ["怎么充值VIP"]
    assert funnel["miss_total"] == 4          # TRANSLATE 不计
    assert funnel["miss_placeholder"] == 1
    assert funnel["miss_not_question"] == 1
    assert funnel["miss_below_threshold"] == 1
    assert funnel["miss_qualified"] == 1
    assert funnel["final"] == 1


def test_collect_funnel_excludes_already_drafted(tmp_path):
    rows = [{"query": "怎么充值VIP", "cnt": 3, "last_at": "t"}]
    kb = _mock_kb(tmp_path, miss_rows=rows)
    dl = DailyLearner(kb, None, db_path=tmp_path / "d.db")
    dl.save_drafts([{"source": "miss", "query": "怎么充值VIP", "hit_count": 3,
                     "title": "充值", "triggers": [], "example_reply": "",
                     "ai_reasoning": "", "confidence": 50}])
    materials, funnel = dl.collect_with_funnel(min_miss_count=2)
    assert materials == []
    assert funnel["already_drafted"] == 1


@pytest.mark.asyncio
async def test_run_without_ai_records_honest_error(tmp_path):
    """有素材但 AI 不可用：如实记 ai_unavailable，素材保留，last_run 照落。"""
    rows = [{"query": "怎么充值VIP", "cnt": 3, "last_at": "t"}]
    kb = _mock_kb(tmp_path, miss_rows=rows)
    dl = DailyLearner(kb, None, db_path=tmp_path / "d.db")
    result = await dl.run_daily_learn(min_miss_count=2, source="manual")
    assert result["collected"] == 1
    assert result["generated"] == 0
    assert result["error"] == "ai_unavailable"
    kb.delete_miss_entry.assert_not_called()   # 素材不销毁，等 AI 恢复
    # last_run 落盘（经 kb.set_meta）
    key, payload = kb.set_meta.call_args[0]
    assert key == LAST_RUN_META_KEY
    data = json.loads(payload)
    assert data["source"] == "manual" and data["error"] == "ai_unavailable"
    assert data["funnel"]["miss_qualified"] == 1


def _fake_ai_returning_draft():
    ai = MagicMock()
    ai.generate_reply = AsyncMock(return_value=json.dumps([{
        "index": 1, "category": "常规咨询", "title": "VIP 充值",
        "triggers": ["充值", "VIP"], "example_reply": "您好，充值入口在……",
        "reasoning": "高频问题", "confidence": 80,
    }], ensure_ascii=False))
    return ai


@pytest.mark.asyncio
async def test_run_with_ai_end_to_end(tmp_path):
    rows = [{"query": "怎么充值VIP", "cnt": 3, "last_at": "t"}]
    kb = _mock_kb(tmp_path, miss_rows=rows)
    dl = DailyLearner(kb, _fake_ai_returning_draft(), db_path=tmp_path / "d.db")
    result = await dl.run_daily_learn(min_miss_count=2, source="scheduled")
    assert result == {"collected": 1, "generated": 1, "saved": 1}
    kb.delete_miss_entry.assert_called_once_with("怎么充值VIP")
    drafts = dl.list_drafts(status="pending")
    assert len(drafts) == 1 and drafts[0]["query"] == "怎么充值VIP"


def test_stats_exposes_last_run(tmp_path):
    kb = _mock_kb(tmp_path)
    kb.get_meta = MagicMock(return_value=json.dumps(
        {"ts": "2026-08-02T07:00:00", "source": "scheduled",
         "collected": 0, "generated": 0, "saved": 0,
         "funnel": {"miss_total": 42}}))
    dl = DailyLearner(kb, None, db_path=tmp_path / "d.db")
    s = dl.stats()
    assert s["last_run"]["funnel"]["miss_total"] == 42


@pytest.mark.asyncio
async def test_feed_without_ai_queues(tmp_path):
    kb = _mock_kb(tmp_path)
    dl = DailyLearner(kb, None, db_path=tmp_path / "d.db")
    r = await dl.feed_and_learn("怎么开发票", min_miss_count=2)
    assert r == {"queued": True, "generated": 0, "reason": "ai_unavailable"}
    kb.seed_miss.assert_called_once_with("怎么开发票", min_cnt=2)


@pytest.mark.asyncio
async def test_feed_with_ai_generates_immediately(tmp_path):
    kb = _mock_kb(tmp_path)
    dl = DailyLearner(kb, _fake_ai_returning_draft(), db_path=tmp_path / "d.db")
    r = await dl.feed_and_learn("怎么开发票")
    assert r == {"queued": True, "generated": 1}
    drafts = dl.list_drafts(status="pending")
    assert len(drafts) == 1 and drafts[0]["source"] == "manual"
    kb.delete_miss_entry.assert_called_once_with("怎么开发票")


@pytest.mark.asyncio
async def test_feed_duplicate_short_circuits(tmp_path):
    """同题已有 pending 草稿 → 不烧 LLM 直接返回 duplicate。"""
    kb = _mock_kb(tmp_path)
    ai = _fake_ai_returning_draft()
    dl = DailyLearner(kb, ai, db_path=tmp_path / "d.db")
    await dl.feed_and_learn("怎么开发票")
    r2 = await dl.feed_and_learn("怎么开发票")
    assert r2["queued"] is False and r2["reason"] == "duplicate"
    assert ai.generate_reply.await_count == 1   # 第二次没有再调 LLM


@pytest.mark.asyncio
async def test_feed_rejects_too_short(tmp_path):
    dl = DailyLearner(_mock_kb(tmp_path), None, db_path=tmp_path / "d.db")
    r = await dl.feed_and_learn("嗯")
    assert r == {"queued": False, "reason": "too_short"}


# ── kb_store.seed_miss（真实 store）────────────────────────────────


def test_seed_miss_real_store(tmp_path):
    from src.utils.kb_store import KnowledgeBaseStore
    kb = KnowledgeBaseStore(tmp_path / "kb.db")
    kb.seed_miss("怎么开发票", min_cnt=2)
    rows = {r["query"]: r["cnt"] for r in kb.get_miss_stats(top_k=10)}
    assert rows["怎么开发票"] == 2
    # 再喂更低的 min_cnt 不回退（MAX 语义）
    kb.seed_miss("怎么开发票", min_cnt=1)
    rows = {r["query"]: r["cnt"] for r in kb.get_miss_stats(top_k=10)}
    assert rows["怎么开发票"] == 2
    # 自然流量继续 +1
    kb.log_miss("怎么开发票")
    rows = {r["query"]: r["cnt"] for r in kb.get_miss_stats(top_k=10)}
    assert rows["怎么开发票"] == 3


def test_learner_accepts_real_store_without_explicit_db_path(tmp_path):
    """真实 store 只有 db_path（无 _db_path）——构造回落不得 AttributeError。"""
    from src.utils.kb_store import KnowledgeBaseStore
    kb = KnowledgeBaseStore(tmp_path / "kb.db")
    dl = DailyLearner(kb, None)
    assert Path(dl._db_path).parent == (tmp_path / "kb.db").parent
