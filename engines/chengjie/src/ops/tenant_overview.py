"""托管租户观测面收集器 — ops-overview「☁️ 托管租户」卡的数据源（只读，纯函数可测）。

聚合四路事实（全部软失败，任一路缺失按空算，绝不抛给路由 5xx）：
  1. stack.json 租户名录（chengjie_* 减去生产双实例）+ 交付卡（状态/公网/客户）；
  2. 运行态：suspended 旗 + 端口 LISTEN（listeners_fn 可注入，生产默认 psutil）；
  3. 履约守护 state（fulfilled_hosted.json）：done 总数 / **持单台账** / 最近心跳；
  4. 三守护日志新鲜度（logs/tenant_guard/<mode>_YYYYMMDD.log 的 mtime）——
     「任务注册了但没在跑」在看板直接可见，不用翻计划任务。

设计：与 gpu_watermark/alert_link_status 同款「收集器纯函数 + 路由薄包装」；
30s 进程内 TTL 缓存（看板 60s 轮询别每次扫端口）。`active=False`（零租户零持单）
时前端整卡隐藏——试点/演练清干净后不占版面。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DEFAULT_STACK = Path(r"D:\boundless\deploy\stack.json")
DEFAULT_INSTANCES_BASE = Path(r"D:\chengjie-instances")
DEFAULT_OPS_BASE = DEFAULT_INSTANCES_BASE / ".ops"
_CORE_SERVICE_IDS = {"chengjie_zhiliao", "chengjie_tongyi"}

_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_CACHE_TTL_SEC = 30


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - 缺文件/坏 JSON 一律按无数据
        return None


def _port_listening_default(port: int) -> bool:
    try:
        import psutil

        for c in psutil.net_connections(kind="tcp"):
            if (c.status == psutil.CONN_LISTEN and c.laddr
                    and c.laddr.port == port):
                return True
    except Exception:  # noqa: BLE001 - psutil 不可用按未知（False）
        pass
    return False


def _latest_backup_age_h(backups_dir: Path, iid: str, now: float) -> Optional[float]:
    try:
        zips = sorted((p for p in (backups_dir / iid).glob(f"{iid}_*.zip")
                       if p.is_file()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        if not zips:
            return None
        return round((now - zips[0].stat().st_mtime) / 3600, 1)
    except Exception:  # noqa: BLE001
        return None


def _guard_log_age_min(log_dir: Path, mode: str, now: float) -> Optional[float]:
    try:
        logs = sorted(log_dir.glob(f"{mode}_*.log"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        if not logs:
            return None
        return round((now - logs[0].stat().st_mtime) / 60, 1)
    except Exception:  # noqa: BLE001
        return None


def collect_tenant_overview(
    *,
    stack_path: Path = DEFAULT_STACK,
    instances_base: Path = DEFAULT_INSTANCES_BASE,
    ops_base: Path = DEFAULT_OPS_BASE,
    guard_log_dir: Optional[Path] = None,
    listeners_fn: Optional[Callable[[int], bool]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """采集托管租户全景快照。所有外部读注入可测；生产走默认路径。"""
    ts = now if now is not None else time.time()
    listening = listeners_fn or _port_listening_default
    if guard_log_dir is None:
        guard_log_dir = Path(__file__).resolve().parents[2] / "logs" / "tenant_guard"

    tenants: List[Dict[str, Any]] = []
    stack = _read_json(stack_path) or {}
    for svc in stack.get("services", []) or []:
        sid = str(svc.get("id") or "")
        if not sid.startswith("chengjie_") or sid in _CORE_SERVICE_IDS:
            continue
        iid = sid[len("chengjie_"):]
        ports = svc.get("ports") or []
        port = int(ports[0]) if ports else 0
        suspended = (ops_base / "suspended" / f"{iid}.flag").is_file()
        card = _read_json(instances_base / iid / "tenant_card.json") or {}
        running = bool(port and not suspended and listening(port))
        state = "suspended" if suspended else ("running" if running else "down")
        from src.ops.tenant_lifecycle import expiry_status

        exp_state, exp_days = expiry_status(card, ts)
        tenants.append({
            "instance_id": iid,
            "port": port,
            "state": state,
            "customer": str(card.get("customer") or ""),
            "card_status": str(card.get("status") or ""),
            "exposed": bool(card.get("exposed")),
            "public_url": str(card.get("public_url") or ""),
            "provisioned_at": str(card.get("provisioned_at") or ""),
            "expiry_state": exp_state,
            "expiry_days_left": exp_days,
            "last_backup_age_h": _latest_backup_age_h(
                ops_base / "backups", iid, ts),
        })

    fulfill_state = _read_json(ops_base / "fulfilled_hosted.json") or {}
    held_raw = fulfill_state.get("held") or {}
    held = [{
        "order_id": oid,
        "instance_id": str((rec or {}).get("instance_id") or ""),
        "public_url": str((rec or {}).get("public_url") or ""),
        "held_hours": round((ts - float((rec or {}).get("since") or ts)) / 3600, 1),
    } for oid, rec in sorted(held_raw.items())]

    counts = {
        "total": len(tenants),
        "running": sum(1 for t in tenants if t["state"] == "running"),
        "suspended": sum(1 for t in tenants if t["state"] == "suspended"),
        "down": sum(1 for t in tenants if t["state"] == "down"),
        # 只数「到期且仍在服务」的（suspended=到期口径已执行，不再红）
        "expired": sum(1 for t in tenants
                       if t["expiry_state"] == "expired" and t["state"] != "suspended"),
        "expiring": sum(1 for t in tenants
                        if t["expiry_state"] == "expiring" and t["state"] != "suspended"),
    }
    data = {
        "active": bool(tenants or held),
        "counts": counts,
        "tenants": tenants,
        "fulfill": {
            "done_total": len(fulfill_state.get("done") or {}),
            "held": held,
            "last_tick": fulfill_state.get("last_tick") or None,
        },
        "guards": {
            "fulfill_log_age_min": _guard_log_age_min(guard_log_dir, "fulfill", ts),
            "watch_log_age_min": _guard_log_age_min(guard_log_dir, "watch", ts),
            "backup_log_age_min": _guard_log_age_min(guard_log_dir, "backup", ts),
        },
    }
    return data


def collect_tenant_overview_cached(force: bool = False) -> Dict[str, Any]:
    """生产入口：30s TTL 缓存（看板 60s 轮询 × 多标签页，别每次全扫端口）。"""
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["ts"] < _CACHE_TTL_SEC:
        return _cache["data"]
    data = collect_tenant_overview()
    _cache["ts"] = now
    _cache["data"] = data
    return data
