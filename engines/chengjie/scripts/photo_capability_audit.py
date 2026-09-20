# -*- coding: utf-8 -*-
"""人设发图能力盘点 CLI（P1，2026-07-31，默认只读）。

默认关相册后，运营需要知道「哪些人设该开回」。本命令扫人设档案 + 相册库存：

    python -m scripts.photo_capability_audit [--data-root PATH] [--json]
    python -m scripts.photo_capability_audit --apply   # 经实例 API 开 capabilities.photos

判据见 ``src.companion.photo_capability_audit``。多实例机逐根出报告
（``scripts._data_root`` 契约）。``--apply`` 只开「建议开启」名单，不关任何已开人设。

⚠️ ``--apply`` 走**运行中实例的 HTTP API**（PUT /api/personas/profiles/{id}
merge 语义，与工作室开关同一路径），而非本进程 PersonaManager：
CLI 新进程里 PersonaManager 是空的，若在其上 upsert + persist 会把实例的
``profiles_runtime.yaml`` **覆盖成只剩一条人设**（P1 初版实现的隐患，P2 修正）。
API 路径同时保证热生效 + 乐观锁 + 审计日志，实例不在线时如实失败并指路工作室开关。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import io
import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from scripts._data_root import ENGINE_ROOT, load_merged_config, resolve_data_roots
from src.companion.photo_capability_audit import (
    stock_map_from_rows,
    summarize_audit,
)


def _ro_conn(db_path: Path) -> Optional[sqlite3.Connection]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _load_stock(data_root: Path) -> Dict[str, Dict[str, Any]]:
    db = data_root / "config" / "persona_media.db"
    if not db.is_file():
        # 引擎根/开发机回落
        db = ENGINE_ROOT / "config" / "persona_media.db"
    if not db.is_file():
        return {}
    conn = _ro_conn(db)
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT persona_id, COUNT(*) AS total, "
            "COALESCE(SUM(enabled),0) AS enabled, "
            "COALESCE(SUM(hits),0) AS hits "
            "FROM persona_media GROUP BY persona_id"
        ).fetchall()
        send_rows = {}
        try:
            for r in conn.execute(
                "SELECT m.persona_id AS persona_id, COUNT(*) AS sends "
                "FROM persona_media_sends s "
                "JOIN persona_media m ON m.id = s.media_id "
                "GROUP BY m.persona_id"
            ).fetchall():
                send_rows[str(r["persona_id"])] = int(r["sends"] or 0)
        except Exception:
            send_rows = {}
        mapped = []
        for r in rows:
            pid = str(r["persona_id"] or "")
            mapped.append({
                "persona_id": pid,
                "total": int(r["total"] or 0),
                "enabled": int(r["enabled"] or 0),
                "hits": int(r["hits"] or 0),
                "sends": send_rows.get(pid, 0),
            })
        return stock_map_from_rows(mapped)
    finally:
        conn.close()


def _load_personas(data_root: Path) -> List[Dict[str, Any]]:
    """从实例/引擎 profiles 读人设（不启 PersonaManager 单例，避免污染服务进程）。"""
    candidates = [
        data_root / "config" / "profiles_runtime.yaml",
        data_root / "config" / "profiles.yaml",
        ENGINE_ROOT / "config" / "profiles_runtime.yaml",
        ENGINE_ROOT / "config" / "profiles.yaml",
    ]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return []
    try:
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    # 兼容 {profiles: {id: {...}}} / 直接 {id: {...}}
    profiles = raw.get("profiles") if isinstance(raw, dict) else None
    if not isinstance(profiles, dict):
        profiles = raw if isinstance(raw, dict) else {}
    out: List[Dict[str, Any]] = []
    for pid, pdata in profiles.items():
        if not isinstance(pdata, dict):
            continue
        if str(pid).startswith("_"):
            continue
        entry = dict(pdata)
        entry["id"] = str(pdata.get("id") or pid)
        out.append(entry)
    return out


def audit_root(data_root: Path) -> Dict[str, Any]:
    personas = _load_personas(data_root)
    stock = _load_stock(data_root)
    summary = summarize_audit(personas, stock)
    summary["data_root"] = str(data_root)
    summary["persona_loaded"] = len(personas)
    summary["stock_personas"] = len(stock)
    return summary


def instance_base_url(data_root: Path) -> str:
    """从实例合并配置推 API 地址（web_admin.port；缺失/无效 → 空串）。"""
    try:
        cfg = load_merged_config(Path(data_root))
        port = int(((cfg.get("web_admin") or {}).get("port")) or 0)
        if 0 < port < 65536:
            return f"http://127.0.0.1:{port}"
    except Exception:
        pass
    return ""


def read_auth_token(data_root: Path) -> str:
    """读实例 web_admin.auth_token（overlay 优先，与 live_multiwin_drill 同口径）。"""
    try:
        cfg = load_merged_config(Path(data_root))
        return str((cfg.get("web_admin") or {}).get("auth_token") or "")
    except Exception:
        return ""


class _ApiClient:
    """session cookie + Bearer 双凭据（与 tools/live_multiwin_drill 同款鉴权）。"""

    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self._cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj))

    def _raw(self, req: urllib.request.Request, timeout: float) -> tuple:
        try:
            with self._opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return 0, json.dumps({"error": str(e)[:200]})

    def login(self, timeout: float = 20) -> bool:
        data = urllib.parse.urlencode({"auth_token": self.token}).encode()
        req = urllib.request.Request(
            self.base + "/login", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        code, _ = self._raw(req, timeout)
        return code in (200, 303) and any(
            c.name == "session" for c in self._cj)

    def call(self, method: str, path: str, body: Any = None,
             timeout: float = 30) -> tuple:
        headers = {"Authorization": f"Bearer {self.token}",
                   "Content-Type": "application/json"}
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method, headers=headers)
        code, raw = self._raw(req, timeout)
        try:
            return code, json.loads(raw)
        except Exception:
            return code, {"raw": raw[:200]}


def _apply_enable_via_api(data_root: Path, ids: List[str],
                          base_override: str = "") -> Dict[str, Any]:
    """经运行中实例 API 逐个开 capabilities.photos（merge，只动这一个字段）。

    写后 GET 回读校验（capabilities.photos 必须为 true 才算成）——上线中的旧代码
    若 normalize 吞掉字段，这里会如实报 verify_failed 而不是假装成功。
    """
    out: Dict[str, Any] = {"enabled": [], "skipped": [], "errors": [],
                           "via": "api"}
    base = str(base_override or "").strip() or instance_base_url(data_root)
    token = read_auth_token(data_root)
    if not base or not token:
        out["errors"].append({
            "id": "*",
            "error": "no_base_or_token（读不到 web_admin.port/auth_token）；"
                     "请在人设工作室相册 tab 手动打开开关"})
        return out
    api = _ApiClient(base, token)
    if not api.login():
        out["errors"].append({
            "id": "*",
            "error": f"login_failed @ {base}（实例未起或 token 不匹配）；"
                     "请在人设工作室相册 tab 手动打开开关"})
        return out
    for pid in ids:
        code, body = api.call(
            "PUT", f"/api/personas/profiles/{urllib.parse.quote(pid)}",
            {"persona": {"capabilities": {"photos": True}}, "merge": True})
        if code != 200:
            out["errors"].append({"id": pid, "error": f"http_{code}: {body}"})
            continue
        vcode, vbody = api.call(
            "GET", f"/api/personas/profiles/{urllib.parse.quote(pid)}")
        caps = (((vbody or {}).get("persona") or {}).get("capabilities")
                or {}) if isinstance(vbody, dict) else {}
        if vcode == 200 and caps.get("photos"):
            out["enabled"].append(pid)
        else:
            out["errors"].append({
                "id": pid,
                "error": "verify_failed（写后回读 capabilities.photos 非 true）"})
    return out


def _render(summary: Dict[str, Any]) -> str:
    lines = [
        f"=== photo capability audit @ {summary.get('data_root')} ===",
        f"personas loaded={summary.get('persona_loaded')}  "
        f"photos_on={summary.get('photos_on')}  "
        f"photos_off={summary.get('photos_off')}  "
        f"orphan_on(开但无库存)={summary.get('orphan_on')}",
        f"recommend_enable={summary.get('recommend_count')}",
    ]
    for r in summary.get("recommend_enable") or []:
        lines.append(
            f"  - {r['id']}  {r['name']}  "
            f"enabled={r['stock_enabled']}/{r['stock_total']}  "
            f"hits={r['hits']} sends={r['sends']}  ({r['reason']})"
        )
    if not summary.get("recommend_enable"):
        lines.append("  (none)")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Audit persona photo capability")
    ap.add_argument("--data-root", default="", help="实例数据根；空=自动发现")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="经运行中实例 API 把建议名单的 capabilities.photos 置 true")
    ap.add_argument("--base", default="",
                    help="实例地址覆写（默认从 web_admin.port 推）")
    ap.add_argument("--only", default="",
                    help="逗号分隔人设 id：只对这些 id 应用（须在建议名单内）")
    args = ap.parse_args(argv)

    roots = resolve_data_roots(args.data_root or None)
    if not roots:
        roots = [ENGINE_ROOT]

    reports = [audit_root(Path(r)) for r in roots]
    if args.apply:
        only = {s.strip() for s in str(args.only or "").split(",") if s.strip()}
        for rep in reports:
            ids = [r["id"] for r in (rep.get("recommend_enable") or [])]
            if only:
                ids = [i for i in ids if i in only]
            if not ids:
                rep["apply"] = {"enabled": [], "skipped": [], "errors": [],
                                "via": "api"}
                continue
            rep["apply"] = _apply_enable_via_api(
                Path(rep["data_root"]), ids, base_override=args.base)

    if args.json:
        print(json.dumps(reports if len(reports) > 1 else reports[0],
                         ensure_ascii=False, indent=2))
    else:
        chunks = [_render(r) for r in reports]
        for r in reports:
            apx = r.get("apply")
            if apx:
                chunks.append(
                    f"APPLY(via={apx.get('via')}) enabled={apx.get('enabled')} "
                    f"skipped={len(apx.get('skipped') or [])} "
                    f"errors={apx.get('errors')}"
                )
        print("\n\n".join(chunks))
    return 0


if __name__ == "__main__":
    # Windows 管道/重定向下避免 GBK 崩
    if sys.stdout and hasattr(sys.stdout, "buffer"):
        try:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())
