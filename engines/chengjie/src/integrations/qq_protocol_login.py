"""QQ 协议登录（个人号）登录 provider —— ``register_login_provider("qq", "protocol", …)``。

登录**不在本窗口扫码**：QQ 号的扫码发生在用户自装的协议端（NapCat / LLOneBot / Lagrange.Milky）
自己的 WebUI / 控制台里；本 provider 只做「连通 + 确认」——按平台配置（或账号 meta）的 Milky
端点探 ``get_login_info``：

- 协议端在且已登录 → ``authorized``：把 QQ 号登进账号注册表（``mode=protocol``，meta 快照
  ``milky_url / milky_token / uin / nickname``，``merge_meta`` 绝不整块覆盖人设绑定），补自身
  昵称/头像（QQ 头像 CDN 按号取，零额外接口）；
- 协议端在但 QQ 未登录（``retcode -403``）→ ``pending``（指引去协议端扫码），轮询直到登录；
- 协议端不可达 → ``pending`` + ``hint_code=service_down``（首次即不可达则 ``reason_code`` 早退，
  防前端对着空转挂满 TTL）。

登录形态 ``login_kind=device``（后端 ``PLATFORM_MODE_OVERRIDES`` 覆盖）：向导词汇＝「去设备上
完成登录，本窗口等账号上线」——与协议端 WebUI 扫码的心智一致，且不需要前端新形态。

契约与 ``zalo_personal_login`` / ``whatsapp_baileys_login`` 对齐（start → poll → 落库 + 富集），
网络经 ``qq_milky.MilkyClient``（``http`` 可注入）。
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
    protocol_enabled,
    service_base_url,
    service_token,
)

logger = logging.getLogger(__name__)

_registered = False

REASON_SERVICE_DOWN = "service_down"
REASON_NEEDS_SERVER_SETUP = "needs_server_setup"


def _client_factory(config: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> MilkyClient:
    return MilkyClient(service_base_url(config, meta), service_token(config, meta), timeout=8.0)


async def probe_login(client: MilkyClient) -> Dict[str, Any]:
    """探一次协议端：``{"state": authorized|not_logged_in|down, "uin", "nickname", "detail"}``。"""
    try:
        info = await client.call("get_login_info")
    except MilkyError as ex:
        if ex.not_logged_in:
            return {"state": "not_logged_in", "uin": "", "nickname": "",
                    "detail": "协议端已连上，QQ 尚未登录"}
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
        if not base:
            return {"instruction": "未配置 QQ 协议端地址（platform_login.qq.milky_url），请先在接入向导填写。",
                    "instruction_key": "inbox.connect.instr_qq_setup",
                    "reason_code": REASON_NEEDS_SERVER_SETUP}
        client = _client_factory(config)
        first = await probe_login(client)
        if first["state"] == "down":
            return {"instruction": f"无法连接 QQ 协议端（{base}）：{first['detail']}。"
                                   "请确认 NapCat / LLOneBot / Lagrange 已启动且 Milky 服务地址、Token 正确。",
                    "instruction_key": "inbox.connect.instr_qq_down",
                    "reason_code": REASON_SERVICE_DOWN, "detail": first["detail"]}

        async def _poll(session: Any) -> Dict[str, Any]:
            res = await probe_login(client)
            if res["state"] == "authorized":
                _persist_authorized(config, res["uin"], res["nickname"])
                await _enrich_self(config, res["uin"], res["nickname"])
                return {"status": "authorized", "account_id": res["uin"],
                        "detail": res["nickname"]}
            if res["state"] == "down":
                return {"status": "pending", "detail": res["detail"], "hint_code": REASON_SERVICE_DOWN}
            return {"status": "pending", "detail": res["detail"]}

        async def _cancel(session: Any) -> None:
            return None

        return {
            "instruction": (
                "请在你自装的 QQ 协议端（NapCat / LLOneBot / Lagrange）的 WebUI 或控制台里用手机 QQ "
                "扫码登录；登录完成后本窗口会自动确认并把该 QQ 号接入。"
            ),
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
           "REASON_SERVICE_DOWN", "REASON_NEEDS_SERVER_SETUP"]
