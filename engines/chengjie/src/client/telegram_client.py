"""
Telegram MTProto客户端
基于pyrogram库的Telegram用户客户端实现
"""

import asyncio
import logging
import os
import random
import re
import time
from html import escape as _html_escape
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

from src.client import daily_stats
from src.inbox.store import (
    TELEGRAM_SERVICE_CHAT_KEYS as _STORE_SERVICE_CHAT_KEYS,
)

# 语音识别导入
try:
    from src.voice_transcriber import VoiceTranscriberFactory
    VOICE_RECOGNITION_AVAILABLE = True
except ImportError:
    VOICE_RECOGNITION_AVAILABLE = False
    VoiceTranscriberFactory = None

# 图片识别导入
try:
    from src.image_recognizer import ImageRecognizerFactory
    IMAGE_RECOGNITION_AVAILABLE = True
except ImportError:
    IMAGE_RECOGNITION_AVAILABLE = False
    ImageRecognizerFactory = None

# 图像理解（Vision）导入 - 智谱 GLM-4V 为主、OCR 兜底
try:
    from src.vision_client import VisionClient
    VISION_AVAILABLE = True
except ImportError:
    VISION_AVAILABLE = False
    VisionClient = None

# 尝试导入pyrogram，如果失败则使用模拟版本
try:
    from pyrogram import Client, filters
    from pyrogram.enums import ParseMode
    from pyrogram.types import Message, User
    from pyrogram.errors import (
        SessionPasswordNeeded, PhoneCodeInvalid,
        PhoneCodeExpired, FloodWait, Unauthorized
    )
    PYROGRAM_AVAILABLE = True
    # 2.0.106 冻结版兼容：channel id 边界放宽（超 int32 群 id，见 pyrogram_compat）。
    # A 线所有 client 从本模块出生 → import 时打补丁即全覆盖。
    from src.client.pyrogram_compat import ensure_wide_channel_ids
    ensure_wide_channel_ids()
except ImportError:
    PYROGRAM_AVAILABLE = False
    ParseMode = None  # type: ignore
    # 无 pyrogram（如 CI requirements-ci）时也保留模块级占位，确保 import-safe 且可被测试 patch
    Client = None  # type: ignore
    filters = None  # type: ignore
    # 创建模拟类型以便代码可以运行
    class Message:
        """模拟Message类"""
        def __init__(self):
            self.text = ""
            self.from_user = None
            self.chat = None
            self.id = 0

    class User:
        """模拟User类"""
        def __init__(self):
            self.id = 0
            self.username = ""
            self.first_name = ""

from src.utils.logger import LoggerMixin
from src.skills.skill_manager import SkillManager
from src.client.trigger import TelegramTriggerMixin
from src.client.sender import TelegramSenderMixin

# 上下文管理和情绪增强导入
try:
    from src.context.context_manager import ContextManager
    from src.skills.emotion_enhancer import EmotionEnhancer
    CONTEXT_AND_EMOTION_AVAILABLE = True
except ImportError:
    CONTEXT_AND_EMOTION_AVAILABLE = False
    ContextManager = None
    EmotionEnhancer = None

# 四层触发决策器导入
try:
    from src.trigger.four_layer_trigger import FourLayerTrigger
    FOUR_LAYER_TRIGGER_AVAILABLE = True
except ImportError:
    FOUR_LAYER_TRIGGER_AVAILABLE = False
    FourLayerTrigger = None

def _metrics():
    try:
        from src.monitoring.metrics_store import get_metrics_store
        return get_metrics_store()
    except Exception:
        return None


def _note_outbound_block(layer: str, reason: str) -> None:
    """P5 拦截统一计数（src/ops/outbound_policy）：本该回但被闸门吞掉记一笔。绝不抛。"""
    try:
        from src.ops.outbound_policy import record_block
        record_block(layer, reason, platform="telegram")
    except Exception:
        pass


# Telegram 官方通知号等「非人」固定 id。它们是货真价实的 PRIVATE 会话且
# outgoing=False，会一路穿过出站守卫落进 AI 管道——让 AI 对着 Telegram 官方回话既
# 荒唐又白烧 token。
# 名单本体在 ``src.inbox.store``（全仓单一事实源），这里只做一次 int 化：热路径每条
# 消息都要比对，用 int 集合免去反复 str 转换。store 只依赖 .models，无循环导入风险。
TELEGRAM_SERVICE_CHAT_IDS = frozenset(
    int(k) for k in _STORE_SERVICE_CHAT_KEYS if str(k).lstrip("-").isdigit()
)

# 兼容旧名（曾只有服务号一条规则时的常量名）。
TELEGRAM_SERVICE_CHAT_ID = 777000


def _inbound_mirror_text(media_type: str, text: str, raw_text: str) -> str:
    """坐席台镜像正文（显示口径，与 AI 层 text 刻意分离）。

    - 语音：``[语音转录] X`` → ``X``（[语音] 语义由 media_type 承载，P1-2 既有）。
    - 纯 emoji 文本（B29 实施49 2026-08-21）：``annotate_inbound_emoji`` 把纯 emoji
      消息整体替换成「[表情] 花痴」——那是给 AI 媒体块解析的注解，按该模块自己的
      教义（P0-198：加注不进存储正文）**不该**出现在坐席看到的气泡里。镜像行还原
      客户原始 emoji（``raw_text``=message.text）；贴纸不在此列（message.text 为空，
      media_type=sticker 走图渲染/占位），AI 层注解不受影响。
    """
    t = str(text or "")
    if media_type == "voice" and t.startswith("[语音转录] "):
        return t[len("[语音转录] "):]
    if not media_type and t.startswith("[表情]"):
        raw = str(raw_text or "").strip()
        if raw:
            return raw
    return t


def _normalize_message_text(raw: Any) -> str:
    """P2 编码防护：将消息文本统一为可安全处理的 str，避免 utf-16-le 等解码异常导致整次处理失败。"""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return raw.decode("latin-1", errors="replace")
    s = str(raw)
    try:
        # 去除 surrogate 与非法码点，避免下游编码报错
        return s.encode("utf-8", errors="replace").decode("utf-8")
    except Exception:
        return "".join(c if ord(c) < 0x10000 else "\ufffd" for c in s)


# 入站视频/GIF 体积上限默认值见 src.ai.inbound_video.DEFAULT_INBOUND_VIDEO_MAX_BYTES；
# 运行时读 telegram.inbound_video_max_bytes（ConfigManager 热重载后需重建 client 才生效）。


def _has_ingestable_media(message: Any) -> bool:
    """消息是否含可入站内容：文本/标题 + 语音/音频/图片/文件 + 视频/视频圆点/GIF/贴纸。

    注意：视频/视频圆点/GIF/贴纸历史上被入口过滤漏掉 → 对方发来的视频在入口即被丢弃、
    统一收件箱完全看不到。此处集中判定，供私聊/群聊实时 handler 与轮询兜底共用。
    """
    return bool(
        getattr(message, "text", None) or getattr(message, "caption", None)
        or getattr(message, "voice", None) or getattr(message, "audio", None)
        or getattr(message, "photo", None) or getattr(message, "document", None)
        or getattr(message, "video", None) or getattr(message, "video_note", None)
        or getattr(message, "animation", None) or getattr(message, "sticker", None)
    )


# 出站镜像媒体归档（mirror_outgoing_media）的内置保守默认：只自动拉「小体积、
# 高信息量」的类型——图片/贴纸/语音；video/document 常见大文件，默认不拉本体
# （media_type 仍结构化落库，坐席看到形态占位卡，原件走后续「按需拉取」补齐）。
_MIRROR_MEDIA_DEFAULT_KINDS = ("image", "sticker", "voice")
_MIRROR_MEDIA_DEFAULT_MAX_MB = 5.0
# 单条媒体下载的硬超时：镜像是旁路功能，绝不允许一条挂死的下载拖住实时 handler
# 或轮询循环；超时退化为「无归档的媒体行」（media_type 在、ref 空），消息不丢。
_MIRROR_MEDIA_DL_TIMEOUT_SEC = 30.0

# M-1 C #219：删除同步 RawUpdateHandler 的独立 group。pyrogram dispatcher 对每个 group
# 只跑第一个命中的 handler（命中即 break）；已读回执 handler 在 group 0 吃掉全部 raw 更新，
# 同组注册的删除 handler 永远轮不到——必须另起一组。
_DELETE_SYNC_HANDLER_GROUP = 7
# 轮询对账兜底（M-1 C）：本地比会话 top_message 更新、且已存在 ≥ 这么久的行才算「服务端
# 已删」——刚发出的消息与 get_dialogs 快照之间有竞态窗口，别把它误标撤回。
_DELETE_RECON_MIN_AGE_SEC = 90.0


def find_deleted_by_top(rows: Any, top_id: Any, *, now: float,
                        min_age_sec: float = _DELETE_RECON_MIN_AGE_SEC) -> List[str]:
    """轮询对账（M-1 C #219，纯函数）：会话服务端 ``top_message.id`` 已知，本地却还有
    **id 更大**且未撤回/未软删、投递成功、存在超过 ``min_age_sec`` 的消息行 → 这些行在
    服务端已不存在（多为己方在手机端删了最新几条），返回其 platform_msg_id 列表。

    只认纯数字 id（``h:<hash>`` 兜底键不参与）；``status`` 为 failed/resent 的失败留痕
    本就没发出去，不算。实时 ``UpdateDeleteMessages`` 是主路径，本函数是丢事件 / 重启期
    的兜底：只能发现「最新几条被删」（更早的被删不会改变 top），够覆盖 #219 形态。
    """
    try:
        top = int(top_id or 0)
    except (TypeError, ValueError):
        return []
    if top <= 0:
        return []
    out: List[str] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        pmid = str(r.get("platform_msg_id") or "").strip()
        if not pmid.isdigit() or int(pmid) <= top:
            continue
        if int(r.get("revoked") or 0) or float(r.get("deleted_at") or 0) > 0:
            continue
        if str(r.get("status") or "") in ("failed", "resent"):
            continue
        try:
            ts = float(r.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts <= 0 or (now - ts) < float(min_age_sec):
            continue
        out.append(pmid)
    return out


def parse_mirror_outgoing_media_cfg(pf_cfg: Any) -> Tuple[bool, int, frozenset]:
    """解析 ``telegram.poll_fallback.mirror_outgoing_media``（出站镜像要不要连媒体本体一起归档）。

    返回 ``(enabled, max_bytes, kinds)``。宽容两种写法：

    - ``mirror_outgoing_media: true`` → 内置默认（image/sticker/voice，单条 ≤5MB）；
    - ``mirror_outgoing_media: {enabled, max_mb, kinds}`` → 逐项覆盖。**写成 dict 即视为
      要开**（``enabled`` 可显式置 false 关回去）；``max_mb: 0`` = 不限体积
      （与 ``download_tg_media(max_bytes=0)`` 的语义对齐）；``kinds: []`` = 只结构化
      落 media_type、一律不下载本体（最保守档）。

    键缺失 / 非法类型 → ``(False, 0, frozenset())`` = 严格旧行为（只落占位文字，
    零下载 RPC）。纯函数、每次调用现读——config 热重载后下一条消息即生效。
    """
    raw = pf_cfg.get("mirror_outgoing_media") if isinstance(pf_cfg, dict) else None
    if raw is None or raw is False:
        return False, 0, frozenset()
    default_bytes = int(_MIRROR_MEDIA_DEFAULT_MAX_MB * 1024 * 1024)
    if raw is True:
        return True, default_bytes, frozenset(_MIRROR_MEDIA_DEFAULT_KINDS)
    if not isinstance(raw, dict):
        return False, 0, frozenset()
    if not bool(raw.get("enabled", True)):
        return False, 0, frozenset()
    try:
        max_mb = float(raw.get("max_mb", _MIRROR_MEDIA_DEFAULT_MAX_MB))
    except (TypeError, ValueError):
        max_mb = _MIRROR_MEDIA_DEFAULT_MAX_MB
    max_bytes = int(max(0.0, max_mb) * 1024 * 1024)
    kinds_raw = raw.get("kinds", None)
    if kinds_raw is None:
        kinds = frozenset(_MIRROR_MEDIA_DEFAULT_KINDS)
    elif isinstance(kinds_raw, (list, tuple, set, frozenset)):
        kinds = frozenset(
            str(k).strip().lower() for k in kinds_raw
            if isinstance(k, str) and str(k).strip()
        )
    else:
        kinds = frozenset(_MIRROR_MEDIA_DEFAULT_KINDS)
    return True, max_bytes, kinds


def _username_blocked(no_reply_usernames: Any, username: Any) -> bool:
    """「不自动回复发送者」（telegram.no_reply_sender_usernames，渠道中心「屏蔽名单」）命中判定。

    单一实现供私聊/群聊实时 handler 与轮询兜底共用，防三处各写一份再漂移。
    语义与群路径历史内联实现完全一致：

    - 名单项：strip 空白 + lower + 去前导 ``@``（UI 里 ``@name`` 与 ``name`` 等价）；
    - 待判 username：strip + lower（Telegram API 给的 username 本身不带 ``@``，不剥）；
    - username 为空/None 恒不命中（没有 username 的用户无从按名单匹配）。
    """
    if not no_reply_usernames:
        return False
    uname = (username or "").strip().lower()
    if not uname:
        return False
    return uname in [u.strip().lower().lstrip("@") for u in no_reply_usernames]


class TelegramClient(TelegramTriggerMixin, TelegramSenderMixin, LoggerMixin):
    """Telegram MTProto客户端"""

    def __init__(self, config, skill_manager: SkillManager, ai_client=None,
                 account_cfg: Optional[Dict[str, Any]] = None):
        """
        初始化Telegram客户端

        Args:
            config: 配置管理器实例
            skill_manager: Skill管理器实例
            ai_client: AI 客户端（可选），用于「前一条+当前消息」上下文判断是否回复
            account_cfg: 多账号覆盖字典（可选）；包含 api_id/api_hash/phone_number/
                         session_name/account_id/persona_ids 等，优先于 config 中的值
        """
        self.config = config
        self.skill_manager = skill_manager
        self.ai_client = ai_client
        self.client: Optional[Client] = None
        self.running = False
        _tg_cfg = config.get('telegram', {}) if hasattr(config, 'get') else {}
        _q_size = int(_tg_cfg.get('message_queue_size', 200) or 200)
        self.message_queue = asyncio.Queue(maxsize=_q_size)
        self._max_concurrent = int(_tg_cfg.get('max_workers', 10) or 10)
        self._process_semaphore = asyncio.Semaphore(self._max_concurrent)
        self._active_tasks: int = 0
        self.user_info: Optional[User] = None
        self._session_reply_ts: Dict[str, float] = {}  # (chat_id:user_id) -> 我们最后回复该用户的时间戳
        # 回复逻辑闸门状态（UI「回复逻辑」页：冷却/最大连续回复；与 _session_reply_ts 同 key 格式。
        # 不复用 _session_reply_ts 做冷却——它受 session_window.enabled 开关控制，关了就不写）。
        self._auto_reply_ts: Dict[str, float] = {}  # (chat_id:user_id) -> 上次自动回复时间戳
        self._auto_reply_streak: Dict[str, int] = {}  # (chat_id:user_id) -> 连续自动回复计数
        self._last_send_wallclock: float = 0.0  # 全局上次 send_message 时间，用于 min_interval
        self._boot_timestamp: float = time.time()  # 启动时间戳，用于跳过启动前的旧消息
        # (chat_id, message_id) 去重 + per-chat 串行锁（2026-07-15 三连发语音事故修复：
        # 私聊实时路径原先不登记去重 → 轮询兜底把处理中的消息再跑一遍，双流水线并行
        # 生成两个矛盾回复。claim 收口在 _process_message 入口，三条入站路径共用）。
        from src.client.message_dedup import (
            MessageDedup, PerChatLocks, PollWatermark,
        )
        self._msg_dedup = MessageDedup(max_size=2000, ttl_sec=600.0)
        self._chat_locks = PerChatLocks(max_size=512)
        # 轮询兜底 per-chat 已处理水位（不过期，与 TTL 去重分职；198 事故修复：
        # 未回复的 top_message 每过 dedup TTL 就被轮询重处理白跑 _process_message）
        self._poll_watermark = PollWatermark(max_size=4000)
        self._gxp_pending: Dict[int, list] = {}  # chat_id -> [{cmd, ts, user_id, user_msg_id}, ...]

        from src.utils.i18n import I18n
        from src.utils.event_tracker import EventTracker
        cfg_dir = Path(config.config_path).parent if hasattr(config, "config_path") else Path("config")
        self._cfg_dir = cfg_dir
        self.i18n = I18n(db_path=cfg_dir / "bot.db")
        self.event_tracker = EventTracker(db_path=cfg_dir / "bot.db")
        from src.utils.multi_bot import BotRouter
        self._bot_router = BotRouter(config.config if hasattr(config, 'config') else {})
        from src.utils.rate_limiter import RateLimiter
        self._rate_limiter = RateLimiter(config.config if hasattr(config, 'config') else {})

        # 从配置获取Telegram设置（account_cfg overlay 优先）
        _ov: Dict[str, Any] = dict(account_cfg or {})
        telegram_config = config.get_telegram_config()
        self.api_id = _ov.get('api_id') or telegram_config.get('api_id')
        self.api_hash = _ov.get('api_hash') or telegram_config.get('api_hash')
        # 每账号隔离字段（由 companion worker 现场解析后经 account_cfg 注入）：
        # 设备指纹本体 + 中央池按付费档下发的独立出口。都缺省为空＝行为不变。
        self._device_fp: Dict[str, Any] = {
            k: v for k, v in (_ov or {}).items()
            if k in ("device_model", "system_version", "app_version") and v
        }
        self._account_proxy: Dict[str, Any] = dict(_ov.get('proxy') or {})
        self.phone_number = _ov.get('phone_number') or telegram_config.get('phone_number')
        self.session_name = (
            _ov.get('session_name') or telegram_config.get('session_name', 'camille_bot')
        )
        # 多账号元信息（供日志/persona 路由使用）
        self.account_id: str = str(_ov.get('account_id') or 'default')
        self.account_label: str = str(_ov.get('account_label') or self.account_id)
        self.account_persona_ids: List[str] = [
            str(p) for p in (list(_ov.get('persona_ids') or [])) if p
        ]
        # SSOT 回落：worker 漏传 persona_ids 时从 registry 再解析一次（防双号无身份）
        if not self.account_persona_ids and self.account_id not in ("", "default"):
            try:
                from src.ai.persona_voice import resolve_account_persona_id
                _full = config.config if hasattr(config, "config") else {}
                _pid = resolve_account_persona_id(
                    _full if isinstance(_full, dict) else {},
                    "telegram", self.account_id,
                )
                if _pid:
                    self.account_persona_ids = [_pid]
            except Exception:
                pass
        self.logger.info(
            "账号人设绑定 account=%s label=%s persona_ids=%s",
            self.account_id, self.account_label, self.account_persona_ids or "[]",
        )
        # N 线 核心2：每号独立代理（反封号命门）。proxy_id 指向 proxy_pool 条目；
        # 与 B 线协议 worker 复用同一份 proxy_pool + _to_pyrogram_proxy，不另造代理逻辑。
        self.proxy_id: str = str(
            _ov.get('proxy_id') or telegram_config.get('proxy_id') or ''
        ).strip()
        # N 线 核心4（统一运行时）：session_string 直接喂已授权 session（扫码/手机登录产物），
        # 让协议号无需 phone 即可拉起 A 线"有灵魂"client。空则回落 session 文件 / phone 登录。
        self.session_string: str = str(_ov.get('session_string') or '').strip()
        # N 线 N4b（入站镜像）：开启后把本号收/发的消息镜像进统一收件箱（坐席台可见）。
        # 默认关 → standalone main.py 行为不变；companion worker 拉起协议号时置 True。
        self._mirror_inbox: bool = bool(_ov.get('mirror_inbox', False))

        # 初始化语音转录服务
        self.voice_transcriber = None
        voice_config = self.config.get('voice_recognition', {})

        # 临时目录设置
        # ★ 必须钉成绝对路径：pyrogram download_media 对相对路径按 PARENT_DIR
        #（main.py 所在目录）解析，而本类的 exists() 检查按进程 cwd 解析。
        # 双实例部署 cwd=实例数据根 ≠ 引擎根 → 相对路径会让「写入」与「检查」
        # 分家：文件好端端下载到引擎根，检查却报「下载失败或文件为空」
        #（2026-07-23 语音收不到事故根因）。resolve() 锚定 cwd，保实例隔离。
        self.temp_dir = Path(voice_config.get('temp_dir', './temp/voice')).resolve()
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.max_file_size = voice_config.get('max_file_size', 16777216)  # 16MB
        from src.ai.inbound_video import resolve_inbound_video_max_bytes
        _cfg_root = getattr(config, "config", None) or {}
        self._inbound_video_max_bytes = resolve_inbound_video_max_bytes(_cfg_root)

        if voice_config.get('enabled', False) and VOICE_RECOGNITION_AVAILABLE:
            try:
                self.voice_transcriber = VoiceTranscriberFactory.create_transcriber(voice_config)
                try:
                    from src.voice_transcriber import register_shared_transcriber
                    register_shared_transcriber(self.voice_transcriber)
                except Exception:
                    pass
                self.logger.info("语音转录服务初始化成功")
            except Exception as e:
                self.logger.warning(f"语音转录服务初始化失败: {e}")

        # 初始化图片识别服务
        self.image_recognizer = None
        image_config = self.config.get('image_recognition', {})

        if image_config.get('enabled', False) and IMAGE_RECOGNITION_AVAILABLE:
            try:
                self.image_recognizer = ImageRecognizerFactory.create_recognizer(image_config)
                self.logger.info("图片识别服务初始化成功")
            except Exception as e:
                self.logger.warning(f"图片识别服务初始化失败: {e}")

        # 图像理解（Vision）：请求时 Ollama→智谱链（见 VisionClient.describe_image_with_ollama_zhipu_fallback）
        self.vision_client = None
        vision_config = self.config.get('vision', {})
        if vision_config.get('enabled', False) and VISION_AVAILABLE and VisionClient:
            try:
                from src.vision_client import has_any_vision_backend
                if has_any_vision_backend(vision_config, vision_config):
                    self.logger.info(
                        "图像理解（Vision）已启用：优先本地 Ollama，不可用时使用智谱"
                    )
                else:
                    self.logger.warning(
                        "vision.enabled=true 但未配置可用的 Ollama(base_url) 或智谱(api_key/zhipu_api_key)"
                    )
            except Exception as e:
                self.logger.warning(f"Vision 配置检查失败: {e}")

        # 初始化上下文管理器和情绪增强器
        self.context_manager = None
        self.emotion_enhancer = None
        context_config = self.config.get('context', {})
        emoticons_config = self.config.get('emoticons', {})

        # 检查上下文功能是否启用
        if context_config.get('enabled', False) and CONTEXT_AND_EMOTION_AVAILABLE:
            try:
                # 初始化上下文管理器
                self.context_manager = ContextManager(config=context_config)
                self.logger.info("上下文管理器初始化成功")

                # 初始化情绪增强器（仅在启用时）
                if emoticons_config.get('enabled', True):  # 默认为true
                    self.emotion_enhancer = EmotionEnhancer(config=self.config)
                    self.logger.info("情绪增强器初始化成功")
                else:
                    self.logger.info("情绪增强器已禁用（配置: emoticons.enabled: false）")
            except Exception as e:
                self.logger.warning(f"上下文或情绪增强器初始化失败: {e}")
        else:
            if CONTEXT_AND_EMOTION_AVAILABLE:
                self.logger.info("上下文或情绪增强功能未启用")
            else:
                self.logger.info("上下文或情绪增强模块未安装，功能不可用")

        # 初始化四层触发决策器
        self.four_layer_trigger = None
        trigger_config = self.config.get('trigger', {})

        if trigger_config.get('enabled', False) and FOUR_LAYER_TRIGGER_AVAILABLE:
            try:
                from src.ai.ai_client import AIClient
                # 注意：这里需要AI客户端，但AI客户端可能在skill_manager中初始化
                # 暂时先不传递ai_client，后续在initialize中设置
                self.four_layer_trigger = FourLayerTrigger(
                    config=self.config,
                    context_manager=self.context_manager,
                    ai_client=self.ai_client
                )
                self.logger.info("四层触发决策器初始化成功")
            except Exception as e:
                self.logger.warning(f"四层触发决策器初始化失败: {e}")
        else:
            if FOUR_LAYER_TRIGGER_AVAILABLE:
                self.logger.info("四层触发功能未启用")
            else:
                self.logger.info("四层触发模块未安装，功能不可用")

        self._human_escalation = None
        self._human_escalation_store = None
        try:
            from src.utils.human_escalation_store import HumanEscalationStore
            from src.utils.human_escalation import HumanEscalationHelper
            self._human_escalation_store = HumanEscalationStore(cfg_dir / "human_escalation.db")
            self._human_escalation = HumanEscalationHelper(
                self.config.config if hasattr(self.config, "config") else {},
                self._human_escalation_store,
            )
            self.logger.info("人工转接（重复问句）模块已加载")
        except Exception as e:
            self.logger.warning("人工转接模块初始化失败: %s", e)

        self.logger.info(f"Telegram客户端初始化: {self.session_name}")

    async def initialize(self) -> bool:
        """初始化Telegram客户端"""
        try:
            if not PYROGRAM_AVAILABLE:
                self.logger.error("pyrogram库未安装，请运行: pip install pyrogram")
                return False

            # 检查 API 凭证：api_id/api_hash 必需；phone 仅在"无既有 session"时必需。
            # N 线 核心4：有 session_string 或已落盘 session 文件 → 视为已授权，免 phone 拉起。
            if not (self.api_id and self.api_hash):
                self.logger.error("Telegram API凭证不完整")
                self.logger.error("请从 https://my.telegram.org 获取api_id和api_hash")
                return False
            _has_session = bool(self.session_string) or os.path.exists(self._session_file_path())
            if not _has_session and not self.phone_number:
                self.logger.error("Telegram 登录信息不完整：需 phone_number 或已有 session（session_string/会话文件）")
                return False

            # 创建客户端
            _client_kwargs: Dict[str, Any] = dict(
                name=self.session_name,
                api_id=int(self.api_id),
                api_hash=self.api_hash,
                workdir="sessions",  # 会话文件保存目录
            )
            if self.phone_number:
                _client_kwargs["phone_number"] = self.phone_number
            # N 线 核心4：session_string 优先（in-memory 已授权 session，协议多开/云端拉起常用）
            if self.session_string:
                _client_kwargs["session_string"] = self.session_string
            # N 线 核心2：注入每号独立代理（复用 B 线 proxy_pool + _to_pyrogram_proxy）
            _proxy = self._resolve_proxy()
            if not _proxy:
                # 中央池按付费档下发的独立出口（account_cfg 直接给的 pyrogram 形状）
                _acct_proxy = getattr(self, "_account_proxy", None) or {}
                if isinstance(_acct_proxy, dict) and _acct_proxy:
                    _proxy = _acct_proxy
            if _proxy:
                _client_kwargs["proxy"] = _proxy
                self.logger.info(
                    "Telegram 客户端绑定代理 proxy_id=%s (%s)",
                    self.proxy_id or "pool", _proxy.get("hostname"),
                )
            # 每账号设备指纹：一批号若 device_model/system_version/app_version 全是
            # pyrogram 默认值，即便各自换了凭据和 IP 也会被判成「同一套软件跑出来的」。
            # 关键是**与该号扫码那一刻报给 Telegram 的必须一致**——每次重连换设备
            # 本身就是可疑信号，所以这里读的是该号落库的指纹本体。
            # getattr 兜底：这两个字段是可选 overlay，而 initialize() 也会被
            # `__new__` 造出的最小对象调用（测试里就是），缺了就是「没配」。
            _fp: Dict[str, Any] = getattr(self, "_device_fp", None) or {}
            for _fk in ("device_model", "system_version", "app_version"):
                _fv = _fp.get(_fk)
                if _fv:
                    _client_kwargs[_fk] = str(_fv)
            if _fp:
                self.logger.info("Telegram 客户端设备指纹 account=%s device=%s",
                                 getattr(self, "account_id", "?"), _fp.get("device_model"))
            self.client = Client(**_client_kwargs)

            self.logger.info("Telegram客户端创建成功")
            return True

        except Exception as e:
            self.logger.error(f"初始化Telegram客户端失败: {e}")
            return False

    def _resolve_proxy(self) -> Optional[Dict[str, Any]]:
        """解析本账号绑定的代理 → pyrogram proxy 配置（无 / 失败 → None）。

        N 线 核心2：复用 B 线 ``proxy_pool`` + ``_to_pyrogram_proxy``，A/B 同一套代理源，
        不重复造代理逻辑。``proxy_id`` 为空或解析失败时静默返回 None（保持直连旧行为）。
        """
        if not self.proxy_id:
            return None
        try:
            from src.integrations.proxy_pool import get_proxy_pool
            from src.integrations.telegram_protocol_login import _to_pyrogram_proxy
            entry = get_proxy_pool().get(self.proxy_id, mask=False)
            return _to_pyrogram_proxy(entry)
        except Exception as ex:
            self.logger.warning("代理解析失败 proxy_id=%s: %s", self.proxy_id, ex)
            return None

    def _session_file_path(self) -> str:
        """会话文件路径：workdir 为 sessions 时，文件为 sessions/{name}.session"""
        import os
        return os.path.join("sessions", f"{self.session_name}.session")

    async def _handle_authorization(self) -> bool:
        """处理授权和登录。有 session 文件时不 connect()，让后面 start() 负责连接并启动收消息；无 session 时先 connect() 再登录。"""
        try:
            import os
            session_path = self._session_file_path()
            if os.path.exists(session_path):
                # 已有 session：不 connect()，直接返回，让 start() 里执行 client.start() 以启动更新循环（才能收消息）
                self.logger.info("检测到已有会话文件，将使用 start() 连接并接收消息")
                return True

            # 无 session：必须先 connect() 再检查/执行登录，否则会报 Client has not been started yet
            is_authorized = await self.client.connect()
            if not is_authorized:
                self.logger.info("需要重新授权登录")

                # 发送验证码
                sent_code = await self.client.send_code(self.phone_number)
                self.logger.info(f"验证码已发送到 {self.phone_number}")

                # 这里需要用户输入验证码
                # 在实际使用中，可以通过其他方式获取验证码
                phone_code = await self._request_phone_code()

                if not phone_code:
                    self.logger.error("未收到验证码，登录失败")
                    return False

                try:
                    # 使用验证码登录
                    await self.client.sign_in(
                        phone_number=self.phone_number,
                        phone_code_hash=sent_code.phone_code_hash,
                        phone_code=phone_code
                    )
                    self.logger.info("登录成功")

                except SessionPasswordNeeded:
                    # 需要两步验证密码
                    password = await self._request_2fa_password()
                    if password:
                        await self.client.check_password(password)
                        self.logger.info("两步验证通过")
                    else:
                        self.logger.error("未提供两步验证密码")
                        return False

                except (PhoneCodeInvalid, PhoneCodeExpired) as e:
                    self.logger.error(f"验证码错误: {e}")
                    return False

                except FloodWait as e:
                    self.logger.error(f"触发洪水等待: 需要等待 {e.value} 秒")
                    return False

                except Unauthorized as e:
                    self.logger.error(f"未授权错误: {e}")
                    return False

            # 获取用户信息
            self.user_info = await self.client.get_me()
            self.logger.info(f"登录用户: {self.user_info.first_name} (@{self.user_info.username})")

            return True

        except Exception as e:
            self.logger.error(f"授权处理失败: {e}")
            return False

    async def _request_phone_code(self) -> Optional[str]:
        """
        请求用户输入手机验证码

        注意: 验证码有效期只有5分钟，需要快速处理
        """
        self.logger.warning("⚠️ 需要手机验证码，请在Telegram应用中查看")
        self.logger.warning("📱 请检查您的手机短信或Telegram应用")
        self.logger.warning("⏰ 验证码有效期: 5分钟，请快速处理")

        # 尝试从文件读取验证码（快速方法）
        import asyncio
        code_file = "code.txt"

        # 等待用户输入验证码
        for i in range(30):  # 等待最多30秒
            try:
                # 检查code.txt文件
                import os
                if os.path.exists(code_file):
                    with open(code_file, 'r') as f:
                        phone_code = f.read().strip()
                    if phone_code and phone_code.isdigit():
                        self.logger.info(f"从文件读取到验证码: {phone_code}")
                        # 删除文件避免重复使用
                        os.remove(code_file)
                        return phone_code
            except Exception:
                pass

            # 等待1秒后重试
            await asyncio.sleep(1)

        self.logger.error("未收到验证码，登录失败")
        return None

    async def _request_2fa_password(self) -> Optional[str]:
        """
        获取两步验证密码。
        按优先级从三个来源读取：环境变量 > 配置文件 > 本地文件 2fa_password.txt
        """
        self.logger.warning("🔐 需要两步验证密码，正在查找...")

        password = os.environ.get("TG_2FA_PASSWORD", "").strip()
        if password:
            self.logger.info("从环境变量 TG_2FA_PASSWORD 读取到两步验证密码")
            return password

        try:
            tg_cfg = self.config.get('telegram', {}) if hasattr(self.config, 'get') else {}
            password = (tg_cfg.get("two_fa_password") or "").strip()
            if password:
                self.logger.info("从配置文件 telegram.two_fa_password 读取到两步验证密码")
                return password
        except Exception:
            pass

        fa_file = "2fa_password.txt"
        try:
            if os.path.exists(fa_file):
                with open(fa_file, "r") as f:
                    password = f.read().strip()
                if password:
                    self.logger.info("从文件 %s 读取到两步验证密码", fa_file)
                    return password
        except Exception:
            pass

        self.logger.error(
            "未找到两步验证密码。请通过以下任一方式提供：\n"
            "  1. 设置环境变量 TG_2FA_PASSWORD\n"
            "  2. 在 config.yaml 的 telegram 段添加 two_fa_password 字段\n"
            "  3. 在项目根目录创建 2fa_password.txt 文件"
        )
        return None

    async def start(self, block: bool = True):
        """启动Telegram客户端。

        block=True（默认，main.py 独立运行）：末尾进入 ``idle()`` 常驻；
        block=False（N 线 核心4，编排器托管）：连接+装处理器+起消息处理任务后即返回，
        由外部事件循环（编排器监督循环）保活，便于按账号生命周期 start/stop。
        """
        # 启动失败根因暴露口：本方法历史上把所有启动异常吞成一行 error 日志，
        # SESSION_REVOKED（手机端登出）这类需要人重新扫码的确定性故障被磨成
        # 编排器侧的泛化 "unhealthy"。这里把原始异常文本存下来，companion worker
        # 在 start 后检查 running=False 时取它重新抛给编排器分类。
        self.last_start_error = ""
        try:
            if not self.client:
                self.logger.error("Telegram客户端未初始化")
                self.last_start_error = "client not initialized"
                return

            self.logger.info("启动Telegram客户端...")

            # 处理授权
            if not await self._handle_authorization():
                self.logger.error("授权失败，无法启动")
                self.last_start_error = "authorization failed"
                return

            # 设置消息处理器
            self._setup_handlers()

            # 启动客户端（含 database is locked 重试）
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    await self.client.start()
                    break
                except Exception as e:
                    err_msg = str(e).lower()
                    if "already connected" in err_msg or (type(e).__name__ == "ConnectionError"):
                        self.logger.info("客户端已连接（首次登录），跳过 start()；若收不到消息请重启程序一次")
                        break
                    elif "database is locked" in err_msg and attempt < max_retries:
                        wait = attempt * 2
                        self.logger.warning("会话数据库锁定 (尝试 %d/%d)，%d 秒后重试...", attempt, max_retries, wait)
                        await asyncio.sleep(wait)
                    else:
                        raise
            if self.user_info is None:
                self.user_info = await self.client.get_me()
                self.logger.info(f"登录用户: {self.user_info.first_name} (@{self.user_info.username})")
            # P1 身份化：把本账号自身昵称/用户名（可选头像）富集进注册表 meta.self_*，
            # 供连接中心/切换条显示真实身份。best-effort + flag 默认关 + 后台任务不阻塞启动。
            try:
                from src.integrations.account_self_profile import enrich_from_user
                asyncio.create_task(enrich_from_user(
                    "telegram", getattr(self, "account_id", "default"),
                    self.user_info, config=self.config, client=self.client))
            except Exception:
                self.logger.debug("[self_profile] 富集调度失败（忽略）", exc_info=True)
            self.running = True

            asyncio.create_task(self._message_processor())
            # 轮询兜底：当实时 MTProto 推送通道失效（如某些 session/运行时收不到 updateNewMessage）时，
            # 用 RPC（get_dialogs，正常可用）定时拉新进站私聊喂给同一条 _process_message，
            # 保证全自动回复不因实时通道静默挂掉而失效。与实时 handler 共用去重，互不重复。
            asyncio.create_task(self._poll_inbound_loop())
            # 目录同步：好友名单 + 云端会话列表 → 通讯录/会话占位（只写名单不产生消息），
            # 让「加了好友但从没开口」的人也能在工作台被看到并主动发起对话。
            asyncio.create_task(self._directory_sync_loop())
            # B123（实施74）：报障群断线补拉（bug_intake 开且配了群才真跑）——
            # 断网/重启窗口漏掉的群消息按 seen 账本差集回喂登记链。
            asyncio.create_task(self._bug_intake_backfill_loop())
            # ASR 启动预热（voice_recognition.warmup_on_boot，默认开）：SenseVoice 懒加载
            # 让重启后首条语音吃 ~20-30s 模型冷启动 → 启动即后台预载；失败不影响启动
            # （转录时仍会按需懒加载兜底）。
            if self.voice_transcriber is not None and \
                    self.config.get('voice_recognition', {}).get('warmup_on_boot', True):
                asyncio.create_task(self._warmup_transcriber())
            self._register_reload_notifier()
            self._start_scheduler()
            # P2：情绪增强配置可观测（便于部署核对）
            ec = self.config.get('emoticons', {})
            self.logger.info("情绪增强: %s", "已启用" if ec.get('enabled', True) else "已禁用（emoticons.enabled: false）")
            self.logger.info("✅ Telegram客户端已启动，等待消息...")

            # N 线 核心4：编排器托管模式不进入 idle()，直接返回让监督循环保活
            if not block:
                return
            # 保持客户端运行（可用 pyrogram.idle() 替代，此处用简单循环）
            try:
                from pyrogram import idle
                await idle()
            except ImportError:
                while self.running:
                    await asyncio.sleep(1)

        except Exception as e:
            self.last_start_error = str(e)
            self.logger.error(f"启动Telegram客户端失败: {e}")

    async def stop(self):
        """停止Telegram客户端"""
        if self.running and self.client:
            self.logger.info("正在停止Telegram客户端...")
            self.running = False

            try:
                await self.client.stop()
                self.logger.info("Telegram客户端已停止")
            except Exception as e:
                self.logger.error(f"停止Telegram客户端时出错: {e}")

    # ── 触发决策方法: TelegramTriggerMixin (src/client/trigger.py) ──
    # ── 发送方法: TelegramSenderMixin (src/client/sender.py) ──


    def _setup_handlers(self):
        """设置消息处理器"""
        if not self.client:
            return

        _reject_cooldowns: dict = {}

        @self.client.on_message(filters.private)
        async def handle_private_message(client, message: Message):
            """处理私聊消息（动态读配置，无需重启即可切换行为）"""
            try:
                # ── P0 出站/系统会话守卫：必须是本 handler 的第一件事 ──
                # 「为什么需要」「三条位置约束（限流之前 / claim 之前 / process_private
                # 之前）」「系统会话为何判在出站之前」全部见 _should_skip_as_outbound
                # 的 docstring，此处不复述。
                _skip, _mirror = self._should_skip_as_outbound(message)
                if _skip:
                    if _mirror:
                        # catchup 传 0：实时到达的消息 mts >= self._boot_timestamp 恒成立，
                        # 镜像方法里的 after_boot 分支必过，不需要 catchup 补窗
                        # （catchup 存在只是为了让轮询能补回宕机期间的旧消息）。
                        await self._mirror_outgoing_message(message.chat, message, 0)
                    return

                uid = str(getattr(getattr(message, 'from_user', None), 'id', 0))

                # ── 限流：私聊也走令牌桶 ──
                if self._rate_limiter.enabled:
                    allowed, reason = self._rate_limiter.allow(uid, message.chat.id)
                    if not allowed:
                        _note_outbound_block("business", f"rate_limit_{reason}")
                        if self._rate_limiter.check_auto_ban(uid):
                            self.logger.warning("[私聊] 用户 %s 触发自动封禁", uid)
                        else:
                            self.logger.info("[私聊] 限流丢弃 user=%s reason=%s", uid, reason)
                        return

                # ── 自动封禁检查 ──
                if self._rate_limiter.is_banned(uid):
                    _note_outbound_block("business", "rate_limit_banned")
                    self.logger.debug("[私聊] 用户 %s 在封禁名单中，静默忽略", uid)
                    return

                # ── 动态读取最新配置 ──
                tg_cfg = self.config.get_telegram_config()
                process_private = tg_cfg.get("process_private", True)

                if process_private:
                    # P-0.5: 私聊消息去重登记（2026-07-15 三连发事故根因修复）——
                    # 此前私聊实时路径从不登记 mid，语音回复链路耗时 > 轮询间隔时，
                    # 轮询兜底把处理中的同一条消息再跑一遍 → 双流水线并行、矛盾回复。
                    _mid = getattr(message, 'id', 0) or getattr(message, 'message_id', 0)
                    if not self._msg_dedup.claim(message.chat.id, _mid):
                        self.logger.debug(
                            "[私聊] 跳过: 去重 chat=%s mid=%s", message.chat.id, _mid)
                        _m = _metrics()
                        if _m:
                            _m.record_dedup_blocked()
                        return
                    # 屏蔽名单（渠道中心「不自动回复发送者」）：与群路径同口径、
                    # 每消息动态读 config（渠道中心保存后热生效）。刻意放在去重
                    # claim 之后——先占住 mid，共用同一去重表的轮询兜底才不会把
                    # 已屏蔽的这条消息当「新进站」再捞回来处理。
                    no_reply = self.config.get('telegram', {}).get('no_reply_sender_usernames') or []
                    _from_user = getattr(message, 'from_user', None)
                    if no_reply and _from_user:
                        _uname = (getattr(_from_user, 'username', None) or '').strip().lower()
                        if _username_blocked(no_reply, _uname):
                            self.logger.debug(
                                "[私聊] 跳过: 配置的不回复发送者 username=%s", _uname)
                            return
                    if _has_ingestable_media(message):
                        await self._process_message(message)
                    else:
                        self.logger.debug("忽略无可入站内容的私聊消息: %s", message.chat.id)
                else:
                    reject_msg = tg_cfg.get(
                        "private_reject_message",
                        "亲，抱歉，我们不接受私聊处理问题，请在群内联系我或者@我，我将竭诚为您服务。"
                    )
                    now = __import__('time').time()
                    cooldown = int(tg_cfg.get("private_reject_cooldown", 60))
                    last_sent = _reject_cooldowns.get(uid, 0)
                    if now - last_sent < cooldown:
                        self.logger.debug("[私聊] 引导语冷却中 user=%s (%.0fs)", uid, now - last_sent)
                        return
                    _reject_cooldowns[uid] = now
                    try:
                        await message.reply_text(reject_msg)
                        self.logger.info("[私聊] 已发送引导语 → user=%s", uid)
                    except Exception as e:
                        self.logger.warning("[私聊] 发送引导语失败: %s", e)

                    if len(_reject_cooldowns) > 5000:
                        cutoff = now - 3600
                        _reject_cooldowns.clear()
                        # 大量缓存时整体清理即可，下次再发自然重新计时
            except Exception as e:
                self.logger.error("处理私聊消息失败: %s", e)

        # 处理群组消息
        @self.client.on_message(filters.group)
        async def handle_group_message(client, message: Message):
            """处理群组消息"""
            try:
                # ── 动态开关：实时读取配置，无需重启 ──
                if not self.config.get_telegram_config().get("process_groups", True):
                    return

                chat_id = getattr(message.chat, 'id', 0)
                self.logger.info("[群消息] 收到一条群消息 chat_id=%s", chat_id)

                # 群聊灰度白名单（P3-1）：allowlist_chat_ids 非空时仅名单内群放行。
                from src.client.reply_logic_gates import group_allowlist_blocked
                _gr_cfg = self.config.get('telegram', {}).get('group_reply', {})
                if group_allowlist_blocked(_gr_cfg, chat_id):
                    self.logger.debug(
                        "[群消息] 跳过: 不在灰度白名单 chat_id=%s", chat_id)
                    return

                # P-1: 跳过 bot 启动之前的旧消息（防止重启后处理历史队列导致循环）
                msg_date = getattr(message, 'date', None)
                if msg_date:
                    msg_ts = msg_date.timestamp() if hasattr(msg_date, 'timestamp') else 0
                    if msg_ts < self._boot_timestamp - 5:
                        self.logger.info("[群消息] 跳过: 启动前旧消息 chat_id=%s", chat_id)
                        return

                # P-0.5: 消息去重 — 防止网络重传导致同一条消息被处理两次
                # （复合键 chat_id:mid——supergroup 消息 id 是 per-channel 的，裸 mid
                # 会跨群相撞导致误判重复、静默丢消息）
                mid = getattr(message, 'id', 0) or getattr(message, 'message_id', 0)
                # C1-②（#123 族，2026-09-02）：报障群序号连续性哨兵——放在去重之前
                # （重投/编辑同号对哨兵是无信息量的旧号，天然幂等），任何真到达
                # 本进程的 id 都算「见过」；跳号＝中间有号没到本进程＝候选漏收，
                # 宽限期后由 _bug_intake_seq_gap_sweep 云端定点补拉并重放本 handler。
                try:
                    from src.ops.bug_intake import note_group_msg_seq
                    _bi_root = (self.config.config
                                if hasattr(self.config, "config") else self.config)
                    if note_group_msg_seq(
                            _bi_root if isinstance(_bi_root, dict) else {},
                            chat_id, mid,
                            account_id=getattr(self, "account_id", "") or "default"):
                        self._schedule_bug_intake_seq_sweep()
                except Exception:
                    self.logger.debug("[bug_intake] 序号哨兵登记失败（忽略）",
                                      exc_info=True)
                if not self._msg_dedup.claim(chat_id, mid):
                    self.logger.debug("[群消息] 跳过: 去重 chat=%s mid=%s", chat_id, mid)
                    _m = _metrics()
                    if _m:
                        _m.record_dedup_blocked()
                    return

                # 多 Bot 路由
                if hasattr(self, '_bot_router') and self._bot_router.enabled:
                    if not self._bot_router.should_handle(message.chat.id, self.session_name):
                        self.logger.info("[群消息] 跳过: 多 Bot 路由(该群由其他 session 处理) chat_id=%s", message.chat.id)
                        return

                # 令牌桶限流
                if self._rate_limiter.enabled:
                    uid = str(getattr(getattr(message, 'from_user', None), 'id', 0))
                    allowed, reason = self._rate_limiter.allow(uid, message.chat.id)
                    if not allowed:
                        _note_outbound_block("business", f"rate_limit_{reason}")
                        self.logger.info("[群消息] 跳过: 限流 user=%s chat=%s reason=%s", uid, message.chat.id, reason)
                        return

                # 检查是否需要处理此消息类型（含视频/视频圆点/GIF/贴纸，防被静默丢弃）
                if not _has_ingestable_media(message):
                    self.logger.info("[群消息] 跳过: 无可入站内容 chat_id=%s title=%s", chat_id, getattr(message.chat, 'title', ''))
                    return

                # P0: 不处理自己发送的消息（避免自我循环）
                from_user = getattr(message, 'from_user', None)
                if from_user and self.user_info and getattr(from_user, 'id', None) == self.user_info.id:
                    self.logger.info("[群消息] 跳过: 自身发送的消息 (from_user.id=%s)", getattr(from_user, 'id', None))
                    return
                if getattr(message, 'outgoing', False):
                    self.logger.info("[群消息] 跳过: outgoing 自身消息")
                    return
                # GXP 结果追踪：在过滤 bot 消息前，先检查是否为 gxp_notify_bot 的回复
                gxp_bot_username = (self.config.get('telegram', {}).get('gxp_commands', {}).get('bot_username') or 'gxp_notify_bot').lower()
                if from_user:
                    sender_uname = (getattr(from_user, 'username', '') or '').strip().lower()
                    if sender_uname == gxp_bot_username:
                        await self._handle_gxp_bot_reply(message)

                # P0: 不回复机器人账号或配置中的「不回复」发送者（避免对 gxp_notify_bot 等误回）
                if from_user and getattr(from_user, 'is_bot', False):
                    self.logger.info("[群消息] 跳过: 机器人发送者 username=%s", getattr(from_user, 'username', ''))
                    return
                no_reply = self.config.get('telegram', {}).get('no_reply_sender_usernames') or []
                if no_reply and from_user:
                    uname = (getattr(from_user, 'username', None) or '').strip().lower()
                    if _username_blocked(no_reply, uname):
                        self.logger.info("[群消息] 跳过: 配置的不回复发送者 username=%s", uname)
                        return
                # 📊 强制记录所有群组消息（监控完整性）- 新增
                text = message.text or message.caption or ""
                chat_title = message.chat.title if message.chat.title else "Unknown"
                username = from_user.username if from_user else "unknown"
                self.logger.info(
                    f"[群组监控] 收到消息 [{chat_title}/{username}] "
                    f"account={getattr(self, 'account_id', 'default')} "
                    f"conv=telegram:{getattr(self, 'account_id', 'default')}:{message.chat.id}: "
                    f"{text[:100]}...")
                daily_stats.bump("messages")

                # 检查是否需要回复（使用异步方法）
                if not await self._should_reply_to_group_message(message):
                    mode = self.config.get('telegram', {}).get('group_reply', {}).get('mode', 'always')
                    trigger_on = self.config.get('trigger', {}).get('enabled', False)
                    self.logger.info(
                        "[群组监控] 跳过未触发消息: %s... (模式=%s, 四层=%s). "
                        "触发方式: 回复我们的消息 / @本账号 / 关键词或图片+文字 / 追问或会话窗口内L2",
                        text[:50] if text else "(无文本)", mode, "开" if trigger_on else "关"
                    )
                    # 2026-09-18 P6 社群舞台实锤：「不自动回复」≠「不让坐席看见」。此前
                    # 未触发的群消息在这里整条消失，工作台「群组」视图要等历史自动同步 /
                    # 手动 from_latest 才滞后出现。这里只镜像进收件箱（群组动态分流，
                    # 不进 SLA / 起草 / 自动回复），触发裁决一字不改。
                    self._mirror_untriggered_group_message(message)
                    return

                # 满足条件，处理消息
                await self._process_message(message)

            except Exception as e:
                self.logger.error(f"处理群组消息失败: {e}")

        # C1-②：序号哨兵补拉重放入口——补回的消息走与实时到达**同一个** handler
        # （去重/触发裁决/镜像/登记/媒体归档全链一致），不另起一条「补拉专用」半链。
        self._group_message_handler = handle_group_message

        # 编辑消息（UI「回复逻辑」页 ignore_edited 开关，缺省=忽略）：pyrogram 的
        # 编辑更新走独立的 on_edited_message，不进上面的 on_message handler。
        # 开关关（ignore_edited=false）时把编辑消息转投给对应的现有 handler——
        # 其内部 _msg_dedup.claim 按 (chat_id, message_id) 去重，原始版已处理过的
        # 消息编辑后重投会被去重挡下（handler 自带 debug 日志）：编辑重投最多生效一次。
        @self.client.on_edited_message(filters.private | filters.group)
        async def handle_edited_message(client, message: Message):
            try:
                from src.client.reply_logic_gates import should_ignore_edited
                _rl_cfg = self.config.get('telegram', {}).get('reply_logic', {})
                if should_ignore_edited(
                        _rl_cfg, getattr(message, 'edit_date', None)):
                    self.logger.debug(
                        "[编辑消息] 忽略（ignore_edited=on）chat=%s mid=%s",
                        getattr(getattr(message, 'chat', None), 'id', None),
                        getattr(message, 'id', 0))
                    return
                _ctype = getattr(getattr(message, 'chat', None), 'type', None)
                _ctype_name = (getattr(_ctype, 'name', None)
                               or str(_ctype or '')).upper()
                if 'PRIVATE' in _ctype_name:
                    await handle_private_message(client, message)
                else:
                    await handle_group_message(client, message)
            except Exception as e:
                self.logger.error("处理编辑消息失败: %s", e)

        # P4-4 已读回执：companion 镜像开启时，注册原始更新处理器——对端读了我们发的消息
        # （UpdateReadHistoryOutbox / 频道版）→ 把镜像进收件箱的出站消息升级为「已读」，
        # 前端出站气泡即显示蓝色双勾。standalone（mirror 关）不注册，零影响。
        if getattr(self, "_mirror_inbox", False):
            try:
                from pyrogram.handlers import RawUpdateHandler
                from pyrogram import raw as _raw

                _acct = getattr(self, "account_id", "default")

                async def _on_read_receipt(_client, update, _users, _chats):
                    try:
                        from src.integrations.protocol_bridge import (
                            report_read_upto, tg_peer_to_chat_key,
                        )
                        if isinstance(update, _raw.types.UpdateReadHistoryOutbox):
                            ck = tg_peer_to_chat_key(getattr(update, "peer", None))
                            if ck:
                                report_read_upto("telegram", _acct, ck,
                                                 getattr(update, "max_id", 0))
                        elif isinstance(update, _raw.types.UpdateReadChannelOutbox):
                            chid = getattr(update, "channel_id", None)
                            if chid is not None:
                                report_read_upto("telegram", _acct, f"-100{int(chid)}",
                                                 getattr(update, "max_id", 0))
                    except Exception:
                        self.logger.debug("[mirror] 已读回执处理失败", exc_info=True)

                self.client.add_handler(RawUpdateHandler(_on_read_receipt))
            except Exception:
                self.logger.debug("[mirror] 注册已读回执处理器失败", exc_info=True)

            # B87（实施68）：对端手机删消息 → 工作台镜像同步软删。
            # UpdateDeleteMessages（私聊/小群，只带裸 id）→ 全账号按 platform_msg_id
            # 软删；UpdateDeleteChannelMessages（带 channel_id）→ 收窄到该会话。
            # 与已读回执同挂在镜像开启分支（都是「让工作台跟手机一致」的镜像职能）。
            try:
                from pyrogram.handlers import RawUpdateHandler as _RUH
                from pyrogram import raw as _raw2

                _acct_d = getattr(self, "account_id", "default")

                async def _on_deleted(_client, update, _users, _chats):
                    try:
                        from src.integrations.protocol_bridge import (
                            report_deleted_messages,
                        )
                        if isinstance(update, _raw2.types.UpdateDeleteMessages):
                            mids = [str(m) for m in (getattr(update, "messages", None) or [])]
                            if mids:
                                # M-1 C #219：删除事件落 INFO（此前只有命中时才有日志，
                                # 「零删除事件」无法与「事件到了没对上」区分）
                                self.logger.info(
                                    "[mirror] 收到删除更新 ids=%s（私聊/小群，裸 id 全账号反查）",
                                    ",".join(mids[:8]) + ("…" if len(mids) > 8 else ""))
                                report_deleted_messages("telegram", _acct_d, mids)
                        elif isinstance(update, _raw2.types.UpdateDeleteChannelMessages):
                            chid = getattr(update, "channel_id", None)
                            mids = [str(m) for m in (getattr(update, "messages", None) or [])]
                            if chid is not None and mids:
                                self.logger.info(
                                    "[mirror] 收到频道删除更新 channel=%s ids=%s",
                                    chid, ",".join(mids[:8]) + ("…" if len(mids) > 8 else ""))
                                report_deleted_messages(
                                    "telegram", _acct_d, mids,
                                    chat_key=f"-100{int(chid)}")
                    except Exception:
                        self.logger.debug("[mirror] 删除同步处理失败", exc_info=True)

                # M-1 C #219 根因：pyrogram 同一 group 内**只跑第一个命中的 handler**（dispatcher
                # 对每个 group 命中即 break），而上面的已读回执 RawUpdateHandler 已在 group 0
                # 对所有 raw 更新都「命中」——本删除 handler 注册在同组从未被调用过（skuio 机
                # 14:03–14:16 零删除事件的真正原因）。放独立 group 让两个 raw handler 都跑。
                self.client.add_handler(_RUH(_on_deleted), group=_DELETE_SYNC_HANDLER_GROUP)
            except Exception:
                self.logger.debug("[mirror] 注册删除同步处理器失败", exc_info=True)

        _tg = self.config.get_telegram_config()
        self.logger.info(
            "消息处理器已设置 - 私聊=%s, 群组=%s (动态开关，保存即生效)",
            "处理" if _tg.get("process_private", True) else "引导语",
            "开" if _tg.get("process_groups", True) else "关",
        )

    async def _warmup_transcriber(self) -> None:
        """后台预热 ASR 模型（start() 调度）：成功记耗时，失败降级为懒加载。"""
        import time as _time
        try:
            t0 = _time.time()
            await self.voice_transcriber.warmup()
            self.logger.info(f"ASR 预热完成 ({_time.time() - t0:.1f}s)")
        except Exception as e:
            self.logger.warning(f"ASR 预热失败（首条语音将按需懒加载）: {e}")

    async def _download_voice_file(self, message: Message) -> Optional[Path]:
        """
        下载语音消息文件到临时目录

        Args:
            message: Telegram消息对象，包含voice或audio属性

        Returns:
            下载的文件路径，如果失败返回None
        """
        try:
            # 确定文件ID和文件类型
            if message.voice:
                file_id = message.voice.file_id
                file_extension = ".ogg"  # Telegram语音通常是OGG格式
            elif message.audio:
                file_id = message.audio.file_id
                file_extension = ".mp3"  # 或根据实际情况
            else:
                self.logger.error("消息不是语音或音频类型")
                return None

            # 生成临时文件名
            import time
            import uuid
            timestamp = int(time.time())
            unique_id = str(uuid.uuid4())[:8]
            temp_filename = f"voice_{timestamp}_{unique_id}{file_extension}"
            temp_file_path = self.temp_dir / temp_filename

            self.logger.info(f"下载语音文件: {file_id} -> {temp_file_path}")

            # 下载文件（保留返回值：pyrogram 实际写入路径，路径分歧类故障的取证关键）
            downloaded = await message.download(file_name=str(temp_file_path))

            # 检查文件是否下载成功
            if temp_file_path.exists() and temp_file_path.stat().st_size > 0:
                file_size = temp_file_path.stat().st_size
                if file_size > self.max_file_size:
                    self.logger.warning(f"文件过大: {file_size} bytes > {self.max_file_size} limit")
                    temp_file_path.unlink(missing_ok=True)
                    return None

                self.logger.info(f"语音文件下载成功: {temp_file_path} ({file_size} bytes)")
                return temp_file_path
            else:
                self.logger.error(
                    f"文件下载失败或文件为空: download返回={downloaded!r} 检查路径={temp_file_path}")
                return None

        except Exception as e:
            self.logger.error(f"下载语音文件失败: {e}")
            return None

    async def _download_image_file(self, message: Message) -> Optional[Path]:
        """
        下载图片消息文件到临时目录

        Args:
            message: Telegram消息对象，包含photo或document属性

        Returns:
            下载的文件路径，如果失败返回None
        """
        try:
            # 确定文件ID和文件类型
            file_id = None
            file_extension = ".jpg"  # 默认扩展名

            if message.photo:
                # 照片消息：Pyrogram 2.x 中 photo 可能是单个 Photo 对象或 PhotoSize 列表，先按单对象取 file_id
                photo_obj = message.photo
                file_id = getattr(photo_obj, 'file_id', None)
                if file_id is None and isinstance(photo_obj, (list, tuple)):
                    try:
                        largest_photo = max(photo_obj, key=lambda p: getattr(p, 'file_size', 0) or 0)
                        file_id = getattr(largest_photo, 'file_id', None)
                    except (TypeError, ValueError):
                        file_id = getattr(photo_obj[0], 'file_id', None) if photo_obj else None
                file_extension = ".jpg"

            elif message.document:
                # 文档消息，检查是否是图片
                document = message.document
                mime_type = document.mime_type or ""

                # 检查是否是图片文件
                if mime_type.startswith('image/'):
                    file_id = document.file_id
                    # 根据MIME类型确定扩展名
                    if 'jpeg' in mime_type or 'jpg' in mime_type:
                        file_extension = ".jpg"
                    elif 'png' in mime_type:
                        file_extension = ".png"
                    elif 'gif' in mime_type:
                        file_extension = ".gif"
                    elif 'bmp' in mime_type:
                        file_extension = ".bmp"
                    else:
                        file_extension = ".jpg"  # 默认
                else:
                    self.logger.warning(f"不是图片文件: {mime_type}")
                    return None
            else:
                self.logger.error("消息不是图片或文档类型")
                return None

            if not file_id:
                self.logger.error("无法获取文件ID")
                return None

            # 生成临时文件名
            import time
            import uuid
            timestamp = int(time.time())
            unique_id = str(uuid.uuid4())[:8]
            temp_filename = f"image_{timestamp}_{unique_id}{file_extension}"
            temp_file_path = self.temp_dir / temp_filename

            self.logger.info(f"下载图片文件: {file_id} -> {temp_file_path}")

            # 下载文件（保留返回值：pyrogram 实际写入路径，路径分歧类故障的取证关键）
            downloaded = await message.download(file_name=str(temp_file_path))

            # 检查文件是否下载成功
            if temp_file_path.exists() and temp_file_path.stat().st_size > 0:
                file_size = temp_file_path.stat().st_size
                if file_size > self.max_file_size:
                    self.logger.warning(f"文件过大: {file_size} bytes > {self.max_file_size} limit")
                    temp_file_path.unlink(missing_ok=True)
                    return None

                self.logger.info(f"图片文件下载成功: {temp_file_path} ({file_size} bytes)")
                return temp_file_path
            else:
                self.logger.error(
                    f"文件下载失败或文件为空: download返回={downloaded!r} 检查路径={temp_file_path}")
                return None

        except Exception as e:
            self.logger.error(f"下载图片文件失败: {e}")
            return None

    def _vision_usable(self) -> bool:
        """是否可走 Vision（含 Ollama→智谱回退链）。"""
        v = self.config.get("vision", {})
        if not v.get("enabled") or not VISION_AVAILABLE or not VisionClient:
            return False
        try:
            from src.vision_client import has_any_vision_backend
            return has_any_vision_backend(v, v)
        except Exception:
            return False

    async def _get_image_content(self, image_path: str) -> Optional[str]:
        """Vision 单轨识图。失败返 None，**不再**回落 OCR（无兜底纪律）。

        OCR 是替代品：看不清画面却用残缺文字硬聊＝8/16 串台同病。调用方
        拿到 None 必须跳过自动回复，不得装懂。
        """
        path = Path(image_path)
        if not path.exists() or not path.is_file():
            return None
        if not self._vision_usable():
            return None
        vision_config = self.config.get("vision", {})
        try:
            text, tag = await VisionClient.describe_image_with_ollama_zhipu_fallback(
                vision_config,
                vision_config,
                str(path),
                prompt=vision_config.get("prompt"),
            )
            if text and text.strip():
                desc = text.strip()[:2000]
                # P0 2026-08-19「乱码识图」闸门：键帽/界面元素逐字抄录汤 → 诚实提示
                # （单一判定源＝media_enrich.desc_looks_garbled，与协议线同口径）。
                try:
                    from src.inbox.media_enrich import (
                        GARBLED_DESC_NOTE, desc_looks_garbled, flatten_desc_line,
                    )
                    # #143 C-补：描述压单行（与 media_enrich / inbound_video 同口径），
                    # 多行续行会逃过语言证据剥离把英文会话带成中文。
                    desc = flatten_desc_line(desc) or desc
                    if desc_looks_garbled(desc):
                        self.logger.info(
                            "Vision 产出判为碎片文字汤（%d 字），已替换为提示", len(desc))
                        return GARBLED_DESC_NOTE
                except Exception:
                    pass
                self.logger.info(
                    f"Vision 解析成功 ({tag})，长度 {len(desc)} 字符"
                )
                return desc
        except Exception as e:
            self.logger.warning(f"Vision 解析失败（不回落 OCR）: {e}")
        return None

    async def _download_video_file(self, message: Message) -> Optional[Path]:
        """下载视频/视频圆点/GIF 到临时目录（超体积上限则跳过）。失败返回 None。"""
        try:
            media = (getattr(message, "video", None)
                     or getattr(message, "video_note", None)
                     or getattr(message, "animation", None))
            if media is None:
                return None
            size = getattr(media, "file_size", 0) or 0
            if size and size > self._inbound_video_max_bytes:
                self.logger.info(
                    "视频超体积上限跳过抽帧: %s bytes > %s",
                    size, self._inbound_video_max_bytes)
                return None
            import time as _t
            import uuid as _u
            temp_file_path = self.temp_dir / f"video_{int(_t.time())}_{str(_u.uuid4())[:8]}.mp4"
            self.logger.info(f"下载视频文件 -> {temp_file_path}")
            await message.download(file_name=str(temp_file_path))
            if temp_file_path.exists() and temp_file_path.stat().st_size > 0:
                return temp_file_path
            return None
        except Exception as e:
            self.logger.error(f"下载视频文件失败: {e}")
            return None

    async def _get_video_content(self, video_path: str) -> Optional[str]:
        """理解视频（委托 inbound_video 共享模块）。"""
        from src.ai.inbound_video import understand_video_file
        vcfg = self.config.get("vision", {}) if hasattr(self.config, "get") else {}
        return await understand_video_file(
            video_path,
            vision_config=vcfg,
            voice_transcriber=self.voice_transcriber,
            speech_emotion_config=self.config.get("speech_emotion", {}) or {},
            voice_recognition_config=self.config.get("voice_recognition", {}) or {},
        )

    async def _get_sticker_content(self, message: Message) -> str:
        """理解贴纸：①读 sticker.emoji（作者标注的情绪符号，零成本）②静态 webp 送 Vision
        看图形动作。产出「[表情] 含义…」交 AI 自然回应；动态贴纸(.tgs/webm)只用 emoji。
        """
        from src.integrations.tg_inbound_text import demojize_one, sticker_text_from_message
        sticker = getattr(message, "sticker", None)
        # ① 关联 emoji → 中文语义（😂→笑哭了），最稳的情绪线索
        emo_hint = ""
        raw_emoji = str(getattr(sticker, "emoji", "") or "")
        if raw_emoji:
            emo_hint = demojize_one(raw_emoji)
        # ② 静态贴纸（非 animated/video）→ Vision 看图形。动图跳过（需转帧，成本高）。
        vis_desc = ""
        is_dynamic = bool(getattr(sticker, "is_animated", False)
                          or getattr(sticker, "is_video", False))
        if sticker is not None and not is_dynamic and self._vision_usable():
            try:
                import time as _t
                import uuid as _u
                webp_path = self.temp_dir / f"sticker_{int(_t.time())}_{str(_u.uuid4())[:8]}.webp"
                await message.download(file_name=str(webp_path))
                if webp_path.exists() and webp_path.stat().st_size > 0:
                    _d = await self._get_image_content(str(webp_path))
                    if _d:
                        # P1 2026-08-19：贴纸文本是「[表情] …」格式（无 [图片内容] 标记），
                        # 前端不解析类型标记 → 首行「类型=C」会裸露，此处剥掉（单一来源）。
                        from src.inbox.media_enrich import parse_desc_type
                        _d = parse_desc_type(_d)[1]
                    if _d:
                        vis_desc = _normalize_message_text(_d).strip()[:80]
                try:
                    webp_path.unlink(missing_ok=True)
                except Exception:
                    pass
            except Exception:
                self.logger.debug("贴纸 Vision 解析失败（忽略）", exc_info=True)
        # 组装（无 Vision 时回落共享 sticker_text_from_message 口径）
        if vis_desc:
            bits = []
            if emo_hint:
                bits.append(emo_hint)
            bits.append(vis_desc)
            return f"[表情] {' · '.join(bits)}"
        return sticker_text_from_message(message)

    def _annotate_inbound_emoji(self, text: str) -> str:
        from src.integrations.tg_inbound_text import annotate_inbound_emoji
        return annotate_inbound_emoji(text)

    async def _get_recent_bot_messages(self, chat_id: int) -> List[Dict[str, str]]:
        """拉取当前群内近期机器人/通知号消息，供 AI 参考（订单、通道通知等）。"""
        cfg = self.config.get('context', {}).get('bot_sources', {})
        if not cfg.get('enabled', False) or not self.client:
            return []
        limit = min(int(cfg.get('limit', 25)), 50)
        include_any_bot = cfg.get('include_any_bot', False)
        usernames = [u.strip().lower().lstrip('@') for u in cfg.get('usernames', [])]
        out = []
        try:
            async for msg in self.client.get_chat_history(chat_id, limit=limit):
                if not getattr(msg, 'text', None):
                    continue
                from_user = getattr(msg, 'from_user', None)
                if not from_user:
                    continue
                uname = (getattr(from_user, 'username') or '').lower()
                is_bot = getattr(from_user, 'is_bot', False)
                if include_any_bot and is_bot:
                    out.append({"from": uname or str(getattr(from_user, 'id', '')), "text": (msg.text or "")[:800]})
                elif usernames and uname in usernames:
                    out.append({"from": uname, "text": (msg.text or "")[:800]})
            if out:
                self.logger.debug(f"拉取到 {len(out)} 条机器人/通知消息供 AI 参考")
        except Exception as e:
            self.logger.warning(f"获取群内机器人消息失败: {e}")
        return out

    async def _get_recent_chat_image_ocr(self, chat_id: int, limit: int = 20) -> Optional[str]:
        """
        当用户问「看到订单图了吗」但当前消息无图时：拉取群内最近一条带图消息并 OCR，
        结果供 AI 使用，从而能回复「看到了」并概括图中内容。
        历史按时间倒序（最新在前），会跳过无图消息，对带图消息逐个尝试 OCR，最多试 3 条带图消息。
        """
        if not self.client or not self._vision_usable():
            self.logger.debug("群内最近图 跳过: 未初始化 client 或 Vision")
            return None
        try:
            tried = 0
            max_photo_messages = 3
            async for msg in self.client.get_chat_history(chat_id, limit=limit):
                has_photo = bool(getattr(msg, 'photo', None))
                has_img_doc = False
                if getattr(msg, 'document', None) and getattr(msg.document, 'mime_type', None):
                    has_img_doc = (msg.document.mime_type or "").startswith("image/")
                if not (has_photo or has_img_doc):
                    continue
                tried += 1
                msg_id = getattr(msg, 'id', None)
                sender = getattr(getattr(msg, 'from_user', None), 'username', None) or getattr(msg, 'sender_id', None)
                self.logger.info(f"群内最近图: 尝试第 {tried} 条带图消息 id={msg_id} 发送者={sender}（Vision/OCR）")
                image_file = await self._download_image_file(msg)
                if not image_file or not image_file.exists():
                    self.logger.warning(f"群内最近图: 下载失败 msg_id={msg_id}")
                    continue
                try:
                    content = await self._get_image_content(str(image_file))
                    if content:
                        # P1 2026-08-19：群图作纯上下文文本消费，类型标记无消费方 → 剥掉
                        from src.inbox.media_enrich import parse_desc_type
                        content = parse_desc_type(content)[1] or content
                        self.logger.info(f"群内最近一张图解析成功 msg_id={msg_id}，长度 {len(content)} 字符")
                        return content[:2000]
                    self.logger.warning(f"群内最近图: 结果为空 msg_id={msg_id}，尝试下一条带图消息")
                except Exception as err:
                    self.logger.warning(f"群内最近图解析异常 msg_id={msg_id}: {err}")
                finally:
                    try:
                        image_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                if tried >= max_photo_messages:
                    break
            if tried == 0:
                self.logger.info("群内最近图: 未找到带图消息")
            return None
        except Exception as e:
            self.logger.warning(f"拉取群内最近图片并 OCR 失败: {e}")
            return None

    def _defer_swallowed_inbound(
        self, chat_id: Any, message_id: Any, retry_after_sec: float,
        reason: str = "",
    ) -> bool:
        """被冷却类闸门吞掉的私聊入站 → 缩短其去重寿命，交轮询兜底定时补答。

        2026-08-09「回复等了 11 分钟」修复（198↔104 实录三连）：冷却/interject
        吞掉回复后没有任何补救调度，唯一复活途径是轮询兜底在去重 TTL（600s）
        过期后把未回复的 top_message 当新进站重拾——等待时长＝一个从未被设计
        过的巧合数字。本方法把该 mid 的去重到期改写为「冷却结束后不久」，
        轮询兜底（默认 12s 一轮）届时自然重拾，全套既有守卫（自动化档位、
        水位一次性重试、top_message 未回复判定）原样生效：
        - 对方期间又来新消息且已被回 → top 变 outgoing，兜底自动不重试；
        - 对方又来新消息也被吞 → 兜底只拾最新那条（正确的合并语义）；
        - 重试再次被吞 → 水位闸已登记，绝不循环（本方法也直接放弃）。

        仅私聊有意义（轮询兜底只扫私聊），调用方负责按 is_group 闸。
        开关 ``telegram.poll_fallback.swallow_reschedule``（默认开，随
        poll_fallback 总闸；关 poll_fallback 时重排去重毫无意义，直接跳过）。
        """
        try:
            if not message_id:
                return False
            try:
                tg_cfg = self.config.get_telegram_config()
            except Exception:
                tg_cfg = {}
            pf = (tg_cfg.get("poll_fallback") or {}) if isinstance(tg_cfg, dict) else {}
            if not pf.get("enabled", True):
                return False
            if not pf.get("swallow_reschedule", True):
                return False
            if self._poll_watermark.already_processed(chat_id, message_id):
                # 这已经是轮询兜底给过的那次重试，又被吞 → 不再追（防循环）；
                # 对话由对方下一条消息自然带动。
                self.logger.info(
                    "[冷却补答] 重试仍被吞，放弃 chat=%s mid=%s reason=%s",
                    chat_id, message_id, reason or "-")
                return False
            margin = 3.0
            try:
                margin = float(pf.get("swallow_retry_margin_sec", 3.0) or 3.0)
            except (TypeError, ValueError):
                margin = 3.0
            expire_in = max(1.0, float(retry_after_sec or 0.0) + margin)
            if not self._msg_dedup.reschedule(chat_id, message_id, expire_in):
                return False
            self.logger.info(
                "[冷却补答] 已安排轮询兜底约 %.0fs 后补答 chat=%s mid=%s reason=%s",
                expire_in, chat_id, message_id, reason or "-")
            try:
                from src.client.gate_stats import bump as _gate_bump
                _gate_bump("swallow_deferred")
            except Exception:
                pass
            return True
        except Exception:
            self.logger.debug("[冷却补答] 调度失败（忽略）", exc_info=True)
            return False

    async def _bug_intake_backfill_loop(self):
        """B123（实施74）：报障群断线补拉循环。

        周期性拉报障群近端历史 × seen 账本差集 → 漏网消息回喂
        ``bug_intake.observe_group_message``（0827 凌晨断网期两条报障漏登记的
        catch-up 闭环）。核心在 ``src/ops/bug_intake_backfill.py``（纯函数 +
        账本），这里只做 pyrogram 适配；bug_intake 未启用/无群配置时循环即退。
        """
        try:
            from src.ops.bug_intake_backfill import (
                parse_backfill_cfg, run_backfill_once,
            )
        except Exception:
            return
        await asyncio.sleep(25.0)  # 启动错峰：让主链先起稳

        async def _fetch(chat_id: str, cap: int):
            rows = []
            try:
                cid: Any = int(chat_id)
            except (TypeError, ValueError):
                cid = chat_id
            async for m in self.client.get_chat_history(cid, limit=cap):
                try:
                    _fu = getattr(m, "from_user", None)
                    rows.append({
                        "id": int(getattr(m, "id", 0) or 0),
                        "text": str(getattr(m, "text", None)
                                    or getattr(m, "caption", None) or ""),
                        "reporter_id": str(getattr(_fu, "id", "") or ""),
                        "reporter_name": str(
                            getattr(_fu, "first_name", "") or ""),
                        "outgoing": bool(getattr(m, "outgoing", False)),
                        "has_media": bool(getattr(m, "media", None)),
                    })
                except Exception:
                    continue
            return rows

        while True:
            interval = 300
            try:
                cfg = self.config.config if hasattr(self.config, "config") \
                    else {}
                bf = parse_backfill_cfg(cfg)
                interval = int(bf.get("interval_sec") or 300)
                if not bf.get("enabled"):
                    return  # 未启用：整个循环退出（配置开启需重启，与其它循环同约定）
                summary = await run_backfill_once(
                    cfg, _fetch, account_id=str(self.account_id or ""))
                if summary.get("replayed"):
                    self.logger.info("[bug_backfill] 本轮补登记 %s 条（群 %s 个）",
                                     summary["replayed"], summary["groups"])
            except Exception:
                self.logger.debug("[bug_backfill] 轮次异常（忽略）", exc_info=True)
            await asyncio.sleep(max(60, interval))

    def _schedule_bug_intake_seq_sweep(self) -> None:
        """C1-②：登记到新洞 → 拉起一次（单飞）宽限期后的收割+补拉任务。"""
        task = getattr(self, "_bi_seq_sweep_task", None)
        if task is not None and not task.done():
            return
        try:
            self._bi_seq_sweep_task = asyncio.create_task(
                self._bug_intake_seq_gap_sweep())
        except Exception:
            self.logger.debug("[bug_intake] 序号哨兵收割任务创建失败", exc_info=True)

    async def _bug_intake_seq_gap_sweep(self) -> None:
        """C1-②（#123 族）：宽限期后收割仍未自愈的跳号洞 → 云端按 id 定点拉取 →
        拉到的真消息**重放进群消息 handler**（与实时到达同链：去重/触发/镜像/
        登记/媒体归档一个不少）；核实为空的号（已删/服务消息）只记账。

        循环直到本账号没有 pending 洞（一次跳号只等一个宽限期，连续跳号顺延）；
        任何异常吞掉只记日志，绝不影响主消息循环。
        """
        try:
            from src.ops.bug_intake import (
                due_seq_gaps, seq_gap_backfilled, seq_grace_sec,
                seq_sentinel_snapshot,
            )
            from src.integrations.protocol_bridge import fetch_tg_messages_by_ids
        except Exception:
            return
        acct = str(getattr(self, "account_id", "") or "default")
        handler = getattr(self, "_group_message_handler", None)
        for _round in range(50):            # 硬上限：绝不成为常驻循环
            await asyncio.sleep(float(seq_grace_sec()) + 1.0)
            try:
                cfg = (self.config.config
                       if hasattr(self.config, "config") else self.config)
                cfg = cfg if isinstance(cfg, dict) else {}
                due = due_seq_gaps(cfg, account_id=acct)
                for item in due:
                    chat_id = str(item.get("chat_id") or "")
                    ids = [int(i) for i in (item.get("missing_ids") or [])]
                    if not chat_id or not ids or self.client is None:
                        continue
                    try:
                        peer: Any = int(chat_id)
                    except (TypeError, ValueError):
                        peer = chat_id
                    filled = 0
                    note = ""
                    try:
                        got, empty = await fetch_tg_messages_by_ids(
                            self.client, peer, ids)
                    except Exception as exc:
                        got, empty = [], []
                        note = f"fetch_fail:{type(exc).__name__}"
                    for m in got:
                        try:
                            if handler is not None:
                                await handler(self.client, m)
                            filled += 1
                            self.logger.warning(
                                "[bug_intake] 序号哨兵 补回漏收消息 chat=%s mid=%s "
                                "from=%s text=%r", chat_id, getattr(m, "id", "?"),
                                getattr(getattr(m, "from_user", None), "id", "?"),
                                str(getattr(m, "text", None)
                                    or getattr(m, "caption", None) or "")[:80])
                        except Exception:
                            self.logger.warning(
                                "[bug_intake] 序号哨兵 重放 %s#%s 失败",
                                chat_id, getattr(m, "id", "?"), exc_info=True)
                    seq_gap_backfilled(chat_id, ids, filled, len(empty), note=note)
            except Exception:
                self.logger.debug("[bug_intake] 序号哨兵收割轮异常（忽略）",
                                  exc_info=True)
            try:
                snap = seq_sentinel_snapshot()
                if not any(int(v.get("pending") or 0) for k, v in snap.items()
                           if k.startswith(f"{acct}:")):
                    return
            except Exception:
                return

    async def _poll_inbound_loop(self):
        """轮询兜底主循环：定时拉取新进站私聊消息（补实时推送缺失）。

        config-gated：``telegram.poll_fallback.enabled``（默认开，dedup 保护下对实时正常的
        部署也安全——实时先处理、轮询命中去重即跳过）。``interval_seconds`` / ``dialogs_limit``
        可调。任何异常不退出循环、不影响实时路径。
        """
        try:
            tg_cfg = self.config.get_telegram_config()
        except Exception:
            tg_cfg = {}
        pf = (tg_cfg.get("poll_fallback") or {}) if isinstance(tg_cfg, dict) else {}
        if not pf.get("enabled", True):
            self.logger.info("[轮询兜底] 已禁用（telegram.poll_fallback.enabled=false）")
            return
        interval = float(pf.get("interval_seconds", 12) or 12)
        dlimit = int(pf.get("dialogs_limit", 30) or 30)
        catchup = float(pf.get("catchup_seconds", 600) or 0)
        self.logger.info(
            "[轮询兜底] 已启动 interval=%.0fs dialogs=%d catchup=%.0fs"
            "（RPC 拉新进站私聊，补实时推送缺失）",
            interval, dlimit, catchup,
        )
        await asyncio.sleep(interval)  # 首轮延迟，避开启动风暴
        _cycle = 0
        while self.running:
            try:
                scanned = await self._poll_inbound_once(dlimit, catchup)
                if _cycle == 0:
                    # 首轮确认：证明 get_dialogs 在 app 内可正常完成（不挂起）
                    self.logger.info("[轮询兜底] 首轮扫描完成 scanned=%d 会话", scanned)
                elif _cycle % 25 == 0:  # 约每 5 分钟一次心跳，确认循环存活
                    self.logger.info("[轮询兜底] 心跳 cycle=%d scanned=%d 会话", _cycle, scanned)
            except Exception as e:
                self.logger.warning("[轮询兜底] 本轮异常（已忽略，下轮继续）: %s", e)
            _cycle += 1
            await asyncio.sleep(interval)

    async def _poll_inbound_once(self, dlimit: int, catchup: float = 0.0) -> int:
        """单轮：扫描最近会话，挑出未处理过的新进站私聊消息并处理。返回扫描的会话数。

        时间闸门：消息时间在 boot 之后**或**距今 ``catchup`` 秒内（覆盖宕机/重启期间到达的
        未读消息），既能补回最近未读、又不回灌远古历史。已回复的会话 top_message 变 outgoing
        会被跳过，故不会重复回复。

        ``telegram.poll_fallback.mirror_outgoing``（默认关）开启后，outgoing 的 top_message
        不再只是被跳过，而是额外镜像进工作台（见 ``_mirror_outgoing_message``）——修
        「老板用手机亲自回了、工作台却仍显示未回复」导致坐席/AI 重复回复的事故。
        """
        if not self.client:
            return 0
        try:
            tg_cfg = self.config.get_telegram_config()
        except Exception:
            tg_cfg = {}
        if not (tg_cfg.get("process_private", True) if isinstance(tg_cfg, dict) else True):
            return 0
        _pf = (tg_cfg.get("poll_fallback") or {}) if isinstance(tg_cfg, dict) else {}
        # 每轮现读（config 热重载后下一轮即生效，无需重启）
        mirror_outgoing = bool(_pf.get("mirror_outgoing", False))
        processed = 0
        scanned = 0
        async for dialog in self.client.get_dialogs(limit=dlimit):
            scanned += 1
            try:
                chat = getattr(dialog, "chat", None)
                if chat is None:
                    continue
                ctype = getattr(chat, "type", None)
                ctype_name = (getattr(ctype, "name", None) or str(ctype or "")).upper()
                if "PRIVATE" not in ctype_name:  # 兜底只覆盖私聊（群有四层触发，避免误回）
                    continue
                msg = getattr(dialog, "top_message", None)
                if msg is None:
                    continue
                # M-1 C #219 对账兜底：本地有比服务端 top_message 更新的行 → 服务端已删
                # （己方手机端删了最新几条 / 实时删除更新丢失）→ 走同一删除同步入口。
                # 零额外 RPC（只读本地库），任何异常不影响轮询主流程。
                try:
                    self._reconcile_deleted_by_top(
                        getattr(chat, "id", 0),
                        getattr(msg, "id", 0) or getattr(msg, "message_id", 0))
                except Exception:
                    self.logger.debug("[轮询兜底] 删除对账失败（已忽略）", exc_info=True)
                if getattr(msg, "outgoing", False):
                    # 我们自己发的（含已回复）→ **绝不进 AI 管道**。flag 开时额外镜像
                    # 进工作台（老板/运营用手机 App 亲自回复的场景），随后照旧跳过。
                    if mirror_outgoing:
                        await self._mirror_outgoing_message(chat, msg, catchup)
                    continue
                from_user = getattr(msg, "from_user", None)
                if from_user and self.user_info and \
                        getattr(from_user, "id", None) == self.user_info.id:
                    continue
                if getattr(from_user, "is_bot", False):
                    continue
                # 屏蔽名单：与实时私聊/群路径同一助手。实时推送通道失效时本循环是
                # 私聊唯一入口，不在此处同判则名单在降级模式下形同虚设。
                # （tg_cfg 每轮现读，热生效口径与 mirror_outgoing 一致。）
                _no_reply = (tg_cfg.get("no_reply_sender_usernames") or []) if isinstance(tg_cfg, dict) else []
                if _username_blocked(_no_reply, getattr(from_user, "username", None)):
                    self.logger.debug(
                        "[轮询兜底] 跳过: 配置的不回复发送者 username=%s",
                        getattr(from_user, "username", None))
                    continue
                if not _has_ingestable_media(msg):
                    continue
                mdate = getattr(msg, "date", None)
                mts = mdate.timestamp() if (mdate and hasattr(mdate, "timestamp")) else 0
                if mts:
                    after_boot = mts >= self._boot_timestamp - 5
                    within_catchup = catchup > 0 and (time.time() - mts) <= catchup
                    if not (after_boot or within_catchup):  # 远古历史，防重启回灌
                        continue
                mid = getattr(msg, "id", 0) or getattr(msg, "message_id", 0)
                _cid = getattr(chat, "id", 0)
                if mid and self._msg_dedup.seen(_cid, mid):  # 与实时 handler 共用去重
                    continue
                # 水位闸（198 修复）：一条一直是 top_message 的未回复消息，靠 TTL
                # 去重挡不住周期性重处理（TTL 600s ≈ catchup 600s）。已处理水位
                # 不过期，命中即彻底跳过——同 peer 新消息 mid 更大不受影响。
                if mid and self._poll_watermark.already_processed(_cid, mid):
                    continue
                uid = str(getattr(from_user, "id", 0))
                if self._rate_limiter.enabled:
                    if self._rate_limiter.is_banned(uid):
                        _note_outbound_block("business", "rate_limit_banned")
                        continue
                    allowed, _reason = self._rate_limiter.allow(uid, _cid)
                    if not allowed:
                        _note_outbound_block("business", f"rate_limit_{_reason}")
                        continue
                # 处理前先 claim 去重（防并发/下一轮在回复落地前重入造成重复回复；
                # 与私聊实时 handler 抢同一把 claim——谁先到谁处理，另一路静默跳过）
                if not self._msg_dedup.claim(_cid, mid):
                    _m = _metrics()
                    if _m:
                        _m.record_dedup_blocked()
                    continue
                # claim 成功＝本条由轮询接手：立刻推进水位（幂等语义与 claim 一致
                # ——即便下方 _process_message 让位/异常，本条也不再被轮询重处理）
                self._poll_watermark.mark(_cid, mid)
                self.logger.info(
                    "[轮询兜底] 发现新进站私聊 chat=%s mid=%s text=%r",
                    getattr(chat, "id", ""), mid,
                    (getattr(msg, "text", None) or getattr(msg, "caption", None) or "")[:50],
                )
                await self._process_message(msg)
                processed += 1
            except Exception as e:
                self.logger.debug("[轮询兜底] 单会话处理失败（已忽略）: %s", e, exc_info=True)
        if processed:
            self.logger.info("[轮询兜底] 本轮处理 %d 条新进站私聊", processed)
        return scanned

    def _reconcile_deleted_by_top(self, chat_id: Any, top_id: Any) -> int:
        """轮询对账兜底（M-1 C #219）：见 ``find_deleted_by_top``。镜像关 / store 缺席 /
        top 未变 → 0。命中 → ``report_deleted_messages``（出站行标撤回、入站行软删、
        记忆/上下文剔除、SSE）+ INFO 留痕。返回处理条数。"""
        if not getattr(self, "_mirror_inbox", False) or not chat_id or not top_id:
            return 0
        try:
            top = int(top_id)
        except (TypeError, ValueError):
            return 0
        seen = getattr(self, "_delete_recon_top", None)
        if seen is None:
            seen = {}
            self._delete_recon_top = seen
        if seen.get(str(chat_id)) == top:
            return 0   # 该会话 top 未变，上一轮已对过账
        seen[str(chat_id)] = top
        if len(seen) > 4096:
            seen.clear()
        from src.integrations.protocol_bridge import (
            get_inbox_store, report_deleted_messages,
        )
        store = get_inbox_store()
        if store is None or not hasattr(store, "list_recent_messages"):
            return 0
        acct = str(getattr(self, "account_id", "default") or "default")
        cid = f"telegram:{acct}:{chat_id}"
        rows = store.list_recent_messages(cid, limit=20) or []
        stale = find_deleted_by_top(rows, top, now=time.time())
        if not stale:
            return 0
        self.logger.info(
            "[轮询兜底] 删除对账：chat=%s 服务端 top=%s，本地更新的 %d 条已不在服务端 → 同步撤回/删除 ids=%s",
            chat_id, top, len(stale), ",".join(stale[:8]))
        return int(report_deleted_messages("telegram", acct, stale, chat_key=str(chat_id)) or 0)

    def _is_system_chat(self, chat_id: Any) -> bool:
        """是否为「不属于任何客户」的系统会话：Saved Messages（自己和自己）/ Telegram 服务号。

        **刻意自包含**：不 import ``src/inbox/store.py`` 里的同类判定。那边服务于联系人
        列表的**展示**（这个 peer 要不要在坐席通讯录里露出），这里服务于消息管道的**安全**
        （要不要让 AI 对它开口）；两个关注点的判据将来完全可能分叉（展示层也许想把
        Saved Messages 当草稿箱入口留着，管道层永远不能）。为两个整数比较让实时 handler
        的热路径吃一次跨模块 import 不划算，故有意重复这几行。

        ``user_info`` 尚未就绪（登录中）时只判服务号——身份未知不是崩溃的理由，
        且此时本账号也还发不出消息，漏判窗口为空。

        「哪些 id 算系统号」读 ``store.TELEGRAM_SERVICE_CHAT_KEYS`` 单一事实源；
        但 Saved Messages 这条**刻意仍按 ``user_info.id`` 判、不改用 account_id**：
        单账号老配置里 ``account_id`` 可能是字面量 ``'default'``，那时 store 版规则
        对本客户端完全失效，而 ``user_info.id`` 恒为真实 TG user id。
        """
        try:
            cid = int(chat_id)
        except (TypeError, ValueError):
            return False
        if cid in TELEGRAM_SERVICE_CHAT_IDS:
            return True
        me_id = getattr(getattr(self, "user_info", None), "id", None)
        try:
            return me_id is not None and cid == int(me_id)
        except (TypeError, ValueError):
            return False

    def _should_skip_as_outbound(self, message: Any) -> Tuple[bool, bool]:
        """私聊实时 handler 的**第一道闸**：本条要不要整个跳过、跳过时要不要镜像。

        返回 ``(skip, mirror)``。之所以抽成不碰限流器/去重表/网络的独立方法，纯粹是为了
        可测——handler 本身是 ``_setup_handlers`` 里的装饰器闭包，测试拿不到句柄。

        **为什么需要**（2026-07-26 修的 P0 潜伏事故）：pyrogram 的 dispatcher 对
        ``UpdateNewMessage`` 零方向过滤（``Message._parse`` 只是把 MTProto 的
        ``message.out`` 原样落进 ``outgoing`` 字段），``filters.private`` 也只看
        ``chat.type``。于是老板用手机 App 回客户时，那条 outgoing 消息经多端同步推给本
        session、照样派发进这个 handler。此前私聊侧一道守卫都没有（群 handler 有双重
        守卫），后果两个且互相耦合：

          1. **安全**：我方自己的话被当客户消息走完整 AI 管道 → 镜像成 ``direction="in"``
             并生成一条回复发给客户（对着自己人的话回复）；
          2. **功能死锁**：``_mirror_outgoing_message`` 内部要 ``claim`` 同一个 mid，而
             实时 handler 已抢先 claim → 轮询侧那次镜像调用**必然失败**，「镜像手机已发
             消息」上线即死（线上实证：2452 条轮询兜底日志，镜像 0 条）。

        **这一改同时解决三件事**：安全（AI 永不处理我方消息）、功能（镜像从上线即死变为
        真正生效）、覆盖度（从「每轮轮询只镜像每会话最后一条」升级为「每条都镜像」——
        老板手机连发 3 条，3 条都进工作台，此前最多 1 条）。轮询侧的镜像调用保留不动，
        降级为真兜底（覆盖客户端断线期间漏收的消息），两条路共用同一把 ``_msg_dedup.claim``
        天然不会重复镜像。

        **三条位置约束**（调用点必须在这三样之前，顺序错了修复就废掉一半）：

          - 在**限流器之前**：否则我方自己的 uid 会白耗令牌桶，连发几条甚至触发
            ``check_auto_ban`` 把自己封了；
          - 在 ``_msg_dedup.claim`` **之前**：镜像方法内部自己 claim，外层先 claim 它就
            再也拿不到——这正是上面第 2 点的机制，别复刻；
          - 在 ``process_private`` 开关**之前**：「是否处理私聊」与「老板手机回复要不要在
            工作台可见」是正交的两件事，关掉私聊 AI 处理不代表坐席就不该看见老板回过了。

        **系统会话判在出站之前**（与需求给的顺序相反，此处刻意如此）：pyrogram 文档写明
        「发给自己的 Saved Messages 不算 outgoing」，但那是服务端 ``message.out`` 的行为，
        不同 layer/客户端并非铁板一块。万一哪天 Saved Messages 真带上 ``outgoing=True``，
        先判出站就会把**老板的私人笔记镜像进客户工作台**（隐私外泄）；先判系统会话则两种
        情况都只是静默 return，恒安全。对「AI 不得处理」这条红线而言两种顺序等价，
        故取更安全的这个。
        """
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        if self._is_system_chat(chat_id):
            # 既不处理也不镜像：收藏夹笔记 / 官方验证码都不是客户会话，落进工作台
            # 只会给坐席凭空造出一条假的「待回复」。
            self.logger.debug("[私聊] 跳过: 系统会话 chat=%s", chat_id)
            return True, False
        if not getattr(message, "outgoing", False):
            return False, False
        # 每次现读（对齐 ``_poll_inbound_once`` 的写法）：config 热重载后下一条消息即生效，
        # 无需重启。读失败 → 不镜像但**仍然跳过**：AI 红线优先于镜像功能。
        try:
            tg_cfg = self.config.get_telegram_config()
            pf = (tg_cfg.get("poll_fallback") or {}) if isinstance(tg_cfg, dict) else {}
            mirror = bool(pf.get("mirror_outgoing", False))
        except Exception:
            mirror = False
        return True, mirror

    async def _mirror_outgoing_message(self, chat: Any, msg: Any, catchup: float) -> bool:
        """把「本账号在手机 App 上亲自发出的消息」镜像进统一收件箱（``direction="out"``）。

        **为什么需要**：老板/运营常直接用手机回客户。轮询兜底原先见 ``outgoing`` 就跳过，
        工作台永远停在客户那条「未回复」上 → 坐席看到会再回一次，L2 autosend 也可能再发
        一次，同一客户收到两三条重复回复。镜像后工作台与手机同一份事实，重复回复消失。

        **绝不触发任何自动化**（本任务红线，三重保证，任一独立成立）：
          1. 本方法只调 ``_emit_inbox``（→ ``emit_incoming`` → inbox sink），**不**调
             ``maybe_auto_reply``、**不**调 ``_process_message``；
          2. sink 内 ``ingest_collected_chats`` 的 new_inbound 回调（auto-draft/System Z）、
             ``quick_analyze``、deep_persona 记忆累积、CSAT 检测**全部**在
             ``direction == "in"`` 分支内（见 src/inbox/ingest.py:210）；
          3. ``maybe_auto_reply`` 自身开头即 ``direction != "in" → return``。

        **去重实证**（2026-07-26 核实，代码级证据）：消息主键
        ``_message_pk = f"{conversation_id}:{platform_msg_id}"``（src/inbox/store.py:655），
        Telegram 的 ``platform_msg_id`` 由 ``extract_platform_msg_id`` 从 ``source.id``
        取，即 MTProto ``message.id``。系统自身发 Telegram 的**每条**出站镜像都带真实
        ``message.id``（编排器 protocol worker → ``send_message`` 返回 ``msg.id``；
        A 线 ``_postsend_mirror_and_record`` → ``_sent.id``；companion worker →
        ``send_message_return_id``），与本方法用的 ``message.id`` 同值 → 主键相同 →
        ``INSERT OR IGNORE`` 无行插入 → 既不多气泡、也不重复发 ``outbound_message`` 事件。
        万一某条发送侧镜像丢了 id（落 ``:h:<hash>`` 兜底键），store 的出站分支会在带 pmid
        的行落库时**删掉** 120s 窗口内同文本的 hash 孪生（store.py:996），仍收敛为一条。
        故 ``mid`` 为空时本方法**直接放弃镜像**——宁可少一条气泡，也不制造重复气泡。

        **只做私聊**：调用点位于 ``_poll_inbound_once`` 的 PRIVATE 过滤之后。群里自己发的
        话噪音大（群本就有四层触发、坐席主要在私聊线作业），且群 top_message 频繁翻动会
        持续刷镜像；先只覆盖私聊这条真正会造成「重复回复事故」的线。

        **媒体本体归档**（2026-08-04，修「手机发图坐席只见『[图片]』占位」）：
        ``telegram.poll_fallback.mirror_outgoing_media``（默认关）开启后，镜像行不再是
        纯文字占位——``media_type`` 恒结构化落库（前端渲染形态卡），且对策略放行的类型
        （默认 image/sticker/voice，单条 ≤5MB）经 ``download_tg_media`` 把本体归档进
        ``/static/protocol_media``，坐席直接看图/回放。下载有硬超时（30s）且任何失败只
        退化为「无归档的媒体行」（ref 空），消息本身绝不丢；成败计入
        ``outbound_mirror_stats``（ops「📤 出站媒体归档」卡可见）。开关关闭时保持旧行为
        （占位文字、零下载 RPC）——历史上「刻意不下载」的风控/流量顾虑由该开关承接，
        由部署方按账号风险偏好决定。

        返回是否真的镜像了一条。best-effort：任何异常吞掉，绝不影响轮询主循环。
        """
        try:
            if not _has_ingestable_media(msg):
                return False
            # 时间闸门：与入站同一判据，防重启后把几年前的老消息回灌进工作台
            mdate = getattr(msg, "date", None)
            mts = mdate.timestamp() if (mdate and hasattr(mdate, "timestamp")) else 0.0
            if mts:
                after_boot = mts >= self._boot_timestamp - 5
                within_catchup = catchup > 0 and (time.time() - mts) <= catchup
                if not (after_boot or within_catchup):
                    return False
            mid = getattr(msg, "id", 0) or getattr(msg, "message_id", 0)
            if not mid:
                return False  # 无 message.id ⇒ 去重不成立，见 docstring
            _cid = getattr(chat, "id", 0)
            # 与入站共用同一把 claim：Telegram 同会话内收发共用一套 message.id 序号空间，
            # 故出站 mid 绝不会与入站 mid 相撞。claim 让同一条消息在后续每 12s 一轮的
            # 轮询里不再重复走镜像（store 侧本就幂等，这里是省 I/O 的前置闸）。
            if not self._msg_dedup.claim(_cid, mid):
                return False
            text = _normalize_message_text(
                getattr(msg, "text", None) or getattr(msg, "caption", None) or "")
            from src.integrations.protocol_bridge import (
                download_tg_media, media_placeholder, tg_media_meta, tg_peer_identity,
            )
            _mt = ""
            _mr = ""
            meta = tg_media_meta(msg)
            if meta:
                kind = meta[0]
                # 媒体归档策略每次现读（与 mirror_outgoing 同口径：热重载后下一条即生效）
                try:
                    _tg_cfg = self.config.get_telegram_config()
                    _pf = (_tg_cfg.get("poll_fallback") or {}) \
                        if isinstance(_tg_cfg, dict) else {}
                except Exception:
                    _pf = {}
                m_on, m_max_bytes, m_kinds = parse_mirror_outgoing_media_cfg(_pf)
                if m_on:
                    # 结构化媒体行：media_type 恒落库（前端渲染形态卡、会话预览自动补
                    # 「[图片]」标记）；本体只对策略放行的类型真下载，超时/失败/超限
                    # 一律退化为 ref 空的媒体行——消息不丢，缺口进观测。
                    _mt = kind
                    if kind in m_kinds:
                        try:
                            _dt, _dr = await asyncio.wait_for(
                                download_tg_media(
                                    msg, self.account_id, max_bytes=m_max_bytes),
                                timeout=_MIRROR_MEDIA_DL_TIMEOUT_SEC)
                            if _dt:
                                _mt = _dt
                            _mr = _dr or ""
                        except Exception:
                            _mr = ""
                        try:
                            from src.integrations.outbound_mirror_stats import (
                                get_outbound_mirror_stats,
                            )
                            get_outbound_mirror_stats().record_publish(
                                "telegram", kind, ok=bool(_mr))
                        except Exception:
                            pass
                elif not text:
                    # 旧行为（媒体归档未开）：无正文媒体落「[图片]」占位（与历史同步
                    # history_message_obj 同口径），坐席至少知道「回过了」。
                    text = media_placeholder(kind)
            if not (text or _mt):
                return False
            ident = tg_peer_identity(chat)
            self._emit_inbox(
                chat_id=_cid, text=text, direction="out",
                name=ident.get("name") or "",
                msg_id=str(mid),
                media_type=_mt, media_ref=_mr,
                username=ident.get("username") or "",
                phone=ident.get("phone") or "",
                ts=mts or None,
            )
            self.logger.info(
                "[轮询兜底] 镜像手机已发消息 chat=%s mid=%s media=%s ref=%s text=%r",
                _cid, mid, _mt or "-", "y" if _mr else "-", text[:50],
            )
            return True
        except Exception as e:
            self.logger.debug("[轮询兜底] 出站镜像失败（已忽略）: %s", e, exc_info=True)
            return False

    async def _directory_sync_loop(self):
        """目录同步主循环：定时把好友名单 / 云端会话列表同步进通讯录与会话占位。

        config-gated：``platform_login.telegram.sync.enabled``（默认关，新子系统约定）。
        与 ``_poll_inbound_loop`` 同构——首轮延迟避开启动风暴、异常吞掉不退出循环。
        首轮刻意只等 ``first_delay_seconds``（默认 45s）而非整个 6 小时周期：
        重启后坐席马上就该看到全量名单，不该等到下一个整周期。

        ⚠ 只写 ``protocol_contacts`` 与会话占位两张表，绝不喂消息管道
        （不触发自动回复 / 不进记忆 / 不算新消息），详见模块 docstring 的红线说明。
        """
        from src.integrations.telegram_directory_sync import (
            directory_sync_cfg, sync_directory_once, sync_enabled,
        )
        try:
            cfg = directory_sync_cfg(self.config)
        except Exception:
            cfg = {}
        if not sync_enabled(cfg):
            return  # 默认关：静默返回，不给日志添噪
        interval = float(cfg.get("interval_seconds", 21600) or 21600)
        first_delay = float(cfg.get("first_delay_seconds", 45) or 45)
        self.logger.info(
            "[目录同步] 已启动 interval=%.0fs first_delay=%.0fs"
            "（好友名单 + 会话占位，只读 RPC 不产生消息）",
            interval, first_delay,
        )
        await asyncio.sleep(first_delay)  # 首轮延迟，避开启动风暴
        while self.running:
            try:
                stats = await sync_directory_once(self.client, self.account_id, cfg)
                self.logger.info("[目录同步] 本轮完成 通讯录=%d 会话占位=%d",
                                 stats.get("contacts", 0), stats.get("chats", 0))
            except Exception as e:
                self.logger.warning("[目录同步] 本轮异常（已忽略，下轮继续）: %s", e)
            await asyncio.sleep(interval)

    async def _process_message(self, message: Message):
        """处理接收到的消息"""
        try:
            # 获取消息信息
            user_id = message.from_user.id if message.from_user else 0
            username = message.from_user.username if message.from_user else "unknown"
            chat_title = message.chat.title if hasattr(message.chat, 'title') and message.chat.title else "私聊"

            # 获取消息文本（包括caption）；P2 编码防护：统一归一化为安全 str
            raw_text = getattr(message, "text", None) or getattr(message, "caption", None)
            text = _normalize_message_text(raw_text) if raw_text else ""
            image_ocr_text = None  # 带图时的 OCR 结果，会传给 AI 以保持话术与图一致
            voice_media_ref = ""   # P1-2 入站语音归档 URL（可回放原音；空=未归档）

            # 处理语音消息
            _peer_audio_emotion = None      # 声学情绪（SER），供出站情感声 + 情绪落库融合
            _voice_asr_suspect = ""         # ASR P1：转写可疑原因码（空=正常）
            if not text and (message.voice or message.audio):
                if self.voice_transcriber:
                    try:
                        self.logger.info(f"收到语音消息 [{chat_title}/{username}]，开始转录...")
                        daily_stats.bump("voice_in")

                        # 1. 下载语音文件
                        voice_file = await self._download_voice_file(message)
                        if not voice_file:
                            text = "[语音消息 - 下载失败]"
                            self.logger.error("语音文件下载失败")
                        else:
                            # 1b. 入站语音留档（P1-2，2026-08-02）：转录**之前**先归档到
                            # /static（与出站镜像同目录、同生命周期）——转录失败时坐席更
                            # 需要能亲耳听原音（质检/纠错的证据链）。归档失败不阻塞转录，
                            # 媒体行退化成仅转写 + 「无音频存档」灰标；成败计入
                            # outbound_mirror_stats（归档链共用观测）。
                            try:
                                from src.integrations.protocol_bridge import (
                                    publish_outbound_media,
                                )
                                _vurl, _ = publish_outbound_media(
                                    "telegram",
                                    getattr(self, "account_id", "default"),
                                    str(voice_file))
                                voice_media_ref = _vurl or ""
                            except Exception:
                                self.logger.debug("入站语音归档失败（忽略）", exc_info=True)
                            try:
                                # 2. 调用转录服务
                                language = self.config.get('voice_recognition', {}).get('language', 'zh')
                                # ASR P0/P1：会话语种先验（镜像收件箱的最近入站文字多数语种）
                                # 只做「检出语种冲突且置信不足 → 按先验重转一次」，算不出 → None。
                                _vhint = None
                                try:
                                    if getattr(self, "_mirror_inbox", False):
                                        from src.inbox.asr_lang_hint import conversation_asr_lang_hint
                                        from src.inbox.normalizer import conv_id as _vh_conv_id
                                        from src.integrations.protocol_bridge import get_inbox_store as _vh_store
                                        _vh_ibx = _vh_store()
                                        if _vh_ibx is not None:
                                            _vhint = conversation_asr_lang_hint(
                                                _vh_ibx, _vh_conv_id(
                                                    "telegram",
                                                    str(getattr(self, "account_id", "default") or "default"),
                                                    str(message.chat.id)))
                                except Exception:
                                    _vhint = None
                                try:
                                    transcribed_text = await self.voice_transcriber.transcribe_voice_message(
                                        str(voice_file), language, lang_hint=_vhint
                                    )
                                except TypeError:
                                    transcribed_text = await self.voice_transcriber.transcribe_voice_message(
                                        str(voice_file), language
                                    )

                                if transcribed_text:
                                    text = f"[语音转录] {transcribed_text}"
                                    self.logger.info(f"语音转录成功: {transcribed_text[:100]}...")
                                    # ASR P1：按转写元数据判「可疑」→ 原因码随 context 进
                                    # skill_manager（_voice_asr_suspect），prompt 走「先确认」块。
                                    try:
                                        from src.inbox.asr_suspect import (
                                            hint_enabled as _asr_hint_on,
                                            transcript_suspect as _asr_suspect,
                                        )
                                        _cfg_root_v = (self.config.config
                                                       if hasattr(self.config, "config") else {})
                                        if _asr_hint_on(_cfg_root_v):
                                            _voice_asr_suspect = _asr_suspect(
                                                getattr(self.voice_transcriber, "last_meta", {}) or {},
                                                transcribed_text)
                                            if _voice_asr_suspect:
                                                self.logger.info(
                                                    "[asr] 转写可疑 reason=%s → 回复先确认",
                                                    _voice_asr_suspect)
                                    except Exception:
                                        _voice_asr_suspect = ""
                                else:
                                    text = "[语音消息 - 转录失败或无内容]"
                                    self.logger.warning("语音转录返回空结果")

                                # 2b. 音频情绪识别（SER）：从声学语气听出情绪（best-effort，
                                # 软降级；即使转录失败也能识别，纯情绪信号）。绝不阻塞主链路。
                                try:
                                    _se_cfg = (
                                        self.config.get('speech_emotion', {})
                                        if hasattr(self.config, 'get') else {}
                                    )
                                    if _se_cfg.get('enabled'):
                                        from src.ai.speech_emotion import (
                                            get_speech_emotion_recognizer)
                                        from src.ai.speech_emotion_stats import (
                                            get_speech_emotion_stats)
                                        _ser = get_speech_emotion_recognizer(_se_cfg)
                                        _res = await _ser.recognize_async(str(voice_file))
                                        _min_conf = float(
                                            _se_cfg.get('min_confidence', 0.5) or 0.5)
                                        _peer_audio_emotion = _res.as_emotion_dict(
                                            min_confidence=_min_conf)
                                        get_speech_emotion_stats().record(
                                            ok=_res.ok, emotion=_res.emotion,
                                            confident=bool(
                                                _peer_audio_emotion
                                                and _peer_audio_emotion.get('confident')),
                                            remote=str(_res.model or '')
                                            .startswith('remote:'),
                                        )
                                        if _peer_audio_emotion and _peer_audio_emotion.get(
                                                'confident'):
                                            self.logger.info(
                                                "[speech_emotion] 声学情绪: %s score=%.2f",
                                                _peer_audio_emotion.get('raw_label'),
                                                _peer_audio_emotion.get('score') or 0.0)
                                except Exception:
                                    self.logger.debug(
                                        "音频情绪识别失败（忽略）", exc_info=True)

                                # 3. 清理临时文件
                                try:
                                    voice_file.unlink(missing_ok=True)
                                    self.logger.debug(f"已清理临时文件: {voice_file}")
                                except Exception as cleanup_error:
                                    self.logger.warning(f"清理临时文件失败: {cleanup_error}")

                            except Exception as transcribe_error:
                                self.logger.error(f"语音转录过程失败: {transcribe_error}")
                                text = "[语音消息 - 转录过程错误]"
                                # 清理临时文件
                                try:
                                    voice_file.unlink(missing_ok=True)
                                except Exception:
                                    pass

                    except Exception as e:
                        self.logger.error(f"处理语音消息失败: {e}")
                        text = "[语音消息 - 处理异常]"
                else:
                    self.logger.info(f"收到语音消息 [{chat_title}/{username}]（语音识别未启用或依赖未安装）")
                    daily_stats.bump("voice_in")
                    text = "[语音消息 - 识别功能未启用]"
                    # 2026-08-20 内测实录「新版本语音消息无法接收」：识别不可用时
                    # 旧实现不落音频归档（归档只在转录分支）——坐席只看到占位文字，
                    # 原音听不了，体感＝「语音收不到」。现在同管道归档，最坏也能
                    # 人工听原音；失败不阻塞（占位文字照常入库）。
                    try:
                        _vf = await self._download_voice_file(message)
                        if _vf:
                            try:
                                from src.integrations.protocol_bridge import (
                                    publish_outbound_media,
                                )
                                _vurl, _ = publish_outbound_media(
                                    "telegram",
                                    getattr(self, "account_id", "default"),
                                    str(_vf))
                                voice_media_ref = _vurl or ""
                            finally:
                                try:
                                    _vf.unlink(missing_ok=True)
                                except Exception:
                                    pass
                    except Exception:
                        self.logger.debug(
                            "识别未启用分支的语音归档失败（忽略）", exc_info=True)

            # 有说明文字但带图时：Vision 单轨，失败不回落 OCR
            has_image = bool(message.photo or (message.document and message.document.mime_type and
                               message.document.mime_type.startswith('image/')))
            if text and has_image and self._vision_usable():
                try:
                    self.logger.info(f"收到带图消息 [{chat_title}/{username}]，解析图中内容（Vision）...")
                    image_file = await self._download_image_file(message)
                    if image_file:
                        try:
                            image_ocr_text = await self._get_image_content(str(image_file))
                            if image_ocr_text:
                                image_ocr_text = _normalize_message_text(image_ocr_text)
                                self.logger.info(f"带图消息解析成功: {image_ocr_text[:80]}...")
                            try:
                                image_file.unlink(missing_ok=True)
                            except Exception:
                                pass
                        except Exception as e:
                            self.logger.warning(f"带图消息解析异常: {e}")
                except Exception as e:
                    self.logger.warning(f"带图消息下载/解析异常: {e}")

            # 处理图片消息（如果没有文本且不是语音消息）— Vision 单轨
            if not text and (message.photo or message.document):
                if self._vision_usable():
                    try:
                        self.logger.info(f"收到图片消息 [{chat_title}/{username}]，解析图中内容（Vision）...")
                        image_file = await self._download_image_file(message)
                        if not image_file:
                            text = "[图片消息 - 下载失败]"
                            self.logger.error("图片文件下载失败")
                        else:
                            try:
                                content = await self._get_image_content(str(image_file))
                                if content:
                                    content = _normalize_message_text(content)
                                    text = f"[图片内容] {content}"
                                    image_ocr_text = content
                                    self.logger.info(f"图片解析成功: {content[:100]}...")
                                else:
                                    text = "[图片消息 - 解析失败或无文字]"
                                    self.logger.warning("图片解析返回空结果")
                                try:
                                    image_file.unlink(missing_ok=True)
                                except Exception as cleanup_error:
                                    self.logger.warning(f"清理临时文件失败: {cleanup_error}")
                            except Exception as recognize_error:
                                self.logger.error(f"图片解析过程失败: {recognize_error}")
                                text = "[图片消息 - 解析过程错误]"
                                try:
                                    image_file.unlink(missing_ok=True)
                                except Exception:
                                    pass
                    except Exception as e:
                        self.logger.error(f"处理图片消息失败: {e}")
                        text = "[图片消息 - 处理异常]"
                else:
                    self.logger.info(f"收到图片消息 [{chat_title}/{username}]（Vision 未启用）")
                    text = "[图片消息 - 识别功能未启用]"

            # 处理视频 / 视频圆点 / GIF：抽帧+音轨（含带 caption 的视频，Phase5）
            if (
                getattr(message, "video", None)
                or getattr(message, "video_note", None)
                or getattr(message, "animation", None)
            ):
                from src.ai.inbound_video import compose_video_inbound_text
                _caption = text
                if self._vision_usable() or self.voice_transcriber:
                    try:
                        self.logger.info(
                            f"收到视频消息 [{chat_title}/{username}]，理解画面+音轨（Vision+ASR）...")
                        video_file = await self._download_video_file(message)
                        if not video_file:
                            text = compose_video_inbound_text(caption=_caption, video_desc="")
                            self.logger.info("视频未下载（超限/失败），落占位")
                        else:
                            try:
                                vcontent = await self._get_video_content(str(video_file))
                                text = compose_video_inbound_text(
                                    caption=_caption,
                                    video_desc=_normalize_message_text(vcontent) if vcontent else "",
                                )
                                if vcontent:
                                    image_ocr_text = vcontent
                                    self.logger.info(f"视频解析成功: {vcontent[:100]}...")
                                elif not _caption:
                                    self.logger.warning("视频解析无结果，落占位")
                            finally:
                                try:
                                    video_file.unlink(missing_ok=True)
                                except Exception:
                                    pass
                    except Exception as e:
                        self.logger.error(f"处理视频消息失败: {e}")
                        text = compose_video_inbound_text(caption=_caption, video_desc="")
                elif not _caption:
                    text = "[视频]"

            # 表情/贴纸（无文本）：读关联 emoji（零成本情绪线索）+ 静态贴纸 Vision 看图形
            if not text and getattr(message, "sticker", None):
                try:
                    text = await self._get_sticker_content(message)
                except Exception as e:
                    self.logger.warning(f"贴纸解析失败: {e}")
                    text = "[表情]"
            elif not text and getattr(message, "animation", None):
                text = "[动态表情]"

            # 入站文本 emoji 语义标注（媒体派生文本已带前缀，不重复处理）
            if text and not text.lstrip().startswith(
                ("[图片", "[视频", "[表情", "[语音", "[贴纸", "[动态表情", "[文件")
            ):
                text = self._annotate_inbound_emoji(text)

            if text:
                # #162（2026-09-04）：同机多账号时两个号收到同一句话只按用户名记日志，
                # 分不出「各收一次」还是「串号」——补 account/conv（conv 与 B 线收件箱
                # 会话 id 同口径：platform:account:chat_id，可直接对上 protocol_media 目录）
                _acct = str(getattr(self, "account_id", "default") or "default")
                try:
                    from src.inbox.normalizer import conv_id as _mk_cid
                    _cid = _mk_cid("telegram", _acct, str(message.chat.id))
                except Exception:
                    _cid = f"telegram:{_acct}:{message.chat.id}"
                self.logger.info(
                    f"收到消息 [{chat_title}/{username}] account={_acct} conv={_cid}: "
                    f"{self._log_safe_text(text)}")
                daily_stats.bump("messages")
                m = _metrics()
                if m:
                    m.record_message_received()
                    m.set_queue_size(self.message_queue.qsize() + 1)
                # 引用消息透传（2026-08-20 内测实锤「引用+『分析』」全链不可见）：
                # 提取被引用消息摘要，供 ①LLM 提示块 ②报障分类 ③镜像 reply_to 列
                _quoted = {"text": "", "sender": "", "is_me": False}
                try:
                    from src.client.quoted_context import extract_quoted
                    _quoted = extract_quoted(
                        message, getattr(self.user_info, "id", None)
                        if getattr(self, "user_info", None) else None)
                except Exception:
                    pass
                msg_data = {
                    'message': message,
                    'user_id': user_id,
                    'username': username,
                    'text': _normalize_message_text(text),
                    'chat_id': message.chat.id,
                    'image_ocr_text': image_ocr_text,
                    '_trigger_path': getattr(message, '_trigger_path', None),
                    '_is_voice_msg': bool(message.voice or message.audio),
                    '_peer_audio_emotion': _peer_audio_emotion,
                    '_voice_asr_suspect': _voice_asr_suspect,
                    'quoted': _quoted,
                    # P1-2：入站语音归档 URL（转录前已发布），异步段透传进镜像媒体行
                    'voice_media_ref': voice_media_ref,
                    # 插话吸收（interject_absorb）：入队时刻——出队合并的年龄判据
                    # + 发送前过期关口的本消息基准时间
                    '_enq_ts': time.time(),
                }
                try:
                    self.message_queue.put_nowait(msg_data)
                    # 插话吸收：登记该会话最新入站时间（仅入队成功才记——队满
                    # 丢弃的消息永远不会被处理，据它中止在途回复会让客户两头落空）
                    # + 未答文本登记（生成前安静窗的合并原料；仅纯文本类会入表）
                    try:
                        from src.client.interject_absorb import (
                            note_inbound, note_inbound_text)
                        note_inbound(message.chat.id, msg_data['_enq_ts'])
                        note_inbound_text(
                            message.chat.id, msg_data['_enq_ts'],
                            msg_data.get('text'))
                    except Exception:
                        pass
                except asyncio.QueueFull:
                    self.logger.warning("消息队列已满 (%d)，丢弃消息: %s/%s",
                                        self.message_queue.maxsize, chat_title, username)
                    m2 = _metrics()
                    if m2:
                        m2.record_queue_drop()
            else:
                self.logger.debug(f"忽略非文本/语音消息: {chat_title}/{username}")

        except Exception as e:
            self.logger.error(f"处理消息失败: {e}")
            m = _metrics()
            if m:
                m.record_error()

    async def _message_processor(self):
        """消息处理任务"""
        self.logger.info("消息处理器已启动")

        while self.running:
            try:
                message_data = await self.message_queue.get()
                self.config.check_and_hot_reload()
                # ── 插话吸收（interject_absorb，默认关）：客户连发的短消息在
                # 出队口吸收成一条输入，一次生成覆盖多条（防「两条各回一条」
                # 自说自话）。仅私聊、纯文本类；只看**队头紧随**的同 chat 消息
                # （peek 不命中即停，绝不打乱其它会话顺序）。任何异常回退单条。──
                try:
                    from src.client.interject_absorb import (
                        can_merge_queued as _ij_can,
                        merge_texts as _ij_merge,
                        parse_interject_cfg as _ij_cfg_fn,
                    )
                    _ij_cfg = _ij_cfg_fn(
                        self.config.config
                        if hasattr(self.config, "config") else {})
                    if _ij_cfg["enabled"] and self.message_queue.qsize() > 0:
                        from src.inbox.reply_split import (
                            looks_like_group_chat as _ij_grp,
                        )
                        _ij_chat = message_data.get('chat_id')
                        _ij_now = time.time()
                        if (not _ij_grp("telegram", str(_ij_chat))
                                and _ij_can(
                                    message_data, chat_id=_ij_chat,
                                    now=_ij_now,
                                    max_age_sec=_ij_cfg["max_age_sec"])):
                            _ij_texts = [message_data.get('text') or ""]
                            while len(_ij_texts) < int(_ij_cfg["max_merge"]):
                                try:
                                    _ij_head = self.message_queue._queue[0]
                                except Exception:
                                    break   # 队空/内部结构不可用 → 停止吸收
                                if not _ij_can(
                                        _ij_head, chat_id=_ij_chat,
                                        now=_ij_now,
                                        max_age_sec=_ij_cfg["max_age_sec"]):
                                    break
                                try:
                                    _ij_next = self.message_queue.get_nowait()
                                except Exception:
                                    break
                                self.message_queue.task_done()
                                _ij_texts.append(_ij_next.get('text') or "")
                                # 锚点采最新一条：回复引用/入队时间戳随最新，
                                # 防发送前过期关口把合并稿误判成旧稿
                                if _ij_next.get('message') is not None:
                                    message_data['message'] = _ij_next['message']
                                if _ij_next.get('_enq_ts'):
                                    message_data['_enq_ts'] = _ij_next['_enq_ts']
                                message_data['_is_voice_msg'] = bool(
                                    message_data.get('_is_voice_msg')
                                    or _ij_next.get('_is_voice_msg'))
                                if _ij_next.get('_peer_audio_emotion'):
                                    message_data['_peer_audio_emotion'] = (
                                        _ij_next.get('_peer_audio_emotion'))
                                # ASR P1：合并进来的任一条语音转写可疑 → 整批按可疑（先确认）
                                if _ij_next.get('_voice_asr_suspect'):
                                    message_data['_voice_asr_suspect'] = (
                                        _ij_next.get('_voice_asr_suspect'))
                            if len(_ij_texts) > 1:
                                message_data['text'] = _ij_merge(_ij_texts)
                                self.logger.info(
                                    "[interject] 出队合并 %d 条连发消息 chat=%s",
                                    len(_ij_texts), _ij_chat)
                except Exception:
                    self.logger.debug(
                        "[interject] 出队合并异常（按单条处理）", exc_info=True)
                m = _metrics()
                if m:
                    m.set_queue_size(self.message_queue.qsize())
                asyncio.create_task(self._guarded_process(message_data))
                self.message_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"消息处理器错误: {e}")
                m = _metrics()
                if m:
                    m.record_error()

    async def _guarded_process(self, message_data: Dict[str, Any]):
        """使用 per-chat 锁 + Semaphore 控制并发的消息处理包装器。

        per-chat 锁在信号量**之外**获取：同一会话的消息串行生成（后一条能看到
        前一条的回复上下文，根治并行生成互不可见 → 前后事实矛盾），排队等待时
        不占并发槽；不同会话仍由信号量并行。
        """
        async with self._chat_locks.lock(message_data.get('chat_id')):
            async with self._process_semaphore:
                self._active_tasks += 1
                m = _metrics()
                if m:
                    m.set_active_tasks(self._active_tasks, self._max_concurrent)
                try:
                    await self._process_message_async(message_data)
                finally:
                    self._active_tasks -= 1
                    if m:
                        m.set_active_tasks(self._active_tasks, self._max_concurrent)

    def _emit_inbox(self, *, chat_id: Any, text: str, direction: str,
                    name: str = "", msg_id: str = "",
                    media_type: str = "", media_ref: str = "",
                    username: str = "", phone: str = "",
                    sender_id: str = "", sender_name: str = "",
                    ts: Optional[float] = None, chat_type: str = "") -> None:
        """N4b：companion 运行时把 A 线收/发的消息镜像进统一收件箱（坐席台可见）。

        默认关（``self._mirror_inbox`` False）→ standalone main.py 零影响。仅 emit 到
        收件箱 sink（不触发 B 线 autoreply，避免与 A 线自身回复重复）。best-effort，
        绝不影响主消息流。

        ``sender_id`` / ``sender_name``：群消息发言人（P4-11E 同款结构化落库，经
        ``source`` 透传 → ``messages.sender_id/sender_name``），供坐席群气泡显示
        发言人名 + 稳定色、群观测按发言者聚合。私聊调用方不传（保持空）。

        ``ts``：消息真实发生时间（epoch 秒）。缺省 None＝落 ``time.time()``（实时
        路径「此刻」即消息时刻）。轮询兜底镜像手机已发消息时会传 ``message.date``，
        否则会话 last_ts 被抬到「发现时刻」而与客户入站消息错序。

        ``chat_type``：显式会话类型（``group`` / ``channel``），经 ``source`` 透传给
        ``infer_chat_type``。缺省空＝沿用 normalizer 的负数 chat_id 启发式（旧调用方不变）。
        """
        if not getattr(self, "_mirror_inbox", False):
            return
        try:
            from src.integrations.protocol_bridge import emit_incoming, make_message
            _src = None
            if sender_id or sender_name:
                _src = {"sender_id": str(sender_id or ""),
                        "sender_name": str(sender_name or "")}
            if chat_type:
                _src = dict(_src or {})
                _src["chat_type"] = str(chat_type)
            emit_incoming(make_message(
                platform="telegram",
                account_id=getattr(self, "account_id", "default"),
                chat_key=str(chat_id),
                name=name or "",
                text=text or "",
                ts=float(ts) if ts else time.time(),
                msg_id=str(msg_id or ""),
                direction=direction,
                media_type=media_type,
                media_ref=media_ref,
                username=username or "",
                phone=phone or "",
                source=_src,
            ))
        except Exception:
            try:
                self.logger.debug("[mirror] 收件箱镜像失败", exc_info=True)
            except Exception:
                pass

    def _mirror_untriggered_group_message(self, message: Any) -> None:
        """未触发自动回复的群消息 → 只镜像进统一收件箱（不回复、不起草、不进 SLA）。

        2026-09-18 P6 社群舞台实锤：群里刚发的询价在工作台「群组」视图里看不到——
        实时 handler 在触发裁决处整条 return，未触发消息从未落库，只能等
        ``tg_history_autosync`` / 手动 ``from_latest`` 补拉，画面滞后几十秒到几分钟。
        「不自动回复」与「不让坐席看见」是两件事，这里补上后者：

        - 文本 / caption 原样；纯媒体**不下载本体**（群里图/视频体量大、风控敏感），
          只落 ``media_type`` + 占位文——与 ``history_message_obj`` 历史同步同口径，
          工作台按形态卡渲染，原件走「拉取原件」按需补。
        - ``msg_id`` 随行 → 与后续历史同步产出同一去重主键，不落重复行。
        - ``name`` 用**群名**（不是发言人名，否则会把群会话改名成发言人）；发言人走
          ``sender_id`` / ``sender_name`` 结构化字段；``chat_type=group`` 显式带。
        - 开关 ``telegram.group_reply.mirror_untriggered``（缺省开）；standalone
          （``_mirror_inbox`` 关）时 ``_emit_inbox`` 自身 no-op。
        best-effort：任何异常只记 debug，绝不影响群 handler。
        """
        try:
            if not getattr(self, "_mirror_inbox", False):
                return
            _gr = self.config.get('telegram', {}).get('group_reply', {}) or {}
            if not bool(_gr.get('mirror_untriggered', True)):
                return
            chat = getattr(message, 'chat', None)
            chat_id = getattr(chat, 'id', None)
            if chat_id is None:
                return
            from src.integrations.protocol_bridge import media_placeholder, tg_media_meta
            raw_text = getattr(message, 'text', None) or getattr(message, 'caption', None)
            text = _normalize_message_text(raw_text) if raw_text else ""
            _meta = tg_media_meta(message)
            media_type = _meta[0] if _meta else ""
            if not text and media_type:
                text = media_placeholder(media_type)
            if not text:
                return
            peer = getattr(message, 'from_user', None)
            sender_id = ""
            sender_name = ""
            if peer is not None:
                sender_name = ((getattr(peer, 'first_name', '') or '') + ' '
                               + (getattr(peer, 'last_name', '') or '')).strip()
                _uname = str(getattr(peer, 'username', '') or '').lstrip('@')
                if not sender_name and _uname:
                    sender_name = '@' + _uname
                sender_id = str(getattr(peer, 'id', '') or '')
            _date = getattr(message, 'date', None)
            _ts = _date.timestamp() if hasattr(_date, 'timestamp') else None
            self._emit_inbox(
                chat_id=chat_id, text=text, direction="in",
                name=str(getattr(chat, 'title', '') or ''),
                msg_id=str(getattr(message, 'id', '') or ''),
                media_type=media_type,
                sender_id=sender_id, sender_name=sender_name,
                ts=_ts, chat_type="group",
            )
            self.logger.info(
                "[群组监控] 未触发消息已镜像进收件箱 chat=%s mid=%s sender=%s",
                chat_id, getattr(message, 'id', ''), sender_name or sender_id or '-')
        except Exception:
            self.logger.debug("[群组监控] 未触发消息镜像失败（忽略）", exc_info=True)

    async def _process_message_async(self, message_data: Dict[str, Any]):
        """异步处理消息"""
        import time
        start_time = time.time()

        try:
            message = message_data['message']
            user_id = message_data['user_id']
            text = message_data['text']
            chat_id = message_data['chat_id']

            # N4b：入站镜像（companion 模式才生效）→ 坐席台/统一收件箱可见用户原话
            _media_type = ""
            _media_ref = ""
            _ocr = str(message_data.get("image_ocr_text") or "").strip()
            if getattr(message, "sticker", None):
                _media_type = "sticker"
                # 静态贴纸(.webp)下载后前端按 <img> 展示；下载失败退回占位「[贴纸]」。
                try:
                    from src.integrations.protocol_bridge import download_tg_media
                    _mt, _mr = await download_tg_media(
                        message, getattr(self, "account_id", "default"),
                    )
                    if _mr:
                        _media_ref = _mr
                except Exception:
                    pass
            elif (
                getattr(message, "video", None)
                or getattr(message, "video_note", None)
                or getattr(message, "animation", None)
            ):
                # 视频 / 视频圆点 / GIF(animation 实为 mp4) → 统一按 video 下载渲染。
                # 加体积上限：超限只保留类型落占位「[视频]」不下载，防大文件拖垮收件箱/磁盘。
                _media_type = "video"
                try:
                    from src.integrations.protocol_bridge import download_tg_media
                    _mt, _mr = await download_tg_media(
                        message, getattr(self, "account_id", "default"),
                        max_bytes=self._inbound_video_max_bytes,
                    )
                    if _mt:
                        _media_type = _mt
                    if _mr:
                        _media_ref = _mr
                except Exception:
                    pass
            elif getattr(message, "photo", None) or (
                getattr(message, "document", None)
                and getattr(getattr(message, "document", None), "mime_type", "")
                and str(message.document.mime_type).startswith("image/")
            ):
                _media_type = "image"
                try:
                    from src.integrations.protocol_bridge import download_tg_media
                    _mt, _mr = await download_tg_media(
                        message, getattr(self, "account_id", "default"),
                    )
                    if _mt:
                        _media_type = _mt
                    if _mr:
                        _media_ref = _mr
                except Exception:
                    pass
                if not _ocr and _media_ref:
                    try:
                        from src.integrations.protocol_bridge import static_media_ref_to_path
                        _img_path = static_media_ref_to_path(_media_ref)
                        if _img_path:
                            _desc = await self._get_image_content(_img_path)
                            if _desc:
                                _ocr = _normalize_message_text(_desc)
                                image_ocr_text = _ocr
                    except Exception:
                        pass
            elif getattr(message, "voice", None) or getattr(message, "audio", None):
                # P1-2 入站语音：音频在 _process_message 里已归档（转录之前），此处只
                # 透传引用——镜像成媒体行后坐席可回放客户原音、质检可核对转写。
                # 归档失败 ref 为空 → 前端按「无音频存档」转写行展示（如实降级）。
                _media_type = "voice"
                _media_ref = str(message_data.get("voice_media_ref") or "")
            if _ocr and _media_type == "image" and "[图片内容]" not in (text or ""):
                # 2026-08-21 值守事故钉子：此处曾把 caption **整个替换**成识图描述——
                # 用户带文字的截图报障（「发送图片，报错，分析问题」）正文被丢，
                # AI/bug_intake 观察层/收件箱镜像只见描述，支持号对着截图里的客户
                # 对话接闲聊（内测群 02:54-03:18 实录，工单 #6 标题也是描述文本）。
                # 对齐 LINE 线 / media_enrich.enrich_media_text 的既定约定：
                # caption 在前、描述在后（下游 strip_media_desc/kb_gate 均按此剥离）。
                _cap = str(text or "").strip()
                text = (f"{_cap}\n[图片内容] {_ocr}" if _cap
                        else f"[图片内容] {_ocr}")
            # 会话显示名优先用对方真实昵称（first + last），无则回落 @username，
            # 再无才留空（前端回落脱敏号）——修「名单里显示数字 id 而非真人昵称」。
            # 同时采集 @username / 电话，落库到会话身份列（客户信息面板/头部显示）。
            _peer = getattr(message, 'from_user', None)
            _peer_name = ''
            _peer_username = ''
            _peer_phone = ''
            if _peer is not None:
                _peer_name = ((getattr(_peer, 'first_name', '') or '') + ' '
                              + (getattr(_peer, 'last_name', '') or '')).strip()
                _peer_username = str(getattr(_peer, 'username', '') or '').lstrip('@')
                _peer_phone = str(getattr(_peer, 'phone_number', '')
                                  or getattr(_peer, 'phone', '') or '')
                if not _peer_name:
                    _peer_name = ('@' + _peer_username) if _peer_username else ''
            if not _peer_name:
                _peer_name = str(message_data.get('username') or '')
            # 群消息带发言人结构化字段（P4-11E 对齐 WhatsApp）：镜像行 sender_id 一直
            # 为空 → 群观测无法按发言者聚合（2026-07-25 灰度实测盲区）。仅群传，
            # 私聊保持空（normalizer 语义「缺省空=非群」）。
            from src.client.reply_logic_gates import normalize_chat_type as _nct
            _mirror_is_group = _nct(
                getattr(getattr(message, 'chat', None), 'type', '')
            ) in ('group', 'supergroup', 'channel')
            # P1-2：语音镜像正文＝干净转写（与 B 线转录回填、A 线出站镜像同口径；
            # [语音] 语义由 media_type 承载）。AI 链内部仍用带前缀的 text——既有
            # 剥离逻辑在消费端（_VOICE_PREFIX），这里只影响坐席台展示与检索。
            # B29：纯 emoji 消息镜像还原原始 emoji（口径见 _inbound_mirror_text）。
            _mirror_text = _inbound_mirror_text(
                _media_type, text,
                _normalize_message_text(getattr(message, "text", None) or ""))
            # 群会话名＝群名，不是发言人名（2026-09-18 P6 实锤：@本账号触发的群消息
            # 把「2026社群聊天」会话改名成了发言人 Katie——store upsert 对非空显示名
            # 一律覆盖）。发言人已走 sender_id/sender_name 结构化字段。
            _mirror_name = _peer_name
            if _mirror_is_group:
                _mirror_name = str(getattr(getattr(message, 'chat', None), 'title', '')
                                   or '') or _peer_name
            self._emit_inbox(
                chat_id=chat_id, text=_mirror_text, direction="in",
                name=_mirror_name,
                msg_id=str(getattr(message, 'id', '') or ''),
                media_type=_media_type,
                media_ref=_media_ref,
                username=_peer_username,
                phone=_peer_phone,
                sender_id=(str(getattr(_peer, 'id', '') or '')
                           if (_mirror_is_group and _peer is not None) else ''),
                sender_name=(_peer_name if _mirror_is_group else ''),
            )

            # ── 无兜底纪律（2026-08-17）：语音没听懂就不装懂────────────────
            # 转写失败/下载失败时旧行为把「[语音消息 - 转录失败]」占位喂给 LLM
            # 继续回——AI 对着听不懂的语音硬聊＝掩盖型兜底（与识图串台同病）。
            # 现在：镜像照常（坐席可回放原音人工处理）→ 故障类弹窗+ERROR 上报
            # → 跳过自动回复。「识别功能未启用」属运营配置不弹窗，仅日志+跳过。
            if _media_type == "voice" and str(text or "").startswith("[语音消息 - "):
                _asr_reason = str(text or "")[len("[语音消息 - "):].rstrip("]")
                if "未启用" not in _asr_reason:
                    try:
                        from src.ops.delivery_block import report_block
                        _ab_acct = str(
                            getattr(self, "account_id", "") or "default")
                        try:
                            from src.inbox.normalizer import (
                                conv_id as _ab_conv_id,
                            )
                            _ab_cid = _ab_conv_id(
                                "telegram", _ab_acct, str(chat_id))
                        except Exception:
                            _ab_cid = f"telegram:{_ab_acct}:{chat_id}"
                        report_block(
                            "asr", reason=_asr_reason, platform="telegram",
                            conversation_id=_ab_cid)
                    except Exception:
                        self.logger.debug(
                            "[asr] delivery_block 上报失败", exc_info=True)
                self.logger.warning(
                    "[asr] 语音未转写成功（%s）→ 跳过自动回复（无兜底纪律），"
                    "坐席可在工作台回放原音 chat=%s", _asr_reason, chat_id)
                return

            # ── 无兜底纪律：没看懂图就不装懂 ────────────────────────────
            # 图片入站必须有 Vision 描述才自动回。caption  alone / 解析失败占位
            # 都不够——对着看不见的图硬聊＝8/16 键盘串台同病。镜像已落收件箱，
            # 坐席可看原图人工处理。
            if _media_type == "image":
                _vision_ok = bool(_ocr) or str(text or "").startswith("[图片内容]")
                _vision_off = str(text or "").startswith("[图片消息 - 识别功能未启用]")
                if _vision_off or not self._vision_usable():
                    self.logger.warning(
                        "[vision] 识图未启用 → 跳过自动回复 chat=%s", chat_id)
                    return
                if not _vision_ok:
                    _vr = "no_desc"
                    _t = str(text or "")
                    if _t.startswith("[图片消息 - "):
                        _vr = _t[len("[图片消息 - "):].rstrip("]")
                    try:
                        from src.ops.delivery_block import report_block
                        _vb_acct = str(
                            getattr(self, "account_id", "") or "default")
                        try:
                            from src.inbox.normalizer import (
                                conv_id as _vb_conv_id,
                            )
                            _vb_cid = _vb_conv_id(
                                "telegram", _vb_acct, str(chat_id))
                        except Exception:
                            _vb_cid = f"telegram:{_vb_acct}:{chat_id}"
                        report_block(
                            "vision", reason=_vr, platform="telegram",
                            conversation_id=_vb_cid)
                    except Exception:
                        self.logger.debug(
                            "[vision] delivery_block 上报失败",
                            exc_info=True)
                    self.logger.warning(
                        "[vision] 图片未看懂（%s）→ 跳过自动回复（无兜底纪律），"
                        "坐席可在工作台看原图 chat=%s", _vr, chat_id)
                    return

            # ── 对方机器人守卫（P0 2026-08-03，SpamBot 空转实锤修复）────────
            # 私聊实时路径此前从不看 from_user.is_bot / reply_markup（群路径反而
            # 有闸）→ AI 跟按钮 bot 80 秒空转 8 轮。放在镜像**之后**：入站照常
            # 进收件箱（bot 线程对运营有查询价值），只拦「继续走 LLM 回复」；
            # 确定级顺手把会话降 manual（A/B/主动触达三链全部自动尊重档位）。
            # 守卫默认关（inbox.peer_bot_guard.enabled），异常一律放行。
            try:
                from src.inbox.peer_bot_guard import guard_a_line_should_skip
                _pbg_reason = guard_a_line_should_skip(
                    config=(self.config.config
                            if hasattr(self.config, 'config') else {}),
                    account_id=str(getattr(self, 'account_id', 'default')
                                   or 'default'),
                    chat_id=chat_id,
                    message=message,
                    current_text=str(text or ''),
                )
                if _pbg_reason:
                    self.logger.info(
                        "[peer_bot_guard] A线跳过自动回复 chat=%s user=%s "
                        "reason=%s", chat_id, user_id, _pbg_reason)
                    return
            except Exception:
                self.logger.debug(
                    "[peer_bot_guard] 检查失败（放行）", exc_info=True)

            _es_cnt, _es_key = 0, ""
            if text and self._human_escalation:
                try:
                    self._human_escalation.reload_config(
                        self.config.config if hasattr(self.config, "config") else {}
                    )
                    _es_cnt, _es_key = self._human_escalation.record_streak(
                        chat_id, user_id, text
                    )
                except Exception as ex:
                    self.logger.warning("人工转接计数失败: %s", ex)

            # 记录接收时间
            receive_time = time.time()

            # 上下文分析（如果启用）
            context_analysis = None
            should_reply = True  # 默认回复

            if self.context_manager:
                try:
                    # 添加上下文消息
                    self.context_manager.add_message(
                        chat_id=chat_id,
                        user_id=user_id,
                        username=message_data.get('username', 'unknown'),
                        text=text,
                        is_ai=False
                    )

                    # 分析上下文
                    context_analysis = self.context_manager.analyze_context(
                        chat_id=chat_id,
                        current_context=text
                    )

                    # 根据上下文分析决定是否需要回复
                    should_reply = context_analysis.get('should_reply', True)

                    if not should_reply:
                        self.logger.info(f"根据上下文分析，不回复此消息: {text[:50]}...")
                        return

                    self.logger.info(
                        f"上下文分析结果 - 情绪: {context_analysis.get('user_emotion', 'unknown')}, "
                        f"主题: {context_analysis.get('conversation_topic', 'unknown')}, "
                        f"优先级: {context_analysis.get('priority', 'normal')}"
                    )

                except Exception as e:
                    self.logger.warning(f"上下文分析失败: {e}")

            # 群内：拉取近期机器人/通知消息，与 OCR 一起供 AI 使用（群聊 id 为负）
            recent_bot_messages = []
            # `_ocr` may be filled above by downloading the mirrored media ref. Do not
            # overwrite it with the original queue payload, which can still be empty.
            image_ocr_text = _ocr or message_data.get('image_ocr_text')
            if isinstance(chat_id, (int, float)) and int(chat_id) < 0:
                recent_bot_messages = await self._get_recent_bot_messages(int(chat_id))
                # 当前消息无图但与订单/查单相关时，拉取群内最近一张图作为查单依据（SOP：有凭证即按凭证确认）
                if not image_ocr_text and text:
                    t = text.strip()
                    order_ask = (
                        ("订单" in t and any(k in t for k in ("看", "图", "发", "了", "吗")))
                        or ("订单" in t and "吗" in t)
                        or ("查" in t and any(k in t for k in ("订单", "单", "到了", "凭证")))
                    )
                    if order_ask:
                        self.logger.info("当前消息无图且为订单相关问句，拉取群内最近一张图做 OCR")
                        recent_ocr = await self._get_recent_chat_image_ocr(int(chat_id))
                        if recent_ocr:
                            image_ocr_text = _normalize_message_text(recent_ocr)
                            self.logger.info("已使用群内最近一张图 OCR 作为订单上下文")
                        else:
                            self.logger.info("群内最近图 OCR 无结果，AI 将无图上下文回复")

            # 群名（用于额度规则：特殊客户/黑名单按群名识别）
            msg = message_data.get('message')
            chat_title = (getattr(msg.chat, 'title', None) or '').strip() if msg and getattr(msg, 'chat', None) else ''
            # chat_type 用于 3-tier persona 路由和下游决策。
            # pyrogram 的 chat.type 是枚举（str() 出 "ChatType.SUPERGROUP"），
            # 必须经 normalize_chat_type 取 .name，否则群判定恒 False →
            # 群消息误入私聊上下文窗（P1-2 分窗失效，2026-07-25 灰度实测踩坑）。
            from src.client.reply_logic_gates import normalize_chat_type
            _chat_type_str = normalize_chat_type(
                getattr(getattr(msg, 'chat', None), 'type', ''))
            _is_group = _chat_type_str in ('group', 'supergroup', 'channel')

            # request_id 串联整条链路，便于日志与排错
            request_id = f"{chat_id}_{getattr(message, 'id', 0)}"
            # 是否因 @ 本账号而触发回复（用于 S5 静默策略：被 @ 时不再按概率跳过）
            triggered_by_mention = False
            if self.user_info and getattr(self.user_info, 'username', None):
                uname = (self.user_info.username or "").strip().lower().lstrip("@")
                if uname and text:
                    t_lower = text.strip().lower()
                    if f"@{uname}" in t_lower or f"@{uname}\u200b" in t_lower:
                        triggered_by_mention = True
            # 情绪粗判传入 AI prompt（在情绪增强之前，与 enhance_reply 独立）
            # N 线 核心1：复用共享 companion_context（A/B 两线同一套情绪/人设逻辑）
            from src.utils.companion_context import (
                emotion_hint as _companion_emotion_hint,
                record_relationship_message as _record_relationship_message,
                resolve_funnel_stage as _resolve_funnel_stage,
                resolve_intimacy_score as _resolve_intimacy_score,
                route_persona_id as _route_persona_id,
            )
            user_emotion_hint = _companion_emotion_hint(text, self.emotion_enhancer)
            # 会话级人设覆写（2026-07-26 方案 A）：坐席在统一收件箱给这条会话
            # 显式换绑过人设 → 原生 bot 回复同样跟随（与 autodraft/autosend 同一
            # 事实源）。仅覆写命中才改写；否则保持 _route_persona_id 原路由
            # （含群聊 3-tier 语义）。开关关/异常＝零行为变化。
            _eff_persona_id = _route_persona_id(
                getattr(self, 'account_persona_ids', None), _chat_type_str
            )
            try:
                from src.ai.persona_voice import (
                    conv_binding_key as _conv_key_fn,
                    conv_override_enabled as _conv_on_fn,
                )
                _full_cfg = getattr(self.config, "config", None) or {}
                if _conv_on_fn(_full_cfg):
                    from src.utils.persona_manager import (
                        PersonaManager as _PM_conv,
                    )
                    _pm_conv = _PM_conv.get_instance()
                    _conv_ref = _pm_conv.get_chat_binding_ref(_conv_key_fn(
                        "telegram",
                        str(getattr(self, "account_id", "") or "default"),
                        str(chat_id),
                    ))
                    if _conv_ref and _pm_conv.get_persona_by_id(_conv_ref) is not None:
                        _eff_persona_id = _conv_ref
            except Exception:
                pass
            # Q3：先把本条入站记入 contacts（recorder 未开则 no-op）→ 刷新 journey 的
            # intimacy_score，再读出，保证融合用到的是"含本轮"的最新分（与 RPA 各线同序）。
            _record_relationship_message(
                self.account_id, chat_id, "in",
                text_preview=text or "",
                display_name=str(message_data.get('username') or ''),
            )
            # Q3：注入统一关系事实源（contacts.IntimacyEngine）→ companion_relationship
            # 双信号融合（沉默衰减自动降阶 + reunion 提示）。provider 未注册时返回 None，
            # 行为完全等同旧版（A 线此前从不传 intimacy_score → 融合恒跳过）。
            _intimacy_score = _resolve_intimacy_score(self.account_id, chat_id)
            _funnel_stage = _resolve_funnel_stage(self.account_id, chat_id)
            # 语音转录：AI 只需看纯文本，剥掉 [语音转录] 前缀标记
            _VOICE_PREFIX = "[语音转录] "
            ai_text = text[len(_VOICE_PREFIX):] if text.startswith(_VOICE_PREFIX) else text

            # ── 生成前安静窗 + 未答连发合并（interject_absorb，pregen_quiet_sec=0
            # 默认关）：客户还在连发（逐字打/分段打）时不急着生成——等安静窗；
            # 等待期又来新消息 → 生成前零成本让位（旧行为是生成完再丢弃＝白烧
            # LLM）；安静达成 → 把该会话全部未答文本碎片感知合并成一条输入，
            # 新闻问句/辱骂检测/multi_message 指令都跑在合并后的全文上。
            # 仅私聊纯文本；任何异常回退单条旧行为。──────────────────────
            try:
                from src.client.interject_absorb import (
                    STALE_TOLERANCE_SEC as _pg_tol,
                    drain_pending_texts as _pg_drain,
                    latest_inbound_ts as _pg_latest,
                    merge_pending_entries as _pg_merge,
                    parse_interject_cfg as _pg_cfg_fn,
                    pregen_quiet_for as _pg_quiet_for,
                )
                _pg_cfg = _pg_cfg_fn(
                    self.config.config if hasattr(self.config, "config") else {})
                _pg_quiet = _pg_quiet_for(text, _pg_cfg) \
                    if _pg_cfg["enabled"] and not _is_group else 0.0
                if _pg_quiet > 0.0:
                    _pg_my_ts = float(message_data.get('_enq_ts') or 0.0)
                    _pg_start = time.time()
                    _pg_max = float(_pg_cfg["pregen_max_wait_sec"] or 0.0)
                    _pg_yield = False
                    while True:
                        _pg_now = time.time()
                        _pg_last = _pg_latest(chat_id)
                        if _pg_last > _pg_my_ts + _pg_tol:
                            _pg_yield = True     # 对方补话 → 让位给新任务
                            break
                        if (_pg_now - max(_pg_last, _pg_my_ts) >= _pg_quiet
                                or _pg_now - _pg_start >= _pg_max):
                            break
                        await asyncio.sleep(0.5)
                    if _pg_yield:
                        self.logger.info(
                            "[interject] 生成前对方补话，本条让位给最新消息 "
                            "chat=%s（未烧 LLM）", chat_id)
                        return
                    if _pg_my_ts > 0.0:
                        _pg_entries = _pg_drain(chat_id, _pg_my_ts)
                        if _pg_entries:
                            # 单条也登记——生成期间被过期中止时 requeue 还回
                            # 注册表，下一任务的合并才能带上这条（否则丢失）
                            message_data['_ij_burst_entries'] = _pg_entries
                        if len(_pg_entries) > 1:
                            _pg_text = _pg_merge(_pg_entries)
                            if _pg_text:
                                ai_text = _pg_text
                                self.logger.info(
                                    "[interject] 生成前合并 %d 条未答连发 "
                                    "chat=%s（碎片拼句）",
                                    len(_pg_entries), chat_id)
            except Exception:
                self.logger.debug(
                    "[interject] 生成前安静窗异常（按单条处理）", exc_info=True)

            # 调用Skill管理器处理消息，传递上下文分析结果、图片 OCR、机器人消息、群名、request_id、情绪、发群消息回调（供 gxp 代发命令等）
            _sm_context = {
                'chat_id': chat_id,
                'chat_title': chat_title,
                # 对方显示名（first+last / @username 回落）：喂给 ①提示词「对方身份」
                # 声明 ②persona_guard 错误自称名守卫的借名锚点（2026-08-08 David Lin 事故）
                '_peer_display_name': _peer_name,
                'context_analysis': context_analysis,
                'image_ocr_text': image_ocr_text,
                'recent_bot_messages': recent_bot_messages,
                'request_id': request_id,
                'user_emotion_hint': user_emotion_hint,
                'triggered_by_mention': triggered_by_mention,
                '_trigger_path': message_data.get('_trigger_path'),
                '_send_to_chat': self.send_message,
                '_send_photo_to_chat': self.send_photo,
                # 实施66 P0-1：A 线唱歌兑现短路的语音文件直发缝（预渲染唱段）
                '_send_voice_to_chat': self.send_voice_file,
                # P3 媒体拟人节奏（2026-08-12）：挑图等待期挂「正在发送照片」气泡
                '_send_media_action': self._send_upload_photo_action,
                '_record_gxp_cmd': self.record_gxp_command,
                '_i18n': self.i18n,
                '_event_tracker': self.event_tracker,
                'user_id': user_id,
                'user_msg_id': getattr(message, 'id', 0),
                'channel': 'telegram',
                'media_type': _media_type,
                'media_ref': _media_ref,
                'media_desc': image_ocr_text or "",
                '_current_user_message_for_lang': ai_text,
                '_peer_audio_emotion': message_data.get('_peer_audio_emotion'),
                # ASR P1：转写可疑原因码（skill_manager _line_merge_keys 透传 → ai_client 先确认块）
                '_voice_asr_suspect': message_data.get('_voice_asr_suspect') or None,
                # N 线 核心1：复用共享 companion_context.route_persona_id（A/B 同一套
                # 3-tier 路由）；会话覆写命中时已在上方改写为覆写人设。
                'account_persona_id': _eff_persona_id,
                'is_group': _is_group,
                'chat_type': _chat_type_str or 'private',
                'platform': 'telegram',  # S5: CrossPlatformIdentity
                # 供 skill_manager 剧情收场把 story_complete 镜像进 contacts journey
                # （与 resolve_intimacy_score 用同一 account_id 寻址同一 journey）。
                'account_id': self.account_id,
            }
            # 引用上下文（2026-08-20）：**恒写键**——user_context 按会话持久且
            # merge 只覆盖有键项，只在有引用时写会让旧引用粘住后续轮次
            # （与 _spoken_variant_request 同教训）。空串=本轮无引用，消费方跳过。
            try:
                from src.client.quoted_context import format_quoted_note
                _sm_context['_quoted_note'] = format_quoted_note(
                    message_data.get('quoted') or {})
            except Exception:
                _sm_context['_quoted_note'] = ""
            _sm_context['quoted'] = message_data.get('quoted') or {}
            # Q3：仅在有值时注入，None 不写键 → 与 RPA 各线一致、向后兼容
            if _intimacy_score is not None:
                _sm_context['intimacy_score'] = _intimacy_score
            if _funnel_stage:
                _sm_context['funnel_stage'] = _funnel_stage
            # 生成层口语分叉（Phase G）：本条回复可能发语音条 → 让 LLM 同次调用
            # 多产 [口语版]（书面进镜像/记忆、口语送 TTS）。门控纯函数自查
            # voice_reply/trigger/colloquial.generated/中文。**恒写键**（True/False）
            # ——user_context 按会话持久，merge 只覆盖有键项，只写 True 会让旧 True
            # 粘住后续不满足门控的轮次（白吃 token）。
            try:
                from src.ai.spoken_variant import should_request_spoken_variant
                _sm_context['_spoken_variant_request'] = should_request_spoken_variant(
                    self.config.config if hasattr(self.config, "config") else {},
                    is_peer_voice=bool(message_data.get('_is_voice_msg')),
                    text=ai_text)
            except Exception:
                pass
            # ── 收件箱自动化档位闸（companion 镜像开时生效）────────────────
            # UI「手动 / AI草稿我审 / 多选」只写 conversation_settings；此前 A 线
            # 完全不读 → 坐席切档后仍全自动互聊。仅 auto_ai 才直发；其余档位
            # 已镜像进收件箱，交 System Z 拟稿人审（或静音）。store 未就绪 fail-open。
            # 2026-08-07：档位再过 effective_automation 封顶（平台/业务线/冷启动
            # 预热），与 B 线拟稿链同源——此前封顶只在 B 线生效，companion 架构下
            # 新号预热期照样直发（.198「新号全自动哑火」在两种架构行为分叉的另一半）。
            # 封顶命中 → A 线让位；B 线因同一封顶判定不再让位，接住 review 拟稿
            # （不双发、不丢消息）。封顶求值异常 = 不封顶（fail-open，与 B 线一致）。
            if getattr(self, "_mirror_inbox", False):
                try:
                    from src.integrations.protocol_bridge import get_inbox_store
                    from src.inbox.normalizer import conv_id as _conv_id
                    from src.inbox.automation_mode import (
                        allows_direct_autosend,
                        resolve_automation_mode,
                    )
                    _ibx = get_inbox_store()
                    if _ibx is not None:
                        _cid = _conv_id(
                            "telegram",
                            str(getattr(self, "account_id", "default") or "default"),
                            str(chat_id),
                        )
                        _cfg_root = (
                            self.config.config
                            if hasattr(self.config, "config") else {}
                        )
                        _mode = resolve_automation_mode(_ibx, _cid, _cfg_root)
                        _eff_mode, _eff_caps = _mode, []
                        try:
                            from src.inbox.effective_automation import (
                                apply_mode_caps,
                                compute_mode_caps,
                            )
                            _eff_mode, _eff_caps = apply_mode_caps(
                                _mode, compute_mode_caps(
                                    platform="telegram",
                                    account_id=str(
                                        getattr(self, "account_id", "default")
                                        or "default"),
                                    config=_cfg_root,
                                ))
                        except Exception:
                            _eff_mode, _eff_caps = _mode, []
                        if not allows_direct_autosend(_eff_mode):
                            self.logger.info(
                                "[automation] A线让位 mode=%s effective=%s "
                                "caps=%s chat=%s account=%s"
                                "（收件箱档位非全自动）",
                                _mode, _eff_mode,
                                ",".join(c.layer for c in _eff_caps) or "-",
                                chat_id,
                                getattr(self, "account_id", "default"),
                            )
                            try:
                                from src.client.gate_stats import bump as _gate_bump
                                _gate_bump("automation_mode")
                                if _eff_caps:
                                    # 封顶导致的让位单独计数（与坐席显式切档区分）
                                    _gate_bump("automation_capped")
                            except Exception:
                                pass
                            return
                except Exception:
                    self.logger.debug(
                        "[automation] 档位闸检查失败（放行）", exc_info=True)

            # ── 工作时间闸（inbox.work_schedule，2026-08-04，默认关）：账号
            # 休息中 A 线不直发（危机消息 severe/elevated 在判定内穿透照发）。
            # 让位后走向：镜像开时本条已进收件箱，System Z 的双轨互斥同一
            # should_hold 口径判「A 线休息」→ 照常拟稿，稿由 AutosendWorker
            # 班表闸扣到复班投递/补觉重拟；镜像关=纯直发部署则整段静默（这
            # 正是「非工作时间不予自动回复」的字面语义）。每条消息活读 config
            # → 班表改动免重启生效；判定 fail-open，异常绝不闸死回复。──
            try:
                from src.inbox.work_hours_gate import (
                    should_hold_auto_reply as _ws_should_hold,
                    work_schedule_cfg as _ws_cfg_fn,
                )
                _ws_root = (
                    self.config.config
                    if hasattr(self.config, "config") else {}
                )
                _ws_hold = _ws_should_hold(
                    _ws_cfg_fn(_ws_root or {}), "telegram",
                    str(getattr(self, "account_id", "default") or "default"),
                    peer_text=ai_text)
                if _ws_hold:
                    self.logger.info(
                        "[work-schedule] A线让位 reason=%s chat=%s account=%s"
                        "（账号休息中；危机消息除外）",
                        _ws_hold, chat_id,
                        getattr(self, "account_id", "default"),
                    )
                    try:
                        from src.client.gate_stats import bump as _gate_bump
                        _gate_bump("work_schedule")
                    except Exception:
                        pass
                    return
            except Exception:
                self.logger.debug(
                    "[work-schedule] 闸检查失败（放行）", exc_info=True)

            # ── 回复逻辑闸门（UI「回复逻辑」页：冷却 + 最大连续回复；每条消息重读
            # config → 保存即生效）。私聊/群/轮询兜底三条入站路径都汇到本函数，
            # 闸门对三者统一生效。放在 process_message 之前 → 被拦时不白跑 LLM。──
            from src.client.reply_logic_gates import (
                consecutive_limit_reached,
                cooldown_remaining,
            )
            _rl_cfg = self.config.get('telegram', {}).get('reply_logic', {})
            # 双号隔离：冷却/连发计数按协议号分桶，防 Katie/Jason 互踩闸门
            _rl_key = f"{self.account_id}:{chat_id}:{user_id}"
            _rl_now = time.time()
            _rl_last = self._auto_reply_ts.get(_rl_key)
            _cd_left = cooldown_remaining(_rl_cfg, _rl_last, _rl_now)
            if _cd_left > 0:
                # outbound.unlimited_mode：回复冷却属业务频控 → 放行（连续回复
                # 上限 max_consecutive_replies 是防机器人对轰的安全刹车，**不**短路）。
                try:
                    from src.ops.outbound_policy import (
                        is_unlimited as _ou, record_unlimited_bypass as _oub)
                    if _ou():
                        _oub("reply_logic_cooldown")
                        _cd_left = 0.0
                except Exception:
                    pass
            if _cd_left > 0:
                _note_outbound_block("business", "reply_logic_cooldown")
                self.logger.info(
                    "[回复逻辑] 冷却中，跳过自动回复 chat=%s user=%s 剩余 %.0f 秒",
                    chat_id, user_id, _cd_left)
                # 结构化拦截计数（UI 今日统计权威口径；lazy import + 静默双保险，
                # 统计失败绝不影响回复链路）
                try:
                    from src.client.gate_stats import bump as _gate_bump
                    _gate_bump("cooldown")
                except Exception:
                    pass
                # 2026-08-09：被冷却吞掉 ≠ 刻意不回——安排轮询兜底在冷却结束后
                # 补答一次（仅私聊；详见 _defer_swallowed_inbound）。
                if not _is_group:
                    self._defer_swallowed_inbound(
                        chat_id, getattr(message, 'id', 0), _cd_left,
                        reason="reply_logic_cooldown")
                return
            _rl_hit, _rl_eff = consecutive_limit_reached(
                _rl_cfg, self._auto_reply_streak.get(_rl_key, 0), _rl_last, _rl_now)
            if _rl_eff != self._auto_reply_streak.get(_rl_key, 0):
                # 静默超复位窗口 → 生效计数已归零，落地回字典（防陈旧计数粘住）
                self._auto_reply_streak[_rl_key] = _rl_eff
            if _rl_hit:
                _note_outbound_block("safety", "reply_logic_streak")
                self.logger.info(
                    "[回复逻辑] 连续自动回复已达上限(%d 条)，暂停回复 chat=%s user=%s"
                    "（静默 30 分钟后自动复位）",
                    _rl_eff, chat_id, user_id)
                try:
                    from src.client.gate_stats import bump as _gate_bump
                    _gate_bump("streak")
                except Exception:
                    pass
                return
            # 2026-08-09：生成前拍冷却记账快照——interject 在下方丢弃已生成回复
            # 时按此回滚（未发出的回复不得占冷却位/进 last_reply 记忆）。拍不到
            # （异常）＝退回旧行为，不影响主链路。
            _acct_snap = None
            try:
                _acct_snap = self.skill_manager.snapshot_reply_accounting(
                    user_id, _sm_context)
            except Exception:
                _acct_snap = None
            reply_text = await self.skill_manager.process_message(
                text=ai_text,
                user_id=user_id,
                context=_sm_context,
            )
            if reply_text is None and not _is_group:
                # 冷却吞没补答（2026-08-09）：skill 侧冷却拦截静默返回 None，与
                # 「刻意不回」（no-reply 判定/屏蔽等）不可区分 → 消费专用跳过
                # 信号；命中则把该 mid 的去重寿命缩短到冷却结束后不久，轮询
                # 兜底届时把它当新进站重拾（水位闸保证至多一次）。
                try:
                    _skip = self.skill_manager.consume_reply_skip(
                        chat_id, user_id,
                        str(getattr(self, "account_id", "") or ""))
                except Exception:
                    _skip = None
                if _skip and _skip.get("reason") == "cooldown":
                    _note_outbound_block("business", "skill_cooldown")
                    self._defer_swallowed_inbound(
                        chat_id, getattr(message, 'id', 0),
                        float(_skip.get("retry_after") or 0.0),
                        reason="skill_cooldown")

            # 情绪增强（仅在启用时）
            enhanced_reply = reply_text
            emoticons_config = self.config.get('emoticons', {})

            # 检查情绪增强器是否启用且实例存在
            if reply_text and self.emotion_enhancer and emoticons_config.get('enabled', True):
                try:
                    # 分析消息情绪（独立于上下文）
                    emotion_analysis = self.emotion_enhancer.analyze_message_emotion(text)

                    # 增强回复
                    enhanced_reply = self.emotion_enhancer.enhance_reply(
                        original_reply=reply_text,
                        emotion=emotion_analysis.get('emotion', 'neutral'),
                        context_analysis=context_analysis or {},
                        message_text=text,
                        chat_id=str(chat_id),
                    )

                    if enhanced_reply != reply_text:
                        self.logger.info(f"情绪增强应用成功: {enhanced_reply[:80]}...")
                    else:
                        self.logger.debug("情绪增强未修改回复内容")

                except Exception as e:
                    self.logger.warning(f"情绪增强失败: {e}")
                    enhanced_reply = reply_text

            # conversion 域：短探询「在吗」等去掉客服台套话（在情绪增强之后）
            if enhanced_reply:
                enhanced_reply = self._rewrite_companion_helpdesk_ping(
                    enhanced_reply, text
                )

            # 记录处理完成时间
            process_time = time.time()

            # 术语后处理：按 config ai.terminology 统一表述后再发送
            reply_final = self._apply_terminology(enhanced_reply) if enhanced_reply else ""
            suffix = ""
            he_forward_spec = None
            if reply_final and self._human_escalation and _es_key:
                try:
                    self._human_escalation.reload_config(
                        self.config.config if hasattr(self.config, "config") else {}
                    )
                    _chat_un = None
                    try:
                        if message and getattr(message, "chat", None):
                            _chat_un = getattr(message.chat, "username", None)
                    except Exception:
                        _chat_un = None
                    _umid = getattr(message, "id", None)
                    he_out = self._human_escalation.format_suffix_if_needed(
                        chat_id,
                        user_id,
                        _es_cnt,
                        _es_key,
                        user_message_id=int(_umid) if _umid is not None else None,
                        user_text=text,
                        chat_username=_chat_un,
                        chat_title=chat_title or None,
                    )
                    if he_out.suffix:
                        suffix = he_out.suffix
                    he_forward_spec = he_out.forward_spec
                except Exception as ex:
                    self.logger.warning("人工转接追加文案失败: %s", ex)
            suffix_html = bool(suffix and ("<a href" in suffix))

            def _apply_suffix_chunks(chunks_in: List[str]) -> List[str]:
                if not suffix or not chunks_in:
                    return chunks_in
                out = list(chunks_in)
                if suffix_html:
                    if len(out) > 1:
                        for i in range(len(out) - 1):
                            out[i] = _html_escape(out[i])
                        out[-1] = _html_escape(out[-1]) + suffix
                    else:
                        out[-1] = _html_escape(out[-1]) + suffix
                else:
                    out[-1] = out[-1] + suffix
                return out

            _parse_mode = ParseMode.HTML if (PYROGRAM_AVAILABLE and suffix_html) else None

            # ── 出站近重复守卫（A 线，2026-08-02）─────────────────────────
            # per-chat 锁只保证「串行」，保证不了第二次生成不复读上一条（context
            # 时差 / LLM echo，实录：同店名介绍 5s 内两发、同一句自我介绍换词重说）。
            # 发送前与最近出站（inbox 镜像 + 进程级在途登记表）最后核对一次，命中
            # 即本轮静默不发（客户视角＝少一条废话；比同义双发暴露机器人便宜）。
            # gated：inbox.outbound_dup_guard.enabled（默认关＝零行为变更）；
            # 放语音判定之前 → 语音复读同样被拦。异常一律放行，绝不阻断正常回复。
            # 乐观登记的 token 存变量：插话吸收关口中止本条回复时撤销登记，
            # 防「登记了却没发」的幽灵条目把下一条（覆盖两问的新回复）误拦成复读。
            _dg_reg_token = 0
            _dg_reg_cid = ""
            if reply_final:
                try:
                    from src.inbox.outbound_dup_guard import (
                        near_duplicate_of_recent as _dg_near,
                        outbound_registry as _dg_reg,
                        record_dup_check as _dg_rec,
                        resolve_guard_cfg as _dg_cfg_fn,
                    )
                    _dg_cfg = _dg_cfg_fn(
                        self.config.config if hasattr(self.config, "config") else {})
                    if _dg_cfg.get("enabled"):
                        from src.inbox.normalizer import conv_id as _dg_conv_id
                        _dg_cid = _dg_conv_id(
                            "telegram",
                            str(getattr(self, "account_id", "default") or "default"),
                            str(chat_id))
                        _dg_rows: List[Dict[str, Any]] = []
                        if getattr(self, "_mirror_inbox", False):
                            try:
                                from src.integrations.protocol_bridge import (
                                    get_inbox_store as _dg_get_store,
                                )
                                _dg_store = _dg_get_store()
                                if _dg_store is not None:
                                    _dg_rows = list(_dg_store.list_recent_messages(
                                        _dg_cid, limit=8) or [])
                            except Exception:
                                _dg_rows = []
                        _dg_rows.extend(_dg_reg.recent_rows(_dg_cid))
                        # #144：similar 档只比对本轮（最新入站之后的出站）——上一轮
                        # 回复与本条相近是两轮各答一次，不是双发；dup 档不受影响。
                        _dg_since = 0.0
                        try:
                            from src.inbox.outbound_dup_guard import (
                                latest_inbound_ts as _dg_lit,
                            )
                            _dg_since = float(_dg_lit(_dg_rows) or 0.0)
                        except Exception:
                            _dg_since = 0.0
                        _dg_hit = _dg_near(
                            reply_final, _dg_rows,
                            window_sec=float(_dg_cfg.get("window_sec", 180.0)),
                            similar_since_ts=_dg_since or None)
                        _dg_lvl = (_dg_hit or {}).get("level", "")
                        _dg_block = bool(_dg_hit) and (
                            _dg_lvl == "dup"
                            or bool(_dg_cfg.get("block_similar", True)))
                        try:
                            _dg_rec(_dg_lvl, source="a_line", blocked=_dg_block)
                        except Exception:
                            pass
                        if _dg_block:
                            self.logger.warning(
                                "[dup_guard] guard=near_duplicate A线出站近重复拦截 "
                                "chat=%s level=%s sim=%.2f age=%.0fs matched=%r",
                                chat_id, _dg_lvl,
                                _dg_hit.get("similarity", 0.0),
                                _dg_hit.get("age_sec", 0.0),
                                str(_dg_hit.get("matched_text", ""))[:60])
                            # impl85 阶段3（#45 taihua009 02:11 实录）：拦得对，但
                            # 拦下后空过一轮＝客户干等。用已发内容当负样本换说法
                            # 重写一条，重写稿再过同一守卫——通过才发，仍雷同/重写
                            # 失败维持静默跳过（守卫只更严不放松）。自身异常绝不
                            # 外泄（外层 except 语义是「守卫坏了放行原文」，那会把
                            # 重复原文发出去）。
                            _dg_rw = None
                            try:
                                if self.ai_client is not None:
                                    from src.inbox.outbound_dup_guard import (
                                        attempt_dup_rewrite as _dg_retry,
                                        build_dup_rewrite_prompt as _dg_rwp,
                                    )

                                    async def _dg_rw_fn(_t, _m, _ai=self.ai_client):
                                        return await _ai.rewrite_local(
                                            _dg_rwp(_m), _t)

                                    _dg_rw = await _dg_retry(
                                        text=reply_final, hit=_dg_hit,
                                        rows=_dg_rows, cfg=_dg_cfg,
                                        rewrite_fn=_dg_rw_fn, source="a_line",
                                        similar_since_ts=_dg_since or None,
                                        conv_id=_dg_cid)
                            except Exception:
                                _dg_rw = None
                            if not _dg_rw:
                                try:
                                    from src.client.gate_stats import bump as _gate_bump
                                    _gate_bump("dup_guard")
                                except Exception:
                                    pass
                                # #144：拦下后绝不静默——A 线本条就是对最新入站的回复，
                                # 没救回＝客户零回复 → 打「需人工」进待处理清单（best-effort）。
                                try:
                                    from src.integrations.protocol_autoreply import (
                                        tag_needs_human as _dg_tag,
                                    )
                                    from src.integrations.protocol_bridge import (
                                        get_inbox_store as _dg_tag_store,
                                    )
                                    _dg_st = _dg_tag_store()
                                    if _dg_st is not None:
                                        _dg_tag(_dg_st, {
                                            "platform": "telegram",
                                            "account_id": str(getattr(
                                                self, "account_id", "default") or "default"),
                                            "chat_key": str(chat_id),
                                        }, reason="dup_guard_blocked", source="system")
                                except Exception:
                                    pass
                                self.logger.warning(
                                    "[dup_guard] A线拦截后未救回，本轮无回复，已标「需人工」"
                                    "(reason=dup_guard_blocked) chat=%s", chat_id)
                                return
                            self.logger.info(
                                "[dup_guard] A线拦截后换说法重试成功 chat=%s "
                                "len %d→%d（重写稿已再过守卫）",
                                chat_id, len(reply_final), len(_dg_rw))
                            reply_final = _dg_rw
                        # 乐观登记待发文本（先于实际发送）：并行在途/跨链投递立即可见；
                        # 条目按守卫窗口自然过期，A 线无自动重发同文本语义故不撤销
                        # （唯一例外：插话吸收关口中止 → 用 token 撤销，见下）。
                        _dg_reg_token = _dg_reg.register(_dg_cid, reply_final)
                        _dg_reg_cid = _dg_cid
                except Exception:
                    self.logger.debug(
                        "[dup_guard] A线近重复守卫异常（放行）", exc_info=True)

            # 语音回复：如果启用且触发条件满足，先尝试发语音；成功则跳过文字
            _is_voice_msg = bool(message_data.get('_is_voice_msg'))
            _voice_sent = False
            _voice_fail_state: Dict[str, Any] = {}
            if reply_final:
                try:
                    _voice_sent = await self._maybe_send_voice_reply(
                        message, reply_final, is_peer_voice=_is_voice_msg,
                        peer_audio_emotion=message_data.get('_peer_audio_emotion'),
                        fail_state=_voice_fail_state,
                    )
                except Exception as _ve:
                    self.logger.warning("[voice_reply] probe failed: %s", _ve)

            # 如果有回复，发送消息（言简意赅：长回复可分条发送）
            if reply_final and not _voice_sent:
                # 无兜底纪律（2026-08-17 老板拍板，docs/实施33 v2）：本轮语音
                # **尝试过且失败** → 不再改发文字（旧「诚实回落改写+文字替发」
                # 拆除）。处置＝回复转工作台待发 + 主机弹窗 + ERROR 上报；并照
                # interject 中止同款善后：撤销 dup_guard 乐观登记 + 回滚冷却
                # 记账（未发出的回复不得占冷却位/进 last_reply——幻影回复）。
                # 注意本分支只拦「语音尝试过且失败」；语音未触发的普通文字回复
                # 不在此列（那是正文不是替代品）。
                if _voice_fail_state.get("synth_failed"):
                    _vb_acct = str(getattr(self, "account_id", "") or "default")
                    _vb_cid = f"telegram:{_vb_acct}:{chat_id}"
                    try:
                        from src.inbox.normalizer import conv_id as _vb_conv_id
                        _vb_cid = _vb_conv_id(
                            "telegram", _vb_acct, str(chat_id))
                    except Exception:
                        pass
                    _vb_queued = False
                    try:
                        from src.integrations.protocol_bridge import (
                            get_inbox_store as _vb_store_fn,
                        )
                        _vb_store = _vb_store_fn()
                        if _vb_store is not None and reply_final.strip():
                            import uuid as _vb_uuid
                            _vb_store.upsert_draft({
                                "draft_id":
                                    f"voiceblock:{_vb_uuid.uuid4().hex[:12]}",
                                "conversation_id": _vb_cid,
                                "platform": "telegram",
                                "account_id": _vb_acct,
                                "chat_key": str(chat_id),
                                "source_kind": "voice_blocked",
                                "source_id": f"{_vb_cid}:{int(time.time())}",
                                "peer_text": str(ai_text or "")[:500],
                                "draft_text": reply_final,
                                "risk_level": "low",
                                "autopilot_level": "L1",
                                "status": "pending",
                                "created_at": time.time(),
                            })
                            _vb_queued = True
                    except Exception:
                        self.logger.warning(
                            "[voice_reply] 语音失败回复转待发队列失败",
                            exc_info=True)
                    try:
                        from src.ops.delivery_block import report_block
                        report_block(
                            "voice",
                            reason=str(_voice_fail_state.get("reason")
                                       or "synth_failed"),
                            platform="telegram",
                            conversation_id=_vb_cid,
                            queued_draft=_vb_queued)
                    except Exception:
                        self.logger.debug(
                            "[voice_reply] delivery_block 上报失败",
                            exc_info=True)
                    if _dg_reg_token:
                        try:
                            from src.inbox.outbound_dup_guard import (
                                outbound_registry as _vb_dg_reg,
                            )
                            _vb_dg_reg.unregister(_dg_reg_cid, _dg_reg_token)
                        except Exception:
                            pass
                    try:
                        if _acct_snap is not None and \
                                self.skill_manager.rollback_reply_accounting(
                                    _acct_snap):
                            self.logger.info(
                                "[voice_reply] 已回滚未发出回复的冷却记账 "
                                "chat=%s", chat_id)
                    except Exception:
                        pass
                    return

                # ── 插话吸收·过期中止关口（interject_absorb，默认关）────────
                # 生成的 4-8 秒里客户又补了话 → 本条回复按旧输入写成，发出去就是
                # 「答非所问/自说自话」。仅私聊文本路径设两道关口（语音已发不可
                # 撤回，不设）：①诚实回落改写后/humanize 前；②humanize 思考延迟
                # sleep 后/真正 send 前（那又是几秒窗口）。中止＝不发送、不记
                # reply 统计、不写 context AI 回复、撤销 dup_guard 乐观登记——
                # 队列里新消息的处理天然带着本条上下文，新回复覆盖两问。
                # 2026-08-09 补：中止还必须**回滚 skill 侧已提交的冷却记账**
                # （_update_after_reply 在生成完成时就写了冷却戳/last_reply）——
                # 否则一条从未发出的回复占住冷却位，把补话本身也吞进冷却，
                # 整个爆发窗全灭（198↔104 实录：三连发零回复，10 分钟后才由
                # 轮询兜底补答）。快照在 process_message 前拍（_acct_snap）。
                def _interject_is_stale() -> bool:
                    """纯判定：生成/延迟期间对方是否又补了话（无副作用，条间复用）。"""
                    try:
                        from src.client.interject_absorb import (
                            latest_inbound_ts as _ij_latest,
                            parse_interject_cfg as _ij_cfg_fn,
                            should_abort_stale_reply as _ij_stale,
                        )
                        if _is_group:
                            return False
                        _ij_cfg = _ij_cfg_fn(
                            self.config.config
                            if hasattr(self.config, "config") else {})
                        if not _ij_cfg.get("enabled"):
                            return False
                        return _ij_stale(
                            float(message_data.get('_enq_ts') or 0.0),
                            _ij_latest(chat_id))
                    except Exception:
                        return False

                def _interject_stale_abort(where: str) -> bool:
                    try:
                        if not _interject_is_stale():
                            return False
                        self.logger.info(
                            "[interject] 生成期间对方补话，放弃本条回复 "
                            "chat=%s gate=%s", chat_id, where)
                        if _dg_reg_token:
                            try:
                                from src.inbox.outbound_dup_guard import (
                                    outbound_registry as _ij_dg_reg,
                                )
                                _ij_dg_reg.unregister(
                                    _dg_reg_cid, _dg_reg_token)
                            except Exception:
                                pass
                        # 2026-08-09：撤销这条从未发出回复的冷却记账/last_reply
                        # ——它占着冷却位会把「补话」本身也吞掉（整个爆发窗
                        # 零回复），且 AI 记忆里会多一条对方从未收到的话。
                        try:
                            if _acct_snap is not None and \
                                    self.skill_manager.rollback_reply_accounting(
                                        _acct_snap):
                                self.logger.info(
                                    "[interject] 已回滚未发出回复的冷却记账 "
                                    "chat=%s", chat_id)
                        except Exception:
                            pass
                        # 2026-08-12：生成前合并消费过的未答文本还回注册表——
                        # 本条回复没发出，那些消息仍是「未答」，下一任务照样合并
                        try:
                            _ij_burst = message_data.get('_ij_burst_entries')
                            if _ij_burst:
                                from src.client.interject_absorb import (
                                    requeue_texts as _ij_requeue,
                                )
                                _ij_requeue(chat_id, _ij_burst)
                                message_data['_ij_burst_entries'] = None
                        except Exception:
                            pass
                        return True
                    except Exception:
                        return False

                if _interject_stale_abort("pre_humanize"):
                    return
                # 发送前拟人序列（已读 → 正在输入 → 思考延迟）：与全自动 autosend 共用
                # humanize 协作器。默认 thinking_delay=0 → 仅已读、近即时（不改现手感），
                # 运营灰度打开后文本回复才有「思考+打字」节奏。只在整段回复前跑一次
                # （非逐分条），语音路径不经此（自带录音节奏）。
                try:
                    # text 用于 adaptive 延迟按长度估时；elapsed_sec=入站至今已耗时
                    # （自适应模式下从目标延迟里扣除，总响应时长更贴近而非叠加）。
                    # emotion 暂空（回复情绪信号未在此可靠在手，留作后续接线）。
                    _msg_ts = 0.0
                    try:
                        _md = getattr(message, "date", None)
                        _msg_ts = _md.timestamp() if _md is not None else 0.0
                    except Exception:
                        _msg_ts = 0.0
                    _elapsed = max(0.0, time.time() - _msg_ts) if _msg_ts > 0 else 0.0
                    await self.run_prereply_humanize(
                        message.chat.id, text=reply_final, elapsed_sec=_elapsed)
                except Exception:
                    self.logger.debug("[prereply_humanize] 调度失败（忽略）", exc_info=True)
                # 插话吸收·关口②：humanize 思考延迟 sleep 期间对方补话 → 同样中止
                if _interject_stale_abort("post_humanize"):
                    return
                sent_text_for_context = reply_final
                split_cfg = self.config.get("reply", {}).get("split_send", {})
                # P1.5 多句分条（inbox.reply_style.bubbles）：陪伴域 prompt 要求
                # 「每行会被拆成独立消息」，A 线此前只有 >max_chars 的段落级拆分，
                # 换行合同没兑现。开关开 + 私聊 + 拆得出 ≥2 条 → 按行连发（条间
                # 思考+打字延迟）；否则回落原 split_send/整段路径（行为不变）。
                _bubble_chunks = None
                try:
                    from src.inbox.reply_split import (
                        collapse_paragraphs as _clp_bub,
                        looks_like_group_chat as _lgc_bub,
                        parse_bubbles_cfg as _pbc_bub,
                        plan_bubble_gaps as _pbg_bub,
                        split_reply_parts as _srp_bub,
                    )
                    # ⚠ self.config 是 ConfigManager（main.py / companion_worker
                    # 传入的都是管理器对象，不是 dict）——2026-08-03 198 实锤：
                    # 旧写法 isinstance(self.config, dict) 恒 False → 恒拿空配置
                    # → A 线分条自上线起从未生效（两段式回复整段一条发出）。
                    # 取法对齐本类其它 20 处：优先 .config 属性，测试传 dict 兼容。
                    _bcfg_bub = _pbc_bub(
                        self.config.config
                        if hasattr(self.config, "config")
                        else (self.config
                              if isinstance(self.config, dict) else {}))
                    if (_bcfg_bub["enabled"]
                            and not _lgc_bub("telegram", str(message.chat.id))):
                        _cand_bub = _srp_bub(
                            reply_final,
                            max_parts=int(_bcfg_bub["max_parts"]),
                            max_chars=int(_bcfg_bub["max_chars"]),
                            min_tail_chars=int(_bcfg_bub["min_tail_chars"]),
                            min_total_chars=int(_bcfg_bub["min_total_chars"]),
                            per_sentence=bool(_bcfg_bub.get("per_sentence")),
                            explicit_newline_only=bool(_bcfg_bub.get(
                                "explicit_newline_only", True)),
                        )
                        if len(_cand_bub) >= 2:
                            # 保留组（2026-08-09，A 线对齐 B 线 holdout）：可拆条的
                            # 回合按 holdout_pct 随机改走整段——「有时一口气说完」
                            # 的真人多样性，也是恒定拆条模式（2026-08-08 客户实锤
                            # 「always 2 parts」）的解药。折叠必做：bubbles 开启时
                            # 拟稿合同是「每行一句」，多行原样单条发出＝被投诉的
                            # 「段1+空行+段2」形态，比拆条更糟。
                            _hp_bub = float(
                                _bcfg_bub.get("holdout_pct") or 0.0)
                            if _hp_bub > 0 and random.random() < _hp_bub:
                                reply_final = (_clp_bub(reply_final)
                                               or reply_final)
                                self.logger.info(
                                    "[reply_bubbles] A线保留组抽中，折叠整段"
                                    "发送 parts=%d chat=%s",
                                    len(_cand_bub), chat_id)
                            else:
                                _bubble_chunks = _cand_bub
                        elif _cand_bub and _cand_bub[0] != reply_final:
                            # 拆不出第二条（#210 短回复门 / 无显式换行）：纯函数已把
                            # 多行折成单段——整条路径发它，不让「每行一句」合同的
                            # 换行原样漏进一条消息（2026-08-08 被投诉形态）。
                            reply_final = _cand_bub[0]
                            sent_text_for_context = reply_final
                except Exception:
                    self.logger.debug(
                        "[reply_bubbles] A线分条判定失败，回落原路径", exc_info=True)
                    _bubble_chunks = None
                if _bubble_chunks:
                    chunks = _apply_suffix_chunks(_bubble_chunks)
                    # 条间隔预排（2026-08-09 对齐 B 线）：整组一次估值 →
                    # total_budget_sec 等比压缩保节奏形状（防 per_sentence 5 条
                    # ×20s 把一条回复拖到 80s+）；latin_per_char_sec=英文真实
                    # 手速（加权刻度会把英文打字耗时低估 ~4 倍）。
                    try:
                        _gaps_bub = _pbg_bub(
                            chunks,
                            gap_sec_lo=float(_bcfg_bub["gap_sec_lo"]),
                            gap_sec_hi=float(_bcfg_bub["gap_sec_hi"]),
                            per_char_sec=float(_bcfg_bub["per_char_sec"]),
                            latin_per_char_sec=_bcfg_bub.get(
                                "latin_per_char_sec"),
                            max_gap_sec=float(
                                _bcfg_bub.get("max_gap_sec", 6.0)),
                            total_budget_sec=float(
                                _bcfg_bub.get("total_budget_sec") or 0.0),
                        )
                    except Exception:
                        _gaps_bub = []
                    # 逐条发送：条间「想（静默）→ 打字（挂正在输入续挂）」与首条前
                    # 同一节奏模型；条间对方插话 → 停发剩余条（真人被打断会停，
                    # 新消息的回复自带完整上下文覆盖两问）。上下文只记**实际发出**
                    # 的部分——记了没发的话，下一轮 AI 会以为自己说过。
                    _sent_chunks: list = []
                    _bubbles_interrupted = False
                    for i, chunk in enumerate(chunks):
                        if i > 0:
                            # 兜底 3.0＝gap_sec_lo 时代缺省（规划器异常不回机关枪）
                            _gap_bub = (_gaps_bub[i - 1]
                                        if i - 1 < len(_gaps_bub) else 3.0)
                            try:
                                from src.integrations.humanize_metrics import (
                                    record_bubble_gap as _rbg_bub,
                                )
                                _rbg_bub("aline", "telegram", _gap_bub)
                            except Exception:
                                pass
                            try:
                                from src.inbox.humanize import (
                                    estimate_typing_lead as _etl_bub,
                                    run_presend_humanization as _rph_bub,
                                )

                                async def _tp_bub(_action):
                                    await self._send_typing_action(
                                        message.chat.id)

                                await _rph_bub(
                                    delay=_gap_bub, action="typing",
                                    typing=_tp_bub, sleep=asyncio.sleep,
                                    typing_lead_sec=_etl_bub(
                                        chunk,
                                        per_char_sec=float(
                                            _bcfg_bub["per_char_sec"]),
                                        latin_per_char_sec=_bcfg_bub.get(
                                            "latin_per_char_sec")),
                                )
                            except Exception:
                                await asyncio.sleep(_gap_bub)
                            if _interject_is_stale():
                                self.logger.info(
                                    "[interject] 分条间对方补话，停发剩余 "
                                    "%d/%d 条 chat=%s",
                                    len(chunks) - len(_sent_chunks),
                                    len(chunks), chat_id)
                                _bubbles_interrupted = True
                                break
                        await self._send_reply(message, chunk, parse_mode=_parse_mode)
                        _sent_chunks.append(chunk)
                    sent_text_for_context = (
                        "\n\n".join(_sent_chunks) if _sent_chunks
                        else "\n\n".join(chunks))
                    # #210 B：一行汇总「实发几条」。逐条的「已回复消息」日志按条打，
                    # 报障按关键词摘日志时只见第一句（82BF95 实录）——这里把条数
                    # 与总条数钉在一行，工作台行数（_send_reply 逐条镜像）应与之一致。
                    self.logger.info(
                        "[reply_bubbles] A线已拆 %d/%d 条发出（工作台按条镜像）chat=%s%s",
                        len(_sent_chunks), len(chunks), chat_id,
                        " interrupted" if _bubbles_interrupted else "")
                    try:
                        from src.inbox.reply_split import record_bubble_send
                        record_bubble_send(
                            "aline", len(_sent_chunks) or len(chunks),
                            partial=_bubbles_interrupted)
                    except Exception:
                        pass
                elif split_cfg.get("enabled", False):
                    max_chars = int(split_cfg.get("max_chars_per_message", 120))
                    min_seg = int(split_cfg.get("min_segments_to_split", 2))
                    delay = float(split_cfg.get("delay_between_seconds", 0.35))
                    chunks = self._split_reply_for_send(reply_final, max_chars, min_seg)
                    chunks = _apply_suffix_chunks(chunks)
                    sent_text_for_context = "\n\n".join(chunks)
                    if len(chunks) > 1:
                        for i, chunk in enumerate(chunks):
                            await self._send_reply(message, chunk, parse_mode=_parse_mode)
                            if i < len(chunks) - 1 and delay >= 0:
                                jitter = float(split_cfg.get("delay_jitter_seconds", 0) or 0)
                                await asyncio.sleep(delay + (random.uniform(0, jitter) if jitter > 0 else 0))
                    else:
                        await self._send_reply(message, chunks[0], parse_mode=_parse_mode)
                else:
                    if suffix:
                        if suffix_html:
                            sent_text_for_context = _html_escape(reply_final) + suffix
                        else:
                            sent_text_for_context = reply_final + suffix
                    else:
                        sent_text_for_context = reply_final
                    await self._send_reply(
                        message, sent_text_for_context, parse_mode=_parse_mode
                    )
                send_time = time.time()
                total_time_ms = (send_time - start_time) * 1000
                m = _metrics()
                if m:
                    m.record_reply()
                    m.record_response_time_ms(total_time_ms)
                    m.set_queue_size(self.message_queue.qsize())

                # 如果启用了上下文管理器，记录AI回复（存术语校正后的内容）
                if self.context_manager:
                    try:
                        self.context_manager.add_message(
                            chat_id=chat_id,
                            user_id=0,  # AI用户
                            username="小灵",
                            text=sent_text_for_context,
                            is_ai=True
                        )
                    except Exception as e:
                        self.logger.warning(f"记录AI回复到上下文失败: {e}")

                # 记录响应时间统计
                total_time = send_time - start_time
                process_duration = process_time - receive_time
                send_duration = send_time - process_time
                self.logger.info(
                    f"响应时间统计 - 总计: {total_time:.2f}s, "
                    f"处理: {process_duration:.2f}s, "
                    f"发送: {send_duration:.2f}s"
                )

                if he_forward_spec:
                    try:
                        await self._forward_escalation_user_to_agents(he_forward_spec)
                    except Exception as fwd_ex:
                        self.logger.warning(
                            "人工转接: 向客服私聊转发用户原话失败: %s", fwd_ex
                        )

            elif reply_final and _voice_sent:
                # Voice was sent — still record metrics & context
                send_time = time.time()
                total_time_ms = (send_time - start_time) * 1000
                m = _metrics()
                if m:
                    m.record_reply()
                    m.record_response_time_ms(total_time_ms)
                    m.set_queue_size(self.message_queue.qsize())
                if self.context_manager:
                    try:
                        self.context_manager.add_message(
                            chat_id=chat_id,
                            user_id=0,
                            username="小灵",
                            text=reply_final,
                            is_ai=True,
                        )
                    except Exception as _e:
                        self.logger.warning("记录语音回复到上下文失败: %s", _e)

                if he_forward_spec:
                    try:
                        await self._forward_escalation_user_to_agents(he_forward_spec)
                    except Exception as fwd_ex:
                        self.logger.warning(
                            "人工转接: 向客服私聊转发用户原话失败: %s", fwd_ex
                        )

        except Exception as e:
            self.logger.error(f"异步处理消息失败: {e}")
            m = _metrics()
            if m:
                m.record_error()

    # ── GXP 命令结果追踪 + 超时提醒（队列化，支持同群并发） ────

    _GXP_TIMEOUT_SEC = 60
    _GXP_MAX_QUEUE = 10

    _CMD_HINT_MAP = {
        "/cxye": r"余额|balance",
        "/hl": r"汇率|exchange|rate",
        "/cgl": r"成功率|success",
        "/cxds": r"代收|collection|deposit",
        "/cxdf": r"提现|withdraw|payout",
        "/utr": r"utr|UTR|补单",
        "/htds": r"回调.*代收|callback.*deposit",
        "/htdf": r"回调.*提现|callback.*withdraw",
    }

    def record_gxp_command(self, chat_id: int, cmd: str, user_id: int = 0, user_msg_id: int = 0):
        ts = time.time()
        entry = {"cmd": cmd, "ts": ts, "user_id": user_id, "user_msg_id": user_msg_id}
        queue = self._gxp_pending.setdefault(chat_id, [])
        queue.append(entry)
        if len(queue) > self._GXP_MAX_QUEUE:
            queue[:] = queue[-self._GXP_MAX_QUEUE:]
        cutoff = ts - 120
        for cid in [c for c, q in self._gxp_pending.items() if not q or q[-1]["ts"] < cutoff]:
            del self._gxp_pending[cid]
        try:
            asyncio.get_running_loop().create_task(self._gxp_timeout_check(chat_id, ts, cmd))
        except RuntimeError:
            pass

    async def _gxp_timeout_check(self, chat_id: int, original_ts: float, cmd: str):
        await asyncio.sleep(self._GXP_TIMEOUT_SEC + 2)
        queue = self._gxp_pending.get(chat_id, [])
        idx = next((i for i, e in enumerate(queue) if e["ts"] == original_ts), -1)
        if idx < 0:
            return
        entry = queue.pop(idx)
        if not queue:
            self._gxp_pending.pop(chat_id, None)
        msg_id = entry.get("user_msg_id") or None
        try:
            text = self.i18n.t("gxp_timeout", chat_id, sec=self._GXP_TIMEOUT_SEC, cmd=cmd)
            await self.client.send_message(chat_id=chat_id, text=text, reply_to_message_id=msg_id)
            self.logger.info("[GXP追踪] 超时提醒已发送: %s", cmd)
        except Exception as e:
            self.logger.warning("[GXP追踪] 超时提醒发送失败: %s", e)

    def _match_pending_by_hint(self, queue: list, bot_text: str) -> int:
        """尝试根据 bot 回复内容精确匹配队列中的命令，返回 index；-1 表示无匹配"""
        for i, entry in enumerate(queue):
            cmd_prefix = entry["cmd"].split()[0] if entry["cmd"] else ""
            pattern = self._CMD_HINT_MAP.get(cmd_prefix)
            if pattern and re.search(pattern, bot_text, re.IGNORECASE):
                return i
        return -1

    async def _handle_gxp_bot_reply(self, message):
        chat_id = message.chat.id
        queue = self._gxp_pending.get(chat_id, [])
        if not queue:
            return
        bot_text = (message.text or message.caption or "").strip()
        if not bot_text:
            return
        idx = self._match_pending_by_hint(queue, bot_text)
        if idx < 0:
            idx = 0
        entry = queue.pop(idx)
        if not queue:
            self._gxp_pending.pop(chat_id, None)
        if time.time() - entry["ts"] > self._GXP_TIMEOUT_SEC:
            return
        if re.search(r"查询失败|不存在|无此订单|已过期|failed|not found", bot_text):
            relay = self.i18n.t("gxp_result_fail", chat_id, text=bot_text[:200])
        elif re.search(r"查询成功|操作成功|success", bot_text):
            relay = self.i18n.t("gxp_result_ok", chat_id, text=bot_text[:500])
        else:
            relay = self.i18n.t("gxp_result_other", chat_id, text=bot_text[:500])
        try:
            await self.client.send_message(
                chat_id=chat_id,
                text=relay,
                reply_to_message_id=entry.get("user_msg_id") or None,
            )
            self.logger.info("[GXP追踪] 已转告结果 (queue_remaining=%d): %s...", len(queue), relay[:80])
        except Exception as e:
            self.logger.warning("[GXP追踪] 转告失败: %s", e)

        await self._check_success_rate_alert(chat_id, bot_text, entry.get("cmd", ""))

    # ── 通道成功率告警 ──────────────────────────────────────────

    async def _check_success_rate_alert(self, chat_id: int, bot_text: str, cmd: str):
        if "/cgl" not in cmd:
            return
        alert_cfg = self.config.get("channel_alerts", {})
        if not alert_cfg.get("enabled"):
            return
        threshold = float(alert_cfg.get("success_rate_threshold", 80))
        m = re.search(r"(\d+\.?\d*)%", bot_text)
        if not m:
            return
        rate = float(m.group(1))
        if rate >= threshold:
            return
        admin_chat = self.config.get("telegram", {}).get("admin_chat_id")
        alert_text = (
            f"⚠️ 成功率告警\n"
            f"当前成功率: {rate}%（阈值: {threshold}%）\n"
            f"来源: {bot_text[:150]}"
        )
        self.logger.warning("[告警] 成功率 %.1f%% < 阈值 %.1f%%", rate, threshold)
        if admin_chat:
            try:
                await self.client.send_message(chat_id=int(admin_chat), text=alert_text)
            except Exception as e:
                self.logger.warning("[告警] 发送失败: %s", e)
        self.event_tracker.track("alert_success_rate", chat_id, detail=f"{rate}%<{threshold}%")

    # ── 配置热重载通知 ──────────────────────────────────────────

    def _register_reload_notifier(self):
        admin_chat_id = self.config.get('telegram', {}).get('admin_chat_id')
        if not admin_chat_id:
            self.logger.debug("未配置 admin_chat_id，热重载通知已跳过")
            return

        def _on_reload():
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    loop.create_task(self._send_reload_notification(int(admin_chat_id)))
            except Exception:
                pass

        self.config.on_reload(_on_reload)
        self.logger.info("热重载通知已注册 → chat_id=%s", admin_chat_id)

    async def _send_reload_notification(self, chat_id: int):
        try:
            import datetime
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            text = self.i18n.t("reload_notify", chat_id, ts=ts)
            await self.client.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            self.logger.warning("热重载通知发送失败: %s", e)

    # ── 定时任务调度 ────────────────────────────────────────────

    def _start_scheduler(self):
        from src.utils.scheduler import TaskScheduler
        cfg = self.config.config if hasattr(self.config, 'config') else {}
        self._scheduler = TaskScheduler.from_config(cfg, self._scheduled_send)
        if self._scheduler._tasks:
            self._scheduler.start()

    async def _scheduled_send(self, chat_id: int, command: str):
        try:
            await self.client.send_message(chat_id=chat_id, text=command)
            self.logger.info("[定时任务] 已发送: chat=%s cmd=%s", chat_id, command)
        except Exception as e:
            self.logger.warning("[定时任务] 发送失败: %s", e)
