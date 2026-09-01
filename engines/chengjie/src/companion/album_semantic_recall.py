"""相册语义召回 · 影子模式（实施90 阶段二，默认关）。

动机：触发词是**字面包含**匹配——客户说「让我看看你在哪」「有没有生活照」
不含任何触发词就 miss，素材明明有货却走了生成/文字。语义召回把客户话与素材
「AI 描述 + 触发词」做 bge-m3 余弦，**影子档**只记录「如果开了会召回哪张、
几分」——绝不真发、绝不阻塞回复链（miss 后 fire-and-forget 后台线程，嵌入
调用/配置读取全部发生在线程里）。读数攒够（jsonl 复核命中质量）再决定升真发。

配置 ``companion.selfie.album_ai.semantic_recall``（新子系统默认关）：
  ``{enabled: false, min_cosine: 0.62, max_candidates: 40}``
``enabled`` 只开**影子**；真发档将来另立键（防手滑把没验证过的召回直接上线）。

持久读数＝``logs/album_recall_shadow/YYYYMMDD.jsonl``（相对路径落**实例数据根**
——服务进程 CWD 契约，与 care_shadow 同哲学：进程计数重启清零，JSONL 才是
周审口径）。观测快照经 ``/api/workspace/metrics.persona_media.semantic_shadow``。
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_LOG_DIR = "logs/album_recall_shadow"   # C 类数据相对路径：落实例数据根恰是想要的

_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {
    "checked": 0, "would_hit": 0, "miss": 0, "errors": 0,
    "last_ts": 0.0, "last_hit": None,
}
_CFG_CACHE: Dict[str, Any] = {"ts": 0.0, "cfg": None}
_CFG_TTL_SEC = 30.0


def resolve_semantic_recall_cfg(cfg: Any) -> Dict[str, Any]:
    """``companion.selfie.album_ai.semantic_recall`` → 归一化配置（默认关）。"""
    c = cfg if isinstance(cfg, dict) else {}
    sel = ((c.get("companion") or {}).get("selfie") or {}) if isinstance(
        c.get("companion"), dict) else {}
    a = sel.get("album_ai") if isinstance(sel.get("album_ai"), dict) else {}
    s = a.get("semantic_recall") if isinstance(
        a.get("semantic_recall"), dict) else {}
    try:
        min_cos = float(s.get("min_cosine", 0.62) or 0.62)
    except (TypeError, ValueError):
        min_cos = 0.62
    try:
        max_cand = int(s.get("max_candidates", 40) or 40)
    except (TypeError, ValueError):
        max_cand = 40
    return {
        "enabled": bool(s.get("enabled", False)),
        "min_cosine": min(0.99, max(0.1, min_cos)),
        "max_candidates": max(1, min(200, max_cand)),
    }


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（纯函数）；空/长度不齐/零范数 → 0.0。"""
    try:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na <= 0 or nb <= 0:
            return 0.0
        return dot / (na * nb)
    except Exception:
        return 0.0


def candidate_text(row: Optional[Dict[str, Any]]) -> str:
    """素材的语义索引文本：AI 描述 + 触发词 + 场景标签值（缺一不慌，全空=不参赛）。"""
    r = row or {}
    parts: List[str] = []
    am = r.get("auto_meta") if isinstance(r.get("auto_meta"), dict) else {}
    desc = str(am.get("desc") or "").strip()
    if desc:
        parts.append(desc)
    trg = [str(t).strip() for t in (r.get("triggers") or []) if str(t).strip()]
    if trg:
        parts.append(" ".join(trg))
    for t in (r.get("tags") or []):
        ts = str(t or "")
        if ts.startswith("scene:"):
            parts.append(ts[6:])
    return " ".join(parts).strip()


def _bump(key: str, hit: Optional[Dict[str, Any]] = None) -> None:
    with _LOCK:
        _STATS[key] = int(_STATS.get(key) or 0) + 1
        _STATS["last_ts"] = time.time()
        if hit is not None:
            _STATS["last_hit"] = hit


def snapshot() -> Dict[str, Any]:
    with _LOCK:
        out = dict(_STATS)
    out["active"] = bool(out.get("checked"))
    return out


def reset_for_tests() -> None:
    with _LOCK:
        for k in ("checked", "would_hit", "miss", "errors"):
            _STATS[k] = 0
        _STATS["last_ts"] = 0.0
        _STATS["last_hit"] = None
        _CFG_CACHE["ts"], _CFG_CACHE["cfg"] = 0.0, None


def _runtime_cfg() -> Dict[str, Any]:
    """线程内读实时配置（30s TTL 缓存；读不到=默认关，绝不抛）。"""
    now = time.time()
    with _LOCK:
        if _CFG_CACHE["cfg"] is not None and now - _CFG_CACHE["ts"] < _CFG_TTL_SEC:
            return dict(_CFG_CACHE["cfg"])
    cfg: Dict[str, Any] = resolve_semantic_recall_cfg(None)
    try:
        from src.utils.config_manager import ConfigManager
        cfg = resolve_semantic_recall_cfg(
            getattr(ConfigManager(), "config", None) or {})
    except Exception:
        pass
    with _LOCK:
        _CFG_CACHE["ts"], _CFG_CACHE["cfg"] = now, dict(cfg)
    return cfg


def _default_embed(texts: List[str]) -> Optional[List[List[float]]]:
    """默认嵌入实现：懒加载 AIClient.embed（异步 → 线程内自建事件循环）。"""
    try:
        import asyncio

        from src.ai.ai_client import AIClient
        from src.utils.config_manager import ConfigManager
        client = AIClient(ConfigManager())
        return asyncio.run(client.embed(list(texts)))
    except Exception:
        logger.debug("[album_recall] 嵌入调用失败", exc_info=True)
        return None


def _log_line(payload: Dict[str, Any], log_dir: str) -> None:
    try:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        fname = d / (time.strftime("%Y%m%d") + ".jsonl")
        with open(fname, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("[album_recall] shadow jsonl 写入失败（已忽略）",
                     exc_info=True)


def shadow_check(
    rows: Sequence[Dict[str, Any]], persona_id: str, text: str, *,
    cfg: Dict[str, Any],
    embed_fn: Optional[Callable[[List[str]], Optional[List[List[float]]]]] = None,
    log_dir: str = _LOG_DIR,
    now: Optional[float] = None,
) -> Optional[Tuple[str, float]]:
    """同步影子判定（线程体/测试直调）。返回 (会召回的 media_id, 余弦) 或 None。"""
    if not cfg.get("enabled"):
        return None
    t = str(text or "").strip()
    if len(t) < 2:
        return None
    cands: List[Tuple[str, str, str]] = []   # (id, index_text, desc)
    for r in rows or []:
        it = candidate_text(r)
        if it:
            cands.append((str(r.get("id")), it,
                          str((r.get("auto_meta") or {}).get("desc") or "")))
        if len(cands) >= int(cfg.get("max_candidates") or 40):
            break
    if not cands:
        return None
    _bump("checked")
    fn = embed_fn or _default_embed
    try:
        vecs = fn([t] + [c[1] for c in cands])
    except Exception:
        vecs = None
    if not vecs or len(vecs) != len(cands) + 1:
        _bump("errors")
        return None
    qv = vecs[0]
    best_id, best_cos, best_desc = "", -1.0, ""
    for (cid, _it, desc), cv in zip(cands, vecs[1:]):
        c = cosine(qv, cv)
        if c > best_cos:
            best_id, best_cos, best_desc = cid, c, desc
    ts = float(now if now is not None else time.time())
    if best_id and best_cos >= float(cfg.get("min_cosine") or 0.62):
        hit = {"ts": ts, "persona": str(persona_id or ""),
               "text": t[:80], "media_id": best_id,
               "cosine": round(best_cos, 4), "desc": best_desc[:60]}
        _bump("would_hit", hit)
        _log_line(hit, log_dir)
        return best_id, best_cos
    _bump("miss")
    return None


def maybe_shadow(store: Any, persona_id: str, text: str) -> bool:
    """触发词 miss 后的影子入口（fire-and-forget，热路零成本）。

    线程里才读配置/取行/打嵌入——默认关时线程秒退；任何异常吞掉。
    返回「是否起了线程」（观测/测试用，不代表命中）。
    """
    pid = str(persona_id or "").strip()
    t = str(text or "").strip()
    if store is None or not pid or len(t) < 2:
        return False

    def _job() -> None:
        try:
            cfg = _runtime_cfg()
            if not cfg.get("enabled"):
                return
            rows = store.list(pid, enabled_only=True)
            shadow_check(rows, pid, t, cfg=cfg)
        except Exception:
            logger.debug("[album_recall] 影子任务异常（已忽略）", exc_info=True)

    try:
        threading.Thread(target=_job, name="album-recall-shadow",
                         daemon=True).start()
        return True
    except Exception:
        return False


# ── 周审汇总（纯函数；CLI=tools/album_recall_report.py 薄壳）────────────────

def _percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def summarize_shadow_lines(
    lines: Sequence[Any], *, now: Optional[float] = None, days: int = 14,
    min_cosine: float = 0.62,
) -> Dict[str, Any]:
    """影子 jsonl 行 → 周审汇总（纯函数）。坏行安全跳过。

    判词哲学与 proactive_review 同族：读数不够就明说「继续攒」，绝不用小样本
    指导阈值；样本够了给的是「下一步人工动作」而不是自动改配置。
    """
    ts_now = float(now if now is not None else time.time())
    floor = ts_now - max(1, int(days)) * 86400
    rows: List[Dict[str, Any]] = []
    for ln in lines or []:
        if not isinstance(ln, dict):
            continue
        try:
            if float(ln.get("ts") or 0) >= floor and str(ln.get("media_id") or ""):
                rows.append(ln)
        except (TypeError, ValueError):
            continue
    by_persona: Dict[str, int] = {}
    days_seen = set()
    coss: List[float] = []
    for r in rows:
        pid = str(r.get("persona") or "?")
        by_persona[pid] = by_persona.get(pid, 0) + 1
        try:
            coss.append(float(r.get("cosine") or 0))
        except (TypeError, ValueError):
            coss.append(0.0)
        days_seen.add(time.strftime("%Y-%m-%d",
                                    time.localtime(float(r.get("ts") or 0))))
    coss_sorted = sorted(coss)
    top = sorted(rows, key=lambda r: -float(r.get("cosine") or 0))[:10]
    out: Dict[str, Any] = {
        "window_days": int(days),
        "n": len(rows),
        "by_persona": dict(sorted(by_persona.items(), key=lambda kv: -kv[1])),
        "distinct_media": len({str(r.get("media_id")) for r in rows}),
        "days_covered": len(days_seen),
        "cosine": {
            "min": round(coss_sorted[0], 4) if coss_sorted else 0,
            "p50": round(_percentile(coss_sorted, 0.5), 4),
            "p90": round(_percentile(coss_sorted, 0.9), 4),
            "max": round(coss_sorted[-1], 4) if coss_sorted else 0,
        },
        "top": [{"text": str(r.get("text") or ""),
                 "media_id": str(r.get("media_id") or ""),
                 "desc": str(r.get("desc") or ""),
                 "cosine": float(r.get("cosine") or 0)} for r in top],
    }
    verdict: List[str] = []
    if not rows:
        verdict.append("窗口内零命中——影子未开启，或触发词已覆盖客户问法（本身也是结论）")
    elif len(rows) < 20:
        verdict.append(f"样本不足（{len(rows)}<20）：继续攒，勿据此调阈值/升真发")
    else:
        p50 = out["cosine"]["p50"]
        if p50 >= min_cosine + 0.10:
            verdict.append(
                f"命中余弦整体偏高（p50={p50}，阈值 {min_cosine}）——人工抽核 top 样本"
                "语义是否真对口，合格再谈升真发档")
        elif p50 < min_cosine + 0.05:
            verdict.append(
                f"命中贴着阈值（p50={p50}≈{min_cosine}）——低分段大概率噪声，"
                "先人工复核再考虑提高 min_cosine")
        total = len(rows)
        top_pid, top_n = next(iter(out["by_persona"].items()))
        if total and top_n / total > 0.8:
            verdict.append(f"命中高度集中在 {top_pid}（{top_n}/{total}）——"
                           "先在该人设上人工复核，别全局放开")
    out["verdict"] = verdict
    return out


__all__ = [
    "resolve_semantic_recall_cfg", "cosine", "candidate_text",
    "shadow_check", "maybe_shadow", "snapshot", "reset_for_tests",
    "summarize_shadow_lines",
]
