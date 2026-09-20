# -*- coding: utf-8 -*-
"""ASR 金标集策展 CLI（ASR P2，2026-09-12）——把归档的真实客户语音变成评测样本。

背景：``run_eval --asr`` 评测轨已在（P1），但金标只有 1 条未核对的探针夹具，样本 <5 不裁决。
换模型（Qwen3-ASR A/B）之前必须先攒够人耳核对过的样本；本工具就是攒样本的路：

    # 1) 列最近 3 天入站语音（只读 inbox.db，带机器转写 / 时长 / 置信三档 / 是否已人工改正）
    python tools/asr_gold_curate.py list --days 3 --limit 40 [--platform whatsapp]

    # 2) 导出候选清单给人耳核对：每行 {audio, machine_text, ref:"", lang, ...}，
    #    人只需把听到的话填进 ref（机器转写对的直接抄过去）；已改正过的行 ref 预填改正文本
    python tools/asr_gold_curate.py export --days 7 --out tmp_diag/asr_candidates.jsonl

    # 3) 把填好 ref 的行导入清单（按音频 sha1 去重；ref 为空的行跳过）
    python tools/asr_gold_curate.py import tmp_diag/asr_candidates.jsonl [--manifest config/eval/asr_samples.jsonl]

    # 4) 单条追加
    python tools/asr_gold_curate.py add --audio D:\\...\\x.ogg --ref "你好呀今天怎么样" --lang zh

    # 5) 看清单现状（条数 / 语种 / 时长桶 / 未核对标签）
    python tools/asr_gold_curate.py status

**只读生产库**（``mode=ro`` URI），写只落仓内清单 ``config/eval/asr_samples.jsonl``（或 --manifest）。
多实例数据根按 ``scripts/_data_root`` 契约自动发现（``--data-root`` 可指定）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

DEFAULT_MANIFEST = _ENGINE_ROOT / "config" / "eval" / "asr_samples.jsonl"
_VOICE = ("voice", "audio")


def _sha1(path: str) -> str:
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _duration(path: str) -> Optional[float]:
    try:
        from src.ai.audio_pipeline_intake import probe_audio_file
        d = probe_audio_file(path, use_ffprobe=True).get("duration_sec")
        return float(d) if isinstance(d, (int, float)) and d > 0 else None
    except Exception:
        return None


def _resolve_media(media_ref: str, root: Path) -> str:
    """media_ref（/static/protocol_media/... 或本地路径）→ 本机文件路径；解析不到 → ""。"""
    ref = str(media_ref or "").strip()
    if not ref:
        return ""
    if os.path.isfile(ref):
        return os.path.abspath(ref)
    try:
        from src.integrations.protocol_bridge import static_media_ref_to_path
        p = static_media_ref_to_path(ref)
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    except Exception:
        pass
    # 数据根下的 /static/protocol_media/<plat>/<file> 形态（工具从别的 CWD 跑时的兜底）
    marker = "/static/"
    if marker in ref:
        tail = ref.split(marker, 1)[1].replace("/", os.sep)
        for cand in (root / tail, root / "static" / tail):
            if cand.is_file():
                return str(cand.resolve())
        # protocol_media 直接挂在数据根
        if tail.startswith("protocol_media" + os.sep):
            cand = root / tail
            if cand.is_file():
                return str(cand.resolve())
    return ""


def list_voice_rows(db_path: Path, *, days: float = 3.0, limit: int = 50,
                    platform: str = "", now: Optional[float] = None) -> List[Dict[str, Any]]:
    """只读扫 inbox.db：最近 N 天入站语音/音频行 + asr_meta KV（同库 app_settings）。"""
    if not db_path.is_file():
        return []
    t_now = float(now if now is not None else time.time())
    since = t_now - float(days) * 86400.0
    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        sql = ("SELECT message_id, conversation_id, platform_msg_id, text, source_lang, media_type, "
               "media_ref, ts FROM messages WHERE direction='in' AND media_type IN ('voice','audio') "
               "AND ts >= ? ")
        params: List[Any] = [since]
        if platform:
            sql += "AND conversation_id LIKE ? "
            params.append(f"{platform.lower()}:%")
        sql += "ORDER BY ts DESC LIMIT ?"
        params.append(max(1, min(2000, int(limit))))
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        meta: Dict[str, Dict[str, Any]] = {}
        try:
            for r in con.execute("SELECT skey, sval FROM app_settings WHERE skey LIKE 'asr_meta:%'"):
                try:
                    meta[str(r["skey"])] = json.loads(str(r["sval"] or "{}"))
                except Exception:
                    continue
        except sqlite3.Error:
            meta = {}
    finally:
        con.close()
    out: List[Dict[str, Any]] = []
    for r in rows:
        cid = str(r.get("conversation_id") or "")
        m = meta.get(f"asr_meta:{cid}:{r.get('message_id')}") or \
            meta.get(f"asr_meta:{cid}:{r.get('platform_msg_id')}") or {}
        out.append({
            "message_id": str(r.get("message_id") or ""),
            "conversation_id": cid,
            "platform": cid.split(":", 1)[0] if ":" in cid else "",
            "ts": float(r.get("ts") or 0),
            "machine_text": str(r.get("text") or ""),
            "lang": str(r.get("source_lang") or ""),
            "media_ref": str(r.get("media_ref") or ""),
            "suspect": str(m.get("suspect") or ""),
            "corrected": bool(m.get("corrected")),
            "corrected_text": str(m.get("corrected_text") or ""),
            "language_probability": m.get("language_probability"),
            "avg_logprob": m.get("avg_logprob"),
        })
    return out


def build_candidates(rows: Iterable[Dict[str, Any]], root: Path) -> List[Dict[str, Any]]:
    """行 → 候选（补音频路径 / 时长 / sha1 / 置信三档；音频不在的行标 missing）。"""
    try:
        from src.inbox.asr_meta import confidence_label
    except Exception:  # pragma: no cover
        def confidence_label(_m):  # type: ignore
            return ""
    out: List[Dict[str, Any]] = []
    for r in rows:
        audio = _resolve_media(r.get("media_ref", ""), root)
        c = dict(r)
        c["audio"] = audio
        c["audio_missing"] = not bool(audio)
        c["duration"] = _duration(audio) if audio else None
        c["audio_sha1"] = _sha1(audio) if audio else ""
        c["confidence"] = confidence_label({
            "suspect": r.get("suspect"), "language_probability": r.get("language_probability"),
            "avg_logprob": r.get("avg_logprob")})
        # ref 预填：已人工改正 → 改正文本（可信）；否则留空让人耳填（机器对的直接抄）
        c["ref"] = c.get("corrected_text") if c.get("corrected") else ""
        c["lang"] = _norm_lang(c.get("lang", ""))
        out.append(c)
    return out


def _norm_lang(lang: str) -> str:
    low = str(lang or "").strip().lower()
    if not low or low == "unknown":
        return ""
    return low.split("-")[0] if low not in ("zh-tw", "yue") else low


def load_manifest(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                out.append(d)
        except Exception:
            continue
    return out


def manifest_sha1_index(path: Path) -> Dict[str, str]:
    """清单里已有样本的 音频 sha1 → audio 字段（去重用；相对路径按清单目录解析）。"""
    idx: Dict[str, str] = {}
    for d in load_manifest(path):
        a = str(d.get("audio") or "")
        p = Path(a)
        if not p.is_absolute():
            p = path.parent / p
        if p.is_file():
            h = _sha1(str(p))
            if h:
                idx[h] = a
    return idx


def append_samples(path: Path, samples: Iterable[Dict[str, Any]], *, tags: Optional[List[str]] = None) -> Dict[str, int]:
    """把 ``ref`` 非空的候选追加进清单（sha1 去重）；返回 {added, skipped_dup, skipped_noref, skipped_missing}。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = manifest_sha1_index(path)
    stats = {"added": 0, "skipped_dup": 0, "skipped_noref": 0, "skipped_missing": 0}
    lines: List[str] = []
    for s in samples:
        ref = str(s.get("ref") or "").strip()
        audio = str(s.get("audio") or "").strip()
        if not ref:
            stats["skipped_noref"] += 1
            continue
        if not audio or not os.path.isfile(audio):
            stats["skipped_missing"] += 1
            continue
        h = str(s.get("audio_sha1") or "") or _sha1(audio)
        if h and h in existing:
            stats["skipped_dup"] += 1
            continue
        rec = {
            "audio": audio, "ref": ref, "lang": _norm_lang(str(s.get("lang") or "")),
            "tags": sorted(set((tags or ["curated"]) + (["corrected"] if s.get("corrected") else []))),
            "note": (f"machine: {str(s.get('machine_text') or '')[:80]}" if s.get("machine_text") else ""),
            "source_conversation": str(s.get("conversation_id") or ""),
            "added_at": time.strftime("%Y-%m-%d"),
        }
        lines.append(json.dumps(rec, ensure_ascii=False))
        if h:
            existing[h] = audio
        stats["added"] += 1
    if lines:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return stats


def manifest_status(path: Path) -> Dict[str, Any]:
    from collections import Counter
    rows = load_manifest(path)
    by_lang: Counter = Counter()
    by_bucket: Counter = Counter()
    unverified = 0
    missing = 0
    try:
        from src.eval.asr_eval import duration_bucket
    except Exception:  # pragma: no cover
        duration_bucket = lambda s: "unknown"  # type: ignore  # noqa: E731
    for d in rows:
        by_lang[str(d.get("lang") or "?")] += 1
        tags = d.get("tags") if isinstance(d.get("tags"), list) else []
        if "ref_unverified" in tags:
            unverified += 1
        a = str(d.get("audio") or "")
        p = Path(a) if Path(a).is_absolute() else path.parent / a
        if not p.is_file():
            missing += 1
            by_bucket["missing"] += 1
        else:
            by_bucket[duration_bucket(_duration(str(p)))] += 1
    return {"manifest": str(path), "n": len(rows), "by_lang": dict(by_lang),
            "by_bucket": dict(by_bucket), "ref_unverified": unverified, "audio_missing": missing,
            "enough_to_judge": (len(rows) - unverified - missing) >= 5}


def _data_root(cli: str) -> Path:
    try:
        from scripts._data_root import resolve_data_roots
        roots = resolve_data_roots(cli)
        return Path(roots[0]) if roots else _ENGINE_ROOT
    except Exception:
        return Path(cli) if cli else _ENGINE_ROOT


def _fmt_row(i: int, c: Dict[str, Any]) -> str:
    ts = time.strftime("%m-%d %H:%M", time.localtime(c.get("ts") or 0))
    dur = f"{c['duration']:.1f}s" if isinstance(c.get("duration"), (int, float)) else "?"
    conf = c.get("confidence") or "-"
    flag = "✎" if c.get("corrected") else (" " if not c.get("audio_missing") else "✗")
    txt = (c.get("corrected_text") if c.get("corrected") else c.get("machine_text")) or ""
    return f"{i:>3} {flag} {ts} {c.get('platform', ''):<9} {dur:>6} {conf:<6} {txt[:60]}"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ASR 金标集策展")
    ap.add_argument("--data-root", default="", help="实例数据根（缺省自动发现活跃实例）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="列最近入站语音（只读）")
    p_list.add_argument("--days", type=float, default=3.0)
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--platform", default="")
    p_exp = sub.add_parser("export", help="导出候选 JSONL 给人耳核对（填 ref）")
    p_exp.add_argument("--days", type=float, default=7.0)
    p_exp.add_argument("--limit", type=int, default=200)
    p_exp.add_argument("--platform", default="")
    p_exp.add_argument("--out", required=True)
    p_imp = sub.add_parser("import", help="把填好 ref 的候选导入清单")
    p_imp.add_argument("file")
    p_imp.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p_add = sub.add_parser("add", help="单条追加")
    p_add.add_argument("--audio", required=True)
    p_add.add_argument("--ref", required=True)
    p_add.add_argument("--lang", default="")
    p_add.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p_st = sub.add_parser("status", help="清单现状")
    p_st.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p_st.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd in ("list", "export"):
        root = _data_root(args.data_root)
        db = root / "config" / "inbox.db"
        rows = list_voice_rows(db, days=args.days, limit=args.limit, platform=args.platform)
        cands = build_candidates(rows, root)
        if args.cmd == "list":
            print(f"data root: {root}  inbox.db: {'ok' if db.is_file() else 'MISSING'}  rows={len(cands)}")
            print("  # ✎=已人工改正 ✗=音频不在   时间  平台  时长  置信  文本（改正优先）")
            for i, c in enumerate(cands, 1):
                print(_fmt_row(i, c))
            return 0
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            for c in cands:
                if c.get("audio_missing"):
                    continue
                f.write(json.dumps({
                    "audio": c["audio"], "ref": c.get("ref") or "", "lang": c.get("lang") or "",
                    "machine_text": c.get("machine_text") or "", "duration": c.get("duration"),
                    "confidence": c.get("confidence"), "suspect": c.get("suspect"),
                    "corrected": bool(c.get("corrected")), "conversation_id": c.get("conversation_id"),
                    "message_id": c.get("message_id"), "audio_sha1": c.get("audio_sha1"),
                }, ensure_ascii=False) + "\n")
        n_pre = sum(1 for c in cands if c.get("ref"))
        print(f"exported {sum(1 for c in cands if not c.get('audio_missing'))} candidates → {out} "
              f"（ref 已预填 {n_pre} 条＝人工改正过；其余请人耳核对后填 ref）")
        return 0
    if args.cmd == "import":
        cands = load_manifest(Path(args.file))
        stats = append_samples(Path(args.manifest), cands)
        print(f"import → {args.manifest}: {stats}")
        return 0
    if args.cmd == "add":
        stats = append_samples(Path(args.manifest), [{"audio": os.path.abspath(args.audio), "ref": args.ref,
                                                      "lang": args.lang}])
        print(f"add → {args.manifest}: {stats}")
        return 0 if stats["added"] else 1
    if args.cmd == "status":
        st = manifest_status(Path(args.manifest))
        if args.json:
            print(json.dumps(st, ensure_ascii=False, indent=2))
        else:
            print(f"{st['manifest']}: n={st['n']} by_lang={st['by_lang']} by_bucket={st['by_bucket']} "
                  f"ref_unverified={st['ref_unverified']} audio_missing={st['audio_missing']} "
                  f"→ {'可裁决' if st['enough_to_judge'] else '样本不足（需 ≥5 条核对过且音频在）'}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
