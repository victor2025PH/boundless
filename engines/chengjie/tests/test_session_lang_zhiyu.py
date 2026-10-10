"""P1-5 会话语言识别（2026-10-08 智语）：tl / Taglish / 宿务补强、会话投票、实时补写、dry-run 回填。

173 实测 unknown 46%、tl 只有 1 个会话。这里钉住：
- 日常 Taglish / 宿务入站判 tl（宿务带 variant=ceb），英文、印尼文、中文夹英文词不误判；
- 「OK 翻语言」护栏不变（极短英文 / 客套词仍 unknown），但「po」「Sige po」算证据；
- 新入站带可信语种、会话还是 unknown → 实时补写；已有值不覆写；
- 回填脚本默认只读打开 inbox.db。``--apply`` 才补 unknown/空语言，不改已有的 en→tl 建议。
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from src.inbox.normalizer import detect_inbound_language
from src.inbox.session_lang import refine_latin_language, tl_ceb_signal, vote_session_language

ENGINE = Path(__file__).resolve().parents[1]

TL = [
    "Pwede po ba ma-refund yung order ko?",
    "Magkano po yung shipping papuntang Cebu?",
    "Sige po, salamat",
    "Saan po kayo located?",
    "Ano po requirements para mag-register?",
    "Hindi pa po dumarating yung parcel ko",
    "Kailan po ang next restock nyo?",
    "Gusto ko po sana mag-order ng dalawa",
    "Pasensya na po, ngayon lang nakapag-reply",
    "May promo po ba kayo ngayon?",
    "Okay lang po, bukas na lang",
    "Paano po magbayad through GCash?",
    "Nasaan na po yung tracking number?",
    "Wala pa po akong natatanggap na code",
    "Grabe ang bilis naman ng delivery, salamat!",
    "Kuya, pwede pa-check ng order ko?",
    "Ate san po pwede mag-pickup?",
    "Di ko po alam paano mag-login",
    "Ano ba yan, mali yung size na dumating",
    "Ingat po kayo palagi",
    "Thank you po",
    "Ok po",
    "Sige",
    "Magkano?",
    "Salamat po sa tulong ninyo",
    "Pakisuyo po i-cancel yung order",
    "Medyo mahal po pala, may discount ba?",
    "Nagbayad na po ako kanina",
    "Yung blue po sana kung meron",
    "Pwede po COD?",
]
CEB = [
    "Unsa man ni?",
    "Salamat kaayo",
    "Dili ko ganahan ana",
    "Asa man mo dapit?",
    "Pila ni tanan?",
    "Naa pa ba mo stock karon?",
    "Palihug ko check sa akong order",
    "Ngano wala pa man niabot akong parcel?",
]
EN = [
    "How much is the shipping fee to Cebu?",
    "Can you send it today?",
    "I ate already, thanks",
    "Hindi movies are my favorite",
    "Please cancel my order",
    "Do you have this in size M?",
    "What time do you open tomorrow?",
    "I paid via bank transfer this morning",
    "The parcel arrived but the box was damaged",
    "Could you send me the tracking number please",
    "Is there a warranty for this product?",
    "My friend recommended your shop",
]
ID = [
    "Terima kasih ya, barangnya sudah sampai",
    "Saya mau pesan dua",
    "Bisa kirim hari ini?",
    "Apa masih ada stok?",
]
ZH_MIX = ["我想问price", "这个GCash可以吗", "订单号发你了 order 123", "请问可以用PayPal付款吗"]


@pytest.mark.parametrize("text", TL)
def test_taglish_inbound_is_tl(text):
    assert detect_inbound_language(text) == "tl", (text, tl_ceb_signal(text))


@pytest.mark.parametrize("text", CEB)
def test_bisaya_is_tl_with_ceb_variant(text):
    assert detect_inbound_language(text) == "tl"
    assert refine_latin_language(text, "en") == ("tl", "ceb"), (text, tl_ceb_signal(text))


@pytest.mark.parametrize("text", EN)
def test_english_stays_english(text):
    assert detect_inbound_language(text) == "en", (text, tl_ceb_signal(text))


@pytest.mark.parametrize("text", ID)
def test_indonesian_not_tl(text):
    assert detect_inbound_language(text) != "tl", (text, tl_ceb_signal(text))


@pytest.mark.parametrize("text", ZH_MIX)
def test_chinese_with_english_words_is_zh(text):
    assert detect_inbound_language(text) == "zh"


def test_ok_guard_unchanged():
    for t in ("OK", "ok", "Okay!", "kk", "yes", "thx", "Hi~", "haha", "go", "12345"):
        assert detect_inbound_language(t) == "unknown", t
    assert detect_inbound_language("po") == "tl"


def test_tl_substring_traps_fixed():
    # 旧全局线索按子串：id 的 "saya" 命中 masaya、"halo" 命中 halos
    assert detect_inbound_language("masaya ako ngayon") == "tl"


def test_vote_taglish_session_with_english_lines():
    v = vote_session_language([
        "Hello, I want to ask about the bundle promo for this month",
        "Magkano po yung bundle?",
        "Can I pay via GCash?",
        "Sige po",
    ])
    assert v["lang"] == "tl" and v["evidence"] == "inbound_vote"


def test_vote_zh_session_with_english_words():
    v = vote_session_language(["你好，请问这个多少钱", "ok", "可以用PayPal吗", "Thanks"])
    assert v["lang"] == "zh"


def test_vote_short_tl_messages_aggregate():
    v = vote_session_language(["ok po", "sige", "salamat"])
    assert v["lang"] == "tl"


def test_vote_no_evidence():
    assert vote_session_language([])["evidence"] == "no_inbound_text"
    v = vote_session_language(["ok", "👍", "haha"])
    assert v["lang"] == "unknown" and v["evidence"] == "insufficient"


def test_vote_ceb_variant():
    v = vote_session_language(["Unsa man ni?", "Pila ni tanan?", "Salamat kaayo"])
    assert v["lang"] == "tl" and v["variant"] == "ceb"


# ── 实时补写（store）────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    from src.inbox.store import InboxStore
    s = InboxStore(tmp_path / "inbox.db")
    yield s
    try:
        s.close()
    except Exception:
        pass


def _conv(cid, lang="unknown"):
    from src.inbox.models import InboxConversation
    return InboxConversation(conversation_id=cid, platform="whatsapp", account_id="a",
                             chat_key=cid.split(":")[-1], language=lang, last_ts=1.0)


def _msg(cid, text, direction="in", lang=None, ts=10.0):
    from src.inbox.models import InboxMessage
    return InboxMessage(conversation_id=cid, direction=direction, text=text,
                        source_lang=lang if lang is not None else detect_inbound_language(text), ts=ts)


def test_inbound_fills_unknown_conversation_language(store):
    cid = "whatsapp:a:639000000001"
    store.upsert_conversation(_conv(cid))
    assert store.get_conversation(cid)["language"] == "unknown"
    store.ingest_message(_msg(cid, "Hi", ts=5.0))  # trivial → unknown，不补
    assert store.get_conversation(cid)["language"] == "unknown"
    store.ingest_message(_msg(cid, "Pwede po ba COD?", ts=6.0))
    assert store.get_conversation(cid)["language"] == "tl"
    store.ingest_message(_msg(cid, "Please cancel my order", ts=7.0))  # 只补 unknown，不覆写
    assert store.get_conversation(cid)["language"] == "tl"


def test_outbound_never_fills_language(store):
    cid = "whatsapp:a:639000000002"
    store.upsert_conversation(_conv(cid))
    store.ingest_message(_msg(cid, "Hello! How can I help you today?", direction="out", lang="en"))
    assert store.get_conversation(cid)["language"] == "unknown"


def test_batch_ingest_fills_unknown(store):
    cid = "whatsapp:a:639000000003"
    conv = _conv(cid)
    store.upsert_conversation(conv)
    store.ingest_batch(conv, [_msg(cid, "👍", lang="unknown", ts=1.0),
                              _msg(cid, "Magkano po shipping?", ts=2.0)])
    assert store.get_conversation(cid)["language"] == "tl"


# ── dry-run 回填 ────────────────────────────────────────────────────────


def _load_backfill():
    spec = importlib.util.spec_from_file_location(
        "backfill_conversation_language", ENGINE / "scripts" / "backfill_conversation_language.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_history(store):
    """模拟历史库：会话语言是旧检测器写的（直接写 unknown / en），消息是旧文本。"""
    rows = {
        "whatsapp:a:1": ("unknown", [("in", "Magkano po yung bundle?"), ("out", "Hello po!")]),
        "whatsapp:a:2": ("unknown", [("out", "Hi! Thanks for reaching out")]),
        "whatsapp:a:3": ("unknown", []),
        "whatsapp:a:4": ("unknown", [("in", "你好，请问怎么付款"), ("in", "ok")]),
        "whatsapp:a:5": ("en", [("in", "Pwede po ba ma-refund yung order ko?"),
                                ("in", "Wala pa po kasi dumating")]),
        "whatsapp:a:6": ("zh", [("in", "在吗")]),
    }
    for cid, (lang, msgs) in rows.items():
        store.upsert_conversation(_conv(cid, lang))
        for i, (d, t) in enumerate(msgs):
            store.ingest_message(_msg(cid, t, direction=d, lang="unknown", ts=100.0 + i))
        # 实时补写已存在：这里模拟「历史」——强制把语言写回旧值
        store._conn.execute("UPDATE conversations SET language=? WHERE conversation_id=?", (lang, cid))
        store._conn.commit()


def test_backfill_dry_run_plans_without_writing(store, tmp_path, capsys):
    _seed_history(store)
    db = Path(store._conn.execute("PRAGMA database_list").fetchone()[2])
    bf = _load_backfill()
    sql_out = tmp_path / "plan.sql"
    plan = bf.build_plan(db, recheck=["en"])
    assert plan["mode"] == "dry-run" and plan["total"] == 6
    filled = {c["conversation_id"]: c["to"] for c in plan["fill_unknown"]}
    assert filled == {"whatsapp:a:1": "tl", "whatsapp:a:4": "zh"}
    assert plan["still_unknown_reasons"] == {"no_inbound_text": 2}
    assert plan["unknown_pct_before"] == 66.7 and plan["unknown_pct_after"] == 33.3
    assert [u["conversation_id"] for u in plan["tl_upgrade_suggestions"]] == ["whatsapp:a:5"]

    assert bf.main(["--db", str(db), "--recheck", "en", "--sql-out", str(sql_out)]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "66.7% → 33.3%" in out
    sql = sql_out.read_text(encoding="utf-8")
    assert "AND language IN ('unknown','')" in sql and sql.count("UPDATE conversations") == 2
    # 库没被动过
    langs = dict(store._conn.execute("SELECT conversation_id, language FROM conversations").fetchall())
    assert langs["whatsapp:a:1"] == "unknown" and langs["whatsapp:a:5"] == "en"


def test_backfill_opens_db_read_only(store, tmp_path):
    _seed_history(store)
    db = Path(store._conn.execute("PRAGMA database_list").fetchone()[2])
    bf = _load_backfill()
    conn = bf._open_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE conversations SET language='x'")
    conn.close()
    src = (ENGINE / "scripts" / "backfill_conversation_language.py").read_text(encoding="utf-8")
    # 计划阶段仍是 mode=ro。--apply 是另开的读写路径，只补 unknown（见 test_session_lang_apply_zhiyu.py）。
    assert "mode=ro" in src
    build_body = src.split("def build_plan", 1)[1].split("def apply_unknown_fills", 1)[0]
    assert "_open_ro" in build_body and "commit(" not in build_body
    assert "--apply" in src and "tl_upgrade_suggestions" in src


def test_unknown_session_language_is_counted_and_kept_apart_from_last_line(store):
    """列表上的 language 仍是末条现场检测；session_language 才是库里的会话语言。"""
    from src.inbox.normalizer import store_row_to_chat

    store.upsert_conversation(_conv("whatsapp:a:u1", "unknown"))
    store.upsert_conversation(_conv("whatsapp:a:u2", ""))
    store.upsert_conversation(_conv("whatsapp:a:known", "tl"))
    assert store.count_unknown_session_language() == 2

    row = store.get_conversation("whatsapp:a:u1")
    row["last_text"] = "Pwede po ba COD?"
    chat = store_row_to_chat(row)
    assert chat["session_language"] == "unknown"
    assert chat["language"] == "tl"

    tpl = (ENGINE / "src/web/templates/unified_inbox.html").read_text(encoding="utf-8")
    assert 'id="lang-unknown-hint"' in tpl and "session_lang_unknown" in tpl
    assert "--apply" not in tpl.split('id="lang-unknown-hint"', 1)[0][-400:]
