"""P2.1 测试: personalized_message — 不依赖真 Ollama, monkeypatch _call_ollama_generate。

覆盖：
- 语言后检 (ja/zh/en)
- 内容审计 (黑名单)
- 长度限制
- LLM 失败 → fallback 模板
- LLM 语言不符 → retry → 仍不符 → fallback
- prompt 构建包含目标用户上下文
"""
from __future__ import annotations

import pytest

from src.ai import personalized_message as pm


# ────────── 语言后检 ──────────

def test_verify_language_ja_with_hiragana():
    assert pm.verify_language("こんにちは🌸 同じグループで...", "ja-JP") is True


def test_verify_language_ja_pure_kanji_fails():
    """纯汉字不算日语 (中日同形字会误判)"""
    assert pm.verify_language("初次见面 多多关照", "ja-JP") is False


def test_verify_language_zh_with_han():
    assert pm.verify_language("你好🌸 看到我们在同一个群里", "zh-CN") is True


def test_verify_language_zh_rejects_japanese():
    assert pm.verify_language("はじめまして🌸", "zh-CN") is False


def test_verify_language_en_mostly_ascii():
    assert pm.verify_language("Hi 🌸 nice to meet you in the group!", "en-US") is True


def test_verify_language_empty_text_fails():
    assert pm.verify_language("", "ja-JP") is False


# ────────── 内容审计 ──────────

def test_audit_rejects_url():
    ok, why = pm.audit_content("Add me on https://example.com 🌸")
    assert ok is False
    assert "blacklist" in why


def test_audit_rejects_promo_jp():
    ok, why = pm.audit_content("無料で稼ぐ方法を教えます🌸")
    assert ok is False


def test_audit_rejects_promo_zh():
    ok, why = pm.audit_content("免费教你赚钱方法🌸")
    assert ok is False


def test_audit_accepts_clean_text():
    ok, why = pm.audit_content("はじめまして🌸 同じママ友グループで...")
    assert ok is True
    assert why == ""


# ────────── prompt 构建 ──────────

def test_build_prompt_includes_target_details():
    target = pm.TargetUser(
        name="田中花子",
        bio="3歳の娘のママです",
        recent_posts=["離乳食レシピ", "公園のお散歩"],
        group_context="ママ友サークル",
    )
    persona = pm.PersonaContext(
        bio="日本东京的中年家庭主妇",
        language="ja-JP",
    )
    prompt = pm.build_prompt(target, persona, "verification_note", "ja-JP")
    assert "田中花子" in prompt
    assert "ママ友サークル" in prompt
    assert "離乳食レシピ" in prompt
    assert "60 character" in prompt or "60 char" in prompt or "60" in prompt
    assert "Japanese" in prompt or "日本語" in prompt


def test_build_prompt_lang_label_zh():
    target = pm.TargetUser(name="王芳", group_context="美食群")
    persona = pm.PersonaContext(language="zh-CN")
    prompt = pm.build_prompt(target, persona, "first_greeting", "zh-CN")
    assert "中文" in prompt or "Chinese" in prompt
    assert "王芳" in prompt


# ────────── generate_message: LLM 路径成功 ──────────

@pytest.fixture
def mock_resolve(monkeypatch):
    """所有 LLM 路径测试都需要 _resolve_model 假装解析成功, 否则会真去 ping ollama"""
    monkeypatch.setattr(pm, "_resolve_model",
                         lambda preferred=None: "qwen2.5:test")
    return monkeypatch


def test_generate_message_llm_success(mock_resolve):
    """LLM 返回符合所有条件的文本 → 直接采用, fallback=False"""
    def fake_llm(prompt, model=None, timeout=None):
        return "はじめまして🌸 ママ友サークルで田中さんを見つけて..."
    mock_resolve.setattr(pm, "_call_ollama_generate", fake_llm)

    target = pm.TargetUser(name="田中", group_context="ママ友サークル")
    persona = pm.PersonaContext(bio="主妇", language="ja-JP")
    text, meta = pm.generate_message(target, persona, "verification_note", "ja-JP")
    assert "はじめまして" in text
    assert meta["fallback"] is False
    assert meta["lang_verified"] is True
    assert meta["audit_ok"] is True
    assert meta["attempts"] == 1
    assert meta["model"] == "qwen2.5:test"


# ────────── generate_message: 模型解析失败 → 直接 fallback ──────────

def test_generate_message_falls_back_when_no_model(monkeypatch):
    """新增: _resolve_model 返 None (Ollama 不可用) → 跳过 LLM 直接 fallback"""
    monkeypatch.setattr(pm, "_resolve_model", lambda preferred=None: None)
    # 即使 _call_ollama_generate 被调用也不应进入这分支
    called = {"hit": False}
    def should_not_call(*a, **kw):
        called["hit"] = True
        return "should_not_be_used"
    monkeypatch.setattr(pm, "_call_ollama_generate", should_not_call)
    target = pm.TargetUser(name="花子")
    persona = pm.PersonaContext(language="ja-JP")
    text, meta = pm.generate_message(target, persona, "verification_note", "ja-JP")
    assert called["hit"] is False  # 关键: 没浪费 HTTP
    assert meta["fallback"] is True
    assert meta["fallback_reason"] == "first:no_model_available"
    assert pm.verify_language(text, "ja-JP")


# ────────── generate_message: LLM 不可用 → fallback ──────────

def test_generate_message_falls_back_when_ollama_down(mock_resolve):
    mock_resolve.setattr(pm, "_call_ollama_generate", lambda *a, **kw: None)
    target = pm.TargetUser(name="花子")
    persona = pm.PersonaContext(language="ja-JP")
    text, meta = pm.generate_message(target, persona, "verification_note", "ja-JP")
    # fallback 模板必须仍是日语 (含假名)
    assert pm.verify_language(text, "ja-JP")
    assert meta["fallback"] is True
    assert "llm_unavailable" in meta["fallback_reason"]


# ────────── generate_message: LLM 语言错误 → retry → fallback ──────────

def test_generate_message_retries_on_lang_mismatch(mock_resolve):
    """LLM 第一次返回中文 (用户要日语), 应 retry 一次, 仍不符则 fallback"""
    calls = {"count": 0}
    def fake_llm(prompt, model=None, timeout=None):
        calls["count"] += 1
        return "你好🌸 看到我们在同一个群里"  # 中文
    mock_resolve.setattr(pm, "_call_ollama_generate", fake_llm)

    target = pm.TargetUser(name="x")
    persona = pm.PersonaContext(language="ja-JP")
    text, meta = pm.generate_message(target, persona, "verification_note", "ja-JP")
    assert calls["count"] == 2  # 重试了一次
    assert meta["fallback"] is True
    assert pm.verify_language(text, "ja-JP")  # fallback 仍正确


# ────────── generate_message: LLM 输出违规 → fallback ──────────

def test_generate_message_falls_back_on_blacklist(mock_resolve):
    def fake_llm(*a, **kw):
        return "はじめまして🌸 加我 LINE: my_id お願い"
    mock_resolve.setattr(pm, "_call_ollama_generate", fake_llm)
    target = pm.TargetUser(name="x")
    persona = pm.PersonaContext(language="ja-JP")
    text, meta = pm.generate_message(target, persona, "verification_note", "ja-JP")
    assert meta["fallback"] is True
    # 兜底模板应通过审计
    assert pm.audit_content(text)[0] is True


# ────────── generate_message: 长度超限 → fallback ──────────

def test_generate_message_falls_back_on_too_long(mock_resolve):
    def fake_llm(*a, **kw):
        return "はじめまして🌸" + "あ" * 200  # 超过 60 限制
    mock_resolve.setattr(pm, "_call_ollama_generate", fake_llm)
    target = pm.TargetUser(name="x")
    persona = pm.PersonaContext(language="ja-JP")
    text, meta = pm.generate_message(target, persona, "verification_note", "ja-JP")
    assert meta["fallback"] is True
    assert len(text) <= pm.LENGTH_LIMITS["verification_note"]


# ────────── generate_message: 不同 purpose ──────────

def test_generate_message_first_greeting_purpose(mock_resolve):
    mock_resolve.setattr(pm, "_call_ollama_generate", lambda *a, **kw: None)
    target = pm.TargetUser(name="花子")
    persona = pm.PersonaContext(language="ja-JP")
    text, meta = pm.generate_message(target, persona, "first_greeting", "ja-JP")
    assert meta["fallback"] is True
    # greeting 长度上限更宽 (200), 但 fallback 模板都很短, 通过即可
    assert pm.verify_language(text, "ja-JP")
    assert len(text) <= pm.LENGTH_LIMITS["first_greeting"]


# ────────── fallback 模板稳定性 ──────────

def test_fallback_template_stable_for_same_target(mock_resolve):
    """同一个 target 多次调用 fallback 应返回同一条模板 (避免抖动)"""
    mock_resolve.setattr(pm, "_call_ollama_generate", lambda *a, **kw: None)
    target = pm.TargetUser(name="田中花子")
    persona = pm.PersonaContext(language="ja-JP")
    t1, _ = pm.generate_message(target, persona, "verification_note", "ja-JP")
    t2, _ = pm.generate_message(target, persona, "verification_note", "ja-JP")
    assert t1 == t2


# ────────── _resolve_model 自动探测 ──────────

def test_resolve_model_picks_first_available(monkeypatch):
    """模拟 ollama /api/tags 返 qwen2.5:latest, 应该被命中"""
    # 重置缓存
    monkeypatch.setattr(pm, "_RESOLVED_MODEL", None)
    monkeypatch.setattr(pm, "_RESOLVED_AT_TS", 0.0)

    class _R:
        def read(self):
            return b'{"models":[{"name":"qwen2.5vl:7b"},{"name":"qwen2.5:latest"},{"name":"gemma4:latest"}]}'
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(pm.urllib.request, "urlopen",
                         lambda url, timeout=None: _R())
    m = pm._resolve_model()
    # qwen2.5:7b 不在列表, 但 qwen2.5:latest 在 → 优先级第 2
    assert m == "qwen2.5:latest"


def test_resolve_model_explicit_preferred_wins(monkeypatch):
    """显式传 preferred 且存在 → 直接用"""
    monkeypatch.setattr(pm, "_RESOLVED_MODEL", None)
    monkeypatch.setattr(pm, "_RESOLVED_AT_TS", 0.0)
    class _R:
        def read(self):
            return b'{"models":[{"name":"gemma4:latest"},{"name":"qwen2.5:latest"}]}'
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(pm.urllib.request, "urlopen",
                         lambda url, timeout=None: _R())
    assert pm._resolve_model(preferred="gemma4:latest") == "gemma4:latest"


def test_resolve_model_returns_none_when_ollama_down(monkeypatch):
    monkeypatch.setattr(pm, "_RESOLVED_MODEL", None)
    monkeypatch.setattr(pm, "_RESOLVED_AT_TS", 0.0)
    def fake_urlopen(*a, **kw):
        raise ConnectionRefusedError("ollama down")
    monkeypatch.setattr(pm.urllib.request, "urlopen", fake_urlopen)
    assert pm._resolve_model() is None


def test_resolve_model_skips_vlm_when_no_preference_match(monkeypatch):
    """ollama 上只有 vl/embed 模型时, 不应被选中"""
    monkeypatch.setattr(pm, "_RESOLVED_MODEL", None)
    monkeypatch.setattr(pm, "_RESOLVED_AT_TS", 0.0)
    class _R:
        def read(self):
            return b'{"models":[{"name":"qwen2.5vl:7b"},{"name":"nomic-embed-text:latest"}]}'
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(pm.urllib.request, "urlopen",
                         lambda url, timeout=None: _R())
    m = pm._resolve_model()
    # 没有 preference 命中, 也没有非 vl/embed 模型 → 返 None
    assert m is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
