# -*- coding: utf-8 -*-
"""发版门禁：出站「去 AI 标点 / 句式」200 样例 0 em dash / 0 分号 / 0 总结尾句 / 0 反问尾。

O-1 B/E（#253 #254 · D-O2，2026-09-08）。用法：

    python scripts/outbound_style_gate.py                 # 内置 ≥200 合成样例（英 / 中 / 日 / 泰）
    python scripts/outbound_style_gate.py --input out.jsonl   # 额外喂真实出站样本（每行 {"text","lang"}）
    python scripts/outbound_style_gate.py --show 10       # 打印前 N 条改写前后
    python scripts/outbound_style_gate.py --draft-log logs/app.log [--max-gen-dash-pct 5]
        # P-1 C（#259）生成侧门禁：读起草层 ``[draft] … dash=n`` 行（净化**前**的 LLM 原文计数），
        # 生成侧破折号率必须 <5%（后处理兜到 0）；样本 <20 条判「样本不足」退出码 3

退出码 0 = 全过；1 = 有样例仍含 AI 标点 / 句式 或 生成侧破折号率超阈；2 = 输入文件读不出；
3 = 生成侧样本不足。**不过不发版**（N-5 E / 1.0.78；P-1 / 1.0.79 加生成侧）。
门禁口径与 ``src/inbox/outbound_humanize.humanize`` 同一函数——脚本不重造规则，只做体检。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.inbox.outbound_humanize import humanize, resolve_cfg  # noqa: E402

EM_DASH_RE = re.compile(r"[\u2014\u2013\u2015\u2012]")
SEMI_RE = re.compile(r";(?![\-\)\(DPpOo\]])|；")
TAG_Q_RE = re.compile(
    r"(?:isn'?t\s+it|aren'?t\s+they|don'?t\s+you\s+think|right|you\s+know|对吧|是吧|不是吗)\s*[?？]\s*$",
    re.IGNORECASE)
SUMMARY_TAIL_RE = re.compile(
    r"(?:^|[.!?。！？\u2026\n]\s*|\.\.\.\s*)(?:at the end of the day|in the end|after all|life is|that'?s what|"
    r"sometimes it'?s|it'?s the little things|here'?s to|生活就是|人生就是|总之|所以说|这就是)",
    re.IGNORECASE | re.MULTILINE)

# ── 合成样例：模板 × 填充，覆盖四语种与每类 AI 痕迹 ─────────────────────────
_EN_OPEN = [
    "Rainy days are the best", "I get what you mean", "That sounds exhausting",
    "Mornings like this are rare", "I love that you noticed", "Work has been a lot lately",
    "Coffee first, always", "You made it through the week", "That movie was something",
    "Your cat sounds like a menace",
]
_EN_DASH = [
    "they make everything slow down", "it's how you keep going", "small wins count",
    "nobody warns you about that", "it kind of grows on you", "you deserve a slow evening",
]
_EN_SUMMARY = [
    "At the end of the day, it's the little things, isn't it?",
    "Life is what happens while we're busy, right?",
    "Sometimes it's the small moments that matter most, don't you think?",
    "That's what makes it all worth it, aren't they?",
    "Here's to more mornings like this.",
    "In the end, we all just want to be seen, you know?",
    "After all, that's the beauty of it.",
]
_EN_MID = [
    "you just breathe; nothing else matters", "I'd say 80% of it is showing up",
    "it was about 30°C by noon; unreal", "I worked 9–5 and then some",
    "there's a “right” way and there's your way", "honestly… I'd just nap",
]
_ZH_OPEN = ["你说得对", "我懂那种感觉", "今天真的累到了", "这种早晨很难得", "工作最近确实多", "先喝杯咖啡再说"]
_ZH_DASH = ["有时候慢一点也挺好的", "撑过去就好了", "小事也算赢", "慢慢就习惯了", "你值得一个慢一点的晚上"]
_ZH_SUMMARY = ["生活就是这样，对吧？", "人生就是不断往前走，是吧？", "总之，好好休息才是最重要的。",
               "所以说，小确幸才是真的。", "这就是生活的意义，不是吗？"]
_ZH_MID = ["你就先喘口气；别的都不急", "差不多 30°C，热得不行", "早上 9–5 排满了……", "有“对的”做法，也有你的做法"]
_JA_OPEN = ["そうだね", "わかるよ", "今日は疲れたね", "こんな朝は珍しいね"]
_JA_DASH = ["雨の日はゆっくりできる", "少しずつでいいと思う", "小さなことも大事"]
_JA_MID = ["まずコーヒー；それから考える", "だいたい 30°C だった…", "9–5 で働いてた"]
_TH_OPEN = ["วันนี้ร้อนมาก", "เข้าใจเลย", "เหนื่อยมากเลยวันนี้"]
_TH_DASH = ["ประมาณ 35°C เลย", "ค่อยเป็นค่อยไปนะ", "พักบ้างก็ได้"]
_TH_MID = ["กินอะไรมาบ้างแล้วหรือยัง; อย่าลืมนะ", "ทำงาน 9–5 แล้วก็ต่อ…"]


def build_samples(n: int = 200) -> List[Tuple[str, str]]:
    """至少 ``n`` 条 ``(lang, text)``，每条含 ≥1 类 AI 痕迹；四语种混合，英文占多数（事故语种）。"""
    out: List[Tuple[str, str]] = []
    i = 0
    while len(out) < n:
        k = i % 10
        if k < 6:
            o = _EN_OPEN[i % len(_EN_OPEN)]
            d = _EN_DASH[(i // 3) % len(_EN_DASH)]
            m = _EN_MID[(i // 5) % len(_EN_MID)]
            s = _EN_SUMMARY[(i // 2) % len(_EN_SUMMARY)]
            forms = [
                f"{o} — {d}. {m}. {s}",
                f"{o} – {d}; {m}. {s}",
                f"{o}—{d}!!! 😊😊😊 {s}",
                f"Here’s the thing:\n- {d}\n- {m}\n{s}",
                f"{o}. {m}… {s}",
            ]
            out.append(("en", forms[i % len(forms)]))
        elif k < 8:
            o = _ZH_OPEN[i % len(_ZH_OPEN)]
            d = _ZH_DASH[(i // 3) % len(_ZH_DASH)]
            m = _ZH_MID[(i // 5) % len(_ZH_MID)]
            s = _ZH_SUMMARY[(i // 2) % len(_ZH_SUMMARY)]
            forms = [
                f"{o}——{d}；{m}。{s}",
                f"{o}—{d}！！！{m}。{s}",
                f"{o}。{m}……{s}",
            ]
            out.append(("zh", forms[i % len(forms)]))
        elif k < 9:
            o = _JA_OPEN[i % len(_JA_OPEN)]
            d = _JA_DASH[(i // 3) % len(_JA_DASH)]
            m = _JA_MID[(i // 5) % len(_JA_MID)]
            out.append(("ja", f"{o}―{d}。{m}。今日はどうだった？"))
        else:
            o = _TH_OPEN[i % len(_TH_OPEN)]
            d = _TH_DASH[(i // 3) % len(_TH_DASH)]
            m = _TH_MID[(i // 5) % len(_TH_MID)]
            out.append(("th", f"{o} — {d} {m}"))
        i += 1
    return out


def load_samples(path: Path) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            out.append(("", line))
            continue
        if isinstance(obj, dict):
            t = str(obj.get("text") or "")
            if t:
                out.append((str(obj.get("lang") or ""), t))
    return out


def violations(text: str) -> List[str]:
    v: List[str] = []
    if EM_DASH_RE.search(text):
        v.append("em_dash")
    if SEMI_RE.search(text):
        v.append("semicolon")
    if TAG_Q_RE.search(text):
        v.append("tag_question")
    if SUMMARY_TAIL_RE.search(text) and _last_sentence_is_summary(text):
        v.append("summary_tail")
    return v


_SENT_BOUNDARY_RE = re.compile(
    r"(?<=[.!?。！？])\s+|(?<=[。！？])|(?<=\.\.\.)(?=\S)|(?<=\u2026)(?=\S)|\n+")


def _last_sentence_is_summary(text: str) -> bool:
    parts = [p for p in _SENT_BOUNDARY_RE.split(text.strip()) if p and p.strip()]
    if len(parts) < 2:
        return False
    last = parts[-1].strip()
    return bool(re.match(
        r"^(?:at the end of the day|in the end|after all|life is|that'?s what|sometimes it'?s|"
        r"it'?s the little things|here'?s to|生活就是|人生就是|总之|所以说|这就是)", last, re.IGNORECASE))


def run_gate(samples: Iterable[Tuple[str, str]], *, show: int = 0) -> Dict[str, object]:
    cfg = resolve_cfg({})
    total = 0
    bad: List[Dict[str, object]] = []
    agg: Dict[str, int] = {"punct_fix": 0, "style_fix": 0, "trimmed": 0}
    shown = 0
    for lang, text in samples:
        total += 1
        out, st = humanize(text, lang, cfg=cfg)
        for k in agg:
            agg[k] += int(st.get(k, 0))
        v = violations(out)
        if v:
            bad.append({"lang": lang, "in": text, "out": out, "violations": v})
        if show and shown < show:
            shown += 1
            print(f"[{lang or '-'}] {text!r}\n   -> {out!r}  (punct={st['punct_fix']} style={st['style_fix']} trimmed={st['trimmed']})")
    return {"total": total, "bad": bad, "agg": agg}


_DRAFT_LINE_RE = re.compile(r"\[draft\] conv=\S+ .*?\bclaim=(\w+) punct_fix=(\d+) style_fix=(\d+) trimmed=(\d+) dash=(\d+)")
GEN_DASH_MIN_SAMPLES = 20


def gen_side_stats(lines: Iterable[str]) -> Dict[str, object]:
    """从 ``[draft]`` 日志行统计**生成侧**（净化前）指标：稿数 / 含破折号稿数 / 引用改写数。"""
    total = dash = claim_rw = 0
    for ln in lines:
        m = _DRAFT_LINE_RE.search(ln)
        if not m:
            continue
        total += 1
        if int(m.group(5)) > 0:
            dash += 1
        if m.group(1) == "rewrite":
            claim_rw += 1
    rate = (dash * 100.0 / total) if total else 0.0
    return {"total": total, "dash": dash, "dash_rate_pct": round(rate, 2), "claim_rewrite": claim_rw}


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", type=str, default="", help="真实出站样本 jsonl（每行 {text, lang}）")
    ap.add_argument("--n", type=int, default=200, help="合成样例条数（默认 200）")
    ap.add_argument("--show", type=int, default=0, help="打印前 N 条改写前后")
    ap.add_argument("--draft-log", type=str, nargs="*", default=[],
                    help="P-1 C 生成侧门禁：含 [draft] 行的日志文件（可多个）")
    ap.add_argument("--max-gen-dash-pct", type=float, default=5.0,
                    help="生成侧破折号率上限（默认 5%%；后处理兜到 0）")
    args = ap.parse_args(argv)
    if args.draft_log:
        lines: List[str] = []
        for f in args.draft_log:
            p = Path(f)
            if not p.exists():
                print(f"[outbound-style-gate] draft log not found: {p}")
                return 2
            lines += p.read_text(encoding="utf-8", errors="replace").splitlines()
        g = gen_side_stats(lines)
        print(f"[outbound-style-gate] generation-side drafts={g['total']} with_dash={g['dash']} "
              f"dash_rate={g['dash_rate_pct']}% (max {args.max_gen_dash_pct}%) claim_rewrite={g['claim_rewrite']}")
        if int(g["total"]) < GEN_DASH_MIN_SAMPLES:  # type: ignore[call-overload]
            print(f"  !! not enough [draft] samples (<{GEN_DASH_MIN_SAMPLES}); load the build and let it draft first")
            return 3
        return 0 if float(g["dash_rate_pct"]) < float(args.max_gen_dash_pct) else 1  # type: ignore[arg-type]
    samples = build_samples(max(1, int(args.n)))
    if args.input:
        p = Path(args.input)
        if not p.exists():
            print(f"[outbound-style-gate] input not found: {p}")
            return 2
        samples += load_samples(p)
    res = run_gate(samples, show=int(args.show))
    bad = res["bad"]  # type: ignore[assignment]
    agg = res["agg"]  # type: ignore[assignment]
    print(f"[outbound-style-gate] samples={res['total']} em_dash=0? semicolon=0? summary_tail=0? "
          f"tag_question=0? -> violations={len(bad)} | fixes punct={agg['punct_fix']} "  # type: ignore[index]
          f"style={agg['style_fix']} trimmed={agg['trimmed']}")  # type: ignore[index]
    for b in bad[:20]:  # type: ignore[index]
        print(f"  !! [{b['lang']}] {b['violations']} {b['out']!r}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
