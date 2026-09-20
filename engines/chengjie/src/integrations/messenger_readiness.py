# -*- coding: utf-8 -*-
"""Messenger 收发/全自动就绪度判定（纯函数单源，P1 2026-08-13）。

背景：坐席看到的「不能收发」与「全自动 0 回复」是**两条不同的链路**被挡：

- 人工收发链：统一收件箱 → messenger-web sidecar（:8791）DOM 收发；
- 全自动链：入站 ingest → 拟稿 → AutosendWorker 投递（多一串配置闸门 +
  后台会话登记表快速失败闸 ``_session_unhealthy``——**仅拦自动路径**）。

故判定输出**双道 verdict**（manual / auto 各自 first_block），而不是一个
含糊的「不健康」。数据源真相分级（谁新谁说话）：

1. sidecar ``/accounts`` = 活体真相（logged_in / 读取滚动窗 / E2EE 占位比）；
2. 后台 ``platform_session_health`` 登记表 = 最近一次**事件**（可能滞后）——
   与 sidecar 活体背离时：人工道只记警示（不误拦），自动道按真实行为记拦
   （worker 确实按登记表快速失败）；
3. 磁盘配置 = 「重启后为真」的意图（platform_modes 封顶 / l2_autosend 开关）。

本模块零 IO——采集在 ``tools/diagnose_messenger.py``（CLI）/ 未来的路由层。
消费方共享同一判定，避免「composer 一套话、ops 卡另一套话」的口径分裂。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

try:
    from src.integrations.platform_session_health import UNHEALTHY_STATUSES
except Exception:  # pragma: no cover - CLI 独立跑时的兜底（语义同源常量）
    UNHEALTHY_STATUSES = frozenset({"expired", "needs_login", "logged_out", "failed"})

# ── 阻塞码（稳定字符串，前端/告警/巡检按码分支，勿改语义）─────────────────────
BLOCK_WEB_DISABLED = "web_disabled"                  # 配置关掉了 messenger-web 接入
BLOCK_SIDECAR_UNREACHABLE = "sidecar_unreachable"    # :8791 探活失败
BLOCK_ACCOUNT_NOT_IN_SIDECAR = "account_not_in_sidecar"  # 注册表有号、sidecar 没恢复
BLOCK_NOT_LOGGED_IN = "not_logged_in"                # sidecar 活体：登录态已丢
BLOCK_INBOX_STALLED = "inbox_stalled"                # 半死：有未读读不到正文（E2EE）
BLOCK_SESSION_UNHEALTHY = "session_unhealthy"        # 后台登记表不健康（仅拦自动道）
BLOCK_MODE_CAPPED = "mode_capped_review"             # platform_modes 封顶人审
BLOCK_WORKER_OFF = "autosend_worker_off"             # l2_autosend.enabled=false
BLOCK_DELIVER_OFF = "deliver_off"                    # AI 只拟稿不投递

WARN_REGISTRY_DIVERGENT = "registry_divergent"       # 登记表不健康但 sidecar 活体健康
WARN_E2EE_HIGH = "e2ee_ratio_high"                   # E2EE 占位比逼近半死阈值
WARN_POLL_STALE = "poll_stale"                       # sidecar 轮询久无成功
WARN_READS_FAILING = "reads_failing"                 # 读取滚动窗出现失败（未达半死）
WARN_REGISTRY_MISSING = "registry_missing"           # sidecar 有号、注册表没有

#: 与 platform_session_health 半死判定同刻度（_STALL_PLACEHOLDER_RATIO=0.6）
E2EE_WARN_RATIO = 0.6
#: sidecar 轮询成功时间落后超过该秒数 → 警示（worker 内部轮询周期远小于此）
POLL_STALE_SEC = 300.0

#: 阻塞码 → 坐席/运维「下一步动作」（人话；消费方直接展示）
ACTION_HINTS: Dict[str, str] = {
    BLOCK_WEB_DISABLED: "配置 platform_login.messenger.web_enabled 为 false —— 开启后重启实例",
    BLOCK_SIDECAR_UNREACHABLE: "messenger-web 服务未运行：检查 services/messenger-web 是否启动（:8791）",
    BLOCK_ACCOUNT_NOT_IN_SIDECAR: "该账号未在服务器浏览器恢复：到统一收件箱对它做「重新登录」（服务器完整流程）",
    BLOCK_NOT_LOGGED_IN: "登录态已丢：到统一收件箱点「重新登录」，在服务器浏览器完整登录（含 2FA）",
    BLOCK_INBOX_STALLED: "入站半死（常见 E2EE 密钥丢失）：服务器完整重登；提示恢复加密聊天时输入 PIN",
    BLOCK_SESSION_UNHEALTHY: "后台登记表标记会话不健康：自动发送被快速失败闸拦下——重新登录或等状态心跳恢复",
    BLOCK_MODE_CAPPED: "配置 inbox.auto_draft.platform_modes 把 Messenger 封顶为人审：AI 只拟稿，人工把关后发",
    BLOCK_WORKER_OFF: "自动发送引擎未开（inbox.l2_autosend.enabled=false）：只拟稿不自动发",
    BLOCK_DELIVER_OFF: "自动投递关闭（inbox.l2_autosend.deliver=false）：AI 拟稿后需人工通过",
}

_WARN_HINTS: Dict[str, str] = {
    WARN_REGISTRY_DIVERGENT: "后台登记表仍标不健康但 sidecar 活体正常——自动发送可能被旧状态拦，等下一次心跳或重登刷新",
    WARN_E2EE_HIGH: "E2EE 占位预览占比偏高（≥60%）：读取一旦开始失败会转入站半死，建议托管 PIN",
    WARN_POLL_STALE: "sidecar 轮询已 5 分钟无成功记录：登录态可能正在失效",
    WARN_READS_FAILING: "读取滚动窗出现失败（未达半死阈值）：留意是否恶化",
    WARN_REGISTRY_MISSING: "sidecar 有该账号但注册表没有：账号登记可能不完整，收件箱路由会找不到它",
}


def warn_hint(code: str) -> str:
    return _WARN_HINTS.get(code, code)


def extract_config_gates(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从合并后的 config dict 抽 Messenger 相关闸门（纯函数，缺键给保守默认）。

    注意语义：磁盘配置=「重启后为真」；活进程可能尚未装载新值。CLI 会把
    这一点显式标注在输出里，不在这里判断。

    ``web_enabled`` 必须走 ``resolve_login_switch`` 三态解析（canonical 读取，
    ``test_no_literal_switch_reads_outside_diagnostics`` 门禁）：桌面升级安装
    从未写过该键时默认开（自愈语义），字面直读会把这类部署误判成「未启用」。
    """
    cfg = cfg or {}
    try:
        from src.integrations.platform_login import resolve_login_switch
        web_on = resolve_login_switch(cfg, "platform_login.messenger.web_enabled")
    except Exception:
        web_on = False  # 解析器不可用（异常态）：保守按关，verdict 会显式点名配置
    plog = ((cfg.get("platform_login") or {}).get("messenger") or {})
    inbox = cfg.get("inbox") or {}
    l2 = inbox.get("l2_autosend") or {}
    modes = ((inbox.get("auto_draft") or {}).get("platform_modes") or {})
    return {
        "web_effective": bool(web_on),
        "web_url": str(plog.get("web_url") or "http://127.0.0.1:8791"),
        "platform_mode": str(modes.get("messenger") or ""),
        "l2_enabled": bool(l2.get("enabled", False)),
        "deliver": bool(l2.get("deliver", False)),
    }


def _sidecar_account_warnings(sc: Dict[str, Any], now: float) -> List[str]:
    warns: List[str] = []
    ratio = sc.get("e2ee_ratio")
    try:
        if ratio is not None and float(ratio) >= E2EE_WARN_RATIO:
            warns.append(WARN_E2EE_HIGH)
    except (TypeError, ValueError):
        pass
    try:
        last_ok = float(sc.get("last_poll_ok_ts") or 0.0)
        if last_ok and (now - last_ok) > POLL_STALE_SEC:
            warns.append(WARN_POLL_STALE)
    except (TypeError, ValueError):
        pass
    try:
        attempts = int(sc.get("read_attempts") or 0)
        fails = int(sc.get("read_fails") or 0)
        if attempts >= 2 and 0 < fails < attempts:
            warns.append(WARN_READS_FAILING)
    except (TypeError, ValueError):
        pass
    return warns


def _stalled(sc: Optional[Dict[str, Any]],
             inbox_health: Optional[Dict[str, Any]]) -> bool:
    """半死判定：sidecar hint 码 / 后台 inbox_health.stall_since 任一命中。"""
    if sc:
        hint = str(sc.get("inbox_hint_code") or "")
        if hint.startswith("e2ee_"):
            return True
    if inbox_health:
        try:
            if float(inbox_health.get("stall_since") or 0.0) > 0:
                return True
        except (TypeError, ValueError):
            pass
    return False


def evaluate_messenger_readiness(
    *,
    registry_accounts: Optional[List[Dict[str, Any]]] = None,
    sidecar: Optional[Dict[str, Any]] = None,
    session_registry: Optional[Dict[str, Any]] = None,
    config_gates: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """双道就绪度判定（纯函数）。

    :param registry_accounts: 账号注册表 messenger 行 ``[{account_id, status, ...}]``
    :param sidecar: ``{"reachable": bool, "accounts": [/accounts 行...], "url": str}``
    :param session_registry: 后台 ``platform_sessions`` dump 的
        ``{"sessions": {"messenger:<id>": {...}}, "inbox_health": {...}}``；
        **None = 不可用**（如 CLI 拿不到主管会话）——判定退化为 sidecar+配置，
        不猜、不因缺源报错。
    :returns: ``{"overall", "accounts": [...], "gates", "sources"}``；每账号
        ``manual`` / ``auto`` 两道各含 ``ok`` / ``first_block`` / ``blocks``。
    """
    ts = time.time() if now is None else float(now)
    gates = dict(config_gates or {})
    sidecar = sidecar or {}
    sc_reachable = bool(sidecar.get("reachable", False))
    sc_accounts = {str(a.get("account_id") or ""): a
                   for a in (sidecar.get("accounts") or [])}

    sessions = (session_registry or {}).get("sessions") or {}
    inbox_health = (session_registry or {}).get("inbox_health") or {}
    registry_available = session_registry is not None

    # 账号全集 = 注册表 ∪ sidecar（任一侧漏都要能看见）
    reg_ids = [str(r.get("account_id") or "") for r in (registry_accounts or [])]
    all_ids = list(dict.fromkeys([i for i in reg_ids if i] + list(sc_accounts)))
    reg_by_id = {str(r.get("account_id") or ""): r for r in (registry_accounts or [])}

    accounts_out: List[Dict[str, Any]] = []
    for aid in all_ids:
        sc = sc_accounts.get(aid)
        key = f"messenger:{aid}"
        sess = sessions.get(key) or {}
        ih = inbox_health.get(key) or {}

        manual_blocks: List[str] = []
        auto_only: List[str] = []
        warnings: List[str] = []

        # ── 人工收发道（配置级 → 进程级 → 会话级，先致命者先说话）──────────
        if not gates.get("web_effective", True):
            manual_blocks.append(BLOCK_WEB_DISABLED)
        if sc_reachable:
            if sc is None:
                manual_blocks.append(BLOCK_ACCOUNT_NOT_IN_SIDECAR)
            else:
                if sc.get("logged_in") is False:
                    manual_blocks.append(BLOCK_NOT_LOGGED_IN)
                if _stalled(sc, ih):
                    manual_blocks.append(BLOCK_INBOX_STALLED)
                warnings.extend(_sidecar_account_warnings(sc, ts))
        else:
            manual_blocks.append(BLOCK_SIDECAR_UNREACHABLE)

        # 后台登记表：sidecar 活体健康时它只算警示；sidecar 不可用/没这号时，
        # 它是仅存的会话证词 → 升为人工道阻塞（宁可指路重登，不装不知道）。
        sess_status = str(sess.get("status") or "")
        sess_unhealthy = sess_status in UNHEALTHY_STATUSES
        if sess_unhealthy:
            if sc is not None and sc.get("logged_in") is not False:
                warnings.append(WARN_REGISTRY_DIVERGENT)
            elif BLOCK_SIDECAR_UNREACHABLE not in manual_blocks \
                    and BLOCK_ACCOUNT_NOT_IN_SIDECAR not in manual_blocks:
                manual_blocks.append(BLOCK_SESSION_UNHEALTHY)

        if sc is not None and aid not in reg_by_id:
            warnings.append(WARN_REGISTRY_MISSING)

        # ── 全自动道 = 人工道 + 自动专属闸门（真实拦截顺序）────────────────
        if sess_unhealthy and WARN_REGISTRY_DIVERGENT in warnings:
            # worker 的快速失败闸按登记表判——活体健康也照拦（真实行为）
            auto_only.append(BLOCK_SESSION_UNHEALTHY)
        if str(gates.get("platform_mode") or "") == "review":
            auto_only.append(BLOCK_MODE_CAPPED)
        if not gates.get("l2_enabled", False):
            auto_only.append(BLOCK_WORKER_OFF)
        elif not gates.get("deliver", False):
            auto_only.append(BLOCK_DELIVER_OFF)

        auto_blocks = manual_blocks + auto_only
        row = {
            "account_id": aid,
            "label": str((reg_by_id.get(aid) or {}).get("label")
                         or (sc or {}).get("pushname") or ""),
            "in_registry": aid in reg_by_id,
            "in_sidecar": sc is not None,
            # 注册表 status 透传（online=编排器期望在线；offline=运营登出/未登录）。
            # watchdog 的 not_restored 对账只看 online——offline 号由 UI/CLI 提示。
            "registry_status": str((reg_by_id.get(aid) or {}).get("status") or ""),
            "manual": {
                "ok": not manual_blocks,
                "first_block": manual_blocks[0] if manual_blocks else "",
                "blocks": manual_blocks,
            },
            "auto": {
                "ok": not auto_blocks,
                "first_block": auto_blocks[0] if auto_blocks else "",
                "blocks": auto_blocks,
            },
            "warnings": warnings,
            "action": ACTION_HINTS.get(
                (manual_blocks or auto_blocks or [""])[0], ""),
            "signals": {
                "logged_in": None if sc is None else bool(sc.get("logged_in", False)),
                "session_status": sess_status,
                "inbox_hint_code": str((sc or {}).get("inbox_hint_code") or ""),
                "e2ee_ratio": (sc or {}).get("e2ee_ratio"),
                "e2ee_pin_set": (sc or {}).get("e2ee_pin_set"),
                "read_attempts": (sc or {}).get("read_attempts"),
                "read_fails": (sc or {}).get("read_fails"),
                "inbox_unread": (sc or {}).get("inbox_unread"),
                "last_inbound_ts": (sc or {}).get("last_inbound_ts"),
                "last_poll_ok_ts": (sc or {}).get("last_poll_ok_ts"),
            },
        }
        accounts_out.append(row)

    if not sc_reachable:
        overall = "sidecar_down"
    elif not accounts_out:
        overall = "no_accounts"
    elif any(not a["manual"]["ok"] for a in accounts_out):
        overall = "manual_blocked"
    elif any(not a["auto"]["ok"] for a in accounts_out):
        overall = "auto_blocked"
    else:
        overall = "ready"

    return {
        "generated_at": ts,
        "overall": overall,
        "gates": gates,
        "sources": {
            "sidecar_reachable": sc_reachable,
            "session_registry_available": registry_available,
        },
        "accounts": accounts_out,
    }
