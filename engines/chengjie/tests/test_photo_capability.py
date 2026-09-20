# -*- coding: utf-8 -*-
"""人设发图能力 SSOT 门禁（2026-07-31，photo_capability）。

守四条不变量（对应人设试聊实录事故：「我翻翻手机相册哈」连环空头支票）：
1. 能力判定纯函数矩阵：capabilities.photos **默认关**、两级开关都开才有效；
2. prompt 反向消费：人设块 photos 关 → 必带「没有发照片功能」约束，开 → 必无
   （与 capabilities.video_call 同款；full 探针另见 test_persona_prompt_chain）；
3. 出站消毒金标：截图三句必剥、回忆句/评论句不误伤；
4. 执行层接线：A 线五个媒体入口 + B 线 run_autosend_image + 试聊路由，
   源码级 wiring 扫描 + B 线行为验证——提示层说「发不了」时执行层真的一张都不发。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from src.companion.photo_capability import (
    NO_PHOTO_PERSONA_LINE,
    no_photo_constraint,
    persona_photos_enabled,
    photos_effective,
    sanitize_no_photo_reply,
    selfie_globally_enabled,
)

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


# ── 1. 纯函数矩阵 ────────────────────────────────────────────────────────────

def test_persona_photos_enabled_default_off():
    # 默认关是产品语义：无人设/缺字段/非 dict 一律关
    assert persona_photos_enabled(None) is False
    assert persona_photos_enabled({}) is False
    assert persona_photos_enabled({"name": "x"}) is False
    assert persona_photos_enabled({"capabilities": {}}) is False
    assert persona_photos_enabled({"capabilities": {"video_call": True}}) is False
    assert persona_photos_enabled({"capabilities": "notadict"}) is False
    # 显式 true 才开（truthy 归一）
    assert persona_photos_enabled({"capabilities": {"photos": True}}) is True
    assert persona_photos_enabled({"capabilities": {"photos": 1}}) is True
    assert persona_photos_enabled({"capabilities": {"photos": False}}) is False


def test_photos_effective_two_level_matrix():
    on_cfg = {"companion": {"selfie": {"enabled": True}}}
    off_cfg = {"companion": {"selfie": {"enabled": False}}}
    p_on = {"capabilities": {"photos": True}}
    p_off = {"capabilities": {"photos": False}}
    assert selfie_globally_enabled(on_cfg) is True
    assert selfie_globally_enabled(off_cfg) is False
    assert selfie_globally_enabled({}) is False
    assert selfie_globally_enabled(None) is False
    # 四象限：两级都开才有效
    assert photos_effective(on_cfg, p_on) is True
    assert photos_effective(on_cfg, p_off) is False
    assert photos_effective(off_cfg, p_on) is False
    assert photos_effective(off_cfg, p_off) is False
    assert photos_effective(on_cfg, None) is False


# ── 2. prompt 反向消费（compact；full 由 test_persona_prompt_chain 探针网守）──

def _compact(persona) -> str:
    from src.utils.persona_manager import PersonaManager
    return PersonaManager()._format_persona_compact(persona)


def test_compact_block_injects_constraint_when_photos_off():
    base = {"name": "探针小照", "role": "陪聊"}
    out = _compact(base)
    assert NO_PHOTO_PERSONA_LINE in out, "photos 缺省（关）时 compact 人设块必须带无发图约束"


def test_compact_block_removes_constraint_when_photos_on():
    base = {"name": "探针小照", "role": "陪聊",
            "capabilities": {"photos": True}}
    out = _compact(base)
    assert NO_PHOTO_PERSONA_LINE not in out, "photos 开时约束必须撤掉（否则与发图协议自相矛盾）"


def test_constraint_texts_allow_memories_forbid_promises():
    # 约束措辞不变量：禁的是「承诺/假装发送」，不禁「谈论照片回忆」——
    # 一刀切禁提照片会让人设躲闪（用户视角分析的核心边界）。
    for text in (NO_PHOTO_PERSONA_LINE, no_photo_constraint()):
        assert "找找" in text or "翻" in text          # 明令禁止翻找式承诺
        assert "这张" in text                           # 明令禁止假装已发
        assert "回忆" in text or "带过" in text          # 给出自然出路而非硬拒


# ── 3. 出站消毒金标（截图实录三连 + 不可误伤反例）────────────────────────────

def test_sanitize_screenshot_case_browse_album():
    # 实录：「有图嘛」→「有啊，我翻翻手机相册哈。那张是阿橘蹲在阳台晒太阳的侧影，特别好看。」
    raw = "有啊，我翻翻手机相册哈。那张是阿橘蹲在阳台晒太阳的侧影，特别好看。"
    out = sanitize_no_photo_reply(raw, media_context=True)
    assert "翻翻" not in out and "那张" not in out
    assert out.strip(), "全剥空必须换婉转兜底话术，不能空回复"


def test_sanitize_screenshot_case_looking_for_stock():
    # 实录：「我以前画过一套猫猫明信片，等我找找看有没有存图。」
    # 句级剥离语义：承诺子句与回忆子句同句（逗号不切句）→ 整句剥 + 兜底。
    raw = "真的吗？那我们可以聊好久啦～我以前画过一套猫猫明信片，等我找找看有没有存图。"
    out = sanitize_no_photo_reply(raw, media_context=True)
    assert "找找" not in out and "存图" not in out
    assert "聊好久" in out, "不含承诺的句子必须保留（整段换话术=过度剥离）"


def test_sanitize_screenshot_case_pretend_showing():
    # 实录：「哈哈那我找找看～ 这张是它刚来我家那天拍的，脏兮兮的小可怜样。」
    raw = "哈哈那我找找看～ 这张是它刚来我家那天拍的，脏兮兮的小可怜样。"
    out = sanitize_no_photo_reply(raw, media_context=True)
    assert "找找" not in out and "这张" not in out
    assert out.strip()


def test_sanitize_strips_photo_directive_markers():
    raw = "今天在画室待了一天呢\n[PHOTO selfie cozy art studio, warm light]"
    out = sanitize_no_photo_reply(raw, media_context=False)
    assert "[PHOTO" not in out and "画室" in out


def test_sanitize_keeps_memory_talk_and_comments():
    # 反例 1：纯回忆句（无承诺动词）绝不误伤——人味的一部分
    raw = "我以前画过一套猫猫明信片，还挺受欢迎的。"
    assert sanitize_no_photo_reply(raw, media_context=True) == raw
    # 反例 2：评论对方发来的图（无媒体语境）绝不误伤
    raw2 = "这张真好看，你拍的吗？"
    assert sanitize_no_photo_reply(raw2, media_context=False) == raw2
    # 反例 3：找的不是照片（同句无照片语境词）
    raw3 = "我找找看那家店在哪，回头带你去。"
    out3 = sanitize_no_photo_reply(raw3, media_context=False)
    assert "那家店" in out3


# ── 3b. 词表单元金标（新增模式的精确行为）───────────────────────────────────

def test_promise_wordlist_browse_patterns():
    from src.ai.outbound_promise_guard import detect_media_promise
    assert detect_media_promise("等我找找看有没有存图") == "image"
    assert detect_media_promise("我翻翻手机相册哈") == "image"
    assert detect_media_promise("let me look through my photos") == "image"
    assert detect_media_promise("let me find a picture for you") == "image"
    # 负向前瞻：图书馆/图纸不是照片
    assert detect_media_promise("我找一下图书馆的位置") == ""
    assert detect_media_promise("我找找看那家店在哪") == ""


def test_claim_wordlist_presenting_patterns():
    from src.ai.outbound_promise_guard import detect_media_claim
    assert detect_media_claim("这张是它刚来我家那天拍的", media_context=True) == "image"
    assert detect_media_claim(
        "那张是阿橘蹲在阳台晒太阳的侧影，特别好看", media_context=True) == "image"
    # 量词正常用法不误伤（是 不紧跟 张）
    assert detect_media_claim("这张桌子是我新买的", media_context=True) == ""
    # 真旧照引用（上次发过）不算谎
    assert detect_media_claim("上次发你的那张是在海边拍的", media_context=True) == ""
    # 无媒体语境一律不判（评论对方图的保护）
    assert detect_media_claim("这张是它刚来我家那天拍的", media_context=False) == ""


def test_claim_wordlist_bare_browse_needs_context():
    from src.ai.outbound_promise_guard import detect_media_claim
    # 光杆「那我找找看」只有媒体语境才定性（句尾锚定防带宾语句）
    assert detect_media_claim("哈哈那我找找看", media_context=True) == "image"
    assert detect_media_claim("哈哈那我找找看", media_context=False) == ""
    assert detect_media_claim("我找找看那家餐厅怎么走", media_context=True) == ""


def test_wants_media_bare_tu_request():
    from src.ai.outbound_promise_guard import wants_media
    assert wants_media("有图嘛") == "image"
    assert wants_media("有图吗？") == "image"
    assert wants_media("图呢") == "image"
    assert wants_media("附近有图书馆吗") == ""


# ── 4. 执行层接线（wiring）─────────────────────────────────────────────────

def _func_source(path: Path, func_name: str) -> str:
    """按缩进抠出方法源码段（def 到下一个同级 def）。"""
    src = path.read_text(encoding="utf-8")
    m = re.search(rf"\n(    |)(async )?def {func_name}\(", src)
    assert m, f"{path.name} 里找不到 {func_name}"
    start = m.start()
    indent = m.group(1)
    nxt = re.search(rf"\n{indent}(async )?def \w+\(", src[m.end():])
    end = m.end() + (nxt.start() if nxt else len(src) - m.end())
    return src[start:end]


def test_a_line_stage_gates_wired():
    """A 线五个媒体入口都必须过 prompt_photos_allowed（提示层说发不了 →
    执行层必须真的一张不发，两层永远一致）。"""
    sm = _ENGINE_ROOT / "src" / "skills" / "skill_manager.py"
    for fn in (
        "_handle_persona_media_request",   # Stage 0 注册相册
        "_handle_selfie_request",          # Stage A 自拍生成
        "_handle_contextual_image_request",  # Stage B 物体图
        "_photo_directive_selfie",         # [PHOTO] 指令执行
        "_async_fulfill_precheck",         # 承诺异步兑现预检
    ):
        seg = _func_source(sm, fn)
        assert "prompt_photos_allowed" in seg, f"{fn} 缺人设级发图闸"


def test_ai_client_prompt_wiring():
    """提示层：能力关必须注入 no_photo_constraint；旧「selfie 关就什么都不注入」
    的倒挂形态不得回潮。"""
    src = (_ENGINE_ROOT / "src" / "ai" / "ai_client.py").read_text(encoding="utf-8")
    assert "no_photo_constraint" in src
    assert "persona_photos_enabled" in src
    assert "_capability_hint_allowed" in src
    # 倒挂回潮哨兵：协议注入不得再是「elif 域 and 全局 hint」单闸形态
    assert "elif _is_companion and self._media_capability_hint_enabled():" not in src


def test_chat_test_route_wired():
    """试聊与生产同轨：[PHOTO] 剥离/占位 + 能力关消毒 + 能力状态回包。"""
    src = (_ENGINE_ROOT / "src" / "web" / "routes" /
           "chat_test_routes.py").read_text(encoding="utf-8")
    assert "sanitize_no_photo_reply" in src
    # P1：能力开走 extract_photo_directive（内含剥离）；能力关仍 sanitize
    assert ("strip_photo_directives" in src
            or "extract_photo_directive" in src)
    assert "photo_capability" in src
    assert "photo_preview" in src


def test_inbox_draft_path_sanitizes_when_photos_off():
    """收件箱草稿路径必须在能力关时消毒（P0 实施中发现的缺口：草稿不走
    process_message 的 promise_guard，空头承诺会进待审/自动发队列）。"""
    seg = _func_source(
        _ENGINE_ROOT / "src" / "skills" / "skill_manager.py",
        "generate_inbox_draft",
    )
    assert "sanitize_no_photo_reply" in seg
    assert "prompt_photos_allowed" in seg
    assert "strip_photo_directives" in seg


def test_b_line_run_autosend_image_gated(monkeypatch):
    """B 线行为验证：人设闸返回 False → 整条发图编排零动作（send_fn 不被碰）。"""
    import src.companion.photo_capability as pc
    from src.inbox.image_autosend import run_autosend_image

    seen = {}

    def fake_gate(pid):
        seen["pid"] = pid
        return False

    monkeypatch.setattr(pc, "persona_photos_enabled_by_id", fake_gate)
    calls = []

    async def send_fn(*a):  # pragma: no cover - 不该被调
        calls.append(a)
        return True

    cfg = {"companion": {"selfie": {"enabled": True}}}
    ok = asyncio.run(run_autosend_image(
        cfg, "telegram", "acct1", "chat1", "persona_x", "发张自拍呗",
        None, send_fn=send_fn,
        directive_override={"kind": "selfie", "scene": "beach"}))
    assert ok is False
    assert not calls, "人设闸关时任何发送路径（含 [PHOTO] 指令直通）都不得触发"
    assert seen.get("pid") == "persona_x", "闸必须按显式 persona_id 判"


def test_b_line_unknown_persona_defaults_off():
    """查不到的人设 id = 关（默认关语义的 B 线口径）。"""
    from src.companion.photo_capability import persona_photos_enabled_by_id
    assert persona_photos_enabled_by_id("__no_such_persona_probe__") is False
    assert persona_photos_enabled_by_id("") is False


def test_a_line_prompt_photos_allowed_default_off():
    """A 线口径：无绑定会话解析到域默认/兜底人设（无 capabilities）→ 关。"""
    from src.companion.photo_capability import prompt_photos_allowed
    assert prompt_photos_allowed({}) is False
    assert prompt_photos_allowed(None) is False
    assert prompt_photos_allowed({"account_persona_id": "__nope__"}) is False
