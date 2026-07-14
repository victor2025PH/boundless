# -*- coding: utf-8 -*-
"""Sprint 3 综合 e2e — 包含 Sprint 2 回归。"""
import io, sys, time
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from src.host.database import init_db
init_db()

results = []
def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  [{'OK' if ok else 'FAIL'}] {name} {detail}")

# ════════════════════════════════════════════════════
# Sprint 2 回归
# ════════════════════════════════════════════════════
print("\n════ Sprint 2 回归 ════")

print("\n[s2.1] fb_store CRUD + funnel:")
from src.host.fb_store import (record_friend_request, record_inbox_message,
                                upsert_group, mark_group_visit,
                                get_funnel_metrics, get_friend_request_stats,
                                list_groups, list_inbox_messages)
DEV_R = "__s3_e2e_regress__"
gid = upsert_group(DEV_R, "Test Group", member_count=50)
check("upsert_group 工作", gid > 0)
mark_group_visit(DEV_R, "Test Group", extracted_count=10)
fid = record_friend_request(DEV_R, "John Doe", source="Test Group", status="accepted")
check("record_friend_request", fid > 0)
mid = record_inbox_message(DEV_R, "John Doe", message_text="hi", direction="incoming")
check("record_inbox_message", mid > 0)
m = get_funnel_metrics(device_id=DEV_R)
check("funnel sum 字段都有", all(k in m for k in
      ["stage_extracted_members", "stage_friend_request_sent",
       "stage_inbox_incoming", "rate_accept", "scope_preset"]))

print("\n[s2.2] fb_lead_scorer v1:")
from src.ai.fb_lead_scorer import score_member, _tier_for_score
r = score_member("Marco Rossi", source_group="Italian Expats Berlin",
                target_country="IT", target_groups=["Italian Expats"])
check("v1 评分 Marco Rossi >= 50", r["score"] >= 50, f"got {r['score']}")
check("v1 含 tier", r["tier"] in {"S", "A", "B", "C", "D"})

print("\n[s2.3] fb_daily_brief 模块加载:")
from src.ai.fb_daily_brief import generate_brief, _gather_metrics
gm = _gather_metrics(device_id=None, hours=168)
check("_gather_metrics 工作", isinstance(gm, dict) and "scope_device" in gm)

print("\n[s2.4] platform-shell.js 文件存在:")
from pathlib import Path
PS = Path("src/host/static/js/platform-shell.js")
check("platform-shell.js 存在", PS.exists())
src_text = PS.read_text(encoding="utf-8")
check("PlatShell.modal", "modal" in src_text)
check("PlatShell.cmdBar", "cmdBar" in src_text)
check("PlatShell.preset", "preset" in src_text)
check("PlatShell.geo", "geo" in src_text)

# ════════════════════════════════════════════════════
# Sprint 3 P0
# ════════════════════════════════════════════════════
print("\n════ Sprint 3 P0 ════")

print("\n[s3.1] 跨平台风控总线:")
from src.host.fb_risk_listener import start_fb_risk_listener, get_healer
from src.host.risk_auto_heal import get_cross_platform_healer
from src.host.event_stream import push_event
from src.host.task_store import create_task, list_tasks, set_task_cancelled

start_fb_risk_listener()
core = get_cross_platform_healer()
check("registered facebook + tiktok",
      set(core._configs.keys()) == {"facebook", "tiktok"})

import uuid as _uuid
DEV_X = f"__s3_e2e_x_{_uuid.uuid4().hex[:8]}__"
# 制造 fb + tt pending(用唯一 device_id 隔离,避免历史累计干扰)
tids = ([create_task("facebook_browse_feed", DEV_X, {}) for _ in range(2)] +
        [create_task("tiktok_browse_home", DEV_X, {}) for _ in range(2)])
core.clear_cooldown("facebook", DEV_X)
push_event("facebook.risk_detected", {"message": "test"}, device_id=DEV_X)
time.sleep(0.5)
cancelled = list_tasks(device_id=DEV_X, status="cancelled", limit=20)
fb_c = [t for t in cancelled if t["type"].startswith("facebook_")
        and not (t.get("params") or {}).get("_origin")]
tt_c = [t for t in cancelled if t["type"].startswith("tiktok_")]
check("fb 风控 → fb 任务 cancelled", len(fb_c) == 2, f"got {len(fb_c)}")
check("fb 风控 → tt 任务株连 cancelled", len(tt_c) == 2, f"got {len(tt_c)}")
warmup = [t for t in list_tasks(device_id=DEV_X, status="pending", limit=10)
          if (t.get("params") or {}).get("_origin") == "facebook_risk_auto_downgrade"]
check("warmup 自动入队", len(warmup) == 1)

# 旧 fb_healer 兼容
fb_healer = get_healer()
check("fb_healer get_history 工作",
      len(fb_healer.get_history(DEV_X)) >= 1)
check("fb_healer get_cooldown_status > 0",
      fb_healer.get_cooldown_status(DEV_X) > 0)

print("\n[s3.2] preset_key 透传 + group_by 切片:")
from src.host.fb_store import get_funnel_metrics_by_preset
DEV_P = f"__s3_e2e_preset_{_uuid.uuid4().hex[:8]}__"
upsert_group(DEV_P, "warmup_grp", preset_key="warmup")
mark_group_visit(DEV_P, "warmup_grp", extracted_count=10)
upsert_group(DEV_P, "agg_grp", preset_key="aggressive")
mark_group_visit(DEV_P, "agg_grp", extracted_count=20)
record_friend_request(DEV_P, "warmup_a", status="accepted", preset_key="warmup")
record_friend_request(DEV_P, "warmup_b", status="accepted", preset_key="warmup")
for i in range(5):
    record_friend_request(DEV_P, f"agg_{i}", status="sent", preset_key="aggressive")

slices = get_funnel_metrics_by_preset(device_id=DEV_P)
check("group_by 切片 = 2 预设", len(slices) == 2)
warmup_slice = next(s for s in slices if s["preset_key"] == "warmup")
agg_slice = next(s for s in slices if s["preset_key"] == "aggressive")
check("warmup 通过率 1.0", warmup_slice["rate_accept"] == 1.0)
check("aggressive 通过率 0.0", agg_slice["rate_accept"] == 0.0)
m_warmup = get_funnel_metrics(device_id=DEV_P, preset_key="warmup")
check("preset_key 字段透传到 store",
      m_warmup["stage_friend_request_sent"] == 2,
      f"got {m_warmup['stage_friend_request_sent']}")

print("\n[s3.3] selector 种子加载:")
from src.vision.auto_selector import SelectorStore
store = SelectorStore()
fb_pkg = store.load("com.facebook.katana")
orca_pkg = store.load("com.facebook.orca")
check("katana 种子 >= 5 个", len(fb_pkg) >= 5, f"got {len(fb_pkg)}")
check("orca 种子 >= 5 个", len(orca_pkg) >= 5, f"got {len(orca_pkg)}")
check("'Add Friend button' 存在", "Add Friend button" in fb_pkg)
check("'Send message button' 存在", "Send message button" in orca_pkg)
# Sprint 3 P2 真机验证补的关键 selectors
check("'Friends tab' 存在 (Sprint 3 P2 加固)", "Friends tab" in fb_pkg)
check("'Groups tab' 存在 (Sprint 3 P2 加固)", "Groups tab" in fb_pkg)

# ════════════════════════════════════════════════════
# Sprint 3 P1
# ════════════════════════════════════════════════════
print("\n════ Sprint 3 P1 ════")

print("\n[s3.4] scorer v2:")
from src.ai.fb_lead_scorer_v2 import score_member_v2, get_cache_stats, clear_cache
clear_cache()
r2 = score_member_v2("Random Lurker", target_country="IT", use_llm=True)
check("v2 低分不调 LLM", not r2["llm_used"])
check("v2 final_score == v1_score (无 LLM 时)",
      r2["final_score"] == r2["v1_score"])
r2b = score_member_v2("Marco Rossi", source_group="Italian Expats",
                      target_groups=["Italian Expats"],
                      target_country="IT", use_llm=False)
check("v2 force-disable LLM 工作", not r2b["llm_used"])
check("v2 含 final_tier", r2b["final_tier"] in {"S", "A", "B", "C", "D"})
stats = get_cache_stats()
check("cache stats 含 weights", "weights" in stats)

print("\n[s3.5] /facebook/funnel?group_by=preset_key:")
from src.host.routers.facebook import fb_funnel
res = fb_funnel(device_id=DEV_P, since_hours=24, group_by="preset_key")
check("group_by 端点 = preset_key", res["_group_by"] == "preset_key")
check("slices >= 1", len(res["slices"]) >= 1)
res_normal = fb_funnel(device_id=DEV_P, since_hours=24, preset_key="warmup")
check("preset_key 过滤端点 _scope_preset 正确",
      res_normal["_scope_preset"] == "warmup")

print("\n[s3.6] PlatShell.leadList 公共组件:")
shell_text = (Path("src/host/static/js/platform-shell.js").read_text(encoding="utf-8"))
check("含 leadListRender 函数", "leadListRender" in shell_text)
check("含 _scoreColor / _tierBadge", "_scoreColor" in shell_text and "_tierBadge" in shell_text)
check("PlatShell.leadList 暴露", "leadList:" in shell_text)
check("版本升至 0.2.0", "0.2.0" in shell_text)
fb_ops = Path("src/host/static/js/facebook-ops.js").read_text(encoding="utf-8")
check("facebook-ops 含 fbOpenLeadsModal", "fbOpenLeadsModal" in fb_ops)
check("命令栏含 🎯 高分线索 按钮", "🎯 高分线索" in fb_ops)

print("\n[s3.7] 跨平台漏斗 API:")
from src.host.routers.unified_dashboard import (cross_platform_funnel,
                                                  UNIFIED_FUNNEL_STEPS)
data = cross_platform_funnel(since_hours=168, platforms="facebook,tiktok")
check("跨平台漏斗 6 步", len(data["steps"]) == 6)
check("跨平台漏斗 2 平台", len(data["platforms"]) == 2)
check("sums 6 元素", len(data["sums"]) == 6)
check("含 _version=sprint3.p1.7", data["_version"] == "sprint3.p1.7")

print("\n[s3 router] selector 健康度 API:")
from src.host.routers.facebook import fb_selectors_health
hr = fb_selectors_health()
check("selector health 返回包列表",
      len(hr["packages"]) == 2)
check("overall_total >= 10", hr["overall_total"] >= 10, f"got {hr['overall_total']}")
check("overall_healthy >= 10", hr["overall_healthy"] >= 10, f"got {hr['overall_healthy']}")
check("overall_health_pct = 100", hr["overall_health_pct"] == 100.0)

# 清理
for did in [DEV_R, DEV_X, DEV_P]:
    for t in list_tasks(device_id=did, status="pending", limit=20):
        set_task_cancelled(t["task_id"])

print("\n" + "=" * 60)
ok = sum(1 for _, b in results if b)
total = len(results)
print(f"Sprint 3 e2e 综合验证: {ok}/{total} 通过")
print("=" * 60)
sys.exit(0 if ok == total else 1)
