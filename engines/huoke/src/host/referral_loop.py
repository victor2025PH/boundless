# -*- coding: utf-8 -*-
"""引流闭环分级安全开关（热切换，2026-08-13）。

Phase 20 的引流回复检测 / LINE 派发 / 发送 / SLA 死信回收基础设施齐备，但一直
默认关闭。直接「翻 scheduled_jobs.enabled + 关掉全局 disable_json_scheduled_jobs」
会一次性把 `facebook_send_referral_replies`（真给用户发消息）也打开——不可控、
难回退。AvatarHub 实锤过「带翻牌计划的开关半开一躺十天没人扳」，这里用一个
**独立于全局 cron 闸门**的三档热开关根治：

    off      默认。即便 cron 到点、全局闸门开着，引流动作也 no-op（带原因）。
    dry_run  跑检测/统计，但**绝不写事件、绝不发消息**。观察期安全档：
             能在真机上看到「回复被识别了几条」而不污染漏斗、不触达用户。
    live     正常执行。

与现有闸门的关系（三重防线，从里到外）：
    referral_loop 档(本模块)  ×  scheduled_jobs[].enabled  ×  disable_json_scheduled_jobs
任一为「关」都不会跑。本档是**最内层、可热切、默认最保守**的一层，让
「先 dry_run 观察 → 再 live」有一个免重启、可秒回退的旋钮。

模式来源优先级（每次调用重新读，热生效、免重启）：
    1) data/referral_loop_mode 文件（单行 off|dry_run|live）——热切换首选
    2) 环境变量 HUOKE_REFERRAL_LOOP_MODE
    3) 默认 off

谁来把它扳到 live：由运营在联调通过、真机验证「回复能识别、话术合规」后，
`set_mode("live")` 或直接写 data/referral_loop_mode。切档即时生效，无需重启。
"""
from __future__ import annotations

import logging
import os
from typing import Tuple

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_DRY_RUN = "dry_run"
MODE_LIVE = "live"
_VALID = {MODE_OFF, MODE_DRY_RUN, MODE_LIVE}

_ENV_VAR = "HUOKE_REFERRAL_LOOP_MODE"
_FLAG_NAME = "referral_loop_mode"


def _normalize(raw: str) -> str:
    v = (raw or "").strip().lower().replace("-", "")
    if v in ("dryrun", "dry_run", "dry"):
        return MODE_DRY_RUN
    if v in ("off", "disable", "disabled", "0", "false", "no"):
        return MODE_OFF
    if v in ("live", "on", "enable", "enabled", "1", "true", "yes"):
        return MODE_LIVE
    return ""


def _flag_path():
    from src.host.device_registry import data_file
    return data_file(_FLAG_NAME)


def _from_flag() -> str:
    try:
        p = _flag_path()
        if p.exists():
            first = ""
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    first = line
                    break
            return _normalize(first)
    except Exception as e:  # pragma: no cover - 读旗标失败退回 env/默认
        logger.debug("[referral_loop] 读旗标失败: %s", e)
    return ""


def mode() -> str:
    """当前引流闭环模式（每次调用重新解析，热生效）。"""
    from_flag = _from_flag()
    if from_flag:
        return from_flag
    from_env = _normalize(os.environ.get(_ENV_VAR, ""))
    if from_env:
        return from_env
    return MODE_OFF


def source() -> str:
    """当前模式来自哪里（flag/env/default），供 /status 与日志排障。"""
    if _from_flag():
        return "flag"
    if _normalize(os.environ.get(_ENV_VAR, "")):
        return "env"
    return "default"


def is_enabled() -> bool:
    return mode() != MODE_OFF


def is_dry_run() -> bool:
    return mode() == MODE_DRY_RUN


def apply_to_scheduled(action: str,
                       params: dict) -> Tuple[bool, dict, dict]:
    """**cron 定时侧**引流动作的统一准入（只作用于自动跑；手动 POST /tasks 不受此限）。

    刻意只在调度器这一层拦——安全隐患是「无人值守自动跑/自动发」，而非运营手动
    点的任务。返回 (should_run, effective_params, info)：
      off      → (False, params, info)              调度器直接 skip，不派发
      dry_run  → (True, params∪{dry_run:True}, info) 注入 dry_run，只检测不写不发
      live     → (True, params, info)               正常派发
    info = {referral_loop_mode, referral_loop_source, action}，判定单一真相只在本模块。
    """
    m = mode()
    info = {
        "referral_loop_mode": m,
        "referral_loop_source": source(),
        "action": action,
    }
    if m == MODE_OFF:
        return False, dict(params or {}), info
    eff = dict(params or {})
    if m == MODE_DRY_RUN:
        eff["dry_run"] = True
    return True, eff, info


def describe() -> dict:
    """当前开关姿态（供 API / 日志）。"""
    return {
        "mode": mode(),
        "source": source(),
        "enabled": is_enabled(),
        "dry_run": is_dry_run(),
        "flag_file": _FLAG_NAME,
        "env_var": _ENV_VAR,
    }


def set_mode(new_mode: str) -> str:
    """写 data/referral_loop_mode 旗标文件（热生效）。返回落地的规范化模式。

    供 API / 运维 / 测试切档；非法值抛 ValueError，绝不静默落一个坏档。
    """
    norm = _normalize(new_mode)
    if not norm:
        raise ValueError(
            f"非法引流闭环模式 {new_mode!r}，仅允许 off|dry_run|live")
    p = _flag_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(norm + "\n", encoding="utf-8")
    logger.info("[referral_loop] 模式已切换为 %s（旗标 %s）", norm, p)
    return norm
