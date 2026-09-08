"""QQ 协议登录（个人号）登录 provider —— ``register_login_provider("qq", "protocol", …)``。

**扫码就在本窗口完成**（``login_kind=qr``）：智聊自研的 QQ 协议边车
（``services/qq-personal``，随桌面壳打包、由壳自动拉起）注入本机 QQ 客户端、驱动其内核，
签名由 QQ 自身完成——**不连外部签名服务、不需任何第三方 token/审核**。本 provider 契约
与 ``whatsapp_baileys_login`` 逐一对齐（start 拉码 → poll 到 authorized 落库 + 富集）：

- ``start``：向边车 ``x_get_login_qrcode`` 拉一张二维码 → 返回 ``qr_image``（弹窗即显示）；
  边车不可达 → ``reason_code=service_down`` 早退（前端给「重启连接服务」按钮）；
  QQ 未安装（``/health.qq_installed=false`` 已由 readiness 拦成 ``qq_not_installed`` blocker）。
- ``poll``：探 ``get_login_info``——已登录（拿到 uin）→ ``authorized``，把 QQ 号登进账号注册表
  （``mode=protocol``、meta 快照 ``uin/nickname``、``merge_meta`` 绝不整块覆盖人设绑定），
  补自身昵称/头像（QQ 头像 CDN 按号取，零额外接口）；``-403``（协议端在、QQ 未登录）→
  ``pending``（继续等扫码），并把最新二维码随 poll 下发（到期自动刷新）。

网络经 ``qq_milky.MilkyClient``（``http``/``ws_connect`` 可注入）。端点与 token 由边车自协商，
**用户不可见、不需填写**（``service_base_url`` 默认 127.0.0.1:8792）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.integrations.account_registry import get_account_registry
from src.integrations.platform_login import register_login_provider
from src.integrations.qq_milky import (
    PLATFORM,
    MilkyClient,
    MilkyError,
    avatar_url_for,
    fetch_login_qrcode,
    protocol_enabled,
    risk_acknowledged,
    service_base_url,
    service_token,
)

logger = logging.getLogger(__name__)

_registered = False

REASON_SERVICE_DOWN = "service_down"
REASON_NEEDS_SERVER_SETUP = "needs_server_setup"
REASON_NEEDS_RISK_ACK = "needs_risk_ack"


def _client_factory(config: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> MilkyClient:
    return MilkyClient(service_base_url(config, meta), service_token(config, meta), timeout=8.0)


async def probe_login(client: MilkyClient) -> Dict[str, Any]:
    """探一次协议端：``{"state": authorized|not_logged_in|down, "uin", "nickname", "detail"}``。"""
    try:
        info = await client.call("get_login_info")
    except MilkyError as ex:
        if ex.not_logged_in:
            return {"state": "not_logged_in", "uin": "", "nickname": "",
                    "detail": "连接服务已就绪，QQ 尚未扫码登录"}
        return {"state": "down", "uin": "", "nickname": "", "detail": str(ex)[:160]}
    except Exception as ex:  # noqa: BLE001
        return {"state": "down", "uin": "", "nickname": "", "detail": str(ex)[:160]}
    uin = str(info.get("uin") or "").strip()
    if not uin:
        return {"state": "down", "uin": "", "nickname": "", "detail": "get_login_info 未返回 uin"}
    return {"state": "authorized", "uin": uin, "nickname": str(info.get("nickname") or ""),
            "detail": ""}


def _persist_authorized(config: Dict[str, Any], uin: str, nickname: str) -> None:
    """登记账号 + 快照端点进 meta + 默认人设 + 自身昵称头像（全部 best-effort）。"""
    try:
        get_account_registry().upsert(
            PLATFORM, uin, mode="protocol", status="online",
            meta={"milky_url": service_base_url(config), "milky_token": service_token(config),
                  "uin": uin, "nickname": nickname},
            merge_meta=True)
        try:
            from src.ai.persona_voice import ensure_account_default_persona
            ensure_account_default_persona(get_account_registry(), PLATFORM, uin, config)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        logger.debug("[qq_protocol] 注册表写入失败", exc_info=True)


async def _enrich_self(config: Dict[str, Any], uin: str, nickname: str) -> None:
    try:
        from src.integrations.account_self_profile import enrich_from_fields
        await enrich_from_fields(PLATFORM, uin, name=nickname, avatar_url=avatar_url_for(uin),
                                 config=config)
    except Exception:  # noqa: BLE001
        logger.debug("[qq_protocol] self_profile 富集失败（忽略）", exc_info=True)


def make_provider(config: Dict[str, Any]):
    async def _provider(request: Any, platform: str, mode: str, account_id: str,
                        ctx: Optional[Dict[str, Any]] = None):
        base = service_base_url(config)
        # 个人号一次性风险须知：未确认 → 先弹协议页（前端据 reason_code 渲染确认按钮，
        # 用户勾选同意后写 platform_login.qq.risk_acknowledged_at 再重来），不直接出码。
        if not risk_acknowledged(config):
            return {"instruction": "QQ 个人号为非官方接入、有账号风控风险，请先阅读并同意《QQ 个人号接入协议与风险须知》。",
                    "instruction_key": "inbox.connect.instr_qq_risk",
                    "reason_code": REASON_NEEDS_RISK_ACK}
        client = _client_factory(config)
        first = await probe_login(client)
        if first["state"] == "down":
            # 连接服务（自研边车）未就绪：早退，前端给「重启连接服务」按钮，别对着空转挂满 TTL
            return {"instruction": f"无法连接 QQ 协议服务（{base}）：{first['detail']}。"
                                   "请点「重新开始」，或在设置里重启连接服务。",
                    "instruction_key": "inbox.connect.instr_qq_down",
                    "reason_code": REASON_SERVICE_DOWN, "detail": first["detail"]}

        # 已就绪但未登录：拉一张二维码，弹窗立即显示（首帧就有码，不用等 poll）
        qr = {} if first["state"] == "authorized" else await fetch_login_qrcode(client)

        async def _poll(session: Any) -> Dict[str, Any]:
            res = await probe_login(client)
            if res["state"] == "authorized":
                _persist_authorized(config, res["uin"], res["nickname"])
                await _enrich_self(config, res["uin"], res["nickname"])
                return {"status": "authorized", "account_id": res["uin"],
                        "detail": res["nickname"]}
            if res["state"] == "down":
                return {"status": "pending", "detail": res["detail"], "hint_code": REASON_SERVICE_DOWN}
            # 仍未登录：把最新二维码随 poll 下发（边车每次拉码即刷新，弹窗到期自动换新码）
            fresh = await fetch_login_qrcode(client)
            out: Dict[str, Any] = {"status": "pending", "detail": res["detail"]}
            if fresh.get("qr_image"):
                out["qr_image"] = fresh["qr_image"]
                if fresh.get("expire_sec"):
                    out["qr_expire_sec"] = fresh["expire_sec"]
            return out

        async def _cancel(session: Any) -> None:
            try:
                await client.call("x_logout")
            except Exception:  # noqa: BLE001
                logger.debug("[qq_protocol] cancel/x_logout 失败（忽略）", exc_info=True)

        return {
            "qr_image": qr.get("qr_image", ""),
            "qr_url": qr.get("qr_url", ""),
            "qr_expire_sec": qr.get("expire_sec", 0),
            "instruction": "用手机 QQ 扫描本窗口二维码即可接入（智聊内置连接，无需安装其它程序）。",
            "instruction_key": "inbox.connect.instr_qq",
            "poll": _poll,
            "cancel": _cancel,
            "state": {"base": base},
        }

    return _provider


def maybe_register(config: Dict[str, Any]) -> bool:
    """按需注册 QQ 协议登录 provider（幂等）。仅 ``platform_login.qq.protocol_enabled`` 为真时注册。"""
    global _registered
    if _registered:
        return True
    if not protocol_enabled(config):
        return False
    register_login_provider(PLATFORM, "protocol", make_provider(config))
    _registered = True
    logger.info("[qq_protocol] QQ 协议登录 provider 已注册 (milky=%s)", service_base_url(config))
    return True


__all__ = ["make_provider", "maybe_register", "probe_login",
           "REASON_SERVICE_DOWN", "REASON_NEEDS_SERVER_SETUP", "REASON_NEEDS_RISK_ACK"]
