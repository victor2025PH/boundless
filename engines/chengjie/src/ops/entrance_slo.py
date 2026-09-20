# -*- coding: utf-8 -*-
"""入口可用性 SLO（P1-7，2026-08-12 可靠性复盘）。

把「工作台入口到底多可用」从体感变成两个互补口径的数字：

- **服务端视角**：``prod_edge_watchdog.log``（每 5 分钟一条 OK/STRIKE/HOLD/FAILED
  探测行，端到端打公网入口）→ 窗口内可用率 + 断连事件段。日志自轮转保尾
  2000 行 ≈ 7 天，因此默认窗口 7 天；30 天趋势需持久 rollup，属下一阶段。
- **坐席端视角**：``ui_event_trend`` 里的 ``conn_*`` 埋点（P0-2：红条恢复时刻
  补发 ``conn_ep_<判定>`` + ``conn_dur_<时长桶>``）→ 按日聚合。

两个视角的差值就是「客户端侧损耗」（办公室线路/坐席本机网络）——服务端全绿
而坐席端断连多，说明该投入线路侧；两边同步恶化才是服务端问题。

纯读、软失败：日志缺失/格式漂移返回空骨架绝不抛；300s TTL 进程缓存防 ops
轮询每次全读日志。消费方：``/api/workspace/metrics.entrance_slo``（drafts_routes）
+ 只读 CLI ``python -m src.ops.entrance_slo``。
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

# 看门狗日志行： [2026-08-12 10:23:24] [STRIKE] unhealthy (1/2): ...
_LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[([A-Z]+)\] ")

# 探测 tick 的语义映射：WARN=「ssh 探针抖但 public=200，用户无感」按可用计；
# BOOT/RESTART/ALERT/KILL/FORCE/DRYRUN 是动作日志不是探测结论，不计 tick。
_OK_LEVELS = {"OK", "WARN", "RECOVERED"}
_BAD_LEVELS = {"STRIKE", "HOLD", "FAILED"}
_TICK_SEC = 300  # prod_edge_watchdog 计划任务节拍

DEFAULT_EDGE_LOG = r"D:\chengjie-instances\.ops\prod_edge_watchdog.log"


def parse_edge_log(text: str, *, now: Optional[float] = None,
                   window_days: int = 7) -> Dict[str, Any]:
    """纯函数：看门狗日志文本 → 服务端可用性快照。

    outage 事件段＝连续 BAD tick 区间（时长按 tick 数 × 5min 近似——看门狗本身
    就是 5 分钟采样，精度到 tick 是口径的诚实上限）。窗口末尾仍未恢复的段照记
    （进行中的事故不该被漏计）。
    """
    base = now if now is not None else time.time()
    cutoff = base - max(1, int(window_days)) * 86400
    ticks = 0
    ok = 0
    outages: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for line in (text or "").splitlines():
        m = _LINE_RE.match(line.strip())
        if not m:
            continue
        try:
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        except Exception:
            continue
        if ts < cutoff:
            continue
        lvl = m.group(2)
        if lvl in _OK_LEVELS:
            ticks += 1
            ok += 1
            if cur is not None:
                outages.append(cur)
                cur = None
        elif lvl in _BAD_LEVELS:
            ticks += 1
            if cur is None:
                cur = {"start": m.group(1), "end": m.group(1), "bad_ticks": 1}
            else:
                cur["end"] = m.group(1)
                cur["bad_ticks"] = int(cur["bad_ticks"]) + 1
    if cur is not None:
        cur["ongoing"] = True
        outages.append(cur)
    for o in outages:
        o["minutes"] = round(int(o["bad_ticks"]) * _TICK_SEC / 60.0, 1)
    pct = round(100.0 * ok / ticks, 3) if ticks else None
    return {
        "window_days": int(window_days),
        "ticks": ticks,
        "ok_ticks": ok,
        "availability_pct": pct,
        "outage_count": len(outages),
        "outage_minutes": round(sum(float(o["minutes"]) for o in outages), 1),
        "outages": outages[-10:],
    }


def _summarize_daily(rows: List[Dict[str, Any]], days: int) -> Dict[str, Any]:
    totals: Dict[str, int] = {}
    for r in rows:
        for k, v in (r.get("by_action") or {}).items():
            totals[k] = totals.get(k, 0) + int(v)
    episodes = sum(v for k, v in totals.items() if k.startswith("conn_ep_"))
    return {"days": int(days), "episodes": episodes,
            "by_action": dict(sorted(totals.items())), "daily": rows}


def client_conn_stats(days: int = 7) -> Dict[str, Any]:
    """坐席端视角：conn_* 断连埋点按日聚合（趋势库未配置返回 {}——零依赖软失败）。

    episodes 只数 ``conn_ep_*``（每次进入过红条 down 态的恢复回执各一条）；
    ``conn_dur_*``/``conn_ep_local|server|none`` 分桶原样透传给消费方。
    """
    try:
        from src.web.ui_event_trend import get_ui_event_trend_store
        store = get_ui_event_trend_store()
        if store is None:
            return {}
        return _summarize_daily(store.daily(days=days, prefix="conn_"), days)
    except Exception:
        return {}


def client_conn_stats_from_db(db_path: str, days: int = 7,
                              now: Optional[float] = None) -> Dict[str, Any]:
    """CLI 用的直读版：进程没有应用启动时那份 store 配置（configure 在 main 里），
    按 ``mode=ro`` URI 只读打开趋势库（本仓 CLI 读生产库纪律：零写事务、绝不建表）。
    文件缺失/表缺失/任何异常 → {}。
    """
    import sqlite3
    try:
        if not os.path.isfile(db_path):
            return {}
        base = now if now is not None else time.time()
        n = max(1, min(int(days or 7), 90))
        day_keys = [time.strftime("%Y-%m-%d", time.localtime(base - i * 86400))
                    for i in range(n - 1, -1, -1)]
        uri = "file:%s?mode=ro" % str(db_path).replace("\\", "/")
        con = sqlite3.connect(uri, uri=True)
        try:
            got = con.execute(
                "SELECT day, action, n FROM ui_event_trend_daily "
                "WHERE day >= ? AND action LIKE 'conn_%'",
                (day_keys[0],)).fetchall()
        finally:
            con.close()
        buckets: Dict[str, Dict[str, int]] = {}
        for day, action, cnt in got:
            buckets.setdefault(str(day), {})[str(action)] = int(cnt)
        rows = [{"day": d, "total": sum(buckets.get(d, {}).values()),
                 "by_action": dict(sorted(buckets.get(d, {}).items()))}
                for d in day_keys]
        return _summarize_daily(rows, days)
    except Exception:
        return {}


DEFAULT_LEDGER = r"D:\chengjie-instances\.ops\entrance_slo_daily.json"
_LEDGER_RETENTION_DAYS = 60


def rollup_daily(text: str) -> Dict[str, Dict[str, Any]]:
    """纯函数：看门狗日志文本 → 每日聚合 {day: {ticks, ok_ticks, bad_ticks, outage_minutes}}。

    不做窗口过滤（日志自轮转天然只有 ~7 天），窗口语义交给账本 merge——
    这样轮转边缘的「半天数据」也会在下次 rollup 时被同日 max-merge 补全。
    """
    per: Dict[str, Dict[str, int]] = {}
    for line in (text or "").splitlines():
        m = _LINE_RE.match(line.strip())
        if not m:
            continue
        lvl = m.group(2)
        if lvl not in _OK_LEVELS and lvl not in _BAD_LEVELS:
            continue
        day = m.group(1)[:10]
        b = per.setdefault(day, {"ticks": 0, "ok_ticks": 0, "bad_ticks": 0})
        b["ticks"] += 1
        if lvl in _OK_LEVELS:
            b["ok_ticks"] += 1
        else:
            b["bad_ticks"] += 1
    return {
        d: {"ticks": v["ticks"], "ok_ticks": v["ok_ticks"],
            "bad_ticks": v["bad_ticks"],
            "outage_minutes": round(v["bad_ticks"] * _TICK_SEC / 60.0, 1)}
        for d, v in per.items()
    }


def update_rollup_ledger(edge_log_path: Optional[str] = None,
                         ledger_path: Optional[str] = None,
                         *, now: Optional[float] = None) -> Dict[str, Any]:
    """把当前日志窗口的每日聚合 upsert 进持久账本（JSON，原子写，60 天裁剪）。

    幂等：同一天重复 rollup 取 **tick 数更大的那份**（日志尚在写入时早跑一次
    不会把后来更全的数据顶掉；日志已轮转时账本里的旧全量也不会被半窗覆盖）。
    供每日计划任务 EntranceSloRollup 与 CLI ``--rollup`` 调用；快照只读它。
    """
    import json
    base = now if now is not None else time.time()
    lpath = ledger_path or DEFAULT_LEDGER
    days: Dict[str, Any] = {}
    try:
        with open(lpath, "r", encoding="utf-8") as f:
            days = (json.load(f) or {}).get("days") or {}
    except Exception:
        days = {}
    path = edge_log_path or DEFAULT_EDGE_LOG
    text = ""
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
    except Exception:
        text = ""
    for day, agg in rollup_daily(text).items():
        old = days.get(day) or {}
        if int(old.get("ticks") or 0) <= int(agg["ticks"]):
            days[day] = agg
    cut = time.strftime("%Y-%m-%d",
                        time.localtime(base - _LEDGER_RETENTION_DAYS * 86400))
    days = {d: v for d, v in days.items() if d >= cut}
    payload = {"days": dict(sorted(days.items())), "updated": int(base)}
    try:
        tmp = str(lpath) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, lpath)
    except Exception:
        pass
    return payload


def ledger_summary(ledger_path: Optional[str] = None, *, days: int = 30,
                   now: Optional[float] = None) -> Dict[str, Any]:
    """账本近 N 天合帐：{window_days, days_covered, ticks, ok_ticks,
    availability_pct, outage_minutes}。账本缺失/为空 → {}（卡片按无数据处理）。"""
    import json
    base = now if now is not None else time.time()
    lpath = ledger_path or DEFAULT_LEDGER
    try:
        with open(lpath, "r", encoding="utf-8") as f:
            all_days = (json.load(f) or {}).get("days") or {}
    except Exception:
        return {}
    cut = time.strftime("%Y-%m-%d", time.localtime(base - max(1, int(days)) * 86400))
    ticks = ok = 0
    minutes = 0.0
    covered = 0
    for d, v in all_days.items():
        if d < cut:
            continue
        t = int((v or {}).get("ticks") or 0)
        if t <= 0:
            continue
        covered += 1
        ticks += t
        ok += int((v or {}).get("ok_ticks") or 0)
        minutes += float((v or {}).get("outage_minutes") or 0.0)
    if not ticks:
        return {}
    return {"window_days": int(days), "days_covered": covered, "ticks": ticks,
            "ok_ticks": ok,
            "availability_pct": round(100.0 * ok / ticks, 3),
            "outage_minutes": round(minutes, 1)}


_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"ts": 0.0, "snap": None}


def _reset_cache() -> None:
    with _CACHE_LOCK:
        _CACHE["ts"] = 0.0
        _CACHE["snap"] = None


def entrance_slo_snapshot(*, ttl_sec: float = 300.0,
                          edge_log_path: Optional[str] = None,
                          ledger_path: Optional[str] = None,
                          now: Optional[float] = None) -> Dict[str, Any]:
    """服务端 + 坐席端 + 30 天账本合帐快照（300s TTL；显式路径时绕过缓存供测试/CLI）。"""
    base = now if now is not None else time.time()
    use_cache = edge_log_path is None and ledger_path is None
    if use_cache:
        with _CACHE_LOCK:
            snap = _CACHE.get("snap")
            if snap is not None and (base - float(_CACHE.get("ts") or 0.0)) < ttl_sec:
                return snap
    path = edge_log_path or DEFAULT_EDGE_LOG
    server: Dict[str, Any] = {}
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                server = parse_edge_log(f.read(), now=base)
    except Exception:
        server = {}
    snap = {
        "server": server,
        "client": client_conn_stats(),
        "d30": ledger_summary(ledger_path, now=base),
        "generated_at": int(base),
    }
    if use_cache:
        with _CACHE_LOCK:
            _CACHE["ts"] = base
            _CACHE["snap"] = snap
    return snap


def _main() -> int:  # pragma: no cover - 只读 CLI 皮（--rollup 时写账本）
    import json
    import sys
    if "--rollup" in sys.argv[1:]:
        payload = update_rollup_ledger()
        print("rollup ok: %d day(s) in ledger %s" % (
            len(payload.get("days") or {}), DEFAULT_LEDGER))
    snap = entrance_slo_snapshot(edge_log_path=DEFAULT_EDGE_LOG,
                                 ledger_path=DEFAULT_LEDGER)
    srv = snap.get("server") or {}
    d30 = snap.get("d30") or {}
    print("=== 工作台入口可用性（服务端视角, %s 天窗）===" % srv.get("window_days", 7))
    if srv.get("ticks"):
        print("可用率 %.3f%%  (tick %d/%d, 断连 %s 段共 %s 分钟)" % (
            srv.get("availability_pct") or 0.0, srv.get("ok_ticks", 0),
            srv.get("ticks", 0), srv.get("outage_count", 0),
            srv.get("outage_minutes", 0)))
        for o in srv.get("outages") or []:
            print("  outage %s -> %s  (%.1f min%s)" % (
                o.get("start"), o.get("end"), float(o.get("minutes") or 0),
                ", ongoing" if o.get("ongoing") else ""))
    else:
        print("（无探测数据：看门狗日志缺失或为空）")
    if d30:
        print("30 天账本：%.3f%%（覆盖 %s 天, 断连 %s 分钟）" % (
            d30.get("availability_pct") or 0.0, d30.get("days_covered", 0),
            d30.get("outage_minutes", 0)))
    cli = snap.get("client") or {}
    if not cli:
        # CLI 进程没有 app 启动时配置的 store —— 按数据根契约逐根找 db 只读直读。
        try:
            from scripts._data_root import resolve_data_roots  # type: ignore
            roots = resolve_data_roots()
        except Exception:
            roots = []
        for root in roots or []:
            db = os.path.join(str(root), "config", "ui_event_trend.db")
            cli = client_conn_stats_from_db(db)
            if cli:
                break
    print("=== 坐席端断连回执（conn_* 埋点, %s 天）===" % (cli or {}).get("days", 7))
    if cli:
        print("episodes=%s  by_action=%s" % (
            cli.get("episodes", 0), json.dumps(cli.get("by_action") or {},
                                               ensure_ascii=False)))
    else:
        print("（趋势库未配置或无数据）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
