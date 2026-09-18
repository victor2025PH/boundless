"""矛盾注入清理（2026-09-18 事故沉淀）：

1. 已发媒体事实：B 线 ``_media_sent_log`` 恒空 → 以 inbox 出站媒体行为真相注入
   （实录：inbox 有我方出站图片行，AI 却说「从来没给你发过照片」）；
2. inside_jokes 质量闸：索要媒体 / 质疑 AI / 寒暄不算梗（实录：「给我/照片/语音/你是ai」被当默契注入）；
   读侧 ``clean_inside_jokes`` 把库里累积的垃圾也挡在 prompt 外；
3. B 线陪聊域 KB 闸：闲聊意图 + 无业务词 → 不注 KB（与 A 线同口径）。
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from src.companion.deep_persona import (
    clean_inside_jokes, detect_recurring_phrases, format_inside_jokes,
)
from src.inbox.media_ledger import (
    apply_media_ledger_hint, build_media_ledger_block, media_ledger_from_store,
    summarize_outbound_media, wants_media_history,
)

DAY = 86400.0


# ── 1. 已发媒体事实 ────────────────────────────────────────────────────────────

def test_wants_media_history_wordlist():
    assert wants_media_history("你发过照片给我吗")
    assert wants_media_history("上次那张图呢")
    assert wants_media_history("can you send me a selfie?")
    assert not wants_media_history("今天好累")
    assert not wants_media_history("")


def test_summarize_counts_only_outbound_media():
    now = time.time()
    rows = [
        {"direction": "in", "text": "[图片]", "media_type": "image", "ts": now - 5 * DAY},
        {"direction": "out", "text": "[我方发出的图片] 刚拍的咖啡", "media_type": "image", "ts": now - 4 * DAY},
        {"direction": "out", "text": "语音稿", "media_type": "voice", "ts": now - 3 * DAY},
        {"direction": "out", "text": "看这个", "media_type": "photo", "ts": now - 2 * DAY},
        {"direction": "out", "text": "纯文字", "media_type": "", "ts": now - DAY},
    ]
    s = summarize_outbound_media(rows)
    assert (s["image"], s["voice"], s["video"]) == (2, 1, 0)
    assert s["last_kind"] == "image" and s["last_caption"] == "看这个"
    assert abs(s["first_ts"] - (now - 4 * DAY)) < 1 and abs(s["last_ts"] - (now - 2 * DAY)) < 1


def test_block_affirms_when_sent_and_denies_when_never():
    now = time.time()
    b = build_media_ledger_block({"image": 2, "voice": 1, "video": 0, "last_ts": now - 2 * DAY,
                                  "last_kind": "image", "last_caption": "看这个"}, now=now)
    assert "照片 2 张" in b and "语音 1 条" in b and "不要否认" in b and "「看这个」" in b
    b0 = build_media_ledger_block({"image": 0, "voice": 0, "video": 0})
    assert "还没有" in b0 and "别说「上次发给你那张」" in b0


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def list_recent_messages(self, cid, *, limit=50, include_deleted=True):
        return self.rows[-limit:]


def test_media_ledger_from_store_gates_on_relevance():
    now = time.time()
    st = _Store([{"direction": "out", "text": "x", "media_type": "image", "ts": now - DAY}])
    assert "照片 1 张" in media_ledger_from_store(st, "c1", "你发过照片吗", now=now)
    assert media_ledger_from_store(st, "c1", "今天好累", now=now) == ""
    assert media_ledger_from_store(None, "c1", "你发过照片吗", now=now) == ""


def test_apply_media_ledger_hint_ignores_local_sent_log():
    """A 线 _media_sent_log 非空也必须以 inbox 为准（坐席手发 / B 线发出站不在本进程账本里）。"""
    now = time.time()
    st = _Store([{"direction": "out", "text": "刚拍的", "media_type": "image", "ts": now - DAY}])
    ctx = {"_media_sent_log": [{"kind": "image", "ts": now}]}
    block = apply_media_ledger_hint(ctx, st, "c1", "你发过照片吗", now=now)
    assert "照片 1 张" in block and "【已发媒体事实】" in ctx["_topic_switch_hint"]
    assert apply_media_ledger_hint(ctx, st, "c1", "你发过照片吗", now=now) == ""  # 不叠
    assert apply_media_ledger_hint(ctx, st, "c1", "今天好累", now=now) == ""


async def _make_cm(tmp_path: Path):
    cfg = {
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "intent": {"keywords": {}, "patterns": {}},
        "reply": {},
        "context_store": {"ttl_days": 30},
        "memory": {"enabled": False},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text("greeting: hi\n", encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text("channels: {}\n", encoding="utf-8")
    from src.utils.config_manager import ConfigManager
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    await cm.load()
    return cm


@pytest.mark.asyncio
async def test_inbox_draft_injects_media_ledger_from_store(tmp_path, monkeypatch):
    from src.skills.skill_manager import SkillManager
    import src.integrations.protocol_bridge as pb
    now = time.time()
    st = _Store([
        {"direction": "out", "text": "刚拍的", "media_type": "image", "ts": now - 3 * DAY},
        {"direction": "in", "text": "你发过照片给我吗", "ts": now - 30},
    ])
    monkeypatch.setattr(pb, "get_inbox_store", lambda: st)
    cm = await _make_cm(tmp_path)
    ai = MagicMock()
    ai.generate_reply_with_intent = AsyncMock(return_value="发过呀")
    sm = SkillManager(cm, ai)
    await sm.generate_inbox_draft(
        text="你发过照片给我吗", chat_key="u1", platform="telegram", conversation_id="c1",
        history=[{"role": "assistant", "content": "刚拍的", "media": "image", "ts": now - 3 * DAY},
                 {"role": "user", "content": "你发过照片给我吗", "ts": now - 30}])
    ctx = ai.generate_reply_with_intent.await_args.kwargs["user_context"]
    tsh = str(ctx.get("_topic_switch_hint") or "")
    assert "【已发媒体事实】" in tsh and "照片 1 张" in tsh and "不要否认" in tsh


# ── 2. inside_jokes 质量闸 ─────────────────────────────────────────────────────

def test_recurring_phrases_ignore_requests_and_ai_suspicion():
    msgs = ["给我发张照片", "给我照片", "发语音给我", "你是ai吗", "你是ai", "给我照片好吗",
            "小笨蛋今天也要加油", "小笨蛋你在干嘛", "小笨蛋想吃火锅"]
    out = detect_recurring_phrases(msgs, min_count=3, top_k=5)
    assert "小笨蛋" in out
    assert not any(("给我" in j or "照片" in j or "语音" in j or "ai" in j.lower()) for j in out)


def test_recurring_phrases_contract_unchanged_for_real_jokes():
    # 既有契约（漂移守卫 / 画像巩固依赖）：2 字词 ≥ min_count 即算
    assert "撸串" in detect_recurring_phrases(["我们去撸串吧", "今晚撸串不", "又想撸串了"], min_count=3)


def test_clean_inside_jokes_filters_persisted_junk():
    junk = ["给我", "照片", "语音", "你是ai", "小笨蛋", "老地方见", "hello", "x"]
    assert clean_inside_jokes(junk) == ["小笨蛋", "老地方见"]
    assert format_inside_jokes(["给我", "照片"]) == ""
    assert "「小笨蛋」" in format_inside_jokes(["给我", "小笨蛋"])


# ── 3. B 线陪聊域 KB 闸 ────────────────────────────────────────────────────────

def test_companion_kb_skip_helper(tmp_path):
    from src.skills.skill_manager import SkillManager

    class _Cfg:
        def __init__(self, config):
            self.config = config

    sm = SkillManager.__new__(SkillManager)
    sm.config = _Cfg({"domain": "conversion"})
    assert sm._companion_kb_should_skip("small_talk", "今天好累") is True
    assert sm._companion_kb_should_skip("small_talk", "帮我查一下订单") is False     # 业务词放行
    assert sm._companion_kb_should_skip("channel_info", "今天好累") is False        # 非闲聊意图放行
    # 支付域（插件开着）放行
    sm.config = _Cfg({"domain": "payment", "plugins": {"payment": {"enabled": True}}})
    from src.utils.domain_policy import effective_domain_name
    if effective_domain_name(sm.config.config) == "payment":
        assert sm._companion_kb_should_skip("small_talk", "今天好累") is False
