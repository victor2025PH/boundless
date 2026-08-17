"""P1-198（2026-08-03）出站语言硬闸门禁。

背景：7/31 的 CJK HOLD 护栏活在出站翻译回调里——``translate.enabled=false`` 的
部署翻译一关、护栏一起消失，错语言草稿对客户裸奔（8/03 事故在这类部署上的
残余风险面）。本批改动：

  1. ``gate_only`` 模式：翻译关闭时仍装配回调，只拦 CJK↔非 CJK 高置信冲突
     （抢救翻译 / HOLD），常规消息原样放行（尊重运营关闭翻译的决定）；
  2. 客户证据缺位（新客户只发过贴纸/语气词）时，用「我们已投递消息的语言」
     （出站历史投票）当独立参照——与生成端决策解耦的地面真相；
  3. 观测：held / rescued / no_target_sent 三桶（rescued 仅 gate_only 计——
     常规翻译模式下 CJK→客户语言是设计内例行路径，计进来会淹没真异常）。
"""

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ai.translation_service import detect_language
from src.inbox.outbound_lang_stats import get_outbound_lang_stats
from src.inbox.outbound_translate import (
    parse_outbound_lang_gate_cfg,
    translate_outbound_text,
    vote_language,
)


class _FakeRes:
    def __init__(self, translated, ok=True, provider="deepl", error=""):
        self.translated_text = translated
        self.ok = ok
        self.provider = provider
        self.error = error


class _FakeTS:
    def __init__(self, res, detect=""):
        self._res = res
        self._detect = detect
        self.calls = []

    def detect_language(self, text):
        return self._detect

    async def translate(self, text, *, target_lang, source_lang, style="chat"):
        self.calls.append((text, target_lang, source_lang, style))
        return self._res


class _FakeStore:
    def __init__(self, language="en"):
        self._language = language
        self.recorded = []

    def get_conversation(self, cid):
        return {"conversation_id": cid, "language": self._language}

    def record_outbound_translation(self, cid, sent, orig, **kw):
        self.recorded.append((cid, sent, orig, kw))
        return True


class _HistStore(_FakeStore):
    """带 list_recent_messages 的 store：驱动投票 + 出站历史参照路径。"""

    def __init__(self, recent, language=""):
        super().__init__(language=language)
        self._recent = recent

    def list_recent_messages(self, cid, *, limit=50):
        return self._recent


@pytest.fixture(autouse=True)
def _reset_stats():
    get_outbound_lang_stats().reset_for_tests()
    yield
    get_outbound_lang_stats().reset_for_tests()


# ── 配置解析 ─────────────────────────────────────────────────────

def test_parse_gate_cfg_default_on_with_kill_switch():
    assert parse_outbound_lang_gate_cfg({})["enabled"] is True
    assert parse_outbound_lang_gate_cfg(None)["enabled"] is True
    off = {"inbox": {"l2_autosend": {"lang_gate": {"enabled": False}}}}
    assert parse_outbound_lang_gate_cfg(off)["enabled"] is False
    off_bool = {"inbox": {"l2_autosend": {"lang_gate": False}}}
    assert parse_outbound_lang_gate_cfg(off_bool)["enabled"] is False


# ── gate_only 模式语义 ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_only_no_conflict_passthrough_zero_translate():
    ts = _FakeTS(_FakeRes("nope"), detect="en")
    out = await translate_outbound_text(
        {"conversation_id": "c0", "text": "see you tonight!"},
        translation_service=ts, store=_FakeStore(language="en"),
        source_lang="zh", gate_only=True)
    assert out == "see you tonight!"
    assert ts.calls == []          # 运营关了翻译：非冲突绝不翻译
    # 中文会话发中文同样放行
    out2 = await translate_outbound_text(
        {"conversation_id": "c0", "text": "好呀，晚点见"},
        translation_service=ts, store=_FakeStore(language="zh"),
        source_lang="zh", gate_only=True)
    assert out2 == "好呀，晚点见" and ts.calls == []


@pytest.mark.asyncio
async def test_gate_only_cjk_fragment_quote_not_conflict():
    """生产实锤误伤面（2026-08-04 首扫）：英文消息引用中文专名（「村BA」×1 字）
    不构成冲突——硬闸放行、零翻译调用、零计数。7/31 锚点（40% CJK）仍拦。"""
    from src.inbox.outbound_translate import cjk_substantial
    frag = "Just read about 「村BA」's grassroots heart, Guangdong is wild!"
    assert cjk_substantial(frag) is False
    assert cjk_substantial("是Steven，别担心。😊") is True     # 7/31 锚点不放松
    assert cjk_substantial("哈哈，你也太逗了。") is True        # 8/03 事故文本
    ts = _FakeTS(_FakeRes("nope"), detect="en")
    out = await translate_outbound_text(
        {"conversation_id": "cf", "text": frag},
        translation_service=ts, store=_FakeStore(language="en"),
        source_lang="zh", gate_only=True)
    assert out == frag and ts.calls == []
    d = get_outbound_lang_stats().dump()
    assert d["held"] == 0 and d["rescued"] == 0 and d["no_target_sent"] == 0


@pytest.mark.asyncio
async def test_conflict_translation_keeping_quoted_name_not_held():
    # 好译文保留专名引用（村BA）≠ 翻译失败：旧口径 contains_cjk(translated)
    # 会把合格译文误 HOLD。
    ts = _FakeTS(_FakeRes("Check out 村BA, it's a viral thing!"), detect="en")
    out = await translate_outbound_text(
        {"conversation_id": "cq", "text": "你去看看村BA吧，特别火！"},
        translation_service=ts, store=_FakeStore(language="en"),
        source_lang="zh", gate_only=True)
    assert out == "Check out 村BA, it's a viral thing!"
    d = get_outbound_lang_stats().dump()
    assert d["rescued"] == 1 and d["held"] == 0


@pytest.mark.asyncio
async def test_gate_only_conflict_rescued_and_counted():
    ts = _FakeTS(_FakeRes("Okay, you're too funny!"), detect="en")
    store = _FakeStore(language="en")
    out = await translate_outbound_text(
        {"conversation_id": "c1", "text": "哈哈，你也太逗了。"},
        translation_service=ts, store=store, source_lang="zh", gate_only=True)
    assert out == "Okay, you're too funny!"
    assert ts.calls and ts.calls[0][1] == "en"
    d = get_outbound_lang_stats().dump()
    assert d["rescued"] == 1 and d["held"] == 0
    assert store.recorded          # 出向译文映射照记（工作台双行展示）


@pytest.mark.asyncio
async def test_gate_only_conflict_translation_dead_holds():
    ts = _FakeTS(_FakeRes("", ok=False), detect="en")
    out = await translate_outbound_text(
        {"conversation_id": "c2", "text": "哈哈，你也太逗了。"},
        translation_service=ts, store=_FakeStore(language="en"),
        source_lang="zh", gate_only=True)
    assert out is None             # HOLD：走投递失败链，绝不发中文给英文客户
    assert get_outbound_lang_stats().dump()["held"] == 1


@pytest.mark.asyncio
async def test_normal_mode_hold_also_counted():
    # identity 回显（译文仍含 CJK）→ HOLD；常规模式同样计 held
    ts = _FakeTS(_FakeRes("是Steven，别担心。"), detect="en")
    out = await translate_outbound_text(
        {"conversation_id": "c3", "text": "是Steven，别担心。😊"},
        translation_service=ts, store=_FakeStore(language="en"), source_lang="zh")
    assert out is None
    d = get_outbound_lang_stats().dump()
    assert d["held"] == 1 and d["rescued"] == 0   # 常规模式成功翻译不算 rescued


# ── 客户证据缺位：出站历史独立参照 ───────────────────────────────

@pytest.mark.asyncio
async def test_target_unknown_uses_outbound_history_reference():
    recent = [
        {"direction": "in", "text": "Haha yes"},                     # 中性不投票
        {"direction": "out", "text": "Sure, sending it over tonight."},
        {"direction": "out", "text": "I will be there at seven."},
    ]
    ts = _FakeTS(_FakeRes("Got it, see you!"), detect="en")
    store = _HistStore(recent, language="")      # 客户语言双缺位
    out = await translate_outbound_text(
        {"conversation_id": "c4", "text": "好呀，到时见。"},
        translation_service=ts, store=store, source_lang="zh", gate_only=True)
    assert out == "Got it, see you!"             # 参照=我们一直在说英文 → 救回
    assert get_outbound_lang_stats().dump()["rescued"] == 1


@pytest.mark.asyncio
async def test_target_unknown_no_reference_blind_send_counted():
    ts = _FakeTS(_FakeRes("x"), detect="")
    store = _HistStore([{"direction": "in", "text": "😊"}], language="")
    out = await translate_outbound_text(
        {"conversation_id": "c5", "text": "你好呀朋友"},
        translation_service=ts, store=store, source_lang="zh", gate_only=True)
    assert out == "你好呀朋友"                     # 零参照 → 放行（可能真是中文客户）
    assert ts.calls == []
    assert get_outbound_lang_stats().dump()["no_target_sent"] == 1


def test_vote_language_direction_out():
    msgs = [
        {"direction": "in", "text": "你在做什么呀今天"},
        {"direction": "out", "text": "Sure, sending it over tonight."},
        {"direction": "out", "text": "I will be there at seven."},
    ]
    assert vote_language(msgs, detect=detect_language) == "zh"
    assert vote_language(msgs, detect=detect_language, direction="out") == "en"


# ── 观测与装配 ───────────────────────────────────────────────────

def test_stats_dump_prom_shape():
    st = get_outbound_lang_stats()
    st.record("held", conversation_id="telegram:acc:123", target="en")
    st.record("bogus_outcome")     # 未知桶静默忽略
    prom = st.dump_prom()
    assert 'outbound_lang_gate_total{outcome="held"} 1' in prom
    assert 'outbound_lang_gate_total{outcome="rescued"} 0' in prom
    d = st.dump()
    assert d["last_events"][0]["outcome"] == "held"
    assert d["last_events"][0]["target"] == "en"


def _assistant(cfg):
    return SimpleNamespace(
        config=SimpleNamespace(config=cfg),
        logger=logging.getLogger("t"), inbox_store=None)


def test_cb_builder_gate_only_mode():
    from src.inbox.autosend_helpers import build_autosend_translate_cb
    web_app = SimpleNamespace(state=SimpleNamespace(translation_service=None))
    # 翻译关 + 硬闸默认开 → gate-only 回调
    cb = build_autosend_translate_cb(
        _assistant({"inbox": {"l2_autosend": {"translate": {"enabled": False}}}}),
        web_app)
    assert cb is not None and getattr(cb, "gate_only", None) is True
    # 两者都关 → None（旧行为）
    cb_off = build_autosend_translate_cb(
        _assistant({"inbox": {"l2_autosend": {
            "translate": {"enabled": False},
            "lang_gate": {"enabled": False}}}}),
        web_app)
    assert cb_off is None
    # 翻译开 → 常规模式（gate_only=False）
    cb_tx = build_autosend_translate_cb(
        _assistant({"inbox": {"l2_autosend": {"translate": {"enabled": True}}}}),
        web_app)
    assert cb_tx is not None and cb_tx.gate_only is False


# ── 复盘扫描 CLI 的判定核心 ──────────────────────────────────────

def _load_scan_tool():
    import importlib.util
    p = Path(__file__).resolve().parent.parent / "tools" / "scan_outbound_lang_mismatch.py"
    spec = importlib.util.spec_from_file_location("scan_outbound_lang_mismatch_t", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scan_classifier_incident_conversation():
    mod = _load_scan_tool()
    rows = [
        {"direction": "in", "text": "can you send me the script", "ts": 1},
        {"direction": "out",
         "text": "Haha, she's lucky I'm such a devoted employee.", "ts": 2},
        {"direction": "in", "text": "Haha 🤣（表情：笑得满地打滚）", "ts": 3},
        {"direction": "out",
         "text": "哈哈，你也太逗了。她大概在笑我自己笑自己的梗吧。", "ts": 4},
        {"direction": "in", "text": "I wanner be that cat", "ts": 5},
    ]
    res = mod.classify_conversation(rows)
    assert res is not None and res["expected"] == "en"
    assert len(res["mismatches"]) == 1            # 只有那条错发的中文
    assert "你也太逗了" in res["mismatches"][0]["snippet"]


def test_scan_classifier_zh_customer_skipped():
    mod = _load_scan_tool()
    rows = [
        {"direction": "in", "text": "你在做什么呀今天", "ts": 1},
        {"direction": "out", "text": "在想你呀", "ts": 2},
    ]
    assert mod.classify_conversation(rows) is None  # 中文客户不在本口径


def test_scan_classifier_placeholders_not_flagged():
    mod = _load_scan_tool()
    rows = [
        {"direction": "in", "text": "how are you doing today", "ts": 1},
        {"direction": "out", "text": "[图片] 一张海边自拍", "ts": 2},
        {"direction": "out", "text": "[语音]×2", "ts": 3},
    ]
    res = mod.classify_conversation(rows)
    assert res is not None and res["expected"] == "en"
    assert res["mismatches"] == []


def test_scan_classifier_cjk_fragment_quote_not_flagged():
    # 2026-08-04 生产首扫实锤：英文新闻分享引用「村BA」被旧口径标成错配。
    mod = _load_scan_tool()
    rows = [
        {"direction": "in", "text": "how are you doing today", "ts": 1},
        {"direction": "out",
         "text": "Just read about 「村BA」's grassroots heart, Guangdong is wild!",
         "ts": 2},
    ]
    res = mod.classify_conversation(rows)
    assert res is not None and res["mismatches"] == []
