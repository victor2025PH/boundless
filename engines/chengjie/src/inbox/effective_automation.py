# -*- coding: utf-8 -*-
"""会话「有效自动化档位」单一事实源（P0 2026-08-07，双向全自动排障沉淀）。

背景（.104/.198 内测机「双向全自动只有一边回」排查结论）：UI 档位
（``conversation_settings.automation_mode``）只是**基础值**；真正决定「AI 会不会
自己发」的还有一串**档位封顶**——平台封顶（``platform_modes``）、账号业务线封顶
（``business_line_modes``，内置默认 translation→review）、冷启动预热封顶
（新号接入 < 72h 强制人审，配置缺失也生效）。此前这三层只长在 B 线拟稿回调里
（``autodraft_helpers.make_auto_draft_cb`` 三段内联代码），后果有三：

1. **A 线不认封顶**：companion 架构下新号照样直发（预热闸形同虚设），而
   System-Z 架构下同一账号全进人审——同一安全闸在两种部署形态行为不一致；
2. **对人完全不可见**：下拉框仍亮「🚀 全自动」，坐席/测试者没有任何入口知道
   「实际在按人审跑」——.198 现场把这体感成「产品坏了」；
3. **排障要翻代码**：封顶既不落库也无 API，「为什么没自动回」只能人肉逐层猜。

本模块把「基础档位 → 逐层封顶 → 有效档位」收成单一实现，四个消费方：

- B 线 ``make_auto_draft_cb``（原三段内联封顶改调这里，行为逐层等价）；
- A 线 ``telegram_client`` 档位闸（**补齐**：封顶后非 auto_ai 即让位，B 线因同
  一判定不再让位而接住拟稿——不双发、不丢消息）；
- ``GET /api/unified-inbox/automation``（``effective`` 段 → 前端档位胶囊说真话）；
- ``tools/why_no_reply.py`` 排障 CLI（只读注入 ``connected_at``/``business_line``，
  不触发任何写路径）。

刻意不做的（改动前先读）：

- **peer_bot_guard 的行为刹车不进本模块**（复读/秒回/预算）：那些是「事件级
  拦截」，自带落库/告警/救济语义，且预算依赖 store 台账——档位封顶是「状态级
  上限」，纯函数可在只读上下文安全求值。两者叠加语义由各消费方保持原状。
- **不在本模块写库**：bootstrap（首条入站持久化档位）仍由 B 线调用方决定；
  resolver 必须可被 API/CLI/巡检在零副作用前提下调用。
- **失败方向**：任何一层求值异常 → 该层不封顶（fail-open），与旧内联实现相同
  ——封顶是护栏不是主链，绝不能变成回复链路的新故障点。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.inbox.automation_mode import (
    global_automation_mode_from_config,
)

# 内置业务线封顶默认值（与 AutoDraftConfig.business_line_ceilings 同源语义：
# 翻译线账号 AI 只拟稿人审后发，绝不自动发送；账号无标签 = 不封顶）。
_BUILTIN_BUSINESS_LINE_CEILINGS: Dict[str, str] = {"translation": "review"}


@dataclass(frozen=True)
class ModeCap:
    """一层档位封顶的判定结果。

    layer   : "platform" | "business_line" | "warmup"
    ceiling : 封顶档位（当前均为 "review"，机制上允许 "manual"）
    detail  : 机器可读细节（平台名 / 业务线名 / age_h=…），进日志与 API
    until_ts: 仅 warmup —— 预热窗结束时刻（前端可倒计时；其余层 0）
    """
    layer: str
    ceiling: str
    detail: str = ""
    until_ts: float = 0.0


def serialize_caps(caps: List[ModeCap]) -> List[Dict[str, Any]]:
    """ModeCap → JSON 安全 dict 列表（API / CLI 共用）。"""
    return [
        {
            "layer": c.layer,
            "ceiling": c.ceiling,
            "detail": c.detail,
            "until_ts": round(float(c.until_ts or 0.0), 1),
        }
        for c in caps
    ]


def platform_ceilings_from_config(
    config: Optional[Dict[str, Any]],
    fallback: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """平台封顶表（活读 ``inbox.auto_draft.platform_modes``）。

    语义与 B 线旧内联实现逐字一致：键**缺席**＝没配过 → 用 fallback 快照；
    显式 ``{}`` ＝运营清空了封顶 → 尊重空表；config 不可用 → fallback。
    """
    fb = dict(fallback or {})
    try:
        pm = ((config or {}).get("inbox") or {}).get(
            "auto_draft", {}).get("platform_modes")
        if isinstance(pm, dict):
            return {str(k).lower(): str(v).lower() for k, v in pm.items()}
    except Exception:
        return fb
    return fb


def business_line_ceilings_from_config(
    config: Optional[Dict[str, Any]],
    fallback: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """业务线封顶表（活读 ``inbox.auto_draft.business_line_modes``）。

    活读值是 dict 即生效（显式 ``{}`` = 关闭业务线封顶）；否则用 fallback
    快照；连 fallback 都没有（API/CLI 场景）→ 内置默认 translation→review
    ——与 ``setup_auto_draft`` 构造 AutoDraftConfig 的缺省一致。
    """
    try:
        raw = ((config or {}).get("inbox") or {}).get(
            "auto_draft", {}).get("business_line_modes")
        if isinstance(raw, dict):
            return {str(k).lower(): str(v).lower() for k, v in raw.items()}
    except Exception:
        pass
    if fallback is not None:
        return dict(fallback)
    return dict(_BUILTIN_BUSINESS_LINE_CEILINGS)


def compute_mode_caps(
    *,
    platform: str,
    account_id: str,
    config: Optional[Dict[str, Any]],
    now: Optional[float] = None,
    platform_ceilings_fallback: Optional[Dict[str, str]] = None,
    business_line_ceilings_fallback: Optional[Dict[str, str]] = None,
    business_line: Optional[str] = None,
    connected_at: Optional[float] = None,
) -> List[ModeCap]:
    """求本会话适用的全部档位封顶（纯读，逐层 fail-open）。

    顺序与 B 线旧内联链一致：平台 → 业务线 → 冷启动预热。
    ``business_line`` / ``connected_at`` 可显式注入（why_no_reply CLI 用只读
    SQLite 自取，避免进程外调 ``get_account_registry`` 触碰生产库写路径）；
    None ＝服务内路径，走各自带缓存的解析函数。
    """
    now = time.time() if now is None else float(now)
    plat = str(platform or "").lower()
    acct = str(account_id or "")
    caps: List[ModeCap] = []

    # ── ① 平台封顶（如 {messenger: review}）─────────────────────────────
    try:
        ceil = platform_ceilings_from_config(
            config, platform_ceilings_fallback).get(plat)
        if ceil:
            caps.append(ModeCap("platform", str(ceil), detail=plat))
    except Exception:
        pass

    # ── ② 账号业务线封顶（默认 translation→review；无标签不封顶）─────────
    try:
        bl = business_line
        if bl is None:
            from src.integrations.account_registry import cached_business_line
            bl = cached_business_line(plat, acct)
        bl = str(bl or "").strip().lower()
        if bl:
            bl_ceil = business_line_ceilings_from_config(
                config, business_line_ceilings_fallback).get(bl)
            if bl_ceil:
                caps.append(ModeCap("business_line", str(bl_ceil), detail=bl))
    except Exception:
        pass

    # ── ③ 冷启动预热封顶（新号 < 72h → review；判不出**不**封顶）─────────
    try:
        from src.inbox.outbound_gate import (
            automation_ceiling,
            resolve_cold_start_cfg,
        )
        cs_cfg = resolve_cold_start_cfg(config)
        conn_at = connected_at
        if conn_at is None:
            from src.inbox.account_connection import (
                resolve_account_connected_at,
            )
            conn_at = resolve_account_connected_at(plat or "telegram", acct)
        conn_at = float(conn_at or 0.0)
        warm_ceil = automation_ceiling(conn_at, now, cs_cfg)
        if warm_ceil:
            hours = float(cs_cfg.get("warmup_hours", 72.0) or 0.0)
            age_h = max(0.0, (now - conn_at) / 3600.0)
            caps.append(ModeCap(
                "warmup", str(warm_ceil),
                detail=f"age_h={age_h:.1f}",
                until_ts=conn_at + hours * 3600.0,
            ))
    except Exception:
        pass

    return caps


def apply_mode_caps(
    base_mode: str, caps: List[ModeCap],
) -> Tuple[str, List[ModeCap]]:
    """按序折叠封顶（只降不升），返回 ``(有效档位, 真正生效的封顶列表)``。

    「生效」＝该层折叠后档位确实变了：两层都要求 review 时只记第一层——
    前端胶囊/日志按此展示主因；要看全部适用层用 ``compute_mode_caps`` 原表。
    """
    mode = str(base_mode or "review")
    applied: List[ModeCap] = []
    for cap in caps or []:
        try:
            from src.inbox.drafts import cap_automation_mode
            new_mode = cap_automation_mode(mode, cap.ceiling)
        except Exception:
            # drafts 不可用（极早期启动/裁剪部署）：保守地只处理 review 封顶
            new_mode = ("review" if (cap.ceiling == "review"
                                     and mode == "auto_ai") else mode)
        if new_mode != mode:
            applied.append(cap)
            mode = new_mode
    return mode, applied


def effective_automation(
    store: Any,
    config: Optional[Dict[str, Any]],
    *,
    conversation_id: str,
    platform: str,
    account_id: str,
    now: Optional[float] = None,
    base_mode: Optional[str] = None,
    business_line: Optional[str] = None,
    connected_at: Optional[float] = None,
) -> Dict[str, Any]:
    """只读求「基础档位 + 封顶明细 + 有效档位」（API / CLI / 巡检入口）。

    ``base_mode`` 显式给定时跳过 store 查询（B 线已 bootstrap 过的场景）。
    返回结构（JSON 安全）::

        {
          "mode": "auto_ai",          # 基础档位（UI 下拉框那个值）
          "source": "explicit|global",
          "effective_mode": "review", # 折叠封顶后的真实执行档位
          "caps": [...],              # 真正生效的封顶（apply 语义）
          "caps_all": [...],          # 本会话适用的全部封顶（排障用）
        }
    """
    explicit: Optional[str] = None
    if base_mode is None and store is not None and conversation_id:
        try:
            explicit = store.get_automation_mode_if_set(conversation_id)
        except Exception:
            explicit = None
    if base_mode is not None:
        ui_mode = str(base_mode)
        source = "given"
    elif explicit is not None:
        ui_mode = str(explicit)
        source = "explicit"
    else:
        ui_mode = global_automation_mode_from_config(config)
        source = "global"
    caps_all = compute_mode_caps(
        platform=platform, account_id=account_id, config=config, now=now,
        business_line=business_line, connected_at=connected_at)
    eff, applied = apply_mode_caps(ui_mode, caps_all)
    return {
        "mode": ui_mode,
        "source": source,
        "effective_mode": eff,
        "caps": serialize_caps(applied),
        "caps_all": serialize_caps(caps_all),
    }


__all__ = [
    "ModeCap",
    "serialize_caps",
    "platform_ceilings_from_config",
    "business_line_ceilings_from_config",
    "compute_mode_caps",
    "apply_mode_caps",
    "effective_automation",
]
