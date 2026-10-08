"""会话语言历史回填的 --apply（智语 2026-10-08）。

只补 language 为 unknown 或空的会话。已有语言（含 en→tl 建议）不改。
默认 dry-run 仍不写库，见 test_session_lang_zhiyu.py。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.inbox.normalizer import detect_inbound_language

ENGINE = Path(__file__).resolve().parents[1]


def _load_backfill():
    spec = importlib.util.spec_from_file_location(
        "backfill_conversation_language_apply",
        ENGINE / "scripts" / "backfill_conversation_language.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def _msg(cid, text, direction="in", ts=10.0):
    from src.inbox.models import InboxMessage
    return InboxMessage(conversation_id=cid, direction=direction, text=text,
                        source_lang=detect_inbound_language(text), ts=ts)


def _seed(store):
    rows = {
        "whatsapp:a:1": ("unknown", [("in", "Magkano po yung bundle?"), ("out", "Hello po!")]),
        "whatsapp:a:2": ("unknown", [("out", "Hi! Thanks for reaching out")]),
        "whatsapp:a:3": ("unknown", []),
        "whatsapp:a:4": ("unknown", [("in", "你好，请问怎么付款"), ("in", "ok")]),
        "whatsapp:a:5": ("en", [("in", "Pwede po ba ma-refund yung order ko?"),
                                ("in", "Wala pa po kasi dumating")]),
        "whatsapp:a:6": ("zh", [("in", "在吗")]),
        "whatsapp:a:7": ("", [("in", "Sige po, salamat")]),
    }
    for cid, (lang, msgs) in rows.items():
        store.upsert_conversation(_conv(cid, lang or "unknown"))
        for i, (direction, text) in enumerate(msgs):
            store.ingest_message(_msg(cid, text, direction=direction, ts=100.0 + i))
        store._conn.execute(
            "UPDATE conversations SET language=? WHERE conversation_id=?", (lang, cid))
        store._conn.commit()


def _langs(store):
    return dict(store._conn.execute(
        "SELECT conversation_id, language FROM conversations").fetchall())


def _db(store) -> Path:
    return Path(store._conn.execute("PRAGMA database_list").fetchone()[2])


def test_apply_fills_unknown_and_blank_only(store):
    _seed(store)
    bf = _load_backfill()
    db = _db(store)
    plan = bf.build_plan(db, recheck=["en"])
    assert plan["mode"] == "dry-run"
    filled = {c["conversation_id"]: c["to"] for c in plan["fill_unknown"]}
    assert filled["whatsapp:a:1"] == "tl"
    assert filled["whatsapp:a:4"] == "zh"
    assert filled["whatsapp:a:7"] == "tl"
    assert "whatsapp:a:5" not in filled
    assert [u["conversation_id"] for u in plan["tl_upgrade_suggestions"]] == ["whatsapp:a:5"]

    result = bf.apply_unknown_fills(db, plan)
    assert result["updated"] == 3
    langs = _langs(store)
    assert langs["whatsapp:a:1"] == "tl"
    assert langs["whatsapp:a:4"] == "zh"
    assert langs["whatsapp:a:7"] == "tl"
    assert langs["whatsapp:a:5"] == "en"
    assert langs["whatsapp:a:6"] == "zh"
    assert langs["whatsapp:a:2"] == "unknown"
    assert langs["whatsapp:a:3"] == "unknown"

    again = bf.apply_unknown_fills(db, plan)
    assert again["updated"] == 0 and again["skipped"] == 3
    assert _langs(store)["whatsapp:a:5"] == "en"


def test_apply_ignores_upgrade_suggestions_and_where_guard(store):
    _seed(store)
    bf = _load_backfill()
    db = _db(store)
    forged = {
        "fill_unknown": [
            {"conversation_id": "whatsapp:a:5", "to": "tl"},
            {"conversation_id": "whatsapp:a:6", "to": "en"},
            {"conversation_id": "whatsapp:a:1", "to": "unknown"},
        ],
        "tl_upgrade_suggestions": [
            {"conversation_id": "whatsapp:a:5", "to": "tl"},
        ],
    }
    result = bf.apply_unknown_fills(db, forged)
    assert result["updated"] == 0
    langs = _langs(store)
    assert langs["whatsapp:a:5"] == "en"
    assert langs["whatsapp:a:6"] == "zh"
    assert langs["whatsapp:a:1"] == "unknown"


def test_main_apply_is_opt_in(store, capsys):
    _seed(store)
    bf = _load_backfill()
    db = _db(store)
    assert bf.main(["--db", str(db), "--recheck", "en"]) == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "[apply]" not in out
    assert _langs(store)["whatsapp:a:1"] == "unknown"
    assert _langs(store)["whatsapp:a:5"] == "en"

    assert bf.main(["--db", str(db), "--recheck", "en", "--apply"]) == 0
    applied = capsys.readouterr().out
    assert "[apply]" in applied and "tl suggestions not written" in applied
    assert "updated=3" in applied
    langs = _langs(store)
    assert langs["whatsapp:a:1"] == "tl" and langs["whatsapp:a:4"] == "zh"
    assert langs["whatsapp:a:7"] == "tl" and langs["whatsapp:a:5"] == "en"
