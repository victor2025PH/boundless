# FastAPI app: v1 check, mycredit, report, admin/stats
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, Query, HTTPException, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, engine, Base
import models  # noqa: F401 — register tables with Base.metadata
from api.schemas import (
    CheckResponse,
    ReportBreakdownResponse,
    MyCreditResponse,
    ReportCreate,
    ReportResponse,
    AdminStatsResponse,
    AdminFeedbackListResponse,
    AdminFeedbackItem,
    FeedbackUpdateBody,
    FeedbackUpdateResponse,
    BlacklistAddBody,
    BlacklistAddResponse,
    InviteCreateResponse,
    InviteConsumeResponse,
    ShareCreateResponse,
    RecentQueriesResponse,
    MyFeedbackResponse,
    AlertSubscribeResponse,
    AlertSubscriptionsResponse,
    PendingAlertsResponse,
    PendingAlertMarkSentBody,
    PendingAlertMarkSentResponse,
    MonitorEventBody,
    TransactionConfirmBody,
    TransactionConfirmResponse,
    TransactionRespondBody,
    TransactionRespondResponse,
    PendingTransactionItem,
    PendingTransactionsResponse,
    NotifyOnCheckedResponse,
    NotifyOnCheckedUpdateBody,
    PendingNotifyCheckedItem,
    PendingNotifyCheckedResponse,
    SharecardResponse,
    GroupScanBody,
    GroupScanResponse,
    AdminReportersResponse,
    AdminReporterItem,
    AdminReportsResponse,
    AdminReportItem,
    ReportUpdateBody,
    BatchReportUpdateBody,
    BatchReportUpdateResponse,
    AdminTransactionsResponse,
    AdminTransactionItem,
    AdminRiskTrendResponse,
    AdminRiskTrendDay,
    ReportRegisterBody,
    ReportRegisterResponse,
    ReportVerifyResponse,
    PrivacySettingsResponse,
    PrivacySettingsUpdateBody,
    BoundWalletItem,
    BoundWalletsResponse,
    BindWalletBody,
    BindWalletResponse,
    WalletShowFlowBody,
    FlowItem,
    FlowResponse,
    FlowStatsResponse,
    CanViewFlowResponse,
    TimelineResponse,
)
from services import (
    resolve_username_to_tg_id,
    resolve_wallet_to_tg_id,
    check_by_tg_id,
    get_report_breakdown,
    report_create,
    admin_stats,
    blacklist_add,
    log_query_anon,
    check_log_insert,
    get_mycredit_queried_stats,
    get_recent_queries,
    get_my_feedback_list,
    get_feedback_list_admin,
    update_feedback_status,
    activity_log as activity_log_svc,
    invite_create,
    invite_consume,
    share_create,
    share_consume,
    alert_subscribe,
    alert_unsubscribe,
    alert_list_subscriptions,
    get_pending_alerts,
    mark_alert_sent,
    monitor_report_status,
    transaction_confirm_create,
    transaction_respond,
    get_pending_transactions_for_counterparty,
    TransactionRejectError,
    get_notify_on_checked,
    set_notify_on_checked,
    get_pending_notify_checked,
    mark_notify_checked_sent,
    get_sharecard_data,
    group_scan,
    get_admin_reporters,
    get_admin_transactions,
    get_admin_risk_trend,
    get_admin_reports,
    update_report_status,
    batch_update_report_status,
    register_report_snapshot,
    get_report_snapshot_by_hash,
    get_privacy_settings,
    set_privacy_settings,
    list_bound_wallets,
    bind_wallet,
    unbind_wallet,
    update_wallet_show_flow,
    set_wallet_verified_at_if_empty,
    can_view_flow,
    get_flow_for_target,
    sync_flow_snapshots,
    get_flow_snapshots,
    get_flow_stats_30d,
)
from config import settings

# 流水接口限流：同一 caller 60 秒内仅允许 1 次
_flow_rate: dict[str, float] = {}
FLOW_RATE_WINDOW = 60.0

# P2: /check 与 /report 按 caller 每分钟限流（内存，无 Redis）
_check_rate: dict[str, tuple[int, float]] = {}  # key -> (count, window_start_ts)
_report_rate: dict[str, tuple[int, float]] = {}
RATE_WINDOW = 60.0
RATE_CLEANUP_MAX = 50000


def _rate_limit_cleanup(d: dict, window: float) -> None:
    import time
    if len(d) <= RATE_CLEANUP_MAX:
        return
    now = time.time()
    cutoff = now - window * 2
    for k in list(d.keys()):
        _, start = d[k]
        if start < cutoff:
            del d[k]


async def check_rate_limit(
    user_id: int | None = Query(None),
    username: str | None = Query(None),
    wallet: str | None = Query(None),
    querier_tg_id: int | None = Query(None),
) -> None:
    """P2: Limit /check per caller per minute. Caller = querier_tg_id if set, else user_id (self-check)."""
    import time
    caller = querier_tg_id if querier_tg_id is not None else user_id
    if caller is None and username:
        return  # will be resolved later; no limit by param
    if caller is None and wallet:
        return
    if caller is None:
        return
    key = f"check:{caller}"
    limit = getattr(settings, "rate_limit_check_per_min", 10) or 10
    now = time.time()
    if key in _check_rate:
        cnt, start = _check_rate[key]
        if now - start >= RATE_WINDOW:
            _check_rate[key] = (1, now)
        else:
            cnt += 1
            if cnt > limit:
                _rate_limit_cleanup(_check_rate, RATE_WINDOW)
                remaining = max(1, int(RATE_WINDOW - (now - start)))
                raise HTTPException(
                    status_code=429,
                    detail="rate_limit_check",
                    headers={"Retry-After": str(remaining)},
                )
            _check_rate[key] = (cnt, start)
    else:
        _check_rate[key] = (1, now)
    _rate_limit_cleanup(_check_rate, RATE_WINDOW)


async def report_rate_limit(body: "ReportCreate") -> None:
    """P2: Limit /report per reporter_tg_id per minute. Called as body dependency."""
    import time
    key = f"report:{body.reporter_tg_id}"
    limit = getattr(settings, "rate_limit_report_per_min", 3) or 3
    now = time.time()
    if key in _report_rate:
        cnt, start = _report_rate[key]
        if now - start >= RATE_WINDOW:
            _report_rate[key] = (1, now)
        else:
            cnt += 1
            if cnt > limit:
                _rate_limit_cleanup(_report_rate, RATE_WINDOW)
                remaining = max(1, int(RATE_WINDOW - (now - start)))
                raise HTTPException(
                    status_code=429,
                    detail="rate_limit_report",
                    headers={"Retry-After": str(remaining)},
                )
            _report_rate[key] = (cnt, start)
    else:
        _report_rate[key] = (1, now)
    _rate_limit_cleanup(_report_rate, RATE_WINDOW)

# 同步流水节流：按 user_id 在配置的 cooldown 秒内仅允许 1 次
_sync_flow_last: dict[int, float] = {}


def _sync_flow_rate_limit_cleanup() -> None:
    import time
    cooldown = getattr(settings, "flow_sync_cooldown_seconds", 300) or 300
    if len(_sync_flow_last) <= 20000:
        return
    now = time.time()
    cutoff = now - cooldown * 2
    for uid in list(_sync_flow_last.keys()):
        if _sync_flow_last[uid] < cutoff:
            del _sync_flow_last[uid]


async def sync_flow_rate_limit(
    user_id: int = Query(..., description="Telegram user ID (current user)"),
) -> int:
    """sync-flow 按用户节流：配置的 cooldown 秒内每用户仅允许 1 次；超限返回 429 与 Retry-After 头。"""
    import time
    now = time.time()
    cooldown = getattr(settings, "flow_sync_cooldown_seconds", 300) or 300
    if user_id in _sync_flow_last and (now - _sync_flow_last[user_id]) < cooldown:
        remaining = int(cooldown - (now - _sync_flow_last[user_id]))
        raise HTTPException(
            status_code=429,
            detail="sync_flow_rate_limit",
            headers={"Retry-After": str(max(1, remaining))},
        )
    _sync_flow_rate_limit_cleanup()
    return user_id


def _flow_rate_limit_key(request: Request, x_telegram_user_id: int | None) -> str:
    if x_telegram_user_id is not None and x_telegram_user_id != 0:
        return f"tg_{x_telegram_user_id}"
    return f"ip_{getattr(request.client, 'host', '')}"


async def flow_rate_limit(
    request: Request,
    x_telegram_user_id: int | None = Header(None, alias="X-Telegram-User-Id"),
) -> None:
    """流水接口限流：每 60 秒每 caller 1 次；超限返回 429。"""
    import time
    key = _flow_rate_limit_key(request, x_telegram_user_id)
    now = time.time()
    if key in _flow_rate and (now - _flow_rate[key]) < FLOW_RATE_WINDOW:
        raise HTTPException(status_code=429, detail="flow_rate_limit")
    _flow_rate[key] = now
    if len(_flow_rate) > 20000:
        cutoff = now - FLOW_RATE_WINDOW * 2
        for k in list(_flow_rate.keys()):
            if _flow_rate[k] < cutoff:
                del _flow_rate[k]


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Log so we can confirm this process has invite routes (avoids 404 from old process)
    invite_paths = [r.path for r in app.routes if hasattr(r, "path") and "invite" in r.path]
    print("API started. Invite routes:", invite_paths)
    yield
    await engine.dispose()


app = FastAPI(title="TrustCheck API", version="1.0", lifespan=lifespan)

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)


@app.exception_handler(TransactionRejectError)
async def transaction_reject_handler(request, exc: TransactionRejectError):
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=400,
        content={"detail": exc.message, "code": exc.code},
    )


def _api_key_or_none(authorization: str | None = Header(None)) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[7:].strip() or None


def _require_admin(authorization: str | None = Header(None)) -> None:
    """P2: In production, require API_INTERNAL_KEY; if unset, return 403."""
    if getattr(settings, "production", False):
        if not (getattr(settings, "api_internal_key", None) or "").strip():
            raise HTTPException(status_code=403, detail="Admin key not configured")
        key = _api_key_or_none(authorization)
        if key != settings.api_internal_key:
            raise HTTPException(status_code=403, detail="Forbidden")
        return
    if not settings.api_internal_key:
        return
    key = _api_key_or_none(authorization)
    if key != settings.api_internal_key:
        raise HTTPException(status_code=403, detail="Forbidden")


@app.get("/v1/check", response_model=CheckResponse)
async def v1_check(
    user_id: int | None = Query(None, description="Telegram user ID"),
    username: str | None = Query(None, description="@username"),
    wallet: str | None = Query(None, description="Wallet address TRC20/TON"),
    querier_tg_id: int | None = Query(None, description="Caller Telegram ID for 谁查过我"),
    _: None = Depends(check_rate_limit),
    session: AsyncSession = Depends(get_db),
):
    tg_id: int | None = None
    tids: list[int] = []
    if user_id is not None:
        tg_id = user_id
    elif username:
        tid = await resolve_username_to_tg_id(username)
        if tid is None:
            return CheckResponse(
                risk="medium",
                score=50,
                tips=[],
                blacklist=False,
                message="Username not found or private. Try with numeric ID if you have it.",
            )
        tg_id = tid
    elif wallet:
        tids = await resolve_wallet_to_tg_id(session, wallet)
        if not tids:
            return CheckResponse(
                risk="medium",
                score=50,
                tips=[],
                blacklist=False,
                tg_id=None,
                message="No record for this wallet. No record does not mean safe.",
            )
        tg_id = tids[0]
    else:
        raise HTTPException(status_code=400, detail="Provide user_id, username, or wallet")

    result = await check_by_tg_id(session, tg_id)
    await log_query_anon(session, tg_id, result["risk"])
    if querier_tg_id is not None and tg_id is not None and querier_tg_id != tg_id:
        await check_log_insert(session, querier_tg_id, tg_id)
    if wallet and len(tids) > 1:
        result["linked_tg_ids"] = tids
    return CheckResponse(**result)


@app.get("/v1/mycredit", response_model=MyCreditResponse)
async def v1_mycredit(
    tg_id: int = Query(..., description="Your Telegram ID"),
    session: AsyncSession = Depends(get_db),
):
    result = await check_by_tg_id(session, tg_id, include_timeline=True)
    stats = await get_mycredit_queried_stats(session, tg_id)
    result.update(stats)
    result["notify_on_checked"] = await get_notify_on_checked(session, tg_id)
    return MyCreditResponse(**result)


@app.post("/v1/report", response_model=ReportResponse)
async def v1_report(
    body: ReportCreate,
    _: None = Depends(report_rate_limit),
    session: AsyncSession = Depends(get_db),
):
    rid = await report_create(
        session,
        reporter_tg_id=body.reporter_tg_id,
        target_tg_id=body.target_tg_id,
        reason=body.reason,
        tx_hash=body.tx_hash,
        screenshot_url=body.screenshot_url,
        description=body.description,
        category=body.category,
    )
    return ReportResponse(status="received", report_id=rid)


@app.get("/v2/report/breakdown", response_model=ReportBreakdownResponse)
async def v2_report_breakdown(
    user_id: int | None = Query(None, description="Telegram user ID"),
    username: str | None = Query(None, description="@username"),
    wallet: str | None = Query(None, description="Wallet address TRC20/TON"),
    session: AsyncSession = Depends(get_db),
):
    """V2.0 风险画像：check 结果 + 风险来源占比 + 举报分类与近 7/30 天统计。"""
    tg_id: int | None = None
    if user_id is not None:
        tg_id = user_id
    elif username:
        tid = await resolve_username_to_tg_id(username)
        if tid is None:
            return ReportBreakdownResponse(
                risk="medium",
                score=50,
                tips=[],
                blacklist=False,
                message="Username not found or private. Try with numeric ID if you have it.",
            )
        tg_id = tid
    elif wallet:
        tids = await resolve_wallet_to_tg_id(session, wallet)
        if not tids:
            return ReportBreakdownResponse(
                risk="medium",
                score=50,
                tips=[],
                blacklist=False,
                tg_id=None,
                message="No record for this wallet. No record does not mean safe.",
            )
        tg_id = tids[0]
    else:
        raise HTTPException(status_code=400, detail="Provide user_id, username, or wallet")

    result = await get_report_breakdown(session, tg_id, include_timeline=False)
    return ReportBreakdownResponse(**result)


@app.post("/v2/report/register", response_model=ReportRegisterResponse)
async def v2_report_register(
    body: ReportRegisterBody,
    session: AsyncSession = Depends(get_db),
):
    """阶段 A：报告生成后 Bot 提交 content_hash，返回 8 位验真码。"""
    report_hash = await register_report_snapshot(
        session, body.target_tg_id, body.content_hash[:64]
    )
    await session.commit()
    return ReportRegisterResponse(report_hash=report_hash)


@app.get("/v2/report/verify", response_model=ReportVerifyResponse)
async def v2_report_verify(
    hash: str = Query(..., description="验真码，如 TC-78A2B9"),
    content_hash: str | None = Query(None, description="可选：报告正文的 sha256 hex，用于校验是否被篡改"),
    session: AsyncSession = Depends(get_db),
):
    """阶段 A：根据验真码查询报告是否真实有效。第四阶段：若传 content_hash 则与存档比对并返回 content_match。"""
    snap = await get_report_snapshot_by_hash(session, hash.strip())
    if not snap:
        return ReportVerifyResponse(
            valid=False,
            message="warning_invalid_or_tampered",
        )
    content_match: bool | None = None
    if content_hash and (ch := (content_hash.strip().lower()[:64])):
        content_match = snap.get("content_hash") == ch
    return ReportVerifyResponse(
        valid=True,
        created_at=snap.get("created_at"),
        target_tg_id=snap.get("target_tg_id"),
        message="valid",
        content_match=content_match,
    )


@app.post("/v2/transaction/confirm", response_model=TransactionConfirmResponse)
async def v2_transaction_confirm(body: TransactionConfirmBody, session: AsyncSession = Depends(get_db)):
    """V2.0 发起交易确认请求：发起方请求对方确认一笔交易；自刷/同对限频会被拒绝。"""
    tid = await transaction_confirm_create(
        session,
        initiator_tg_id=body.initiator_tg_id,
        counterparty_tg_id=body.counterparty_tg_id,
        amount=body.amount,
        currency=body.currency,
        memo=body.memo,
    )
    return TransactionConfirmResponse(transaction_id=tid, status="pending")


@app.post("/v2/transaction/respond", response_model=TransactionRespondResponse)
async def v2_transaction_respond(body: TransactionRespondBody, session: AsyncSession = Depends(get_db)):
    """V2.0 对方对交易确认请求做出响应：同意或拒绝。"""
    result = await transaction_respond(
        session,
        transaction_id=body.transaction_id,
        responder_tg_id=body.responder_tg_id,
        accept=body.accept,
    )
    return TransactionRespondResponse(**result)


@app.get("/v2/transaction/pending", response_model=PendingTransactionsResponse)
async def v2_transaction_pending(
    counterparty_tg_id: int = Query(..., description="Telegram ID of the user who needs to respond"),
    session: AsyncSession = Depends(get_db),
):
    """V2.0 待对方确认的交易列表（供 Bot 展示同意/拒绝按钮）。"""
    items = await get_pending_transactions_for_counterparty(session, counterparty_tg_id)
    return PendingTransactionsResponse(
        items=[PendingTransactionItem(**x) for x in items],
    )


@app.get("/v1/user/notify_on_checked", response_model=NotifyOnCheckedResponse)
async def v1_user_notify_on_checked(
    tg_id: int = Query(..., description="Your Telegram ID"),
    session: AsyncSession = Depends(get_db),
):
    """查询被查通知开关状态。"""
    value = await get_notify_on_checked(session, tg_id)
    return NotifyOnCheckedResponse(notify_on_checked=value)


@app.patch("/v1/user/notify_on_checked", response_model=NotifyOnCheckedResponse)
async def v1_user_notify_on_checked_update(
    body: NotifyOnCheckedUpdateBody,
    session: AsyncSession = Depends(get_db),
):
    """设置被查通知开关（Bot 内设置入口）。"""
    await set_notify_on_checked(session, body.tg_id, body.notify_on_checked)
    return NotifyOnCheckedResponse(notify_on_checked=body.notify_on_checked)


@app.get("/v1/user/privacy", response_model=PrivacySettingsResponse)
async def v1_user_privacy_get(
    tg_id: int = Query(..., description="Your Telegram ID"),
    session: AsyncSession = Depends(get_db),
):
    """阶段 B：查询当前用户隐私设置（他人查你时可见的项）。"""
    settings = await get_privacy_settings(session, tg_id)
    return PrivacySettingsResponse(**settings)


@app.patch("/v1/user/privacy", response_model=PrivacySettingsResponse)
async def v1_user_privacy_update(
    body: PrivacySettingsUpdateBody,
    session: AsyncSession = Depends(get_db),
):
    """阶段 B：更新隐私设置；仅提交要修改的键。"""
    current = await get_privacy_settings(session, body.tg_id)
    updates = {}
    if body.show_register_year is not None:
        updates["show_register_year"] = body.show_register_year
    if body.show_premium is not None:
        updates["show_premium"] = body.show_premium
    if body.show_tx_30d is not None:
        updates["show_tx_30d"] = body.show_tx_30d
    new_settings = {**current, **updates}
    await set_privacy_settings(session, body.tg_id, new_settings)
    await session.commit()
    return PrivacySettingsResponse(**new_settings)


@app.get("/v2/notify_checked/pending", response_model=PendingNotifyCheckedResponse)
async def v2_notify_checked_pending(
    limit: int = Query(50, ge=1, le=100),
    session: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """Bot 拉取待发送的被查通知（需 Admin Key）。"""
    items = await get_pending_notify_checked(session, limit=limit)
    return PendingNotifyCheckedResponse(items=[PendingNotifyCheckedItem(**x) for x in items])


@app.patch("/v2/notify_checked/pending/{pending_id}")
async def v2_notify_checked_mark_sent(
    pending_id: int,
    session: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """Bot 发送被查通知后标记已发送。"""
    ok = await mark_notify_checked_sent(session, pending_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Pending notify not found")
    return {"ok": True}


@app.get("/v2/report/sharecard", response_model=SharecardResponse)
async def v2_report_sharecard(
    user_id: int = Query(..., description="Telegram user ID (e.g. your own for /sharecard)"),
    session: AsyncSession = Depends(get_db),
):
    """V2.0 信用卡片：返回可分享的简要数据，Bot/前端可格式化为文案或图片。"""
    data = await get_sharecard_data(session, user_id)
    return SharecardResponse(**data)


# ---------- 绑定收款地址与流水 ----------
@app.get("/v2/user/me/wallets", response_model=BoundWalletsResponse)
async def v2_user_me_wallets(
    user_id: int = Query(..., description="Telegram user ID (current user)"),
    session: AsyncSession = Depends(get_db),
):
    """当前用户绑定的收款地址列表。"""
    items = await list_bound_wallets(session, user_id)
    return BoundWalletsResponse(items=[BoundWalletItem(**x) for x in items])


@app.post("/v2/user/me/wallets", response_model=BindWalletResponse)
async def v2_user_me_wallets_bind(
    body: BindWalletBody,
    session: AsyncSession = Depends(get_db),
):
    """绑定一条收款地址。"""
    try:
        row = await bind_wallet(
            session,
            body.user_id,
            body.chain.strip().upper(),
            body.address.strip(),
            label=body.label,
            show_flow_to_public=body.show_flow_to_public,
        )
        return BindWalletResponse(**row)
    except ValueError as e:
        code = str(e)
        if code == "unsupported_chain":
            raise HTTPException(status_code=400, detail="Unsupported chain (use TRC20 or TON)")
        if code == "invalid_address":
            raise HTTPException(status_code=400, detail="Invalid address format")
        if code == "limit_reached":
            raise HTTPException(status_code=400, detail="Max bound wallets reached")
        if code == "already_bound":
            raise HTTPException(status_code=409, detail="Address already bound")
        raise HTTPException(status_code=400, detail=code)


@app.delete("/v2/user/me/wallets/{wallet_id}")
async def v2_user_me_wallets_unbind(
    wallet_id: int,
    user_id: int = Query(..., description="Telegram user ID (current user)"),
    session: AsyncSession = Depends(get_db),
):
    """解绑一条收款地址。"""
    ok = await unbind_wallet(session, user_id, wallet_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return {"ok": True}


@app.patch("/v2/user/me/wallets/{wallet_id}")
async def v2_user_me_wallets_show_flow(
    wallet_id: int,
    body: WalletShowFlowBody,
    session: AsyncSession = Depends(get_db),
):
    """更新某条绑定「是否对查阅者展示流水」。"""
    if body.user_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid user_id")
    ok = await update_wallet_show_flow(session, body.user_id, wallet_id, body.show_flow_to_public)
    if not ok:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return {"ok": True}


@app.get("/v2/user/me/wallets/flow", response_model=FlowResponse)
async def v2_user_me_wallets_flow(
    user_id: int = Query(..., description="Telegram user ID (current user, my flow)"),
    limit: int = Query(30, ge=1, le=50),
    session: AsyncSession = Depends(get_db),
    _: None = Depends(flow_rate_limit),
):
    """当前用户「我的流水」：聚合其所有已绑定地址的链上流水（含未开放给别人的地址，仅自己看）。首次拉取到流水时自动设 verified_at。"""
    wallets = await list_bound_wallets(session, user_id)
    if not wallets:
        return FlowResponse(items=[], error="no_wallets")
    from chain_flow import get_flow_for_address
    tron_key = getattr(settings, "trongrid_api_key", "") or ""
    ton_key = getattr(settings, "toncenter_api_key", "") or tron_key
    merged = []
    for w in wallets:
        try:
            rows = await get_flow_for_address(
                w["chain"], w["address"], limit=min(limit, 20),
                api_key=tron_key, ton_api_key=ton_key,
            )
            if rows and w.get("id"):
                await set_wallet_verified_at_if_empty(session, w["id"])
            for r in rows:
                r["chain"] = w["chain"]
                r["address_masked"] = w.get("address_masked") or "***"
            merged.extend(rows)
        except Exception:
            continue
    merged.sort(key=lambda x: (x.get("time") or 0), reverse=True)
    return FlowResponse(items=[FlowItem(**{k: v for k, v in x.items() if k in FlowItem.model_fields}) for x in merged[:limit]])


@app.post("/v2/user/me/wallets/sync-flow")
async def v2_user_me_wallets_sync_flow(
    user_id: int = Depends(sync_flow_rate_limit),
    limit_per_wallet: int = Query(50, ge=1, le=100),
    session: AsyncSession = Depends(get_db),
):
    """按需同步：拉取当前用户所有绑定地址的流水并写入快照表，供分页/离线展示。返回新写入条数。节流：每用户每 flow_sync_cooldown_seconds 秒仅允许 1 次。"""
    import time
    written = await sync_flow_snapshots(session, user_id, limit_per_wallet=limit_per_wallet)
    _sync_flow_last[user_id] = time.time()
    return {"ok": True, "written": written}


@app.get("/v2/user/me/wallets/flow/from_db", response_model=FlowResponse)
async def v2_user_me_wallets_flow_from_db(
    user_id: int = Query(..., description="Telegram user ID (current user)"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db),
):
    """从快照表分页读取流水（需先调用 sync-flow）。"""
    items = await get_flow_snapshots(session, user_id, limit=limit, offset=offset)
    return FlowResponse(items=[FlowItem(**x) for x in items])


@app.get("/v2/user/me/wallets/flow/stats_30d", response_model=FlowStatsResponse)
async def v2_user_me_wallets_flow_stats_30d(
    user_id: int = Query(..., description="Telegram user ID (current user)"),
    session: AsyncSession = Depends(get_db),
):
    """近 30 天「我的流水」统计：按当前用户绑定钱包聚合流入/流出笔数与金额。"""
    stats = await get_flow_stats_30d(session, user_id)
    return FlowStatsResponse(**stats)


@app.get("/v2/report/can_view_flow", response_model=CanViewFlowResponse)
async def v2_report_can_view_flow(
    target_user_id: int = Query(..., description="被查人 Telegram ID"),
    session: AsyncSession = Depends(get_db),
):
    """是否可查看该用户的链上流水（仅当对方已绑定且开启「对查阅者展示流水」为 true）。"""
    ok = await can_view_flow(session, target_user_id)
    return CanViewFlowResponse(can_view_flow=ok)


@app.get("/v2/report/flow/from_db", response_model=FlowResponse)
async def v2_report_flow_from_db(
    target_user_id: int = Query(..., description="被查人 Telegram ID（仅当其开放流水时可查看）"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db),
):
    """查阅方从快照分页查看被查人流水；仅当被查人已绑定且开启「对查阅者展示流水」时返回。数据来自被查人本地快照。"""
    if not await can_view_flow(session, target_user_id):
        return FlowResponse(items=[], error="no_wallets")
    items = await get_flow_snapshots(session, target_user_id, limit=limit, offset=offset)
    return FlowResponse(items=[FlowItem(**x) for x in items])


@app.get("/v2/report/flow", response_model=FlowResponse)
async def v2_report_flow(
    target_user_id: int = Query(..., description="被查人 Telegram ID（仅当其开放流水时可查看）"),
    limit: int = Query(30, ge=1, le=50),
    session: AsyncSession = Depends(get_db),
    _: None = Depends(flow_rate_limit),
):
    """查阅方查看被查人的链上流水；仅当被查人已绑定且开启「对查阅者展示流水」时返回数据。"""
    if not await can_view_flow(session, target_user_id):
        return FlowResponse(items=[], error="no_wallets")
    items, err = await get_flow_for_target(session, target_user_id, limit=limit)
    if err:
        return FlowResponse(items=[], error=err)
    return FlowResponse(items=[FlowItem(**{k: v for k, v in x.items() if k in FlowItem.model_fields}) for x in items])


@app.post("/v2/group/scan", response_model=GroupScanResponse)
async def v2_group_scan(body: GroupScanBody, session: AsyncSession = Depends(get_db)):
    """V2.0 群扫描：每群每日限 1 次；仅群管理员可调用（由 Bot 校验）。"""
    ok, msg = await group_scan(session, body.group_id, body.requester_tg_id)
    if not ok:
        raise HTTPException(status_code=400, detail=msg, headers={"X-Code": "already_scanned"})
    return GroupScanResponse(ok=True, message=msg)


@app.get("/v1/admin/stats", response_model=AdminStatsResponse)
async def v1_admin_stats(
    session: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    s = await admin_stats(session)
    return AdminStatsResponse(**s)


@app.get("/v1/admin/feedback", response_model=AdminFeedbackListResponse)
async def v1_admin_feedback_list(
    status: str | None = Query(None, description="Filter by status: pending, processed, rejected"),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """管理端：申诉列表，可选按状态筛选。"""
    items = await get_feedback_list_admin(session, status=status, limit=limit)
    return AdminFeedbackListResponse(items=items)


@app.patch("/v1/admin/feedback/{feedback_id}", response_model=FeedbackUpdateResponse)
async def v1_admin_feedback_update(
    feedback_id: int,
    body: FeedbackUpdateBody,
    session: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """管理端：更新申诉状态为已处理或已驳回。"""
    ok = await update_feedback_status(session, feedback_id, body.status)
    if not ok:
        raise HTTPException(status_code=404, detail="Feedback not found")
    return FeedbackUpdateResponse(ok=True, message=body.status)


@app.post("/v1/admin/blacklist", response_model=BlacklistAddResponse)
async def v1_admin_blacklist_add(
    body: BlacklistAddBody,
    session: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """管理端：将用户加入黑名单；触发风险变化提醒（blacklist_added）。"""
    added = await blacklist_add(session, body.tg_id, reason=body.reason, source=body.source or "admin")
    return BlacklistAddResponse(ok=True, added=added)


@app.get("/v1/admin/reporters", response_model=AdminReportersResponse)
async def v1_admin_reporters(
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """管理端：举报人信誉列表（按举报数、已核实数聚合）。"""
    items = await get_admin_reporters(session, limit=limit)
    return AdminReportersResponse(items=[AdminReporterItem(**x) for x in items])


@app.get("/v1/admin/reports", response_model=AdminReportsResponse)
async def v1_admin_reports_list(
    status: str | None = Query(None, description="pending | verified | rejected"),
    reporter_tg_id: int | None = Query(None, description="Filter by reporter Telegram ID"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """管理端：举报列表，可选按状态/举报人筛选、分页。"""
    items = await get_admin_reports(session, status=status, reporter_tg_id=reporter_tg_id, limit=limit, offset=offset)
    return AdminReportsResponse(items=[AdminReportItem(**x) for x in items])


@app.patch("/v1/admin/reports/{report_id}", response_model=FeedbackUpdateResponse)
async def v1_admin_report_update(
    report_id: int,
    body: ReportUpdateBody,
    session: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """管理端：将举报状态更新为已核实(verified)或已驳回(rejected)。"""
    ok = await update_report_status(session, report_id, body.status)
    if not ok:
        raise HTTPException(status_code=404, detail="Report not found")
    return FeedbackUpdateResponse(ok=True, message=body.status)


@app.post("/v1/admin/reports/batch", response_model=BatchReportUpdateResponse)
async def v1_admin_reports_batch(
    body: BatchReportUpdateBody,
    session: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """管理端：批量更新举报状态。"""
    if len(body.ids) > 100:
        raise HTTPException(status_code=400, detail="Max 100 reports per batch")
    updated = await batch_update_report_status(session, body.ids, body.status)
    return BatchReportUpdateResponse(ok=True, updated=updated, message=f"{updated} reports set to {body.status}")


@app.get("/v1/admin/transactions", response_model=AdminTransactionsResponse)
async def v1_admin_transactions(
    status: str | None = Query(None, description="pending | confirmed | rejected"),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """管理端：交易列表，可选按状态筛选。"""
    items = await get_admin_transactions(session, status=status, limit=limit)
    return AdminTransactionsResponse(items=[AdminTransactionItem(**x) for x in items])


@app.get("/v1/admin/stats/risk_trend", response_model=AdminRiskTrendResponse)
async def v1_admin_risk_trend(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """管理端：风险趋势（按日统计查询结果风险分布，供图表）。"""
    items = await get_admin_risk_trend(session, days=days)
    return AdminRiskTrendResponse(items=[AdminRiskTrendDay(**x) for x in items])


@app.post("/v1/invite/create", response_model=InviteCreateResponse)
async def v1_invite_create(
    querier_tg_id: int = Query(..., description="Telegram ID of the person who requested the check"),
    session: AsyncSession = Depends(get_db),
):
    result = await invite_create(session, querier_tg_id)
    return InviteCreateResponse(**result)


@app.get("/v1/invite/consume", response_model=InviteConsumeResponse)
async def v1_invite_consume(
    token: str = Query(..., description="Invite token from start parameter"),
    session: AsyncSession = Depends(get_db),
):
    querier_tg_id = await invite_consume(session, token)
    if querier_tg_id is None:
        raise HTTPException(status_code=404, detail="Invalid or expired invite link")
    return InviteConsumeResponse(querier_tg_id=querier_tg_id)


@app.post("/v1/share/create", response_model=ShareCreateResponse)
async def v1_share_create(
    tg_id: int = Query(..., description="Owner Telegram ID"),
    session: AsyncSession = Depends(get_db),
):
    result = await share_create(session, tg_id)
    return ShareCreateResponse(**result)


@app.get("/v1/share/view")
async def v1_share_view(
    token: str = Query(..., description="Share token from start parameter"),
    session: AsyncSession = Depends(get_db),
):
    """One-time: return report data for the share link owner; invalidates token."""
    data = await share_consume(session, token)
    if data is None:
        raise HTTPException(status_code=404, detail="Invalid, expired, or already used share link")
    return data


@app.get("/v1/my-recent-queries", response_model=RecentQueriesResponse)
async def v1_my_recent_queries(
    tg_id: int = Query(..., description="Your Telegram ID"),
    limit: int = Query(10, ge=1, le=20),
    session: AsyncSession = Depends(get_db),
):
    """最近查询：当前用户最近查过的对象列表，按时间倒序、按被查人去重。"""
    items = await get_recent_queries(session, tg_id, limit=limit)
    return RecentQueriesResponse(items=items)


@app.get("/v1/my-feedback", response_model=MyFeedbackResponse)
async def v1_my_feedback(
    tg_id: int = Query(..., description="Your Telegram ID (reporter)"),
    session: AsyncSession = Depends(get_db),
):
    """我的申诉：当前用户发起的纠错/误报反馈列表。"""
    items = await get_my_feedback_list(session, tg_id)
    return MyFeedbackResponse(items=items)


@app.post("/v1/activity/log")
async def v1_activity_log(
    tg_id: int = Query(..., description="User Telegram ID (target of the event)"),
    event_type: str = Query(..., description="Event type: invite_clicked | share_viewed"),
    session: AsyncSession = Depends(get_db),
):
    """Log one activity event (e.g. invite_clicked when user opens invite link). Used by bot."""
    if event_type not in ("invite_clicked", "share_viewed"):
        return {"status": "ignored"}
    await activity_log_svc(session, tg_id, event_type)
    return {"status": "ok"}


@app.get("/v1/activity/timeline", response_model=TimelineResponse)
async def v1_activity_timeline(
    tg_id: int = Query(..., description="User Telegram ID"),
    days: int = Query(7, ge=1, le=30, description="Number of days to look back (1-30)"),
    session: AsyncSession = Depends(get_db),
):
    """P1: Get activity timeline for a user (queried, invite_clicked, share_viewed) aggregated by day."""
    from services import get_timeline
    timeline = await get_timeline(session, tg_id, days=days)
    return TimelineResponse(timeline=timeline)


# ---------- 风险变化提醒 ----------
@app.post("/v1/alert/subscribe", response_model=AlertSubscribeResponse, status_code=201)
async def v1_alert_subscribe(
    tg_id: int = Query(..., description="Subscriber Telegram ID (you)"),
    target_tg_id: int = Query(..., description="Target Telegram ID to watch"),
    lang: str | None = Query(None, description="Preferred language for alerts: en, zh, tl"),
    source: str | None = Query(None, description="Optional source for analytics: query, invite, recheck, bot"),
    type: str = Query("risk_change", description="Alert type: risk_change | online_offline"),
    session: AsyncSession = Depends(get_db),
):
    """订阅：type=risk_change 为新举报/拉黑提醒，type=online_offline 为上下线提醒。"""
    status, code = await alert_subscribe(
        session, tg_id, target_tg_id, preferred_lang=lang, source=source, alert_type=type
    )
    if status == "already":
        raise HTTPException(status_code=409, detail="Already subscribed")
    if status in ("limit", "self"):
        raise HTTPException(status_code=400, detail="Subscription limit reached" if status == "limit" else "Cannot subscribe to yourself")
    return AlertSubscribeResponse(status="subscribed", target_tg_id=target_tg_id)


@app.delete("/v1/alert/subscribe", status_code=204)
async def v1_alert_unsubscribe(
    tg_id: int = Query(..., description="Subscriber Telegram ID"),
    target_tg_id: int = Query(..., description="Target to unfollow"),
    type: str | None = Query(None, description="If set, only unsubscribe this type: risk_change | online_offline"),
    session: AsyncSession = Depends(get_db),
):
    """取消订阅；未传 type 时取消该 target 下全部类型。"""
    ok = await alert_unsubscribe(session, tg_id, target_tg_id, alert_type=type)
    if not ok:
        raise HTTPException(status_code=404, detail="Subscription not found")


@app.get("/v1/alert/subscriptions", response_model=AlertSubscriptionsResponse)
async def v1_alert_subscriptions(
    tg_id: int = Query(..., description="Your Telegram ID"),
    session: AsyncSession = Depends(get_db),
):
    """我的订阅列表。"""
    items = await alert_list_subscriptions(session, tg_id)
    return AlertSubscriptionsResponse(items=items)


@app.get("/v1/alert/pending", response_model=PendingAlertsResponse)
async def v1_alert_pending(
    limit: int = Query(50, ge=1, le=100),
    session: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """Bot 拉取待发送提醒（需 Admin Key）。"""
    items = await get_pending_alerts(session, limit=limit)
    return PendingAlertsResponse(items=items)


@app.patch("/v1/alert/pending/{pending_id}", response_model=PendingAlertMarkSentResponse)
async def v1_alert_pending_mark_sent(
    pending_id: int,
    body: PendingAlertMarkSentBody,
    session: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """Bot 发送成功后标记已发送。"""
    if not body.sent:
        return PendingAlertMarkSentResponse(ok=True)
    ok = await mark_alert_sent(session, pending_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Pending alert not found")
    return PendingAlertMarkSentResponse(ok=True)


@app.post("/v1/monitor/event")
async def v1_monitor_event(
    body: MonitorEventBody,
    session: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """监控端上报目标上下线（Bot 众包或专用客户端，需 Admin Key）。"""
    enqueued, status = await monitor_report_status(
        session,
        target_tg_id=body.target_tg_id,
        event_type=body.event_type,
        reporter_tg_id=body.reporter_tg_id,
        monitor_tg_id=body.monitor_tg_id,
        payload=body.payload,
    )
    return {"status": status, "enqueued": enqueued}


@app.get("/health")
async def health():
    return {"status": "ok"}
