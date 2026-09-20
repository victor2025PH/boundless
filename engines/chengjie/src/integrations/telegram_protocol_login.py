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

import asyncio
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


def classify_login_exception(ex: Exception) -> str:
    """把发起扫码登录的异常归类为漏斗 ``reason_code``（同 LINE 侧 classify_login_error 哲学）。

    只归**高置信**桶，判定按「具体 → 泛化」排序（API_ID_PUBLISHED_FLOOD 同时含
    FLOOD 字样，凭据判定必须先于限流判定）：
    - ``cred_invalid``：api_id/api_hash 本体无效（托管池组废了 / 用户自填错值）——
      重试与刷新二维码永远无解，2026-08-10 实锤为「用户拍照求助」级事故，必须与
      笼统 login_failed 分开，前端才能给「换凭据/重启」而非「请重试」。
    - ``rate_limited``：FLOOD_WAIT 限流——处置是等待，复用既有前端文案。
    - ``tg_unreachable``：连不上 Telegram DC（超时/拒连/代理故障）——大陆直连被墙
      的典型形态，前端据此给「配代理」行动指引，而不是让用户反复点刷新。
    其余返回空串（上层回落 login_failed 通用文案）——宁可笼统，不可误导。
    """
    name = type(ex).__name__
    text = str(ex or "").lower()
    if ("api_id_invalid" in text or "api_id_published_flood" in text
            or name in ("ApiIdInvalid", "ApiIdPublishedFlood")):
        return "cred_invalid"
    if "flood_wait" in text or name == "FloodWait":
        return "rate_limited"
    if "proxy" in name.lower() or isinstance(ex, (OSError, asyncio.TimeoutError)):
        # OSError 族覆盖 ConnectionError/TimeoutError/socket.gaierror；
        # python-socks 的 Proxy*Error 按类名兜（用户代理工具没开也归这桶）
        return "tg_unreachable"
    if any(k in text for k in ("timed out", "timeout", "connection", "unreachable",
                               "network", "refused", "reset", "getaddrinfo",
                               "socks", "proxy")):
        return "tg_unreachable"
    return ""


def cred_fail_meta(cfg: Dict[str, Any], alloc: Any) -> Dict[str, Any]:
    """``cred_invalid`` 失败时给前端的处置元信息（QR / phone 两 provider 共用）。

    旧 UI 只能把「凭据是自动配置的还是你自己填的」这道判断题抛给用户——而系统
    明明知道答案（``alloc.source`` + ``telegram._hosted_cred``）。本函数把答案与
    「多久后重试才有意义」显式下发，前端据此分流：

    - ``hosted``：官网托管派发。换发冷却（hosted_gateway.SWAP_COOLDOWN_SEC）走完
      之前重试必然拿回同一组废凭据 → ``retry_after_sec``＝冷却剩余秒，前端画
      倒计时到点自动重试（扫码链）/解锁重试按钮（手机号链，防自动重发短信）。
    - ``pool``：LAN credpool，池有自己的健康轮换 → 立即重试即可（0）。
    - ``self``：用户自填值，机器不猜人的意图 → 前端亮修正表单（-1＝无冷却语义）。
    """
    src = str(getattr(alloc, "source", "") or "")
    if src == "credpool":
        return {"cred_source": "pool", "retry_after_sec": 0}
    tg = cfg.get("telegram") if isinstance(cfg.get("telegram"), dict) else {}
    if (tg or {}).get("_hosted_cred"):
        try:
            from src.ai import hosted_gateway as _hg
            wait = int(_hg.swap_retry_after_sec())
        except Exception:  # noqa: BLE001 —— 观测辅助字段，绝不因它挂掉登录链
            wait = 0
        return {"cred_source": "hosted", "retry_after_sec": wait}
    return {"cred_source": "self", "retry_after_sec": -1}


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


def _is_token_expired(ex: Exception) -> bool:
    """判定异常是否为「二维码令牌过期」（AUTH_TOKEN_EXPIRED）——正常老化，非故障。"""
    if type(ex).__name__ == "AuthTokenExpired":
        return True
    return "AUTH_TOKEN_EXPIRED" in str(ex).upper()


def _flood_wait_sec(ex: Exception) -> int:
    """从 FloodWait 异常提取等待秒数（pyrogram 放在 ``.value``；取不到回 0）。"""
    try:
        v = getattr(ex, "value", None)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    except Exception:  # noqa: BLE001
        pass
    return 0


class LoginStorageClosed(RuntimeError):
    """登录 client 的 sqlite storage 已关闭（Q-10 #268，2026-09-08 13:32 实锤）。

    时序：用户扫码 → ``_advance`` 进 DC 迁移；同时另一路（cancel / TTL 清理 /
    换码）已对同一 client ``disconnect()`` → storage.close()。``_migrate`` 里再
    ``storage.dc_id()`` / ``session.start()`` 撞 pyrogram sqlite_storage 的
    ``sqlite3.ProgrammingError: Cannot operate on a closed database``，且
    ``_advance`` 的「首试失败自动重试一次」对同一具尸体再撞一次，日志两份栈。
    这类异常＝会话已被收走，不是网络抖动：不重试、归因 ``session_closed``。
    """


def _storage_closed(client: Any) -> bool:
    """client 的 storage 是否已关闭（best-effort 探测，判不出＝当作未关）。

    pyrogram ``SQLiteStorage`` 没有 closed 标志：``close()`` 只 ``conn.close()``，
    对象仍在。故用 ``conn`` 做一条 ``SELECT 1`` 探针；``conn`` 为 None（``close``
    后某些版本置 None）也算关。假件 / 内存 storage 没有 ``conn`` 属性 → 未关。
    """
    if client is None:
        return True
    storage = getattr(client, "storage", None)
    if storage is None:
        return True
    if not hasattr(storage, "conn"):
        return False
    conn = getattr(storage, "conn", None)
    if conn is None:
        return True
    try:
        conn.execute("SELECT 1")
        return False
    except Exception as ex:  # noqa: BLE001
        return "closed database" in str(ex).lower()


def _is_closed_db_error(ex: Exception) -> bool:
    return isinstance(ex, LoginStorageClosed) or (
        "cannot operate on a closed database" in str(ex).lower())


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
        # 失败归因码（classify_login_exception 产出；空=不明，前端回落通用文案）
        self.reason_code = ""
        # cred_invalid 处置元信息（cred_fail_meta 产出，provider 在换发收尾后落）：
        # 凭据来源 hosted/pool/self + 换发冷却剩余秒。前端据此倒计时自愈/亮修正表单，
        # 不再把「凭据是谁配的」这道系统明知答案的判断题抛给用户。
        self.cred_source = ""
        self.retry_after_sec = -1
        # N2：扫码成功时导出的 session_string（in-memory 启动用，比文件 session 抗 DC 迁移）
        self.session_string = ""
        # P1 身份化：扫码成功时从授权返回的 user 抽取自身昵称/用户名（写入注册表 meta.self_*）
        self.self_profile: Dict[str, str] = {}
        # 2026-08-14（198 事故）：扫码后的 DC 迁移要 3~10s，而前端 2.5s 一轮询——
        # 并发 poll 对「会话半开」的 client 再 invoke 必砸锅，把可成功的登录打成 expired。
        # 锁内只跑一轮 ExportLoginToken/迁移；锁被占时 poll 直接返回快照。
        self._poll_lock = asyncio.Lock()
        # 是否已观测到用户扫码（LoginTokenMigrateTo/Success 只在客户端确认后出现）。
        # 扫码后 token 已消耗、换码无解 → 之后的异常必须归为终态失败而非 expired。
        self._scan_seen = False

    def result(self) -> Dict[str, Any]:
        out = {
            "status": self.status,
            "account_id": self.account_id,
            "qr_url": self.qr_url,
            "detail": self.detail,
            # 路由 poll 只在非空时落 sess.reason_code → 前端按码取行动指引文案
            "reason_code": self.reason_code,
        }
        if self.cred_source:
            out["cred_source"] = self.cred_source
            out["retry_after_sec"] = int(self.retry_after_sec)
        elif self.retry_after_sec >= 0:
            # rate_limited（FloodWait）等无关凭据来源的等待秒数：前端画倒计时用
            out["retry_after_sec"] = int(self.retry_after_sec)
        return out

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
            self.reason_code = classify_login_exception(ex)
            # ERROR 级（2026-08-10 事故沉淀）：此前是 DEBUG，公网桌面版的 beacon 只回传
            # ERROR——池组凭据废掉（cred_invalid）这种「我们侧坏了」的信号全靠用户拍照
            # 才被发现。升级后同码多机聚集会直接出现在官网 /console/errors。
            logger.error("[tg_protocol_login] 发起登录失败 reason=%s type=%s: %s",
                         self.reason_code or "login_failed", type(ex).__name__, ex,
                         exc_info=True)
            await self._safe_disconnect()
        return self.result()

    async def poll(self) -> Dict[str, Any]:
        # password_needed 是稳定的等待态：不再打 ExportLoginToken（会一直抛
        # SESSION_PASSWORD_NEEDED），停在此态等 submit_password。
        if self.status in ("authorized", "failed", "expired", "password_needed"):
            return self.result()
        # 并发闸（2026-08-14 198 事故）：迁移/上一轮询进行中，本轮直接返回快照，
        # 绝不对同一 client 并发 invoke（会砸在已 stop 的 session 上抛异常）。
        if self._poll_lock.locked():
            return self.result()
        async with self._poll_lock:
            if self.status in ("authorized", "failed", "expired", "password_needed"):
                return self.result()
            try:
                if _storage_closed(self.client):
                    raise LoginStorageClosed("login client storage already closed")
                from pyrogram.raw.functions.auth import ExportLoginToken
                r = await self.client.invoke(ExportLoginToken(
                    api_id=self.api_id, api_hash=self.api_hash, except_ids=[]))
                await self._advance(r)
            except Exception as ex:  # noqa: BLE001
                self._classify_poll_failure(ex)
        return self.result()

    def _classify_poll_failure(self, ex: Exception) -> None:
        """poll 异常归因（2026-08-14 沉淀：此前一律吞成 expired + DEBUG 日志——
        扫码后的 DC 迁移失败被前端「自动换码」掩盖成无限循环，watchdog 只能看到
        「8 次尝试 0 成功」却说不出为什么）。语义分三层：

        - 等待态：SESSION_PASSWORD_NEEDED → password_needed（非失败）。
        - 令牌老化：AUTH_TOKEN_EXPIRED（扫码前）→ expired + qr_expired，
          前端自动换码是正确处置。
        - **扫码之后（_scan_seen）的任何异常都是终态失败**：token 单次有效、
          迁移半途，换码救不回来——必须把归因与行动指引亮给用户。
        """
        if _is_password_needed(ex):
            # 扫码已确认，但账号开了两步验证 → 进入等待云密码态（不算失败/过期）
            self.status = "password_needed"
            self.detail = "该账号已开启两步验证，请输入云密码完成登录"
            logger.info("[tg_protocol_login] 扫码已确认，等待两步验证云密码")
            return
        if _is_closed_db_error(ex):
            # Q-10 #268：storage 已关（cancel / TTL 清理 / 换码抢先 disconnect）——
            # 会话已被收走，换码也救不回，终态 + 一条 WARNING（不带栈：栈里只有
            # pyrogram sqlite_storage 的同一句话，13:32 那两份就是它）。
            self.status = "failed"
            self.reason_code = "session_closed"
            self.detail = "登录会话已关闭（被取消或超时清理），请重新发起扫码"
            logger.warning(
                "[tg_protocol_login] storage 已关闭，登录会话终止 scan_seen=%s type=%s: %s",
                self._scan_seen, type(ex).__name__, ex)
            return
        code = classify_login_exception(ex)
        if code == "rate_limited":
            self.status = "failed"
            self.reason_code = code
            self.retry_after_sec = _flood_wait_sec(ex)
            self.detail = str(ex)
            logger.warning("[tg_protocol_login] poll 被限流 wait=%ss: %s",
                           self.retry_after_sec, ex)
            return
        if not self._scan_seen and _is_token_expired(ex):
            self.status = "expired"
            self.reason_code = "qr_expired"
            self.detail = str(ex)
            logger.info("[tg_protocol_login] 二维码令牌过期（正常老化，前端自动刷新）")
            return
        if self._scan_seen:
            self.status = "failed"
            self.reason_code = code or "dc_migrate_failed"
            self.detail = f"扫码后建立连接失败：{ex}"
            logger.warning("[tg_protocol_login] 扫码后失败 reason=%s type=%s: %s",
                           self.reason_code, type(ex).__name__, ex, exc_info=True)
            return
        # 扫码前的其余异常：多为网络抖动 → 维持 expired 让前端自动换码重试，
        # 但归因码与 WARNING 日志不再省略（旧 DEBUG ≈ 线上全盲）。
        self.status = "expired"
        self.reason_code = code
        self.detail = str(ex)
        logger.warning("[tg_protocol_login] poll 失败（扫码前）reason=%s type=%s: %s",
                       code or "unknown", type(ex).__name__, ex)

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
                # 非密码错误的 2FA 失败（扫码已确认后的收尾故障）罕见且重要 → ERROR 进 beacon
                logger.error("[tg_protocol_login] check_password 失败 type=%s: %s",
                             type(ex).__name__, ex, exc_info=True)
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
            # MigrateTo 只在用户扫码确认后出现：先亮 scanned（并发 poll 返回的快照
            # 会立刻带上），前端从「等待扫码」切到「已扫码，正在建立安全连接」；
            # 再做耗时的 DC 迁移（跨区账号归属 DC 与默认 DC 不同是常态）。
            self._scan_seen = True
            self.status = "scanned"
            self.detail = ""
            try:
                await self._migrate(r.dc_id)
                r2 = await self.client.invoke(ImportLoginToken(token=r.token))
            except Exception as ex:  # noqa: BLE001
                if _is_password_needed(ex):
                    raise  # 2FA 等待态由 poll 归因，重迁移纯属浪费
                if _is_closed_db_error(ex) or _storage_closed(self.client):
                    raise  # storage 已关：重试只会再撞一次 closed database（Q-10 #268）
                # 迁移链（新 DC 握手 / 导入令牌）自动重试一次：跨区首次握手
                # 偶发超时是常态，直接判死会把可救的登录变成用户可见失败。
                logger.warning("[tg_protocol_login] DC 迁移首试失败，自动重试一次 "
                               "dc=%s type=%s: %s", r.dc_id, type(ex).__name__, ex)
                await self._migrate(r.dc_id)
                r2 = await self.client.invoke(ImportLoginToken(token=r.token))
            await self._advance(r2)
        elif isinstance(r, LoginTokenSuccess):
            await self._finish(r)
        else:
            self.status = "failed"
            self.detail = f"未知的登录响应：{type(r).__name__}"

    async def _migrate(self, dc_id: int) -> None:
        """切换到目标 DC（扫码后账号归属 DC 通常与默认 DC 不同）。

        ⚠ loop 亲和性（2026-08-14 根因，勿回退成裸 await）：pyrogram 的 ``sync.py``
        会把**所有 Client 公开方法**（connect/invoke/disconnect…）包一层
        ``async_to_sync``——从**非主线程**（本进程 web 请求全在 web 线程 loop 上）
        调用时，真正的协程被 ``run_coroutine_threadsafe`` **调度到主线程 loop** 执行。
        于是 ``connect()`` 建出的 ``Session``（含 ping_task/recv_task）都活在主 loop；
        而本方法操作的是 Session/Auth **内部对象**（不在包装范围），裸 await 会在
        web loop 上执行 ``session.stop()`` → ``await ping_task`` 撞
        「attached to a different loop」→ 跨区账号扫码后 DC 迁移必败（本地同 DC
        账号不走迁移所以从未暴露）。修法＝整段迁移调度到 **session 自己的 loop**
        上执行；同 loop 场景（单测/主线程运行）自动走直连路径，行为不变。
        """
        from pyrogram.session import Auth, Session

        # Q-10 #268：迁移前先探 storage——已关（另一路 disconnect 收走了会话）就别碰
        # session.stop()/storage.dc_id()，直接抛可归因异常，_advance 不重试、poll 落终态。
        if _storage_closed(self.client):
            logger.info("[tg_protocol_login] DC 迁移前 storage 已关闭，跳过迁移 dc=%s status=%s",
                        dc_id, self.status)
            raise LoginStorageClosed(f"storage closed before migrate to dc={dc_id}")

        async def _do() -> None:
            await self.client.session.stop()
            await self.client.storage.dc_id(dc_id)
            test_mode = await self.client.storage.test_mode()
            auth_key = await Auth(self.client, dc_id, test_mode).create()
            await self.client.storage.auth_key(auth_key)
            self.client.session = Session(
                self.client, dc_id, auth_key, test_mode)
            await self.client.session.start()

        sess_loop = getattr(getattr(self.client, "session", None), "loop", None)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if sess_loop is not None and running is not None and sess_loop is not running:
            # 迁移全程（stop→auth→新 Session→start）在 session 所属 loop 上原子执行；
            # 新 Session 也因此绑到同一 loop，与后续包装过的 invoke（同样被调度到
            # 主 loop）保持一致。120s 上限防「目标 loop 卡死 → poll 锁被永久占用」。
            fut = asyncio.run_coroutine_threadsafe(_do(), sess_loop)
            await asyncio.wait_for(asyncio.wrap_future(fut), timeout=120)
        else:
            await _do()

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
                # P1-198 升级为 INFO：session_string 缺位=该号重启后只能靠文件
                # session 拉起（抗 DC 迁移弱一档）。198 实锤两个号 meta 均无
                # session_string 且无任何日志痕迹——静默弱化不可接受。
                logger.info("[tg_protocol_login] 导出 session_string 失败"
                            "（该号将依赖文件 session 恢复）", exc_info=True)
        except Exception as ex:  # noqa: BLE001
            self.status = "failed"
            self.detail = f"完成登录失败：{ex}"
            # 用户已扫码确认、收尾却失败＝最伤信任的一档，必须远程可见
            logger.error("[tg_protocol_login] finish 失败 type=%s: %s",
                         type(ex).__name__, ex, exc_info=True)
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
            # 必须带 reason_code（同 messenger_web 的教训）：只回 instruction 的话，
            # start 路由不会置终态，会话挂到 TTL 耗尽——用户对着转圈干等 3 分钟。
            return {"instruction": "未配置 Telegram api_id/api_hash，无法发起协议登录。",
                    "reason_code": "creds_missing"}
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

        # ── 凭据无感换发（2026-08-10 API_ID_INVALID 事故闭环）────────────────
        # 托管注入的凭据废了（池组死）→ 当场向官网举报换新组 → 同一轮重试一次。
        # 用户视角：二维码照常出现，全程无感；换发也失败才把 cred_invalid 露给 UI。
        # 只对「官网托管注入」的凭据自愈（LAN credpool 有自己的 report/release 闭环；
        # 用户自填的值绝不代改）。设备指纹沿用同一种子——同一台机器的身份不因换组漂移。
        if (login.status == "failed" and login.reason_code == "cred_invalid"
                and alloc.source != "credpool"):
            try:
                from src.ai import hosted_gateway as _hg
                swapped = _hg.report_invalid_and_refetch(cfg, str(api_id))
            except Exception:  # noqa: BLE001 —— 自愈失败保持原失败语义，绝不加戏
                logger.debug("[tg_protocol_login] 废凭据换发异常（忽略）", exc_info=True)
                swapped = None
            if swapped:
                api_id, api_hash = swapped  # TelegramQrLogin.__init__ 自会 int() 校型
                # 出口跟着新组走（P2-⑨）：换发可能从「无出口组」换到「带出口组」
                # （或反之），_hosted_proxy 已被换发函数同步更新；显式 ctx 代理仍最高优先
                proxy = (_to_pyrogram_proxy((ctx or {}).get("proxy"))
                         or _to_pyrogram_proxy(
                             (cfg.get("telegram") or {}).get("_hosted_proxy")))
                retry = TelegramQrLogin(
                    api_id, api_hash, sessions_dir, proxy=proxy, device_kwargs=device_fp)
                await retry.start()
                logger.info("[tg_protocol_login] 换发后重试 status=%s（api_id=%s）",
                            retry.status, api_id)
                login = retry

        # 换发也救不回来（或不适用换发）→ 把处置元信息落到最终 login 上，随
        # result() 进 start/poll 响应；前端据此倒计时自动重试或亮修正表单。
        if login.status == "failed" and login.reason_code == "cred_invalid":
            _meta = cred_fail_meta(cfg, alloc)
            login.cred_source = str(_meta.get("cred_source") or "")
            login.retry_after_sec = int(_meta.get("retry_after_sec", -1))

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
                # P2-⑨ 托管凭据随账号落库（复用池内号的 META_CRED_KEY 缓存，runner
                # 优先读它）：托管机器换组后（隔离/换发），config 注入的是**新组**，
                # 而本账号 session 是**这组**建的——不落库的话 runner 重启就会拿
                # 新组凭据跑旧 session（session×api_id 错配=风控信号）。出口一并存，
                # 重启后仍按登录时的出口连（IP 恒定同样是隔离三件套的一部分）。
                if (not used_pool_key and alloc.source != "credpool"
                        and (cfg.get("telegram") or {}).get("_hosted_cred")):
                    try:
                        _hp = (cfg.get("telegram") or {}).get("_hosted_proxy")
                        _cp.remember_cred(
                            {"platform": "telegram",
                             "account_id": res["account_id"], "meta": {}},
                            _cp.Allocation(int(api_id), str(api_hash), "config",
                                           "free",
                                           dict(_hp) if isinstance(_hp, dict) else None))
                    except Exception:  # noqa: BLE001
                        logger.debug("[tg_protocol_login] 托管凭据落库失败（忽略）",
                                     exc_info=True)
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

        out: Dict[str, Any] = {
            "qr_url": login.qr_url,
            "instruction": "用手机 Telegram：设置 → 设备 → 关联桌面设备，扫描二维码。",
            # i18n 键随会话下发：英文坐席按键取本地化指引，raw instruction 仅兜底
            "instruction_key": "inbox.connect.instr_tg_protocol",
            "poll": _poll,
            "submit_password": _submit_password,
            "cancel": _cancel,
            "state": login,
        }
        # 开局即失败（凭据废/连不上）：与 phone 侧同口径把终态直接回给 start 路由，
        # 前端零轮询窗口看到真相（旧行为要等首轮 poll 2.5s 后才知道失败——用户
        # 这 2.5s 对着「正在生成二维码…」的转圈干等）。
        if login.status == "failed":
            out["status"] = login.status
            out["reason_code"] = login.reason_code
            out["detail"] = login.detail
            if login.cred_source:
                out["cred_source"] = login.cred_source
                out["retry_after_sec"] = int(login.retry_after_sec)
            # 新前端看到终态不再发起首轮 poll → 池容量不能再靠 _poll 顺路归还。
            # 与 phone 侧同口径在此就地归还（release 幂等，老前端多 poll 一次无害）。
            if used_pool_key:
                _release_if_abandoned("start_failed")
        return out

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
