"""
Persona Manager — handles loading, binding, and prompt assembly for personas.

Supports:
- Loading persona from domain pack persona.yaml
- Per-chat persona binding (different groups use different personas)
- Dynamic system prompt assembly: persona context + domain prompt + KB context
- Runtime persona override via Web admin API
"""

import logging
import copy
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

logger = logging.getLogger("PersonaManager")

RUNTIME_PERSONA_FILENAME = "persona_runtime.yaml"
PROFILES_RUNTIME_FILENAME = "profiles_runtime.yaml"
BINDINGS_RUNTIME_FILENAME = "bindings_runtime.yaml"
GLOBAL_RULES_FILENAME = "global_rules.yaml"
_HISTORY_MAXLEN = 3  # versions kept per profile

# ── prompt 数据链登记表（K1，2026-07-28）────────────────────────────────────
# 背景：canonical persona schema 里「数据填了但从来没进 prompt」的断链已连续修过
# 三轮（context.hobbies → tastes.* → age/gender），每次都是真机考题当场抓获：
#   林小雨 age=22 → AI 答「我今年20岁」；赵老师 age=58 → AI 答「六十八了」。
# 一次修一个字段治不了本，因此把「每个字段的表态」变成显式登记 + 门禁
# （tests/test_persona_prompt_chain.py）：新增 schema 字段若两张表都没登记 → 红。
#
# 约定：点路径与 profiles_runtime.yaml 结构一致；登记某个 dict 路径即表示
# 「整棵子树」按同一表态处理（如 voice_profile 整体豁免、context.family 整体消费）。
PROMPT_CONSUMED_FIELDS = frozenset({
    # 身份
    "name",                              # 「你是X」+ 身份硬锁
    "role",
    "age",                               # 【年龄事实·硬锁】（K1 修复）
    "gender",                            # 【性别事实】（K1 修复）
    "names.full_western", "names.english", "names.german",
    "names.french", "names.nickname", "names.usage_notes",
    # 人生素材
    "background",
    "appearance",                        # 【你的外貌】（K1 修复；与自拍图口径同源）
    "context.family",                    # 【你的家人】（K1 修复，整棵子树）
    "context.hobbies",
    "context.specific_memories",
    "context.emotional_triggers.positive",
    "context.emotional_triggers.negative",
    "context.emotional_triggers.deep_empathy",
    "context.filipino_connection",       # 【你的在地文化】（K1 修复，整棵子树）
    "context.schedule",                  # 【你的作息】（K1 修复，整棵子树）
    "tastes.likes", "tastes.dislikes", "tastes.opinions",
    # 性格与说话
    "personality.traits", "personality.style", "personality.quirks",
    "personality.humor", "personality.temperament", "personality.emoji_level",
    "speaking.emoji_level",              # personality.emoji_level 的兼容位（K1 修复）
    "speaking.forbidden_phrases", "speaking.openers",
    "speaking.forbidden_opener_words", "speaking.reply_length",
    "speaking.max_reply_sentences", "speaking.language_follow",
    "speaking.banter_profanity",
    # 身份/边界/情绪
    "identity.deny_ai", "identity.deny_ai_reply", "identity.claim_human",
    "boundaries.topics_to_avoid",
    "capabilities.video_call",           # 反向消费：为真时撤掉「不能视频」约束
    "capabilities.photos",               # 反向消费：为真时撤掉「不能发照片」约束
                                         # （人设级发图总闸，默认关；SSOT=
                                         #  src/companion/photo_capability.py）
    "emotion.frustrated_response",
})

PROMPT_EXEMPT_FIELDS = frozenset({
    # 运行时配置 / 内部字段——本就不该出现在给 LLM 的人设文本里
    "id",                    # 内部主键（profile store 的键），说出来就是穿帮
    "_mrpa_source",          # 导入来源标记，纯内部血缘
    "tags",                  # 运营分组/路由元数据（Studio 筛选、bulk_bind），非人设事实
    "voice_profile",         # TTS 后端/音色/参考音路径：语音链运行时配置
    # 由**其它子系统**各自拼块注入，这里注入会形成双源真相
    "location",              # persona_location：本地时钟/天气/场景块的单一事实源
    "life_arc",              # deep_persona：L1 生活线按 stride_days 派生自己的块
    "selfie_scenes",         # 生图场景池；「AI 此刻在哪」归 scene_state SSOT（Phase18）
    # 非人设台词
    "boundaries.escalation_phrases",  # 转人工触发词（退款/投诉/威胁/诈骗）——是匹配
                                      # 信号不是话术；注入反而诱导 AI 主动说这些词
})

# 已表态的字段全集。门禁比对「真实 schema ⊆ 本集合」，
# 新字段两边都没登记即红（见 tests/test_persona_prompt_chain.py）。
PERSONA_SCHEMA_FIELDS = PROMPT_CONSUMED_FIELDS | PROMPT_EXEMPT_FIELDS

# emoji_level 别名归一：低/中档历史上写法不一（low/medium 曾整档静默失效——
# chen_meiling=low、su_wan=medium 在旧 if/elif 链里一个分支都不命中）。
_EMOJI_LEVEL_ALIASES = {
    "low": "minimal",
    "medium": "moderate",
    "balanced": "moderate",
    "very_rich": "high",
    "many": "high",
}

_GENDER_WORDS = {
    "female": "女性", "f": "女性", "woman": "女性", "女": "女性", "女性": "女性",
    "male": "男性", "m": "男性", "man": "男性", "男": "男性", "男性": "男性",
}

# context 子块的中文标签（未登记的子键按原键名输出，不吞数据）
_FAMILY_LABELS = {
    "father": "父亲", "mother": "母亲", "parents": "父母",
    "brother": "哥哥/弟弟", "sister": "姐姐/妹妹",
    "son": "儿子", "daughter": "女儿", "child": "孩子", "children": "孩子",
    "husband": "丈夫", "wife": "妻子", "partner": "伴侣",
    "ex_husband": "前夫", "ex_wife": "前妻",
    "grandpa": "爷爷/外公", "grandma": "奶奶/外婆", "pet": "宠物",
}
_SCHEDULE_LABELS = {
    "work_hours": "上班时间", "daytime_tone": "白天状态",
    "night_tone": "夜里状态", "rest_day": "休息日", "sleep": "睡眠",
}
_CULTURE_LABELS = {
    "language": "语言", "food": "饮食", "values": "价值观",
    "festival": "节日", "custom": "习俗",
}


def _join_text_items(v: Any) -> str:
    """list/tuple → 「、」拼接；str → strip；其余 → 空串。全空返回空串。"""
    if isinstance(v, (list, tuple)):
        return "、".join(str(x).strip() for x in v if str(x).strip())
    return str(v or "").strip()


def _labelled_pairs(v: Any, labels: Dict[str, str]) -> str:
    """dict → 「父亲：X；母亲：Y」；str/list → 直接拼接。空值一律返回空串。

    未登记的子键按原键名输出——宁可标签生硬，也不能把运营填的内容静默吞掉。
    """
    if isinstance(v, dict):
        parts = [
            f"{labels.get(str(k), str(k))}：{_join_text_items(val)}"
            for k, val in v.items()
            if _join_text_items(val)
        ]
        return "；".join(parts)
    return _join_text_items(v)


def _persona_age(persona: Dict[str, Any]) -> int:
    """人设年龄（1..120 的整数）；缺失/脏数据/越界 → 0 表示「不注入」。

    绝不让 "你今年None岁" 这类占位漏进 prompt。
    """
    raw = persona.get("age")
    if isinstance(raw, bool) or raw is None:
        return 0
    try:
        if isinstance(raw, str):
            raw = raw.strip()
            if not raw.isdigit():
                return 0
        age = int(raw)
    except (TypeError, ValueError):
        return 0
    return age if 1 <= age <= 120 else 0


def _persona_gender_word(persona: Dict[str, Any]) -> str:
    """性别的中文说法；缺失 → 空串（不注入）。未知取值原样透出，不猜。"""
    raw = str(persona.get("gender") or "").strip()
    if not raw:
        return ""
    return _GENDER_WORDS.get(raw.lower(), raw)


def _canonical_emoji_level(persona: Dict[str, Any]) -> str:
    """emoji 档位：``personality.emoji_level`` 优先，回落 ``speaking.emoji_level``。

    两处都有真实人设在用（zhang_jingguang 只填了 speaking 侧），旧代码只读
    personality 侧 → speaking 侧填了等于没填。别名一并归一（low/medium…）。
    """
    p = persona.get("personality")
    s = persona.get("speaking")
    raw = ""
    if isinstance(p, dict):
        raw = str(p.get("emoji_level") or "").strip()
    if not raw and isinstance(s, dict):
        raw = str(s.get("emoji_level") or "").strip()
    lv = raw.lower()
    return _EMOJI_LEVEL_ALIASES.get(lv, lv)

# Default persona when none is configured
_DEFAULT_PERSONA: Dict[str, Any] = {
    "name": "Assistant",
    "role": "AI 助手",
    "personality": {
        "traits": ["友好", "专业"],
        "style": "自然聊天风格",
        "emoji_level": "moderate",
    },
    "speaking": {
        "openers": [],
        "forbidden_phrases": ["作为一个AI"],
        "reply_length": "moderate",
        "max_reply_sentences": 5,
        "language_follow": True,
    },
    "identity": {
        "deny_ai": False,
        "deny_ai_reply": "",
        "claim_human": False,
    },
    "boundaries": {
        "topics_to_avoid": [],
        "escalation_phrases": [],
    },
}


def _same_file(a: Path, b: Path) -> bool:
    """两个路径是否指向同一物理文件（联接/符号链接安全）。任一不存在 → 比较路径。"""
    try:
        if a.exists() and b.exists():
            import os as _os
            return _os.path.samefile(str(a), str(b))
    except OSError:
        pass
    return a == b


def profile_rev(profile: Optional[Dict[str, Any]]) -> str:
    """**通用**内容指纹（乐观锁 rev，多开治理 2026-07-29）。

    canonical JSON（键排序）→ sha1 前 12 位；空/None → ""。纯函数：同内容恒同 rev、
    任一字段变化即变。用于「加载时取 rev → 保存时带 expected_rev → 不一致 409」的
    丢更新防线（两个窗口/两坐席同编一份文档，后保存方被拦下确认再覆盖）。

    现有消费方（都是**整文档替换**语义的长编辑表单）：
      - 人设档案 ``/api/personas/profiles/{id}``（首个落地点，函数名由此而来）
      - 全局规则 ``/api/persona/global-rules``（影响所有人设，覆盖破坏面最大）
      - 意图关键词 ``/api/settings/intent-keywords``（整字典替换，删除意图必须生效）

    ⚠️ **不要**给字段级 patch 语义的端点加 rev（如 ``/api/settings/save`` 的
    ``{section, fields}``）：那类端点两窗口改不同字段本就能正确合并，加 rev 只会制造
    假冲突（A 改 temperature、B 改 max_tokens 本该都成功）。判据＝**整文档替换才需要**。

    ``None``（文档不存在）→ ``""``；``{}``（存在但为空）→ 真指纹。二者刻意分开：
    若空文档也返回 ""，前端就不会带 expected_rev，「两个窗口都从空表开始各加一批
    内容」这种最典型的丢更新场景反而**毫无保护**（意图关键词表实测踩到）。
    而 ``""`` 保留给「档案已被删除」——此时任何 expected_rev 都对不上，正确地 409。
    """
    if profile is None:
        return ""
    import hashlib
    import json as _json
    try:
        blob = _json.dumps(profile, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        blob = repr(sorted((str(k), str(v)) for k, v in profile.items()))
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:12]


class PersonaManager:
    """Manages persona lifecycle, multi-group binding, and prompt assembly."""

    _instance: Optional["PersonaManager"] = None

    # Role/business labels that must NEVER be spoken as the bot's own name.
    # The domain 'conversion' persona is *named* '线上陪伴' (an operator-facing
    # label — persona.yaml itself notes 展示名以 config / 主系统提示为准), but if
    # persona resolution falls back to it the model will answer 「我叫线上陪伴」and
    # send it to the customer (observed 2026-07-01). These are filtered out of the
    # *spoken* name so the identity lock never instructs the model to say them.
    _FORBIDDEN_SPOKEN_NAMES = frozenset({
        "线上陪伴", "在线陪伴", "線上陪伴", "在線陪伴",
        "线上客服", "在线客服", "線上客服", "在線客服",
        "客服", "小客服", "助手", "小助手", "ai助手", "ai 助手",
        "机器人", "機器人", "assistant",
    })
    # Neutral, real-sounding fallback when no persona resolves to a real name.
    # A companion bot must have a person-like name, never a service label.
    _DEFAULT_SPOKEN_NAME = "小柔"

    @classmethod
    def resolve_spoken_name(
        cls,
        persona: Any,
        *,
        name_override: str = "",
        fallback: str = "",
    ) -> str:
        """Resolve the name the bot may *speak* as its own, never a role label.

        Priority: explicit ``name_override`` (config ``ai.ai_name``) → persona's own
        ``name`` → ``fallback`` (config ``ai.fallback_display_name``) → neutral default.
        Any candidate that is a known role/business label (``线上陪伴`` etc.) or empty
        is skipped, so 「我叫线上陪伴」can never be produced at the source.
        """
        def _real(x: Any) -> str:
            s = str(x or "").strip()
            return s if s and s.lower() not in cls._FORBIDDEN_SPOKEN_NAMES else ""

        pname = ""
        if isinstance(persona, dict):
            pname = persona.get("name", "")
        elif isinstance(persona, str):
            pname = persona
        return (
            _real(name_override)
            or _real(pname)
            or _real(fallback)
            or cls._DEFAULT_SPOKEN_NAME
        )

    def __init__(self):
        self._default_persona: Dict[str, Any] = copy.deepcopy(_DEFAULT_PERSONA)
        self._chat_personas: Dict[str, Dict[str, Any]] = {}  # inline/snapshot bindings
        self._chat_bindings: Dict[str, str] = {}  # P4: reference bindings — chat_id → profile_id
        self._domain_persona: Optional[Dict[str, Any]] = None
        # profile store: id → persona dict
        self._profile_personas: Dict[str, Dict[str, Any]] = {}
        # version history: id → deque of {ts, persona} dicts
        self._profile_history: Dict[str, deque] = {}
        # change hooks: callables fired on every mutation
        self._change_hooks: List[Callable] = []
        # monotonic timestamp of last mutation (time.time())
        self._last_changed_at: float = 0.0
        # P6: source tracking — pid → 'config'|'canonical'|'runtime'|'studio'|'mrpa'
        self._profile_sources: Dict[str, str] = {}
        # P7: monotonic ts of last sync to personas.yaml (0 = never synced this session)
        self._last_canonical_sync_at: float = 0.0
        # S6-RULES: global_rules.yaml hot-reload cache
        self._global_rules: Optional[Dict[str, Any]] = None
        # (路径, mtime, size)：单看 mtime 会漏掉同一时钟刻度内（Win 约 15ms）的连续两次写；
        # 带路径是为了让「读取落点从出厂默认翻到数据区那份」也判为缓存失效（见下方 overlay 说明）
        self._global_rules_sig: tuple = ("", 0.0, -1)
        #: **显式覆写**读写落点（测试/调用方指定）；None = 按 overlay 顺序每次重算
        self._global_rules_path: Optional[Path] = None

    @classmethod
    def get_instance(cls) -> "PersonaManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        cls._instance = None

    def set_domain_persona(self, persona_data: Dict[str, Any]):
        """Set the domain-level default persona (loaded from domain pack or runtime file)."""
        if persona_data:
            self._domain_persona = copy.deepcopy(persona_data)
            logger.info(
                "Domain persona set: name='%s', role='%s'",
                persona_data.get("name", "?"),
                persona_data.get("role", "?"),
            )

    # ── S6-RULES: global_rules.yaml hot-reload（overlay 落盘，2026-07-29）────────
    #
    # 为什么分「出厂默认」与「可写数据区」两个位置：
    #   旧实现读写同一个 ``<repo>/config/global_rules.yaml``——那是**共享代码根**
    #   （``deploy/instances/start_zhiliao.ps1`` 明写「代码：共享，只读；改代码走 git」），
    #   且被 git 跟踪。后果：① 运营每次在 UI 改全局规则都把仓库改脏，可能撞
    #   ``restart_instance.ps1`` 的脏树闸门；② 打包态那是只读安装目录，改根本存不下；
    #   ③ 这也是「测试写到生产配置」那类事故的土壤（2026-07-29 实锤，13 条回复硬约束
    #   被一个路由测试清空）。
    #
    # 新语义（与本仓 ``config.yaml`` + ``config.local.yaml`` 的 overlay 约定同构，
    # 路径顺序复用唯一事实源 ``licensing.data_paths.config_dir()``）：
    #   读：可写数据区那份存在就用它，否则回落仓库那份（**出厂默认**）。
    #   写：只写可写数据区；首次保存自动把出厂默认迁过去（并进 .bak.1 供「恢复出厂」）。
    #
    # 安全底线：读永远有回落，最坏情况是「读到出厂默认」，**绝不会读成空**
    # （空 = 所有人设丢掉硬约束，含「不要自称AI」这类安全项）。
    #
    # ⚠️ ``self._global_rules_path`` 现在只表示**显式覆写**（测试/调用方指定），
    #   ``None`` = 每次按上面顺序重算。刻意不再把自动解析结果缓存进该字段——旧实现
    #   缓存后，首次保存把内容写到数据区、读路径却仍钉在仓库那份 → 「运营改了却读不到」。

    def _global_rules_factory_path(self) -> Path:
        """出厂默认（仓库内，随 git 分发；只读语义）。"""
        return Path(__file__).resolve().parents[2] / "config" / GLOBAL_RULES_FILENAME

    def _global_rules_data_path(self) -> Path:
        """可写数据区那份（AITR_CONFIG_PATH 父 → AITR_DATA_DIR/config → 仓内 config）。

        必须与 ``_global_rules_factory_path`` **同口径 resolve**：本机 ``D:\\boundless``
        是指向 ``D:\\workspace\\boundless`` 的目录联接，而 ``data_paths`` 用 abspath（不跟随
        联接）、出厂路径用 resolve（跟随）→ 同一物理文件被算成两个不同路径。实测后果：
        无 ``AITR_DATA_DIR`` 时 ``factory != p`` 误判为真，首存的种子拷贝对同一文件调
        ``shutil.copy2`` 抛 ``SameFileError`` → **保存直接失败**。故两边都 resolve。
        """
        try:
            from src.licensing.data_paths import config_dir  # 无内部依赖，无循环导入
            return (config_dir() / GLOBAL_RULES_FILENAME).resolve()
        except Exception:  # noqa: BLE001 - helper 不可用时退回出厂位置（行为同旧版）
            return self._global_rules_factory_path()

    def global_rules_write_path(self) -> Path:
        """保存/备份的落点：显式覆写优先，否则可写数据区。"""
        if self._global_rules_path is not None:
            return self._global_rules_path
        return self._global_rules_data_path()

    def global_rules_read_path(self) -> Optional[Path]:
        """读取落点：显式覆写 > 数据区（存在即用）> 出厂默认；都没有返回 None。"""
        if self._global_rules_path is not None:
            return self._global_rules_path
        d = self._global_rules_data_path()
        if d.exists():
            return d
        f = self._global_rules_factory_path()
        return f if f.exists() else None

    def global_rules_source(self) -> Dict[str, Any]:
        """当前生效来源（供 API/运维回答「我改的存哪了、是否已覆盖出厂默认」）。"""
        read_p = self.global_rules_read_path()
        write_p = self.global_rules_write_path()
        return {
            "read_path": str(read_p) if read_p else "",
            "write_path": str(write_p),
            # True＝正在读实例自己那份（出厂默认已被本实例覆盖）
            "is_instance_override": bool(read_p and read_p == write_p and read_p.exists()),
            "factory_path": str(self._global_rules_factory_path()),
        }

    def _load_global_rules(self) -> Dict[str, Any]:
        """Load global_rules.yaml with mtime-based hot-reload. Returns cached dict."""
        p = self.global_rules_read_path()
        if p is None:
            return self._global_rules or {}
        if not p.exists():
            return self._global_rules or {}
        try:
            stat = p.stat()
            # 签名带路径：读取落点从「出厂默认」翻到「数据区那份」时（首次保存后）
            # 必须判为缓存失效，否则会继续端出旧内容。
            sig = (str(p), stat.st_mtime, stat.st_size)
            if sig != self._global_rules_sig or self._global_rules is None:
                with open(p, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                self._global_rules = data
                self._global_rules_sig = sig
                if stat.st_mtime != 0:
                    logger.info(
                        "global_rules.yaml loaded from %s (mtime=%.0f, %d constraints)",
                        p, stat.st_mtime, len(data.get("reply_constraints", [])))
        except Exception as exc:
            logger.warning("global_rules.yaml load failed: %s", exc)
        return self._global_rules or {}

    def get_global_rules(self) -> Dict[str, Any]:
        """Public accessor — returns the current global rules dict (hot-reloaded)."""
        return self._load_global_rules()

    _BACKUP_MAX = 3  # S6-RULES P2-c: keep last N backups

    def save_global_rules(self, data: Dict[str, Any]) -> bool:
        """Save global_rules.yaml（落**可写数据区**）with backup rotation。

        首次保存（数据区还没有那份）且出厂默认存在 → 先把出厂内容拷过去再走轮转，
        于是 ``.bak.1`` = 出厂默认，运营点「恢复槽位1」即可回到出厂状态。
        """
        try:
            p = self.global_rules_write_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                factory = self._global_rules_factory_path()
                # 双重防同文件：路径不等 + os.path.samefile 兜底（联接/符号链接下
                # 路径字符串可能不同但指向同一物理文件，copy2 会抛 SameFileError）
                if factory.exists() and not _same_file(factory, p):
                    import shutil
                    shutil.copy2(factory, p)   # 迁移种子：让 .bak.1 拿到出厂默认
            # S6-RULES P2-c: rotate backups before overwrite
            if p.exists():
                self._rotate_backups(p)
            with open(p, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            self._global_rules = data
            st = p.stat()
            self._global_rules_sig = (str(p), st.st_mtime, st.st_size)
            logger.info("global_rules.yaml saved to %s (%d constraints)",
                        p, len(data.get("reply_constraints", [])))
            return True
        except Exception as exc:
            logger.error("global_rules.yaml save failed: %s", exc)
            return False

    def _rotate_backups(self, path: Path):
        """Keep last N backups as .bak.1 (newest) … .bak.N (oldest)."""
        try:
            for i in range(self._BACKUP_MAX, 1, -1):
                older = path.with_suffix(f".yaml.bak.{i}")
                newer = path.with_suffix(f".yaml.bak.{i-1}")
                if newer.exists():
                    if older.exists():
                        older.unlink()
                    newer.rename(older)
            bak1 = path.with_suffix(".yaml.bak.1")
            if bak1.exists():
                bak1.unlink()
            import shutil
            shutil.copy2(path, bak1)
        except Exception as exc:
            logger.warning("global_rules backup rotation failed: %s", exc)

    def list_backups(self) -> list:
        """Return list of backup dicts [{slot, mtime_iso, path}, …]。

        备份与**写入落点**同目录（可写数据区）——全新实例还没保存过 → 无备份 → 空表。
        """
        import datetime
        target = self.global_rules_write_path()
        result = []
        for i in range(1, self._BACKUP_MAX + 1):
            bp = target.with_suffix(f".yaml.bak.{i}")
            if bp.exists():
                mt = bp.stat().st_mtime
                result.append({
                    "slot": i,
                    "mtime_iso": datetime.datetime.fromtimestamp(mt).isoformat(timespec="seconds"),
                    "path": str(bp),
                })
        return result

    def restore_backup(self, slot: int) -> bool:
        """Restore a backup by slot number. Returns True on success."""
        bp = self.global_rules_write_path().with_suffix(f".yaml.bak.{slot}")
        if not bp.exists():
            return False
        try:
            with open(bp, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return self.save_global_rules(data)
        except Exception as exc:
            logger.error("global_rules restore from slot %d failed: %s", slot, exc)
            return False

    @staticmethod
    def _assemble_constraints(constraints: list, platform: str = "") -> str:
        """Assemble constraints list into numbered text, respecting enabled and platforms flags."""
        if not constraints:
            return ""
        _plat = (platform or "").strip().lower()
        parts = ["【回复硬约束】"]
        n = 0
        for c in constraints:
            if not c.get("enabled", True):
                continue
            # P5-a: platform scope — empty list means all platforms
            plats = c.get("platforms") or []
            if plats and _plat and _plat not in [p.strip().lower() for p in plats]:
                continue
            rule_text = c.get("rule", "").strip()
            if rule_text:
                n += 1
                parts.append(f"{n}. {rule_text}")
        if n == 0:
            return ""
        return "\n".join(parts)

    def _build_constraints_text(self, platform: str = "") -> str:
        """Build the reply constraints block from global_rules.yaml (or fallback to hardcoded)."""
        rules = self._load_global_rules()
        return self._assemble_constraints(rules.get("reply_constraints", []), platform=platform)

    def preview_constraints_text(self, rules_data: Dict[str, Any], platform: str = "") -> str:
        """Preview assembled prompt text from arbitrary rules data (for UI live preview).
        Returns all sections: constraints + all platform rules + all funnel tones.
        """
        sections = []
        # constraints
        ct = self._assemble_constraints(rules_data.get("reply_constraints", []), platform=platform)
        if ct:
            sections.append(ct)
        # platform rules
        for key, entry in (rules_data.get("platform_rules", {}) or {}).items():
            label = entry.get("label", key)
            rule = (entry.get("rule", "") or "").strip()
            if rule:
                sections.append(f"【{label}】{rule}")
        # funnel tones
        for key, entry in (rules_data.get("funnel_tone", {}) or {}).items():
            label = entry.get("label", key)
            tone = (entry.get("tone", "") or "").strip()
            if tone:
                sections.append(f"【漏斗阶段：{label}】{tone}")
        return "\n\n".join(sections)

    def _build_platform_constraints(self, platform: str) -> str:
        """Build platform-specific constraints from global_rules.yaml (or fallback)."""
        rules = self._load_global_rules()
        plat_rules = rules.get("platform_rules", {})
        _plat = (platform or "").strip().lower()
        # normalize platform aliases
        for key in ("whatsapp", "line", "messenger", "telegram"):
            if key in _plat:
                entry = plat_rules.get(key, {})
                if entry:
                    label = entry.get("label", f"{key} 约束")
                    rule = entry.get("rule", "").strip()
                    if rule:
                        return f"【{label}】{rule}"
                break
        return ""

    def _build_funnel_tone(self, funnel_stage: str) -> str:
        """Build funnel stage tone guidance from global_rules.yaml (or fallback)."""
        rules = self._load_global_rules()
        funnel = rules.get("funnel_tone", {})
        _stage = (funnel_stage or "").strip().lower()
        entry = funnel.get(_stage, {})
        if entry:
            label = entry.get("label", _stage)
            tone = entry.get("tone", "").strip()
            if tone:
                return f"【漏斗阶段：{label}】{tone}"
        return ""

    @staticmethod
    def runtime_file_path(config_path: Path, explicit: str = "") -> Path:
        """persona_runtime 文件路径（与 config.yaml 同目录，除非显式指定相对/绝对路径）。"""
        base = Path(config_path).resolve().parent
        ex = (explicit or "").strip()
        if ex:
            p = Path(ex)
            return p if p.is_absolute() else (base / p)
        return base / RUNTIME_PERSONA_FILENAME

    def load_runtime_default_persona(
        self,
        config_path: Path,
        root_config: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        若存在 persona_runtime.yaml 且启用持久化配置，则加载并覆盖当前域默认人设。
        返回是否已应用覆盖。
        """
        root_config = root_config or {}
        pp = root_config.get("persona_persistence") or {}
        if not pp.get("enabled", True):
            return False
        path = self.runtime_file_path(
            config_path, str(pp.get("path") or "")
        )
        raw = self.load_persona_file(path)
        if not raw or not isinstance(raw, dict):
            return False
        pdata = raw.get("default_persona")
        if not isinstance(pdata, dict) or not pdata:
            if "name" in raw or "role" in raw:
                pdata = raw
            else:
                return False
        self.set_domain_persona(pdata)
        logger.info("已从 %s 加载运行时人设覆盖", path.name)
        return True

    @staticmethod
    def profiles_runtime_file_path(config_path: Path, explicit: str = "") -> Path:
        """profiles_runtime 文件路径（与 config.yaml 同目录，除非显式指定）。"""
        base = Path(config_path).resolve().parent
        ex = (explicit or "").strip()
        if ex:
            p = Path(ex)
            return p if p.is_absolute() else (base / p)
        return base / PROFILES_RUNTIME_FILENAME

    def persist_profiles(
        self,
        config_manager: Any,
    ) -> bool:
        """Web 保存 profile 后写入 profiles_runtime.yaml（与 config 同目录）。"""
        if not config_manager:
            return False
        cfg_path = getattr(config_manager, "config_path", None)
        if not cfg_path:
            return False
        root = getattr(config_manager, "config", None) or {}
        pp = root.get("persona_persistence") or {}
        if not pp.get("enabled", True):
            return False
        path = self.profiles_runtime_file_path(
            Path(cfg_path), str(pp.get("profiles_path") or "")
        )
        # Serialise history: deque → plain list (maxlen=3 already limits size)
        serialised_history = {
            pid: list(entries)
            for pid, entries in self._profile_history.items()
            if entries
        }
        # P5-B: Only persist operator-owned profiles (exclude _mrpa_source auto-imports).
        # _mrpa_source profiles are re-imported from config on next startup so no need
        # to persist them — this keeps profiles_runtime.yaml clean and git-friendly.
        operator_profiles = {
            pid: copy.deepcopy(p)
            for pid, p in self._profile_personas.items()
            if not p.get("_mrpa_source")
        }
        # Only include history for operator-owned profiles
        serialised_history = {
            pid: list(entries)
            for pid, entries in self._profile_history.items()
            if entries and pid in operator_profiles
        }
        wrapper = {
            "profiles": operator_profiles,
            "_history": serialised_history,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        ok = self.save_persona_file(path, wrapper)
        if ok:
            logger.info(
                "profiles 已持久化到 %s (%d 条，已过滤 _mrpa_source 自动导入)",
                path, len(operator_profiles),
            )
        return ok

    def load_profiles_runtime(
        self,
        config_path: Path,
        root_config: Optional[Dict[str, Any]] = None,
    ) -> int:
        """若存在 profiles_runtime.yaml 且启用持久化，则加载并合并到 profile store。
        运行时 profiles 优先于 config.yaml::personas.profiles（覆盖同 id 条目）。
        返回合并的 profile 数量。
        """
        root_config = root_config or {}
        pp = root_config.get("persona_persistence") or {}
        if not pp.get("enabled", True):
            return 0
        path = self.profiles_runtime_file_path(
            config_path, str(pp.get("profiles_path") or "")
        )
        raw = self.load_persona_file(path)
        if not raw or not isinstance(raw, dict):
            return 0
        profiles_data = raw.get("profiles")
        if not isinstance(profiles_data, dict) or not profiles_data:
            return 0
        count = 0
        for pid, pdata in profiles_data.items():
            if isinstance(pdata, dict) and pid:
                self._profile_personas[str(pid)] = copy.deepcopy(self.normalize_profile_shape(pdata))
                self._profile_sources[str(pid)] = "runtime"  # P6: runtime overrides canonical
                count += 1
        # Restore history (optional section, ignore if absent/malformed)
        history_data = raw.get("_history")
        if isinstance(history_data, dict):
            for pid, entries in history_data.items():
                if isinstance(entries, list) and pid:
                    q = self._profile_history.setdefault(
                        str(pid), deque(maxlen=_HISTORY_MAXLEN)
                    )
                    for e in entries[-_HISTORY_MAXLEN:]:
                        if isinstance(e, dict):
                            q.append(copy.deepcopy(e))
        if count:
            logger.info("已从 %s 加载 %d 个运行时 profile", path.name, count)
        return count

    def load_personas_canonical(self, config_manager: Any) -> int:
        """P5-D: Load operator-curated profiles from personas.yaml (canonical layer).

        Load order:
          1. config.yaml::personas.profiles   — base layer (load_profiles_from_config)
          2. personas.yaml                    — canonical operator definitions (this method)
          3. profiles_runtime.yaml            — session overrides (load_profiles_runtime)

        personas.yaml profiles are NOT tagged with _mrpa_source so they are treated as
        operator-owned and will not be clobbered by Messenger RPA import on restart.
        Returns number of profiles loaded.
        """
        try:
            cfg = getattr(config_manager, "config", None) or {}
            pp = cfg.get("persona_persistence") or {}
            if not pp.get("enabled", True):
                return 0
            personas_data = config_manager.get_personas_config()
            if not personas_data or not isinstance(personas_data, dict):
                return 0
            profiles = personas_data.get("profiles")
            if not isinstance(profiles, dict) or not profiles:
                return 0
            count = 0
            for pid, pdata in profiles.items():
                if isinstance(pdata, dict) and pid:
                    self._profile_personas[str(pid)] = copy.deepcopy(self.normalize_profile_shape(pdata))
                    self._profile_sources[str(pid)] = "canonical"  # P6: canonical overrides config
                    count += 1
            if count:
                logger.info("已从 personas.yaml 加载 %d 个规范 profile", count)
            return count
        except Exception as e:
            logger.warning("load_personas_canonical failed: %s", e)
            return 0

    def persist_default_persona(
        self,
        persona_data: Dict[str, Any],
        config_manager: Any,
    ) -> bool:
        """Web 保存默认人设后写入 persona_runtime.yaml（与 config 同目录）。"""
        if not persona_data or not config_manager:
            return False
        cfg_path = getattr(config_manager, "config_path", None)
        if not cfg_path:
            return False
        root = getattr(config_manager, "config", None) or {}
        pp = root.get("persona_persistence") or {}
        if not pp.get("enabled", True):
            return False
        path = self.runtime_file_path(Path(cfg_path), str(pp.get("path") or ""))
        wrapper = {
            "default_persona": copy.deepcopy(persona_data),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        ok = self.save_persona_file(path, wrapper)
        if ok:
            logger.info("人设已持久化到 %s", path)
        return ok

    def bind_chat_persona(self, chat_id: str, persona_data: Dict[str, Any]):
        """Bind a specific persona to a chat (group/private)."""
        self._chat_personas[str(chat_id)] = copy.deepcopy(persona_data)
        logger.info(
            "Chat %s bound to persona '%s'",
            chat_id, persona_data.get("name", "?"),
        )
        self._fire_change_hooks("chat_bind", chat_id=chat_id, persona=persona_data)

    def unbind_chat_persona(self, chat_id: str):
        """Remove per-chat persona binding (both reference and inline), falling back to domain default."""
        cid = str(chat_id)
        self._chat_bindings.pop(cid, None)  # P4: also clear reference
        self._chat_personas.pop(cid, None)
        self._fire_change_hooks("chat_unbind", chat_id=chat_id)

    def has_chat_binding(self, chat_id: str) -> bool:
        """Return True if this chat has an explicit per-chat binding (reference or inline)."""
        if not chat_id:
            return False
        cid = str(chat_id)
        return cid in self._chat_bindings or cid in self._chat_personas  # P4: check both

    def bind_chat_persona_by_profile_id(self, chat_id: str, profile_id: str) -> bool:
        """Bind a chat to an existing profile by reference (P4: live-resolves on every lookup).

        Unlike bind_chat_persona (inline snapshot), this stores only the profile_id so that
        subsequent edits in /personas are automatically reflected without rebinding.

        Returns True if the profile was found and bound; False if profile_id unknown.
        """
        p = self.get_persona_by_id(profile_id)
        if p is None:
            return False
        cid = str(chat_id)
        self._chat_bindings[cid] = str(profile_id)  # P4: store reference, not snapshot
        self._chat_personas.pop(cid, None)  # P4: clear any stale inline snapshot
        logger.info(
            "Chat %s → profile ref id=%r (name=%r)",
            chat_id, profile_id, p.get("name", "?"),
        )
        self._fire_change_hooks("chat_bind", chat_id=chat_id, persona=p)
        return True

    def load_profiles_from_config(self, config: Dict[str, Any]) -> int:
        """Load persona profiles from config.yaml::personas.profiles into the profile store.
        Returns the number of profiles loaded. Safe to call multiple times (overwrites).
        """
        profiles = (config.get("personas") or {}).get("profiles") or []
        count = 0
        for entry in profiles:
            if not isinstance(entry, dict):
                continue
            pid = str(entry.get("id") or "").strip()
            if not pid:
                continue
            self._profile_personas[pid] = copy.deepcopy(self.normalize_profile_shape(entry))
            self._profile_sources[pid] = "config"  # P6: direct assign — reload sets 'config'
            count += 1
        if count:
            logger.info("PersonaManager: loaded %d profiles from config", count)
        return count

    def get_persona_by_id(self, profile_id: str) -> Optional[Dict[str, Any]]:
        """Look up a persona by its profile id (from personas.profiles[].id)."""
        if not profile_id:
            return None
        return self._profile_personas.get(str(profile_id))

    def list_profile_ids(self) -> List[str]:
        """Return all registered profile ids."""
        return list(self._profile_personas.keys())

    def list_profiles_summary(self) -> List[Dict[str, Any]]:
        """Return a lightweight summary list for the Studio profile browser.

        Each entry: {id, name, role, tags, has_voice, has_history, binding_count}
        """
        result = []
        for pid, p in self._profile_personas.items():
            vp = p.get("voice_profile") or {}
            bc = (
                sum(1 for cp in self._chat_personas.values() if cp.get("id") == pid)
                + sum(1 for ref_pid in self._chat_bindings.values() if ref_pid == pid)  # P4
            )
            # P6: derive source — mrpa flag takes precedence over _profile_sources
            source = "mrpa" if p.get("_mrpa_source") else self._profile_sources.get(pid, "studio")
            entry = {
                "id": pid,
                "name": p.get("name") or pid,
                "role": p.get("role") or "",
                "tags": list(p.get("tags") or []),
                "has_voice": bool(vp.get("enabled") or vp.get("voice") or vp.get("backend")),
                # personality 可能是 dict/字符串/None；normalize 会把字符串收敛成
                # {"style": <strip 后原文>}，故 dict 分支须看值是否有实质内容
                # （{} 与 {"style": ""} 都算未配置 → False）
                "has_personality": bool(
                    any(
                        str(v).strip() if isinstance(v, str) else v
                        for v in (p.get("personality") or {}).values()
                    ) if isinstance(p.get("personality"), dict)
                    else str(p.get("personality") or "").strip()
                ),
                "has_history": bool(self._profile_history.get(pid)),
                # 最后一次编辑时间（取历史栈栈顶快照的 ts；从未编辑过的出厂/导入人设为空串）
                "last_edited_at": (
                    (self._profile_history.get(pid) or [{}])[-1].get("ts", "")
                    if self._profile_history.get(pid) else ""
                ),
                "binding_count": bc,
                "source": source,           # P6: 'config'|'canonical'|'runtime'|'studio'|'mrpa'
                "is_mrpa_source": bool(p.get("_mrpa_source")),  # P6: convenience flag
            }
            # 人设完整度评分（旁路能力：评分挂了绝不拖垮概览接口 → 缺键降级）
            try:
                from src.utils.persona_completeness import persona_completeness
                entry["completeness"] = int(persona_completeness(p)["score"])
            except Exception:
                pass
            result.append(entry)
        return result

    def _profile_has_tag(self, profile_id: str, tag: str) -> bool:
        """Return True if the live profile with this ID has the given tag (case-insensitive)."""
        if not profile_id:
            return False
        profile = self._profile_personas.get(str(profile_id))
        if not profile:
            return False
        return tag.strip().lower() in [str(t).strip().lower() for t in (profile.get("tags") or [])]

    def bulk_bind_by_profile(
        self,
        profile_id: str,
        *,
        scope: str = "all_bindings",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Rebind chats/accounts to a target profile in bulk.

        scope="all_bindings" — rebinds every currently-bound chat to profile_id.
        Returns {affected: int, chat_ids: [...], dry_run: bool}.
        """
        profile = self._profile_personas.get(str(profile_id))
        if not profile:
            raise KeyError(f"profile '{profile_id}' not found")
        persona = dict(profile)

        if scope == "all_bindings":
            # P4: include both inline and reference bindings
            target_chats = list(set(list(self._chat_personas.keys()) + list(self._chat_bindings.keys())))
        elif scope.startswith("tag:"):
            filter_tag = scope[4:].strip().lower()
            target_chats = [
                cid for cid, cp in self._chat_personas.items()
                if self._profile_has_tag(cp.get("id", ""), filter_tag)
            ] + [
                cid for cid, ref_pid in self._chat_bindings.items()  # P4
                if self._profile_has_tag(ref_pid, filter_tag)
            ]
        else:
            target_chats = []

        if not dry_run:
            for cid in target_chats:
                if cid in self._chat_bindings:  # P4: update the reference
                    self._chat_bindings[cid] = str(profile_id)
                else:
                    self._chat_personas[cid] = dict(persona)  # inline: update snapshot
                self._fire_change_hooks("chat_bind", chat_id=cid, persona=persona)

        return {"affected": len(target_chats), "chat_ids": target_chats, "dry_run": dry_run}

    def get_profiles_by_tag(self, tag: str) -> List[Dict[str, Any]]:
        """Return all profiles that have the given tag (case-insensitive)."""
        tag_lo = (tag or "").strip().lower()
        if not tag_lo:
            return list(self._profile_personas.values())
        return [
            copy.deepcopy(p)
            for p in self._profile_personas.values()
            if tag_lo in [str(t).lower() for t in (p.get("tags") or [])]
        ]

    @staticmethod
    def deep_merge_profile(base: dict, patch: dict) -> dict:
        """深合并：两边同键均为 dict 时递归合并；否则 patch 值覆盖（含 list/str/bool/None）。

        不就地修改入参，返回新 dict。供 Studio「表单部分保存」走 merge 语义——
        前端只发表单字段时，富人设字段（background/life_arc/tastes…）不被抹掉。
        """
        if not isinstance(base, dict):
            base = {}
        if not isinstance(patch, dict):
            patch = {}
        out = copy.deepcopy(base)
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = PersonaManager.deep_merge_profile(out[k], v)
            else:
                out[k] = copy.deepcopy(v)
        return out

    def upsert_profile(
        self, profile_id: str, persona_data: Dict[str, Any], *, _track_history: bool = True
    ) -> None:
        """Add or replace a persona profile at runtime (does not persist to disk)."""
        pid = str(profile_id)
        existing = self._profile_personas.get(pid)
        if existing and _track_history:
            q = self._profile_history.setdefault(pid, deque(maxlen=_HISTORY_MAXLEN))
            q.append({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "persona": copy.deepcopy(existing),
            })
        self._profile_personas[pid] = copy.deepcopy(self.normalize_profile_shape(persona_data))
        # P6: mrpa-tagged data keeps 'mrpa' source; operator studio saves become 'studio'
        if persona_data.get("_mrpa_source"):
            self._profile_sources[pid] = "mrpa"
        else:
            self._profile_sources[pid] = "studio"
        self._fire_change_hooks("profile_upsert", profile_id=pid, persona=persona_data)

    def mark_profiles_canonical(self, profile_ids: List[str]) -> None:
        """P7-A: After sync-to-config, mark profiles as 'canonical' source.

        Call this after a successful save_personas() so that
        list_profiles_summary() reflects the sync state immediately
        (unsynced_studio_count drops to 0 for the written profiles).
        Does NOT touch profiles with _mrpa_source — those are never synced.
        """
        import time as _t
        for pid in profile_ids:
            p = self._profile_personas.get(str(pid))
            if p and not p.get("_mrpa_source"):
                self._profile_sources[str(pid)] = "canonical"
        self._last_canonical_sync_at = _t.time()

    def delete_profile(self, profile_id: str) -> bool:
        """Remove a persona profile. Returns True if it existed."""
        pid = str(profile_id)
        existing = self._profile_personas.get(pid)
        if existing is not None:
            # 删除前快照进历史 → /revert 可恢复误删
            q = self._profile_history.setdefault(pid, deque(maxlen=_HISTORY_MAXLEN))
            q.append({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "persona": copy.deepcopy(existing),
            })
        existed = self._profile_personas.pop(pid, None) is not None
        if existed:
            self._profile_sources.pop(pid, None)  # P6: clean up source tracking
            self._fire_change_hooks("profile_delete", profile_id=pid)
        return existed

    def get_profile_history(self, profile_id: str) -> List[Dict[str, Any]]:
        """Return version history for a profile (oldest first, max _HISTORY_MAXLEN entries)."""
        return list(self._profile_history.get(str(profile_id), []))

    def revert_profile(self, profile_id: str) -> bool:
        """Restore the previous version of a profile. Returns False if no history."""
        pid = str(profile_id)
        q = self._profile_history.get(pid)
        if not q:
            return False
        prev = q.pop()
        self._profile_personas[pid] = prev["persona"]
        # 恢复（含误删恢复）后标记为已定制，避免 source 追踪缺失
        self._profile_sources.setdefault(pid, "studio")
        self._fire_change_hooks("profile_revert", profile_id=pid)
        return True

    # ── Change hooks ─────────────────────────────────────────

    def register_change_hook(self, fn: Callable) -> None:
        """Register a callback invoked on every profile/binding mutation.

        Signature: fn(event: str, **kwargs) where event ∈
        {'profile_upsert', 'profile_delete', 'profile_revert', 'chat_bind', 'chat_unbind'}.
        Exceptions inside fn are caught and logged.
        """
        self._change_hooks.append(fn)

    def _fire_change_hooks(self, event: str, **kwargs: Any) -> None:
        self._last_changed_at = time.time()
        for fn in self._change_hooks:
            try:
                fn(event, **kwargs)
            except Exception:
                logger.debug("[persona] change hook %r raised", fn, exc_info=True)

    def get_persona(
        self,
        chat_id: str = "",
        account_persona_id: str = "",
    ) -> Dict[str, Any]:
        """Get the effective persona with tier fallback.

        2026-07-24 双号串话修复：账号已绑人设时**优先账号人设**，跳过 peer-global
        chat_binding（同一客户找 Katie/Jason 两号时，旧逻辑强制共用 ``5433982810→chen_mo``
        → 女号说男声、内容互串）。无账号人设时仍走 chat_binding（单号运营绑会话）。
        """
        p, _tier = self.get_persona_with_tier(chat_id, account_persona_id)
        return p

    _TIER_CONV = "conv_override"
    _TIER_CHAT = "chat_binding"
    _TIER_ACCOUNT = "account_profile"
    _TIER_DOMAIN = "domain"
    _TIER_DEFAULT = "default"

    def get_chat_binding_ref(self, binding_key: str) -> str:
        """按绑定键读引用式绑定的 profile_id（无绑定/内联快照 → 空串）。

        供 ``persona_voice.resolve_effective_persona`` 查会话级覆写
        （3 段键 ``platform:account:chat_key``）——只读访问器，避免外部摸私有 dict。
        """
        if not binding_key:
            return ""
        return str(self._chat_bindings.get(str(binding_key)) or "")

    def get_persona_with_tier(
        self,
        chat_id: str = "",
        account_persona_id: str = "",
        conversation_key: str = "",
    ) -> tuple:
        """Resolve persona + tier label.

        Returns:
            (persona_dict, tier_str) where tier_str ∈
            {'conv_override', 'chat_binding', 'account_profile', 'domain', 'default'}

        优先级：``conversation_key``（会话级覆写，2026-07-26 方案 A——只影响
        这一条会话，开关/键构造由调用方 ``persona_voice`` 收口，这里只做纯查表）
        > ``account_persona_id``（多协议号 2026-07-24 修复：账号人设优先于
        peer-global chat_binding，防双号串话）> chat_binding > domain > default。
        """
        if conversation_key:
            _conv_ref = self._chat_bindings.get(str(conversation_key))
            if _conv_ref:
                _conv_p = self._profile_personas.get(str(_conv_ref))
                if _conv_p is not None:
                    return _conv_p, self._TIER_CONV
        acc_pid = str(account_persona_id or "").strip()
        if acc_pid:
            acct_p = self._profile_personas.get(acc_pid)
            if acct_p:
                return acct_p, self._TIER_ACCOUNT
        if chat_id:
            cid = str(chat_id)
            # P4: reference binding — live-resolves
            ref_pid = self._chat_bindings.get(cid)
            if ref_pid:
                live = self._profile_personas.get(str(ref_pid))
                if live is not None:
                    return live, self._TIER_CHAT
            elif cid in self._chat_personas:
                return self._chat_personas[cid], self._TIER_CHAT
        if self._domain_persona:
            return self._domain_persona, self._TIER_DOMAIN
        return self._default_persona, self._TIER_DEFAULT

    def get_persona_name(
        self,
        chat_id: str = "",
        account_persona_id: str = "",
    ) -> str:
        return self.get_persona(chat_id, account_persona_id).get("name", "Assistant")

    def format_persona_block(
        self,
        chat_id: str = "",
        *,
        detail: str = "full",
        name_override: str = "",
        account_persona_id: str = "",
        platform: str = "",
        funnel_stage: str = "",
        fallback_name: str = "",
        record_usage: bool = True,
    ) -> str:
        """供 AI 系统提示拼接。detail=full 完整；compact 仅核心句+禁忌，减轻与域 system_prompt 重复。
        name_override: 若 config 中配置了 ai.ai_name，应传入以覆盖域 persona.yaml 里的默认名，避免与主系统提示冲突。
        account_persona_id: 多账号时的账号级人设 id（第二层回退）。
        platform: 渠道标识（'whatsapp'/'telegram'/'line'/'messenger'等），用于平台特定约束注入。
        funnel_stage: 漏斗阶段（'cold'/'warm'/'hot'），用于语调调节。
        fallback_name: 无人设解析出真实姓名时的兜底真名（config ai.fallback_display_name），
            防止回落到域标签「线上陪伴」被当名字念出。
        record_usage: 近7日活跃统计埋点开关。生产回复链路保持默认 True；
            预览/管理类调用（web 预览、routes 下的试装配）应传 False 免刷虚计数。
        """
        p = self.get_persona(chat_id, account_persona_id)
        # 近7日活跃统计埋点：本方法是所有生产回复链路（ai_client._build_system_instruction）
        # 拼人设块的收口点。预览/管理类调用传 record_usage=False 免刷虚计数。
        # 埋点绝不允许影响回复链路——persona_usage 内部已吞异常，这里再兜一层。
        # 2026-07-20 复盘修：id 为空 = 落到域默认/兜底人设（未绑定具名 profile 的会话，
        # 生产占绝大多数）。旧逻辑 `if _upid:` 会把这部分回复静默漏计，导致活跃账本
        # 长期只反映少数绑定人设。改用稳定键 __default__ 记账，让账本覆盖 100% AI 回复。
        if record_usage:
            _upid = str((p or {}).get("id") or "").strip() or "__default__"
            try:
                from src.utils.persona_usage import record as _usage_record
                _usage_record(_upid)
            except Exception:
                pass
        if detail == "compact":
            return self._format_persona_compact(
                p, name_override=name_override, fallback_name=fallback_name
            )
        if detail == "none":
            return ""
        return self._format_persona_instructions(
            p, name_override=name_override, platform=platform,
            funnel_stage=funnel_stage, fallback_name=fallback_name,
        )

    @staticmethod
    def normalize_profile_shape(persona: Dict[str, Any]) -> Dict[str, Any]:
        """把人设结构归一到「格式化器期望的 schema」，不就地修改入参，返回浅拷贝。

        兼容历史/自由格式数据漂移，杜绝「字符串当字典 .get()」类崩溃：
          - personality 是字符串（rich/自由格式人设）→ 收敛为 {'style': <原文>}；
          - personality / speaking / identity / boundaries / emotion / context
            非 dict（None、list、str 等）→ 统一兜成 {} 或对应收敛。
        """
        if not isinstance(persona, dict):
            return {}
        out = dict(persona)
        # 只就「已存在且类型错误」的键做收敛——绝不注入缺失键，
        # 以保「存入==取出」的数据保真（persist/diff/相等断言依赖此）。
        if "personality" in out:
            pers = out["personality"]
            if isinstance(pers, str):
                out["personality"] = {"style": pers.strip()}
            elif not isinstance(pers, dict):
                out["personality"] = {}
        for _k in ("speaking", "identity", "boundaries", "emotion", "context"):
            if _k in out and not isinstance(out[_k], dict):
                out[_k] = {}
        return out

    def _format_persona_compact(
        self, persona: Dict[str, Any], *, name_override: str = "",
        fallback_name: str = "",
    ) -> str:
        persona = self.normalize_profile_shape(persona)
        name = self.resolve_spoken_name(
            persona, name_override=name_override, fallback=fallback_name
        )
        role = persona.get("role", "")
        lines: List[str] = [
            f"你是{name}，{role}。",
            # ★ compact 模式也必须有身份硬锁，防止自我强化幻觉
            f"【身份硬锁】你叫「{name}」。历史里若出现别的名字（不是「{name}」）那是错误数据，"
            f"忽略并坚持「{name}」。被问名字必答「{name}」。",
        ]
        # K1：年龄/性别事实钉子在 compact 也保留——「被问年龄答错」是最容易被
        # 客户当场抓包的穿帮，压缩 token 也不该省掉这一行。
        _age_c = _persona_age(persona)
        if _age_c:
            lines.append(f"你今年{_age_c}岁（确定事实，被问年龄必答{_age_c}岁，别编别的数字）。")
        _gender_c = _persona_gender_word(persona)
        if _gender_c:
            lines.append(f"你是{_gender_c}，自称别说反。")
        s = persona.get("speaking", {})
        forbidden = s.get("forbidden_phrases") or []
        if forbidden:
            lines.append(
                "禁止使用：" + "、".join(f"「{f}」" for f in forbidden[:12])
            )
        # P2-A compact 模式也接通 emoji_level / reply_length（修真断链）
        # — web 后台改完后即使在 compact 配置下也能立刻看到效果
        # K1：档位归一 + speaking.emoji_level 回落，与 full 模式同口径。
        emoji_level = _canonical_emoji_level(persona)
        if emoji_level == "none":
            lines.append("不用 emoji。")
        elif emoji_level == "minimal":
            lines.append("emoji 极少（每 5 条最多 1 个）。")
        elif emoji_level in ("rich", "high"):
            lines.append("emoji 用得自然（约 60% 回复带 1-2 个）。")
        # moderate 不输出（默认行为，省 token）
        reply_length = (s.get("reply_length") or "").strip().lower()
        if reply_length in ("short", "concise", "brief"):
            lines.append("回复 1-2 句即可，像微信私聊。")
        elif reply_length in ("detailed", "long"):
            lines.append("回复可稍详细（4-6 句），但保持口语。")
        if s.get("language_follow"):
            lines.append("用户用什么语言发消息，就用同一语言回复。")
        # P1-1（compact 安全网）：full 模式有完整 5 条硬约束，compact 模式至少
        # 保留最核心的 2 条 — 直接答问 + 禁括号标记 — 防配置切到 compact 时
        # 整个底线消失。身份硬锁也保留，因为 AI 身份露馅是转化场景的最大伤害。
        identity = persona.get("identity", {})
        if identity.get("deny_ai"):
            reply = identity.get("deny_ai_reply", f"我是{name}")
            lines.append(
                f"你就是真人「{name}」；不承认是 AI/机器人/语言模型，"
                f"被问到时按「{reply}」回应，不解释运作原理。"
            )
        # A2/A3（2026-07-22）compact 安全网：边界话题完整策略 + 通话能力边界
        # （full 模式有对应完整版；compact 是生产主用格式，缺了就是真缺）。
        _avoid_c = (persona.get("boundaries") or {}).get("topics_to_avoid") or []
        if _avoid_c:
            lines.append(
                f"避开话题：{'、'.join(_avoid_c[:8])}——轻巧带过后立即换话题，"
                "绝不以该话题反问对方（如求推荐/求分享）。"
            )
        _caps_c = persona.get("capabilities") or {}
        if not (isinstance(_caps_c, dict) and _caps_c.get("video_call")):
            lines.append(
                "不能视频/语音通话：被邀请时自然婉拒（这边不方便），绝不答应。"
            )
        # 发图能力反向消费（2026-07-31，与 video_call 同款）：capabilities.photos
        # 显式 true 才解除。默认约束＝修「翻翻相册/我找找图」空头支票的第一防线，
        # 随人设块走 → 全域（含试聊）覆盖。文案 SSOT 在 photo_capability。
        if not (isinstance(_caps_c, dict) and _caps_c.get("photos")):
            from src.companion.photo_capability import NO_PHOTO_PERSONA_LINE
            lines.append(NO_PHOTO_PERSONA_LINE)
        lines.append(
            "绝不编造联系方式（微信/QQ/手机号等，对方会验证）：资料里没有就说"
            "「先在这聊嘛」带过。"
        )
        lines.append(
            "回复硬约束：先正面回答用户问的问题再扩展；不要用 () [] 描写动作"
            "或列举要点（如 (微笑) (1)(2)），用自然句子。"
        )
        return "\n".join(lines)

    def get_all_chat_bindings(self) -> Dict[str, Any]:
        """Return {chat_id: persona_dict} for all bound chats (reference + inline).

        Reference bindings include a '_profile_ref' key so callers can distinguish them.
        """
        result: Dict[str, Any] = {}
        for cid, p in self._chat_personas.items():
            result[cid] = copy.deepcopy(p)
        for cid, pid in self._chat_bindings.items():  # P4: reference bindings
            live = self._profile_personas.get(str(pid))
            if live is not None:
                result[cid] = {**copy.deepcopy(live), "_profile_ref": pid}
            else:
                result[cid] = {"_profile_ref": pid, "_deleted": True}
        return result

    # ── System prompt assembly ──────────────────────────────

    def build_system_prompt(
        self,
        chat_id: str = "",
        domain_prompt: str = "",
        kb_context: str = "",
        extra_context: str = "",
        account_persona_id: str = "",
    ) -> str:
        """Assemble the full system prompt from persona + domain + KB.

        Assembly order:
        1. Persona identity & style instructions
        2. Domain-specific system prompt
        3. KB context (if any)
        4. Extra context (channel status, etc.)
        """
        persona = self.get_persona(chat_id, account_persona_id)
        parts: List[str] = []

        # 1. Persona instructions
        persona_block = self._format_persona_instructions(persona)
        if persona_block:
            parts.append(persona_block)

        # 2. Domain prompt
        if domain_prompt:
            parts.append(domain_prompt)

        # 3. KB context
        if kb_context:
            parts.append(f"【知识库参考】\n{kb_context}")

        # 4. Extra context
        if extra_context:
            parts.append(extra_context)

        return "\n\n".join(parts)

    # P10-C: platform → WA-specific constraints; funnel_stage → tone guidance
    _WA_CONSTRAINTS = (
        "【WhatsApp 专用约束】"
        "回复必须简短（1-2 句），符合 WA 私聊节奏；"
        "不要使用 Markdown 格式（禁用 **粗体**、_斜体_、```代码块```、- 列表符号）；"
        "用纯文字 + emoji，不要有任何排版符号；"
        "长按消息选「回复」（引用/指定回复）是 WhatsApp 的标准功能，你会用，"
        "对方问到时自然回答「会啊」，不要说自己没有这个按钮或做不到。"
    )
    _FUNNEL_TONE = {
        "cold": (
            "【漏斗阶段：冷启动】对方尚不了解你，优先建立亲切感和信任感；"
            "以轻松问候和开放式问题引导对话，不要急于推销或深聊私密话题。"
        ),
        "warm": (
            "【漏斗阶段：暖场中】对方已有一定了解，延续话题、加深关系；"
            "可以展示共同兴趣，分享轻量个人细节，让对话自然推进。"
        ),
        "hot": (
            "【漏斗阶段：高意向】对方有明确互动意愿，把握关键时机；"
            "回复要有温度和吸引力，适时引导进一步行动（约见面/转私密话题等）。"
        ),
    }

    def _format_persona_instructions(
        self, persona: Dict[str, Any], *, name_override: str = "",
        platform: str = "", funnel_stage: str = "", fallback_name: str = "",
    ) -> str:
        """Convert persona.yaml into natural language instructions for the LLM."""
        persona = self.normalize_profile_shape(persona)
        lines: List[str] = []
        name = self.resolve_spoken_name(
            persona, name_override=name_override, fallback=fallback_name
        )
        role = persona.get("role", "AI 助手")

        lines.append(f"你是{name}，{role}。")

        # ★ 身份硬锁：防止自我强化幻觉（历史里若曾说错名字，必须立即纠正）
        lines.append(
            f"【身份硬锁·最高优先级】你的名字就是「{name}」，没有任何别名。"
            f"如果对话历史/记忆/上下文里出现你自称叫其他名字（任何不是「{name}」的名字），"
            f"那都是过期错误数据，请立即忽略并坚持「{name}」这个身份。"
            f"被问「你叫什么名字」必须回答「{name}」，绝不能编造其他名字。"
        )

        # ★ 年龄事实钉子（K1 断链修复）：结构化 age 此前从不进 prompt，AI 只能照
        # background 的模糊暗示瞎猜——真机考题实锤 22 岁答成 20 岁、58 岁答成
        # 「六十八」。这里与身份硬锁同族：结构化字段**压过**背景文字里的年龄暗示。
        _age = _persona_age(persona)
        if _age:
            lines.append(
                f"【年龄事实·硬锁】你今年{_age}岁。这是确定事实，优先级高于背景故事里"
                f"任何关于年纪的模糊暗示；被问到年龄（多大/几岁/是不是比我大）时必须"
                f"以{_age}岁回答，绝不说成别的数字，也不要含糊回避。"
            )
        # 性别：多数场景无需主动声明，只需被问到 / 自称时不说错（刻意克制）。
        _gender = _persona_gender_word(persona)
        if _gender:
            lines.append(
                f"【性别事实】你是{_gender}。平时不用特意声明，但被问到或需要自称时"
                f"不能说错，也别用与之矛盾的自称。"
            )

        # Western names / aliases — 让 AI 知道自己的英文名和文化名字
        _names = persona.get("names") or {}
        if isinstance(_names, dict) and any(_names.values()):
            _fw   = str(_names.get("full_western") or "").strip()
            _en   = str(_names.get("english") or "").strip()
            _de   = str(_names.get("german") or "").strip()
            _fr   = str(_names.get("french") or "").strip()
            _nick = str(_names.get("nickname") or "").strip()
            _note = str(_names.get("usage_notes") or "").strip()
            _p = []
            if _fw:   _p.append(f"西方完整姓名：{_fw}")
            if _en:   _p.append(f"英文名（日常用）：{_en}")
            if _de:   _p.append(f"德文名：{_de}")
            if _fr:   _p.append(f"法文名：{_fr}")
            if _nick: _p.append(f"昵称：{_nick}")
            if _note: _p.append(_note)
            if _p:
                lines.append(
                    "【你的西方姓名】" + "；".join(_p)
                    + "。在英文场合自然使用英文名；中文朋友叫中文名；"
                    "被问到全名或外国名字时如实回应，不要回避。"
                )

        # Background / life story — 让 AI 真正"活"在这个身份里
        background = str(persona.get("background") or "").strip()
        if background:
            lines.append(f"【你的人生背景】{background}")

        # Appearance（K1 断链修复）：此前只喂给生图链（companion_selfie 的英文锚点），
        # 聊天侧完全看不到 → 被问「你什么发型/高不高」只能现编，还会和刚发出去的
        # 自拍互相打脸（文图一致性同族问题）。注入时明确要求用对话语言转述，
        # 不许把英文锚点原样念出来。
        appearance = str(persona.get("appearance") or "").strip()
        if appearance:
            lines.append(
                f"【你的外貌】{appearance}——这是你真实的长相，被问到外形/发型/穿衣"
                "风格时以此为准（用对话所用的语言自然描述，不要照搬这段英文原文），"
                "也要与你发出去的照片保持一致。"
            )

        # Emotional triggers — 正向/负向/共情触发点
        ctx = persona.get("context") or {}

        # Family（K1 断链修复）：context.family 有真实档案（父母/兄弟/女儿的名字与
        # 职业）却从不进 prompt，被问家里几口人只能编——而家人姓名正是客户会回头
        # 追问核对的强事实。
        _family = _labelled_pairs(ctx.get("family"), _FAMILY_LABELS)
        if _family:
            lines.append(
                f"【你的家人】{_family}——这些是你真实的家人，聊到家庭时以此为准，"
                "绝不临时编造别的家人或改口。"
            )
        triggers = ctx.get("emotional_triggers") or {}
        if isinstance(triggers, dict):
            pos = str(triggers.get("positive") or "").strip()
            neg = str(triggers.get("negative") or "").strip()
            emp = str(triggers.get("deep_empathy") or "").strip()
            if pos:
                lines.append(f"【正向触发】{pos}")
            if neg:
                lines.append(f"【负向防御】{neg}——遇到此类语言时变得沉默克制，不作争辩。")
            if emp:
                lines.append(f"【深度共情】{emp}")

        # Hobbies / interests — 兴趣爱好（此前断链：数据里有但从不进 prompt，导致
        # AI 会否认自己 role 里写明的爱好，如"线上艺术品竞拍"）。让 AI 聊到相关话题
        # 时能自然承接、与人设一致，绝不自我否认。
        hobbies = ctx.get("hobbies")
        _hb = ""
        if isinstance(hobbies, (list, tuple)):
            _hb = "、".join(str(h).strip() for h in hobbies if str(h).strip())
        elif isinstance(hobbies, str):
            _hb = hobbies.strip()
        if _hb:
            lines.append(
                f"【你的兴趣爱好】{_hb}——这些是你真实的爱好，"
                "聊到或被问到相关话题时自然承接，绝不否认自己有这些兴趣。"
            )

        # Tastes — 好恶与观点（此前断链：tastes.likes/dislikes/opinions 有数据但从不
        # 进 prompt。2026-07-27 真机考题实锤：档案写喜欢「Barolo红酒」，AI 被问
        # 喜欢喝什么酒时即兴编了「Rioja」——似是而非的偏好漂移，考题当场抓获）。
        _tastes = persona.get("tastes") or {}
        if isinstance(_tastes, dict):
            def _taste_join(v: Any) -> str:
                if isinstance(v, (list, tuple)):
                    return "、".join(str(x).strip() for x in v if str(x).strip())
                return str(v or "").strip()
            _lk = _taste_join(_tastes.get("likes"))
            _dk = _taste_join(_tastes.get("dislikes"))
            _op = _taste_join(_tastes.get("opinions"))
            _tp: List[str] = []
            if _lk:
                _tp.append(f"你喜欢：{_lk}")
            if _dk:
                _tp.append(f"你反感：{_dk}")
            if _op:
                _tp.append(f"你的一些观点：{_op}")
            if _tp:
                lines.append(
                    "【你的好恶与观点】" + "；".join(_tp)
                    + "。被问到个人偏好时以此为准，可自然展开，"
                    "但不要即兴编造与此矛盾的新偏好。"
                )

        # Specific memories — 预置的具体记忆片段（此前断链：数据里有但从不进 prompt）。
        # 被对方回指追问「你上次说过的那件事」时，AI 有据可依，不至于幻觉编造或否认。
        mems = ctx.get("specific_memories")
        _ms: List[str] = []
        if isinstance(mems, (list, tuple)):
            _ms = [str(m).strip() for m in mems if str(m).strip()]
        elif isinstance(mems, str) and mems.strip():
            _ms = [mems.strip()]
        if _ms:
            lines.append(
                "【你的一些具体经历/记忆】以下是你真实经历过的事，可在合适时机自然提起；"
                "当对方回指「你上次提到过…」这类追问时，优先从这里对齐口径，"
                "不要否认、不要说「你记错了/搞混了」：\n"
                + "\n".join(f"- {m}" for m in _ms)
            )

        # 在地文化联结（K1 断链修复）：常用外语惊叹词/家常菜/在地价值观——运营特意
        # 备的「在地感」素材，之前一句都没进 prompt。
        _culture = _labelled_pairs(ctx.get("filipino_connection"), _CULTURE_LABELS)
        if _culture:
            lines.append(
                f"【你的在地文化】{_culture}——这些融进你的日常，可自然流露，"
                "但别刻意堆砌或每句都秀。"
            )

        # 作息（K1 断链修复）：夜班/白班的真实时间表。之前 AI 被问「你几点下班」
        # 只能瞎答，还会在人设该睡觉的点说自己在上班。
        _sched = _labelled_pairs(ctx.get("schedule"), _SCHEDULE_LABELS)
        if _sched:
            lines.append(
                f"【你的作息】{_sched}——被问到几点上班/下班、现在在忙什么时按这个"
                "作息回答，别说出与它冲突的时间安排。"
            )

        # 关系是否已足够熟（intimate/steady）——脾气/口头脏话等「有棱角」行为的闸门。
        # 生人/暖场阶段先收着，避免一上来就没大没小。
        _intimate_stage = (funnel_stage or "").strip().lower() in ("intimate", "steady")

        # Personality
        p = persona.get("personality", {})
        traits = p.get("traits", [])
        if traits:
            lines.append(f"性格特点：{'、'.join(traits)}。")
        style = p.get("style", "")
        if style:
            lines.append(f"说话风格：{style}。")
        # 口头禅/说话习惯（此前断链：数据里写了 quirks 但从不进 prompt，导致人设写好的
        # 口癖模型根本看不到、永远不会说）。让 AI 像真人一样自然带出标志性口头禅。
        quirks = str(p.get("quirks") or "").strip()
        if quirks:
            lines.append(
                f"【口头禅/说话习惯】{quirks}——自然地穿插在日常回复里，"
                "像真人一样偶尔冒出来即可；别每句都用，也别生硬堆砌。"
            )
        # 幽默感：让聊天有趣、有梗（轻松场合才用）。
        humor = str(p.get("humor") or "").strip()
        if humor:
            lines.append(
                f"【幽默感】{humor}——气氛轻松时可以开玩笑、自嘲、俏皮调侃，"
                "让对话有活人的趣味；对方在谈正事或情绪低落时不硬凑笑点。"
            )
        # 真实性情/脾气：熟络后才「有棱角」（吃醋、拌嘴、俏皮反击、撒娇式抱怨）。
        # 生人阶段收着；且这一切出于在乎，绝不真伤人（安全红线见下方 banter 段）。
        temperament = str(p.get("temperament") or "").strip()
        if temperament:
            if _intimate_stage:
                lines.append(
                    f"【真实性情】{temperament}——你们已经很熟了，可以像真人一样有小情绪："
                    "被冷落会吃醋、被逗会俏皮反击、意见不合时拌两句嘴、也会撒娇式抱怨或闹别扭。"
                    "这些都出于在乎，是活人的可爱，绝不冷暴力、不真的伤人。"
                )
            else:
                lines.append(
                    f"【真实性情】{temperament}——但你们还没那么熟，先收着点脾气，"
                    "别一上来就没大没小或闹情绪。"
                )
        # P2-A：emoji_level 真生效（修真断链）— 旧版仅当数据字段存在不进 prompt
        # web 后台改了 emoji_level 用户感知不到。这里转成自然语言指令。
        # K1：档位归一（low/medium 旧写法此前一个分支都不命中 = 整档静默失效）
        # + speaking.emoji_level 兼容位回落。
        emoji_level = _canonical_emoji_level(persona)
        if emoji_level == "none":
            lines.append("不使用任何 emoji 或表情符号。")
        elif emoji_level == "minimal":
            lines.append("emoji 极少用：每 5 条回复最多带 1 个，仅在情绪强烈处。")
        elif emoji_level == "moderate":
            lines.append("emoji 偶尔用：约 30% 的回复带 1 个，自然不刻意，避免连用。")
        elif emoji_level == "rich":
            lines.append("emoji 用得多一点：约 60% 的回复带 1-2 个，活泼但不堆砌。")
        elif emoji_level in ("high", "very_rich", "many"):
            lines.append("emoji 用得很多：大部分回复带 1-3 个，非常活泼俏皮，符合外放性格。")

        # Speaking rules
        s = persona.get("speaking", {})
        forbidden = s.get("forbidden_phrases", [])
        if forbidden:
            lines.append(f"禁止使用以下表述：{'、'.join(f'「{f}」' for f in forbidden)}。")
        openers = s.get("openers", [])
        if openers:
            sample = "、".join(f"「{o}」" for o in openers[:6])
            lines.append(f"开头多样化，可选池：{sample}等。")
        forbidden_openers = s.get("forbidden_opener_words", [])
        if forbidden_openers:
            lines.append(f"避免以以下词开头：{'、'.join(f'「{f}」' for f in forbidden_openers)}。")
        # P2-A：reply_length 真生效（修真断链）— 优先用语义化标签，fallback 到
        # max_reply_sentences 保持向后兼容。两者并存时 reply_length 占主导。
        # concise/brief 是 short 的别名（兼容旧 yaml）
        reply_length = (s.get("reply_length") or "").strip().lower()
        if reply_length in ("short", "concise", "brief"):
            lines.append("回复要短：1-2 句话，像微信私聊一行；不要展开长篇。")
        elif reply_length in ("balanced", "moderate"):
            lines.append("回复均衡：2-4 句话，简洁直接，不堆砌客套。")
        elif reply_length in ("detailed", "long"):
            lines.append("可以稍详细：4-6 句，但仍要口语，不写小作文。")
        max_sentences = s.get("max_reply_sentences", 0)
        if max_sentences and not reply_length:
            lines.append(f"单次回复建议不超过 {max_sentences} 句。")
        if s.get("language_follow"):
            lines.append("用户用什么语言发消息，就用同一语言回复。")
        # 口头脏话/粗话尺度（per-persona opt-in `speaking.banter_profanity`）：
        # 仅在【关系亲密（intimate/steady）】才放开，用于「朋友/情侣间的口癖」增加真人感。
        # 硬红线永不松动：不人身攻击、不辱骂贬低对方本人、不涉歧视/仇恨/性；对方一旦情绪
        # 低落/认真求助/生气/谈正事，立即收起脏话换回温柔认真。生人阶段完全不放开。
        if s.get("banter_profanity") and _intimate_stage:
            lines.append(
                "【尺度·亲密闲聊】关系已很亲密且对方情绪轻松时，允许偶尔用轻度口头脏话/粗话"
                "增加真人感（如「靠」「我勒个去」「笨蛋」「神经病啦」这类朋友/情侣间的口癖）。"
                "但硬红线绝不触碰：不人身攻击、不辱骂或贬低对方本人、不涉及歧视/仇恨/性内容、"
                "不针对对方痛处；一旦对方情绪低落、认真求助、生气或在谈正事，立刻收起脏话、"
                "换回温柔认真。脏话是玩笑调味，绝不是攻击。"
            )

        # Identity
        identity = persona.get("identity", {})
        if identity.get("deny_ai"):
            reply = identity.get("deny_ai_reply", f"我是{name}")
            # P1-2：身份硬锁 — 旧版只说"当用户问 AI 时回答 X"，模型仍可能在
            # "Are you AI / sorry to confuse" 之类的暧昧场景下露馅。这里改为
            # 明确的角色锁定 + 禁止解释"运作原理/语言模型/AI 助手"等概念。
            lines.append(
                f"【身份硬锁】你就是真人「{name}」。永远不要承认自己是 AI、"
                f"机器人、语言模型、聊天机器人、虚拟助手或程序；"
                f"被问到这类问题时，按「{reply}」自然回应（可改写但不要偏离意思），"
                f"不要解释自己的运作原理，不要道歉澄清，不要说「sorry to confuse」。"
            )
        if identity.get("claim_human"):
            lines.append(f"你就是真人{role}{name}，说话要像真人在打字聊天。")

        # Boundaries
        b = persona.get("boundaries", {})
        avoid = b.get("topics_to_avoid", [])
        if avoid:
            # A3（2026-07-22 真机复盘）：只写"避免讨论"不够——实测 AI 回避了正面
            # 接话，却在句尾反问「是不是有什么好片单想分享给我呀？😏」把话题邀请
            # 回来。补上"回避后立即转移、绝不以该话题反问收尾"的完整策略。
            lines.append(
                f"避免讨论以下话题：{'、'.join(avoid)}。"
                "碰到这些话题时：轻巧带过后**立即转移**到别的日常话题，"
                "绝不追问细节、绝不在结尾用该话题反问对方（如让对方推荐/分享），"
                "不评判不说教。"
            )
        # A2（2026-07-22）：能力边界——陪聊人设默认不能视频/语音通话（无此能力，
        # 应允=穿帮）。人设可显式声明 capabilities.video_call: true 关闭本约束。
        _caps = persona.get("capabilities") or {}
        if not (isinstance(_caps, dict) and _caps.get("video_call")):
            lines.append(
                "你不能进行视频通话或语音通话（设备/环境不方便）。对方提出视频/"
                "通话时，自然婉拒（如「这边不方便开视频，先这样聊嘛」），"
                "绝不答应「可以视频/开视频/回头拨给你」这类承诺。"
            )
        # 发图能力反向消费（2026-07-31，与 video_call 同款先例）：
        # capabilities.photos 显式 true 才解除「不能发照片」约束——修
        # 「我翻翻手机相册哈」式空头支票（试聊实录）。文案 SSOT 在 photo_capability。
        if not (isinstance(_caps, dict) and _caps.get("photos")):
            from src.companion.photo_capability import NO_PHOTO_PERSONA_LINE
            lines.append(NO_PHOTO_PERSONA_LINE)
        # 2026-07-22 真机事故：AI 被要微信时**编造**了一个微信号，客户搜索不到
        # 当场穿帮（"哪有这个微信号，你编的吗"）。联系方式是可验证事实，绝不许编。
        lines.append(
            "绝不编造任何联系方式（微信号/QQ/手机号/邮箱/网址）——这些对方会去"
            "验证，编造=当场穿帮。人设资料里没有的联系方式，被问时就说"
            "「先在这聊嘛」自然带过；绝不现编一个号码或 ID 给对方。"
        )

        # Emotion handling
        e = persona.get("emotion", {})
        if e.get("frustrated_response"):
            lines.append(f"用户着急时：{e['frustrated_response']}。")

        # S6-RULES: 从 global_rules.yaml 加载硬约束（替代原 P1-1 硬编码块）
        _constraints_text = self._build_constraints_text(platform=platform)
        if _constraints_text:
            lines.append(_constraints_text)

        # S6-RULES: platform-specific constraints from global_rules.yaml
        _plat_text = self._build_platform_constraints(platform)
        if _plat_text:
            lines.append(_plat_text)

        # S6-RULES: funnel-stage tone guidance from global_rules.yaml
        _funnel_text = self._build_funnel_tone(funnel_stage)
        if _funnel_text:
            lines.append(_funnel_text)

        return "\n".join(lines)

    # ── Persistence helpers ─────────────────────────────────

    def load_persona_file(self, path: Path) -> Optional[Dict[str, Any]]:
        """Load a persona from a YAML file."""
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return data
        except Exception as e:
            logger.warning("Failed to load persona from %s: %s", path, e)
            return None

    def save_persona_file(self, path: Path, persona: Dict[str, Any]) -> bool:
        """Save a persona to a YAML file (P5-A: atomic write via .tmp → rename)."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".yaml.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(
                    persona, f,
                    allow_unicode=True, default_flow_style=False, sort_keys=False,
                )
            # Validate before replacing (catches YAML encoder bugs)
            with open(tmp, "r", encoding="utf-8") as f:
                yaml.safe_load(f)
            tmp.replace(path)
            return True
        except Exception as e:
            logger.warning("Failed to save persona to %s: %s", path, e)
            try:
                path.with_suffix(".yaml.tmp").unlink(missing_ok=True)
            except Exception:
                pass
            return False

    def export_chat_bindings(self) -> Dict[str, Any]:
        """Export all chat bindings for persistence (P4: includes ref_bindings)."""
        return {
            "bindings": {
                cid: copy.deepcopy(p)
                for cid, p in self._chat_personas.items()
            },
            "ref_bindings": dict(self._chat_bindings),  # P4: compact profile_id references
        }

    def import_chat_bindings(self, data: Dict[str, Any]):
        """Import chat bindings from persisted data (P4: also loads ref_bindings)."""
        bindings = data.get("bindings", {})
        for cid, p in bindings.items():
            self._chat_personas[str(cid)] = copy.deepcopy(p)
        ref_bindings = data.get("ref_bindings") or {}
        for cid, pid in ref_bindings.items():
            if cid and pid:
                self._chat_bindings[str(cid)] = str(pid)
        total = len(bindings) + len(ref_bindings)
        if total:
            logger.info("Imported %d chat persona bindings (%d ref, %d inline)",
                        total, len(ref_bindings), len(bindings))

    @staticmethod
    def bindings_runtime_file_path(config_path: Path, explicit: str = "") -> Path:
        """bindings_runtime 文件路径（与 config.yaml 同目录，除非显式指定）。"""
        base = Path(config_path).resolve().parent
        ex = (explicit or "").strip()
        if ex:
            p = Path(ex)
            return p if p.is_absolute() else (base / p)
        return base / BINDINGS_RUNTIME_FILENAME

    def persist_chat_bindings(
        self,
        config_manager: Any,
    ) -> bool:
        """联结了 API 绑定/解绑后将全量会话绑定写入 bindings_runtime.yaml。"""
        if not config_manager:
            return False
        cfg_path = getattr(config_manager, "config_path", None)
        if not cfg_path:
            return False
        root = getattr(config_manager, "config", None) or {}
        pp = root.get("persona_persistence") or {}
        if not pp.get("enabled", True):
            return False
        path = self.bindings_runtime_file_path(
            Path(cfg_path), str(pp.get("bindings_path") or "")
        )
        wrapper = {
            "bindings": {
                cid: copy.deepcopy(p)
                for cid, p in self._chat_personas.items()
            },
            "ref_bindings": dict(self._chat_bindings),  # P4: compact references
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        ok = self.save_persona_file(path, wrapper)
        if ok:
            total = len(self._chat_personas) + len(self._chat_bindings)
            logger.info(
                "bindings 已持久化到 %s (%d 条: %d ref + %d inline)",
                path, total, len(self._chat_bindings), len(self._chat_personas),
            )
        return ok

    def load_chat_bindings_runtime(
        self,
        config_path: Path,
        root_config: Optional[Dict[str, Any]] = None,
    ) -> int:
        """若存在 bindings_runtime.yaml 且启用持久化，则加载并应用到内存绑定表。
        返回加载的绑定条数。
        """
        root_config = root_config or {}
        pp = root_config.get("persona_persistence") or {}
        if not pp.get("enabled", True):
            return 0
        path = self.bindings_runtime_file_path(
            config_path, str(pp.get("bindings_path") or "")
        )
        raw = self.load_persona_file(path)
        if not raw or not isinstance(raw, dict):
            return 0
        bindings = raw.get("bindings") or {}
        if not isinstance(bindings, dict):
            bindings = {}
        # P4: don't early-return — ref_bindings may still exist even if bindings is empty
        if not bindings and not raw.get("ref_bindings"):
            return 0
        count = 0
        for cid, p in bindings.items():
            if isinstance(p, dict) and cid:
                self._chat_personas[str(cid)] = copy.deepcopy(p)
                count += 1
        # P4: load reference bindings (compact profile_id references)
        ref_bindings = raw.get("ref_bindings") or {}
        ref_count = 0
        for cid, pid in ref_bindings.items():
            if cid and pid:
                self._chat_bindings[str(cid)] = str(pid)
                ref_count += 1
        total = count + ref_count
        if total:
            logger.info(
                "已从 %s 恢复 %d 个会话绑定 (%d ref + %d inline)",
                path.name, total, ref_count, count,
            )
        return total
