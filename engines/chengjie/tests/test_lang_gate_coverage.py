"""P3-198（2026-08-04）语言防线路径覆盖 + 投诉观测门禁。

背景：8/03 事故（L2 链）修复后，30 天生产扫描又抓到主动触达直发链漏网
（Yasmin 6 条中文晨安）——「修一条链」不等于「修完」。本批把**全部自动出站
文本路径**逐一接入同一个语言闸，并加上「客户已被伤到」的结果面观测：

  已接闸：L2 autosend / 人工通过投递（P1）、proactive 直发（P2）、
          protocol_autoreply 协议号自动回复（本批）、deferred 队列
          care/reactivation（本批，HOLD→DeferredSenderNotReady 推后重试）；
  证据口径消费方：holding 缓冲话术兜底语言（本批，裸 detect → peer_language_hint）；
  刻意不闸（有据）：friend_welcome（运营策划的多语模板池，非 LLM 生成）、
          A 线媒体配文（低风险，语音链另有 effective_clone_language 评测兜底）、
          坐席手动发送（人的明示决定）。

闭包函数不可直测 → 源码级接线不变量（wiring 门禁族风格）；投诉检测为纯函数直测。
"""

import sqlite3
import time
from pathlib import Path

import pytest

from src.inbox.inbound_enrich import (
    apply_inbound_enrichments,
    build_language_recovery_hint,
    detect_language_complaint,
)
from src.inbox.outbound_lang_stats import get_outbound_lang_stats

_ROOT = Path(__file__).resolve().parent.parent

# 8/03 事故原文（真实语料金标）
_A_WRONG_ZH = "哈哈，你也太逗了。她大概在笑我自己笑自己的梗吧。"


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_stats():
    get_outbound_lang_stats().reset_for_tests()
    yield
    get_outbound_lang_stats().reset_for_tests()


# ── 路径覆盖：源码级接线不变量 ──────────────────────────────────────

def test_protocol_autoreply_send_gated_before_voice():
    src = _src("src/integrations/protocol_autoreply.py")
    body = src[src.index("async def _send(*"):src.index("async def hook(")]
    assert "translate_outbound_text" in body
    assert "gate_only=not _otx.get" in body
    assert "lang_gate_hold" in body                      # HOLD → 抛错走失败链
    # 闸必须在语音分支之前（语音稿与文本同源，闸后语音念的就是客户语言）
    assert body.index("translate_outbound_text") < body.index("autosend_voice")


def test_deferred_translate_hook_has_gate_only_and_hold():
    src = _src("main.py")
    seg = src[src.index("async def _maybe_translate_outbound"):]
    seg = seg[:seg.index("def _enqueue_deferred_outbox")]
    assert "parse_outbound_lang_gate_cfg" in seg
    assert "gate_only=not cfg.get" in seg
    bt = _src("src/bootstrap/background_tasks.py")
    sender = bt[bt.index("async def _universal_send"):]
    sender = sender[:sender.index("# 1) 编排器受管 worker")]
    assert "if text is None" in sender
    assert 'DeferredSenderNotReady("lang_gate_hold")' in sender


def test_holding_reply_fallback_uses_evidence_hint():
    src = _src("src/inbox/autodraft_helpers.py")
    seg = src[src.index("async def _maybe_holding_after_enrich"):]
    seg = seg[:seg.index("await maybe_send_holding_reply")]
    assert "peer_language_hint" in seg
    # 旧的「裸检测单条原话」兜底不得回潮
    assert "detect_language as _dl" not in seg


# ── 语言投诉/困惑检测（纯函数） ─────────────────────────────────────

def _hist(*rows):
    return [{"role": r, "content": c} for r, c in rows]


def test_complaint_detected_after_wrong_language_message():
    hist = _hist(("user", "can you send me the script"),
                 ("assistant", _A_WRONG_ZH))
    assert detect_language_complaint("??", hist) == "confusion"
    assert detect_language_complaint("English please??", hist) == "confusion"
    assert detect_language_complaint("i can't understand this", hist) == "confusion"


def test_complaint_reverse_direction_zh_customer():
    # 反向：中文客户收到我方英文 → 「看不懂」同样命中
    hist = _hist(("user", "你在做什么呀今天"),
                 ("assistant", "Sorry I was busy earlier, what's up my friend?"))
    assert detect_language_complaint("看不懂你发的什么", hist) == "confusion"


def test_complaint_detail_exposes_customer_lang_for_by_target():
    # P2-7：complaint 按客户语种分桶——detail 返回客户实际语种，未命中为空
    from src.inbox.inbound_enrich import language_complaint_detail
    from src.inbox.outbound_lang_stats import OutboundLangStats
    hist = _hist(("user", "can you send me the script"),
                 ("assistant", _A_WRONG_ZH))
    assert language_complaint_detail("English please??", hist) == ("confusion", "en")
    hist_zh = _hist(("user", "你在做什么呀今天"),
                    ("assistant", "Sorry I was busy earlier, what's up my friend?"))
    v, lang = language_complaint_detail("看不懂你发的什么", hist_zh)
    assert v == "confusion" and lang.startswith("zh")
    assert language_complaint_detail("i can't understand why she left",
                                     _hist(("assistant", "ok see you"))) == ("", "")
    st = OutboundLangStats()
    st.record("complaint", conversation_id="whatsapp:a:x", target="EN")
    st.record("complaint", conversation_id="whatsapp:a:y", target="")
    d = st.dump()
    assert d["complaint"] == 2 and d["by_target"]["en"]["complaint"] == 1
    assert "outbound_lang_gate_target_total{outcome=\"complaint\",target=\"en\"} 1" in st.dump_prom()


def test_complaint_requires_mismatch_context():
    # 语言一致 → 同样的句式绝不误报（说的是内容不是语言）
    hist_en = _hist(("user", "she left without a word"),
                    ("assistant", "Oh no, what happened between you two?"))
    assert detect_language_complaint("i can't understand why she left", hist_en) == ""
    hist_zh = _hist(("user", "你在做什么呀今天"), ("assistant", "在想你呀，怎么啦"))
    assert detect_language_complaint("看不懂她为什么走", hist_zh) == ""
    assert detect_language_complaint("？？", hist_zh) == ""


def test_complaint_needs_assistant_evidence():
    assert detect_language_complaint("??", _hist(("user", "hello there"))) == ""
    assert detect_language_complaint("", None) == ""


# ── 恢复话术注入（端到端经 apply_inbound_enrichments） ──────────────

def test_recovery_hint_injected_and_counted():
    uc: dict = {"chat_id": "tg:1"}
    hist = _hist(("user", "can you send me the script"),
                 ("assistant", _A_WRONG_ZH))
    apply_inbound_enrichments(
        uc, text="??", history=hist, reply_lang="en", platform="telegram")
    assert "语言致歉" in uc.get("_topic_switch_hint", "")
    assert get_outbound_lang_stats().dump()["complaint"] == 1


def test_recovery_hint_suppressed_when_working_language_pinned():
    # 运营钉死草稿语言（mismatch hint 在场）→ 只观测不注入「改语言重说」
    # （两条提示会打架：一条说按设定写中文、一条说改用对方语言）。
    uc: dict = {"chat_id": "tg:2"}
    hist = _hist(("user", "你在做什么呀今天"),
                 ("assistant", "Sorry I was busy earlier, what's up my friend?"))
    apply_inbound_enrichments(
        uc, text="看不懂你发的什么英文", history=hist,
        reply_lang="en", platform="telegram")
    hint = uc.get("_topic_switch_hint", "")
    assert "工作语言说明" in hint            # mismatch hint 在场
    assert "语言致歉" not in hint            # 恢复话术让位
    assert get_outbound_lang_stats().dump()["complaint"] == 1   # 但照样计数


def test_recovery_hint_text_contract():
    h = build_language_recovery_hint()
    assert "道歉" in h and "对方的语言" in h
    assert "辩解" in h                       # 明令禁止辩解


# ── 扫描工具趋势分桶（scan_db 契约，临时 sqlite） ────────────────────

def test_scan_db_by_day_buckets(tmp_path):
    import importlib.util
    p = _ROOT / "tools" / "scan_outbound_lang_mismatch.py"
    spec = importlib.util.spec_from_file_location("scan_lm_cov", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    db = tmp_path / "inbox.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE conversations (conversation_id TEXT, platform TEXT,"
                " display_name TEXT)")
    con.execute("CREATE TABLE messages (conversation_id TEXT, direction TEXT,"
                " text TEXT, ts REAL)")
    now = time.time()
    con.execute("INSERT INTO conversations VALUES ('c1','telegram','Yasmin')")
    rows = [
        ("c1", "in", "good morning! how are you today", now - 3600),
        ("c1", "out", _A_WRONG_ZH, now - 3000),
        ("c1", "out", "Sure, sending it over tonight.", now - 2000),
    ]
    con.executemany("INSERT INTO messages VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()

    rep = mod.scan_db(db, now - 86400)
    s = rep["summary"]
    assert s["mismatch_rows"] == 1 and s["out_rows_checked"] == 2
    today = time.strftime("%Y-%m-%d", time.localtime(now - 3000))
    assert s["by_day"][today]["mismatch_rows"] == 1
    assert s["by_day"][today]["out_rows"] == 2


def test_scan_db_judges_silent_contact_via_out_of_window_inbound(tmp_path):
    """首周批实锤盲区：被主动触达打扰却没回话的「沉默客户」，短窗内零入站证据
    → 旧口径整会话被跳过（Yasmin 6 条中文晨安在 7 天窗下隐身、KPI 假 0）。
    新口径：客户语言用不限窗口的最近入站（会话属性），出站只数窗口内。"""
    import importlib.util
    p = _ROOT / "tools" / "scan_outbound_lang_mismatch.py"
    spec = importlib.util.spec_from_file_location("scan_lm_silent", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    db = tmp_path / "inbox.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE conversations (conversation_id TEXT, platform TEXT,"
                " display_name TEXT)")
    con.execute("CREATE TABLE messages (conversation_id TEXT, direction TEXT,"
                " text TEXT, ts REAL)")
    now = time.time()
    con.execute("INSERT INTO conversations VALUES ('c1','telegram','Yasmin')")
    con.executemany("INSERT INTO messages VALUES (?,?,?,?)", [
        ("c1", "in", "good morning! how are you today", now - 30 * 86400),
        ("c1", "out", "今早的风挺舒服，想着跟你说声早安。", now - 3600),
    ])
    con.commit()
    con.close()

    rep = mod.scan_db(db, now - 7 * 86400)
    s = rep["summary"]
    assert s["convs_judged"] == 1
    assert s["mismatch_rows"] == 1 and s["mismatch_convs"] == 1
