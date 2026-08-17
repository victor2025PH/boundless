"""协议账号 ↔ 统一收件箱 桥接（M6 ①）。

把 protocol 模式账号（Telegram pyrogram / WhatsApp Baileys）的**收发消息**接入统一收件箱：

- 入站（push 模型）：worker 收到消息 → ``emit_incoming(msg)`` → 经已注册的 sink 落库
  （复用 ``ingest_collected_chats``，自动触发 SSE 实时推送 + auto-draft + 智能分析），
  随后由 ``ProtocolInboxAdapter`` 从 store 读出，显示在收件箱列表。
- 出站：收件箱发送 → 编排器路由到对应 worker → 发送成功后同样 ``emit_incoming(direction=out)``
  回写，使对话线程立即可见。

设计：sink 由 web 层在启动时注册（注入 ``inbox_store``），本模块**不依赖 FastAPI**，
``ingest_incoming`` 为可单测的纯落库函数。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.inbox.ingest import ingest_collected_chats
from src.inbox.normalizer import PLATFORM_DISPLAY, message_obj, normalize_chat

logger = logging.getLogger(__name__)

_sink: Optional[Callable[[Dict[str, Any]], Any]] = None
# 入站 store 的惰性取法（web 层启动时注册 lambda: app.state.inbox_store）。
# 供非 FastAPI 模块（官方 webhook 的 auto_ai 让位护栏）只读查 automation_mode，
# 不引入对 FastAPI/app 的硬依赖。
_inbox_store_getter: Optional[Callable[[], Any]] = None

# 媒体落地：下载到 web 静态目录，前端按 /static/... URL 直接加载（复用既有 StaticFiles 挂载）
_STATIC_MEDIA_SUBDIR = "protocol_media"

_MEDIA_PLACEHOLDER = {
    "image": "[图片]", "sticker": "[贴纸]", "voice": "[语音]", "video": "[视频]",
    "document": "[文件]", "file": "[文件]",
}


def media_placeholder(media_type: str) -> str:
    """入站媒体无正文时用于 auto-draft / 会话预览的占位文案（公开，供 ingest 等复用）。"""
    return _MEDIA_PLACEHOLDER.get(str(media_type or "").lower(), "[媒体]")


def _media_placeholder(media_type: str) -> str:
    return media_placeholder(media_type)


def media_preview_text(text: str, media_type: str) -> str:
    """媒体消息的**会话列表预览**文案（P1-1，2026-08-02）。

    背景：媒体行正文自 2026-08-02 起是干净转写/配文（``[语音]``/``[图片]`` 语义由
    ``media_type`` 承载）——气泡里由媒体卡渲染形态，但**会话列表是纯文本**，没了
    标记就分不清「一句话」和「一条语音」。故预览层按 media_type 补形态标记：

    - 正文为空 → 纯占位（``[语音]``，与旧行为一致）；
    - 正文已带 ``[…]`` 标记（如 ``[图片内容] 描述``、存量旧行）→ 原样不叠加；
    - 干净正文（转写/配文）→ ``[语音] 正文``。

    只影响 conversations 预览列，消息正文不动。
    """
    t = str(text or "").strip()
    if not t:
        return media_placeholder(media_type)
    if t.startswith("["):
        return t
    return f"{media_placeholder(media_type)} {t}"


def protocol_media_root() -> Path:
    """协议媒体落地根目录：``src/web/static/protocol_media``（按需创建）。"""
    root = Path(__file__).resolve().parents[1] / "web" / "static" / _STATIC_MEDIA_SUBDIR
    return root


def static_media_ref_to_path(media_ref: str) -> Optional[str]:
    """若 ``media_ref`` 是 protocol 落地的 ``/static/protocol_media/...`` URL，
    返回其本进程可读的本地绝对路径；否则返回 None（非 protocol 媒体，交由原逻辑）。

    供媒体识别翻译端点把 URL 形态的 ref 解析成本地文件（OCR/ASR 的前提）。
    路径穿越由调用方的 base_dirs 容纳检查兜底。
    """
    ref = str(media_ref or "")
    prefix = f"/static/{_STATIC_MEDIA_SUBDIR}/"
    if not ref.startswith(prefix):
        return None
    rel = ref[len(prefix):]
    return str(protocol_media_root() / rel)


def media_paths(platform: str, name: str, ext: str) -> Tuple[Path, str]:
    """返回 (本地落地绝对路径, 浏览器可加载的 /static URL)。父目录按需创建。"""
    platform = str(platform or "x").lower()
    safe = "".join(c for c in str(name or "") if c.isalnum() or c in "_-") or \
        secrets.token_hex(6)
    ext = ext if ext.startswith(".") else f".{ext}" if ext else ".bin"
    dest_dir = protocol_media_root() / platform
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{safe}{ext}"
    url = f"/static/{_STATIC_MEDIA_SUBDIR}/{platform}/{safe}{ext}"
    return dest, url


_OUT_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_OUT_AUDIO_EXT = {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".amr", ".aac"}
_OUT_VIDEO_EXT = {".mp4", ".mov", ".webm"}


def media_type_from_ext(ext: str) -> str:
    """按扩展名归一出站媒体大类：image | voice | video | document。"""
    e = str(ext or "").lower()
    if not e.startswith("."):
        e = "." + e
    if e in _OUT_IMAGE_EXT:
        return "image"
    if e in _OUT_AUDIO_EXT:
        return "voice"
    if e in _OUT_VIDEO_EXT:
        return "video"
    return "document"


def save_outbound_media(
    platform: str, account_id: str, filename: str, data: bytes,
) -> Tuple[str, str, str]:
    """把坐席上传的出站媒体写入 static 目录。返回 (本地路径, /static URL, media_type)。"""
    ext = os.path.splitext(str(filename or ""))[1] or ".bin"
    media_type = media_type_from_ext(ext)
    name = f"out_{account_id}_{secrets.token_hex(6)}"
    dest, url = media_paths(str(platform or "x"), name, ext)
    with open(dest, "wb") as fh:
        fh.write(data or b"")
    return str(dest), url, media_type


def publish_outbound_media(
    platform: str, account_id: str, local_path: str,
) -> Tuple[str, str]:
    """把一个**已存在的本地媒体文件**变成可经 ``/static`` 访问的 URL。

    返回 ``(url, media_type)``；失败一律 ``('', '')``（调用方据此退回纯文本镜像＝旧行为）。

    为什么需要它：A 线（Telegram 原生 pyrogram 直发）的图/语音文件在**别处**生成，
    发完就镜像一条纯文字 ``[图片] 配文`` 进收件箱——坐席在工作台看不到自家人设发出去
    的图，且任何按 ``media_type`` 统计出站媒体的口径都会把 A 线整条漏掉
    （2026-07-31 实测：Telegram 868 条出站里按 media_type 只数出 1 条，而同期文本占位
    有 166 条）。B 线走 ``save_outbound_media`` 天生有 URL，这里给 A 线补上同一能力。

    已经落在 ``protocol_media`` 根下的文件**直接推 URL、不重复拷贝**；其余拷进去
    （与坐席上传出站媒体同一目录，生命周期一致）。

    每次调用的成败都进 ``outbound_mirror_stats``（2026-08-02）：失败＝该条媒体在
    坐席台退化成纯文本占位，此前完全静默，现在有计数/看板指向根因。
    """
    url, mt = _publish_outbound_media_impl(platform, account_id, local_path)
    try:
        from src.integrations.outbound_mirror_stats import get_outbound_mirror_stats
        kind = mt or media_type_from_ext(
            os.path.splitext(str(local_path or ""))[1])
        get_outbound_mirror_stats().record_publish(platform, kind, ok=bool(url))
    except Exception:
        pass
    return url, mt


def _publish_outbound_media_impl(
    platform: str, account_id: str, local_path: str,
) -> Tuple[str, str]:
    p = str(local_path or "")
    if not p or not os.path.isfile(p):
        return "", ""
    try:
        root = protocol_media_root().resolve()
        src = Path(p).resolve()
        if src.is_relative_to(root):
            rel = src.relative_to(root).as_posix()
            return f"/static/{_STATIC_MEDIA_SUBDIR}/{rel}", media_type_from_ext(
                src.suffix)
    except (OSError, ValueError):
        logger.debug("[protocol_bridge] 媒体路径归属判定失败 path=%s", p, exc_info=True)
    try:
        with open(p, "rb") as fh:
            data = fh.read()
        _local, url, mt = save_outbound_media(
            platform, account_id, os.path.basename(p), data)
        return url, mt
    except Exception:
        logger.debug("[protocol_bridge] 出站媒体发布失败 path=%s", p, exc_info=True)
        return "", ""


def tg_media_meta(message: Any) -> Optional[Tuple[str, str]]:
    """识别 pyrogram Message 的媒体类型，返回 (kind, ext)；无媒体返回 None。"""
    if getattr(message, "photo", None):
        return "image", ".jpg"
    if getattr(message, "voice", None):
        return "voice", ".ogg"
    if getattr(message, "audio", None):
        return "voice", ".mp3"
    if getattr(message, "video", None) or getattr(message, "video_note", None):
        return "video", ".mp4"
    if getattr(message, "animation", None):
        return "video", ".mp4"
    if getattr(message, "sticker", None):
        return "sticker", ".webp"
    doc = getattr(message, "document", None)
    if doc is not None:
        fn = getattr(doc, "file_name", "") or ""
        return "document", (os.path.splitext(fn)[1] or ".bin")
    return None


def tg_media_file_size(message: Any) -> int:
    """尽力取 pyrogram 媒体对象的 ``file_size``（字节）；取不到返回 0（未知→不拦截）。"""
    for attr in ("video", "video_note", "animation", "document",
                 "audio", "voice", "photo", "sticker"):
        obj = getattr(message, attr, None)
        if obj is not None:
            try:
                return int(getattr(obj, "file_size", 0) or 0)
            except (TypeError, ValueError):
                return 0
    return 0


async def download_tg_media(
    message: Any, account_id: str, *, max_bytes: int = 0,
) -> Tuple[str, str]:
    """下载 pyrogram 媒体到 static 目录，返回 (media_type, media_url)。

    - 无媒体：返回 ``('', '')``。
    - ``max_bytes > 0`` 且媒体体积已知并超限：返回 ``(kind, '')``——保留类型让调用方
      落占位（如「[视频]」），但**不下载**大文件，避免大视频拖垮收件箱/磁盘。
      体积未知（file_size 缺失）时不拦截，照常尝试下载（向后兼容）。
    - 下载失败：返回 ``('', '')``（与历史行为一致）。
    """
    meta = tg_media_meta(message)
    if not meta:
        return "", ""
    kind, ext = meta
    if max_bytes and max_bytes > 0:
        size = tg_media_file_size(message)
        if size and size > max_bytes:
            logger.info(
                "[protocol_bridge] tg 媒体超上限跳过下载 kind=%s size=%s max=%s",
                kind, size, max_bytes,
            )
            return kind, ""
    try:
        mid = str(getattr(message, "id", "") or "") or secrets.token_hex(6)
        dest, url = media_paths("telegram", f"{account_id}_{mid}", ext)
        path = await message.download(file_name=str(dest))
        if path:
            return kind, url
    except Exception:
        logger.debug("[protocol_bridge] tg 媒体下载失败", exc_info=True)
    return "", ""


def register_inbox_sink(fn: Optional[Callable[[Dict[str, Any]], Any]]) -> None:
    """注册入站消息 sink（web 层启动时注入 ``lambda m: ingest_incoming(store, **m)``）。"""
    global _sink
    _sink = fn


def get_inbox_sink() -> Optional[Callable[[Dict[str, Any]], Any]]:
    return _sink


def register_inbox_store_getter(fn: Optional[Callable[[], Any]]) -> None:
    """注册 inbox store 惰性取法（web 层启动时注入）。"""
    global _inbox_store_getter
    _inbox_store_getter = fn


def get_inbox_store() -> Any:
    """取当前 inbox store（未注册/异常返回 None）。"""
    fn = _inbox_store_getter
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None


def emit_incoming(msg: Dict[str, Any]) -> None:
    """worker 调用：把一条消息送入收件箱（sink 未注册则静默丢弃）。"""
    fn = _sink
    if fn is None:
        return
    try:
        fn(msg)
    except Exception:
        logger.debug("[protocol_bridge] sink 落库失败", exc_info=True)


# ── P4-4 已读回执（协议号在本进程内直接回写 messages.status，无需 HTTP） ──────────
#   worker 收到平台回执/发送成功后调用，走 store 的单调升级写入，前端轮询即见勾变化。

def report_message_status(
    platform: str, account_id: str, chat_key: str, msg_id: str, status: str,
) -> bool:
    """把单条出站消息的投递状态（sent/delivered/read）单调升级写入收件箱。

    best-effort：store 未注册 / 目标消息未落库 / 任何异常都静默返回 False，绝不外泄。
    """
    if not (chat_key and msg_id and status):
        return False
    store = get_inbox_store()
    if store is None:
        return False
    try:
        return bool(store.set_message_status(
            f"{str(platform).lower()}:{account_id}:{chat_key}", str(msg_id), status))
    except Exception:
        logger.debug("[protocol_bridge] 回执落库失败", exc_info=True)
        return False


def report_read_upto(
    platform: str, account_id: str, chat_key: str, max_id: Any,
) -> int:
    """对端「已读到 max_id」→ 把该会话所有 platform_msg_id ≤ max_id 的出站消息升级为 read。

    Telegram 的 ``UpdateReadHistoryOutbox`` 语义（对端把你发的消息读到某条为止）。
    best-effort：返回更新条数，异常/未就绪返回 0。
    """
    if not chat_key:
        return 0
    store = get_inbox_store()
    if store is None:
        return 0
    try:
        return int(store.mark_outbound_read_upto(
            f"{str(platform).lower()}:{account_id}:{chat_key}", max_id))
    except Exception:
        logger.debug("[protocol_bridge] 批量已读落库失败", exc_info=True)
        return 0


def tg_peer_to_chat_key(peer: Any) -> str:
    """把 pyrogram raw Peer（``PeerUser``/``PeerChat``/``PeerChannel``）归一为收件箱 chat_key
    （= pyrogram ``chat.id`` 的字符串形态：用户正数 / 群负数 / 频道 -100 前缀）。

    仅用 getattr 探测字段，无需导入 pyrogram（便于单测传 duck-typed 对象）。无法解析返回 ''。
    """
    if peer is None:
        return ""
    uid = getattr(peer, "user_id", None)
    if uid is not None:
        return str(uid)
    cid = getattr(peer, "chat_id", None)
    if cid is not None:
        return str(-int(cid))
    chid = getattr(peer, "channel_id", None)
    if chid is not None:
        return f"-100{int(chid)}"
    return ""


# ── Phase 3：protocol 自动回复 hook（与入站 sink 分离，async，可不挂）──────────
#   worker 收到入站消息 → 已 emit_incoming 落库后 → maybe_auto_reply(payload)。
#   web 层在启动时 register_reply_hook(build_reply_hook(app))；默认全局/账号双闸门皆关。
_reply_hook: Optional[Callable[[Dict[str, Any]], Any]] = None


def register_reply_hook(fn: Optional[Callable[[Dict[str, Any]], Any]]) -> None:
    global _reply_hook
    _reply_hook = fn


def get_reply_hook() -> Optional[Callable[[Dict[str, Any]], Any]]:
    return _reply_hook


def _publish_outbound_event(chat: Dict[str, Any]) -> None:
    """出站镜像新插入后发 outbound_message 事件（SSE → 工作台即时刷新气泡/列表预览）。

    与 ingest.py::_publish_inbox_message 同形 payload + direction/preview 语义一致；
    独立事件类型使前端可以「刷新不加未读」。best-effort，绝不影响发送主流程。
    """
    try:
        from src.integrations.shared.event_bus import get_event_bus
        lm = chat.get("last_message") or {}
        get_event_bus().publish("outbound_message", {
            "conversation_id": str(chat.get("conversation_id") or ""),
            "platform": str(chat.get("platform") or ""),
            "account_id": str(chat.get("account_id") or ""),
            "chat_key": str(chat.get("chat_key") or ""),
            "name": str(chat.get("name") or ""),
            "chat_type": str(chat.get("chat_type") or "private"),
            "preview": str(chat.get("last_msg") or lm.get("text") or "")[:80],
            "direction": "out",
            "ts": float(lm.get("ts") or chat.get("last_ts") or time.time()),
        })
    except Exception:
        logger.debug("[protocol_bridge] outbound_message 事件发布失败", exc_info=True)


async def maybe_auto_reply(payload: Dict[str, Any]) -> None:
    """入站消息已落库后调用：若注册了 reply hook 且为入站，交由 hook 决定是否自动回复。

    best-effort：hook 内部异常不外泄，绝不影响入站落库主流程。
    """
    fn = _reply_hook
    if fn is None:
        return
    if (payload or {}).get("direction", "in") != "in":
        return
    try:
        res = fn(payload)
        if hasattr(res, "__await__"):
            await res
    except Exception:
        logger.debug("[protocol_bridge] auto-reply hook 失败", exc_info=True)


def ingest_incoming(
    store: Any,
    *,
    platform: str,
    account_id: str,
    chat_key: str,
    name: str = "",
    text: str = "",
    ts: float = 0,
    msg_id: str = "",
    direction: str = "in",
    source: Optional[Dict[str, Any]] = None,
    media_type: str = "",
    media_ref: str = "",
    chat_type: str = "",
    reply_to: Optional[Dict[str, Any]] = None,
    username: str = "",
    phone: str = "",
    avatar_url: str = "",
    mentioned: bool = False,
    mentions: Optional[Any] = None,
    sender_id: str = "",
    sender_name: str = "",
) -> Optional[str]:
    """把一条 protocol 消息落库到统一收件箱。返回 conversation_id（失败返回 None）。

    M6④：携带 ``media_type`` / ``media_ref``（已下载好的 /static URL 或本地路径）。
    媒体消息即使无文本也会落库为一条消息（会话预览用占位符如「[图片]」，但消息正文仍为空，
    不污染 auto-draft）。

    P4-2：``reply_to``={id,text,sender} 携带被引用消息摘要，落 messages.reply_to_*，
    供 thread 渲染引用条（缺省 None=普通消息）。

    身份画像：``username`` / ``phone`` / ``avatar_url`` 落库到会话身份列（列表/头部/
    客户信息面板显示真实昵称与头像）。号码补名收口于此——``name`` 空或就是裸 chat_key 时，
    用已同步的通讯录名（``protocol_contacts``）兜底，使**所有入站路径**（HTTP 桥 + 进程内
    sink）一致地把裸号码补成真人名，而非仅 HTTP 桥。
    """
    if store is None or not chat_key:
        return None
    platform = str(platform or "").lower()
    # 号码补名（收口到唯一落库入口，覆盖进程内 sink 与 HTTP 桥）：无来显名或来显名
    # 就是裸号码 → 用已同步通讯录名补齐（好友名单同步后即贴近官方客户端体验）。
    if not name or name == str(chat_key):
        try:
            _cn = store.get_protocol_contact_name(platform, str(account_id), str(chat_key))
            if _cn:
                name = _cn
        except Exception:
            logger.debug("[protocol_bridge] 号码补名失败", exc_info=True)
    src: Dict[str, Any] = dict(source or {})
    if isinstance(reply_to, dict) and (reply_to.get("id") or reply_to.get("text")):
        src["reply_to"] = {
            "id": str(reply_to.get("id") or ""),
            "text": str(reply_to.get("text") or ""),
            "sender": str(reply_to.get("sender") or ""),
        }
    # P4-11D：群提及明细 [{jid,number}] → 持久前 best-effort 补 name（通讯录名，缺则回落号码），
    # 落 messages.mentions_json，供气泡把 @号码 渲染成 @名字（离线/列表/引用皆可读）。
    if isinstance(mentions, (list, tuple)) and mentions:
        _ml = []
        for mm in mentions:
            if not isinstance(mm, dict):
                continue
            jid = str(mm.get("jid") or "")
            num = str(mm.get("number") or (jid.split("@")[0] if jid else "")).strip()
            nm = str(mm.get("name") or "").strip()
            if not nm and num:
                try:
                    nm = store.get_protocol_contact_name(platform, str(account_id), num) or ""
                except Exception:
                    nm = ""
            if jid or num:
                _ml.append({"jid": jid, "number": num, "name": nm or num})
        if _ml:
            src["mentions"] = _ml
    # P4-11E：群发言人结构化落库（替代把「发言人：」拼进正文）——消息正文保持干净，
    # 供气泡上方显示发言人名 + 稳定色。
    _sender_name = str(sender_name or "").strip()
    _sender_id = str(sender_id or "").strip()
    if _sender_id or _sender_name:
        src["sender_id"] = _sender_id
        src["sender_name"] = _sender_name
    if msg_id:
        src.setdefault("message_id", str(msg_id))
        if platform == "telegram":
            src.setdefault("id", str(msg_id))
        elif platform == "whatsapp":
            src.setdefault("wamid", str(msg_id))
    has_media = bool(media_type or media_ref)
    # P2 群聊：chat_type=group 让会话分流到「群组动态」（不进 SLA/自动回复/auto-draft）
    if chat_type:
        src.setdefault("chat_type", str(chat_type))
    chat = normalize_chat(
        platform=platform,
        platform_name=PLATFORM_DISPLAY.get(platform, platform.title()),
        account_id=str(account_id), account_label=str(account_id),
        chat_key=str(chat_key), name=name or str(chat_key),
        last_msg=text, last_ts=ts or 0,
        unread=1 if direction == "in" else 0, source=src,
        chat_type=str(chat_type or ""),
        username=str(username or ""), phone=str(phone or ""),
        avatar_url=str(avatar_url or ""),
    )
    if direction == "out":
        m = message_obj(text=text, ts=ts or 0, direction="out",
                        message_id=str(msg_id), source=src)
        chat["last_message"] = m
        chat["messages"] = [m] if (text or has_media) else []
        chat["unread"] = 0
    if has_media:
        lm = chat["last_message"]
        lm["media_type"] = str(media_type or "")
        lm["media_ref"] = str(media_ref or "")
        chat["messages"] = [lm]
        # 会话预览：空文本→占位；干净转写/配文→补形态标记「[语音] 转写」（P1-1）。
        # 消息正文保持原文（空则不喂 auto-draft），只动预览列。
        chat["last_msg"] = media_preview_text(text, media_type)
    # P4-11E：群入站会话列表预览前缀发言人名（「张三：早上好」，对齐官方群聊列表），
    # 只改**会话预览** last_msg，不动**消息正文**（气泡正文保持干净，发言人走结构化字段）。
    if direction == "in" and str(chat_type or "") == "group" and _sender_name:
        _base = str(chat.get("last_msg") or "")
        chat["last_msg"] = f"{_sender_name}：{_base}" if _base else _sender_name
    try:
        _n = ingest_collected_chats(store, [chat], publish_events=(direction == "in"))
        # P2-2（2026-07-23）：出站镜像也发 SSE 事件（独立类型 outbound_message，不复用
        # inbox_message——前端对后者会给非选中会话 unread+1，出站消息不该点未读）。
        # 条件 _n>0 =「真的新插入」：编排器镜像与 worker fromMe 回显同 msg_id 落同键，
        # 第二次 INSERT OR IGNORE 无行插入 → 不重复发事件。让打开会话的坐席在 AI
        # autosend/主动触达后即时看到气泡，选中会话轮询由 10s 放宽到 30s（见 P1-3）。
        if direction == "out" and _n > 0:
            _publish_outbound_event(chat)
    except Exception:
        logger.debug("[protocol_bridge] ingest_collected_chats 失败", exc_info=True)
    # P4-11B：入站群消息 @ 本账号 → 置会话「@我」未读旗标（best-effort，不阻断落库）
    if direction == "in" and mentioned:
        try:
            store.set_conversation_mentioned(str(chat["conversation_id"]), True)
        except Exception:
            logger.debug("[protocol_bridge] set_conversation_mentioned 失败", exc_info=True)
    return str(chat["conversation_id"])


def make_message(
    *, platform: str, account_id: str, chat_key: str, text: str,
    name: str = "", ts: float = 0, msg_id: str = "", direction: str = "in",
    media_type: str = "", media_ref: str = "",
    username: str = "", phone: str = "", avatar_url: str = "",
    source: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造给 ``emit_incoming`` 的标准消息 dict。

    ``source``：上游平台原始字段（如 LINE 的 ``{"chat_type": "group"|"room"|"user"}``），
    会透传给落库时的 ``infer_chat_type``，让群组/房间正确分流到「群组动态」而非 SLA 告警。

    ``username`` / ``phone`` / ``avatar_url``：peer 真实身份画像（缺省空）。经 sink
    ``ingest_incoming(store, **m)`` 落库到会话身份列，供列表/头部/客户信息面板显示真实
    昵称与头像，替代「一排数字 id」。均为纯加法可选键——旧调用方不传即空、行为不变。
    """
    msg: Dict[str, Any] = {
        "platform": platform, "account_id": account_id, "chat_key": chat_key,
        "name": name, "text": text, "ts": ts or time.time(),
        "msg_id": msg_id, "direction": direction,
        "media_type": media_type, "media_ref": media_ref,
        "username": username, "phone": phone, "avatar_url": avatar_url,
    }
    if source:
        msg["source"] = dict(source)
    return msg


_WA_PHONE_RE = re.compile(r"^\d{6,20}$")  # WhatsApp 私聊 chat_key 即 E.164 裸号（6~20 位）

# 「绝不可能是人名」的状态/导航文案黑名单（2026-08-14，修「好友名单显示 Active now」）。
# 覆盖：在线状态（Active now / Active 3m ago / 在线 / 刚刚活跃 / 3 分钟前活跃）、
# 输入中（typing… / 正在输入）、导航标签（Message requests / 消息请求）、未读标记、
# 列表头（Chats · 3 unread）。锚定整行（^…$）+ 词形收紧（active 后必须跟 now/时间量），
# 「Active Fitness Club」「Online Shop PH」这类真名不误伤。
# ⚠ 跨语言契约：与 services/messenger-web/server.js::DIRTY_NAME_RE **逐字一致**，
# 由 tests/test_peer_name_sanitizer.py 抽取比对 + 金标样本双向钉住；改任何一侧先跑门禁。
_DIRTY_NAME_PATTERN = (
    r"^(?:[·•]|回复？|是否跟进？|在线|在线状态|刚刚活跃|昨天活跃"
    r"|(?:\d+\s*(?:分钟|小时|天|周)前)?活跃|正在输入.*|对方正在输入.*|typing.*|online"
    r"|active(?:\s+(?:now|today|yesterday))?"
    r"|active\s+\d+\s*(?:m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|weeks?)?(?:\s+ago)?"
    r"|消息请求|message\s+requests?|未读消息.*|unread\s+messages?.*"
    r"|chats?\s*[·•].*|聊天\s*[·•].*)$"
)
_DIRTY_NAME_RE = re.compile(_DIRTY_NAME_PATTERN, re.IGNORECASE)


def sanitize_peer_name(name: Any) -> str:
    """清洗 peer 显示名：状态/导航/列表头文案不是名字，归一为空串。

    空串语义＝「诚实缺名」：调用方回落链（通讯录补名 → 裸 chat_key）接手，
    且 store 的 upsert CASE 护栏保证空串**绝不覆盖**库里已有真名——脏名从
    「覆盖真名」降级为「无操作」。纯函数，worker 侧（server.js DIRTY_NAME_RE）
    与服务端共用同一 pattern（防绕过：老版本 worker / 其他平台边车同样被兜住）。
    """
    s = str(name or "").strip()
    if not s or _DIRTY_NAME_RE.match(s):
        return ""
    return s


def enrich_ingest_identity(
    platform: str, chat_key: str, name: str = "",
    chat_type: str = "", contact_name: str = "",
) -> Dict[str, str]:
    """入站身份归一（跨平台，纯函数便于单测）——WhatsApp/Messenger 等经 HTTP ingest 的号共用。

    定 display_name / phone，并给出分类 ``outcome`` 供观测（量化各平台「一排数字」残留）：

    - **display_name**：来显名是真名（非空且 != chat_key）→ 用之（``named``）；否则用**已同步
      通讯录名**补齐（``backfilled``）；仍取不到 → 空串，调用方回落裸 ``chat_key``（``raw``＝用户
      最初抱怨的「一排数字」）。全程 no-clobber 友好——空串交给 store 的 CASE 护栏不覆盖已有真名。
    - **phone**：WhatsApp **私聊**的 ``chat_key`` 即 E.164 裸号 → 补进 ``phone``（资料面板可显
      号码，补齐 Telegram 之外平台的信息面板）；群聊 / 其他平台 → 空。

    入参 ``contact_name`` 由调用方（有 store 的路由）查好传入，保持本函数纯净、可离线单测。

    2026-08-14 起来显名/通讯录名都先过 ``sanitize_peer_name``：状态文案（"Active now"/
    "Active 3m ago"/"消息请求"…）被 DOM 侧误抓成名字时在此归零 → 走 backfilled/raw
    回落，脏名绝不落 display_name（fail-closed 服务端兜底，不依赖 worker 版本）。
    """
    platform = str(platform or "").strip().lower()
    chat_key = str(chat_key or "").strip()
    name = sanitize_peer_name(name)
    contact_name = sanitize_peer_name(contact_name)
    is_group = str(chat_type or "").strip().lower() == "group"
    if name and name != chat_key:
        display_name, outcome = name, "named"
    elif contact_name and contact_name != chat_key:
        display_name, outcome = contact_name, "backfilled"
    else:
        display_name, outcome = "", "raw"
    phone = chat_key if (platform == "whatsapp" and not is_group
                         and _WA_PHONE_RE.match(chat_key)) else ""
    return {"display_name": display_name, "phone": phone, "outcome": outcome}


def tg_peer_identity(peer: Any) -> Dict[str, str]:
    """从 pyrogram Chat/User（``message.chat``/``from_user``）抽取真实身份画像。

    返回 ``{"name", "username", "phone"}``（均字符串，缺省空）。仅用 getattr（无需导入
    pyrogram，单测可传 duck-typed 对象）。name 组装优先级：群/频道标题 → 名(first+last)
    → @username → 空（交由调用方回落裸 id）。修「私聊显示数字 id 而非真人昵称」。
    """
    if peer is None:
        return {"name": "", "username": "", "phone": ""}
    title = str(getattr(peer, "title", "") or "").strip()
    first = str(getattr(peer, "first_name", "") or "").strip()
    last = str(getattr(peer, "last_name", "") or "").strip()
    username = str(getattr(peer, "username", "") or "").strip().lstrip("@")
    phone = str(getattr(peer, "phone_number", "") or getattr(peer, "phone", "") or "").strip()
    full = (first + " " + last).strip()
    name = title or full or (("@" + username) if username else "")
    return {"name": name, "username": username, "phone": phone}


def tg_message_payload(
    message: Any, account_id: str, *, media_type: str = "", media_ref: str = "",
) -> Optional[Dict[str, Any]]:
    """把一条 pyrogram Message 归一为 ``emit_incoming`` 的消息 dict（仅用 getattr，无需导入 pyrogram）。

    实时消息处理器与历史回填共用，单测可传任意 duck-typed 对象。返回 None 表示无法解析。

    会话身份取自 ``message.chat``（私聊即对端本人，群/频道即群名）——组装 first+last 全名、
    ``@username``、电话，落库后列表/头部/客户信息面板显示真实昵称，替代裸 chat_id。
    """
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return None
    ident = tg_peer_identity(chat)
    name = ident["name"] or str(chat_id)
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    # Phase4：B 线贴纸/emoji 语义与 A 线同口径 → inbound_enrich 可解析 [表情] 块
    if getattr(message, "sticker", None) is not None and not str(text).strip():
        from src.integrations.tg_inbound_text import sticker_text_from_message
        text = sticker_text_from_message(message)
    elif str(text).strip():
        from src.integrations.tg_inbound_text import annotate_inbound_emoji
        text = annotate_inbound_emoji(str(text))
    date = getattr(message, "date", None)
    ts = date.timestamp() if hasattr(date, "timestamp") else 0
    # 2026-08-17 文件体验 P0/P0.1：文档原名 + 体积进 source（卡片显示「report.pdf ·
    # 1.2 MB」；下载端点用原名另存）。体积走 tg_media_file_size（document/video/
    # audio 同源），取不到就不写，前端不渲染尺寸行。
    _src: Optional[Dict[str, Any]] = None
    _doc = getattr(message, "document", None)
    _fn = str(getattr(_doc, "file_name", "") or "").strip() if _doc is not None else ""
    _sz = tg_media_file_size(message)
    if _fn or _sz:
        _src = {}
        if _fn:
            _src["file_name"] = _fn
        if _sz > 0:
            _src["file_size"] = _sz
    return make_message(
        platform="telegram", account_id=str(account_id), chat_key=str(chat_id),
        name=str(name), text=str(text), ts=ts,
        msg_id=str(getattr(message, "id", "") or ""),
        direction="out" if getattr(message, "outgoing", False) else "in",
        media_type=media_type, media_ref=media_ref,
        username=ident["username"], phone=ident["phone"],
        source=_src,
    )


async def backfill_telegram(
    client: Any, account_id: str, limit: int = 20,
    *, emit: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> int:
    """首连历史回填：拉取最近会话的末条消息推入收件箱，使新接入账号即有上下文。

    best-effort：任何异常都吞掉（不阻断 worker 启动）；返回成功推入的会话数。
    ``emit`` 可注入用于单测，默认走 ``emit_incoming``。
    """
    if client is None or limit <= 0:
        return 0
    sink = emit or emit_incoming
    n = 0
    try:
        async for dialog in client.get_dialogs(limit=limit):
            try:
                msg = getattr(dialog, "top_message", None)
                if msg is None:
                    continue
                payload = tg_message_payload(msg, account_id)
                if payload and str(payload.get("text") or "").strip():
                    sink(payload)
                    n += 1
            except Exception:
                logger.debug("[protocol_bridge] tg 回填单条失败", exc_info=True)
    except Exception:
        logger.debug("[protocol_bridge] tg 回填失败", exc_info=True)
    return n


# ── Telegram 聊天记录同步（对齐手机/官方客户端） ────────────────────────────────
#   Telegram 的云聊天历史存在其服务器上——用该账号的 session 拉 get_dialogs +
#   get_chat_history 得到的就是手机上看到的同一份数据（密聊除外，其为端到端加密、
#   不过云端）。与 backfill_telegram（仅每会话末条）不同，这里逐会话拉近期 N 条，
#   落库走 ingest_thread 直写 store：**不**触发 SSE / auto-draft / 自动回复，
#   避免把陈年历史当新消息洪泛处理。媒体不下载（防大流量/风控），以「[图片]」等
#   占位文本入库，保留上下文可读性。


def history_message_obj(payload: Optional[Dict[str, Any]], message: Any) -> Dict[str, Any]:
    """把 ``tg_message_payload`` 的产物归一为 thread message dict（历史同步用）。

    - 无文本但有媒体 → 用「[图片]」等占位文本（不下载媒体本体）；
    - 仍无文本（服务消息等）→ 返回 {}，调用方跳过；
    - ``source`` 带 ``id`` 让 ``extract_platform_msg_id`` 抽到 MTProto 消息 id，
      与实时路径产出同一去重主键——重复同步/实时推送不会落重复行。
    - **媒体行结构化**（2026-08-04）：有媒体时落 ``media_type``（本体仍不下载、
      ``media_ref`` 留空）——工作台按形态卡渲染并给出「拉取原件」入口
      （``/api/platforms/telegram/{acct}/fetch-media`` 按行回填），替代纯占位文字。
      占位文本保留（会话预览与旧口径一致；气泡层 ``_dropBarePlaceholder`` 会剥掉）。
    """
    text = str((payload or {}).get("text") or "")
    meta = tg_media_meta(message)
    if not text and meta:
        text = media_placeholder(meta[0])
    if not text:
        return {}
    mid = str((payload or {}).get("msg_id") or "")
    return message_obj(
        text=text, ts=(payload or {}).get("ts") or 0,
        direction=str((payload or {}).get("direction") or "in"),
        message_id=mid, source={"id": mid} if mid else {},
        media_type=(meta[0] if meta else ""),
    )


def tg_chat_dict(
    chat: Any, account_id: str, *,
    last_msg: str = "", last_ts: float = 0, unread: int = 0,
) -> Optional[Dict[str, Any]]:
    """把 pyrogram Chat 归一为收件箱 chat dict（历史同步/按需拉取共用）。

    身份取自 ``tg_peer_identity``；``chat.type``（pyrogram ChatType 枚举）经 source
    交由 ``infer_chat_type`` 归一（supergroup→group 等）。无 id → None。
    """
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return None
    ident = tg_peer_identity(chat)
    ctype = str(getattr(getattr(chat, "type", None), "value", "") or "").lower()
    return normalize_chat(
        platform="telegram",
        platform_name=PLATFORM_DISPLAY.get("telegram", "Telegram"),
        account_id=str(account_id), account_label=str(account_id),
        chat_key=str(chat_id), name=ident["name"] or str(chat_id),
        last_msg=last_msg, last_ts=last_ts or 0, unread=int(unread or 0),
        source={"chat_type": ctype} if ctype else None,
        username=ident["username"], phone=ident["phone"],
    )


async def collect_tg_dialog_history(
    client: Any, account_id: str, chat_key: str,
    *, limit: int = 50, offset_id: int = 0,
) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    """拉取单个 Telegram 会话的云端历史，归一为 ``(chat dict, [message dict])``。

    ``offset_id`` > 0 时只取比该消息 id 更早的（「加载更早」锚点语义，与 WhatsApp
    fetchMessageHistory 对齐）；0 = 从最新开始。拉不到任何可入库消息 → None。
    """
    if client is None or not chat_key:
        return None
    peer: Any = chat_key
    try:
        peer = int(chat_key)
    except (TypeError, ValueError):
        peer = chat_key
    kwargs: Dict[str, Any] = {"limit": max(1, int(limit or 50))}
    if offset_id:
        kwargs["offset_id"] = int(offset_id)
    msgs: List[Dict[str, Any]] = []
    chat_obj = None
    async for message in client.get_chat_history(peer, **kwargs):
        if chat_obj is None:
            chat_obj = getattr(message, "chat", None)
        payload = tg_message_payload(message, account_id)
        if payload is None:
            continue
        obj = history_message_obj(payload, message)
        if obj:
            msgs.append(obj)
    if chat_obj is None or not msgs:
        return None
    newest = max(msgs, key=lambda m: float(m.get("ts") or 0))
    chat = tg_chat_dict(
        chat_obj, account_id,
        last_msg=str(newest.get("text") or ""),
        last_ts=float(newest.get("ts") or 0),
    )
    if chat is None:
        return None
    return chat, msgs


# Telegram「会话已死」错误特征：这些 401 不是网络抖动，重试永远不会好，唯一出路是
# 重新登录（实锤：2026-08-11 坐席机主号被「终止所有会话」踢下线，同步按钮只报笼统
# 的「同步聊天记录失败」，坐席连点 4 次无从知道该去重新登录）。
# 刻意不含 USER_DEACTIVATED：它会与「对方账号注销」的 INPUT_USER_DEACTIVATED 撞子串，
# 且账号被注销/封禁不是「重新登录」能解决的，误提示比不提示更糟。
_TG_SESSION_DEAD_MARKS = (
    "SESSION_REVOKED", "SESSION_EXPIRED", "AUTH_KEY_UNREGISTERED",
    "AUTH_KEY_INVALID", "AUTH_KEY_DUPLICATED", "KEY IS NOT REGISTERED",
)


def tg_error_kind(text: Any) -> str:
    """把 Telegram RPC 异常文本归类成机器可读 kind（前端据此给可行动的提示）。

    当前只识别一类：``session_revoked``＝该账号登录会话已失效（被吊销/过期/在别处
    登出），需要重新登录。其余返回空串＝按普通错误处理。纯函数，供账号级历史同步/
    单会话深度回填/全量深同步三条链的 ``state=error`` 落状态时统一分类。
    """
    t = str(text or "").upper()
    if any(mark in t for mark in _TG_SESSION_DEAD_MARKS):
        return "session_revoked"
    return ""


async def sync_telegram_history(
    client: Any, account_id: str, *,
    dialogs_limit: int = 100, per_chat: int = 30,
    ingest: Optional[Callable[[Dict[str, Any], List[Dict[str, Any]]], Any]] = None,
    progress: Optional[Callable[[int, int, int], None]] = None,
    pace_sec: float = 0.35,
) -> Dict[str, int]:
    """账号级聊天记录同步：遍历云端会话列表、逐会话拉最近 ``per_chat`` 条落库。

    效果对齐「新设备登录官方客户端」：会话列表 + 未读数 + 近期历史与手机一致。

    - ``ingest(chat, msgs) -> int``：由调用方注入（生产为 ``ingest_thread(store,…)``
      直写 store——不触发 SSE/auto-draft/自动回复），返回新插入条数；
    - ``progress(done, total, messages)``：每处理完一个会话回调一次（供进度轮询）；
    - ``pace_sec``：会话间隔节流，叠加 pyrogram 自身的 FloodWait 自动等待，温和拉取；
    - 单会话失败只跳过该会话（best-effort），返回 ``{"dialogs": n, "messages": m}``
      —— n=成功处理的会话数，m=新插入消息总数。
    """
    stats = {"dialogs": 0, "messages": 0}
    if client is None or dialogs_limit <= 0 or ingest is None:
        return stats
    dialogs: List[Any] = []
    async for dialog in client.get_dialogs(limit=dialogs_limit):
        dialogs.append(dialog)
    total = len(dialogs)
    if progress is not None:
        try:
            progress(0, total, 0)
        except Exception:
            pass
    for idx, dialog in enumerate(dialogs):
        try:
            chat_obj = getattr(dialog, "chat", None)
            chat_id = getattr(chat_obj, "id", None)
            if chat_id is None:
                continue
            msgs: List[Dict[str, Any]] = []
            async for message in client.get_chat_history(chat_id, limit=per_chat):
                payload = tg_message_payload(message, account_id)
                if payload is None:
                    continue
                obj = history_message_obj(payload, message)
                if obj:
                    msgs.append(obj)
            last_msg, last_ts = "", 0.0
            if msgs:
                newest = max(msgs, key=lambda m: float(m.get("ts") or 0))
                last_msg = str(newest.get("text") or "")
                last_ts = float(newest.get("ts") or 0)
            else:
                # 近期无可入库消息的会话仍要在列表可见（与手机一致）：
                # 用 top_message 文本兜底做预览，占位入库
                top = getattr(dialog, "top_message", None)
                top_payload = tg_message_payload(top, account_id) if top is not None else None
                if top_payload is not None:
                    top_obj = history_message_obj(top_payload, top)
                    if top_obj:
                        last_msg = str(top_obj.get("text") or "")
                        last_ts = float(top_obj.get("ts") or 0)
            chat = tg_chat_dict(
                chat_obj, account_id, last_msg=last_msg, last_ts=last_ts,
                unread=int(getattr(dialog, "unread_messages_count", 0) or 0),
            )
            if chat is None:
                continue
            try:
                inserted = ingest(chat, msgs)
                stats["messages"] += int(inserted or 0)
            except Exception:
                logger.debug("[protocol_bridge] tg 历史同步 ingest 失败", exc_info=True)
            stats["dialogs"] += 1
        except Exception:
            logger.debug("[protocol_bridge] tg 历史同步单会话失败", exc_info=True)
        if progress is not None:
            try:
                progress(idx + 1, total, stats["messages"])
            except Exception:
                pass
        if pace_sec > 0:
            await asyncio.sleep(pace_sec)
    return stats


async def deep_backfill_tg_history(
    client: Any, account_id: str, chat_key: str, *,
    max_messages: int = 2000, offset_id: int = 0,
    ingest: Optional[Callable[[Dict[str, Any], List[Dict[str, Any]]], Any]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    batch_size: int = 200, pace_sec: float = 0.3,
) -> Dict[str, Any]:
    """单会话「深度回填」：从锚点（``offset_id``，0=最新）向更早方向连续拉取，
    直到攒够 ``max_messages`` 或云端到头。

    与 ``collect_tg_dialog_history``（单页、攒齐一次性返回）的区别：**流式分批落库**
    ——每 ``batch_size`` 条 ingest 一次并回调 ``progress(fetched, inserted)``，几千条
    的深回填中途崩溃时已落批次不重来；pyrogram 的 ``get_chat_history`` 生成器内部
    自带分页与 FloodWait 自动等待，无需手工翻页；批间 ``pace_sec`` 温和节流
    （与账号级同步同哲学）。

    返回 ``{"fetched": 云端拉到的原始条数, "inserted": 新插入条数,
    "exhausted": 云端是否已到头}``。fetched < max_messages 即到头（生成器提前收尾
    只有「没有更早了」一种含义）；服务消息等不可入库条目计入 fetched 不计入
    inserted，两数值差不代表丢失。
    """
    stats: Dict[str, Any] = {"fetched": 0, "inserted": 0, "exhausted": False}
    if client is None or not chat_key or int(max_messages or 0) <= 0 or ingest is None:
        return stats
    peer: Any = chat_key
    try:
        peer = int(chat_key)
    except (TypeError, ValueError):
        peer = chat_key
    kwargs: Dict[str, Any] = {"limit": int(max_messages)}
    if offset_id:
        kwargs["offset_id"] = int(offset_id)
    chat_obj: Any = None
    batch: List[Dict[str, Any]] = []

    def _flush() -> None:
        nonlocal batch
        if chat_obj is None or not batch:
            batch = []
            return
        newest = max(batch, key=lambda m: float(m.get("ts") or 0))
        chat = tg_chat_dict(
            chat_obj, account_id,
            last_msg=str(newest.get("text") or ""),
            last_ts=float(newest.get("ts") or 0),
        )
        if chat is not None:
            try:
                stats["inserted"] += int(ingest(chat, batch) or 0)
            except Exception:
                logger.debug("[protocol_bridge] 深度回填 ingest 批次失败（已跳过）",
                             exc_info=True)
        batch = []

    def _report() -> None:
        if progress is not None:
            try:
                progress(stats["fetched"], stats["inserted"])
            except Exception:
                pass

    async for message in client.get_chat_history(peer, **kwargs):
        stats["fetched"] += 1
        if chat_obj is None:
            chat_obj = getattr(message, "chat", None)
        payload = tg_message_payload(message, account_id)
        if payload is not None:
            obj = history_message_obj(payload, message)
            if obj:
                batch.append(obj)
        if len(batch) >= max(1, int(batch_size)):
            _flush()
            _report()
            if pace_sec > 0:
                await asyncio.sleep(pace_sec)
    _flush()
    stats["exhausted"] = stats["fetched"] < int(max_messages)
    _report()
    return stats
