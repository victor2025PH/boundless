"""会话语言契约端到端验收 —— 真实 SkillManager 驱动 process_message 多轮对话。

对应 skill_manager._handle_message_guarded 的「3b. 会话级语言决策」块
（lang_policy 单一事实源），验收 2026-07 三类线上语言事故的整链修复：

  事故1：用户明确说「用日语聊」被无视 —— 请求内容从不参与决策，回复语言
         仍镜像消息书写语言（场景 2 / 3：明确请求立即生效并持久为偏好；
         中文书写请求日语后继续打中文，偏好因 pref_input 豁免绝不释放）。
  事故2：中文会话里发一个品牌词「whatsapp」→ 被当英语强证据，整条回复翻成
         英文并污染后续轮次（场景 1 / 7：中性 token 零证据，粘住上一轮）。
  事故3：单条误判直接改写 reply_lang 并粘住后续轮次（场景 4 / 5：强证据
         才立即跟随；偏好释放要求稳定漂移到第三种语言 ≥2 条强证据）。

与 tests/test_lang_policy.py（纯函数层）的差异：本文件经真实 SkillManager
实例（stub 掉 LLM）跑通 process_message 全链路，断言 ContextStore 持久的
user_context 中 reply_lang / user_lang_pref / user_lang_pref_input 的演进。

时序说明：_conversation_history 滞后一轮（第 N 轮的用户消息在第 N+1 轮才补录，
而 3b 决策先于补录执行）。3b 已做「时序补齐」——把 last_message（= 上一轮用户
消息）临时并入策略扫描窗口，使偏好漂移释放与收件箱 draft 产线语义一致：
漂移后的第 **2** 条强证据消息即释放（单条永不释放的硬护栏不变）。
详见场景 5 内注释。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml

from src.hooks.registry import HookRegistry
from src.skills.skill_manager import GreetingSkill, SkillManager
from src.utils.config_manager import ConfigManager


# ── 测试基建 ────────────────────────────────────────────────────


class FakeAIClient:
    """process_message 路径上被调用的最小 AI 接口（不出网）。

    - generate_reply_with_intent：返回带自增序号 + 随机后缀的字符串——
      每轮回复彼此不同，避免触发 5b 防复读重试干扰调用计数；
    - chat：3b 的 _llm_judge_language_request 兜底短判用，固定回 "no"
      （消息提及语言名但正则未命中时才会走到这里）；
    - _detect_message_language：复用 lang_policy 的确定性检测做简单 stub
      （仅媒体/自拍 Stage 文案语言用，本测试路径不依赖）。
    """

    model = "fake-model"

    def __init__(self):
        self.reply_count = 0
        self.chat_calls: list = []

    async def generate_reply_with_intent(self, *args, **kwargs):
        self.reply_count += 1
        return f"回复{self.reply_count}-{uuid.uuid4().hex}"

    async def chat(self, *args, **kwargs):
        self.chat_calls.append(args[0] if args else "")
        return "no"

    def _detect_message_language(self, text: str) -> str:
        from src.ai.lang_policy import classify_evidence
        return classify_evidence(text)[0] or "zh"

    async def embed(self, texts):
        return [[0.0] * 8 for _ in texts]

    async def embed_with_fallback(self, texts):
        return [[0.0] * 8 for _ in texts]


async def _make_sm(tmp_path: Path) -> SkillManager:
    """最小可跑通 process_message 的真实 SkillManager。

    构造方式照抄既有测试：临时 config 目录（同 test_process_message_kb_skip
    的 _write_min_config，冷却全 0）+ memory 配置（同 test_inbox_draft_engine
    的 _make_cm，vector/extract 关闭避免后台 LLM 抽取）。不调 initialize()
    （跳过域包加载），手工注册 GreetingSkill —— _select_skill 对任何意图都会
    回退到 greeting，从而每轮都产出回复、_conversation_history 正常累积
    （偏好释放场景依赖它）。
    """
    cfg = {
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {
            "enabled": [],
            # 冷却全 0：多轮连发不被 per_user/per_content/per_chat_user 拦截
            # （冷却拦截发生在 3b 之前，会直接吞掉当轮语言决策）
            "cooldown": {"global": 0, "per_user": 0, "per_content": 0, "per_chat_user": 0},
        },
        "intent": {"keywords": {}, "patterns": {}},
        "reply": {},
        "context_store": {"ttl_days": 30},
        "memory": {
            "enabled": True,
            "db_path": str(tmp_path / "episodic.db"),
            "vector": {"enabled": False},
            "extract": {"enabled": False},
        },
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "templates.yaml").write_text("greeting: hi\n", encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text("channels: {}\n", encoding="utf-8")
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    await cm.load()

    ai = FakeAIClient()
    sm = SkillManager(cm, ai)
    sm.skills["greeting"] = GreetingSkill(cm, ai)
    return sm


@pytest.fixture(autouse=True)
def _clean_hook_registry():
    """隔离域包 hook 单例：同进程其他测试可能注册过 payment hook，
    其 is_ambiguous_token_message（EP/JC 等）会干预 3b 的检测源文本。"""
    HookRegistry.reset()
    yield
    HookRegistry.reset()


async def _say(sm: SkillManager, user_id: str, text: str, chat_id: int, **extra):
    """发一轮消息并断言全链路产出了回复（保证历史正常累积）。"""
    ctx = {"chat_id": chat_id}
    ctx.update(extra)
    reply = await sm.process_message(text, user_id, ctx)
    assert reply, f"process_message 未产出回复: {text!r}"
    return reply


def _lang_events() -> dict:
    from src.monitoring.metrics_store import get_metrics_store
    return dict(get_metrics_store()._lang_events)


# ── 场景 1：事故2回归 —— 中性品牌词不改变会话语言 ────────────────


async def test_incident2_neutral_brand_word_sticks_to_chinese(tmp_path):
    """中文聊两轮后发「whatsapp」→ reply_lang 仍 zh，且不产生任何语言偏好。"""
    sm = await _make_sm(tmp_path)
    uid = "u_incident2"

    await _say(sm, uid, "今天上班好累啊", 101)
    assert sm._get_user_context(uid).get("reply_lang") == "zh"

    await _say(sm, uid, "晚上想吃火锅呢", 101)
    assert sm._get_user_context(uid).get("reply_lang") == "zh"

    # 品牌词剥离后零证据 → 粘住上一轮 zh，绝不切英文
    await _say(sm, uid, "whatsapp", 101)
    uc = sm._get_user_context(uid)
    assert uc.get("reply_lang") == "zh"
    assert "user_lang_pref" not in uc


# ── 场景 2：事故1回归 —— 中文书写的日语请求 + 偏好坚持 ──────────


async def test_incident1_chinese_written_japanese_request_persists(tmp_path):
    """「我们用日语聊吧」→ 立即切 ja 并持久偏好（pref_input=zh）；
    后续中文短句与中性词都不动摇偏好（pref_input 豁免释放）。"""
    sm = await _make_sm(tmp_path)
    uid = "u_incident1"

    await _say(sm, uid, "今天上班好忙啊", 102)
    assert sm._get_user_context(uid).get("reply_lang") == "zh"

    # 中文书写的明确请求：请求内容（日语）胜过书写语言（中文）
    await _say(sm, uid, "我们用日语聊吧", 102)
    uc = sm._get_user_context(uid)
    assert uc.get("reply_lang") == "ja"
    assert uc.get("user_lang_pref") == "ja"
    assert uc.get("user_lang_pref_input") == "zh"
    assert _lang_events().get("explicit_request", 0) >= 1  # 埋点

    # 中文强证据短句：lang==pref_input（用户本来就在写中文）→ 豁免，不释放
    await _say(sm, uid, "好的知道了", 102)
    uc = sm._get_user_context(uid)
    assert uc.get("reply_lang") == "ja"
    assert uc.get("user_lang_pref") == "ja"

    # 中性词零证据 → 偏好继续生效
    await _say(sm, uid, "ok", 102)
    uc = sm._get_user_context(uid)
    assert uc.get("reply_lang") == "ja"
    assert uc.get("user_lang_pref") == "ja"
    assert uc.get("user_lang_pref_input") == "zh"


# ── 场景 3：英文书写的日语请求 ──────────────────────────────────


async def test_english_written_japanese_request(tmp_path):
    """"can you speak japanese?" → reply_lang=ja、偏好持久（pref_input=en）。"""
    sm = await _make_sm(tmp_path)
    uid = "u_en_request"

    await _say(sm, uid, "can you speak japanese?", 103)
    uc = sm._get_user_context(uid)
    assert uc.get("reply_lang") == "ja"
    assert uc.get("user_lang_pref") == "ja"
    assert uc.get("user_lang_pref_input") == "en"
    assert _lang_events().get("explicit_request", 0) >= 1


# ── 场景 4：真实语言切换（强证据）立即跟随 ──────────────────────


async def test_genuine_language_switch_follows_immediately(tmp_path):
    """中文两轮后发完整英文长句 → 当条即切 en；检测切换不是偏好，不落 pref。"""
    sm = await _make_sm(tmp_path)
    uid = "u_switch"

    await _say(sm, uid, "今天上班好累啊", 104)
    await _say(sm, uid, "晚上想吃火锅呢", 104)
    assert sm._get_user_context(uid).get("reply_lang") == "zh"

    await _say(sm, uid, "What did you eat today my friend?", 104)
    uc = sm._get_user_context(uid)
    assert uc.get("reply_lang") == "en"
    assert "user_lang_pref" not in uc  # 强证据跟随 ≠ 明确请求，不持久偏好


# ── 场景 5：偏好漂移释放（stable_switch）────────────────────────


async def test_pref_drift_release_after_stable_switch(tmp_path):
    """日文书写请求（pref=ja、pref_input=ja）后用户稳定漂移回中文 →
    偏好被释放（source=stable_switch），reply_lang 跟随 zh。

    时序说明（时序补齐后的 A 线实际行为）：_conversation_history 滞后一轮
    （第 N 轮消息在第 N+1 轮才入史，3b 决策先于入史），但 3b 已把
    last_message（= 上一轮用户消息）临时并入策略扫描窗口（skill_manager
    「时序补齐」块）——process_message 与收件箱 draft 产线（传入含当前
    消息的完整历史）**同轮**释放：
      第 1 条中文：补齐进窗口的上一轮消息 = 日文请求本身 → 连续段不
        成立，偏好坚持（单条永不释放的硬护栏）；
      第 2 条中文：补齐进窗口的上一轮消息 = 第 1 条中文，本条 + 补齐
        历史 = 连续 2 条同语言强证据 → stable_switch 释放。
    """
    sm = await _make_sm(tmp_path)
    uid = "u_drift"

    # 日文书写的明确请求：pref=ja 且 pref_input=ja（漂移到 zh 不受豁免保护）
    await _say(sm, uid, "日本語で話してください", 105)
    uc = sm._get_user_context(uid)
    assert uc.get("reply_lang") == "ja"
    assert uc.get("user_lang_pref") == "ja"
    assert uc.get("user_lang_pref_input") == "ja"

    # 中文强证据第 1 条：单条绝不释放偏好（硬性护栏）
    await _say(sm, uid, "今天上班的时候好累啊", 105)
    uc = sm._get_user_context(uid)
    assert uc.get("reply_lang") == "ja"
    assert uc.get("user_lang_pref") == "ja"

    # 中文强证据第 2 条：last_message 补齐把第 1 条中文并入扫描窗口 →
    # 本条 + 补齐历史 = 连续 2 条 → stable_switch 释放（与收件箱产线同轮）
    await _say(sm, uid, "晚上我想去吃一顿火锅", 105)
    uc = sm._get_user_context(uid)
    assert uc.get("reply_lang") == "zh"
    assert "user_lang_pref" not in uc
    assert "user_lang_pref_input" not in uc
    ev = _lang_events()
    assert ev.get("explicit_request", 0) >= 1
    assert ev.get("stable_switch", 0) >= 1  # 释放埋点

    # 释放后幂等：继续中文 → 保持 zh，偏好不复活
    await _say(sm, uid, "周末打算去公园散散步", 105)
    uc = sm._get_user_context(uid)
    assert uc.get("reply_lang") == "zh"
    assert "user_lang_pref" not in uc


# ── 场景 6：reply_lang_locked 锁定透传 ──────────────────────────


async def test_reply_lang_locked_passthrough(tmp_path):
    """context 带 reply_lang=en + reply_lang_locked=True → 跳过整个 3b 决策，
    哪怕消息是中文也不改写；锁是单次请求级，下一轮无锁即恢复自动决策。"""
    sm = await _make_sm(tmp_path)
    uid = "u_locked"

    await _say(
        sm, uid, "你今天吃饭了吗", 106,
        reply_lang="en", reply_lang_locked=True,
    )
    uc = sm._get_user_context(uid)
    assert uc.get("reply_lang") == "en"  # 中文消息未触发改写
    assert "user_lang_pref" not in uc

    # 残留锁清理：下一轮 context 无锁 → 恢复自动决策，中文强证据即跟随
    await _say(sm, uid, "明天想去爬山呢", 106)
    uc = sm._get_user_context(uid)
    assert not uc.get("reply_lang_locked")
    assert uc.get("reply_lang") == "zh"


# ── 场景 7：中性短词在英文会话同样粘滞 ──────────────────────────


async def test_neutral_short_word_sticky_in_english_session(tmp_path):
    """全英文会话两轮后发 "ok" → 零证据粘住上一轮，reply_lang 保持 en。"""
    sm = await _make_sm(tmp_path)
    uid = "u_en_sticky"

    await _say(sm, uid, "What did you have for lunch today?", 107)
    assert sm._get_user_context(uid).get("reply_lang") == "en"

    await _say(sm, uid, "I went hiking with my brother last weekend.", 107)
    assert sm._get_user_context(uid).get("reply_lang") == "en"

    await _say(sm, uid, "ok", 107)
    uc = sm._get_user_context(uid)
    assert uc.get("reply_lang") == "en"
    assert "user_lang_pref" not in uc
