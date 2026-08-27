"""一键健康报告 — 把「观察一周」变成「每天一条命令」（2026-07 /thread 性能重构收尾）。

聚合四类信号，读数即可判断本轮修复是否稳住、backfill 是否消化完：

1. **实例健康**：main.py 进程数 vs 活跃实例数 + 每实例 web 就绪探测；
2. **/thread 延迟采样**：对最近活跃的 N 个会话实测（回归「加载超时」的第一信号）;
3. **入站翻译**：存量未译候选（应趋零）+ 按日漏斗（translated/noop/deferred/failed 近 7 天）;
4. **重启频率**：logs/restart_*/boot_* 按日计数（纪律执行情况，目标 ≤3 次/日）。

⚠ **所有信号一律按「实例数据根」解析，禁止再按引擎根猜**（2026-08-27 事故沉淀）：
本机 2026-07 迁双实例后，生产的 inbox.db / auth_token / logs 全在
``D:\\chengjie-instances\\<iid>\\data`` 下，而本脚本原先按引擎仓库根读
``config/inbox.db`` 与 ``config/config.yaml``——两者都是迁移时刻的陈旧副本。
后果不是「少看几个数」而是**结构性失明 + 每天误报**：会话列表冻结在 8/11
（两周告警里 5 个会话 ID 一字不差）、token 是旧的（/thread 恒 401）、
重启与热重载计数恒空（真超纪律也看不见），连续 14 天没人能从告警里读出信息。
与 ``src/eval/eval_config.py`` 同一收口方式：走 ``scripts/_data_root`` 唯一事实源。

用法：
    python -m scripts.health_report                    # 人读表格（全部活跃实例）
    python -m scripts.health_report --json             # 机读（接告警/趋势）
    python -m scripts.health_report --data-root D:\\...  # 只看指定实例
默认只读 DB（只读连接，对活库零写事务）与日志、对各实例发少量 GET，无副作用。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE_ROOT))

from scripts._data_root import (  # noqa: E402
    instances_base,
    load_merged_config,
    resolve_data_roots,
)
from src.ai.translation_service import detect_language  # noqa: E402

RESTART_RED_LINE = 3      # 每日重启纪律红线（超过即告警）
THREAD_SLOW_MS = 5000     # /thread 慢阈值（「加载超时」前兆）

# 采不到样的两类语义必须分开：前者是巡检坏了（必须发声），后者是实例确实还没数据
# （新租户常态）。混为一谈就是拿噪音换覆盖——本次事故的教训正是「天天报＝没人看」。
SKIP_BLIND = {"no_token", "no_db", "db_error"}


@dataclass
class Target:
    """一个待巡检实例的全部落点（数据根推导，绝不掺引擎根）。"""

    iid: str
    root: Path
    port: int
    token: str

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def db(self) -> Path:
        return self.root / "config" / "inbox.db"

    @property
    def logs(self) -> Path:
        return self.root / "logs"


def _is_suspended(iid: str) -> bool:
    """暂停旗判定，与 ``tenant_ops`` 同一语义（``.ops/suspended/<iid>.flag``）。

    刻意不下沉进 ``_data_root``：那条契约回答「哪些根有数据」，被夜间渲染等多个
    离线工具共用；「该不该在跑」只有本巡检需要，混进去会改掉别人的行为。
    """
    try:
        return (instances_base() / ".ops" / "suspended" / f"{iid}.flag").is_file()
    except Exception:
        return False


def build_targets(cli_root: str = "", token_override: str = "") -> list:
    """按数据根契约展开巡检目标；暂停中的租户不计入（它本就不该在跑）。"""
    roots = resolve_data_roots(cli_root)
    out = []
    for root in roots:
        iid = root.parent.name if root.name == "data" else root.name
        if _is_suspended(iid):
            continue
        cfg = load_merged_config(root)
        wa = cfg.get("web_admin") or {}
        try:
            port = int(wa.get("port") or 18799)
        except (TypeError, ValueError):
            port = 18799
        token = str(wa.get("auth_token") or "")
        # --token 只在单目标时接管：多实例下一把 token 打所有端口必然 401，
        # 那正是本次事故的形态，不能让 CLI 再造一次。
        if token_override and len(roots) == 1:
            token = token_override
        out.append(Target(iid=iid, root=root, port=port, token=token))
    return out


def _http_error_label(exc: BaseException) -> str:
    """把失败原因收敛成可读标签——原实现只记 ``HTTPError``，把 401 藏了两周。"""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    return type(exc).__name__


def _main_py_processes() -> int:
    """本机 main.py 进程数；探测失败返回 -1（不阻断报告、也不误报）。"""
    try:
        import subprocess
        # @(...) 强制数组：PS 5.1 下单个对象无 .Count（会输出空串误报 0 实例）
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'main\\.py' }).Count"],
            capture_output=True, text=True, timeout=20)
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return -1


def _probe_web(t: Target) -> dict:
    """该实例 web 就绪探测（/login 无需鉴权，200 即活）。"""
    out = {"web_ready": False, "probe_ms": None, "web_error": ""}
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(f"{t.base}/login", timeout=5) as r:
            out["web_ready"] = (r.status == 200)
        out["probe_ms"] = int((time.monotonic() - t0) * 1000)
    except Exception as exc:
        out["web_error"] = _http_error_label(exc)
    return out


def _connect_ro(db: Path):
    """只读连接活体生产库（连 mtime 都不动，巡检对被观测对象零副作用）。"""
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _thread_latency(t: Target, n: int = 5) -> tuple:
    """对最近活跃 n 个会话实测 /thread 延迟，返回 ``(样本, skip)``。

    ``skip`` 为 ``{"code","text"}``；``code`` 落在 :data:`SKIP_BLIND` 里才算失明。
    静默返回空列表是「监控假装在工作」的温床，所以任何采不到样都要留下 code。
    """
    if not t.token:
        return [], {"code": "no_token", "text": "该实例配置里没有 web_admin.auth_token"}
    if not t.db.is_file():
        return [], {"code": "no_db", "text": f"找不到 inbox.db（{t.db}）"}
    try:
        conn = _connect_ro(t.db)
        conn.row_factory = sqlite3.Row
        convs = conn.execute(
            "SELECT platform, account_id, chat_key FROM conversations "
            "WHERE platform != 'web' ORDER BY last_ts DESC LIMIT ?", (n,)).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        return [], {"code": "db_error", "text": f"读 inbox.db 失败: {type(exc).__name__}"}
    if not convs:
        return [], {"code": "no_conversations", "text": "该实例还没有非 web 会话（新实例）"}
    out = []
    for c in convs:
        url = (f"{t.base}/api/unified-inbox/thread?platform={c['platform']}"
               f"&account_id={c['account_id']}&chat_key={c['chat_key']}&limit=100")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {t.token}"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                ok = (r.status == 200)
            out.append({"conv": f"{c['platform']}:{c['chat_key']}",
                        "ms": int((time.monotonic() - t0) * 1000), "ok": ok})
        except Exception as exc:
            out.append({"conv": f"{c['platform']}:{c['chat_key']}",
                        "ms": int((time.monotonic() - t0) * 1000),
                        "ok": False, "error": _http_error_label(exc)})
    return out, {}


def _xlate_stock(t: Target, top_n: int = 20) -> dict:
    """top_n 活跃会话的未译存量（与 backfill 候选同判定；应随 backfill 趋零）。"""
    empty = {"untranslated_stock": 0, "by_conv": {}, "daily_7d": []}
    if not t.db.is_file():
        return empty
    try:
        conn = _connect_ro(t.db)
    except sqlite3.Error:
        return empty
    conn.row_factory = sqlite3.Row
    try:
        convs = conn.execute(
            "SELECT conversation_id FROM conversations ORDER BY last_ts DESC LIMIT ?",
            (top_n,)).fetchall()
        total = 0
        by_conv = {}
        for c in convs:
            cid = c["conversation_id"]
            rows = conn.execute(
                "SELECT source_lang, text, translated_text, target_lang FROM messages "
                "WHERE conversation_id=? AND direction='in' ORDER BY ts DESC LIMIT 100",
                (cid,)).fetchall()
            n = 0
            for r in rows:
                text = (r["text"] or "").strip()
                if not text or len(text) > 400:
                    continue
                if (r["translated_text"] or "") and r["target_lang"] == "zh":
                    continue  # 已处理（含 noop 标记）
                lang = r["source_lang"] or "unknown"
                if not lang or lang == "unknown":
                    lang = detect_language(text)
                if lang == "zh":
                    continue
                n += 1
            if n:
                by_conv[cid] = n
                total += n
        # 按日漏斗近 7 天
        since = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
        daily = [dict(r) for r in conn.execute(
            "SELECT day, translated, failed, noop, deferred FROM inbound_xlate_daily "
            "WHERE day >= ? ORDER BY day", (since,)).fetchall()]
    except sqlite3.Error:
        return empty
    finally:
        conn.close()
    return {"untranslated_stock": total, "by_conv": by_conv, "daily_7d": daily}


def _restart_counts(t: Target, days: int = 7) -> dict:
    """按日统计 restart_*/boot_* 日志（重启纪律执行情况，目标 ≤3 次/日）。"""
    pat = re.compile(r"^(?:restart|boot)_(\d{8})_\d{6}\.out\.log$")
    counter: Counter = Counter()
    cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - days * 86400))
    if not t.logs.is_dir():
        return {}
    for f in t.logs.glob("*.out.log"):
        m = pat.match(f.name)
        if m and m.group(1) >= cutoff:
            counter[m.group(1)] += 1
    return dict(sorted(counter.items()))


def _hot_reload_counts(t: Target, days: int = 7) -> dict:
    """按日统计 app.log 里的热重载事件（config / i18n 免重启通道的使用证据）。

    只扫当前 app.log（轮转旧档不追溯，日粒度趋势足够）。
    """
    pats = {
        "config": re.compile(r"^\[(\d{4}-\d{2}-\d{2}) .*配置热重载完成"),
        "i18n": re.compile(r"^\[(\d{4}-\d{2}-\d{2}) .*web_i18n 热重载完成"),
    }
    out: dict = {"config": Counter(), "i18n": Counter()}
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    log = t.logs / "app.log"
    if not log.exists():
        return {k: {} for k in out}
    try:
        with log.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                for kind, pat in pats.items():
                    m = pat.match(line)
                    if m and m.group(1) >= cutoff:
                        out[kind][m.group(1)] += 1
                        break
    except OSError:
        pass
    return {k: dict(sorted(v.items())) for k, v in out.items()}


def _unclean_deaths_today(t: Target) -> int:
    """今日「非正常死亡」哨兵告警行数（exit_sentinel 启动检测写入 app.log）。"""
    log = t.logs / "app.log"
    if not log.exists():
        return 0
    today = time.strftime("%Y-%m-%d")
    n = 0
    try:
        with log.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith(f"[{today}") and "非正常死亡" in line:
                    n += 1
    except OSError:
        pass
    return n


def _detect_alerts(report: dict) -> list:
    """异常判定（观察期的「主动上报」规则，供 --alert 弹窗/留痕）。

    实例数判据＝**进程数 vs 活跃实例数**。硬编码 `!= 1` 是单实例时代的遗物，
    迁多实例后恒红两周（zhiliao 生产 + zhiliao_pilot 受保护租户 = 正常的 2 个）。
    """
    alerts = []
    inst = report["instances"]
    n_proc = inst.get("main_py_processes")
    expected = inst.get("expected_instances") or 0
    ids = ", ".join(inst.get("instance_ids") or []) or "-"
    if n_proc == 0:
        alerts.append(f"main.py 进程数 0 —— 服务全死（应有 {expected} 个: {ids}）")
    elif n_proc is not None and n_proc > 0 and expected and n_proc != expected:
        why = "少了＝有实例没起来" if n_proc < expected else "多了＝多实例踩踏"
        alerts.append(
            f"main.py 进程数 {n_proc} ≠ 活跃实例数 {expected}（{why}；活跃实例: {ids}）")

    for it in report["by_instance"]:
        tag = it["instance"]
        if not it["web_ready"]:
            alerts.append(
                f"[{tag}] web 后台({it['port']})不可达: {it.get('web_error') or '-'}")
        skip = it.get("skip") or {}
        if skip.get("code") in SKIP_BLIND:
            alerts.append(f"[{tag}] /thread 未采样（{skip['text']}）—— 巡检对该实例失明")
        lat = it["thread_latency"]
        bad = [x for x in lat if not x.get("ok")]
        if bad:
            kinds = "/".join(sorted({x.get("error", "?") for x in bad}))
            alerts.append(f"[{tag}] /thread 采样失败 {len(bad)}（{kinds}）: "
                          f"{[x['conv'] for x in bad]}")
        worst = max((x["ms"] for x in lat if x.get("ok")), default=0)
        if worst > THREAD_SLOW_MS:
            alerts.append(f"[{tag}] /thread 最慢 {worst}ms（回归「加载超时」前兆）")
        rt = it["restarts_by_day"].get(time.strftime("%Y%m%d"), 0)
        if rt > RESTART_RED_LINE:
            alerts.append(f"[{tag}] 今日重启 {rt} 次，超纪律红线({RESTART_RED_LINE})")
        if it["unclean_deaths_today"]:
            alerts.append(f"[{tag}] 今日非正常死亡 {it['unclean_deaths_today']} 次"
                          f"（查 {it['logs']}\\app.log『非正常死亡』行）")
    return alerts


def _emit_alerts(alerts: list) -> None:
    """告警出口：host_alert（弹窗+EventBus 镜像+去抖）+ 专用留痕文件。

    health_report 是独立进程——host_alert 的 logger 无 file handler（进不了
    app.log），故自留 logs/health_alerts.log 一行（计划任务场景无人看 stdout）。
    留痕仍落引擎根 logs：它是「巡检自己的账本」，不属于任何被观测实例。
    """
    msg = "\n".join(f"- {a}" for a in alerts)
    try:
        from src.utils.host_alert import notify_host
        notify_host("生产健康告警（health_report）", msg,
                    key="health_report", cooldown_sec=300)
    except Exception:
        pass
    try:
        log_dir = ENGINE_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "health_alerts.log").open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n{msg}\n")
    except OSError:
        pass


def collect(targets: list) -> dict:
    """跑一轮巡检，返回完整报告（纯数据，供人读/机读/告警三个消费面同源）。"""
    by_instance = []
    for t in targets:
        lat, skip = _thread_latency(t)
        row = {
            "instance": t.iid,
            "root": str(t.root),
            "port": t.port,
            "logs": str(t.logs),
            "thread_latency": lat,
            "skip": skip,
            "inbound_xlate": _xlate_stock(t),
            "restarts_by_day": _restart_counts(t),
            "hot_reloads_by_day": _hot_reload_counts(t),
            "unclean_deaths_today": _unclean_deaths_today(t),
        }
        row.update(_probe_web(t))
        by_instance.append(row)
    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "instances": {
            "main_py_processes": _main_py_processes(),
            "expected_instances": len(targets),
            "instance_ids": [t.iid for t in targets],
        },
        "by_instance": by_instance,
    }
    report["alerts"] = _detect_alerts(report)
    return report


def _trend_row(report: dict) -> dict:
    """趋势行（不携带逐会话明细，只留可画线的聚合数 + 逐实例分解）。"""
    _today_c = time.strftime("%Y%m%d")
    _today_d = time.strftime("%Y-%m-%d")
    per = {}
    for it in report["by_instance"]:
        lat = it["thread_latency"]
        hr = it["hot_reloads_by_day"]
        per[it["instance"]] = {
            "web_ready": it["web_ready"],
            "thread_worst_ms": max((x["ms"] for x in lat), default=None),
            "thread_fail": sum(1 for x in lat if not x.get("ok")),
            "thread_skipped": (it.get("skip") or {}).get("code", ""),
            "xlate_stock": it["inbound_xlate"]["untranslated_stock"],
            "restarts_today": it["restarts_by_day"].get(_today_c, 0),
            "hot_reloads_today": (hr.get("config", {}).get(_today_d, 0)
                                  + hr.get("i18n", {}).get(_today_d, 0)),
            "unclean_deaths_today": it["unclean_deaths_today"],
        }
    vals = list(per.values())
    return {
        "ts": report["ts"],
        "instances": report["instances"]["main_py_processes"],
        "expected_instances": report["instances"]["expected_instances"],
        "web_ready": all(v["web_ready"] for v in vals) if vals else False,
        "thread_worst_ms": max((v["thread_worst_ms"] for v in vals
                                if v["thread_worst_ms"] is not None), default=None),
        "thread_fail": sum(v["thread_fail"] for v in vals),
        "xlate_stock": sum(v["xlate_stock"] for v in vals),
        "restarts_today": sum(v["restarts_today"] for v in vals),
        "hot_reloads_today": sum(v["hot_reloads_today"] for v in vals),
        "unclean_deaths_today": sum(v["unclean_deaths_today"] for v in vals),
        "alerts": len(report["alerts"]),
        "by_instance": per,
    }


def _print_human(report: dict) -> None:
    inst = report["instances"]
    n_proc = inst["main_py_processes"]
    exp = inst["expected_instances"]
    mark = "✅" if n_proc == exp else ("？" if n_proc < 0 else "⚠")
    print(f"== 健康报告 {report['ts']} ==")
    print(f"[实例] main.py 进程 x{n_proc} / 活跃实例 x{exp} {mark}"
          f"  ({', '.join(inst['instance_ids']) or '-'})")
    for it in report["by_instance"]:
        print(f"\n-- {it['instance']} :{it['port']} · {it['root']} --")
        print(f"  [web] {'✅' if it['web_ready'] else '❌'} ({it.get('probe_ms')}ms)"
              + (f" · {it['web_error']}" if it.get("web_error") else ""))
        lat = it["thread_latency"]
        skip = it.get("skip") or {}
        if skip:
            mark = "⚠" if skip["code"] in SKIP_BLIND else "·"
            print(f"  [/thread] {mark} 未采样: {skip['text']}")
        elif lat:
            worst = max(x["ms"] for x in lat)
            bad = [x for x in lat if not x["ok"]]
            print(f"  [/thread] 采样 {len(lat)} 会话 · 最慢 {worst}ms"
                  + (f" · 失败 {len(bad)}: {[x['conv'] for x in bad]}" if bad else " · 全部 OK"))
            for x in lat:
                print(f"      {x['conv']:44s} {x['ms']:>6}ms "
                      f"{'OK' if x['ok'] else 'FAIL ' + x.get('error', '')}")
        ix = it["inbound_xlate"]
        print(f"  [入站翻译] top20 活跃会话未译存量: {ix['untranslated_stock']}"
              + (f"（{ix['by_conv']}）" if ix["by_conv"] else "（已清空 ✅）"))
        for d in ix["daily_7d"]:
            print(f"      {d['day']}: 译出 {d['translated']} · noop {d['noop']}"
                  f" · 转后台 {d['deferred']} · 失败 {d['failed']}")
        rc = it["restarts_by_day"]
        flagged = {k: v for k, v in rc.items() if v > RESTART_RED_LINE}
        print(f"  [重启频率] {rc}"
              + (f" ⚠ 超纪律（>{RESTART_RED_LINE}/日）: {flagged}" if flagged else " ✅"))
        hr = it["hot_reloads_by_day"]
        print(f"  [免重启通道] config 热重载 {hr.get('config') or '{}'}"
              f" · i18n 热加载 {hr.get('i18n') or '{}'}")
        if it["unclean_deaths_today"]:
            print(f"  [退出哨兵] ⚠ 今日非正常死亡 {it['unclean_deaths_today']} 次")
    print()
    if report["alerts"]:
        for a in report["alerts"]:
            print(f"[告警] {a}")
    else:
        print("[告警] 无 ✅")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--jsonl", default="",
                    help="单行 JSON 追加到指定文件（计划任务每日跟踪用），成功时静默")
    ap.add_argument("--alert", action="store_true",
                    help="异常时主动告警（host_alert 弹窗 + logs/health_alerts.log 留痕）")
    ap.add_argument("--data-root", default="",
                    help="只巡检指定实例数据根（默认自动发现本机全部活跃实例）")
    ap.add_argument("--token", default="",
                    help="web_admin.auth_token 覆写（仅单目标时生效；默认逐实例自取）")
    args = ap.parse_args()

    targets = build_targets(args.data_root, args.token)
    if not targets:
        print("[health_report] 没有可巡检的活跃实例（全部退役/暂停？）", file=sys.stderr)
        return 0

    report = collect(targets)
    if args.alert and report["alerts"]:
        _emit_alerts(report["alerts"])

    if args.jsonl:
        out = Path(args.jsonl)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_trend_row(report), ensure_ascii=False) + "\n")
        return 0

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    _print_human(report)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
