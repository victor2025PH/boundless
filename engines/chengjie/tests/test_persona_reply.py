"""共享人设回复生成器单测（Phase 1：生成产线收敛）。

锁定 ``src/inbox/persona_reply.py`` 契约：
  - normalize_history：方向归一 + 取最后入站文本 + 兜底
  - generate_persona_reply：主路径（skill_manager 人设产线）/ 兜底路径（仅 ai_client）/
    空上下文早退 / 译文附加
这些是 /api/desktop/smart-reply 与收件箱全自动草稿复用的同一条产线，必须稳定。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.ai.translation_service import TranslationService
from src.inbox.persona_reply import (
    generate_persona_reply,
    normalize_history,
    resolve_reply_language,
    trim_stale_history,
)


# ── trim_stale_history：时间断层修剪（治「换英语」类陈旧上下文幻觉）────────

_DAY = 86400.0


def test_trim_stale_keeps_fresh_and_labels_stale():
    """10 天前的旧对话只留 keep_stale 条并打 [N天前] 标记；断层后的新鲜段全保留。"""
    base = 1_800_000_000.0
    msgs = [
        {"direction": "in", "text": "gimme talk English", "ts": base},
        {"direction": "out", "text": "嘿你突然说英语啦", "ts": base + 60},
        {"direction": "in", "text": "你会方言吗", "ts": base + 120},
        {"direction": "out", "text": "会一点粤语～", "ts": base + 180},
        # —— 10 天断层 ——
        {"direction": "in", "text": "给我你的近照", "ts": base + 10 * _DAY},
    ]
    out = trim_stale_history(msgs, gap_hours=48, keep_stale=2)
    assert len(out) == 3  # 2 条旧 + 1 条新
    assert out[-1]["text"] == "给我你的近照"  # 新鲜段原样
    assert out[0]["text"].startswith("[10天前]") or out[0]["text"].startswith("[9天前]")
    assert out[1]["text"].startswith("[")
    # 最早那两条（英语话题）被裁掉——不再喂给 LLM 当新鲜话头
    assert not any("English" in str(m.get("text")) for m in out)


def test_trim_stale_no_gap_returns_all():
    base = 1_800_000_000.0
    msgs = [{"direction": "in", "text": f"m{i}", "ts": base + i * 60}
            for i in range(5)]
    assert trim_stale_history(msgs) == msgs


def test_trim_stale_missing_ts_backcompat():
    """行内无 ts（旧调用方/测试桩）→ 视为新鲜，零行为变更。"""
    msgs = [{"direction": "in", "text": "a"}, {"direction": "out", "text": "b"}]
    assert trim_stale_history(msgs) == msgs


# ── resolve_reply_language：回复语言决策 + 短消息防误切 ─────────────────

def test_resolve_lang_explicit_wins():
    """手动 UI 选定的目标语最高优先，不被二次猜测覆盖。"""
    assert resolve_reply_language("你好", explicit="en") == "en"


def test_resolve_lang_follows_latest_inbound():
    """客户切到英文（足够长）→ 跟最新一条走英文。"""
    assert resolve_reply_language("What did you eat today?") == "en"


def test_resolve_lang_short_token_keeps_window_dominant():
    """中文会话里偶发一个英文短 token，不应误切英文（回落窗口主导语言）。"""
    history = [
        {"role": "user", "content": "你在干嘛呢"},
        {"role": "assistant", "content": "刚下班～"},
        {"role": "user", "content": "ok"},
    ]
    assert resolve_reply_language("ok", history) == "zh"


def test_resolve_lang_short_token_english_window_stays_english():
    """全英文会话里的短 token（yes）→ 仍英文，不被默认 zh 拽回。"""
    history = [
        {"role": "user", "content": "Where are you now?"},
        {"role": "assistant", "content": "On my way home."},
        {"role": "user", "content": "ok"},
    ]
    assert resolve_reply_language("ok", history) == "en"


def test_resolve_lang_empty_defaults():
    assert resolve_reply_language("", default="zh") == "zh"


def test_resolve_lang_brand_word_not_english():
    """事故2回归：中文会话里发「whatsapp」（8 字符，旧 <4 护栏漏过）→ 仍中文。"""
    history = [
        {"role": "user", "content": "你们支持哪些收款方式"},
        {"role": "assistant", "content": "USDT、跨境电汇都可以"},
        {"role": "user", "content": "whatsapp"},
    ]
    assert resolve_reply_language("whatsapp", history) == "zh"


def test_resolve_lang_explicit_request_in_chinese():
    """事故1回归：中文书写的「用日语」请求 → 回复语言立即切日语。"""
    history = [
        {"role": "user", "content": "你好呀"},
        {"role": "assistant", "content": "你好！"},
        {"role": "user", "content": "我们用日语聊吧"},
    ]
    assert resolve_reply_language("我们用日语聊吧", history) == "ja"


def test_resolve_lang_request_persists_from_history():
    """事故1回归：请求在历史里，本条是中性短词 → 偏好持久，仍日语。"""
    history = [
        {"role": "user", "content": "我们用日语聊吧"},
        {"role": "assistant", "content": "わかりました！これから日本語で話しますね"},
        {"role": "user", "content": "ok"},
    ]
    assert resolve_reply_language("ok", history) == "ja"


# ── normalize_history ─────────────────────────────────────────

def test_normalize_history_roles_and_last_inbound():
    msgs = [
        {"direction": "in", "text": "你好"},
        {"direction": "out", "text": "您好，在的"},
        {"direction": "inbound", "text": "怎么下单？"},
        {"direction": "out", "text": ""},  # 空文本应被滤掉
    ]
    history, last_inbound = normalize_history(msgs)
    assert history == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "您好，在的"},
        {"role": "user", "content": "怎么下单？"},
    ]
    assert last_inbound == "怎么下单？"


def test_normalize_history_fallback_to_last_when_no_inbound():
    """全是出向消息时，last_inbound 回落到末条内容（兜底锚点）。"""
    msgs = [{"direction": "out", "text": "我先发一句"}]
    history, last_inbound = normalize_history(msgs)
    assert last_inbound == "我先发一句"


def test_normalize_history_ignores_non_dict_and_empty():
    history, last_inbound = normalize_history([None, {}, {"text": "  "}, "bad"])
    assert history == []
    assert last_inbound == ""


# ── 测试替身 ──────────────────────────────────────────────────

class _FakeAI:
    def __init__(self):
        self.last_ctx = None

    async def generate_reply_with_intent(self, *, user_message, intent,
                                         user_context, strategy_overrides=None):
        self.last_ctx = user_context
        return f"[人设]{user_message}"

    async def chat(self, prompt):
        return "[兜底]回复"


class _FakeSM:
    def __init__(self, ai):
        self.ai_client = ai

    def _recognize_intent(self, text):
        return "consult"

    def get_strategy_for_intent(self, intent, user_id):
        return ({"temperature": 0.7}, "sid-1")


class _FakeRes:
    ok = True

    def __init__(self, text="<translated-en>"):
        self._text = text

    def to_dict(self):
        return {"translated_text": self._text}


class _FakeTranslation(TranslationService):
    """P0-198 起 _translate_reply 会在 CJK→非 CJK 冲突时显式传 source_lang，
    替身签名须兼容；占位译文用英文——中文占位会被新的「译文仍含 CJK=假译文」
    守卫正确拒掉（那个守卫有专属用例）。"""

    def __init__(self, out="<translated-en>"):  # 不调父类，避免真实依赖
        self._out = out

    async def translate(self, text, target_lang="", style="", source_lang="", **kw):
        return _FakeRes(self._out)


def _app(**state):
    return SimpleNamespace(state=SimpleNamespace(**state))


# ── generate_persona_reply ───────────────────────────────────

@pytest.mark.asyncio
async def test_generate_persona_reply_main_path_uses_skill_manager():
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "怎么下单"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="room1",
        last_inbound=last, history=history,
    )
    assert out["ok"] is True
    assert out["reply"] == "[人设]怎么下单"
    assert out["intent"] == "consult"
    # 策略覆盖透传到 ctx 的 reply_strategy
    assert ai.last_ctx["_reply_strategy"] == {"temperature": 0.7}
    assert "persona_tier" in out


@pytest.mark.asyncio
async def test_generate_persona_reply_fallback_when_no_skill_manager():
    ai = _FakeAI()
    app = _app(skill_manager=None, ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "在吗"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r2",
        last_inbound=last, history=history,
    )
    assert out["ok"] is True
    assert out["reply"] == "[兜底]回复"


@pytest.mark.asyncio
async def test_generate_persona_reply_empty_context_returns_not_ok():
    app = _app(skill_manager=None, ai_client=_FakeAI())
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r3",
        last_inbound="", history=[],
    )
    assert out["ok"] is False
    assert out["reply"] == ""


@pytest.mark.asyncio
async def test_generate_persona_reply_appends_translation():
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=_FakeTranslation())
    history, last = normalize_history([{"direction": "in", "text": "hi"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r4",
        last_inbound=last, history=history, target_lang="en",
    )
    assert out["ok"] is True
    assert out["translated"] == "<translated-en>"


@pytest.mark.asyncio
async def test_generate_persona_reply_rejects_cjk_echo_translation():
    """P0-198：中文草稿→en 目标，引擎回显中文（identity/半吊子）→ 不给假译文。

    198 实锤链路：『哈哈，我叫Steven啦』被判 en → identity 把中文当译文 →
    工坊「填入并发送」按已译直发 → 中文直达外语客户。译文仍含 CJK 必须按
    「无译文」处理，UI 不再出现可一键发送的假译文。"""
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None,
               translation_service=_FakeTranslation(out="哈哈，我叫Steven啦"))
    history, last = normalize_history([{"direction": "in", "text": "hi"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r4b",
        last_inbound=last, history=history, target_lang="en",
    )
    assert out["ok"] is True
    assert "translated" not in out


# ── P2-198 直出模式：客户语言正文 + 坐席 UI 语言对照（gloss，只读）──────────

@pytest.mark.asyncio
async def test_gloss_attached_when_reply_lang_differs_from_ui_lang():
    """正文中文直出（FakeAI 回显含 CJK）+ 坐席 UI=en → 附 en 对照。"""
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=_FakeTranslation())
    history, last = normalize_history([{"direction": "in", "text": "怎么下单"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="g1",
        last_inbound=last, history=history, gloss_lang="en",
    )
    assert out["ok"] is True
    assert out["gloss"] == "<translated-en>"
    assert out["gloss_lang"] == "en"


@pytest.mark.asyncio
async def test_gloss_skipped_when_same_language_or_real_translation():
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=_FakeTranslation())
    history, last = normalize_history([{"direction": "in", "text": "怎么下单"}])
    # 正文语言 == UI 语言 → 不产对照
    out_same = await generate_persona_reply(
        app=app, platform="telegram", chat_key="g2",
        last_inbound=last, history=history, gloss_lang="zh",
    )
    assert "gloss" not in out_same
    # 已有真的跨语言 translated（坐席显式选目标语）→ 不叠第二份对照
    out_t = await generate_persona_reply(
        app=app, platform="telegram", chat_key="g3",
        last_inbound=last, history=history, target_lang="en", gloss_lang="en",
    )
    assert out_t.get("translated") == "<translated-en>"
    assert "gloss" not in out_t


# ── generate_topic_opener（P1-198 工坊「开启新话题」模式）─────────────────

@pytest.mark.asyncio
async def test_topic_opener_works_without_inbound():
    """开新话题不依赖入站消息（没人说话也能主动开场），指令注入产线主路径。"""
    from src.inbox.persona_reply import generate_topic_opener
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    out = await generate_topic_opener(
        app=app, platform="whatsapp", chat_key="c1", history=[])
    assert out["ok"] is True
    assert out["intent"] == "proactive_opener"
    assert out["mode"] == "opener"
    # FakeAI 回显 directive → 指令核心要素可见
    assert "主动开启一个新话题" in out["reply"]
    assert ai.last_ctx["intent"] == "proactive_opener"
    assert ai.last_ctx["reply_lang"]      # 语言决策必须给出结果


def test_opener_directive_language_avoid_and_no_template():
    """指令纯函数：目标语言钉入 + 最近已发内容列入反例 + 模板壳出现在禁令里。"""
    from src.inbox.persona_reply import build_opener_directive
    d = build_opener_directive(
        angle="聊聊窗外的天气", reply_lang="en",
        recent_out=["Hi there my friend, how was your day"])
    assert "（en）" in d
    assert "聊聊窗外的天气" in d
    assert "好久没联系" in d                     # 作为禁令出现
    assert "Hi there my friend" in d             # 防复读反例


@pytest.mark.asyncio
async def test_topic_opener_fallback_without_skill_manager():
    """无 SkillManager → ai.chat 兜底，仍能出开场。"""
    from src.inbox.persona_reply import generate_topic_opener
    ai = _FakeAI()
    app = _app(skill_manager=None, ai_client=ai, telegram_client=None,
               translation_service=None)
    out = await generate_topic_opener(
        app=app, platform="telegram", chat_key="c2",
        history=[{"role": "user", "content": "hello"}])
    assert out["ok"] is True
    assert out["reply"] == "[兜底]回复"


@pytest.mark.asyncio
async def test_generate_persona_reply_no_ai_at_all_not_ok():
    """skill_manager 与 ai_client 皆缺 → 无回复，ok=False（不抛错）。"""
    app = _app(skill_manager=None, ai_client=None, telegram_client=None)
    history, last = normalize_history([{"direction": "in", "text": "测试"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r5",
        last_inbound=last, history=history,
    )
    assert out["ok"] is False
    assert out["reply"] == ""


# ── 语言决策单一事实源（Phase 2 收敛）───────────────────────────

@pytest.mark.asyncio
async def test_persona_reply_auto_resolves_chinese():
    """不传 reply_lang/target_lang → 按客户消息自动决策；中文 → zh 并回写 out。"""
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "今天怎么下单呀"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r6",
        last_inbound=last, history=history,
    )
    assert out["reply_lang"] == "zh"
    assert ai.last_ctx["reply_lang"] == "zh"


@pytest.mark.asyncio
async def test_persona_reply_auto_resolves_english():
    """英文客户、无显式语言 → 自动决策 en（修复前默认 zh 的核心隐患）。"""
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history(
        [{"direction": "in", "text": "What did you eat today?"}]
    )
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r7",
        last_inbound=last, history=history,
    )
    assert out["reply_lang"] == "en"
    assert ai.last_ctx["reply_lang"] == "en"


@pytest.mark.asyncio
async def test_persona_reply_explicit_reply_lang_wins():
    """显式 reply_lang 最高优先，压过自动决策与 target_lang。"""
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "今天怎么下单"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r8",
        last_inbound=last, history=history,
        reply_lang="ja", target_lang="en",
    )
    assert out["reply_lang"] == "ja"
    assert ai.last_ctx["reply_lang"] == "ja"


@pytest.mark.asyncio
async def test_persona_reply_target_lang_used_as_body_lang_when_no_reply_lang():
    """坐席选定 target_lang（无 reply_lang）→ 正文按 target_lang 生成。"""
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "今天怎么下单"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r9",
        last_inbound=last, history=history, target_lang="en",
    )
    assert out["reply_lang"] == "en"
    assert ai.last_ctx["reply_lang"] == "en"


# ── 统一规则引擎接线（彻底对齐：优先 generate_inbox_draft）───────────────

class _FakeSMUnified(_FakeSM):
    """带统一引擎 + config 的 SM 替身。

    记录是否调用统一引擎 / 是否走了直连记忆写回，验证：
      - 默认走 generate_inbox_draft 并透传入参
      - 统一引擎已写记忆 → 不再触发 persona_reply 文末的 _episodic_memory_extract_async
    """

    def __init__(self, ai, *, unified=True):
        super().__init__(ai)
        self.config = SimpleNamespace(config={
            "inbox": {"auto_draft": {"unified_pipeline": unified}}
        })
        self.inbox_draft_calls = []
        self.episodic_writeback_calls = []

    async def generate_inbox_draft(self, *, text, chat_key, platform,
                                   history=None, persona_id="", reply_lang="",
                                   risk_level="", media_type="", media_ref="",
                                   media_desc="", channel="inbox",
                                   conversation_id="", peer_audio_emotion=None,
                                   account_id="", agent_instruction=""):
        # ⚠ 签名必须与真 SkillManager.generate_inbox_draft 同步：漂移会让
        # persona_reply 的统一引擎调用 TypeError 被吞、静默回落直连——本替身
        # 曾漏 agent_instruction（P22 加参后），本文件两例红到 2026-08-01 才被发现。
        self.inbox_draft_calls.append({
            "text": text, "chat_key": chat_key, "platform": platform,
            "persona_id": persona_id, "reply_lang": reply_lang,
            "risk_level": risk_level,
            "history_len": len(history or []),
            "agent_instruction": agent_instruction,
        })
        return {"reply": f"[统一]{text}", "intent": "unified_intent"}

    async def _episodic_memory_extract_async(self, *a, **k):
        self.episodic_writeback_calls.append((a, k))


@pytest.mark.asyncio
async def test_persona_reply_prefers_unified_engine():
    """SM 暴露 generate_inbox_draft 且 flag 开 → 走统一引擎，入参透传，记忆不双写。"""
    ai = _FakeAI()
    sm = _FakeSMUnified(ai)
    app = _app(skill_manager=sm, ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "我叫Jun，记得吗"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="7340576921",
        last_inbound=last, history=history, persona_id="p1",
        risk_level="medium",
    )
    assert out["ok"] is True
    assert out["reply"] == "[统一]我叫Jun，记得吗"
    assert out["intent"] == "unified_intent"
    assert len(sm.inbox_draft_calls) == 1
    call = sm.inbox_draft_calls[0]
    assert call["chat_key"] == "7340576921"
    assert call["platform"] == "telegram"
    assert call["persona_id"] == "p1"
    assert call["risk_level"] == "medium"  # 风险分档透传到统一引擎
    # 统一引擎自带记忆写回 → persona_reply 不应再触发一次（避免双写）
    assert sm.episodic_writeback_calls == []
    # 统一引擎主路径不应回落到直连 ai_client
    assert ai.last_ctx is None


@pytest.mark.asyncio
async def test_persona_reply_unified_flag_off_falls_back_to_direct():
    """flag=false → 跳过统一引擎，回落直连产线（ai_client 被调用）。"""
    ai = _FakeAI()
    sm = _FakeSMUnified(ai, unified=False)
    app = _app(skill_manager=sm, ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "在吗"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r10",
        last_inbound=last, history=history,
    )
    assert out["ok"] is True
    assert out["reply"] == "[人设]在吗"        # 直连路径产物
    assert sm.inbox_draft_calls == []           # 未走统一引擎
    assert ai.last_ctx is not None              # 直连 ai_client 被调用


# ── 分段计时（2026-08-01 观测补齐）────────────────────────────
# 「尖峰慢在生成还是翻译」的归因数据面：out["timings"] 带 gen/xlate/gloss_ms +
# gen_path（unified/direct/fallback），smart-reply 路由并进日志后 pop 掉。

def _assert_timings_shape(out):
    tm = out.get("timings")
    assert isinstance(tm, dict), "成功产出必须带 timings"
    for k in ("gen_ms", "xlate_ms", "gloss_ms"):
        assert isinstance(tm[k], int) and tm[k] >= 0, f"{k} 须为非负整数毫秒"
    return tm


@pytest.mark.asyncio
async def test_persona_reply_timings_direct_path():
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "怎么下单"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="t1",
        last_inbound=last, history=history,
    )
    tm = _assert_timings_shape(out)
    assert tm["gen_path"] == "direct"


@pytest.mark.asyncio
async def test_persona_reply_timings_fallback_path():
    ai = _FakeAI()
    app = _app(skill_manager=None, ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "在吗"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="t2",
        last_inbound=last, history=history,
    )
    tm = _assert_timings_shape(out)
    assert tm["gen_path"] == "fallback"


@pytest.mark.asyncio
async def test_persona_reply_timings_unified_path():
    ai = _FakeAI()
    sm = _FakeSMUnified(ai)
    app = _app(skill_manager=sm, ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "你好"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="t3",
        last_inbound=last, history=history,
    )
    tm = _assert_timings_shape(out)
    assert tm["gen_path"] == "unified"


def test_fake_unified_signature_in_sync_with_real():
    """替身签名同步门禁：persona_reply 会把全部具名参数喂给真
    SkillManager.generate_inbox_draft——真引擎加参而替身不跟，统一引擎调用在测试里
    TypeError 被吞、静默回落直连，「统一路径」从此失测（agent_instruction 实锤）。"""
    import inspect
    from src.skills.skill_manager import SkillManager
    real = set(inspect.signature(SkillManager.generate_inbox_draft).parameters) - {"self"}
    fake = set(inspect.signature(_FakeSMUnified.generate_inbox_draft).parameters) - {"self"}
    missing = real - fake
    assert not missing, (
        f"_FakeSMUnified.generate_inbox_draft 缺参：{sorted(missing)}"
        "（真引擎加参后替身必须同步，否则统一路径静默失测）")


@pytest.mark.asyncio
async def test_persona_reply_no_context_has_no_timings():
    """早退路径（无上下文）不产出 timings——没生成过任何东西，计时是噪声。"""
    app = _app(skill_manager=None, ai_client=_FakeAI())
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="t4",
        last_inbound="", history=[],
    )
    assert out["ok"] is False
    assert "timings" not in out
