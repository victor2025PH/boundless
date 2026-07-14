# -*- coding: utf-8 -*-
"""P2-4 Sprint B 冷启动探针：验证 facebook_profile_hunt 全链路（不触真机）。

覆盖：
  1. persona 配置可加载（画像下拉数据源）
  2. classifier 对伪造候选的 L1 判定行为
  3. executor 路由分支：task_type=facebook_profile_hunt 在 mock Facebook 下能跑通
  4. 结果 card_type=fb_profile_hunt + 字段完整（供前端渲染）
  5. 候选来源兜底：candidates_from_task_id 从 mock task_store 拉取上游 members

用法::
    python scripts/smoke_test_p2b_profile_hunt.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _g(s): return f"\033[32m{s}\033[0m"
def _r(s): return f"\033[31m{s}\033[0m"
def _y(s): return f"\033[33m{s}\033[0m"
def _b(s): return f"\033[36m{s}\033[0m"


failures: List[str] = []


def check(name: str, ok: bool, detail: str = ""):
    label = _g("PASS") if ok else _r("FAIL")
    print(f"[{label}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(f"{name}: {detail}")


# ─────────────────────────────────────────────
# Step 1: persona 配置加载
# ─────────────────────────────────────────────
print(_b("\n─── Step 1: target persona 配置 ───"))
from src.host import fb_target_personas  # noqa: E402

personas = fb_target_personas.list_personas()
check("list_personas 非空", len(personas) > 0, f"count={len(personas)}")
jp = fb_target_personas.get_persona("jp_female_midlife")
check("get_persona jp_female_midlife 存在", bool(jp))
check("persona 有 l1.rules", bool((jp or {}).get("l1", {}).get("rules")),
      f"rule_count={len((jp or {}).get('l1', {}).get('rules', []))}")
check("persona vlm.model 定义", bool(fb_target_personas.get_vlm_config().get("model")))

# ─────────────────────────────────────────────
# Step 2: classifier L1 对一组伪候选的判定
# ─────────────────────────────────────────────
print(_b("\n─── Step 2: classifier L1 规则对候选的判定 ───"))
from src.host import fb_profile_classifier  # noqa: E402

cases = [
    ("山田花子",    True),   # 典型日本女名
    ("Miyuki Tanaka", True), # 罗马字日本女名（优化后应命中）
    ("John Smith",  False),  # 非日名
    ("李小明",       False),  # 中文名
    ("佐藤美恵",     True),   # 日本姓+女名
    ("Bob",         False),
]
l1_threshold = float(((jp or {}).get("l1") or {}).get("pass_threshold") or 30)
l1_scores: List[Tuple[str, float, bool]] = []
for name, expected_pass in cases:
    sc, reasons = fb_profile_classifier.score_l1(jp, {"display_name": name, "bio": "", "username": ""})
    passed = sc >= l1_threshold
    l1_scores.append((name, sc, passed))
    check(f"L1 '{name}' 期望 {'通过' if expected_pass else '拒绝'}", passed == expected_pass,
          f"score={sc:.0f} threshold={l1_threshold:.0f} reasons={reasons}")

# ─────────────────────────────────────────────
# Step 3: mock Facebook + executor 路由 smoke
# ─────────────────────────────────────────────
print(_b("\n─── Step 3: executor 路由 facebook_profile_hunt（mock fb）───"))
class MockFB:
    """最小 Facebook mock：只实现 profile_hunt 需要的方法，返回合成结果。"""

    def __init__(self):
        self.calls = []

    def profile_hunt(self, candidates, persona_key=None, action_on_match="none",
                     note="", max_targets=None, inter_target_sec=(20.0, 34.0),
                     shot_count=3, task_id="", device_id=None):
        self.calls.append({"candidates": list(candidates),
                           "persona_key": persona_key,
                           "action_on_match": action_on_match,
                           "max_targets": max_targets})
        return {
            "card_type": "fb_profile_hunt",
            "persona_key": persona_key or "jp_female_midlife",
            "persona_name": "日本 37-60 岁女性",
            "action_on_match": action_on_match,
            "candidates_total": len(candidates),
            "processed": len(candidates),
            "l1_pass": 3,
            "l2_run": 2,
            "matched": 1,
            "actioned": 1 if action_on_match != "none" else 0,
            "skipped": {"l1_fail": 1, "l2_cap": 0, "risk_pause": 0,
                        "cached": 0, "search_fail": 0, "classify_err": 0},
            "risk_interrupted": None,
            "results": [
                {"name": "山田花子", "match": True, "score": 82.0, "stage": "L2",
                 "reason": "", "action_ok": action_on_match != "none", "from_cache": False},
                {"name": "Miyuki Tanaka", "match": False, "score": 66.0, "stage": "L2",
                 "reason": "age_below_min", "action_ok": False, "from_cache": False},
                {"name": "John Smith", "match": False, "score": 0.0, "stage": "L1",
                 "reason": "l1_below_threshold", "action_ok": False, "from_cache": False},
            ],
        }


# 打桩：把 _execute_facebook 里的 _fresh_facebook 替换为返回 MockFB 的函数
from src.host import executor  # noqa: E402

mock_fb = MockFB()
original_fresh = executor._fresh_facebook
executor._fresh_facebook = lambda manager, resolved: mock_fb

# 验证分发：直接调用 _execute_facebook（绕过顶层重试包装）
try:
    ok, msg, stats = executor._execute_facebook(
        manager=None,
        resolved="test-device-001",
        task_type="facebook_profile_hunt",
        params={
            "candidates": "山田花子\nMiyuki Tanaka\nJohn Smith",
            "persona_key": "jp_female_midlife",
            "action_on_match": "follow",
            "max_targets": 30,
            "shot_count": 3,
        },
    )
finally:
    executor._fresh_facebook = original_fresh

check("executor 返回 ok=True", ok is True, f"msg={msg}")
check("stats.card_type == fb_profile_hunt", (stats or {}).get("card_type") == "fb_profile_hunt")
check("stats 字段齐全",
      all(k in (stats or {}) for k in
          ("candidates_total", "processed", "l1_pass", "l2_run", "matched", "actioned", "skipped", "results")))
check("mock 被调用一次", len(mock_fb.calls) == 1, f"calls={len(mock_fb.calls)}")
if mock_fb.calls:
    c0 = mock_fb.calls[0]
    check("candidates 按行拆分正确", c0["candidates"] == ["山田花子", "Miyuki Tanaka", "John Smith"],
          f"got={c0['candidates']}")
    check("persona_key 透传", c0["persona_key"] == "jp_female_midlife")
    check("action_on_match 透传", c0["action_on_match"] == "follow")

# ─────────────────────────────────────────────
# Step 4: candidates_from_task_id 兜底
# ─────────────────────────────────────────────
print(_b("\n─── Step 4: 候选来源兜底（candidates_from_task_id）───"))


class _FakeTaskStore:
    def __init__(self, payload):
        self.payload = payload

    def __call__(self, tid):
        if tid == "upstream-task-A":
            return {
                "task_id": tid,
                "result": json.dumps({
                    "members": [
                        {"name": "佐藤美恵", "role": "member"},
                        {"name": "田中裕子", "role": "admin"},
                        "山口和子",
                    ],
                    "count": 3,
                })
            }
        return None


import src.host.task_store as _ts_mod  # noqa: E402
orig_get = getattr(_ts_mod, "get_task", None)
_ts_mod.get_task = _FakeTaskStore(None)

mock_fb2 = MockFB()
executor._fresh_facebook = lambda manager, resolved: mock_fb2
try:
    ok2, msg2, stats2 = executor._execute_facebook(
        manager=None,
        resolved="test-device-001",
        task_type="facebook_profile_hunt",
        params={
            "candidates_from_task_id": "upstream-task-A",
            "persona_key": "jp_female_midlife",
            "action_on_match": "none",
        },
    )
finally:
    executor._fresh_facebook = original_fresh
    if orig_get is not None:
        _ts_mod.get_task = orig_get

check("上游抽取成功 (ok=True)", ok2 is True, f"msg={msg2}")
check("从上游抽出 3 个候选（含 str 型）",
      len(mock_fb2.calls) == 1 and len(mock_fb2.calls[0]["candidates"]) == 3,
      f"got={mock_fb2.calls[0]['candidates'] if mock_fb2.calls else 'N/A'}")

# ─────────────────────────────────────────────
# Step 5: Facebook.profile_hunt 单体优化验证（L1 prefilter 短路）
# ─────────────────────────────────────────────
print(_b("\n─── Step 5: profile_hunt 内部 L1 预筛短路（不真 search）───"))


class _SpyFB:
    """真实装载 profile_hunt 方法的最小包装，search_people 被打桩为计数。"""
    pass


# 直接拿 Facebook 类的 profile_hunt 函数作为 unbound（monkey 附着到最小 spy）
from src.app_automation import facebook as fb_mod  # noqa: E402


# 绕开 @_with_fb_foreground 装饰器的执行（它预期 self.hb/ self._u2 等）
# 做法：解包出原函数。_with_fb_foreground 保留原函数在 __wrapped__ 上。
raw_profile_hunt = getattr(fb_mod.FacebookAutomation.profile_hunt, "__wrapped__",
                           fb_mod.FacebookAutomation.profile_hunt)
raw_do_action = fb_mod.FacebookAutomation._do_action_on_profile
raw_goback = fb_mod.FacebookAutomation._go_back_to_feed

calls = {"search": 0, "classify": 0}


class _HB:
    def tap(self, *a, **kw): pass


class _D:
    def press(self, *a, **kw): pass
    def swipe_ext(self, *a, **kw): pass


class SpyFB:
    hb = _HB()

    def _did(self, x): return x or "dev-spy"
    def _u2(self, did): return _D()
    def _detect_risk_dialog(self, d): return False, ""
    def search_people(self, name, did, max_results=3):
        calls["search"] += 1
        return [{"name": name}]
    def _first_search_result_element(self, d): return object()
    @staticmethod
    def _el_center(el): return (500, 500)
    # Sprint E-1.1: profile_hunt 现在走 navigate_to_profile。
    # Spy 把 display_name 路径转发到 search_people 以保留"search 次数"计数语义。
    def navigate_to_profile(self, candidate, device_id=None,
                            post_open_dwell_sec=(0.0, 0.0)):
        # 这里 smoke 用的都是 display_name（含空格/日文/中文），均应转发 search
        self.search_people(candidate, device_id, max_results=3)
        return {"ok": True, "kind": "display_name", "via": "search",
                "target_key": f"search:{candidate}", "url": "", "reason": ""}
    def classify_current_profile(self, target_key, persona_key=None, task_id="",
                                 shot_count=3, device_id=None):
        calls["classify"] += 1
        return {
            "match": False, "score": 0, "stage_reached": "L1",
            "l1": {"pass": False}, "quota": {"exceeded": ""}, "from_cache": False,
        }
    _do_action_on_profile = raw_do_action
    _go_back_to_feed = raw_goback


spy = SpyFB()
stats5 = raw_profile_hunt(
    spy,
    candidates=["Bob", "Alice", "Peter", "John Smith"],
    persona_key="jp_female_midlife",
    action_on_match="none",
    max_targets=10,
    inter_target_sec=(0.01, 0.02),  # 跑得快一点
    device_id="dev-spy",
)

check("4 个纯英文名全部被预筛（0 search / 0 classify）",
      calls["search"] == 0 and calls["classify"] == 0,
      f"search={calls['search']} classify={calls['classify']}")
check("prefilter 计数 = 4", stats5["skipped"]["prefilter"] == 4,
      f"prefilter={stats5['skipped']['prefilter']}")
check("optimizations_applied 标记存在",
      "name_prefilter" in (stats5.get("optimizations_applied") or []))

# 再混入日文名，预筛应该放行
calls2 = {"search": 0, "classify": 0}
class SpyFB2(SpyFB):
    def search_people(self, name, did, max_results=3):
        calls2["search"] += 1
        return [{"name": name}]
    def navigate_to_profile(self, candidate, device_id=None,
                            post_open_dwell_sec=(0.0, 0.0)):
        self.search_people(candidate, device_id, max_results=3)
        return {"ok": True, "kind": "display_name", "via": "search",
                "target_key": f"search:{candidate}", "url": "", "reason": ""}
    def classify_current_profile(self, target_key, persona_key=None, task_id="",
                                 shot_count=3, device_id=None):
        calls2["classify"] += 1
        return {"match": False, "score": 35, "stage_reached": "L2",
                "l1": {"pass": True}, "quota": {"exceeded": ""}, "from_cache": False}
raw_profile_hunt(
    SpyFB2(),
    candidates=["Bob", "山田花子", "Miyuki Tanaka"],
    persona_key="jp_female_midlife",
    action_on_match="none",
    max_targets=10,
    inter_target_sec=(0.01, 0.02),
    device_id="dev-spy",
)
check("日文名被放行（search 应被调用 2 次）", calls2["search"] == 2,
      f"search={calls2['search']}")
check("纯英文仍被预筛（classify 应 = 2, 不含 Bob）", calls2["classify"] == 2,
      f"classify={calls2['classify']}")

# ─────────────────────────────────────────────
# Step 6: VLM 全局锁串行化 + queue_wait_ms 指标（Sprint C-1）
# ─────────────────────────────────────────────
print(_b("\n─── Step 6: ollama_vlm 全局锁串行化（多线程并发） ───"))

import threading  # noqa: E402
from src.host import ollama_vlm as _vlm  # noqa: E402

# 打桩 urlopen，让它睡 0.3s 模拟 VLM 推理
original_urlopen = _vlm.urllib.request.urlopen


class _FakeResp:
    def __init__(self):
        self._data = json.dumps({"response": "{}", "prompt_eval_count": 1, "eval_count": 1}).encode()
    def read(self):
        return self._data
    def __enter__(self): return self
    def __exit__(self, *a): pass


def _fake_urlopen(*a, **kw):
    time.sleep(0.3)
    return _FakeResp()


_vlm.urllib.request.urlopen = _fake_urlopen
# 同时打桩 _log_ai_cost，不写 DB
original_log = _vlm._log_ai_cost
_vlm._log_ai_cost = lambda **kw: None

# 重置并发统计
_vlm._VLM_CONCURRENCY_STATS["peak_wait_ms"] = 0
_vlm._VLM_CONCURRENCY_STATS["total_calls"] = 0
_vlm._VLM_CONCURRENCY_STATS["total_wait_ms"] = 0

results_concurrent: List[Dict[str, Any]] = []
lock = threading.Lock()


def _worker(i):
    _, meta = _vlm.generate(prompt="test", image_paths=None,
                            scene="smoke", task_id=f"smoke-{i}",
                            device_id=f"dev-{i}")
    with lock:
        results_concurrent.append(meta)


threads = [threading.Thread(target=_worker, args=(i,)) for i in range(4)]
t_start = time.time()
for th in threads:
    th.start()
for th in threads:
    th.join()
total_elapsed = time.time() - t_start

# 还原打桩
_vlm.urllib.request.urlopen = original_urlopen
_vlm._log_ai_cost = original_log

check("4 个并发 VLM 调用全部完成", len(results_concurrent) == 4,
      f"got={len(results_concurrent)}")
# 每次 VLM 0.3s；串行 4 次 ≈ 1.2s。并行（无锁）应 <0.5s。
check("并发调用被串行化（总耗时 >= ~1.2s）",
      total_elapsed >= 1.1,
      f"elapsed={total_elapsed:.2f}s（串行 ~1.2s，无锁并行 ~0.3s）")

queue_waits = sorted([int(m.get("queue_wait_ms", 0)) for m in results_concurrent])
check("queue_wait_ms 单调递增（0ms, ~300ms, ~600ms, ~900ms）",
      queue_waits[0] <= 50 and queue_waits[-1] >= 600,
      f"queue_waits={queue_waits}")

stats_vlm = _vlm.get_concurrency_stats()
check("get_concurrency_stats total_calls==4", stats_vlm["total_calls"] == 4,
      f"stats={stats_vlm}")
check("peak_wait_ms 记录最长等待（>= 600ms）",
      stats_vlm["peak_wait_ms"] >= 600,
      f"peak={stats_vlm['peak_wait_ms']}ms")

# ─────────────────────────────────────────────
# Step 7: 小时配额短路（Sprint C-1）
# ─────────────────────────────────────────────
print(_b("\n─── Step 7: profile_hunt 小时配额短路 ───"))

# 动态改 quotas.l2_per_device_per_hour=2
from src.host import fb_target_personas as _persona  # noqa: E402
_orig_get_quotas = _persona.get_quotas

def _fake_quotas():
    q = dict(_orig_get_quotas() or {})
    q["l2_per_device_per_hour"] = 2
    q["l2_per_device_per_day"] = 100
    return q

_persona.get_quotas = _fake_quotas

# 打桩：_db_count_recent_hours 返回 3（超过小时 cap=2）
from src.host import fb_profile_classifier as _clf  # noqa: E402
_orig_count_today = _clf._db_count_today
_orig_count_hours = _clf._db_count_recent_hours
_clf._db_count_today = lambda did, stage: 10     # 日配额没满
_clf._db_count_recent_hours = lambda did, stage, hours: 3 if stage == "L2" else 0  # 小时满

calls7 = {"search": 0, "classify": 0}


class SpyFB3(SpyFB):
    def search_people(self, name, did, max_results=3):
        calls7["search"] += 1
        return [{"name": name}]

    def classify_current_profile(self, target_key, persona_key=None, task_id="",
                                 shot_count=3, device_id=None):
        calls7["classify"] += 1
        return {"match": False, "score": 50, "stage_reached": "L2",
                "l1": {"pass": True}, "quota": {"exceeded": ""}, "from_cache": False}


stats7 = raw_profile_hunt(
    SpyFB3(),
    candidates=["山田花子", "佐藤美恵", "田中裕子"],
    persona_key="jp_female_midlife",
    action_on_match="none",
    max_targets=10,
    inter_target_sec=(0.01, 0.02),
    device_id="dev-hour-cap",
)

# 还原
_persona.get_quotas = _orig_get_quotas
_clf._db_count_today = _orig_count_today
_clf._db_count_recent_hours = _orig_count_hours

check("小时配额满时 search 不被调用（全部短路）",
      calls7["search"] == 0 and calls7["classify"] == 0,
      f"search={calls7['search']} classify={calls7['classify']}")
check("skipped.l2_hourly_cap 计数 == 3",
      stats7["skipped"].get("l2_hourly_cap", 0) == 3,
      f"skipped={stats7['skipped']}")
check("stats.optimizations_applied 含 l2_hourly_cap_short_circuit",
      "l2_hourly_cap_short_circuit" in (stats7.get("optimizations_applied") or []))

# ─────────────────────────────────────────────
# Step 8: 风控软降档（Sprint D-1）
# ─────────────────────────────────────────────
print(_b("\n─── Step 8: profile_hunt 软降档（近 N 小时有风控）───"))

# Mock fb_store.count_risk_events_recent → 返回 2，且 risk_guard 启用软降档
import src.host.fb_store as _fbs_mod  # noqa: E402

_orig_count_risk = getattr(_fbs_mod, "count_risk_events_recent", None)

def _fake_count_risk(device_id: str, hours: int) -> int:
    # 1h 内无（避免触发 classifier 内部 hard pause），但 24h 内有 2 次
    return 2 if hours >= 12 else 0

_fbs_mod.count_risk_events_recent = _fake_count_risk

from src.host import fb_target_personas as _persona2  # noqa: E402
_orig_risk_guard = _persona2.get_risk_guard

def _fake_risk_guard():
    return {
        "pause_l2_after_risk_hours": 0,   # 关掉硬暂停，只测软降档
        "soft_throttle_window_hours": 24,
        "soft_throttle_cap_ratio": 0.5,
        "soft_throttle_interval_factor": 2.0,
    }

_persona2.get_risk_guard = _fake_risk_guard

class SpyFB4(SpyFB):
    def search_people(self, name, did, max_results=3):
        return [{"name": name}]
    def navigate_to_profile(self, candidate, device_id=None,
                            post_open_dwell_sec=(0.0, 0.0)):
        return {"ok": True, "kind": "display_name", "via": "search",
                "target_key": f"search:{candidate}", "url": "", "reason": ""}
    def classify_current_profile(self, target_key, persona_key=None, task_id="",
                                 shot_count=3, device_id=None):
        return {"match": False, "score": 40, "stage_reached": "L2",
                "l1": {"pass": True}, "quota": {"exceeded": ""}, "from_cache": False}

stats8 = raw_profile_hunt(
    SpyFB4(),
    candidates=["山田花子"],
    persona_key="jp_female_midlife",
    action_on_match="none",
    max_targets=1,
    inter_target_sec=(20.0, 30.0),  # 原始间隔
    device_id="dev-risk",
)

# 还原
if _orig_count_risk is not None:
    _fbs_mod.count_risk_events_recent = _orig_count_risk
_persona2.get_risk_guard = _orig_risk_guard

check("soft_throttled == True", stats8.get("soft_throttled") is True,
      f"stats.soft_throttled={stats8.get('soft_throttled')}")
check("effective_l2_daily_cap 被降半（100→50）",
      stats8.get("effective_l2_daily_cap") == 50,
      f"cap={stats8.get('effective_l2_daily_cap')}")
check("effective_l2_hourly_cap 被降半（30→15）",
      stats8.get("effective_l2_hourly_cap") == 15,
      f"cap={stats8.get('effective_l2_hourly_cap')}")
check("effective_interval_sec 翻倍（20~30→40~60）",
      stats8.get("effective_interval_sec") == [40.0, 60.0],
      f"ivl={stats8.get('effective_interval_sec')}")
check("optimizations_applied 含 soft_throttle_by_risk",
      "soft_throttle_by_risk" in (stats8.get("optimizations_applied") or []))

# ─────────────────────────────────────────────
# Step 9: content_exposure 写入（Sprint D-2）
# ─────────────────────────────────────────────
print(_b("\n─── Step 9: L2 命中时 content_exposure 入库 ───"))

from src.host import fb_profile_classifier as _clf2  # noqa: E402
from src.host.database import get_conn, init_db  # noqa: E402

init_db()

# 构造合成 insights（VLM 返回）
fake_insights = {
    "age_band": "40s",
    "gender": "female",
    "is_japanese": True,
    "japanese_confidence": 0.8,
    "overall_confidence": 0.82,
    "interests": ["ヨガ", "Yoga", "園芸", "cooking", "ヨガ"],  # 故意包含 dup + 大小写变体
    "language": "ja",
}
written = _clf2._db_insert_content_exposure(
    device_id="dev-exp",
    task_id="smoke-exp",
    persona_key="jp_female_midlife",
    target_key="search:smoke_taro",
    display_name="Smoke Taro",
    insights=fake_insights,
)
check("写入条数 >= 3（去重后 yoga/園芸/cooking）", written >= 3,
      f"written={written}")

with get_conn() as conn:
    rows = conn.execute(
        "SELECT topic, lang, meta_json FROM fb_content_exposure "
        "WHERE task_id='smoke-exp' ORDER BY id DESC LIMIT 10"
    ).fetchall()

topics = {r[0] for r in rows}
check("topic 'yoga' 存在（大小写合并）", "yoga" in topics, f"topics={topics}")
check("lang == 'ja'", all(r[1] == "ja" for r in rows), f"langs={[r[1] for r in rows]}")
# meta_json 校验一行
if rows:
    meta = json.loads(rows[0][2] or "{}")
    check("meta 含 target_key/persona_key",
          meta.get("target_key") == "search:smoke_taro" and meta.get("persona_key") == "jp_female_midlife",
          f"meta={meta}")

# 清理
with get_conn() as conn:
    conn.execute("DELETE FROM fb_content_exposure WHERE task_id='smoke-exp'")

# ─────────────────────────────────────────────
# Step 10: L1 rule 分析端点（Sprint D-1）
# ─────────────────────────────────────────────
print(_b("\n─── Step 10: /facebook/l1-rule-analytics 响应字段 ───"))

# 直接调函数（避免起 FastAPI）
try:
    from src.host.routers.facebook import fb_l1_rule_analytics, _derive_l1_recommendations
    resp = fb_l1_rule_analytics(hours=168, persona_key=None)
    check("响应含 ok/rules/recommendations",
          resp.get("ok") and "rules" in resp and "recommendations" in resp,
          f"keys={list(resp.keys())}")

    # _derive_l1_recommendations 纯函数测试
    synthetic = [
        {"reason": "rule_high_precision", "hits": 20, "l2_match": 16, "precision": 0.8},
        {"reason": "rule_low_precision", "hits": 20, "l2_match": 2, "precision": 0.1},
        {"reason": "rule_few_samples", "hits": 3, "l2_match": 3, "precision": 1.0},
        {"reason": "rule_middle", "hits": 20, "l2_match": 10, "precision": 0.5},
    ]
    recs = _derive_l1_recommendations(synthetic)
    rec_by = {r["reason"]: r for r in recs}
    check("高精度 → boost_weight",
          rec_by.get("rule_high_precision", {}).get("action") == "boost_weight")
    check("低精度 → demote_or_remove",
          rec_by.get("rule_low_precision", {}).get("action") == "demote_or_remove")
    check("小样本 (hits<5) 不给建议",
          "rule_few_samples" not in rec_by)
    check("中段 (0.15<p<0.75) 不给建议",
          "rule_middle" not in rec_by)
except Exception as e:
    check("加载 l1_rule_analytics 路由", False, str(e))


# ─────────────────────────────────────────────
# Step 11: VLM warmup 幂等性 + 状态机（Sprint E-0.1）
# ─────────────────────────────────────────────
print(_b("\n─── Step 11: VLM warmup 幂等性 + 状态机 ───"))
try:
    from src.host import ollama_vlm as _vlm_mod

    # 重置状态，避免前面测试留下的影响
    with _vlm_mod._WARMUP_LOCK:
        _vlm_mod._WARMUP_STATE["last_ok_ts"] = 0.0
        _vlm_mod._WARMUP_STATE["last_error"] = ""
        _vlm_mod._WARMUP_STATE["in_progress"] = False

    st0 = _vlm_mod.get_warmup_state()
    check("warmup 初始未 fresh", st0.get("fresh") is False,
          f"state={st0}")

    # mock generate + check_health，避免真的打 Ollama
    _calls = {"n": 0}

    def _fake_health(timeout=3.0):
        return {"online": True, "model": "qwen2.5vl:7b", "model_available": True,
                "models": ["qwen2.5vl:7b"], "endpoint": "http://mock"}

    def _fake_generate(prompt, image_paths=None, **kw):
        _calls["n"] += 1
        meta = {"ok": True, "model": "qwen2.5vl:7b", "image_count": 0,
                "latency_ms": 10, "queue_wait_ms": 0, "total_ms": 10,
                "attempts": 1, "error": ""}
        return "OK", meta

    _real_h, _real_g = _vlm_mod.check_health, _vlm_mod.generate
    _vlm_mod.check_health = _fake_health
    _vlm_mod.generate = _fake_generate
    try:
        r1 = _vlm_mod.warmup(force=True)
        check("warmup 第一次 ok",
              r1.get("ok") and not r1.get("skipped") and _calls["n"] == 1,
              f"r1={r1} calls={_calls['n']}")
        r2 = _vlm_mod.warmup(force=False)
        check("warmup 第二次 (10min TTL 内) skipped",
              r2.get("ok") and r2.get("skipped") and _calls["n"] == 1,
              f"r2={r2} calls={_calls['n']}")
        r3 = _vlm_mod.warmup(force=True)
        check("warmup force=True 重新跑",
              r3.get("ok") and not r3.get("skipped") and _calls["n"] == 2,
              f"r3={r3} calls={_calls['n']}")
        st2 = _vlm_mod.get_warmup_state()
        check("get_warmup_state fresh=True 且含 age_sec",
              st2.get("fresh") and st2.get("age_sec") is not None and
              st2.get("last_error") == "",
              f"state={st2}")

        # Ollama 离线时的优雅失败
        def _offline_health(timeout=3.0):
            return {"online": False, "error": "connection refused"}
        _vlm_mod.check_health = _offline_health
        with _vlm_mod._WARMUP_LOCK:
            _vlm_mod._WARMUP_STATE["last_ok_ts"] = 0.0
        r4 = _vlm_mod.warmup(force=True)
        check("Ollama 离线时 warmup 返回 ok=False 且不卡",
              r4.get("ok") is False and r4.get("error") == "ollama_offline",
              f"r4={r4}")
    finally:
        _vlm_mod.check_health = _real_h
        _vlm_mod.generate = _real_g
        # 恢复干净态
        with _vlm_mod._WARMUP_LOCK:
            _vlm_mod._WARMUP_STATE["last_ok_ts"] = 0.0
            _vlm_mod._WARMUP_STATE["last_error"] = ""
            _vlm_mod._WARMUP_STATE["in_progress"] = False
except Exception as e:
    check("Step 11 warmup 测试加载", False, str(e))


# ─────────────────────────────────────────────
# Step 12: navigate_to_profile / _classify_candidate 分类（Sprint E-1.1）
# ─────────────────────────────────────────────
print(_b("\n─── Step 12: candidate kind 分类 + deeplink URL 拼接 ───"))
try:
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)  # 不走 __init__，只测纯函数
    kind, norm = fb._classify_candidate("https://facebook.com/zuck")
    check("https URL → kind=url",
          kind == "url" and norm == "https://facebook.com/zuck",
          f"kind={kind} norm={norm}")
    kind, _ = fb._classify_candidate("facebook.com/zuck")
    check("裸域 facebook.com/... → kind=url", kind == "url")
    kind, norm = fb._classify_candidate("zuck.fb123")
    check("英文+数字+点 → kind=username",
          kind == "username" and norm == "zuck.fb123", f"kind={kind}")
    kind, norm = fb._classify_candidate("100012345678")
    check("纯数字 12 位 → kind=user_id",
          kind == "user_id" and norm == "100012345678", f"kind={kind}")
    kind, _ = fb._classify_candidate("Tanaka Yumi")
    check("含空格英文 → kind=display_name (fallback search)",
          kind == "display_name")
    kind, _ = fb._classify_candidate("山田 花子")
    check("日文名 → kind=display_name", kind == "display_name")
    kind, _ = fb._classify_candidate("张三")
    check("中文名 → kind=display_name", kind == "display_name")
    kind, _ = fb._classify_candidate("")
    check("空串 → kind=display_name", kind == "display_name")
    kind, _ = fb._classify_candidate("12345")  # 太短
    check("5 位数字 → kind=display_name (不当 user_id)",
          kind == "display_name")
except Exception as e:
    check("Step 12 navigate_to_profile 分类", False, str(e))


# ─────────────────────────────────────────────
# Step 13: VLM OCR 兜底（Sprint E-0.3）
# ─────────────────────────────────────────────
print(_b("\n─── Step 13: _vlm_ocr_profile_texts 成功/失败路径 ───"))
try:
    from src.app_automation import facebook as _fb_mod
    from src.app_automation.facebook import FacebookAutomation

    fb2 = FacebookAutomation.__new__(FacebookAutomation)

    # ---- 成功路径 ----
    def _fake_classify_ok(prompt, image_paths=None, **kw):
        return ({"display_name": "山田 花子",
                 "bio": "東京 ヨガ 45歳 主婦"}, {"ok": True, "total_ms": 3200})
    import src.host.ollama_vlm as _vl
    _real_classify = _vl.classify_images
    _vl.classify_images = _fake_classify_ok
    try:
        name, bio = fb2._vlm_ocr_profile_texts(["/tmp/fake.png"])
        check("OCR 成功解析 display_name", name == "山田 花子", f"name={name!r}")
        check("OCR 成功解析 bio", "ヨガ" in bio, f"bio={bio!r}")
    finally:
        _vl.classify_images = _real_classify

    # ---- 空 paths 路径 ----
    name, bio = fb2._vlm_ocr_profile_texts([])
    check("空 image_paths → 返回空", name == "" and bio == "")

    # ---- VLM 返回非 dict ----
    def _fake_bad(prompt, image_paths=None, **kw):
        return ("not json", {"ok": False, "error": "bad_json"})
    _vl.classify_images = _fake_bad
    try:
        name, bio = fb2._vlm_ocr_profile_texts(["/tmp/fake.png"])
        check("VLM ok=False → 返回空不抛",
              name == "" and bio == "", f"name={name!r} bio={bio!r}")
    finally:
        _vl.classify_images = _real_classify

    # ---- VLM 抛异常 ----
    def _fake_exc(*a, **kw):
        raise RuntimeError("boom")
    _vl.classify_images = _fake_exc
    try:
        name, bio = fb2._vlm_ocr_profile_texts(["/tmp/fake.png"])
        check("VLM 抛异常 → 返回空不抛", name == "" and bio == "")
    finally:
        _vl.classify_images = _real_classify

    # ---- display_name 长度过滤 ----
    def _fake_long(prompt, image_paths=None, **kw):
        return ({"display_name": "x" * 200, "bio": "y" * 1000},
                {"ok": True, "total_ms": 100})
    _vl.classify_images = _fake_long
    try:
        name, bio = fb2._vlm_ocr_profile_texts(["/tmp/fake.png"])
        check("display_name 截断到 ≤60", len(name) <= 60, f"len={len(name)}")
        check("bio 截断到 ≤300", len(bio) <= 300, f"len={len(bio)}")
    finally:
        _vl.classify_images = _real_classify

    # ---- 垃圾输入过滤 ----
    def _fake_trash(prompt, image_paths=None, **kw):
        return ({"display_name": "123456789", "bio": ""},
                {"ok": True, "total_ms": 100})
    _vl.classify_images = _fake_trash
    try:
        name, bio = fb2._vlm_ocr_profile_texts(["/tmp/fake.png"])
        check("display_name 纯数字被过滤", name == "", f"name={name!r}")
    finally:
        _vl.classify_images = _real_classify
except Exception as e:
    check("Step 13 vlm_ocr 测试加载", False, str(e))


# ─────────────────────────────────────────────
# Step 14: Sprint F — facebook_browse_feed_by_interest 注册 + DB 查询 SQL
# ─────────────────────────────────────────────
print(_b("\n─── Step 14: Sprint F interest feed 任务注册 ───"))
try:
    from src.host.executor import _TASK_TYPE_TIMEOUTS as _tt
    check(
        "_TASK_TYPE_TIMEOUTS 含 facebook_browse_feed_by_interest",
        "facebook_browse_feed_by_interest" in _tt,
        f"timeout={_tt.get('facebook_browse_feed_by_interest')}",
    )
    from src.host.schemas import TaskType
    check(
        "TaskType 枚举含 FACEBOOK_BROWSE_FEED_BY_INTEREST",
        TaskType.FACEBOOK_BROWSE_FEED_BY_INTEREST.value == "facebook_browse_feed_by_interest",
    )
    from src.app_automation.facebook import FacebookAutomation as _FBI
    fb_i = _FBI.__new__(_FBI)
    rows = fb_i._fetch_device_interest_topics(
        "nonexistent-device-xyz", None, 24, 5)
    check("_fetch_device_interest_topics 空设备返回列表（不抛）",
          isinstance(rows, list), f"type={type(rows)} len={len(rows)}")
except Exception as e:
    check("Step 14 加载", False, str(e))


# ─────────────────────────────────────────────
# 总结
# ─────────────────────────────────────────────
print(_b("\n─── 总结 ───"))
if failures:
    print(_r(f"{len(failures)} 个检查失败："))
    for f in failures:
        print(_r(f"  - {f}"))
    sys.exit(1)
print(_g("ALL GREEN — Sprint B smoke 通过（P2-4 profile_hunt 全链路 OK）"))
