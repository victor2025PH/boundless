"""Discord Bot worker：在本进程内保活一条 Gateway 长连，收发消息接统一收件箱。

架构位置与 ``TelegramProtocolWorker`` 完全同构（编排器 ``register_worker("discord",
"protocol", …)`` 拉起），差异只在三处平台事实：

1. **不能主动私聊陌生人**。Discord Bot 只有在「与对方有共同服务器且对方允许服务器成员
   私信」或「对方先私信过 Bot」时才能开 DM，否则 403 ``50007``。本 worker 把它翻译成
   ``{"delivered": False, "error": "forbidden"}`` 而**不抛异常**——那不是故障，是平台
   规则，抛异常会让编排器把健康的账号标成 error 并触发重启风暴。真正的止损在上游：
   主动触达在**规划阶段**就跳过 discord（见 ``proactive_topic``），不靠这里兜。
2. **附件 URL 24h 过期**。入站附件必须**当场下载**落 ``/static``，不能只存 CDN 链接
   ——存链接的话坐席隔天点开就是死图。落盘复用 ``protocol_bridge.media_paths``
   （绝对路径铁律，见 tests/test_static_asset_paths.py）。
3. **2000 字符硬上限**。超长回复必须切分连发，切点优先段落 > 句子 > 词 > 硬切，
   避免把一句话拦腰砍断（纯函数 ``split_discord_text``，可离线单测）。

**服务器频道默认只收 @ 提及**（``guild_mode: mention``）：Bot 一旦被拉进上千人的服务器，
「全收」会把统一收件箱瞬间冲垮、把 SLA 看板变成噪音源。私聊永远全收（那才是客服语义）。

依赖 ``discord.py`` **惰性导入**：未安装时本模块仍可 import（纯函数可单测、编排器注册
被门控跳过），只有真去 ``start()`` 才报缺依赖。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Discord 单条消息硬上限（平台常量，不是可调参数）
MAX_MESSAGE_LEN = 2000

# 入站附件下载上限（超过只记文件名占位，不下载——防一条 100MB 视频卡死 Gateway 心跳）
DEFAULT_MAX_MEDIA_BYTES = 16 * 1024 * 1024

# 出站上传上限的**兜底**值：Discord 免费档 25MiB（私聊恒为此值）。服务器加成后
# 会涨到 50/100MiB，那时以 `guild.filesize_limit` 为准（见 outbound_size_limit）——
# 把上限写死在配置里，等于让加成过的服务器白白发不了大文件。
DEFAULT_UPLOAD_LIMIT_BYTES = 25 * 1024 * 1024
#: 「文件过大」的 HTTP 状态与 Discord 业务码（413 / 40005）
TOO_LARGE_CODE = 40005

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv"}
_AUDIO_EXT = {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".flac"}

_CHAT_KEY_RE = re.compile(r"^(dm|ch):(\d+)$")


# ── 纯函数（无需 discord.py，门禁主战场） ────────────────────────────────────

def split_discord_text(text: str, limit: int = MAX_MESSAGE_LEN) -> List[str]:
    """把长文本切成 ≤limit 的若干段，切点优先级：空行 > 换行 > 句末 > 空格 > 硬切。

    为什么不用「每 2000 字硬切」：那会把一个词/一句话拦腰砍断，客户看到的是两条断句，
    比分两条本身更像机器人。空串返回 ``[]``（调用方据此不发，而不是发一条空消息）。
    """
    s = str(text or "")
    if not s.strip():
        return []
    if len(s) <= limit:
        return [s]
    out: List[str] = []
    rest = s
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for sep in ("\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", " "):
            idx = window.rfind(sep)
            # 太靠前的切点（<40% 窗口）宁可不用，否则会切出一堆碎片
            if idx > limit * 0.4:
                cut = idx + len(sep)
                break
        if cut <= 0:
            cut = limit
        piece = rest[:cut].rstrip()
        if piece:
            out.append(piece)
        rest = rest[cut:].lstrip()
    if rest.strip():
        out.append(rest)
    return out


def parse_chat_key(chat_key: str) -> Tuple[str, int]:
    """``dm:123`` → ``("dm", 123)``；``ch:456`` → ``("ch", 456)``；裸数字 → ``("raw", n)``。

    ``raw`` 表示「不知道是用户还是频道」，由调用方先试频道再试用户（宽容解析，
    兼容早期/外部写入的裸 id）。无法解析返回 ``("", 0)``。
    """
    k = str(chat_key or "").strip()
    m = _CHAT_KEY_RE.match(k)
    if m:
        return m.group(1), int(m.group(2))
    if k.isdigit():
        return "raw", int(k)
    return "", 0


def dm_chat_key(user_id: Any) -> str:
    return f"dm:{user_id}"


def channel_chat_key(channel_id: Any) -> str:
    return f"ch:{channel_id}"


def attachment_kind(filename: str, content_type: str = "") -> Tuple[str, str]:
    """``(media_type, ext)``——media_type ∈ image|video|voice|file，与本仓其他平台同词表。"""
    name = str(filename or "")
    ext = os.path.splitext(name)[1].lower()
    ct = str(content_type or "").lower()
    if ext in _IMAGE_EXT or ct.startswith("image/"):
        return "image", ext or ".jpg"
    if ext in _VIDEO_EXT or ct.startswith("video/"):
        return "video", ext or ".mp4"
    if ext in _AUDIO_EXT or ct.startswith("audio/"):
        return "voice", ext or ".ogg"
    return "file", ext or ".bin"


def should_ingest(
    *, is_dm: bool, is_self: bool, author_is_bot: bool, mentioned: bool,
    guild_mode: str = "mention", ignore_bots: bool = True,
) -> bool:
    """要不要把这条消息推进统一收件箱（纯函数，入站过滤的单一事实源）。

    三条不变量：
    - **自己发的永不收**（send() 已经镜像过一次，再收一次就是重复 + 自问自答死循环）；
    - **其他 Bot 默认不收**（两个 Bot 互相触发自动回复＝无限对轰，本仓最贵的事故形态）；
    - **服务器频道按 guild_mode 分档**，私聊永远收。
    """
    if is_self:
        return False
    if author_is_bot and ignore_bots:
        return False
    if is_dm:
        return True
    gm = str(guild_mode or "mention").lower()
    if gm == "off":
        return False
    if gm == "all":
        return True
    return bool(mentioned)


def discord_message_payload(
    message: Any, account_id: str, *, media_type: str = "", media_ref: str = "",
    mentioned: bool = False,
) -> Optional[Dict[str, Any]]:
    """把一条 discord.Message 归一为 ``emit_incoming`` 的消息 dict。

    只用 ``getattr``，不 import discord —— 单测可传任意 duck-typed 对象
    （与 ``protocol_bridge.tg_message_payload`` 同一范式）。
    """
    channel = getattr(message, "channel", None)
    author = getattr(message, "author", None)
    if channel is None or author is None:
        return None
    guild = getattr(message, "guild", None)
    author_id = getattr(author, "id", None)
    # 群组私信（GroupChannel）同样没有 guild，但**不是** 1:1：按发言人建 dm: 键会把
    # 一个群聊裂成 N 个独立会话，回复还会私发给某个人而不是发回群里。
    # 判据取 ``recipients``（复数，仅群组私信有；1:1 的 DMChannel 是单数 recipient）。
    is_group_dm = guild is None and getattr(channel, "recipients", None) is not None
    is_private = guild is None and not is_group_dm
    if is_private:
        if author_id is None:
            return None
        chat_key = dm_chat_key(author_id)
        name = str(getattr(author, "global_name", None)
                   or getattr(author, "display_name", None)
                   or getattr(author, "name", "") or author_id)
        chat_type = "private"
    else:
        cid = getattr(channel, "id", None)
        if cid is None:
            return None
        chat_key = channel_chat_key(cid)
        gname = str(getattr(guild, "name", "") or "")
        cname = str(getattr(channel, "name", "") or cid)
        name = f"{gname} #{cname}" if gname else f"#{cname}"
        chat_type = "group"

    created = getattr(message, "created_at", None)
    ts = created.timestamp() if hasattr(created, "timestamp") else time.time()
    avatar = getattr(author, "display_avatar", None)
    avatar_url = str(getattr(avatar, "url", "") or "") if avatar is not None else ""

    from src.integrations.protocol_bridge import make_message
    msg = make_message(
        platform="discord", account_id=str(account_id), chat_key=chat_key,
        name=name, text=str(getattr(message, "content", "") or ""), ts=ts,
        msg_id=str(getattr(message, "id", "") or ""), direction="in",
        media_type=media_type, media_ref=media_ref,
        username=str(getattr(author, "name", "") or ""),
        avatar_url=avatar_url,
        source={"chat_type": chat_type},
    )
    # 群聊里「谁说的」与「哪个会话」是两件事，落库分列（私聊二者同一人）
    msg["sender_id"] = str(author_id or "")
    msg["sender_name"] = name if is_private else str(
        getattr(author, "display_name", None) or getattr(author, "name", "") or "")
    # 频道/群组私信被 @ 到 → 落库 mentioned，让群组视图的「点名我」筛选与提醒生效；
    # 1:1 私聊天然算点名（整条会话就是冲着我们来的），与其他平台口径一致。
    # 群组私信**不**自动算点名：Bot 被拉进一个群聊不代表每句话都在跟它说话。
    msg["mentioned"] = True if is_private else bool(mentioned)
    return msg


#: Discord 「无法私信该用户」的业务错误码（403 之下还有几十种，这个是我们关心的那个）
DM_FORBIDDEN_CODE = 50007


def is_forbidden(exc: BaseException) -> bool:
    """这个异常是不是「平台规则不让发」（403），而不是我们的故障。

    **刻意不按类名精确匹配**：``discord.Forbidden`` 是 ``HTTPException`` 的子类，
    调用方可能拿到它的子类、也可能拿到别处包装过的同义异常；只认 ``__name__ ==
    "Forbidden"`` 会在这些形态上静默失手，把「对方不让私信」抛成故障——那正是这条
    路径存在的全部意义（见模块 docstring 第 1 条）。故按三个**语义**判据取并集：
    MRO 上出现 Forbidden、HTTP 状态 403、或 Discord 业务码 50007。
    """
    for cls in type(exc).__mro__:
        if cls.__name__ == "Forbidden":
            return True
    if getattr(exc, "status", None) == 403:
        return True
    return getattr(exc, "code", None) == DM_FORBIDDEN_CODE


def mention_signal(message: Any, me: Any) -> bool:
    """这条频道消息是不是「在叫我们」——`guild_mode=mention` 的唯一判据。

    只看 ``message.mentions`` 会漏掉社群里**最常见**的召唤方式：ping 一个客服身份组
    （`@客服`）而不是 ping Bot 本身。漏掉的后果是运营开了 mention 档却发现「@客服
    没人理」，只能退回 `all` 把整个服务器的闲聊灌进收件箱。

    刻意**不认** ``@everyone`` / ``@here``：那是广播不是召唤，认了等于每次全员通知都
    往收件箱灌一条。discord.py 把它们单独放在 ``mention_everyone``、本就不进
    ``role_mentions``；这里再显式排掉默认身份组（每个成员都带 ``@everyone`` 角色，
    万一哪天平台把它塞进 role_mentions，就会变成「全员通知必进收件箱」）。

    回复 Bot 的消息天然带上作者提及，会走 ``mentions`` 命中——那正是我们想要的：
    有人回复 Bot，就是在跟 Bot 说话。
    """
    if me is None:
        return False
    try:
        if me in (getattr(message, "mentions", None) or []):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        pinged = [r for r in (getattr(message, "role_mentions", None) or [])
                  if not _is_default_role(r)]
        if not pinged:
            return False
        guild = getattr(message, "guild", None)
        my_roles = getattr(getattr(guild, "me", None), "roles", None) or []
        return any(r in my_roles for r in pinged)
    except Exception:  # noqa: BLE001
        return False


def _is_default_role(role: Any) -> bool:
    try:
        fn = getattr(role, "is_default", None)
        return bool(fn()) if callable(fn) else False
    except Exception:  # noqa: BLE001
        return False


def is_too_large(exc: BaseException) -> bool:
    """这个异常是不是「文件超过上传上限」（413 / 40005）。

    与 :func:`is_forbidden` 同理由按语义判而非类名：它是可预期的**投递失败**，
    不是我们的故障——抛出去会让上层按异常处理（栈打进日志、deferred 当故障重试），
    而运营需要看到的只是一句「这个文件太大，换个小的」。
    """
    if getattr(exc, "status", None) == 413:
        return True
    return getattr(exc, "code", None) == TOO_LARGE_CODE


def outbound_size_limit(dest: Any, fallback: int = DEFAULT_UPLOAD_LIMIT_BYTES) -> int:
    """该目的地允许的上传字节上限。

    优先取 Discord 自己算好的 ``guild.filesize_limit``（随服务器加成等级变化，
    也随平台调整免费档时自动跟进）；私聊没有 guild → 用兜底值。
    """
    try:
        guild = getattr(dest, "guild", None)
        lim = int(getattr(guild, "filesize_limit", 0) or 0)
        if lim > 0:
            return lim
    except Exception:  # noqa: BLE001
        pass
    return max(1, int(fallback or DEFAULT_UPLOAD_LIMIT_BYTES))


def too_large_result(size: int, limit: int) -> Dict[str, Any]:
    mb = lambda n: f"{n / 1048576:.1f}MB"  # noqa: E731
    return {"delivered": False, "error": "media_too_large",
            "detail": f"文件 {mb(size)} 超过该频道上传上限 {mb(limit)}"
                      "（服务器加成等级越高上限越大；或改发链接）"}


FORBIDDEN_DETAIL = "Discord Bot 无法私信该用户（需与 Bot 有共同服务器且对方允许服务器成员私信）"
FORBIDDEN_CHANNEL_DETAIL = (
    "Discord Bot 在该频道没有发言权限（检查频道权限覆盖，或重新邀请 Bot 时勾选 "
    "Send Messages / Attach Files）")


def forbidden_result(raw: str = "", *, chat_key: str = "",
                     code: Any = None) -> Dict[str, Any]:
    """403 的统一返回（**不是异常**，见模块 docstring 第 1 条）。

    ``detail`` 恒为一句人话、平台原文另放 ``raw``：坐席界面直显 ``detail``，
    而 Discord 的原文常常只是一句 ``50007``——把它当解释给人看等于没说。

    **两类 403 必须分开说**：私聊被拒（``50007``）要去引导对方加服务器/开 DM，
    频道无权限（``50013`` 等）要去改频道权限覆盖或重邀 Bot。给频道权限问题
    显示「对方不让私信」，运营会照着完全错误的方向排查一整天。
    判据优先取业务码，取不到则退到会话类型（``ch:`` = 频道）。
    """
    if code == DM_FORBIDDEN_CODE:
        detail = FORBIDDEN_DETAIL
    elif code is not None:
        detail = FORBIDDEN_CHANNEL_DETAIL
    else:
        detail = (FORBIDDEN_CHANNEL_DETAIL
                  if str(chat_key or "").startswith("ch:") else FORBIDDEN_DETAIL)
    out = {"delivered": False, "error": "forbidden", "detail": detail}
    if str(raw or "").strip():
        out["raw"] = str(raw)
    return out


# ── worker ───────────────────────────────────────────────────────────────────

class DiscordBotWorker:
    """保活一条 Discord Bot Gateway 连接（token 来自账号 meta，回落配置）。"""

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account or {}
        self.config = config or {}
        self.account_id = str(self.account.get("account_id") or "")
        self.client: Any = None
        self.state = "stopped"
        self.detail = ""
        self._task: Optional[asyncio.Task] = None
        self._bot_name = ""
        self._started_at = 0.0

    # ── 配置 ────────────────────────────────────────────────────────────────

    def _cfg(self) -> Dict[str, Any]:
        from src.integrations.discord_bot_login import discord_cfg
        return discord_cfg(self.config)

    def _token(self) -> str:
        meta = self.account.get("meta") or {}
        tok = str(meta.get("bot_token") or "").strip()
        if tok:
            return tok
        from src.integrations.discord_bot_login import configured_token
        return configured_token(self.config)

    def _guild_mode(self) -> str:
        from src.integrations.discord_bot_login import guild_mode
        return guild_mode(self.config)

    def _max_media_bytes(self) -> int:
        try:
            return int(self._cfg().get("max_media_bytes") or DEFAULT_MAX_MEDIA_BYTES)
        except Exception:  # noqa: BLE001
            return DEFAULT_MAX_MEDIA_BYTES

    def _proxy_url(self) -> str:
        """账号绑定的代理（一号一出口，与本仓其他平台同纪律）。取不到即直连。"""
        pid = self.account.get("proxy_id") or ""
        if not pid:
            return ""
        try:
            from src.integrations.proxy_pool import get_proxy_pool
            p = get_proxy_pool().get(pid, mask=False) or {}
            url = str(p.get("url") or "")
            if url:
                return url
            host, port = p.get("host"), p.get("port")
            if not (host and port):
                return ""
            scheme = str(p.get("scheme") or p.get("type") or "http")
            user, pwd = p.get("username") or "", p.get("password") or ""
            auth = f"{user}:{pwd}@" if user else ""
            return f"{scheme}://{auth}{host}:{port}"
        except Exception:  # noqa: BLE001
            logger.debug("[discord-worker] 代理解析失败", exc_info=True)
            return ""

    # ── 生命周期 ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        token = self._token()
        if not token:
            raise RuntimeError("缺少 Discord Bot Token（账号 meta.bot_token 或配置 platform_login.discord.bot_token）")
        try:
            import discord
        except Exception as ex:  # noqa: BLE001
            raise RuntimeError(f"未安装 discord.py：{ex}") from ex

        await self._close_client()

        intents = discord.Intents.default()
        # message_content 是 privileged intent：开发者后台没勾就会在连接期抛
        # PrivilegedIntentsRequired（下面翻译成人话）。不开的话所有消息 content 全空，
        # 等于收了一堆空消息——那比连不上更难排查，所以宁可硬失败。
        intents.message_content = True
        intents.dm_messages = True
        intents.guild_messages = True

        kwargs: Dict[str, Any] = {"intents": intents, "max_messages": 1000}
        proxy = self._proxy_url()
        if proxy:
            kwargs["proxy"] = proxy
        try:
            client = discord.Client(**kwargs)
        except TypeError:
            kwargs.pop("proxy", None)
            client = discord.Client(**kwargs)
        self.client = client
        self._wire_inbound(client)

        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run(token))
        ready = loop.create_task(client.wait_until_ready())
        timeout = float(self._cfg().get("connect_timeout") or 45)
        done, _pending = await asyncio.wait(
            {self._task, ready}, timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED)

        if ready in done and not ready.cancelled():
            self.state = "running"
            self.detail = ""
            self._started_at = time.time()
            me = getattr(client, "user", None)
            self._bot_name = str(getattr(me, "name", "") or "")
            logger.info("[discord-worker] 已连接 bot=%s account=%s guilds=%d",
                        self._bot_name, self.account_id,
                        len(getattr(client, "guilds", []) or []))
            return

        ready.cancel()
        # start 任务先结束 = 连接失败（正常情况它会一直跑到 stop 才返回）
        err = "连接超时"
        if self._task in done:
            exc = self._task.exception() if not self._task.cancelled() else None
            err = self._explain(exc) if exc else "连接被关闭"
        await self._close_client()
        self.state = "error"
        self.detail = err
        raise RuntimeError(f"Discord 连接失败：{err}")

    @staticmethod
    def _explain(exc: BaseException) -> str:
        """把 discord.py 的异常翻成运营能照做的一句话（首次接入最常踩的两个坑）。"""
        name = type(exc).__name__
        if name == "PrivilegedIntentsRequired":
            return ("未开启 MESSAGE CONTENT INTENT——请到 Discord 开发者后台 → "
                    "你的应用 → Bot → Privileged Gateway Intents 勾选 "
                    "「MESSAGE CONTENT INTENT」后重试。")
        if name == "LoginFailure":
            return "Bot Token 无效或已被重置，请重新生成后更新配置。"
        return f"{name}: {exc}"

    async def _run(self, token: str) -> None:
        try:
            await self.client.start(token)
        except asyncio.CancelledError:  # noqa: PERF203
            raise
        except Exception as ex:  # noqa: BLE001
            self.state = "error"
            self.detail = self._explain(ex)
            logger.warning("[discord-worker] Gateway 断开 account=%s: %s",
                           self.account_id, self.detail)
            raise

    async def _close_client(self) -> None:
        client, task = self.client, self._task
        self.client, self._task = None, None
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except Exception:  # noqa: BLE001
                pass

    async def stop(self) -> None:
        await self._close_client()
        self.state = "stopped"
        self.detail = ""

    async def healthy(self) -> bool:
        c = self.client
        if c is None:
            return False
        try:
            return bool(not c.is_closed() and c.is_ready())
        except Exception:  # noqa: BLE001
            return False

    def status(self) -> Dict[str, Any]:
        c = self.client
        latency = 0.0
        guilds = 0
        try:
            if c is not None:
                lat = float(getattr(c, "latency", 0) or 0)
                # 未连接时 discord.py 的 latency 是 nan
                latency = round(lat * 1000, 1) if lat == lat else 0.0
                guilds = len(getattr(c, "guilds", []) or [])
        except Exception:  # noqa: BLE001
            pass
        return {"type": "discord_bot", "state": self.state, "detail": self.detail,
                "bot_name": self._bot_name, "guilds": guilds,
                "latency_ms": latency, "guild_mode": self._guild_mode(),
                "uptime_sec": int(time.time() - self._started_at) if self._started_at else 0}

    # ── 入站 ────────────────────────────────────────────────────────────────

    def _wire_inbound(self, client: Any) -> None:
        account_id = self.account_id
        ignore_bots = bool(self._cfg().get("ignore_bots", True))
        download = bool(self._cfg().get("download_media", True))

        async def on_message(message: Any) -> None:  # noqa: ANN401
            try:
                me = getattr(client, "user", None)
                author = getattr(message, "author", None)
                is_self = bool(me is not None and author is not None
                               and getattr(author, "id", None) == getattr(me, "id", None))
                mentioned = mention_signal(message, me)
                if not should_ingest(
                    is_dm=getattr(message, "guild", None) is None,
                    is_self=is_self,
                    author_is_bot=bool(getattr(author, "bot", False)),
                    mentioned=mentioned,
                    guild_mode=self._guild_mode(),
                    ignore_bots=ignore_bots,
                ):
                    return
                media_type, media_ref = ("", "")
                if download:
                    media_type, media_ref = await self._download_first_attachment(message)
                payload = discord_message_payload(
                    message, account_id, media_type=media_type,
                    media_ref=media_ref, mentioned=mentioned)
                if payload is None:
                    return
                from src.integrations.protocol_bridge import emit_incoming, maybe_auto_reply
                emit_incoming(payload)
                await maybe_auto_reply(payload)
            except Exception:  # noqa: BLE001
                logger.debug("[discord-worker] inbound 处理失败", exc_info=True)

        try:
            client.event(on_message)
        except Exception:  # noqa: BLE001
            logger.debug("[discord-worker] 注册消息处理器失败", exc_info=True)

    async def _download_first_attachment(self, message: Any) -> Tuple[str, str]:
        """下载首个附件落 ``/static``（CDN 链接 24h 过期，必须当场存字节）。"""
        atts = list(getattr(message, "attachments", None) or [])
        if not atts:
            return "", ""
        if len(atts) > 1:
            # Discord 一条消息可挂 10 个附件，收件箱一条消息只有一个媒体位。
            # 只取首个是刻意取舍，但必须留痕——否则「客户发了 5 张图我们只看见 1 张」
            # 会被当成客户没发，坐席对着残缺信息回复。
            logger.info("[discord-worker] 消息含 %d 个附件，仅收首个 msg=%s",
                        len(atts), getattr(message, "id", ""))
        att = atts[0]
        kind = ""
        try:
            kind, ext = attachment_kind(str(getattr(att, "filename", "") or ""),
                                        str(getattr(att, "content_type", "") or ""))
            size = int(getattr(att, "size", 0) or 0)
            cap = self._max_media_bytes()
            if cap and size > cap:
                # 与 download_tg_media 同口径：**保留类型、不下载**。丢掉类型会让
                # 「只有一张大图、没有文字」的消息落成一条彻头彻尾的空消息，
                # 坐席侧表现为「会话冒泡但点开什么都没有」。
                logger.info("[discord-worker] 附件超限跳过下载 size=%d cap=%d", size, cap)
                return kind, ""
            data = await att.read()
            if not data:
                return kind, ""
            from src.integrations.protocol_bridge import media_paths
            dest, url = media_paths(
                "discord", f"{self.account_id}_{getattr(message, 'id', '')}", ext)
            with open(dest, "wb") as fh:
                fh.write(data)
            return kind, url
        except Exception:  # noqa: BLE001
            # 保留已算出的类型（与超限分支同口径）：下载失败不该让一条「只有图」的
            # 消息落成彻底的空消息，坐席至少要看得见「对方发了张图但我们没取到」。
            logger.debug("[discord-worker] 附件下载失败", exc_info=True)
            return kind, ""

    # ── 出站 ────────────────────────────────────────────────────────────────

    async def _resolve_dest(self, chat_key: str) -> Any:
        if self.client is None:
            raise RuntimeError("discord client 未连接")
        kind, ident = parse_chat_key(chat_key)
        if not ident:
            raise RuntimeError(f"无法解析 chat_key: {chat_key!r}")
        c = self.client
        if kind in ("ch", "raw"):
            ch = c.get_channel(ident)
            if ch is not None:
                return ch
            try:
                return await c.fetch_channel(ident)
            except Exception:
                if kind == "ch":
                    raise
        user = c.get_user(ident)
        if user is None:
            user = await c.fetch_user(ident)
        return getattr(user, "dm_channel", None) or await user.create_dm()

    async def send(self, chat_key: str, text: str,
                   reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        parts = split_discord_text(text)
        if not parts:
            return {"delivered": False, "error": "empty_text"}
        try:
            dest = await self._resolve_dest(chat_key)
        except Exception as ex:  # noqa: BLE001
            if is_forbidden(ex):
                return forbidden_result(str(ex), chat_key=chat_key,
                                        code=getattr(ex, "code", None))
            raise
        ref = self._message_reference(dest, reply_to)
        last_id = ""
        try:
            for i, piece in enumerate(parts):
                kw = {"reference": ref} if (ref is not None and i == 0) else {}
                msg = await dest.send(piece, **kw)
                last_id = str(getattr(msg, "id", "") or "")
        except Exception as ex:  # noqa: BLE001
            if is_forbidden(ex):
                # 已发出去的部分算数（不重发），未发的丢弃——半条总比重复两条好
                return forbidden_result(
                    str(ex), chat_key=chat_key, code=getattr(ex, "code", None),
                ) if not last_id else {
                    "delivered": True, "message_id": last_id, "partial": True}
            raise
        return {"delivered": True, "message_id": last_id,
                "parts": len(parts) if len(parts) > 1 else 1}

    @staticmethod
    def _message_reference(dest: Any, reply_to: Optional[Dict[str, Any]]) -> Any:
        mid = str((reply_to or {}).get("msg_id") or "").strip()
        if not mid.isdigit():
            return None
        try:
            import discord
            return discord.MessageReference(
                message_id=int(mid), channel_id=int(getattr(dest, "id", 0)),
                fail_if_not_exists=False)
        except Exception:  # noqa: BLE001
            return None

    async def send_media(self, chat_key: str, *, media_path: str,
                         media_type: str = "", caption: str = "") -> Dict[str, Any]:
        if not media_path or not os.path.exists(media_path):
            return {"delivered": False, "error": "media_missing", "detail": media_path}
        try:
            dest = await self._resolve_dest(chat_key)
        except Exception as ex:  # noqa: BLE001
            if is_forbidden(ex):
                return forbidden_result(str(ex), chat_key=chat_key,
                                        code=getattr(ex, "code", None))
            raise
        # 先本地量一次再上传：超限的文件传上去也是被服务端拒，白耗一次带宽，而在慢
        # 出口上传一个 100MB 视频还会拖住整条出站队列几分钟。放在 import 之前是刻意的
        # ——这条判断不需要 discord.py，没必要被依赖缺失挡住。
        size = os.path.getsize(media_path)
        limit = outbound_size_limit(dest)
        if size > limit:
            logger.info("[discord-worker] 出站文件超限 size=%d limit=%d", size, limit)
            return too_large_result(size, limit)
        try:
            import discord
        except Exception as ex:  # noqa: BLE001
            raise RuntimeError(f"未安装 discord.py：{ex}") from ex
        try:
            # caption 也受 2000 上限约束；超长时图随首段发、余下补发文本
            caps = split_discord_text(caption)
            head = caps[0] if caps else ""
            msg = await dest.send(
                content=head or None,
                file=discord.File(media_path, filename=os.path.basename(media_path)))
            mid = str(getattr(msg, "id", "") or "")
            for piece in caps[1:]:
                await dest.send(piece)
            return {"delivered": True, "message_id": mid}
        except Exception as ex:  # noqa: BLE001
            if is_forbidden(ex):
                return forbidden_result(str(ex), chat_key=chat_key,
                                        code=getattr(ex, "code", None))
            if is_too_large(ex):
                # 本地预检放行了但服务端拒了（filesize_limit 读不到、或平台临时收紧）
                # → 以服务端为准，仍按投递失败如实返回，不抛栈
                return too_large_result(size, min(limit, DEFAULT_UPLOAD_LIMIT_BYTES))
            raise

    async def send_chat_action(self, chat_key: str, action: str = "typing") -> bool:
        """挂「正在输入…」（Discord 无「正在录音」语义，语音同样显示为输入中）。"""
        try:
            dest = await self._resolve_dest(chat_key)
            typing = dest.typing()
            try:
                await typing            # discord.py 2.x：Typing 可直接 await（一次性触发）
            except TypeError:
                async with typing:      # 老版本只支持上下文管理器
                    await asyncio.sleep(0.2)
            return True
        except Exception:  # noqa: BLE001
            logger.debug("[discord-worker] typing 失败", exc_info=True)
            return False
