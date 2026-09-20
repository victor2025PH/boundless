# -*- coding: utf-8 -*-
"""融合实例 P2 合库迁移 CLI 门禁。

覆盖：dry-run 零写入 / 实写行数校验 / 幂等重跑 inserted=0 / 列交集容差 /
messages→FTS 同步 / 账号凭证 源钥→目标钥 换密 + business_line 标签 /
密文解不开跳过账号并 FAIL / 同钥透传 / web_users 同名冲突跳过 / 备份产物。
全部用 tmp_path 微型库（与生产同名表同关键列），不碰任何真实实例数据。
"""
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.merge_instance_data import (
    _ENC_PREFIX,
    recrypt_meta_json,
    run_merge,
)

cryptography = pytest.importorskip("cryptography")
from cryptography.fernet import Fernet  # noqa: E402


def _mk_db(path: Path, ddl: str, rows: list = ()):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(ddl)
    for sql, args in rows:
        conn.execute(sql, args)
    conn.commit()
    conn.close()


_INBOX_DDL = """
CREATE TABLE conversations (
  conversation_id TEXT PRIMARY KEY, platform TEXT, account_id TEXT,
  chat_key TEXT, display_name TEXT, language TEXT, last_text TEXT,
  last_ts REAL, unread INTEGER DEFAULT 0);
CREATE TABLE messages (
  message_id TEXT PRIMARY KEY, conversation_id TEXT, direction TEXT,
  text TEXT, ts REAL);
CREATE VIRTUAL TABLE messages_fts USING fts5(
  message_id, conversation_id, text, ts, direction);
CREATE TABLE conversation_settings (
  conversation_id TEXT PRIMARY KEY, automation_mode TEXT, updated_at REAL);
"""

_TM_DDL = """
CREATE TABLE translation_memory (
  cache_key TEXT PRIMARY KEY, source_text TEXT, translated_text TEXT,
  source_lang TEXT, target_lang TEXT, engine TEXT);
"""

_KB_DDL = """
CREATE TABLE kb_entries (id TEXT PRIMARY KEY, category TEXT, title TEXT,
  example_reply_zh TEXT, enabled INTEGER DEFAULT 1,
  template_key TEXT DEFAULT '');
CREATE UNIQUE INDEX idx_kb_template_key ON kb_entries(template_key)
  WHERE template_key != '';
CREATE TABLE kb_error_codes (id TEXT PRIMARY KEY, code TEXT);
CREATE TABLE kb_rules (id TEXT PRIMARY KEY, description TEXT);
"""

_USERS_DDL = """
CREATE TABLE web_users (id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE, pw_salt BLOB, pw_hash BLOB, role TEXT,
  display_name TEXT, enabled INTEGER DEFAULT 1);
"""

_REG_DDL = """
CREATE TABLE platform_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,
  account_id TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'device',
  label TEXT NOT NULL DEFAULT '', proxy_id TEXT NOT NULL DEFAULT '',
  fingerprint_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  meta_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0,
  last_online_at REAL NOT NULL DEFAULT 0, UNIQUE(platform, account_id));
"""
# 目标侧带 business_line（P1 之后的生产形态）
_REG_DDL_TARGET = _REG_DDL.replace(
    "meta_json TEXT NOT NULL DEFAULT '{}'",
    "business_line TEXT NOT NULL DEFAULT '', meta_json TEXT NOT NULL DEFAULT '{}'")


def _write_key(path: Path) -> bytes:
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    return key


def _mk_instance(root: Path, *, target: bool, src_key_bytes: bytes = None):
    cfg = root / "config"
    key = src_key_bytes or _write_key(cfg / "registry.key")
    if src_key_bytes:
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "registry.key").write_bytes(src_key_bytes)
    f = Fernet(key)
    enc = _ENC_PREFIX + f.encrypt(b"SESSION-SECRET-src").decode("ascii")
    _mk_db(cfg / "inbox.db", _INBOX_DDL, rows=[
        ("INSERT INTO conversations (conversation_id, platform, account_id, chat_key, last_text, last_ts) VALUES (?,?,?,?,?,?)",
         (("telegram:src:1", "telegram", "srcacct", "1", "hola", 1.0)
          if not target else
          ("telegram:dst:9", "telegram", "dstacct", "9", "hey", 2.0))),
        ("INSERT INTO messages (message_id, conversation_id, direction, text, ts) VALUES (?,?,?,?,?)",
         (("telegram:src:1:m1", "telegram:src:1", "in", "hola amigo", 1.0)
          if not target else
          ("telegram:dst:9:m1", "telegram:dst:9", "in", "hey there", 2.0))),
    ])
    if not target:
        conn = sqlite3.connect(str(cfg / "inbox.db"))
        conn.execute(
            "INSERT INTO messages (message_id, conversation_id, direction, text, ts) VALUES (?,?,?,?,?)",
            ("telegram:src:1:m2", "telegram:src:1", "out", "buenas", 1.5))
        conn.execute(
            "INSERT INTO conversation_settings VALUES (?,?,?)",
            ("telegram:src:1", "manual", 1.0))
        conn.commit()
        conn.close()
    _mk_db(cfg / "translation_memory.db", _TM_DDL, rows=[
        ("INSERT INTO translation_memory VALUES (?,?,?,?,?,?)",
         (("ck-src-1", "hello", "你好", "en", "zh", "ollama_mt")
          if not target else
          ("ck-dst-1", "bye", "再见", "en", "zh", "ollama_mt"))),
    ])
    # kb_entries 复刻 2026-07-24 首切事故拓扑：id=内容哈希（两侧天然不同），
    # 模板身份在 template_key（部分唯一索引 WHERE != ''）——
    # 两侧各自播种同一模板包 → 同 template_key 不同 id，必须由守卫跳过。
    _mk_db(cfg / "knowledge_base.db", _KB_DDL, rows=[
        ("INSERT INTO kb_entries (id, category, title, template_key) VALUES (?,?,?,?)",
         (("kb-src-1", "翻译", "运费说明（通译版）", "tpl_shipping")
          if not target
          else ("kb-dst-1", "陪伴", "运费说明", "tpl_shipping"))),
        ("INSERT INTO kb_entries (id, category, title, template_key) VALUES (?,?,?,?)",
         (("kb-src-2", "翻译", "报价流程", "tpl_quote") if not target
          else ("kb-dst-2", "陪伴", "自由问答", ""))),  # 目标自由条目（空 tk）
        ("INSERT INTO kb_error_codes VALUES (?,?)",
         ("ec-shared", "X004")),  # 两侧同 id → 应跳过
    ])
    if not target:
        conn = sqlite3.connect(str(cfg / "knowledge_base.db"))
        # 源侧自由条目（空 tk）——与目标空 tk 行合法共存，守卫不得误拦
        conn.execute(
            "INSERT INTO kb_entries (id, category, title, template_key) "
            "VALUES (?,?,?,?)",
            ("kb-src-3", "翻译", "客户口头禅备注", ""))
        conn.commit()
        conn.close()
    _mk_db(cfg / "web_users.db", _USERS_DDL, rows=[
        ("INSERT INTO web_users (username, role, display_name) VALUES (?,?,?)",
         ("admin", "master", "管理员")),  # 两侧同名 → 冲突跳过
    ])
    if not target:
        conn = sqlite3.connect(str(cfg / "web_users.db"))
        conn.execute(
            "INSERT INTO web_users (username, role, display_name) VALUES (?,?,?)",
            ("agent_ty", "agent", "通译坐席"))
        conn.commit()
        conn.close()
    _mk_db(cfg / "account_registry.db",
           _REG_DDL_TARGET if target else _REG_DDL,
           rows=[
               ("INSERT INTO platform_accounts (platform, account_id, mode, status, meta_json) VALUES (?,?,?,?,?)",
                (("telegram", "88888", "protocol", "online",
                  json.dumps({"session_string": enc, "phone": "63999"}))
                 if not target else
                 ("telegram", "77777", "protocol", "online", "{}"))),
           ])
    return key


@pytest.fixture()
def instances(tmp_path):
    src_root = tmp_path / "src_data"
    dst_root = tmp_path / "dst_data"
    src_key = _mk_instance(src_root, target=False)
    dst_key = _mk_instance(dst_root, target=True)
    return src_root, dst_root, src_key, dst_key


def _counts(db: Path, table: str) -> int:
    conn = sqlite3.connect(str(db))
    n = conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
    conn.close()
    return n


def test_dry_run_writes_nothing(instances):
    src, dst, *_ = instances
    before = {t: _counts(dst / "config/inbox.db", t)
              for t in ("conversations", "messages")}
    report = run_merge(src, dst, apply=False)
    assert report["ok"] is True
    assert report["apply"] is False
    assert report["backups"] == []
    # 干跑报告出「将插入」计数，但目标零变化
    conv = next(t for t in report["tables"] if t["table"] == "conversations")
    assert conv["inserted"] == 1
    assert _counts(dst / "config/inbox.db", "conversations") == before["conversations"]
    assert _counts(dst / "config/inbox.db", "messages") == before["messages"]
    # 唯一索引守卫在干跑同判（干跑预测 ≡ 实写行为，收敛校验才可信）：
    # 同 template_key 模板 → skipped_guard，不进 inserted
    kb = next(t for t in report["tables"] if t["table"] == "kb_entries")
    assert kb["inserted"] == 2, kb          # tpl_quote + 空 tk 自由条目
    assert kb["skipped_guard"] == 1, kb     # tpl_shipping 两侧同模板


def test_apply_merges_and_is_idempotent(instances):
    src, dst, src_key, dst_key = instances
    report = run_merge(src, dst, apply=True)
    assert report["ok"] is True, report
    inbox = dst / "config/inbox.db"
    assert _counts(inbox, "conversations") == 2
    assert _counts(inbox, "messages") == 3
    assert _counts(inbox, "conversation_settings") == 1
    assert _counts(dst / "config/translation_memory.db", "translation_memory") == 2
    # kb：目标 2 + 源新模板 1 + 源自由条目 1 = 4；同模板 tpl_shipping 守卫跳过
    kb_db = dst / "config/knowledge_base.db"
    assert _counts(kb_db, "kb_entries") == 4
    conn = sqlite3.connect(str(kb_db))
    tks = [r[0] for r in conn.execute(
        "SELECT template_key FROM kb_entries WHERE template_key != ''")]
    conn.close()
    assert sorted(tks) == ["tpl_quote", "tpl_shipping"]  # 无重复模板
    kb_rep = next(t for t in report["tables"] if t["table"] == "kb_entries")
    assert kb_rep["skipped_guard"] == 1 and kb_rep["skipped_conflict"] == 0
    # 同 id 错误码不重复
    assert _counts(dst / "config/knowledge_base.db", "kb_error_codes") == 1
    # 用户：admin 冲突跳过，agent_ty 并入
    conn = sqlite3.connect(str(dst / "config/web_users.db"))
    users = {r[0] for r in conn.execute("SELECT username FROM web_users")}
    conn.close()
    assert users == {"admin", "agent_ty"}
    # FTS 同步：新消息可全文检索
    conn = sqlite3.connect(str(inbox))
    hit = conn.execute(
        "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'hola'"
    ).fetchall()
    conn.close()
    assert any("telegram:src:1:m1" in r[0] for r in hit)
    # 备份产物存在
    assert report["backups"] and all(Path(b).exists() for b in report["backups"])

    # 幂等重跑：inserted 全 0，行数不变（守卫行稳定停在 skipped_guard）
    report2 = run_merge(src, dst, apply=True, backup=False)
    assert report2["ok"] is True
    assert sum(int(t.get("inserted") or 0) for t in report2["tables"]) == 0
    assert _counts(inbox, "messages") == 3


def test_integrity_conflict_net_never_crashes(instances, monkeypatch):
    """守卫盲区兜底：约束自省被遮蔽（模拟看不见的复杂约束）时，
    IntegrityError 必须行级跳过点名，绝不炸穿整个实写（首切事故语义）。"""
    import scripts.merge_instance_data as mid

    src, dst, *_ = instances
    monkeypatch.setattr(mid, "_unique_guards", lambda *a, **k: [])
    report = mid.run_merge(src, dst, apply=True)
    kb = next(t for t in report["tables"] if t["table"] == "kb_entries")
    # tpl_shipping 撞唯一索引 → skipped_conflict 而非异常；其余行照常插入
    assert kb["skipped_conflict"] == 1, kb
    assert kb["inserted"] == 2, kb
    assert any("integrity skip" in n for n in kb["notes"])
    assert kb["ok"] is True  # 兜底跳过不判死（收敛校验会让它持续可见）
    # 后续表未被炸穿：账号照常并入
    acc = next(t for t in report["tables"] if t["table"] == "platform_accounts")
    assert acc["inserted"] == 1


def test_account_recrypted_and_tagged(instances):
    src, dst, src_key, dst_key = instances
    report = run_merge(src, dst, apply=True)
    acc = next(t for t in report["tables"] if t["table"] == "platform_accounts")
    assert acc["ok"] is True
    assert acc["inserted"] == 1
    assert acc["recrypted_fields"] == 1
    assert acc["credential_failures"] == []
    conn = sqlite3.connect(str(dst / "config/account_registry.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM platform_accounts WHERE account_id='88888'").fetchone()
    conn.close()
    assert row["business_line"] == "translation"
    meta = json.loads(row["meta_json"])
    ss = meta["session_string"]
    assert ss.startswith(_ENC_PREFIX)
    # 目标钥可解、源钥不可解 → 换密真的发生了
    token = ss[len(_ENC_PREFIX):].encode("ascii")
    assert Fernet(dst_key).decrypt(token) == b"SESSION-SECRET-src"
    with pytest.raises(Exception):
        Fernet(src_key).decrypt(token)
    assert meta["phone"] == "63999"  # 非敏感字段原样


def test_undecryptable_credentials_skip_account_and_fail(instances, tmp_path):
    src, dst, *_ = instances
    # 源 key 换成不相干的钥匙 → 密文解不开
    (src / "config" / "registry.key").write_bytes(Fernet.generate_key())
    report = run_merge(src, dst, apply=True)
    acc = next(t for t in report["tables"] if t["table"] == "platform_accounts")
    assert acc["ok"] is False
    assert acc["inserted"] == 0
    assert acc["credential_failures"]
    assert report["ok"] is False
    # 其它表不受影响照常迁移
    assert _counts(dst / "config/inbox.db", "messages") == 3


def test_same_key_passthrough(tmp_path):
    """两实例同钥（如共用引擎根 key）→ meta_json 原样透传仍可解。"""
    shared = Fernet.generate_key()
    src_root = tmp_path / "s"
    dst_root = tmp_path / "d"
    _mk_instance(src_root, target=False, src_key_bytes=shared)
    _mk_instance(dst_root, target=True, src_key_bytes=shared)
    report = run_merge(src_root, dst_root, apply=True)
    assert report["registry_key"]["same_key"] is True
    acc = next(t for t in report["tables"] if t["table"] == "platform_accounts")
    assert acc["ok"] is True and acc["inserted"] == 1
    assert acc["recrypted_fields"] == 0  # 透传不换密
    conn = sqlite3.connect(str(dst_root / "config/account_registry.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM platform_accounts WHERE account_id='88888'").fetchone()
    conn.close()
    meta = json.loads(row["meta_json"])
    tok = meta["session_string"][len(_ENC_PREFIX):].encode("ascii")
    assert Fernet(shared).decrypt(tok) == b"SESSION-SECRET-src"


def test_recrypt_meta_json_unit():
    k1, k2 = Fernet.generate_key(), Fernet.generate_key()
    f1, f2 = Fernet(k1), Fernet(k2)
    enc = _ENC_PREFIX + f1.encrypt(b"abc").decode("ascii")
    out, n, failed = recrypt_meta_json(
        json.dumps({"session_string": enc, "x": 1}), f1, f2)
    assert n == 1 and failed == []
    meta = json.loads(out)
    assert meta["x"] == 1
    assert Fernet(k2).decrypt(
        meta["session_string"][len(_ENC_PREFIX):].encode()) == b"abc"
    # 明文字段 → 目标钥加密
    out2, n2, _ = recrypt_meta_json(
        json.dumps({"two_fa_password": "pw"}), f1, f2)
    assert n2 == 1
    assert json.loads(out2)["two_fa_password"].startswith(_ENC_PREFIX)
    # 坏 JSON 容错
    out3, n3, failed3 = recrypt_meta_json("not-json", f1, f2)
    assert json.loads(out3) == {} and n3 == 0 and failed3 == []
