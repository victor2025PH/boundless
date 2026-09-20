"""渠道中心 UI ↔ 运行时契约门禁（常驻，防 UI 与运行时再漂移）。

三条契约（均为「UI 宣称 ≠ 运行时实况」类事故的回归网）:

(a) telegram 页语音 TTS 引擎卡片（#vr-be-cards）的 data-val 全集必须落在
    TTSPipeline 真实支持集内（出处 src/ai/tts_pipeline.py 的 backend 分派，
    见 876 行附近 ``backend in (...)`` 与 primary_backend 分支），且生产主力
    三卡 avatar_clone / edge_tts / openai 必须在场——防 UI 提供运行时根本
    不认识的引擎（选了静默回落）或藏掉真实主力引擎。

(b) whatsapp 页拟人节奏保存 payload 必须用运行时真读的键 ``split_mode``，
    不得再出现 JS 对象键形式的 ``split_strategy:``（历史漂移键——保存了运行时
    不读，UI 改了没效果）。回落读取表达式 ``hp.split_strategy||...`` 与注释里
    的提及放行（那是兼容读旧值，不是写錯键）。

(c) 四个渠道页模板 onclick="..." 属性值内不得出现弯引号
    U+201C/U+201D/U+2018/U+2019——「快照恢复按钮弯引号」类必坏 bug：弯引号
    提前/错位终结属性或进入 JS 源码，按钮点击即 SyntaxError/静默失效。

模板由并行工作流施工中；本门禁描述的是**终态契约**，兄弟改动未落地前对应
断言会红，属预期（主线收尾统一重跑）。
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "src" / "web" / "templates"
_TTS_PIPELINE = _ROOT / "src" / "ai" / "tts_pipeline.py"


def _read(name: str) -> str:
    return (_TEMPLATES / name).read_text(encoding="utf-8")


# ── (a) telegram TTS 引擎卡片 ⊆ TTSPipeline 真实支持集 ────────────────────────

# 必须在场的生产主力卡（UI 不得藏掉）
_TTS_UI_REQUIRED = {"avatar_clone", "edge_tts", "openai"}
# TTSPipeline 真实支持的克隆/云/在线 backend 全集（src/ai/tts_pipeline.py 分派）
_TTS_PIPELINE_SUPPORTED = {
    "avatar_clone", "edge_tts", "openai", "voice_clone_command",
    "minicpm_clone", "coqui_http", "elevenlabs", "voice_clone_lan",
}


def _element_block(text: str, elem_id: str) -> str:
    """按 id 取元素整块（开标签到配对闭标签），同名标签深度计数，不引 HTML 解析依赖。"""
    m = re.search(
        r"<(\w+)\b[^>]*\bid=[\"']" + re.escape(elem_id) + r"[\"'][^>]*>", text)
    assert m, f"模板里找不到 id={elem_id!r} 的元素"
    tag = m.group(1)
    depth = 0
    for t in re.finditer(rf"<{tag}\b|</{tag}\s*>", text[m.start():]):
        depth += 1 if not t.group(0).startswith("</") else -1
        if depth == 0:
            return text[m.start():m.start() + t.end()]
    raise AssertionError(f"id={elem_id!r} 的 <{tag}> 块未闭合")


def test_tts_universe_matches_pipeline_source():
    # 全集出处自证：每个成员都必须真实出现在 tts_pipeline.py 分派源码里，
    # 防 pipeline 改名/删 backend 后本门禁还拿着过期全集当真理。
    src = _TTS_PIPELINE.read_text(encoding="utf-8")
    missing = sorted(b for b in _TTS_PIPELINE_SUPPORTED if b not in src)
    assert not missing, f"全集成员在 tts_pipeline.py 里已不存在: {missing}"


def test_telegram_tts_backend_cards_within_pipeline_support():
    block = _element_block(_read("_channel_body_telegram.html"), "vr-be-cards")
    vals = set(re.findall(r"data-val=[\"']([^\"']+)[\"']", block))
    assert vals, "#vr-be-cards 块内没有任何 data-val 卡片"
    unknown = vals - _TTS_PIPELINE_SUPPORTED
    assert not unknown, (
        f"UI 提供了 TTSPipeline 不支持的引擎卡: {sorted(unknown)}"
        f"（支持集出处 src/ai/tts_pipeline.py backend 分派）")
    absent = _TTS_UI_REQUIRED - vals
    assert not absent, f"生产主力引擎卡缺席 #vr-be-cards: {sorted(absent)}"


# ── (b) whatsapp 保存 payload 用 split_mode，禁 split_strategy: 键 ────────────

def _mask_comments(text: str) -> str:
    """抹掉 HTML 注释 / JS 块注释 / JS 行注释（保 ``://`` 协议串不误伤）。"""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"(?<!:)//[^\n]*", " ", text)
    return text


# 键形式＝裸键 `split_strategy:` 或引号键 `"split_strategy":`；
# 前置负查（. 与 \w）放过读取表达式 `hp.split_strategy`（含三元 `?  x.split_strategy : y`）
# 与更长标识符 `foo_split_strategy`。
_SPLIT_KEY_RE = re.compile(r"(?<![.\w])['\"]?split_strategy['\"]?\s*:")


def test_whatsapp_pacing_payload_uses_split_mode():
    masked = _mask_comments(_read("_channel_body_whatsapp.html"))
    assert "split_mode" in masked, (
        "whatsapp 拟人节奏应使用运行时真读的键 split_mode（注释里提及不算）")
    bad = [
        f"line {masked.count(chr(10), 0, m.start()) + 1}: {m.group(0)!r}"
        for m in _SPLIT_KEY_RE.finditer(masked)
    ]
    assert not bad, (
        "保存 payload 仍在写运行时不读的对象键 split_strategy:（漂移键，保存无效）→ "
        + "; ".join(bad))


# ── (c) 渠道页 onclick 属性值零弯引号 ────────────────────────────────────────

_CURLY_QUOTES = {"\u201c", "\u201d", "\u2018", "\u2019"}
_CHANNELS = ("telegram", "whatsapp", "line", "messenger")


def _onclick_curly_violations(text: str) -> list:
    """onclick= 开引号到下一同款直引号之间（＝浏览器眼中的属性值）扫弯引号。

    弯引号不能终结 HTML 属性——一旦混入，属性值必然跨过它继续吞文本
    （telegram 快照恢复按钮实录：``onclick="restoreSnapshot('..')”>`` 弯引号
    伪装闭合，真实属性值吞到下一个直引号，按钮整只坏死）。
    """
    out = []
    for m in re.finditer(r"onclick\s*=\s*([\"'])", text):
        quote = m.group(1)
        start = m.end()
        end = text.find(quote, start)
        if end == -1:  # 属性未闭合本身就是坏的，扫到文件尾如实暴露
            end = len(text)
        value = text[start:end]
        hits = sorted({f"U+{ord(c):04X}" for c in value if c in _CURLY_QUOTES})
        if hits:
            line = text.count("\n", 0, m.start()) + 1
            out.append((line, hits, value[:90]))
    return out


def test_channel_body_onclick_no_curly_quotes():
    names = sorted(p.name for p in _TEMPLATES.glob("_channel_body_*.html"))
    expected = {f"_channel_body_{c}.html" for c in _CHANNELS}
    assert expected <= set(names), f"渠道页模板缺失: {sorted(expected - set(names))}"
    problems = []
    for name in names:  # 扫全部渠道页（未来新增渠道自动纳管）
        for line, hits, snippet in _onclick_curly_violations(_read(name)):
            problems.append(f"{name}:{line} {'/'.join(hits)} onclick 值≈{snippet!r}")
    assert not problems, "onclick 属性值含弯引号（必坏 bug）:\n" + "\n".join(problems)


# ── (d) telegram ASR 识别引擎卡片 ⊆ voice_transcriber 工厂真实支持集 ─────────
#
# 与 (a) 同款「UI 宣称 ≠ 运行时实况」防线：#asr-be-cards 的 data-val 全集必须
# 落在 VoiceTranscriberFactory._create_one 真实分派集内（出处
# src/voice_transcriber.py 的 provider 分支，见 ``_create_one`` 620 行附近），
# 且生产主力卡 sensevoice / faster_whisper / openai 必须在场——防 UI 提供工厂
# 根本不认识的引擎（保存后静默落回默认 whisper_local）或藏掉真实主力引擎。

_VOICE_TRANSCRIBER = _ROOT / "src" / "voice_transcriber.py"
# 必须在场的生产主力卡（UI 不得藏掉；sensevoice=本机方言主力，openai=兼容端点族）
_ASR_UI_REQUIRED = {"sensevoice", "faster_whisper", "openai"}
# 工厂真实支持的 provider 全集（src/voice_transcriber.py::_create_one 分派，
# 含别名：qwen3_asr/funasr_api/openai_compatible→OpenAI 契约、
# sense_voice/funasr→SenseVoice、avatarhub→AvatarWhisper）
_ASR_FACTORY_SUPPORTED = {
    "openai", "qwen3_asr", "funasr_api", "openai_compatible",
    "sensevoice", "sense_voice", "funasr",
    "avatar_whisper", "avatarhub",
    "faster_whisper", "whisper_local",
}


def test_asr_universe_matches_transcriber_source():
    # 全集出处自证：每个成员都必须以带引号字面量真实出现在 voice_transcriber.py
    # 分派源码里，防工厂改名/删 provider 后本门禁还拿着过期全集当真理。
    src = _VOICE_TRANSCRIBER.read_text(encoding="utf-8")
    missing = sorted(
        p for p in _ASR_FACTORY_SUPPORTED
        if f"'{p}'" not in src and f'"{p}"' not in src
    )
    assert not missing, f"全集成员在 voice_transcriber.py 里已不存在: {missing}"


def test_telegram_asr_provider_cards_within_factory_support():
    block = _element_block(_read("_channel_body_telegram.html"), "asr-be-cards")
    vals = set(re.findall(r"data-val=[\"']([^\"']+)[\"']", block))
    assert vals, "#asr-be-cards 块内没有任何 data-val 卡片"
    unknown = vals - _ASR_FACTORY_SUPPORTED
    assert not unknown, (
        f"UI 提供了 voice_transcriber 工厂不支持的识别引擎卡: {sorted(unknown)}"
        f"（支持集出处 src/voice_transcriber.py::_create_one 分派）")
    absent = _ASR_UI_REQUIRED - vals
    assert not absent, f"生产主力识别引擎卡缺席 #asr-be-cards: {sorted(absent)}"


# ── (e) telegram 试听异步任务契约（提交 background + 轮询 tts-test-jobs）────
#
# 与异步后端的接口契约钉子：POST /api/voice/tts-test 提交须带 background:true
# （新后端秒回 job_id），随后轮询 GET /api/voice/tts-test-jobs/{job_id} 直到
# done/error。三个静态特征缺一即契约被删/被改（后端异步化后前端将退回
# 「同步长等 + 15s 提前 abort」的坏态）。

def test_telegram_tts_test_async_contract_pinned():
    text = _read("_channel_body_telegram.html")
    assert re.search(r"\bbackground\s*[:=]\s*true\b", text), (
        "试听提交 body 缺 background:true（异步任务化契约被删）")
    assert "/api/voice/tts-test-jobs/" in text, (
        "缺 GET /api/voice/tts-test-jobs/{job_id} 轮询路径（异步任务化契约被删）")
    assert re.search(r"\bjob_id\b", text), (
        "试听响应不再识别 job_id（异步任务化契约被删）")


# ── (f) telegram ASR 级联链只读展示行在场 ────────────────────────────────────
#
# voice_recognition.fallback（yaml 高级配置，PUT voice-asr 白名单不含它）曾在
# UI 完全隐身——运营看页面以为只有单引擎。#asr-cascade-line 是它的只读可见性
# 保证，删掉=级联再度隐身。

def test_telegram_asr_cascade_line_present():
    text = _read("_channel_body_telegram.html")
    assert re.search(r"id=[\"']asr-cascade-line[\"']", text), (
        "缺 #asr-cascade-line 识别级联只读展示行")
