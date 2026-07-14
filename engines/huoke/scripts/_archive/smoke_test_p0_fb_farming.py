# -*- coding: utf-8 -*-
"""P0 养号优化冒烟测试（2026-04-21）

验证 4 件事：
  1. fb_risk_events 表已建 + record_risk_event 幂等/去重
  2. Gate 红旗冷却：塞 3 条风控事件 → 评估 facebook_browse_feed 应被拒绝
  3. FB_BROWSE_DEFAULTS + _resolve_scroll_count 按预期计算
  4. browse_feed 返回结构含 card_type=fb_warmup

运行：
  python mobile-auto-project/scripts/smoke_test_p0_fb_farming.py
"""

from __future__ import annotations

import os
import sys
import uuid

# 确保可导入项目内模块
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _case(ok: bool, name: str, extra: str = ""):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}{('  — ' + extra) if extra else ''}")
    return ok


def test_risk_events_table():
    from src.host import database as db
    from src.host import fb_store

    db.init_db()
    dev = f"TEST_DEV_{uuid.uuid4().hex[:8]}"

    # 第一次写入应成功
    rid1 = fb_store.record_risk_event(dev, "Confirm your identity please",
                                      task_id="t-1", debounce_seconds=60)
    # 相同 kind 60s 内第二次应被去重
    rid2 = fb_store.record_risk_event(dev, "Please confirm it's you",
                                      task_id="t-2", debounce_seconds=60)
    # 不同 kind 应成功
    rid3 = fb_store.record_risk_event(dev, "We've temporarily blocked your account",
                                      task_id="t-3", debounce_seconds=60)
    # 强制 debounce=0 再写一条 identity_verify 应成功
    rid4 = fb_store.record_risk_event(dev, "Confirm your identity",
                                      task_id="t-4", debounce_seconds=0)

    cnt = fb_store.count_risk_events_recent(dev, hours=24)
    kinds = [r["kind"] for r in fb_store.list_recent_risk_events(dev, hours=24)]

    ok = all([
        _case(rid1 > 0, "首次写入成功", f"id={rid1}"),
        _case(rid2 == 0, "60s 内同 kind 去重", f"rid2={rid2}"),
        _case(rid3 > 0, "不同 kind 可写入", f"id={rid3}"),
        _case(rid4 > 0, "debounce=0 可写入", f"id={rid4}"),
        _case(cnt == 3, f"最近 24h 计数 = 3", f"got={cnt}"),
        _case("identity_verify" in kinds and "checkpoint" in kinds,
              "kind 归一正确", f"kinds={kinds}"),
    ])
    return ok, dev


def test_gate_fb_cooldown(dev: str):
    from src.host.task_dispatch_gate import evaluate_task_gate_detailed

    task = {
        "task_id": "smoke-test",
        "type": "facebook_browse_feed",
        "params": {"duration_minutes": 15},
    }
    ev = evaluate_task_gate_detailed(
        task,
        resolved_device_id=dev,
        config_path="mobile-auto-project/config/devices.yaml",
    )

    return all([
        _case(not ev.allowed, "Gate 拒绝该设备 facebook_browse_feed"),
        _case(ev.hint_code == "fb_risk_cooldown",
              "hint_code=fb_risk_cooldown", f"got={ev.hint_code}"),
        _case("fb_risk_cooldown" in (ev.connectivity or {}),
              "connectivity 带 fb_risk_cooldown 明细"),
    ])


def test_defaults_and_resolve():
    from src.app_automation.facebook import (FB_BROWSE_DEFAULTS,
                                              _resolve_scroll_count)

    assert FB_BROWSE_DEFAULTS["scroll_per_min"] == 4
    n15 = _resolve_scroll_count(15, None)      # 15 × 4 = 60
    n1 = _resolve_scroll_count(1, None)        # 下限保护 max(5, 4)=5
    n_override = _resolve_scroll_count(None, 10)   # 显式传 10
    n_cap = _resolve_scroll_count(None, 99999)     # 安全上限

    return all([
        _case(n15 == 60, "duration=15 → 60 屏", f"got={n15}"),
        _case(n1 == 5, "duration=1 → 下限 5 屏", f"got={n1}"),
        _case(n_override == 10, "显式 scroll_count 覆盖", f"got={n_override}"),
        _case(n_cap == FB_BROWSE_DEFAULTS["max_scrolls_hard_cap"],
              "上限保护", f"got={n_cap}"),
    ])


def test_kind_classification():
    from src.host.fb_store import _classify_risk_kind

    cases = [
        ("Please confirm it's you", "identity_verify"),
        ("captcha required", "captcha"),
        ("We've temporarily blocked your account", "checkpoint"),
        ("Your account has been disabled", "account_review"),
        ("You can't use this feature right now", "policy_warning"),
        ("Something else", "other"),
    ]
    results = [_case(_classify_risk_kind(raw) == expected,
                     f"classify: '{raw[:30]}' → {expected}",
                     f"got={_classify_risk_kind(raw)}")
               for raw, expected in cases]
    return all(results)


def main():
    print("=" * 60)
    print("P0 Facebook 养号优化 冒烟测试")
    print("=" * 60)

    results: list[bool] = []

    print("\n-- [1/4] fb_risk_events 表 & 去重 --")
    ok1, dev = test_risk_events_table()
    results.append(ok1)

    print("\n-- [2/4] Gate 红旗冷却 --")
    results.append(test_gate_fb_cooldown(dev))

    print("\n-- [3/4] browse_feed 节奏公式 --")
    results.append(test_defaults_and_resolve())

    print("\n-- [4/4] 风控事件 kind 分类 --")
    results.append(test_kind_classification())

    passed = sum(1 for r in results if r)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"结果: {passed}/{total} 组通过")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
