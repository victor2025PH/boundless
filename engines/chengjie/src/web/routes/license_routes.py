"""C0-1 授权状态只读 API + C4 粘贴激活。

``GET  /api/admin/license``          —— 授权状态快照（state / plan / 到期 / 席位 /
渠道 / 功能位 / 提示 + P0-4 字符额度用量）。前端「授权状态卡」消费此端点。
``POST /api/admin/license/reload``   —— 重新读取授权文件。
``POST /api/admin/license/activate`` —— C4：粘贴已签发的 license key → 先 preview
验签（不落盘），active/grace 才写 ``config/license.key`` 并 reload。私钥/签发
永远不在本产品侧——本端点只接受**厂商已签发**的 key。

C5（半自动发卡 API：/api/admin/license/issue）**刻意不做**：签发需要 Ed25519
私钥托管 + 订单/CRM 对接，属厂商基础设施（见 scripts/license_tool.py 的离线
签发 CLI）。产品侧永不持有私钥，故不留可误开的路由 stub，仅此注释存档。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def _quota_snapshot() -> dict:
    """P0-4 字符额度快照（无额度授权 → included=0/used=0）。绝不抛。"""
    try:
        from src.licensing.quota_store import check_license_quota

        q = check_license_quota()
        return {
            "included_chars": q.get("included", 0),
            "used_chars": q.get("used", 0),
            "remaining_chars": q.get("remaining"),
            "exceeded": q.get("exceeded", False),
            # P2 首启体验档：无授权时额度来自本地赠量，前端据 source 区分显示口径
            "source": q.get("source", "license"),
            "trial_hours_left": q.get("trial_hours_left"),
            "trial_expired": q.get("trial_expired", False),
        }
    except Exception:
        return {"included_chars": 0, "used_chars": 0,
                "remaining_chars": None, "exceeded": False,
                "source": "license", "trial_hours_left": None,
                "trial_expired": False}


#: 顶栏额度徽章的分级阈值。放在服务端而不是前端：坐席顶栏 / 会员中心 / 首启向导
#: 三处都要用同一套判断，前端各写一份必然漂移。
QUOTA_LOW_RATIO = 0.2        # 剩余占比低于此 → 提醒档
QUOTA_LOW_HOURS = 12.0       # 体验档剩余时长低于此 → 提醒档


def apply_license_token(token: str) -> dict:
    """验签 → 关体验档 → 落盘 → reload。授权落地的**唯一**实现。

    粘贴激活与「注册领试用」自动激活共用这一条：两处各写一份的话，「激活即永久
    关闭首启体验档」这类反白嫖规则迟早只在其中一处生效。

    返回 ``{ok, error?, state, ...}``——不抛、不做 i18n（文案留给各自路由）。
    """
    from src.licensing import get_license_manager

    t = str(token or "").strip()
    if not t:
        return {"ok": False, "error": "empty", "state": "invalid"}
    mgr = get_license_manager()
    preview = mgr.preview_token(t)
    if preview.state not in ("active", "grace"):
        return {"ok": False, "error": "invalid", "state": preview.state}

    # 激活成功即永久关闭首启体验档：否则「激活 → 删掉 license.key」就能循环
    # 白嫖剩余赠量。体验档是首装赠礼，一旦进入正式授权语义就不该再回头。
    try:
        from src.licensing.local_trial import get_local_trial
        _lt = get_local_trial()
        if _lt is not None:
            _lt.close("license_activated")
    except Exception:
        logger.debug("[license] 关闭首启体验档失败（已忽略）", exc_info=True)

    path = mgr.license_path
    if not path:
        return {"ok": False, "error": "no_path", "state": preview.state}
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(t + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning("[license] 激活写盘失败：%s", e)
        return {"ok": False, "error": "save_failed", "detail": str(e), "state": preview.state}

    st = mgr.reload()
    logger.info("[license] 激活成功：plan=%s state=%s customer=%s lic_id=%s",
                st.plan, st.state, st.customer or "-", st.lic_id or "-")
    data = st.to_dict()
    data["quota"] = _quota_snapshot()
    data["ok"] = True
    return data


#: 由 register_license_routes 注入的 ConfigManager（本仓惯例是注入而非全局导入——
#: `src.utils.config_manager` 只导出类 ConfigManager，没有模块级单例）。
_CONFIG_MANAGER: Any = None


def _cfg_or_none() -> dict:
    """当前生效配置（取不到就给空 dict，让 trial_claim_client 走内置默认站点）。

    ⚠️ 这里取不到配置的后果不是「少个可选项」而是**静默连错站点**：
    `licensing.trial.site_url` 读不到就回落成默认官网，本地测试台会把单子建到生产
    台账里（2026-07-27 实锤：曾因错误地 `from ... import config_manager`（该符号不
    存在）被 except 吞掉，整段配置永远为空）。故此处不再吞异常式地依赖导入。
    """
    return getattr(_CONFIG_MANAGER, "config", None) or {}


def _consume_trial_payload(res: dict, license_token: str, voucher: str) -> dict:
    """把官网回来的授权/加量凭证就地落地，并把结果并进响应。

    刻意**不把明文回传给前端**：授权已写进 license.key、加量已入账，再往浏览器
    送一份只是多一个泄漏面。前端要的只是「成了没有」。
    """
    from src.licensing import trial_claim_client as tc

    if license_token:
        try:
            act = apply_license_token(license_token)
            if act.get("ok"):
                tc.mark_activated(_cfg_or_none())
                res["activated"] = True
                res["plan"] = act.get("plan", "")
                res["quota"] = act.get("quota") or {}
            elif act.get("state") == "expired":
                # 同一台机器再领会按机器码去重、拿回**上次那张已过期的**授权。
                # 报「invalid」会让人以为系统坏了；这其实是「你的试用已经用完」。
                tc.mark_expired(_cfg_or_none())
                res["trial_exhausted"] = True
                res["activate_error"] = "expired"
                logger.info("[trial-claim] 本机试用已用尽（授权过期），引导购买/联系客服")
            else:
                res["activate_error"] = str(act.get("error") or "invalid")
                logger.warning("[trial-claim] 自动激活失败：%s", res["activate_error"])
        except Exception:  # pragma: no cover - 激活异常不能吃掉轮询结果
            logger.debug("[trial-claim] 自动激活异常", exc_info=True)
            res["activate_error"] = "internal"

    if voucher:
        try:
            from src.licensing.topup_voucher import redeem_topup_voucher

            rd = redeem_topup_voucher(voucher)
            if rd.get("ok"):
                chars = int(rd.get("chars") or 0)
                tc.mark_topup(chars, _cfg_or_none())
                res["gift_redeemed"] = True
                res["gift_chars"] = chars
                logger.info("[trial-claim] 客服赠量已入账：+%s 字符", chars)
            else:
                # duplicate_ref = 之前那轮其实已经入过账，属正常幂等命中，不是失败。
                err = str(rd.get("error") or "internal")
                if err == "duplicate_ref":
                    tc.mark_topup(int(rd.get("chars") or 0), _cfg_or_none())
                    res["gift_redeemed"] = True
                else:
                    res["gift_error"] = err
        except Exception:  # pragma: no cover
            logger.debug("[trial-claim] 赠量入账异常", exc_info=True)
            res["gift_error"] = "internal"
    return res


def quota_level(q: dict) -> str:
    """额度紧张度：``ok`` | ``low`` | ``out``（纯函数，便于单测与复用）。"""
    if q.get("exceeded"):
        return "out"
    included = int(q.get("included_chars") or 0)
    if included > 0:
        remaining = q.get("remaining_chars")
        if remaining is not None and int(remaining) <= included * QUOTA_LOW_RATIO:
            return "low"
    hours = q.get("trial_hours_left")
    if hours is not None and float(hours) <= QUOTA_LOW_HOURS:
        return "low"
    return "ok"


def register_license_routes(app, *, api_auth, config_manager=None) -> None:
    global _CONFIG_MANAGER
    if config_manager is not None:
        _CONFIG_MANAGER = config_manager

    @app.get("/api/workspace/quota")
    async def api_workspace_quota(request: Request):
        """坐席可读的额度摘要（顶栏徽章 60s 轮询用；任意登录用户）。

        为什么坐席也要看得见：额度用尽会让**翻译与 AI 草稿直接停摆**，而坐席是第一个
        撞上的人。只让主管在会员中心看得到，等于让一线在毫无预警的情况下发现"AI 不干活了"。

        刻意**不含**敏感字段（授权码/客户名/lic_id 都不给）——它只回答
        「还剩多少、紧不紧张、是不是体验档」。``visible=false`` 时前端整个徽章隐藏
        （不限量授权 / 社区模式且体验档未启用）。
        """
        api_auth(request)
        try:
            q = _quota_snapshot()
            included = int(q.get("included_chars") or 0)
            source = str(q.get("source") or "license")
            return {
                "ok": True,
                "visible": bool(included > 0 or source == "local_trial"),
                "source": source,
                "included": included,
                "used": int(q.get("used_chars") or 0),
                "remaining": q.get("remaining_chars"),
                "exceeded": bool(q.get("exceeded")),
                "hours_left": q.get("trial_hours_left"),
                "expired": bool(q.get("trial_expired")),
                "level": quota_level(q),
            }
        except Exception:  # pragma: no cover - 观测端点绝不影响工作台
            logger.debug("[license] 坐席额度摘要失败", exc_info=True)
            return {"ok": False, "visible": False, "level": "ok"}
    @app.get("/api/admin/license")
    async def api_admin_license(request: Request):
        api_auth(request)
        try:
            from src.licensing import get_license_manager

            st = get_license_manager().status()
            data = st.to_dict()
            data["quota"] = _quota_snapshot()
            data["ok"] = True
            return data
        except Exception as e:  # pragma: no cover - 异常兜底
            return {
                "ok": True,
                "state": "unavailable",
                "licensed": False,
                "plan": "community",
                "messages": [f"授权状态读取失败：{e}"],
            }

    @app.post("/api/admin/license/reload")
    async def api_admin_license_reload(request: Request):
        api_auth(request)
        try:
            from src.licensing import get_license_manager

            st = get_license_manager().reload()
            data = st.to_dict()
            data["quota"] = _quota_snapshot()
            data["ok"] = True
            return data
        except Exception as e:  # pragma: no cover
            return {"ok": False, "msg": str(e)}

    @app.post("/api/admin/license/activate")
    async def api_admin_license_activate(request: Request):
        """C4 粘贴激活：验签通过（active/grace）才写 ``config/license.key`` + reload。

        防呆：invalid / expired / unavailable 的 key **不落盘**——否则把坏 key 写进
        文件反而使现有授权降级。写盘失败回滚语义＝不 reload（原授权不受影响）。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        token = str((body or {}).get("key") or "").strip()
        if not token:
            raise HTTPException(
                400, tr(request, "err.ws.field_required", field="key"))

        res = apply_license_token(token)
        if not res.get("ok"):
            err = str(res.get("error") or "invalid")
            if err == "no_path":
                raise HTTPException(500, tr(request, "err.lic.no_license_path"))
            if err == "save_failed":
                raise HTTPException(
                    500, tr(request, "err.lic.save_failed", err=res.get("detail", "")))
            raise HTTPException(
                400, tr(request, "err.lic.activate_invalid", state=res.get("state", "invalid")))
        return res

    # ── P2 注册领试用：官网建单 → 厂商机签发 → 本地自动激活 ──────────────────
    #
    # 三个端点都**绝不抛**：领试用失败不该让首启向导卡死，用户随时能退回
    # 「粘贴授权码」那条老路。网络调用走 to_thread，别把 web loop 堵在外网上。

    async def _maybe_enable_hosted_ai(app_) -> None:
        """领取试用成功后立即接入托管 AI 网关（设备令牌）并热重建 AIClient。

        网关只对台账里的指纹发令牌 → 首启「领试用」是接入前置；此钩子把
        「领完 → AI 能用」压缩到同一次交互，无需重启/等守护线程下一轮。
        已配置（用户自有 Key 或已持有令牌）时零开销跳过；全程 best-effort 绝不抛。
        """
        try:
            import asyncio

            from src.utils.golive import _is_placeholder
            ai = (_cfg_or_none().get("ai") or {})
            if not _is_placeholder(ai.get("api_key")):
                return  # 已有可用 Key（自有或令牌），别反复 reload
            from src.ai.hosted_gateway import ensure_hosted_ai
            if not await asyncio.to_thread(ensure_hosted_ai, _CONFIG_MANAGER):
                return
            from src.web.routes.unified_inbox_setup_routes import reload_ai_runtime
            await reload_ai_runtime(app_, _CONFIG_MANAGER)
            logger.info("[hosted-ai] 领取试用后已自动接入 AI 网关并热生效")
        except Exception:
            logger.debug("[hosted-ai] claim 后接入网关失败（守护线程会重试）", exc_info=True)

    @app.post("/api/admin/license/trial-claim")
    async def api_trial_claim(request: Request):
        """向官网领取 7 天试用（按本机机器码去重，同一台机器重复领拿回同一单）。"""
        api_auth(request)
        import asyncio

        from src.licensing import trial_claim_client as tc

        try:
            body = await request.json()
        except Exception:
            body = {}
        contact = str((body or {}).get("contact") or "")
        res = await asyncio.to_thread(tc.claim, contact, config=_cfg_or_none(), source="desktop")
        # 去重命中已签发的单子 → 授权当场就在响应里，直接激活，省掉一轮轮询。
        if res.get("ok") and res.get("license"):
            res = _consume_trial_payload(res, res.pop("license", ""), "")
        if res.get("ok"):
            await _maybe_enable_hosted_ai(request.app)
        return res

    @app.get("/api/admin/license/trial-claim")
    async def api_trial_claim_status(request: Request):
        """轮询履约进度；授权/加量凭证一旦就绪即当场落地（激活 / 入账）。"""
        api_auth(request)
        import asyncio

        from src.licensing import trial_claim_client as tc

        res = await asyncio.to_thread(tc.poll, config=_cfg_or_none())
        if res.get("ok"):
            res = _consume_trial_payload(
                res, res.pop("license", ""), res.pop("topup_voucher", ""))
        if res.get("ok") and res.get("claimed"):
            await _maybe_enable_hosted_ai(request.app)
        return res

    @app.post("/api/admin/license/trial-bind-code")
    async def api_trial_bind_code(request: Request):
        """取「加客服领字符」的一次性绑定码 + TG/WhatsApp 深链。"""
        api_auth(request)
        import asyncio

        from src.licensing import trial_claim_client as tc

        return await asyncio.to_thread(tc.bind_code, config=_cfg_or_none())

    @app.post("/api/admin/license/trial-funnel")
    async def api_trial_funnel(request: Request):
        """首启向导漏斗埋点转发（桌面壳 → 官网 /api/track）。

        事件名白名单在 trial_claim_client.FUNNEL_EVENTS 收口；这里与其余 trial
        端点同款「绝不抛」——埋点是旁路，任何失败都不该在向导侧冒泡。
        """
        api_auth(request)
        import asyncio

        from src.licensing import trial_claim_client as tc

        try:
            body = await request.json()
        except Exception:
            body = {}
        event = str((body or {}).get("event") or "")
        return await asyncio.to_thread(tc.funnel, event, config=_cfg_or_none())
