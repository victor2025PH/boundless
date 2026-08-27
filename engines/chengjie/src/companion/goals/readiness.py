"""获客增长环开闸就绪度（纯聚合，零副作用）。

运营常问「苏婉漏斗能不能跑」——答案散落在 overlay 开关、人设 allowlist、
账号绑定、目录文件、订单回流配置里。本模块一次聚成「就绪 / 卡在哪 / 下一步」，
供 ops 卡与 ``GET /api/goals/readiness`` 消费。绝不抛。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.companion.goals.service import resolve_goals_cfg


def _flag(d: Any, *keys: str, default: bool = False) -> bool:
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return bool(cur) if cur is not None else default


def _persona_exists(persona_id: str) -> bool:
    """人设是否在档：PersonaManager 运行时优先；冷启/CLI 回落读
    ``profiles_runtime.yaml``（避免「人明明在库里却报 missing」假卡点）。"""
    pid = str(persona_id or "").strip()
    if not pid:
        return False
    try:
        from src.utils.persona_manager import PersonaManager
        if PersonaManager.get_instance().get_persona_by_id(pid) is not None:
            return True
    except Exception:
        pass
    try:
        from pathlib import Path
        import yaml
        # 引擎根 config/profiles_runtime.yaml（与 PersonaManager 同路径约定）
        for cand in (
            Path("config") / "profiles_runtime.yaml",
            Path(__file__).resolve().parents[3] / "config" / "profiles_runtime.yaml",
        ):
            if not cand.is_file():
                continue
            data = yaml.safe_load(cand.read_text(encoding="utf-8")) or {}
            profiles = data.get("profiles") or data
            if isinstance(profiles, dict) and pid in profiles:
                return True
            if isinstance(profiles, list):
                for p in profiles:
                    if isinstance(p, dict) and str(p.get("id") or "") == pid:
                        return True
    except Exception:
        pass
    return False


def _iter_account_personas(
    cfg_root: Any,
) -> List[Dict[str, str]]:
    """扫 registry：每账号带 platform/account_id/persona_id/status/label。
    removed 已由 list() 默认剔除。失败 → []。"""
    out: List[Dict[str, str]] = []
    try:
        from src.ai.persona_voice import resolve_account_persona_id
        from src.integrations.account_registry import get_account_registry
        reg = get_account_registry()
        rows = reg.list() if reg is not None else []
    except Exception:
        return []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        plat = str(row.get("platform") or "").strip()
        acct = str(row.get("account_id") or "").strip()
        if not plat or not acct:
            continue
        try:
            pid = resolve_account_persona_id(cfg_root or {}, plat, acct)
        except Exception:
            pid = ""
        out.append({
            "platform": plat,
            "account_id": acct,
            "persona_id": str(pid or ""),
            "status": str(row.get("status") or ""),
            "label": str(row.get("label") or acct),
        })
    return out


def _bound_accounts(
    cfg_root: Any, allowlist: List[str], *, limit: int = 20,
) -> List[Dict[str, str]]:
    """列出生效人设落在 allowlist 内的账号。"""
    if not allowlist:
        return []
    wanted = set(allowlist)
    out: List[Dict[str, str]] = []
    for row in _iter_account_personas(cfg_root):
        if row.get("persona_id") in wanted:
            out.append({
                "platform": row["platform"],
                "account_id": row["account_id"],
                "persona_id": row["persona_id"],
            })
            if len(out) >= max(1, int(limit)):
                break
    return out


def _short_label(label: str, *, platform: str = "", keep: int = 14) -> str:
    """账号显示名收短：``messenger:Uk4bhj…kQOg``（长 id 掐头留尾，短名原样）。"""
    s = str(label or "").strip()
    p = str(platform or "").strip()
    if len(s) > keep:
        s = f"{s[:6]}…{s[-4:]}"
    return f"{p}:{s}" if p else s


def _bind_candidates(
    cfg_root: Any, allowlist: List[str], *, limit: int = 8,
) -> List[Dict[str, str]]:
    """待换绑候选：已登记、人设**不在** allowlist 的账号（运营可一键换绑目标人设）。

    优先 online，其次其余非 removed；同序保留 registry 原序。空 allowlist → []。
    """
    if not allowlist:
        return []
    wanted = set(allowlist)
    online: List[Dict[str, str]] = []
    other: List[Dict[str, str]] = []
    for row in _iter_account_personas(cfg_root):
        if row.get("persona_id") in wanted:
            continue
        suggest = allowlist[0]
        label = row.get("label") or row["account_id"]
        item = {
            "platform": row["platform"],
            "account_id": row["account_id"],
            "persona_id": row.get("persona_id") or "",
            "status": row.get("status") or "",
            "label": label,
            # 无昵称的账号 label＝一长串平台 id，整串进 hint 没法读也没法核对；
            # 掐头留尾（首尾各留几位足以人工比对）
            "short": _short_label(label, platform=row["platform"]),
            "suggest": suggest,
            # P12：人设页深链（#profile= 已支持打开抽屉）
            "href": f"/personas#profile={suggest}",
        }
        if item["status"] == "online":
            online.append(item)
        else:
            other.append(item)
    merged = online + other
    return merged[: max(1, int(limit))]


def _catalog_ok(cfg_root: Any) -> bool:
    try:
        from src.companion.goals import site_catalog as sc
        catalog = sc.load_catalog(sc.catalog_path(cfg_root, None))
        products = catalog.get("products") or []
        site = catalog.get("site") or {}
        return bool(products) and bool(site.get("base_url"))
    except Exception:
        return False


def _offers_summary(cfg_root: Any) -> Dict[str, Any]:
    """运营授权活动（P13）现状：有效条数 + 最近到期（天）。异常 → 空态。"""
    out: Dict[str, Any] = {"active": 0, "soonest_id": "", "soonest_days": -1}
    try:
        import time as _t
        from src.companion.goals import offers as offers_mod
        from src.companion.goals import site_catalog as sc
        catalog = sc.load_catalog(sc.catalog_path(cfg_root, None))
        items = offers_mod.active_offers(catalog)
        out["active"] = len(items)
        if items:
            soonest = min(items, key=lambda o: o["valid_until_ts"])
            out["soonest_id"] = soonest.get("id") or soonest.get("label") or ""
            out["soonest_days"] = max(
                0, int((float(soonest["valid_until_ts"]) - _t.time()) // 86400))
    except Exception:
        pass
    return out


def growth_readiness(cfg_root: Any) -> Dict[str, Any]:
    """聚合开闸就绪快照。

    ``status``：
    - ``disabled`` —— goals 总闸关
    - ``waiting_bind`` —— 自动建已开但 allowlist 无人设绑定账号（最常见卡点）
    - ``partial`` —— 主链可跑，留存/订单等下游未齐
    - ``ready`` —— 获客→成交→留存→挽回→回流主链配置齐且有绑定账号
    """
    cfg = resolve_goals_cfg(cfg_root)
    goals_on = bool(cfg.get("enabled", False))
    ac = cfg.get("auto_create") if isinstance(cfg.get("auto_create"), dict) else {}
    ac_on = bool(ac.get("enabled", False))
    personas = [str(x).strip() for x in (ac.get("personas") or [])
                if str(x).strip()]
    platforms = [str(x).strip().lower() for x in (ac.get("platforms") or [])
                 if str(x).strip()]
    persona_ok = {p: _persona_exists(p) for p in personas}
    bound = _bound_accounts(cfg_root, personas) if (goals_on and ac_on) else []
    # P11：待绑时可操作候选——列「现在绑在别人设上的账号 + 建议换绑目标」，
    # 避免运维只看见「去人设页绑定」却不知道绑哪个号。
    candidates = (
        _bind_candidates(cfg_root, personas) if (goals_on and ac_on and not bound)
        else [])
    # P15 首跑监测：自动获客目标**历史上有没有诞生过**（读库跨重启稳——进程
    # 计数 auto_created 一重启就归零，「绑完到底触发没有」得看持久事实）。
    auto_goals_ever = -1
    if goals_on and ac_on:
        try:
            from src.companion.goals.service import get_configured_store
            auto_goals_ever = int(get_configured_store(
                cfg_root, None).count_created_by_since("auto_create", 0.0))
        except Exception:
            auto_goals_ever = -1

    ret = cfg.get("retention") if isinstance(cfg.get("retention"), dict) else {}
    ret_on = bool(ret.get("enabled", False))
    wb = ret.get("winback") if isinstance(ret.get("winback"), dict) else {}
    wb_on = bool(wb.get("enabled", False))
    rcv = wb.get("reconvert") if isinstance(wb.get("reconvert"), dict) else {}
    rcv_on = bool(rcv.get("enabled", False))

    hook = cfg.get("order_hook") if isinstance(cfg.get("order_hook"), dict) else {}
    pull = cfg.get("order_pull") if isinstance(cfg.get("order_pull"), dict) else {}
    hook_on = bool(hook.get("enabled", False)) and bool(
        str(hook.get("token") or "").strip())
    pull_on = bool(pull.get("enabled", False)) and bool(
        str(pull.get("admin_key") or "").strip()) and bool(
        str(pull.get("site_url") or "").strip())
    order_ok = hook_on or pull_on
    catalog_ok = _catalog_ok(cfg_root) if goals_on else False
    llm = cfg.get("profile_llm") if isinstance(cfg.get("profile_llm"), dict) else {}

    blockers: List[str] = []
    hints: List[str] = []
    if not goals_on:
        blockers.append("goals_disabled")
        hints.append("overlay 开 companion.goals.enabled")
    else:
        if not ac_on:
            blockers.append("auto_create_disabled")
            hints.append("overlay 开 companion.goals.auto_create.enabled")
        elif not personas:
            blockers.append("personas_allowlist_empty")
            hints.append("auto_create.personas 填入获客人设（如 su_wan）")
        else:
            missing = [p for p, ok in persona_ok.items() if not ok]
            if missing:
                blockers.append("persona_missing:" + ",".join(missing))
                hints.append("人设不存在：检查 profiles_runtime / 人设页")
            if not bound:
                blockers.append("no_account_bound")
                target = "/".join(personas) if personas else "获客人设"
                if candidates:
                    # 首条候选写进 hint：平台:号(当前人设) → 建议人设
                    c0 = candidates[0]
                    who = c0.get("short") or f"{c0.get('platform')}:{c0.get('account_id')}"
                    cur = c0.get("persona_id") or "未绑"
                    hints.append(
                        f"人设页把 {who}（现 {cur}）换绑到 {target}"
                        f"——否则 auto_create 永远不触发"
                        + (f"；另有 {len(candidates)-1} 个可换绑账号"
                           if len(candidates) > 1 else ""))
                else:
                    hints.append(
                        f"在人设页给至少一个账号绑定 {target}"
                        "——否则 auto_create 永远不触发")
            elif auto_goals_ever == 0:
                # 绑好了但一单目标都没诞生过 → 运营最想知道的是「等就行还是坏了」
                hints.append(
                    "账号已绑定但还没有自动获客目标诞生——auto_create 在该号"
                    "下一条私聊入站时触发（从没建过目标的老会话也会补建），"
                    "等新消息进线即可；持续为 0 检查 platforms 过滤与入站链路")
        if not catalog_ok:
            blockers.append("catalog_missing")
            hints.append("确认 site_catalog.yaml 有 products + site.base_url")
        if not order_ok:
            blockers.append("order_channel_off")
            hints.append("开 order_hook（带 token）或 order_pull（带 site_url+admin_key）")
        if not ret_on:
            hints.append("留存环未开（成交后不会自动起续费目标）")
        elif not wb_on:
            hints.append("流失挽回未开（留存过期后不会自动挽回）")
        elif not rcv_on:
            hints.append("回流再转化未开（挽回回话后不会自动转卖）")
        # P3 2026-08-18：摸底自动化组合警告——正则轨只覆盖结构化表达
        # （年龄/职业/坐标等），「兴趣」这类开放槽主要靠 LLM 抽取；LLM 轨
        # 关着时摸底目标能推进但兴趣槽基本要人工补录，如实提示而非装满血。
        if not bool(llm.get("enabled", False)):
            hints.append(
                "profile_llm 未开：摸底目标的开放槽位（兴趣等）主要靠人工"
                "补录（正则只认高置信结构化自述）——建议开 "
                "companion.goals.profile_llm 提升摸底自动化率")

    # 主链可跑：总闸 + 自动建 + allowlist 非空 + 人设在档 + 有绑定账号
    acquire_ready = bool(
        goals_on and ac_on and personas
        and all(persona_ok.values()) and bound)
    full_ready = bool(
        acquire_ready and catalog_ok and order_ok
        and ret_on and wb_on and rcv_on)

    if not goals_on or (not ac_on and "auto_create_disabled" in blockers):
        status = "disabled"
    elif "no_account_bound" in blockers or "personas_allowlist_empty" in blockers \
            or any(b.startswith("persona_missing") for b in blockers):
        status = "waiting_bind"
    elif full_ready:
        status = "ready"
    elif acquire_ready:
        status = "partial"
    else:
        status = "disabled"

    personas_href = (
        f"/personas#profile={personas[0]}" if personas else "/personas")
    return {
        "status": status,
        "ready": full_ready,
        "acquire_ready": acquire_ready,
        "personas_href": personas_href,
        "checks": {
            "goals_enabled": goals_on,
            "auto_create_enabled": ac_on,
            "personas_allowlist": personas,
            "platforms_filter": platforms,
            "persona_exists": persona_ok,
            "bound_accounts": bound,
            "bound_count": len(bound),
            "bind_candidates": candidates,
            "auto_goals_ever": auto_goals_ever,
            "catalog_ok": catalog_ok,
            "offers": _offers_summary(cfg_root) if goals_on else {
                "active": 0, "soonest_id": "", "soonest_days": -1},
            "order_hook": hook_on,
            "order_pull": pull_on,
            "order_channel_ok": order_ok,
            "profile_llm": bool(llm.get("enabled", False)),
            "retention": ret_on,
            "winback": wb_on,
            "reconvert": rcv_on,
        },
        "blockers": blockers,
        "hints": hints,
    }


__all__ = ["growth_readiness"]
