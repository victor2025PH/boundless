"""WhatsApp 协议多开（Baileys）扫码登录 provider（M3）。

WhatsApp 没有官方多账号协议库，社区主流方案是 **Baileys（Node.js）**。本模块是 Python 侧
桥接：把统一收件箱「账号管理 → ＋ 扫码新增（协议）」的登录请求转发给一个独立运行的
**Baileys Node 微服务**（见 ``services/whatsapp-baileys/``），由它生成网页二维码、维护
WhatsApp 连接、回报账号上线。

落地约束（与 M2 一致的谨慎姿态）：
- 需先 ``npm install`` 并启动 Node 微服务，且需真号扫码联调；故默认**不启用**，需在
  ``config.platform_login.whatsapp.protocol_enabled: true`` 显式开启。
- 桥接通过 HTTP 调用微服务；服务不可达时**优雅降级**为错误提示，不影响主进程。
- 网络调用集中在 ``_post_json`` / ``_get_json`` 两个可被测试替换的薄封装里。
- 入站鉴权（P0-1，2026-10-08）：边车除 ``/health`` 外要求 ``Authorization: Bearer
  <边车令牌>``。两个薄封装自动带上（:func:`sidecar_auth_headers`）；令牌是**独立的**，
  不复用 web_admin 管理 token。来源：环境变量 ``WA_SIDECAR_TOKEN`` → 实例 config 目录的
  ``wa_sidecar_token.key``（``services/whatsapp-baileys/start.ps1`` 首次启动生成，
  ``*.key`` 已被 .gitignore 忽略）。都没有 = 不带头（兼容未开鉴权的回环边车/桌面壳）。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.integrations.account_registry import get_account_registry
from src.integrations.platform_login import register_login_provider, resolve_login_switch

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:8790"
_registered = False


def service_base_url(config: Dict[str, Any]) -> str:
    pl = (config or {}).get("platform_login", {}) or {}
    wa = pl.get("whatsapp", {}) or {}
    return str(wa.get("baileys_url") or _DEFAULT_BASE_URL).rstrip("/")


def protocol_enabled(config: Dict[str, Any]) -> bool:
    # 三态：显式配置优先（含 false）；未写过时桌面版默认开（见 resolve_login_switch 注释）。
    return resolve_login_switch(config, "platform_login.whatsapp.protocol_enabled")


# ── 边车入站鉴权令牌（P0-1） ────────────────────────────────────────────────

SIDECAR_TOKEN_ENV = "WA_SIDECAR_TOKEN"
SIDECAR_TOKEN_FILE_ENV = "WA_SIDECAR_TOKEN_FILE"
SIDECAR_TOKEN_FILENAME = "wa_sidecar_token.key"
_token_cache: Dict[str, Any] = {"path": None, "mtime": None, "token": ""}
_last_401_warn = 0.0


def _config_dir() -> Path:
    """复刻 config_manager 的 config 目录定位：AITR_CONFIG_PATH 父 → AITR_DATA_DIR/config → 引擎根/config。"""
    env_path = (os.environ.get("AITR_CONFIG_PATH") or "").strip()
    if env_path:
        return Path(env_path).expanduser().parent
    env_dir = (os.environ.get("AITR_DATA_DIR") or "").strip()
    if env_dir:
        return Path(env_dir).expanduser() / "config"
    return Path(__file__).resolve().parents[2] / "config"


def sidecar_token_path() -> Path:
    override = (os.environ.get(SIDECAR_TOKEN_FILE_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return _config_dir() / SIDECAR_TOKEN_FILENAME


def sidecar_token() -> Tuple[str, str]:
    """返回 ``(token, source)``；source ∈ {"env", "file", ""}。令牌本身绝不进日志。

    文件按 mtime 缓存：start.ps1 轮换令牌后（重启边车），引擎下次调用即读到新值，无需重启引擎。
    """
    env_tok = (os.environ.get(SIDECAR_TOKEN_ENV) or "").strip()
    if env_tok:
        return env_tok, "env"
    p = sidecar_token_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return "", ""
    if _token_cache["path"] == str(p) and _token_cache["mtime"] == mtime:
        tok = _token_cache["token"]
    else:
        try:
            # utf-8-sig：PowerShell 5.1 写文件可能带 BOM
            tok = p.read_text(encoding="utf-8-sig").strip()
        except OSError:
            tok = ""
        _token_cache.update(path=str(p), mtime=mtime, token=tok)
    return (tok, "file") if tok else ("", "")


def sidecar_auth_headers() -> Dict[str, str]:
    """调边车要带的鉴权头；未配置令牌时返回空 dict（兼容未开鉴权的回环边车）。"""
    tok, _src = sidecar_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def sidecar_auth_status() -> Dict[str, Any]:
    """只读诊断：是否配了令牌、来源、文件路径（不含令牌值）。"""
    tok, src = sidecar_token()
    return {"configured": bool(tok), "source": src, "file": str(sidecar_token_path())}


def _warn_if_unauthorized(r: Any, url: str) -> None:
    global _last_401_warn
    if getattr(r, "status_code", 0) != 401:
        return
    now = time.monotonic()
    if now - _last_401_warn < 60:
        return
    _last_401_warn = now
    st = sidecar_auth_status()
    logger.warning(
        "[wa_baileys] 边车拒绝鉴权（401）：%s —— 引擎侧令牌 configured=%s source=%s file=%s；"
        "请确认边车 SIDECAR_TOKEN 与该文件/环境变量 %s 一致（重启边车会按 start.ps1 重新读取）。",
        url.split("?", 1)[0], st["configured"], st["source"] or "-", st["file"], SIDECAR_TOKEN_ENV)


# ── HTTP 薄封装（测试可 monkeypatch） ────────────────────────────────────────

async def _post_json(url: str, payload: Dict[str, Any], timeout: float = 20.0) -> Dict[str, Any]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=payload, headers=sidecar_auth_headers())
        _warn_if_unauthorized(r, url)
        r.raise_for_status()
        return r.json()


async def _get_json(url: str, timeout: float = 20.0) -> Dict[str, Any]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, headers=sidecar_auth_headers())
        _warn_if_unauthorized(r, url)
        r.raise_for_status()
        return r.json()


def _normalize_status(raw: str) -> str:
    s = str(raw or "").lower()
    if s in ("open", "authorized", "connected", "online"):
        return "authorized"
    if s in ("scanned", "pairing"):
        return "scanned"
    if s in ("expired", "timeout"):
        return "expired"
    if s in ("failed", "error", "logged_out"):
        return "failed"
    return "pending"


# ── provider 工厂 + 注册 ─────────────────────────────────────────────────────

def make_provider(config: Dict[str, Any]):
    base = service_base_url(config)

    async def _provider(request: Any, platform: str, mode: str, account_id: str,
                        ctx: Optional[Dict[str, Any]] = None):
        proxy = (ctx or {}).get("proxy") or {}
        payload: Dict[str, Any] = {"account_id": account_id or ""}
        if proxy.get("host"):
            # 透传给 Node 微服务（由 Baileys 侧通过 agent 应用，一号一代理）
            payload["proxy_url"] = proxy.get("url") or ""
        try:
            data = await _post_json(f"{base}/login/start", payload)
        except Exception as ex:  # noqa: BLE001
            logger.debug("[wa_baileys] start 调用失败", exc_info=True)
            # 同 messenger_web：不带 reason_code 上游无从判定失败，会话只能挂到 TTL 耗尽。
            return {"instruction": f"无法连接 WhatsApp 协议服务（{ex}）。请确认 Baileys 微服务已启动。",
                    "reason_code": "service_down"}

        login_id = str(data.get("login_id") or "")
        qr_image = str(data.get("qr_image") or "")
        qr_url = str(data.get("qr_url") or "")

        async def _poll(session: Any) -> Dict[str, Any]:
            try:
                res = await _get_json(f"{base}/login/{login_id}/status")
            except Exception as ex:  # noqa: BLE001
                logger.debug("[wa_baileys] status 调用失败", exc_info=True)
                return {"status": "pending", "detail": str(ex)}
            st = _normalize_status(res.get("status"))
            aid = str(res.get("account_id") or "")
            if st == "authorized" and aid:
                try:
                    # merge_meta：只登记 baileys_login_id，绝不整块覆盖 meta——
                    # 2026-07-23 事故：重登录抹掉 persona_id 绑定 → 新好友回落
                    # 默认人设 + 错语言音色（客户投诉"老是讲日语"）。
                    get_account_registry().upsert(
                        "whatsapp", aid, mode="protocol", status="online",
                        meta={"baileys_login_id": login_id}, merge_meta=True)
                    try:
                        from src.ai.persona_voice import ensure_account_default_persona
                        ensure_account_default_persona(
                            get_account_registry(), "whatsapp", aid, config)
                    except Exception:  # noqa: BLE001
                        logging.getLogger(__name__).debug("swallowed in make_provider._provider._poll", exc_info=True)
                except Exception:  # noqa: BLE001
                    logger.debug("[wa_baileys] 注册表写入失败", exc_info=True)
                # P4 身份化：Baileys 微服务若在 status 里回传 pushname/name → 富集自身昵称
                # （前向兼容：字段缺失则 enrich 内部无有效字段直接 no-op；flag 默认关）
                try:
                    from src.integrations.account_self_profile import enrich_from_fields
                    await enrich_from_fields(
                        "whatsapp", aid,
                        name=str(res.get("pushname") or res.get("name") or ""),
                        avatar_url=str(res.get("avatar_url") or res.get("profile_pic_url") or ""),
                        config=config)
                except Exception:  # noqa: BLE001
                    logger.debug("[wa_baileys] self_profile 富集失败（忽略）", exc_info=True)
            out: Dict[str, Any] = {
                "status": st, "account_id": aid,
                "detail": str(res.get("detail") or ""),
                "qr_image": str(res.get("qr_image") or ""),
                # #181（J-6 B-2）实时提示码：边车 scan-signal 判出配对期 DNS 连败
                # （getaddrinfo ENOTFOUND 被 Baileys 包成 408）→ hint_code="dns_retry"，
                # 非终态、可来回变，空值也照落（与 messenger/instagram 桥接同口径）。
                "hint_code": str(res.get("hint_code") or ""),
            }
            try:
                dns_fails = int(res.get("pairing_dns_fails") or 0)
            except (TypeError, ValueError):
                dns_fails = 0
            if dns_fails > 0:
                out["pairing_dns_fails"] = dns_fails
            return out

        async def _cancel(session: Any) -> None:
            try:
                await _post_json(f"{base}/login/{login_id}/cancel", {})
            except Exception:  # noqa: BLE001
                logger.debug("[wa_baileys] cancel 调用失败", exc_info=True)

        return {
            "qr_image": qr_image,
            "qr_url": qr_url,
            "instruction": "用手机 WhatsApp：设置 → 已关联的设备 → 关联新设备，扫描二维码。",
            # i18n 键随会话下发：英文坐席按键取本地化指引，raw instruction 仅兜底
            "instruction_key": "inbox.connect.instr_wa_protocol",
            "poll": _poll,
            "cancel": _cancel,
            "state": {"login_id": login_id, "base": base},
        }

    return _provider


def maybe_register(config: Dict[str, Any]) -> bool:
    """按需注册 WhatsApp protocol provider（幂等）。

    仅当 ``protocol_enabled: true`` 时注册（服务可达性在 start 时检测并降级）。
    """
    global _registered
    if _registered:
        return True
    if not protocol_enabled(config):
        return False
    register_login_provider("whatsapp", "protocol", make_provider(config))
    _registered = True
    logger.info("[wa_baileys] WhatsApp protocol 登录 provider 已注册 (base=%s)",
                service_base_url(config))
    return True
