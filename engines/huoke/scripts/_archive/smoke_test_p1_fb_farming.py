# -*- coding: utf-8 -*-
"""P1 养号优化冒烟测试（2026-04-21）

验证：
  1. fb_playbook.yaml 热加载（改文件 mtime → 下次 load 自动生效）
  2. resolve_browse_feed_params 按 phase 返回差异化参数
  3. fb_account_phase 状态机：
     - 新设备自动落 cold_start
     - on_scrolls 累计 → 达标迁移到 growth
     - on_risk 触发 → 迁移到 cooldown
  4. fb_campaign_store: start_run → update_step → finish_run 幂等
  5. Dashboard API: /facebook/dashboard/ops 可返回完整结构

运行：
  python mobile-auto-project/scripts/smoke_test_p1_fb_farming.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PASS_MARK, FAIL_MARK = "PASS", "FAIL"


def _case(ok: bool, name: str, extra: str = "") -> bool:
    print(f"[{PASS_MARK if ok else FAIL_MARK}] {name}" + (f"  -- {extra}" if extra else ""))
    return ok


def test_playbook_phase_resolve():
    from src.host.fb_playbook import resolve_browse_feed_params
    cold = resolve_browse_feed_params("cold_start")
    growth = resolve_browse_feed_params("growth")
    mature = resolve_browse_feed_params("mature")
    cd = resolve_browse_feed_params("cooldown")
    return all([
        _case(cold["scroll_per_min"] == 2, "cold_start scroll_per_min=2",
              f"got={cold['scroll_per_min']}"),
        _case(growth["scroll_per_min"] == 4, "growth scroll_per_min=4"),
        _case(mature["scroll_per_min"] == 5, "mature scroll_per_min=5"),
        _case(cd["like_probability"] == 0.0, "cooldown 零点赞"),
        _case(cd["max_scrolls_hard_cap"] == 30, "cooldown 上限 30 屏"),
        _case(isinstance(cold["short_wait_ms"], tuple),
              "list→tuple 归一化", f"type={type(cold['short_wait_ms']).__name__}"),
    ])


def test_playbook_hotreload():
    """改文件 mtime → 下一次 get() 应重读。"""
    from src.host.fb_playbook import _CACHE, load_playbook
    path = _CACHE.path
    orig = load_playbook(force_reload=True)
    mt1 = _CACHE.mtime()
    # 触 mtime（读写同样内容即可）
    time.sleep(1.1)
    raw = path.read_bytes()
    path.write_bytes(raw)
    d2 = load_playbook()  # 不强制，应自动检测 mtime 变化
    mt2 = _CACHE.mtime()
    return all([
        _case(mt2 > mt1, "mtime 推进", f"{mt1:.2f} -> {mt2:.2f}"),
        _case(d2["defaults"]["browse_feed"]["scroll_per_min"] ==
              orig["defaults"]["browse_feed"]["scroll_per_min"],
              "热加载后数据一致"),
    ])


def test_account_phase_state_machine():
    from src.host import database as db
    from src.host.fb_account_phase import (get_phase, on_scrolls, on_risk,
                                            evaluate_transition)
    from src.host.fb_store import record_risk_event
    db.init_db()
    dev = f"TEST_PHASE_{uuid.uuid4().hex[:8]}"

    # 新设备 → cold_start
    p0 = get_phase(dev)

    # 累计 300 屏（> 200 阈值），但 first_seen_at 现在，age_hours<24 → 不该迁移
    on_scrolls(dev, 300)
    p1 = get_phase(dev)

    # 人为把 first_seen_at 改成 2 天前，模拟"养号到位"
    import sqlite3
    with sqlite3.connect(str(db.DB_PATH)) as conn:
        conn.execute("UPDATE fb_account_phase SET first_seen_at=datetime('now','-2 days') WHERE device_id=?", (dev,))
        conn.commit()
    t = evaluate_transition(dev)
    p2 = get_phase(dev)

    # 注入 3 条风控，触发 cooldown
    for i in range(3):
        record_risk_event(dev, f"Confirm your identity (event {i})",
                          task_id=f"t-{i}", debounce_seconds=0)
    on_risk(dev)   # 触发一次评估
    p3 = get_phase(dev)

    return all([
        _case(p0["phase"] == "cold_start", "新设备 → cold_start"),
        _case(p1["phase"] == "cold_start", "屏数达标但账号太新 → 仍 cold_start"),
        _case(t.get("to") == "growth", "年龄 + 屏数达标 → growth",
              f"transition={t}"),
        _case(p2["phase"] == "growth", "持久化生效：phase=growth"),
        _case(p3["phase"] == "cooldown", "3 次风控 → cooldown"),
    ])


def test_campaign_store_resume():
    from src.host import database as db
    from src.host.fb_campaign_store import (start_run, update_step,
                                             finish_run, get_run)
    db.init_db()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    dev = f"TEST_RUN_DEV_{uuid.uuid4().hex[:6]}"

    # 首次启动
    s1 = start_run(run_id, dev, task_id="task-1", total_steps=5)
    # 完成 warmup + group_engage
    update_step(run_id, 0, "warmup", {"steps_completed": ["warmup"], "extracted_members": 0})
    update_step(run_id, 1, "group_engage",
                {"steps_completed": ["warmup", "group_engage"],
                 "extracted_members": 0})
    r1 = get_run(run_id)

    # Resume: 同 run_id 再 start，应该把之前的 state 带回
    s2 = start_run(run_id, dev, task_id="task-1-retry", total_steps=5)

    finish_run(run_id, "completed",
               {"steps_completed": ["warmup", "group_engage", "extract_members",
                                     "add_friends", "check_inbox"]})
    r2 = get_run(run_id)

    return all([
        _case(s1 == {}, "首次启动 state 为空", f"got={s1}"),
        _case(r1["state"] == "running", "run state=running"),
        _case(r1["current_step_name"] == "group_engage", "current_step_name 正确"),
        _case(set(s2.get("steps_completed") or []) == {"warmup", "group_engage"},
              "Resume 时返回已完成步骤"),
        _case(r2["state"] == "completed", "finish_run 持久化 completed"),
    ])


def test_dashboard_ops_api():
    try:
        with urllib.request.urlopen("http://127.0.0.1:18080/facebook/dashboard/ops?hours=24", timeout=5) as resp:
            data = json.loads(resp.read().decode())
        return all([
            _case(data.get("ok") is True, "API ok=True"),
            _case("phases" in data and "counts" in data["phases"],
                  "phases.counts 存在"),
            _case("risks" in data and "events_total" in data["risks"],
                  "risks 汇总存在"),
            _case("funnel" in data and "stage_extracted_members" in data["funnel"],
                  "funnel 接入"),
            _case("campaign_runs" in data and "success_rate" in data["campaign_runs"],
                  "campaign_runs 汇总存在"),
        ])
    except Exception as e:
        return _case(False, "Dashboard API 可访问", f"err={e}")


def cleanup():
    import sqlite3
    from src.host import database as db
    with sqlite3.connect(str(db.DB_PATH)) as conn:
        n1 = conn.execute(
            "DELETE FROM fb_risk_events WHERE device_id LIKE 'TEST_%'"
        ).rowcount
        n2 = conn.execute(
            "DELETE FROM fb_account_phase WHERE device_id LIKE 'TEST_%'"
        ).rowcount
        n3 = conn.execute(
            "DELETE FROM fb_campaign_runs WHERE device_id LIKE 'TEST_%'"
        ).rowcount
        conn.commit()
    print(f"\n[cleanup] risks={n1} phases={n2} runs={n3}")


def main():
    print("=" * 60)
    print("P1 Facebook 养号优化 冒烟测试")
    print("=" * 60)
    results = []

    print("\n-- [1/5] Playbook phase 参数解析 --")
    results.append(test_playbook_phase_resolve())

    print("\n-- [2/5] Playbook 热加载（mtime 触发） --")
    results.append(test_playbook_hotreload())

    print("\n-- [3/5] 账号阶段状态机迁移 --")
    results.append(test_account_phase_state_machine())

    print("\n-- [4/5] campaign_runs store & resume --")
    results.append(test_campaign_store_resume())

    print("\n-- [5/5] Dashboard /facebook/dashboard/ops --")
    results.append(test_dashboard_ops_api())

    cleanup()
    passed = sum(1 for r in results if r)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"结果: {passed}/{total} 组通过")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
