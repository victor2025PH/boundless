# Bot -> API HTTP client
import httpx
from config import settings

# For better error messages
class InviteLinkError(Exception):
    pass
class InviteLinkConnectionError(InviteLinkError):
    pass

API_BASE = settings.api_base_url.rstrip("/")


async def check(
    user_id: int | None = None,
    username: str | None = None,
    wallet: str | None = None,
    querier_tg_id: int | None = None,
) -> dict:
    params = {}
    if user_id is not None:
        params["user_id"] = user_id
    if username:
        params["username"] = username
    if wallet:
        params["wallet"] = wallet
    if querier_tg_id is not None:
        params["querier_tg_id"] = querier_tg_id
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{API_BASE}/v1/check", params=params)
        r.raise_for_status()
        return r.json()


async def report_breakdown(
    user_id: int | None = None,
    username: str | None = None,
    wallet: str | None = None,
) -> dict:
    """V2.0 风险画像：含 risk_factor_breakdown、report_by_category、last_7d/30d_reports。"""
    params = {}
    if user_id is not None:
        params["user_id"] = user_id
    if username:
        params["username"] = username
    if wallet:
        params["wallet"] = wallet
    if not params:
        return {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{API_BASE}/v2/report/breakdown", params=params)
        r.raise_for_status()
        return r.json()


async def report_register(target_tg_id: int, content_hash: str) -> str:
    """阶段 A：注册报告快照，返回 8 位验真码。"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{API_BASE}/v2/report/register",
            json={"target_tg_id": target_tg_id, "content_hash": content_hash},
        )
        r.raise_for_status()
        data = r.json()
        return data.get("report_hash", "")


async def report_verify(report_hash: str, content_hash: str | None = None) -> dict:
    """阶段 A：验真码查询。第四阶段：可选 content_hash 用于校验正文是否与存档一致。返回 valid, created_at, content_match, message。"""
    params: dict = {"hash": report_hash.strip()}
    if content_hash and content_hash.strip():
        params["content_hash"] = content_hash.strip().lower()[:64]
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{API_BASE}/v2/report/verify", params=params)
        r.raise_for_status()
        return r.json()


async def privacy_get(tg_id: int) -> dict:
    """阶段 B：获取当前用户隐私设置。"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{API_BASE}/v1/user/privacy", params={"tg_id": tg_id})
        r.raise_for_status()
        return r.json()


async def privacy_set(
    tg_id: int,
    show_register_year: bool | None = None,
    show_premium: bool | None = None,
    show_tx_30d: bool | None = None,
) -> dict:
    """阶段 B：更新隐私设置（仅传要修改的键）。"""
    payload = {"tg_id": tg_id}
    if show_register_year is not None:
        payload["show_register_year"] = show_register_year
    if show_premium is not None:
        payload["show_premium"] = show_premium
    if show_tx_30d is not None:
        payload["show_tx_30d"] = show_tx_30d
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.patch(f"{API_BASE}/v1/user/privacy", json=payload)
        r.raise_for_status()
        return r.json()


async def mycredit(tg_id: int) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{API_BASE}/v1/mycredit", params={"tg_id": tg_id})
        r.raise_for_status()
        return r.json()


async def report(
    reporter_tg_id: int,
    target_tg_id: int,
    reason: str,
    category: str | None = None,
    tx_hash: str | None = None,
    screenshot_url: str | None = None,
    description: str | None = None,
) -> dict:
    payload = {
        "reporter_tg_id": reporter_tg_id,
        "target_tg_id": target_tg_id,
        "reason": reason,
        "tx_hash": tx_hash,
        "screenshot_url": screenshot_url,
        "description": description,
    }
    if category is not None:
        payload["category"] = category
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(f"{API_BASE}/v1/report", json=payload)
        r.raise_for_status()
        return r.json()


async def transaction_confirm(
    initiator_tg_id: int,
    counterparty_tg_id: int,
    amount: str,
    currency: str = "USDT",
    memo: str | None = None,
) -> dict:
    """V2.0 发起交易确认请求。"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{API_BASE}/v2/transaction/confirm",
            json={
                "initiator_tg_id": initiator_tg_id,
                "counterparty_tg_id": counterparty_tg_id,
                "amount": amount,
                "currency": currency,
                "memo": memo,
            },
        )
        r.raise_for_status()
        return r.json()


async def transaction_respond(transaction_id: int, responder_tg_id: int, accept: bool) -> dict:
    """V2.0 对方同意/拒绝交易确认。"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{API_BASE}/v2/transaction/respond",
            json={
                "transaction_id": transaction_id,
                "responder_tg_id": responder_tg_id,
                "accept": accept,
            },
        )
        r.raise_for_status()
        return r.json()


async def transaction_pending(counterparty_tg_id: int) -> list[dict]:
    """V2.0 待我确认的交易列表。"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{API_BASE}/v2/transaction/pending",
            params={"counterparty_tg_id": counterparty_tg_id},
        )
        r.raise_for_status()
        data = r.json()
        return data.get("items", [])


async def admin_stats() -> dict:
    headers = {}
    if settings.api_internal_key:
        headers["Authorization"] = f"Bearer {settings.api_internal_key}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{API_BASE}/v1/admin/stats", headers=headers)
        r.raise_for_status()
        return r.json()


async def invite_create(querier_tg_id: int) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.post(f"{API_BASE}/v1/invite/create", params={"querier_tg_id": querier_tg_id})
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise InviteLinkConnectionError("API unreachable") from e
        r.raise_for_status()
        return r.json()


async def invite_consume(token: str) -> dict | None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{API_BASE}/v1/invite/consume", params={"token": token})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def share_create(tg_id: int) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(f"{API_BASE}/v1/share/create", params={"tg_id": tg_id})
        r.raise_for_status()
        return r.json()


async def share_view(token: str) -> dict | None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{API_BASE}/v1/share/view", params={"token": token})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def recent_queries(tg_id: int, limit: int = 10) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{API_BASE}/v1/my-recent-queries", params={"tg_id": tg_id, "limit": limit})
        r.raise_for_status()
        return r.json()


async def my_feedback(tg_id: int) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{API_BASE}/v1/my-feedback", params={"tg_id": tg_id})
        r.raise_for_status()
        return r.json()


async def activity_log(tg_id: int, event_type: str) -> None:
    """Log activity event (e.g. invite_clicked when user opens invite link)."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.post(f"{API_BASE}/v1/activity/log", params={"tg_id": tg_id, "event_type": event_type})
        if r.status_code != 200:
            return
        r.raise_for_status()


def _admin_headers() -> dict:
    if not getattr(settings, "api_internal_key", None):
        return {}
    return {"Authorization": f"Bearer {settings.api_internal_key}"}


async def alert_subscribe(
    tg_id: int,
    target_tg_id: int,
    lang: str | None = None,
    source: str | None = None,
    alert_type: str = "risk_change",
) -> dict:
    """Subscribe to alerts for target. alert_type: risk_change | online_offline."""
    params = {"tg_id": tg_id, "target_tg_id": target_tg_id, "type": alert_type}
    if lang:
        params["lang"] = lang
    if source:
        params["source"] = source[:32]
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(f"{API_BASE}/v1/alert/subscribe", params=params)
        if r.status_code == 409:
            return {"status": "already"}
        if r.status_code == 400:
            return {"status": "limit" if "limit" in (r.text or "").lower() else "self"}
        r.raise_for_status()
        return r.json() or {"status": "subscribed"}


async def alert_unsubscribe(tg_id: int, target_tg_id: int, alert_type: str | None = None) -> bool:
    """Unsubscribe. If alert_type set, only that type; else all for this target."""
    params = {"tg_id": tg_id, "target_tg_id": target_tg_id}
    if alert_type:
        params["type"] = alert_type
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.delete(f"{API_BASE}/v1/alert/subscribe", params=params)
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True


async def alert_subscriptions(tg_id: int) -> dict:
    """List my subscriptions."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{API_BASE}/v1/alert/subscriptions", params={"tg_id": tg_id})
        r.raise_for_status()
        return r.json()


async def alert_pending(limit: int = 50) -> dict:
    """Fetch pending alerts (Bot only, uses admin key)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{API_BASE}/v1/alert/pending", params={"limit": limit}, headers=_admin_headers())
        r.raise_for_status()
        return r.json()


async def monitor_report_event(
    target_tg_id: int,
    event_type: str,
    reporter_tg_id: int | None = None,
) -> dict:
    """Report target online/offline (crowdsource). Uses admin key."""
    payload = {"target_tg_id": target_tg_id, "event_type": event_type}
    if reporter_tg_id is not None:
        payload["reporter_tg_id"] = reporter_tg_id
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(f"{API_BASE}/v1/monitor/event", json=payload, headers=_admin_headers())
        r.raise_for_status()
        return r.json() or {}


async def alert_mark_sent(pending_id: int) -> bool:
    """Mark alert as sent (Bot only)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.patch(f"{API_BASE}/v1/alert/pending/{pending_id}", json={"sent": True}, headers=_admin_headers())
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True


async def notify_on_checked_get(tg_id: int) -> bool:
    """Get 被查通知 switch state."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{API_BASE}/v1/user/notify_on_checked", params={"tg_id": tg_id})
        r.raise_for_status()
        return (r.json() or {}).get("notify_on_checked", True)


async def notify_on_checked_set(tg_id: int, value: bool) -> None:
    """Set 被查通知 switch (Bot settings)."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.patch(f"{API_BASE}/v1/user/notify_on_checked", json={"tg_id": tg_id, "notify_on_checked": value})
        r.raise_for_status()


async def notify_checked_pending(limit: int = 50) -> dict:
    """Fetch pending 被查通知 (Bot only, admin key)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{API_BASE}/v2/notify_checked/pending", params={"limit": limit}, headers=_admin_headers())
        r.raise_for_status()
        return r.json()


async def notify_checked_mark_sent(pending_id: int) -> bool:
    """Mark 被查通知 as sent (Bot only)."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.patch(f"{API_BASE}/v2/notify_checked/pending/{pending_id}", headers=_admin_headers())
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True


async def sharecard_get(tg_id: int) -> dict:
    """V2.0 信用卡片数据（用于 /sharecard 或分享按钮）。"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{API_BASE}/v2/report/sharecard", params={"user_id": tg_id})
        r.raise_for_status()
        return r.json()


# ---------- 绑定收款地址与流水 ----------
async def wallets_list(tg_id: int) -> list[dict]:
    """当前用户绑定的收款地址列表。"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{API_BASE}/v2/user/me/wallets", params={"user_id": tg_id})
        r.raise_for_status()
        data = r.json()
        return data.get("items") or []


async def wallets_bind(tg_id: int, chain: str, address: str, label: str | None = None, show_flow_to_public: bool = False) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{API_BASE}/v2/user/me/wallets",
            json={"user_id": tg_id, "chain": chain, "address": address.strip(), "label": label, "show_flow_to_public": show_flow_to_public},
        )
        r.raise_for_status()
        return r.json()


async def wallets_unbind(tg_id: int, wallet_id: int) -> bool:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.delete(f"{API_BASE}/v2/user/me/wallets/{wallet_id}", params={"user_id": tg_id})
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True


async def wallets_set_show_flow(tg_id: int, wallet_id: int, show_flow_to_public: bool) -> bool:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.patch(
            f"{API_BASE}/v2/user/me/wallets/{wallet_id}",
            json={"user_id": tg_id, "show_flow_to_public": show_flow_to_public},
        )
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True


def _auth_headers(tg_id: int | None) -> dict[str, str]:
    if tg_id is None:
        return {}
    key = getattr(__import__("config", fromlist=["settings"]).settings, "api_internal_key", None) or ""
    h = {"X-Telegram-User-Id": str(tg_id)}
    if key:
        h["X-Internal-Key"] = key
    return h


async def my_flow_get(tg_id: int, limit: int = 30) -> tuple[list[dict], str | None]:
    """我的流水。返回 (items, error)。"""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{API_BASE}/v2/user/me/wallets/flow",
            params={"user_id": tg_id, "limit": limit},
            headers=_auth_headers(tg_id),
        )
        r.raise_for_status()
        data = r.json()
        return data.get("items") or [], data.get("error")


async def report_flow_get(target_tg_id: int, limit: int = 30, caller_tg_id: int | None = None) -> tuple[list[dict], str | None]:
    """对方流水（仅当对方开放时有效）。caller_tg_id 用于限流。返回 (items, error)。"""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{API_BASE}/v2/report/flow",
            params={"target_user_id": target_tg_id, "limit": limit},
            headers=_auth_headers(caller_tg_id),
        )
        r.raise_for_status()
        data = r.json()
        return data.get("items") or [], data.get("error")


async def sync_flow_post(tg_id: int, limit_per_wallet: int = 50) -> int:
    """同步流水到本地快照。返回新写入条数 written。"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{API_BASE}/v2/user/me/wallets/sync-flow",
            params={"user_id": tg_id, "limit_per_wallet": limit_per_wallet},
        )
        r.raise_for_status()
        data = r.json()
        return data.get("written", 0)


async def flow_from_db_get(tg_id: int, limit: int = 20, offset: int = 0) -> list[dict]:
    """从本地快照分页读取流水。"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{API_BASE}/v2/user/me/wallets/flow/from_db",
            params={"user_id": tg_id, "limit": limit, "offset": offset},
        )
        r.raise_for_status()
        data = r.json()
        return data.get("items") or []


async def flow_stats_30d_get(tg_id: int) -> dict:
    """近 30 天我的流水统计。"""
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(
            f"{API_BASE}/v2/user/me/wallets/flow/stats_30d",
            params={"user_id": tg_id},
        )
        r.raise_for_status()
        return r.json() or {}


async def report_flow_from_db_get(target_tg_id: int, limit: int = 20, offset: int = 0) -> list[dict]:
    """从快照分页读取对方流水（仅当对方开放流水时有效）。"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{API_BASE}/v2/report/flow/from_db",
            params={"target_user_id": target_tg_id, "limit": limit, "offset": offset},
        )
        r.raise_for_status()
        data = r.json()
        return data.get("items") or []


async def can_view_flow_get(target_tg_id: int) -> bool:
    """被查人是否开放了流水（用于仅当开放时显示「查看对方流水」按钮）。"""
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{API_BASE}/v2/report/can_view_flow", params={"target_user_id": target_tg_id})
        r.raise_for_status()
        data = r.json()
        return data.get("can_view_flow", False)


async def group_scan_post(group_id: int, requester_tg_id: int) -> dict:
    """V2.0 群扫描（每群每日 1 次，仅管理员）。"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{API_BASE}/v2/group/scan",
            json={"group_id": group_id, "requester_tg_id": requester_tg_id},
        )
        r.raise_for_status()
        return r.json()
