"""LINE(okline 协议) 媒体收发 —— 把 LINE 号从「只能收发文字」补齐到与 TG/WA 同级。

背景（2026-07-31 实测缺口，两个后果都是**静默**的）：

1. **收不到**：``LineProtocolWorker`` 的入站 handler 只取 ``ctx.text`` → payload 无
   ``media_type``/``media_ref`` → ``protocol_autoreply`` 判 ``incomplete`` 早退，
   客户发图/语音/视频**AI 一声不响**（其余三平台都会下载后过 VLM/ASR 变成可喂 AI 的
   文本）。对陪伴类产品，客户发来的哭脸贴纸比一句「在吗」信息量更大。
2. **发不出**：编排器 ``owns_media()`` 的判据是 ``hasattr(worker, "send_media")``
   → LINE 恒 False → 自拍/相册/克隆语音/命理 K 线整条媒体链在 LINE 会话里
   「逻辑跑完、投递必失败」，最后静默回落纯文字。

本模块只做 **LINE 特有的那一层**（contentType 归一 + OBS 取字节/传字节）：下游识别
（``media_enrich``）与落盘（``protocol_bridge.media_paths``）全是现成的平台无关设施，
所以入站只要把 ``media_ref`` 填上，识图 / 语音转写 / 识别结果回写收件箱 / 多模态
prompt 字段就**全部白捡**，零下游改动。

两处刻意的设计取舍：

- **入站下载在 okline 接收线程内同步做**。它会阻塞该账号的 ops 长轮询派发，但上界由
  okline 自己的 ``transport.config.timeout``（30s）兜住，且 LINE 侧是单账号低频通道；
  换来的是「一条消息一次 emit」的简单语义（两段式 emit＋后补 media_ref 需要动 store
  更新与乱序处理，复杂度不划算）。真到高频再改两段式。
- **出站自己编排两步（占位消息 → OBS 上传字节），不直接用 okline 的 ``send_image``**。
  因为那条路径在「占位发成功、字节上传失败」时会给客户留一条**永久损坏的空图片**；
  我们在上传失败时撤回占位（``unsend_message``），宁可什么都没发，也不发一条坏消息。
  payload 构造仍复用 okline 的 ``Message`` 工厂，只接管编排。

⚠ **reqSeq 判重病理**（读是安全的，见 ``_sync_bootstrap_blocking``；发必须防）。
``OkLine`` 每次新建都把 ``_reqseq`` 归 0，而服务端去重键是 **(reqSeq, 消息内容)**
（2026-07-31 真机实测）：以撞历史的 reqSeq 发**同样**的媒体占位会被判重、返回
**旧** message id，随后 OBS 上传撞 HTTP 423 Locked，若按「上传失败」撤回那个 id
就**把之前已经成功的消息删掉**。最初以为只有一次性工具会踩（长驻 worker 单调递增），
2026-09-01 skuio 客户机实锤（backend.log 四笔 423）：**应用重启**后 worker 的新
client 同样从 0 起——媒体占位内容彼此相同，撞上上一轮进程的历史 reqSeq。
两层修复（2026-09-02）：

- ``bump_client_reqseq``：worker 启动即把 ``_reqseq`` 推到秒级时间基线的新鲜区间
  （跨重启单调递增；侧车文件持久化 floor 防时钟回拨），从源头不撞；
- ``send_line_media`` 的 423 路径：识别为判重碰撞 → **不撤回**（那个 id 多半是
  历史好消息）→ 换新鲜 reqSeq 重发占位、对新 id 再传一次字节。

离线工具若要发，同样先调 ``bump_client_reqseq``（见 tools/probe_line_media.py）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── LINE ContentType（镜像 okline.enums.ContentType，本模块的纯函数不依赖 okline
#    以便离线跑门禁；漂移由 tests/test_line_media.py 与真枚举对钉）────────────────
CT_NONE = 0
CT_IMAGE = 1
CT_VIDEO = 2
CT_AUDIO = 3
CT_STICKER = 7
CT_FILE = 14

#: LINE contentType → (系统媒体大类, 落地扩展名)。
#: 大类字符串**必须**落在 ``media_enrich`` 的 ``_IMAGE_KINDS`` / ``_VOICE_KINDS`` /
#: ``_VIDEO_KINDS`` 里，否则下载了也不会被识别（下载白做、AI 仍看不见）。
LINE_INBOUND_KINDS: Dict[int, Tuple[str, str]] = {
    CT_IMAGE: ("image", ".jpg"),
    CT_VIDEO: ("video", ".mp4"),
    CT_AUDIO: ("voice", ".m4a"),
    CT_STICKER: ("sticker", ".png"),
    CT_FILE: ("document", ".bin"),
}

#: 出站：系统媒体大类 → (LINE contentType, OBS obs_type, OBS cat)
LINE_OUTBOUND_KINDS: Dict[str, Tuple[int, str, Optional[str]]] = {
    "image": (CT_IMAGE, "image", "original"),
    "photo": (CT_IMAGE, "image", "original"),
    "sticker": (CT_IMAGE, "image", "original"),  # 自有图当普通图发（贴纸需商品 id）
    "voice": (CT_AUDIO, "audio", None),
    "audio": (CT_AUDIO, "audio", None),
    "video": (CT_VIDEO, "video", None),
    "document": (CT_FILE, "file", None),
    "file": (CT_FILE, "file", None),
}

_OBS_SERVICE = "talk"
_OBS_SID = "m"

#: 入站缺省 32MB（2026-09-05 J-3 C 真机：LINE 服务端把 100MB 源视频转码成 27.5MB 再下发，
#: 旧 20MB 会把这类转码件 ``too_large_actual`` 拒下＝「发得出、收不到」；出站已放到 100MB）。
#: 现场可用 ``platform_login.line.media.inbound_max_bytes`` 覆写。
DEFAULT_INBOUND_MAX_BYTES = 32 * 1024 * 1024
#: 出站缺省只在 ``src.inbox.media_limits`` 不可用时兜底（#169 起真值走那边，见
#: ``_outbound_default_bytes``）。保留常量是为了旧测试/旧调用方的 import 不断。
DEFAULT_OUTBOUND_MAX_BYTES = 20 * 1024 * 1024

#: #169 OBS 大文件上传：okline transport 缺省 timeout=30s 且是 **socket 级**（含写）。
#: 2026-09-05 117 号真机：30MB 18.5s 送达；60MB 单 POST 在 30s 写超时上撞
#: ``('Connection aborted.', TimeoutError('The write operation timed out'))`` ×3 次重试
#: ＝91s 后 obs_upload_failed。上传期按体积放宽：``base + size / OBS_UPLOAD_MIN_BPS``。
#: 256KB/s 是对坐席机上行带宽的保守下界（低于它上传本身就不可用）。
OBS_UPLOAD_TIMEOUT_BASE_SEC = 30.0
OBS_UPLOAD_MIN_BPS = 256 * 1024
OBS_UPLOAD_TIMEOUT_MAX_SEC = 900.0


def _outbound_default_bytes(config: Optional[Dict[str, Any]]) -> int:
    """LINE 出站体积缺省＝收件箱路由同源（``inbox.media.limits_mb.line`` → 内建 100MB）。"""
    try:
        from src.inbox.media_limits import platform_media_cap_bytes
        return int(platform_media_cap_bytes(config, "line"))
    except Exception:
        return DEFAULT_OUTBOUND_MAX_BYTES


def obs_upload_timeout_sec(size_bytes: int) -> float:
    """按体积算 OBS 单次上传的 socket 超时（秒），夹 [base, max]。纯函数。"""
    try:
        n = max(0, int(size_bytes or 0))
    except (TypeError, ValueError):
        n = 0
    t = OBS_UPLOAD_TIMEOUT_BASE_SEC + n / float(OBS_UPLOAD_MIN_BPS)
    return float(max(OBS_UPLOAD_TIMEOUT_BASE_SEC, min(OBS_UPLOAD_TIMEOUT_MAX_SEC, t)))

#: #172：OBS 404 ≠「已过期」。只有消息龄 ≥ 此值（小时）才许标 MISS_EXPIRED。
#: 默认 7 天——2026-09-05 真机取证（117 号，region PH）：手机发的图 98.7h、语音 98.7h、
#: 自发语音 162.6h 仍全部 200；LINE 自己给 FILE 消息标的 ``FILE_EXPIRE_TIMESTAMP``
#: 恰好 = 发送时刻 + 7 天。指令草稿里的 24h 占位值被这组数据直接证伪（24h 的对象
#: 还在服务端），故不采用。运营可按 ``platform_login.line.media.expired_after_hours`` 覆盖。
DEFAULT_EXPIRED_AFTER_HOURS = 24.0 * 7

#: 贴纸不在 OBS 消息对象里（消息只带 STKID/STKPKGID），图在公开贴纸商店 CDN。
#: 动图贴纸取到的是静帧——对 VLM「这人发了什么表情」足够。
_STICKER_URL = (
    "https://stickershop.line-scdn.net/stickershop/v1/sticker/{stkid}/android/sticker.png"
)


# ── 纯函数（无 IO、无 okline 依赖，可离线单测）────────────────────────────────────

def resolve_line_media_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """读 ``platform_login.line.media``，出齐全默认值（软失败，绝不抛）。

    两个开关的默认值刻意**不同**，因为它们的性质不同：

    - ``inbound`` 默认 **True**：那是在修一个缺陷（客户发的图/语音被静默丢弃），
      且全程只读（一次 OBS GET，官方客户端打开聊天时本就会做同样的请求）。
      最坏情况是下载失败 → 回落占位＝改动前的行为。
    - ``outbound`` 默认 **False**：这是新增能力，且开启即让 ``owns_media()`` 翻成
      True，**级联**打开自拍/相册/克隆语音/命理 K 线在 LINE 上的投递。逆向协议发
      媒体还有账号风险，必须由运营显式 opt-in。
    """
    line_cfg = ((config or {}).get("platform_login") or {}).get("line") or {}
    raw = line_cfg.get("media") if isinstance(line_cfg, dict) else None
    cfg = raw if isinstance(raw, dict) else {}

    def _flag(key: str, default: bool) -> bool:
        v = cfg.get(key, default)
        return default if v is None else bool(v)

    def _size(key: str, default: int) -> int:
        try:
            v = int(cfg.get(key) or 0)
        except (TypeError, ValueError):
            return default
        return v if v > 0 else default

    def _hours(key: str, default: float) -> float:
        try:
            v = float(cfg.get(key) or 0)
        except (TypeError, ValueError):
            return default
        return v if v > 0 else default

    return {
        "inbound": _flag("inbound", True),
        "outbound": _flag("outbound", False),
        "stickers": _flag("stickers", True),
        # 群聊媒体默认不下载：群与私聊共用同一条 okline 接收线程，热闹的群会把下载
        # 耗时叠到私聊 AI 回复的延迟上。关＝群消息维持改动前的「[图片]」占位行为。
        "groups": _flag("groups", False),
        "inbound_max_bytes": _size("inbound_max_bytes", DEFAULT_INBOUND_MAX_BYTES),
        # #169：出站上限缺省与收件箱 send-media 路由**同源**（inbox.media.limits_mb.line
        # → 内建 LINE 默认 100MB）。此前这里独立硬编码 20MB、比路由的 25 还低——路由
        # 放行的 20~25MB 文件到 worker 才被 media_too_large 拒掉。显式配
        # platform_login.line.media.outbound_max_bytes 仍最高优先。
        "outbound_max_bytes": _size("outbound_max_bytes", _outbound_default_bytes(config)),
        # #172：OBS 404 只有在消息龄 ≥ 此阈值时才许判「已过期」（见 classify_missing）
        "expired_after_hours": _hours("expired_after_hours", DEFAULT_EXPIRED_AFTER_HOURS),
    }


def line_media_meta(message: Optional[Dict[str, Any]]) -> Optional[Tuple[str, str]]:
    """LINE 消息 dict → ``(系统媒体大类, 扩展名)``；纯文本/未知类型返回 ``None``。

    ``FILE`` 类型尽量沿用原始文件名的扩展名（``contentMetadata.FILE_NAME``），
    这样落地文件仍是 ``.pdf`` / ``.xlsx``，坐席点开即认得。
    """
    if not isinstance(message, dict):
        return None
    try:
        ct = int(message.get("contentType") or 0)
    except (TypeError, ValueError):
        return None
    hit = LINE_INBOUND_KINDS.get(ct)
    if hit is None:
        return None
    kind, ext = hit
    if ct == CT_FILE:
        meta = message.get("contentMetadata")
        fn = str((meta or {}).get("FILE_NAME") or "") if isinstance(meta, dict) else ""
        real = os.path.splitext(fn)[1]
        if real and len(real) <= 12:
            ext = real
    return kind, ext


def line_declared_size(message: Optional[Dict[str, Any]]) -> int:
    """取消息自报的字节数（仅 ``FILE`` 带 ``FILE_SIZE``）；未知返回 0＝不拦截。"""
    if not isinstance(message, dict):
        return 0
    meta = message.get("contentMetadata")
    if not isinstance(meta, dict):
        return 0
    try:
        return max(0, int(meta.get("FILE_SIZE") or 0))
    except (TypeError, ValueError):
        return 0


def sticker_image_url(message: Optional[Dict[str, Any]]) -> str:
    """贴纸消息 → 贴纸商店静态图 URL；非贴纸/缺 STKID 返回空串。"""
    if not isinstance(message, dict):
        return ""
    try:
        if int(message.get("contentType") or 0) != CT_STICKER:
            return ""
    except (TypeError, ValueError):
        return ""
    meta = message.get("contentMetadata")
    stkid = str((meta or {}).get("STKID") or "").strip() if isinstance(meta, dict) else ""
    if not stkid or not stkid.isdigit():
        return ""
    return _STICKER_URL.format(stkid=stkid)


def sticker_text_hint(message: Optional[Dict[str, Any]]) -> str:
    """贴纸自带的文字提示（``STKTXT``，如「開心」）。

    贴纸图下不来时（CDN 变更/动图/网络）它就是唯一的语义线索——比裸「[贴纸]」
    强得多，且零成本零风险。
    """
    if not isinstance(message, dict):
        return ""
    meta = message.get("contentMetadata")
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("STKTXT") or "").strip()


def outbound_kind(media_type: str, ext: str = "") -> Optional[Tuple[int, str, Optional[str]]]:
    """出站媒体大类 → ``(contentType, obs_type, cat)``；认不出返回 ``None``。

    ``media_type`` 缺失/陌生时按扩展名兜底（复用 ``protocol_bridge`` 的同一套判定，
    避免两处对「.ogg 是语音还是文件」有不同看法）。
    """
    mt = str(media_type or "").strip().lower()
    hit = LINE_OUTBOUND_KINDS.get(mt)
    if hit is not None:
        return hit
    if ext:
        from src.integrations.protocol_bridge import media_type_from_ext
        return LINE_OUTBOUND_KINDS.get(media_type_from_ext(ext))
    return None


# ── IO（同步；okline 是 requests 同步库，调用方负责丢线程）───────────────────────

def _record_inbound(kind: str, *, ok: bool, skip_reason: str = "") -> None:
    """观测埋点（best-effort，绝不影响收发主链路）。"""
    try:
        from src.integrations.line_media_stats import get_line_media_stats
        get_line_media_stats().record_inbound(kind, ok=ok, skip_reason=skip_reason)
    except Exception:
        pass


def _record_outbound(kind: str, *, ok: bool, error: str = "") -> None:
    try:
        from src.integrations.line_media_stats import get_line_media_stats
        get_line_media_stats().record_outbound(kind, ok=ok, error=error)
    except Exception:
        pass


def _record_recall(*, recalled: bool) -> None:
    try:
        from src.integrations.line_media_stats import get_line_media_stats
        get_line_media_stats().record_orphan_recall(recalled=recalled)
    except Exception:
        pass


def _probe_media_flow(api: Any, chat_mid: str) -> str:
    """取该会话的 ``determineMediaMessageFlow``（只在失败路径调，软失败返回空）。

    形状（2026-07-31 真机）：``{"flowMap": {"1":2,"2":2,"3":2,"14":2},
    "cacheTtlMillis":"21600000"}``——键是 contentType，值是该类型该走的上传流程。
    """
    try:
        return str(api.determine_media_message_flow(chat_mid))[:200]
    except Exception:
        return ""


#: Opus 的 OGG granulepos 恒按 48kHz 计（RFC 7845 §4），与编码采样率无关
_OPUS_GRANULE_RATE = 48_000
#: OGG 尾页扫描窗：正常语音条的最后一页远小于此；防对超大文件全量搜索
_OGG_TAIL_SCAN_BYTES = 128 * 1024


def _ogg_opus_duration_ms(path: str) -> int:
    """OGG/Opus 时长（毫秒）——零依赖读容器：末页 granulepos ÷ 48kHz。

    只认 Opus（头部 4KB 内有 ``OpusHead``）：Vorbis 的 granule 单位是流采样率，
    还得再解析 ident 头，而本仓语音链全是 Opus——宁窄勿错。解析失败返回 0。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
            if b"OggS" != head[:4] or b"OpusHead" not in head:
                return 0
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _OGG_TAIL_SCAN_BYTES))
            tail = fh.read()
        at = tail.rfind(b"OggS")
        if at < 0 or at + 14 > len(tail):
            return 0
        granule = int.from_bytes(tail[at + 6:at + 14], "little", signed=True)
        if granule <= 0:
            return 0
        return int(granule * 1000 // _OPUS_GRANULE_RATE)
    except Exception:
        logger.debug("[line_media] OGG 纯解析失败 path=%s", path, exc_info=True)
        return 0


def _mp4_duration_ms(path: str) -> int:
    """MP4/M4A 时长（毫秒）——零依赖 box 走查：``moov→mvhd`` 的 duration/timescale。

    顶层顺序扫 box（支持 64 位 largesize），进 ``moov`` 后找 ``mvhd``（v0 32 位 /
    v1 64 位两种布局）。任何异常/形态不符返回 0。
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fsize = fh.tell()
            fh.seek(0)

            def _walk(start: int, end: int, depth: int = 0) -> int:
                pos = start
                while pos + 8 <= end and depth < 4:
                    fh.seek(pos)
                    hdr = fh.read(8)
                    if len(hdr) < 8:
                        return 0
                    box_size = int.from_bytes(hdr[:4], "big")
                    box_type = hdr[4:8]
                    payload_at = pos + 8
                    if box_size == 1:  # 64 位 largesize
                        big = fh.read(8)
                        if len(big) < 8:
                            return 0
                        box_size = int.from_bytes(big, "big")
                        payload_at = pos + 16
                    if box_size < 8 or pos + box_size > end + 8:
                        return 0
                    if box_type == b"moov":
                        got = _walk(payload_at, min(end, pos + box_size), depth + 1)
                        if got:
                            return got
                    elif box_type == b"mvhd":
                        body = fh.read(32)
                        if len(body) < 20:
                            return 0
                        version = body[0]
                        if version == 1:
                            timescale = int.from_bytes(body[20:24], "big")
                            duration = int.from_bytes(body[24:32], "big")
                        else:
                            timescale = int.from_bytes(body[12:16], "big")
                            duration = int.from_bytes(body[16:20], "big")
                        if timescale <= 0 or duration <= 0:
                            return 0
                        return int(duration * 1000 // timescale)
                    pos += box_size
                return 0

            return _walk(0, fsize)
    except Exception:
        logger.debug("[line_media] MP4 纯解析失败 path=%s", path, exc_info=True)
        return 0


def _pure_duration_ms(path: str) -> int:
    """零依赖时长兜底（#101 P2）：ffmpeg/ffprobe 彻底缺席时的最后一层。

    覆盖本仓语音链的两种真实容器：OGG/Opus（TTS 统一产出）与 M4A（LINE 侧
    转码产物/回取件）。解析失败返回 0＝维持「按 0 发」旧行为。
    """
    ext = os.path.splitext(str(path or ""))[1].lower()
    if ext in (".ogg", ".opus", ".oga"):
        return _ogg_opus_duration_ms(path)
    if ext in (".m4a", ".mp4", ".aac", ".mov"):
        return _mp4_duration_ms(path)
    return 0


def _probe_duration_ms(path: str) -> int:
    """音/视频时长（毫秒），软失败返回 0。

    LINE 的语音/视频条按 ``contentMetadata.DURATION`` 画时长与波形，给 0 会显示成
    「0:00」——像坏消息（#101 实锤：客户桌面包对方端全是 0:00）。三层：

    1. ``voice_sender.probe_audio_duration_ms``：经 ``ffmpeg_resolver`` 找 ffprobe
       ——客户包的 ffprobe 在 ``resources/ffmpeg/``、不在 PATH，此前这里走的
       ``media_probe`` 是裸 ``shutil.which``，打包态永远探不到＝0:00 的根因；
    2. ``media_probe.probe_video`` 兜底（同接 resolver；对纯音频同样出容器级时长）；
    3. 纯 Python 容器解析（``_pure_duration_ms``）——随包 ffmpeg 被杀毒软件隔离/
       损坏这类「彻底没有 ffprobe」的机器也能给出真时长，0:00 不再有死角。
    """
    try:
        from src.client.voice_sender import probe_audio_duration_ms
        ms = probe_audio_duration_ms(path)
        if ms and int(ms) > 0:
            return int(ms)
    except Exception:
        logger.debug("[line_media] probe_audio_duration_ms 异常（转 media_probe 兜底）",
                     exc_info=True)
    try:
        from src.companion.media_probe import probe_video
        info = probe_video(path) or {}
        ms = int(info.get("duration_ms") or 0)
        if ms > 0:
            return ms
    except Exception:
        logger.debug("[line_media] media_probe 探测异常（转纯解析兜底）", exc_info=True)
    return max(0, _pure_duration_ms(path))


_OBS_RETRY_SLEEP_SEC = 0.8
_OBS_REFRESH_COOLDOWN_SEC = 600.0
#: 这些 miss 原因是「链路坏了」而非配置/护栏选择——必须在 WARNING 级可见
#: （#101 教训：旧 debug 级日志在客户机 backend.log 里根本查不到，失败原因只能猜）。
_MISS_WARN_REASONS = frozenset({"download_error", "empty_body", "write_error"})


def _http_status(exc: BaseException) -> int:
    """从 requests.HTTPError 类异常里挖 HTTP 状态码；挖不到返回 0。"""
    resp = getattr(exc, "response", None)
    try:
        return int(getattr(resp, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


# ── reqSeq 抗判重（HTTP 423 病理，见模块头注释）──────────────────────────────────

#: reqSeq 时间基线锚点（2025-06 附近的固定秒戳）。floor = 当前秒 - 锚点：时间以
#: 1/秒前进、reqSeq 以 1/条消耗，LINE 单号频率远低于 1 条/秒，故「本轮用到的最大
#: reqSeq」永远追不上「下轮启动时的时间基线」——跨重启天然单调不重叠。锚点让值域
#: 从千万级起步，thrift i32 上限（2^31）还够用约 60 年。
_REQSEQ_EPOCH = 1_750_000_000
#: 时钟回拨时的兜底前进量：floor 至少比已知值（持久化/当前）大这么多。
_REQSEQ_MIN_STEP = 1_000


def compute_reqseq_floor(current: int = 0, now: Optional[float] = None) -> int:
    """算「保证不与历史重叠」的 reqSeq 起点：max(时间基线, 已知值 + 兜底步长)。"""
    ts = time.time() if now is None else now
    try:
        cur = int(current or 0)
    except (TypeError, ValueError):
        cur = 0
    step_from_known = cur + _REQSEQ_MIN_STEP if cur > 0 else 1
    return max(int(ts) - _REQSEQ_EPOCH, step_from_known, 1)


def bump_client_reqseq(api: Any, *, account_id: str = "", floor_file: str = "") -> int:
    """把 client 的 ``_reqseq`` 推到跨重启单调递增的新鲜区间（软失败，返回新 floor 或 0）。

    worker 启动时必调（HTTP 423 病理的源头修复）：新建 client 的 reqSeq 从 0 起，
    媒体占位内容彼此相同，重启后必撞上一轮进程的历史 (reqSeq, 内容) 被服务端判重。
    ``floor_file``（约定 ``<tokens_path>.reqseq``）持久化本次 floor：正常情况下时间
    基线已单调，它只在**时钟回拨**时兜底；读写失败都不阻断（退化为纯时间基线）。
    """
    persisted = 0
    if floor_file:
        try:
            with open(floor_file, "r", encoding="utf-8") as fh:
                persisted = int(str(fh.read()).strip() or 0)
        except FileNotFoundError:
            persisted = 0
        except Exception:
            logger.debug("[line_media] reqSeq floor 读取失败 path=%s", floor_file,
                         exc_info=True)
    try:
        current = int(getattr(api, "_reqseq", 0) or 0)
    except (TypeError, ValueError):
        current = 0
    floor = compute_reqseq_floor(max(persisted, current))
    try:
        api._reqseq = floor  # noqa: SLF001 —— okline 无公开 setter，病理修复只能进内部
    except Exception:
        logger.warning("[line_media] reqSeq 推进失败（维持从 0 起，重启后首条媒体"
                       "可能撞判重）acct=%s", account_id, exc_info=True)
        return 0
    if floor_file:
        try:
            with open(floor_file, "w", encoding="utf-8") as fh:
                fh.write(str(floor))
        except Exception:
            logger.debug("[line_media] reqSeq floor 持久化失败 path=%s", floor_file,
                         exc_info=True)
    logger.info("[line_media] reqSeq floor 推进 acct=%s %d→%d (persisted=%d)",
                account_id, current, floor, persisted)
    return floor


def _is_obs_locked(exc: BaseException) -> bool:
    """这次失败是不是 HTTP 423 Locked（reqSeq 判重碰撞的指纹）。

    okline 的 OBS 上传抛 ``LineApiError``（带 ``status`` 属性、消息含
    ``"HTTP 423"``）；三路都认：``status`` 属性 / requests 的 ``response`` /
    消息文本，任一命中即是。
    """
    try:
        if int(getattr(exc, "status", 0) or 0) == 423:
            return True
    except (TypeError, ValueError):
        pass
    return _http_status(exc) == 423 or "HTTP 423" in str(exc)


def _try_refresh_token(api: Any) -> bool:
    """OBS 下载遇 401/403 时显式刷一次 access token（600s 冷却，绝不抛）。

    okline 只给 thrift 调用（``transport.post_json``）挂了 401 自动刷新钩子；
    OBS 下载走裸 ``_send`` GET **不在保护圈内**——token 陈旧时文字链自愈、媒体
    下载全灭，正是 #101「文字收得到、语音无音频存档」的结构洞。刷新复用
    ``line_pull_sync.refresh_client_token``（摘钩防递归 + 回写会话文件）；
    冷却防 refresh token 已死时每条媒体都白打一次 tokenRefresh。
    """
    now = time.monotonic()
    raw = getattr(api, "_lm_refresh_ts", None)
    try:
        last = None if raw is None else float(raw)
    except (TypeError, ValueError):
        last = None
    # 属性缺失 ≠ 时间戳 0。monotonic 从开机起算，新进程/新机器上经常 < 600；
    # 旧写法 getattr(..., 0.0) 把「从没刷过」当成「刚刚刷新过」，启动后 10 分钟内
    # 401 自愈被静默跳过（文字链靠 okline 钩子自愈、OBS 裸 GET 不在圈内，#101）。
    if last is not None and (now - last) < _OBS_REFRESH_COOLDOWN_SEC:
        return False
    try:
        api._lm_refresh_ts = now  # noqa: SLF001 —— 按 client 记冷却戳（duck-typed）
    except Exception:
        pass
    try:
        from src.integrations.line_pull_sync import refresh_client_token
        ok = bool(refresh_client_token(api))
        if ok:
            logger.info("[line_media] OBS 下载 401 → access token 已刷新，重试下载")
        return ok
    except Exception:
        logger.debug("[line_media] token 刷新异常", exc_info=True)
        return False


def _obs_download_with_retry(
    api: Any, msg_id: str, *, sid: str = _OBS_SID, oid_path: str = "",
    talk_meta: str = "",
) -> Tuple[bytes, str, int]:
    """OBS 对象下载，至多两次尝试；返回 ``(字节, 最后失败摘要, 最后 HTTP 状态)``。

    第三个返回值是 A2（2026-09-03）加的：``404``＝对象在该路径下不存在。#172 起
    **不再**由本函数判「已过期」——404 只是一个 HTTP 状态，是否终局交
    ``classify_missing``（消息龄 + ``object_info.obs`` 服务端状态）裁决。

    ``sid`` / ``oid_path`` / ``talk_meta``（#172，2026-09-05）：镜像 LINE Chrome
    客户端 ``yb()`` 的对象定位——Letter Sealing 媒体存在 ``talk/em/<OID>``（不是
    ``talk/m/<消息id>``）且要带 ``X-Talk-Meta`` 头；原图质量的图在 ``<id>/original``。
    ``oid_path`` 缺省＝消息 id（旧行为，存量调用方/门禁不变）。

    真机取证（#101）：同一对象几分钟内两败两成——瞬态失败 × 零重试就是
    「无音频存档」的日常来源，一次重试即可吃掉大部分瞬态。401/403 先刷
    token 再立刻重试（见 ``_try_refresh_token``）；其余失败（含 200 空体）
    短退避后重试一次。上界＝2 次 GET + 至多 1 次 tokenRefresh，单次仍由
    okline transport 的 30s 超时兜底。
    """
    last = ""
    status = 0
    oid = oid_path or msg_id
    kwargs: Dict[str, Any] = {}
    if talk_meta:
        kwargs["talk_meta"] = talk_meta
    for attempt in (1, 2):
        try:
            data = api.obs.download_object(_OBS_SERVICE, sid, oid, **kwargs) or b""
            if data:
                return data, "", 0
            last, status = "empty_body", 0
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {str(exc)[:120]}"
            status = _http_status(exc)
            logger.debug("[line_media] OBS 下载失败 attempt=%d id=%s path=%s/%s",
                         attempt, msg_id, sid, oid, exc_info=True)
            if status == 404:
                break   # 该路径下没有对象：换路径/判定交调用方，重试同路径没有意义
            if attempt == 1 and status in (401, 403):
                if _try_refresh_token(api):
                    continue  # 刷新成功 → 立刻重试，不吃退避
                break  # 刷新失败/冷却中：再试大概率仍 401，别白打
            if 400 <= status < 500:
                break   # 其余 4xx（如 em 路径缺 X-Talk-Meta 的 400）是请求形态错，重打无益
        if attempt == 1:
            time.sleep(_OBS_RETRY_SLEEP_SEC)
    return b"", last, status


# ── #172：Letter Sealing 媒体 + 对象定位 + 404 分档（2026-09-05）────────────────
#
# 事故：skuio 机（88MP86 / 3U298U）Kevin(钧) 发来的图与视频在 4 分钟内就被本模块
# 判成「已过期（OBS 404）」，而同会话的语音、以及 117 号收 skuio 发的图/语音全部正常。
# 读 okline 随包的 LINE Chrome 客户端源码（``okline/ltsm/ltsmSandbox.js``，line-chrome
# 3.7.2）后定层——是**对象定位**与**加密**两层都缺，不是保留期：
#
# * ``yb()``：消息 ``contentMetadata.e2eeVersion`` 非空（Letter Sealing 媒体）时，
#   对象在 ``/r/talk/<SID=contentMetadata.SID or "m">/<OID=contentMetadata.OID or id>``
#   （e2ee-next 用 ``SID="em"``），可带 ``?p=<OBS_POP>``；预览＝``<OID>__ud-preview``。
#   非加密消息：``/r/talk/m/<id>``，原图质量（``MEDIA_CONTENT_INFO.category=="original"``）
#   在 ``<id>/original``，预览在 ``<id>/preview``。我们一律打 ``talk/m/<id>`` → 404。
# * 下载加密对象要带 ``X-Talk-Meta``（``SE(id)``：thrift 二进制 Message{4:id, 27:[]}
#   → base64 → ``{"message":…}`` → base64）。
# * 对象字节是密文：密钥材料 ``keyMaterial`` 在**消息 chunks 解密后的 JSON** 里
#   （Chrome 端解密后塞进 ``contentMetadata.ENC_KM``），HKDF-SHA256(salt=∅,
#   info="FileEncryption") 派生 encKey(32)+macKey(32)+nonce(12)；末 32 字节是
#   HMAC-SHA256 标签（视频按 128KB 分块 SHA-256 拼接后再 HMAC）；正文 AES-256-CTR，
#   counter = nonce || 0x00000000。okline 的 ``decrypt_message`` 只回填 ``text``，
#   把 ``keyMaterial`` 丢掉了——本模块自己走一遍解密拿它。
#
# 117 号收 skuio 的图/语音为什么好：那些消息**没有** ``e2eeVersion``（skuio 手机对
# 媒体没开 Letter Sealing），走的正是 ``talk/m/<id>``；钧的手机对媒体开了。

#: 入站媒体 miss 原因（``out["reason"]``）。终局只有 MISS_EXPIRED；其余都允许重试。
MISS_EXPIRED = "expired"          # 龄 ≥ 阈值且服务端说对象不存在：让客户重发
MISS_NOT_FOUND = "not_found"      # 404 但龄很小/未知：拉取失败，可重试（#172 的默认档）
MISS_PENDING = "pending"          # object_info: uploading / incompleted（对方还没传完）
MISS_ENCODING = "encoding"        # object_info: encodeStatus=ing（服务端转码中，视频/音频）
MISS_E2EE_NO_KEY = "e2ee_no_key"  # 加密媒体但本设备无法解出 keyMaterial（无 E2EE 密钥）
MISS_E2EE_DECRYPT = "e2ee_decrypt_failed"  # 拿到密文但 HMAC/解密失败

#: 允许坐席/回填链重试的 miss 档（与 MISS_EXPIRED 互斥）
RETRYABLE_MISS = frozenset({
    MISS_NOT_FOUND, MISS_PENDING, MISS_ENCODING, MISS_E2EE_DECRYPT,
    "download_error", "empty_body",
})

#: 加密媒体按解密明文 ``fileName`` 采信扩展名的白名单（按大类）
_E2EE_MEDIA_EXTS: Dict[str, Tuple[str, ...]] = {
    "image": (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"),
    "video": (".mp4", ".mov", ".m4v", ".webm", ".3gp"),
    "voice": (".m4a", ".aac", ".mp4", ".ogg", ".opus", ".oga", ".mp3", ".wav"),
}

_MEDIA_KM_INFO = b"FileEncryption"
_MEDIA_KM_OKM_LEN = 76          # 32 encKey + 32 macKey + 12 nonce
_MEDIA_HMAC_LEN = 32
_MEDIA_VIDEO_CHUNK = 131072     # 视频 HMAC 按 128KB 分块哈希（Chrome ``bv``）


class E2EEMediaError(Exception):
    """Letter Sealing 媒体解密失败（HMAC 不匹配 / 密钥材料坏 / 缺依赖）。"""


def line_message_age_sec(message: Optional[Dict[str, Any]],
                         now: Optional[float] = None) -> float:
    """消息龄（秒）：``createdTime``（LINE 给毫秒）→ 现在。缺失/非法返回 ``-1``。"""
    try:
        created = int(str((message or {}).get("createdTime") or "0").strip() or 0)
    except (TypeError, ValueError):
        return -1.0
    if created <= 0:
        return -1.0
    created_s = created / 1000.0 if created > 100_000_000_000 else float(created)
    ts = time.time() if now is None else float(now)
    return max(0.0, ts - created_s)


def classify_missing(age_sec: float, expired_after_hours: float, *,
                     obj_status: str = "", encode_status: str = "") -> str:
    """404/对象不存在 → miss 档位（纯函数）。

    - ``object_info`` 说 ``uploading``/``incompleted`` → MISS_PENDING；
      ``encodeStatus=="ing"`` → MISS_ENCODING（对方视频还在转码，稍后就有）。
    - 其余：只有 **龄已知且 ≥ 阈值** 才是 MISS_EXPIRED；龄未知（-1）一律 NOT_FOUND
      ——没有证据就不许对坐席说「LINE 已回收」。
    """
    st = str(obj_status or "").strip().lower()
    if st in ("uploading", "incompleted"):
        return MISS_PENDING
    if str(encode_status or "").strip().lower() == "ing":
        return MISS_ENCODING
    try:
        thr_h = float(expired_after_hours)
    except (TypeError, ValueError):
        thr_h = DEFAULT_EXPIRED_AFTER_HOURS
    if thr_h <= 0:
        thr_h = DEFAULT_EXPIRED_AFTER_HOURS
    if age_sec is not None and age_sec >= 0 and age_sec >= thr_h * 3600.0:
        return MISS_EXPIRED
    return MISS_NOT_FOUND


def is_e2ee_media(message: Optional[Dict[str, Any]]) -> bool:
    """Chrome ``dv()``：``contentMetadata.e2eeVersion`` 非空＝Letter Sealing 媒体。"""
    if not isinstance(message, dict):
        return False
    meta = message.get("contentMetadata")
    return bool(isinstance(meta, dict) and str(meta.get("e2eeVersion") or "").strip())


def obs_object_locator(message: Optional[Dict[str, Any]], *,
                       preview: bool = False) -> Tuple[str, str]:
    """Chrome ``yb()``：消息 → ``(sid, oid_path)``，拼成 ``/r/talk/<sid>/<oid_path>``。

    ``oid_path`` 已含 tid（``/original`` / ``/preview``）与 ``?p=<OBS_POP>``——
    okline 的 ``download_object`` 只按 f-string 拼 URL，直接把它当 oid 传即可。
    """
    msg = message if isinstance(message, dict) else {}
    msg_id = str(msg.get("id") or "")
    meta = msg.get("contentMetadata") if isinstance(msg.get("contentMetadata"), dict) else {}
    pop = str(meta.get("OBS_POP") or "").strip()
    suffix = f"?p={pop}" if pop else ""
    if is_e2ee_media(msg):
        sid = str(meta.get("SID") or "").strip() or _OBS_SID
        oid = str(meta.get("OID") or "").strip() or msg_id
        if preview:
            oid = f"{oid}__ud-preview"
        return sid, f"{oid}{suffix}"
    tid = ""
    if preview:
        tid = "preview"
    else:
        info = meta.get("MEDIA_CONTENT_INFO")
        if isinstance(info, str):
            try:
                import json as _json
                info = _json.loads(info)
            except Exception:  # noqa: BLE001
                info = None
        if isinstance(info, dict) and str(info.get("category") or "") == "original":
            tid = "original"
    return _OBS_SID, f"{msg_id}{'/' + tid if tid else ''}{suffix}"


def obs_object_candidates(message: Optional[Dict[str, Any]]) -> list:
    """按 Chrome 口径的主路径 + 兜底路径列表（去重，主路径在前）。

    兜底：加密消息若 ``SID/OID`` 缺省则与 ``talk/m/<id>`` 同路径；带 ``/original``
    的原图消息回落到不带 tid 的基路径（服务端是否同时保留标清版未知，多试一次
    只是一个 GET）。
    """
    msg = message if isinstance(message, dict) else {}
    msg_id = str(msg.get("id") or "")
    primary = obs_object_locator(msg)
    cands = [primary]
    base = (_OBS_SID, msg_id)
    if msg_id and base not in cands:
        cands.append(base)
    return cands


def build_talk_meta(message_id: str) -> str:
    """Chrome ``SE(id)``：下载加密对象要带的 ``X-Talk-Meta`` 头。

    thrift 二进制 ``Message`` 只写两个字段：``4:id``（STRING）与 ``27``（空 LIST
    of STRUCT），STOP 结尾 → base64 → ``{"message": <b64>}`` → 再 base64。
    """
    import base64 as _b64
    import json as _json
    raw = str(message_id or "").encode("utf-8")
    buf = (
        b"\x0b" + (4).to_bytes(2, "big") + len(raw).to_bytes(4, "big") + raw
        + b"\x0f" + (27).to_bytes(2, "big") + b"\x0c" + (0).to_bytes(4, "big")
        + b"\x00"
    )
    inner = _b64.b64encode(buf).decode("ascii")
    return _b64.b64encode(
        _json.dumps({"message": inner}, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def e2ee_media_material(api: Any, message: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """解开 Letter Sealing 媒体消息的 chunks → ``{"keyMaterial", "fileName"}``。

    okline 的 ``E2EEManager.decrypt`` 把明文 JSON 只取 ``text``/``location``，媒体
    的 ``keyMaterial``/``fileName`` 被丢掉——这里复用它的信道/解密原语（V1/V2、
    1:1/群）自己走一遍，拿全量明文。任何失败返回 ``{}``（调用方记 e2ee_no_key）。
    """
    msg = message if isinstance(message, dict) else {}
    chunks = msg.get("chunks") or []
    if not isinstance(chunks, list) or len(chunks) < 3:
        return {}
    e2ee = getattr(api, "e2ee", None)
    if e2ee is None:
        return {}
    try:
        if not e2ee.is_ready():
            return {}
    except Exception:  # noqa: BLE001
        return {}
    try:
        import base64 as _b64
        from okline import e2ee_crypto as fr
        version = fr.message_e2ee_version(msg)
        parse = fr.parse_chunks_v1 if version == 1 else fr.parse_chunks
        ciphertext, skid, rkid = parse(chunks)
        sender = str(msg.get("from") or "")
        to = str(msg.get("to") or "")
        if e2ee._is_group(msg):  # noqa: SLF001 —— okline 无公开的「解出全量明文」入口
            gk_handle, _gkid = e2ee._group_key_handle(to, rkid or None)  # noqa: SLF001
            channel = e2ee._bridge.e2ee_create_channel_with_pubkey(  # noqa: SLF001
                gk_handle, e2ee._user_pub(sender, skid or 0))  # noqa: SLF001
        else:
            channel = e2ee._channel_for_receive(sender, skid or 0, rkid or 0)  # noqa: SLF001
        ct_b64 = _b64.b64encode(ciphertext).decode("ascii")
        if version == 1:
            pt_b64 = e2ee._bridge.e2ee_decrypt_v1(channel, ciphertext_b64=ct_b64)  # noqa: SLF001
        else:
            pt_b64 = e2ee._bridge.e2ee_decrypt_v2(  # noqa: SLF001
                channel, to=to, frm=sender, sender_key_id=skid or 0,
                receiver_key_id=rkid or 0,
                content_type=int(msg.get("contentType", 0) or 0),
                ciphertext_b64=ct_b64)
        plain = fr.deserialize_plaintext(_b64.b64decode(pt_b64))
    except Exception:  # noqa: BLE001
        logger.debug("[line_media] E2EE 媒体 keyMaterial 解密失败 id=%s",
                     msg.get("id"), exc_info=True)
        return {}
    if not isinstance(plain, dict):
        return {}
    out: Dict[str, str] = {}
    km = str(plain.get("keyMaterial") or "").strip()
    if km:
        out["keyMaterial"] = km
    fn = str(plain.get("fileName") or "").strip()
    if fn:
        out["fileName"] = fn
    return out


def _hkdf_sha256(ikm: bytes, info: bytes, length: int, salt: bytes = b"") -> bytes:
    import hashlib
    import hmac as _hmac
    prk = _hmac.new(salt or b"\x00" * 32, ikm, hashlib.sha256).digest()
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = _hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def derive_media_keys(key_material_b64: str) -> Tuple[bytes, bytes, bytes]:
    """Chrome ``mv()``：keyMaterial(b64) → ``(encKey32, macKey32, nonce12)``。"""
    import base64 as _b64
    try:
        ikm = _b64.b64decode(str(key_material_b64 or "").strip() + "==")
    except Exception as exc:  # noqa: BLE001
        raise E2EEMediaError(f"bad_key_material: {exc}") from exc
    if not ikm:
        raise E2EEMediaError("empty_key_material")
    okm = _hkdf_sha256(ikm, _MEDIA_KM_INFO, _MEDIA_KM_OKM_LEN)
    return okm[:32], okm[32:64], okm[64:76]


def _media_hmac_input(body: bytes, *, is_video: bool) -> bytes:
    """Chrome ``vv()``：视频对正文按 128KB 分块 SHA-256 后拼接再 HMAC；其余直接正文。"""
    if not is_video:
        return body
    import hashlib
    digests = []
    for at in range(0, len(body), _MEDIA_VIDEO_CHUNK):
        digests.append(hashlib.sha256(body[at:at + _MEDIA_VIDEO_CHUNK]).digest())
    return b"".join(digests)


def encrypt_e2ee_media(plain: bytes, key_material_b64: str, *, is_video: bool = False) -> bytes:
    """Chrome ``gv()`` 的逆向（门禁/探针自证用）：AES-CTR 加密 + 追加 HMAC 标签。"""
    enc_key, mac_key, nonce = derive_media_keys(key_material_b64)
    import hmac as _hmac
    import hashlib
    body = _aes_ctr(enc_key, nonce + b"\x00\x00\x00\x00", bytes(plain))
    tag = _hmac.new(mac_key, _media_hmac_input(body, is_video=is_video),
                    hashlib.sha256).digest()
    return body + tag


def decrypt_e2ee_media(data: bytes, key_material_b64: str, *, is_video: bool = False) -> bytes:
    """Chrome ``vv()``：验 HMAC（末 32 字节）→ AES-256-CTR 解正文。不匹配即抛。"""
    import hmac as _hmac
    import hashlib
    blob = bytes(data or b"")
    if len(blob) <= _MEDIA_HMAC_LEN:
        raise E2EEMediaError("ciphertext_too_short")
    enc_key, mac_key, nonce = derive_media_keys(key_material_b64)
    body, tag = blob[:-_MEDIA_HMAC_LEN], blob[-_MEDIA_HMAC_LEN:]
    expect = _hmac.new(mac_key, _media_hmac_input(body, is_video=is_video),
                       hashlib.sha256).digest()
    if not _hmac.compare_digest(expect, tag):
        raise E2EEMediaError("hmac_mismatch")
    return _aes_ctr(enc_key, nonce + b"\x00\x00\x00\x00", body)


def _aes_ctr(key: bytes, counter16: bytes, data: bytes) -> bytes:
    """AES-CTR（加解密同一运算）。优先 ``cryptography``（okline 硬依赖），回落 pycryptodome。"""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        c = Cipher(algorithms.AES(key), modes.CTR(counter16)).encryptor()
        return c.update(data) + c.finalize()
    except ImportError:
        pass
    try:
        from Crypto.Cipher import AES  # type: ignore
        from Crypto.Util import Counter  # type: ignore
        ctr = Counter.new(128, initial_value=int.from_bytes(counter16, "big"))
        return AES.new(key, AES.MODE_CTR, counter=ctr).encrypt(data)
    except ImportError as exc:
        raise E2EEMediaError("no_aes_backend") from exc


def obs_object_info(api: Any, message: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Chrome ``mE()``：``GET /r/talk/<sid>/<oid>/object_info.obs`` → 服务端对象状态。

    返回 ``{"status": exist|notexist|uploading|incompleted|"", "encodeStatus": …,
    "http_status": int}``；任何失败返回空 status（调用方回落纯龄判定）。只在下载
    失败路径调（多一次轻量 GET），它是「404 到底是没有还是还没好」的唯一服务端真相。
    """
    out: Dict[str, Any] = {"status": "", "encodeStatus": "", "http_status": 0}
    obs = getattr(api, "obs", None)
    fn = getattr(obs, "object_info", None)
    if fn is None:
        return out
    msg = message if isinstance(message, dict) else {}
    sid, oid_path = obs_object_locator(msg)
    oid_only = oid_path.split("?", 1)[0].split("/", 1)[0]
    path = f"/r/{_OBS_SERVICE}/{sid}/{oid_only}"
    try:
        kw: Dict[str, Any] = {}
        if is_e2ee_media(msg):
            kw["talk_meta"] = build_talk_meta(str(msg.get("id") or ""))
        res = fn(path, **kw)
    except Exception as exc:  # noqa: BLE001
        out["http_status"] = _http_status(exc)
        logger.debug("[line_media] object_info 失败 path=%s", path, exc_info=True)
        return out
    if isinstance(res, dict):
        out["status"] = str(res.get("status") or "").strip().lower()
        out["encodeStatus"] = str(res.get("encodeStatus") or "").strip().lower()
        out["http_status"] = 200
    return out


#: 「已过期」镜像占位文案（按大类给人话）。与贴纸的 ``[表情] …`` 占位同一形态：
#: 落库文本必须自解释——坐席看会话时不该只看到一个点不开的 [图片]。
_EXPIRED_TEXT = {
    "image": "[图片] 该图片已过期（LINE 服务器不再保留），如需查看请让客户重发",
    "video": "[视频] 该视频已过期（LINE 服务器不再保留），如需查看请让客户重发",
    "voice": "[语音] 该语音已过期（LINE 服务器不再保留），如需收听请让客户重发",
    "audio": "[音频] 该音频已过期（LINE 服务器不再保留），如需收听请让客户重发",
    "document": "[文件] 该文件已过期（LINE 服务器不再保留），如需查看请让客户重发",
}

_KIND_LABEL = {"image": "图片", "video": "视频", "voice": "语音", "audio": "音频",
               "document": "文件", "sticker": "贴纸"}

#: 可重试 miss 的镜像占位文案（#172）：说清「拉取失败、可以重试」，不许说「已过期」。
_MISS_TEXT_TEMPLATES = {
    MISS_NOT_FOUND: "[{label}] 拉取失败（LINE 服务端 404，消息龄 {age}），点击重试",
    MISS_PENDING: "[{label}] 对方的{label}还在上传中，稍后点击重试",
    MISS_ENCODING: "[{label}] 对方的{label}仍在 LINE 服务端处理（转码）中，稍后点击重试",
    MISS_E2EE_NO_KEY: "[{label}] 加密{label}（Letter Sealing），本设备暂无解密密钥，请在手机上查看",
    MISS_E2EE_DECRYPT: "[{label}] 加密{label}解密失败，点击重试",
}


def expired_media_text(kind: str) -> str:
    """入站媒体已过期的镜像占位文案；未知大类给通用兜底（绝不返回空串）。"""
    k = str(kind or "").strip().lower()
    return _EXPIRED_TEXT.get(
        k, "[媒体] 该文件已过期（LINE 服务器不再保留），如需查看请让客户重发")


def _age_phrase(age_sec: float) -> str:
    if age_sec is None or age_sec < 0:
        return "未知"
    if age_sec < 3600:
        return f"{max(1, int(age_sec // 60))} 分钟"
    if age_sec < 48 * 3600:
        return f"{age_sec / 3600:.1f} 小时"
    return f"{age_sec / 86400:.1f} 天"


def inbound_miss_text(kind: str, out: Optional[Dict[str, Any]]) -> str:
    """按 ``download_line_media`` 的 ``out`` 出参给镜像占位文案；无需改文案的 miss 返回空串。

    只有**对坐席有处置意义**的档才出话：已过期（让客户重发）/ 可重试（点击重试）/
    加密无钥（去手机看）。开关关、超限、瞬态 download_error 保持原「[图片]」占位。
    """
    o = out if isinstance(out, dict) else {}
    reason = str(o.get("reason") or "")
    k = str(kind or "").strip().lower()
    if reason == MISS_EXPIRED or o.get("expired"):
        return expired_media_text(k)
    tpl = _MISS_TEXT_TEMPLATES.get(reason)
    if not tpl:
        return ""
    label = _KIND_LABEL.get(k, "媒体")
    return tpl.format(label=label, age=_age_phrase(float(o.get("age_sec", -1) or -1)))


def _download_sticker(url: str, timeout: float = 10.0) -> bytes:
    """公开贴纸 CDN 取静态图（无需鉴权）。失败返回空 bytes。"""
    try:
        import requests
        resp = requests.get(url, timeout=timeout)
        if resp.status_code >= 400:
            logger.debug("[line_media] 贴纸下载 HTTP %s url=%s", resp.status_code, url)
            return b""
        return resp.content or b""
    except Exception:
        logger.debug("[line_media] 贴纸下载失败 url=%s", url, exc_info=True)
        return b""


def download_line_media(
    api: Any, message: Optional[Dict[str, Any]], account_id: str, *,
    cfg: Optional[Dict[str, Any]] = None,
    out: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """下载 LINE 入站媒体到 static 目录，返回 ``(媒体大类, /static URL)``。

    与 ``protocol_bridge.download_tg_media`` 同契约（便于两条链对读）：

    - 非媒体消息 → ``('', '')``
    - 媒体但取不到字节（超限/下载失败/开关关） → ``(kind, '')``——**保留类型**让调用方
      落「[图片]」占位（即改动前的行为），只是没有识别素材
    - 成功 → ``(kind, '/static/protocol_media/line/...')``

    ``out``（A2，2026-09-03）：可选出参 dict，回填
    ``{"reason", "detail", "http_status", "expired", "retryable", "age_sec",
    "e2ee", "obs_status", "path"}``——调用方据此把**终局**的过期渲染成「已过期」、
    把可重试的 miss 渲染成「拉取失败，点击重试」（``inbound_miss_text``）。返回值
    形状刻意不变（与 ``download_tg_media`` 同契约，且存量调用方/门禁按二元组断言）。

    #172（2026-09-05）三处改动，见模块内「Letter Sealing 媒体 + 对象定位」注释：
    ① 对象定位按 Chrome ``yb()``（加密媒体走 ``talk/<SID>/<OID>`` + ``X-Talk-Meta``，
    原图走 ``<id>/original``），主路径 404 再试兜底路径；② 加密媒体解出 keyMaterial
    后 HMAC 校验 + AES-CTR 解密，解不出钥匙记 ``e2ee_no_key`` 不下载密文白占磁盘；
    ③ 404 不再直接判「已过期」：先问 ``object_info.obs``（uploading/转码中→可重试），
    再按消息龄 vs ``expired_after_hours`` 分 NOT_FOUND（可重试）/EXPIRED（终局）。

    全程软失败，绝不抛——入站落库主流程不能被一次下载失败带走。
    """
    if out is not None:
        out.clear()
        out.update({"reason": "", "detail": "", "http_status": 0,
                    "expired": False, "retryable": False, "age_sec": -1.0,
                    "e2ee": False, "obs_status": "", "path": ""})
    meta = line_media_meta(message)
    if meta is None:
        return "", ""
    kind, ext = meta
    msg_id = str((message or {}).get("id") or "")
    age_sec = line_message_age_sec(message)
    e2ee = is_e2ee_media(message) and kind != "sticker"
    if out is not None:
        out.update({"age_sec": age_sec, "e2ee": e2ee})

    def _miss(reason: str, detail: str = "", status: int = 0,
              obs_status: str = "") -> Tuple[str, str]:
        _record_inbound(kind, ok=False, skip_reason=reason)
        if out is not None:
            out.update({"reason": reason, "detail": detail,
                        "http_status": int(status or 0),
                        "expired": reason == MISS_EXPIRED,
                        "retryable": reason in RETRYABLE_MISS,
                        "obs_status": obs_status})
        if reason in ("disabled", "stickers_disabled"):
            # 运营配置选择，不是故障——保持安静
            logger.debug("[line_media] 入站媒体按开关跳过 kind=%s id=%s reason=%s",
                         kind, msg_id, reason)
        elif reason == MISS_EXPIRED:
            # A2：终局，且**有可执行处置**（让客户重发）——必须 WARNING 且话说清楚，
            # 别让值守把它当成又一次「下载失败，回头重试」（3U298U 实锤两条 404）
            logger.warning(
                "[line_media] 入站媒体已过期（OBS 404 且消息龄 %s ≥ 阈值，对象已被 LINE"
                " 回收，重试/回填都救不回）kind=%s id=%s acct=%s obs_status=%s"
                " → 工作台标「已过期」提示让客户重发",
                _age_phrase(age_sec), kind, msg_id, account_id, obs_status or "-")
        elif reason in (MISS_NOT_FOUND, MISS_PENDING, MISS_ENCODING,
                        MISS_E2EE_NO_KEY, MISS_E2EE_DECRYPT):
            # #172：可重试档必须把 kind/id/status/age 一行写清（重试点击也走这里）
            logger.warning(
                "[line_media] 入站媒体未取到（可重试）kind=%s id=%s acct=%s reason=%s"
                " http=%s age=%s e2ee=%s obs_status=%s%s",
                kind, msg_id, account_id, reason, status or "-", _age_phrase(age_sec),
                e2ee, obs_status or "-", f" err={detail}" if detail else "")
        else:
            # 链路失败必须 INFO/WARNING 级可见（#101：debug 级＝客户机上无法归因）
            log = logger.warning if reason in _MISS_WARN_REASONS else logger.info
            log("[line_media] 入站媒体未取到 kind=%s id=%s acct=%s reason=%s%s",
                kind, msg_id, account_id, reason,
                f" err={detail}" if detail else "")
        return kind, ""

    mcfg = cfg if isinstance(cfg, dict) else resolve_line_media_cfg(None)
    if not mcfg.get("inbound", True):
        return _miss("disabled")

    max_bytes = int(mcfg.get("inbound_max_bytes") or DEFAULT_INBOUND_MAX_BYTES)
    declared = line_declared_size(message)
    if declared and declared > max_bytes:
        return _miss("too_large_declared", f"size={declared} max={max_bytes}")

    data = b""
    fail_detail = ""
    fail_status = 0
    key_material = ""
    if kind == "sticker":
        if not mcfg.get("stickers", True):
            return _miss("stickers_disabled")
        url = sticker_image_url(message)
        if not url:
            return _miss("no_sticker_id")
        try:
            data = _download_sticker(url)
        except Exception:  # noqa: BLE001 —— _download_sticker 自身软失败，此处纯保险
            logger.debug("[line_media] 贴纸下载异常 id=%s", msg_id, exc_info=True)
            data = b""
    else:
        if not msg_id:
            return _miss("no_message_id")
        talk_meta = ""
        if e2ee:
            material = e2ee_media_material(api, message)
            key_material = material.get("keyMaterial", "")
            if not key_material:
                # 没钥匙下载密文毫无意义（还白占磁盘）；Chrome 端同样只在有 keyMaterial
                # 时才带 X-Talk-Meta 去取加密对象。
                return _miss(MISS_E2EE_NO_KEY, "no keyMaterial (e2ee not ready or "
                             "chunks undecryptable)")
            talk_meta = build_talk_meta(msg_id)
            # 加密媒体的真实文件名只在解密明文里（服务端元数据不带）：文件类沿用原扩展名
            # （坐席点开即认得），音/视频只在扩展名是已知容器时采信（官方客户端发的是
            # m4a/mp4，但 e2ee 流程原样上传，OGG 原件就还是 OGG——扩展名错会让播放器不认）
            real = os.path.splitext(material.get("fileName", ""))[1].lower()
            if real and len(real) <= 12 and (
                    kind == "document" or real in _E2EE_MEDIA_EXTS.get(kind, ())):
                ext = real
        try:
            for sid, oid_path in obs_object_candidates(message):
                if out is not None:
                    out["path"] = f"/r/{_OBS_SERVICE}/{sid}/{oid_path}"
                data, fail_detail, fail_status = _obs_download_with_retry(
                    api, msg_id, sid=sid, oid_path=oid_path, talk_meta=talk_meta)
                if data or fail_status != 404:
                    break
        except Exception as exc:  # noqa: BLE001 —— 纯保险：helper 自身不应抛
            return _miss("download_error", f"{type(exc).__name__}: {str(exc)[:120]}")

    if not data:
        if fail_status == 404:
            # #172：404 ≠ 已过期。先问服务端对象状态（上传中/转码中都不是「没了」），
            # 再按消息龄 vs 阈值分 NOT_FOUND（可重试）/EXPIRED（终局）。
            info = obs_object_info(api, message)
            reason = classify_missing(
                age_sec, float(mcfg.get("expired_after_hours")
                               or DEFAULT_EXPIRED_AFTER_HOURS),
                obj_status=info.get("status", ""),
                encode_status=info.get("encodeStatus", ""))
            return _miss(reason, fail_detail, 404, obs_status=info.get("status", ""))
        if fail_detail and fail_detail != "empty_body":
            return _miss("download_error", fail_detail, fail_status)
        return _miss("empty_body", "", fail_status)

    if key_material:
        try:
            data = decrypt_e2ee_media(data, key_material, is_video=(kind == "video"))
        except E2EEMediaError as exc:
            return _miss(MISS_E2EE_DECRYPT, str(exc), 200)
        except Exception as exc:  # noqa: BLE001
            return _miss(MISS_E2EE_DECRYPT, f"{type(exc).__name__}: {str(exc)[:120]}", 200)
        if not data:
            return _miss(MISS_E2EE_DECRYPT, "empty_plaintext", 200)

    # 自报体积不可信/缺失时的兜底：真实字节到手才知道大小，超限即丢（护磁盘）
    if len(data) > max_bytes:
        return _miss("too_large_actual", f"size={len(data)} max={max_bytes}")

    try:
        from src.integrations.protocol_bridge import media_paths
        name = f"{account_id}_{msg_id or secrets.token_hex(6)}"
        dest, url = media_paths("line", name, ext)
        with open(dest, "wb") as fh:
            fh.write(data)
        _record_inbound(kind, ok=True)
        if e2ee:
            logger.info("[line_media] Letter Sealing 媒体已解密落盘 kind=%s id=%s acct=%s"
                        " bytes=%d path=%s", kind, msg_id, account_id, len(data),
                        (out or {}).get("path") if out is not None else "-")
        return kind, url
    except Exception as exc:  # noqa: BLE001
        return _miss("write_error", f"{type(exc).__name__}: {str(exc)[:120]}")


#: 已是 AAC 家族容器（LINE 语音条原生格式）——无需转码
_LINE_AUDIO_READY_EXTS = frozenset({".m4a", ".aac", ".mp4"})


def _convert_audio_for_line(path: str) -> str:
    """出站音频 → AAC/M4A 临时文件；不需要/失败返回空串＝按原格式直传（旧行为）。

    全平台语音统一产 OGG/Opus（TG/WA 的语音条格式）。真机取证（#101）：LINE
    服务端会把上传的 OGG 转码成 M4A 再分发（download 回吐 ``ftypisom``）——
    能用但属未文档化行为；显式转 M4A 上传把「对方端能不能播」变成确定性，
    且时长探测对象与实发文件永远一致。ffmpeg 走 resolver（客户包
    ``resources/ffmpeg/`` 可达）。**调用方负责删除返回的临时文件。**
    """
    ext = os.path.splitext(str(path or ""))[1].lower()
    if ext in _LINE_AUDIO_READY_EXTS:
        return ""
    try:
        from src.utils.ffmpeg_resolver import ffmpeg_path
        ff = ffmpeg_path()
    except Exception:
        ff = None
    if not ff:
        return ""
    import subprocess
    import tempfile
    dst = os.path.join(tempfile.gettempdir(),
                       f"line_voice_{secrets.token_hex(6)}.m4a")
    try:
        r = subprocess.run(
            [ff, "-y", "-v", "error", "-i", str(path), "-vn",
             "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", dst],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0:
            return dst
        logger.debug("[line_media] 音频转 M4A 失败 rc=%s（按原格式发）：%s",
                     getattr(r, "returncode", "?"),
                     (getattr(r, "stderr", "") or "")[:200])
    except Exception:
        logger.debug("[line_media] 音频转 M4A 异常（按原格式发）", exc_info=True)
    try:
        os.remove(dst)
    except Exception:
        pass
    return ""


@contextlib.contextmanager
def _obs_upload_timeout(api: Any, size_bytes: int):
    """上传期把 okline transport 的 socket 超时按体积放宽，退出恢复原值。

    ``ObsClient.upload_message_object`` 不接 timeout 形参、透传的是
    ``transport.config.timeout``（LineConfig 是可变 dataclass），只能在这一段临时改。
    调用方在 worker 的 ``_api_call`` 锁内（整段占位→上传→配文互斥），同 transport 的
    receiver 长轮询自带显式 ``long_poll_timeout``，不受影响。任何取不到/改不了的
    形状（测试假 api、okline 升级换字段）一律静默不放宽＝旧行为。
    """
    cfgobj = None
    old = None
    try:
        cfgobj = getattr(getattr(getattr(api, "obs", None), "_t", None), "config", None)
        if cfgobj is None:
            cfgobj = getattr(getattr(api, "transport", None), "config", None)
        old = getattr(cfgobj, "timeout", None)
    except Exception:
        cfgobj, old = None, None
    want = obs_upload_timeout_sec(size_bytes)
    bumped = False
    if cfgobj is not None and isinstance(old, (int, float)) and want > float(old):
        try:
            cfgobj.timeout = want
            bumped = True
        except Exception:
            bumped = False
    try:
        yield want if bumped else old
    finally:
        if bumped:
            try:
                cfgobj.timeout = old
            except Exception:
                pass


def send_line_media(
    api: Any, to: str, *, media_path: str, media_type: str = "", caption: str = "",
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """发送 LINE 媒体（两步：占位消息 → OBS 上传字节），返回编排器契约的 dict。

    返回 ``{"delivered": bool, "message_id": str}``，失败时带 ``"error"``。

    **上传失败即撤回占位**：LINE 的媒体消息是「先发一条 contentType=IMAGE 的空壳、
    再把字节传到 ``/r/talk/m/<msgId>``」，两步之间失败会给客户留一条永久点不开的
    破图。撤回后 ``delivered=False``，调用方按既有失败路径回落文字——宁可不发，
    不发坏消息。

    ``caption`` 作**独立文本消息**在媒体之后补发（LINE 媒体消息没有 caption 字段）；
    caption 失败不影响 ``delivered``（图已经到了，这是锦上添花）。
    """
    mcfg = cfg if isinstance(cfg, dict) else resolve_line_media_cfg(None)
    path = str(media_path or "")
    kind_label = str(media_type or "").strip().lower() or "unknown"

    def _fail(error: str, **extra: Any) -> Dict[str, Any]:
        _record_outbound(kind_label, ok=False, error=error)
        return {"delivered": False, "error": error, **extra}

    if not path or not os.path.isfile(path):
        return _fail("media_missing")

    ext = os.path.splitext(path)[1]
    hit = outbound_kind(media_type, ext)
    if hit is None:
        return _fail("unsupported_media_type")
    content_type, obs_type, cat = hit

    # 音频显式转 M4A（#101 P1-2）：转成即用转码产物（字节/文件名/时长三者同源），
    # 转不成按原格式直传＝旧行为。临时产物本函数负责清理。
    tmp_m4a = _convert_audio_for_line(path) if content_type == CT_AUDIO else ""
    if tmp_m4a:
        path = tmp_m4a

    def _drop_tmp() -> None:
        if tmp_m4a:
            try:
                os.remove(tmp_m4a)
            except Exception:
                pass

    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except Exception:
        logger.debug("[line_media] 出站媒体读取失败 path=%s", path, exc_info=True)
        _drop_tmp()
        return _fail("media_unreadable")

    max_bytes = int(mcfg.get("outbound_max_bytes") or DEFAULT_OUTBOUND_MAX_BYTES)
    if len(data) > max_bytes:
        _drop_tmp()
        return _fail("media_too_large")

    name = os.path.basename(path) or "media.bin"
    duration_ms = (
        _probe_duration_ms(path) if content_type in (CT_AUDIO, CT_VIDEO) else 0
    )
    if content_type in (CT_AUDIO, CT_VIDEO) and duration_ms <= 0:
        # 到这一步还探不到时长＝ffprobe 彻底缺席：对方端将显示 0:00（#101 的
        # 用户可见形态）。发送不阻断（有声音总比没有强），但必须在日志可归因。
        logger.warning("[line_media] 出站媒体时长未探到（对方端将显示 0:00）"
                       " kind=%s path=%s", kind_label, name)
    _drop_tmp()

    from okline.enums import ContentType, EncryptedAccessTokenFeatureType
    from okline.models import Message

    if content_type == int(ContentType.IMAGE):
        placeholder = Message.image(to)
    elif content_type == int(ContentType.VIDEO):
        placeholder = Message.video(to, duration_ms)
    elif content_type == int(ContentType.AUDIO):
        placeholder = Message.audio(to, duration_ms)
    else:
        placeholder = Message.file(to, name, len(data))

    # 占位发送**必须包异常**：Letter Sealing(E2EE) 会话会在这一步被服务端拒（okline
    # 对媒体占位刻意不做 code 82 重加密重试）。让它抛出去等于把「这个会话发不了图」
    # 变成调用链上的异常，而不是一次可回落文字的普通失败。错误串进观测——
    # 它正是「要不要做 determineMediaMessageFlow 预检」的判据。
    try:
        sent = api.send_message(placeholder)
    except Exception as exc:
        # 顺手把该会话的 flowMap 记下来——这是**预检唯一缺的那块数据**。
        # 2026-07-31 真机实测：能发成功的会话 flowMap 是 {1:2,2:2,3:2,14:2}；
        # 但「发不了的会话长什么样」没有样本，所以刻意没有据此写预检（只有正样本
        # 就去猜哪些值该拒 = 可能拒发一切）。第一次真失败带回来的这行日志，就是
        # 把 determineMediaMessageFlow 做成事前预检的设计依据。
        flow = _probe_media_flow(api, to)
        logger.warning("[line_media] 媒体占位消息被拒 to=%s flow=%s: %s", to, flow, exc)
        return _fail("placeholder_rejected", detail=str(exc)[:200])
    msg_id = str(sent.get("id") or "") if isinstance(sent, dict) else ""
    if not msg_id:
        return _fail("no_message_id")

    dedup_retried = False
    while True:
        try:
            enc = api.get_encrypted_access_token(
                int(EncryptedAccessTokenFeatureType.OBS_GENERAL))
            with _obs_upload_timeout(api, len(data)):
                api.obs.upload_message_object(
                    msg_id, data, name=name, obs_type=obs_type, cat=cat, enc_token=enc,
                )
            break
        except Exception as exc:
            if _is_obs_locked(exc) and not dedup_retried:
                # HTTP 423 = reqSeq 判重碰撞的指纹（见模块头注释）：服务端把这次
                # 占位判成历史消息的重复、回了**旧** message id——那条多半是客户
                # 已收到的好消息，**绝不能**按「上传失败」去撤回它。处置：把
                # reqSeq 推到时间基线新鲜区间，重发占位拿真正的新 id，再传一次。
                dedup_retried = True
                try:
                    old_seq = int(getattr(api, "_reqseq", 0) or 0)
                except (TypeError, ValueError):
                    old_seq = 0
                fresh = compute_reqseq_floor(old_seq)
                try:
                    api._reqseq = fresh  # noqa: SLF001
                except Exception:
                    logger.debug("[line_media] 判重重试 reqSeq 推进失败", exc_info=True)
                logger.warning(
                    "[line_media] OBS 上传 HTTP 423（reqSeq 判重碰撞，msg=%s 疑为"
                    "历史消息，不撤回）reqSeq %d→%d 重发占位重试", msg_id, old_seq, fresh)
                try:
                    sent = api.send_message(placeholder)
                except Exception:
                    logger.warning("[line_media] 判重重试的占位重发被拒 to=%s",
                                   to, exc_info=True)
                    return _fail("obs_upload_locked", message_id=msg_id)
                new_id = str(sent.get("id") or "") if isinstance(sent, dict) else ""
                if not new_id or new_id == msg_id:
                    logger.warning("[line_media] 判重重试仍拿到同一 message id=%s"
                                   "（放弃，不撤回）", msg_id)
                    return _fail("obs_upload_locked", message_id=msg_id)
                msg_id = new_id
                continue
            # 占位已在对话里了 → 必须撤回，否则客户看到一条永久点不开的破图。
            # （判重重试后的占位持新鲜 reqSeq，id 必是新消息，撤回安全。）
            # 撤回本身失败也只能记日志（没有第二条退路），但 delivered 一定是 False。
            logger.warning("[line_media] OBS 上传失败，撤回占位 msg=%s: %s", msg_id, exc)
            try:
                api.unsend_message(msg_id)
                _record_recall(recalled=True)
            except Exception:
                _record_recall(recalled=False)
                logger.warning("[line_media] 占位撤回也失败，会话里可能残留破损媒体 msg=%s",
                               msg_id, exc_info=True)
            return _fail("obs_upload_failed", message_id=msg_id)

    cap = str(caption or "").strip()
    if cap:
        try:
            api.send_text(to, cap)
        except Exception:
            try:
                from src.integrations.line_media_stats import get_line_media_stats
                get_line_media_stats().record_caption_failed()
            except Exception:
                pass
            logger.debug("[line_media] 媒体配文补发失败（媒体已送达）", exc_info=True)

    _record_outbound(kind_label, ok=True)
    return {"delivered": True, "message_id": msg_id}
