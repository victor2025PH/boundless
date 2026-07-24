# -*- coding: utf-8 -*-
"""fetch → apiFetch 迁移工具（P1-1 GET 批 / P2-1 写路径批）。

用法（在 repo 根或任意 cwd 均可）：
  python tools/fetch_codemod.py --gets            # GET 形态调用 dry-run（P1-1 已跑完）
  python tools/fetch_codemod.py --writes          # 写路径调用 dry-run（P2-1）
  python tools/fetch_codemod.py --writes --apply  # 实施
  python tools/fetch_codemod.py --list-writes     # 列出所有写路径端点，供人工分桶

策略（生产安全优先）：
  GET 批（P1-1，已完成）：
    - fetch(URL) / fetch(URL, INIT 无 method 或 GET) → apiFetch(...)，默认 20s 超时。
    - INIT 含 signal: 跳过（调用方自管生命周期）。
  写路径批（P2-1）：
    - 默认 → apiFetch(URL, INIT, {timeoutMs: 60000})：60s 上限只在真挂死时触发
      （覆盖 LLM 长尾：本地兜底冷载 15-27s、RPA 发送 UI 自动化 10-30s），
      正常写操作 <2s 完全无感；不配重试（写操作非幂等）。
    - 长操作端点（LONG_OP_PATTERNS 命中 URL 或 span 含 FormData 上传）→ 保持裸 fetch
      并留在 ratchet 台账里：这些操作（embed-all/导入/上传/文档翻译同步档…）
      可以合法跑几分钟，任何统一超时都是回归风险；后续按端点单独治理。
  两批共同：字符串/注释内的伪命中跳过；_api_fetch.html 基建自身豁免。

写回保持原文件换行风格（CRLF 文件还原 CRLF）——P1-1 曾把 6 个 CRLF 模板压成 LF，
git diff 被 1.7 万行 EOL 抖动淹没，勿删该逻辑。
"""
import pathlib, re, sys, io, json

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from tests._inline_handler_scan import _mask, _SCRIPT  # noqa: E402

ROOT = _REPO / "src/web/templates"
FETCH_RE = re.compile(r"(?<![\w.])fetch\s*\(")
SKIP_FILES = {"_api_fetch.html"}

# 写路径里"合法慢"的端点——统一超时=回归风险，保持裸 fetch 留在台账。
LONG_OP_PATTERNS = (
    "embed-all", "seed-pack", "import", "upload", "enroll",
    "translate-document", "relogin", "restart", "prerender",
    "backup", "export", "media",          # persona 相册上传（POST /media）
    "clone", "selfie",                    # 语音克隆/自拍生成（GPU 合成分钟级）
    "backfill",                           # 批扫描任务（episodic/relations）
    "bulk-autosend",                      # 同步循环 ≤200 张草稿逐张 resolve
    "data-purge",                         # 大库批删
)

APPLY = "--apply" in sys.argv
MODE_GETS = "--gets" in sys.argv
MODE_WRITES = "--writes" in sys.argv
MODE_LIST = "--list-writes" in sys.argv
WRITE_OPT = ", {timeoutMs: 60000}"


def masked_canvas(raw: str) -> str:
    """全文等长画布：<script> 体经 JS 掩码器处理，其余区域一律置空格。

    对整页 HTML 直接跑 JS 掩码器会被 HTML 文案里的撇号带偏（line_rpa 曾整页误判）；
    只有 script 体才是 JS 语法域。"""
    canvas = [" "] * len(raw)
    for m in _SCRIPT.finditer(raw):
        attrs = m.group(1) or ""
        if re.search(r"\bsrc\s*=", attrs, re.IGNORECASE):
            continue
        body = m.group(2)
        start = m.start(2)
        mb = _mask(body)
        canvas[start:start + len(body)] = list(mb)
    return "".join(canvas)


def call_span(raw: str, masked: str, m: re.Match):
    """解析一次 fetch(...) 调用：返回 (open, close, commas, ok)。"""
    pos = m.start()
    if masked[pos:pos + 5] != "fetch":
        return None
    op = raw.index("(", pos)
    depth = 0
    commas = []
    i = op
    n = len(masked)
    while i < n:
        c = masked[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0 and c == ")":
                return op, i, commas
        elif c == "," and depth == 1:
            commas.append(i)
        i += 1
    return None


def analyze(raw: str, masked: str, m: re.Match):
    """Return (decision, reason, close_pos)."""
    span = call_span(raw, masked, m)
    if span is None:
        why = "in-string-or-comment" if masked[m.start():m.start() + 5] != "fetch" else "unbalanced"
        return "skip", why, -1
    op, close, commas = span
    if not commas:
        return ("migrate-get" if MODE_GETS else "skip"), "single-arg", close
    arg1_raw = raw[op + 1:commas[0]]
    arg2_raw = raw[commas[0] + 1:close]
    arg2_masked = masked[commas[0] + 1:close]
    if re.search(r"\bsignal\b\s*:", arg2_masked):
        return "skip", "has-signal", close
    mm = re.search(r"\bmethod\b\s*:", arg2_masked)
    is_get = False
    if not mm:
        is_get = True
    else:
        tail = arg2_raw[mm.end():mm.end() + 40]
        if re.match(r"\s*['\"]GET['\"]", tail, re.I):
            is_get = True
    if is_get:
        return ("migrate-get" if MODE_GETS else "skip"), "get-shaped", close
    # ---- 写路径 ----
    url_hint = " ".join(arg1_raw.split())[:120]
    if "FormData" in arg2_raw or re.search(r"\bbody\s*:\s*fd\b", arg2_raw):
        return "skip", f"long-op(form-data) {url_hint}", close
    low = arg1_raw.lower()
    for pat in LONG_OP_PATTERNS:
        if pat in low:
            return "skip", f"long-op({pat}) {url_hint}", close
    if MODE_LIST:
        return "skip", f"write {url_hint}", close
    if MODE_WRITES:
        return "migrate-write", "write-60s", close
    return "skip", "write(not in mode)", close


report = {}
total_mig = total_skip = 0
for p in sorted(ROOT.rglob("*.html")):
    rel = p.relative_to(ROOT).as_posix()
    if p.name in SKIP_FILES:
        continue
    raw = p.read_text(encoding="utf-8")
    masked = masked_canvas(raw)
    assert len(masked) == len(raw), rel
    edits = []   # (pos, close, kind)
    skips = []
    for m in FETCH_RE.finditer(raw):
        dec, why, close = analyze(raw, masked, m)
        if dec.startswith("migrate"):
            edits.append((m.start(), close, dec))
        else:
            skips.append(why)
    if not edits and not skips:
        continue
    if MODE_LIST:
        for why in skips:
            if why.startswith(("write ", "long-op")):
                print(f"  [{rel}] {why}")
        continue
    report[rel] = {"migrate": len(edits), "skip": len(skips),
                   "skip_reasons": {r: skips.count(r) for r in set(skips)}}
    total_mig += len(edits)
    total_skip += len(skips)
    if APPLY and edits:
        out = raw
        for pos, close, kind in sorted(edits, reverse=True):
            assert out[pos:pos + 5] == "fetch"
            if kind == "migrate-write":
                out = out[:close] + WRITE_OPT + out[close:]
            out = out[:pos] + "apiFetch" + out[pos + 5:]
        if b"\r\n" in p.read_bytes():
            out = out.replace("\n", "\r\n")
        p.write_text(out, encoding="utf-8", newline="")

if not MODE_LIST:
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\nTOTAL migrate={total_mig} skip={total_skip} applied={APPLY}")
