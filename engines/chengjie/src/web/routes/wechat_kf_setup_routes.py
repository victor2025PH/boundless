# -*- coding: utf-8 -*-
"""微信客服（企业微信）五步接入向导的后端（实施97 线 A · 引导页 ``/workspace/connect/wechat_kf``）。

主管专属（``api_auth`` + ``_require_supervisor``）：
- ``GET  /api/setup/wechat_kf/egress-ip``      本机出站公网 IP（要填进企微「可信 IP」）
- ``POST /api/setup/wechat_kf/test``           凭证测试：gettoken → kf/account/list → 三项检查（人话 + 怎么办）
- ``GET  /api/setup/wechat_kf/accounts``       客服账号卡片（含是否已绑定）
- ``POST /api/setup/wechat_kf/accounts``       新建客服账号（头像默认品牌图，经 media/upload）
- ``POST /api/setup/wechat_kf/bind``           绑定所选客服账号：注册表 official 行 + 配置 open_kfid + 热拉起 worker
- ``POST /api/setup/wechat_kf/contact-way``    客户扫码链接 + 二维码（data URL，可下载）
- ``POST /api/setup/wechat_kf/reception``      AI 接待：档位（manual/review/auto_ai）+ 人设 + 欢迎语

纯逻辑在 ``src/integrations/wechat_kf_setup.py``；这里只做鉴权、取配置、拼装。
"""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Any, Dict, List

from fastapi import Depends, FastAPI, HTTPException, Request

from src.integrations.wechat_kf import WeChatKfClient
from src.integrations.wechat_kf_setup import (
    assemble_checks, describe_kf_errcode, fetch_egress_ip, normalize_kf_accounts,
)

logger = logging.getLogger(__name__)

PLATFORM = "wechat_kf"
_DEFAULT_AVATAR = Path(__file__).resolve().parents[1] / "static" / "brand" / "chatx.png"


def _cfg(request: Request) -> Dict[str, Any]:
    cm = getattr(request.app.state, "config_manager", None)
    return dict(getattr(cm, "config", None) or {}) if cm is not None else {}


def _kf_block(cfg: Dict[str, Any]) -> Dict[str, Any]:
    blk = cfg.get("wechat_kf") or {}
    return dict(blk) if isinstance(blk, dict) else {}


def _client_from(cfg: Dict[str, Any], corpid: str = "", secret: str = "") -> WeChatKfClient:
    blk = _kf_block(cfg)
    return WeChatKfClient(str(corpid or blk.get("corpid") or ""), str(secret or blk.get("secret") or ""))


def _bound_kfids() -> set:
    try:
        from src.integrations.account_registry import get_account_registry
        rows = get_account_registry().list(platform=PLATFORM) or []
        return {str(r.get("account_id") or "") for r in rows if str(r.get("status") or "") != "removed"}
    except Exception:
        return set()


def _save_patch(request: Request, patch: Dict[str, Any]) -> bool:
    cm = getattr(request.app.state, "config_manager", None)
    if cm is None:
        return False
    fn = getattr(cm, "save_overlay_patch", None)
    if callable(fn):
        try:
            return bool(fn(patch))
        except Exception:
            logger.debug("[kf_setup] save_overlay_patch 失败", exc_info=True)
            return False
    return False


def _qr_data_url(text: str) -> str:
    try:
        import qrcode
        qr = qrcode.QRCode(box_size=8, border=2)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        logger.debug("[kf_setup] 二维码渲染失败", exc_info=True)
        return ""


async def _hot_start(cfg: Dict[str, Any]) -> None:
    try:
        from src.integrations.account_orchestrator import (
            ensure_builtin_workers, get_orchestrator, orchestrator_enabled,
        )
        if orchestrator_enabled(cfg):
            ensure_builtin_workers(cfg)
            await get_orchestrator(cfg).start_loop()
    except Exception:
        logger.debug("[kf_setup] 热拉起编排器失败（重启后生效）", exc_info=True)


def register_wechat_kf_setup_routes(app: FastAPI, api_auth: Any) -> None:
    from src.web.routes.unified_inbox_auth import _require_supervisor

    def _guard(request: Request) -> None:
        _require_supervisor(request)

    @app.get("/api/setup/wechat_kf/egress-ip")
    async def api_kf_egress_ip(request: Request, _=Depends(api_auth)):
        _guard(request)
        import asyncio
        res = await asyncio.get_event_loop().run_in_executor(None, fetch_egress_ip)
        return {"ok": bool(res.get("ok")), "ip": res.get("ip") or "", "source": res.get("source") or "",
                "errors": res.get("errors") or []}

    @app.post("/api/setup/wechat_kf/test")
    async def api_kf_test(request: Request, _=Depends(api_auth)):
        """凭证测试（不保存）：body ``{corpid?, secret?}``，缺省用已保存配置。"""
        _guard(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        cfg = _cfg(request)
        corpid = str((body or {}).get("corpid") or "").strip()
        secret = str((body or {}).get("secret") or "").strip()
        client = _client_from(cfg, corpid, secret)
        if not (client.corpid and client.secret):
            return {"ok": False, "checks": [{"id": "credentials", "ok": False, "errcode": 0,
                                             "problem": "CorpID / Secret 还没填", "fix": "先填这两项再测试"}],
                    "accounts": [], "egress_ip": ""}
        import asyncio
        egress = await asyncio.get_event_loop().run_in_executor(None, fetch_egress_ip)
        egress_ip = str(egress.get("ip") or "")
        tok = await client.get_token(force=True)
        lst = await client.list_accounts() if tok.get("ok") else None
        out = assemble_checks(tok, lst, egress_ip=egress_ip)
        bound = _bound_kfids()
        for a in out["accounts"]:
            a["bound"] = a["open_kfid"] in bound
        out["egress_ip"] = egress_ip
        out["corpid"] = client.corpid
        return out

    @app.get("/api/setup/wechat_kf/accounts")
    async def api_kf_accounts(request: Request, _=Depends(api_auth)):
        _guard(request)
        client = _client_from(_cfg(request))
        res = await client.list_accounts()
        if not res.get("ok"):
            h = describe_kf_errcode(res.get("errcode"))
            return {"ok": False, "accounts": [], "errcode": int(res.get("errcode") or -1),
                    "problem": h["problem"], "fix": h["fix"]}
        return {"ok": True, "accounts": normalize_kf_accounts(
            (res.get("data") or {}).get("account_list"), bound_ids=_bound_kfids())}

    @app.post("/api/setup/wechat_kf/accounts")
    async def api_kf_account_add(request: Request, _=Depends(api_auth)):
        """新建客服账号：body ``{name, avatar_data_url?}``；未给头像用品牌图（企微要求头像必填）。"""
        _guard(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = str((body or {}).get("name") or "").strip()[:16]
        if not name:
            raise HTTPException(400, "name required")
        client = _client_from(_cfg(request))
        import tempfile
        avatar_b64 = str((body or {}).get("avatar_data_url") or "")
        tmp_path = ""
        try:
            if avatar_b64.startswith("data:image") and "," in avatar_b64:
                raw = base64.b64decode(avatar_b64.split(",", 1)[1])
                with tempfile.NamedTemporaryFile("wb", suffix=".png", delete=False) as fh:
                    fh.write(raw)
                    tmp_path = fh.name
                avatar_path = tmp_path
            else:
                avatar_path = str(_DEFAULT_AVATAR)
            up = await client.upload_media(avatar_path, "image")
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink()
                except Exception:
                    pass
        if not up.get("ok"):
            h = describe_kf_errcode(up.get("errcode"))
            return {"ok": False, "stage": "avatar", "errcode": int(up.get("errcode") or -1),
                    "problem": h["problem"] or str(up.get("errmsg") or ""), "fix": h["fix"]}
        res = await client.api("kf/account/add", json_body={
            "name": name, "media_id": str((up.get("data") or {}).get("media_id") or "")})
        if not res.get("ok"):
            h = describe_kf_errcode(res.get("errcode"))
            return {"ok": False, "stage": "create", "errcode": int(res.get("errcode") or -1),
                    "problem": h["problem"] or str(res.get("errmsg") or ""), "fix": h["fix"]}
        kfid = str((res.get("data") or {}).get("open_kfid") or "")
        return {"ok": True, "open_kfid": kfid, "name": name}

    @app.post("/api/setup/wechat_kf/bind")
    async def api_kf_bind(request: Request, _=Depends(api_auth)):
        """绑定客服账号：body ``{accounts:[{open_kfid, name?}]}`` → 注册表 official 行（account_id=open_kfid）
        + 配置 ``wechat_kf.open_kfid``（首个）+ 热拉起。"""
        _guard(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        items = (body or {}).get("accounts") or []
        kfids: List[Dict[str, str]] = []
        for it in items:
            if isinstance(it, str):
                kfid, nm = it.strip(), ""
            elif isinstance(it, dict):
                kfid, nm = str(it.get("open_kfid") or "").strip(), str(it.get("name") or "").strip()
            else:
                continue
            if kfid:
                kfids.append({"open_kfid": kfid, "name": nm})
        if not kfids:
            raise HTTPException(400, "accounts required")
        cfg = _cfg(request)
        blk = _kf_block(cfg)
        if not (blk.get("corpid") and blk.get("secret")):
            raise HTTPException(409, "credentials_missing")
        from src.integrations.account_registry import get_account_registry
        reg = get_account_registry()
        rows = []
        for it in kfids:
            row = reg.upsert(PLATFORM, it["open_kfid"], mode="official", status="online",
                             label=it["name"] or None, merge_meta=True,
                             meta={"open_kfid": it["open_kfid"]})
            rows.append({"account_id": row.get("account_id"), "label": row.get("label"), "status": row.get("status")})
        patch: Dict[str, Any] = {"wechat_kf": {"enabled": True, "open_kfid": kfids[0]["open_kfid"]}}
        # 三态解析（桌面模式从未写过=默认开）——字面直读会让升级安装重复写 true
        from src.integrations.platform_login import resolve_login_switch
        if not resolve_login_switch(cfg, "platform_login.orchestrator_enabled"):
            patch["platform_login"] = {"orchestrator_enabled": True}
        saved = _save_patch(request, patch)
        await _hot_start(_cfg(request))
        return {"ok": True, "saved": saved, "accounts": rows}

    @app.post("/api/setup/wechat_kf/contact-way")
    async def api_kf_contact_way(request: Request, _=Depends(api_auth)):
        """客户扫码入口：body ``{open_kfid, scene?}`` → ``{url, qr_image}``。"""
        _guard(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        kfid = str((body or {}).get("open_kfid") or "").strip()
        if not kfid:
            raise HTTPException(400, "open_kfid required")
        client = _client_from(_cfg(request))
        res = await client.add_contact_way(kfid, str((body or {}).get("scene") or "chatx")[:64])
        if not res.get("ok"):
            h = describe_kf_errcode(res.get("errcode"))
            return {"ok": False, "errcode": int(res.get("errcode") or -1),
                    "problem": h["problem"] or str(res.get("errmsg") or ""), "fix": h["fix"]}
        url = str((res.get("data") or {}).get("url") or "")
        return {"ok": bool(url), "url": url, "qr_image": _qr_data_url(url) if url else ""}

    @app.post("/api/setup/wechat_kf/reception")
    async def api_kf_reception(request: Request, _=Depends(api_auth)):
        """AI 接待设置：body ``{open_kfid, tier: manual|review|auto_ai, persona_id?, welcome_text?}``。"""
        _guard(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        kfid = str((body or {}).get("open_kfid") or "").strip()
        tier = str((body or {}).get("tier") or "").strip().lower()
        if not kfid:
            raise HTTPException(400, "open_kfid required")
        if tier and tier not in ("manual", "review", "auto_ai"):
            raise HTTPException(400, "bad tier")
        cfg = _cfg(request)
        out: Dict[str, Any] = {"ok": True, "open_kfid": kfid}
        from src.integrations.account_registry import get_account_registry
        reg = get_account_registry()
        if reg.get(PLATFORM, kfid) is None:
            raise HTTPException(404, "account_not_bound")
        if "persona_id" in (body or {}):
            pid = str((body or {}).get("persona_id") or "").strip()
            reg.upsert(PLATFORM, kfid, meta={"persona_id": pid, "persona_ids": [pid] if pid else [],
                                             "persona_id_auto": False}, merge_meta=True)
            out["persona_id"] = pid
        if "welcome_text" in (body or {}):
            wt = str((body or {}).get("welcome_text") or "")[:400]
            out["welcome_saved"] = _save_patch(request, {"wechat_kf": {"welcome_text": wt}})
        if tier:
            try:
                from src.inbox.account_bulk_mode import apply_account_bulk
                store = getattr(request.app.state, "inbox_store", None)
                try:
                    actor = str(request.session.get("username") or "")
                except Exception:
                    actor = ""
                res = apply_account_bulk(store, PLATFORM, kfid, tier, actor=actor, config=cfg)
                out["tier"] = tier
                out["tier_result"] = {k: res.get(k) for k in ("changed", "cancelled_l2") if isinstance(res, dict)}
            except Exception:
                logger.debug("[kf_setup] 账号档位写入失败", exc_info=True)
                out["tier"] = tier
                out["tier_result"] = {"error": "apply_failed"}
        return out


__all__ = ["register_wechat_kf_setup_routes"]
