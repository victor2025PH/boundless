# -*- coding: utf-8 -*-
"""实施93：CTA 追踪短链——「引导转化」业务模型的事实源。

本部署不做聊天内报价成交，转化＝把客户引到网站/落地页/APP。站外发生的事
聊天侧看不见，所以每次「引导」发一张可归因的凭证：

    运营配转化目标（cta_targets）→ 按 会话×目标 铸 token 短链
    {public_base}/r/{token} → 客户点击 → 302 到目标（可附 utm）
    → 点击落账 → 旅程 guided→converted 自动推进 → 旧阶段促发链让路 → 事件

设计要点：
- **public_base 必配**（`inbox.cta.public_base`）：短链必须公网可点；未配置时
  铸链接口如实拒绝（绝不把内网地址发给客户）；
- token 8 位 urlsafe 随机（不可枚举）；同会话同目标 24h 复用同 token（防重复
  铸造刷库，也让「点了没点」有稳定归因对象）；
- 点击是**轻转化**：推进阶段、不写 deal_events（点击不是钱；深转化 webhook /
  人工标记才进营收台账）；
- 步骤级 CTA（`step.cta`）：链话术步声明目标 id（或 ``@primary``＝第一个启用
  目标），自动步拟稿成功后**确定性追加**短链——刻意不用「LLM 保留占位符」方案
  （小模型丢占位符=死链风险；追加是可预期的）。铸链失败 → 整步降级提醒坐席
  （fail-closed：发不带链的引导话术=白说，发死链更糟）。
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any, Dict, Optional
from urllib.parse import quote as _urlquote

logger = logging.getLogger(__name__)

# 同会话同目标的 token 复用窗（秒）
REUSE_WINDOW_SEC = 24 * 3600
# 铸 token 撞库重试次数（8 位 urlsafe ≈ 2^48 空间，撞库纯理论）
_MINT_RETRIES = 3


def resolve_cta_cfg(cfg_root: Any) -> Dict[str, Any]:
    """``inbox.cta`` 配置段。public_base 空＝功能不可用（诚实降级）。
    webhook_secret＝深转化回传（/api/cta/convert）鉴权；空＝回传端点拒绝。"""
    out = {"public_base": "", "utm_source": "chat", "webhook_secret": ""}
    try:
        if not isinstance(cfg_root, dict):
            return out
        c = ((cfg_root.get("inbox") or {}).get("cta") or {})
        if not isinstance(c, dict):
            return out
        out["public_base"] = str(c.get("public_base") or "").strip().rstrip("/")
        out["utm_source"] = str(c.get("utm_source") or "chat").strip() or "chat"
        out["webhook_secret"] = str(c.get("webhook_secret") or "").strip()
    except Exception:
        pass
    return out


def short_url(public_base: str, token: str) -> str:
    return f"{public_base.rstrip('/')}/r/{token}"


def mint_link(
    store: Any, cfg_root: Any, conversation_id: str, target_id: str,
    *, now: Optional[float] = None, mark_guided: bool = True,
) -> Dict[str, Any]:
    """铸造（或复用）会话×目标的追踪短链。

    返回 {ok, url, token, reused} 或 {ok:False, error:
    no_public_base|target_not_found|target_disabled|mint_failed}。
    ``mark_guided``：铸链即把旅程推进到 guided（quoting 位）——铸链的意图就是
    引导；阶段是前进棘轮，重复铸链/复用零副作用。
    """
    n = float(now if now is not None else time.time())
    cfg = resolve_cta_cfg(cfg_root)
    if not cfg["public_base"]:
        return {"ok": False, "error": "no_public_base"}
    cid = str(conversation_id or "").strip()
    tid = str(target_id or "").strip()
    if not cid or not tid:
        return {"ok": False, "error": "target_not_found"}
    target = store.get_cta_target(tid)
    if not target:
        return {"ok": False, "error": "target_not_found"}
    if not int(target.get("enabled") or 0):
        return {"ok": False, "error": "target_disabled"}
    reused = store.find_recent_cta_link(
        cid, tid, since_ts=n - REUSE_WINDOW_SEC)
    if reused:
        token = str(reused.get("token"))
        out = {"ok": True, "url": short_url(cfg["public_base"], token),
               "token": token, "reused": True}
    else:
        token = ""
        for _ in range(_MINT_RETRIES):
            cand = secrets.token_urlsafe(6)          # 8 chars urlsafe
            if store.insert_cta_link(cand, cid, tid, ts=n):
                token = cand
                break
        if not token:
            return {"ok": False, "error": "mint_failed"}
        out = {"ok": True, "url": short_url(cfg["public_base"], token),
               "token": token, "reused": False}
    if mark_guided:
        try:
            from src.inbox.journey_stage import mark_guided_by_cta
            mark_guided_by_cta(store, cid, now=n)
        except Exception:
            logger.debug("mark_guided failed（已忽略）", exc_info=True)
    return out


def resolve_step_target_id(store: Any, raw: str) -> str:
    """步骤 ``cta`` 字段 → 真实 target_id。``@primary``＝第一个启用目标。
    返回空串＝解析不到（调用方降级）。"""
    v = str(raw or "").strip()
    if not v:
        return ""
    if v == "@primary":
        try:
            rows = store.list_cta_targets(enabled_only=True)
        except Exception:
            rows = []
        return str(rows[0]["target_id"]) if rows else ""
    t = store.get_cta_target(v)
    return v if (t and int(t.get("enabled") or 0)) else ""


def build_redirect(target: Dict[str, Any], token: str,
                   utm_source: str) -> str:
    """目标 URL + 可选 utm（utm_source=chat&utm_content={token}）。"""
    url = str(target.get("url") or "")
    if not url:
        return ""
    if not int(target.get("utm", 1) or 0):
        return url
    sep = "&" if "?" in url else "?"
    return (f"{url}{sep}utm_source={_urlquote(utm_source)}"
            f"&utm_medium=im&utm_content={_urlquote(token)}")


def handle_click(
    store: Any, cfg_root: Any, token: str, *, now: Optional[float] = None,
) -> Optional[str]:
    """公开路由 /r/{token} 的处理核心。返回 302 目标 URL；None=404。

    首点副作用（均 best-effort，绝不影响跳转本身）：
    - 旅程 guided→converted（deal 位，src=cta；棘轮防降级）；
    - 取消该会话上因 guided 阶段自动挂的促发链（事实变了节奏让路）；
    - EventBus ``cta_clicked``（坐席可感知「客户点了链接」）。
    """
    n = float(now if now is not None else time.time())
    row = store.record_cta_click(token, ts=n)
    if not row:
        return None
    target = store.get_cta_target(str(row.get("target_id") or ""))
    if not target:
        return None
    cfg = resolve_cta_cfg(cfg_root)
    url = build_redirect(target, str(token), cfg["utm_source"])
    first_click = abs(float(row.get("first_click_ts") or 0) - n) < 0.001
    if first_click:
        cid = str(row.get("conversation_id") or "")
        try:
            from src.inbox.journey_stage import record_conversion
            record_conversion(store, cid, source="cta_click", now=n)
        except Exception:
            logger.debug("click→conversion 推进失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("cta_clicked", {
                "conversation_id": cid,
                "target_id": row.get("target_id"),
                "target_name": target.get("name") or "",
                "token": str(token),
                "ts": n,
            })
        except Exception:
            logger.debug("cta_clicked 事件发布失败", exc_info=True)
    return url or None
