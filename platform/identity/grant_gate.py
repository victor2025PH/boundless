#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""人设跨产品引用授权 —— 运行时软门控（PERSONA_BUS v1.2，仅 Python 标准库）。

设计原则（比强制拒绝更安全、可回滚）：
  - **默认 warn**：无 grant / 缓存缺失 / 过期 → **放行**，打 AUDIT/warning 日志；
  - **enforce**：仅当 env ``PERSONA_GRANT_ENFORCE`` 为 1/true/yes/on，或
    ``check(..., enforce=True)`` 时，无有效 grant 才 ``allowed=False``；
  - 缓存可离线：断网不挡业务；过期（默认 24h）仍可用，仅标 stale。

缓存 JSON（由 ``tools/persona_bus/fetch_grants.py`` 从集团 sync API 拉取写入）::

    {"version":1,"fetched_at":"<ISO8601>","system":"avatarhub",
     "grants":[{"source_key":"<键>","product_id":"huanying","status":"granted"}]}

用法::

    from grant_gate import load_cache, check
    load_cache("data/persona_bus_out/avatarhub_grants.json")
    r = check("avatarhub", "主播小雅", "huanying")
    # r == {"allowed": True, "reason": "granted", "mode": "warn"}

自测::

    python platform/identity/grant_gate.py --selftest

【重要】勿把仓库根加入 sys.path 后 ``import platform.identity``——会与标准库
platform 冲突；把本目录加入 path 后 ``import grant_gate``，或用 importlib 按路径加载。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "CACHE_ENV",
    "DEFAULT_MAX_AGE_HOURS",
    "ENFORCE_ENV",
    "LOG",
    "check",
    "is_enforce",
    "load_cache",
    "parse_fetched_at",
]

ENFORCE_ENV = "PERSONA_GRANT_ENFORCE"
CACHE_ENV = "PERSONA_GRANT_CACHE"
DEFAULT_MAX_AGE_HOURS = 24

LOG = logging.getLogger("persona.grant_gate")

# load_cache 写入的模块级缓存；check() 在未显式传入 cache 时使用。
_CACHE: dict[str, Any] | None = None
_CACHE_PATH: str | None = None


def is_enforce(enforce: bool | None = None) -> bool:
    """enforce 显式优先；否则读 PERSONA_GRANT_ENFORCE（1/true/yes/on 才强制）。"""
    if enforce is not None:
        return bool(enforce)
    v = (os.environ.get(ENFORCE_ENV) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def parse_fetched_at(raw: Any) -> datetime | None:
    """解析缓存 fetched_at；失败返回 None。"""
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


def load_cache(path: str | Path) -> dict[str, Any]:
    """加载本地 grant 缓存；成功后写入模块级缓存供后续 check() 使用。

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: JSON 损坏或字段不合约定
    """
    global _CACHE, _CACHE_PATH
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"grant cache JSON invalid: {e}") from e
    if not isinstance(doc, dict):
        raise ValueError("grant cache root must be object")
    if doc.get("version") != 1:
        raise ValueError(f"unsupported grant cache version: {doc.get('version')!r}")
    system = doc.get("system")
    if not isinstance(system, str) or not system.strip():
        raise ValueError("grant cache missing system")
    grants = doc.get("grants")
    if not isinstance(grants, list):
        raise ValueError("grant cache grants must be array")
    for i, g in enumerate(grants):
        if not isinstance(g, dict):
            raise ValueError(f"grants[{i}] must be object")
        for key in ("source_key", "product_id", "status"):
            if not isinstance(g.get(key), str) or not str(g.get(key)).strip():
                raise ValueError(f"grants[{i}].{key} required string")
    _CACHE = doc
    _CACHE_PATH = str(p)
    return doc


def _resolve_cache(
    cache: dict[str, Any] | None,
    cache_path: str | Path | None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """返回 (cache_doc, err_reason, path_hint)。err_reason 非空表示无法使用缓存。"""
    if cache is not None:
        return cache, None, None
    path = cache_path
    if path is None:
        env_path = (os.environ.get(CACHE_ENV) or "").strip()
        path = env_path or _CACHE_PATH
    if path:
        try:
            return load_cache(path), None, str(path)
        except FileNotFoundError:
            return None, "cache_missing", str(path)
        except (OSError, ValueError) as e:
            return None, f"cache_invalid:{e}", str(path)
    if _CACHE is not None:
        return _CACHE, None, _CACHE_PATH
    return None, "cache_missing", None


def _cache_stale(doc: dict[str, Any], max_age_hours: float) -> bool:
    fetched = parse_fetched_at(doc.get("fetched_at"))
    if fetched is None:
        return True
    age_s = (datetime.now(timezone.utc) - fetched).total_seconds()
    return age_s > float(max_age_hours) * 3600.0


def _has_granted(doc: dict[str, Any], source_key: str, product_id: str) -> bool:
    sk = source_key.strip()
    pid = product_id.strip()
    for g in doc.get("grants") or []:
        if not isinstance(g, dict):
            continue
        if str(g.get("source_key", "")).strip() != sk:
            continue
        if str(g.get("product_id", "")).strip() != pid:
            continue
        status = str(g.get("status", "")).strip().lower()
        if status == "granted":
            return True
    return False


def check(
    source_system: str,
    source_key: str,
    product_id: str,
    *,
    enforce: bool | None = None,
    cache: dict[str, Any] | None = None,
    cache_path: str | Path | None = None,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
) -> dict[str, Any]:
    """检查 product 是否被授权使用该人设。

    Returns:
        ``{"allowed": bool, "reason": str, "mode": "warn"|"enforce"}``

    reason 常见值：
      granted / no_grant / cache_missing / cache_invalid:* / system_mismatch /
      以及带 ``stale:`` 前缀的上述原因（缓存超龄但仍参与判定时）。
    """
    mode = "enforce" if is_enforce(enforce) else "warn"
    ss = (source_system or "").strip()
    sk = (source_key or "").strip()
    pid = (product_id or "").strip()

    doc, err, _hint = _resolve_cache(cache, cache_path)
    if err is not None or doc is None:
        reason = err or "cache_missing"
        # 断网/无缓存：不挡业务（enforce 也不因缺缓存拒绝——缺证据 ≠ 无授权）
        allowed = True
        LOG.warning(
            "[PERSONA_GRANT_AUDIT] mode=%s allowed=%s reason=%s "
            "system=%s source_key=%s product_id=%s",
            mode,
            allowed,
            reason,
            ss,
            sk,
            pid,
        )
        return {"allowed": allowed, "reason": reason, "mode": mode}

    stale = _cache_stale(doc, max_age_hours)
    cache_system = str(doc.get("system", "")).strip()
    if cache_system and ss and cache_system != ss:
        reason = "system_mismatch"
        if stale:
            reason = f"stale:{reason}"
        allowed = True  # 缓存系统对不上时无法判定，放行 + 告警
        LOG.warning(
            "[PERSONA_GRANT_AUDIT] mode=%s allowed=%s reason=%s "
            "system=%s cache_system=%s source_key=%s product_id=%s",
            mode,
            allowed,
            reason,
            ss,
            cache_system,
            sk,
            pid,
        )
        return {"allowed": allowed, "reason": reason, "mode": mode}

    if _has_granted(doc, sk, pid):
        reason = "granted"
        if stale:
            reason = f"stale:{reason}"
            LOG.warning(
                "[PERSONA_GRANT_AUDIT] mode=%s allowed=True reason=%s "
                "system=%s source_key=%s product_id=%s (cache past %sh)",
                mode,
                reason,
                ss,
                sk,
                pid,
                max_age_hours,
            )
        return {"allowed": True, "reason": reason, "mode": mode}

    reason = "no_grant"
    if stale:
        reason = f"stale:{reason}"
    allowed = mode != "enforce"
    LOG.warning(
        "[PERSONA_GRANT_AUDIT] mode=%s allowed=%s reason=%s "
        "system=%s source_key=%s product_id=%s",
        mode,
        allowed,
        reason,
        ss,
        sk,
        pid,
    )
    return {"allowed": allowed, "reason": reason, "mode": mode}


# ── selftest ──────────────────────────────────────────────────────────


def _selftest() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    # 隔离环境，避免宿主机 PERSONA_GRANT_* 干扰
    os.environ.pop(ENFORCE_ENV, None)
    os.environ.pop(CACHE_ENV, None)
    global _CACHE, _CACHE_PATH
    _CACHE = None
    _CACHE_PATH = None

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale_ts = "2000-01-01T00:00:00Z"
    doc = {
        "version": 1,
        "fetched_at": now,
        "system": "avatarhub",
        "grants": [
            {"source_key": "主播小雅", "product_id": "huanying", "status": "granted"},
            {"source_key": "主播小雅", "product_id": "zhituo", "status": "revoked"},
        ],
    }

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "grants.json"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        loaded = load_cache(path)
        assert loaded["system"] == "avatarhub"
        assert len(loaded["grants"]) == 2

        r = check("avatarhub", "主播小雅", "huanying")
        assert r == {"allowed": True, "reason": "granted", "mode": "warn"}, r

        r = check("avatarhub", "主播小雅", "zhituo")  # revoked ≠ granted
        assert r["allowed"] is True and r["reason"] == "no_grant" and r["mode"] == "warn", r

        r = check("avatarhub", "主播小雅", "huansheng", enforce=True)
        assert r == {"allowed": False, "reason": "no_grant", "mode": "enforce"}, r

        r = check("avatarhub", "不存在", "huanying", enforce=False)
        assert r["allowed"] is True and r["mode"] == "warn", r

        # 缺缓存：warn / enforce 均放行
        _CACHE = None
        _CACHE_PATH = None
        r = check("avatarhub", "x", "huanying", cache_path=Path(td) / "nope.json")
        assert r["allowed"] is True and r["reason"] == "cache_missing", r
        r = check(
            "avatarhub", "x", "huanying",
            cache_path=Path(td) / "nope.json", enforce=True,
        )
        assert r["allowed"] is True and r["mode"] == "enforce", r

        # stale 仍可用
        stale_doc = dict(doc)
        stale_doc["fetched_at"] = stale_ts
        r = check("avatarhub", "主播小雅", "huanying", cache=stale_doc)
        assert r["allowed"] is True and r["reason"] == "stale:granted", r
        r = check("avatarhub", "主播小雅", "huansheng", cache=stale_doc, enforce=True)
        assert r["allowed"] is False and r["reason"] == "stale:no_grant", r

        # env enforce
        os.environ[ENFORCE_ENV] = "1"
        r = check("avatarhub", "主播小雅", "huansheng", cache=doc)
        assert r["mode"] == "enforce" and r["allowed"] is False, r
        os.environ.pop(ENFORCE_ENV, None)

        # system mismatch → 放行
        r = check("chengjie", "主播小雅", "huanying", cache=doc)
        assert r["allowed"] is True and "system_mismatch" in r["reason"], r

        # 坏 JSON
        bad = Path(td) / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        try:
            load_cache(bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    print("grant_gate selftest OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--selftest"]:
        return _selftest()
    print(
        "usage: python platform/identity/grant_gate.py --selftest\n"
        "API: load_cache(path); check(system, source_key, product_id, *, enforce=None)",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
