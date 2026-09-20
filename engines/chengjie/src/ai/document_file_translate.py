"""Phase L2：.docx 文档**带版式**整篇翻译。

用 ``python-docx`` 原地翻译每个段落 / 表格单元格的文字，**保留文档结构与样式**
（标题/列表/表格/字体随段落首 run 保持），再存回 .docx 字节。对标 DeepL 文档翻译。

逐段复用注入的 ``TranslationService.translate``（享 L1/L2 缓存 + 术语强制 + 品牌词保护 +
F+ 会话首选引擎），与 L1（纯文本）同源不重复造翻译逻辑。

设计要点：
- 仅 ``.docx``（pdf 不可结构化回填，留 L2b 文本抽取）；``python-docx`` 缺失 → ok=False 软失败。
- 有界并发；单段失败保留原文（best-effort，整体仍 ok）。
- 版式保真折中：译文写入段落**首 run**、清空其余 run——保住该段字体/样式，避免 run 边界割裂译文。
- 上限保护：段落数封顶（防超大文档 OOM）。
"""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 进度回调类型：progress(done, total)。best-effort——回调内异常被吞，不影响翻译主流程
ProgressCb = Optional[Callable[[int, int], None]]


def _emit(progress: ProgressCb, done: int, total: int) -> None:
    if progress is None:
        return
    try:
        progress(done, total)
    except Exception:
        logger.debug("[doc-xlate] 进度回调异常（忽略）", exc_info=True)

# .docx/.xlsx/.pptx 保版式真往返；.pdf 只做文本抽取→译→纯文本（pdf 不可结构化回填）；
# .srt/.vtt 保时间轴逐条译（P1 2026-08-18，外贸高频：客户甩来的视频字幕）
SUPPORTED_EXT = (".docx", ".xlsx", ".pdf", ".pptx", ".srt", ".vtt")
_MAX_PARAGRAPHS = 5000
_MAX_CELLS = 20000
_MAX_SUBTITLE_LINES = 20000


def docx_available() -> bool:
    try:
        import docx  # noqa: F401
        return True
    except Exception:
        return False


def xlsx_available() -> bool:
    try:
        import openpyxl  # noqa: F401
        return True
    except Exception:
        return False


def pdf_available() -> bool:
    try:
        from pdfminer.high_level import extract_text  # noqa: F401
        return True
    except Exception:
        return False


def pptx_available() -> bool:
    try:
        import pptx  # noqa: F401
        return True
    except Exception:
        return False


def _collect_paragraphs(container: Any) -> List[Any]:
    """收集 container（Document / _Cell）下所有段落，含表格单元格（递归一层嵌套表）。"""
    out: List[Any] = []
    for p in getattr(container, "paragraphs", []) or []:
        out.append(p)
    for table in getattr(container, "tables", []) or []:
        for row in table.rows:
            for cell in row.cells:
                out.extend(_collect_paragraphs(cell))
    return out


def _set_paragraph_text(paragraph: Any, text: str) -> None:
    """把译文写回段落，尽量保留版式：写入首 run、清空其余 run。"""
    runs = paragraph.runs
    if runs:
        runs[0].text = text
        for r in runs[1:]:
            r.text = ""
    else:
        paragraph.text = text  # 无 run（罕见）→ 直接设（python-docx 会补一个 run）


async def translate_docx(
    data: bytes,
    *,
    xlate: Any,
    target_lang: str = "zh",
    source_lang: str = "",
    style: str = "chat",
    engine: str = "",
    max_concurrency: int = 4,
    progress: ProgressCb = None,
) -> Dict[str, Any]:
    """翻译 .docx 字节，返回 {ok, data(bytes)?, stats, reason?}。

    ``progress(done, total)`` 可选，每段完成后回调一次（供 SSE 进度条）。
    """
    if not docx_available():
        return {"ok": False, "reason": "docx_unavailable",
                "message": "未安装 python-docx，无法翻译 .docx 文档"}
    import docx

    try:
        document = docx.Document(BytesIO(data))
    except Exception:
        logger.debug("[docx-xlate] 打开文档失败", exc_info=True)
        return {"ok": False, "reason": "bad_docx", "message": "文档损坏或非 .docx 格式"}

    paragraphs = [p for p in _collect_paragraphs(document) if (p.text or "").strip()]
    if len(paragraphs) > _MAX_PARAGRAPHS:
        return {"ok": False, "reason": "too_many_segments",
                "message": f"段落过多（上限 {_MAX_PARAGRAPHS}）"}

    total = len(paragraphs)
    sem = asyncio.Semaphore(max(1, int(max_concurrency)))
    stats = {"total": total, "translated": 0, "failed": 0, "cached": 0}
    _emit(progress, 0, total)

    async def _do(paragraph: Any) -> None:
        text = paragraph.text
        async with sem:
            try:
                res = await xlate.translate(
                    text, target_lang=target_lang, source_lang=source_lang,
                    style=style, engine=engine)
            except Exception:
                stats["failed"] += 1
                logger.debug("[docx-xlate] 段翻译异常（保留原文）", exc_info=True)
                _emit(progress, stats["translated"] + stats["failed"], total)
                return
        dst = (res.translated_text or "").strip() if res.ok else ""
        if res.ok and dst:
            _set_paragraph_text(paragraph, dst)
            stats["translated"] += 1
            if getattr(res, "cached", False):
                stats["cached"] += 1
        else:
            stats["failed"] += 1
        _emit(progress, stats["translated"] + stats["failed"], total)

    await asyncio.gather(*(_do(p) for p in paragraphs))

    out = BytesIO()
    try:
        document.save(out)
    except Exception:
        logger.debug("[docx-xlate] 保存失败", exc_info=True)
        return {"ok": False, "reason": "save_failed", "message": "译文写回失败"}
    return {"ok": True, "data": out.getvalue(), "stats": stats}


async def translate_xlsx(
    data: bytes,
    *,
    xlate: Any,
    target_lang: str = "zh",
    source_lang: str = "",
    style: str = "chat",
    engine: str = "",
    max_concurrency: int = 4,
    progress: ProgressCb = None,
) -> Dict[str, Any]:
    """翻译 .xlsx 字节（保表格/样式），返回 {ok, data(bytes)?, stats, reason?}。

    仅翻译**字符串单元格**；数字/日期/公式（以 ``=`` 开头）原样保留。
    ``progress(done, total)`` 可选，每格完成后回调一次。
    """
    if not xlsx_available():
        return {"ok": False, "reason": "xlsx_unavailable",
                "message": "未安装 openpyxl，无法翻译 .xlsx"}
    import openpyxl

    try:
        # 同步解析放线程池，避免大文件阻塞 ASGI 事件循环
        wb = await asyncio.to_thread(openpyxl.load_workbook, BytesIO(data))
    except Exception:
        logger.debug("[xlsx-xlate] 打开失败", exc_info=True)
        return {"ok": False, "reason": "bad_xlsx", "message": "文件损坏或非 .xlsx 格式"}

    targets: List[Any] = []  # 待译单元格
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if isinstance(v, str) and v.strip() and not v.lstrip().startswith("="):
                    targets.append(cell)
    if len(targets) > _MAX_CELLS:
        return {"ok": False, "reason": "too_many_cells",
                "message": f"单元格过多（上限 {_MAX_CELLS}）"}

    total = len(targets)
    sem = asyncio.Semaphore(max(1, int(max_concurrency)))
    stats = {"total": total, "translated": 0, "failed": 0, "cached": 0}
    _emit(progress, 0, total)

    async def _do(cell: Any) -> None:
        text = cell.value
        async with sem:
            try:
                res = await xlate.translate(
                    text, target_lang=target_lang, source_lang=source_lang,
                    style=style, engine=engine)
            except Exception:
                stats["failed"] += 1
                _emit(progress, stats["translated"] + stats["failed"], total)
                return
        dst = (res.translated_text or "").strip() if res.ok else ""
        if res.ok and dst:
            cell.value = dst
            stats["translated"] += 1
            if getattr(res, "cached", False):
                stats["cached"] += 1
        else:
            stats["failed"] += 1
        _emit(progress, stats["translated"] + stats["failed"], total)

    await asyncio.gather(*(_do(c) for c in targets))

    out = BytesIO()
    try:
        await asyncio.to_thread(wb.save, out)
    except Exception:
        logger.debug("[xlsx-xlate] 保存失败", exc_info=True)
        return {"ok": False, "reason": "save_failed", "message": "译文写回失败"}
    return {"ok": True, "data": out.getvalue(), "stats": stats}


async def translate_pdf_to_text(
    data: bytes,
    *,
    xlate: Any,
    target_lang: str = "zh",
    source_lang: str = "",
    style: str = "chat",
    engine: str = "",
    progress: ProgressCb = None,
) -> Dict[str, Any]:
    """抽取 .pdf 文本 → 整篇翻译 → 返回**纯文本**（pdf 不可结构化回填，故只出文本）。

    返回 {ok, text?, stats, reason?}。复用 L1 ``DocumentTranslateService`` 逐段翻译。
    ``progress(done, total)`` 可选，逐段回调。
    """
    if not pdf_available():
        return {"ok": False, "reason": "pdf_unavailable",
                "message": "未安装 pdfminer.six，无法解析 .pdf"}
    from pdfminer.high_level import extract_text

    try:
        # pdfminer 抽取是同步 CPU 密集，放线程池避免阻塞事件循环
        raw = (await asyncio.to_thread(extract_text, BytesIO(data))) or ""
    except Exception:
        logger.debug("[pdf-xlate] 抽取失败", exc_info=True)
        return {"ok": False, "reason": "bad_pdf", "message": "PDF 解析失败（可能为扫描件/加密）"}
    if not raw.strip():
        return {"ok": False, "reason": "no_text",
                "message": "PDF 无可抽取文本（扫描件请用「图片翻译」逐页 OCR）"}

    from src.ai.document_translate import DocumentTranslateService
    svc = DocumentTranslateService(xlate)
    res = await svc.translate_document(
        raw, target_lang=target_lang, source_lang=source_lang, style=style,
        engine=engine, progress=progress)
    if not res.get("ok"):
        return res
    return {"ok": True, "text": res.get("translated_text", ""),
            "stats": res.get("stats", {})}


# ── P1（2026-08-18）：.srt/.vtt 字幕保时间轴翻译 ─────────────────────────────
def classify_subtitle_lines(lines: List[str], kind: str = "srt") -> List[bool]:
    """逐行判定「是否为待译字幕文本」（True=译，False=原样保留）。纯函数便于金标测试。

    保留：空行 / 时间轴行（含 ``-->``）/ 纯数字序号行 / **紧邻时间轴行之前的 cue id 行**
    （vtt 允许任意字符串 id）/ vtt 头（``WEBVTT``）与 ``NOTE``/``STYLE``/``REGION``
    元块（起始行到下一空行整块保留）。其余行=字幕文本。
    """
    out: List[bool] = []
    in_meta = False
    for i, raw in enumerate(lines):
        line = raw.strip()
        if in_meta:
            out.append(False)
            if not line:
                in_meta = False
            continue
        if not line:
            out.append(False)
            continue
        if kind == "vtt" and (
            line.upper().startswith("WEBVTT")
            or line.startswith(("NOTE", "STYLE", "REGION"))
        ):
            out.append(False)
            in_meta = True
            continue
        if "-->" in line:
            out.append(False)
            continue
        if line.isdigit():
            out.append(False)
            continue
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if "-->" in nxt:
            out.append(False)   # cue id 行（下一行就是时间轴）
            continue
        out.append(True)
    return out


async def translate_subtitle(
    data: bytes,
    *,
    xlate: Any,
    kind: str = "srt",
    target_lang: str = "zh",
    source_lang: str = "",
    style: str = "chat",
    engine: str = "",
    bilingual: bool = False,
    max_concurrency: int = 4,
    progress: ProgressCb = None,
) -> Dict[str, Any]:
    """翻译 .srt/.vtt 字幕字节：**时间轴/序号/cue id/元块逐字节级保留**，只译文本行。

    ``bilingual=True`` → 双语字幕（原文行下加一行译文，SRT/VTT 多行 cue 合法）。
    返回 {ok, data(bytes)?, stats, reason?}。
    """
    try:
        text = data.decode("utf-8-sig", errors="replace")
    except Exception:
        return {"ok": False, "reason": "bad_subtitle", "message": "字幕文件解码失败"}
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if len(lines) > _MAX_SUBTITLE_LINES:
        return {"ok": False, "reason": "too_many_segments",
                "message": f"字幕行数过多（上限 {_MAX_SUBTITLE_LINES}）"}
    flags = classify_subtitle_lines(lines, kind=kind)
    idxs = [i for i, f in enumerate(flags) if f]
    if not idxs:
        return {"ok": False, "reason": "no_text", "message": "未找到可译字幕文本行"}

    total = len(idxs)
    sem = asyncio.Semaphore(max(1, int(max_concurrency)))
    stats = {"total": total, "translated": 0, "failed": 0, "cached": 0}
    out_lines = list(lines)
    _emit(progress, 0, total)

    async def _do(idx: int) -> None:
        src = lines[idx]
        async with sem:
            try:
                res = await xlate.translate(
                    src, target_lang=target_lang, source_lang=source_lang,
                    style=style, engine=engine)
            except Exception:
                stats["failed"] += 1
                logger.debug("[srt-xlate] 行翻译异常（保留原文）", exc_info=True)
                _emit(progress, stats["translated"] + stats["failed"], total)
                return
        dst = (res.translated_text or "").strip() if res.ok else ""
        if res.ok and dst:
            out_lines[idx] = f"{src}\n{dst}" if bilingual else dst
            stats["translated"] += 1
            if getattr(res, "cached", False):
                stats["cached"] += 1
        else:
            stats["failed"] += 1
        _emit(progress, stats["translated"] + stats["failed"], total)

    await asyncio.gather(*(_do(i) for i in idxs))
    return {"ok": True, "data": "\n".join(out_lines).encode("utf-8"), "stats": stats}


# ── P1（2026-08-18）：.pptx 保版式整篇翻译（与 .docx 同模式） ─────────────────
def _collect_pptx_paragraphs(prs: Any) -> List[Any]:
    """收集演示文稿全部待译段落：形状文本框（含组合形状递归）+ 表格单元格 + 备注页。"""
    out: List[Any] = []

    def _walk_shape(shape: Any) -> None:
        try:
            for sub in getattr(shape, "shapes", None) or []:   # 组合形状递归
                _walk_shape(sub)
        except Exception:
            pass
        try:
            if getattr(shape, "has_text_frame", False):
                out.extend(shape.text_frame.paragraphs)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    for cell in row.cells:
                        out.extend(cell.text_frame.paragraphs)
        except Exception:
            logger.debug("[pptx-xlate] 形状遍历异常（跳过该形状）", exc_info=True)

    for slide in prs.slides:
        for shape in slide.shapes:
            _walk_shape(shape)
        try:
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                out.extend(slide.notes_slide.notes_text_frame.paragraphs)
        except Exception:
            pass
    return out


def _set_pptx_paragraph_text(paragraph: Any, text: str) -> None:
    """译文写回段落首 run、清空其余 run（与 docx 同折中：保住段级字体/样式）。"""
    runs = paragraph.runs
    if runs:
        runs[0].text = text
        for r in runs[1:]:
            r.text = ""


async def translate_pptx(
    data: bytes,
    *,
    xlate: Any,
    target_lang: str = "zh",
    source_lang: str = "",
    style: str = "chat",
    engine: str = "",
    max_concurrency: int = 4,
    progress: ProgressCb = None,
) -> Dict[str, Any]:
    """翻译 .pptx 字节（保幻灯片版式/表格/备注），返回 {ok, data(bytes)?, stats, reason?}。"""
    if not pptx_available():
        return {"ok": False, "reason": "pptx_unavailable",
                "message": "未安装 python-pptx，无法翻译 .pptx"}
    from pptx import Presentation

    try:
        prs = await asyncio.to_thread(Presentation, BytesIO(data))
    except Exception:
        logger.debug("[pptx-xlate] 打开失败", exc_info=True)
        return {"ok": False, "reason": "bad_pptx", "message": "文件损坏或非 .pptx 格式"}

    paragraphs = [p for p in _collect_pptx_paragraphs(prs)
                  if (getattr(p, "text", "") or "").strip()]
    if len(paragraphs) > _MAX_PARAGRAPHS:
        return {"ok": False, "reason": "too_many_segments",
                "message": f"段落过多（上限 {_MAX_PARAGRAPHS}）"}

    total = len(paragraphs)
    sem = asyncio.Semaphore(max(1, int(max_concurrency)))
    stats = {"total": total, "translated": 0, "failed": 0, "cached": 0}
    _emit(progress, 0, total)

    async def _do(paragraph: Any) -> None:
        text = paragraph.text
        async with sem:
            try:
                res = await xlate.translate(
                    text, target_lang=target_lang, source_lang=source_lang,
                    style=style, engine=engine)
            except Exception:
                stats["failed"] += 1
                logger.debug("[pptx-xlate] 段翻译异常（保留原文）", exc_info=True)
                _emit(progress, stats["translated"] + stats["failed"], total)
                return
        dst = (res.translated_text or "").strip() if res.ok else ""
        if res.ok and dst:
            _set_pptx_paragraph_text(paragraph, dst)
            stats["translated"] += 1
            if getattr(res, "cached", False):
                stats["cached"] += 1
        else:
            stats["failed"] += 1
        _emit(progress, stats["translated"] + stats["failed"], total)

    await asyncio.gather(*(_do(p) for p in paragraphs))

    out = BytesIO()
    try:
        await asyncio.to_thread(prs.save, out)
    except Exception:
        logger.debug("[pptx-xlate] 保存失败", exc_info=True)
        return {"ok": False, "reason": "save_failed", "message": "译文写回失败"}
    return {"ok": True, "data": out.getvalue(), "stats": stats}


__all__ = [
    "translate_docx", "translate_xlsx", "translate_pdf_to_text",
    "translate_pptx", "translate_subtitle", "classify_subtitle_lines",
    "docx_available", "xlsx_available", "pdf_available", "pptx_available",
    "SUPPORTED_EXT",
]
