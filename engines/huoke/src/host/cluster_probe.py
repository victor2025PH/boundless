# -*- coding: utf-8 -*-
"""集群轻量探针：通过 OpenAPI 判断是否声明 ``/devices/{{id}}/contacts/enriched`` 等路由。

用于主控排查「标准通讯录视图」背后 Worker 未部署同版本 API 的情况。
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)


def _local_api_base() -> str:
    from src.openclaw_env import local_api_base

    return local_api_base()


def probe_openapi_contacts_enriched(base: str) -> dict[str, Any]:
    """请求 ``GET {base}/openapi.json``，检查 paths 是否含 contacts/enriched。"""
    url = base.rstrip("/") + "/openapi.json"
    h = {"Accept-Encoding": "identity"}
    key = (os.environ.get("OPENCLAW_API_KEY") or "").strip()
    if key:
        h["X-API-Key"] = key
    req = urllib.request.Request(url, headers=h)
    out: dict[str, Any] = {"base": base}
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            code = resp.getcode()
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        paths = data.get("paths") or {}
        keys = [str(k) for k in paths.keys()]
        hit = any("contacts/enriched" in k for k in keys)
        contact_keys = [k for k in keys if "contact" in k.lower()][:12]
        out.update(
            {
                "reachable": True,
                "http_status": code,
                "contacts_enriched": hit,
                "openapi_paths_with_contact": contact_keys,
            }
        )
        return out
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except Exception:
            pass
        out.update(
            {
                "reachable": e.code < 500,
                "http_status": e.code,
                "contacts_enriched": False,
                "error": getattr(e, "reason", str(e)) or str(e),
            }
        )
        return out
    except Exception as e:
        logger.debug("probe %s: %s", base, e)
        out.update({"reachable": False, "contacts_enriched": False, "error": str(e)[:220]})
        return out


def run_contacts_enriched_probe() -> dict[str, Any]:
    """本机 + 各 Worker 的 enriched 路由声明探测。"""
    from src.host.worker_device_proxy import list_worker_api_bases

    t0 = time.time()
    local = probe_openapi_contacts_enriched(_local_api_base())
    seen: set[str] = set()
    workers: list[dict[str, Any]] = []
    for b in list_worker_api_bases():
        if b in seen:
            continue
        seen.add(b)
        workers.append(probe_openapi_contacts_enriched(b))
    elapsed = round((time.time() - t0) * 1000, 1)
    any_hit = bool(local.get("contacts_enriched")) or any(
        w.get("contacts_enriched") for w in workers
    )
    return {
        "ok": True,
        "elapsed_ms": elapsed,
        "summary": {
            "any_node_has_enriched": any_hit,
            "local_has_enriched": bool(local.get("contacts_enriched")),
            "worker_count": len(workers),
        },
        "local": local,
        "workers": workers,
    }
