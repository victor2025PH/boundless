# -*- coding: utf-8 -*-
"""「被吞回复」修复门禁（2026-08-09，198↔104 实录「回复等了 11 分钟」）。

事故机制（104 backend.log 三连实锤 05:32/06:10/06:24）：
1. A 线冷却（per_user 60s）把对方秒回的追问静默吞掉——无计数、无补救调度；
2. interject 在 client 层丢弃**已生成**的回复，但 skill 层 _update_after_reply
   早已提交冷却记账/last_reply → 幻影回复占冷却位，整个消息爆发全灭；
3. 被吞消息唯一复活途径＝轮询兜底在去重 TTL（600s）过期后碰巧重拾 →
   用户看到「隔 10-11 分钟才回」。

三层修复（本文件逐层钉住）：
- MessageDedup.reschedule：把被吞 mid 的去重寿命缩短到冷却结束后不久
  （只提前不延后），轮询兜底按时重拾（水位闸保证至多一次）；
- SkillManager 冷却拦截 → 显式跳过信号（_note_reply_skip / consume_reply_skip），
  上层能区分「刻意不回」vs「被冷却吃了」；_cooldown_remaining 返回全部失败桶
  的最大剩余（只看首桶会让唯一一次重试撞上更长的桶）；
- snapshot_reply_accounting / rollback_reply_accounting：interject 丢弃时精确
  撤销本轮冷却记账与 last_reply（时间戳 >= 快照时刻才动，绝不误伤此前
  合法回复的记账）。

接线本体在 telegram_client 的闭包/handler 里（测试拿不到句柄），按仓库惯例
用静态接线断言钉住（同 test_human_deliver_* 的「静态接线不得被 if 包住」风格）。
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
import yaml

from src.client.message_dedup import MessageDedup

ENGINE_ROOT = Path(__file__).resolve().parents[1]


# ── MessageDedup.reschedule ────────────────────────────────────────────────


class _Clock:
    def __init__(self, t0: float = 1000.0):
        self.now = t0

    def __call__(self) -> float:
        return self.now


def test_reschedule_shortens_expiry_and_poll_can_reclaim():
    clk = _Clock()
    dd = MessageDedup(max_size=100, ttl_sec=600.0, clock=clk)
    assert dd.claim("c1", 42) is True
    assert dd.seen("c1", 42) is True
    # 冷却剩 55s → 改写为 ~58s 后到期（而不是 600s）
    assert dd.reschedule("c1", 42, 58.0) is True
    clk.now += 30
    assert dd.seen("c1", 42) is True  # 未到期仍去重
    clk.now += 30
    assert dd.seen("c1", 42) is False  # 58s 已过 → 轮询可当新进站重拾
    assert dd.claim("c1", 42) is True  # 重拾时 claim 成功


def test_reschedule_only_shortens_never_extends():
    clk = _Clock()
    dd = MessageDedup(max_size=100, ttl_sec=600.0, clock=clk)
    dd.claim("c1", 7)
    # 恶意/误用：要求 9999s —— 不得比原 TTL 更晚到期
    assert dd.reschedule("c1", 7, 9999.0) is True
    clk.now += 601
    assert dd.seen("c1", 7) is False  # 原 600s 到点照常过期


def test_reschedule_unknown_or_empty_mid_is_noop():
    dd = MessageDedup(max_size=10, ttl_sec=600.0, clock=_Clock())
    assert dd.reschedule("c1", 99, 10.0) is False  # 从未登记
    assert dd.reschedule("c1", 0, 10.0) is False  # 无 mid
    dd.claim("c1", 1)
    assert dd.reschedule("c2", 1, 10.0) is False  # 复合键不同会话不误伤


def test_reschedule_on_never_expiring_table_deletes_entry():
    dd = MessageDedup(max_size=10, ttl_sec=0.0, clock=_Clock())
    dd.claim("c1", 5)
    assert dd.reschedule("c1", 5, 30.0) is True
    assert dd.seen("c1", 5) is False  # 永不过期表无定时可言 → 删除等效


# ── SkillManager：真实实例全链 ────────────────────────────────────────────


class _FakeAIClient:
    model = "fake-model"

    def __init__(self):
        self.reply_count = 0

    async def generate_reply_with_intent(self, *args, **kwargs):
        self.reply_count += 1
        return f"回复{self.reply_count}-{uuid.uuid4().hex[:6]}"

    async def chat(self, *args, **kwargs):
        return "no"

    def _detect_message_language(self, text: str) -> str:
        return "zh"

    async def embed(self, texts):
        return [[0.0] * 8 for _ in texts]

    async def embed_with_fallback(self, texts):
        return [[0.0] * 8 for _ in texts]


async def _make_sm(tmp_path: Path, *, cooldown: dict):
    from src.skills.skill_manager import GreetingSkill, SkillManager
    from src.utils.config_manager import ConfigManager

    cfg = {
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": [], "cooldown": cooldown},
        "intent": {"keywords": {}, "patterns": {}},
        "reply": {},
        "context_store": {"ttl_days": 30},
        "memory": {
            "enabled": True,
            "db_path": str(tmp_path / "episodic.db"),
            "vector": {"enabled": False},
            "extract": {"enabled": False},
        },
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text("greeting: hi\n", encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(
        "channels: {}\n", encoding="utf-8")
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    await cm.load()
    sm = SkillManager(cm, _FakeAIClient())
    # 冷却检查先于技能选择：必须有技能真出话，记账/冷却才会被写——
    # 注册 greeting 承接问候类意图（与 test_lang_prior 同构）。
    sm.skills["greeting"] = GreetingSkill(cm, sm.ai_client)
    return sm


async def test_cooldown_skip_emits_consumable_signal(tmp_path):
    """冷却吞消息 → 跳过信号可被上层消费（一次性）；正常回复不产生信号。"""
    sm = await _make_sm(tmp_path, cooldown={
        "global": 0, "per_user": 60, "per_content": 0, "per_chat_user": 0})
    ctx = {"chat_id": "7001", "account_id": "acctA", "request_id": "t-1"}

    r1 = await sm.process_message("你好呀", "7001", context=dict(ctx))
    assert r1  # 首条正常出话
    assert sm.consume_reply_skip("7001", "7001", "acctA") is None  # 无信号

    r2 = await sm.process_message("你好哇", "7001", context=dict(ctx))
    assert r2 is None  # 60s 冷却窗内被吞
    sig = sm.consume_reply_skip("7001", "7001", "acctA")
    assert sig and sig.get("reason") == "cooldown"
    assert 0 < float(sig["retry_after"]) <= 60.0
    # 取走即删：同一信号不得二次消费（防陈旧信号误触发补答）
    assert sm.consume_reply_skip("7001", "7001", "acctA") is None


async def test_cooldown_remaining_reports_max_bucket(tmp_path):
    """剩余时长必须取全部失败桶的最大值——重试只有一次机会，不能撞上更长的桶。"""
    sm = await _make_sm(tmp_path, cooldown={
        "global": 0, "per_user": 30, "per_content": 120, "per_chat_user": 0})
    ctx = {"chat_id": "7002", "account_id": "acctA", "request_id": "t-2"}
    assert await sm.process_message("你好呀", "7002", context=dict(ctx))
    # 同文本重发：per_user 剩 ~30s、per_content 剩 ~120s → 必须按 120 报
    left = sm._cooldown_remaining(
        "你好呀", "7002", chat_id="7002", account_id="acctA")
    assert left > 60.0
    # 布尔壳与剩余口径一致（签名契约由 test_group_context_split 另行钉住）
    assert sm._check_cooldown(
        "你好呀", "7002", chat_id="7002", account_id="acctA") is False


async def test_snapshot_rollback_frees_cooldown_and_memory(tmp_path):
    """快照→记账→回滚：冷却位释放、last_reply/防复读环恢复，如同从未生成过。"""
    sm = await _make_sm(tmp_path, cooldown={
        "global": 30, "per_user": 60, "per_content": 120, "per_chat_user": 5})
    ctx = {"chat_id": "7003", "account_id": "acctB"}
    snap = sm.snapshot_reply_accounting("7003", dict(ctx))
    assert snap and snap["cu_key"]

    # 复刻 _handle_message_guarded 的写序：上下文写入 → 生成完成记账
    uc = sm._get_user_context("7003", account_id="acctB")
    if not uc.get("account_id"):
        uc["account_id"] = "acctB"
    uc["last_message"] = "路上小心点。"
    sm._update_after_reply("到屋了，夜风一吹反而不困了。", "7003", uc,
                           chat_id="7003", user_msg="路上小心点。")

    assert sm._cooldown_remaining(
        "在吗", "7003", chat_id="7003", account_id="acctB") > 0
    assert uc.get("last_reply")
    assert uc.get("recent_replies")

    assert sm.rollback_reply_accounting(snap) is True

    # 冷却位释放（四桶全部回到放行态）
    assert sm._cooldown_remaining(
        "在吗", "7003", chat_id="7003", account_id="acctB") == 0.0
    # 记忆分叉修复：从未发出的回复不得留在 last_reply / 防复读环
    assert not uc.get("last_reply")
    assert not uc.get("recent_replies")
    assert int(uc.get("reply_count", 0) or 0) == 0
    # 幂等：再滚一次没有可撤销项
    assert sm.rollback_reply_accounting(snap) is False


async def test_rollback_never_touches_pre_snapshot_accounting(tmp_path):
    """快照前的合法记账（真实已发回复）绝不能被回滚误伤。"""
    sm = await _make_sm(tmp_path, cooldown={
        "global": 0, "per_user": 600, "per_content": 0, "per_chat_user": 0})
    ctx = {"chat_id": "7004", "account_id": "acctC"}
    uc = sm._get_user_context("7004", account_id="acctC")
    uc["account_id"] = "acctC"
    uc["last_message"] = "早安"
    sm._update_after_reply("早呀。", "7004", uc, chat_id="7004", user_msg="早安")
    before = dict(
        last_reply=uc.get("last_reply"),
        reply_count=uc.get("reply_count"),
        remaining=sm._cooldown_remaining(
            "早", "7004", chat_id="7004", account_id="acctC"),
    )
    assert before["remaining"] > 0

    snap = sm.snapshot_reply_accounting("7004", dict(ctx))
    # 本轮什么都没写（生成前就被丢弃的场景不存在写入）→ 回滚必须是 no-op
    assert sm.rollback_reply_accounting(snap) is False
    assert uc.get("last_reply") == before["last_reply"]
    assert uc.get("reply_count") == before["reply_count"]
    assert sm._cooldown_remaining(
        "早", "7004", chat_id="7004", account_id="acctC") > 0


async def test_rollback_handles_garbage_snapshot(tmp_path):
    sm = await _make_sm(tmp_path, cooldown={
        "global": 0, "per_user": 0, "per_content": 0, "per_chat_user": 0})
    assert sm.rollback_reply_accounting(None) is False
    assert sm.rollback_reply_accounting({}) is False
    assert sm.rollback_reply_accounting({"taken_at": 0}) is False


# ── 静态接线断言（handler 闭包拿不到句柄，按仓库惯例钉源码结构） ──────────


def _client_src() -> str:
    return (ENGINE_ROOT / "src" / "client" / "telegram_client.py").read_text(
        encoding="utf-8", errors="replace")


def _sm_src() -> str:
    return (ENGINE_ROOT / "src" / "skills" / "skill_manager.py").read_text(
        encoding="utf-8", errors="replace")


def test_wiring_defer_called_on_both_cooldown_gates():
    """两处冷却吞没点（client 回复逻辑闸 / skill 冷却信号）都必须接补答调度。"""
    src = _client_src()
    assert 'reason="reply_logic_cooldown"' in src
    assert 'reason="skill_cooldown"' in src
    # skill 侧信号消费必须在 process_message 返回 None 后（不误伤刻意不回）
    m = re.search(
        r"reply_text = await self\.skill_manager\.process_message"
        r"[\s\S]{0,900}?consume_reply_skip",
        src)
    assert m, "process_message 返回后必须消费 consume_reply_skip 信号"


def test_wiring_snapshot_before_generation_and_rollback_in_interject():
    src = _client_src()
    # 快照必须在 process_message 调用之前拍（否则拍到的是写入后状态，回滚失效）
    snap_pos = src.find("snapshot_reply_accounting")
    call_pos = src.find("reply_text = await self.skill_manager.process_message")
    assert 0 < snap_pos < call_pos, "快照必须先于 process_message"
    # interject 全量丢弃闭包内必须回滚（分条部分发出的 interject 不回滚）
    m = re.search(
        r"def _interject_stale_abort\(where: str\) -> bool:"
        r"[\s\S]{0,1600}?rollback_reply_accounting",
        src)
    assert m, "_interject_stale_abort 内必须调用 rollback_reply_accounting"


def test_wiring_skill_skip_note_at_cooldown_site():
    src = _sm_src()
    m = re.search(
        r"_cd_left = self\._cooldown_remaining\("
        r"[\s\S]{0,600}?_note_reply_skip\(",
        src)
    assert m, "skill 冷却拦截点必须记录跳过信号"


def test_update_after_reply_stamp_contract_pinned():
    """rollback 镜像的三个记账写点必须还在 _update_after_reply 里——
    谁挪了写点，rollback 会静默失效，此处先红。"""
    src = _sm_src()
    start = src.find("def _update_after_reply(")
    assert start > 0, "找不到 _update_after_reply 函数"
    nxt = src.find("\n    def ", start)
    body = src[start:nxt if nxt > 0 else start + 8000]
    assert "self.global_last_reply_time = current_time" in body
    assert "self._chat_user_last_reply[" in body
    assert "self.reply_cache[content_hash] = current_time" in body
