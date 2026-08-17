# -*- coding: utf-8 -*-
"""自动化覆盖率聚合（P1 2026-08-09，.198/.104 事故第三批——给老板的那张页）。

回答一个此前只能逐会话点开才能拼出来的问题：**「多少会话真在全自动跑？没在跑
的，被什么压住了？」**——.198/.104 两次事故的共同形态都是「老板以为全自动开着，
实际整机在人审/手动」，而没有任何一个界面能一眼看出这件事。

口径（与护栏同源，绝不另算一套）：

- 基础档位＝conversation_settings 显式行；无显式行＝运行时回落全局默认
  （``automation_mode.global_automation_mode_from_config``），单列 ``defaulted``
  计数如实标注「这些是按全局默认在跑」。
- 账号级封顶＝``effective_automation.compute_mode_caps``（平台/业务线/冷启动预热，
  与 A/B 两线同一实现）——账号被封顶时，其 auto_ai 会话实际按 review 执行，
  ``effective_auto`` 计 0（这正是「界面亮全自动、实际全进人审」的量化）。
- 接管态＝source 以 ``takeover`` 开头的 manual（与让位横幅/自动接回同判定）。
- 待审草稿稿龄分桶：<2h（新鲜）/ 2-24h（该处理了）/ >24h（烂稿风险，
  与 stale_approve_hours 默认阈值同界）。

只读纯函数：store 出事实行（``automation_coverage_rows``/``list_drafts``），
本模块出口径；任何一段取数失败该段缺席（fail-open），绝不 500。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MODES = ("auto_ai", "review", "multi_choice", "manual")


def _account_caps(platform: str, account_id: str,
                  config: Optional[Dict[str, Any]],
                  now: float) -> List[Dict[str, Any]]:
    try:
        from src.inbox.effective_automation import (
            compute_mode_caps,
            serialize_caps,
        )
        return serialize_caps(compute_mode_caps(
            platform=platform, account_id=account_id, config=config, now=now))
    except Exception:
        logger.debug("[coverage] caps 求值失败（忽略）%s:%s",
                     platform, account_id, exc_info=True)
        return []


def _account_status(platform: str, account_id: str) -> str:
    try:
        from src.integrations.account_registry import peek_account
        row = peek_account(platform, account_id)
        return str((row or {}).get("status") or "")
    except Exception:
        return ""


def collect_automation_coverage(
    store: Any,
    config: Optional[Dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """聚合快照。结构：

    ``{ok, generated_at, global_mode, totals:{conversations, groups,
    by_mode:{...}, defaulted, effective_auto, capped_auto, takeover_manual},
    accounts:[{platform, account_id, status, convs, groups, by_mode, defaulted,
    takeover_manual, caps, effective_auto, effective_auto_pct}],
    drafts:{pending, by_age:{fresh_2h, day, stale}, oldest_h},
    counters:{takeover_total, rearm_total}}``
    """
    ts_now = float(now or time.time())
    cfg = config if isinstance(config, dict) else {}
    from src.inbox.automation_mode import global_automation_mode_from_config
    global_mode = global_automation_mode_from_config(cfg)

    out: Dict[str, Any] = {
        "ok": True,
        "generated_at": round(ts_now, 1),
        "global_mode": global_mode,
        "totals": {},
        "accounts": [],
        "drafts": {},
        "counters": {},
    }

    # ── 会话 × 档位 按账号聚合 ─────────────────────────────────────────
    rows: List[Dict[str, Any]] = []
    if store is not None and hasattr(store, "automation_coverage_rows"):
        try:
            rows = store.automation_coverage_rows() or []
        except Exception:
            logger.debug("[coverage] 会话行取数失败（忽略）", exc_info=True)
            rows = []
    accounts: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        key = (r.get("platform") or "", r.get("account_id") or "default")
        acc = accounts.get(key)
        if acc is None:
            acc = accounts[key] = {
                "platform": key[0], "account_id": key[1],
                "convs": 0, "groups": 0, "defaulted": 0,
                "takeover_manual": 0,
                "by_mode": {m: 0 for m in _MODES},
            }
        acc["convs"] += 1
        if str(r.get("chat_type") or "") in ("group", "channel"):
            acc["groups"] += 1
        mode = r.get("mode")
        if mode is None:
            acc["defaulted"] += 1
            mode = global_mode
        mode = str(mode)
        if mode in acc["by_mode"]:
            acc["by_mode"][mode] += 1
        if mode == "manual" and str(r.get("source") or "").startswith("takeover"):
            acc["takeover_manual"] += 1

    totals = {
        "conversations": 0, "groups": 0, "defaulted": 0,
        "takeover_manual": 0, "effective_auto": 0, "capped_auto": 0,
        "by_mode": {m: 0 for m in _MODES},
    }
    account_list: List[Dict[str, Any]] = []
    for (plat, acct), acc in sorted(accounts.items()):
        caps = _account_caps(plat, acct, cfg, ts_now)
        auto_n = int(acc["by_mode"].get("auto_ai") or 0)
        capped = bool(caps)   # 任一层封顶 → auto_ai 实际不直发
        acc["caps"] = caps
        acc["status"] = _account_status(plat, acct)
        acc["effective_auto"] = 0 if capped else auto_n
        acc["effective_auto_pct"] = (
            round(acc["effective_auto"] * 100.0 / acc["convs"], 1)
            if acc["convs"] else 0.0)
        account_list.append(acc)
        totals["conversations"] += acc["convs"]
        totals["groups"] += acc["groups"]
        totals["defaulted"] += acc["defaulted"]
        totals["takeover_manual"] += acc["takeover_manual"]
        totals["effective_auto"] += acc["effective_auto"]
        totals["capped_auto"] += (auto_n if capped else 0)
        for m in _MODES:
            totals["by_mode"][m] += int(acc["by_mode"].get(m) or 0)
    out["accounts"] = account_list
    out["totals"] = totals

    # ── 待审草稿稿龄分桶 ───────────────────────────────────────────────
    drafts = {"pending": 0, "by_age": {"fresh_2h": 0, "day": 0, "stale": 0},
              "oldest_h": 0.0}
    if store is not None and hasattr(store, "list_drafts"):
        try:
            pend = store.list_drafts(status="pending", limit=500) or []
            drafts["pending"] = len(pend)
            oldest = 0.0
            for d in pend:
                try:
                    created = float(d.get("created_at") or 0.0)
                except (TypeError, ValueError):
                    created = 0.0
                if created <= 0:
                    continue
                age_h = (ts_now - created) / 3600.0
                oldest = max(oldest, age_h)
                if age_h < 2:
                    drafts["by_age"]["fresh_2h"] += 1
                elif age_h < 24:
                    drafts["by_age"]["day"] += 1
                else:
                    drafts["by_age"]["stale"] += 1
            drafts["oldest_h"] = round(oldest, 1)
        except Exception:
            logger.debug("[coverage] 草稿取数失败（忽略）", exc_info=True)
    out["drafts"] = drafts

    # ── 接管/接回 进程计数（本进程窗口口径，重启清零——如实标注）─────────
    try:
        from src.inbox.automation_mode_stats import metrics_snapshot
        snap = metrics_snapshot()
        out["counters"] = {
            "takeover_total": int(snap.get("takeover_total") or 0),
            "rearm_total": int(snap.get("rearm_total") or 0),
        }
    except Exception:
        out["counters"] = {}
    return out


__all__ = ["collect_automation_coverage"]
