"""P3（2026-08-18）：译文贴回原图（图片补丁翻译）。

链路：VLM 带坐标 OCR（qwen-vl 系 **0-1000 归一化 bbox** 契约，spike 实测见
tmp_bbox_spike/）→ 逐块翻译（复用 TranslationService：术语/缓存/引擎路由）→
PIL 回绘（框外扩 + 背景取色 + 亮度对比字色 + 缩字适配）。

P0-U（2026-08-19）三项增量（灯箱语言选择器配套，路线图见当日方案）：
- **bbox 结果缓存**（``text_cache`` 注入，键 ``bbox:{sha1}``）：换目标语重译 /
  先「识别翻译」后「生成译文图」都复用同一次 VLM 坐标 OCR——语言切换从
  「再烧一遍 VLM」变成「只重翻译+重渲染」。刻意只缓存**解析成功**的原始输出
  （坏 JSON 缓存会把失败钉死一小时）。
- **整图上下文批量翻译**（``context_translate``，调用方显式开启）：全部文字块
  编号合并成一次翻译调用——包装/招牌类碎片短语（"50 PULLS x 5 PLY"）孤立逐块
  翻译最容易译飞，同一请求内互为上下文。行号协议解析不回来（引擎吞编号/并行
  改写）→ **整批回落逐块路径**，宁可多一次调用不接受错位。
- **文本模式**（``render=False``）：同一条 bbox 管线产出「识别原文/译文」文本
  （面板消费），与译文图同源同块——消除「面板一套 OCR、贴回另一套」的分叉。
  stats 增 identity/failed 维度，前端可如实展示「识别 N 处 · 已译 M 处」。

诚实边界（与「媒体产物验证纪律」同源）：
- VLM bbox 是近似框：回绘=「背景色填充+重写」，不做像素级 inpaint。纯色底截图
  （聊天记录/商品详情/表单）效果好；照片上的字/渐变底会露怯——前端必须保留
  「看原图」切换，坐席核对权不可剥夺。
- 译文==原文（identity/同语）的块**跳过重绘**：白涂一块再写一遍原文是纯破坏。
- 全链软失败：bbox 解析不出 / 无字 / 渲染异常 → ok=False + reason，绝不抛上层。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from io import BytesIO
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 与 OCR_PROMPT（纯文本）并列的带坐标变体；0-1000 归一化是 qwen-vl 训练态的
# 原生输出习惯（spike 双轮字节级一致），显式写进 prompt 钉住契约。
BBOX_PROMPT = (
    "识别图中所有文字块。只输出 JSON 数组，不要任何解释、不要 markdown 代码块："
    '[{"text":"文字内容","bbox_2d":[x1,y1,x2,y2]}]。'
    "坐标为 0-1000 归一化整数，(x1,y1)=左上角，(x2,y2)=右下角。"
)

_MAX_BLOCKS = 60          # 单图文字块上限（超出=海报级密集排版，回绘必花，不如不做）
_INFLATE_RATIO = 0.12     # 框外扩比例：spike 实测框稍紧留原字残边，外扩吃掉残边
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑（CJK+拉丁全覆盖）
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

# boxes_fn: async (image_path) -> (raw_text|None, tag)——与 image_translate.OcrFn 同形，
# 换 BBOX_PROMPT 即可复用 VisionClient 全套故障转移管道。
BoxesFn = Callable[[str], Awaitable[Tuple[Optional[str], str]]]
# struct_boxes_fn（P1-OCR 2026-08-19）: async (image_path) -> (items|None, tag)，items
# 已是 [{text, box:(x1,y1,x2,y2) 像素}]——专用 OCR（PP-OCRv5 微服务）返回结构化行框，
# 不经 VLM JSON 近似解析。业界实证（CVPR 2026 PP-OCRv5/v6 论文）：通用 VLM 文字
# 定位 Hmean 比专用检测模型低 ~39pp——「译文图漏块/盖偏」的结构性根因在此。
StructBoxesFn = Callable[[str], Awaitable[Tuple[Optional[List[Dict[str, Any]]], str]]]


def sanitize_struct_items(
    items: Any, img_w: int, img_h: int,
) -> Optional[List[Dict[str, Any]]]:
    """专用 OCR 返回的结构化行框 → 与 parse_bbox_items 同形的安全 items。

    clamp 进图幅、剔退化框（<2px）与空文本；坐标容忍 list/tuple 混形。
    全剔光 → None（调用方回落 VLM 链）。
    """
    if not isinstance(items, list):
        return None
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        txt = str(it.get("text") or "").strip()
        bb = it.get("box") or it.get("bbox") or []
        if not txt or not isinstance(bb, (list, tuple)) or len(bb) != 4:
            continue
        try:
            x1, y1, x2, y2 = (float(v) for v in bb)
        except Exception:
            continue
        x1, x2 = sorted((max(0.0, min(x1, img_w)), max(0.0, min(x2, img_w))))
        y1, y2 = sorted((max(0.0, min(y1, img_h)), max(0.0, min(y2, img_h))))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        out.append({"text": txt, "box": (x1, y1, x2, y2)})
    return out or None


def parse_bbox_items(raw: str, img_w: int, img_h: int) -> Optional[List[Dict[str, Any]]]:
    """解析 VLM 返回的 bbox JSON → [{text, box:(x1,y1,x2,y2) 像素}]；解析不出 → None。

    坐标按 0-1000 归一化契约缩放到像素（spike 实测 qwen3-vl 恒为归一化输出）；
    越界值 clamp 进图幅；退化框（零宽/零高）剔除。
    """
    s = str(raw or "").strip()
    if not s:
        return None
    s = re.sub(r"^```(json)?|```$", "", s, flags=re.M).strip()
    m = re.search(r"\[.*\]", s, re.S)
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(arr, list):
        return None
    out: List[Dict[str, Any]] = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        txt = str(it.get("text") or "").strip()
        bb = it.get("bbox_2d") or it.get("bbox") or []
        if not txt or not isinstance(bb, list) or len(bb) != 4:
            continue
        try:
            x1, y1, x2, y2 = (float(v) for v in bb)
        except Exception:
            continue
        # 0-1000 归一化 → 像素
        x1, x2 = x1 * img_w / 1000.0, x2 * img_w / 1000.0
        y1, y2 = y1 * img_h / 1000.0, y2 * img_h / 1000.0
        x1, x2 = sorted((max(0.0, min(x1, img_w)), max(0.0, min(x2, img_w))))
        y1, y2 = sorted((max(0.0, min(y1, img_h)), max(0.0, min(y2, img_h))))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        out.append({"text": txt, "box": (x1, y1, x2, y2)})
    return out or None


# ── P0-U：整图上下文批量翻译的行号协议 ─────────────────────────────────
# 载荷=纯编号行（不带指令——指令会被 NMT 引擎当正文翻译）；解析端宽容编号后的
# 标点变体（. ． 、 ) ） : ：），但对「行数不符 / 编号越界 / 重号 / 无编号行」
# 一律判失败让调用方回落逐块——错位贴回比不贴更糟。
_BATCH_LINE_RE = re.compile(r"^\s*(\d+)\s*[.．、)）:：]?\s*(.*)$")


def build_blocks_payload(items: List[Dict[str, Any]]) -> str:
    """[{text}] → "1. t1\\n2. t2"；块内换行折空格（回绘是单行语义）。"""
    lines = []
    for i, it in enumerate(items):
        t = re.sub(r"\s+", " ", str(it.get("text") or "").strip())
        lines.append(f"{i + 1}. {t}")
    return "\n".join(lines)


def parse_blocks_payload(out: str, n: int) -> Optional[List[str]]:
    """批量译文 → 按编号还原 n 条；任何结构失真 → None（调用方回落逐块）。"""
    if n <= 0:
        return None
    got: Dict[int, str] = {}
    for line in str(out or "").splitlines():
        if not line.strip():
            continue
        m = _BATCH_LINE_RE.match(line)
        if not m:
            return None
        idx = int(m.group(1))
        if idx < 1 or idx > n or idx in got:
            return None
        got[idx] = m.group(2).strip()
    if len(got) != n:
        return None
    return [got[i] for i in range(1, n + 1)]


def inflate_box(box: Tuple[float, float, float, float], img_w: int, img_h: int,
                *, ratio: float = _INFLATE_RATIO) -> Tuple[int, int, int, int]:
    """框按高度比例外扩（吃掉 VLM 紧框留下的原字残边），clamp 进图幅。"""
    x1, y1, x2, y2 = box
    pad = max(2.0, (y2 - y1) * ratio)
    return (int(max(0, x1 - pad)), int(max(0, y1 - pad)),
            int(min(img_w, x2 + pad)), int(min(img_h, y2 + pad)))


def sample_bg_color(img: Any, box: Tuple[int, int, int, int]) -> Tuple[int, int, int]:
    """框外一圈像素取中位数当背景色（框内是字，框外紧邻大概率是底色）。"""
    x1, y1, x2, y2 = box
    w, h = img.size
    ring = 3
    pts: List[Tuple[int, int, int]] = []
    px = img.load()
    for xx in range(max(0, x1 - ring), min(w, x2 + ring)):
        for yy in (max(0, y1 - ring), min(h - 1, y2 + ring - 1)):
            pts.append(px[xx, yy][:3])
    for yy in range(max(0, y1 - ring), min(h, y2 + ring)):
        for xx in (max(0, x1 - ring), min(w - 1, x2 + ring - 1)):
            pts.append(px[xx, yy][:3])
    if not pts:
        return (255, 255, 255)
    rs = sorted(p[0] for p in pts)
    gs = sorted(p[1] for p in pts)
    bs = sorted(p[2] for p in pts)
    mid = len(pts) // 2
    return (rs[mid], gs[mid], bs[mid])


def text_color_for(bg: Tuple[int, int, int]) -> Tuple[int, int, int]:
    """按背景亮度选黑/白字（WCAG 相对亮度近似）。"""
    lum = 0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]
    return (17, 17, 17) if lum >= 128 else (245, 245, 245)


def _load_font(size: int) -> Any:
    from PIL import ImageFont
    for cand in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(cand, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def render_patched(img: Any, blocks: List[Dict[str, Any]]) -> Any:
    """把 [{box(像素原框), dst}] 回绘到图上（原图不改，返回副本）。"""
    from PIL import ImageDraw
    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out)
    w, h = out.size
    for b in blocks:
        dst = str(b.get("dst") or "").strip()
        if not dst:
            continue
        box = inflate_box(b["box"], w, h)
        bg = sample_bg_color(out, box)
        d.rectangle(list(box), fill=bg)
        ink = text_color_for(bg)
        x1, y1, x2, y2 = box
        bw, bh = x2 - x1, y2 - y1
        size = max(10, int(bh * 0.62))
        font = _load_font(size)
        while size > 10 and d.textlength(dst, font=font) > bw * 0.96:
            size -= 2
            font = _load_font(size)
        # 仍放不下 → 字符级截断加省略号（诚实：面板里有全文，图上不硬挤出框）
        text = dst
        if d.textlength(text, font=font) > bw * 0.96:
            while len(text) > 1 and d.textlength(text + "…", font=font) > bw * 0.96:
                text = text[:-1]
            text += "…"
        d.text((x1 + max(2, bw * 0.02), y1 + max(0, (bh - size * 1.25) / 2)),
               text, fill=ink, font=font)
    return out


class ImagePatchTranslateService:
    """bbox OCR → 翻译 → （可选）回绘。

    ``text_cache``：注入 MediaTextCache 实例即启用 bbox 原始输出缓存（键
    ``bbox:{sha1}``）；默认 None=不缓存（单测密闭——同内容临时图 sha1 相同，
    默认吃进程级单例会跨用例串味）。
    ``context_translate``：显式开启整图上下文批量翻译（生产由
    ``resolve_patch_cfg`` 决定，默认开；构造默认 False 保住既有直构调用方
    与单测的逐块语义）。
    """

    def __init__(self, translation_service: Any, boxes_fn: BoxesFn,
                 *, max_concurrency: int = 4, text_cache: Any = None,
                 max_blocks: int = _MAX_BLOCKS, context_translate: bool = False,
                 struct_boxes_fn: Optional[StructBoxesFn] = None) -> None:
        self._xlate = translation_service
        self._boxes = boxes_fn
        self._struct_boxes = struct_boxes_fn
        self._sem = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._cache = text_cache
        self._max_blocks = max(1, int(max_blocks or _MAX_BLOCKS))
        self._context = bool(context_translate)

    def _cache_key(self, image_path: str, *, struct: bool) -> str:
        """bbox 缓存键。专用 OCR 与 VLM 分键（'bboxp:' vs 'bbox:'）——两套检测的
        块集合不同，混键会让「切换 provider 做 A/B」永远读到旧引擎的结果。"""
        if self._cache is None:
            return ""
        try:
            from src.ai.media_text_cache import hash_file
            h = hash_file(image_path)
        except Exception:
            return ""
        if not h:
            return ""
        return f"{'bboxp' if struct else 'bbox'}:{h}"

    async def _struct_ocr_items(
        self, image_path: str, img_w: int, img_h: int,
    ) -> Tuple[Optional[List[Dict[str, Any]]], str, bool]:
        """专用 OCR（PP-OCRv5 微服务）路径：(items|None, tag, cached)。

        失败/空 → None（调用方回落 VLM 链，服务掉线绝不断功能）。缓存存
        sanitize 后的 items JSON（像素坐标，读回免再净化）。
        """
        ck = self._cache_key(image_path, struct=True)
        if ck:
            cached = self._cache.get(ck)
            if cached is not None:
                try:
                    items = json.loads(cached)
                    out = [{"text": it["text"], "box": tuple(it["box"])}
                           for it in items]
                    if out:
                        return out, "ppocr_cache", True
                except Exception:
                    pass
        try:
            raw_items, tag = await self._struct_boxes(image_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("专用 OCR 调用异常（回落 VLM）: %s", exc)
            return None, f"error:{type(exc).__name__}", False
        items = sanitize_struct_items(raw_items, img_w, img_h)
        if not items:
            return None, tag or "ppocr_empty", False
        if ck:
            try:
                self._cache.put(ck, json.dumps(
                    [{"text": it["text"], "box": list(it["box"])} for it in items],
                    ensure_ascii=False))
            except Exception:
                pass
        return items, tag or "ppocr", False

    async def _ocr_items(
        self, image_path: str, img_w: int, img_h: int,
    ) -> Tuple[Optional[List[Dict[str, Any]]], str, bool, str]:
        """带缓存的 bbox OCR。返回 (items|None, tag, cached, fail_reason)。

        P1-OCR：配了专用 OCR（struct_boxes_fn）则优先走它（定位精度碾压 VLM），
        失败/空自动回落 VLM 链——微服务掉线时行为=今天的 VLM 近似，零断面。
        """
        if self._struct_boxes is not None:
            items, tag, cached = await self._struct_ocr_items(
                image_path, img_w, img_h)
            if items:
                return items, tag, cached, ""
            logger.info("专用 OCR 无结果(%s) → 回落 VLM bbox", tag)
        ck = self._cache_key(image_path, struct=False)
        if ck:
            raw_cached = self._cache.get(ck)
            if raw_cached is not None:
                items = parse_bbox_items(raw_cached, img_w, img_h)
                if items:
                    return items, "cache", True, ""
        try:
            raw, tag = await self._boxes(image_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("bbox OCR 调用异常: %s", exc)
            return None, f"error:{type(exc).__name__}", False, "ocr_error"
        items = parse_bbox_items(raw or "", img_w, img_h)
        if not items:
            return None, tag, False, "no_boxes"
        if ck:
            try:
                self._cache.put(ck, str(raw))
            except Exception:
                pass
        return items, tag, False, ""

    async def _translate_items(
        self, items: List[Dict[str, Any]], *,
        target_lang: str, source_lang: str, style: str,
    ) -> str:
        """逐块填 it["dst"]/it["identity"]/it["fail"]；返回主导 provider。

        批量路径（context_translate 且 ≥2 块）：编号载荷一次翻译——碎片短语
        互为上下文；协议解析失败整批回落逐块（绝不接受错位）。
        """
        providers: List[str] = []
        if self._context and len(items) >= 2:
            payload = build_blocks_payload(items)
            try:
                res = await self._xlate.translate(
                    payload, target_lang=target_lang,
                    source_lang=source_lang, style=style)
            except Exception:
                res = None
            if res is not None and res.ok and (res.translated_text or "").strip():
                parts = parse_blocks_payload(res.translated_text, len(items))
                if parts is not None:
                    for it, dst in zip(items, parts):
                        d = (dst or "").strip()
                        norm_src = re.sub(r"\s+", " ", it["text"].strip())
                        if not d or d == norm_src or d == it["text"]:
                            it["dst"] = ""
                            it["identity"] = True
                        else:
                            it["dst"] = d
                    return str(getattr(res, "provider", "") or "")
            logger.info("整图批量翻译协议解析失败/不可用，回落逐块")

        async def _tr(it: Dict[str, Any]) -> None:
            async with self._sem:
                try:
                    res = await self._xlate.translate(
                        it["text"], target_lang=target_lang,
                        source_lang=source_lang, style=style)
                except Exception:
                    it["dst"] = ""
                    it["fail"] = True
                    return
            if not res.ok or not (res.translated_text or "").strip():
                it["dst"] = ""
                it["fail"] = True
                return
            dst = res.translated_text.strip()
            providers.append(str(getattr(res, "provider", "") or ""))
            # identity（同语/引擎回原文）→ 不重绘该块
            if dst == it["text"]:
                it["dst"] = ""
                it["identity"] = True
            else:
                it["dst"] = dst

        await asyncio.gather(*(_tr(it) for it in items))
        if not providers:
            return ""
        return max(set(providers), key=providers.count)

    async def translate_image_patched(
        self,
        image_path: str,
        *,
        target_lang: str = "zh",
        source_lang: str = "",
        style: str = "chat",
        render: bool = True,
    ) -> Dict[str, Any]:
        """render=True → 回绘 PNG（旧契约不变）；render=False → 文本模式：
        同一批文字块出「识别原文/译文」（面板与译文图同源同块）。"""
        from PIL import Image

        try:
            img = await asyncio.to_thread(Image.open, image_path)
            img.load()
        except Exception:
            return {"ok": False, "reason": "bad_image", "message": "图片无法打开"}

        items, tag, ocr_cached, fail = await self._ocr_items(image_path, *img.size)
        if fail == "ocr_error":
            return {"ok": False, "reason": "ocr_error", "ocr_tag": tag}
        if not items:
            return {"ok": False, "reason": "no_boxes", "ocr_tag": tag,
                    "message": "识别不到带坐标的文字块（可尝试普通「识别翻译」）"}
        if len(items) > self._max_blocks:
            return {"ok": False, "reason": "too_many_blocks",
                    "message": f"文字块过多（{len(items)} > {self._max_blocks}），密集排版不适合贴回"}

        provider = await self._translate_items(
            items, target_lang=target_lang, source_lang=source_lang, style=style)
        patched_n = sum(1 for it in items if it.get("dst"))
        identity_n = sum(1 for it in items if it.get("identity"))
        failed_n = sum(1 for it in items if it.get("fail"))
        stats = {"blocks": len(items), "patched": patched_n,
                 "identity": identity_n, "failed": failed_n}

        src_text = "\n".join(it["text"] for it in items)
        out_text = "\n".join((it.get("dst") or it["text"]) for it in items)
        try:
            from src.ai.translation_service import detect_language
            src_lang_detected = detect_language(src_text)
        except Exception:
            src_lang_detected = ""

        base = {
            "ocr_tag": tag,
            "ocr_cached": ocr_cached,
            "source_lang": src_lang_detected,
            "provider": provider,
            "src_text": src_text,
            "out_text": out_text,
            "items": [{"text": it["text"], "dst": it.get("dst", ""),
                       "box": [round(v, 1) for v in it["box"]]} for it in items],
            "stats": stats,
        }

        if not render:
            if patched_n + identity_n == 0:
                return {"ok": False, "reason": "translate_failed", "ocr_tag": tag,
                        "stats": stats, "message": "各块翻译失败"}
            return {"ok": True, "mode": "blocks", **base}

        if patched_n == 0:
            return {"ok": False, "reason": "nothing_to_patch", "ocr_tag": tag,
                    "stats": stats,
                    "message": "各块译文与原文一致或翻译失败，无需/无法贴回"}

        try:
            patched = await asyncio.to_thread(render_patched, img, items)
            buf = BytesIO()
            await asyncio.to_thread(patched.save, buf, "PNG")
        except Exception:
            logger.warning("贴回渲染失败", exc_info=True)
            return {"ok": False, "reason": "render_failed", "message": "译文贴回渲染失败"}

        return {"ok": True, "png": buf.getvalue(), **base}


def build_vision_boxes_fn(vision_cfg: Dict[str, Any], global_vision: Dict[str, Any]) -> BoxesFn:
    """生产用带坐标 OCR fn：VisionClient 同一条故障转移管道 + BBOX_PROMPT。"""

    async def _boxes(image_path: str) -> Tuple[Optional[str], str]:
        from src.vision_client import VisionClient
        return await VisionClient.describe_image_with_ollama_zhipu_fallback(
            merged_config=vision_cfg,
            global_vision=global_vision,
            image_path=image_path,
            prompt=BBOX_PROMPT,
        )

    return _boxes


def build_ppocr_boxes_fn(base_url: str, *, timeout_sec: float = 12.0) -> StructBoxesFn:
    """PP-OCRv5 微服务（scripts/ocr176）结构化行框 fn。

    契约：``POST {base_url}/v1/ocr`` body ``{"image_b64": ...}`` →
    ``{"ok": true, "lines": [{"text","box":[x1,y1,x2,y2],"conf"}], "ms": ...}``
    （box=像素坐标）。任何异常/非 2xx/ok=false → (None, tag)——调用方回落 VLM。
    """
    url = str(base_url or "").rstrip("/") + "/v1/ocr"

    async def _boxes(image_path: str) -> Tuple[Optional[List[Dict[str, Any]]], str]:
        import base64 as _b64

        import httpx

        try:
            with open(image_path, "rb") as f:
                payload = _b64.b64encode(f.read()).decode()
        except Exception:
            return None, "ppocr_read_failed"
        try:
            async with httpx.AsyncClient(timeout=float(timeout_sec)) as cli:
                resp = await cli.post(url, json={"image_b64": payload})
            if resp.status_code != 200:
                return None, f"ppocr_http_{resp.status_code}"
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return None, f"ppocr_error:{type(exc).__name__}"
        if not data.get("ok"):
            return None, f"ppocr_notok:{str(data.get('reason') or '')[:32]}"
        return list(data.get("lines") or []), "ppocr"

    return _boxes


def resolve_patch_cfg(full_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """config.media.image_patch_translate（enabled 默认关）。

    - ``unified_ocr``（默认开，仅 enabled 时有意义）：灯箱「识别翻译」走 bbox
      管线文本模式——面板与译文图同源同块，且 bbox 缓存让后续贴回/换语言免 VLM。
    - ``context_translate``（默认开）：整图文字块合并一次翻译（碎片短语互为
      上下文），协议失败自动回落逐块。
    - ``max_blocks``（默认 60）：单图文字块上限。
    """
    try:
        pc = dict(((full_cfg or {}).get("media") or {}).get("image_patch_translate") or {})
    except Exception:
        pc = {}
    try:
        max_blocks = int(pc.get("max_blocks", _MAX_BLOCKS) or _MAX_BLOCKS)
    except Exception:
        max_blocks = _MAX_BLOCKS
    ocr = pc.get("ocr") or {}
    if not isinstance(ocr, dict):
        ocr = {}
    try:
        ocr_timeout = float(ocr.get("timeout_sec", 12.0) or 12.0)
    except Exception:
        ocr_timeout = 12.0
    return {
        "enabled": bool(pc.get("enabled", False)),
        "unified_ocr": bool(pc.get("unified_ocr", True)),
        "context_translate": bool(pc.get("context_translate", True)),
        "max_blocks": max_blocks,
        # P1-OCR：专用 OCR 微服务（provider=ppocr 且 base_url 非空才启用；
        # 服务掉线自动回落 VLM 链——切换/回退都是 overlay 热改，零重启）
        "ocr_provider": str(ocr.get("provider") or "vlm").strip().lower(),
        "ocr_base_url": str(ocr.get("base_url") or "").strip(),
        "ocr_timeout_sec": ocr_timeout,
    }


__all__ = [
    "ImagePatchTranslateService", "build_vision_boxes_fn", "build_ppocr_boxes_fn",
    "resolve_patch_cfg", "parse_bbox_items", "sanitize_struct_items", "inflate_box",
    "sample_bg_color", "text_color_for", "render_patched",
    "build_blocks_payload", "parse_blocks_payload", "BBOX_PROMPT",
]
