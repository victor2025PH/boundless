# -*- coding: utf-8 -*-
"""对话可观测指标：线程安全的滚动统计存储（看板 v2 起全量持久化）。

记录每轮对话的 TTFA / 情绪分布 / 垫话命中 / 情绪参考命中 / LLM 延迟，
供 /api/metrics 聚合返回，/dashboard 实时展示，量化每次优化效果。

持久化（看板 v2，2026-07-06）：
  历史上只有 feedback 落 SQLite，turns/clone_scores/naturalness 全是内存 deque——
  Hub 一重启看板即失忆（满屏 0ms/暂无数据的根因）。现四类序列统一落
  data/metrics.db，启动回载最近窗口；turns 另带 day 列支撑按日趋势（daily_trend）。

清空语义（拆分，防数据事故）：
  reset(scope="window")            软清：只立窗口标记，视图归零、账本无损（默认）
  reset(scope="all")               硬清：turns/clone/naturalness 全删，好评账本保留
  reset(scope="all_with_feedback") 连听感评分一起删（唯一会动 feedback 的路径）
"""
from __future__ import annotations
import json
import os
import sqlite3
import threading, time
from collections import deque, Counter, defaultdict
from pathlib import Path
from urllib.parse import quote as _url_quote

import app_config

_LOCK = threading.Lock()
_FB_DB_PATH = Path(os.environ.get("CONV_METRICS_DB", str(app_config.DATA_DIR / "metrics.db")))
_FB_DB_LOAD_N = int(os.environ.get("CONV_FEEDBACK_LOAD_N", "500"))
_RETENTION_DAYS = int(os.environ.get("CONV_METRICS_RETENTION_DAYS", "90"))
_MAX = 300                       # 保留最近 N 轮明细
_turns: deque = deque(maxlen=_MAX)
_clone_scores: deque = deque(maxlen=100)   # 克隆相似度打分历史（趋势）
_naturalness: deque = deque(maxlen=100)    # 自然度（韵律）打分历史（趋势）
_feedback: deque = deque(maxlen=500)       # 用户听感评分（👍/👎）
_started_at = time.time()
_turns_alltime = 0               # 全时累计对话轮数（DB COUNT 起底 + 运行时自增）
_window_start = 0.0              # 软清窗口标记（meta 表持久化；0=无标记）

# 自然度回归告警：滑动窗口 + 绝对阈值 / 相对体检基线
_NAT_ALERT_WINDOW = int(os.environ.get("CONV_NAT_ALERT_WINDOW", "8"))
_NAT_ALERT_MIN_SAMPLES = int(os.environ.get("CONV_NAT_ALERT_MIN_SAMPLES", "3"))
_NAT_ALERT_FLOOR = float(os.environ.get("CONV_NAT_ALERT_FLOOR", "0.70"))
_NAT_ALERT_DROP = float(os.environ.get("CONV_NAT_ALERT_DROP", "0.05"))


def _day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(str(_FB_DB_PATH), check_same_thread=False)


def _init_db() -> None:
    """建全部表（feedback 沿用既有结构；turns/clone/naturalness/meta 为看板 v2 新增）+
    按保留期清老账（默认 90 天，CONV_METRICS_RETENTION_DAYS 可调）。"""
    try:
        _FB_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = _conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, profile TEXT, rating INTEGER, source TEXT,
                text_preview TEXT, reply_lang TEXT, msg_id INTEGER
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fb_ts ON feedback(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fb_prof ON feedback(profile)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS turns(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, day TEXT, profile TEXT,
                ttfa_ms INTEGER, perceived_ttfa_ms INTEGER, stt_ms INTEGER,
                first_sentence_ms INTEGER, llm_first_token_ms INTEGER,
                total_ms INTEGER, n_sentences INTEGER, filler INTEGER,
                emotions TEXT, emoref_hits INTEGER, emo_sentences INTEGER,
                cancelled INTEGER
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_day ON turns(day)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS clone_scores(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, day TEXT, profile TEXT, cosine REAL, label TEXT
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS naturalness(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, day TEXT, profile TEXT, naturalness REAL, label TEXT
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta(
                key TEXT PRIMARY KEY, value TEXT
            )""")
        cutoff = time.time() - _RETENTION_DAYS * 86400
        for tbl in ("turns", "clone_scores", "naturalness"):
            conn.execute(f"DELETE FROM {tbl} WHERE ts < ?", (cutoff,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _meta_get(key: str, default: str = "") -> str:
    try:
        conn = _conn()
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default


def _meta_set(key: str, value: str) -> None:
    try:
        conn = _conn()
        conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _meta_del(key: str) -> None:
    try:
        conn = _conn()
        conn.execute("DELETE FROM meta WHERE key=?", (key,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _persist_turn_row(t: dict) -> None:
    try:
        conn = _conn()
        conn.execute(
            "INSERT INTO turns(ts,day,profile,ttfa_ms,perceived_ttfa_ms,stt_ms,"
            "first_sentence_ms,llm_first_token_ms,total_ms,n_sentences,filler,"
            "emotions,emoref_hits,emo_sentences,cancelled) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t["ts"], _day_of(t["ts"]), t["profile"], t["ttfa_ms"], t["perceived_ttfa_ms"],
             t["stt_ms"], t["first_sentence_ms"], t["llm_first_token_ms"], t["total_ms"],
             t["n_sentences"], int(t["filler"]), json.dumps(t["emotions"], ensure_ascii=False),
             t["emoref_hits"], t["emo_sentences"], int(t["cancelled"])))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _persist_score_row(table: str, ts: float, profile: str, col: str,
                       val: float, label: str) -> None:
    try:
        conn = _conn()
        conn.execute(
            f"INSERT INTO {table}(ts,day,profile,{col},label) VALUES(?,?,?,?,?)",
            (ts, _day_of(ts), profile, val, label))
        conn.commit()
        conn.close()
    except Exception:
        pass


def record_clone_score(*, profile: str = "", cosine: float = 0.0,
                       label: str = "") -> None:
    row = {"ts": time.time(), "profile": profile or "",
           "cosine": round(float(cosine or 0.0), 4), "label": label or ""}
    with _LOCK:
        _clone_scores.append(row)
    _persist_score_row("clone_scores", row["ts"], row["profile"], "cosine",
                       row["cosine"], row["label"])


def record_naturalness(*, profile: str = "", naturalness: float = 0.0,
                       label: str = "") -> None:
    row = {"ts": time.time(), "profile": profile or "",
           "naturalness": round(float(naturalness or 0.0), 4), "label": label or ""}
    with _LOCK:
        _naturalness.append(row)
    _persist_score_row("naturalness", row["ts"], row["profile"], "naturalness",
                       row["naturalness"], row["label"])


def _persist_feedback_row(row: dict) -> None:
    try:
        conn = _conn()
        conn.execute(
            "INSERT INTO feedback(ts,profile,rating,source,text_preview,reply_lang,msg_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (row["ts"], row["profile"], row["rating"], row["source"],
             row["text_preview"], row["reply_lang"], row["msg_id"]))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _load_feedback_from_db() -> None:
    if not _FB_DB_PATH.exists():
        return
    try:
        conn = _conn()
        rows = conn.execute(
            "SELECT ts,profile,rating,source,text_preview,reply_lang,msg_id "
            f"FROM feedback ORDER BY id DESC LIMIT {_FB_DB_LOAD_N}"
        ).fetchall()
        conn.close()
        with _LOCK:
            _feedback.clear()
            for r in reversed(rows):
                _feedback.append({
                    "ts": r[0], "profile": r[1] or "", "rating": int(r[2]),
                    "source": r[3] or "", "text_preview": r[4] or "",
                    "reply_lang": r[5] or "", "msg_id": int(r[6] or 0),
                })
    except Exception:
        pass


def _load_series_from_db() -> None:
    """启动回载：turns/clone/naturalness 最近窗口 + 全时计数 + 软清标记。
    看板 v2 的核心承诺——Hub 重启后看板不再失忆。"""
    global _turns_alltime, _window_start
    if not _FB_DB_PATH.exists():
        return
    try:
        conn = _conn()
        t_rows = conn.execute(
            "SELECT ts,profile,ttfa_ms,perceived_ttfa_ms,stt_ms,first_sentence_ms,"
            "llm_first_token_ms,total_ms,n_sentences,filler,emotions,emoref_hits,"
            f"emo_sentences,cancelled FROM turns ORDER BY id DESC LIMIT {_MAX}"
        ).fetchall()
        c_rows = conn.execute(
            "SELECT ts,profile,cosine,label FROM clone_scores ORDER BY id DESC LIMIT 100"
        ).fetchall()
        n_rows = conn.execute(
            "SELECT ts,profile,naturalness,label FROM naturalness ORDER BY id DESC LIMIT 100"
        ).fetchall()
        cnt = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        conn.close()
        with _LOCK:
            _turns.clear()
            for r in reversed(t_rows):
                try:
                    emos = json.loads(r[10]) if r[10] else {}
                except Exception:
                    emos = {}
                _turns.append({
                    "ts": r[0], "profile": r[1] or "", "ttfa_ms": int(r[2] or 0),
                    "perceived_ttfa_ms": int(r[3] or 0), "stt_ms": int(r[4] or 0),
                    "first_sentence_ms": int(r[5] or 0),
                    "llm_first_token_ms": int(r[6] or 0), "total_ms": int(r[7] or 0),
                    "n_sentences": int(r[8] or 0), "filler": bool(r[9]),
                    "emotions": emos if isinstance(emos, dict) else {},
                    "emoref_hits": int(r[11] or 0), "emo_sentences": int(r[12] or 0),
                    "cancelled": bool(r[13]),
                })
            _clone_scores.clear()
            for r in reversed(c_rows):
                _clone_scores.append({"ts": r[0], "profile": r[1] or "",
                                      "cosine": float(r[2] or 0.0), "label": r[3] or ""})
            _naturalness.clear()
            for r in reversed(n_rows):
                _naturalness.append({"ts": r[0], "profile": r[1] or "",
                                     "naturalness": float(r[2] or 0.0), "label": r[3] or ""})
            _turns_alltime = int(cnt or 0)
    except Exception:
        pass
    try:
        _window_start = float(_meta_get("window_start", "0") or 0.0)
    except Exception:
        _window_start = 0.0


def feedback_streak(profile: str = "", rating: int = -1) -> int:
    """从最近评分向前统计连续同向次数。"""
    prof = (profile or "").strip()
    want = 1 if int(rating) > 0 else -1
    with _LOCK:
        items = list(_feedback)
    n = 0
    for f in reversed(items):
        if prof and (f.get("profile") or "").strip() != prof:
            break
        if int(f.get("rating") or 0) == want:
            n += 1
        else:
            break
    return n


def feedback_db_count() -> int:
    if not _FB_DB_PATH.exists():
        return 0
    try:
        conn = _conn()
        n = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0


def record_feedback(*, profile: str = "", rating: int = 0, source: str = "phone",
                    text_preview: str = "", reply_lang: str = "",
                    msg_id: int = 0) -> None:
    """记录用户听感评分：rating=1 好评，-1 差评。"""
    r = 1 if int(rating or 0) > 0 else -1
    row = {
        "ts": time.time(),
        "profile": profile or "",
        "rating": r,
        "source": source or "phone",
        "text_preview": (text_preview or "")[:120],
        "reply_lang": reply_lang or "",
        "msg_id": int(msg_id or 0),
    }
    with _LOCK:
        _feedback.append(row)
    _persist_feedback_row(row)


_init_db()
_load_feedback_from_db()
_load_series_from_db()


def record_turn(*, profile: str = "", ttfa_ms: int = 0, perceived_ttfa_ms: int = 0,
                stt_ms: int = 0, first_sentence_ms: int = 0,
                llm_first_token_ms: int = 0, total_ms: int = 0, n_sentences: int = 0,
                filler: bool = False, emotions: dict | None = None,
                emoref_hits: int = 0, emo_sentences: int = 0,
                cancelled: bool = False) -> None:
    global _turns_alltime
    row = {
        "ts": time.time(),
        "profile": profile or "",
        "ttfa_ms": int(ttfa_ms or 0),
        "perceived_ttfa_ms": int(perceived_ttfa_ms or 0),
        "stt_ms": int(stt_ms or 0),
        "first_sentence_ms": int(first_sentence_ms or 0),
        "llm_first_token_ms": int(llm_first_token_ms or 0),
        "total_ms": int(total_ms or 0),
        "n_sentences": int(n_sentences or 0),
        "filler": bool(filler),
        "emotions": dict(emotions or {}),
        "emoref_hits": int(emoref_hits or 0),
        "emo_sentences": int(emo_sentences or 0),
        "cancelled": bool(cancelled),
    }
    with _LOCK:
        _turns.append(row)
        _turns_alltime += 1
    _persist_turn_row(row)


def _pct(vals: list, p: float) -> int:
    if not vals:
        return 0
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return int(s[k])


def _quality_alerts(nats: list, profile_baselines: dict | None = None) -> list:
    """按角色检测自然度回归：滑动均值跌破阈值，或较体检基线显著下降。"""
    baselines = profile_baselines or {}
    by_prof: dict[str, list[float]] = defaultdict(list)
    for n in nats:
        prof = (n.get("profile") or "").strip()
        if prof:
            by_prof[prof].append(float(n.get("naturalness") or 0.0))

    alerts = []
    for prof, vals in by_prof.items():
        window = vals[-_NAT_ALERT_WINDOW:]
        if len(window) < _NAT_ALERT_MIN_SAMPLES:
            continue
        avg = sum(window) / len(window)
        base = baselines.get(prof) or {}
        base_nat = float(base.get("naturalness") or 0.0)
        reasons = []
        if avg < _NAT_ALERT_FLOOR:
            reasons.append(f"滑动均值 {avg:.3f} 低于阈值 {_NAT_ALERT_FLOOR:.2f}")
        if base_nat > 0 and avg < base_nat - _NAT_ALERT_DROP:
            reasons.append(f"较体检基线 {base_nat:.3f} 下降 {base_nat - avg:.3f}")
        if not reasons:
            continue
        sev = "critical" if avg < max(_NAT_ALERT_FLOOR - 0.05, 0.60) else "warning"
        alerts.append({
            "profile": prof,
            "window_avg": round(avg, 4),
            "baseline": round(base_nat, 4) if base_nat > 0 else None,
            "samples": len(window),
            "severity": sev,
            "reasons": reasons,
        })
    alerts.sort(key=lambda a: a["window_avg"])
    return alerts


def _feedback_scatter(fb: list, cscores: list, window: float = 180.0) -> list:
    """将听感评分与临近 clone_score 对齐，供看板散点分析「高分难听/低分好听」。"""
    points = []
    for f in fb:
        prof = (f.get("profile") or "").strip()
        ts = float(f.get("ts") or 0)
        if not prof or not ts:
            continue
        best, best_dt = None, window + 1.0
        for c in cscores:
            if (c.get("profile") or "").strip() != prof:
                continue
            dt = abs(float(c.get("ts") or 0) - ts)
            if dt < best_dt:
                best_dt = dt
                best = c
        if not best or best_dt > window:
            continue
        points.append({
            "rating": int(f.get("rating") or 0),
            "cosine": float(best.get("cosine") or 0.0),
            "profile": prof,
            "text_preview": (f.get("text_preview") or "")[:60],
            "label": best.get("label") or "",
            "delta_sec": round(best_dt, 1),
        })
    return points[-40:]


def _feedback_alerts(fb: list) -> list:
    """听感差评聚类：近期好评率过低时告警。"""
    if len(fb) < 3:
        return []
    window = fb[-12:]
    up = sum(1 for f in window if f.get("rating", 0) > 0)
    down = sum(1 for f in window if f.get("rating", 0) < 0)
    total = up + down
    if total < 3:
        return []
    rate = up / total
    if rate >= 0.70:
        return []
    by_prof: dict[str, dict] = defaultdict(lambda: {"up": 0, "down": 0})
    for f in window:
        prof = (f.get("profile") or "").strip() or "—"
        if f.get("rating", 0) > 0:
            by_prof[prof]["up"] += 1
        elif f.get("rating", 0) < 0:
            by_prof[prof]["down"] += 1
    alerts = []
    for prof, v in by_prof.items():
        t = v["up"] + v["down"]
        if t < 2:
            continue
        pr = v["up"] / t
        if pr < 0.70:
            enc = _url_quote(prof, safe="")
            alerts.append({
                "profile": prof,
                "rate": round(pr, 3),
                "up": v["up"],
                "down": v["down"],
                "samples": t,
                "severity": "critical" if pr < 0.50 else "warning",
                "reasons": [f"近期好评率 {pr*100:.0f}%（👎 {v['down']}/{t}）"],
                "tune_url": f"/ui?profile={enc}&open=tune",
                "phone_url": f"/phone?profile={enc}",
                "auto_tune_available": True,
            })
    if not alerts and rate < 0.70:
        alerts.append({
            "profile": "全部",
            "rate": round(rate, 3),
            "up": up, "down": down, "samples": total,
            "severity": "warning",
            "reasons": [f"全局近期好评率 {rate*100:.0f}%"],
        })
    return alerts


def _feedback_stats(fb: list) -> dict:
    up = sum(1 for f in fb if f.get("rating", 0) > 0)
    down = sum(1 for f in fb if f.get("rating", 0) < 0)
    total = up + down
    by_prof: dict[str, dict] = defaultdict(lambda: {"up": 0, "down": 0})
    for f in fb:
        prof = (f.get("profile") or "").strip() or "—"
        if f.get("rating", 0) > 0:
            by_prof[prof]["up"] += 1
        elif f.get("rating", 0) < 0:
            by_prof[prof]["down"] += 1
    return {
        "n": total,
        "up": up,
        "down": down,
        "rate": round(up / total, 3) if total else 0.0,
        "by_profile": {k: {**v, "rate": round(v["up"] / (v["up"] + v["down"]), 3)
                           if (v["up"] + v["down"]) else 0.0}
                       for k, v in by_prof.items()},
        "recent": [{
            "ts": f["ts"], "profile": f["profile"], "rating": f["rating"],
            "source": f["source"], "text_preview": f["text_preview"],
            "reply_lang": f["reply_lang"],
        } for f in fb[-20:][::-1]],
    }


def snapshot(recent: int = 30, profile_baselines: dict | None = None) -> dict:
    ws = _window_start
    with _LOCK:
        turns = [t for t in _turns if t["ts"] >= ws]
        cscores = [c for c in _clone_scores if c["ts"] >= ws]
        nats = [n for n in _naturalness if n["ts"] >= ws]
        fb = [f for f in _feedback if f["ts"] >= ws]
        alltime = _turns_alltime
    total = len(turns)
    # TTFA 仅统计有有效首音的轮次
    ttfas = [t["ttfa_ms"] for t in turns if t["ttfa_ms"] > 0]
    perceived = [t["perceived_ttfa_ms"] for t in turns if t["perceived_ttfa_ms"] > 0]
    first_sents = [t["first_sentence_ms"] for t in turns if t["first_sentence_ms"] > 0]
    llm_lat = [t["llm_first_token_ms"] for t in turns if t["llm_first_token_ms"] > 0]
    totals = [t["total_ms"] for t in turns if t["total_ms"] > 0]
    filler_turns = sum(1 for t in turns if t["filler"])
    emo_counter: Counter = Counter()
    for t in turns:
        emo_counter.update(t["emotions"])
    total_sentences = sum(t["n_sentences"] for t in turns)
    total_emo_sent = sum(t["emo_sentences"] for t in turns)
    total_emoref = sum(t["emoref_hits"] for t in turns)

    def stat(vals):
        return {"avg": int(sum(vals) / len(vals)) if vals else 0,
                "p50": _pct(vals, 50), "p95": _pct(vals, 95),
                "min": min(vals) if vals else 0, "max": max(vals) if vals else 0,
                "n": len(vals)}

    return {
        "ok": True,
        "uptime_sec": int(time.time() - _started_at),
        "total_turns": total,
        "total_turns_alltime": alltime,
        "window_start": ws,
        "persisted": True,
        "ttfa_ms": stat(ttfas),
        "perceived_ttfa_ms": stat(perceived),
        "first_sentence_ms": stat(first_sents),
        "llm_first_token_ms": stat(llm_lat),
        "total_ms": stat(totals),
        "filler_rate": round(filler_turns / total, 3) if total else 0.0,
        "emotion_distribution": dict(emo_counter),
        "total_sentences": total_sentences,
        "emo_sentences": total_emo_sent,
        "emoref_hits": total_emoref,
        "emoref_hit_rate": round(total_emoref / total_emo_sent, 3) if total_emo_sent else 0.0,
        "clone_scores": {
            "avg": round(sum(c["cosine"] for c in cscores) / len(cscores), 4) if cscores else 0.0,
            "n": len(cscores),
            "recent": [{"ts": c["ts"], "profile": c["profile"],
                        "cosine": c["cosine"], "label": c["label"]}
                       for c in cscores[-20:][::-1]],
        },
        "naturalness": {
            "avg": round(sum(c["naturalness"] for c in nats) / len(nats), 4) if nats else 0.0,
            "n": len(nats),
            "recent": [{"ts": c["ts"], "profile": c["profile"],
                        "naturalness": c["naturalness"], "label": c["label"]}
                       for c in nats[-20:][::-1]],
        },
        "quality_alerts": _quality_alerts(nats, profile_baselines),
        "feedback": _feedback_stats(fb),
        "feedback_alerts": _feedback_alerts(fb),
        "feedback_scatter": _feedback_scatter(fb, cscores),
        "feedback_persisted": _FB_DB_PATH.exists(),
        "feedback_db_count": feedback_db_count(),
        "recent": [{
            "ts": t["ts"], "profile": t["profile"], "ttfa_ms": t["ttfa_ms"],
            "perceived_ttfa_ms": t["perceived_ttfa_ms"],
            "first_sentence_ms": t["first_sentence_ms"],
            "total_ms": t["total_ms"], "n_sentences": t["n_sentences"],
            "filler": t["filler"], "emotions": t["emotions"],
            "emoref_hits": t["emoref_hits"], "cancelled": t["cancelled"],
        } for t in turns[-recent:][::-1]],
    }


def daily_trend(days: int = 14) -> dict:
    """按日运营趋势（看板 v2 / P2-3）：对话轮数、开口速度日均、好评/差评。
    turns 走 SQL day 列聚合；feedback 量小，取近窗在 Python 里按本地日分桶。"""
    days = max(1, min(int(days or 14), 90))
    today = time.time()
    day_keys = [_day_of(today - i * 86400) for i in range(days - 1, -1, -1)]
    turns_by_day: dict[str, dict] = {}
    fb_by_day: dict[str, dict] = {}
    try:
        conn = _conn()
        rows = conn.execute(
            "SELECT day, COUNT(*), "
            "AVG(CASE WHEN perceived_ttfa_ms>0 THEN perceived_ttfa_ms END), "
            "AVG(CASE WHEN ttfa_ms>0 THEN ttfa_ms END) "
            "FROM turns WHERE day>=? GROUP BY day", (day_keys[0],)).fetchall()
        for d, n, pavg, tavg in rows:
            turns_by_day[d] = {"turns": int(n or 0),
                               "perceived_avg": int(pavg or 0),
                               "ttfa_avg": int(tavg or 0)}
        cutoff = today - days * 86400
        for ts, rating in conn.execute(
                "SELECT ts, rating FROM feedback WHERE ts>=?", (cutoff,)).fetchall():
            d = _day_of(float(ts))
            b = fb_by_day.setdefault(d, {"up": 0, "down": 0})
            if int(rating or 0) > 0:
                b["up"] += 1
            else:
                b["down"] += 1
        conn.close()
    except Exception:
        pass
    out = []
    for d in day_keys:
        t = turns_by_day.get(d, {})
        f = fb_by_day.get(d, {"up": 0, "down": 0})
        n_fb = f["up"] + f["down"]
        out.append({
            "date": d,
            "turns": t.get("turns", 0),
            "perceived_avg": t.get("perceived_avg", 0),
            "ttfa_avg": t.get("ttfa_avg", 0),
            "fb_up": f["up"], "fb_down": f["down"],
            "fb_rate": round(f["up"] / n_fb, 3) if n_fb else None,
        })
    return {"ok": True, "days": days, "trend": out}


def profile_series(name: str, days: int = 14, recent: int = 30) -> dict:
    """单角色下钻档案（看板 v3）：相似度/自然度全史序列、听感账本、轮次明细、按日趋势。
    刻意不吃软清窗口标记——下钻看的是角色履历，清屏不该抹历史。全部走 SQLite。"""
    name = (name or "").strip()
    days = max(1, min(int(days or 14), 90))
    out = {
        "ok": True, "profile": name,
        "clone": [], "naturalness": [], "feedback": {"n": 0, "up": 0, "down": 0,
                                                     "rate": 0.0, "recent": []},
        "turns": {"alltime": 0, "recent": [], "perceived_ttfa_ms": {}, "ttfa_ms": {}},
        "daily": [],
    }
    if not name:
        out["ok"] = False
        return out
    today = time.time()
    day_keys = [_day_of(today - i * 86400) for i in range(days - 1, -1, -1)]
    try:
        conn = _conn()
        out["clone"] = [
            {"ts": r[0], "cosine": float(r[1] or 0.0), "label": r[2] or ""}
            for r in reversed(conn.execute(
                "SELECT ts,cosine,label FROM clone_scores WHERE profile=? "
                "ORDER BY id DESC LIMIT 60", (name,)).fetchall())]
        out["naturalness"] = [
            {"ts": r[0], "naturalness": float(r[1] or 0.0), "label": r[2] or ""}
            for r in reversed(conn.execute(
                "SELECT ts,naturalness,label FROM naturalness WHERE profile=? "
                "ORDER BY id DESC LIMIT 60", (name,)).fetchall())]
        fb_rows = conn.execute(
            "SELECT ts,rating,source,text_preview FROM feedback WHERE profile=? "
            "ORDER BY id DESC LIMIT 200", (name,)).fetchall()
        up = sum(1 for r in fb_rows if int(r[1] or 0) > 0)
        down = len(fb_rows) - up
        out["feedback"] = {
            "n": len(fb_rows), "up": up, "down": down,
            "rate": round(up / len(fb_rows), 3) if fb_rows else 0.0,
            "recent": [{"ts": r[0], "rating": int(r[1] or 0), "source": r[2] or "",
                        "text_preview": r[3] or ""} for r in fb_rows[:20]],
        }
        t_rows = conn.execute(
            "SELECT ts,ttfa_ms,perceived_ttfa_ms,total_ms,n_sentences,filler,cancelled "
            "FROM turns WHERE profile=? ORDER BY id DESC LIMIT 300", (name,)).fetchall()
        ttfas = [int(r[1]) for r in t_rows if r[1] and r[1] > 0]
        pers = [int(r[2]) for r in t_rows if r[2] and r[2] > 0]

        def _stat(vals):
            return {"avg": int(sum(vals) / len(vals)) if vals else 0,
                    "p50": _pct(vals, 50), "p95": _pct(vals, 95), "n": len(vals)}

        out["turns"] = {
            "alltime": int(conn.execute("SELECT COUNT(*) FROM turns WHERE profile=?",
                                        (name,)).fetchone()[0] or 0),
            "perceived_ttfa_ms": _stat(pers),
            "ttfa_ms": _stat(ttfas),
            "recent": [{"ts": r[0], "ttfa_ms": int(r[1] or 0),
                        "perceived_ttfa_ms": int(r[2] or 0), "total_ms": int(r[3] or 0),
                        "n_sentences": int(r[4] or 0), "filler": bool(r[5]),
                        "cancelled": bool(r[6])} for r in t_rows[:recent]],
        }
        d_turn = {d: {"turns": int(n or 0)} for d, n in conn.execute(
            "SELECT day, COUNT(*) FROM turns WHERE profile=? AND day>=? GROUP BY day",
            (name, day_keys[0])).fetchall()}
        cutoff = today - days * 86400
        d_fb: dict[str, dict] = {}
        for ts, rating in conn.execute(
                "SELECT ts,rating FROM feedback WHERE profile=? AND ts>=?",
                (name, cutoff)).fetchall():
            b = d_fb.setdefault(_day_of(float(ts)), {"up": 0, "down": 0})
            b["up" if int(rating or 0) > 0 else "down"] += 1
        conn.close()
        for d in day_keys:
            f = d_fb.get(d, {"up": 0, "down": 0})
            n_fb = f["up"] + f["down"]
            out["daily"].append({
                "date": d, "turns": d_turn.get(d, {}).get("turns", 0),
                "fb_up": f["up"], "fb_down": f["down"],
                "fb_rate": round(f["up"] / n_fb, 3) if n_fb else None,
            })
    except Exception as e:
        out["ok"] = False
        out["detail"] = str(e)[:120]
    return out


def weekly_report(profile_baselines: dict | None = None) -> dict:
    """听感运维周报：好评率、差评聚类、cosine 趋势、角色基线（供 dashboard 导出）。"""
    snap = snapshot(recent=100, profile_baselines=profile_baselines)
    fb = snap.get("feedback") or {}
    return {
        "ok": True,
        "generated_at": time.time(),
        "period": "recent_window",
        "feedback": fb,
        "feedback_alerts": snap.get("feedback_alerts") or [],
        "feedback_scatter": snap.get("feedback_scatter") or [],
        "clone_scores": snap.get("clone_scores") or {},
        "quality_alerts": snap.get("quality_alerts") or [],
        "profiles": profile_baselines or {},
        "total_turns": snap.get("total_turns", 0),
        "ttfa_ms": snap.get("ttfa_ms") or {},
        "perceived_ttfa_ms": snap.get("perceived_ttfa_ms") or {},
        "feedback_db_count": snap.get("feedback_db_count", 0),
    }


def reset(scope: str = "window") -> dict:
    """清空统计（拆语义，防「清个页面把好评账本也删了」的数据事故）：
      window            软清（默认）：立窗口标记，视图从现在起重新累计，账本无损
      all               硬清：turns/clone/naturalness 内存+DB 全删，好评账本保留
      all_with_feedback 硬清 + 连听感评分一起删（唯一动 feedback 的路径）"""
    global _window_start, _turns_alltime
    scope = (scope or "window").strip().lower()
    if scope not in ("window", "all", "all_with_feedback"):
        scope = "window"
    if scope == "window":
        _window_start = time.time()
        _meta_set("window_start", str(_window_start))
        return {"ok": True, "scope": "window", "window_start": _window_start}
    with _LOCK:
        _turns.clear()
        _clone_scores.clear()
        _naturalness.clear()
        _turns_alltime = 0
        if scope == "all_with_feedback":
            _feedback.clear()
    _window_start = 0.0
    _meta_del("window_start")
    try:
        conn = _conn()
        for tbl in ("turns", "clone_scores", "naturalness"):
            conn.execute(f"DELETE FROM {tbl}")
        if scope == "all_with_feedback":
            conn.execute("DELETE FROM feedback")
        conn.commit()
        conn.close()
    except Exception:
        pass
    return {"ok": True, "scope": scope}
