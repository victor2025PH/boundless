# -*- coding: utf-8 -*-
"""spoken_style 真人感文本层桥接（platform/spoken_style 交付包 → chengjie 回复管线）。

包是 avatarhub 线交付的「像真人说话」文本层（README 见 platform/spoken_style/），
本桥接把它以**默认关闭**的姿势挂进三处：
  L1 稳定 system 段  → ai_client._build_system_instruction 末尾（说话指纹/副语言/情绪协议）
  L2 轮变尾注        → _generate_reply_openai_compat 拼 user 消息处（说话稿档位+意图篇幅）
  L3 出口清洁        → generate_reply_with_intent 出口（剥情绪/副语言标记）

设计约束（与 chengjie 既有真人感层的关系，勿破坏）：
  * chengjie 自有 persona 体系（persona_manager）——桥接**不注入**包里的人设卡，
    只取与之正交的层（说话稿/篇幅/指纹/协议）。
  * chengjie 已有生成层口语分叉（spoken_variant）与 TTS 前口语化（voice_colloquial*）：
    L2 与它们语义相近但作用位置不同（L2 让书面主回复本身口语化）。同时开不坏但冗余，
    建议试点期二选一（见 config 注释）。
  * 模板是中文口语指令——多语种部署必须 zh_only（默认开）：用户消息非中文主体时
    L2 不注入，防止把「嘛/呢/啦」指令塞进乌尔都语/英语对话。
  * 包缺席/加载失败/开关关闭 → 一切函数退化为 no-op（telemetry._load_emitter 同哲学），
    绝不影响主链。

配置（config.yaml ai.spoken_style，全部可缺省）：
    enabled: false / level: 2 / zh_only: true / role: "" / paraling: false / emotion_tags: false
    rewrite: false / rewrite_llm: "" / rewrite_model: ""   ← L4 口语化改写（见 rewrite_reply）
"""
from __future__ import annotations

import logging
import re
import sys
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_PKG_DIR_OVERRIDE: Optional[str] = None    # 测试注入用
_LOCK = threading.Lock()
_MOD: Any = None
_FAILED = False

_HAN_RE = re.compile(r"[\u4e00-\u9fff]")

# 灰度观测计数（进程内累计；stats() 随取随读，L4 每 20 次尝试打一行 INFO 汇总——
# 灰度期「事实锁拒绝率高不高」不用等人翻 DEBUG 日志）
_STATS_LOCK = threading.Lock()
_STATS = {
    "l1_inject": 0,        # system 段真注入次数
    "l2_inject": 0,        # 轮变尾注真注入次数
    "l2_skip_lang": 0,     # 因非中文主体被 zh_only 拦下的次数（外语占比的直接读数）
    "l3_changed": 0,       # 出口清洁真剥掉了东西的次数
    "l4_attempt": 0,       # 改写尝试
    "l4_applied": 0,       # 改写生效
    "l4_passthrough": 0,   # 事实锁拒绝/超时/后端失败 → 原句直通
}
_L4_LOG_EVERY = 20


def _bump(key: str) -> None:
    with _STATS_LOCK:
        _STATS[key] = _STATS.get(key, 0) + 1


def stats() -> dict:
    """观测计数快照（灰度巡检用；/admin 或日志脚本随取随读）。"""
    with _STATS_LOCK:
        return dict(_STATS)


def _find_platform_dir() -> Optional[Path]:
    """从本文件向上逐级找 ``platform/spoken_style/__init__.py``（telemetry 同款姿势）。"""
    if _PKG_DIR_OVERRIDE is not None:
        p = Path(_PKG_DIR_OVERRIDE)
        return p if (p / "spoken_style" / "__init__.py").is_file() else None
    for parent in Path(__file__).resolve().parents:
        cand = parent / "platform" / "spoken_style" / "__init__.py"
        try:
            if cand.is_file():
                return cand.parent.parent
        except OSError:
            continue
    return None


def _load():
    """惰性加载 spoken_style 包；失败即永久 no-op。"""
    global _MOD, _FAILED
    if _MOD is not None:
        return _MOD
    if _FAILED:
        return None
    with _LOCK:
        if _MOD is not None or _FAILED:
            return _MOD
        try:
            pdir = _find_platform_dir()
            if pdir is None:
                raise FileNotFoundError("platform/spoken_style 未找到")
            if str(pdir) not in sys.path:
                # 加的是 platform 目录本身，import 的名字是 spoken_style——
                # 不会与标准库 platform 模块冲突（那是 import platform 才踩的坑）。
                sys.path.insert(0, str(pdir))
            import spoken_style  # noqa: PLC0415
            _MOD = spoken_style
        except Exception as e:
            _FAILED = True
            logger.info("spoken_style 包不可用，桥接退化为 no-op: %s", e)
    return _MOD


def _cfg(config) -> dict:
    """读 ai.spoken_style 配置段；config 形态兼容 ConfigManager（.config dict）。"""
    try:
        root = (config.config or {}) if config is not None else {}
        c = ((root.get("ai") or {}).get("spoken_style") or {})
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def _enabled(config) -> bool:
    return bool(_cfg(config).get("enabled", False))


def _is_zh(text: str) -> bool:
    """用户消息是否以中文为主体（han 占比 ≥40% 且 ≥2 字）——L2 语种门控。"""
    t = (text or "").strip()
    if not t:
        return False
    han = len(_HAN_RE.findall(t))
    return han >= 2 and han >= len(t) * 0.4


def system_block(config, role: str = "") -> str:
    """L1：稳定 system 追加段（说话指纹/副语言/情绪协议；不含人设卡）。空串=不注入。

    ``role`` 是本会话人设的口称名（ai_client 从 context._resolved_persona_name 透传），
    优先于配置里的静态 ai.spoken_style.role——智聊是多人设产品，说话指纹按
    「哪个人设在说话」分流才有意义；指纹键契约=人设口称名（resolve_spoken_name 输出）。
    """
    if not _enabled(config):
        return ""
    ss = _load()
    if ss is None:
        return ""
    c = _cfg(config)
    try:
        blocks = ss.system_blocks(
            persona_card=None,                      # chengjie 自有 persona 体系，不双注入
            role=str(role or c.get("role") or ""),
            laugh=False,                            # 无真笑素材一律呼吸版（假笑更毁真实感）
            emotion_tags=bool(c.get("emotion_tags", False)),
        )
        if not c.get("paraling", False):
            blocks = [b for b in blocks if not b.startswith("【副语言】")]
        out = "\n\n".join(b for b in blocks if b)
        if out:
            _bump("l1_inject")
        return out
    except Exception:
        logger.debug("spoken_style system_block 失败，跳过", exc_info=True)
        return ""


def turn_tail(config, user_text: str) -> str:
    """L2：本轮 user 消息的轮变尾注。非中文主体消息（zh_only 默认开）返回空串。"""
    if not _enabled(config):
        return ""
    ss = _load()
    if ss is None:
        return ""
    c = _cfg(config)
    if bool(c.get("zh_only", True)) and not _is_zh(user_text):
        _bump("l2_skip_lang")
        return ""
    try:
        level = int(c.get("level", 2))
        if level >= 2 and ss.naturalness.STORY_INTENT_RE.search(user_text or ""):
            level = 3                               # 故事/陪伴意图自动升档
        out = ss.turn_tail_hint(user_text or "", level=level)
        if out:
            _bump("l2_inject")
        return out
    except Exception:
        logger.debug("spoken_style turn_tail 失败，跳过", exc_info=True)
        return ""


async def rewrite_reply(config, reply: str, role: str = "") -> str:
    """L4：口语化改写（本地小模型整段重写句子架构，事实锁把关；失败/超时/拒绝=原句直通）。

    前提（都写在 config 注释里）：ai.spoken_style.rewrite: true，且 role 在包内
    data/speech_prints.json 有说话指纹（改写提示按指纹分流，无指纹不开——文本×
    声学要配套是包侧拍板）。改写后端缺省 192.168.0.173 qwen14b（同 LAN 零 API 费），
    rewrite_llm / rewrite_model 可换成任何 OpenAI 兼容端点。
    非中文主体回复直接跳过（改写器是中文口语手艺）。
    ``role`` 同 system_block：会话人设口称名优先，缺省回落配置静态 role。
    """
    if not reply or not _enabled(config):
        return reply
    c = _cfg(config)
    if not c.get("rewrite", False):
        return reply
    role = str(role or c.get("role") or "")
    if not role or not _is_zh(reply):
        return reply
    ss = _load()
    if ss is None:
        return reply
    try:
        cr = ss.colloquial_rewrite
        # 包的灰度旗标是文件机制（data/conv_colloquial.flag）；桥接由 config 单点控制，
        # 直接钉住旗标缓存为「全开」（TTL 检查 now-inf>2 恒 False，永不回读文件）——
        # 不落盘、不弄脏 git 工作区，开关语义完全交给 ai.spoken_style.rewrite。
        cr._flag_cache["t"] = float("inf")
        cr._flag_cache["v"] = True
        if c.get("rewrite_llm"):
            cr._LLM_URL = str(c["rewrite_llm"])       # 模块全局在调用时读，改了即生效
        if c.get("rewrite_model"):
            cr._LLM_MODEL = str(c["rewrite_model"])
        fn = cr.build_rewrite_fn(role)
        if fn is None:
            return reply
        _bump("l4_attempt")
        out = await fn(reply, first=True)
        _bump("l4_applied" if out else "l4_passthrough")
        with _STATS_LOCK:
            n, ok, pt = _STATS["l4_attempt"], _STATS["l4_applied"], _STATS["l4_passthrough"]
        if n % _L4_LOG_EVERY == 0:
            # 直通率高于三成 = 事实锁在大量拒绝（改写模型/提示词该调了）或后端在超时
            logger.info("spoken_style L4 汇总: 尝试 %d / 生效 %d / 直通 %d (直通率 %.0f%%)",
                        n, ok, pt, pt * 100.0 / max(n, 1))
        return out if out else reply                  # None=事实锁/超时直通
    except Exception:
        logger.debug("spoken_style rewrite 失败，原样返回", exc_info=True)
        return reply


def clean_reply_text(config, reply: str) -> str:
    """L3：出口清洁——剥情绪标记/副语言标记/技术 token。未启用时原样返回。

    仅返回干净文本（chengjie 文字主链所需）；语音链要拿情绪键/停顿建议时，
    直接用 spoken_style.clean_reply(reply) 的完整 dict（见包 README「TTS 侧」）。
    """
    if not reply or not _enabled(config):
        return reply
    ss = _load()
    if ss is None:
        return reply
    try:
        out = ss.clean_reply(reply)["text"]
        if out and out != reply:
            _bump("l3_changed")
        return out if out else reply                # 清洁不许清成空（兜底原句）
    except Exception:
        logger.debug("spoken_style clean_reply 失败，原样返回", exc_info=True)
        return reply
