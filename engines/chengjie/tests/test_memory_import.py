"""聊天记录导入（跨平台档案 P1）门禁：解析器多格式 / 编码回退 / 体量守卫 /
LLM 摘要宽松解析 / 事实接地标注 / 批次台账 / episodic 按指纹撤销 / 注入块出处行。

全部离线：LLM 用假 client；store 建在 tmp_path。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.contacts.memory_import import (
    MAX_FILE_BYTES,
    MAX_MESSAGES,
    decode_bytes,
    detect_format,
    ground_facts,
    guess_customer_sender,
    parse_chat_export,
    parse_summary_json,
    summarize_import,
    transcript_sample,
)


# ── 解码 / 探测 ──────────────────────────────────────────────────────────────

def test_decode_gb18030_fallback():
    raw = "2024-03-05 14:23:45 阿宝\n今天去钓鱼了".encode("gb18030")
    assert "阿宝" in decode_bytes(raw)


def test_decode_utf16_bom():
    raw = "hello 世界".encode("utf-16")
    assert "世界" in decode_bytes(raw)


# ── WhatsApp ────────────────────────────────────────────────────────────────

_WA_BRACKET = """[2024/3/5, 14:23:45] Bob: 今天去钓鱼了
[2024/3/5, 14:24:01] 我: 钓到什么了
[2024/3/6, 09:00:00] Bob: 一条鲈鱼
还挺大
"""

_WA_DASH = """3/5/24, 14:23 - Bob: hi there
3/5/24, 14:24 - Me: hello
3/6/24, 09:00 - Bob: my son starts primary school
"""


def test_parse_whatsapp_bracket_with_continuation():
    out = parse_chat_export("chat.txt", _WA_BRACKET.encode("utf-8"))
    assert out["ok"] and out["format"] == "whatsapp_txt"
    assert out["msg_count"] == 3
    assert out["messages"][2]["text"] == "一条鲈鱼\n还挺大"
    assert out["date_from"] == "2024-03-05" and out["date_to"] == "2024-03-06"
    assert {s["name"] for s in out["senders"]} == {"Bob", "我"}


def test_parse_whatsapp_dash():
    out = parse_chat_export("wa.txt", _WA_DASH.encode("utf-8"))
    assert out["ok"] and out["format"] == "whatsapp_txt"
    assert out["msg_count"] == 3
    assert out["date_from"] == "2024-03-05"


# ── 微信（第三方导出）────────────────────────────────────────────────────────

_WX = """2024-03-05 14:23:45 阿宝
今天做了燕窝粥
2024-03-05 14:25:00 我
好吃吗
2024-03-07 10:00:00 阿宝
下周去三亚出差
"""


def test_parse_wechat_header_style():
    out = parse_chat_export("wx.txt", _WX.encode("gb18030"))
    assert out["ok"] and out["format"] == "wechat_txt"
    assert out["msg_count"] == 3
    assert out["messages"][0]["text"] == "今天做了燕窝粥"
    assert out["date_to"] == "2024-03-07"


def test_parse_wechat_slash_date_and_qq_suffix():
    """P3 变体：斜杠日期 + QQ 导出「昵称(QQ号)」尾缀剥离（探测阈值需 ≥3 头行）。"""
    txt = ("2024/3/5 14:23 阿宝(10086)\n"
           "今天钓了条鲈鱼\n"
           "2024/3/5 14:25 我<me@qq.com>\n"
           "厉害了\n"
           "2024/3/6 09:00 阿宝(10086)\n"
           "下周去三亚\n")
    out = parse_chat_export("qq.txt", txt.encode("gb18030"))
    assert out["ok"] and out["format"] == "wechat_txt"
    assert out["msg_count"] == 3
    assert out["messages"][0]["sender"] == "阿宝"
    assert out["messages"][1]["sender"] == "我"
    assert out["date_from"] == "2024-03-05" and out["date_to"] == "2024-03-06"


# ── Telegram JSON ───────────────────────────────────────────────────────────

def test_parse_telegram_json_entities():
    doc = {"messages": [
        {"type": "message", "date": "2024-03-05T14:23:45", "from": "Bob",
         "text": ["check ", {"type": "link", "text": "this"}]},
        {"type": "service", "date": "2024-03-05T14:24:00", "from": "Bob",
         "text": "joined"},
        {"type": "message", "date": "2024-03-06T09:00:00", "from": "我",
         "text": "好的"},
    ]}
    out = parse_chat_export("result.json", json.dumps(doc).encode("utf-8"))
    assert out["ok"] and out["format"] == "telegram_json"
    assert out["msg_count"] == 2
    assert out["messages"][0]["text"] == "check this"
    assert out["date_from"] == "2024-03-05"


# ── CSV / 通用 / 失败面 ─────────────────────────────────────────────────────

def test_parse_csv_chinese_headers():
    csv_text = "时间,昵称,内容\n2024-03-05,阿宝,想换辆SUV\n2024-03-06,我,预算多少\n"
    out = parse_chat_export("chat.csv", csv_text.encode("utf-8"))
    assert out["ok"] and out["format"] == "csv"
    assert out["msg_count"] == 2
    assert out["messages"][0]["sender"] == "阿宝"


def test_parse_generic_colon_lines():
    txt = "Bob: 早上好\n我: 早\nBob: 今天有空吗\n"
    out = parse_chat_export("any.txt", txt.encode("utf-8"))
    assert out["ok"] and out["format"] == "generic_txt"
    assert out["msg_count"] == 3


def test_parse_unrecognized_and_empty_and_too_large():
    bad = parse_chat_export("x.txt", "这是一篇散文，没有对话结构。\n" .encode("utf-8"))
    assert not bad["ok"] and bad["error"] == "unrecognized_format"
    assert parse_chat_export("x.txt", b"")["error"] == "empty_file"
    big = parse_chat_export("x.txt", b"a" * (MAX_FILE_BYTES + 1))
    assert big["error"] == "file_too_large"


def test_parse_truncates_to_recent():
    lines = "\n".join(f"[2024/3/5, 14:00:00] Bob: msg{i}" for i in range(MAX_MESSAGES + 50))
    out = parse_chat_export("wa.txt", lines.encode("utf-8"))
    assert out["ok"] and out["truncated"]
    assert out["msg_count"] == MAX_MESSAGES
    assert out["messages"][-1]["text"] == f"msg{MAX_MESSAGES + 49}"


# ── 客户方猜测 / 打样 ────────────────────────────────────────────────────────

def test_guess_customer_excludes_self_names():
    senders = [{"name": "我", "count": 90}, {"name": "阿宝", "count": 60}]
    assert guess_customer_sender(senders) == "阿宝"
    assert guess_customer_sender(
        [{"name": "客服小林", "count": 9}, {"name": "Bob", "count": 5}],
        self_names=["客服小林"]) == "Bob"


def test_transcript_sample_roles_and_cap():
    msgs = [{"ts": "", "sender": "阿宝", "text": "我儿子上一年级了"},
            {"ts": "", "sender": "我", "text": "真快啊"}]
    s = transcript_sample(msgs, "阿宝")
    assert s.splitlines()[0].startswith("客户:")
    assert s.splitlines()[1].startswith("我方:")
    long = transcript_sample(
        [{"ts": "", "sender": "阿宝", "text": "长" * 190}] * 200, "阿宝", max_chars=1000)
    assert len(long) <= 1000


# ── LLM JSON 宽松解析 / 接地 ─────────────────────────────────────────────────

def test_parse_summary_json_variants():
    ok = parse_summary_json('{"topics":["钓鱼"],"note":"老客户","facts":[{"text":"儿子上一年级","quote":"我儿子上一年级了"}]}')
    assert ok["topics"] == ["钓鱼"] and ok["facts"][0]["text"] == "儿子上一年级"
    fenced = parse_summary_json('```json\n{"topics":[],"note":"","facts":["爱钓鱼"]}\n```')
    assert fenced is not None and fenced["facts"][0]["text"] == "爱钓鱼"
    embedded = parse_summary_json('前言 {"topics":["a"],"note":"n","facts":[]} 后记')
    assert embedded is not None and embedded["topics"] == ["a"]
    assert parse_summary_json("不是 JSON") is None
    assert parse_summary_json("") is None


def test_ground_facts_quote_and_overlap():
    msgs = [{"ts": "", "sender": "阿宝", "text": "我儿子上一年级了，想换辆SUV"},
            {"ts": "", "sender": "我", "text": "预算多少"}]
    facts = [
        {"text": "儿子上一年级", "quote": "我儿子上一年级了"},   # quote 命中
        {"text": "想换SUV", "quote": ""},                        # 词汇重叠命中
        {"text": "在迪拜有房产", "quote": "我在迪拜买了房"},      # 原话不存在 → 存疑
    ]
    out = ground_facts(facts, msgs, "阿宝")
    assert out[0]["grounded"] and out[1]["grounded"]
    assert not out[2]["grounded"]
    # 我方说的话不构成接地（防把客服的话记成客户事实）
    out2 = ground_facts([{"text": "预算多少", "quote": "预算多少"}], msgs, "阿宝")
    assert not out2[0]["grounded"]


class _FakeAI:
    def __init__(self, reply=None, raise_exc=False):
        self._reply = reply
        self._raise = raise_exc

    async def chat(self, messages, strategy_overrides=None):
        if self._raise:
            raise RuntimeError("boom")
        return self._reply


def test_summarize_import_paths():
    msgs = [{"ts": "", "sender": "阿宝", "text": "我儿子上一年级了"}]
    good = asyncio.run(summarize_import(
        _FakeAI('{"topics":["家庭"],"note":"n","facts":[{"text":"儿子上一年级","quote":"我儿子上一年级了"}]}'),
        msgs, "阿宝"))
    assert good["error"] == "" and good["facts"][0]["grounded"]
    bad = asyncio.run(summarize_import(_FakeAI("垃圾输出"), msgs, "阿宝"))
    assert bad["error"] == "llm_bad_json"
    boom = asyncio.run(summarize_import(_FakeAI(raise_exc=True), msgs, "阿宝"))
    assert boom["error"] == "llm_unavailable"
    none = asyncio.run(summarize_import(None, msgs, "阿宝"))
    assert none["error"] == "llm_unavailable"


# ── 批次台账 / episodic 撤销 ─────────────────────────────────────────────────

def _mk_store(tmp_path):
    from src.contacts.store import ContactStore
    return ContactStore(db_path=tmp_path / "contacts_p1.db")


def test_import_ledger_roundtrip_and_revoke(tmp_path):
    st = _mk_store(tmp_path)
    contact, _ci, _ = st.ensure_channel_identity(
        channel="telegram", account_id="default", external_id="10086")
    bid = st.insert_memory_import(
        contact_id=contact.contact_id, source_channel="wechat",
        source_label="微信-老号", file_name="wx.txt", file_sha256="abc123",
        msg_count=214, date_from="2025-11-01", date_to="2026-02-28",
        summary={"topics": ["钓鱼"], "note": "老客户"},
        memory_key="telegram:10086", fact_hashes=["h1", "h2"],
        facts_written=2, created_by="tester")
    assert bid
    rows = st.list_memory_imports(contact.contact_id)
    assert len(rows) == 1 and rows[0]["fact_hashes"] == ["h1", "h2"]
    assert rows[0]["summary"] == {"topics": ["钓鱼"], "note": "老客户"}
    assert st.find_confirmed_import_by_sha(contact.contact_id, "abc123") is not None
    assert st.mark_memory_import_revoked(bid) is True
    assert st.mark_memory_import_revoked(bid) is False  # 幂等：二次撤销 False
    assert st.find_confirmed_import_by_sha(contact.contact_id, "abc123") is None
    assert st.get_memory_import(bid)["status"] == "revoked"


def test_episodic_delete_by_hashes(tmp_path):
    from src.utils.episodic_memory_store import (
        EpisodicMemoryStore, content_hash_of,
    )
    es = EpisodicMemoryStore(str(tmp_path / "epi.db"))
    key = "telegram:10086"
    rid = es.add_fact(key, "儿子上一年级", category="imported", source="user_stated")
    assert rid is not None
    es.add_fact(key, "常驻事实不该被误删", category="general", source="user_stated")
    h = content_hash_of("儿子上一年级")
    assert es.delete_by_hashes(key, [h]) == 1
    assert es.delete_by_hashes(key, [h]) == 0  # 幂等
    assert es.delete_by_hashes("别的key", [content_hash_of("常驻事实不该被误删")]) == 0


# ── 注入块吃导入出处 ────────────────────────────────────────────────────────

def test_origin_block_renders_import_provenance():
    from src.contacts.origin_context import build_origin_block
    imports = [{"source_channel": "wechat", "msg_count": 214,
                "date_from": "2025-11-01", "date_to": "2026-02-28",
                "status": "confirmed"}]
    # 无档案、无轨迹，仅导入 → 导入来源即来处，块可渲染
    blk = build_origin_block(None, [], imports=imports)
    assert "微信" in blk and "214" in blk and "2025-11-01" in blk
    assert "时间线别记错" in blk
    # 有档案时出处行与话题行并存
    prof = {"origin_channel": "wechat", "topics": ["钓鱼"], "ai_visible": True}
    blk2 = build_origin_block(prof, [], imports=imports)
    assert "钓鱼" in blk2 and "214" in blk2


def test_provider_includes_imports(tmp_path):
    from types import SimpleNamespace
    from src.contacts.origin_context import invalidate_origin_cache, make_origin_provider
    st = _mk_store(tmp_path)
    contact, _ci, _ = st.ensure_channel_identity(
        channel="telegram", account_id="default", external_id="10086")
    st.insert_memory_import(
        contact_id=contact.contact_id, source_channel="wechat",
        msg_count=99, date_from="2026-01-01", date_to="2026-02-01",
        memory_key="k", fact_hashes=[], facts_written=0)
    cfg = SimpleNamespace(config={
        "contacts": {"origin_profile": {"enabled": True, "max_chars": 500}}})
    invalidate_origin_cache()
    blk = make_origin_provider(st, cfg)(
        channel="telegram", account_id="default", external_id="10086")
    invalidate_origin_cache()
    assert "微信" in blk and "99" in blk
