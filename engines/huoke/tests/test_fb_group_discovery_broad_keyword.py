from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from unittest.mock import MagicMock


def test_extract_group_search_results_from_split_chinese_rows():
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    xml = (
        '<hierarchy>'
        '<node text="全部" class="android.widget.TextView" bounds="[20,160][90,210]" />'
        '<node text="小组" class="android.widget.TextView" bounds="[110,160][180,210]" />'
        '<node text="潮味决士林中正店 · 加入" class="android.widget.TextView" bounds="[96,330][520,372]" />'
        '<node text="公开小组 · 385 位成员" class="android.widget.TextView" bounds="[96,376][600,414]" />'
        '<node text="潮味日本交流" class="android.widget.TextView" bounds="[96,470][520,512]" />'
        '<node text="私密小组 · 1.2K members" class="android.widget.TextView" bounds="[96,516][600,554]" />'
        '</hierarchy>'
    )
    d = MagicMock()
    d.dump_hierarchy.return_value = xml

    out = fb._extract_group_search_results(d, keyword="潮味", max_groups=5)

    assert [g["group_name"] for g in out] == ["潮味决士林中正店", "潮味日本交流"]
    assert out[0]["requires_join"] is True
    assert out[0]["member_count"] == 385
    assert out[1]["member_count"] == 1200
    assert all("meta" in g for g in out)


def test_extract_group_search_results_from_content_desc_row():
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    xml = (
        '<hierarchy>'
        '<node text="" content-desc="潮味俱樂部, 公開社團 · 2,300 位成員" '
        'class="android.widget.Button" clickable="true" bounds="[0,300][720,430]" />'
        '</hierarchy>'
    )
    d = MagicMock()
    d.dump_hierarchy.return_value = xml

    out = fb._extract_group_search_results(d, keyword="潮味", max_groups=5)

    assert out[0]["group_name"] == "潮味俱樂部"


def test_group_members_search_is_not_global_fb_search_surface():
    from src.app_automation.fb_search_markers import (
        hierarchy_looks_like_fb_group_members_search,
        hierarchy_looks_like_fb_search_surface,
    )

    xml = (
        '<hierarchy>'
        '<node text="Members" class="android.widget.TextView" />'
        '<node text="主婦 交流" class="android.widget.EditText" '
        'hint="Search members" />'
        '<node text="" content-desc="Admins and moderators" />'
        '<node text="" content-desc="Group contributors" />'
        '</hierarchy>'
    )

    assert hierarchy_looks_like_fb_group_members_search(xml) is True
    assert hierarchy_looks_like_fb_search_surface(xml) is False


def test_global_fb_search_surface_still_matches_edittext():
    from src.app_automation.fb_search_markers import (
        hierarchy_looks_like_fb_search_surface,
    )

    xml = (
        '<hierarchy>'
        '<node text="ペット" class="android.widget.EditText" hint="Search" />'
        '<node text="Recent searches" class="android.widget.TextView" />'
        '</hierarchy>'
    )

    assert hierarchy_looks_like_fb_search_surface(xml) is True


def test_extract_group_search_results_marks_separate_join_button():
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    xml = (
        '<hierarchy>'
        '<node text="潮味決士林中正店" class="android.widget.TextView" '
        'bounds="[166,304][420,350]" />'
        '<node text="加入" class="android.widget.TextView" '
        'bounds="[430,304][490,350]" />'
        '<node text="公开 · 385 位成员" class="android.widget.TextView" '
        'bounds="[166,356][600,396]" />'
        '</hierarchy>'
    )
    d = MagicMock()
    d.dump_hierarchy.return_value = xml

    out = fb._extract_group_search_results(d, keyword="潮味", max_groups=5)

    assert out[0]["group_name"] == "潮味決士林中正店"
    assert out[0]["requires_join"] is True


def test_extract_group_search_results_rejects_post_text_false_positive():
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    xml = (
        '<hierarchy>'
        '<node text="全部" class="android.widget.TextView" bounds="[20,160][90,210]" />'
        '<node text="小组" class="android.widget.TextView" bounds="[110,160][180,210]" />'
        '<node text=".. GOLDEN WEEK – AN NUONG CUC DA ..&#10;…" '
        'class="android.widget.TextView" bounds="[96,330][620,372]" />'
        '<node text="公开 · 12 位成员" class="android.widget.TextView" bounds="[96,376][600,414]" />'
        '<node text="カレー研究会" class="android.widget.TextView" bounds="[96,470][520,512]" />'
        '<node text="公開グループ · 2.2 万位成员" class="android.widget.TextView" bounds="[96,516][600,554]" />'
        '</hierarchy>'
    )
    d = MagicMock()
    d.dump_hierarchy.return_value = xml

    out = fb._extract_group_search_results(d, keyword="潮味", max_groups=5)

    assert [g["group_name"] for g in out] == ["カレー研究会"]
    assert out[0]["member_count"] == 22000


def test_extract_group_search_results_rejects_truncated_group_title():
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    xml = (
        '<hierarchy>'
        '<node text="玄奶皿奈玉伙反模逜.." class="android.widget.TextView" '
        'bounds="[96,330][520,372]" />'
        '<node text="公开小组 · 385 位成员" class="android.widget.TextView" '
        'bounds="[96,376][600,414]" />'
        '<node text="トイプードルは家族..&#160;·" class="android.widget.TextView" '
        'bounds="[96,420][520,462]" />'
        '<node text="Public · 13K members" class="android.widget.TextView" '
        'bounds="[96,466][600,504]" />'
        '<node text="我がペットの日常" class="android.widget.TextView" '
        'bounds="[96,560][520,602]" />'
        '<node text="公開グループ · 9,259 位成员" class="android.widget.TextView" '
        'bounds="[96,606][600,644]" />'
        '</hierarchy>'
    )
    d = MagicMock()
    d.dump_hierarchy.return_value = xml

    out = fb._extract_group_search_results(d, keyword="ペット", max_groups=5)

    assert [g["group_name"] for g in out] == ["我がペットの日常"]


def test_extract_group_search_results_rejects_marketplace_service_row():
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    xml = (
        '<hierarchy>'
        '<node text="Pet sitter · £ · 28.5 km · 神奈川県" '
        'class="android.widget.TextView" bounds="[96,330][620,372]" />'
        '<node text="Public · 13K members" class="android.widget.TextView" '
        'bounds="[96,376][600,414]" />'
        '<node text="我がペットの日常" class="android.widget.TextView" '
        'bounds="[96,470][520,512]" />'
        '<node text="Public · 9,259 members" class="android.widget.TextView" '
        'bounds="[96,516][600,554]" />'
        '</hierarchy>'
    )
    d = MagicMock()
    d.dump_hierarchy.return_value = xml

    out = fb._extract_group_search_results(d, keyword="ペット 女性", max_groups=5)

    assert [g["group_name"] for g in out] == ["我がペットの日常"]


def test_fb_store_discovered_does_not_downgrade_joined_group(monkeypatch):
    from src.host import fb_store

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE facebook_groups ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "device_id TEXT NOT NULL,"
        "group_name TEXT NOT NULL,"
        "group_url TEXT DEFAULT '',"
        "member_count INTEGER DEFAULT 0,"
        "language TEXT DEFAULT '',"
        "country TEXT DEFAULT '',"
        "status TEXT NOT NULL DEFAULT 'joined',"
        "joined_at TEXT NOT NULL,"
        "last_visited_at TEXT,"
        "visit_count INTEGER DEFAULT 0,"
        "extracted_member_count INTEGER DEFAULT 0,"
        "preset_key TEXT DEFAULT '',"
        "UNIQUE(device_id, group_name))"
    )

    @contextmanager
    def fake_connect():
        yield conn
        conn.commit()

    monkeypatch.setattr(fb_store, "_connect", fake_connect)

    fb_store.upsert_group("dev1", "潮味", status="joined")
    fb_store.upsert_group("dev1", "潮味", status="discovered")
    st = fb_store.group_visit_state("dev1", "潮味")

    assert st["status"] == "joined"

    fb_store.mark_group_visit("dev1", "潮味", extracted_count=3)
    st2 = fb_store.group_visit_state("dev1", "潮味")
    assert st2["visit_count"] == 1
    assert st2["extracted_member_count"] == 3


def test_fb_store_rejects_and_filters_metadata_only_group_rows(monkeypatch):
    from src.host import fb_store

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE facebook_groups ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "device_id TEXT NOT NULL,"
        "group_name TEXT NOT NULL,"
        "group_url TEXT DEFAULT '',"
        "member_count INTEGER DEFAULT 0,"
        "language TEXT DEFAULT '',"
        "country TEXT DEFAULT '',"
        "status TEXT NOT NULL DEFAULT 'joined',"
        "joined_at TEXT NOT NULL,"
        "last_visited_at TEXT,"
        "visit_count INTEGER DEFAULT 0,"
        "extracted_member_count INTEGER DEFAULT 0,"
        "preset_key TEXT DEFAULT '',"
        "UNIQUE(device_id, group_name))"
    )

    @contextmanager
    def fake_connect():
        yield conn
        conn.commit()

    monkeypatch.setattr(fb_store, "_connect", fake_connect)

    assert fb_store.upsert_group("dev1", "公开 · 9", status="pending") == 0
    assert conn.execute("SELECT COUNT(*) FROM facebook_groups").fetchone()[0] == 0

    conn.execute(
        "INSERT INTO facebook_groups"
        " (device_id, group_name, status, joined_at, member_count)"
        " VALUES (?,?,?,?,?)",
        ("dev1", "公开 · 7", "pending", "2026-05-01T00:00:00Z", 7383),
    )
    fb_store.upsert_group("dev1", "我がペットの日常", status="pending")

    rows = fb_store.list_unvisited_groups("dev1")
    assert [r["group_name"] for r in rows] == ["我がペットの日常"]


def test_fb_store_group_filter_does_not_filter_inbox_rows(monkeypatch):
    from src.host import fb_store

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE facebook_inbox_messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "device_id TEXT NOT NULL,"
        "peer_name TEXT NOT NULL,"
        "peer_type TEXT DEFAULT 'friend',"
        "message_text TEXT DEFAULT '',"
        "direction TEXT DEFAULT 'incoming',"
        "ai_decision TEXT DEFAULT '',"
        "ai_reply_text TEXT DEFAULT '',"
        "language_detected TEXT DEFAULT '',"
        "seen_at TEXT NOT NULL,"
        "sent_at TEXT,"
        "replied_at TEXT,"
        "lead_id INTEGER,"
        "preset_key TEXT DEFAULT '',"
        "template_id TEXT DEFAULT '')"
    )

    @contextmanager
    def fake_connect():
        yield conn
        conn.commit()

    monkeypatch.setattr(fb_store, "_connect", fake_connect)

    fb_store.record_inbox_message(
        "dev1", "Yoshikazu Sakai", message_text="hello", direction="incoming"
    )
    rows = fb_store.list_inbox_messages("dev1")
    assert len(rows) == 1
    assert rows[0]["peer_name"] == "Yoshikazu Sakai"


def test_discover_groups_falls_back_to_all_results_section(monkeypatch):
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb._did = MagicMock(return_value="dev1")
    d = MagicMock()
    fb._u2 = MagicMock(return_value=d)
    fb.guarded = MagicMock()
    fb.guarded.return_value.__enter__ = lambda *a: None
    fb.guarded.return_value.__exit__ = lambda *a: None
    fb._tap_search_bar_preferred = MagicMock(return_value=True)
    fb._type_fb_search_query = MagicMock(return_value=True)
    fb._submit_fb_search_with_verify = MagicMock(return_value=True)
    fb._tap_search_results_groups_filter = MagicMock(return_value=True)
    fb._extract_group_search_results = MagicMock(return_value=[
        {"group_name": "潮味决士林中正店", "member_count": 385,
         "requires_join": True},
    ])

    monkeypatch.setattr(
        "src.app_automation.facebook.hierarchy_looks_like_fb_groups_filtered_results_page",
        lambda _xml: False,
    )
    monkeypatch.setattr(
        "src.host.fb_store.has_group_been_visited",
        lambda _did, _name: False,
    )
    saved = []
    monkeypatch.setattr(
        "src.host.fb_store.upsert_group",
        lambda did, name, **kw: saved.append((did, name, kw)) or 1,
    )

    out = fb.discover_groups_by_keyword("潮味", max_groups=3)

    assert out[0]["group_name"] == "潮味决士林中正店"
    assert saved and saved[0][1] == "潮味决士林中正店"
    assert saved[0][2]["status"] == "pending"


def test_join_group_continues_when_groups_chip_missing_but_group_visible(monkeypatch):
    from src.app_automation import facebook as fb_mod
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb._did = MagicMock(return_value="dev1")

    d = MagicMock()
    d.dump_hierarchy.return_value = (
        '<hierarchy><node text="猫・ネコ・こねこ♡猫好き集合" /></hierarchy>'
    )
    fb._u2 = MagicMock(return_value=d)
    fb.guarded = MagicMock()
    fb.guarded.return_value.__enter__ = lambda *a: None
    fb.guarded.return_value.__exit__ = lambda *a: None
    fb._tap_search_bar_preferred = MagicMock(return_value=True)
    fb._type_fb_search_query = MagicMock(return_value=True)
    fb._submit_fb_search_with_verify = MagicMock(return_value=True)
    fb._tap_search_results_groups_filter = MagicMock(return_value=False)
    fb._extract_group_search_results = MagicMock(return_value=[{
        "group_name": "猫・ネコ・こねこ♡猫好き集合",
        "member_count": 136000,
        "requires_join": True,
    }])
    fb._tap_join_button_near_group_result = MagicMock(return_value=False)
    fb._tap_first_search_result_group = MagicMock(return_value=True)
    fb._classify_join_group_page = MagicMock(
        return_value="already_joined_or_accessible")
    fb._continue_group_welcome_if_present = MagicMock()

    monkeypatch.setattr(
        fb_mod, "hierarchy_looks_like_fb_search_surface", lambda _xml: True)
    monkeypatch.setattr(
        fb_mod, "hierarchy_looks_like_fb_groups_filtered_results_page",
        lambda _xml: False,
    )

    assert fb.join_group("猫・ネコ・こねこ♡猫好き集合", device_id="dev1") is True
    assert fb.last_join_group_outcome == "already_joined_or_accessible"
    fb._tap_first_search_result_group.assert_called_once()


def test_type_fb_search_query_refuses_external_foreground():
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb._did = MagicMock(return_value="dev1")
    fb.hb = MagicMock()

    d = MagicMock()
    d.app_current.return_value = {
        "package": "com.google.android.googlequicksearchbox",
    }
    d.dump_hierarchy.return_value = (
        '<hierarchy><node class="android.widget.EditText" text="" /></hierarchy>'
    )

    assert fb._type_fb_search_query(d, "ペット", "dev1") is False
    fb.hb.type_text.assert_not_called()
    d.send_keys.assert_not_called()


def test_type_fb_search_query_requires_fb_search_surface():
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb._did = MagicMock(return_value="dev1")
    fb.hb = MagicMock()

    d = MagicMock()
    d.app_current.return_value = {"package": "com.facebook.katana"}
    d.dump_hierarchy.return_value = (
        '<hierarchy>'
        '<node class="android.widget.TextView" text="What is on your mind?" />'
        '<node class="android.widget.Button" content-desc="Search" />'
        '</hierarchy>'
    )

    assert fb._type_fb_search_query(d, "ペット", "dev1") is False
    fb.hb.type_text.assert_not_called()
    d.send_keys.assert_not_called()
