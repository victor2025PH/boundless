# -*- coding: utf-8 -*-
"""审计展示层纯函数门禁（src/web/audit_display.py，2026-08-05 首页卡人话化）。

重点钉住的不变量：
- 批量操作逐条记账在展示层折叠（×N），但**时间断层/换人/换动作不误并**；
- 排序恒最新在前（store.query 的旧→新契约不动，翻转发生在展示层）；
- 危险分类/target 语义化只对已知家族生效，认不出的绝不猜；
- /audit 分页第 1 页恒为最新一段。
"""

import re

from src.web.audit_display import (
    FAMILY_DEFS,
    action_icon_kind,
    build_recent_groups,
    classify_target,
    family_like_patterns,
    family_predicate,
    group_days,
    is_danger_action,
    operator_hue,
    paginate_newest_first,
    summarize_actions,
)


def _e(ts, action, op="admin", target="", old="", snap="", rid=0):
    return {
        "ts": ts, "action": action, "user_id": op, "target": target,
        "old_val": old, "new_val": "", "snapshot_id": snap, "id": rid,
    }


# ── 分类纯函数 ──────────────────────────────────────────────────────


def test_danger_classification():
    for a in ("episodic_delete", "episodic_bulk_delete", "kb_delete_entry",
              "revoke_all_sessions", "wa_pending_cancel_all", "identity_unlink",
              "batch_delete_templates", "persona_legacy_remove"):
        assert is_danger_action(a), a
    for a in ("save_settings", "kb_update_entry", "kb_restore", "persona_bind",
              "kb_add_entry", "update_template", "ack_incident", ""):
        assert not is_danger_action(a), a


def test_icon_kind_families():
    assert action_icon_kind("update_template") == "update"
    assert action_icon_kind("kb_add_entry") == "add"
    assert action_icon_kind("episodic_delete") == "delete"
    # unbind/unlink 必须先于 bind/link 命中删除族
    assert action_icon_kind("persona_unbind") == "delete"
    assert action_icon_kind("identity_unlink") == "delete"
    assert action_icon_kind("identity_link") == "add"
    assert action_icon_kind("ack_incident") == "other"
    assert action_icon_kind("kb_restore") == "update"


def test_classify_target_families():
    assert classify_target("episodic_delete", "42") == ("memory", "42")
    assert classify_target("episodic_confirm_inferred", "7") == ("memory", "7")
    assert classify_target("episodic_bulk_delete", "咖啡") == ("keyword", "咖啡")
    assert classify_target("identity_link", "tg:1|line:2") == ("identity", "tg:1|line:2")
    assert classify_target("update_template", "welcome") == ("generic", "welcome")
    assert classify_target("save_settings", "") == ("", "")


def test_operator_hue_stable_and_in_range():
    assert operator_hue("admin") == operator_hue("admin")
    assert 0 <= operator_hue("admin") < 360
    assert 0 <= operator_hue("") < 360


# ── 聚合 ────────────────────────────────────────────────────────────


def _burst(n=9, start_sec=33, op="admin"):
    """模拟批量删除逐条记账（截图实录）：42→34 两秒内逐条删，自增 id 随时间递增。"""
    rows = []
    for i in range(n):
        sec = start_sec + (0 if i < 5 else 1)
        rows.append(_e(f"2026-08-02 11:44:{sec:02d}", "episodic_delete", op=op,
                       target=str(42 - i), old=f"[ai_inferred] mem_{42 - i} | 内容{i}",
                       rid=1000 + i))
    return rows


def test_burst_aggregates_into_one_group():
    entries = _burst(9) + [
        _e("2026-08-02 10:00:00", "kb_update_entry", op="alice", target="faq-1", rid=1),
    ]
    groups = build_recent_groups(entries)
    assert len(groups) == 2
    g = groups[0]  # 最新在前
    assert g["action"] == "episodic_delete"
    assert g["count"] == 9
    assert g["target_kind"] == "memory"
    # 组内最新一条 = 最后删的 #34（同秒按自增 id 定序）
    assert g["targets_shown"] == ["34", "35", "36"]
    assert g["targets_more"] == 6
    assert g["danger"] is True
    # brief 取组内最新一条的 old_val（被删内容摘要）
    assert g["brief"].startswith("[ai_inferred] mem_34")
    assert g["ts"].endswith("11:44:34")
    assert g["hm"] == "11:44"
    assert groups[1]["action"] == "kb_update_entry"
    assert groups[1]["danger"] is False


def test_no_merge_across_time_gap():
    entries = [
        _e("2026-08-02 11:00:00", "episodic_delete", target="1", rid=1),
        _e("2026-08-02 11:10:00", "episodic_delete", target="2", rid=2),
    ]
    groups = build_recent_groups(entries, window_sec=90)
    assert [g["count"] for g in groups] == [1, 1]
    assert groups[0]["targets_shown"] == ["2"]  # 最新在前


def test_no_merge_across_operator_or_action():
    entries = [
        _e("2026-08-02 11:00:00", "episodic_delete", op="a", target="1", rid=1),
        _e("2026-08-02 11:00:01", "episodic_delete", op="b", target="2", rid=2),
        _e("2026-08-02 11:00:02", "kb_delete_entry", op="b", target="3", rid=3),
    ]
    assert len(build_recent_groups(entries)) == 3


def test_bad_ts_never_merges_nor_crashes():
    entries = [
        _e("not-a-ts", "episodic_delete", target="1", rid=1),
        _e("not-a-ts", "episodic_delete", target="2", rid=2),
    ]
    groups = build_recent_groups(entries)
    assert [g["count"] for g in groups] == [1, 1]


def test_snapshot_flag_survives_merge():
    entries = [
        _e("2026-08-02 11:00:00", "save_settings", target="a", rid=1),
        _e("2026-08-02 11:00:10", "save_settings", target="b", snap="snap-9", rid=2),
    ]
    groups = build_recent_groups(entries)
    assert len(groups) == 1
    assert groups[0]["snapshot"] is True


def test_max_groups_cap():
    entries = [
        _e(f"2026-08-02 0{i}:00:00", f"action_{i}", rid=i) for i in range(1, 10)
    ]
    assert len(build_recent_groups(entries, max_groups=4)) == 4


# ── 日期分段 / 摘要 ─────────────────────────────────────────────────


def test_group_days_kinds():
    groups = build_recent_groups([
        _e("2026-08-05 09:00:00", "save_settings", rid=3),
        _e("2026-08-04 23:00:00", "kb_add_entry", rid=2),
        _e("2026-08-01 08:00:00", "kb_import", rid=1),
    ])
    days = group_days(groups, today="2026-08-05", yesterday="2026-08-04")
    assert [d["kind"] for d in days] == ["today", "yesterday", "date"]
    assert days[2]["date"] == "2026-08-01"
    assert [len(d["items"]) for d in days] == [1, 1, 1]


def test_summarize_actions():
    s = summarize_actions(["episodic_delete", "save_settings", "kb_delete_entry"])
    assert s == {"total": 3, "danger": 2}
    assert summarize_actions([]) == {"total": 0, "danger": 0}


# ── family：谓词 ≡ SQL LIKE 模式（单源派生的一致性门禁）─────────────


def _like_match(pattern: str, text: str) -> bool:
    """SQLite ``LIKE ? ESCAPE '\\'`` 语义模拟：% 任意串、_ 单字符、\\ 转义、
    ASCII 大小写不敏感——把 SQL 侧行为搬进测试，谓词与模式跑同一语料对账。"""
    rx = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            rx.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        rx.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    return re.fullmatch("".join(rx), text, re.IGNORECASE) is not None


def _action_corpus():
    """语料 = 全词典真实动作 + 刻意构造的边界（前缀差一个字符/下划线通配陷阱）。"""
    from src.web.i18n_packs import audit_actions

    real = [k[len("aud_act_"):] for k in audit_actions.ZH if k.startswith("aud_act_")]
    synthetic = [
        "episodicx_delete",   # 危险族命中，但绝不该落 memory 族（episodic\_ 前缀不匹配）
        "kbx_entry",          # kb\_% 的下划线必须是字面量，不得当单字符通配吞掉 x
        "identityx",
        "profilex_tune",      # persona 族按 profile 前缀（无下划线）命中
        "myrpax_toggle",      # contains rpa
        "",
    ]
    return real + synthetic


def test_family_patterns_equal_predicates():
    corpus = _action_corpus()
    assert len(corpus) > 100
    for fam in FAMILY_DEFS:
        pred = family_predicate(fam)
        pats = family_like_patterns(fam)
        assert pred is not None and pats
        for action in corpus:
            expect = pred(action)
            got = any(_like_match(p, action) for p in pats)
            assert got == expect, f"family={fam} action={action!r}: SQL={got} pred={expect}"


def test_family_danger_predicate_is_single_source():
    pred = family_predicate("danger")
    for a in _action_corpus():
        assert pred(a) == is_danger_action(a), a


def test_family_unknown_returns_empty():
    assert family_like_patterns("") == []
    assert family_like_patterns("nope") == []
    assert family_predicate("nope") is None


def test_family_underscore_escaped_in_patterns():
    pats = family_like_patterns("memory")
    assert any(p.startswith("episodic\\_") for p in pats), pats
    assert not _like_match("episodic\\_%", "episodicx_foo")
    assert _like_match("episodic\\_%", "episodic_delete")


# ── /audit 倒序分页 ─────────────────────────────────────────────────


def test_paginate_newest_first_page1_is_newest():
    entries = [
        _e(f"2026-08-01 {h:02d}:{m:02d}:00", "save_settings", rid=h * 60 + m)
        for h in range(2) for m in range(60)
    ]  # 120 条，旧→新（模拟 AuditStore.query 契约）
    page1, total, pages, page = paginate_newest_first(entries, 1, 50)
    assert (total, pages, page) == (120, 3, 1)
    assert page1[0]["ts"] == "2026-08-01 01:59:00"   # 第 1 页第 1 条 = 全库最新
    assert page1[-1]["ts"] == "2026-08-01 01:10:00"  # 页内降序
    last, _, _, _ = paginate_newest_first(entries, 3, 50)
    assert last[-1]["ts"] == "2026-08-01 00:00:00"   # 末页末条 = 最旧


def test_paginate_clamps_page_and_handles_empty():
    entries = [_e("2026-08-01 00:00:00", "save_settings", rid=1)]
    _, total, pages, page = paginate_newest_first(entries, 99, 50)
    assert (total, pages, page) == (1, 1, 1)
    rows, total, pages, page = paginate_newest_first([], 0, 50)
    assert rows == [] and (total, pages, page) == (0, 1, 1)
