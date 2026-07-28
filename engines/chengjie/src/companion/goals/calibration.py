"""获客增长环校准建议（纯函数，零副作用）。

把 readiness / 报表 / 进程计数收成「下一步干什么」——运营不必自己拼
「待绑 + 都嫌贵 + 转向 0」三处读数。不发明折扣；定价权限仍归官网/运营。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def growth_calibration(
    *,
    readiness: Optional[Dict[str, Any]] = None,
    report: Optional[Dict[str, Any]] = None,
    stats: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """聚合校准快照。

    返回 ``{priority, hints, focus}``：
    - ``priority``：bind / traffic / churn_price / churn_trust / healthy / idle
    - ``hints``：≤5 条可执行中文建议（含人设深链提示）
    - ``focus``：主因流失标签（有矩阵时）或空
    """
    ready = readiness if isinstance(readiness, dict) else {}
    rep = report if isinstance(report, dict) else {}
    st = stats if isinstance(stats, dict) else {}
    hints: List[str] = []
    priority = "idle"
    focus = ""

    status = str(ready.get("status") or "")
    checks = ready.get("checks") if isinstance(ready.get("checks"), dict) else {}
    blockers = list(ready.get("blockers") or [])

    # 1) 开闸卡点优先于一切读数
    if status == "waiting_bind" or "no_account_bound" in blockers:
        priority = "bind"
        allow = list(checks.get("personas_allowlist") or [])
        target = allow[0] if allow else "获客人设"
        cands = list(checks.get("bind_candidates") or [])
        href = f"/personas#profile={target}"
        if cands:
            c0 = cands[0] if isinstance(cands[0], dict) else {}
            who = (c0.get("short") or c0.get("label")
                   or c0.get("account_id") or "?")
            hints.append(
                f"先到人设页把「{who}」换绑到 {target}（{href}），"
                "否则 auto_create 不触发")
        else:
            hints.append(
                f"先到人设页给任一账号绑定 {target}（{href}）")
        return {"priority": priority, "hints": hints[:5], "focus": focus,
                "personas_href": href}

    if status in ("disabled", "") and "goals_disabled" in blockers:
        priority = "bind"
        hints.append("overlay 开 companion.goals.enabled 后再看漏斗读数")
        return {"priority": priority, "hints": hints[:5], "focus": focus,
                "personas_href": ""}

    # 2) 生命周期矩阵 → 主因对症
    outcomes = rep.get("churn_outcomes") if isinstance(
        rep.get("churn_outcomes"), dict) else {}
    if outcomes:
        # 取样本量最大的有标签主因（跳过未采）
        ranked = [(k, v) for k, v in outcomes.items()
                  if k and k != "(未采)" and isinstance(v, dict)]
        ranked.sort(key=lambda kv: -int(kv[1].get("n") or 0))
        if ranked:
            focus, top = ranked[0]
            n = int(top.get("n") or 0)
            won_rate = float(top.get("won_rate") or 0.0)
            if n >= 3 and won_rate < 0.15:
                if focus in ("太贵", "预算紧张"):
                    priority = "churn_price"
                    offers = (checks.get("offers")
                              if isinstance(checks.get("offers"), dict) else {})
                    n_off = int(offers.get("active") or 0)
                    if n_off:
                        cited = int(st.get("offer_cited") or 0)
                        hints.append(
                            f"主因「{focus}」成交率偏低（won {won_rate:.0%} / n={n}）"
                            f"——已有 {n_off} 条授权活动"
                            + (f"、引用 {cited} 次；看是活动力度不够还是没投到人"
                               if cited else "但一次都没被引用；确认活动 for_churn "
                                             "是否覆盖该主因、且会话走到 order/cs 档"))
                    else:
                        hints.append(
                            f"主因「{focus}」成交率偏低（won {won_rate:.0%} / n={n}）"
                            "——引擎已入门档转向；若仍低，把官网真实在售活动登记到 "
                            "site_catalog.yaml 的 offers 段（引擎只转述，不发明折扣码）")
                elif focus in ("没用起来", "效果不佳", "出了问题"):
                    priority = "churn_trust"
                    hints.append(
                        f"主因「{focus}」成交偏低（won {won_rate:.0%} / n={n}）"
                        "——检查客服收口是否到位，补 onboarding/稳定性话术")
                else:
                    priority = "churn_price"
                    hints.append(
                        f"主因「{focus}」成交偏低（won {won_rate:.0%} / n={n}）"
                        "——对照 ops 转向计数与目录 CTA 分布")

    # 3) 转向/注入健康
    steered = int(st.get("churn_steered") or 0)
    captured = int(st.get("churn_captured") or 0)
    catalog_n = int(st.get("catalog_injected") or 0)
    if captured >= 3 and steered == 0 and catalog_n > 0:
        hints.append(
            "已采到流失原因但转向计数为 0——确认生命周期目标（留存/挽回/回流）"
            "是否走到 soft/direct 日且模板挂了 catalog")
    offers = (checks.get("offers")
              if isinstance(checks.get("offers"), dict) else {})
    days = int(offers.get("soonest_days", -1) or 0) if offers else -1
    if int(offers.get("active") or 0) > 0 and 0 <= days <= 3:
        hints.append(
            f"授权活动「{offers.get('soonest_id') or '?'}」还有 {days} 天到期"
            "——到期后引擎自动停止引用；要续期改 offers.valid_until")

    inj = st.get("injected") if isinstance(st.get("injected"), dict) else {}
    inj_total = int(inj.get("total") or 0)
    # 首跑判定优先读库口径（checks.auto_goals_ever 跨重启稳）：0=真的从没
    # 触发过；-1/缺失=读不到库，回落进程计数（重启后会短暂误报，可接受）
    _ever = checks.get("auto_goals_ever")
    ever = int(_ever) if isinstance(_ever, (int, float)) else -1
    never_fired = (ever == 0) if ever >= 0 else (
        int(st.get("auto_created") or 0) == 0 and inj_total == 0)
    if status in ("ready", "partial") and never_fired:
        if priority == "idle":
            priority = "traffic"
        hints.append(
            "配置已齐但还没有自动获客目标诞生过——等绑定号新私聊进线"
            "（下一条入站即触发），或检查 auto_create.personas/platforms "
            "与账号人设是否一致")

    if not hints and status == "ready":
        priority = "healthy"
        hints.append("主链就绪；周审看 churn_outcomes 主因×won_rate 再调节奏")

    if not hints:
        hints.extend(list(ready.get("hints") or [])[:2])

    allow = list(checks.get("personas_allowlist") or [])
    href = f"/personas#profile={allow[0]}" if allow else "/personas"
    return {
        "priority": priority,
        "hints": hints[:5],
        "focus": focus,
        "personas_href": href,
    }


__all__ = ["growth_calibration"]
