#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""grant 软门控切换 enforce 前的证据化就绪检查器（仅 Python 标准库，对仓库只读）。

契约：platform/identity/PERSONA_BUS.md §4.1（运行时软门控）/ §4.2（enforce 切换流程）
配套：tools/persona_bus/fetch_grants.py（拉取写缓存）、platform/identity/grant_gate.py（消费缓存）

设计原则：切 enforce 必须基于证据——缓存持续新鲜、结构合法、非空、（可选）同步通道
可达——而不是拍脑袋。本脚本逐项输出 PASS/WARN/FAIL 清单并以退出码表达结论：
0 = ready（允许切换）；1 = not ready（暂缓，存在 FAIL 项）；2 = 用法错误。

检查项（固定 8 项，无法执行的标 SKIP）：
  cache_file    缓存文件存在且可读
  json_valid    JSON 合法且根为对象
  version       version == 1
  system        system 非空、在引擎枚举内、与 --engine 一致
  grants_schema grants 数组每条含 source_key/product_id/status（与 grant_gate.load_cache 同口径）
  freshness     fetched_at 可解析且 age ≤ --max-age-hours（缺省 24h，与门控 stale 阈值一致）
  grants_count  条数 ≥ --min-grants；空清单合法但 WARN（enforce 后将拒绝一切跨产品引用）
  probe         （可选）GET --probe-url 判 sync API 可达；失败降级 WARN，--require-probe 时计 FAIL

用法::

    # 按引擎推导缺省缓存路径 engines/<engine>/data/persona_grants_cache.json
    python tools/persona_bus/enforce_readiness.py --engine avatarhub

    # 显式路径 + 在线探测 + 机器可读输出（供 cron 日志归档）
    python tools/persona_bus/enforce_readiness.py --cache data/persona_bus_out/avatarhub_grants.json \
        --probe-url "https://bd2026.cc/api/sync/personas/grants?system=avatarhub" --json

自测::

    python tools/persona_bus/enforce_readiness.py --selftest

【重要】本脚本自含实现、不 import grant_gate——勿把仓库根加入 sys.path 后
``import platform.identity``（与标准库 platform 冲突，见 grant_gate.py 文件头）。
判定口径（fetched_at 解析、24h 阈值、grants 字段校验）与 grant_gate.py 保持一致，
grant_gate 改口径时本脚本须同步。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SOURCE_SYSTEMS = ("avatarhub", "chengjie", "huoke")
DEFAULT_MAX_AGE_HOURS = 24.0
DEFAULT_MIN_GRANTS = 0
PROBE_TIMEOUT_S = 5.0

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

# tools/persona_bus/enforce_readiness.py → 仓库根
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_fetched_at(raw: Any) -> datetime | None:
    """解析缓存 fetched_at（与 grant_gate.parse_fetched_at 同口径）；失败返回 None。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def default_cache_path(engine: str) -> Path:
    """--engine 推导缺省缓存路径（PERSONA_BUS.md §4.1 路径约定表）。

    env ``PERSONA_GRANT_CACHE`` 若设置则优先——与 grant_gate 运行时解析一致，
    保证检查的就是引擎实际会读的那份缓存。
    """
    env_path = (os.environ.get("PERSONA_GRANT_CACHE") or "").strip()
    if env_path:
        return Path(env_path)
    return _REPO_ROOT / "engines" / engine / "data" / "persona_grants_cache.json"


def _item(check_id: str, status: str, detail: str) -> dict[str, str]:
    return {"id": check_id, "status": status, "detail": detail}


def probe_sync_api(url: str, key: str = "", timeout: float = PROBE_TIMEOUT_S) -> tuple[bool, str]:
    """GET 探测 sync API 可达性；返回 (成功, 说明)，不抛异常。带 key 时加 Bearer。"""
    headers = {
        "Accept": "application/json",
        "User-Agent": "boundless-persona-bus-enforce-readiness/1.0",
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as e:
        snippet = ""
        try:
            snippet = e.read().decode("utf-8", errors="replace")[:200].strip()
        except Exception:
            pass
        return False, (
            f"HTTP {e.code}：{snippet or e.reason}"
            "（可达但被拒/出错；401 多为 key 不匹配，503 为服务端未配置 key）"
        )
    except urllib.error.URLError as e:
        return False, f"网络错误：{e.reason!r}（DNS/防火墙/超时 {timeout:g}s）"
    except Exception as e:  # 兜底 socket 层超时等
        return False, f"探测异常：{e!r}"
    if status != 200:
        return False, f"HTTP {status}（非 200）"
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False, "HTTP 200 但响应非 JSON（可能被代理/登录页拦截）"
    if not isinstance(payload, dict) or not payload.get("ok"):
        return False, f"HTTP 200 但业务失败：{str(payload)[:200]}"
    return True, f"HTTP 200 ok=true count={payload.get('count')}"


def run_checks(
    cache_path: str | Path,
    *,
    engine: str | None = None,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    min_grants: int = DEFAULT_MIN_GRANTS,
    probe_url: str | None = None,
    probe_key: str = "",
    require_probe: bool = False,
) -> dict[str, Any]:
    """执行全部检查，返回报告 dict（ready/checks/summary…）。只读，不写任何文件。"""
    checks: list[dict[str, str]] = []
    p = Path(cache_path)

    # 1) 缓存文件存在且可读
    raw_text: str | None = None
    if p.is_file():
        try:
            raw_text = p.read_text(encoding="utf-8")
            checks.append(_item("cache_file", PASS, f"存在（{p.stat().st_size} bytes）"))
        except UnicodeDecodeError as e:
            checks.append(_item("cache_file", FAIL, f"存在但非 UTF-8 文本：{e}"))
        except OSError as e:
            checks.append(_item("cache_file", FAIL, f"存在但不可读：{e}"))
    else:
        checks.append(
            _item("cache_file", FAIL, f"缓存文件不存在：{p}（先跑 fetch_grants.py 拉取）")
        )

    # 2) JSON 合法且根为对象
    doc: dict[str, Any] | None = None
    if raw_text is None:
        checks.append(_item("json_valid", SKIP, "无文件内容可解析"))
    else:
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as e:
            parsed = None
            checks.append(_item("json_valid", FAIL, f"JSON 损坏：{e}"))
        if isinstance(parsed, dict):
            doc = parsed
            checks.append(_item("json_valid", PASS, "JSON 合法，根为对象"))
        elif parsed is not None:
            checks.append(
                _item("json_valid", FAIL, f"根必须是对象，实为 {type(parsed).__name__}")
            )

    if doc is None:
        for cid in ("version", "system", "grants_schema", "freshness", "grants_count"):
            checks.append(_item(cid, SKIP, "缓存不可用，跳过"))
    else:
        # 3) version
        ver = doc.get("version")
        if ver == 1:
            checks.append(_item("version", PASS, "version=1"))
        else:
            checks.append(_item("version", FAIL, f"version={ver!r}（本契约只认 1）"))

        # 4) system
        system = doc.get("system")
        sys_name = system.strip() if isinstance(system, str) else ""
        if not sys_name:
            checks.append(_item("system", FAIL, "system 缺失或非字符串"))
        elif engine and sys_name != engine:
            checks.append(_item(
                "system", FAIL,
                f"system={sys_name!r} 与 --engine {engine} 不一致"
                "（grant_gate 将按 system_mismatch 放行，enforce 形同虚设）",
            ))
        elif sys_name not in SOURCE_SYSTEMS:
            checks.append(_item(
                "system", WARN,
                f"system={sys_name!r} 不在已知引擎枚举 {'/'.join(SOURCE_SYSTEMS)}",
            ))
        else:
            suffix = "（与 --engine 一致）" if engine else ""
            checks.append(_item("system", PASS, f"system={sys_name}{suffix}"))

        # 5) grants 结构（与 grant_gate.load_cache 同口径：坏一条即不合格）
        grants = doc.get("grants")
        granted_n = 0
        schema_err: str | None = None
        if not isinstance(grants, list):
            schema_err = "grants 必须是数组"
            grants = []
        else:
            for i, g in enumerate(grants):
                if not isinstance(g, dict):
                    schema_err = f"grants[{i}] 非对象"
                    break
                for key in ("source_key", "product_id", "status"):
                    v = g.get(key)
                    if not isinstance(v, str) or not v.strip():
                        schema_err = f"grants[{i}].{key} 缺失或非字符串"
                        break
                if schema_err:
                    break
                if str(g.get("status", "")).strip().lower() == "granted":
                    granted_n += 1
        if schema_err:
            checks.append(_item(
                "grants_schema", FAIL,
                f"{schema_err}（enforce 前缓存必须干净，重跑 fetch_grants.py）",
            ))
        else:
            checks.append(_item(
                "grants_schema", PASS,
                f"{len(grants)} 条结构合法（granted={granted_n}，"
                f"revoked/其他={len(grants) - granted_n}）",
            ))

        # 6) fetched_at 新鲜度
        fetched_raw = doc.get("fetched_at")
        fetched = parse_fetched_at(fetched_raw)
        if fetched is None:
            checks.append(_item(
                "freshness", FAIL,
                f"fetched_at={fetched_raw!r} 无法解析（grant_gate 将视为 stale）",
            ))
        else:
            age_h = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600.0
            if age_h > float(max_age_hours):
                checks.append(_item(
                    "freshness", FAIL,
                    f"缓存已过期：age={age_h:.1f}h > 阈值 {max_age_hours:g}h"
                    f"（fetched_at={fetched_raw}；检查 grants_sync 定时任务）",
                ))
            elif age_h < -0.1:
                checks.append(_item(
                    "freshness", WARN,
                    f"fetched_at={fetched_raw} 在未来 {-age_h:.1f}h（时钟偏移？）",
                ))
            else:
                checks.append(_item(
                    "freshness", PASS,
                    f"age={age_h:.1f}h ≤ 阈值 {max_age_hours:g}h（fetched_at={fetched_raw}）",
                ))

        # 7) grants 条数
        if schema_err:
            checks.append(_item("grants_count", SKIP, "grants 结构不合法，条数不作判定"))
        elif len(grants) < min_grants:
            checks.append(_item(
                "grants_count", FAIL, f"grants={len(grants)} < --min-grants {min_grants}",
            ))
        elif len(grants) == 0:
            checks.append(_item(
                "grants_count", WARN,
                "grants=0：合法但 enforce 后将拒绝一切跨产品引用，"
                "请确认控制台确实未授权任何 persona",
            ))
        elif granted_n == 0:
            checks.append(_item(
                "grants_count", WARN,
                f"grants={len(grants)} 但 granted=0（全部 revoked）：enforce 后没有任何放行项",
            ))
        else:
            checks.append(_item(
                "grants_count", PASS,
                f"grants={len(grants)}（granted={granted_n}）≥ --min-grants {min_grants}",
            ))

    # 8) sync API 在线探测（可选）
    if not probe_url:
        checks.append(_item("probe", SKIP, "未提供 --probe-url，跳过 sync API 在线探测"))
    else:
        ok, detail = probe_sync_api(probe_url, probe_key)
        if ok:
            checks.append(_item("probe", PASS, detail))
        elif require_probe:
            checks.append(_item("probe", FAIL, f"{detail}（--require-probe 生效）"))
        else:
            checks.append(_item(
                "probe", WARN,
                f"{detail}（探测失败降级 WARN；--require-probe 可升级为 FAIL）",
            ))

    summary = {s.lower(): sum(1 for c in checks if c["status"] == s)
               for s in (PASS, WARN, FAIL, SKIP)}
    return {
        "ready": summary["fail"] == 0,
        "cache": str(p),
        "engine": engine,
        "max_age_hours": float(max_age_hours),
        "min_grants": int(min_grants),
        "probe_url": probe_url,
        "checked_at": _now_iso(),
        "checks": checks,
        "summary": summary,
    }


def render_human(report: dict[str, Any]) -> str:
    """人类可读的逐项清单 + 最终结论。"""
    head = f"enforce 就绪检查 · cache={report['cache']}"
    if report.get("engine"):
        head += f" · engine={report['engine']}"
    head += f" · 阈值={report['max_age_hours']:g}h · min_grants={report['min_grants']}"
    lines = [head]
    for c in report["checks"]:
        lines.append(f"  [{c['status']:<4}] {c['id']:<13} {c['detail']}")
    s = report["summary"]
    lines.append(f"  —— PASS={s['pass']} WARN={s['warn']} FAIL={s['fail']} SKIP={s['skip']}")
    if report["ready"]:
        lines.append(
            "结论：READY —— 证据齐备，可按 PERSONA_BUS.md §4.2 切换 enforce"
            "（设 PERSONA_GRANT_ENFORCE=1 并重启引擎）。"
        )
        if s["warn"]:
            lines.append(f"      注意：仍有 {s['warn']} 项 WARN，切换前请逐条确认为预期。")
    else:
        lines.append("结论：NOT READY —— 存在 FAIL 项，暂缓切换 enforce；修复后重跑本检查。")
    return "\n".join(lines)


# ── selftest ──────────────────────────────────────────────────────────


def _selftest() -> int:
    """tempfile 造缓存覆盖 fresh/stale/缺文件/坏 JSON/空 grants 等场景；全过退出 0。"""

    def _write(path: Path, doc: Any) -> Path:
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return path

    def _status(report: dict[str, Any], cid: str) -> str:
        return next(c["status"] for c in report["checks"] if c["id"] == cid)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = {
        "version": 1,
        "fetched_at": now,
        "system": "avatarhub",
        "grants": [
            {"source_key": "主播小雅", "product_id": "huanying", "status": "granted"},
            {"source_key": "主播小雅", "product_id": "zhituo", "status": "revoked"},
        ],
    }

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # 1) fresh 缓存 → READY；probe 未请求 → SKIP
        r = run_checks(_write(root / "fresh.json", base), engine="avatarhub")
        assert r["ready"] is True, r
        assert _status(r, "freshness") == PASS and _status(r, "grants_count") == PASS, r
        assert _status(r, "probe") == SKIP, r

        # 2) stale 缓存（2000 年）→ NOT READY（freshness FAIL）
        stale = dict(base)
        stale["fetched_at"] = "2000-01-01T00:00:00Z"
        r = run_checks(_write(root / "stale.json", stale))
        assert r["ready"] is False and _status(r, "freshness") == FAIL, r

        # 3) 缺文件 → NOT READY（cache_file FAIL，下游 SKIP）
        r = run_checks(root / "nope.json")
        assert r["ready"] is False and _status(r, "cache_file") == FAIL, r
        assert _status(r, "version") == SKIP, r

        # 4) 坏 JSON → NOT READY（json_valid FAIL）
        bad = root / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        r = run_checks(bad)
        assert r["ready"] is False and _status(r, "json_valid") == FAIL, r

        # 5) 空 grants + 缺省 min-grants=0 → READY 但 WARN 提示
        empty = dict(base)
        empty["grants"] = []
        r = run_checks(_write(root / "empty.json", empty))
        assert r["ready"] is True and _status(r, "grants_count") == WARN, r

        # 6) 空 grants + --min-grants 1 → NOT READY
        r = run_checks(root / "empty.json", min_grants=1)
        assert r["ready"] is False and _status(r, "grants_count") == FAIL, r

        # 7) version 不合法 → NOT READY
        v2 = dict(base)
        v2["version"] = 2
        r = run_checks(_write(root / "v2.json", v2))
        assert r["ready"] is False and _status(r, "version") == FAIL, r

        # 8) system 与 --engine 不一致 → NOT READY
        r = run_checks(root / "fresh.json", engine="chengjie")
        assert r["ready"] is False and _status(r, "system") == FAIL, r

        # 9) fetched_at 不可解析 → NOT READY（与 grant_gate 视为 stale 同口径）
        nofa = dict(base)
        nofa["fetched_at"] = "someday"
        r = run_checks(_write(root / "nofa.json", nofa))
        assert r["ready"] is False and _status(r, "freshness") == FAIL, r

        # 10) grants 结构脏（缺 product_id）→ NOT READY，条数 SKIP
        dirty = dict(base)
        dirty["grants"] = [{"source_key": "x", "status": "granted"}]
        r = run_checks(_write(root / "dirty.json", dirty))
        assert r["ready"] is False and _status(r, "grants_schema") == FAIL, r
        assert _status(r, "grants_count") == SKIP, r

        # 11) 全部 revoked → READY 但 WARN（enforce 后无放行项）
        rev = dict(base)
        rev["grants"] = [
            {"source_key": "x", "product_id": "huanying", "status": "revoked"},
        ]
        r = run_checks(_write(root / "rev.json", rev))
        assert r["ready"] is True and _status(r, "grants_count") == WARN, r

        # 12) --json 输出可 round-trip、键齐全、检查项数固定
        r = run_checks(root / "fresh.json", engine="avatarhub")
        blob = json.loads(json.dumps(r, ensure_ascii=False))
        assert blob["ready"] is True, blob
        assert {"cache", "checks", "summary", "checked_at"} <= set(blob), blob
        assert len(blob["checks"]) == 8, blob

    print("enforce_readiness selftest OK（12 场景全过）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="grant 软门控切 enforce 前的证据化就绪检查（PERSONA_BUS.md §4.2）",
    )
    ap.add_argument(
        "--cache",
        help="grant 缓存 JSON 路径（与 --engine 至少给一个；同给时 --cache 优先）",
    )
    ap.add_argument(
        "--engine",
        choices=SOURCE_SYSTEMS,
        help="引擎名：推导缺省缓存 engines/<engine>/data/persona_grants_cache.json"
             "（env PERSONA_GRANT_CACHE 优先），并校验缓存 system 一致",
    )
    ap.add_argument(
        "--max-age-hours",
        type=float,
        default=DEFAULT_MAX_AGE_HOURS,
        help=f"缓存新鲜度阈值小时（缺省 {DEFAULT_MAX_AGE_HOURS:g}，与门控 stale 阈值一致）",
    )
    ap.add_argument(
        "--min-grants",
        type=int,
        default=DEFAULT_MIN_GRANTS,
        help=f"grants 最少条数（缺省 {DEFAULT_MIN_GRANTS}；空清单合法但 WARN）",
    )
    ap.add_argument(
        "--probe-url",
        help="可选：GET 探测 sync API 可达性"
             "（如 <base>/api/sync/personas/grants?system=<engine>，超时 5s）",
    )
    ap.add_argument(
        "--key",
        default=(os.environ.get("EVENT_INGEST_KEY") or "").strip(),
        help="探测用 Bearer 密钥（缺省 env EVENT_INGEST_KEY；留空则匿名 GET）",
    )
    ap.add_argument(
        "--require-probe",
        action="store_true",
        help="探测失败按 FAIL 计（缺省仅降级 WARN）",
    )
    ap.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    ap.add_argument("--selftest", action="store_true", help="tempfile 自测（不触网）")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    if not args.cache and not args.engine:
        ap.error("--cache 与 --engine 至少提供一个")
    if args.require_probe and not args.probe_url:
        ap.error("--require-probe 需要同时提供 --probe-url")

    cache = Path(args.cache) if args.cache else default_cache_path(args.engine)
    report = run_checks(
        cache,
        engine=args.engine,
        max_age_hours=args.max_age_hours,
        min_grants=args.min_grants,
        probe_url=args.probe_url,
        probe_key=args.key,
        require_probe=args.require_probe,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_human(report))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
