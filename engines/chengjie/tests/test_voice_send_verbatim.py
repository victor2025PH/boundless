# -*- coding: utf-8 -*-
"""坐席语音链「原文直念 + 所听即所发」接线门禁（2026-08-10 实录事故）。

事故：坐席手打「你晚上吃饭了吗，晚上有什么安排」，送 TTS 前被 LLM 口语化层
改写成**对这句话的回答**（「你呢吃了吗，晚上打算随便弄点东西吃…」）后念出
——气泡 caption=原文、音频=另一句话，坐席不点开听根本发现不了。

修复三件套（本门禁逐一钉住，参数被摘掉即红）：
  1. send-voice 合成传 ``pre_colloquialized=True``（手打文字＝最终意图，整段
     跳过改写链）+ ``interactive=True``（hub 候选封顶 + 豁免开场词剥词）+
     ``total_budget_sec``（防链内超时之和越过前端 60s 等待线）；
  2. tts-test 与 send-voice **全同参**（试听=发送契约：听到的字=发出的字）；
  3. 主输入框 sendVoiceReply 带回试听产物 ``preview_filename``（服务端复用
     校验 2026-08-05 起就绪，此前前端一直没接——试听与发送是两次独立合成）。

路由行为正路依赖在线协议账号与真 TTS 后端，不在单测打真外呼 → 用源码断言钉
接线（与「静态接线不得再被 if 包住」同款模式）；管线侧不变量（interactive
候选封顶 / 缓存键改写变体维度）同钉。
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _synth_call(src: str, anchor: str) -> str:
    """取 anchor 之后第一个 tts.synthesize(...) 调用的完整实参段（括号配平）。"""
    i = src.index(anchor)
    j = src.index("tts.synthesize(", i)
    k = j + len("tts.synthesize(")
    depth = 1
    while depth and k < len(src):
        ch = src[k]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        k += 1
    assert depth == 0, f"anchor {anchor!r} 后的 tts.synthesize 括号不配平"
    return src[j:k]


def test_send_voice_synthesizes_verbatim_interactive_budgeted():
    src = _read("src/web/routes/unified_inbox_send_routes.py")
    call = _synth_call(src, "async def api_unified_inbox_send_voice")
    assert "pre_colloquialized=True" in call, "手打文字必须整段跳过口语化改写链"
    assert "interactive=True" in call, "坐席在等：hub 候选封顶 + 豁免开场词剥词"
    assert "total_budget_sec" in call, "无总预算＝各级超时可堆到前端 60s 线之外"


def test_tts_test_matches_send_voice_params():
    """试听=发送契约：tts-test 与 send-voice 同 pre_colloquialized+interactive，
    试听产物被「所听即所发」复用时零分叉。"""
    src = _read("src/web/routes/voice_routes.py")
    call = _synth_call(src, "async def _run_tts_preview")
    assert "pre_colloquialized=True" in call
    assert "interactive=True" in call
    assert "total_budget_sec" in call


def test_composer_send_carries_preview_filename():
    """主输入框「所听即所发」前端接线：genVoiceReply 暂存试听产物，
    sendVoiceReply 在会话/文本/音色未变时带 preview_filename。"""
    html = _read("src/web/templates/unified_inbox.html")
    gi = html.index("async function genVoiceReply")
    gseg = html[gi:gi + 4000]
    assert "_voicePreviewReuse" in gseg, "试听成功后必须暂存 filename"
    si = html.index("async function sendVoiceReply")
    sseg = html[si:si + 4000]
    assert "preview_filename" in sseg, "发送必须带回试听产物（复用校验在服务端）"
    assert "_vpConvKey()" in sseg, "复用必须校验会话未切换（防串会话发音频）"


def test_pipeline_interactive_caps_hub_candidates():
    src = _read("src/ai/tts_pipeline.py")
    seg = src[src.index("async def _try_hub_fish"):]
    seg = seg[:seg.index("async def ", 10)]
    assert "best_of_interactive" in seg
    assert "if interactive:" in seg, "交互式必须封顶 hub 候选数"


def test_pipeline_cache_key_has_rewrite_variant():
    """原文直念与改写链产出的不是同一份音频：缓存键必须分变体，
    否则手动「原文直念」会命中自动链缓存的「改过词」音频（穿帮借尸还魂）。"""
    src = _read("src/ai/tts_pipeline.py")
    assert 'variant=("verbatim" if pre_colloquialized else "")' in src
    seg = src[src.index("def _cache_key"):]
    seg = seg[:seg.index("def ", 10)]
    assert "variant" in seg


def test_pipeline_opener_dedupe_exempts_interactive():
    """开场词去重剥词只作用于机器产文——手打文字「所打即所念」一个字不动。"""
    src = _read("src/ai/tts_pipeline.py")
    m = re.search(
        r"if \(self\.variety_key and colloquial_lead and not interactive", src)
    assert m, "opener_dedupe 必须豁免 interactive（坐席手动链）"
