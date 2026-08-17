# Build check result message body (risk, score, tips, details, verdict, disclaimer)
import hashlib
import secrets
from datetime import datetime, timezone

from bot.i18n import t, translate_tip


def _report_id() -> str:
    """Short unique report id: R + YYYYMMDDHHMM + 2 random chars."""
    now = datetime.now(timezone.utc)
    tail = secrets.token_hex(1).upper()
    return f"R{now.strftime('%Y%m%d%H%M')}-{tail}"


def _md_esc(s: str) -> str:
    """Escape Markdown special chars so user-provided text doesn't break parse."""
    return (s or "").replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*")


def format_check_result(data: dict, locale: str, display_name: str | None = None) -> str:
    """One place to build the full result card text from API data. display_name for invite flow (first+last name)."""
    score = data.get("score", 50)
    risk = data.get("risk", "medium")
    risk_label = t(f"risk_{risk}", lang=locale) if risk in ("low", "medium", "high", "extreme") else risk
    tips = data.get("tips") or []
    tips_translated = [translate_tip(tip, locale) for tip in tips]
    tips_str = "• " + "\n• ".join(tips_translated) if tips_translated else t("tip_no_indicators", lang=locale)
    blacklist = data.get("blacklist", False)
    first_seen = data.get("first_seen")
    last_seen_at = data.get("last_seen_at")
    report_count = data.get("report_count", 0)
    report_pending = data.get("report_pending", 0)
    report_verified = data.get("report_verified", 0)
    is_premium = data.get("is_premium", False)
    query_count = data.get("query_count", 0)
    updated_at = data.get("updated_at")
    verdict_key = data.get("verdict") or "verdict_medium"
    tags = data.get("tags") or []
    tg_id = data.get("tg_id")
    username = data.get("username")

    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%d %H:%M")
    report_no = _report_id()

    body = ""
    # Report header (专业版面)
    body += f"**{t('report_no_label', lang=locale)}**: `{report_no}`\n"
    body += f"**{t('report_generated_label', lang=locale)}**: {generated_at}{t('report_time_utc', lang=locale)}\n"
    body += f"_{t('report_usage_hint', lang=locale)}_\n\n"
    # 1. 被查用户 [Official = TG API]
    if tg_id is not None:
        identity_lines = [f"**{t('report_user_id', lang=locale)}**: `{tg_id}`"]
        if username:
            identity_lines.append(f"**{t('report_username', lang=locale)}**: @{_md_esc(username)}")
        if display_name and display_name.strip():
            identity_lines.append(f"**{t('report_display_name', lang=locale)}**: {_md_esc(display_name.strip())}")
        body += f"**{t('report_identity_title', lang=locale)}** {t('source_official', lang=locale)}\n" + "\n".join(identity_lines) + "\n\n"
    if is_premium:
        body += f"• {t('source_official', lang=locale)} {t('detail_premium', lang=locale)} ✓\n\n"

    # 2. 信用结论
    body += f"**{t('section_credit_result', lang=locale)}**\n"
    body += f"{t('result_risk', lang=locale, risk=risk_label)}\n"
    body += f"{t('result_score', lang=locale, score=score)}\n"
    body += f"{t('result_tips', lang=locale, tips=tips_str)}\n\n"
    if blacklist:
        body += f"⚠️ {t('source_crowd', lang=locale)} {t('result_blacklist', lang=locale)}\n\n"

    # 3. 综合建议 + 下一步建议（可执行）
    body += f"**{t('section_verdict', lang=locale)}**\n{t(verdict_key, lang=locale)}\n\n"
    next_step_key = f"next_step_{risk}" if risk in ("extreme", "high", "medium", "low") else "next_step_medium"
    body += f"▶ {t(next_step_key, lang=locale)}\n\n"
    body += f"_{t('risk_basis', lang=locale)}_\n\n"

    # 阶段 C：四维度风险（身份稳定性、资金实力、社区评价、关联风险）
    risk_dimensions = data.get("risk_dimensions") or {}
    if risk_dimensions:
        body += f"**{t('section_risk_dimensions', lang=locale)}**\n"
        dim_names = (("identity", "dim_identity"), ("funds", "dim_funds"), ("community", "dim_community"), ("association", "dim_association"))
        for key, label_key in dim_names:
            level = risk_dimensions.get(key, "unknown")
            level_label = t(f"dim_level_{level}", lang=locale) if level in ("high", "medium", "low", "unverified", "unknown") else str(level)
            body += f"• {t(label_key, lang=locale)}: {level_label}\n"
        body += "\n"

    # 4. 标签（无标签时也保留区块，版面一致）
    tag_labels = [t(k, lang=locale) for k in tags if k.startswith("tag_")] if tags else []
    body += f"**{t('section_tags', lang=locale)}** {', '.join(tag_labels) if tag_labels else t('no_tags_placeholder', lang=locale)}\n\n"

    # 关联钱包 / 该钱包关联多账号（实用：换号不换地址）
    linked_wallets = data.get("linked_wallets") or []
    linked_tg_ids = data.get("linked_tg_ids") or []
    if linked_wallets:
        body += f"**{t('section_linked_wallets', lang=locale)}** {t('source_on_chain', lang=locale)}\n• " + "\n• ".join(linked_wallets) + "\n\n"
    if len(linked_tg_ids) > 1 and tg_id is not None:
        other_ids = [x for x in linked_tg_ids if x != tg_id]
        if other_ids:
            body += f"**{t('section_wallet_other_accounts', lang=locale)}** {t('source_on_chain', lang=locale)}\n`" + "`, `".join(str(i) for i in other_ids) + "`\n\n"

    # V2.0 站内交易确认统计（与链上流水区分：此处为双方在本应用内确认的笔数）
    tx_total = data.get("tx_total_count", 0)
    if tx_total and tx_total > 0:
        tx_ok = data.get("tx_success_count", 0)
        tx_rate = data.get("tx_success_rate", 0)
        tx_amount = data.get("tx_total_amount", 0)
        body += f"**{t('section_tx_stats', lang=locale)}**\n"
        body += f"_{t('section_tx_stats_subtitle', lang=locale)}_\n"
        body += f"• {t('tx_stats_line', lang=locale, count=tx_ok, total=tx_total, rate=tx_rate, amount=tx_amount)}\n\n"

    # 谁查过我（仅我的信用返回）
    if "recent_7d_query_count" in data or data.get("last_queried_at"):
        body += f"**{t('section_who_queried_me', lang=locale)}**\n"
        body += f"• {t('who_queried_me_7d_count', lang=locale, count=data.get('recent_7d_query_count', 0))}\n"
        if data.get("last_queried_at"):
            body += f"• {t('who_queried_me_last_time', lang=locale, time=data['last_queried_at'])}\n"
        body += "\n"

    # 最近活动时间线（按日）
    timeline = data.get("timeline") or []
    if timeline:
        body += f"**{t('section_timeline', lang=locale)}**\n"
        for day in timeline:
            d = day if isinstance(day, dict) else {}
            date_str = d.get("date", "")
            q = d.get("queried", 0)
            inv = d.get("invite_clicked", 0)
            sh = d.get("share_viewed", 0)
            parts = []
            if q:
                parts.append(t("timeline_queried", lang=locale, n=q))
            if inv:
                parts.append(t("timeline_invite_clicked", lang=locale, n=inv))
            if sh:
                parts.append(t("timeline_share_viewed", lang=locale, n=sh))
            if parts and date_str:
                body += f"• {date_str}: " + "；".join(parts) + "\n"
        body += "\n"

    # 5. 详细信息（多源标识：Official/Crowd/Network/On-Chain）
    body += f"**{t('section_details', lang=locale)}**\n"
    if first_seen:
        body += f"• {t('source_official', lang=locale)} {t('detail_first_seen', lang=locale)}: {first_seen} {t('detail_first_seen_note', lang=locale)}\n"
    body += f"• {t('source_crowd', lang=locale)} {t('detail_reports_breakdown', lang=locale, total=report_count, pending=report_pending, verified=report_verified)}\n"
    # V2.0: 举报分类（已核实）与近 7/30 天
    report_by_category = data.get("report_by_category") or {}
    last_7d = data.get("last_7d_reports", 0)
    last_30d = data.get("last_30d_reports", 0)
    if report_by_category or last_7d or last_30d:
        if report_by_category:
            cat_parts = [f"{t('report_category_' + k, lang=locale)}: {v}" for k, v in report_by_category.items() if v]
            if cat_parts:
                body += f"• {t('source_crowd', lang=locale)} {t('section_report_by_category', lang=locale)}: " + ", ".join(cat_parts) + "\n"
        if last_7d or last_30d:
            body += f"• {t('source_crowd', lang=locale)} {t('report_last_7d', lang=locale)}: {last_7d}  |  {t('report_last_30d', lang=locale)}: {last_30d}\n"
    # V2.0: 风险来源占比（用中文来源标识）
    risk_breakdown = data.get("risk_factor_breakdown")
    if isinstance(risk_breakdown, dict) and any(risk_breakdown.get(k) for k in ("report", "blacklist", "high_risk_groups", "new_account", "wallet_anomaly")):
        parts_br = []
        key_label = (("report", "risk_src_report", "source_crowd"), ("blacklist", "risk_src_blacklist", "source_crowd"), ("high_risk_groups", "risk_src_high_risk_groups", "source_network"), ("new_account", "risk_src_new_account", "source_official"), ("wallet_anomaly", "risk_src_wallet_anomaly", "source_on_chain"))
        for key, i18n_key, src_key in key_label:
            pct = risk_breakdown.get(key, 0) or 0
            if pct:
                parts_br.append(f"{t(src_key, lang=locale)} {t(i18n_key, lang=locale)} {pct}%")
        if parts_br:
            body += f"• {t('section_risk_breakdown', lang=locale)}: " + ", ".join(parts_br) + "\n"
    body += f"• {t('source_crowd', lang=locale)} {t('detail_blacklist', lang=locale)}: {t('detail_blacklist_yes' if blacklist else 'detail_blacklist_no', lang=locale)}\n"
    body += f"• {t('detail_query_count', lang=locale)}: {query_count}\n"
    if updated_at:
        body += f"• {t('detail_updated_at', lang=locale)}: {updated_at}\n"
    if last_seen_at:
        body += f"• {t('detail_last_seen', lang=locale)}: {last_seen_at}\n"
    # 6. 数据说明
    body += f"\n**{t('data_disclaimer_header', lang=locale)}**\n_{t('detail_privacy_note', lang=locale)}_\n\n"
    # 7. 结尾
    body += t("result_disclaimer", lang=locale)
    return body


def format_sharecard(data: dict, locale: str) -> str:
    """Build sharecard text from sharecard API data. Order: header → risk/score → backing → tags → disclaimer/verify."""
    risk = data.get("risk", "medium")
    score = data.get("score", 50)
    risk_key = f"sharecard_risk_{risk}" if risk in ("low", "medium", "high", "extreme") else None
    risk_label = t(risk_key, lang=locale) if risk_key else risk
    lines = [
        t("sharecard_title", lang=locale),
    ]
    label = data.get("label") or ""
    if label:
        role_key = "sharecard_role_merchant" if (label.strip().lower() in ("merchant", "商户")) else "sharecard_role_user"
        lines.append(t(role_key, lang=locale))
    updated_at = data.get("updated_at")
    if updated_at:
        lines.append(t("sharecard_updated", lang=locale, at=updated_at))
    lines.append("")
    lines.append(t("sharecard_line_risk", lang=locale, risk=risk_label))
    score_line = t("sharecard_line_score", lang=locale, score=score)
    if score >= 80:
        score_line += " " + t("sharecard_score_hint_high", lang=locale)
    elif score >= 56:
        score_line += " " + t("sharecard_score_hint_mid", lang=locale)
    else:
        score_line += " " + t("sharecard_score_hint_low", lang=locale)
    lines.append(score_line)
    lines.append("")
    first_seen = data.get("first_seen")
    if first_seen:
        lines.append(t("sharecard_line_age", lang=locale, date=first_seen))
    tx_total = data.get("tx_total_count", 0)
    if tx_total and tx_total > 0:
        lines.append(t("sharecard_section_tx_label", lang=locale) + ": " + t("sharecard_line_tx", lang=locale, count=data.get("tx_success_count", 0), rate=data.get("tx_success_rate", 0)))
    else:
        lines.append(t("sharecard_section_tx_label", lang=locale) + ": " + t("sharecard_line_tx_none", lang=locale))
    if data.get("has_flow_visible"):
        if data.get("flow_verified"):
            lines.append(t("sharecard_line_flow_verified", lang=locale))
        else:
            lines.append(t("sharecard_line_flow_unverified", lang=locale))
    report_count = data.get("report_count", 0)
    report_verified = data.get("report_verified", 0)
    bl = t("detail_blacklist_yes", lang=locale) if data.get("blacklist") else t("detail_blacklist_no", lang=locale)
    lines.append(t("sharecard_line_crowd", lang=locale, total=report_count, verified=report_verified, bl=bl))
    tags = data.get("tags") or []
    tag_labels = [t(k, lang=locale) for k in tags if isinstance(k, str) and k.startswith("tag_")]
    if tag_labels:
        lines.append(t("sharecard_tags_intro", lang=locale) + " · ".join(tag_labels))
    else:
        lines.append(t("sharecard_tags_intro", lang=locale) + t("sharecard_tags_none", lang=locale))
    lines.append("")
    lines.append(t("sharecard_disclaimer", lang=locale))
    lines.append(t("sharecard_verify_hint", lang=locale))
    return "\n".join(lines)


async def append_report_footer(body: str, tg_id: int | None, locale: str) -> str:
    """阶段 A：当有 tg_id 时注册报告快照并追加验真码尾注；无 tg_id 则返回原 body。"""
    if tg_id is None:
        return body
    try:
        from bot.api_client import report_register
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        report_hash = await report_register(tg_id, content_hash)
        footer = "\n\n" + t("report_footer_verify", lang=locale, hash=report_hash)
        return body + footer
    except Exception:
        return body
