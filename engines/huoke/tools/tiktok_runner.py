#!/usr/bin/env python3
"""
TikTok 引流自动化 — 主入口脚本 (OpenClaw 集成版 v2)

阶段式流程 (按行为指标推进, 不固定天数):
  冷启动期:     只浏览 For You, 低互动, 模拟真人
  兴趣建立期:   搜索 hashtag, 提升互动, 随机测试关注
  活跃期:       智能关注 + 回关聊天 + 维持活跃度

v2 changes:
  - DeviceStateStore (SQLite) 替代 tiktok_state.json + stats.json
  - LeadsFollowTracker (SQLite) 替代 GlobalFollowTracker (JSON)
  - 多设备并行执行 (concurrent.futures)
  - AI 话术生成 (MessageRewriter)

用法 (直接执行):
  python tiktok_runner.py warmup                # 养号 (自动判断阶段)
  python tiktok_runner.py test-follow           # 手动测试关注能力
  python tiktok_runner.py follow                # 智能筛选关注
  python tiktok_runner.py chat                  # 回关聊天
  python tiktok_runner.py auto                  # 全自动 (根据阶段决定行为)
  python tiktok_runner.py status                # 查看统计数据

用法 (通过 OpenClaw Host API):
  POST /tasks  {"type": "tiktok_auto", "device_id": "...", "params": {...}}
"""

import argparse
import logging
import sys
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import List, Optional

project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from src.device_control.device_manager import get_device_manager
from src.app_automation.tiktok import TikTokAutomation
from src.app_automation.target_filter import TargetProfile
from src.leads.follow_tracker import LeadsFollowTracker
from src.host.database import init_db
from src.host.device_state import DeviceStateStore, get_device_state_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/tiktok_runner.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("tiktok_runner")

CONFIG_PATH = str(project_root / "config" / "devices.yaml")
CHAT_MESSAGES_FILE = project_root / "config" / "chat_messages.yaml"

# ═══════════════════════════════════════════════════════════════════════════
# 配置区
# ═══════════════════════════════════════════════════════════════════════════

TARGET = TargetProfile(
    country="italy",
    language="italian",
    gender="male",
    min_age=30,
    max_age=0,
    min_followers=0,
    max_followers=0,
    min_score=0.40,
)

SEED_ACCOUNTS: List[str] = []

MAX_FOLLOWS_PER_RUN = 20
MAX_CHATS_PER_RUN = 10
WARMUP_SESSIONS_PER_DAY = 3

COLD_START_MIN_WATCHED = 100
FOLLOW_TEST_PROBABILITY = 0.20
FOLLOW_TEST_MAX_PER_DAY = 2

FOLLOW_RAMP_UP = {1: 5, 2: 8, 3: 12, 4: 15}

DEFAULT_CHAT_MESSAGES = [
    "Ciao {name}! Grazie per il follow back! Mi piacciono molto i tuoi contenuti. Restiamo in contatto!",
    "Hey {name}! Ho visto che mi segui, grazie! Anche io sono in questo settore, parliamone!",
    "Grazie {name} per il follow! I tuoi video sono fantastici. Dai un'occhiata al mio profilo!",
]


# ═══════════════════════════════════════════════════════════════════════════
# 话术加载 (支持 AI 改写)
# ═══════════════════════════════════════════════════════════════════════════

def load_chat_messages() -> List[str]:
    if CHAT_MESSAGES_FILE.exists():
        try:
            import yaml
            data = yaml.safe_load(CHAT_MESSAGES_FILE.read_text(encoding="utf-8"))
            messages = data.get("messages", [])
            if messages:
                return messages
        except Exception as e:
            log.warning("读取话术配置失败: %s, 使用默认", e)
    return DEFAULT_CHAT_MESSAGES


def ai_rewrite_message(template: str, context: dict) -> str:
    """Use MessageRewriter to generate a unique variant. Falls back to template."""
    try:
        from src.ai.message_rewriter import get_rewriter
        rw = get_rewriter()
        return rw.rewrite(template, context, platform="tiktok")
    except Exception:
        msg = template
        for k, v in context.items():
            msg = msg.replace(f"{{{k}}}", str(v))
        return msg


# ═══════════════════════════════════════════════════════════════════════════
# 设备工具
# ═══════════════════════════════════════════════════════════════════════════

def get_all_devices():
    dm = get_device_manager(CONFIG_PATH)
    dm.discover_devices()
    return [d.device_id for d in dm.get_all_devices()
            if d.status.value == "connected"]


def get_tiktok(device_id: str) -> TikTokAutomation:
    dm = get_device_manager(CONFIG_PATH)
    tt = TikTokAutomation(device_manager=dm)
    tt.set_current_device(device_id)
    return tt


# ═══════════════════════════════════════════════════════════════════════════
# 命令
# ═══════════════════════════════════════════════════════════════════════════

def cmd_warmup(args):
    devices = [args.device] if args.device else get_all_devices()
    ds = get_device_state_store()

    def _warmup_one(did: str):
        ds.init_device(did)
        sessions = ds.get_sessions_today(did)
        if sessions >= WARMUP_SESSIONS_PER_DAY:
            log.info("[%s] 今天已养号 %d 次", did[:8], sessions)
            return

        phase = ds.determine_phase(did, COLD_START_MIN_WATCHED)
        day = ds.get_device_day(did)

        if phase == "cold_start":
            duration = random.randint(20, 40)
        elif phase == "interest_building":
            duration = random.randint(30, 50)
        else:
            duration = random.randint(30, 45)

        log.info("=" * 60)
        log.info("[%s] Day %d | phase=%s | 第 %d/%d 次养号 | %d 分钟",
                 did[:8], day, phase, sessions + 1,
                 WARMUP_SESSIONS_PER_DAY, duration)

        try:
            tt = get_tiktok(did)
            warmup_stats = tt.warmup_session(
                duration_minutes=duration,
                target_country=TARGET.country,
                phase=phase)
            ds.record_warmup(did, warmup_stats)
        except Exception as e:
            log.error("[%s] 养号失败: %s", did[:8], e)

    _run_parallel(devices, _warmup_one, desc="warmup")


def cmd_test_follow(args):
    devices = [args.device] if args.device else get_all_devices()
    ds = get_device_state_store()

    for did in devices:
        ds.init_device(did)
        if ds.can_follow(did):
            log.info("[%s] 已可关注", did[:8])
            continue
        try:
            tt = get_tiktok(did)
            d = tt._u2(did)
            if not tt.launch(did):
                continue
            ok = tt._random_test_follow(d, did)
            ds.mark_can_follow(did, ok)
            ds.record_follow_test(did)
            log.info("[%s] %s", did[:8],
                     "可以关注!" if ok else "暂不能关注")
        except Exception as e:
            log.error("[%s] 测试失败: %s", did[:8], e)


def cmd_follow(args):
    devices = [args.device] if args.device else get_all_devices()
    ds = get_device_state_store()
    tracker = LeadsFollowTracker()
    seeds = SEED_ACCOUNTS or None

    def _follow_one(did: str):
        ds.init_device(did)
        if not ds.can_follow(did):
            log.info("[%s] 还不能关注, 跳过", did[:8])
            return

        max_f = args.max_follows or ds.get_follow_ramp_max(did, FOLLOW_RAMP_UP)
        log.info("[%s] 开始智能关注: %s %s 30+ (最多 %d)",
                 did[:8], TARGET.country, TARGET.gender, max_f)
        try:
            tt = get_tiktok(did)
            result = tt.smart_follow(
                target=TARGET,
                max_follows=max_f,
                seed_accounts=seeds,
                global_tracker=tracker,
            )
            ds.record_follows(did, result["followed"])
            log.info("[%s] 结果: 检查 %d, 关注 %d, 跳过 %d",
                     did[:8], result["checked"],
                     result["followed"], result["skipped"])
            for u in result["users"]:
                log.info("  → %s (@%s) score=%.2f %s",
                         u["name"], u["username"], u["score"], u["reasons"])
        except Exception as e:
            log.error("[%s] 关注失败: %s", did[:8], e)

    _run_parallel(devices, _follow_one, desc="follow")


def cmd_chat(args):
    devices = [args.device] if args.device else get_all_devices()
    ds = get_device_state_store()
    tracker = LeadsFollowTracker()
    messages = load_chat_messages()

    def _chat_one(did: str):
        try:
            tt = get_tiktok(did)
            result = tt.check_and_chat_followbacks(
                messages, args.max_chats or MAX_CHATS_PER_RUN,
                global_tracker=tracker)
            ds.record_chats(did, result["messaged"])
            log.info("[%s] 聊天: 检查 %d, 发消息 %d",
                     did[:8], result["checked"], result["messaged"])
        except Exception as e:
            log.error("[%s] 聊天失败: %s", did[:8], e)

    _run_parallel(devices, _chat_one, desc="chat")


def cmd_auto(args):
    """全自动: 根据阶段自动决定做什么。多设备并行执行。"""
    devices = [args.device] if args.device else get_all_devices()
    ds = get_device_state_store()
    tracker = LeadsFollowTracker()
    messages = load_chat_messages()

    def _auto_one(did: str):
        ds.init_device(did)
        phase = ds.determine_phase(did, COLD_START_MIN_WATCHED)
        day = ds.get_device_day(did)
        sessions = ds.get_sessions_today(did)
        can_follow_now = ds.can_follow(did)

        log.info("=" * 60)
        log.info("[%s] Day %d | phase=%s | 养号 %d/%d | 关注: %s",
                 did[:8], day, phase, sessions, WARMUP_SESSIONS_PER_DAY,
                 "已解锁" if can_follow_now else "未解锁")
        log.info("=" * 60)

        tt = get_tiktok(did)

        try:
            if sessions < WARMUP_SESSIONS_PER_DAY:
                if phase == "cold_start":
                    duration = random.randint(20, 40)
                elif phase == "interest_building":
                    duration = random.randint(30, 50)
                else:
                    duration = random.randint(30, 45)

                log.info("[%s] 养号 (%d/%d) phase=%s %d分钟...",
                         did[:8], sessions + 1, WARMUP_SESSIONS_PER_DAY,
                         phase, duration)
                warmup_stats = tt.warmup_session(
                    duration_minutes=duration,
                    target_country=TARGET.country,
                    phase=phase)
                ds.record_warmup(did, warmup_stats)

            if phase == "interest_building" and not can_follow_now:
                tests_today = ds.get_follow_tests_today(did)
                if (tests_today < FOLLOW_TEST_MAX_PER_DAY
                        and random.random() < FOLLOW_TEST_PROBABILITY):
                    log.info("[%s] 随机关注测试...", did[:8])
                    try:
                        d = tt._u2(did)
                        if tt.launch(did):
                            ok = tt._random_test_follow(d, did)
                            ds.mark_can_follow(did, ok)
                            ds.record_follow_test(did)
                            can_follow_now = ok
                            if ok:
                                phase = "active"
                    except Exception as e:
                        log.error("[%s] 关注测试失败: %s", did[:8], e)

            if phase == "active" and can_follow_now:
                max_f = ds.get_follow_ramp_max(did, FOLLOW_RAMP_UP)
                log.info("[%s] 智能关注: %s %s 30+ (最多 %d)",
                         did[:8], TARGET.country, TARGET.gender, max_f)
                try:
                    result = tt.smart_follow(
                        target=TARGET,
                        max_follows=max_f,
                        seed_accounts=SEED_ACCOUNTS or None,
                        global_tracker=tracker)
                    ds.record_follows(did, result["followed"])
                    log.info("[%s] 关注结果: 检查 %d, 关注 %d",
                             did[:8], result["checked"], result["followed"])
                except Exception as e:
                    log.error("[%s] 智能关注失败: %s", did[:8], e)

                if random.random() < 0.6:
                    log.info("[%s] 关注后刷视频...", did[:8])
                    try:
                        tt.warmup_session(
                            duration_minutes=random.randint(5, 15),
                            target_country=TARGET.country,
                            phase="active")
                    except Exception:
                        pass

                log.info("[%s] 检查回关聊天...", did[:8])
                try:
                    chat_result = tt.check_and_chat_followbacks(
                        messages, MAX_CHATS_PER_RUN,
                        global_tracker=tracker)
                    ds.record_chats(did, chat_result["messaged"])
                except Exception as e:
                    log.error("[%s] 聊天失败: %s", did[:8], e)

        except Exception as e:
            log.error("[%s] 失败: %s", did[:8], e, exc_info=True)

    _run_parallel(devices, _auto_one, desc="auto")
    log.info("全部设备处理完毕")


def cmd_status(args):
    devices = get_all_devices() if not args.device else [args.device]
    ds = get_device_state_store()
    tracker = LeadsFollowTracker()

    print(f"\n{'=' * 70}")
    print(f"  TikTok 引流数据统计 (v2 — SQLite)")
    print(f"{'=' * 70}")
    print(f"\n  目标画像: {TARGET.country} | {TARGET.gender} | {TARGET.min_age}+")
    print()

    header = (f"  {'设备ID':<16} {'天数':>4} {'阶段':<14} "
              f"{'观看':>6} {'点赞':>5} {'关注':>5} {'聊天':>5} {'今日':>6}")
    print(header)
    print("  " + "-" * 68)

    for did in devices:
        ds.init_device(did)
        summary = ds.get_device_summary(did)

        phase_cn = {"cold_start": "冷启动", "interest_building": "兴趣建立",
                    "active": "活跃期"}.get(summary["phase"], summary["phase"])

        can_str = "✓" if summary["can_follow"] else "✗"
        today_sessions = summary["sessions_today"]

        print(f"  {did[:16]:<16} {summary['day']:>4} {phase_cn:<14} "
              f"{summary['total_watched']:>6} {summary['total_liked']:>5} "
              f"{summary['total_followed']:>5} {summary['total_dms_sent']:>5} "
              f"{today_sessions:>3}次{can_str}")

    global_stats = tracker.get_stats()
    print()
    print(f"  全局统计 (LeadsStore CRM):")
    print(f"    总关注: {global_stats['total_followed']} | "
          f"总回关: {global_stats['total_follow_backs']} "
          f"({global_stats['follow_back_rate']:.0%}) | "
          f"总聊天: {global_stats['total_dms']}")
    try:
        from src.leads.store import get_leads_store
        store = get_leads_store()
        pipeline = store.pipeline_stats()
        print(f"    CRM: {pipeline['total_leads']} leads | "
              f"状态: {pipeline['by_status']} | "
              f"均分: {pipeline['avg_score']}")
    except Exception:
        pass
    print()


# ═══════════════════════════════════════════════════════════════════════════
# 并行执行引擎
# ═══════════════════════════════════════════════════════════════════════════

def _run_parallel(devices: List[str], fn, desc: str = "task"):
    """Run fn(device_id) for each device. Serial if 1 device, parallel if many."""
    if len(devices) <= 1:
        for did in devices:
            fn(did)
        return

    log.info("[并行] %s: %d 台设备并行执行", desc, len(devices))
    with ThreadPoolExecutor(max_workers=min(len(devices), 4),
                            thread_name_prefix=f"tiktok-{desc}") as pool:
        futures = {pool.submit(fn, did): did for did in devices}
        for future in as_completed(futures):
            did = futures[future]
            try:
                future.result()
                log.info("[并行] %s 完成: %s", desc, did[:8])
            except Exception as e:
                log.error("[并行] %s 失败 %s: %s", desc, did[:8], e)


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    Path("logs").mkdir(exist_ok=True)
    Path("data").mkdir(exist_ok=True)
    init_db()

    p = argparse.ArgumentParser(description="TikTok 引流自动化 (OpenClaw v2)")
    p.add_argument("--device", "-d", help="指定设备序列号 (不指定则所有设备并行)")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("warmup", help="养号 (自动判断阶段)")
    sub.add_parser("test-follow", help="测试关注能力")

    pf = sub.add_parser("follow", help="智能筛选关注")
    pf.add_argument("--max-follows", type=int)

    pc = sub.add_parser("chat", help="回关聊天")
    pc.add_argument("--max-chats", type=int)

    sub.add_parser("auto", help="全自动模式 (阶段驱动, 多设备并行)")
    sub.add_parser("status", help="查看统计数据")

    args = p.parse_args()
    if not args.command:
        p.print_help()
        return

    {"warmup": cmd_warmup, "test-follow": cmd_test_follow,
     "follow": cmd_follow, "chat": cmd_chat,
     "auto": cmd_auto, "status": cmd_status}[args.command](args)


if __name__ == "__main__":
    main()
