# -*- coding: utf-8 -*-
"""
策略优化器 — A/B 测试自动应用 + 数据驱动参数动态调节。

功能:
  1. 每6小时检查 A/B 测试结果，自动将最优变体应用到下一轮任务
  2. 每日凌晨根据漏斗数据动态调整 max_follows / max_chats 参数
  3. 实验自动初始化（系统启动时确保关键实验存在）
  4. 将优化决策记录到日志，方便运营追踪

设计原则:
  - 所有调整保守（每次步进 ±2）
  - 失败时静默跳过，不影响主流程
  - 所有变更可审计（写 optimization_log.json）
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from src.host.device_registry import data_file

log = logging.getLogger(__name__)

_opt_log_path = data_file("optimization_log.json")
_params_cache_path = data_file("strategy_params.json")

# 默认参数边界
_PARAM_BOUNDS = {
    "max_follows": {"min": 5,  "max": 25, "default": 15, "step": 2},
    "max_chats":   {"min": 3,  "max": 20, "default": 10, "step": 2},
    "duration_minutes": {"min": 20, "max": 60, "default": 30, "step": 5},
}

# A/B 实验定义（自动初始化）
_EXPERIMENTS = [
    {
        "name": "dm_template_style",
        "category": "message",
        "variants": ["warm_greeting", "question_opener", "compliment_first", "direct_referral"],
    },
    {
        "name": "follow_timing",
        "category": "behavior",
        "variants": ["morning", "afternoon", "evening"],
    },
    {
        "name": "seed_selection_method",
        "category": "strategy",
        "variants": ["top_followers", "recent_active", "engagement_ratio"],
    },
    {
        "name": "dm_send_delay",
        "category": "behavior",
        "variants": ["immediate", "delay_30m", "delay_2h"],
    },
]


class StrategyOptimizer:
    """策略优化器单例。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._current_params: Dict[str, int] = {}
        self._load_params()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def _load_params(self):
        """从缓存文件加载当前参数（启动时恢复）。"""
        try:
            if _params_cache_path.exists():
                with open(_params_cache_path, encoding="utf-8") as f:
                    self._current_params = json.load(f)
                log.info("[StrategyOpt] 已恢复参数: %s", self._current_params)
            else:
                self._current_params = {k: v["default"] for k, v in _PARAM_BOUNDS.items()}
        except Exception as e:
            log.warning("[StrategyOpt] 参数加载失败，使用默认值: %s", e)
            self._current_params = {k: v["default"] for k, v in _PARAM_BOUNDS.items()}

    def _save_params(self):
        """持久化当前参数到缓存文件。"""
        try:
            _params_cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(_params_cache_path, "w", encoding="utf-8") as f:
                json.dump(self._current_params, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning("[StrategyOpt] 参数保存失败: %s", e)

    def _log_optimization(self, action: str, details: dict):
        """记录优化决策到 JSON 日志。"""
        try:
            _opt_log_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": action,
                **details,
            }
            logs = []
            if _opt_log_path.exists():
                with open(_opt_log_path, encoding="utf-8") as f:
                    logs = json.load(f)
            logs.append(entry)
            # 保留最近 500 条
            if len(logs) > 500:
                logs = logs[-500:]
            with open(_opt_log_path, "w", encoding="utf-8") as f:
                json.dump(logs, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────
    # ★ 1. 初始化 A/B 实验
    # ─────────────────────────────────────────────────────────

    def ensure_experiments(self):
        """确保所有关键 A/B 实验已创建（幂等）。"""
        try:
            from src.host.ab_testing import get_ab_store
            store = get_ab_store()
            for exp in _EXPERIMENTS:
                exp_id = store.create(
                    name=exp["name"],
                    category=exp["category"],
                    variants=exp["variants"],
                )
                log.debug("[A/B] 实验确认: %s (id=%s)", exp["name"], exp_id)
            log.info("[StrategyOpt] A/B 实验初始化完成: %d 个实验", len(_EXPERIMENTS))
        except Exception as e:
            log.warning("[StrategyOpt] A/B 实验初始化失败: %s", e)

    # ─────────────────────────────────────────────────────────
    # ★ 2. A/B 自动应用
    # ─────────────────────────────────────────────────────────

    def apply_ab_winners(self) -> Dict[str, str]:
        """
        检查所有活跃实验，将最优变体保存到全局配置。
        返回: {experiment_name: best_variant}
        """
        winners = {}
        try:
            from src.host.ab_testing import get_ab_store
            store = get_ab_store()

            for exp in _EXPERIMENTS:
                name = exp["name"]
                best = store.best_variant(name, metric="reply_received", min_samples=10)
                if best and best != "control":
                    winners[name] = best
                    log.info("[A/B] 最优变体 %s = %s", name, best)

            if winners:
                # 持久化到 ab_winner.json（tiktok.py 会读取此文件）
                winner_path = _project_root / "data" / "ab_winner.json"
                winner_path.parent.mkdir(parents=True, exist_ok=True)
                existing = {}
                if winner_path.exists():
                    with open(winner_path, encoding="utf-8") as f:
                        existing = json.load(f)
                existing.update(winners)
                existing["_updated_at"] = datetime.now(timezone.utc).isoformat()
                with open(winner_path, "w", encoding="utf-8") as f:
                    json.dump(existing, f, indent=2, ensure_ascii=False)

                self._log_optimization("ab_winners_applied", {"winners": winners})
                log.info("[StrategyOpt] A/B 胜出变体已应用: %s", winners)

        except Exception as e:
            log.warning("[StrategyOpt] A/B 自动应用失败: %s", e)

        return winners

    # ─────────────────────────────────────────────────────────
    # ★ 3. 漏斗数据驱动参数调节
    # ─────────────────────────────────────────────────────────

    def optimize_params_from_funnel(self) -> Dict[str, int]:
        """
        读取近7天漏斗数据，动态调整 max_follows / max_chats。
        策略:
          - 回关率 < 15% → 减少关注数量（质量差时降量）
          - 回关率 > 35% → 增加关注数量（效果好时扩量）
          - DM 回复率 < 10% → 减少私信频率（防封）
          - DM 回复率 > 25% → 增加私信频率
        """
        try:
            from src.leads.store import get_leads_store
            store = get_leads_store()

            # 使用现有的漏斗数据接口（近7天）
            funnel = store.get_conversion_funnel(platform="tiktok", days=7)
            if not funnel:
                return self._current_params

            follows_sent = funnel.get("followed", 0)
            follows_back = funnel.get("follow_back", 0)
            dms_sent = funnel.get("chatted", 0)
            dms_replied = funnel.get("dm_received", 0)

            follow_back_rate = follows_back / max(follows_sent, 1)
            dm_reply_rate = dms_replied / max(dms_sent, 1)

            changes = {}
            new_params = dict(self._current_params)

            # max_follows 调节
            mf_bounds = _PARAM_BOUNDS["max_follows"]
            current_mf = new_params.get("max_follows", mf_bounds["default"])
            if follow_back_rate < 0.15 and follows_sent > 50:
                new_mf = max(mf_bounds["min"], current_mf - mf_bounds["step"])
                if new_mf != current_mf:
                    changes["max_follows"] = {"old": current_mf, "new": new_mf,
                                              "reason": f"回关率偏低({follow_back_rate:.1%})"}
                    new_params["max_follows"] = new_mf
            elif follow_back_rate > 0.35 and follows_sent > 30:
                new_mf = min(mf_bounds["max"], current_mf + mf_bounds["step"])
                if new_mf != current_mf:
                    changes["max_follows"] = {"old": current_mf, "new": new_mf,
                                              "reason": f"回关率优秀({follow_back_rate:.1%})，扩量"}
                    new_params["max_follows"] = new_mf

            # max_chats 调节
            mc_bounds = _PARAM_BOUNDS["max_chats"]
            current_mc = new_params.get("max_chats", mc_bounds["default"])
            if dm_reply_rate < 0.10 and dms_sent > 30:
                new_mc = max(mc_bounds["min"], current_mc - mc_bounds["step"])
                if new_mc != current_mc:
                    changes["max_chats"] = {"old": current_mc, "new": new_mc,
                                            "reason": f"私信回复率偏低({dm_reply_rate:.1%})"}
                    new_params["max_chats"] = new_mc
            elif dm_reply_rate > 0.25 and dms_sent > 20:
                new_mc = min(mc_bounds["max"], current_mc + mc_bounds["step"])
                if new_mc != current_mc:
                    changes["max_chats"] = {"old": current_mc, "new": new_mc,
                                            "reason": f"私信回复率优秀({dm_reply_rate:.1%})，扩量"}
                    new_params["max_chats"] = new_mc

            if changes:
                with self._lock:
                    self._current_params = new_params
                self._save_params()
                self._log_optimization("params_optimized", {
                    "changes": changes,
                    "follow_back_rate": round(follow_back_rate, 4),
                    "dm_reply_rate": round(dm_reply_rate, 4),
                    "data_points": {"follows": follows_sent, "dms": dms_sent},
                })
                log.info("[StrategyOpt] 参数已调整: %s", changes)
                return changes  # 返回变更详情供告警使用

            return {}  # 无变化

        except Exception as e:
            log.warning("[StrategyOpt] 参数优化失败: %s", e)
            return self._current_params

    def get_current_params(self) -> Dict[str, int]:
        """获取当前优化后的参数（供调度器使用）。"""
        with self._lock:
            return dict(self._current_params)

    # ─────────────────────────────────────────────────────────
    # ★ 4. 后台调度循环
    # ─────────────────────────────────────────────────────────

    def _run_loop(self):
        """后台优化循环：6小时运行一次。"""
        # 启动时立即初始化实验
        time.sleep(10)  # 等待其他模块初始化完毕
        self.ensure_experiments()

        _cycle_count = 0
        while self._running:
            try:
                _cycle_count += 1
                log.debug("[StrategyOpt] 开始第 %d 轮优化循环", _cycle_count)

                # A/B 自动应用（每次循环）
                winners = self.apply_ab_winners()

                # ★ P2-3: A/B 胜出告警
                if winners:
                    try:
                        from src.host.alert_notifier import get_alert_notifier
                        notifier = get_alert_notifier()
                        winners_str = ", ".join(f"{k}={v}" for k, v in winners.items())
                        notifier.notify_event(
                            event_type="ab_winner",
                            title="A/B实验胜出变体已应用",
                            body=f"已自动应用最优变体:\n{winners_str}",
                            level="info",
                        )
                    except Exception:
                        pass

                # 参数优化（每4个循环=每天一次，在第1次循环时也执行）
                if _cycle_count == 1 or _cycle_count % 4 == 0:
                    changes = self.optimize_params_from_funnel()

                    # ★ P2-3: 参数调整告警（仅当有实质变化时）
                    if isinstance(changes, dict) and changes:
                        try:
                            from src.host.alert_notifier import get_alert_notifier
                            notifier = get_alert_notifier()
                            lines = []
                            for param, info in changes.items():
                                if isinstance(info, dict):
                                    lines.append(
                                        f"{param}: {info.get('old')} → {info.get('new')} "
                                        f"({info.get('reason', '')})"
                                    )
                            if lines:
                                notifier.notify_event(
                                    event_type="params_optimized",
                                    title="引流参数已自动调整",
                                    body="\n".join(lines),
                                    level="info",
                                )
                        except Exception:
                            pass

                    # ★ P2-1: 种子账号分层更新（每天一次，与参数优化同步）
                    try:
                        from src.host.seed_ranker import update_seed_ranks, get_seed_ranker
                        update_seed_ranks(days=30)
                        # ★ P3-4: 自动发现新优质种子
                        new_seeds = get_seed_ranker().discover_new_seeds(days=14)
                        if new_seeds:
                            log.info("[StrategyOpt] 自动发现 %d 个新种子: %s",
                                     len(new_seeds), new_seeds[:5])
                    except Exception as _sr_err:
                        log.debug("[StrategyOpt] 种子分层更新跳过: %s", _sr_err)

            except Exception as e:
                log.error("[StrategyOpt] 优化循环异常: %s", e)

            # 等待6小时
            for _ in range(6 * 3600 // 10):
                if not self._running:
                    break
                time.sleep(10)

    def start(self):
        """启动后台优化线程。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="strategy-optimizer",
        )
        self._thread.start()
        log.info("[StrategyOpt] 策略优化器已启动（6小时循环）")

    def stop(self):
        self._running = False


# ─────────────────────────────────────────────────────────────
# 单例 + 公共接口
# ─────────────────────────────────────────────────────────────

_optimizer: Optional[StrategyOptimizer] = None
_opt_lock = threading.Lock()


def get_strategy_optimizer() -> StrategyOptimizer:
    global _optimizer
    if _optimizer is None:
        with _opt_lock:
            if _optimizer is None:
                _optimizer = StrategyOptimizer()
    return _optimizer


def start_strategy_optimizer():
    """启动策略优化器（供 api.py lifespan 调用）。"""
    get_strategy_optimizer().start()


def stop_strategy_optimizer():
    """停止策略优化器（供 api.py lifespan shutdown 调用）。

    2026-05-05 Stage H.2: 让 lifespan SIGTERM 路径干净退出. instance.stop()
    设 _running=False, 6h 循环 thread 在下次 wait 醒来时退出.
    """
    global _optimizer
    if _optimizer is not None:
        _optimizer.stop()


def get_optimized_params() -> Dict[str, int]:
    """
    获取当前优化后的任务参数。
    供调度器（scheduler.py / job_scheduler.py）在创建任务时使用。
    """
    return get_strategy_optimizer().get_current_params()
