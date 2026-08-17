# -*- coding: utf-8 -*-
"""聊天新鲜度观测报告（只读 CLI，2026-08-03）。

「2026-08-02 体检修的那批（分条发送 / 防复读 / 语音履约）到底有没有好转」
从翻库手查变成一条命令：

    python tools/chat_freshness_report.py [--days 7] [--data-root PATH]
                                          [--scene-words 抹茶,咖啡]
                                          [--json]

数据源：``<data_root>/config/inbox.db`` 的 messages 表，**只读**
（``mode=ro`` URI，对活体生产库零写风险）；多实例机自动逐根出报告
（``scripts/_data_root.py`` 契约）。库不存在 / 表缺列 → 人话报错 exit 2。

统计口径（与 ``--days`` 无关的常量口径，输出里同样标注）：
- 出站回合：同会话**连续出站**（中间无入站打断）且相邻间隔 ≤120s 记同一回合；
- 语音履约窗：入站请求后 **10 分钟**内同会话出站 voice/audio 算兑现；
- 基线对照：2026-08-02 体检值（14 天窗），硬编码于 ``BASELINE``，
  仅作方向对照——与本次 ``--days`` 窗口不同宽时比"率"不比"量"。

统计逻辑全部是可导入纯函数（吃 rows 列表返回 dict），CLI 只做取数和渲染；
门禁 ``tests/test_chat_freshness_report.py`` 用构造 rows 测每个纯函数。
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

# ── 2026-08-02 体检基线（硬编码对照值；14 天窗）──────────────────────────────
BASELINE = {
    "window_days": 14,           # 体检窗口宽度
    "burst_single_pct": 91.0,    # 出站回合 91% 单条
    "voice_requests": 32,        # 语音请求 32 次
    "voice_fulfilled": 6,        # 10 分钟内兑现 6 次
    "voice_fulfill_pct": 18.75,  # 履约率
    "haha_msgs": 327,            # 「哈哈」条数
    "out_total": 1106,           # 出站总条数
    "matcha_count": 29,          # 「抹茶」出站条数
}

DEFAULT_SCENE_WORDS = ("抹茶", "咖啡", "手冲", "便利店", "关东煮")

BURST_GAP_SEC = 120.0     # 回合切分：相邻出站间隔 ≤120s 记同一回合
VOICE_WINDOW_SEC = 600.0  # 语音履约窗：请求后 10 分钟

# 入站语音请求（任务口径正则）
VOICE_REQ_RE = re.compile(r"发.{0,3}语音|想听.*声音|唱.*歌|语音.{0,2}(呗|吧)")
# 哈哈家族——口径对齐 src/ai/reply_variety._LAUGH_RE（2026-08-02）；本工具
# 刻意持有本地拷贝：趋势报告要求口径跨周稳定（与 proactive_review 独立实现
# SQL 口径同一模式）。
LAUGH_RE = re.compile(
    r"(?i)(哈{2,}|嘿{2,}|呵{2,}|嘻{2,}|h{3,}|(?:a?ha){2,}h?|lo+l+|lmao+|23{2,})")
# 句尾语气字符（任务口径：～/~/呀/啦/呢/哦/嘛 结尾）；判定前先剥句terminal
# 标点（「好呀！」也算语气尾），～/~ 本身是语气不剥。
TAIL_CHARS = "～~呀啦呢哦嘛"
_TERMINAL_PUNCT = "。！？!?…．."
# 记忆回带（任务口径正则，中英）
MEMORY_RE = re.compile(
    r"上次你说|你上次|之前你说|你之前提到|你说过"
    r"|you mentioned|last time you said", re.IGNORECASE)
VOICE_MEDIA = ("voice", "audio")

# 复读归一：小写后只保留词字符（\w 含中日韩），去空白/标点/emoji
_WORD_RE = re.compile(r"\w+")
# 媒体占位行（存量镜像格式「[语音]×N」「[图片]」，归一化后剩「语音N/图片」）
# 不算复读——那是收件箱镜像记法不是文案；带配文的媒体行**剥掉前缀标记**后
# 配文照常参与，且与同句纯文本合并计数
# （真库首跑实锤：「语音」×81 霸榜，把真正的配文复读挤出 Top）。
_MEDIA_PLACEHOLDER_RE = re.compile(r"^(语音|图片|视频)\d*$")
_MEDIA_MARK_PREFIX_RE = re.compile(r"^\s*\[(语音|图片|视频)\](\s*[×xX]\s*\d+)?\s*")


def _norm_text(text: str) -> str:
    return "".join(_WORD_RE.findall(str(text or "").lower()))


# 测试/验收消息排除（2026-08-03）：生产库里混有「[验收QA103…]」类测试消息（体检
# 实锤 ×9），会污染复读榜与占比口径。默认只剔**带标记括号**的形态（[验收…/[QA…/
# [测试…）——句首恰好说「验收/测试」的正常聊天不误伤；--exclude-pattern 可覆写
# （空串=不剔除）。
DEFAULT_EXCLUDE_PATTERN = r"^\[(验收|QA|测试)"


def filter_test_rows(
    rows: Iterable[Dict[str, Any]], pattern: str = DEFAULT_EXCLUDE_PATTERN,
) -> List[Dict[str, Any]]:
    """剔除命中测试标记的消息行（纯函数；pattern 空/非法 → 原样返回）。"""
    items = list(rows or [])
    pat = str(pattern or "").strip()
    if not pat:
        return items
    try:
        rx = re.compile(pat)
    except re.error:
        return items
    return [r for r in items
            if not rx.search(str((r or {}).get("text") or "").strip())]


def trend_line(rep: Dict[str, Any]) -> Dict[str, Any]:
    """把单份报告压成一行 JSONL 趋势摘要（周批追加，跨周看走势）。"""
    b = rep.get("bursts") or {}
    v = rep.get("voice") or {}
    c = rep.get("catchphrase") or {}
    mm = rep.get("memory") or {}
    rp = rep.get("repeats") or {}
    top_repeat = ""
    full = rp.get("full") or []
    if full:
        try:
            top_repeat = f"x{full[0][1]}:{str(full[0][0])[:24]}"
        except Exception:
            top_repeat = ""
    return {
        "ts": float(rep.get("ts") or 0.0),
        "date": time.strftime(
            "%Y-%m-%d", time.localtime(float(rep.get("ts") or 0.0) or None)),
        "days": float(rep.get("days") or 0.0),
        "data_root": str(rep.get("data_root") or ""),
        "out_total": int(c.get("out_total") or 0),
        "burst_single_pct": float(b.get("pct_1") or 0.0),
        "voice_requests": int(v.get("requests") or 0),
        "voice_fulfilled": int(v.get("fulfilled") or 0),
        "voice_rate_pct": float(v.get("rate_pct") or 0.0),
        "haha_per_100": float(c.get("haha_per_100") or 0.0),
        "tail_per_100": float(c.get("tail_per_100") or 0.0),
        "scene_words": dict(rep.get("scene_words") or {}),
        "memory_pct": float(mm.get("pct") or 0.0),
        "top_repeat": top_repeat,
    }


def _rows_by_conv(rows: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """按会话分组并按 ts 升序（稳定排序，同 ts 保输入序）。"""
    by: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows or []:
        by.setdefault(str(r.get("conversation_id") or ""), []).append(r)
    for msgs in by.values():
        msgs.sort(key=lambda m: float(m.get("ts") or 0.0))
    return by


def _out_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in (rows or [])
            if str(r.get("direction") or "") == "out"]


# ── 1) 出站回合分条分布 ──────────────────────────────────────────────────────
def split_outbound_bursts(
    rows: Iterable[Dict[str, Any]], gap_sec: float = BURST_GAP_SEC,
) -> Dict[str, Any]:
    """出站回合分条分布（基线：91% 单条）。

    回合口径：同会话内**连续出站**（中间被入站打断即分回合）且相邻出站间隔
    ≤``gap_sec`` 记同一回合。返回 1/2/3+ 条回合的计数与占比。
    """
    sizes: List[int] = []
    for msgs in _rows_by_conv(rows).values():
        cur = 0
        last_out_ts = 0.0
        for m in msgs:
            if str(m.get("direction") or "") == "out":
                ts = float(m.get("ts") or 0.0)
                if cur > 0 and (ts - last_out_ts) <= float(gap_sec):
                    cur += 1
                else:
                    if cur > 0:
                        sizes.append(cur)
                    cur = 1
                last_out_ts = ts
            else:  # 入站打断 → 收口当前回合
                if cur > 0:
                    sizes.append(cur)
                cur = 0
        if cur > 0:
            sizes.append(cur)
    n = len(sizes)
    c1 = sum(1 for s in sizes if s == 1)
    c2 = sum(1 for s in sizes if s == 2)
    c3 = sum(1 for s in sizes if s >= 3)

    def _pct(c: int) -> float:
        return round(c * 100.0 / n, 1) if n else 0.0

    return {
        "rounds": n, "size_1": c1, "size_2": c2, "size_3plus": c3,
        "pct_1": _pct(c1), "pct_2": _pct(c2), "pct_3plus": _pct(c3),
    }


# ── 2) 语音履约 ──────────────────────────────────────────────────────────────
def voice_fulfillment(
    rows: Iterable[Dict[str, Any]], window_sec: float = VOICE_WINDOW_SEC,
) -> Dict[str, Any]:
    """入站语音请求 → 同会话 ``window_sec`` 内出站 voice/audio 的兑现率
    （基线 18.75%＝32 请求 6 兑现）+ 最后一次出站语音时间（窗口内）+
    出站媒体类型分布。
    """
    requests = 0
    fulfilled = 0
    last_voice_ts = 0.0
    media_dist: Counter = Counter()
    for msgs in _rows_by_conv(rows).values():
        voice_ts = []
        for m in msgs:
            if str(m.get("direction") or "") != "out":
                continue
            mt = str(m.get("media_type") or "")
            media_dist[mt or "text"] += 1
            if mt in VOICE_MEDIA:
                ts = float(m.get("ts") or 0.0)
                voice_ts.append(ts)
                last_voice_ts = max(last_voice_ts, ts)
        for m in msgs:
            if str(m.get("direction") or "") != "in":
                continue
            if not VOICE_REQ_RE.search(str(m.get("text") or "")):
                continue
            requests += 1
            req_ts = float(m.get("ts") or 0.0)
            if any(0 < (v - req_ts) <= float(window_sec) for v in voice_ts):
                fulfilled += 1
    return {
        "requests": requests, "fulfilled": fulfilled,
        "rate_pct": round(fulfilled * 100.0 / requests, 1) if requests else 0.0,
        "last_voice_ts": last_voice_ts,
        "media_dist": dict(media_dist),
    }


# ── 3) 口头禅密度 ────────────────────────────────────────────────────────────
def catchphrase_density(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """每百条出站的「哈哈家族」条数与句尾语气条数（按消息条数计，同条多个
    哈哈只算 1；基线：哈哈 327 条/1106 条出站）。"""
    outs = _out_rows(rows)
    total = len(outs)
    haha = 0
    tail = 0
    for r in outs:
        text = str(r.get("text") or "")
        if LAUGH_RE.search(text):
            haha += 1
        t = text.strip().rstrip(_TERMINAL_PUNCT).rstrip()
        if t and t[-1] in TAIL_CHARS:
            tail += 1

    def _per100(c: int) -> float:
        return round(c * 100.0 / total, 1) if total else 0.0

    return {
        "out_total": total,
        "haha_msgs": haha, "haha_per_100": _per100(haha),
        "tail_msgs": tail, "tail_per_100": _per100(tail),
    }


# ── 4) 场景词计数 ────────────────────────────────────────────────────────────
def scene_word_counts(
    rows: Iterable[Dict[str, Any]], words: Sequence[str],
) -> Dict[str, int]:
    """逐词出站条数（按消息条数计；基线：抹茶 29 条/14 天）。"""
    outs = _out_rows(rows)
    out: Dict[str, int] = {}
    for w in words or ():
        word = str(w or "").strip()
        if not word:
            continue
        out[word] = sum(
            1 for r in outs if word in str(r.get("text") or ""))
    return out


# ── 5) 复读探测 ──────────────────────────────────────────────────────────────
def repeat_detection(
    rows: Iterable[Dict[str, Any]], top_k: int = 8,
) -> Dict[str, Any]:
    """出站复读 Top：归一化整句（小写 + 去空白/标点/emoji）重复 ≥2 的 Top-K
    + 开头 8 字（归一化后前 8 字符，仅长度足够的消息参与）重复 ≥2 的 Top-K。
    媒体占位行（[语音]×N / [图片]）剔除，媒体配文照常参与。"""
    full: Counter = Counter()
    heads: Counter = Counter()
    for r in _out_rows(rows):
        raw = _MEDIA_MARK_PREFIX_RE.sub("", str(r.get("text") or ""))
        norm = _norm_text(raw)
        if _MEDIA_PLACEHOLDER_RE.match(norm):
            continue
        if len(norm) >= 2:
            full[norm] += 1
        if len(norm) >= 8:
            heads[norm[:8]] += 1

    def _top(c: Counter) -> List[Tuple[str, int]]:
        pairs = [(t, n) for t, n in c.items() if n >= 2]
        pairs.sort(key=lambda p: (-p[1], p[0]))
        return pairs[:max(1, int(top_k))]

    return {"full": _top(full), "heads": _top(heads)}


# ── 6) 记忆回带率 ────────────────────────────────────────────────────────────
def memory_callback(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """出站命中记忆回带句式（上次你说/你之前提到/you mentioned…）的条数与占比。"""
    outs = _out_rows(rows)
    hits = sum(
        1 for r in outs if MEMORY_RE.search(str(r.get("text") or "")))
    return {
        "out_total": len(outs), "hits": hits,
        "pct": round(hits * 100.0 / len(outs), 1) if outs else 0.0,
    }


# ── 汇总与渲染 ───────────────────────────────────────────────────────────────
def collect_freshness(
    rows: List[Dict[str, Any]],
    *,
    scene_words: Sequence[str] = DEFAULT_SCENE_WORDS,
) -> Dict[str, Any]:
    """rows（窗口内全量 in+out 消息）→ 六段指标 dict（纯函数，不碰库）。"""
    return {
        "bursts": split_outbound_bursts(rows),
        "voice": voice_fulfillment(rows),
        "catchphrase": catchphrase_density(rows),
        "scene_words": scene_word_counts(rows, scene_words),
        "repeats": repeat_detection(rows),
        "memory": memory_callback(rows),
        "baseline": dict(BASELINE),
    }


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "（窗口内无）"
    return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))


def render_report(rep: Dict[str, Any]) -> str:
    b = rep.get("bursts") or {}
    v = rep.get("voice") or {}
    c = rep.get("catchphrase") or {}
    sw = rep.get("scene_words") or {}
    rp = rep.get("repeats") or {}
    mm = rep.get("memory") or {}
    bl = rep.get("baseline") or BASELINE
    lines = [
        f"=== 聊天新鲜度报告（{rep.get('data_root', '?')}，"
        f"近 {rep.get('days', 0):.0f} 天，出站 {c.get('out_total', 0)} 条）===",
        f"口径：回合=同会话连续出站间隔≤{BURST_GAP_SEC:.0f}s（入站打断即分回合）；"
        f"语音履约窗=请求后 {VOICE_WINDOW_SEC/60:.0f} 分钟；",
        f"      基线=2026-08-02 体检（{bl['window_days']} 天窗，硬编码对照，"
        "与 --days 无关）。",
        "",
        "-- 1) 出站回合分条分布 --",
        f"  1 条/回合   {b.get('size_1', 0):>5}  {b.get('pct_1', 0):5.1f}%"
        f"   （体检基线 {bl['burst_single_pct']:.1f}% 单条）",
        f"  2 条/回合   {b.get('size_2', 0):>5}  {b.get('pct_2', 0):5.1f}%",
        f"  3+条/回合   {b.get('size_3plus', 0):>5}  {b.get('pct_3plus', 0):5.1f}%"
        f"   （回合总数 {b.get('rounds', 0)}）",
        "",
        "-- 2) 语音履约（10 分钟窗）--",
        f"  请求 {v.get('requests', 0)}  兑现 {v.get('fulfilled', 0)}"
        f"  {v.get('rate_pct', 0):.1f}%"
        f"   （体检基线 {bl['voice_fulfill_pct']:.2f}%＝"
        f"{bl['voice_requests']} 请求 {bl['voice_fulfilled']} 兑现）",
        f"  最后一次出站语音：{_fmt_ts(v.get('last_voice_ts') or 0)}",
        "  出站媒体分布：" + ("  ".join(
            f"{k}={n}" for k, n in sorted(
                (v.get("media_dist") or {}).items(),
                key=lambda kv: -kv[1])) or "（无出站）"),
        "",
        "-- 3) 口头禅密度（每百条出站）--",
        f"  哈哈家族        {c.get('haha_msgs', 0):>5} 条"
        f"  {c.get('haha_per_100', 0):5.1f}/百条"
        f"   （体检基线 {bl['haha_msgs']} 条/{bl['out_total']} 条"
        f" ≈ {bl['haha_msgs']*100.0/bl['out_total']:.1f}/百条）",
        f"  句尾语气(～~呀啦呢哦嘛) {c.get('tail_msgs', 0):>5} 条"
        f"  {c.get('tail_per_100', 0):5.1f}/百条",
        "",
        "-- 4) 场景词出站条数 --",
    ]
    for w, n in sw.items():
        base = (f"   （体检基线 {bl['matcha_count']} 条/{bl['window_days']} 天）"
                if w == "抹茶" else "")
        lines.append(f"  {w:<6} {n:>5} 条{base}")
    if not sw:
        lines.append("  （未指定场景词）")
    lines += ["", "-- 5) 复读探测 Top（归一化，×N=重复条数）--"]
    full = rp.get("full") or []
    heads = rp.get("heads") or []
    if full:
        for t, n in full:
            lines.append(f"  整句   ×{n:<3} {t[:40]}")
    else:
        lines.append("  整句   （无 ≥2 次的重复）")
    if heads:
        for t, n in heads:
            lines.append(f"  开头8字 ×{n:<3} {t}")
    else:
        lines.append("  开头8字 （无 ≥2 次的重复）")
    lines += [
        "",
        "-- 6) 记忆回带 --",
        f"  {mm.get('hits', 0)} 条  占出站 {mm.get('pct', 0):.1f}%"
        "   （上次你说/你之前提到/you mentioned…）",
    ]
    return "\n".join(lines)


# ── CLI（取数 + 渲染；统计不在这层）─────────────────────────────────────────
def _ro_conn(db_path: Path) -> sqlite3.Connection:
    uri = "file:%s?mode=ro" % str(db_path).replace("\\", "/")
    return sqlite3.connect(uri, uri=True)


def load_rows(db_path: Path, since_ts: float) -> List[Dict[str, Any]]:
    """窗口内全量消息行（只读；库/表/列问题原样抛给调用方转人话）。"""
    conn = _ro_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT conversation_id, direction, COALESCE(text,'') AS text, "
            "COALESCE(media_type,'') AS media_type, ts "
            "FROM messages WHERE ts >= ? ORDER BY ts", (since_ts,))
        return [
            {"conversation_id": r[0], "direction": r[1], "text": r[2],
             "media_type": r[3], "ts": r[4]}
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def _parse_scene_words(raw: str) -> List[str]:
    parts = re.split(r"[,，]", str(raw or ""))
    return [p.strip() for p in parts if p.strip()]


def _ensure_utf8_stdout() -> None:
    try:
        enc = (getattr(sys.stdout, "encoding", "") or "").lower()
        if enc.replace("-", "") != "utf8" and hasattr(sys.stdout, "buffer"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace",
                line_buffering=True)
    except Exception:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    _ensure_utf8_stdout()
    ap = argparse.ArgumentParser(description="聊天新鲜度观测报告（只读）")
    ap.add_argument("--data-root", default="", help="实例数据根（默认自动发现）")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--scene-words", default=",".join(DEFAULT_SCENE_WORDS),
                    help="逗号分隔的场景词表")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--exclude-pattern", default=DEFAULT_EXCLUDE_PATTERN,
                    help="剔除测试消息的正则（空串=不剔除）")
    ap.add_argument("--out-jsonl", default="",
                    help="趋势行追加落点（周批用，如 logs/eval/chat_freshness_trend.jsonl）")
    args = ap.parse_args(argv)

    from scripts._data_root import resolve_data_roots
    roots = resolve_data_roots(args.data_root)
    words = _parse_scene_words(args.scene_words)
    now = time.time()
    since = now - float(args.days) * 86400.0

    reports: List[Dict[str, Any]] = []
    failed = False
    for root in roots:
        db = Path(root) / "config" / "inbox.db"
        if not db.is_file():
            print(f"[错误] 找不到收件箱库：{db}（--data-root 指错了？）",
                  file=sys.stderr)
            failed = True
            continue
        try:
            rows = load_rows(db, since)
        except sqlite3.OperationalError as e:
            print(f"[错误] 读库失败（表/列缺失或库损坏）：{db}：{e}",
                  file=sys.stderr)
            failed = True
            continue
        rows = filter_test_rows(rows, args.exclude_pattern)
        rep = {"data_root": str(root), "days": float(args.days), "ts": now}
        rep.update(collect_freshness(rows, scene_words=words))
        reports.append(rep)

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for rep in reports:
            print(render_report(rep))
            print()

    if args.out_jsonl and reports:
        try:
            out = Path(args.out_jsonl)
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("a", encoding="utf-8") as fh:
                for rep in reports:
                    fh.write(json.dumps(
                        trend_line(rep), ensure_ascii=False) + "\n")
            print(f"[trend] 已追加 {len(reports)} 行 → {out}")
        except Exception as e:
            print(f"[警告] 趋势行写入失败：{e}", file=sys.stderr)
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
