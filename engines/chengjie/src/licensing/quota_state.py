# -*- coding: utf-8 -*-
"""额度状态单一事实源（quotawall v2，2026-08-21）。

背景：本仓有多套并存「水表」——授权字符池（license included+topup / 首启体验档
local_trial）、Token 钱包（licensing.token_ledger，enforce=降级不断线）、托管 AI
日额度（云端网关按日计量）、坐席月度字符额度（usage.agent_chars）。此前每个消费
面各读各的表：顶栏徽章只认字符池、拦截弹层只认字符池、Token 用尽只降级零解释、
坐席 403 只有一条报错 toast——同一个「AI 为什么不干活」在不同面上有不同答案。

本模块 = ``peer_bot_guard.budget_flags`` 同款模式：纯函数 ``resolve_quota_state``
推导唯一裁决（verdict + 该弹哪面墙），顶栏 pill / 拦截弹层 / 未来的入口锁态共读
一份，绝不各算一套。装配函数 ``collect_quota_state`` 负责 I/O，任何一路子快照
失败都降级为「该表不可见」——观测链绝不把工作台弄崩。

裁决优先级（重者先；同时命中只报最重的一个，弹层一次只讲一件事）：
  chars / trial（授权字符池硬拦 402；体验档单列 trial——出路是「注册领免费
      100 万字符」而不是「买」，恢复动线完全不同）
  > agent（坐席月度额度 403——只挡当前请求用户，但对 TA 就是全挡；master
      在 check_request_quota 内天然放行，故 master 永远看不到这面墙）
  > tok（Token 钱包 enforce 降级——服务还在只是省耗模式，措辞必须诚实：
      是「降级」不是「停摆」）
  > daily（托管日额度用尽——明天自动恢复，最轻；**刻意不进额度墙**，它的
      弹层=既有 #ws-aitrial-upsell 每日推销窗，前端调度器据 verdict 互斥）
  > low > ok

命名注意：响应字段用 ``tok``/``tok_out`` 而非 token——坐席可读端点有防泄漏门禁
断言响应体不含 ``token`` 子串（防凭证类字段溜出去），裁决值撞词属误伤，改名
比削弱门禁正确。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: 前端 feat 探测握手：payload.state.feat == "qs1" 才走新版弹层/锁态逻辑，
#: 旧后端（无 state 字段）自动回落 legacy 行为——模板热更新先于重启上线的
#: 中间态必须自洽（本仓混合态纪律）。
QUOTA_STATE_FEAT = "qs1"

#: 弹层变体常量（前端 __wsQuotaWall 的 variant 词表；"" = 不弹）。
WALL_NONE = ""
WALL_CHARS = "chars"
WALL_TRIAL = "trial"
WALL_TOK = "tok"
WALL_AGENT = "agent"


def resolve_quota_state(
    *,
    quota: Optional[Dict[str, Any]],
    level: str,
    wallet: Optional[Dict[str, Any]] = None,
    hosted: Optional[Dict[str, Any]] = None,
    agent: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """四表合议 → 唯一裁决（纯函数，零 I/O）。

    入参契约（都允许 None/缺键，缺=该表不存在）：
      quota  = license_routes._quota_snapshot() 形状（included_chars/used_chars/
               remaining_chars/exceeded/source/trial_hours_left/trial_expired）
      level  = license_routes.quota_level(quota) 输出（ok|low|out）——阈值单源
               在服务端，本函数不重算（两处算必然漂移）。
      wallet = token_ledger.wallet_snapshot() 子集（enabled/enforce/balance/
               active_granted/expired_lost/monthly）
      hosted = hosted_gateway.quota_probe() 形状（enabled/used/budget/remaining/
               busy/exhausted/error?）
      agent  = agent_char_usage.check_request_quota(request) 形状（allowed/
               enabled/enforce/used/quota）

    返回（全部可安全给任意登录坐席看，无敏感字段）：
      {feat, verdict, wall, level, tok{}, hosted{}, agent{}}
    """
    q = dict(quota or {})
    w = dict(wallet or {})
    h = dict(hosted or {})
    a = dict(agent or {})

    src = str(q.get("source") or "license")
    trial = src == "local_trial"
    # 体验档「窗口到期但字符没用完」也算 out——trial_expired 单独成立即拦
    #（quota_store 的 exceeded 已并入窗口判定，此处 or 是防御性冗余不是双源）。
    chars_out = bool(q.get("exceeded")) or bool(trial and q.get("trial_expired"))

    tok_enabled = bool(w.get("enabled"))
    tok_enforce = bool(w.get("enforce"))
    # funded 语义与 token_ledger.should_degrade_action 完全同源：从未注资的
    # 部署不适用 Token 语义（防 enforce 一开就把存量部署全体标成「用尽」）。
    tok_funded = (
        int(w.get("active_granted") or 0) > 0
        or int(w.get("expired_lost") or 0) > 0
    )
    tok_out = bool(
        tok_enabled and tok_enforce and tok_funded
        and int(w.get("balance") or 0) <= 0
    )

    agent_blocked = bool(a) and (a.get("allowed") is False)

    hosted_enabled = bool(h.get("enabled")) and not h.get("error")
    # busy（通道全局繁忙）不是用户的额度问题，不算 daily_out（与升级窗同判据）。
    daily_out = bool(hosted_enabled and h.get("exhausted") and not h.get("busy"))

    if chars_out:
        verdict = "trial_out" if trial else "chars_out"
        wall = WALL_TRIAL if trial else WALL_CHARS
    elif agent_blocked:
        verdict, wall = "agent_out", WALL_AGENT
    elif tok_out:
        verdict, wall = "tok_out", WALL_TOK
    elif daily_out:
        # daily 的弹层与横幅归 #ws-aitrial 体系（已有每日一次治理），这里只报
        # verdict 供互斥；pill 也不抢（pill 是字符/Token 语义，托管额度有绿条）。
        verdict, wall = "daily_out", WALL_NONE
    elif str(level or "ok") == "low":
        verdict, wall = "low", WALL_NONE
    else:
        verdict, wall = "ok", WALL_NONE

    pill_level = "out" if wall else str(level or "ok")

    return {
        "feat": QUOTA_STATE_FEAT,
        "verdict": verdict,
        "wall": wall,
        "level": pill_level,
        "tok": {
            "enabled": tok_enabled,
            "enforce": tok_enforce,
            "funded": tok_funded,
            "balance": int(w.get("balance") or 0),
            "monthly": int(w.get("monthly") or 0),
            "out": tok_out,
        },
        "hosted": {
            "enabled": hosted_enabled,
            "exhausted": bool(h.get("exhausted")),
            "busy": bool(h.get("busy")),
            "remaining": int(h.get("remaining") or 0),
            "budget": int(h.get("budget") or 0),
        },
        "agent": {
            "enabled": bool(a.get("enabled")),
            "blocked": agent_blocked,
            "used": int(a.get("used") or 0),
            "quota": int(a.get("quota") or 0),
        },
    }


def forecast_exhaustion(
    remaining: Any, daily: Any,
) -> Optional[Dict[str, Any]]:
    """近 N 天日均外推「还能用几天」（纯函数）。

    与用量页 ``uqRenderExhaust`` 同一算式（remaining ÷ 日均），quotawall v2 起
    上移服务端单源——顶栏 tooltip / 预警条 / 会员页共用同一个数字。
    诚实边界：不限量（remaining=None）/ 无历史 / 近窗零消耗 → None
    （外推不了就不编，「预计 ∞ 天」比不显示更糟）。
    """
    try:
        if remaining is None:
            return None
        days = [max(0, int(x or 0)) for x in (daily or [])]
        if not days:
            return None
        avg = sum(days) / float(len(days))
        if avg <= 0:
            return None
        left = max(0.0, float(remaining)) / avg
        return {"days_left": round(left, 1), "burn7": int(avg)}
    except Exception:
        return None


def _shop_url(config_manager: Any, *, quota_exceeded: bool) -> str:
    """充值/购买深链（licensing.shop_url 未配置 → 空串=前端不渲染按钮）。

    复用 shop_link 的 offer 解析：quota_exceeded=True 时深链偏向加量/充值
    offer（与会员页同一套映射，绝不在这里另编 SKU）。
    """
    try:
        cfg = getattr(config_manager, "config", None) or {}
        base = str(((cfg.get("licensing") or {}).get("shop_url")) or "").strip()
        if not base:
            return ""
        lic: Dict[str, Any] = {}
        try:
            from src.licensing import get_license_manager

            st = get_license_manager().status()
            lic = {
                "sku_id": getattr(st, "sku_id", "") or "",
                "product_id": getattr(st, "product_id", "") or "",
                "plan": getattr(st, "plan", "") or "",
                "lic_id": getattr(st, "lic_id", "") or "",
            }
        except Exception:
            lic = {}
        from src.licensing.shop_link import shop_cta_from_license

        return str(
            shop_cta_from_license(base, lic, quota_exceeded=quota_exceeded)
            .get("url") or ""
        )
    except Exception:
        logger.debug("[quota_state] shop_url 解析失败（回空）", exc_info=True)
        return ""


def collect_quota_state(
    *,
    quota: Dict[str, Any],
    level: str,
    request: Any = None,
    config_manager: Any = None,
) -> Dict[str, Any]:
    """装配四表快照 → resolve（I/O 面；任一子快照失败按「该表不存在」降级）。

    quota/level 由调用方传入（license_routes 的轮询端点已经算过一份，不双读）；
    wallet/hosted/agent 三路在此拉取：
      - wallet_snapshot 内含幂等的当月含量补账（会员页同款顺路触发）；
      - quota_probe 自带 60s 进程缓存，5 分钟轮询下近零开销；
      - check_request_quota 需要 request（坐席身份），拿不到就当无此表。
    绝不抛。
    """
    wallet = hosted = agent = None
    try:
        from src.licensing.token_ledger import wallet_snapshot

        w = wallet_snapshot()
        wallet = {
            k: w.get(k)
            for k in ("enabled", "enforce", "balance",
                      "active_granted", "expired_lost", "monthly")
        }
    except Exception:
        logger.debug("[quota_state] wallet 快照失败（忽略）", exc_info=True)
    try:
        if config_manager is not None:
            from src.ai.hosted_gateway import quota_probe

            hosted = quota_probe(config_manager)
    except Exception:
        logger.debug("[quota_state] hosted 快照失败（忽略）", exc_info=True)
    try:
        if request is not None:
            from src.utils.agent_char_usage import check_request_quota

            agent = check_request_quota(request)
    except Exception:
        logger.debug("[quota_state] agent 快照失败（忽略）", exc_info=True)

    state = resolve_quota_state(
        quota=quota, level=level, wallet=wallet, hosted=hosted, agent=agent)
    state["shop_url"] = _shop_url(
        config_manager,
        quota_exceeded=state["wall"] in (WALL_CHARS, WALL_TRIAL, WALL_TOK),
    )
    # 预计耗尽（P2）：近 7 天日均外推。展示数据不参与 verdict；lic_id 只在
    # 进程内流转（响应白名单外），前端只拿到 days_left/burn7 两个数字。
    state["forecast"] = None
    try:
        q = dict(quota or {})
        rem = q.get("remaining_chars")
        if rem is not None and int(q.get("included_chars") or 0) > 0:
            from src.licensing.quota_store import current_daily_totals

            daily = current_daily_totals(7, lic_id=str(q.get("lic_id") or ""))
            state["forecast"] = forecast_exhaustion(rem, daily)
    except Exception:
        logger.debug("[quota_state] forecast 失败（置空）", exc_info=True)
    # 出站拦截视图（P3-2）：额度四表都说「有钱」而 AI 还是不回，答案多半在
    # 业务频控/安全刹车层——把 outbound_policy 的拦截计数并进同一份「为什么
    # 不干活」快照。展示数据不参与 verdict；层名用 tok 非 token（防泄漏门禁）。
    state["outbound"] = None
    try:
        from src.ops.outbound_policy import blocked_brief

        state["outbound"] = blocked_brief()
    except Exception:
        logger.debug("[quota_state] outbound 快照失败（置空）", exc_info=True)
    return state


__all__ = [
    "QUOTA_STATE_FEAT",
    "WALL_NONE", "WALL_CHARS", "WALL_TRIAL", "WALL_TOK", "WALL_AGENT",
    "resolve_quota_state",
    "forecast_exhaustion",
    "collect_quota_state",
]
