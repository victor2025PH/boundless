# -*- coding: utf-8 -*-
"""Shared test fixtures."""

import logging
import os
import tempfile
import pytest
from pathlib import Path

# 单测会启动 daemon/池化线程，pytest 结束并关闭 capture 的 stdout 后，这些线程仍可能写
# logging；StreamHandler.emit 会失败并触发 handleError 刷屏。默认关闭；调试时可设
# 环境变量 PYTEST_LOG_RAISE=1 恢复标准库的 raiseExceptions 行为。
if not os.environ.get("PYTEST_LOG_RAISE", "").strip() in ("1", "true", "yes", "y"):
    logging.raiseExceptions = False

os.environ.setdefault("OPENCLAW_API_KEY", "")
# 跳过设备 HealthMonitor 后台线程，避免 pytest 结束时 I/O on closed file（生产不设即可）
os.environ.setdefault("OPENCLAW_DISABLE_DEVICE_HEALTH_MONITOR", "1")
# 跳过启动时壁纸自动编号守护线程（单测/CI）
os.environ.setdefault("OPENCLAW_DISABLE_AUTO_WALLPAPER_THREAD", "1")
# AI 指令创建任务前的设备预检：默认关闭以免单测依赖真机/ADB
os.environ.setdefault("OPENCLAW_CHAT_PREFLIGHT", "0")
# 分流单测走规则，不调用真实 LLM（与生产 chat.yaml 的 llm_first 区分）
os.environ.setdefault("OPENCLAW_CHAT_TRIAGE_STRATEGY", "rules_first")
# 合并解析单测关闭，避免 Mock ChatAI 误走 parse_unified
os.environ.setdefault("OPENCLAW_UNIFIED_PARSE", "0")


@pytest.fixture
def tmp_db(tmp_path):
    """临时 SQLite 数据库，每个测试独立。"""
    import src.host.database as db_mod
    original = db_mod.DB_PATH
    db_mod.DB_PATH = tmp_path / "test.db"
    db_mod.init_db()
    yield db_mod.DB_PATH
    db_mod.DB_PATH = original


# ── P2-⑨ 跨测试状态污染防护 (autouse, 全套测试都生效) ──
#
# 根因:
#   全套 pytest 累计 fail 在涨 (1 → 2 → 6+), 全部"单独跑都 PASS"才挂.
#   多个 module 有 module-level dict/counter (_metrics / _device_fail_streak /
#   _RETRY_THROTTLE / _LAST_CLEANUP_TS / _cache / _state ...), 上一 test 留的状
#   态污染下一 test.
#
# 修复:
#   作者们已经在 8 个模块暴露了 reset_*_for_tests / reset_for_tests / _reset_*
#   helper. conftest autouse fixture 在每个 test 跑前后调一次, 让 module-level
#   全局回到"刚 import"状态.
#
#   设计要点:
#   - 失败时静默 (importlib 找不到 / 模块没装某个属性 → continue)
#     防 e.g. tests/test_unrelated.py 里没 import 某 module 的子集场景下 reset
#     抛 ImportError 把 test 自己拖死
#   - reset 在 test 跑*前*和*后*各一次. 前清以防上一 test 没自清; 后清以防本
#     test 出错跳出 fixture cleanup 把 dirty 留给下一 test.
#   - 不删既有 reset_state fixture (test_central_push_drain 已用), autouse 会
#     先跑, 然后 reset_state 再跑一次 (幂等 reset 无副作用).
def _p29_reset_global_state():
    """P2-⑨ 调所有已暴露的 reset helper, 静默吞 import / attr error."""
    import importlib

    # 形如 (module, fn_name): 调用 fn() 即重置.
    # 注意: 只挂"清运行期状态(metrics/cache/queue)"的 reset, **不挂**
    # gate_registry._reset_for_tests — 它清空 _TASK_GATES dict 但不重新调用
    # _register_default_gates(), 会让后续 test 看到空 registry → fail.
    # 这种"清后不重建"型 reset 必须 test 自己显式调用, 不适合 autouse.
    reset_targets = [
        ("src.host.central_push_client", "reset_push_metrics_for_tests"),
        # 2026-05-04 D.4: shutdown 旧 ThreadPoolExecutor + cancel_futures, 防
        # fire_and_forget task leak 到下一 test 调真 _http_post_json 后 inc
        # push_async_enqueue counter (test_metrics_async_enqueue_counter flaky).
        ("src.host.central_push_client", "reset_async_executor_for_tests"),
        ("src.host.central_push_drain", "reset_for_tests"),
        ("src.host.fb_concurrency", "reset_metrics_for_tests"),
        ("src.host.cluster_lock_client", "reset_caches_for_tests"),
        # 2026-05-04 F.2: _apply_tuning_adjustments mutate RISK_MULTIPLIERS
        # 让 phase5 TestAdaptiveCompliance 3 个 test (assume 默认值) 在全 suite
        # 跑序下 fail. reset 还原默认 dict + 清 singleton.
        ("src.behavior.adaptive_compliance", "reset_for_tests"),
        # 2026-05-05 Stage I: reverse heartbeat prober daemon stop+join,
        # 防孤儿 thread 跨 test 反向 probe 真 worker IP 引发网络副作用 +
        # 跨 test 写 cluster_state.json 污染.
        ("src.host.multi_host", "reset_reverse_prober_for_tests"),
    ]
    for mod_name, fn_name in reset_targets:
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                fn()
        except Exception:
            pass

    # P2-⑨ 涉及但作者没暴露 reset helper 的全局: inline 清
    inline_clears = [
        # (module_path, attribute_name, expected_type, action)
        ("src.host.routers.tasks", "_RETRY_THROTTLE", "dict", "clear"),
        ("src.host.task_store", "_device_fail_streak", "dict", "clear"),
        ("src.host.fb_store", "_PEER_NAME_REJECT_COUNTER", "dict", "reset_count"),
        # 2026-05-04 Stage E.3 audit 发现: fb_store 已有 _COUNTER 兜底, 但 by-event
        # dict + recent samples list 漏盖, 跨 test 累积. scripts/audit_module_globals.py
        # 22 SUSPICIOUS 中前 5 个 high-priority 优先入册.
        ("src.host.fb_store", "_PEER_NAME_REJECT_BY_EVENT", "dict", "clear"),
        ("src.host.fb_store", "_PEER_NAME_REJECT_RECENT", "list", "clear"),
        ("src.host.preflight", "_cache", "dict", "clear"),
        ("src.host.routers.cluster", "_WEBHOOK_COOLDOWN", "dict", "clear"),
        # 2026-05-04 Stage F.4 audit cache 类入册 (剥洋葱第 N 层 — 可能 cover
        # phase12_3 test_recycle_no_dead_at_meta_still_revives 的 setup error,
        # 该 test 调 _line_pool_recycle_dead_peers 用 _REFERRAL_*_CACHE):
        ("src.host.executor", "_REFERRAL_KEYWORDS_CACHE", "dict", "clear"),
        ("src.host.executor", "_PEER_REGION_CACHE", "dict", "clear"),
        # VLM cache: 测试 mock VLM 后, cache leak 让下个 test 误判 healthy
        ("src.host.fb_profile_classifier", "_VLM_HEALTH", "dict", "clear"),
        ("src.host.ollama_vlm", "_VLM_CONCURRENCY_STATS", "dict", "clear"),
        ("src.host.ollama_vlm", "_WARMUP_STATE", "dict", "clear"),
        # worker proxy cache: 跨 test fixture 可能 spam probe 不同 IP
        ("src.host.worker_device_proxy", "_worker_base_cache", "dict", "clear"),
        ("src.host.worker_device_proxy", "_negative_cache", "dict", "clear"),
        # central_customer_store 熔断 state + customer_sync_bridge AB cache
        ("src.host.customer_sync_bridge", "_ab_winner_cache", "dict", "clear"),
    ]
    for mod_name, attr, _typ, action in inline_clears:
        try:
            mod = importlib.import_module(mod_name)
            obj = getattr(mod, attr, None)
            if obj is None:
                continue
            if action == "clear" and hasattr(obj, "clear"):
                obj.clear()
            elif action == "reset_count" and isinstance(obj, dict) and "count" in obj:
                obj["count"] = 0
        except Exception:
            pass

    # task_forensics 的 timestamp scalar (非 dict, 单独处理)
    try:
        import src.host.task_forensics as tf_mod
        tf_mod._LAST_CLEANUP_TS = 0.0
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _p29_isolation():
    """P2-⑨ 每 test 前后清一次核心全局状态. 静默, 0 维护成本."""
    _p29_reset_global_state()
    yield
    _p29_reset_global_state()
