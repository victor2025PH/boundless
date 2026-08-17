# Business logic: resolve username/wallet, get or create user, score, report, invite.
import hashlib
import secrets
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select, func, delete, or_, case
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models import User, Blacklist, Report, WalletEntityGraph, UserBoundWallet, WalletTransactionSnapshot, QueryLog, PendingInvite, CheckLog, ReportShareToken, Feedback, ActivityEvent, RiskAlertSubscription, RiskAlertPending, OnlineStatusEvent, NotifyCheckedPending, GroupScanLog, Transaction, TransactionStatus, ReportSnapshot, ReporterStats, REPORT_CATEGORIES, SUPPORTED_BIND_CHAINS, MAX_BOUND_WALLETS_PER_USER
from scoring import compute_score_and_tips, compute_risk_factor_breakdown

# Optional: resolve @username to tg_id via Telegram Bot API
async def resolve_username_to_tg_id(username: str) -> int | None:
    """Call Telegram getChat with @username; return tg_id or None."""
    import httpx
    token = getattr(settings, "bot_token", None) or ""
    if not token or not username.strip().startswith("@"):
        return None
    un = username.strip().lstrip("@")
    url = f"https://api.telegram.org/bot{token}/getChat"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url, params={"chat_id": f"@{un}"})
            data = r.json()
            if data.get("ok") and "result" in data:
                return int(data["result"]["id"])
    except Exception:
        pass
    return None


def normalize_wallet(w: str) -> str:
    return w.strip()[:128]


async def resolve_wallet_to_tg_id(session: AsyncSession, wallet: str) -> list[int]:
    """Return list of tg_ids linked to this wallet."""
    w = normalize_wallet(wallet)
    r = await session.execute(select(WalletEntityGraph.tg_id).where(WalletEntityGraph.wallet_address == w).distinct())
    return list(r.scalars().all())


async def get_user_by_tg_id(session: AsyncSession, tg_id: int) -> User | None:
    r = await session.execute(select(User).where(User.tg_id == tg_id))
    return r.scalar_one_or_none()


async def get_blacklist(session: AsyncSession, tg_id: int) -> Blacklist | None:
    r = await session.execute(select(Blacklist).where(Blacklist.tg_id == tg_id))
    return r.scalar_one_or_none()


async def get_or_create_user(session: AsyncSession, tg_id: int, username: str | None = None) -> User:
    u = await get_user_by_tg_id(session, tg_id)
    if u:
        if username and u.username != username:
            u.username = username
            await session.flush()
        return u
    u = User(tg_id=tg_id, username=username, risk_score=50, risk_level="medium", tips=["No data yet"])
    session.add(u)
    await session.flush()
    return u


def _reporter_credibility(verified_count: int, total_reports: int) -> float:
    """P2: 举报人可信度 0~1，用于加权。total_reports=0 时返回 1.0。"""
    if total_reports <= 0:
        return 1.0
    return min(1.0, max(0.0, (verified_count or 0) / total_reports))


async def _effective_report_count_for_target(session: AsyncSession, target_tg_id: int) -> float | None:
    """P2: 按举报人可信度加权的有效举报数。无 verified 举报或无 ReporterStats 时返回 None（调用方用 report_count）。"""
    r = await session.execute(
        select(Report.reporter_tg_id).where(
            Report.target_tg_id == target_tg_id,
            Report.status == "verified",
        )
    )
    reporter_ids = [row.reporter_tg_id for row in r.all()]
    if not reporter_ids:
        return None
    r2 = await session.execute(
        select(ReporterStats.reporter_tg_id, ReporterStats.verified_count, ReporterStats.total_reports).where(
            ReporterStats.reporter_tg_id.in_(reporter_ids),
        )
    )
    stats_by_reporter = {
        row.reporter_tg_id: (_reporter_credibility(row.verified_count or 0, row.total_reports or 0))
        for row in r2.all()
    }
    effective = 0.0
    for rid in reporter_ids:
        effective += stats_by_reporter.get(rid, 1.0)  # 无统计时权重 1.0
    return effective if effective > 0 else None


async def recompute_user_score(session: AsyncSession, user: User) -> None:
    bl = await get_blacklist(session, user.tg_id)
    blacklist = bl is not None and getattr(bl, "status", "confirmed") == "confirmed"
    user.blacklist = blacklist
    effective = await _effective_report_count_for_target(session, user.tg_id) if getattr(settings, "use_effective_report_count", True) else None
    score, level, tips = compute_score_and_tips(
        register_date=user.register_date,
        is_premium=user.is_premium,
        high_risk_group_count=user.high_risk_group_count,
        blacklist=blacklist,
        report_count=user.report_count,
        effective_report_count=effective,
    )
    user.risk_score = score
    user.risk_level = level
    user.tips = tips
    await session.flush()


def _verdict_key(risk_level: str, blacklist: bool, report_count: int) -> str:
    """Return i18n key: verdict_extreme, verdict_high, verdict_medium, verdict_low."""
    if blacklist or risk_level == "extreme":
        return "verdict_extreme"
    if risk_level == "high":
        return "verdict_high"
    if risk_level == "medium":
        return "verdict_medium"
    return "verdict_low"


def _tags_list(
    first_seen: str | None,
    report_count: int,
    blacklist: bool,
    is_premium: bool,
    label: str | None,
    wallet_association_risk: bool = False,
) -> list[str]:
    """Return list of tag keys for display. 阶段 C：换号嫌疑 = 同钱包关联黑名单或高风险账号。"""
    tags = []
    if first_seen:
        try:
            from datetime import datetime
            from datetime import timedelta
            d = datetime.strptime(first_seen, "%Y-%m-%d")
            if (datetime.utcnow() - d).days < 90:
                tags.append("tag_new_account")
        except Exception:
            pass
    if report_count > 0:
        tags.append("tag_has_reports")
    if blacklist:
        tags.append("tag_blacklist")
    if is_premium:
        tags.append("tag_premium")
    if label and label in ("商户", "merchant"):
        tags.append("tag_merchant")
    if wallet_association_risk:
        tags.append("tag_same_wallet_risk")
    return tags


def _compute_risk_dimensions(
    first_seen: str | None,
    blacklist: bool,
    report_count: int,
    tx_success_rate: float,
    tx_total_count: int,
    linked_tg_count: int,
    high_risk_group_count: int,
) -> dict[str, str]:
    """阶段 C：四维度档位。identity/funds/community/association；high=好(稳定/信誉好)，association 的 high=风险高。"""
    # 身份稳定性：有首次记录则按年龄；无则未知
    identity = "unknown"
    if first_seen:
        try:
            from datetime import datetime
            d = datetime.strptime(first_seen, "%Y-%m-%d")
            months = max(0, (datetime.utcnow() - d).days / 30.0)
            if months >= 24:
                identity = "high"
            elif months >= 12:
                identity = "medium"
            elif months >= 6:
                identity = "medium"
            elif months >= 3:
                identity = "low"
            else:
                identity = "low"
        except Exception:
            pass
    funds = "unverified"
    # 社区评价：无黑名单、举报少、交易成功率高 = 高
    if blacklist or report_count >= 3:
        community = "low"
    elif report_count >= 1:
        community = "medium"
    elif tx_total_count >= 3 and tx_success_rate >= 0.8:
        community = "high"
    else:
        community = "high" if report_count == 0 else "medium"
    # 关联风险：同钱包多号、高危群多 = 风险高
    if linked_tg_count >= 3 or high_risk_group_count >= 5:
        association = "high"
    elif linked_tg_count >= 1 or high_risk_group_count >= 1:
        association = "medium"
    else:
        association = "low"
    return {
        "identity": identity,
        "funds": funds,
        "community": community,
        "association": association,
    }


async def _linked_wallet_has_blacklist_or_high_risk(session: AsyncSession, linked_tg_ids: list[int]) -> bool:
    """阶段 C：同钱包关联的其他 ID 中是否存在黑名单或高风险（high/extreme）。"""
    if not linked_tg_ids:
        return False
    bl_count = await session.execute(select(func.count(Blacklist.tg_id)).where(Blacklist.tg_id.in_(linked_tg_ids)))
    if (bl_count.scalar() or 0) > 0:
        return True
    r = await session.execute(
        select(User.tg_id).where(User.tg_id.in_(linked_tg_ids), User.risk_level.in_(("high", "extreme")))
    )
    return r.first() is not None


async def check_by_tg_id(session: AsyncSession, tg_id: int, include_timeline: bool = False) -> dict[str, Any]:
    """Return check result for tg_id. Creates user if not exists. include_timeline=True 仅用于「我的信用」以展示时间线。阶段 B：按被查人隐私设置遮蔽。阶段 C：四维度 + 换号嫌疑标签。"""
    user = await get_or_create_user(session, tg_id)
    privacy = await get_privacy_settings(session, tg_id)
    bl = await get_blacklist(session, tg_id)
    is_black = bl is not None and getattr(bl, "status", "confirmed") == "confirmed"
    await recompute_user_score(session, user)
    # Report breakdown: pending / verified
    r_pending = await session.execute(select(func.count(Report.id)).where(Report.target_tg_id == tg_id, Report.status == "pending"))
    r_verified = await session.execute(select(func.count(Report.id)).where(Report.target_tg_id == tg_id, Report.status == "verified"))
    report_pending = r_pending.scalar() or 0
    report_verified = r_verified.scalar() or 0
    report_count = report_pending + report_verified
    # Bump query count and last_seen_at (expose even if column missing on old DB)
    try:
        user.query_count = getattr(user, "query_count", 0) + 1
        if hasattr(user, "last_seen_at"):
            user.last_seen_at = datetime.utcnow()
        await session.flush()
    except Exception:
        pass
    await session.refresh(user)
    # 阶段 B：按隐私设置遮蔽，避免报告中出现「未公开」项
    first_seen = user.created_at.strftime("%Y-%m-%d") if (user.created_at and privacy.get("show_register_year", True)) else None
    updated_at = user.updated_at.strftime("%Y-%m-%d %H:%M") if user.updated_at else None
    last_seen_at = getattr(user, "last_seen_at", None)
    last_seen_at_str = last_seen_at.strftime("%Y-%m-%d %H:%M") if last_seen_at else None
    is_premium = getattr(user, "is_premium", False) if privacy.get("show_premium", True) else False
    high_risk_group_count = getattr(user, "high_risk_group_count", 0) or 0
    # 关联钱包与多 TG（换号不换地址场景）
    r_wallets = await session.execute(
        select(WalletEntityGraph.wallet_address).where(WalletEntityGraph.tg_id == tg_id).distinct()
    )
    linked_wallets = list(r_wallets.scalars().all())
    linked_tg_ids: list[int] = []
    if linked_wallets:
        r_tids = await session.execute(
            select(WalletEntityGraph.tg_id).where(WalletEntityGraph.wallet_address.in_(linked_wallets)).distinct()
        )
        linked_tg_ids = [x for x in r_tids.scalars().all() if x != tg_id]
    linked_wallet_count = len(linked_wallets)
    linked_tg_count = len(linked_tg_ids) + 1 if linked_tg_ids else (1 if linked_wallets else 0)
    wallet_association_risk = await _linked_wallet_has_blacklist_or_high_risk(session, linked_tg_ids)
    verdict = _verdict_key(user.risk_level, is_black, report_count)
    tags = _tags_list(first_seen, report_count, is_black, is_premium, getattr(user, "label", None), wallet_association_risk=wallet_association_risk)
    query_count = getattr(user, "query_count", 0)
    timeline = await get_timeline(session, tg_id, days=TIMELINE_DAYS) if include_timeline else []
    result = {
        "risk": user.risk_level,
        "score": user.risk_score,
        "tips": user.tips or [],
        "blacklist": is_black,
        "tg_id": user.tg_id,
        "username": user.username,
        "first_seen": first_seen,
        "report_count": report_count,
        "report_pending": report_pending,
        "report_verified": report_verified,
        "is_premium": is_premium,
        "query_count": query_count,
        "updated_at": updated_at,
        "last_seen_at": last_seen_at_str,
        "verdict": verdict,
        "tags": tags,
        "linked_wallets": linked_wallets,
        "linked_tg_ids": linked_tg_ids,
        "linked_wallet_count": linked_wallet_count,
        "linked_tg_count": linked_tg_count,
        "high_risk_group_count": high_risk_group_count,
        "label": getattr(user, "label", None),
        "timeline": timeline,
    }
    tx_stats = await get_user_transaction_stats(session, tg_id)
    result.update(tx_stats)
    if not privacy.get("show_tx_30d", True):
        result["tx_success_count"] = 0
        result["tx_total_count"] = 0
        result["tx_success_rate"] = 0.0
        result["tx_total_amount"] = 0.0
    result["risk_dimensions"] = _compute_risk_dimensions(
        first_seen=result.get("first_seen"),
        blacklist=is_black,
        report_count=report_count,
        tx_success_rate=result.get("tx_success_rate", 0) or 0,
        tx_total_count=result.get("tx_total_count", 0) or 0,
        linked_tg_count=linked_tg_count,
        high_risk_group_count=high_risk_group_count,
    )
    return result


async def get_report_stats_for_target(session: AsyncSession, target_tg_id: int) -> dict[str, Any]:
    """V2.0: 按分类统计举报（verified）、近7天/30天举报数。"""
    since_7d = datetime.utcnow() - timedelta(days=7)
    since_30d = datetime.utcnow() - timedelta(days=30)
    # report_by_category: verified only
    r_cat = await session.execute(
        select(Report.category, func.count(Report.id))
        .where(Report.target_tg_id == target_tg_id, Report.status == "verified", Report.category.isnot(None))
        .group_by(Report.category)
    )
    report_by_category = {row[0]: row[1] for row in r_cat.all()}
    r_7 = await session.execute(
        select(func.count(Report.id)).where(Report.target_tg_id == target_tg_id, Report.created_at >= since_7d)
    )
    r_30 = await session.execute(
        select(func.count(Report.id)).where(Report.target_tg_id == target_tg_id, Report.created_at >= since_30d)
    )
    return {
        "report_by_category": report_by_category,
        "last_7d_reports": r_7.scalar() or 0,
        "last_30d_reports": r_30.scalar() or 0,
    }


async def get_report_breakdown(session: AsyncSession, tg_id: int, include_timeline: bool = False) -> dict[str, Any]:
    """V2.0: 完整风险画像 = check 结果 + 风险来源占比 + 举报分类与时间分布 + 关联数量。"""
    base = await check_by_tg_id(session, tg_id, include_timeline=include_timeline)
    stats = await get_report_stats_for_target(session, tg_id)
    base.update(stats)
    first_seen = base.get("first_seen")
    try:
        from datetime import datetime as dt
        reg_date = dt.strptime(first_seen, "%Y-%m-%d").date() if first_seen else None
    except Exception:
        reg_date = None
    user = await get_user_by_tg_id(session, tg_id)
    high_risk_groups = (getattr(user, "high_risk_group_count", 0) or 0) if user else 0
    base["risk_factor_breakdown"] = compute_risk_factor_breakdown(
        register_date=reg_date,
        is_premium=base.get("is_premium", False),
        high_risk_group_count=high_risk_groups,
        blacklist=base.get("blacklist", False),
        report_count=base.get("report_count", 0),
        linked_tg_count=base.get("linked_tg_count", 0),
    )
    return base


async def get_sharecard_data(session: AsyncSession, tg_id: int) -> dict[str, Any]:
    """信用卡片：用于分享的简要数据（风险、分数、背书、标签）；含 first_seen/report/updated_at/label 以增强信任感。"""
    base = await check_by_tg_id(session, tg_id, include_timeline=False)
    # 是否对查阅者展示流水：有至少一个绑定且 show_flow_to_public；是否至少一个已验证
    wallets_public = await _get_bound_wallets_with_show_flow(session, tg_id)
    flow_verified = any(w.get("verified_at") for w in wallets_public)
    return {
        "tg_id": base.get("tg_id"),
        "username": base.get("username"),
        "risk": base.get("risk"),
        "score": base.get("score"),
        "blacklist": base.get("blacklist", False),
        "tags": base.get("tags") or [],
        "tx_success_count": base.get("tx_success_count", 0),
        "tx_total_count": base.get("tx_total_count", 0),
        "tx_success_rate": base.get("tx_success_rate", 0),
        "first_seen": base.get("first_seen"),
        "report_count": base.get("report_count", 0),
        "report_verified": base.get("report_verified", 0),
        "updated_at": base.get("updated_at"),
        "label": base.get("label"),
        "has_flow_visible": len(wallets_public) > 0,
        "flow_verified": flow_verified,
    }


# ---------- 绑定收款地址与流水 ----------
async def _get_bound_wallets_with_show_flow(session: AsyncSession, tg_id: int) -> list[dict[str, Any]]:
    r = await session.execute(
        select(UserBoundWallet).where(UserBoundWallet.tg_id == tg_id, UserBoundWallet.show_flow_to_public == True)
    )
    rows = list(r.scalars().all())
    return [
        {"id": w.id, "chain": w.chain, "address": w.address, "label": w.label, "verified_at": getattr(w, "verified_at", None)}
        for w in rows
    ]


async def list_bound_wallets(session: AsyncSession, tg_id: int) -> list[dict[str, Any]]:
    """当前用户绑定的收款地址列表（含脱敏地址与展示开关）。"""
    r = await session.execute(select(UserBoundWallet).where(UserBoundWallet.tg_id == tg_id).order_by(UserBoundWallet.created_at))
    rows = list(r.scalars().all())
    out = []
    for w in rows:
        addr = (w.address or "")
        masked = f"{addr[:6]}...{addr[-4:]}" if len(addr) >= 12 else "***"
        out.append({
            "id": w.id,
            "chain": w.chain,
            "address": w.address,
            "address_masked": masked,
            "label": w.label,
            "show_flow_to_public": w.show_flow_to_public,
            "created_at": w.created_at.isoformat() if w.created_at else None,
            "verified_at": w.verified_at.isoformat() if getattr(w, "verified_at", None) else None,
        })
    return out


async def bind_wallet(
    session: AsyncSession,
    tg_id: int,
    chain: str,
    address: str,
    label: str | None = None,
    show_flow_to_public: bool = False,
) -> dict[str, Any]:
    """绑定一条收款地址。校验格式与上限，返回新建记录。"""
    from chain_flow import validate_address
    chain = (chain or "").strip().upper()
    if chain not in SUPPORTED_BIND_CHAINS:
        raise ValueError("unsupported_chain")
    addr = (address or "").strip()[:128]
    if not addr or not validate_address(chain, addr):
        raise ValueError("invalid_address")
    count = await session.execute(select(func.count()).select_from(UserBoundWallet).where(UserBoundWallet.tg_id == tg_id))
    if (count.scalar() or 0) >= MAX_BOUND_WALLETS_PER_USER:
        raise ValueError("limit_reached")
    existing = await session.execute(
        select(UserBoundWallet).where(UserBoundWallet.tg_id == tg_id, UserBoundWallet.chain == chain, UserBoundWallet.address == addr)
    )
    if existing.scalar_one_or_none():
        raise ValueError("already_bound")
    w = UserBoundWallet(tg_id=tg_id, chain=chain, address=addr, label=(label or "").strip() or None, show_flow_to_public=show_flow_to_public)
    session.add(w)
    await session.flush()
    # P0：钱包变动推送触发 — 用户绑定新地址后，为订阅了 wallet_change 的用户入队
    await enqueue_wallet_change_for_target(session, tg_id)
    return {"id": w.id, "chain": w.chain, "address": w.address, "label": w.label, "show_flow_to_public": w.show_flow_to_public}


async def unbind_wallet(session: AsyncSession, tg_id: int, wallet_id: int) -> bool:
    r = await session.execute(select(UserBoundWallet).where(UserBoundWallet.id == wallet_id, UserBoundWallet.tg_id == tg_id))
    w = r.scalar_one_or_none()
    if not w:
        return False
    # 删除绑定记录本身
    await session.delete(w)
    # 同步删除该绑定地址下的所有流水快照，避免解绑后仍残留可关联的数据
    await session.execute(
        delete(WalletTransactionSnapshot).where(WalletTransactionSnapshot.wallet_id == wallet_id)
    )
    await session.flush()
    return True


async def set_wallet_verified_at_if_empty(session: AsyncSession, wallet_id: int) -> bool:
    """当该绑定尚未验证时，将 verified_at 设为当前时间（用于首次成功拉取到流水后自动标记）。"""
    r = await session.execute(select(UserBoundWallet).where(UserBoundWallet.id == wallet_id))
    w = r.scalar_one_or_none()
    if not w or getattr(w, "verified_at", None) is not None:
        return False
    w.verified_at = datetime.utcnow()
    await session.flush()
    return True


async def update_wallet_show_flow(session: AsyncSession, tg_id: int, wallet_id: int, show_flow_to_public: bool) -> bool:
    r = await session.execute(select(UserBoundWallet).where(UserBoundWallet.id == wallet_id, UserBoundWallet.tg_id == tg_id))
    w = r.scalar_one_or_none()
    if not w:
        return False
    w.show_flow_to_public = show_flow_to_public
    await session.flush()
    return True


def _mask_addr(a: str) -> str:
    if not a or len(a) < 12:
        return "***"
    return f"{a[:6]}...{a[-4:]}"


async def get_bound_wallets_visible_to_querier(session: AsyncSession, target_tg_id: int) -> list[dict[str, Any]]:
    """被查人开放给查阅者看的绑定列表（脱敏，用于「查看对方流水」入口）。"""
    wallets = await _get_bound_wallets_with_show_flow(session, target_tg_id)
    return [{"id": x["id"], "chain": x["chain"], "address_masked": _mask_addr(x["address"]), "label": x["label"]} for x in wallets]


async def can_view_flow(session: AsyncSession, target_tg_id: int) -> bool:
    """被查人是否开放了至少一个地址的流水。"""
    wallets = await _get_bound_wallets_with_show_flow(session, target_tg_id)
    return len(wallets) > 0


async def get_flow_for_target(session: AsyncSession, target_tg_id: int, limit: int = 30) -> tuple[list[dict[str, Any]], str | None]:
    """拉取被查人所有「对查阅者展示」地址的流水，合并按时间倒序；失败返回错误信息。首次拉取到流水时自动设 verified_at。"""
    from chain_flow import get_flow_for_address
    wallets = await _get_bound_wallets_with_show_flow(session, target_tg_id)
    if not wallets:
        return [], "no_wallets"
    tron_key = getattr(settings, "trongrid_api_key", "") or ""
    ton_key = getattr(settings, "toncenter_api_key", "") or tron_key
    merged: list[dict[str, Any]] = []
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
                r["address_masked"] = _mask_addr(w["address"])
            merged.extend(rows)
        except Exception:
            continue
    merged.sort(key=lambda x: (x.get("time") or 0), reverse=True)
    return merged[:limit], None


async def sync_flow_snapshots(session: AsyncSession, tg_id: int, limit_per_wallet: int = 50) -> int:
    """按需同步：拉取当前用户所有绑定地址的流水并写入 wallet_transaction_snapshots，用于分页/统计。返回新写入条数。"""
    from chain_flow import get_flow_for_address
    wallets = await list_bound_wallets(session, tg_id)
    if not wallets:
        return 0
    tron_key = getattr(settings, "trongrid_api_key", "") or ""
    ton_key = getattr(settings, "toncenter_api_key", "") or tron_key
    written = 0
    for w in wallets:
        wid = w.get("id")
        if not wid:
            continue
        try:
            rows = await get_flow_for_address(
                w["chain"], w["address"], limit=limit_per_wallet,
                api_key=tron_key, ton_api_key=ton_key,
            )
        except Exception:
            continue
        existing = await session.execute(
            select(WalletTransactionSnapshot.tx_hash).where(WalletTransactionSnapshot.wallet_id == wid)
        )
        have = set(existing.scalars().all())
        for r in rows:
            tx_hash = (r.get("tx_hash") or "").strip() or str(r.get("time", ""))
            if not tx_hash or tx_hash in have:
                continue
            have.add(tx_hash)
            snap = WalletTransactionSnapshot(
                wallet_id=wid,
                tx_hash=tx_hash[:128],
                chain=r.get("chain", ""),
                direction=r.get("direction", "in"),
                amount=float(r.get("amount", 0)),
                currency=r.get("currency", "USDT"),
                counterparty_masked=(r.get("counterparty_masked") or "")[:32],
                block_time_ms=int(r.get("time") or 0),
            )
            session.add(snap)
            written += 1
    await session.flush()
    # 顺带清理该用户下超过保留期的快照，控制表体积（保留期从配置读取）
    retention_days = getattr(settings, "flow_snapshot_retention_days", 90) or 90
    await cleanup_flow_snapshots_older_than_days(session, tg_id, days=retention_days)
    return written


async def cleanup_flow_snapshots_older_than_days(
    session: AsyncSession, tg_id: int, days: int = 90
) -> int:
    """删除该用户所有绑定钱包下早于 N 天的流水快照；按 created_at 判断。返回删除条数。"""
    from datetime import timedelta
    r0 = await session.execute(select(UserBoundWallet.id).where(UserBoundWallet.tg_id == tg_id))
    wallet_ids = list(r0.scalars().all())
    if not wallet_ids:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    r = await session.execute(
        delete(WalletTransactionSnapshot).where(
            WalletTransactionSnapshot.wallet_id.in_(wallet_ids),
            WalletTransactionSnapshot.created_at < cutoff,
        )
    )
    await session.flush()
    return r.rowcount if hasattr(r, "rowcount") else 0


async def get_flow_snapshots(
    session: AsyncSession, tg_id: int, limit: int = 30, offset: int = 0
) -> list[dict]:
    """从快照表读取流水（分页），需先 sync。"""
    r0 = await session.execute(select(UserBoundWallet.id).where(UserBoundWallet.tg_id == tg_id))
    wallet_ids = list(r0.scalars().all())
    if not wallet_ids:
        return []
    r = await session.execute(
        select(WalletTransactionSnapshot)
        .where(WalletTransactionSnapshot.wallet_id.in_(wallet_ids))
        .order_by(WalletTransactionSnapshot.block_time_ms.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = list(r.scalars().all())
    return [
        {
            "direction": s.direction,
            "amount": s.amount,
            "currency": s.currency,
            "counterparty_masked": s.counterparty_masked or "",
            "time": s.block_time_ms,
            "tx_hash": (s.tx_hash or "")[:16],
            "chain": s.chain or "",
        }
        for s in rows
    ]


async def get_flow_stats_30d(session: AsyncSession, tg_id: int) -> dict:
    """近 30 天流水统计：按用户所有绑定钱包聚合流入/流出笔数与金额。"""
    from datetime import timedelta

    r0 = await session.execute(select(UserBoundWallet.id).where(UserBoundWallet.tg_id == tg_id))
    wallet_ids = list(r0.scalars().all())
    if not wallet_ids:
        return {
            "days": 30,
            "in_count": 0,
            "in_amount": 0.0,
            "out_count": 0,
            "out_amount": 0.0,
        }
    # 以 block_time_ms 为准，取最近 30 天
    now = datetime.utcnow()
    cutoff = now - timedelta(days=30)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    r = await session.execute(
        select(
            WalletTransactionSnapshot.direction,
            func.count(WalletTransactionSnapshot.id),
            func.sum(WalletTransactionSnapshot.amount),
        ).where(
            WalletTransactionSnapshot.wallet_id.in_(wallet_ids),
            WalletTransactionSnapshot.block_time_ms >= cutoff_ms,
        ).group_by(
            WalletTransactionSnapshot.direction
        )
    )
    in_count = in_amount = out_count = out_amount = 0
    for direction, cnt, amt in r.all():
        if direction == "in":
            in_count = int(cnt or 0)
            in_amount = float(amt or 0.0)
        elif direction == "out":
            out_count = int(cnt or 0)
            out_amount = float(amt or 0.0)
    return {
        "days": 30,
        "in_count": in_count,
        "in_amount": in_amount,
        "out_count": out_count,
        "out_amount": out_amount,
    }


async def log_query_anon(session: AsyncSession, target_tg_id: int, risk_level: str) -> None:
    """Optional: log anonymous query for stats."""
    day = datetime.utcnow().strftime("%Y-%m-%d")
    h = hashlib.sha256(f"{target_tg_id}:{day}".encode()).hexdigest()[:64]
    session.add(QueryLog(query_hash=h, result_risk_level=risk_level))
    await session.flush()


def _generate_report_hash() -> str:
    """8 位验真码：TC- + 6 位十六进制，如 TC-78A2B9。"""
    return "TC-" + secrets.token_hex(3).upper()


async def register_report_snapshot(
    session: AsyncSession, target_tg_id: int, content_hash: str
) -> str:
    """写入报告快照，返回 8 位 report_hash。content_hash 为报告正文的 sha256 hex。"""
    report_hash = _generate_report_hash()
    snap = ReportSnapshot(
        report_hash=report_hash,
        target_tg_id=target_tg_id,
        content_hash=content_hash,
    )
    session.add(snap)
    await session.flush()
    return report_hash


async def get_report_snapshot_by_hash(
    session: AsyncSession, report_hash: str
) -> dict[str, Any] | None:
    """根据验真码查询快照。返回 None 表示无效；否则返回 valid, created_at, target_tg_id。"""
    r = await session.execute(
        select(ReportSnapshot).where(ReportSnapshot.report_hash == report_hash.strip().upper())
    )
    row = r.scalar_one_or_none()
    if not row:
        return None
    return {
        "valid": True,
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else None,
        "target_tg_id": row.target_tg_id,
        "content_hash": (row.content_hash or "").strip().lower() if getattr(row, "content_hash", None) else None,
    }


def _normalize_report_category(category: str | None) -> str | None:
    if not category:
        return None
    c = (category or "").strip().lower()
    return c if c in REPORT_CATEGORIES else None


async def report_create(
    session: AsyncSession,
    reporter_tg_id: int,
    target_tg_id: int,
    reason: str,
    tx_hash: str | None = None,
    screenshot_url: str | None = None,
    description: str | None = None,
    category: str | None = None,
) -> int:
    ev = {}
    if tx_hash:
        ev["tx_hash"] = str(tx_hash)[:128]
    if screenshot_url:
        ev["screenshot_url"] = str(screenshot_url)[:500]
    if description:
        ev["description"] = str(description)[:2000]
    cat = _normalize_report_category(category)
    r = Report(reporter_tg_id=reporter_tg_id, target_tg_id=target_tg_id, reason=reason, category=cat, evidence_data=ev or None, status="pending")
    session.add(r)
    await session.flush()
    # 同步一条 Feedback 供「我的申诉」展示；后续管理员可单独更新 Feedback 状态
    fb = Feedback(reporter_tg_id=reporter_tg_id, target_tg_id=target_tg_id, reason=reason, evidence_refs=None, status="pending")
    session.add(fb)
    await session.flush()
    await get_or_create_user(session, target_tg_id)
    await enqueue_alerts_for_target(session, target_tg_id, "new_report", exclude_tg_id=reporter_tg_id)
    # 任务 1.6：举报人统计，仅落数据；scoring 暂不接入权重
    now = datetime.utcnow()
    rstat = await session.execute(select(ReporterStats).where(ReporterStats.reporter_tg_id == reporter_tg_id))
    row = rstat.scalar_one_or_none()
    if row:
        row.total_reports += 1
        row.last_report_at = now
    else:
        session.add(ReporterStats(reporter_tg_id=reporter_tg_id, total_reports=1, last_report_at=now))
    await session.flush()
    return r.id


# ---------- V2.0 交易确认 ----------
MAX_PENDING_TRANSACTIONS_PER_PAIR = 5  # 同一对 (initiator, counterparty) 最多未处理条数


class TransactionRejectError(Exception):
    """发起或响应交易确认被拒绝时抛出（自刷、限频、权限等）"""
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


async def transaction_confirm_create(
    session: AsyncSession,
    initiator_tg_id: int,
    counterparty_tg_id: int,
    amount: str,
    currency: str = "USDT",
    memo: str | None = None,
) -> int:
    """发起一笔交易确认请求；对方需在 Bot/API 中同意或拒绝。返回 transaction id。"""
    if initiator_tg_id == counterparty_tg_id:
        raise TransactionRejectError("self_trade", "Cannot confirm transaction with yourself.")
    amount_str = (amount or "").strip()[:32]
    if not amount_str:
        raise TransactionRejectError("invalid_amount", "Amount is required.")
    # 同对 (initiator, counterparty) 未处理数量限制
    pending_count = await session.execute(
        select(func.count(Transaction.id)).where(
            Transaction.initiator_tg_id == initiator_tg_id,
            Transaction.counterparty_tg_id == counterparty_tg_id,
            Transaction.status == TransactionStatus.PENDING.value,
        )
    )
    if (pending_count.scalar() or 0) >= MAX_PENDING_TRANSACTIONS_PER_PAIR:
        raise TransactionRejectError("too_many_pending", "Too many pending requests with this user. Wait for them to respond.")
    tx = Transaction(
        initiator_tg_id=initiator_tg_id,
        counterparty_tg_id=counterparty_tg_id,
        amount=amount_str,
        currency=(currency or "USDT").strip()[:16],
        memo=(memo or "").strip()[:500] or None,
        status=TransactionStatus.PENDING.value,
    )
    session.add(tx)
    await session.flush()
    return tx.id


async def transaction_respond(
    session: AsyncSession,
    transaction_id: int,
    responder_tg_id: int,
    accept: bool,
) -> dict[str, Any]:
    """对方对某笔交易确认请求做出响应：同意或拒绝。返回该笔交易当前状态与摘要。"""
    r = await session.execute(select(Transaction).where(Transaction.id == transaction_id))
    tx = r.scalar_one_or_none()
    if not tx:
        raise TransactionRejectError("not_found", "Transaction not found.")
    if tx.counterparty_tg_id != responder_tg_id:
        raise TransactionRejectError("forbidden", "Only the counterparty can respond to this request.")
    if tx.status != TransactionStatus.PENDING.value:
        raise TransactionRejectError("already_resolved", "This request was already confirmed or rejected.")
    now = datetime.utcnow()
    tx.status = TransactionStatus.CONFIRMED.value if accept else TransactionStatus.REJECTED.value
    tx.confirmed_at = now if accept else None
    await session.flush()
    return {
        "transaction_id": tx.id,
        "status": tx.status,
        "initiator_tg_id": tx.initiator_tg_id,
        "counterparty_tg_id": tx.counterparty_tg_id,
        "amount": tx.amount,
        "currency": tx.currency,
        "confirmed_at": now.isoformat() if accept else None,
    }


async def get_pending_transactions_for_counterparty(session: AsyncSession, counterparty_tg_id: int) -> list[dict[str, Any]]:
    """对方待确认的交易列表（供 Bot 展示「同意/拒绝」）。"""
    r = await session.execute(
        select(Transaction)
        .where(
            Transaction.counterparty_tg_id == counterparty_tg_id,
            Transaction.status == TransactionStatus.PENDING.value,
        )
        .order_by(Transaction.created_at.desc())
        .limit(50)
    )
    rows = r.scalars().all()
    return [
        {
            "id": t.id,
            "initiator_tg_id": t.initiator_tg_id,
            "counterparty_tg_id": t.counterparty_tg_id,
            "amount": t.amount,
            "currency": t.currency,
            "memo": t.memo,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in rows
    ]


async def get_user_transaction_stats(session: AsyncSession, tg_id: int) -> dict[str, Any]:
    """某用户作为发起方或对方参与且已结束（confirmed/rejected）的笔数、成功率、累计确认金额。"""
    # 参与的所有已结束交易（发起方或对方）
    subq = select(
        Transaction.id,
        Transaction.status,
        Transaction.amount,
    ).where(
        or_(Transaction.initiator_tg_id == tg_id, Transaction.counterparty_tg_id == tg_id),
        Transaction.status.in_([TransactionStatus.CONFIRMED.value, TransactionStatus.REJECTED.value]),
    )
    r_all = await session.execute(subq)
    rows = r_all.all()
    total_resolved = len(rows)
    success_count = sum(1 for _, status, _ in rows if status == TransactionStatus.CONFIRMED.value)
    total_amount = 0.0
    for _, status, amt in rows:
        if status == TransactionStatus.CONFIRMED.value and amt:
            try:
                total_amount += float(amt)
            except (ValueError, TypeError):
                pass
    success_rate = (success_count / total_resolved * 100) if total_resolved else 0
    return {
        "tx_success_count": success_count,
        "tx_total_count": total_resolved,
        "tx_success_rate": round(success_rate, 1),
        "tx_total_amount": round(total_amount, 2),
    }


INVITE_EXPIRE_MINUTES = 15


async def invite_create(session: AsyncSession, querier_tg_id: int) -> dict[str, Any]:
    """Create one-time invite link; return token and full link."""
    token = "inv_" + secrets.token_urlsafe(8)[:10]
    expires_at = datetime.utcnow() + timedelta(minutes=INVITE_EXPIRE_MINUTES)
    session.add(PendingInvite(token=token, querier_tg_id=querier_tg_id, expires_at=expires_at))
    await session.flush()
    username = getattr(settings, "bot_username", "xyc2026_bot") or "xyc2026_bot"
    link = f"https://t.me/{username.lstrip('@')}?start={token}"
    return {"token": token, "link": link, "expires_in_minutes": INVITE_EXPIRE_MINUTES}


async def invite_consume(session: AsyncSession, token: str) -> int | None:
    """Return querier_tg_id if token valid and not expired; then delete the invite (one-time). Also log activity event."""
    now = datetime.utcnow()
    r = await session.execute(
        select(PendingInvite).where(PendingInvite.token == token, PendingInvite.expires_at > now)
    )
    row = r.scalar_one_or_none()
    if not row:
        return None
    querier_tg_id = row.querier_tg_id
    # P1: 在服务层也写入 ActivityEvent，即使 Bot 端调用失败也有记录（双重保险）
    # 注意：这里 target_tg_id 是点击邀请链接的用户，但此时我们还不知道，所以由 Bot 端调用 activity_log 传入 target_tg_id
    # 这里仅删除 invite token，ActivityEvent 由 Bot 端在知道 target_tg_id 后写入
    await session.execute(delete(PendingInvite).where(PendingInvite.token == token))
    await session.flush()
    return querier_tg_id


def _activity_insert(session: AsyncSession, target_tg_id: int, event_type: str) -> None:
    session.add(ActivityEvent(target_tg_id=target_tg_id, event_type=event_type))


async def check_log_insert(session: AsyncSession, querier_tg_id: int, target_tg_id: int) -> None:
    """Record who queried whom (for 谁查过我), activity timeline; 若被查人开启通知则入队待推送。"""
    session.add(CheckLog(querier_tg_id=querier_tg_id, target_tg_id=target_tg_id))
    _activity_insert(session, target_tg_id, "queried")
    await session.flush()
    user = await get_user_by_tg_id(session, target_tg_id)
    if user and getattr(user, "notify_on_checked", True):
        session.add(NotifyCheckedPending(target_tg_id=target_tg_id, querier_tg_id=querier_tg_id))
        await session.flush()


# 阶段 B：隐私设置默认值（对查阅者展示项；未设或缺失键视为 True 以兼容旧数据）
DEFAULT_PRIVACY_SETTINGS = {"show_register_year": True, "show_premium": True, "show_tx_30d": True}


def _normalize_privacy_settings(raw: dict | None) -> dict[str, bool]:
    """返回合并默认值后的隐私设置，保证三键存在且为 bool。"""
    out = dict(DEFAULT_PRIVACY_SETTINGS)
    if not raw or not isinstance(raw, dict):
        return out
    for k in ("show_register_year", "show_premium", "show_tx_30d"):
        if k in raw:
            out[k] = bool(raw[k])
    return out


async def get_privacy_settings(session: AsyncSession, tg_id: int) -> dict[str, bool]:
    """阶段 B：获取用户隐私设置；无用户或未设则返回默认（全展示）。"""
    user = await get_user_by_tg_id(session, tg_id)
    if not user:
        return dict(DEFAULT_PRIVACY_SETTINGS)
    raw = getattr(user, "privacy_settings", None)
    return _normalize_privacy_settings(raw)


async def set_privacy_settings(session: AsyncSession, tg_id: int, settings: dict[str, bool]) -> None:
    """阶段 B：更新用户隐私设置；仅写入允许的键。"""
    user = await get_or_create_user(session, tg_id)
    allowed = {"show_register_year", "show_premium", "show_tx_30d"}
    current = _normalize_privacy_settings(getattr(user, "privacy_settings", None))
    for k in allowed:
        if k in settings:
            current[k] = bool(settings[k])
    user.privacy_settings = current
    await session.flush()


async def get_notify_on_checked(session: AsyncSession, tg_id: int) -> bool:
    """被查通知开关：无用户或未设则默认 True（开启）。"""
    user = await get_user_by_tg_id(session, tg_id)
    if not user:
        return True
    return bool(getattr(user, "notify_on_checked", True))


async def set_notify_on_checked(session: AsyncSession, tg_id: int, value: bool) -> None:
    """设置被查通知开关；若用户不存在则先 get_or_create_user。"""
    user = await get_or_create_user(session, tg_id)
    user.notify_on_checked = value
    await session.flush()


async def get_pending_notify_checked(session: AsyncSession, limit: int = 50) -> list[dict[str, Any]]:
    """待发送的被查通知（Bot 拉取后发送并 mark_sent）。"""
    r = await session.execute(
        select(NotifyCheckedPending)
        .where(NotifyCheckedPending.sent_at.is_(None))
        .order_by(NotifyCheckedPending.created_at.asc())
        .limit(limit)
    )
    rows = r.scalars().all()
    return [
        {"id": x.id, "target_tg_id": x.target_tg_id, "querier_tg_id": x.querier_tg_id, "created_at": x.created_at.isoformat() if x.created_at else None}
        for x in rows
    ]


async def mark_notify_checked_sent(session: AsyncSession, pending_id: int) -> bool:
    """标记被查通知已发送。"""
    r = await session.execute(select(NotifyCheckedPending).where(NotifyCheckedPending.id == pending_id))
    row = r.scalar_one_or_none()
    if not row:
        return False
    row.sent_at = datetime.utcnow()
    await session.flush()
    return True


async def group_scan(session: AsyncSession, group_id: int, requester_tg_id: int) -> tuple[bool, str]:
    """V2.0 群扫描：每群每日限 1 次。返回 (ok, message)。"""
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    r = await session.execute(
        select(func.count(GroupScanLog.id)).where(
            GroupScanLog.group_id == group_id,
            GroupScanLog.created_at >= today_start,
        )
    )
    if (r.scalar() or 0) >= 1:
        return False, "already_scanned"
    session.add(GroupScanLog(group_id=group_id, requester_tg_id=requester_tg_id))
    await session.flush()
    return True, "ok"


async def get_mycredit_queried_stats(session: AsyncSession, target_tg_id: int) -> dict[str, Any]:
    """Return recent_7d_query_count and last_queried_at for 我的信用."""
    since = datetime.utcnow() - timedelta(days=7)
    r_count = await session.execute(
        select(func.count(CheckLog.id)).where(
            CheckLog.target_tg_id == target_tg_id,
            CheckLog.created_at >= since,
        )
    )
    recent_7d = r_count.scalar() or 0
    r_last = await session.execute(
        select(CheckLog.created_at)
        .where(CheckLog.target_tg_id == target_tg_id)
        .order_by(CheckLog.created_at.desc())
        .limit(1)
    )
    row = r_last.scalar_one_or_none()
    last_queried_at = row.strftime("%Y-%m-%d %H:%M") if row else None
    return {"recent_7d_query_count": recent_7d, "last_queried_at": last_queried_at}


RECENT_QUERIES_LIMIT = 10


async def get_recent_queries(session: AsyncSession, querier_tg_id: int, limit: int = RECENT_QUERIES_LIMIT) -> list[dict[str, Any]]:
    """最近查询：按被查人去重，取最近 N 条（每条含 target_tg_id, last_queried_at）。"""
    r = await session.execute(
        select(CheckLog.target_tg_id, CheckLog.created_at)
        .where(CheckLog.querier_tg_id == querier_tg_id)
        .order_by(CheckLog.created_at.desc())
        .limit(limit * 3)
    )
    rows = r.all()
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for target_tg_id, created_at in rows:
        if target_tg_id in seen:
            continue
        seen.add(target_tg_id)
        out.append({"target_tg_id": target_tg_id, "last_queried_at": created_at.strftime("%Y-%m-%d %H:%M")})
        if len(out) >= limit:
            break
    return out


TIMELINE_DAYS = 7


async def activity_log(session: AsyncSession, target_tg_id: int, event_type: str) -> None:
    """Log one activity event (e.g. invite_clicked from bot). event_type: queried | invite_clicked | share_viewed."""
    if event_type not in ("queried", "invite_clicked", "share_viewed"):
        return
    _activity_insert(session, target_tg_id, event_type)
    await session.flush()


async def get_timeline(session: AsyncSession, target_tg_id: int, days: int = TIMELINE_DAYS) -> list[dict[str, Any]]:
    """最近活动时间线：按日聚合，返回 [{date, queried, invite_clicked, share_viewed}, ...] 倒序."""
    since = datetime.utcnow() - timedelta(days=days)
    r = await session.execute(
        select(ActivityEvent.event_type, ActivityEvent.created_at).where(
            ActivityEvent.target_tg_id == target_tg_id,
            ActivityEvent.created_at >= since,
        )
    )
    by_date: dict[str, dict[str, int]] = {}
    for event_type, created_at in r.all():
        d = created_at.strftime("%Y-%m-%d") if created_at else ""
        if d not in by_date:
            by_date[d] = {"date": d, "queried": 0, "invite_clicked": 0, "share_viewed": 0}
        if event_type in by_date[d]:
            by_date[d][event_type] += 1
    out = sorted(by_date.values(), key=lambda x: x["date"], reverse=True)
    return out


async def get_my_feedback_list(session: AsyncSession, reporter_tg_id: int) -> list[dict[str, Any]]:
    """我的申诉：我发起的纠错/误报反馈列表（id, target_tg_id, reason, status, created_at）。"""
    r = await session.execute(
        select(Feedback.id, Feedback.target_tg_id, Feedback.reason, Feedback.status, Feedback.created_at)
        .where(Feedback.reporter_tg_id == reporter_tg_id)
        .order_by(Feedback.created_at.desc())
        .limit(20)
    )
    return [
        {
            "id": row.id,
            "target_tg_id": row.target_tg_id,
            "reason": (row.reason or "")[:200],
            "status": row.status or "pending",
            "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else None,
        }
        for row in r.all()
    ]


async def get_feedback_list_admin(
    session: AsyncSession, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """管理端：申诉列表，可选按 status 筛选（pending/processed/rejected）。"""
    q = select(
        Feedback.id,
        Feedback.reporter_tg_id,
        Feedback.target_tg_id,
        Feedback.reason,
        Feedback.status,
        Feedback.created_at,
    ).order_by(Feedback.created_at.desc()).limit(limit)
    if status:
        q = q.where(Feedback.status == status)
    r = await session.execute(q)
    return [
        {
            "id": row.id,
            "reporter_tg_id": row.reporter_tg_id,
            "target_tg_id": row.target_tg_id,
            "reason": (row.reason or "")[:500],
            "status": row.status or "pending",
            "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else None,
        }
        for row in r.all()
    ]


async def update_feedback_status(session: AsyncSession, feedback_id: int, status: str) -> bool:
    """管理端：更新申诉状态为 processed 或 rejected。返回是否更新成功。"""
    if status not in ("processed", "rejected"):
        return False
    r = await session.execute(select(Feedback).where(Feedback.id == feedback_id))
    row = r.scalar_one_or_none()
    if not row:
        return False
    row.status = status
    await session.flush()
    return True


# ---------- 风险变化提醒 ----------
ALERT_SUBSCRIPTION_LIMIT = 20
ALERT_RATE_LIMIT_HOURS = 24  # 同一 (sub, target, event_type) 在此时间内只入队一次；0 表示仅去重不限频


def _normalize_alert_lang(lang: str | None) -> str | None:
    """只保留支持的推送语言，便于 i18n。"""
    if not lang or not isinstance(lang, str):
        return None
    v = lang.strip().lower()[:8]
    return v if v in ("en", "zh", "tl") else None


def _normalize_alert_type(t: str | None) -> str:
    if t in ("risk_change", "online_offline", "report_spike", "wallet_change"):
        return t
    return "risk_change"


async def alert_subscribe(
    session: AsyncSession,
    subscriber_tg_id: int,
    target_tg_id: int,
    preferred_lang: str | None = None,
    source: str | None = None,
    alert_type: str = "risk_change",
) -> tuple[str, int]:
    """订阅：subscriber 关注 target；alert_type=risk_change|online_offline。"""
    if subscriber_tg_id == target_tg_id:
        return "self", 400
    at = _normalize_alert_type(alert_type)
    r = await session.execute(
        select(RiskAlertSubscription).where(
            RiskAlertSubscription.subscriber_tg_id == subscriber_tg_id,
            RiskAlertSubscription.target_tg_id == target_tg_id,
            RiskAlertSubscription.alert_type == at,
        )
    )
    if r.scalar_one_or_none():
        return "already", 409
    count = await session.execute(
        select(func.count(RiskAlertSubscription.id)).where(RiskAlertSubscription.subscriber_tg_id == subscriber_tg_id)
    )
    if (count.scalar() or 0) >= ALERT_SUBSCRIPTION_LIMIT:
        return "limit", 400
    lang = _normalize_alert_lang(preferred_lang)
    source_trim = (source or "").strip()[:32] or None
    session.add(
        RiskAlertSubscription(
            subscriber_tg_id=subscriber_tg_id,
            target_tg_id=target_tg_id,
            alert_type=at,
            preferred_lang=lang,
            source=source_trim,
        )
    )
    await session.flush()
    return "ok", 0


async def alert_unsubscribe(
    session: AsyncSession,
    subscriber_tg_id: int,
    target_tg_id: int,
    alert_type: str | None = None,
) -> bool:
    """取消订阅。若指定 alert_type 只删该类型，否则删该 target 下所有类型（兼容旧调用）。"""
    q = select(RiskAlertSubscription).where(
        RiskAlertSubscription.subscriber_tg_id == subscriber_tg_id,
        RiskAlertSubscription.target_tg_id == target_tg_id,
    )
    if alert_type:
        q = q.where(RiskAlertSubscription.alert_type == _normalize_alert_type(alert_type))
    r = await session.execute(q)
    rows = r.scalars().all()
    for row in rows:
        await session.delete(row)
    if rows:
        await session.flush()
    return len(rows) > 0


async def alert_list_subscriptions(session: AsyncSession, subscriber_tg_id: int) -> list[dict[str, Any]]:
    """我的订阅列表；每项含 target_tg_id, created_at, alert_type。"""
    r = await session.execute(
        select(
            RiskAlertSubscription.target_tg_id,
            RiskAlertSubscription.created_at,
            RiskAlertSubscription.alert_type,
        )
        .where(RiskAlertSubscription.subscriber_tg_id == subscriber_tg_id)
        .order_by(RiskAlertSubscription.created_at.desc())
    )
    return [
        {
            "target_tg_id": row.target_tg_id,
            "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else None,
            "alert_type": getattr(row, "alert_type", None) or "risk_change",
        }
        for row in r.all()
    ]


async def _alert_skip_set(
    session: AsyncSession,
    target_tg_id: int,
    event_type: str,
    since: datetime | None,
) -> set[tuple[int, int, str]]:
    """去重+限频：返回应跳过的 (subscriber_tg_id, target_tg_id, event_type) 集合。"""
    q = (
        select(
            RiskAlertPending.subscriber_tg_id,
            RiskAlertPending.target_tg_id,
            RiskAlertPending.event_type,
        )
        .where(
            RiskAlertPending.target_tg_id == target_tg_id,
            RiskAlertPending.event_type == event_type,
        )
        .distinct()
    )
    if since is not None:
        q = q.where((RiskAlertPending.sent_at.is_(None)) | (RiskAlertPending.created_at >= since))
    else:
        q = q.where(RiskAlertPending.sent_at.is_(None))
    r = await session.execute(q)
    return set((row.subscriber_tg_id, row.target_tg_id, row.event_type) for row in r.all())


async def enqueue_alerts_for_target(
    session: AsyncSession,
    target_tg_id: int,
    event_type: str,
    exclude_tg_id: int | None = None,
) -> None:
    """触发时：为所有订阅了 target 的用户写入待发提醒（排除 exclude_tg_id）；去重+限频，带 subscriber_lang。"""
    if event_type not in ("new_report", "blacklist_added", "report_spike", "wallet_change"):
        return
    since = None
    if ALERT_RATE_LIMIT_HOURS > 0:
        since = datetime.utcnow() - timedelta(hours=ALERT_RATE_LIMIT_HOURS)
    skip_set = await _alert_skip_set(session, target_tg_id, event_type, since)
    # risk_change（及 null）订阅者：仅对 new_report / blacklist_added 入队
    if event_type in ("new_report", "blacklist_added"):
        r = await session.execute(
            select(
                RiskAlertSubscription.subscriber_tg_id,
                RiskAlertSubscription.preferred_lang,
            ).where(
                RiskAlertSubscription.target_tg_id == target_tg_id,
                or_(
                    RiskAlertSubscription.alert_type == "risk_change",
                    RiskAlertSubscription.alert_type.is_(None),
                ),
            )
        )
        for row in r.all():
            subscriber_tg_id = row.subscriber_tg_id
            if exclude_tg_id is not None and subscriber_tg_id == exclude_tg_id:
                continue
            if (subscriber_tg_id, target_tg_id, event_type) in skip_set:
                continue
            lang = _normalize_alert_lang(row.preferred_lang) if getattr(row, "preferred_lang", None) else None
            session.add(
                RiskAlertPending(
                    subscriber_tg_id=subscriber_tg_id,
                    target_tg_id=target_tg_id,
                    event_type=event_type,
                    subscriber_lang=lang,
                )
            )
    # report_spike 订阅者：仅对 new_report 入队为 event_type report_spike
    if event_type == "new_report":
        r2 = await session.execute(
            select(
                RiskAlertSubscription.subscriber_tg_id,
                RiskAlertSubscription.preferred_lang,
            ).where(
                RiskAlertSubscription.target_tg_id == target_tg_id,
                RiskAlertSubscription.alert_type == "report_spike",
            )
        )
        ev = "report_spike"
        skip_spike = await _alert_skip_set(session, target_tg_id, ev, since)
        for row in r2.all():
            subscriber_tg_id = row.subscriber_tg_id
            if exclude_tg_id is not None and subscriber_tg_id == exclude_tg_id:
                continue
            if (subscriber_tg_id, target_tg_id, ev) in skip_spike:
                continue
            lang = _normalize_alert_lang(row.preferred_lang) if getattr(row, "preferred_lang", None) else None
            session.add(
                RiskAlertPending(
                    subscriber_tg_id=subscriber_tg_id,
                    target_tg_id=target_tg_id,
                    event_type=ev,
                    subscriber_lang=lang,
                )
            )
    await session.flush()


async def enqueue_wallet_change_for_target(
    session: AsyncSession,
    target_tg_id: int,
) -> None:
    """V2.0 当 target 关联新钱包时调用，为订阅了 wallet_change 的用户入队。"""
    event_type = "wallet_change"
    since = None
    if ALERT_RATE_LIMIT_HOURS > 0:
        since = datetime.utcnow() - timedelta(hours=ALERT_RATE_LIMIT_HOURS)
    skip_set = await _alert_skip_set(session, target_tg_id, event_type, since)
    r = await session.execute(
        select(
            RiskAlertSubscription.subscriber_tg_id,
            RiskAlertSubscription.preferred_lang,
        ).where(
            RiskAlertSubscription.target_tg_id == target_tg_id,
            RiskAlertSubscription.alert_type == "wallet_change",
        )
    )
    for row in r.all():
        sid = row.subscriber_tg_id
        if (sid, target_tg_id, event_type) in skip_set:
            continue
        lang = _normalize_alert_lang(row.preferred_lang) if getattr(row, "preferred_lang", None) else None
        session.add(
            RiskAlertPending(
                subscriber_tg_id=sid,
                target_tg_id=target_tg_id,
                event_type=event_type,
                subscriber_lang=lang,
            )
        )
    await session.flush()


ONLINE_STATUS_DEDUPE_MINUTES = 5


async def monitor_report_status(
    session: AsyncSession,
    target_tg_id: int,
    event_type: str,
    reporter_tg_id: int | None = None,
    monitor_tg_id: int | None = None,
    payload: dict | None = None,
) -> tuple[bool, str]:
    """
    监控端上报目标上下线。event_type 为 online 或 offline。
    去重：同 target 同 event_type 在 5 分钟内已有一条则跳过入队（仍写事件表）。
    返回 (是否入队了订阅者, "ok"|"dedupe"|"invalid")。
    """
    if event_type not in ("online", "offline"):
        return False, "invalid"
    now = datetime.utcnow()
    since = now - timedelta(minutes=ONLINE_STATUS_DEDUPE_MINUTES)
    skip_set = await _alert_skip_set(session, target_tg_id, event_type, since)
    session.add(
        OnlineStatusEvent(
            target_tg_id=target_tg_id,
            event_type=event_type,
            reporter_tg_id=reporter_tg_id,
            monitor_tg_id=monitor_tg_id,
            at_ts=now,
            payload=payload,
        )
    )
    await session.flush()
    r = await session.execute(
        select(
            RiskAlertSubscription.subscriber_tg_id,
            RiskAlertSubscription.preferred_lang,
        ).where(
            RiskAlertSubscription.target_tg_id == target_tg_id,
            RiskAlertSubscription.alert_type == "online_offline",
        )
    )
    enqueued = 0
    for row in r.all():
        sid = row.subscriber_tg_id
        if (sid, target_tg_id, event_type) in skip_set:
            continue
        lang = _normalize_alert_lang(row.preferred_lang) if getattr(row, "preferred_lang", None) else None
        session.add(
            RiskAlertPending(
                subscriber_tg_id=sid,
                target_tg_id=target_tg_id,
                event_type=event_type,
                subscriber_lang=lang,
            )
        )
        enqueued += 1
    await session.flush()
    return enqueued > 0, "ok" if enqueued else "dedupe"


async def get_pending_alerts(session: AsyncSession, limit: int = 50) -> list[dict[str, Any]]:
    """Bot 拉取待发送提醒（sent_at 为 NULL）；含 subscriber_lang 供按用户语言推送。"""
    r = await session.execute(
        select(
            RiskAlertPending.id,
            RiskAlertPending.subscriber_tg_id,
            RiskAlertPending.target_tg_id,
            RiskAlertPending.event_type,
            RiskAlertPending.subscriber_lang,
            RiskAlertPending.created_at,
        )
        .where(RiskAlertPending.sent_at.is_(None))
        .order_by(RiskAlertPending.created_at.asc())
        .limit(limit)
    )
    return [
        {
            "id": row.id,
            "subscriber_tg_id": row.subscriber_tg_id,
            "target_tg_id": row.target_tg_id,
            "event_type": row.event_type,
            "subscriber_lang": getattr(row, "subscriber_lang", None) or None,
            "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else None,
        }
        for row in r.all()
    ]


async def mark_alert_sent(session: AsyncSession, pending_id: int) -> bool:
    """Bot 发送成功后标记已发送。"""
    r = await session.execute(select(RiskAlertPending).where(RiskAlertPending.id == pending_id))
    row = r.scalar_one_or_none()
    if not row:
        return False
    row.sent_at = datetime.utcnow()
    await session.flush()
    return True


SHARE_EXPIRE_HOURS = 24


async def share_create(session: AsyncSession, owner_tg_id: int) -> dict[str, Any]:
    """Create one-time share link for 出示我的报告. Returns token, link, expires_in_hours."""
    token = "share_" + secrets.token_urlsafe(10)[:14]
    expires_at = datetime.utcnow() + timedelta(hours=SHARE_EXPIRE_HOURS)
    session.add(ReportShareToken(token=token, owner_tg_id=owner_tg_id, expires_at=expires_at))
    await session.flush()
    username = getattr(settings, "bot_username", "xyc2026_bot") or "xyc2026_bot"
    link = f"https://t.me/{username.lstrip('@')}?start={token}"
    return {"token": token, "link": link, "expires_in_hours": SHARE_EXPIRE_HOURS}


async def share_consume(session: AsyncSession, token: str) -> dict[str, Any] | None:
    """Validate share token and return report data for owner; mark used (one-time). Returns None if invalid/expired/used."""
    now = datetime.utcnow()
    r = await session.execute(
        select(ReportShareToken).where(
            ReportShareToken.token == token,
            ReportShareToken.expires_at > now,
            ReportShareToken.used_at.is_(None),
        )
    )
    row = r.scalar_one_or_none()
    if not row:
        return None
    row.used_at = now
    _activity_insert(session, row.owner_tg_id, "share_viewed")
    await session.flush()
    return await check_by_tg_id(session, row.owner_tg_id)


async def blacklist_add(
    session: AsyncSession,
    tg_id: int,
    reason: str | None = None,
    source: str = "admin",
) -> bool:
    """管理端：将 tg_id 加入黑名单；若已在黑名单则不再重复写入且不触发提醒。返回是否本次新加入。"""
    existing = await get_blacklist(session, tg_id)
    if existing is not None:
        return False
    session.add(
        Blacklist(
            tg_id=tg_id,
            reason=reason,
            source=source[:32] if source else "admin",
            status="confirmed",
        )
    )
    await session.flush()
    await get_or_create_user(session, tg_id)
    await enqueue_alerts_for_target(session, tg_id, "blacklist_added")
    return True


async def admin_stats(session: AsyncSession) -> dict[str, int]:
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    q_today = await session.execute(select(func.count(QueryLog.id)).where(QueryLog.created_at >= today_start))
    queries_today = q_today.scalar() or 0
    total_u = await session.execute(select(func.count(User.id)))
    total_users = total_u.scalar() or 0
    bl_count = await session.execute(select(func.count(Blacklist.tg_id)))
    blacklist_count = bl_count.scalar() or 0
    rp = await session.execute(select(func.count(Report.id)).where(Report.status == "pending"))
    reports_pending = rp.scalar() or 0
    fp = await session.execute(select(func.count(Feedback.id)).where(Feedback.status == "pending"))
    feedback_pending = fp.scalar() or 0
    tx_pending = await session.execute(
        select(func.count(Transaction.id)).where(Transaction.status == TransactionStatus.PENDING.value)
    )
    transactions_pending = tx_pending.scalar() or 0
    # 第三阶段：绑定与流水运营统计
    bw = await session.execute(select(func.count(UserBoundWallet.id)))
    bound_wallets_count = bw.scalar() or 0
    uv = await session.execute(
        select(UserBoundWallet.tg_id).where(UserBoundWallet.show_flow_to_public == True).distinct()
    )
    users_with_flow_visible = len(set(uv.scalars().all()))
    fs = await session.execute(select(func.count(WalletTransactionSnapshot.id)))
    flow_snapshots_count = fs.scalar() or 0
    return {
        "queries_today": queries_today,
        "total_users": total_users,
        "blacklist_count": blacklist_count,
        "reports_pending": reports_pending,
        "feedback_pending": feedback_pending,
        "transactions_pending": transactions_pending,
        "bound_wallets_count": bound_wallets_count,
        "users_with_flow_visible": users_with_flow_visible,
        "flow_snapshots_count": flow_snapshots_count,
    }


async def get_admin_reporters(session: AsyncSession, limit: int = 100) -> list[dict[str, Any]]:
    """管理后台：举报人列表。下一阶段优化：优先从 ReporterStats 读取，减少 Report 全表聚合。"""
    r = await session.execute(
        select(ReporterStats)
        .order_by(ReporterStats.total_reports.desc())
        .limit(limit)
    )
    rows = r.scalars().all()
    return [
        {
            "reporter_tg_id": rs.reporter_tg_id,
            "report_count": rs.total_reports,
            "verified_count": getattr(rs, "verified_count", 0) or 0,
            "last_at": (rs.last_report_at.isoformat() if rs.last_report_at else None),
        }
        for rs in rows
    ]


async def get_admin_transactions(
    session: AsyncSession,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """管理后台：交易列表，可选按状态筛选。"""
    q = (
        select(Transaction)
        .order_by(Transaction.created_at.desc())
        .limit(limit)
    )
    if status:
        q = q.where(Transaction.status == status)
    r = await session.execute(q)
    rows = r.scalars().all()
    return [
        {
            "id": t.id,
            "initiator_tg_id": t.initiator_tg_id,
            "counterparty_tg_id": t.counterparty_tg_id,
            "amount": t.amount,
            "currency": t.currency,
            "status": t.status,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "confirmed_at": t.confirmed_at.isoformat() if t.confirmed_at else None,
        }
        for t in rows
    ]


async def get_admin_reports(
    session: AsyncSession,
    status: str | None = None,
    reporter_tg_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """管理后台：举报列表，可选按状态/举报人筛选，分页。"""
    q = (
        select(Report)
        .order_by(Report.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status and status.strip():
        q = q.where(Report.status == status.strip())
    if reporter_tg_id is not None:
        q = q.where(Report.reporter_tg_id == reporter_tg_id)
    r = await session.execute(q)
    rows = r.scalars().all()
    return [
        {
            "id": x.id,
            "reporter_tg_id": x.reporter_tg_id,
            "target_tg_id": x.target_tg_id,
            "reason": (x.reason or "")[:500],
            "category": x.category or None,
            "evidence_data": x.evidence_data or None,
            "status": x.status,
            "created_at": x.created_at.isoformat() if x.created_at else None,
        }
        for x in rows
    ]


async def batch_update_report_status(
    session: AsyncSession, report_ids: list[int], status: str
) -> int:
    """管理后台：批量更新举报状态。返回实际更新条数。"""
    if status not in ("verified", "rejected"):
        return 0
    updated = 0
    for rid in report_ids:
        ok = await update_report_status(session, rid, status)
        if ok:
            updated += 1
    return updated


async def update_report_status(session: AsyncSession, report_id: int, status: str) -> bool:
    """管理后台：将举报状态更新为 verified 或 rejected。返回是否更新成功。P2: 同步更新 ReporterStats.verified_count/rejected_count。"""
    if status not in ("verified", "rejected"):
        return False
    r = await session.execute(select(Report).where(Report.id == report_id))
    row = r.scalar_one_or_none()
    if not row:
        return False
    reporter_id = row.reporter_tg_id
    row.status = status
    await session.flush()
    # P2: 更新举报人统计，供 scoring 权重使用
    rstat = await session.execute(select(ReporterStats).where(ReporterStats.reporter_tg_id == reporter_id))
    rs = rstat.scalar_one_or_none()
    if rs is None:
        rs = ReporterStats(reporter_tg_id=reporter_id, total_reports=0, verified_count=0, rejected_count=0)
        session.add(rs)
        await session.flush()
    if status == "verified":
        rs.verified_count = getattr(rs, "verified_count", 0) + 1
    else:
        rs.rejected_count = getattr(rs, "rejected_count", 0) + 1
    await session.flush()
    # 同步被举报人的 User.report_count（verified 数量）
    target_id = row.target_tg_id
    cnt = await session.execute(
        select(func.count(Report.id)).where(Report.target_tg_id == target_id, Report.status == "verified")
    )
    u = await get_user_by_tg_id(session, target_id)
    if u:
        u.report_count = cnt.scalar() or 0
        await session.flush()
    return True


async def get_admin_risk_trend(session: AsyncSession, days: int = 7) -> list[dict[str, Any]]:
    """管理后台：风险趋势（按日统计 query_log 的 result_risk_level 分布）。"""
    since = datetime.utcnow().date() - timedelta(days=days)
    since_ts = datetime.combine(since, datetime.min.time().replace(tzinfo=None))
    r = await session.execute(
        select(
            func.date(QueryLog.created_at).label("dt"),
            QueryLog.result_risk_level,
            func.count(QueryLog.id).label("cnt"),
        )
        .where(QueryLog.created_at >= since_ts)
        .group_by(func.date(QueryLog.created_at), QueryLog.result_risk_level)
    )
    by_date: dict[str, dict[str, int]] = {}
    for row in r.all():
        d = row.dt.isoformat() if hasattr(row.dt, "isoformat") else str(row.dt)
        if d not in by_date:
            by_date[d] = {"low": 0, "medium": 0, "high": 0, "extreme": 0}
        level = (row.result_risk_level or "medium").lower()
        if level in by_date[d]:
            by_date[d][level] = row.cnt
        else:
            by_date[d]["medium"] += row.cnt
    return [{"date": d, **counts} for d, counts in sorted(by_date.items())]
