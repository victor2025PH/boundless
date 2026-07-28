"""人设 canonical schema → prompt 数据链门禁（K1，注册表驱动）。

为什么要这条门禁
----------------
「字段在人设工作室里能填、考题在问、用户看得见，但 ``_format_persona_instructions``
从来没把它喂给 LLM」的断链已连续爆过三轮，每轮都是真机考题当场抓获：

  1. ``context.hobbies``  → AI 否认自己 role 里写明的爱好
  2. ``tastes.likes``     → 档案写「Barolo红酒」，AI 现编「Rioja」
  3. ``age`` / ``gender`` → 林小雨 22 岁答成 20 岁、赵老师 58 岁答成「六十八」

一次修一个字段治不了本。本门禁把「每个 schema 字段的表态」变成**强制登记**：

  * ``PROMPT_CONSUMED_FIELDS``（persona_manager）——已接入 prompt，本文件为每个字段
    备一枚**独一无二的哨兵值**，跑一遍格式化器断言哨兵真的出现在输出里（漏接即红）；
  * ``PROMPT_EXEMPT_FIELDS`` ——明确不该进 prompt（运行时配置 / 别的子系统自己拼块 /
    非人设台词），其哨兵值必须**不出现**在输出里；
  * 真实 schema（默认人设 + 完整度表 + doc-import canonical 形状 +
    profiles_runtime.yaml 实况）里**任何**未登记的字段 → 红，逼开发者显式表态。

纯函数式：直接调 ``PersonaManager._format_persona_instructions``，零 IO、零 LLM。
"""

import copy
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Set, Tuple

import pytest
import yaml

from src.utils import persona_completeness as _pc
from src.utils.persona_manager import (
    _DEFAULT_PERSONA,
    PERSONA_SCHEMA_FIELDS,
    PROMPT_CONSUMED_FIELDS,
    PROMPT_EXEMPT_FIELDS,
    PersonaManager,
)

_REGISTRY_HINT = (
    "请在 src/utils/persona_manager.py 的 PROMPT_CONSUMED_FIELDS / "
    "PROMPT_EXEMPT_FIELDS 清单中显式表态"
)

# 样例人设里当「名字」「角色」用的哨兵（claim_human 的期望文案要拼这两个值）
_NAME = "沈知遥"
_ROLE = "海岛民宿主理人"


class _Probe(NamedTuple):
    """单字段哨兵探针。

    value     写进样例 persona 该点路径的哨兵值
    expect    该字段生效时 prompt 里必须出现的片段
    mode      present=必须出现；absent=该字段生效时该片段必须**消失**（反向消费，
              如 capabilities.video_call=True 撤掉「不能视频通话」约束）
    conflicts 验证本字段时需临时移除的其它字段（互斥消费，如 reply_length 存在时
              max_reply_sentences 不注入）
    """

    value: Any
    expect: str
    mode: str = "present"
    conflicts: Tuple[str, ...] = ()


# 每个 PROMPT_CONSUMED_FIELDS 成员都必须在此备探针（缺一个 → 见
# test_every_consumed_field_has_probe 报红）。
_PROBES: Dict[str, _Probe] = {
    # ── 身份 ────────────────────────────────────────────────────────────
    "name": _Probe(_NAME, _NAME),
    "role": _Probe(_ROLE, _ROLE),
    "age": _Probe(47, "你今年47岁"),
    "gender": _Probe("female", "你是女性"),
    "names.full_western": _Probe("Seraphina Vaughn-Kestrel", "Seraphina Vaughn-Kestrel"),
    "names.english": _Probe("ProbeSerafina", "ProbeSerafina"),
    "names.german": _Probe("ProbeSerafine", "ProbeSerafine"),
    "names.french": _Probe("ProbeSeraphine", "ProbeSeraphine"),
    "names.nickname": _Probe("小遥探针", "小遥探针"),
    "names.usage_notes": _Probe("老同学只叫探针昵称", "老同学只叫探针昵称"),
    # ── 人生素材 ────────────────────────────────────────────────────────
    "background": _Probe("在探针镇长大的第三代渔家女", "在探针镇长大的第三代渔家女"),
    "appearance": _Probe(
        "a probe-marked woman with freckles", "a probe-marked woman with freckles"
    ),
    "context.family": _Probe(
        {"father": "探针父亲沈立诚", "daughter": "探针女儿阿枝"}, "父亲：探针父亲沈立诚"
    ),
    "context.hobbies": _Probe(["探针爱好夜潜"], "探针爱好夜潜"),
    "context.specific_memories": _Probe(
        ["探针记忆：2019 年台风夜守着码头"], "探针记忆：2019 年台风夜守着码头"
    ),
    "context.emotional_triggers.positive": _Probe("探针正向触发", "探针正向触发"),
    "context.emotional_triggers.negative": _Probe("探针负向触发", "探针负向触发"),
    "context.emotional_triggers.deep_empathy": _Probe("探针共情触发", "探针共情触发"),
    "context.filipino_connection": _Probe(
        {"food": "探针家常菜 Adobo"}, "饮食：探针家常菜 Adobo"
    ),
    "context.schedule": _Probe(
        {"work_hours": "探针班表 17:00-05:00"}, "上班时间：探针班表 17:00-05:00"
    ),
    "tastes.likes": _Probe(["探针偏好巴罗洛"], "探针偏好巴罗洛"),
    "tastes.dislikes": _Probe(["探针反感被催"], "探针反感被催"),
    "tastes.opinions": _Probe(["探针观点钱花在体验上"], "探针观点钱花在体验上"),
    # ── 性格与说话 ──────────────────────────────────────────────────────
    "personality.traits": _Probe(["探针特质爱较真"], "探针特质爱较真"),
    "personality.style": _Probe("探针风格短句多", "探针风格短句多"),
    "personality.quirks": _Probe("探针口头禅诶呀", "探针口头禅诶呀"),
    "personality.humor": _Probe("探针幽默爱自嘲", "探针幽默爱自嘲"),
    "personality.temperament": _Probe("探针脾气会吃醋", "探针脾气会吃醋"),
    "personality.emoji_level": _Probe("high", "emoji 用得很多"),
    # speaking 侧是 personality.emoji_level 的兼容位（真实人设 zhang_jingguang 只填了
    # 这一处）；验证它必须先撤掉优先级更高的 personality 侧。
    "speaking.emoji_level": _Probe(
        "none", "不使用任何 emoji", conflicts=("personality.emoji_level",)
    ),
    "speaking.forbidden_phrases": _Probe(["探针禁语亲爱的用户"], "探针禁语亲爱的用户"),
    "speaking.openers": _Probe(["探针开场诶对了"], "探针开场诶对了"),
    "speaking.forbidden_opener_words": _Probe(["探针禁头那么"], "探针禁头那么"),
    "speaking.reply_length": _Probe("detailed", "可以稍详细"),
    # reply_length 存在时按语义标签走，max_reply_sentences 让位（既有兼容语义）
    "speaking.max_reply_sentences": _Probe(
        7, "不超过 7 句", conflicts=("speaking.reply_length",)
    ),
    "speaking.language_follow": _Probe(True, "用户用什么语言发消息"),
    "speaking.banter_profanity": _Probe(True, "尺度·亲密闲聊"),
    # ── 身份 / 边界 / 情绪 ──────────────────────────────────────────────
    "identity.deny_ai": _Probe(True, "永远不要承认自己是 AI"),
    "identity.deny_ai_reply": _Probe("探针否认话术我是真人啦", "探针否认话术我是真人啦"),
    "identity.claim_human": _Probe(True, f"你就是真人{_ROLE}{_NAME}"),
    "boundaries.topics_to_avoid": _Probe(["探针雷区话题"], "探针雷区话题"),
    # 反向消费：声明会视频 → 「不能视频通话」的默认约束必须消失
    "capabilities.video_call": _Probe(True, "你不能进行视频通话", mode="absent"),
    "emotion.frustrated_response": _Probe("探针安抚先道歉", "探针安抚先道歉"),
}

# 豁免字段的哨兵：这些值出现在 prompt 里就是泄漏（运行时配置 / 内部 id /
# 别的子系统的单一事实源被抄了第二份）。
_EXEMPT_PROBES: Dict[str, Any] = {
    "id": "probe_exempt_profile_id",
    "_mrpa_source": "probe_exempt_mrpa_source",
    "tags": ["probe_exempt_tag"],
    "voice_profile": {
        "backend": "probe_exempt_backend",
        "voice": "probe_exempt_voice",
        "reference_audio_path": "D:/probe_exempt_ref.wav",
    },
    "location": "probe_exempt_location_slug",
    "life_arc": {
        "theme": "probe_exempt_arc_theme",
        "beats": ["probe_exempt_arc_beat"],
        "stride_days": 3,
    },
    "selfie_scenes": ["probe exempt scene"],
    "boundaries.escalation_phrases": ["probe_exempt_escalate"],
}


# ── 工具 ────────────────────────────────────────────────────────────────


def _set_path(target: Dict[str, Any], path: str, value: Any) -> None:
    node = target
    parts = path.split(".")
    for key in parts[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[parts[-1]] = copy.deepcopy(value)


def _drop_path(target: Dict[str, Any], path: str) -> None:
    node: Any = target
    parts = path.split(".")
    for key in parts[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def _sample_persona(*, with_exempt: bool = True) -> Dict[str, Any]:
    """字段全满的样例人设：每个字段一枚独一无二的哨兵值。"""
    persona: Dict[str, Any] = {}
    for path, probe in _PROBES.items():
        _set_path(persona, path, probe.value)
    if with_exempt:
        for path, value in _EXEMPT_PROBES.items():
            _set_path(persona, path, value)
    return persona


def _fmt(persona: Dict[str, Any], stage: str = "intimate") -> str:
    # stage=intimate：temperament / banter_profanity 这类关系闸门字段只有在亲密档
    # 才注入，用它跑「全字段」通道覆盖面最大（闸门本身由
    # tests/test_persona_humanization.py 守）。
    return PersonaManager()._format_persona_instructions(persona, funnel_stage=stage)


def _walk_paths(obj: Any, prefix: str, out: Set[str]) -> None:
    if not isinstance(obj, dict):
        return
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        out.add(path)
        if isinstance(value, dict):
            _walk_paths(value, path, out)


def _is_declared(path: str) -> bool:
    """点路径是否已表态。

    登记某个 dict 路径 = 整棵子树表态（``voice_profile`` 覆盖 ``voice_profile.voice``）；
    反过来，容器路径若其**子键**已逐个登记（``names`` vs ``names.english``）也算表态。
    """
    if path in PERSONA_SCHEMA_FIELDS:
        return True
    parts = path.split(".")
    for i in range(1, len(parts)):
        if ".".join(parts[:i]) in PERSONA_SCHEMA_FIELDS:
            return True
    return any(known.startswith(path + ".") for known in PERSONA_SCHEMA_FIELDS)


def _discover_schema_fields() -> Dict[str, List[str]]:
    """真实 schema 字段 → 发现来源列表（取各来源并集，别只信一处）。"""
    found: Dict[str, List[str]] = {}

    def _absorb(obj: Any, source: str) -> None:
        paths: Set[str] = set()
        _walk_paths(obj, "", paths)
        for p in paths:
            found.setdefault(p, []).append(source)

    # 1) 出厂默认人设
    _absorb(_DEFAULT_PERSONA, "_DEFAULT_PERSONA")
    # 2) 完整度评分表（Studio 档案页签的字段口径）
    for key in _pc.FIELD_WEIGHTS:
        found.setdefault(key, []).append("persona_completeness.FIELD_WEIGHTS")
    # 3) doc-import canonical 形状（人设文档抽取落地的结构）
    try:
        from src.utils.persona_doc_import import clamp_persona

        _absorb(clamp_persona(_doc_import_maximal_input()), "persona_doc_import.clamp_persona")
    except Exception:  # pragma: no cover - 依赖缺失时不拖垮门禁
        pass
    # 4) 生产实况：profiles_runtime.yaml 里真实人设的键集
    runtime = Path(__file__).resolve().parents[1] / "config" / "profiles_runtime.yaml"
    if runtime.exists():
        try:
            raw = yaml.safe_load(runtime.read_text(encoding="utf-8")) or {}
            for pid, prof in (raw.get("profiles") or {}).items():
                _absorb(prof, f"profiles_runtime.yaml::{pid}")
        except Exception:  # pragma: no cover
            pass
    return found


def _doc_import_maximal_input() -> Dict[str, Any]:
    """喂给 clamp_persona 的「什么键都有」输入，用来榨出 canonical 输出形状。"""
    return {
        "name": "N", "role": "R", "age": 30, "gender": "female",
        "tags": ["t1"],
        "names": {
            "full_western": "A B", "english": "A", "german": "G",
            "french": "F", "nickname": "N", "usage_notes": "U",
        },
        "background": "B",
        "personality": {
            "traits": ["x"], "style": "s", "quirks": "q",
            "humor": "h", "temperament": "t",
        },
        "speaking": {"openers": ["o"]},
        "context": {
            "hobbies": ["hb"],
            "specific_memories": ["m"],
            "emotional_triggers": {"positive": "p", "negative": "n", "deep_empathy": "d"},
        },
        "boundaries": {"topics_to_avoid": ["a"]},
        "appearance": "ap",
        "selfie_scenes": ["sc"],
        "tastes": {"likes": ["l"], "dislikes": ["dl"], "opinions": ["op"]},
        "identity": {"deny_ai": True, "deny_ai_reply": "r", "claim_human": True},
    }


# ── 断言 1：CONSUMED 里每个字段的哨兵值必须真的出现在 prompt 里 ─────────────


def test_every_consumed_field_has_probe():
    """登记为「已接入」却没备哨兵 → 门禁形同虚设，这里先拦一道。"""
    missing = sorted(PROMPT_CONSUMED_FIELDS - set(_PROBES))
    extra = sorted(set(_PROBES) - PROMPT_CONSUMED_FIELDS)
    assert not missing, (
        f"以下字段登记在 PROMPT_CONSUMED_FIELDS 但本文件没有哨兵探针：{missing}\n"
        f"请在 tests/test_persona_prompt_chain.py 的 _PROBES 里补一枚独一无二的哨兵值"
    )
    assert not extra, (
        f"以下探针对应的字段已不在 PROMPT_CONSUMED_FIELDS：{extra}\n"
        f"要么把字段登记回去，要么删掉过期探针（{_REGISTRY_HINT}）"
    )


def test_consumed_fields_reach_prompt():
    """字段全满样例跑一遍格式化器：每个已登记字段的哨兵必须落进 prompt。"""
    sample = _sample_persona()
    out_full = _fmt(sample)

    broken: List[str] = []
    for path, probe in _PROBES.items():
        if probe.mode == "absent":
            continue
        out = out_full
        if probe.conflicts:
            variant = _sample_persona()
            for other in probe.conflicts:
                _drop_path(variant, other)
            out = _fmt(variant)
        if probe.expect not in out:
            broken.append(f"{path}（期望片段：{probe.expect!r}）")

    assert not broken, (
        "以下字段登记为「已接入 prompt」但哨兵值没出现在 "
        "_format_persona_instructions 输出里——数据链断了：\n  "
        + "\n  ".join(broken)
        + "\n修法：在 persona_manager._format_persona_instructions 里真正消费该字段；"
        f"若判定它本就不该进 prompt，改登记到 PROMPT_EXEMPT_FIELDS（{_REGISTRY_HINT}）"
    )


def test_reverse_consumed_fields_drive_output():
    """反向证明：撤掉字段 → 对应片段必须消失（防「哨兵恰好被别处文案带出来」）。"""
    ghosts: List[str] = []
    for path, probe in _PROBES.items():
        variant = _sample_persona()
        _drop_path(variant, path)
        for other in probe.conflicts:
            _drop_path(variant, other)
        out = _fmt(variant)
        if probe.mode == "absent":
            # 反向消费：字段撤掉后默认约束应回来
            if probe.expect not in out:
                ghosts.append(f"{path}（撤掉后默认文案 {probe.expect!r} 没回来）")
        elif probe.expect in out:
            ghosts.append(f"{path}（撤掉后片段 {probe.expect!r} 仍在）")
    assert not ghosts, "以下探针不是由目标字段驱动，门禁强度不可信：\n  " + "\n  ".join(ghosts)


def test_absent_mode_fields_suppress_default_text():
    """capabilities.video_call 这类「反向消费」：字段生效时默认约束必须消失。"""
    out = _fmt(_sample_persona())
    leaked = [
        f"{path}（{probe.expect!r} 应被撤掉却仍在）"
        for path, probe in _PROBES.items()
        if probe.mode == "absent" and probe.expect in out
    ]
    assert not leaked, "\n".join(leaked)


# ── 断言 2：schema 全字段必须已表态（CONSUMED ∪ EXEMPT）───────────────────


def test_no_field_without_stance():
    """任何真实 schema 字段都必须在两张清单之一登记——本条是「根治」的关键。"""
    discovered = _discover_schema_fields()
    unregistered = {
        path: sources for path, sources in discovered.items() if not _is_declared(path)
    }
    assert not unregistered, (
        "以下人设字段既没接入 prompt 也没登记豁免：\n  "
        + "\n  ".join(
            f"{path}（发现于：{'、'.join(sorted(set(src)))}）"
            for path, src in sorted(unregistered.items())
        )
        + f"\n{_REGISTRY_HINT}："
        "\n  · 该字段是人设事实、被问到会露馅 → 在 _format_persona_instructions 里注入"
        "并加入 PROMPT_CONSUMED_FIELDS（记得来 _PROBES 补哨兵）；"
        "\n  · 该字段是运行时配置 / 内部 id / 由别的子系统自己拼块 → 加入 "
        "PROMPT_EXEMPT_FIELDS 并写清理由。"
    )


def test_consumed_and_exempt_are_disjoint():
    overlap = sorted(PROMPT_CONSUMED_FIELDS & PROMPT_EXEMPT_FIELDS)
    assert not overlap, f"字段不能同时「已接入」又「豁免」：{overlap}（{_REGISTRY_HINT}）"
    assert PERSONA_SCHEMA_FIELDS == PROMPT_CONSUMED_FIELDS | PROMPT_EXEMPT_FIELDS


# ── 断言 3：EXEMPT 的哨兵值不得出现在 prompt 里 ──────────────────────────


def test_exempt_fields_never_leak_into_prompt():
    out = _fmt(_sample_persona())

    def _sentinels(value: Any) -> List[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return [s for v in value for s in _sentinels(v)]
        if isinstance(value, dict):
            return [s for v in value.values() for s in _sentinels(v)]
        return []

    leaked = [
        f"{path} → {sentinel!r}"
        for path, value in _EXEMPT_PROBES.items()
        for sentinel in _sentinels(value)
        if sentinel in out
    ]
    assert not leaked, (
        "以下豁免字段的值泄漏进了 prompt：\n  "
        + "\n  ".join(leaked)
        + "\n这些字段是运行时配置 / 内部标识 / 别的子系统的单一事实源；"
        f"若确实该进 prompt，请改登记到 PROMPT_CONSUMED_FIELDS（{_REGISTRY_HINT}）"
    )


# ── 断言 4：空值不产生垃圾 ───────────────────────────────────────────────


_EMPTY_PERSONA: Dict[str, Any] = {
    "name": "", "role": "", "age": None, "gender": "", "background": "",
    "appearance": "", "tags": [],
    "names": {"english": "", "nickname": None},
    "personality": {"traits": [], "style": "", "quirks": "", "humor": "",
                    "temperament": "", "emoji_level": ""},
    "speaking": {"openers": [], "forbidden_phrases": [], "reply_length": "",
                 "max_reply_sentences": 0, "language_follow": False,
                 "emoji_level": None},
    "identity": {"deny_ai": False, "deny_ai_reply": "", "claim_human": False},
    "boundaries": {"topics_to_avoid": [], "escalation_phrases": []},
    "context": {
        "hobbies": [], "specific_memories": [],
        "emotional_triggers": {"positive": "", "negative": "", "deep_empathy": ""},
        "family": {}, "schedule": {"work_hours": "", "night_tone": None},
        "filipino_connection": {},
    },
    "tastes": {"likes": [], "dislikes": "", "opinions": None},
    "emotion": {"frustrated_response": ""},
    "voice_profile": {},
    "life_arc": {},
}

# 只要块头出现就说明「空值也拼了块」——这些块必须整块消失，不留悬空标题。
_OPTIONAL_HEADERS = (
    "【年龄事实", "【性别事实", "【你的西方姓名", "【你的人生背景", "【你的外貌",
    "【你的家人", "【正向触发", "【负向防御", "【深度共情", "【你的兴趣爱好",
    "【你的好恶与观点", "【你的一些具体经历", "【你的在地文化", "【你的作息",
    "【口头禅", "【幽默感", "【真实性情", "【尺度·亲密闲聊",
)


@pytest.mark.parametrize("stage", ["", "warming", "intimate"])
def test_empty_persona_emits_no_placeholder_garbage(stage):
    out = PersonaManager()._format_persona_instructions(_EMPTY_PERSONA, funnel_stage=stage)
    # 注意：不能拿 "[]" / "()" 当占位符信号——global_rules 的硬约束正文里就有
    # 「禁止用 () 和 [] 描写动作」这类合法括号，那是规则文案不是空值残渣。
    for token in ("None", "你今年None", "：None", "、None", "：空", "、、", "：，"):
        assert token not in out, f"空值人设漏出占位符 {token!r}：\n{out}"
    for header in _OPTIONAL_HEADERS:
        assert header not in out, f"空值人设拼出了悬空块头 {header}：\n{out}"
    # 悬空冒号（「上班时间：」后面没内容）与空行都不允许
    for line in out.splitlines():
        assert line.strip(), "输出里有空行（悬空块）"
        assert not line.rstrip().endswith("："), f"悬空冒号：{line}"
        assert "：；" not in line and "；；" not in line, f"空段拼接残留：{line}"


def test_missing_keys_persona_is_safe():
    """整个键都不存在（而非空值）的极简人设同样不得产出占位。"""
    out = _fmt({"name": "阿柔", "role": "邻家女孩"}, stage="intimate")
    assert "None" not in out
    for header in _OPTIONAL_HEADERS:
        assert header not in out


@pytest.mark.parametrize(
    "bad_age", [None, "", "  ", "二十二", 0, -3, 130, True, False, [22], {"v": 22}]
)
def test_dirty_age_never_injected(bad_age):
    """脏 age（非数字/越界/布尔/容器）一律不注入，绝不出现「你今年X岁」。"""
    out = _fmt({"name": "阿柔", "role": "邻家女孩", "age": bad_age})
    assert "【年龄事实" not in out
    assert "你今年" not in out


def test_age_and_gender_are_fact_nails():
    """事故用例回归：22 岁 / 58 岁必须被钉成不可改写的事实。"""
    out = _fmt({"name": "林小雨", "role": "大学生", "age": 22, "gender": "female"})
    assert "【年龄事实·硬锁】你今年22岁" in out
    assert "必须" in out and "22岁回答" in out
    # 结构化字段必须显式压过背景文字里的年龄暗示
    assert "优先级高于背景故事" in out
    assert "【性别事实】你是女性" in out

    out2 = _fmt({"name": "赵老师", "role": "退休语文老师", "age": 58, "gender": "male"})
    assert "你今年58岁" in out2
    assert "你是男性" in out2


def test_context_dicts_keep_unknown_subkeys():
    """未登记标签的子键按原键名透出——运营填的内容绝不静默吞掉。"""
    out = _fmt({
        "name": "阿柔", "role": "邻家女孩",
        "context": {"family": {"godmother": "干妈周姐"},
                    "schedule": {"gym_time": "周三晚上"}},
    })
    assert "godmother：干妈周姐" in out
    assert "gym_time：周三晚上" in out


def test_speaking_emoji_level_fallback_and_aliases():
    """emoji 档位：low/medium 别名生效 + speaking 侧兼容位回落（此前整档静默失效）。"""
    base = {"name": "阿柔", "role": "邻家女孩"}
    assert "emoji 极少" in _fmt({**base, "personality": {"emoji_level": "low"}})
    assert "emoji 偶尔用" in _fmt({**base, "personality": {"emoji_level": "medium"}})
    # personality 侧没填 → 回落 speaking 侧（真实人设 zhang_jingguang 就是这种写法）
    assert "emoji 偶尔用" in _fmt({**base, "speaking": {"emoji_level": "moderate"}})
    # personality 侧优先
    out = _fmt({**base, "personality": {"emoji_level": "none"},
                "speaking": {"emoji_level": "rich"}})
    assert "不使用任何 emoji" in out


def test_real_production_profiles_have_no_unregistered_fields():
    """生产实况兜底：profiles_runtime.yaml 每个人设跑一遍格式化器不崩、且键已表态。"""
    runtime = Path(__file__).resolve().parents[1] / "config" / "profiles_runtime.yaml"
    if not runtime.exists():
        pytest.skip("profiles_runtime.yaml 不在（CI 精简环境）")
    raw = yaml.safe_load(runtime.read_text(encoding="utf-8")) or {}
    profiles = raw.get("profiles") or {}
    if not profiles:
        pytest.skip("profiles_runtime.yaml 无 profiles")
    for pid, prof in profiles.items():
        out = _fmt(prof, stage="warming")
        assert out and "None" not in out, f"人设 {pid} 的 prompt 出现 None 占位"
        age = prof.get("age")
        if isinstance(age, int) and 1 <= age <= 120:
            assert f"你今年{age}岁" in out, f"人设 {pid} 的 age={age} 没进 prompt"
