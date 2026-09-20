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
    """P0-4 字符额度快照（无额度授权 → included=0/used=0）。绝不抛。

    ``lic_id`` 仅供进程内消费（quota_state 预测取逐日消耗用）——端点响应按
    显式白名单构造，**绝不**把它带给前端（防泄漏门禁钉着字段集）。
    """
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
            "lic_id": str(q.get("lic_id") or ""),
        }
    except Exception:
        return {"included_chars": 0, "used_chars": 0,
                "remaining_chars": None, "exceeded": False,
                "source": "license", "trial_hours_left": None,
                "trial_expired": False, "lic_id": ""}


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


def _consume_trial_payload(res: dict, license_token: str, voucher: str,
                           extras: Any = None) -> dict:
    """把官网回来的授权/加量凭证就地落地，并把结果并进响应。

    刻意**不把明文回传给前端**：授权已写进 license.key、加量已入账，再往浏览器
    送一份只是多一个泄漏面。前端要的只是「成了没有」。

    ``res["applied_license"]``（本次真的落盘了一份授权）与 summarize 的
    ``activated``（历史上激活过）是两个语义——会员页「同步授权」按前者决定
    要不要刷新，用后者会无限刷新循环。
    """
    from src.licensing import trial_claim_client as tc

    if license_token:
        try:
            act = apply_license_token(license_token)
            if act.get("ok"):
                tc.mark_activated(_cfg_or_none(),
                                  token_sha=tc.token_sha(license_token))
                res["activated"] = True
                res["applied_license"] = True
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

    # 追加凭证（邀请见面礼/邀请人奖励等）：逐张兑换，quota_store 按 ref 幂等，
    # duplicate_ref 与成功同样标记「已入账」——绝不重复入账，也绝不反复重试。
    credited = 0
    for v in (extras or []):
        try:
            from src.licensing.topup_voucher import redeem_topup_voucher

            tok = str((v or {}).get("voucher") or "")
            ref = str((v or {}).get("ref") or "")
            if not tok or not ref:
                continue
            rd = redeem_topup_voucher(tok)
            err = str(rd.get("error") or "")
            if rd.get("ok") or err == "duplicate_ref":
                chars = int(rd.get("chars") or (v or {}).get("chars") or 0)
                tc.mark_extra_voucher(ref, chars if rd.get("ok") else 0, _cfg_or_none())
                if rd.get("ok"):
                    credited += chars
                    logger.info("[trial-claim] 追加凭证已入账 ref=%s +%s 字符", ref, chars)
            elif err == "not_licensed":
                # 授权还没落地（先到的凭证）：留给下一轮，等 license 激活后再兑。
                continue
            else:
                logger.warning("[trial-claim] 追加凭证兑换失败 ref=%s err=%s", ref, err)
        except Exception:  # pragma: no cover
            logger.debug("[trial-claim] 追加凭证异常", exc_info=True)
    if credited:
        res["extra_credited_chars"] = credited
    return res


#: 用量水位上报的进程内去抖（线程正在飞就不再起新线程；真正的节流在
#: trial_claim_client.maybe_report_usage 里按状态文件判）。
_USAGE_BEACON_BUSY = False

#: ops「🎁 邀请裂变」卡的官网聚合缓存（300s TTL，防 ops 轮询把外网当内网打）。
_REFERRAL_STATS_CACHE: dict = {"ts": 0.0, "data": None}


def _spawn_usage_beacon(used_chars: int) -> None:
    """fire-and-forget 上报本机消耗水位（邀请达标判定的数据源）。

    挂在坐席顶栏额度端点的旁路上：该端点 60s/客户端 一轮，是全站唯一「额度变化
    就会被路过」的常驻脉搏——不必为水位上报另起后台循环。绝不阻塞请求、绝不抛。
    """
    global _USAGE_BEACON_BUSY
    try:
        if _USAGE_BEACON_BUSY or used_chars <= 0:
            return
        import threading

        from src.licensing import trial_claim_client as tc

        cfg = _cfg_or_none()

        def _run() -> None:
            global _USAGE_BEACON_BUSY
            try:
                tc.maybe_report_usage(used_chars, config=cfg)
            except Exception:
                logger.debug("[trial-claim] 用量水位上报失败（忽略）", exc_info=True)
            finally:
                _USAGE_BEACON_BUSY = False

        _USAGE_BEACON_BUSY = True
        threading.Thread(target=_run, name="trial-usage-beacon", daemon=True).start()
    except Exception:
        _USAGE_BEACON_BUSY = False
        logger.debug("[trial-claim] 用量水位线程启动失败（忽略）", exc_info=True)


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
            _spawn_usage_beacon(int(q.get("used_chars") or 0))
            payload = {
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
            # quotawall v2（2026-08-21）：四表合议裁决随同一次轮询下发（feat=qs1，
            # 旧前端忽略新字段零影响；新前端 feat 探测，缺 state 自动走 legacy）。
            # 单独 try：合议失败只丢 state，legacy 徽章字段绝不陪葬。
            try:
                from src.licensing.quota_state import collect_quota_state

                payload["state"] = collect_quota_state(
                    quota=q, level=payload["level"],
                    request=request, config_manager=_CONFIG_MANAGER)
            except Exception:
                logger.debug("[license] quota_state 合议失败（略过）", exc_info=True)
            return payload
        except Exception:  # pragma: no cover - 观测端点绝不影响工作台
            logger.debug("[license] 坐席额度摘要失败", exc_info=True)
            return {"ok": False, "visible": False, "level": "ok"}

    @app.get("/api/admin/license/topup-trend")
    async def api_license_topup_trend(request: Request, days: int = 14):
        """近 N 天入账台账按日聚合（额度墙漏斗卡「服务端到账真值」，P2.5）。

        ``qw_credited`` 埋点只统计「坐席开着页面等到 watch 侦测命中」的场景，
        页面关早了就漏计；本端点读 ``license_char_topup`` 台账＝全部真实到账
        （凭证兑换/手工直充/自动履约），给 ops 漏斗卡做对账分母。响应只含
        day/n/chars 聚合数字，无 lic_id 无订单号。
        """
        api_auth(request)
        try:
            from src.licensing.quota_store import current_topup_daily

            rows = current_topup_daily(int(days or 14))
            return {
                "ok": True,
                "days": rows,
                "total_n": sum(int(r.get("n") or 0) for r in rows),
                "total_chars": sum(int(r.get("chars") or 0) for r in rows),
            }
        except Exception:  # pragma: no cover - 观测端点绝不抛
            logger.debug("[license] topup-trend 读取失败", exc_info=True)
            return {"ok": True, "days": [], "total_n": 0, "total_chars": 0}

    def _plan_meta(st) -> dict:
        """L-4 B（#197 / D-L7）：生效档位 + 来源，与顶栏徽标同源（feature_gate.gate_snapshot）。

        skuio 机实录：系统设置卡「授权：社区模式（未检测到授权文件）」与右上「旗舰版」互矛盾
        ——右上读 plan_override（内测种子 flagship），卡只读授权文件。两面都改读同一快照：
        ``effective_plan`` + ``plan_source``（license / override / community），页面据此措辞。
        """
        try:
            from src.licensing.feature_gate import gate_snapshot
            cfg = getattr(config_manager, "config", None) if config_manager is not None else None
            snap = gate_snapshot(cfg if isinstance(cfg, dict) else None, st)
            return {"effective_plan": snap.get("plan", "community"),
                    "plan_source": snap.get("plan_source", "community"),
                    "gate_enabled": bool(snap.get("enabled", False))}
        except Exception:
            return {"effective_plan": "community", "plan_source": "community",
                    "gate_enabled": False}

    @app.get("/api/admin/license")
    async def api_admin_license(request: Request):
        api_auth(request)
        try:
            from src.licensing import get_license_manager

            st = get_license_manager().status()
            data = st.to_dict()
            data["quota"] = _quota_snapshot()
            data.update(_plan_meta(st))
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

            from src.ai.hosted_gateway import (
                ensure_hosted_ai, ensure_hosted_asr, ensure_hosted_telegram,
                ensure_hosted_vision, ensure_hosted_voice, ensure_hosted_chatx)
            from src.utils.golive import _is_placeholder

            # Telegram 托管凭据独立于 AI Key 状态尝试（池未配则静默跳过）——
            # 即便 AI 已配好也要补领凭据，别被 AI 的早返挡掉。
            try:
                await asyncio.to_thread(ensure_hosted_telegram, _CONFIG_MANAGER)
            except Exception:
                logger.debug("[hosted-tg] claim 后领取凭据失败（忽略）", exc_info=True)

            async def _link_vision() -> None:
                """识图与聊天共用设备令牌：令牌一到就顺手把识图也接上。

                否则识图供给只在 main.py 启动时跑过那一次——首启「未领试用」时它必然
                失败，而领完试用后没人再喊它，用户要等到下次重启才有识图（2026-07-31）。
                """
                try:
                    await asyncio.to_thread(ensure_hosted_vision, _CONFIG_MANAGER)
                except Exception:
                    logger.debug("[hosted-vision] claim 后接入识图失败（忽略）", exc_info=True)

            async def _link_voice() -> None:
                """克隆语音/语音识别与识图同理：令牌一到就顺手接上（B21）。

                main.py 启动跑的是五件套（ai/telegram/vision/voice/asr），但首启
                「未领试用」时全部失败；本钩子此前只补前三件，voice/asr 要等
                下次重启才接通——「领完试用聊天正常、语音登记却报未接入」的
                窗口期即此（2026-08-21）。
                """
                try:
                    await asyncio.to_thread(ensure_hosted_voice, _CONFIG_MANAGER)
                except Exception:
                    logger.debug("[hosted-voice] claim 后接入克隆语音失败（忽略）", exc_info=True)
                try:
                    await asyncio.to_thread(ensure_hosted_asr, _CONFIG_MANAGER)
                except Exception:
                    logger.debug("[hosted-asr] claim 后接入语音识别失败（忽略）", exc_info=True)
                try:
                    await asyncio.to_thread(ensure_hosted_chatx, _CONFIG_MANAGER)
                except Exception:
                    logger.debug("[hosted-chatx] claim 后接入 ChatX 失败（忽略）", exc_info=True)

            ai = (_cfg_or_none().get("ai") or {})
            if not _is_placeholder(ai.get("api_key")):
                await _link_vision()
                await _link_voice()
                return  # AI 已有可用 Key（自有或令牌），别反复 reload
            if not await asyncio.to_thread(ensure_hosted_ai, _CONFIG_MANAGER):
                return
            await _link_vision()
            await _link_voice()
            from src.web.routes.unified_inbox_setup_routes import reload_ai_runtime
            await reload_ai_runtime(app_, _CONFIG_MANAGER)
            logger.info("[hosted-ai] 领取试用后已自动接入 AI 网关并热生效")
        except Exception:
            logger.debug("[hosted-ai] claim 后接入网关失败（守护线程会重试）", exc_info=True)

    @app.post("/api/admin/license/trial-claim")
    async def api_trial_claim(request: Request):
        """向官网领取免费额度（100 万字符 · 按本机机器码去重，重复领拿回同一单）。

        body 可带 ``invite_code``（好友邀请码，选填）——官网 referral 台账据此归因，
        双方奖励凭证后续经轮询自动入账。
        """
        api_auth(request)
        import asyncio

        from src.licensing import trial_claim_client as tc

        try:
            body = await request.json()
        except Exception:
            body = {}
        contact = str((body or {}).get("contact") or "")
        invite_code = str((body or {}).get("invite_code") or "")
        res = await asyncio.to_thread(
            tc.claim, contact, config=_cfg_or_none(), source="desktop",
            invite_code=invite_code)
        # 去重命中已签发的单子 → 授权当场就在响应里，直接激活，省掉一轮轮询。
        if res.get("ok") and res.get("license"):
            res = _consume_trial_payload(res, res.pop("license", ""), "")
        if res.get("ok"):
            await _maybe_enable_hosted_ai(request.app)
        return res

    @app.get("/api/admin/license/trial-claim")
    async def api_trial_claim_status(request: Request):
        """轮询履约进度；授权/加量/邀请奖励凭证一旦就绪即当场落地（激活 / 入账）。

        存量升级：厂商机对旧规格授权重签后，本端点按 license 内容指纹发现变化并
        自动重新落盘（``applied_license=True``）——会员页「同步授权」就是打这一发。
        """
        api_auth(request)
        import asyncio

        from src.licensing import trial_claim_client as tc

        res = await asyncio.to_thread(tc.poll, config=_cfg_or_none())
        if res.get("ok"):
            res = _consume_trial_payload(
                res, res.pop("license", ""), res.pop("topup_voucher", ""),
                res.pop("extra_vouchers", None))
        if res.get("ok") and res.get("claimed"):
            await _maybe_enable_hosted_ai(request.app)
        return res

    @app.get("/api/admin/license/referral")
    async def api_license_referral(request: Request):
        """「邀请好友送字符」会员页卡片数据：我的邀请码 / 分享链接 / 进度统计。

        数据源=官网 referral 台账（invite-info），claim 状态在本地。未领取过
        免费额度 → ``{ok:false, error:not_claimed}``，前端据此引导先注册领取。
        与其余 trial 端点同款「绝不抛」。
        """
        api_auth(request)
        import asyncio

        from src.licensing import trial_claim_client as tc

        return await asyncio.to_thread(tc.invite_info, config=_cfg_or_none())

    @app.get("/api/admin/referral-stats")
    async def api_referral_stats(request: Request):
        """邀请裂变全局聚合（官网台账代理，厂商 ops 看板「🎁 邀请裂变」卡消费）。

        默认关（``licensing.trial.referral_stats.enabled``）——聚合数字是**全站**
        口径，只有厂商自己的 ops 实例该看；客户实例开了也只会看到别人的总账。
        官网端点纯计数零 PII；此处 300s TTL 缓存防 ops 轮询打穿外网。绝不抛。
        """
        api_auth(request)
        import asyncio
        import json as _json
        import time as _time
        import urllib.request as _rq

        cfg = ((_cfg_or_none().get("licensing") or {}).get("trial") or {})
        rs = cfg.get("referral_stats") or {}
        if not bool(rs.get("enabled", False)):
            return {"ok": True, "enabled": False}
        now = _time.time()
        cached = _REFERRAL_STATS_CACHE.get("data")
        if cached is not None and now - float(_REFERRAL_STATS_CACHE.get("ts") or 0) < 300:
            return cached
        from src.licensing import trial_claim_client as tc

        site = str(rs.get("site_url") or "").rstrip("/") or tc.site_url(_cfg_or_none())

        def _fetch() -> dict:
            req = _rq.Request(f"{site}/api/trial/referral-stats",
                              headers={"accept": "application/json"})
            with _rq.urlopen(req, timeout=10) as resp:
                return _json.loads(resp.read().decode("utf-8") or "{}")

        try:
            data = await asyncio.to_thread(_fetch)
        except Exception:
            logger.debug("[referral-stats] 官网聚合不可达（忽略）", exc_info=True)
            return {"ok": False, "enabled": True, "error": "unreachable"}
        out = {"ok": True, "enabled": True}
        for k in ("codes", "registered", "qualified", "flagged",
                  "invitee_rewarded", "inviter_rewarded", "chars_granted"):
            out[k] = int(data.get(k) or 0)
        _REFERRAL_STATS_CACHE["ts"] = now
        _REFERRAL_STATS_CACHE["data"] = out
        return out

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
