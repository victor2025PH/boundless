"""AuditStore SQLite + JSONL 迁移 测试"""

import json
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.audit_store import AuditStore


@pytest.fixture
def store(tmp_path):
    return AuditStore(db_path=tmp_path / "audit.db")


class TestBasicOps:
    def test_log_and_query(self, store):
        store.log("u1", "update_template", "greeting", "old", "new")
        entries = store.query(limit=10)
        assert len(entries) == 1
        assert entries[0]["action"] == "update_template"
        assert entries[0]["user_id"] == "u1"

    def test_last_entry(self, store):
        store.log("u1", "a1")
        store.log("u2", "a2")
        last = store.last_entry()
        assert last["action"] == "a2"
        assert last["user_id"] == "u2"

    def test_query_filter_action(self, store):
        store.log("u1", "update_rate")
        store.log("u1", "rollback")
        store.log("u1", "update_rate")
        results = store.query(action="update_rate")
        assert all(r["action"] == "update_rate" for r in results)
        assert len(results) == 2

    def test_query_filter_user(self, store):
        store.log("admin", "a1")
        store.log("other", "a2")
        results = store.query(user_id="admin")
        assert len(results) == 1
        assert results[0]["user_id"] == "admin"

    def test_query_limit(self, store):
        for i in range(20):
            store.log("u1", f"action_{i}")
        assert len(store.query(limit=5)) == 5

    def test_empty_query(self, store):
        assert store.query() == []
        assert store.last_entry() is None


class TestActionPatterns:
    """action_patterns 下推（LIKE + ESCAPE，模式源=audit_display.family_like_patterns）"""

    def test_prefix_pattern_with_escaped_underscore(self, store):
        store.log("u1", "episodic_delete", "42")
        store.log("u1", "episodicx_delete", "x")   # 下划线是字面量，不得当通配吞掉 x
        store.log("u1", "kb_delete_entry", "e1")
        rows = store.query(action_patterns=["episodic\\_%"])
        assert [r["action"] for r in rows] == ["episodic_delete"]

    def test_contains_patterns_or_semantics(self, store):
        store.log("u1", "kb_delete_entry")
        store.log("u1", "persona_legacy_remove")
        store.log("u1", "save_settings")
        rows = store.query(action_patterns=["%delete%", "%remove%"])
        assert {r["action"] for r in rows} == {"kb_delete_entry", "persona_legacy_remove"}

    def test_patterns_combine_with_other_filters(self, store):
        store.log("admin", "episodic_delete", "1")
        store.log("other", "episodic_delete", "2")
        rows = store.query(action_patterns=["episodic\\_%"], user_id="admin")
        assert len(rows) == 1 and rows[0]["target"] == "1"

    def test_none_patterns_means_no_filter(self, store):
        store.log("u1", "a1")
        assert len(store.query(action_patterns=None)) == 1
        assert len(store.query(action_patterns=[])) == 1


class TestActionsSince:
    def test_actions_since_boundary(self, store):
        store._conn.execute(
            "INSERT INTO audit_log (ts, user_id, action, target, old_val, new_val, snapshot_id) "
            "VALUES ('2026-08-04 23:59:59', 'u', 'old_action', '', '', '', '')")
        store._conn.execute(
            "INSERT INTO audit_log (ts, user_id, action, target, old_val, new_val, snapshot_id) "
            "VALUES ('2026-08-05 00:00:00', 'u', 'new_action', '', '', '', '')")
        store._conn.commit()
        acts = store.actions_since("2026-08-05 00:00:00")
        assert acts == ["new_action"]


class TestJSONLMigration:
    def test_migrate_from_jsonl(self, tmp_path):
        jsonl = tmp_path / "audit_log.jsonl"
        entries = [
            {"ts": "2026-01-01 10:00:00", "user": "admin", "action": "update_rate",
             "target": "ep", "old": "0.5%", "new": "1.0%", "snap": "exchange_rates_111"},
            {"ts": "2026-01-01 10:01:00", "user": "admin", "action": "rollback",
             "target": "", "old": "", "new": "", "snap": ""},
        ]
        jsonl.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries), encoding="utf-8")

        store = AuditStore(db_path=tmp_path / "audit.db", legacy_jsonl_path=jsonl)
        results = store.query(limit=100)
        assert len(results) == 2
        assert results[0]["action"] == "update_rate"
        assert results[0]["snapshot_id"] == "exchange_rates_111"
        assert not jsonl.exists()
        assert (tmp_path / "audit_log.jsonl.bak").exists()

    def test_no_double_migrate(self, tmp_path):
        jsonl = tmp_path / "audit_log.jsonl"
        jsonl.write_text(json.dumps({"ts": "", "user": "u", "action": "a",
                                     "target": "", "old": "", "new": "", "snap": ""}), encoding="utf-8")
        store = AuditStore(db_path=tmp_path / "audit.db", legacy_jsonl_path=jsonl)
        assert len(store.query()) == 1
        (tmp_path / "audit_log.jsonl.bak").rename(jsonl)
        store2 = AuditStore(db_path=tmp_path / "audit.db", legacy_jsonl_path=jsonl)
        assert len(store2.query()) == 1


class TestSqlPaginationAndFilters:
    def test_newest_first_offset_and_count(self, store):
        for i in range(5):
            store.log("u1", f"a{i}", str(i))
        assert store.count() == 5
        page1 = store.query(limit=2, offset=0, newest_first=True)
        page2 = store.query(limit=2, offset=2, newest_first=True)
        assert [r["action"] for r in page1] == ["a4", "a3"]
        assert [r["action"] for r in page2] == ["a2", "a1"]
        # 默认旧→新契约不动（CSV）
        chrono = store.query(limit=2)
        assert [r["action"] for r in chrono] == ["a3", "a4"]

    def test_until_and_keyword(self, store):
        store._conn.execute(
            "INSERT INTO audit_log (ts, user_id, action, target, old_val, new_val, snapshot_id) "
            "VALUES ('2026-08-04 12:00:00', 'admin', 'kb_delete_entry', 'x', '', '', '')")
        store._conn.execute(
            "INSERT INTO audit_log (ts, user_id, action, target, old_val, new_val, snapshot_id) "
            "VALUES ('2026-08-05 12:00:00', 'admin', 'update_template', 'greet', '', '', '')")
        store._conn.commit()
        assert store.count(until="2026-08-04 23:59:59") == 1
        assert store.count(keyword="greet") == 1
        assert store.distinct_actions(user_id="admin") == ["kb_delete_entry", "update_template"]


class TestDangerAlert:
    def test_danger_log_publishes_once_per_minute(self, store, monkeypatch):
        published = []

        class _Bus:
            def publish(self, etype, data):
                published.append((etype, data))

        monkeypatch.setattr(
            "src.integrations.shared.event_bus.get_event_bus", lambda: _Bus())
        import src.utils.audit_store as mod
        mod._danger_alert_last.clear()
        store.log("admin", "episodic_delete", "42", "memo", "")
        store.log("admin", "episodic_delete", "43", "memo2", "")  # 同 actor×action 节流
        store.log("admin", "update_template", "k")  # 非高危
        assert len(published) == 1
        assert published[0][0] == "audit_danger_alert"
        assert published[0][1]["action"] == "episodic_delete"
        assert published[0][1]["rate_key"] == "audit_danger:admin:episodic_delete"
