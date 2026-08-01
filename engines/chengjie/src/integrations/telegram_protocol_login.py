"""Telegram protocol（pyrogram）扫码登录 provider（M2）。

实现 Telegram 官方「关联设备」二维码登录（``auth.ExportLoginToken`` 流），用于
在统一收件箱「账号管理 → ＋ 扫码新增」里**新增任意多个 Telegram 账号**（协议多开）。

安全 / 落地约束（重要）：
- pyrogram 的 QR 登录涉及 DC 迁移（``LoginTokenMigrateTo``）等底层细节，**无法在无真账号
  的环境联调**。因此本 provider 默认**不启用**，需操作者用测试号验证后，在
  ``config.platform_login.telegram.protocol_enabled: true`` 显式开启。
- 全程隔离：每次登录用独立 session 文件（``sessions/tg_login_*.session``），失败仅影响该
  次登录，**不触碰正在运行的主客户端**。
- 纯函数（``tg_login_url`` / ``resolve_credentials`` / ``is_pyrogram_available``）与状态机
  可单测；真实 pyrogram 调用集中在 ``TelegramQrLogin`` 内并全程 try/except 降级。

成功后：把账号写入 ``account_registry``（mode=protocol，meta 记 session_name/phone），
供账号池编排器后续以该 session 拉起 runner。
"""

from __future__ import annotations

import base64
import logging
import secrets
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.integrations.account_registry import get_account_registry
from src.integrations.platform_login import register_login_provider, resolve_login_switch

logger = logging.getLogger(__name__)

_DEFAULT_SESSIONS_DIR = "sessions"
_registered = False
#: 最近一次 maybe_register 收到的配置（ConfigManager 热重载会整个换掉字典对象，
#: provider 闭包里那份会永远停在启动时刻——见 make_provider 的 config_getter）
_latest_config: Dict[str, Any] = {}


# ── 纯函数（可单测） ─────────────────────────────────────────────────────────

def is_pyrogram_available() -> bool:
    import importlib.util as u
    return u.find_spec("pyrogram") is not None


def tg_login_url(token: bytes) -> str:
    """构造 Telegram 关联设备登录 URL（二维码内容）。"""
    b64 = base64.urlsafe_b64encode(bytes(token)).decode().rstrip("=")
    return f"tg://login?token={b64}"


def resolve_credentials(config: Dict[str, Any]) -> Optional[Tuple[int, str]]:
    """从 config 解析 api_id/api_hash（先扁平 telegram.*，再取首个 account）。"""
    tg = (config or {}).get("telegram", {}) or {}
    api_id = tg.get("api_id")
    api_hash = tg.get("api_hash")
    if not (api_id and api_hash):
        for a in (tg.get("accounts") or []):
            if isinstance(a, dict) and a.get("api_id") and a.get("api_hash"):
                api_id, api_hash = a.get("api_id"), a.get("api_hash")
                break
    try:
        if api_id and api_hash:
            return int(api_id), str(api_hash)
    except (TypeError, ValueError):
        pass
    return None


def protocol_enabled(config: Dict[str, Any]) -> bool:
    # 三态：显式配置优先（含 false）；未写过时桌面版默认开（见 resolve_login_switch）。
    # 注册仍另需凭据（``maybe_register`` 里 resolve_credentials / credpool 前置），故本处
    # 放开不会在无 api_id 时硬注册——那种情况诊断如实报 creds_missing，不是 not_enabled。
    return resolve_login_switch(config, "platform_login.telegram.protocol_enabled")


def _is_password_needed(ex: Exception) -> bool:
    """判定异常是否为「账号开启两步验证，需云密码」（SESSION_PASSWORD_NEEDED）。

    不硬 import pyrogram.errors.SessionPasswordNeeded（跨版本类路径可能变），按类名
    + 错误串双判，稳。
    """
    if type(ex).__name__ == "SessionPasswordNeeded":
        return True
    return "SESSION_PASSWORD_NEEDED" in str(ex).upper()


def _is_bad_password(ex: Exception) -> bool:
    """判定异常是否为「两步验证密码错误」（PASSWORD_HASH_INVALID）——保持可重试。"""
    name = type(ex).__name__
    if name in ("PasswordHashInvalid", "BadRequest"):
        return "PASSWORD_HASH_INVALID" in str(ex).upper() or name == "PasswordHashInvalid"
    return "PASSWORD_HASH_INVALID" in str(ex).upper()


# ── 登录状态机（真实 pyrogram 调用，全程降级保护） ───────────────────────────

class TelegramQrLogin:
    """管理一次 Telegram 二维码登录的生命周期（异步）。"""

    def __init__(self, api_id: int, api_hash: str, sessions_dir: str,
                 proxy: Optional[Dict[str, Any]] = None,
                 device_kwargs: Optional[Dict[str, str]] = None) -> None:
        self.api_id = int(api_id)
        self.api_hash = str(api_hash)
        # 每账号设备指纹（防多开被聚类连坐）；空 = 用 pyrogram 默认值，行为不变。
        # 必须与该账号后续 runner 拉起时用的指纹一致，故由调用方按同一种子派生。
        self.device_kwargs = dict(device_kwargs or {})
        self.sessions_dir = Path(sessions_dir)
        self.session_name = f"tg_login_{secrets.token_hex(6)}"
        self.proxy = proxy or None
        self.client: Any = None
        self.status = "pending"      # pending|password_needed|authorized|expired|failed
        self.account_id = ""
        self.phone = ""
        self.qr_url = ""
        self.detail = ""
        # N2：扫码成功时导出的 session_string（in-memory 启动用，比文件 session 抗 DC 迁移）
        self.session_string = ""
        # P1 身份化：扫码成功时从授权返回的 user 抽取自身昵称/用户名（写入注册表 meta.self_*）
        self.self_profile: Dict[str, str] = {}

    def result(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "account_id": self.account_id,
            "qr_url": self.qr_url,
            "detail": self.detail,
        }

    async def start(self) -> Dict[str, Any]:
        try:
            from pyrogram import Client  # noqa: WPS433 (lazy)
            from pyrogram.raw.functions.auth import ExportLoginToken

            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            client_kwargs: Dict[str, Any] = dict(
                api_id=self.api_id, api_hash=self.api_hash,
                workdir=str(self.sessions_dir),
            )
            if self.proxy:
                client_kwargs["proxy"] = self.proxy
            client_kwargs.update(self.device_kwargs)
            self.client = Client(self.session_name, **client_kwargs)
            await self.client.connect()
            r = await self.client.invoke(ExportLoginToken(
                api_id=self.api_id, api_hash=self.api_hash, except_ids=[]))
            await self._advance(r)
        except Exception as ex:  # noqa: BLE001
            self.status = "failed"
            self.detail = f"发起登录失败：{ex}"
            logger.debug("[tg_protocol_login] start 失败", exc_info=True)
            await self._safe_disconnect()
        return self.result()

    async def poll(self) -> Dict[str, Any]:
        # password_needed 是稳定的等待态：不再打 ExportLoginToken（会一直抛
        # SESSION_PASSWORD_NEEDED），停在此态等 submit_password。
        if self.status in ("authorized", "failed", "expired", "password_needed"):
            return self.result()
        try:
            from pyrogram.raw.functions.auth import ExportLoginToken
            r = await self.client.invoke(ExportLoginToken(
                api_id=self.api_id, api_hash=self.api_hash, except_ids=[]))
            await self._advance(r)
        except Exception as ex:  # noqa: BLE001
            if _is_password_needed(ex):
                # 扫码已确认，但账号开了两步验证 → 进入等待云密码态（不算失败/过期）
                self.status = "password_needed"
                self.detail = "该账号已开启两步验证，请输入云密码完成登录"
                logger.info("[tg_protocol_login] 扫码已确认，等待两步验证云密码")
            else:
                # 多为 token 过期 / 网络抖动 → 标记过期让前端刷新
                self.status = "expired"
                self.detail = str(ex)
                logger.debug("[tg_protocol_login] poll 失败", exc_info=True)
        return self.result()

    async def submit_password(self, password: str) -> Dict[str, Any]:
        """两步验证：提交云密码完成登录（SRP 校验走 pyrogram 高层 check_password）。

        仅在 status==password_needed 时有效。密码错误保持 password_needed 可重试；
        成功则走与扫码成功一致的收尾（导出 session_string、抽身份、落盘、置 authorized）。
        """
        if self.status != "password_needed":
            return self.result()
        if not password:
            self.detail = "云密码为空"
            return self.result()
        try:
            user = await self.client.check_password(password)
            await self._finish_with_user(user)
        except Exception as ex:  # noqa: BLE001
            if _is_bad_password(ex):
                self.detail = "云密码错误，请重新输入"
                logger.info("[tg_protocol_login] 两步验证密码错误，可重试")
            else:
                self.status = "failed"
                self.detail = f"两步验证失败：{ex}"
                logger.debug("[tg_protocol_login] check_password 失败", exc_info=True)
                await self._safe_disconnect()
        return self.result()

    async def cancel(self) -> None:
        await self._safe_disconnect(remove_session=(self.status != "authorized"))

    # ── 内部 ──────────────────────────────────────────────────────────────

    async def _advance(self, r: Any) -> None:
        from pyrogram.raw.types.auth import (
            LoginToken, LoginTokenMigrateTo, LoginTokenSuccess,
        )
        if isinstance(r, LoginToken):
            self.qr_url = tg_login_url(r.token)
            self.status = "pending"
        elif isinstance(r, LoginTokenMigrateTo):
            from pyrogram.raw.functions.auth import ImportLoginToken
            await self._migrate(r.dc_id)
            r2 = await self.client.invoke(ImportLoginToken(token=r.token))
            await self._advance(r2)
        elif isinstance(r, LoginTokenSuccess):
            await self._finish(r)
        else:
            self.status = "failed"
            self.detail = f"未知的登录响应：{type(r).__name__}"

    async def _migrate(self, dc_id: int) -> None:
        """切换到目标 DC（扫码后账号归属 DC 通常与默认 DC 不同）。"""
        from pyrogram.session import Auth, Session
        await self.client.session.stop()
        await self.client.storage.dc_id(dc_id)
        test_mode = await self.client.storage.test_mode()
        auth_key = await Auth(self.client, dc_id, test_mode).create()
        await self.client.storage.auth_key(auth_key)
        self.client.session = Session(
            self.client, dc_id, auth_key, test_mode)
        await self.client.session.start()

    async def _finish(self, success: Any) -> None:
        # 二维码令牌直接成功（无两步验证）路径：从 authorization 抽 user 收尾。
        await self._finish_with_user(success.authorization.user)

    async def _finish_with_user(self, user: Any) -> None:
        """统一收尾：token 成功 与 两步验证成功 共用（抽身份 + 导出 session_string + 落盘）。"""
        try:
            self.account_id = str(getattr(user, "id", "") or "")
            self.phone = str(
                getattr(user, "phone_number", "") or getattr(user, "phone", "") or "")
            # P1：从授权返回的 user 抽取自身昵称/用户名（纯函数，无副作用；flag 在写入时判定）
            try:
                from src.integrations.account_self_profile import extract_self_profile
                self.self_profile = extract_self_profile(user)
            except Exception:  # noqa: BLE001
                self.self_profile = {}
            # check_password 已在内部设过 storage.user_id/is_bot；token 成功路径需显式设。
            try:
                await self.client.storage.user_id(user.id)
                await self.client.storage.is_bot(False)
            except Exception:  # noqa: BLE001
                logger.debug("[tg_protocol_login] 写 storage user_id 失败（忽略）", exc_info=True)
            self.status = "authorized"
            self.detail = ""
            # N2：趁连接未断导出 session_string（A 线可 in-memory 启动，抗文件 session DC 迁移不稳）
            try:
                self.session_string = str(await self.client.export_session_string() or "")
            except Exception:  # noqa: BLE001
                self.session_string = ""
                logger.debug("[tg_protocol_login] 导出 session_string 失败（忽略）", exc_info=True)
        except Exception as ex:  # noqa: BLE001
            self.status = "failed"
            self.detail = f"完成登录失败：{ex}"
            logger.debug("[tg_protocol_login] finish 失败", exc_info=True)
        finally:
            # disconnect 以把 session 落盘（供编排器后续拉起）
            await self._safe_disconnect(remove_session=False)

    async def _safe_disconnect(self, *, remove_session: bool = False) -> None:
        try:
            if self.client is not None:
                try:
                    await self.client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            if remove_session:
                f = self.sessions_dir / f"{self.session_name}.session"
                if f.exists():
                    f.unlink()
        except Exception:  # noqa: BLE001
            logger.debug("[tg_protocol_login] disconnect 清理失败", exc_info=True)


# ── provider 工厂 + 注册 ─────────────────────────────────────────────────────

def _to_pyrogram_proxy(proxy: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """把代理池条目转成 pyrogram 的 proxy 配置。"""
    if not proxy or not proxy.get("host"):
        return None
    out: Dict[str, Any] = {
        "scheme": str(proxy.get("scheme") or "socks5"),
        "hostname": str(proxy.get("host")),
        "port": int(proxy.get("port") or 0),
    }
    if proxy.get("username"):
        out["username"] = str(proxy.get("username"))
    if proxy.get("password"):
        out["password"] = str(proxy.get("password"))
    return out


def make_provider(config: Dict[str, Any], sessions_dir: str = _DEFAULT_SESSIONS_DIR,
                  config_getter: Optional[Any] = None):
    """构造 protocol 登录 provider。

    ``config_getter``：返回**实时**配置的可调用对象。为什么需要它——
    provider 只在首次 ``maybe_register`` 时构造一次，而 ``ConfigManager`` 热重载是
    **整个替换** config 字典（``self.config = new_data``），闭包里捕获的那份会永远停在
    启动那一刻。于是「开中央池 / 开设备指纹」这类开关改了配置也不生效，只能重启生产。
    传入 getter 后这些开关即刻热生效；不传（如单测直接构造）就用捕获的那份，行为不变。
    """

    def _live(cfg_fallback: Dict[str, Any]) -> Dict[str, Any]:
        if config_getter is None:
            return cfg_fallback
        try:
            fresh = config_getter()
        except Exception:  # noqa: BLE001 —— 取不到实时配置就用捕获的，绝不因此挂掉登录
            logger.debug("[tg_protocol_login] 实时配置获取失败，回落注册时快照", exc_info=True)
            return cfg_fallback
        return fresh if isinstance(fresh, dict) and fresh else cfg_fallback

    async def _provider(request: Any, platform: str, mode: str, account_id: str,
                        ctx: Optional[Dict[str, Any]] = None):
        cfg = _live(config)
        # 凭据按**每次登录**解析（而非 provider 注册时定死一组）：中央池优先，
        # 回落配置自带。这样每个账号能拿到各自的 api_id，避免全部号共用一组
        # 凭据被 Telegram 聚类连坐。
        from src.integrations import credpool_bridge as _cp
        pool_key = _cp.new_pool_key() if _cp.credpool_enabled(cfg) else ""
        alloc = await _cp.aresolve_for_account(cfg, pool_key=pool_key)
        if alloc is None:
            return {"instruction": "未配置 Telegram api_id/api_hash，无法发起协议登录。"}
        api_id, api_hash = alloc.api_id, alloc.api_hash
        # 只有真从池里拿到才需要把粘定键落库/回报（配置自带的不占池容量）
        used_pool_key = pool_key if alloc.source == "credpool" else ""
        # 出口优先级：运营在 ctx 里显式指定的代理 > 中央池按付费档下发的独立出口。
        # 显式配置代表人的意图，不该被自动分配悄悄覆盖。
        proxy = (_to_pyrogram_proxy((ctx or {}).get("proxy"))
                 or _to_pyrogram_proxy(alloc.proxy))
        # 设备指纹种子在**扫码这一刻**定下并随账号落库，runner 后续用同一种子派生
        # 出完全相同的指纹——会话建立后换设备型号等于自曝异常。
        from src.integrations import device_fingerprint as _fp
        device_seed = _fp.new_device_seed() if _fp.enabled(cfg) else ""
        device_fp = _fp.client_kwargs(cfg, device_seed)
        # 指纹不经中央池，覆盖率只能在这里记（否则三隔离里有一件套在看板上是黑的）
        try:
            from src.integrations.credpool_stats import get_credpool_stats
            get_credpool_stats().record_login(bool(device_fp))
        except Exception:  # noqa: BLE001
            pass
        login = TelegramQrLogin(
            api_id, api_hash, sessions_dir, proxy=proxy, device_kwargs=device_fp)
        try:
            await login.start()
        except Exception as exc:  # 连不上/凭据坏 → 回报池，让健康分与自动切换生效
            _cp.report(cfg, api_id, False, error=str(exc)[:200], pool_key=used_pool_key)
            # 起不来就不会有人来 cancel/poll，容量必须在这里还，否则直接漏掉
            if used_pool_key:
                _cp.release(cfg, used_pool_key)
            raise

        def _persist_if_authorized(res: Dict[str, Any]) -> None:
            """扫码/两步验证成功即把账号落注册表（幂等；供编排器重启后拉起）。"""
            if res.get("status") != "authorized" or not res.get("account_id"):
                return
            try:
                _meta = {"session_name": login.session_name, "phone": login.phone}
                # 中央池粘定键：pyrogram session 与登录所用 api_id 绑定，之后 runner
                # 每次拉起都用同一个 key 向池索取，保证拿回**同一组**凭据。
                if used_pool_key:
                    from src.integrations import credpool_bridge as _cpb
                    _meta[_cpb.META_KEY] = used_pool_key
                    # 凭据本体也缓存一份：池不可达时 runner 用它上线，而**不能**回落
                    # 配置自带凭据（那会让这条 session 报出另一个 api_id＝风控信号）。
                    # 注册表本来就存 session_string（账号完全访问权），api_hash 的
                    # 敏感度远低于它，不构成暴露面升级。
                    _cred = {"api_id": api_id, "api_hash": api_hash,
                             "tier": str(getattr(alloc, "tier", "") or "free")}
                    if getattr(alloc, "proxy", None):
                        _cred["proxy"] = dict(alloc.proxy)
                    _meta[_cpb.META_CRED_KEY] = _cred
                    _cpb.report(cfg, api_id, True, pool_key=used_pool_key)
                # 指纹**本体**随账号落库（不只是种子）——机型表将来刷新时，
                # 存量账号仍报同一套设备信息，不会被悄悄换掉身份。
                if device_seed:
                    _meta[_fp.META_KEY] = device_seed
                if device_fp:
                    _meta[_fp.META_FP_KEY] = dict(device_fp)
                # N2：有 session_string 则一并存（A 线优先 in-memory 启动；见 telegram_client）
                if getattr(login, "session_string", ""):
                    _meta["session_string"] = login.session_string
                # P1 身份化：flag 开启则把自身昵称/用户名一并落 meta.self_*（头像待该号
                # 后续被 telegram_client 拉起时补齐，见 account_self_profile.enrich_from_user）
                try:
                    from src.integrations.account_self_profile import (
                        self_profile_enabled,
                    )
                    if self_profile_enabled(cfg) and getattr(login, "self_profile", None):
                        _meta.update(login.self_profile)
                except Exception:  # noqa: BLE001
                    logger.debug("[tg_protocol_login] self_profile 合并失败（忽略）", exc_info=True)
                # merge_meta：只更新会话凭据/self_* 键，防重登录整块覆盖 meta
                # 抹掉 persona_id / auto_reply / autoreply_override 等运营绑定。
                _prev = get_account_registry().get("telegram", res["account_id"]) or {}
                get_account_registry().upsert(
                    "telegram", res["account_id"], mode="protocol",
                    status="online", meta=_meta, merge_meta=True,
                )
                # 重扫同一个号会生成**新的**粘定键，旧键那笔分配就成了孤儿永远挂在
                # active 上（容量泄漏：一个号反复重扫能把一组凭据的名额吃干）。
                # 这里拿旧键去归还——只有走到授权、确认换了键才做。
                if used_pool_key:
                    _old_key = str((_prev.get("meta") or {}).get(_cpb.META_KEY) or "")
                    if _old_key and _old_key != used_pool_key:
                        try:
                            _cpb.release(cfg, _old_key)
                            logger.info(
                                "[tg_protocol_login] 重扫换键，已归还旧键容量 %s → %s",
                                _old_key, used_pool_key)
                        except Exception:  # noqa: BLE001
                            logger.debug("[tg_protocol_login] 旧键归还失败（池侧巡检兜底）",
                                         exc_info=True)
                # 新人设空号：登录即落默认人设（已绑定 merge 不覆盖）
                try:
                    from src.ai.persona_voice import ensure_account_default_persona
                    ensure_account_default_persona(
                        get_account_registry(), "telegram",
                        res["account_id"], cfg,
                    )
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                logger.debug("[tg_protocol_login] 注册表写入失败", exc_info=True)

        def _release_if_abandoned(reason: str) -> None:
            """登录没走到授权就结束 → 把池容量还回去。

            不还会造成**每次放弃扫码都永久吃掉一个账号位**：一次登录申请一组凭据，
            用户没扫完（关页面 / 码过期 / 取消），那笔分配就永远挂在 active 上。
            max_accounts=5 时，五次放弃就能把一组凭据占满，之后新号全部回落自带凭据。
            （2026-07 真实演练读台账时发现：只成功扫了 1 个号，却占了 3 个位。）
            """
            if not used_pool_key:
                return
            try:
                _cp.release(cfg, used_pool_key)
                logger.info("[tg_protocol_login] 登录未完成（%s），已归还池容量 key=%s",
                            reason, used_pool_key)
            except Exception:  # noqa: BLE001
                logger.debug("[tg_protocol_login] 归还池容量失败（忽略）", exc_info=True)

        async def _poll(session: Any) -> Dict[str, Any]:
            res = await login.poll()
            _persist_if_authorized(res)
            if res.get("status") in ("expired", "failed"):
                _release_if_abandoned(res.get("status") or "terminal")
            return res

        async def _submit_password(session: Any, password: str) -> Dict[str, Any]:
            res = await login.submit_password(password)
            _persist_if_authorized(res)
            if res.get("status") == "failed":
                _release_if_abandoned("password_failed")
            return res

        async def _cancel(session: Any) -> None:
            await login.cancel()
            if getattr(login, "status", "") != "authorized":
                _release_if_abandoned("cancelled")

        return {
            "qr_url": login.qr_url,
            "instruction": "用手机 Telegram：设置 → 设备 → 关联桌面设备，扫描二维码。",
            "poll": _poll,
            "submit_password": _submit_password,
            "cancel": _cancel,
            "state": login,
        }

    return _provider


def maybe_register(config: Dict[str, Any], *, sessions_dir: str = _DEFAULT_SESSIONS_DIR) -> bool:
    """按需注册 Telegram protocol provider。

    仅当：pyrogram 可用 + 配置了 api 凭据 + ``protocol_enabled: true`` 时注册（幂等）。
    返回是否已注册（已注册过也返回 True）。

    **每次调用都会刷新 `_latest_config`**（即使已注册直接短路）——调用方
    ``unified_inbox_login_routes._ensure_login_providers`` 每个登录请求都会带着
    ``config_manager.config`` 调一次，于是 provider 总能看到热重载后的新配置。
    没有这一步，`credpool` / `device_fingerprint` 这类开关就只能靠重启生产才生效。
    """
    global _registered, _latest_config
    if isinstance(config, dict) and config:
        _latest_config = config
    if _registered:
        return True
    if not is_pyrogram_available():
        return False
    if resolve_credentials(config) is None:
        # 开了中央池就无需本地自带凭据——凭据在登录时按账号从池里取，
        # 这正是「新用户不用申请 api_id」的前提。
        from src.integrations.credpool_bridge import credpool_enabled
        if not credpool_enabled(config):
            return False
    if not protocol_enabled(config):
        return False
    register_login_provider("telegram", "protocol",
                            make_provider(config, sessions_dir,
                                          config_getter=lambda: _latest_config))
    _registered = True
    logger.info("[tg_protocol_login] Telegram protocol 登录 provider 已注册")
    return True
