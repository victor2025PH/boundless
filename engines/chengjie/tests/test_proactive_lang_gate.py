"""P2-198（2026-08-04）主动触达语言闸门禁。

生产实锤（tools/scan_outbound_lang_mismatch.py 30 天首扫）：英文客户
telegram:8244899… 收到 **6 条中文晨安**（最近 08-03 07:56「今早的风挺舒服，
想着跟你说声早安。」）。双层根因：

  1. 生成侧：``_peer_language`` 只读 ``conversations.language`` 持久列，而
     Telegram ingest **从不写该列**（恒 'unknown'）→ ``build_proactive_prompt``
     的语言硬约束块因 ``lang != "unknown"`` 判定被**整块跳过**——约束从未生效；
  2. 投递侧：主动触达直发 ``orch.send``，不经 AutosendWorker 翻译回调——
     L2 链修好后这条链仍在裸奔。

修复：生成侧 ``peer_language_hint``（证据投票 → 持久列 → 出站历史参照，与
出站翻译/硬闸同一剥离口径）；投递侧 ``_send`` 在文案定稿点接
``translate_outbound_text``（照片配文/语音稿/文本三分支同源，一处全守），
HOLD → mark_attempt 放弃本轮 + ``lang_gate_blocks`` 计数。
"""

from pathlib import Path

from src.ai.translation_service import detect_language
from src.inbox.outbound_translate import peer_language_hint

_PT_SRC = (Path(__file__).resolve().parent.parent
           / "src" / "companion" / "proactive_topic.py").read_text(encoding="utf-8")


class _Store:
    def __init__(self, recent=None, language=""):
        self._recent = list(recent or [])
        self._language = language

    def list_recent_messages(self, cid, *, limit=50):
        return self._recent

    def get_conversation(self, cid):
        return {"conversation_id": cid, "language": self._language}


def _in(text):
    return {"direction": "in", "text": text}


def _out(text):
    return {"direction": "out", "text": text}


# ── peer_language_hint：三层证据 ────────────────────────────────────

def test_hint_inbound_evidence_first():
    st = _Store(recent=[_in("你在做什么呀今天")], language="en")
    assert peer_language_hint(st, "c1", detect=detect_language) == "zh"


def test_hint_falls_to_persisted_language():
    st = _Store(recent=[_in("Haha yes"), _in("😂")], language="en")  # 中性无证据
    assert peer_language_hint(st, "c1", detect=detect_language) == "en"


def test_hint_unknown_column_falls_to_outbound_history():
    # Yasmin 形态：持久列恒 unknown（TG ingest 从不写）+ 入站全中性 →
    # 参照「我们一直在用什么语言聊」。
    st = _Store(
        recent=[
            _in("Haha yes"),
            _out("Good morning! Hope your day goes easy."),
            _out("Sure, sending it over tonight."),
        ],
        language="unknown")
    assert peer_language_hint(st, "c1", detect=detect_language) == "en"


def test_hint_all_blank_returns_empty():
    st = _Store(recent=[_in("😂")], language="unknown")
    assert peer_language_hint(st, "c1", detect=detect_language) == ""
    assert peer_language_hint(None, "c1", detect=detect_language) == ""


def test_hint_own_wrong_language_history_cannot_override_inbound():
    # Yasmin 关键性质：她的出站历史里躺着 6 条错发的中文晨安——出站参照层
    # 绝不能把自己犯过的错反向锁死成「会话语言=中文」。入站证据（en）优先，
    # 出站参照只在客户侧零证据时才被咨询。
    st = _Store(
        recent=[
            _out("今早的风挺舒服，想着跟你说声早安。"),
            _in("I wanner be that cat"),
            _out("早安呀，今天也要加油哦。"),
        ],
        language="unknown")
    assert peer_language_hint(st, "c1", detect=detect_language) == "en"


# ── 接线不变量（闭包函数不可直测 → 源码级钉住，风格同 wiring 门禁族）──

def _send_body() -> str:
    start = _PT_SRC.index("async def _send(plan):")
    end = _PT_SRC.index("def _on_teaser_sent")
    return _PT_SRC[start:end]


def test_send_wired_through_lang_gate_before_all_branches():
    body = _send_body()
    assert "_guard_outbound_language(" in body
    # 闸必须在照片/语音/文本三条发送分支**之前**（配文/语音稿/正文同源）
    gate_at = body.index("_guard_outbound_language(")
    assert gate_at < body.index("_try_send_photo(")
    assert gate_at < body.index("_try_send_voice(")
    assert gate_at < body.index("orch.send(")
    # HOLD → 记 attempt（不烧冷却）+ 计数 + 放弃本轮
    assert "mark_attempt" in body[gate_at:body.index("_try_send_photo(")]
    assert "record_lang_gate_block" in body


def test_peer_language_uses_evidence_hint():
    start = _PT_SRC.index("def _peer_language(plan)")
    end = _PT_SRC.index("async def _guard_outbound_language")
    seg = _PT_SRC[start:end]
    assert "peer_language_hint" in seg
    # 旧的「裸读持久列直接 return」模式不得回潮
    assert 'return str(conv.get("language")' not in seg


def test_guard_uses_shared_translate_entry_with_gate_only():
    start = _PT_SRC.index("async def _guard_outbound_language")
    end = _PT_SRC.index("def _persona_style")
    seg = _PT_SRC[start:end]
    assert "translate_outbound_text" in seg
    assert "gate_only=not _otx.get" in seg     # translate 关闭 → gate-only 语义
    assert "parse_outbound_lang_gate_cfg" in seg


# ── proactive_stats 计数 ────────────────────────────────────────────

def test_lang_gate_block_counter_roundtrip():
    from src.companion import proactive_stats as ps
    before = ps.metrics_snapshot().get("lang_gate_blocks", 0)
    ps.record_lang_gate_block()
    after = ps.metrics_snapshot().get("lang_gate_blocks", 0)
    assert after == before + 1
