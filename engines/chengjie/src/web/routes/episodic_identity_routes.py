"""情景记忆 + 跨平台身份 API 路由（Phase E1 续拆，从 admin.py 抽出）。

两者同域：CrossPlatformIdentity 的 link/unlink 正是为了让多平台 UID 共享同一份
情景记忆。仅迁移 API 端点（页面路由因需 templates 仍留 admin.py，与既有约定一致）。
行为与抽出前一致；依赖经 AdminRouteContext 注入。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from fastapi import HTTPException, Request
from src.utils.episodic_identity_display import (
    find_conversation_keys,
    resolve_identities,
)
from src.utils.identity_shadow_actions import (
    build_pair_evidence,
    confirm_link_pair,
    dismiss_pair,
    pair_key,
)
from src.utils.identity_shadow_periodic import (
    resolve_inbox_db,
    read_state,
    run_periodic_scan,
    shadow_periodic_config,
    state_path,
    write_state,
)
from src.web.web_i18n import tr


def _parse_pair_body(body: Optional[Mapping[str, Any]]) -> Tuple[str, str, str, str]:
    """Extract platform_a/chat_a/platform_b/chat_b from JSON body or query-like mapping."""
    src = body or {}
    return (
        str(src.get("platform_a") or "").strip(),
        str(src.get("chat_a") or "").strip(),
        str(src.get("platform_b") or "").strip(),
        str(src.get("chat_b") or "").strip(),
    )


def _sample_row_pair_key(row: Mapping[str, Any]) -> str:
    """pair_key from structured sample fields, or from a/b ``plat:chat`` strings."""
    pa = str(row.get("a_platform") or "").strip()
    ca = str(row.get("a_chat") or "").strip()
    pb = str(row.get("b_platform") or "").strip()
    cb = str(row.get("b_chat") or "").strip()
    if pa and ca and pb and cb:
        return pair_key(pa, ca, pb, cb)
    a = str(row.get("a") or "").strip()
    b = str(row.get("b") or "").strip()
    if ":" in a and ":" in b:
        pa, ca = a.split(":", 1)
        pb, cb = b.split(":", 1)
        return pair_key(pa, ca, pb, cb)
    return ""


def _sample_has_structured_pair_fields(sample: Any) -> bool:
    if not isinstance(sample, list):
        return False
    for row in sample:
        if not isinstance(row, Mapping):
            continue
        if all(
            str(row.get(k) or "").strip()
            for k in ("a_platform", "a_chat", "b_platform", "b_chat")
        ):
            return True
    return False


def build_correction_stats(
    audit_store: Any,
    skill_manager: Any,
    *,
    days: int = 30,
    recent_limit: int = 10,
    with_trend: bool = True,
) -> Dict[str, Any]:
    """R17/R18：聚合"AI 推断→人工确认"质量指标。

    采纳数来自审计（action=episodic_confirm_inferred）；``pending_inferred`` 自 J-10 二期起
    ＝例外队列待处理数（store.inferred_counts 已统一到 review_reason 口径）。
    ``adoption_rate`` = confirmed/(confirmed+pending) 保持 R18 语义——它是**积压信号**
    （确认少、待看多 → 队列没人处理），alert-status 低采纳告警继续读它。

    J-10 二期新增「抽检准确率」：只数坐席**已判过**的——``rejected``＝审计里的
    ``episodic_ignore`` + ``episodic_delete``（坐席判「不对/不要」），``judged`` =
    confirmed + rejected，``accuracy_rate`` = confirmed/judged；积压量不再污染准确率。
    供 correction-stats 端点与 alert-status 共用，避免聚合逻辑两处漂移。
    """
    win = max(1, min(int(days or 30), 365))
    since = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - win * 86400)
    )
    rows = []
    rejected = 0
    if audit_store:
        try:
            rows = audit_store.query(
                limit=5000, action="episodic_confirm_inferred", since=since,
            ) or []
        except Exception:
            rows = []
        for act in ("episodic_ignore", "episodic_delete"):
            try:
                rejected += len(audit_store.query(limit=5000, action=act, since=since) or [])
            except Exception:
                pass
    by_actor: Dict[str, int] = {}
    daily: Dict[str, int] = {}
    recent = []
    for r in rows:
        actor = str(r.get("user_id") or "?")
        by_actor[actor] = by_actor.get(actor, 0) + 1
        day = str(r.get("ts") or "")[:10]
        if day:
            daily[day] = daily.get(day, 0) + 1
        if len(recent) < max(0, int(recent_limit)):
            recent.append({
                "ts": r.get("ts", ""),
                "actor": actor,
                "content": str(r.get("new_val") or ""),
                "target": str(r.get("target") or ""),
            })
    confirmed = len(rows)
    inferred = {"pending": 0, "total": 0}
    if skill_manager and hasattr(skill_manager, "episodic_inferred_counts"):
        try:
            inferred = skill_manager.episodic_inferred_counts()
        except Exception:
            inferred = {"pending": 0, "total": 0}
    pending = int(inferred.get("pending", 0) or 0)
    denom = confirmed + pending
    adoption_rate = round(confirmed / denom, 4) if denom else 0.0
    judged = confirmed + rejected
    out: Dict[str, Any] = {
        "ok": True,
        "window_days": win,
        "confirmed": confirmed,
        "pending_inferred": pending,
        "total_inferred": int(inferred.get("total", 0) or 0),
        "adoption_rate": adoption_rate,
        "sample": denom,
        "rejected": rejected,
        "judged": judged,
        "accuracy_rate": round(confirmed / judged, 4) if judged else 0.0,
        "by_actor": sorted(
            [{"actor": a, "count": c} for a, c in by_actor.items()],
            key=lambda x: x["count"], reverse=True,
        ),
        "recent": recent,
    }
    if with_trend:
        out["trend"] = [
            {"date": d, "count": daily[d]} for d in sorted(daily)
        ]
    return out


#: O-2 D（#201 WYNN22）：入站累计 ≥ 此数而记忆自安装以来仍 0 条 → 页面红字「记忆没有在增长」
MEMORY_STALL_MIN_INBOUND = 50


def inbound_message_total(inbox_db: Optional[Path]) -> Optional[int]:
    """inbox.db 里客户入站（``direction='in'``）消息总数——「自安装以来收到多少条」。

    只读 URI 连接、2s 超时、任何异常 → None（页面拿到 None 就**不**判停滞，宁不报
    勿误报）。不经 InboxStore 实例：本路由本就按路径富化身份，与之同口径。
    """
    if not inbox_db:
        return None
    try:
        uri = Path(inbox_db).resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM messages WHERE direction = 'in'").fetchone()
            return int((row or [0])[0] or 0)
        finally:
            con.close()
    except Exception:
        return None


def memory_growth_status(
    growth: Optional[Mapping[str, Any]], inbound_total: Optional[int], *,
    min_inbound: int = MEMORY_STALL_MIN_INBOUND,
) -> Dict[str, Any]:
    """把 store 的 growth 读数与入站累计合成页面用块（纯函数）。

    ``stalled`` = 入站累计已知且 ≥ min_inbound **且** 自安装以来记忆写入 0 条——
    这正是 WYNN22 现场形态（8 小时 30 次 schedule run 全 skip、6 客户零抽取）；
    抽取修好后 since_install 从 0 开始涨，红字自然消失。入站数拿不到（None）不判。
    """
    g: Dict[str, Any] = dict(growth or {})
    since = int(g.get("since_install") or 0)
    g["inbound_total"] = inbound_total
    g["stall_min_inbound"] = int(min_inbound)
    g["stalled"] = bool(
        inbound_total is not None and int(inbound_total) >= int(min_inbound) and since == 0)
    return g


def register_episodic_identity_routes(app, ctx) -> None:
    """挂载 /api/episodic-memory/* 与 /api/identity/* 到 app。"""
    telegram_client = ctx.telegram_client
    _api_auth = ctx.api_auth
    _api_write = ctx.api_write

    def _get_sm():
        """SkillManager：主客户端 → app.state 双通路（protocol 账号实例主客户端为 None）。"""
        from src.web.web_context import resolve_skill_manager
        return resolve_skill_manager(telegram_client, app)

    # Q-5 C（#263）：画像冲突通知落工作台通知中心（app.state.notif_queue sys_status）——把 app 交给
    # profile_fill（抽取链跑在 skill_manager 里拿不到 app）。装配即绑定，失败零影响。
    try:
        from src.companion.goals.profile_fill import bind_app as _pf_bind_app
        _pf_bind_app(app)
    except Exception:
        pass

    # ── 情景记忆 API ──────────────────────────────────────────────────────

    def _inbox_db_or_none():
        """身份富化用的 inbox.db 路径；缺库/异常 → None（富化静默降级）。"""
        try:
            cfg, cfg_dir = _cfg_and_dir()
            p = resolve_inbox_db(cfg, cfg_dir)
            return p if p.exists() else None
        except Exception:
            return None

    @app.get("/api/episodic-memory")
    async def api_episodic_memory_list(
        request: Request, prefix: str = "", limit: int = 100, source: str = "",
        q: str = "", identity: int = 1, offset: int = 0,
        status: str = "active", review: str = "",
    ):
        """情景记忆条目列表（memory_key = 私聊用户 id 或 群id_用户id）。

        R13：可选 ``source`` 筛选（user_stated / ai_inferred）。
        身份化（P0）：``q``＝人类可读联合搜索（昵称/用户名/手机号经会话表译成
        键集 + 记忆键/内容 LIKE 的并集）；``identity=1``（默认）时每行附
        ``identity`` 块（昵称/头像/平台，经 conversations 主键点查 + TTL 缓存），
        inbox 库缺席/异常一律静默降级为旧响应形状，绝不阻断列表。
        ``offset``＝「加载更多」分页（P1 前端消费）。
        管理者摘要走独立端点 ``GET /api/episodic-memory/summary``（P3）。
        新参缺省时对 skill_manager 保持旧三参调用形状（兼容既有 fake/断言）。
        J-10 A2：``status``＝active（默认）/ ignored（维护区「已不再使用」）/ all；
        ``review``＝pending 或具体原因（conflict / high_impact / low_confidence /
        self_fact / commitment）。
        """
        _api_auth(request)
        sm = _get_sm()
        if not sm:
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready_sm"))
        lim = max(1, min(int(limit or 100), 500))
        off = max(0, min(int(offset or 0), 100000))
        src = source if source in ("user_stated", "ai_inferred") else ""
        qq = (q or "").strip()[:120]
        pre = (prefix or "")[:120]
        if qq and pre.strip() == qq:
            pre = ""  # 过渡期前端 q+prefix 双发同值：按 q 语义接管，防 AND 缩窄
        st = str(status or "active").strip().lower()
        st = st if st in ("active", "ignored", "all") else "active"
        rv = str(review or "").strip().lower()[:24]
        inbox_db = _inbox_db_or_none()
        kwargs: Dict[str, Any] = dict(prefix=pre, limit=lim, source=src)
        if qq:
            q_keys: list = []
            if inbox_db is not None:
                q_keys = find_conversation_keys(inbox_db, qq)
            kwargs.update(q=qq, q_keys=q_keys)
        if off:
            kwargs["offset"] = off
        if st != "active":
            kwargs["status"] = st
        if rv:
            kwargs["review"] = rv
        rows = sm.episodic_list_for_admin(**kwargs)
        if identity and inbox_db is not None and rows:
            try:
                idmap = resolve_identities(
                    inbox_db,
                    [str(r.get("memory_key") or "") for r in rows],
                )
                for r in rows:
                    ident = idmap.get(str(r.get("memory_key") or ""))
                    if ident:
                        r["identity"] = ident
            except Exception:
                pass  # 富化失败不阻断主表
        return {"ok": True, "items": rows, "count": len(rows), "offset": off}

    @app.delete("/api/episodic-memory/{row_id}")
    async def api_episodic_memory_delete(request: Request, row_id: int):
        _api_write("episodic_memory")(request)
        sm = _get_sm()
        if not sm:
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready"))
        # 删除前取行摘要供审计留痕（软失败：老 store 无该方法 → 只记 row_id）
        brief = None
        _store = getattr(sm, "_episodic_store", None)
        if _store is not None and hasattr(_store, "get_row_brief"):
            try:
                brief = _store.get_row_brief(int(row_id))
            except Exception:
                brief = None
        ok = sm.episodic_delete_for_admin(int(row_id))
        if not ok:
            raise HTTPException(status_code=404, detail=tr(request, "err.epi.record_not_found"))
        # 与 confirm 审计对称：删除不可逆，留痕「谁删了谁的哪条记忆」
        audit = getattr(ctx, "audit_store", None)
        if audit:
            try:
                actor = str(
                    request.session.get("username")
                    or request.session.get("role") or "web_admin"
                )
                old_val = ""
                if brief:
                    old_val = (
                        f"[{brief.get('source', '')}] {brief.get('memory_key', '')}"
                        f" | {str(brief.get('content', ''))[:160]}"
                    )
                audit.log(
                    actor, "episodic_delete", target=str(row_id),
                    old_val=old_val,
                )
            except Exception:
                pass
        return {"ok": True, "deleted": int(row_id)}

    @app.put("/api/episodic-memory/{row_id}")
    async def api_episodic_memory_edit(request: Request, row_id: int):
        """五件套·可编辑（#41 0830 定稿问题③）：人工改写记忆条文。

        「错误记忆只能删」→「改对了再留」。Body: ``{content}``。语义见
        ``store.update_fact_content``：新文重算哈希/显著性、向量清空待回填、
        ``source`` 升 ``user_stated``（人改过＝人工核准，不再是 AI 推断）。
        改成与既有条目同文 → 404 语义复用（前端提示改用删除）。
        """
        _api_write("episodic_memory")(request)
        sm = _get_sm()
        if not sm:
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready"))
        _store = getattr(sm, "_episodic_store", None)
        if _store is None or not hasattr(_store, "update_fact_content"):
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        content = str(body.get("content") or "").strip()
        if len(content) < 2 or len(content) > 500:
            raise HTTPException(status_code=400, detail=tr(
                request, "err.ws.field_required", field="content"))
        brief = None
        if hasattr(_store, "get_row_brief"):
            try:
                brief = _store.get_row_brief(int(row_id))
            except Exception:
                brief = None
        ok = _store.update_fact_content(int(row_id), content)
        if not ok:
            raise HTTPException(status_code=404, detail=tr(request, "err.epi.record_not_found"))
        # 与 confirm/delete 审计对称：谁把哪条记忆从什么改成了什么
        audit = getattr(ctx, "audit_store", None)
        if audit:
            try:
                actor = str(
                    request.session.get("username")
                    or request.session.get("role") or "web_admin"
                )
                old_val = ""
                if brief:
                    old_val = (
                        f"[{brief.get('source', '')}] "
                        f"{str(brief.get('content', ''))[:160]}"
                    )
                audit.log(
                    actor, "episodic_edit", target=str(row_id),
                    old_val=old_val, new_val=content[:160],
                )
            except Exception:
                pass
        return {"ok": True, "edited": int(row_id)}

    @app.post("/api/episodic-memory/bulk-delete")
    async def api_episodic_memory_bulk_delete(request: Request):
        """按关键词批量删除情景记忆（人设内容排查的清理配套，2026-08-03）。

        Body: ``{q, prefix?, source?, dry_run?=true, limit?=200}``。
        安全设计：``q`` 必填（禁止裸 prefix 全清一个客户的记忆——那是另一种
        破坏性操作，走既有逐条删除）；``dry_run`` 缺省 **true** 只回预览；
        单次上限 500；真删逐条走既有 ``episodic_delete_for_admin``，
        汇总一条审计（谁、按什么词、删了几条）。
        """
        _api_write("episodic_memory")(request)
        sm = _get_sm()
        if not sm:
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        qq = str(body.get("q") or "").strip()[:120]
        if not qq:
            raise HTTPException(status_code=400, detail=tr(request, "err.epi.bulk_q_required"))
        prefix = str(body.get("prefix") or "").strip()[:120]
        source = body.get("source")
        source = source if source in ("user_stated", "ai_inferred") else ""
        dry_run = bool(body.get("dry_run", True))
        lim = max(1, min(int(body.get("limit") or 200), 500))
        rows = sm.episodic_list_for_admin(
            prefix=prefix, limit=lim, source=source, q=qq, q_keys=[])
        matched = [
            {"row_id": int(r.get("id") or 0),
             "memory_key": str(r.get("memory_key") or ""),
             "content": str(r.get("content") or "")[:160]}
            for r in rows if r.get("id") is not None
        ]
        if dry_run:
            return {"ok": True, "dry_run": True, "matched": len(matched),
                    "items": matched}
        deleted = 0
        for m in matched:
            try:
                if sm.episodic_delete_for_admin(int(m["row_id"])):
                    deleted += 1
            except Exception:
                continue
        audit = getattr(ctx, "audit_store", None)
        if audit:
            try:
                actor = str(
                    request.session.get("username")
                    or request.session.get("role") or "web_admin"
                )
                audit.log(
                    actor, "episodic_bulk_delete", target=qq,
                    old_val=f"matched={len(matched)} deleted={deleted}"
                            f" prefix={prefix or '-'} source={source or '-'}",
                )
            except Exception:
                pass
        return {"ok": True, "dry_run": False,
                "matched": len(matched), "deleted": deleted}

    @app.get("/api/episodic-memory/summary")
    async def api_episodic_memory_summary(
        request: Request, days: int = 7, top: int = 3,
    ):
        """管理者摘要（P3）：近 N 天新增条数/覆盖用户数 + 记忆最多 Top-N。

        Top 键经 conversations 主键点查附 ``identity`` 块（与列表同一解析器/
        缓存）；inbox 库缺席时静默省略身份，绝不阻断摘要。前端按「接口是否
        存在」做能力探测——旧后端 404 即整条摘要行隐藏。
        """
        _api_auth(request)
        sm = _get_sm()
        store = getattr(sm, "_episodic_store", None) if sm else None
        if store is None or not hasattr(store, "admin_summary"):
            raise HTTPException(
                status_code=503, detail=tr(request, "err.epi.bot_not_ready_sm"))
        out = store.admin_summary(days=int(days or 7), top_n=int(top or 3))
        inbox_db = _inbox_db_or_none()
        if inbox_db is not None and out.get("top"):
            try:
                idmap = resolve_identities(
                    inbox_db, [t["memory_key"] for t in out["top"]])
                for t in out["top"]:
                    ident = idmap.get(t["memory_key"])
                    if ident:
                        t["identity"] = ident
            except Exception:
                pass  # 富化失败不阻断摘要
        # O-2 D（#201）：「自安装以来新增 N 条」+ 入站累计 → 停滞判定（旧 store 无 growth 则省略）
        if isinstance(out.get("growth"), dict):
            out["growth"] = memory_growth_status(
                out["growth"], inbound_message_total(inbox_db))
            # Q-5 D（#263）：连续 50 次抽取 facts=0 slots=0 → 页面第二条告警行（两链合一口径）
            try:
                from src.companion.goals.profile_fill import stall_status
                out["growth"]["extract_stall"] = stall_status()
            except Exception:
                pass
        return {"ok": True, **out}

    @app.get("/api/episodic-memory/key-health")
    async def api_episodic_key_health(request: Request, sample: int = 10):
        """记忆 key 健康探针：盘点裸 key（无 ``platform:`` 前缀）漂移。

        裸 key 下的记忆对收件箱引擎不可见 → 拉低命中率。一次性迁移清存量后，本探针
        让复发可观测（``bare_keys`` 回升即说明某入口又漏传 platform）。
        """
        _api_auth(request)
        sm = _get_sm()
        if not sm or not hasattr(sm, "episodic_key_health"):
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready_sm"))
        return {"ok": True, **sm.episodic_key_health(sample=max(0, min(int(sample or 10), 100)))}

    @app.get("/api/episodic-memory/key-migrate/plan")
    async def api_episodic_key_migrate_plan(request: Request, platform: str = "telegram"):
        """裸 key → canonical 迁移 dry-run（只读）：预览将并入哪些 key。"""
        _api_auth(request)
        sm = _get_sm()
        if not sm or not hasattr(sm, "episodic_plan_key_migration"):
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready_sm"))
        plat = (platform or "telegram").strip()[:32]
        if not plat:
            raise HTTPException(status_code=400, detail=tr(request, "err.epi.need_platform"))
        return {"ok": True, **sm.episodic_plan_key_migration(plat)}

    @app.post("/api/episodic-memory/key-migrate")
    async def api_episodic_key_migrate_apply(request: Request, platform: str = "telegram"):
        """裸 key → canonical 迁移落地（幂等、按 content_hash 去重）。一键修复漂移。"""
        _api_write("episodic_memory")(request)
        sm = _get_sm()
        if not sm or not hasattr(sm, "episodic_apply_key_migration"):
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready_sm"))
        plat = (platform or "telegram").strip()[:32]
        if not plat:
            raise HTTPException(status_code=400, detail=tr(request, "err.epi.need_platform"))
        rep = sm.episodic_apply_key_migration(plat)
        # 落审计：谁在何时把哪个平台的裸 key 并入 canonical
        audit = getattr(ctx, "audit_store", None)
        if audit and rep.get("enabled"):
            try:
                actor = str(
                    request.session.get("username")
                    or request.session.get("role") or "web_admin"
                )
                audit.log(
                    actor, "episodic_key_migrate", target=plat,
                    new_val=f"merged={rep.get('merged_keys',0)} moved={rep.get('moved_rows',0)}",
                )
            except Exception:
                pass
        return {"ok": True, **rep}

    @app.get("/api/episodic-memory/correction-stats")
    async def api_episodic_correction_stats(request: Request, days: int = 30):
        """R17：记忆校正质量看板——AI 推断采纳量/采纳率 + 各坐席确认量。

        采纳数来自审计（action=episodic_confirm_inferred）；待确认数来自记忆库当前
        raw 的 ai_inferred。采纳率为近似：confirmed/(confirmed+pending)。
        """
        _api_auth(request)
        sm = _get_sm()
        return build_correction_stats(
            getattr(ctx, "audit_store", None), sm, days=int(days or 30),
        )

    @app.post("/api/episodic-memory/{row_id}/confirm")
    async def api_episodic_memory_confirm(request: Request, row_id: int):
        """R15/R16：确认一条 AI 推断为属实——升格 user_stated 且置 stable，并落审计。"""
        _api_write("episodic_memory")(request)
        sm = _get_sm()
        if not sm:
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready"))
        content = sm.episodic_confirm_for_admin(int(row_id))
        if not content:
            raise HTTPException(status_code=404, detail=tr(request, "err.epi.record_not_ai_inferred"))
        # R16：谁在何时把哪条 AI 推断确认成事实——与危机处置审计对称，便于回溯校正质量
        audit = getattr(ctx, "audit_store", None)
        if audit:
            try:
                actor = str(
                    request.session.get("username")
                    or request.session.get("role")
                    or "web_admin"
                )
                audit.log(
                    actor, "episodic_confirm_inferred",
                    target=str(row_id),
                    old_val="ai_inferred",
                    new_val=str(content)[:200],
                )
            except Exception:
                pass
        return {"ok": True, "confirmed": int(row_id)}

    # ── J-10 A2（#183 · D8）：例外队列 / 软删 / 冲突择一 ─────────────────────

    def _store_or_503(request: Request, *methods: str):
        sm = _get_sm()
        store = getattr(sm, "_episodic_store", None) if sm else None
        if store is None or not all(hasattr(store, m) for m in methods):
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready"))
        return store

    def _audit_epi(request: Request, action: str, target: str, *, old_val: str = "",
                   new_val: str = "") -> None:
        audit = getattr(ctx, "audit_store", None)
        if not audit:
            return
        try:
            actor = str(
                request.session.get("username")
                or request.session.get("role") or "web_admin"
            )
            audit.log(actor, action, target=target, old_val=old_val, new_val=new_val)
        except Exception:
            pass

    @app.get("/api/episodic-memory/review-queue")
    async def api_episodic_review_queue(
        request: Request, memory_key: str = "", reason: str = "",
        limit: int = 100, offset: int = 0, identity: int = 1,
    ):
        """例外队列：只列 ``review_reason`` 非空且 ``status=active`` 的条目（其余不进队列）。

        返回 ``{items, count, counts: {pending, by_reason, high_impact_pending}}``；
        ``conflict`` 条目带 ``conflict_with``（同组 stable 那条）供「保留哪条」并列展示。
        ``memory_key`` 只看某客户（档案抽屉）；``reason`` 只看某类（同类型批量动作）。
        """
        _api_auth(request)
        store = _store_or_503(request, "review_queue", "review_counts")
        mk = str(memory_key or "").strip()[:200]
        rs = str(reason or "").strip().lower()[:24]
        items = store.review_queue(
            user_id=mk, reason=rs,
            limit=max(1, min(int(limit or 100), 500)),
            offset=max(0, min(int(offset or 0), 100000)))
        inbox_db = _inbox_db_or_none()
        if identity and inbox_db is not None and items:
            try:
                idmap = resolve_identities(
                    inbox_db, [str(r.get("memory_key") or "") for r in items])
                for r in items:
                    ident = idmap.get(str(r.get("memory_key") or ""))
                    if ident:
                        r["identity"] = ident
            except Exception:
                pass
        return {"ok": True, "items": items, "count": len(items),
                "counts": store.review_counts(user_id=mk)}

    @app.post("/api/episodic-memory/{row_id}/ignore")
    async def api_episodic_memory_ignore(request: Request, row_id: int):
        """「不再使用」＝软删（``status=ignored``：不召回、不进队列、可恢复）。
        硬删仍是 ``DELETE /api/episodic-memory/{row_id}``（页面收进「更多」二级确认）。"""
        _api_write("episodic_memory")(request)
        store = _store_or_503(request, "ignore_fact")
        content = store.ignore_fact(int(row_id))
        if content is None:
            raise HTTPException(status_code=404, detail=tr(request, "err.epi.record_not_found"))
        _audit_epi(request, "episodic_ignore", str(row_id), old_val=str(content)[:160])
        return {"ok": True, "ignored": int(row_id)}

    @app.post("/api/episodic-memory/{row_id}/restore")
    async def api_episodic_memory_restore(request: Request, row_id: int):
        """恢复软删条目（``ignored`` → ``active``）。"""
        _api_write("episodic_memory")(request)
        store = _store_or_503(request, "restore_fact")
        content = store.restore_fact(int(row_id))
        if content is None:
            raise HTTPException(status_code=404, detail=tr(request, "err.epi.record_not_found"))
        _audit_epi(request, "episodic_restore", str(row_id), new_val=str(content)[:160])
        return {"ok": True, "restored": int(row_id)}

    @app.post("/api/episodic-memory/resolve-conflict")
    async def api_episodic_memory_resolve_conflict(request: Request):
        """冲突组人工择一。Body ``{keep_id}``：保留那条转正 stable，同组其余标过时（stale，
        不硬删）。``keep_id`` 不在任何冲突组 → 404。"""
        _api_write("episodic_memory")(request)
        store = _store_or_503(request, "resolve_conflict")
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        try:
            keep_id = int(body.get("keep_id"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=tr(
                request, "err.ws.field_required", field="keep_id"))
        res = store.resolve_conflict(keep_id)
        if not res.get("kept"):
            raise HTTPException(status_code=404, detail=tr(request, "err.epi.record_not_found"))
        _audit_epi(request, "episodic_resolve_conflict", str(keep_id),
                   old_val=",".join(str(i) for i in res.get("staled") or []),
                   new_val=str(res.get("content") or "")[:160])
        return {"ok": True, **res}

    # ── J-10 A3（#183）：使用侧——最近几轮用了哪些记忆 ──────────────────────

    @app.get("/api/episodic-memory/used")
    async def api_episodic_memory_used(
        request: Request, conversation_id: str = "", memory_key: str = "",
        limit: int = 1, rows: int = 60,
    ):
        """最近 ``limit`` 轮注入 prompt 的记忆（按会话或记忆键）。

        契约（交 J-4 草稿旁「本轮用了这几条记忆 · 一键不再使用」）：
        ``{ok, batches: [{batch_id, ts, chain: inbox|direct, inbound_msg_id, conversation_id,
        items: [{id, content, source, tier, review_reason, impact, source_quote, status,
        recall_count, last_recalled_ts, …} | {id, deleted: true}]}], count}``——最新批在前，
        ``items`` 顺序＝当轮 prompt 里的 bullet 顺序。「不再使用」→ ``POST
        /api/episodic-memory/{id}/ignore``。两参皆空 → 400。
        """
        _api_auth(request)
        conv = str(conversation_id or "").strip()[:200]
        mk = str(memory_key or "").strip()[:200]
        if not conv and not mk:
            raise HTTPException(status_code=400, detail=tr(
                request, "err.ws.field_required", field="conversation_id"))
        store = _store_or_503(request, "recent_recalls")
        batches = store.recent_recalls(
            conversation_id=conv, memory_key=mk,
            batches=max(1, min(int(limit or 1), 20)),
            limit=max(1, min(int(rows or 60), 500)))
        return {"ok": True, "batches": batches, "count": len(batches)}

    # ── J-10 A4（#177）：「AI 自身经历」入口——人设说过 / 承诺过 / 发过（只读 + 删一条）──

    def _self_log_ctx(sm: Any, memory_key: str, conversation_id: str):
        """记忆键 → 该会话的 user_context（只读 ``peek``，绝不凭空建 ctx）。

        ContextStore 键＝``make_context_key(chat_key, account_id)``（``acct:peer`` / 裸
        peer）；记忆键多带 ``platform:`` 前缀或已链成 canonical。候选顺序：
        conversation_id 拆出的 acct/chat → identity 解析出的 conversation_id → 记忆键
        剥平台前缀 → 记忆键本身。返回 ``(ctx, key)``；找不到 → ``(None, "")``。
        """
        cs = getattr(sm, "_context_store", None) if sm else None
        peek = getattr(cs, "peek", None)
        if not callable(peek):
            return None, ""
        from src.utils.context_store import make_context_key
        from src.utils.episodic_memory_store import strip_composite_user_id
        from src.inbox.peer_delete_purge import split_conversation_id
        cands: list = []
        convs = [conversation_id]
        if memory_key:
            inbox_db = _inbox_db_or_none()
            if inbox_db is not None:
                try:
                    ident = resolve_identities(inbox_db, [memory_key]).get(memory_key) or {}
                    if ident.get("conversation_id"):
                        convs.append(str(ident["conversation_id"]))
                except Exception:
                    pass
        for c in convs:
            plat, acct, chat = split_conversation_id(str(c or ""))
            if chat:
                cands.append(make_context_key(chat, acct))
                cands.append(chat)
        if memory_key:
            for plat in ("telegram", "whatsapp", "line", "messenger", "instagram", "wechat"):
                s = strip_composite_user_id(memory_key, plat)
                if s != memory_key:
                    cands.append(s)
            cands.append(memory_key)
        seen = set()
        for k in cands:
            k = str(k or "").strip()
            if not k or k in seen:
                continue
            seen.add(k)
            try:
                ctx = peek(k)
            except Exception:
                ctx = None
            if isinstance(ctx, dict):
                return ctx, k
        return None, ""

    @app.get("/api/episodic-memory/self-log")
    async def api_episodic_self_log(
        request: Request, memory_key: str = "", conversation_id: str = "",
    ):
        """人设自己的经历时间线：坐席替人设说过（``_human_said_log``，#177）/ AI 自述近况
        （``_self_state_log``）/ 发过的媒体（``_media_sent_log``）/ TTL 内承诺待兑现
        （``_media_pending``）。只读；``items`` 按时间倒序，每条 ``{kind, ts, text, …}``。
        找不到对应会话上下文 → ``items=[]`` + ``found=false``（不是错误）。
        """
        _api_auth(request)
        sm = _get_sm()
        if not sm:
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready_sm"))
        mk = str(memory_key or "").strip()[:200]
        conv = str(conversation_id or "").strip()[:200]
        if not mk and not conv:
            raise HTTPException(status_code=400, detail=tr(
                request, "err.ws.field_required", field="memory_key"))
        from src.utils.memory_self_log import collect_self_experience
        ctx, key = _self_log_ctx(sm, mk, conv)
        items = collect_self_experience(ctx) if ctx is not None else []
        return {"ok": True, "found": ctx is not None, "context_key": key,
                "items": items, "count": len(items)}

    @app.post("/api/episodic-memory/self-log/delete")
    async def api_episodic_self_log_delete(request: Request):
        """删一条人设经历记录。Body ``{memory_key|conversation_id, kind, ts, text}``。
        只改那份 bounded 日志并落盘；不触碰任何注入逻辑（下一轮自然不再带这条）。"""
        _api_write("episodic_memory")(request)
        sm = _get_sm()
        if not sm:
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        mk = str(body.get("memory_key") or "").strip()[:200]
        conv = str(body.get("conversation_id") or "").strip()[:200]
        kind = str(body.get("kind") or "").strip()[:32]
        text = str(body.get("text") or "")[:400]
        if (not mk and not conv) or not kind:
            raise HTTPException(status_code=400, detail=tr(
                request, "err.ws.field_required", field="kind"))
        from src.utils.memory_self_log import delete_self_experience_entry
        ctx, key = _self_log_ctx(sm, mk, conv)
        if ctx is None:
            raise HTTPException(status_code=404, detail=tr(request, "err.epi.record_not_found"))
        ok = delete_self_experience_entry(ctx, kind, body.get("ts"), text)
        if not ok:
            raise HTTPException(status_code=404, detail=tr(request, "err.epi.record_not_found"))
        cs = getattr(sm, "_context_store", None)
        try:
            if cs is not None and key:
                cs.mark_dirty(key)
                cs.flush(key)
        except Exception:
            pass
        _audit_epi(request, "episodic_self_log_delete", key or mk or conv,
                   old_val=f"[{kind}] {text[:160]}")
        return {"ok": True, "deleted": {"kind": kind, "text": text[:160]}, "context_key": key}

    @app.get("/api/episodic-memory/export")
    async def api_episodic_memory_export(
        request: Request, memory_key: str = "", format: str = "json", include_ignored: int = 0,
    ):
        """J-10 二期：单客户记忆导出（隐私行承诺的「可导出」）。

        ``format=json``：``{memory_key, exported_at, identity?, memories: [...], self_experience: [...]}``
        （附件下载）；``format=csv``：只导记忆行（content / 谁说的 / 状态 / 时间 / 原话 / 被用次数）。
        默认不含「不再使用」的；``include_ignored=1`` 全导。导出落审计（谁导了谁的记忆）。
        """
        _api_auth(request)
        mk = str(memory_key or "").strip()[:200]
        if not mk:
            raise HTTPException(status_code=400, detail=tr(
                request, "err.ws.field_required", field="memory_key"))
        store = _store_or_503(request, "export_user")
        rows = store.export_user(mk, include_ignored=bool(include_ignored))
        sm = _get_sm()
        ident: Dict[str, Any] = {}
        inbox_db = _inbox_db_or_none()
        if inbox_db is not None:
            try:
                ident = resolve_identities(inbox_db, [mk]).get(mk) or {}
            except Exception:
                ident = {}
        self_items: list = []
        try:
            from src.utils.memory_self_log import collect_self_experience
            ctx, _k = _self_log_ctx(sm, mk, str(ident.get("conversation_id") or ""))
            self_items = collect_self_experience(ctx) if ctx is not None else []
        except Exception:
            self_items = []
        _audit_epi(request, "episodic_export", mk, new_val=f"rows={len(rows)} fmt={format}")
        from fastapi.responses import Response
        import json as _json
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(ch if ch.isalnum() else "_" for ch in mk)[:60] or "memory"
        if str(format or "").lower() == "csv":
            import csv
            import io
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["id", "content", "source", "tier", "status", "review_reason", "impact",
                        "created_at", "source_quote", "recall_count", "last_recalled_ts"])
            for r in rows:
                w.writerow([r.get("id"), r.get("content"), r.get("source"), r.get("tier"),
                            r.get("status"), r.get("review_reason"), r.get("impact"),
                            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(r.get("created_at") or 0))),
                            r.get("source_quote"), r.get("recall_count"),
                            (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(r.get("last_recalled_ts") or 0)))
                             if r.get("last_recalled_ts") else "")])
            return Response(
                content="\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="memory_{safe}_{stamp}.csv"'})
        payload = {"memory_key": mk, "exported_at": time.time(), "identity": ident or None,
                   "memories": rows, "self_experience": self_items}
        return Response(
            content=_json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="memory_{safe}_{stamp}.json"'})

    @app.get("/api/episodic-memory/promises/open")
    async def api_episodic_promises_open(
        request: Request, overdue_only: int = 0, limit: int = 50, identity: int = 1,
    ):
        """J-10 三期：跨客户「AI 答应过还没做」——未兑现承诺汇总（超期在前）。

        每条带 ``context_key``（ContextStore 键）与解析出的 ``memory_key``（打开客户档案抽屉用）
        + ``identity``；``counts.{open, overdue}``。数据源＝各会话 ``_promise_log``，
        `memory_promises.collect_open_promises`。
        """
        _api_auth(request)
        sm = _get_sm()
        if not sm:
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready_sm"))
        from src.utils.memory_promises import collect_open_promises
        cs = getattr(sm, "_context_store", None)
        res = collect_open_promises(cs, overdue_only=bool(overdue_only))
        lim = max(1, min(int(limit or 50), 500))
        items = list(res.get("items") or [])[:lim]
        store = getattr(sm, "_episodic_store", None)
        for it in items:
            mk = ""
            if store is not None and hasattr(store, "find_memory_keys"):
                try:
                    ks = store.find_memory_keys(it["context_key"])
                    mk = ks[0] if ks else ""
                except Exception:
                    mk = ""
            it["memory_key"] = mk or it["context_key"]
        inbox_db = _inbox_db_or_none()
        if identity and inbox_db is not None and items:
            try:
                idmap = resolve_identities(inbox_db, [i["memory_key"] for i in items])
                for it in items:
                    ident = idmap.get(it["memory_key"])
                    if ident:
                        it["identity"] = ident
            except Exception:
                pass
        return {"ok": True, "items": items, "count": len(items), "counts": res.get("counts") or {}}

    @app.post("/api/episodic-memory/self-log/mark-done")
    async def api_episodic_self_log_mark_done(request: Request):
        """J-10 二期：坐席点「已兑现」。Body ``{memory_key|conversation_id, kind: "promise", ts, text}``。
        媒体类承诺由真发记录自动结清，这里给电话/见面/口头约定这类系统看不见的兑现。"""
        _api_write("episodic_memory")(request)
        sm = _get_sm()
        if not sm:
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        mk = str(body.get("memory_key") or "").strip()[:200]
        conv = str(body.get("conversation_id") or "").strip()[:200]
        kind = str(body.get("kind") or "promise").strip()[:32]
        text = str(body.get("text") or "")[:400]
        if not mk and not conv:
            raise HTTPException(status_code=400, detail=tr(
                request, "err.ws.field_required", field="memory_key"))
        from src.utils.memory_self_log import mark_self_experience_done
        ctx, key = _self_log_ctx(sm, mk, conv)
        if ctx is None or not mark_self_experience_done(ctx, kind, body.get("ts"), text):
            raise HTTPException(status_code=404, detail=tr(request, "err.epi.record_not_found"))
        cs = getattr(sm, "_context_store", None)
        try:
            if cs is not None and key:
                cs.mark_dirty(key)
                cs.flush(key)
        except Exception:
            pass
        _audit_epi(request, "episodic_promise_done", key or mk or conv, new_val=text[:160])
        return {"ok": True, "done": {"kind": kind, "text": text[:160]}, "context_key": key}

    @app.post("/api/episodic-memory/backfill")
    async def api_episodic_memory_backfill(
        request: Request, limit: int = 20, prefix: str = "", force: bool = False
    ):
        """为情景记忆行补全 embedding（限流：单次最多 100 条；可选 prefix 筛选 memory_key）。

        ``force=1`` 重嵌**所有**行（换 embedding 模型后重建全量向量用），否则只补缺失向量的行。
        """
        _api_write("episodic_memory")(request)
        sm = _get_sm()
        if not sm:
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready"))
        lim = max(1, min(int(limit or 20), 100))
        pre = (prefix or "")[:120]
        out = await sm.episodic_backfill_embeddings(lim, memory_key_prefix=pre, force=bool(force))
        if out.get("ok") is False:
            err = str(out.get("error") or "")
            if err == "vector_disabled":
                raise HTTPException(
                    status_code=400, detail=tr(request, "err.epi.vector_disabled")
                )
            if err == "daily_embed_budget_exceeded":
                raise HTTPException(
                    status_code=429,
                    detail=tr(request, "err.epi.embed_budget_exhausted"),
                )
            if err == "no_store":
                raise HTTPException(
                    status_code=503, detail=tr(request, "err.epi.memory_or_ai_unavailable")
                )
            raise HTTPException(status_code=400, detail=err or "backfill_failed")
        return out

    # ── S5: CrossPlatformIdentity API ─────────────────────────────────────

    def _get_cpi():
        """Return CPI instance from SkillManager or None."""
        sm = _get_sm()
        return getattr(sm, "_cpi", None) if sm else None

    def _audit_identity(
        request: Request, action: str, target: str, *, new_val: str = "",
    ) -> None:
        """best-effort 身份操作审计（谁链/解链了什么）；绝不阻断请求。"""
        audit = getattr(ctx, "audit_store", None)
        if not audit:
            return
        try:
            actor = str(
                request.session.get("username")
                or request.session.get("role") or "web_admin"
            )
            audit.log(actor, action, target=target, new_val=new_val)
        except Exception:
            pass

    @app.get("/api/identity")
    async def api_identity_list(request: Request, limit: int = 200):
        """List all (platform, platform_uid, canonical_id) rows."""
        _api_auth(request)
        cpi = _get_cpi()
        if not cpi:
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.identity_not_ready"))
        rows = cpi.list_all(limit=min(int(limit), 500))
        return {"ok": True, "items": [
            {"platform": r[0], "platform_uid": r[1], "canonical_id": r[2], "created_at": r[3]}
            for r in rows
        ]}

    @app.post("/api/identity/link")
    async def api_identity_link(request: Request):
        """Link two platform UIDs to share the same episodic memory.
        Body: {platform_a, uid_a, platform_b, uid_b}

        P10：与影子确认同口径——B 侧旧 canonical 的历史情景记忆随关联
        merge 进共享 canonical，B 既有 cluster 整簇改挂（传递性）。"""
        _api_write("identity")(request)
        cpi = _get_cpi()
        if not cpi:
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.identity_not_ready"))
        body = await request.json()
        pa, ua = str(body.get("platform_a", "")), str(body.get("uid_a", ""))
        pb, ub = str(body.get("platform_b", "")), str(body.get("uid_b", ""))
        if not all([pa, ua, pb, ub]):
            raise HTTPException(status_code=400, detail=tr(request, "err.epi.need_ab_pairs"))
        from src.utils.cross_platform_identity import link_and_merge_memory
        _sm = _get_sm()
        _store = getattr(_sm, "_episodic_store", None) if _sm else None
        out = link_and_merge_memory(cpi, _store, pa, ua, pb, ub)
        # 合流观测累计（与影子确认同一 totals 文件）。幂等重关联不计数。
        if not out.get("already_linked"):
            try:
                from src.utils.identity_shadow_actions import record_merge_event
                _, _cfg_dir = _cfg_and_dir()
                record_merge_event(
                    _cfg_dir, "manual_link",
                    merged_rows=int(out.get("memory_rows_merged") or 0),
                    cluster_relinked=len(out.get("cluster_relinked") or []),
                    canonical=str(out.get("canonical_id") or ""),
                )
            except Exception:
                pass
        _audit_identity(
            request, "identity_link", f"{pa}:{ua}|{pb}:{ub}",
            new_val=(
                f"{str(out.get('canonical_id') or '')[:80]}"
                f" merged={int(out.get('memory_rows_merged') or 0)}"
            ),
        )
        return {
            "ok": True,
            "canonical_id": out.get("canonical_id"),
            "memory_rows_merged": int(out.get("memory_rows_merged") or 0),
            "cluster_relinked": out.get("cluster_relinked") or [],
        }

    @app.post("/api/identity/unlink")
    async def api_identity_unlink(request: Request):
        """Detach a platform UID back to its own canonical_id.
        Body: {platform, uid}

        注意：已合流的历史记忆留在共享 canonical（内容已混合无法归属拆分），
        unlink 只影响未来读写。"""
        _api_write("identity")(request)
        cpi = _get_cpi()
        if not cpi:
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.identity_not_ready"))
        body = await request.json()
        plat, uid = str(body.get("platform", "")), str(body.get("uid", ""))
        if not plat or not uid:
            raise HTTPException(status_code=400, detail=tr(request, "err.epi.need_platform_uid"))
        new_canon = cpi.unlink(plat, uid)
        _audit_identity(request, "identity_unlink", f"{plat}:{uid}",
                        new_val=str(new_canon or "")[:80])
        return {"ok": True, "canonical_id": new_canon}

    # ── 身份影子：证据包 / 否定 / 确认关联（只读扫描之外的人工处置）──────────

    def _cfg_and_dir():
        cm = getattr(ctx, "config_manager", None)
        cfg = (getattr(cm, "config", None) if cm is not None else None) or {}
        raw_path = getattr(cm, "config_path", None) if cm is not None else None
        cfg_dir = Path(raw_path).parent if raw_path else Path("config")
        return cfg, cfg_dir

    @app.get("/api/identity/shadow/evidence")
    async def api_identity_shadow_evidence(
        request: Request,
        platform_a: str = "",
        chat_a: str = "",
        platform_b: str = "",
        chat_b: str = "",
    ):
        """影子配对证据包（消息片段 + 账号分桶），供人工核对。"""
        _api_auth(request)
        pa, ca, pb, cb = _parse_pair_body({
            "platform_a": platform_a,
            "chat_a": chat_a,
            "platform_b": platform_b,
            "chat_b": chat_b,
        })
        if not all([pa, ca, pb, cb]):
            raise HTTPException(status_code=400, detail=tr(request, "err.ish.need_pair"))
        cfg, cfg_dir = _cfg_and_dir()
        inbox_db = resolve_inbox_db(cfg, cfg_dir)
        pair = {
            "a": {"platform": pa, "chat_key": ca},
            "b": {"platform": pb, "chat_key": cb},
        }
        evidence = build_pair_evidence(inbox_db, pair)
        return {"ok": True, **evidence}

    @app.post("/api/identity/shadow/dismiss")
    async def api_identity_shadow_dismiss(request: Request):
        """标记「不是同一人」并轻量刷新影子 state 样本。"""
        _api_write("identity")(request)
        body = await request.json()
        pa, ca, pb, cb = _parse_pair_body(body if isinstance(body, dict) else {})
        if not all([pa, ca, pb, cb]):
            raise HTTPException(status_code=400, detail=tr(request, "err.ish.need_pair"))
        cfg, cfg_dir = _cfg_and_dir()
        reason = str((body or {}).get("reason") or "not_same_person")[:80]
        result = dismiss_pair(cfg_dir, pa, ca, pb, cb, reason=reason)
        pk = str(result.get("pair_key") or "")
        if result.get("ok") and pk:
            try:
                sp = state_path(cfg_dir)
                st = read_state(sp)
                sample = st.get("sample")
                if isinstance(sample, list) and sample:
                    has_structured = _sample_has_structured_pair_fields(sample)
                    can_match = any(
                        _sample_row_pair_key(r)
                        for r in sample if isinstance(r, Mapping)
                    )
                    if has_structured or can_match:
                        kept = []
                        removed_tiers: Dict[str, int] = {}
                        for row in sample:
                            if not isinstance(row, Mapping):
                                kept.append(row)
                                continue
                            if _sample_row_pair_key(row) == pk:
                                tier = str(row.get("tier") or "")
                                if tier:
                                    removed_tiers[tier] = (
                                        removed_tiers.get(tier, 0) + 1
                                    )
                                continue
                            kept.append(row)
                        removed_n = len(sample) - len(kept)
                        if removed_n > 0:
                            st["sample"] = kept
                            st["pairs"] = max(
                                0, int(st.get("pairs") or 0) - removed_n,
                            )
                            counts = dict(st.get("counts") or {})
                            for tier, n in removed_tiers.items():
                                counts[tier] = max(
                                    0, int(counts.get(tier) or 0) - n,
                                )
                            st["counts"] = counts
                            write_state(sp, st)
                    elif (
                        not has_structured
                        and shadow_periodic_config(cfg).get("enabled")
                    ):
                        # 样本既无结构化字段也无法解析 a/b → 整轮重扫兜底
                        run_periodic_scan(cfg, cfg_dir)
            except Exception:
                pass
        return result

    @app.post("/api/identity/shadow/confirm-link")
    async def api_identity_shadow_confirm_link(request: Request):
        """人工确认同一人：链两侧全部账号分桶键 → 同一 canonical。"""
        _api_write("identity")(request)
        cpi = _get_cpi()
        if not cpi:
            raise HTTPException(
                status_code=503, detail=tr(request, "err.epi.identity_not_ready"),
            )
        body = await request.json()
        pa, ca, pb, cb = _parse_pair_body(body if isinstance(body, dict) else {})
        if not all([pa, ca, pb, cb]):
            raise HTTPException(status_code=400, detail=tr(request, "err.ish.need_pair"))
        cfg, cfg_dir = _cfg_and_dir()
        inbox_db = resolve_inbox_db(cfg, cfg_dir)
        # 顺带记忆合流：把两侧旧 canonical 下的历史事实并入共享 canonical
        # （store 缺席=纯关联，行为同旧版，绝不阻断）
        _sm = _get_sm()
        _store = getattr(_sm, "_episodic_store", None) if _sm else None
        result = confirm_link_pair(
            cpi, inbox_db, pa, ca, pb, cb, episodic_store=_store)
        if not result.get("ok"):
            err = str(result.get("error") or "")
            if err == "bad_pair_or_cpi":
                raise HTTPException(
                    status_code=400, detail=tr(request, "err.ish.bad_pair"),
                )
            if err == "no_uids":
                raise HTTPException(
                    status_code=400, detail=tr(request, "err.ish.no_uids"),
                )
            raise HTTPException(status_code=400, detail=err or "confirm_failed")
        pk = str(result.get("pair_key") or "")
        try:
            sp = state_path(cfg_dir)
            st = read_state(sp)
            sample = st.get("sample")
            bumped = False
            if isinstance(sample, list) and pk:
                for row in sample:
                    if not isinstance(row, dict):
                        continue
                    if _sample_row_pair_key(row) != pk:
                        continue
                    if not row.get("already_linked"):
                        row["already_linked"] = True
                        bumped = True
                if bumped:
                    counts = dict(st.get("counts") or {})
                    counts["already_linked"] = int(
                        counts.get("already_linked") or 0
                    ) + 1
                    st["counts"] = counts
                    st["sample"] = sample
                    write_state(sp, st)
        except Exception:
            pass
        # 合流观测累计（best-effort；独立 totals 文件，ops 卡读数）。
        # 幂等重确认（already_linked：验证 ping / 双击）不计数，防 totals 虚胀。
        if not result.get("already_linked"):
            try:
                from src.utils.identity_shadow_actions import record_merge_event
                record_merge_event(
                    cfg_dir, "confirm",
                    merged_rows=int(result.get("merged_rows") or 0),
                    cluster_relinked=len(result.get("cluster_relinked") or []),
                    canonical=str(result.get("canonical_id") or ""),
                )
            except Exception:
                pass
        _audit_identity(
            request, "identity_shadow_confirm",
            pk or f"{pa}:{ca}|{pb}:{cb}",
            new_val=(
                f"{str(result.get('canonical_id') or '')[:80]}"
                f" merged={int(result.get('merged_rows') or 0)}"
            ),
        )
        return result
