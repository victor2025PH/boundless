"""J-9 #184：知识库来源隔离与检索接通（2026-09-05）。

事故：桌面首装把生产机 ~105 条厂商产品话术随 knowledge_base.db 播进用户 KB →
用户的 AI 对用户的客户推销厂商产品。四道防线各有门禁：
  ① source 列 + 一次性回填（vendor 按分类 / system 按 template_key，幂等）；
  ② 检索层硬隔离（桌面模式 / 显式覆写下 vendor 不进 RRF、不占 top_k，与 enabled 无关）；
  ③ 管理面：按来源列表 / 整批清空只许 vendor|system|import（store 层 fail-closed）；
  ④ 首装不再带 knowledge_base.db，只播 3 条停用态格式示例（source=system，一次性）。
另钉 health 自检口径（hits_7d 取自 kb_query_log 的 hit=1）与「0 反馈显示暂无反馈」。
"""
from __future__ import annotations

import inspect
import re
import sqlite3
from pathlib import Path

import pytest

from src.utils import kb_store as kbs
from src.utils.kb_store import (
    KB_FORMAT_EXAMPLE_PREFIX,
    KB_FORMAT_EXAMPLES,
    KB_FORMAT_EXAMPLES_SEEDED_KEY,
    KB_SOURCE_BACKFILL_KEY,
    KnowledgeBaseStore,
    normalize_kb_source,
    seed_kb_format_examples,
    set_vendor_retrieval_excluded,
    vendor_retrieval_excluded,
)

ENGINE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_vendor_env(monkeypatch):
    """每例从「非桌面、无覆写」出发，结束后清掉进程级覆写（模块全局，跨例会泄漏）。"""
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    monkeypatch.delenv("AITR_KB_EXCLUDE_VENDOR", raising=False)
    set_vendor_retrieval_excluded(None)
    yield
    set_vendor_retrieval_excluded(None)


@pytest.fixture
def store(tmp_path):
    return KnowledgeBaseStore(tmp_path / "kb.db")


def _add(store, title, *, source=None, category="常规咨询", triggers=None,
         enabled=1, template_key="", entry_id=None):
    data = {
        "title": title,
        "category": category,
        "triggers": triggers or [title],
        "scenario": title,
        "steps": "s",
        "principles": "p",
        "example_reply_zh": f"{title} 的示例回复",
        "enabled": enabled,
        "template_key": template_key,
    }
    if source is not None:
        data["source"] = source
    if entry_id:
        data["id"] = entry_id
    return store.add_entry(data)


# ── ① source 列与回填 ──────────────────────────────────────────

def test_normalize_source_unknown_falls_back_to_user():
    assert normalize_kb_source("vendor") == "vendor"
    assert normalize_kb_source(" IMPORT ") == "import"
    assert normalize_kb_source("bogus") == "user"
    assert normalize_kb_source(None) == "user"


def test_add_entry_source_defaults(store):
    plain = _add(store, "营业时间")
    tpl = _add(store, "模板话术", template_key="greet")
    imp = _add(store, "导入条", source="import")
    ven = _add(store, "厂商条", source="vendor")
    by_id = {e["id"]: e for e in store.list_entries()}
    assert by_id[plain]["source"] == "user"
    assert by_id[tpl]["source"] == "system"     # template_key 非空 → system
    assert by_id[imp]["source"] == "import"
    assert by_id[ven]["source"] == "vendor"


def test_legacy_db_without_source_column_is_migrated_and_backfilled(tmp_path):
    """模拟内测包旧库：无 source 列，含厂商分类条目 / 模板条目 / 普通用户条目。"""
    db = tmp_path / "legacy.db"
    first = KnowledgeBaseStore(db)
    with first._conn() as c:
        c.execute("DROP INDEX IF EXISTS idx_kb_source")
        c.execute("ALTER TABLE kb_entries DROP COLUMN source")
        c.execute("DELETE FROM kb_meta WHERE k=?", (KB_SOURCE_BACKFILL_KEY,))
        now = "2026-01-01T00:00:00"
        for eid, cat, tk in (
            ("v1", "产品介绍", ""), ("v2", "价格与支付", ""),
            ("s1", "常规咨询", "tpl_hi"), ("u1", "常规咨询", ""),
        ):
            c.execute(
                "INSERT INTO kb_entries (id,category,title,triggers,scenario,steps,"
                "principles,example_reply_zh,forbidden,enabled,use_count,rating,"
                "reply_mode,template_key,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,1,0,0,'ai_guided',?,?,?)",
                (eid, cat, eid, "[]", "", "", "", "", "", tk, now, now),
            )
    cols = {r[1] for r in sqlite3.connect(db).execute("pragma table_info(kb_entries)")}
    assert "source" not in cols, "前置：旧库确实没有 source 列"

    second = KnowledgeBaseStore(db)   # 迁移 + 回填发生在 _init_db
    src = {e["id"]: e["source"] for e in second.list_entries()}
    assert src == {"v1": "vendor", "v2": "vendor", "s1": "system", "u1": "user"}
    assert second.get_meta(KB_SOURCE_BACKFILL_KEY)


def test_backfill_is_one_shot(tmp_path):
    """运营把某条 vendor 改回 user 后，下次启动不得再刷成 vendor。"""
    db = tmp_path / "kb.db"
    s1 = KnowledgeBaseStore(db)
    eid = _add(s1, "厂商条", category="产品介绍", source="vendor")
    assert s1.update_entry(eid, {"source": "user"})
    s2 = KnowledgeBaseStore(db)
    assert s2.get_entry(eid)["source"] == "user"


def test_list_entries_filters_by_source(store):
    _add(store, "a", source="user")
    _add(store, "b", source="vendor")
    _add(store, "c", source="import")
    assert {e["title"] for e in store.list_entries(source="vendor")} == {"b"}
    assert {e["title"] for e in store.list_entries(source="import")} == {"c"}
    assert len(store.list_entries()) == 3


# ── ② 检索层硬隔离 ─────────────────────────────────────────────

def test_vendor_exclusion_decision_order(monkeypatch):
    assert vendor_retrieval_excluded() is False            # 服务器部署默认不排除
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    assert vendor_retrieval_excluded() is True             # 桌面默认排除
    monkeypatch.setenv("AITR_KB_EXCLUDE_VENDOR", "0")
    assert vendor_retrieval_excluded() is False            # env 显式放行压过桌面
    set_vendor_retrieval_excluded(True)
    assert vendor_retrieval_excluded() is True             # 进程覆写最高


def test_search_hard_excludes_enabled_vendor_entries(store):
    """vendor 条目 enabled=1 且触发词完全命中，排除态下仍不得进结果（不靠 enabled 标志）。"""
    _add(store, "智聊价格", source="vendor", category="价格与支付",
         triggers=["智聊多少钱", "智聊价格"])
    _add(store, "营业时间", source="user", triggers=["营业时间"])

    set_vendor_retrieval_excluded(True)
    res = store.search("智聊多少钱", top_k=5)
    assert res["vendor_excluded"] is True
    assert all(e["source"] != "vendor" for e in res["entries"])

    res_admin = store.search("智聊多少钱", top_k=5, include_vendor=True)
    assert res_admin["vendor_excluded"] is False
    assert any(e["title"] == "智聊价格" for e in res_admin["entries"])

    set_vendor_retrieval_excluded(False)
    res_open = store.search("智聊多少钱", top_k=5)
    assert any(e["title"] == "智聊价格" for e in res_open["entries"])


def test_vendor_does_not_consume_top_k_slots(store):
    """排除必须发生在截断之前：满屏厂商条目不能把用户条目挤出 top_k。"""
    for i in range(6):
        _add(store, f"厂商价格{i}", source="vendor", category="价格与支付",
             triggers=["价格", "多少钱"])
    _add(store, "我的价格", source="user", triggers=["价格", "多少钱"])
    set_vendor_retrieval_excluded(True)
    res = store.search("价格 多少钱", top_k=2)
    titles = [e["title"] for e in res["entries"]]
    assert "我的价格" in titles
    assert not any(t.startswith("厂商价格") for t in titles)


def test_search_cache_keyed_by_exclusion(store):
    """同 query 先排除再放行：缓存不得把排除态结果回给放行态调用。"""
    _add(store, "厂商条", source="vendor", category="产品介绍", triggers=["厂商条"])
    set_vendor_retrieval_excluded(True)
    assert store.search("厂商条")["entries"] == []
    assert any(e["title"] == "厂商条"
               for e in store.search("厂商条", include_vendor=True)["entries"])


def test_desktop_mode_default_excludes_without_explicit_arg(store, monkeypatch):
    """skill_manager 调 _kb.search 不传 include_vendor → 桌面模式自动排除。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    _add(store, "厂商条", source="vendor", category="产品介绍", triggers=["厂商条"])
    res = store.search("厂商条")
    assert res["vendor_excluded"] is True and res["entries"] == []


def test_skill_manager_kb_calls_do_not_force_include_vendor():
    """只读核对（skill_manager.py 不可改）：对客链路的 _kb.search 调用不得显式放行 vendor。"""
    src = (ENGINE_ROOT / "src/skills/skill_manager.py").read_text(encoding="utf-8")
    assert "include_vendor=True" not in src
    # 命中记账仍在 skill_manager 的 kb_gate 注入点：health.hits_7d 的数据源
    assert "log_query(" in src


# ── ③ 管理面：stats / health / purge ───────────────────────────

def test_stats_source_breakdown_and_satisfaction_none_when_no_feedback(store):
    _add(store, "u", source="user")
    _add(store, "i", source="import")
    _add(store, "v", source="vendor")
    _add(store, "s", source="system")
    st = store.stats()
    assert st["entries_user"] == 2          # user + import 都算「用户的」
    assert st["entries_vendor"] == 1
    assert st["entries_system"] == 1
    assert st["by_source"] == {"user": 1, "import": 1, "vendor": 1, "system": 1}
    assert st["feedback"] == 0
    assert st["satisfaction_rate"] is None   # 不再假报 0.0%
    assert st["vendor_excluded"] is False


def test_health_hits_come_from_query_log(store):
    _add(store, "u", source="user")
    h0 = store.health()
    for k in ("entries_total", "entries_user", "entries_vendor", "entries_system",
              "embedded", "queries_7d", "hits_7d", "last_hit_ts", "vendor_excluded",
              "satisfaction_rate"):
        assert k in h0
    assert h0["hits_7d"] == 0 and h0["last_hit_ts"] == 0.0
    store.log_query("营业时间", hit=False)
    store.log_query("营业时间", hit=True, matched_entry_id="x")
    h1 = store.health()
    assert h1["queries_7d"] == 2
    assert h1["hits_7d"] == 1
    assert h1["last_hit_ts"] > 0


def test_purge_by_source_only_vendor_system_import(store):
    keep = _add(store, "我的", source="user")
    _add(store, "厂商1", source="vendor", category="产品介绍")
    _add(store, "厂商2", source="vendor", category="产品支持")
    assert store.purge_by_source("vendor") == 2
    assert store.purge_by_source("vendor") == 0
    assert store.get_entry(keep) is not None
    for bad in ("user", "bogus", "", None):
        with pytest.raises(ValueError):
            store.purge_by_source(bad)     # normalize 会折成 user，这里必须 fail-closed
    assert store.get_entry(keep) is not None


def test_kb_routes_expose_purge_and_health():
    from src.web.routes import kb_routes
    src = inspect.getsource(kb_routes)
    assert '"/api/kb/entries/purge-source"' in src
    assert '"/api/kb/health"' in src
    assert 'src not in ("vendor", "system", "import")' in src
    # 首装示例播种挂在 kb_routes（不是 admin.py）
    assert "seed_kb_format_examples(_kb_store)" in src


# ── ④ 首装：不带 KB、只播示例 ──────────────────────────────────

def test_format_examples_noop_outside_desktop(store):
    r = seed_kb_format_examples(store)
    assert r == {"added": 0, "skipped": 0, "reason": "not_desktop"}
    assert store.stats()["total_entries"] == 0


def test_format_examples_seed_once_on_fresh_desktop(store, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    r = seed_kb_format_examples(store)
    assert r["added"] == len(KB_FORMAT_EXAMPLES) == 3
    rows = store.list_entries()
    assert all(e["source"] == "system" for e in rows)
    assert all(int(e["enabled"]) == 0 for e in rows)          # 停用态：只教格式不对客
    assert all(e["title"].startswith(KB_FORMAT_EXAMPLE_PREFIX) for e in rows)
    assert store.get_meta(KB_FORMAT_EXAMPLES_SEEDED_KEY)
    # 用户删掉一条后再启动：不得灌回
    store.delete_entry(rows[0]["id"])
    r2 = seed_kb_format_examples(store)
    assert r2["reason"] == "already_seeded" and r2["added"] == 0
    assert store.stats()["total_entries"] == 2


def test_format_examples_skip_when_kb_in_use(store):
    """已有用户条目（含旧包升级上来带 vendor 的库）不需要「教格式」，且打标不再重试。"""
    _add(store, "我的", source="user")
    r = seed_kb_format_examples(store, force=True)
    assert r["added"] == 0 and r["reason"] == "kb_in_use"
    assert store.get_meta(KB_FORMAT_EXAMPLES_SEEDED_KEY) == "skipped_nonempty"
    assert store.stats()["total_entries"] == 1


def test_format_examples_excluded_from_customer_retrieval_by_enabled(store):
    """示例条目 enabled=0：即使触发词命中也不进对客检索（索引只装 enabled=1）。"""
    seed_kb_format_examples(store, force=True)
    res = store.search("退款 退货", top_k=5, include_vendor=True)
    assert res["entries"] == []


def test_desktop_seed_no_longer_ships_knowledge_base_db():
    from src.utils.config_manager import ConfigManager
    assert not any("knowledge_base.db" in x for x in ConfigManager._SEED_ASSET_ITEMS)

    after_pack = (ENGINE_ROOT / "desktop/build/after-pack.js").read_text(encoding="utf-8")
    forbidden_block = after_pack.split("FORBIDDEN", 1)[1]
    assert re.search(r'"seed-data",\s*"config",\s*"knowledge_base\.db"', forbidden_block)
    seed_required = after_pack.split("SEED_REQUIRED", 1)[1].split("]", 1)[0]
    assert "knowledge_base" not in seed_required

    stage = (ENGINE_ROOT / "desktop/build/stage_internal_assets.py").read_text(encoding="utf-8")
    # 暂存循环里只剩 persona_bio / persona_media 两个 sqlite 快照
    copy_loop = stage.split("for _name in (", 1)[1].split(")", 1)[0] if "for _name in (" in stage else ""
    assert "knowledge_base" not in copy_loop
    assert '"kb_entries": 50' not in stage


def test_howto_pack_carries_vendor_product_entries():
    """厂商产品说明的归宿是内置帮助（用户问助手），不是对客 KB。"""
    from src.assistant import howto_pack
    ids = {row[0] for row in howto_pack._HOWTO}
    assert {"vendor-product-lines", "vendor-support-contact", "kb-vendor-preset"} <= ids
