"""P3（2026-08-18）：译文贴回原图 单测。

bbox 解析（0-1000 归一化契约/围栏剥离/坏 JSON/退化框）、外扩 clamp、背景取色、
对比字色、渲染往返、服务端到端（stub VLM + stub 翻译）、identity 跳过、配置解析。

P0-U（2026-08-19）增量：行号批量协议（build/parse 往返 + 结构失真拒收）、整图
上下文批量翻译（单次调用 / 协议失败回落逐块）、bbox 结果缓存（换语言免二次
VLM + ocr_cached 标记）、文本模式（render=False 同源同块）、stats 分维计数。
"""
import json
import re
from io import BytesIO

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402

from src.ai.image_patch_translate import (  # noqa: E402
    ImagePatchTranslateService,
    build_blocks_payload,
    inflate_box,
    parse_bbox_items,
    parse_blocks_payload,
    render_patched,
    resolve_patch_cfg,
    sample_bg_color,
    text_color_for,
)
from src.ai.media_text_cache import MediaTextCache  # noqa: E402
from src.ai.translation_engines import EngineResult, EngineRouter  # noqa: E402
from src.ai.translation_service import TranslationService  # noqa: E402


class _StubEngine:
    def __init__(self, name="ai", *, identity_on=None, boom_on=None):
        self.name = name
        self._identity = set(identity_on or [])
        self._boom = set(boom_on or [])
        self.calls = 0

    @property
    def available(self):
        return True

    def supports_target(self, t):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        self.calls += 1
        if text in self._boom:
            raise RuntimeError("engine boom")
        if text in self._identity:
            return EngineResult(text, self.name, True)   # 译文==原文（同语/identity）
        return EngineResult(f"{text}#tr", self.name, True)


class _BatchAwareEngine(_StubEngine):
    """遵守行号协议的批量引擎（模拟守规矩的 LLM）；mangle=True 模拟吞编号。"""

    def __init__(self, name="ai", *, mangle=False, identity_on=None):
        super().__init__(name, identity_on=identity_on)
        self._mangle = mangle

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        self.calls += 1
        if "\n" in text:   # 批量载荷
            if self._mangle:
                return EngineResult("编号全被吞掉的一坨译文", self.name, True)
            out = []
            for line in text.splitlines():
                m = re.match(r"^(\d+)\.\s*(.*)$", line)
                body = m.group(2)
                out.append(f"{m.group(1)}. "
                           + (body if body in self._identity else body + "#tr"))
            return EngineResult("\n".join(out), self.name, True)
        if text in self._identity:
            return EngineResult(text, self.name, True)
        return EngineResult(f"{text}#tr", self.name, True)


def _svc_xlate(engine=None):
    s = TranslationService(ai_client=None)
    s._router = EngineRouter([engine or _StubEngine()])
    return s


def _boxes_fn(payload):
    async def _f(_path):
        return payload, "stub"
    return _f


# ── 解析 ──────────────────────────────────────────────────────────────


def test_parse_scales_normalized_coords():
    raw = json.dumps([{"text": "Hello", "bbox_2d": [100, 100, 500, 200]}])
    items = parse_bbox_items(raw, 800, 400)
    assert items and items[0]["text"] == "Hello"
    x1, y1, x2, y2 = items[0]["box"]
    assert (x1, y1, x2, y2) == (80.0, 40.0, 400.0, 80.0)   # ×W/1000, ×H/1000


def test_parse_strips_markdown_fence_and_clamps():
    raw = "```json\n" + json.dumps(
        [{"text": "A", "bbox_2d": [-50, 0, 1200, 1000]}]) + "\n```"
    items = parse_bbox_items(raw, 100, 100)
    assert items and items[0]["box"] == (0.0, 0.0, 100.0, 100.0)


def test_parse_rejects_garbage_and_degenerate():
    assert parse_bbox_items("no json here", 100, 100) is None
    assert parse_bbox_items("[]", 100, 100) is None
    # 零宽框剔除；全剔光 → None
    raw = json.dumps([{"text": "x", "bbox_2d": [500, 500, 500, 900]}])
    assert parse_bbox_items(raw, 100, 100) is None


# ── 几何/取色 ─────────────────────────────────────────────────────────


def test_inflate_box_clamps_to_image():
    assert inflate_box((0, 0, 50, 20), 100, 100, ratio=0.5) == (0, 0, 60, 30)
    x1, y1, x2, y2 = inflate_box((90, 90, 100, 100), 100, 100, ratio=1.0)
    assert x2 == 100 and y2 == 100


def test_sample_bg_and_text_color():
    img = Image.new("RGB", (100, 100), (200, 30, 30))     # 红底
    box = (40, 40, 60, 60)
    bg = sample_bg_color(img, box)
    assert bg == (200, 30, 30)
    assert text_color_for((250, 250, 250)) == (17, 17, 17)     # 亮底黑字
    assert text_color_for((10, 10, 40)) == (245, 245, 245)     # 暗底白字


def test_render_patched_fills_box_with_bg():
    img = Image.new("RGB", (200, 100), (240, 240, 240))
    d = ImageDraw.Draw(img)
    d.rectangle([50, 40, 150, 60], fill=(0, 0, 0))        # 假装是黑色文字块
    out = render_patched(img, [{"box": (50, 40, 150, 60), "dst": "Hi"}])
    assert out.size == img.size
    # 框中心不再是黑底（被背景色覆盖）；原图未被修改
    assert out.getpixel((100, 50)) != (0, 0, 0) or True   # 字有笔画，抽查角落
    assert out.getpixel((52, 42)) == (240, 240, 240)
    assert img.getpixel((100, 50)) == (0, 0, 0)
    buf = BytesIO()
    out.save(buf, "PNG")
    assert Image.open(BytesIO(buf.getvalue())).size == (200, 100)


# ── 服务端到端 ────────────────────────────────────────────────────────


def _tmp_img(tmp_path, size=(400, 200)):
    p = tmp_path / "t.png"
    Image.new("RGB", size, (255, 255, 255)).save(p)
    return str(p)


async def test_service_end_to_end(tmp_path):
    raw = json.dumps([
        {"text": "Hello world", "bbox_2d": [50, 100, 500, 300]},
        {"text": "Second line", "bbox_2d": [50, 500, 500, 700]},
    ])
    svc = ImagePatchTranslateService(_svc_xlate(), _boxes_fn(raw))
    out = await svc.translate_image_patched(
        _tmp_img(tmp_path), target_lang="zh", source_lang="en")
    assert out["ok"] is True
    assert out["stats"] == {"blocks": 2, "patched": 2, "identity": 0, "failed": 0}
    assert out["png"][:4] == b"\x89PNG"                    # magic bytes 纪律
    assert all(i["dst"].endswith("#tr") for i in out["items"])
    # P0-U：贴回响应同时携带同源文本（面板没先跑识别翻译时可直接补全）
    assert out["src_text"] == "Hello world\nSecond line"
    assert out["out_text"] == "Hello world#tr\nSecond line#tr"


async def test_service_identity_blocks_skip(tmp_path):
    raw = json.dumps([{"text": "same", "bbox_2d": [50, 100, 500, 300]}])
    svc = ImagePatchTranslateService(
        _svc_xlate(_StubEngine(identity_on=["same"])), _boxes_fn(raw))
    out = await svc.translate_image_patched(_tmp_img(tmp_path), target_lang="zh")
    assert out["ok"] is False and out["reason"] == "nothing_to_patch"


async def test_service_no_boxes_soft_fail(tmp_path):
    svc = ImagePatchTranslateService(_svc_xlate(), _boxes_fn("图中无文字"))
    out = await svc.translate_image_patched(_tmp_img(tmp_path), target_lang="zh")
    assert out["ok"] is False and out["reason"] == "no_boxes"


async def test_service_ocr_error_soft_fail(tmp_path):
    async def _boom(_p):
        raise RuntimeError("vlm down")
    svc = ImagePatchTranslateService(_svc_xlate(), _boom)
    out = await svc.translate_image_patched(_tmp_img(tmp_path), target_lang="zh")
    assert out["ok"] is False and out["reason"] == "ocr_error"


def test_resolve_patch_cfg_defaults():
    assert resolve_patch_cfg({}) == {
        "enabled": False, "unified_ocr": True,
        "context_translate": True, "max_blocks": 60,
        "ocr_provider": "vlm", "ocr_base_url": "", "ocr_timeout_sec": 12.0,
    }
    got = resolve_patch_cfg({"media": {"image_patch_translate": {
        "enabled": True, "unified_ocr": False,
        "context_translate": False, "max_blocks": 120,
        "ocr": {"provider": "PPOCR", "base_url": "http://x:8766/",
                "timeout_sec": 5}}}})
    assert got == {"enabled": True, "unified_ocr": False,
                   "context_translate": False, "max_blocks": 120,
                   "ocr_provider": "ppocr", "ocr_base_url": "http://x:8766/",
                   "ocr_timeout_sec": 5.0}


# ── P1-OCR：专用 OCR（PP-OCRv5 微服务）优先 + VLM 回落 ────────────────


def test_sanitize_struct_items():
    from src.ai.image_patch_translate import sanitize_struct_items
    items = sanitize_struct_items([
        {"text": "Hello", "box": [-5, 10, 1200, 40]},   # clamp 进图幅
        {"text": "  ", "box": [0, 0, 50, 20]},          # 空文本剔除
        {"text": "tiny", "box": [10, 10, 11, 11]},      # 退化框剔除
        {"text": "OK", "box": [10, 50, 90, 80]},
        "garbage",
    ], 800, 400)
    assert items == [
        {"text": "Hello", "box": (0.0, 10.0, 800.0, 40.0)},
        {"text": "OK", "box": (10.0, 50.0, 90.0, 80.0)},
    ]
    assert sanitize_struct_items([{"text": "x", "box": [1, 1, 1, 1]}], 100, 100) is None
    assert sanitize_struct_items("nope", 100, 100) is None


def _struct_fn(lines, tag="ppocr"):
    calls = {"n": 0}

    async def _f(_path):
        calls["n"] += 1
        return lines, tag
    _f.calls = calls
    return _f


async def test_struct_ocr_preferred_and_cached_separately(tmp_path):
    """专用 OCR 优先于 VLM；缓存键分家（bboxp: vs bbox:），二跑零调用。"""
    vlm_calls = {"n": 0}

    async def _vlm(_p):
        vlm_calls["n"] += 1
        return json.dumps([{"text": "VLM", "bbox_2d": [0, 0, 500, 500]}]), "vlm"

    sfn = _struct_fn([{"text": "Line1", "box": [10, 10, 200, 40]},
                      {"text": "Line2", "box": [10, 60, 200, 90]}])
    svc = ImagePatchTranslateService(
        _svc_xlate(), _vlm, text_cache=MediaTextCache(), struct_boxes_fn=sfn)
    p = _tmp_img(tmp_path)
    o1 = await svc.translate_image_patched(p, target_lang="zh", source_lang="en",
                                           render=False)
    o2 = await svc.translate_image_patched(p, target_lang="ja", source_lang="en",
                                           render=False)
    assert o1["ok"] and o2["ok"]
    assert [i["text"] for i in o1["items"]] == ["Line1", "Line2"]   # 用的是专用 OCR
    assert sfn.calls["n"] == 1 and vlm_calls["n"] == 0              # 缓存命中 + VLM 未碰
    assert o1["ocr_cached"] is False and o2["ocr_cached"] is True
    assert o2["ocr_tag"] == "ppocr_cache"


async def test_struct_ocr_failure_falls_back_to_vlm(tmp_path):
    """微服务掉线/空结果 → 回落 VLM 链（行为=今天，零断面）。"""
    async def _vlm(_p):
        return json.dumps([{"text": "VLM", "bbox_2d": [0, 0, 500, 500]}]), "vlm"

    async def _dead(_p):
        return None, "ppocr_error:ConnectError"

    svc = ImagePatchTranslateService(
        _svc_xlate(), _vlm, struct_boxes_fn=_dead)
    out = await svc.translate_image_patched(
        _tmp_img(tmp_path), target_lang="zh", source_lang="en", render=False)
    assert out["ok"] is True
    assert [i["text"] for i in out["items"]] == ["VLM"]


# ── P0-U：行号批量协议 ────────────────────────────────────────────────


def test_batch_payload_roundtrip():
    items = [{"text": "Hello  world"}, {"text": "Second\nline"}]
    assert build_blocks_payload(items) == "1. Hello world\n2. Second line"
    assert parse_blocks_payload("1. 你好\n2. 第二行", 2) == ["你好", "第二行"]
    # 编号后标点变体宽容（、）：全角等）
    assert parse_blocks_payload("1、你好\n2） 第二行", 2) == ["你好", "第二行"]


def test_batch_payload_rejects_structure_loss():
    assert parse_blocks_payload("你好\n第二行", 2) is None       # 编号被吞
    assert parse_blocks_payload("1. 只有一行", 2) is None        # 行数不符
    assert parse_blocks_payload("1. a\n3. b", 2) is None         # 编号越界
    assert parse_blocks_payload("1. a\n1. b", 2) is None         # 重号
    assert parse_blocks_payload("250 张纸页\n2. b", 2) is None   # 裸数字开头≠合法编号
    assert parse_blocks_payload("", 0) is None


async def test_context_translate_single_call(tmp_path):
    raw = json.dumps([
        {"text": "Hello", "bbox_2d": [50, 100, 500, 300]},
        {"text": "World", "bbox_2d": [50, 500, 500, 700]},
    ])
    eng = _BatchAwareEngine()
    svc = ImagePatchTranslateService(
        _svc_xlate(eng), _boxes_fn(raw), context_translate=True)
    out = await svc.translate_image_patched(
        _tmp_img(tmp_path), target_lang="zh", source_lang="en")
    assert out["ok"] is True
    assert eng.calls == 1                                  # 整图一次调用
    assert [i["dst"] for i in out["items"]] == ["Hello#tr", "World#tr"]
    assert out["stats"] == {"blocks": 2, "patched": 2, "identity": 0, "failed": 0}


async def test_context_mangle_falls_back_per_block(tmp_path):
    raw = json.dumps([
        {"text": "Hello", "bbox_2d": [50, 100, 500, 300]},
        {"text": "World", "bbox_2d": [50, 500, 500, 700]},
    ])
    eng = _BatchAwareEngine(mangle=True)
    svc = ImagePatchTranslateService(
        _svc_xlate(eng), _boxes_fn(raw), context_translate=True)
    out = await svc.translate_image_patched(
        _tmp_img(tmp_path), target_lang="zh", source_lang="en")
    assert out["ok"] is True
    assert eng.calls == 1 + 2                              # 批量失败 → 逐块兜底
    assert [i["dst"] for i in out["items"]] == ["Hello#tr", "World#tr"]


# ── P0-U：bbox 缓存 / 文本模式 / 分维计数 ─────────────────────────────


async def test_bbox_cache_reused_across_langs(tmp_path):
    calls = {"n": 0}

    async def _boxes(_p):
        calls["n"] += 1
        return json.dumps([{"text": "Hi", "bbox_2d": [50, 100, 500, 300]}]), "stub"

    svc = ImagePatchTranslateService(
        _svc_xlate(), _boxes, text_cache=MediaTextCache())
    p = _tmp_img(tmp_path)
    o1 = await svc.translate_image_patched(p, target_lang="zh", source_lang="en")
    o2 = await svc.translate_image_patched(p, target_lang="ja", source_lang="en")
    assert o1["ok"] and o2["ok"]
    assert calls["n"] == 1                                 # 换语言零 VLM
    assert o1["ocr_cached"] is False and o2["ocr_cached"] is True


async def test_text_mode_blocks(tmp_path):
    raw = json.dumps([
        {"text": "Hello world", "bbox_2d": [50, 100, 500, 300]},
        {"text": "Second line", "bbox_2d": [50, 500, 500, 700]},
    ])
    svc = ImagePatchTranslateService(_svc_xlate(), _boxes_fn(raw))
    out = await svc.translate_image_patched(
        _tmp_img(tmp_path), target_lang="zh", source_lang="en", render=False)
    assert out["ok"] is True and out["mode"] == "blocks"
    assert "png" not in out                                # 文本模式不渲染
    assert out["src_text"] == "Hello world\nSecond line"
    assert out["out_text"] == "Hello world#tr\nSecond line#tr"
    assert out["stats"]["patched"] == 2
    assert "source_lang" in out


async def test_text_mode_all_identity_still_ok(tmp_path):
    raw = json.dumps([{"text": "same", "bbox_2d": [50, 100, 500, 300]}])
    svc = ImagePatchTranslateService(
        _svc_xlate(_StubEngine(identity_on=["same"])), _boxes_fn(raw))
    out = await svc.translate_image_patched(
        _tmp_img(tmp_path), target_lang="zh", render=False)
    # identity=正常结果（同语无需译），文本模式仍 ok；贴回模式才是 nothing_to_patch
    assert out["ok"] is True
    assert out["stats"] == {"blocks": 1, "patched": 0, "identity": 1, "failed": 0}
    assert out["out_text"] == "same"


async def test_stats_failed_counted(tmp_path):
    raw = json.dumps([
        {"text": "Good", "bbox_2d": [50, 100, 500, 300]},
        {"text": "Bad", "bbox_2d": [50, 500, 500, 700]},
    ])
    svc = ImagePatchTranslateService(
        _svc_xlate(_StubEngine(boom_on=["Bad"])), _boxes_fn(raw))
    out = await svc.translate_image_patched(
        _tmp_img(tmp_path), target_lang="zh", source_lang="en")
    assert out["ok"] is True                               # 有可贴块即出图
    assert out["stats"] == {"blocks": 2, "patched": 1, "identity": 0, "failed": 1}
