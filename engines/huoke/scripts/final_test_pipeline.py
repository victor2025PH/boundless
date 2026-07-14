# -*- coding: utf-8 -*-
"""
最终全链路测试脚本（dry_run 模式）
测试内容：
  1. L1 规则（29个历史 profile → 期望 >= 25 通过）
  2. W1 获客任务导入+语法 OK
  3. W2 打招呼任务（注入测试数据 → 生成话术 → 无禁词 → 审计记录）
  4. W3 Playbook 整体 dry_run（获客+打招呼串联，跳过实际 UI 操作）
  5. DB 表完整性检查
"""
import sys, io, json, os, logging
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, '.')

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

DEVICE = "8DWOF6CYY5R8YHX8"
PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    status = "PASS" if cond else "FAIL"
    if not cond:
        FAIL += 1
        print(f"  [{status}] {name}  {detail}")
    else:
        PASS += 1
        print(f"  [{status}] {name}")

print("=" * 60)
print("全链路测试 (dry_run)")
print("=" * 60)

# ── 1. L1 规则 ──────────────────────────────────────────────────────
print("\n[T1] L1 规则改进测试")
try:
    from src.host.fb_profile_classifier import score_l1
    from src.host.fb_target_personas import get_persona
    persona = get_persona("jp_female_midlife")
    threshold = (persona.get("l1") or {}).get("pass_threshold", 30)
    with open("data/w0_jp_ground_truth_v2.json", encoding="utf-8") as f:
        profiles = json.load(f)["profiles"]
    pass_count = 0
    for p in profiles:
        name = p.get("display_name", "")
        if "," in name:
            parts = name.split(",")[0].strip().split()
            if len(parts) >= 2 and all(w[0].isupper() for w in parts if w):
                name = name.split(",")[0].strip()
        bio = p.get("bio", "")
        ctx = {"display_name": name, "bio": bio, "username": "", "locale": "ja"}
        score, _ = score_l1(persona, ctx)
        if score >= threshold:
            pass_count += 1
    check(f"L1通过率 >= 90% ({pass_count}/29)", pass_count >= 26, f"实际={pass_count}/29")
    check(f"L1阈值 = 20", threshold == 20, f"实际={threshold}")
except Exception as e:
    check("L1 规则测试", False, str(e))

# ── 2. W1 模块导入 ──────────────────────────────────────────────────
print("\n[T2] W1 获客任务模块")
try:
    from src.app_automation.fb_acquire_task import facebook_acquire_from_keyword, AcquireTask
    check("fb_acquire_task 导入成功", True)
    t = AcquireTask(device_id=DEVICE)
    check("AcquireTask 初始化", True)
except Exception as e:
    check("fb_acquire_task 导入", False, str(e))

# ── 3. W2 打招呼任务 ────────────────────────────────────────────────
print("\n[T3] W2 打招呼任务测试")
try:
    from src.app_automation.fb_greet_task import facebook_jp_female_greet, generate_greeting
    from src.app_automation.fb_greet_task import _has_forbidden_word
    check("fb_greet_task 导入成功", True)

    # 测试禁词检查
    check("禁词检查(投資)", _has_forbidden_word("投資しませんか"), True)
    check("禁词检查(正常文)", not _has_forbidden_word("はじめまして！"), True)

    # 测试话术生成
    g = generate_greeting(
        display_name="Mieko Ishikawa",
        insights={"topics": ["料理", "旅行"], "age_band": "40s"},
        greeting_from_library="料理がお好きなんですね。私も大好きです！",
        use_llm=False,
    )
    check("话术生成非空", bool(g), g[:40] if g else "")
    check("话术无禁词", not _has_forbidden_word(g), g[:40] if g else "")

    # 注入测试数据并运行打招呼 dry_run
    from src.host.fb_targets_store import try_claim_target, mark_status
    from datetime import datetime, timedelta
    ok, tid = try_claim_target(
        identity_raw="final_test_greet_001",
        device_id=DEVICE,
        persona_key="jp_female_midlife",
        display_name="Test Hanako",
    )
    if tid > 0:
        friended_time = (datetime.now() - timedelta(hours=73)).strftime("%Y-%m-%d %H:%M:%S")
        mark_status(tid, "friended", DEVICE, extra_fields={
            "friended_at": friended_time,
            "insights_json": json.dumps({"topics": ["料理"], "age_band": "40s"}),
            "qualified": 1,
        })
        result = facebook_jp_female_greet(DEVICE, dry_run=True, max_greets=1)
        check("打招呼队列找到目标", result.get("queue_size", 0) >= 1, f"size={result.get('queue_size')}")
        check("dry_run greeted=1", result.get("greeted", 0) >= 1, f"greeted={result.get('greeted')}")
        # 检查审计记录（dry_run 写入，sent_ok=0）
        from src.host.database import get_conn
        with get_conn() as c:
            cnt_total = c.execute("SELECT COUNT(*) FROM fb_outbound_messages").fetchone()[0]
            cnt_dry = c.execute("SELECT COUNT(*) FROM fb_outbound_messages WHERE sent_ok=0").fetchone()[0]
        check("DM审计表有记录(dry_run)", cnt_total >= 1 and cnt_dry >= 1,
              f"total={cnt_total} dry_run={cnt_dry}")
    else:
        check("注入测试数据", False, "try_claim_target 失败")
except Exception as e:
    check("W2 打招呼任务测试", False, str(e))
    import traceback; traceback.print_exc()

# ── 4. W3 Playbook ──────────────────────────────────────────────────
print("\n[T4] W3 Playbook 模块")
try:
    from src.app_automation.fb_playbook import run_fb_jp_playbook
    check("fb_playbook 导入成功", True)
    # 只测试 greet 阶段（skip_acquire 避免真实 UI 操作）
    result = run_fb_jp_playbook(
        device_id=DEVICE,
        dry_run=True,
        skip_acquire=True,
        skip_greet=False,
        daily_limits={"max_greets": 1},
    )
    check("Playbook 运行完成(无abort)", not result.get("abort_reason"), result.get("abort_reason", ""))
    check("Playbook health 有数据", bool(result.get("health")), "")
    check("Playbook greet 有数据", result.get("greet") is not None, "")
except Exception as e:
    check("W3 Playbook 测试", False, str(e))

# ── 5. DB 表完整性 ──────────────────────────────────────────────────
print("\n[T5] 数据库表完整性")
try:
    from src.host.database import get_conn
    with get_conn() as c:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    for t in ["fb_targets_global", "fb_account_health", "fb_targets_blocklist",
              "fb_greeting_library", "fb_outbound_messages", "fb_profile_insights"]:
        check(f"表 {t} 存在", t in tables)
except Exception as e:
    check("DB 表检查", False, str(e))

# ── 汇总 ──────────────────────────────────────────────────────────
print()
print("=" * 60)
total = PASS + FAIL
print(f"结果: {PASS}/{total} PASS  {FAIL}/{total} FAIL")
print("=" * 60)
if FAIL == 0:
    print("全部测试通过!")
else:
    print(f"有 {FAIL} 项测试失败，请检查上面的 FAIL 行。")
    sys.exit(1)
