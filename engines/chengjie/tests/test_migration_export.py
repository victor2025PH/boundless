# -*- coding: utf-8 -*-
"""迁移包导出门禁（真 InboxStore@tmp / 真 zip / 零生产依赖，实施47 §5 工单 2）。

覆盖：
- 纯函数（可加回档位 / 汇总 / 文件名 / CSV 投影 / zip 成员路径净化 / manifest）；
- 包结构与成员齐备（含「不打媒体也必须有 media_index」这条契约）；
- **counts 是真正写进包的行数**（manifest 不许自我抹平差异）；
- 联系人三源合并（并集口径 / 身份列 / CRM 缺席时字段仍在）；
- 媒体打包 + 预算闸 + 缺失记账 + **zip-slip 与磁盘越界双防线**；
- sha256 完整性可复核；
- 字段白名单与 export-history 逐字一致（静态比对源码，防单侧漂移）。
"""
from __future__ import annotations

import io
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.integrations.protocol_bridge as pb
from src.inbox.migration_export import (
    CONTACT_CSV_HEADER,
    CONV_FIELDS,
    MSG_FIELDS,
    SCHEMA_VERSION,
    build_manifest,
    build_migration_kit,
    collect_contacts,
    contact_csv_row,
    derive_handles,
    media_member_path,
    merge_contact_row,
    reachability_of,
    reachability_rule,
    readme_text,
    safe_slug,
    summarize_reachability,
    zip_filename,
)
from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore


# ── 夹具 ─────────────────────────────────────────────────────────────────────

def _conv(cid, plat, acct, ck, *, username="", phone="", name="",
          chat_type="private", last_ts=0.0, first_seen=0.0):
    return InboxConversation(
        conversation_id=cid, platform=plat, account_id=acct, chat_key=ck,
        display_name=name or ck, username=username, phone=phone,
        chat_type=chat_type, last_ts=last_ts, first_seen=first_seen,
        last_text="hi")


def _msg(cid, pmid, *, media_ref="", ts=1.0, text="t"):
    return InboxMessage(conversation_id=cid, platform_msg_id=pmid, text=text,
                        ts=ts, media_ref=media_ref,
                        media_type=("photo" if media_ref else ""))


PLAT, ACCT = "telegram", "acct1"


@pytest.fixture()
def store(tmp_path):
    """4 个私聊（双句柄/仅用户名/仅手机/无句柄）+ 1 个群（不进联系人）。"""
    st = InboxStore(tmp_path / "inbox.db")
    st.ingest_batch(
        _conv(f"{PLAT}:{ACCT}:u1", PLAT, ACCT, "u1", username="ann",
              phone="+15550001", name="Ann", last_ts=300, first_seen=100),
        [_msg(f"{PLAT}:{ACCT}:u1", "m1",
              media_ref="/static/protocol_media/telegram/a.jpg", ts=280),
         _msg(f"{PLAT}:{ACCT}:u1", "m2", ts=300)])
    st.ingest_batch(
        _conv(f"{PLAT}:{ACCT}:u2", PLAT, ACCT, "u2", username="bob",
              name="Bob", last_ts=200),
        [_msg(f"{PLAT}:{ACCT}:u2", "m3", ts=200)])
    st.ingest_batch(
        _conv(f"{PLAT}:{ACCT}:u3", PLAT, ACCT, "u3", phone="+15550003",
              name="Cid", last_ts=150), [])
    st.ingest_batch(
        _conv(f"{PLAT}:{ACCT}:u4", PLAT, ACCT, "u4", name="Dee", last_ts=120),
        [_msg(f"{PLAT}:{ACCT}:u4", "m4", ts=120)])
    st.ingest_batch(
        _conv(f"{PLAT}:{ACCT}:g1", PLAT, ACCT, "g1", chat_type="group",
              name="Group", last_ts=400), [])
    yield st
    try:
        st.close()
    except Exception:
        pass


@pytest.fixture()
def media_root(tmp_path, monkeypatch):
    """媒体双根 → tmp（绝不碰真实引擎树/数据根）。"""
    main = tmp_path / "media_main"
    (main / "telegram").mkdir(parents=True)
    (main / "telegram" / "a.jpg").write_bytes(b"JPEGDATA-A")
    legacy = tmp_path / "media_legacy"
    legacy.mkdir(parents=True)
    monkeypatch.setattr(pb, "protocol_media_root", lambda: main)
    monkeypatch.setattr(pb, "legacy_protocol_media_root", lambda: legacy)
    return main


def _open_kit(path):
    return zipfile.ZipFile(path, "r")


def _jsonl_rows(zf, name):
    raw = zf.read(name).decode("utf-8")
    return [json.loads(x) for x in raw.splitlines() if x.strip()]


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def test_reachability_four_buckets():
    assert reachability_of("ann", "+1") == "both"
    assert reachability_of("ann", "") == "username"
    assert reachability_of("", "+1") == "phone"
    assert reachability_of("", "") == "none"
    # 空白串等同于没有（平台常回填空格）
    assert reachability_of("  ", "  ") == "none"
    assert reachability_of(None, None) == "none"


def test_summarize_reachability_covered_excludes_none():
    rows = [{"reachability": "both"}, {"reachability": "username"},
            {"reachability": "none"}, {"reachability": "weird"}]
    s = summarize_reachability(rows)
    assert s["both"] == 1 and s["username"] == 1
    # 未知桶保守归 none（绝不当成「有句柄」虚报覆盖率）
    assert s["none"] == 2
    assert s["total"] == 4 and s["covered"] == 2
    assert summarize_reachability([])["total"] == 0


def test_derive_handles_prefers_real_columns():
    assert derive_handles("whatsapp", "639273815533", "ann", "+1") == (
        "ann", "+1", "column")
    assert derive_handles("telegram", "8506426282", "bob", "") == (
        "bob", "", "column")


def test_derive_handles_whatsapp_chat_key_is_the_phone():
    """WhatsApp 协议层用号码寻址 → chat_key 即手机号，必须计入可加回。

    生产实测：47 条 WA 私聊只有 11 条填了 phone 身份列，而 47 条 chat_key 全是
    裸号码；不派生的话该平台覆盖率虚低到 6%（而真实接近 100%）。
    """
    assert derive_handles("whatsapp", "639273815533", "", "") == (
        "", "639273815533", "chat_key")
    assert derive_handles("WhatsApp", "+85263115820", "", "") == (
        "", "85263115820", "chat_key")
    assert reachability_of(*derive_handles(
        "whatsapp", "639273815533", "", "")[:2]) == "phone"


def test_derive_handles_never_guesses_by_shape():
    """**本条是这个功能的安全底线**：只按平台白名单派生，绝不按形态猜。

    Telegram 的 chat_key 是 10 位数字用户 id，与手机号形态无从区分——按形态派生
    会把 155 个 TG 会话全标成「有手机号可加回」，运营照单去加会全部加不上，
    比少报坏得多。LINE 的 MID / Messenger 的 FB 数字 id 同样不可用于重新加好友。
    """
    assert derive_handles("telegram", "8506426282", "", "") == ("", "", "none")
    assert derive_handles("messenger", "1054679137531045", "", "") == (
        "", "", "none")
    assert derive_handles(
        "line", "UIq2koGBkmzY7hBMCoKrk24SBeDO4jfjsMnrNVaZMpEo", "", "") == (
        "", "", "none")
    # 白名单内但形态不是号码（群 jid / 过长 / 含字母）→ 同样不派生
    assert derive_handles("whatsapp", "12345-67890@g.us", "", "") == (
        "", "", "none")
    assert derive_handles("whatsapp", "12345", "", "") == ("", "", "none")
    assert derive_handles("whatsapp", "9" * 16, "", "") == ("", "", "none")
    assert derive_handles("whatsapp", "", "", "") == ("", "", "none")


def test_reachability_rule_is_self_describing():
    r = reachability_rule()
    assert r["handles"] == ["username", "phone"]
    assert r["derive_phone_from_chat_key"] == ["whatsapp"]
    assert r["phone_digits"] == [7, 15]


def test_safe_slug_and_zip_filename():
    assert safe_slug("a/b\\c:d") == "a_b_c_d"
    assert safe_slug("...") == "account"
    assert safe_slug("", fallback="x") == "x"
    fn = zip_filename("Telegram", "84/99", now=0)
    assert fn.startswith("chatx-migration_telegram_84_99_")
    assert fn.endswith(".zip")
    assert "/" not in fn and "\\" not in fn
    # 与既有 chatx-history_* 同族命名（运营在下载目录能分清两种包）
    assert re.match(r"^chatx-migration_[\w.-]+_\d{8}-\d{4}\.zip$", fn)


def test_merge_contact_row_three_sources():
    row = merge_contact_row(
        {"chat_key": "u1", "name": "Ann", "notify_name": "A",
         "in_book": True, "has_conversation": True, "never_spoke": False,
         "last_ts": 300.0, "unread": 2},
        {"username": "ann", "phone": "+1", "avatar_url": "http://x",
         "first_seen": 100.0, "language": "en"},
        {"contact_id": "c9", "funnel_stage": "warm", "intimacy_score": 7,
         "tags": ["vip", ""], "notes": "n", "follow_up_at": 9.0})
    assert row["reachability"] == "both"
    assert row["display_name"] == "Ann" and row["username"] == "ann"
    assert row["first_seen"] == 100.0 and row["language"] == "en"
    assert row["tags"] == ["vip"]          # 空标签被剔
    assert row["contact_id"] == "c9" and row["intimacy_score"] == 7
    assert row["handle_source"] == "column"


def test_merge_contact_row_survives_missing_sources():
    """身份列/CRM 全缺 → 字段仍在（下游导入器不必分支），覆盖率诚实为 none。"""
    row = merge_contact_row({"chat_key": "u9", "name": "", "notify_name": "Nk"})
    assert row["chat_key"] == "u9"
    assert row["display_name"] == "Nk"     # name 空 → 回落 notify_name
    assert row["reachability"] == "none"
    for k in ("username", "phone", "contact_id", "funnel_stage", "notes"):
        assert row[k] == ""
    assert row["tags"] == [] and row["intimacy_score"] is None
    assert row["handle_source"] == "none"
    assert merge_contact_row(None)["chat_key"] == ""


def test_merge_contact_row_derives_for_whatsapp_only():
    base = {"chat_key": "639273815533", "name": "Long"}
    wa = merge_contact_row(base, {}, platform="whatsapp")
    assert wa["phone"] == "639273815533" and wa["reachability"] == "phone"
    assert wa["handle_source"] == "chat_key"
    # 同一 chat_key 形态，平台换成 telegram → 绝不派生
    tg = merge_contact_row(base, {}, platform="telegram")
    assert tg["phone"] == "" and tg["reachability"] == "none"
    # 不传 platform（旧调用方）→ 保守不派生
    assert merge_contact_row(base, {})["reachability"] == "none"


def test_contact_csv_row_is_operator_readable():
    row = merge_contact_row(
        {"chat_key": "u1", "name": "Ann", "in_book": True,
         "has_conversation": True, "never_spoke": False, "unread": 3,
         "last_ts": 0},
        {"username": "ann", "phone": ""})
    cells = contact_csv_row(row)
    assert len(cells) == len(CONTACT_CSV_HEADER)
    assert cells[CONTACT_CSV_HEADER.index("reachability")] == "username"
    assert cells[CONTACT_CSV_HEADER.index("in_book")] == "是"
    assert cells[CONTACT_CSV_HEADER.index("never_spoke")] == "否"
    # last_ts=0 → 空串而不是 1970（对运营诚实）
    assert cells[CONTACT_CSV_HEADER.index("last_ts_iso")] == ""
    assert contact_csv_row(None)[0] == ""


@pytest.mark.parametrize("ref,expected", [
    ("/static/protocol_media/telegram/a.jpg", "media/telegram/a.jpg"),
    ("/static/protocol_media/wa/x/y.ogg", "media/wa/x/y.ogg"),
    # zip-slip：`..` 段必须被吃掉，绝不产出能写到包外的成员名
    ("/static/protocol_media/../../config/config.yaml", "media/config/config.yaml"),
    ("/static/protocol_media/a/../../b.jpg", "media/a/b.jpg"),
    ("/static/protocol_media/./a.jpg", "media/a.jpg"),
    ("/static/protocol_media/a.jpg?v=1", "media/a.jpg"),
    # 非 protocol 命名空间一律不进包
    ("https://cdn.example.com/a.jpg", ""),
    ("/static/other/a.jpg", ""),
    ("", ""),
    ("/static/protocol_media/", ""),
])
def test_media_member_path_sanitizes(ref, expected):
    got = media_member_path(ref)
    assert got == expected
    if got:
        assert ".." not in got.split("/")
        assert got.startswith("media/")


def test_build_manifest_keeps_both_count_sources():
    m = build_manifest(platform="Telegram", account_id="a1", label="主号",
                       counts={"messages": 3}, store_counts={"messages": 5},
                       integrity={"contacts.jsonl": "ab"},
                       reachability={"total": 1},
                       ban={"banned": True, "reason": "flood"},
                       generated_at=1.0, app_version="9.9")
    assert m["schema_version"] == SCHEMA_VERSION and m["platform"] == "telegram"
    # 刻意不抹平：写入 3 vs 预估 5 两个数并列摆着（导出完整率的原料）
    assert m["counts"]["messages"] == 3
    assert m["store_counts"]["messages"] == 5
    assert m["ban"]["reason"] == "flood"
    assert m["generator"]["app_version"] == "9.9"
    assert m["media"]["included"] is False
    # 覆盖率算法自描述随包走（日后可复现那个百分比）
    assert m["reachability_rule"]["derive_phone_from_chat_key"] == ["whatsapp"]


def test_readme_mentions_reachability_and_media_state():
    on = readme_text("telegram", "a1", media_included=True)
    off = readme_text("telegram", "a1", media_included=False)
    for txt in (on, off):
        assert "reachability" in txt and "contacts.csv" in txt
        assert "English" in txt
    assert "未包含媒体原件" in off and "未包含媒体原件" not in on


# ── 取数 ─────────────────────────────────────────────────────────────────────

def test_collect_contacts_union_and_identity(store):
    rows = collect_contacts(store, PLAT, ACCT)
    by = {r["chat_key"]: r for r in rows}
    # 「人的并集」口径：4 个私聊入，群不入
    assert set(by) == {"u1", "u2", "u3", "u4"}
    assert by["u1"]["reachability"] == "both"
    assert by["u2"]["reachability"] == "username"
    assert by["u3"]["reachability"] == "phone"
    assert by["u4"]["reachability"] == "none"
    assert by["u1"]["first_seen"] == 100.0
    # CRM 缺席 → 字段留空但存在
    assert by["u1"]["contact_id"] == ""


def test_collect_contacts_derives_whatsapp_phone_end_to_end(tmp_path):
    """整链证明：WA 账号身份列全空，覆盖率仍应是 100%（chat_key 即号码）。"""
    st = InboxStore(tmp_path / "wa.db")
    try:
        for ck in ("639273815533", "85263115820"):
            st.ingest_batch(
                InboxConversation(
                    conversation_id=f"whatsapp:wa1:{ck}", platform="whatsapp",
                    account_id="wa1", chat_key=ck, display_name=ck,
                    chat_type="private", last_ts=10.0), [])
        rows = collect_contacts(st, "whatsapp", "wa1")
        assert len(rows) == 2
        assert all(r["reachability"] == "phone" for r in rows)
        assert all(r["handle_source"] == "chat_key" for r in rows)
        s = summarize_reachability(rows)
        assert s["covered"] == 2 and s["total"] == 2
    finally:
        try:
            st.close()
        except Exception:
            pass


def test_collect_contacts_crm_lookup_wired(store):
    seen = []

    def _crm(ck):
        seen.append(ck)
        return {"contact_id": "c-" + ck, "funnel_stage": "hot"} if ck == "u1" else None

    rows = {r["chat_key"]: r for r in
            collect_contacts(store, PLAT, ACCT, crm_lookup=_crm)}
    assert set(seen) == {"u1", "u2", "u3", "u4"}
    assert rows["u1"]["contact_id"] == "c-u1"
    assert rows["u1"]["funnel_stage"] == "hot"
    assert rows["u2"]["contact_id"] == ""


def test_collect_contacts_crm_failure_does_not_break_export(store):
    """CRM 抛异常绝不能让迁移包导不出来（句柄才是必要条件）。"""
    def _boom(ck):
        raise RuntimeError("crm down")

    rows = collect_contacts(store, PLAT, ACCT, crm_lookup=_boom)
    assert len(rows) == 4
    assert all(r["contact_id"] == "" for r in rows)


def test_identity_map_absent_db_degrades_to_no_handle(store, tmp_path):
    """身份列读不到 → 全落 none 桶（诚实报 0%，不假装有句柄、不崩）。"""
    rows = collect_contacts(store, PLAT, ACCT,
                            db_path=tmp_path / "nope.db")
    assert len(rows) == 4
    assert {r["reachability"] for r in rows} == {"none"}


# ── 打包 ─────────────────────────────────────────────────────────────────────

def test_kit_structure_and_counts(store, media_root, tmp_path):
    kit = build_migration_kit(store, PLAT, ACCT, label="主号",
                              out_dir=tmp_path / "out", app_version="1.2.3")
    assert Path(kit.path).is_file()
    assert kit.filename.startswith("chatx-migration_telegram_acct1_")
    with _open_kit(kit.path) as zf:
        names = set(zf.namelist())
        # media_index **恒有**：不打包也要留对应关系
        assert {"manifest.json", "contacts.jsonl", "contacts.csv",
                "conversations.jsonl", "media_index.jsonl",
                "README.txt"} <= names
        assert not any(n.startswith("media/") for n in names)
        man = json.loads(zf.read("manifest.json"))
        convs = _jsonl_rows(zf, "conversations.jsonl")
        contacts = _jsonl_rows(zf, "contacts.jsonl")

    # counts = 真正写进包里的行数，且与 store 预估对账一致
    assert man["counts"]["conversations"] == 5      # 含群会话
    assert man["counts"]["messages"] == 4
    assert man["counts"]["contacts"] == 4          # 联系人是「人」口径，群不入
    assert man["store_counts"]["conversations"] == 5
    assert man["store_counts"]["messages"] == 4
    assert kit.reconciled is True
    assert man["label"] == "主号" and man["ban"] == {}
    assert man["reachability"]["total"] == 4 and man["reachability"]["covered"] == 3

    # 逐行数据与 counts 相符（manifest 不许说谎）
    assert len([r for r in convs if r["type"] == "conversation"]) == 5
    assert len([r for r in convs if r["type"] == "message"]) == 4
    assert len(contacts) == 4


def test_kit_conversation_rows_use_export_history_whitelist(store, media_root,
                                                            tmp_path):
    kit = build_migration_kit(store, PLAT, ACCT, out_dir=tmp_path / "o")
    with _open_kit(kit.path) as zf:
        rows = _jsonl_rows(zf, "conversations.jsonl")
    conv = next(r for r in rows if r["type"] == "conversation")
    msg = next(r for r in rows if r["type"] == "message")
    assert set(conv) - {"type"} <= set(CONV_FIELDS)
    assert set(msg) - {"type", "conversation_id"} <= set(MSG_FIELDS)
    # 内部列绝不静默泄进导出文件
    assert "risk_level" not in conv and "created_at" not in conv


def test_kit_csv_has_bom_and_header(store, media_root, tmp_path):
    kit = build_migration_kit(store, PLAT, ACCT, out_dir=tmp_path / "o")
    with _open_kit(kit.path) as zf:
        raw = zf.read("contacts.csv")
    assert raw.startswith("\ufeff".encode("utf-8"))   # Excel 直开不乱码
    text = raw.decode("utf-8-sig")
    import csv as _csv
    rows = list(_csv.reader(io.StringIO(text)))
    assert rows[0] == list(CONTACT_CSV_HEADER)
    assert len(rows) - 1 == 4


def test_kit_integrity_hashes_verify(store, media_root, tmp_path):
    kit = build_migration_kit(store, PLAT, ACCT, out_dir=tmp_path / "o")
    import hashlib
    with _open_kit(kit.path) as zf:
        man = json.loads(zf.read("manifest.json"))
        for name, digest in man["integrity"].items():
            actual = hashlib.sha256(zf.read(name)).hexdigest()
            assert actual == digest, f"{name} 校验和不符"
    # manifest 自身不进 integrity（自指无意义）
    assert "manifest.json" not in man["integrity"]
    # P-2 E（#259 · D-P4）：blocklist.jsonl 账号级停联名单成员随包走
    assert {"contacts.jsonl", "contacts.csv", "conversations.jsonl",
            "media_index.jsonl", "blocklist.jsonl", "README.txt"} == set(man["integrity"])


def test_media_index_present_even_when_not_packed(store, media_root, tmp_path):
    kit = build_migration_kit(store, PLAT, ACCT, out_dir=tmp_path / "o")
    with _open_kit(kit.path) as zf:
        idx = _jsonl_rows(zf, "media_index.jsonl")
        man = json.loads(zf.read("manifest.json"))
    assert len(idx) == 1
    assert idx[0]["path"] == "media/telegram/a.jpg"
    assert idx[0]["missing"] is False and idx[0]["size"] == 10
    assert man["media"]["included"] is False
    assert man["counts"]["media_files"] == 0      # 未打包 → 0 个文件进包
    assert man["counts"]["media_missing"] == 0    # 但文件确实在盘上


def test_media_packed_when_requested(store, media_root, tmp_path):
    kit = build_migration_kit(store, PLAT, ACCT, include_media=True,
                              out_dir=tmp_path / "o")
    with _open_kit(kit.path) as zf:
        assert zf.read("media/telegram/a.jpg") == b"JPEGDATA-A"
        man = json.loads(zf.read("manifest.json"))
        idx = _jsonl_rows(zf, "media_index.jsonl")
    assert man["media"]["included"] is True
    assert man["counts"]["media_files"] == 1
    assert man["counts"]["media_bytes"] == 10
    assert idx[0]["sha256"]


def test_media_missing_file_is_accounted_not_fatal(store, media_root, tmp_path):
    """盘上没有的媒体 → missing 记账，导出照常完成（封号后媒体常已被清）。"""
    (media_root / "telegram" / "a.jpg").unlink()
    kit = build_migration_kit(store, PLAT, ACCT, include_media=True,
                              out_dir=tmp_path / "o")
    with _open_kit(kit.path) as zf:
        man = json.loads(zf.read("manifest.json"))
        idx = _jsonl_rows(zf, "media_index.jsonl")
    assert man["counts"]["media_missing"] == 1
    assert man["counts"]["media_files"] == 0
    assert idx[0]["missing"] is True
    assert not any(n.startswith("media/") for n in
                   zipfile.ZipFile(kit.path).namelist())


def test_media_budget_gate_records_skips(store, media_root, tmp_path):
    kit = build_migration_kit(store, PLAT, ACCT, include_media=True,
                              media_budget_bytes=1, out_dir=tmp_path / "o")
    with _open_kit(kit.path) as zf:
        man = json.loads(zf.read("manifest.json"))
        idx = _jsonl_rows(zf, "media_index.jsonl")
    # 超预算 → 如实记账而不是静默截断
    assert man["media"]["skipped_budget"] == 1
    assert man["counts"]["media_files"] == 0
    assert idx[0]["skipped"] == "budget"


def test_media_outside_root_is_refused(store, media_root, tmp_path):
    """磁盘侧越界防线：投毒的 media_ref 不得把包外文件打进 zip。"""
    secret = tmp_path / "secret.yaml"
    secret.write_bytes(b"api_key: TOPSECRET")
    st = store
    st.ingest_batch(
        _conv(f"{PLAT}:{ACCT}:u5", PLAT, ACCT, "u5", name="Evil", last_ts=99),
        [_msg(f"{PLAT}:{ACCT}:u5", "m9",
              media_ref="/static/protocol_media/../../secret.yaml", ts=99)])
    kit = build_migration_kit(st, PLAT, ACCT, include_media=True,
                              out_dir=tmp_path / "o")
    with _open_kit(kit.path) as zf:
        names = zf.namelist()
        blob = b"".join(zf.read(n) for n in names)
    assert not any("secret" in n for n in names)
    assert b"TOPSECRET" not in blob


def test_kit_reconciled_false_when_counts_diverge(store, media_root, tmp_path):
    """预估与实际不符时 reconciled 必须为 False（这是完整率告警的判据）。"""
    real = store.count_account_data

    def _inflated(p, a):
        d = dict(real(p, a) or {})
        d["messages"] = int(d.get("messages") or 0) + 7
        return d

    store.count_account_data = _inflated       # type: ignore[method-assign]
    try:
        kit = build_migration_kit(store, PLAT, ACCT, out_dir=tmp_path / "o")
    finally:
        store.count_account_data = real        # type: ignore[method-assign]
    assert kit.reconciled is False
    with _open_kit(kit.path) as zf:
        man = json.loads(zf.read("manifest.json"))
    assert man["counts"]["messages"] == 4 and man["store_counts"]["messages"] == 11


def test_kit_on_empty_account_is_valid(store, media_root, tmp_path):
    """零资产账号也要产出结构完整的包（空态不是异常态）。"""
    kit = build_migration_kit(store, PLAT, "ghost", out_dir=tmp_path / "o")
    with _open_kit(kit.path) as zf:
        man = json.loads(zf.read("manifest.json"))
        assert zf.read("contacts.jsonl") == b""
        assert _jsonl_rows(zf, "media_index.jsonl") == []
    assert man["counts"] == {"conversations": 0, "messages": 0, "contacts": 0,
                             "media_files": 0, "media_bytes": 0,
                             "media_missing": 0, "blocklist": 0}
    assert man["reachability"]["total"] == 0
    assert kit.reconciled is True


# ── 跨模块契约 ───────────────────────────────────────────────────────────────

def test_field_whitelists_match_export_history():
    """字段白名单必须与 export-history 路由里的两个同名元组逐字一致。

    两处**刻意各自持有**（不 import 5900 行的路由模块），所以用静态源码比对当
    防漂移网：谁只改了一侧，这条就红——这正是本仓「两个消费面绝不各算一套」纪律
    在无法共享符号时的替代手段。
    """
    src = (Path(__file__).resolve().parents[1] / "src" / "web" / "routes"
           / "unified_inbox_account_routes.py").read_text(encoding="utf-8")

    def _tuple_after(marker: str):
        i = src.index(marker)
        chunk = src[i:i + 900]
        return tuple(re.findall(r'"([a-z_]+)"', chunk[chunk.index("("):
                                                      chunk.index(")")]))

    assert _tuple_after("_CONV_FIELDS = (") == CONV_FIELDS
    assert _tuple_after("_MSG_FIELDS = (") == MSG_FIELDS


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
