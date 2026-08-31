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

⚠ **别用「一次性 client」发媒体**（读是安全的，见 ``_sync_bootstrap_blocking``）。
``OkLine`` 每次新建都把 ``_reqseq`` 归 0，而服务端去重键是 **(reqSeq, 消息内容)**
（2026-07-31 真机实测）：两个新 client 各自以 reqSeq=1 发**同样**的图片占位会被判重、
返回同一个 message id，随后 OBS 上传撞 HTTP 423 Locked，本模块便会按「上传失败」
撤回那个 id ——**把上一条已经成功的消息删掉**。长驻 worker 的 reqSeq 单调递增故不受
影响；离线工具若要发，先把 ``_reqseq`` 推到不重叠区间（见 tools/probe_line_media.py）。
"""

from __future__ import annotations

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

DEFAULT_INBOUND_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_OUTBOUND_MAX_BYTES = 20 * 1024 * 1024

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

    return {
        "inbound": _flag("inbound", True),
        "outbound": _flag("outbound", False),
        "stickers": _flag("stickers", True),
        # 群聊媒体默认不下载：群与私聊共用同一条 okline 接收线程，热闹的群会把下载
        # 耗时叠到私聊 AI 回复的延迟上。关＝群消息维持改动前的「[图片]」占位行为。
        "groups": _flag("groups", False),
        "inbound_max_bytes": _size("inbound_max_bytes", DEFAULT_INBOUND_MAX_BYTES),
        "outbound_max_bytes": _size("outbound_max_bytes", DEFAULT_OUTBOUND_MAX_BYTES),
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


def _probe_duration_ms(path: str) -> int:
    """音/视频时长（毫秒），软失败返回 0。

    LINE 的语音/视频条按 ``contentMetadata.DURATION`` 画时长与波形，给 0 会显示成
    「0:00」——像坏消息（#101 实锤：客户桌面包对方端全是 0:00）。首选
    ``voice_sender.probe_audio_duration_ms``：它经 ``ffmpeg_resolver`` 找 ffprobe
    ——客户包的 ffprobe 在 ``resources/ffmpeg/``、不在 PATH，此前这里走的
    ``media_probe`` 是裸 ``shutil.which``，打包态永远探不到＝0:00 的根因。
    ``media_probe.probe_video`` 兜底（现已同接 resolver，双保险；对纯音频同样
    能出容器级 duration）。仍探不到只能按 0 发，不阻断发送。
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
        return max(0, int(info.get("duration_ms") or 0))
    except Exception:
        logger.debug("[line_media] 时长探测失败（按 0 发）", exc_info=True)
        return 0


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


def _try_refresh_token(api: Any) -> bool:
    """OBS 下载遇 401/403 时显式刷一次 access token（600s 冷却，绝不抛）。

    okline 只给 thrift 调用（``transport.post_json``）挂了 401 自动刷新钩子；
    OBS 下载走裸 ``_send`` GET **不在保护圈内**——token 陈旧时文字链自愈、媒体
    下载全灭，正是 #101「文字收得到、语音无音频存档」的结构洞。刷新复用
    ``line_pull_sync.refresh_client_token``（摘钩防递归 + 回写会话文件）；
    冷却防 refresh token 已死时每条媒体都白打一次 tokenRefresh。
    """
    now = time.monotonic()
    try:
        last = float(getattr(api, "_lm_refresh_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        last = 0.0
    if (now - last) < _OBS_REFRESH_COOLDOWN_SEC:
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


def _obs_download_with_retry(api: Any, msg_id: str) -> Tuple[bytes, str]:
    """OBS 对象下载，至多两次尝试；返回 ``(字节, 最后失败摘要)``。

    真机取证（#101）：同一对象几分钟内两败两成——瞬态失败 × 零重试就是
    「无音频存档」的日常来源，一次重试即可吃掉大部分瞬态。401/403 先刷
    token 再立刻重试（见 ``_try_refresh_token``）；其余失败（含 200 空体）
    短退避后重试一次。上界＝2 次 GET + 至多 1 次 tokenRefresh，单次仍由
    okline transport 的 30s 超时兜底。
    """
    last = ""
    for attempt in (1, 2):
        try:
            data = api.obs.download_object(_OBS_SERVICE, _OBS_SID, msg_id) or b""
            if data:
                return data, ""
            last = "empty_body"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {str(exc)[:120]}"
            logger.debug("[line_media] OBS 下载失败 attempt=%d id=%s",
                         attempt, msg_id, exc_info=True)
            if attempt == 1 and _http_status(exc) in (401, 403):
                if _try_refresh_token(api):
                    continue  # 刷新成功 → 立刻重试，不吃退避
                break  # 刷新失败/冷却中：再试大概率仍 401，别白打
        if attempt == 1:
            time.sleep(_OBS_RETRY_SLEEP_SEC)
    return b"", last


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
) -> Tuple[str, str]:
    """下载 LINE 入站媒体到 static 目录，返回 ``(媒体大类, /static URL)``。

    与 ``protocol_bridge.download_tg_media`` 同契约（便于两条链对读）：

    - 非媒体消息 → ``('', '')``
    - 媒体但取不到字节（超限/下载失败/开关关） → ``(kind, '')``——**保留类型**让调用方
      落「[图片]」占位（即改动前的行为），只是没有识别素材
    - 成功 → ``(kind, '/static/protocol_media/line/...')``

    全程软失败，绝不抛——入站落库主流程不能被一次下载失败带走。
    """
    meta = line_media_meta(message)
    if meta is None:
        return "", ""
    kind, ext = meta
    msg_id = str((message or {}).get("id") or "")

    def _miss(reason: str, detail: str = "") -> Tuple[str, str]:
        _record_inbound(kind, ok=False, skip_reason=reason)
        if reason in ("disabled", "stickers_disabled"):
            # 运营配置选择，不是故障——保持安静
            logger.debug("[line_media] 入站媒体按开关跳过 kind=%s id=%s reason=%s",
                         kind, msg_id, reason)
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
        try:
            data, fail_detail = _obs_download_with_retry(api, msg_id)
        except Exception as exc:  # noqa: BLE001 —— 纯保险：helper 自身不应抛
            return _miss("download_error", f"{type(exc).__name__}: {str(exc)[:120]}")

    if not data:
        if fail_detail and fail_detail != "empty_body":
            return _miss("download_error", fail_detail)
        return _miss("empty_body")
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

    try:
        enc = api.get_encrypted_access_token(
            int(EncryptedAccessTokenFeatureType.OBS_GENERAL))
        api.obs.upload_message_object(
            msg_id, data, name=name, obs_type=obs_type, cat=cat, enc_token=enc,
        )
    except Exception as exc:
        # 占位已在对话里了 → 必须撤回，否则客户看到一条永久点不开的破图。
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
