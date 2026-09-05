# -*- coding: utf-8 -*-
"""「自动推进」到底真开没开——一条命令三层验收（D1b P0-5，2026-09-05）。

给重启窗后的验证人（任何 agent 线 / 运维）用。默认**免登录只读**：文件真相
（合并配置 → ``sprint_engine_status``）+ 库真相（活跃 auto 目标 / ``goal:*``
派发行 24h 真发证据）+ 匿名 HTTP 探针（preflight 路由 401/403=已装载、404=等重启窗）。
``--live`` 再用 ``web_admin.auth_token`` 登录，逐条活跃 auto 目标打
``GET /api/goals/{id}/preflight``——那是与 ticker ``run_once`` 同一函数的运行时闸，
preflight 说「能」推进器就一定会排；同时读到进程层真相（ticker/dispatcher 线程活没活）。

    python tools/goal_sprint_drill.py                # 只读体检（默认全实例根）
    python tools/goal_sprint_drill.py --live         # + 登录逐目标 preflight（仍只读）
    python tools/goal_sprint_drill.py --json         # 机器可读

判定（``verdict`` 纯函数，tests/test_goal_sprint_drill.py 钉住）：
- DISABLED       goals.enabled 关——整域关闸是选择不是故障，不进非零退出；
- BROKEN         配置层两条链（冲刺 / 自然档）都进不了真发，或 --live 发现
                 ticker / dispatcher 线程没在跑——重启多少次都不会好，先修；
- RIDES_RESTART  文件真相就绪、preflight 路由 404 ＝ 代码没装载，等下个重启窗；
- IDLE           引擎能发，但库里一条活跃 auto 目标都没有——没东西可推，信息态；
- BLOCKED        --live 逐条 preflight 全被运行时闸拦（会话人审档/opt-out/危机窗…）
                 ——引擎没病，是这些目标各自的会话不满足自发条件；
- READY          引擎能发 + 有活跃 auto 目标 +（--live 时 ≥1 条 preflight ok）；
                 24h 内有 ``goal:*`` 真发行时附 PROVEN 证据（不是「应该会发」，是发过了）。

只读纪律：goals 库 ``GoalStore.open_readonly``、care_schedule 走 ``mode=ro`` URI，
对活体生产库零写事务；HTTP 只有 GET + /login 换 session。**不真发任何消息**。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402

DEFAULT_BASE_URL = "http://127.0.0.1:18799"
EVIDENCE_WINDOW_SEC = 24 * 3600


# ── 纯函数（门禁钉这里）────────────────────────────────────────────────────

def summarize_care_rows(rows: List[Dict[str, Any]], *, now: float) -> Dict[str, Any]:
    """``goal:*`` 派发行 → 分型计数 + 24h 真发证据。

    ``pending`` 已排未发；``sent_real`` 状态 sent 且 note≠dry_run（真到客户）；
    ``sent_dry`` dry_run 拟稿样本（说明链在跑、但被 dry 闸兜住）；``last_real_ts``
    最近一次真发时刻；``real_24h`` 窗口内真发条数＝「真开」的直接证据。
    按 kind（sprint 相位 / daily 每日拍 / care 约定回访）分桶。
    """
    from src.companion.goals.sprint_ticker import parse_goal_care_kind
    out: Dict[str, Any] = {
        "total": 0, "pending": 0, "sent_real": 0, "sent_dry": 0, "other": 0,
        "real_24h": 0, "last_real_ts": 0.0,
        "by_kind": {"sprint": 0, "daily": 0, "care": 0},
    }
    for r in rows or []:
        kind, _gid, _arg = parse_goal_care_kind(r.get("topic_norm"))
        if not kind:
            continue
        out["total"] += 1
        out["by_kind"][kind] = out["by_kind"].get(kind, 0) + 1
        st = str(r.get("status") or "")
        note = str(r.get("note") or "")
        if st == "pending":
            out["pending"] += 1
        elif st == "sent" and note == "dry_run":
            out["sent_dry"] += 1
        elif st == "sent":
            out["sent_real"] += 1
            ts = float(r.get("sent_at") or 0.0)
            if ts > out["last_real_ts"]:
                out["last_real_ts"] = ts
            if ts >= now - EVIDENCE_WINDOW_SEC:
                out["real_24h"] += 1
        else:
            out["other"] += 1
    return out


def verdict(checks: Dict[str, Any]) -> Tuple[str, List[str]]:
    """体检字典 → (READY|BLOCKED|IDLE|RIDES_RESTART|BROKEN|DISABLED, 人话原因)。"""
    eng = checks.get("engine") or {}
    if not eng.get("enabled"):
        return "DISABLED", ["companion.goals.enabled 未开——目标域整体关闸（选择而非故障）"]
    reasons: List[str] = []
    sprint_ok = bool(eng.get("sprint_effective"))
    natural_ok = bool(eng.get("natural_auto_effective"))
    if not sprint_ok:
        reasons.append("冲刺链（today/session）进不了真发："
                       + ", ".join(eng.get("sprint_blockers") or []))
    if not natural_ok:
        reasons.append("自然档链进不了真发："
                       + ", ".join(eng.get("natural_auto_blockers") or []))
    if not sprint_ok and not natural_ok:
        return "BROKEN", reasons
    if not eng.get("sprint_platforms_explicit"):
        reasons.append("goals.sprint.platforms 未显式配置，走出厂默认 "
                       f"{eng.get('sprint_platforms')}——建议 overlay 写死（启动 WARNING 同源）")
    for p, blk in (checks.get("platform_blockers") or {}).items():
        if blk:
            reasons.append(f"平台 {p} 上的活跃 auto 目标被引擎闸拦：{', '.join(blk)}")

    proc = checks.get("process") or {}
    if proc.get("ticker_running") is False:
        reasons.append("推进器线程未在运行（进程内已停/未挂载）——配置说开≠进程活着")
    if proc.get("dispatcher_running") is False:
        reasons.append("派发循环未在运行——排好的拍一条都发不出去")
    if proc.get("ticker_running") is False or proc.get("dispatcher_running") is False:
        return "BROKEN", reasons

    http = checks.get("http_routes")
    if http == "missing":
        reasons.append("preflight 路由 404＝本批 .py 未装载，等下个重启窗（文件真相已就绪）")
        return "RIDES_RESTART", reasons
    if http == "unknown":
        reasons.append("实例不可达：HTTP 探针跳过（只验证了文件+库真相）")

    n_auto = int(checks.get("active_auto", 0) or 0)
    if n_auto <= 0:
        reasons.append(f"活跃目标 {checks.get('active_total', 0)} 条中 0 条是「自动推进」档"
                       "——引擎能发但没东西可推")
        return "IDLE", reasons

    care = checks.get("care") or {}
    if int(care.get("real_24h") or 0) > 0:
        reasons.append(f"PROVEN：24h 内 goal:* 真发 {care['real_24h']} 条"
                       f"（累计真发 {care.get('sent_real', 0)}，待发 {care.get('pending', 0)}）")
    elif int(care.get("sent_dry") or 0) > 0 and int(care.get("sent_real") or 0) == 0:
        reasons.append(f"只有 dry_run 拟稿样本 {care['sent_dry']} 条、零真发——"
                       "链在跑但历史上一直被 dry 闸兜住（现配置已 live，等下一拍验证）")
    elif int(care.get("pending") or 0) > 0:
        reasons.append(f"已排未发 {care['pending']} 条（等 due_at 到点由派发循环真发）")

    pf = checks.get("preflight")
    if isinstance(pf, list) and pf:
        ok_n = sum(1 for x in pf if x.get("ok"))
        if ok_n == 0:
            reasons.append(f"--live：{len(pf)} 条 auto 目标 preflight 全被运行时闸拦——"
                           "引擎没病，是各会话不满足自发条件（见逐条 blockers）")
            return "BLOCKED", reasons
        reasons.append(f"--live：{ok_n}/{len(pf)} 条 auto 目标 preflight ok（推进器会排）")
    return "READY", reasons


# ── 采集（IO；对坏根软失败）─────────────────────────────────────────────────

def _load_active_goals(root: Path, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    from src.companion.goals.service import resolve_db_path
    from src.companion.goals.store import GoalStore
    db = Path(resolve_db_path(cfg, root / "config" / "config.yaml"))
    if not db.is_file():
        return []
    try:
        store = GoalStore.open_readonly(db)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! goals 库只读打开失败: {exc}", file=sys.stderr)
        return []
    try:
        rows: List[Dict[str, Any]] = []
        off = 0
        while True:
            page = store.list_active_page(offset=off, limit=200)
            if not page:
                break
            rows.extend(page)
            off += len(page)
            if len(page) < 200:
                break
        return rows
    except Exception as exc:  # noqa: BLE001
        print(f"  ! goals 库读取失败: {exc}", file=sys.stderr)
        return []


def _load_goal_care_rows(root: Path) -> List[Dict[str, Any]]:
    db = root / "config" / "care_schedule.db"
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT topic_norm, status, note, sent_at, due_at FROM care_schedule"
                " WHERE topic_norm LIKE 'goal:%'").fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        print(f"  ! care_schedule.db 读取失败: {exc}", file=sys.stderr)
        return []


def _http_code(url: str, timeout: float = 4.0) -> int:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except Exception:
        return 0


def read_token(root: Path) -> str:
    cfg = load_merged_config(root)
    wa = cfg.get("web_admin") if isinstance(cfg.get("web_admin"), dict) else {}
    return str(wa.get("auth_token") or "")


def instance_base_url(cfg: Dict[str, Any], default: str = DEFAULT_BASE_URL) -> str:
    """按实例合并配置的 ``web_admin.port`` 推 HTTP 基址（多实例根各探各的端口；
    zhiliao=18799 / zhiliao_pilot=18999，此前同一 base 打两次会把 pilot 误判）。"""
    wa = cfg.get("web_admin") if isinstance(cfg.get("web_admin"), dict) else {}
    try:
        port = int(wa.get("port") or 0)
    except Exception:
        port = 0
    return f"http://127.0.0.1:{port}" if 0 < port < 65536 else default


class _Client:
    """session cookie + Bearer 双凭据（与 live_multiwin_drill 同鉴权口径）。"""

    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self._cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj))

    def _raw(self, req: urllib.request.Request, timeout: float) -> Tuple[int, str]:
        try:
            with self._opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return 0, json.dumps({"error": str(e)[:200]})

    def login(self) -> bool:
        data = urllib.parse.urlencode({"auth_token": self.token}).encode()
        req = urllib.request.Request(
            self.base + "/login", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        code, _ = self._raw(req, 20)
        return code in (200, 303) and any(c.name == "session" for c in self._cj)

    def get(self, path: str) -> Tuple[int, Any]:
        req = urllib.request.Request(
            self.base + path, method="GET",
            headers={"Authorization": f"Bearer {self.token}"})
        code, raw = self._raw(req, 30)
        try:
            return code, json.loads(raw)
        except Exception:
            return code, {"raw": raw[:200]}


def collect(root: Path, *, base_url: str = "", live: bool = False,
            now: Optional[float] = None) -> Dict[str, Any]:
    from src.companion.goals.pace import resolve_pace
    from src.companion.goals.service import sprint_engine_status
    n = float(now if now is not None else time.time())
    cfg = load_merged_config(root)
    if base_url == "auto":
        base_url = instance_base_url(cfg)
    engine = sprint_engine_status(cfg)
    goals = _load_active_goals(root, cfg)
    auto = [g for g in goals if str(g.get("autonomy") or "") == "auto"]
    plat_blk: Dict[str, List[str]] = {}
    for p in sorted({str(g.get("platform") or "") for g in auto if g.get("platform")}):
        plat_blk[p] = list(sprint_engine_status(cfg, platform=p).get("sprint_blockers") or [])
    care = summarize_care_rows(_load_goal_care_rows(root), now=n)
    checks: Dict[str, Any] = {
        "root": str(root),
        "engine": engine,
        "platform_blockers": plat_blk,
        "active_total": len(goals),
        "active_auto": len(auto),
        "auto_goals": [{
            "goal_id": str(g.get("goal_id") or ""),
            "platform": str(g.get("platform") or ""),
            "pace": str(resolve_pace(g) or ""),
            "title": str(g.get("title") or "")[:40],
        } for g in auto],
        "care": care,
        "http_routes": "skipped",
        "base_url": base_url,
        "process": {},
        "preflight": None,
    }
    if not base_url:
        return checks
    if _http_code(f"{base_url}/login") != 200:
        checks["http_routes"] = "unknown"
        return checks
    codes = (
        _http_code(f"{base_url}/api/goals/engine-status"),
        _http_code(f"{base_url}/api/goals/__probe__/preflight"),
    )
    checks["http_codes"] = list(codes)
    if codes[1] == 404 and codes[0] in (401, 403):
        checks["http_routes"] = "missing"       # 早批已 live、preflight 等重启窗
    elif all(c == 404 for c in codes):
        checks["http_routes"] = "missing"
    elif all(c in (401, 403) for c in codes):
        checks["http_routes"] = "loaded"
    else:
        checks["http_routes"] = "partial"
    if not live or checks["http_routes"] != "loaded":
        return checks

    tok = read_token(root)
    if not tok:
        checks["live_error"] = "读不到 web_admin.auth_token，--live 跳过"
        return checks
    cli = _Client(base_url, tok)
    if not cli.login():
        checks["live_error"] = "登录失败（auth_token 不匹配？），--live 跳过"
        return checks
    pf: List[Dict[str, Any]] = []
    for g in checks["auto_goals"]:
        code, body = cli.get(f"/api/goals/{g['goal_id']}/preflight")
        if code != 200 or not isinstance(body, dict):
            pf.append({"goal_id": g["goal_id"], "ok": False,
                       "blockers": [f"http_{code}"], "next_due": 0})
            continue
        pf.append({
            "goal_id": g["goal_id"], "pace": body.get("pace"),
            "ok": bool(body.get("ok")),
            "blockers": list(body.get("blockers") or []),
            "due_now": bool(body.get("due_now")),
            "next_due": float(body.get("next_due") or 0),
            "pending_rows": int(body.get("pending_rows") or 0),
        })
        proc = body.get("process") if isinstance(body.get("process"), dict) else {}
        if proc and not checks["process"]:
            checks["process"] = {
                "ticker_running": proc.get("ticker_running"),
                "dispatcher_running": proc.get("dispatcher_running"),
            }
    checks["preflight"] = pf
    return checks


def _fmt_ts(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts > 0 else "-"


def main() -> int:
    ap = argparse.ArgumentParser(description="「自动推进」真开验收（免登录只读；--live 登录逐目标 preflight）")
    ap.add_argument("--data-root", default="", help="实例数据根（缺省自动发现）")
    ap.add_argument("--base", default="auto",
                    help="实例 HTTP 基址（缺省 auto=按各根 web_admin.port 推；空串=跳过 HTTP）")
    ap.add_argument("--live", action="store_true",
                    help="登录后逐条活跃 auto 目标打 /preflight（仍只读，不发消息）")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    roots = resolve_data_roots(args.data_root)
    exit_code = 0
    reports = []
    for root in roots:
        checks = collect(root, base_url=args.base, live=args.live)
        status, reasons = verdict(checks)
        reports.append({"status": status, "reasons": reasons, **checks})
        if status in ("BROKEN", "RIDES_RESTART"):
            exit_code = max(exit_code, 1)
        elif status == "BLOCKED":
            exit_code = max(exit_code, 2)
        if args.as_json:
            continue
        eng = checks["engine"]
        care = checks["care"]
        print(f"\n=== {root} → {status} ===")
        print(f"  engine   goals={eng.get('enabled')} sprint={eng.get('sprint_enabled')}"
              f" dry_run={eng.get('sprint_dry_run')} platforms={eng.get('sprint_platforms')}"
              f"{'' if eng.get('sprint_platforms_explicit') else '(default)'}"
              f" natural_daily={eng.get('natural_daily')}")
        print(f"           sprint_effective={eng.get('sprint_effective')}"
              f" natural_auto_effective={eng.get('natural_auto_effective')}"
              f" via={eng.get('natural_auto_via') or '-'}")
        print(f"  goals    active={checks['active_total']} auto={checks['active_auto']}")
        for g in checks["auto_goals"][:12]:
            print(f"    auto: {g['goal_id'][:12]} {g['platform']:9} pace={g['pace']:8} {g['title']}")
        print(f"  care     goal:* total={care['total']} pending={care['pending']}"
              f" sent_real={care['sent_real']} sent_dry={care['sent_dry']}"
              f" real_24h={care['real_24h']} last_real={_fmt_ts(care['last_real_ts'])}"
              f" kinds={care['by_kind']}")
        print(f"  http     {checks['http_routes']} {checks.get('http_codes', '')}")
        if checks.get("process"):
            print(f"  process  {checks['process']}")
        if checks.get("live_error"):
            print(f"  live     ! {checks['live_error']}")
        for p in checks.get("preflight") or []:
            mark = "OK " if p.get("ok") else "BLK"
            extra = (f"next={_fmt_ts(p.get('next_due', 0))}"
                     f"{' DUE' if p.get('due_now') else ''}") if p.get("ok") \
                else ", ".join(p.get("blockers") or [])
            print(f"    {mark} {p['goal_id'][:12]} {str(p.get('pace') or ''):8} {extra}")
        for r in reasons:
            print(f"  - {r}")
    if args.as_json:
        print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
