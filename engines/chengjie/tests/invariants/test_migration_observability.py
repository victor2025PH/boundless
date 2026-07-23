"""数据完整性 · 迁移可观测（Sprint1）。

固定：InboxStore 迁移由 schema_migrations 版本表记录、可重复开库（幂等）、
正常路径 migration_errors==0（duplicate column 视为良性、不计错）。
"""
from src.inbox.store import InboxStore, _MIGRATIONS


def test_schema_migrations_recorded_and_no_errors(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    assert store.migration_errors == 0, "全新库迁移不应有真失败（duplicate 属良性）"
    with store._lock:
        n = store._conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    # 应记录全部（含 duplicate 良性）迁移条目
    assert n == len(_MIGRATIONS), f"应记录全部 {len(_MIGRATIONS)} 条迁移，实得 {n}"


def test_reopen_is_idempotent(tmp_path):
    db = tmp_path / "inbox.db"
    s1 = InboxStore(db)
    assert s1.migration_errors == 0
    # 二次开库：迁移应被版本表跳过，不再撞 duplicate、不报错
    s2 = InboxStore(db)
    assert s2.migration_errors == 0
    assert s2.list_conversations() == []


def test_migrations_applied_columns_exist(tmp_path):
    """迁移确实生效：抽查迁移新增列存在（如 conversations.chat_type / escalations.assigned_to）。"""
    store = InboxStore(tmp_path / "inbox.db")
    with store._lock:
        conv_cols = {r[1] for r in store._conn.execute("PRAGMA table_info(conversations)").fetchall()}
        esc_cols = {r[1] for r in store._conn.execute("PRAGMA table_info(escalations)").fetchall()}
    assert "chat_type" in conv_cols
    assert "assigned_to" in esc_cols
