# -*- coding: utf-8 -*-
"""TikTok 设备状态、账号调度与行为配置路由。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends, Body
from .auth import verify_api_key
from src.openclaw_env import local_api_base
from src.host.device_registry import (
    DEFAULT_DEVICES_YAML,
    config_file,
    data_file,
    get_device_row_safe as _tt_get_device_row,
    get_device_row_strict as _tt_get_device_row_strict,
    is_device_in_local_registry as _is_local_device,
)

router = APIRouter(prefix="/tiktok", tags=["tiktok"], dependencies=[Depends(verify_api_key)])

_W03_BASE = "http://192.168.0.103:8000"


def _tt_campaign_skip_worker_when_local_offline() -> bool:
    """OPENCLAW_TT_CAMPAIGN_SKIP_WORKER_WHEN_OFFLINE=1：本地 devices.yaml 有设备但非在线时不再转发 Worker。"""
    import os

    return os.environ.get(
        "OPENCLAW_TT_CAMPAIGN_SKIP_WORKER_WHEN_OFFLINE", ""
    ).strip().lower() in ("1", "true", "yes")


def _w3_get(path: str, timeout: int = 5):
    """向 Worker-03 发 GET 请求，返回 JSON 结果。失败抛出 Exception。"""
    import json as _j, urllib.request as _ur
    req = _ur.Request(f"{_W03_BASE}{path}", headers={"Connection": "close"})
    resp = _ur.urlopen(req, timeout=timeout)
    try:
        return _j.loads(resp.read())
    finally:
        resp.close()


def _w3_post(path: str, body: dict, timeout: int = 10):
    """向 Worker-03 发 POST 请求，返回 JSON 结果。失败抛出 Exception。"""
    import json as _j, urllib.request as _ur
    data = _j.dumps(body).encode()
    req = _ur.Request(f"{_W03_BASE}{path}", data=data,
                      headers={"Content-Type": "application/json", "Connection": "close"}, method="POST")
    resp = _ur.urlopen(req, timeout=timeout)
    try:
        return _j.loads(resp.read())
    finally:
        resp.close()


@router.get("/referral-config")
def get_referral_config():
    """获取所有设备的引流账号配置。"""
    import yaml
    cfg_path = config_file("chat_messages.yaml")
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return {"referrals": data.get("device_referrals", {})}
    return {"referrals": {}}


@router.post("/referral-config")
def set_referral_config(body: dict):
    """设置设备的引流账号。

    Body: {"device_id": "89NZ...", "telegram": "@dthb3", "whatsapp": "+639..."}
    或    {"all": true, "telegram": "@dthb3", "whatsapp": "+639..."}  对全部设备
    """
    import yaml
    cfg_path = config_file("chat_messages.yaml")

    data = {}
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    data.setdefault("device_referrals", {})

    device_id = body.get("device_id", "")
    # 通用联系方式：支持任意 app（telegram/whatsapp/instagram/line/...）
    # 保留字段：device_id, all, 以 _ 开头的字段
    _RESERVED = {"device_id", "all"}
    _contacts = {k: v for k, v in body.items()
                 if k not in _RESERVED and not k.startswith("_")}

    if body.get("all"):
        # 对所有本地设备设置
        from src.device_control.device_manager import get_device_manager
        mgr = get_device_manager(DEFAULT_DEVICES_YAML)
        devices = [d.device_id for d in mgr.get_all_devices()]
        for did in devices:
            ref = data["device_referrals"].get(did, {})
            for app, val in _contacts.items():
                if val:
                    ref[app] = val
                elif app in ref:
                    del ref[app]  # 空值 = 删除
            data["device_referrals"][did] = ref
        updated = len(devices)

        # 同步到 Worker-03（W03 运行相同代码，也支持 all=true）
        import threading as _thr
        def _sync_w03():
            try:
                _w3_post("/tiktok/referral-config", body, timeout=5)
            except Exception:
                pass  # W03 离线时静默跳过，不影响本地保存
        _thr.Thread(target=_sync_w03, daemon=True, name="w03-ref-sync").start()
    elif device_id:
        ref = data["device_referrals"].get(device_id, {})
        for app, val in _contacts.items():
            if val:
                ref[app] = val
            elif app in ref:
                del ref[app]  # 空值 = 删除该 app
        data["device_referrals"][device_id] = ref
        updated = 1
    else:
        raise HTTPException(400, "需要 device_id 或 all=true")

    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False)

    return {"ok": True, "updated": updated, "referrals": data["device_referrals"]}


@router.get("/referral-config/full")
def get_referral_config_full():
    """获取引流账号配置 + 所有设备状态（含 Worker-03）。

    返回: {"ok": true, "devices": [{device_id, alias, host, node, status, telegram, whatsapp, configured}]}
    """
    import yaml as _yaml, json as _json, urllib.request as _ur

    # 1. 读取本地引流配置
    _cfg_path = config_file("chat_messages.yaml")
    _data = {}
    if _cfg_path.exists():
        with open(_cfg_path, encoding="utf-8") as _f:
            _data = _yaml.safe_load(_f) or {}
    _refs = _data.get("device_referrals", {})

    # 2+3. 并行获取：Worker-03别名 + Worker-03设备状态（避免串行等待）
    _aliases = {}
    _w3_status: dict = {}
    try:
        from .devices_core import _load_aliases as _la
        _aliases = _la()  # 读本地 JSON，瞬时完成
    except Exception:
        pass

    import concurrent.futures as _cf
    def _fetch_w3_aliases():
        try:
            return _w3_get("/devices/aliases", timeout=3)
        except Exception:
            return {}

    def _fetch_w3_devices():
        try:
            _d = _w3_get("/devices", timeout=3)
            return _d.get("devices", []) if isinstance(_d, dict) else _d
        except Exception:
            return []

    # 两个 Worker-03 请求并行执行，总耗时 ≤ max(timeout_aliases, timeout_devices)
    with _cf.ThreadPoolExecutor(max_workers=2) as _ex:
        _fut_aliases = _ex.submit(_fetch_w3_aliases)
        _fut_devices = _ex.submit(_fetch_w3_devices)
        _w3_aliases = _fut_aliases.result()
        _w3_devs    = _fut_devices.result()

    # 合并 Worker-03 别名
    for _wdid, _winfo in _w3_aliases.items():
        if _wdid not in _aliases:
            _aliases[_wdid] = _winfo
        elif not _aliases[_wdid].get("host_name") and _winfo.get("host_name"):
            _aliases[_wdid]["host_name"] = _winfo["host_name"]

    # 整理 Worker-03 设备在线状态
    for _d in _w3_devs:
        _did = _d.get("device_id", "")
        if _did:
            _w3_status[_did] = _d.get("status", "unknown")

    # 4. 汇总所有候选设备（Worker-03在线 或 有配置）
    _candidates = {}  # device_id -> entry
    _alias_sorted = sorted(_aliases.items(), key=lambda x: x[1].get("number", 999))
    for _did, _info in _alias_sorted:
        _status = _w3_status.get(_did, "")
        _ref = _refs.get(_did, {})
        _has_config = bool(_ref.get("telegram") or _ref.get("whatsapp"))
        _in_w3 = _did in _w3_status
        _in_cluster = bool(_info.get("host_name")) or _in_w3
        if not _in_cluster and not _has_config:
            continue
        _dl = _info.get("display_label") or _info.get("alias") or f'{_info.get("number", 0):02d}号'
        _candidates[_did] = {
            "device_id": _did,
            "alias": _dl,
            "display_label": _info.get("display_label") or _dl,
            "number": _info.get("number", 99),
            "slot": _info.get("slot", _info.get("number", 0)),
            "host_scope": _info.get("host_scope", ""),
            "host": _info.get("host_name", "主控"),
            "node": "worker03" if _info.get("host_name", "").lower().startswith("worker") else "local",
            "status": _status or ("connected" if _has_config else "offline"),
            "telegram": _ref.get("telegram", ""),
            "whatsapp": _ref.get("whatsapp", ""),
            "configured": _has_config,
            "_in_w3": _in_w3,
        }

    # 5. 每台设备一行（分域后不同主机可同号，不再按 number 去重）
    _devices = list(_candidates.values())
    for _d in _devices:
        _d.pop("_in_w3", None)  # 移除内部字段

    _devices.sort(key=lambda x: (x.get("host") or "", x.get("number", 0), x.get("device_id", "")))

    return {"ok": True, "devices": _devices, "total": len(_devices),
            "configured": sum(1 for d in _devices if d["configured"])}


@router.get("/device-grid")
def get_device_grid():
    """设备为中心的聚合视图 — 一次请求返回所有设备的状态、引流配置、线索数。

    替代多个独立 API 请求，专为设备网格 UI 设计。
    """
    import yaml as _yaml, json as _json, urllib.request as _ur, datetime as _dt
    import concurrent.futures as _cf

    # ── 1. 本地数据（瞬时） ──
    _aliases = {}
    try:
        from .devices_core import _load_aliases as _la
        _aliases = _la()
    except Exception:
        pass

    # 主控本机 USB：须包含「已授权 online」+「仍插着但未授权/offline」的序列号，否则不会进入下方 _aliases 循环，TikTok 面板空白。
    _local_dids: set = set()
    _local_connected_dids: set = set()
    _usb_problem: dict = {}
    try:
        from src.device_control.device_manager import get_device_manager
        _dm = get_device_manager(DEFAULT_DEVICES_YAML)
        _dm.discover_devices(force=True)
        for _dev in _dm.get_connected_devices() or []:
            _lid = getattr(_dev, "device_id", "") or ""
            if not _lid:
                continue
            _local_dids.add(_lid)
            _local_connected_dids.add(_lid)
        for _pdid, _pst in getattr(_dm, "_last_problem_devices", []) or []:
            if _pdid:
                _local_dids.add(_pdid)
                _usb_problem[str(_pdid)] = _pst
        try:
            from src.host.device_alias_labels import (
                load_local_cluster_identity,
                apply_slot_and_labels,
                next_free_slot_resolved,
            )
            _chid, _cname = load_local_cluster_identity()
        except Exception:
            _chid, _cname = "coordinator", ""
        for _lid in _local_dids:
            if _lid not in _aliases:
                _n = next_free_slot_resolved(
                    _aliases, _chid or "coordinator", 1, 999,
                    local_device_ids=set(_local_dids),
                )
                _aliases[_lid] = apply_slot_and_labels(
                    {"display_name": _lid[:8]}, _n, _chid or "coordinator", _cname or "",
                )
    except Exception:
        pass

    _cfg_path = config_file("chat_messages.yaml")
    _refs = {}
    if _cfg_path.exists():
        with open(_cfg_path, encoding="utf-8") as _f:
            _refs = (_yaml.safe_load(_f) or {}).get("device_referrals", {})

    # 本地设备状态（phase/day/stats）
    _local_states = {}
    try:
        from src.host.device_state import get_device_state_store as _gds
        _ds = _gds("tiktok")
        for _did in (_ds.list_devices() or []):
            _local_states[_did] = _ds.get_device_summary(_did)
    except Exception:
        pass

    # 本地线索按设备计数
    _leads_by_dev: dict = {}
    _replied_by_dev: dict = {}  # N3: 有实际入站消息的线索数
    _hot_leads_total = 0
    _all_leads_list: list = []
    try:
        from src.leads.store import get_leads_store as _gls
        _ls = _gls()
        _all_leads_local = _ls.list_leads() or []
        for _lead in _all_leads_local:
            _d = _lead.get("device_id", "") or ""
            if _d:
                _leads_by_dev[_d] = _leads_by_dev.get(_d, 0) + 1
        _hot_leads_total = len(_all_leads_local)
        _all_leads_list = _all_leads_local
    except Exception:
        pass

    # N3: 从 interactions 表同时计算 leads_count + replied_count（独立 try，不依赖 list_leads）
    try:
        import sqlite3 as _sq3
        _ldb = str(data_file("leads.db"))
        _lconn = _sq3.connect(_ldb, timeout=5)
        # 1. 有入站消息的线索数（replied_count）
        _rep_rows = _lconn.execute("""
            SELECT device_id, COUNT(DISTINCT lead_id)
            FROM interactions
            WHERE direction = 'inbound'
              AND action NOT IN ('follow_back', 'follow_back_confirmed')
              AND device_id != ''
            GROUP BY device_id
        """).fetchall()
        # 2. 有任意互动的线索数（leads_count fallback，当 list_leads() 返回空时）
        _lead_rows = _lconn.execute("""
            SELECT device_id, COUNT(DISTINCT lead_id)
            FROM interactions
            WHERE device_id != ''
            GROUP BY device_id
        """).fetchall()
        _lconn.close()
        for _rdid, _rcnt in _rep_rows:
            if _rdid:
                _replied_by_dev[str(_rdid)] = int(_rcnt)
        for _ldid, _lcnt in _lead_rows:
            if _ldid and not _leads_by_dev.get(str(_ldid)):
                # 仅当 list_leads() 没有该设备数据时才用 interactions 兜底
                _leads_by_dev[str(_ldid)] = int(_lcnt)
    except Exception:
        pass

    # ── 2. Worker-03 并行请求（3路同时发） ──
    def _w3_aliases():
        try:
            return _w3_get("/devices/aliases", timeout=3)
        except Exception:
            return {}

    def _w3_devices():
        try:
            _d = _w3_get("/devices", timeout=3)
            return _d.get("devices", []) if isinstance(_d, dict) else _d
        except Exception:
            return []

    def _w3_states():
        try:
            _d = _w3_get("/tiktok/devices", timeout=3)
            return _d if isinstance(_d, list) else _d.get("devices", [])
        except Exception:
            return []

    def _w3_leads():
        try:
            _d = _w3_get("/tiktok/qualified-leads?limit=200", timeout=3)
            return _d.get("leads", []) if isinstance(_d, dict) else []
        except Exception:
            return []

    with _cf.ThreadPoolExecutor(max_workers=4) as _ex:
        _f1 = _ex.submit(_w3_aliases)
        _f2 = _ex.submit(_w3_devices)
        _f3 = _ex.submit(_w3_states)
        _f4 = _ex.submit(_w3_leads)
        _wa = _f1.result(); _wd = _f2.result()
        _ws = _f3.result(); _wl = _f4.result()

    # 合并别名
    for _wdid, _winfo in _wa.items():
        if _wdid not in _aliases:
            _aliases[_wdid] = _winfo

    # W03 在线状态
    _w3_status = {_d.get("device_id"): _d.get("status", "offline") for _d in _wd if _d.get("device_id")}

    # W03 设备状态（TikTok phase/day/stats）
    _w3_state_map: dict = {}
    for _sd in _ws:
        _sdid = _sd.get("device_id", "")
        if _sdid:
            _w3_state_map[_sdid] = _sd

    # W03 线索（计入 leads_count + replied_count）
    for _lead in _wl:
        _d = _lead.get("device_id", "") or ""
        if not _d:
            # fallback: 从 recent_interactions 里推断 device_id
            _ri = _lead.get("recent_interactions") or []
            for _ri_item in _ri:
                _di = _ri_item.get("device_id") or (_ri_item.get("metadata") or {}).get("device_id") or ""
                if _di:
                    _d = _di
                    break
        if _d:
            _leads_by_dev[_d] = _leads_by_dev.get(_d, 0) + 1
        # N3: 有实际入站消息 = recent_interactions 里有 direction=inbound 或 last_message 非空
        _has_reply = bool(_lead.get("last_message"))
        if not _has_reply:
            _ri = _lead.get("recent_interactions") or []
            _has_reply = any(_ri_i.get("direction") == "inbound" for _ri_i in _ri)
        if _has_reply and _d:
            _replied_by_dev[_d] = _replied_by_dev.get(_d, 0) + 1
    _hot_leads_total += len(_wl)

    # 合并所有线索（用于面板按设备过滤）
    _all_leads_list.extend(_wl)

    # ── 3. 汇总候选设备 ──
    _candidates: dict = {}
    for _did, _info in sorted(_aliases.items(), key=lambda x: x[1].get("number", 999)):
        _status = _w3_status.get(_did, "")
        _ref = _refs.get(_did, {})
        # 所有配置的联系方式（支持任意 app）
        _all_contacts = {k: v for k, v in _ref.items() if v and not k.startswith("_")}
        _has_cfg = bool(_all_contacts)
        _in_w3 = _did in _w3_status
        _in_cluster = bool(_info.get("host_name")) or _in_w3
        _on_coordinator_usb = _did in _local_dids
        if not _in_cluster and not _has_cfg and not _on_coordinator_usb:
            continue

        # 优先用 W03 状态（主要设备在那里），其次本地
        _state = _w3_state_map.get(_did) or _local_states.get(_did) or {}
        # 主控 USB：已授权显示 connected；仅 unauthorized/offline 时不要伪装成已连接
        if _did in _local_connected_dids:
            if _on_coordinator_usb and _status not in ("connected", "online", "active"):
                _status = "connected"
        elif _on_coordinator_usb and _did not in _local_connected_dids:
            _status = _usb_problem.get(str(_did), "") or "offline"
        _online = _status in ("connected", "online", "active")

        # 今日数据：尝试读取 daily 键，否则用累计
        _today_key = "daily:" + _dt.date.today().isoformat()
        _today_watched = _state.get(_today_key + ":watched") or _state.get("total_watched", 0)
        _today_followed = _state.get(_today_key + ":followed") or _state.get("total_followed", 0)
        _today_dms = _state.get(_today_key + ":dms") or _state.get("total_dms_sent", 0)

        _dl = _info.get("display_label") or _info.get("alias") or f'{_info.get("number", 0):02d}号'
        _candidates[_did] = {
            "device_id": _did,
            "alias": _dl,
            "display_label": _info.get("display_label") or _dl,
            "number": _info.get("number", 99),
            "slot": _info.get("slot", _info.get("number", 0)),
            "host_scope": _info.get("host_scope", ""),
            "host": _info.get("host_name", "主控"),
            "node": "worker03" if (_info.get("host_name", "") or "").lower().startswith("worker") else "local",
            "status": _status or ("connected" if _has_cfg else "offline"),
            "online": _online,
            "phase": _state.get("phase", "unknown"),
            "day": _state.get("day", 0),
            "recovery": bool(_state.get("recovery_active", False)),
            "algo_score": round(float(_state.get("algorithm_score", 0) or 0), 1),
            "sessions_today": _state.get("sessions_today", 0),
            "today_watched": int(_today_watched or 0),
            "today_followed": int(_today_followed or 0),
            "today_dms": int(_today_dms or 0),
            "telegram": _ref.get("telegram", ""),   # backward compat
            "whatsapp": _ref.get("whatsapp", ""),   # backward compat
            "contacts": _all_contacts,               # 全量联系方式（含所有 app）
            "configured": _has_cfg,
            "leads_count": _leads_by_dev.get(_did, 0),
            "replied_count": _replied_by_dev.get(_did, 0),  # N3: 有实际回复消息的线索数
            "_in_w3": _in_w3,
        }

    # 每台设备一行（分域编号后不再按 number 合并）
    _devices = sorted(
        _candidates.values(),
        key=lambda x: (x.get("host") or "", x.get("number", 0), x.get("device_id", "")),
    )
    for _d in _devices:
        _d.pop("_in_w3", None)

    # 附加缓存的就绪状态（不触发新 ADB 检查，纯内存查询，耗时 < 1ms）
    try:
        from src.host.preflight import _cache as _pf_cache, _cache_lock as _pf_lock
        import time as _time_mod
        _now_ts = _time_mod.time()
        with _pf_lock:
            _pf_snapshot = dict(_pf_cache)
        for _d in _devices:
            _cached = _pf_snapshot.get(_d["device_id"])
            if _cached and (_now_ts - _cached[1]) < 90:
                _d["readiness"] = _cached[0]
            else:
                _d["readiness"] = None  # 无缓存，前端不显示就绪行
    except Exception:
        pass

    _online_n = sum(1 for d in _devices if d["online"])
    _cfg_n = sum(1 for d in _devices if d["configured"])

    # 简化线索列表（只保留必要字段，避免响应过大）
    _slim_leads = [
        {k: l.get(k) for k in ("id","lead_id","device_id","username","name","score","status")}
        for l in _all_leads_list[:200]
    ]

    return {
        "ok": True,
        "devices": _devices,
        "leads": _slim_leads,
        "summary": {
            "total": len(_devices),
            "online": _online_n,
            "configured": _cfg_n,
            "hot_leads": _hot_leads_total,
        },
    }


# ═══ 设备级操作 API ═══

@router.post("/device/{device_id}/launch")
def device_launch(device_id: str, body: dict = Body(default={})):
    """为指定设备启动工作流。

    支持两种模式：
    1. flow_steps=[{type, params},...] — 自定义步骤序列（来自流程配置器）
    2. 无 flow_steps — 原始全流程模式（兼容旧逻辑）

    环境变量 ``OPENCLAW_TT_CAMPAIGN_SKIP_WORKER_WHEN_OFFLINE=1``：若 ``devices.yaml`` 有该设备且
    状态非 ``connected``/``busy``，则 **flow_steps** 不向主控/Worker 发 ``/tasks``，且 **campaign** 不再转发
    Worker-03 的 ``launch-campaign``（均返回 ``skipped=local_offline``；指标分桶见 ``/health``）。
    """
    import json as _json, urllib.request as _ur

    # ── 新: 自定义步骤序列（流程配置器传入）──
    flow_steps = body.get("flow_steps")
    if flow_steps and isinstance(flow_steps, list) and len(flow_steps) > 0:
        # 根据设备位置路由：本地设备走主控，W03设备直接走W03
        # W03任务ID通过 GET /tasks/{id} 代理查询（tasks.py 已实现代理）
        from src.device_control.device_manager import DeviceStatus

        _dev_fs = _tt_get_device_row(device_id)
        _is_local = (_dev_fs is not None) or _is_local_device(device_id)
        _base = local_api_base() if _is_local else _W03_BASE
        if (
            _dev_fs
            and _dev_fs.status not in (DeviceStatus.CONNECTED, DeviceStatus.BUSY)
            and _tt_campaign_skip_worker_when_local_offline()
        ):
            try:
                from src.host.health_monitor import metrics as _tt_fs_m

                _tt_fs_m.record_tt_flow_steps_skip_local_offline()
            except Exception:
                pass
            _st = getattr(_dev_fs.status, "value", str(_dev_fs.status))
            return {
                "ok": False,
                "device_id": device_id,
                "flow_tasks": [],
                "message": "flow_steps 未执行：本地设备离线且已启用 OPENCLAW_TT_CAMPAIGN_SKIP_WORKER_WHEN_OFFLINE",
                "task_count": 0,
                "skipped": "local_offline",
                "device_status": _st,
                "error": (
                    "本地设备状态为 "
                    + _st
                    + "，已按 OPENCLAW_TT_CAMPAIGN_SKIP_WORKER_WHEN_OFFLINE 跳过 flow_steps（未发 /tasks）"
                ),
            }

        results = []
        for step in flow_steps:
            s_type = step.get("type", "")
            s_params = dict(step.get("params") or {})
            # __pitch__ 特殊步骤：调用设备所在节点的 pitch 接口
            if s_type == "__pitch__":
                try:
                    rb = _json.dumps({
                        "max_pitch": s_params.get("max_pitch", 5),
                        "cta_url": s_params.get("cta_url", ""),
                    }).encode()
                    req = _ur.Request(
                        f"{_base}/tiktok/device/{device_id}/pitch",
                        data=rb, headers={"Content-Type": "application/json"}, method="POST"
                    )
                    resp = _ur.urlopen(req, timeout=10)
                    r = _json.loads(resp.read())
                    results.append({"type": "pitch", "task_id": r.get("task_id", ""), "ok": True})
                except Exception as e:
                    results.append({"type": "pitch", "ok": False, "error": str(e)})
                continue
            # 普通任务步骤：直接在设备所在节点创建任务
            # 本地设备 → 主控 /tasks；W03设备 → W03 /tasks
            # 前端轮询通过主控 GET /tasks/{id} 代理透明处理
            try:
                rb = _json.dumps({
                    "type": s_type,
                    "device_id": device_id,
                    "params": s_params,
                }).encode()
                req = _ur.Request(
                    f"{_base}/tasks",
                    data=rb, headers={"Content-Type": "application/json"}, method="POST"
                )
                resp = _ur.urlopen(req, timeout=10)
                r = _json.loads(resp.read())
                results.append({"type": s_type, "task_id": r.get("task_id", ""), "ok": True})
            except Exception as e:
                results.append({"type": s_type, "ok": False, "error": str(e)})

        ok_n = sum(1 for r in results if r.get("ok"))
        fail_n = sum(1 for r in results if not r.get("ok"))
        try:
            from src.host.health_monitor import metrics as _tt_launch_metrics

            _tt_launch_metrics.record_tt_device_launch(
                is_local_enqueue=_is_local,
                steps_ok=ok_n,
                steps_failed=fail_n,
            )
        except Exception:
            pass
        return {
            "ok": ok_n > 0,
            "device_id": device_id,
            "flow_tasks": results,
            "message": f"已创建 {ok_n}/{len(flow_steps)} 个步骤任务",
            "task_count": ok_n,
        }

    # ── 原始全流程兼容模式 ──
    from src.device_control.device_manager import DeviceStatus

    dev_obj = _tt_get_device_row(device_id)

    if not dev_obj:
        try:
            from src.host.health_monitor import metrics as _tt_skip_m

            _tt_skip_m.record_tt_campaign_skip_no_local_device()
        except Exception:
            pass

    # 本地设备：直接调 launch-campaign
    if dev_obj and dev_obj.status in (DeviceStatus.CONNECTED, DeviceStatus.BUSY):
        try:
            req_body = _json.dumps({
                "country": body.get("country", "italy"),
                "duration_minutes": body.get("duration_minutes", 30),
                "device_ids": [device_id],
                "auto_vpn": body.get("auto_vpn", True),
            }).encode()
            req = _ur.Request(
                f"{local_api_base()}/tiktok/launch-campaign",
                data=req_body, headers={"Content-Type": "application/json"}, method="POST"
            )
            resp = _ur.urlopen(req, timeout=10)
            result = _json.loads(resp.read())
            result["device_id"] = device_id
            try:
                from src.host.health_monitor import metrics as _tt_camp_m

                _tt_camp_m.record_tt_campaign_launch(
                    is_local_enqueue=True,
                    ok=bool(result.get("ok", True)),
                )
            except Exception:
                pass
            return result
        except Exception as e:
            try:
                from src.host.health_monitor import metrics as _tt_camp_m

                _tt_camp_m.record_tt_campaign_launch(is_local_enqueue=True, ok=False)
            except Exception:
                pass
            return {"ok": False, "error": str(e), "device_id": device_id}

    if (
        dev_obj
        and dev_obj.status not in (DeviceStatus.CONNECTED, DeviceStatus.BUSY)
        and _tt_campaign_skip_worker_when_local_offline()
    ):
        try:
            from src.host.health_monitor import metrics as _tt_off_m

            _tt_off_m.record_tt_campaign_skip_local_offline()
        except Exception:
            pass
        return {
            "ok": False,
            "device_id": device_id,
            "error": (
                "本地设备状态为 "
                + (getattr(dev_obj.status, "value", str(dev_obj.status)))
                + "，已按 OPENCLAW_TT_CAMPAIGN_SKIP_WORKER_WHEN_OFFLINE 跳过 Worker launch-campaign"
            ),
            "skipped": "local_offline",
            "device_status": getattr(dev_obj.status, "value", str(dev_obj.status)),
        }

    # Worker-03 设备：转发请求
    try:
        req_body = _json.dumps({
            "country": body.get("country", "italy"),
            "duration_minutes": body.get("duration_minutes", 30),
            "device_ids": [device_id],
        }).encode()
        req = _ur.Request(
            "http://192.168.0.103:8000/tiktok/launch-campaign",
            data=req_body, headers={"Content-Type": "application/json"}, method="POST"
        )
        resp = _ur.urlopen(req, timeout=10)
        result = _json.loads(resp.read())
        result["device_id"] = device_id
        result["via"] = "worker03"
        try:
            from src.host.health_monitor import metrics as _tt_camp_m

            _tt_camp_m.record_tt_campaign_launch(
                is_local_enqueue=False,
                ok=bool(result.get("ok", True)),
            )
        except Exception:
            pass
        return result
    except Exception as e:
        try:
            from src.host.health_monitor import metrics as _tt_camp_m

            _tt_camp_m.record_tt_campaign_launch(is_local_enqueue=False, ok=False)
        except Exception:
            pass
        return {"ok": False, "error": "设备不在线或Worker-03无响应: " + str(e), "device_id": device_id}


@router.post("/device/{device_id}/inbox")
def device_inbox(device_id: str, body: dict = {}):
    """检查指定设备的TikTok收件箱，处理新消息并触发AI回复。
    本地设备 → 本地 worker pool；W03 设备 → 转发到 Worker-03。
    """
    # W03 设备：直接转发，避免本地 pool 无意义等待
    if not _is_local_device(device_id):
        try:
            result = _w3_post(f"/tiktok/device/{device_id}/inbox",
                              {"auto_reply": True, "max_conversations": body.get("max_conversations", 30)})
            result["via"] = "worker03"
            return result
        except Exception as _e:
            return {"ok": False, "error": f"Worker-03 无响应: {_e}", "device_id": device_id}

    # 本地设备：提交到本地 worker pool
    from src.host.task_origin import with_origin
    from src.host.worker_pool import get_worker_pool
    from ..api import task_store
    from ..executor import run_task

    _cp = DEFAULT_DEVICES_YAML
    try:
        task_id = task_store.create_task(
            task_type="tiktok_check_inbox",
            device_id=device_id,
            params=with_origin(
                {"device_id": device_id, "reply_with_ai": True},
                "tiktok_device_route",
            ),
        )
        pool = get_worker_pool()
        pool.submit(task_id, device_id, run_task, task_id, _cp)
        return {"ok": True, "task_id": task_id, "message": "收件箱检查任务已提交", "device_id": device_id}
    except Exception as e:
        return {"ok": False, "error": str(e), "device_id": device_id}


@router.post("/device/{device_id}/follow")
def device_follow(device_id: str, body: dict = {}):
    """为指定设备执行关注目标用户操作。
    本地设备 → 本地 worker pool；W03 设备 → 转发到 Worker-03。
    """
    count = body.get("count", 10)

    # W03 设备：直接转发
    if not _is_local_device(device_id):
        try:
            result = _w3_post(f"/tiktok/device/{device_id}/follow", {"count": count})
            result["via"] = "worker03"
            return result
        except Exception as _e:
            return {"ok": False, "error": f"Worker-03 无响应: {_e}", "device_id": device_id}

    # 本地设备：提交到本地 worker pool
    from src.host.task_origin import with_origin
    from src.host.worker_pool import get_worker_pool
    from ..api import task_store
    from ..executor import run_task

    _cp = DEFAULT_DEVICES_YAML
    try:
        task_id = task_store.create_task(
            task_type="tiktok_follow",
            device_id=device_id,
            params=with_origin(
                {"device_id": device_id, "count": count},
                "tiktok_device_route",
            ),
        )
        pool = get_worker_pool()
        pool.submit(task_id, device_id, run_task, task_id, _cp)
        return {"ok": True, "task_id": task_id,
                "message": f"关注任务已提交（目标 {count} 人）", "device_id": device_id}
    except Exception as e:
        return {"ok": False, "error": str(e), "device_id": device_id}


@router.post("/device/{device_id}/pitch")
def device_pitch(device_id: str, body: dict = {}):
    """向指定设备的合格线索发话术。
    本地设备 → 本地 leads + 本地 task queue；W03 设备 → 转发到 Worker-03。
    """
    import yaml as _yaml, json as _json, urllib.request as _ur

    # W03 设备：完整转发到 W03（W03 自己知道它的线索和设备）
    if not _is_local_device(device_id):
        try:
            result = _w3_post(f"/tiktok/device/{device_id}/pitch", body, timeout=15)
            result["via"] = "worker03"
            return result
        except Exception as _e:
            return {"ok": False, "error": f"Worker-03 无响应: {_e}", "device_id": device_id}

    max_pitch = body.get("max_pitch", 5)
    cta_url = body.get("cta_url", "")
    lead_id = body.get("lead_id")  # 单条模式
    custom_message = (body.get("custom_message") or "").strip()  # 前端预览后自定义内容

    # 获取该设备的引流配置
    _cfg_path = config_file("chat_messages.yaml")
    _refs = {}
    if _cfg_path.exists():
        with open(_cfg_path, encoding="utf-8") as f:
            _refs = (_yaml.safe_load(f) or {}).get("device_referrals", {}).get(device_id, {})

    # 获取设备线索
    try:
        from src.leads.store import get_leads_store
        ls = get_leads_store()
        if lead_id:
            leads = [ls.get_lead(lead_id)] if hasattr(ls, 'get_lead') else []
            leads = [l for l in leads if l]
        else:
            all_leads = ls.get_leads(statuses=["responded", "qualified"]) or []
            leads = [l for l in all_leads
                     if l.get("device_id") == device_id or l.get("source_device") == device_id]
    except Exception:
        leads = []

    if not leads:
        # 尝试从 Worker-03 拉取
        try:
            url = "http://192.168.0.103:8000/tiktok/qualified-leads?limit=50"
            resp = _ur.urlopen(_ur.Request(url), timeout=4)
            w3_leads = _json.loads(resp.read()).get("leads", [])
            leads = [l for l in w3_leads
                     if l.get("device_id") == device_id or l.get("source_device") == device_id]
        except Exception:
            pass

    if not leads:
        return {"ok": True, "sent": 0, "message": "该设备暂无合格线索", "device_id": device_id}

    # 发话术
    sent = 0
    errors = []
    # 通用联系方式：按优先级排序，最多取 2 个附在话术里
    _APP_PRIORITY = ["telegram", "whatsapp", "instagram", "line", "wechat", "viber", "signal", "facebook"]
    _APP_LABELS = {"telegram": "Telegram", "whatsapp": "WhatsApp", "instagram": "Instagram",
                   "line": "Line", "wechat": "WeChat", "viber": "Viber",
                   "signal": "Signal", "facebook": "Facebook"}
    _all_contacts_pitch = {k: v for k, v in _refs.items() if v and not k.startswith("_")}
    _contact_parts = []
    for _app in _APP_PRIORITY:
        if _app in _all_contacts_pitch:
            _contact_parts.append(f"{_APP_LABELS.get(_app, _app.capitalize())}: {_all_contacts_pitch[_app]}")
    for _app, _val in _all_contacts_pitch.items():
        if _app not in _APP_PRIORITY:
            _contact_parts.append(f"{_app.capitalize()}: {_val}")
    _contact_str = " / ".join(_contact_parts[:2])  # 最多显示 2 个

    for lead in leads[:max_pitch]:
        try:
            username = lead.get("username") or lead.get("name") or ""
            lid = lead.get("lead_id") or lead.get("id")
            if not username:
                continue

            # 生成话术（如果前端传入自定义内容直接用，否则用模板）
            if custom_message:
                pitch = custom_message.replace("{name}", username).replace("{username}", username)
            else:
                pitch_parts = ["嗨 " + username + "，谢谢你的互动！"]
                if _contact_str:
                    pitch_parts.append("有兴趣可加 " + _contact_str)
                if cta_url:
                    pitch_parts.append(cta_url)
                pitch = " ".join(pitch_parts)

            # 通过任务系统发送
            req_body = _json.dumps({
                "type": "tiktok_send_dm",
                "device_id": device_id,
                "params": {"recipient": username, "message": pitch}
            }).encode()
            req = _ur.Request(
                f"{local_api_base()}/tasks",
                data=req_body, headers={"Content-Type": "application/json"}, method="POST"
            )
            _ur.urlopen(req, timeout=5)
            sent += 1

            # 更新线索状态
            if lid:
                try:
                    upd = _json.dumps({"status": "qualified"}).encode()
                    upd_req = _ur.Request(
                        f"{local_api_base()}/leads/" + str(lid),
                        data=upd, headers={"Content-Type": "application/json"}, method="PUT"
                    )
                    _ur.urlopen(upd_req, timeout=3)
                except Exception:
                    pass
        except Exception as ex:
            errors.append(str(ex))

    return {
        "ok": True, "sent": sent, "total_leads": len(leads),
        "message": "已向 " + str(sent) + " 人发话术" + (" (配置: " + _contact_str + ")" if _contact_str else " (未配置引流账号)"),
        "errors": errors[:3] if errors else [],
        "device_id": device_id
    }


@router.get("/device/{device_id}/leads")
def device_leads(device_id: str, limit: int = 30, offset: int = 0, q: str = ""):
    """获取指定设备的线索列表，支持分页。"""
    import json as _json, urllib.request as _ur
    from datetime import datetime as _dt

    leads = []

    # 判断该设备节点（用于决定从哪里拉线索）
    _is_w3 = False
    try:
        from .devices_core import _load_aliases as _la
        _aliases = _la()
        _info = _aliases.get(device_id, {})
        _is_w3 = str(_info.get("host_name", "")).lower().startswith("worker")
    except Exception:
        pass

    # 通过 interactions 表明确归因到此设备的 lead_ids（含 Worker 设备自动建桩的线索）
    import sqlite3 as _sq3
    _attributed_lids: set = set()
    try:
        _ldb2 = str(data_file("leads.db"))
        _aconn = _sq3.connect(_ldb2, timeout=5)
        _attr_rows = _aconn.execute(
            "SELECT DISTINCT lead_id FROM interactions WHERE device_id=?", (device_id,)
        ).fetchall()
        _aconn.close()
        _attributed_lids = {r[0] for r in _attr_rows}
    except Exception:
        pass

    # 本地线索（含无 device_id 的全量线索，以及通过 interactions 归因的线索）
    try:
        from src.leads.store import get_leads_store
        ls = get_leads_store()
        _search_q = q.strip() if q else None
        all_local = ls.list_leads(search=_search_q, limit=500) or []
        for l in all_local:
            did = l.get("device_id") or l.get("source_device") or ""
            lid = l.get("id") or l.get("lead_id")
            # interactions 归因 OR 精确匹配 OR 本地设备兜底（device_id为空且不是w3设备）
            if lid in _attributed_lids or did == device_id or (not did and not _is_w3):
                leads.append(l)
    except Exception:
        pass

    # Worker-03 线索：使用 /tiktok/qualified-leads（含 last_message + recent_interactions）
    try:
        resp = _ur.urlopen(
            _ur.Request("http://192.168.0.103:8000/tiktok/qualified-leads?limit=200"), timeout=4)
        raw = _json.loads(resp.read())
        w3_leads = raw.get("leads", []) if isinstance(raw, dict) else []
        _q_lower = q.strip().lower() if q else ""
        for l in w3_leads:
            did = l.get("device_id") or l.get("source_device") or ""
            if not (did == device_id or (not did and _is_w3)):
                continue
            # 服务端搜索过滤：与本地 leads 保持一致
            if _q_lower:
                _uname = ((l.get("username") or "") + " " + (l.get("name") or "")).lower()
                if _q_lower not in _uname:
                    continue
            # 从 recent_interactions 提取 last_message（如果 API 字段为空）
            if not l.get("last_message"):
                _ri = l.get("recent_interactions") or []
                for _ri_item in _ri:
                    if _ri_item.get("direction") == "inbound" and _ri_item.get("content"):
                        l["last_message"] = _ri_item["content"][:200]
                        break
            leads.append(l)
    except Exception:
        pass

    # ── 用户名归一化去重（@Rajiya_Akhter == @Rajiya Akhter）
    import re as _re
    def _norm_user(s):
        s = (s or "").lower().strip()
        s = _re.sub(r"^@+", "", s)
        s = _re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", s)
        return s

    seen_keys: set = set()
    deduped: list = []
    for l in leads:
        uname = l.get("username") or l.get("name") or ""
        key = _norm_user(uname) or str(l.get("lead_id") or l.get("id") or id(l))
        if key and key not in seen_keys:
            seen_keys.add(key)
            deduped.append(l)
    leads = deduped

    # 按评分降序排序
    leads.sort(key=lambda x: float(x.get("score") or 0), reverse=True)

    # N1: 批量查询 interactions 获取 last_message + intent + 互动总数 + 状态覆盖 + 设备归因
    import json as _json_int, sqlite3 as _sq3

    _interactions_map: dict = {}  # lead_id → {content, intent, ts}
    _count_map: dict = {}          # lead_id → total interaction count (for badge)
    _qualify_override: dict = {}   # lead_id → "qualified"（qualify_manual 状态覆盖）
    _source_device_map: dict = {}  # lead_id → 最近互动的 device_id
    try:
        _ldb = str(data_file("leads.db"))
        _lconn = _sq3.connect(_ldb, timeout=5)
        _page_leads = leads[offset:offset+limit]
        _page_ids = [(l.get("id") or l.get("lead_id")) for l in _page_leads
                     if (l.get("id") or l.get("lead_id"))]
        if _page_ids:
            _ph = ",".join("?" * len(_page_ids))
            # 最近入站消息（最多1条/lead，带 metadata）
            _irows = _lconn.execute(
                f"""SELECT lead_id, content, metadata, created_at
                    FROM interactions
                    WHERE lead_id IN ({_ph}) AND direction='inbound' AND platform='tiktok'
                    ORDER BY created_at DESC""",
                _page_ids
            ).fetchall()
            # 全量互动计数（用于对话历史徽章）
            _crows = _lconn.execute(
                f"""SELECT lead_id, COUNT(*) FROM interactions
                    WHERE lead_id IN ({_ph}) GROUP BY lead_id""",
                _page_ids
            ).fetchall()
            # 状态覆盖：qualify_manual 操作记录（W03 线索状态持久化）
            _qrows = _lconn.execute(
                f"""SELECT lead_id, json_extract(metadata,'$.status')
                    FROM interactions
                    WHERE lead_id IN ({_ph}) AND action='qualify_manual'
                    ORDER BY created_at DESC""",
                _page_ids
            ).fetchall()
            # 设备归因：最近有 device_id 的互动（用于显示归因 badge）
            _drows = _lconn.execute(
                f"""SELECT lead_id, device_id FROM interactions
                    WHERE lead_id IN ({_ph}) AND device_id != ''
                    ORDER BY created_at DESC""",
                _page_ids
            ).fetchall()
        _lconn.close()
        for _ir in _irows:
            _lid = _ir[0]
            if _lid not in _interactions_map:
                _imeta = {}
                try: _imeta = _json_int.loads(_ir[2] or "{}")
                except Exception: pass
                _interactions_map[_lid] = {
                    "content": (_ir[1] or "")[:100],
                    "intent": _imeta.get("intent", ""),
                    "ts": _ir[3] or "",
                }
        for _cr in _crows:
            _count_map[_cr[0]] = int(_cr[1])
        for _qr in _qrows:
            if _qr[0] not in _qualify_override:
                _qualify_override[_qr[0]] = _qr[1] or "qualified"
        for _dr in _drows:
            if _dr[0] not in _source_device_map:
                _source_device_map[_dr[0]] = _dr[1]
    except Exception:
        pass

    # 设备别名缓存（用于 source_device_alias）+ 节点缓存（用于 source_node badge）
    _alias_cache: dict = {}
    _local_dids: set = set()  # 本地设备 ID 集合，用于判断 node
    try:
        from .devices_core import _load_aliases as _la2
        for _did2, _info2 in (_la2() or {}).items():
            _alias_cache[_did2] = _info2.get("alias", _did2[:6])
        # 本地设备只是别名表里有 host_name='主控' 或没有 host_name
        _local_dids = {_d for _d, _i in (_la2() or {}).items()
                       if not _i.get("host_name") or
                          not str(_i.get("host_name", "")).lower().startswith("worker")}
    except Exception:
        pass

    # 关键词快速意图分类（毫秒级，不调用 LLM，用单词边界避免误匹配）
    import re as _re
    def _quick_intent(msg: str) -> str:
        m = " " + (msg or "").lower() + " "  # 前后加空格确保单词边界
        if not m.strip():
            return ""
        # 负面信号（单词边界匹配，避免 "non profit" 误触）
        neg_patterns = [r'\bno\b', r'\bnon\b', r'\bstop\b', r'\bbasta\b',
                        r'\bnot interested\b', r'\bnon voglio\b', r'\bspam\b',
                        r'\bunsubscribe\b', r'\bleave me alone\b', r'\blasciami\b',
                        r'\bnon grazie\b', r'\bno thanks\b', r'\bblock\b']
        if any(_re.search(p, m) for p in neg_patterns):
            return "NO_REPLY"
        # 强正面信号：明确询问/请求行动
        buy = ["telegram", "whatsapp", "contatto", "contact", "come si", "how to",
               "quanto", "prezzo", "price", "costo", "cost", "link", "gruppo", "group",
               "unirmi", "join", "dove", "where", "quando", "when",
               "interessato", "interested", "info", "dettagli", "details",
               "numero", "number", "più info", "more info", "aggiungimi", "add me",
               # 意大利语额外关键词
               "mandami", "inviami", "scrivimi", "contattami", "fammi sapere",
               "come funziona", "mi interessa", "voglio sapere", "dimmi",
               "puoi mandarmi", "puoi scrivermi", "cosa è", "cos'è",
               # 英文额外
               "send me", "tell me", "what is", "how much", "sign me up",
               "i want", "i'm interested", "sounds good", "let me know"]
        if any(k in m for k in buy):
            return "NEEDS_REPLY"
        # 弱正面信号（含问号 + 实质内容，排除纯感叹）
        if "?" in m and len(m.strip()) > 5:
            return "NEEDS_REPLY"
        # 弱正面：表情符号 + 短句（互动意愿）
        emoji_interest = ["😊", "👍", "🔥", "💪", "❤️", "🙏", "✅", "💯"]
        if any(e in msg for e in emoji_interest) and len(msg.strip()) < 40:
            return "NEEDS_REPLY"
        return "OPTIONAL"

    # 格式化输出
    slim = []
    for l in leads[offset:offset+limit]:
        _lid = l.get("id") or l.get("lead_id")
        _inter = _interactions_map.get(_lid, {})
        # last_message 优先级：本地 DB > W03 API 字段 > recent_interactions（已在上方提取）
        _last_msg = _inter.get("content") or (l.get("last_message") or "")
        # intent 优先级：本地 DB > W03 API intent > 快速分类
        _intent = _inter.get("intent") or (l.get("intent") or "")
        if not _intent and _last_msg:
            _intent = _quick_intent(_last_msg)
        _int_count = _count_map.get(_lid, 0)
        # 状态优先级：qualify_manual 覆盖 > W03 API 状态（持久化 W03 手动升格）
        _status = _qualify_override.get(_lid) or l.get("status") or "responded"
        # 设备归因：interactions 记录 > API 字段 > 请求设备
        _src_dev = _source_device_map.get(_lid) or l.get("device_id") or l.get("source_device") or ""
        _src_alias = _alias_cache.get(_src_dev, _src_dev[:6]) if _src_dev else ""
        # 节点来源：本地别名表未包含该设备 → 视为 W03
        _src_node = "local" if (not _src_dev or _src_dev in _local_dids) else "worker03"
        slim.append({
            "id": _lid,
            "lead_id": l.get("lead_id") or l.get("id"),
            "username": l.get("username") or l.get("name") or "",
            "name": l.get("name") or l.get("username") or "",
            "score": round(float(l.get("score") or 0), 1),
            "status": _status,
            "device_id": _src_dev or device_id,
            "source_device_alias": _src_alias,             # 设备归因别名（用于面板 badge）
            "source_node": _src_node,                      # 来源节点：local / worker03
            "last_message": _last_msg[:120],
            "intent": _intent,                             # N1: 意图（DB优先→快速分类）
            "last_msg_at": _inter.get("ts", ""),           # N1: 消息时间
            "interaction_count": _int_count,               # N2徽章：互动总数
            "pitched_at": l.get("pitched_at") or l.get("last_pitched"),
            "updated_at": l.get("updated_at") or "",
            "status_overridden": _lid in _qualify_override, # 标记：状态已被手动覆盖
        })

    return {
        "ok": True, "device_id": device_id,
        "leads": slim,
        "total": len(leads),  # 去重后总数
        "raw_count": len(deduped),
        "limit": limit, "offset": offset,
    }


@router.get("/device/{device_id}/stats")
def device_stats(device_id: str, stat: str = "sessions"):
    """获取设备指定统计项的今日明细（会话/关注/私信等）。"""
    import json as _json, urllib.request as _ur
    from datetime import datetime as _dt

    items = []
    today = _dt.now().strftime("%Y-%m-%d")

    stat_type_map = {
        "sessions": ["tiktok_warmup", "tiktok_browse"],
        "watched":  ["tiktok_warmup", "tiktok_browse"],
        "followed": ["tiktok_follow"],
        "dms":      ["tiktok_send_dm"],
    }
    type_filter = stat_type_map.get(stat, [])

    # ── 本地任务库（直接调用模块函数，无需 get_task_store）
    try:
        from host.task_store import list_tasks
        all_tasks = list_tasks(limit=500) or []
        for t in all_tasks:
            if (t.get("device_id") != device_id) or \
               not (t.get("created_at") or "").startswith(today):
                continue
            tt = t.get("task_type") or t.get("type") or ""
            if type_filter and not any(f in tt for f in type_filter):
                continue
            ts_str = t.get("created_at") or ""
            time_str = ts_str[11:16] if len(ts_str) >= 16 else ""
            params = t.get("params") or {}
            label = params.get("recipient") or params.get("target_user") or \
                    tt.replace("tiktok_", "").replace("_", " ")
            st = t.get("status") or ""
            items.append({"time": time_str, "label": label,
                          "status": "✓" if st == "completed" else ("⟳" if st == "running" else st)})
    except Exception:
        pass

    # ── Worker-03 任务补充（如果本地无数据）
    if not items:
        try:
            url = "http://192.168.0.103:8000/tasks?device_id=" + \
                  _ur.quote(device_id) + "&limit=100"
            resp = _ur.urlopen(_ur.Request(url), timeout=4)
            w3_tasks = _json.loads(resp.read())
            if isinstance(w3_tasks, list):
                pass
            else:
                w3_tasks = w3_tasks.get("tasks") or w3_tasks.get("items") or []
            for t in w3_tasks:
                if not (t.get("created_at") or "").startswith(today):
                    continue
                tt = t.get("task_type") or t.get("type") or ""
                if type_filter and not any(f in tt for f in type_filter):
                    continue
                ts_str = t.get("created_at") or ""
                time_str = ts_str[11:16] if len(ts_str) >= 16 else ""
                params = t.get("params") or {}
                label = params.get("recipient") or params.get("target_user") or \
                        tt.replace("tiktok_", "").replace("_", " ")
                st = t.get("status") or ""
                items.append({"time": time_str, "label": label,
                              "status": "✓" if st == "completed" else st,
                              "via": "w03"})
        except Exception:
            pass

    if not items:
        stat_desc = {"sessions": "会话", "watched": "视频观看",
                     "followed": "关注", "dms": "私信"}
        items = [{"time": "", "label": "今日暂无" + stat_desc.get(stat, stat) + "记录", "status": ""}]

    return {"ok": True, "device_id": device_id, "stat": stat, "items": items, "date": today}


@router.get("/device/{device_id}/history")
def device_history(device_id: str, days: int = 7):
    """获取设备最近 N 天的统计数据，用于 sparkline 趋势图。"""
    try:
        from host.device_stats_aggregator import get_history
        rows = get_history(device_id, days)
    except Exception:
        rows = []

    # 若本地无历史数据，尝试从 W03 device-grid 补一条今日快照
    if not rows:
        import json as _json, urllib.request as _ur
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y-%m-%d")
        try:
            resp = _ur.urlopen(
                _ur.Request("http://192.168.0.103:8000/tiktok/devices",
                            headers={"Accept": "application/json"}), timeout=5)
            devices = _json.loads(resp.read())
            if isinstance(devices, dict):
                devices = devices.get("devices", devices.get("items", []))
            for dev in (devices or []):
                if (dev.get("device_id") or dev.get("id")) == device_id:
                    rows = [{
                        "date": today,
                        "sessions": int(dev.get("sessions_today") or 0),
                        "watched": int(dev.get("today_watched") or 0),
                        "follows": int(dev.get("today_followed") or 0),
                        "dms_sent": int(dev.get("today_dms") or 0),
                        "dms_responded": 0,
                        "leads_qualified": 0,
                        "algo_score": float(dev.get("algo_score") or 0),
                    }]
                    break
        except Exception:
            pass

    return {"ok": True, "device_id": device_id, "days": days, "history": rows}


@router.get("/device/{device_id}/lead/{lead_id}/history")
def device_lead_history(device_id: str, lead_id: int, limit: int = 12):
    """获取线索对话历史 — 从 interactions 表读取最近 N 条互动，oldest-first。

    返回:
        history: [{id, action, direction, content, created_at, intent, display_time}]
    """
    import json as _json_h
    from src.leads.store import get_leads_store as _gls_h
    from datetime import datetime as _dth

    try:
        _store = _gls_h()
        _rows = _store.get_interactions(lead_id, platform="tiktok", limit=limit)

        def _rel_time(ts_str: str) -> str:
            """将 ISO 时间转为相对时间（今天显示时:分，否则显示月/日）"""
            if not ts_str:
                return ""
            try:
                ts = _dth.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)
                now = _dth.now()
                diff = now - ts
                if diff.days == 0:
                    return ts.strftime("%H:%M")
                elif diff.days < 7:
                    return f"{diff.days}天前"
                else:
                    return ts.strftime("%m/%d")
            except Exception:
                return ts_str[:10]

        _action_label = {
            "send_dm": "私信",
            "dm_received": "回复",
            "auto_reply": "自动回复",
            "pitch": "发话术",
            "follow": "关注",
            "follow_back": "回关",
            "message_classified": "分类",
        }

        history = []
        for ix in reversed(_rows):  # 倒序→正向时间线
            _meta = {}
            try: _meta = _json_h.loads(ix.get("metadata") or "{}")
            except Exception: pass
            _intent = _meta.get("intent", "")
            _content = (ix.get("content") or "").strip()
            if not _content:
                continue  # 跳过无内容条目（follow/classify等行为）
            history.append({
                "id": ix.get("id"),
                "action": ix.get("action", ""),
                "action_label": _action_label.get(ix.get("action", ""), ix.get("action", "")),
                "direction": ix.get("direction", "outbound"),
                "content": _content[:300],
                "created_at": ix.get("created_at", ""),
                "display_time": _rel_time(ix.get("created_at", "")),
                "intent": _intent,
            })

        return {"ok": True, "lead_id": lead_id, "device_id": device_id, "history": history}
    except Exception as _e:
        import logging
        logging.getLogger(__name__).error("get lead history failed: %s", _e)
        return {"ok": False, "error": str(_e), "history": []}


@router.post("/device/{device_id}/lead/{lead_id}/qualify")
def device_lead_qualify(device_id: str, lead_id: int, body: dict = None):
    """升格线索状态（responded → qualified）。"""
    import urllib.request as _ur2, json as _j2
    status = (body or {}).get("status", "qualified")
    if status not in ("qualified", "responded", "converted", "pitched"):
        return {"ok": False, "error": f"无效状态: {status}"}
    try:
        # 优先尝试本地 DB（仅当 lead 确实存在时才更新）
        from src.leads.store import get_leads_store
        ls = get_leads_store()
        if ls.get_lead(lead_id):
            ls.update_lead(lead_id, status=status)
            return {"ok": True, "lead_id": lead_id, "status": status, "source": "local"}
    except Exception:
        pass
    # 本地无记录 → 用 username 创建正确 stub，实现 W03 状态真正持久化
    try:
        from src.leads.store import get_leads_store as _gls_q
        _lsq = _gls_q()
        _username = (body or {}).get("username", "")
        _new_lid = None
        # 统一的跨设备同步写入函数
        def _write_cross_device_override(_local_lid, _w03_lid, _st, _did):
            """同时在 W03 原始 lead_id 写入 qualify_manual，保证任何设备面板都能查到。"""
            if _local_lid == _w03_lid: return
            try:
                import sqlite3 as _sq3x
                _ldbx = str(data_file("leads.db"))
                _cx = _sq3x.connect(_ldbx, timeout=5)
                import json as _jx, datetime as _dtx
                _cx.execute("""INSERT INTO interactions
                    (lead_id, platform, action, direction, content, status, metadata, created_at, device_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (_w03_lid, "tiktok", "qualify_manual", "outbound",
                     f"跨设备覆盖: 已升格为{_st}(本地#{_local_lid})", "sent",
                     _jx.dumps({"status": _st, "device_id": _did,
                                "local_lead_id": _local_lid, "cross_device": True}),
                     _dtx.datetime.now(_dtx.timezone.utc).isoformat(), _did))
                _cx.commit(); _cx.close()
            except Exception:
                pass
        # 1. 先检查 username 是否已在本地 leads 存在（可能是之前 auto_discovered 建的）
        if _username:
            _found_lid = _lsq.find_by_platform_username("tiktok", _username)
            if _found_lid:
                _lsq.update_lead(_found_lid, status=status)
                _lsq.add_interaction(_found_lid, "tiktok", "qualify_manual",
                    direction="outbound", content=f"手动升格为{status}",
                    metadata={"status": status, "device_id": device_id, "w03_lead_id": lead_id},
                    device_id=device_id)
                # 跨设备：同步写入 W03 原始 lead_id（保证 W03 设备面板也能看到覆盖）
                _write_cross_device_override(_found_lid, lead_id, status, device_id)
                return {"ok": True, "lead_id": _found_lid, "status": status, "source": "local_found"}
            # 2. 创建真实 stub（带 username → platform_profile 可检索）
            _new_lid = _lsq.add_lead(
                name=_username.lstrip("@"), source_platform="tiktok",
                tags=["tiktok", "w03_qualified"],
            )
            if _new_lid:
                _lsq.add_platform_profile(_new_lid, "tiktok", username=_username.lstrip("@"))
                _lsq.update_lead(_new_lid, status=status)
        # 3. 记录 qualify_manual interaction（用于所有面板的状态覆盖查询）
        _target_lid = _new_lid or lead_id  # 优先用新建 lid，退而使用 W03 lid
        _meta = {"status": status, "device_id": device_id, "w03_lead_id": lead_id}
        _lsq.add_interaction(_target_lid, "tiktok", "qualify_manual",
            direction="outbound", content=f"手动升格为{status}",
            metadata=_meta, device_id=device_id)
        # 跨设备同步：同时对 W03 原始 lead_id 记录 override（统一函数）
        if _new_lid and lead_id != _new_lid:
            _write_cross_device_override(_new_lid, lead_id, status, device_id)
        _src = "local_created" if _new_lid else "w03_interaction_mark"
        return {"ok": True, "lead_id": _target_lid, "status": status, "source": _src}
    except Exception as _e:
        return {"ok": False, "error": str(_e)}


@router.get("/device/{device_id}/pitch/preview")
def device_pitch_preview(device_id: str, lead_name: str = "", username: str = ""):
    """预览将要发送给指定线索的话术内容（不实际发送）。"""
    import yaml as _yaml

    _cfg_path = config_file("chat_messages.yaml")
    _refs = {}
    if _cfg_path.exists():
        with open(_cfg_path, encoding="utf-8") as f:
            _refs = (_yaml.safe_load(f) or {}).get("device_referrals", {}).get(device_id, {})

    name = username or lead_name or "用户"
    _ap_prio2 = ["telegram", "whatsapp", "instagram", "line", "wechat", "viber", "signal", "facebook"]
    _ap_labels2 = {"telegram": "Telegram", "whatsapp": "WhatsApp", "instagram": "Instagram",
                   "line": "Line", "wechat": "WeChat", "viber": "Viber",
                   "signal": "Signal", "facebook": "Facebook"}
    _all_c2 = {k: v for k, v in _refs.items() if v and not k.startswith("_")}
    _c_parts2 = []
    for _ap in _ap_prio2:
        if _ap in _all_c2:
            _c_parts2.append(f"{_ap_labels2.get(_ap, _ap.capitalize())}: {_all_c2[_ap]}")
    for _ap, _v in _all_c2.items():
        if _ap not in _ap_prio2:
            _c_parts2.append(f"{_ap.capitalize()}: {_v}")
    _contact_str2 = " / ".join(_c_parts2[:2])

    pitch_parts = ["嗨 " + name + "，谢谢你的互动！"]
    if _contact_str2:
        pitch_parts.append("有兴趣可联系：" + _contact_str2)
    pitch = " ".join(pitch_parts)

    return {
        "ok": True,
        "device_id": device_id,
        "preview": pitch,
        "telegram": _refs.get("telegram", ""),
        "whatsapp": _refs.get("whatsapp", ""),
        "contacts": _all_c2,
        "configured": bool(_all_c2),
        "char_count": len(pitch),
    }


@router.get("/device/{device_id}/pitch/ai-preview")
def device_pitch_ai_preview(device_id: str, username: str = "", last_message: str = ""):
    """使用AI生成个性化话术预览（不实际发送）。

    参数:
    - username: TikTok 用户名
    - last_message: 对方最后一条消息（可选，用于个性化回复）
    """
    import yaml as _yaml
    from src.ai.llm_client import get_llm_client

    _cfg_path = config_file("chat_messages.yaml")
    _refs = {}
    if _cfg_path.exists():
        with open(_cfg_path, encoding="utf-8") as f:
            _refs = (_yaml.safe_load(f) or {}).get("device_referrals", {}).get(device_id, {})

    name = username or "用户"

    # 通用联系方式：按优先级拼接所有已配置的 app
    _ap_prio = ["telegram", "whatsapp", "instagram", "line", "wechat", "viber", "signal", "facebook"]
    _ap_labels = {"telegram": "Telegram", "whatsapp": "WhatsApp", "instagram": "Instagram",
                  "line": "Line", "wechat": "WeChat", "viber": "Viber",
                  "signal": "Signal", "facebook": "Facebook"}
    _all_c = {k: v for k, v in _refs.items() if v and not k.startswith("_")}
    _c_parts = []
    for _ap in _ap_prio:
        if _ap in _all_c:
            _c_parts.append(f"{_ap_labels.get(_ap, _ap.capitalize())}: {_all_c[_ap]}")
    for _ap, _v in _all_c.items():
        if _ap not in _ap_prio:
            _c_parts.append(f"{_ap.capitalize()}: {_v}")
    contact_hint = " / ".join(_c_parts[:3]) if _c_parts else ""

    # 语言检测：根据对方消息语言自动切换回复语言
    if last_message:
        _non_cjk = sum(1 for c in last_message if '\u4e00' <= c <= '\u9fff') == 0
        _lang_hint = "用和对方消息完全相同的语言回复（检测到的语言）" if _non_cjk else "用中文回复"
        system_prompt = (
            "你是一个TikTok私信营销员，正在回复一位已经和你互动过的TikTok用户。"
            f"规则：①{_lang_hint} ②直接回应对方说的内容不要重复问候 "
            "③自然引导他关注我们的社交账号/联系我们 ④消息长度15-35字 "
            "⑤口吻轻松友好，像真人聊天 ⑥联系方式必须出现在回复中。"
            "只输出私信文字，不要任何解释、符号或引号。"
        )
        user_prompt = (
            f"对方用户名：{name}\n"
            f"对方发来的消息：「{last_message}」\n"
            f"我方联系方式：{contact_hint or '（未配置）'}\n"
            f"请生成一条自然的续聊回复，顺带把联系方式发给他。"
        )
    else:
        # 初次引流模式：用户未回复，发送第一条引流话术
        system_prompt = (
            "你是一个TikTok营销机器人，专门通过私信把用户引流到我方社交账号。"
            "要求：用意大利语或英语写（根据用户档案），自然热情，15-30字，必须包含联系方式，不像广告，像朋友。"
            "直接输出私信内容，不要任何解释或引号。"
        )
        user_prompt = (
            f"用户名：{name}\n"
            f"我方联系方式：{contact_hint or '（未配置）'}\n"
            f"为这位互动过的TikTok用户生成一条引流私信，邀请他联系我们。"
        )

    try:
        client = get_llm_client()
        ai_text = client.chat_with_system(system_prompt, user_prompt, temperature=0.85, max_tokens=80, use_cache=False)
        ai_text = ai_text.strip().strip('"').strip("「」")
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("AI话术生成失败: %s", e)
        ai_text = ""

    # 回退到模板话术
    if not ai_text:
        parts = ["嗨 " + name + "，谢谢你的互动！"]
        if contact_hint:
            parts.append("有兴趣可联系：" + contact_hint)
        ai_text = " ".join(parts)

    tg = _refs.get("telegram", "")
    wa = _refs.get("whatsapp", "")
    return {
        "ok": True,
        "device_id": device_id,
        "preview": ai_text,
        "telegram": tg,
        "whatsapp": wa,
        "configured": bool(tg or wa),
        "char_count": len(ai_text),
        "ai_generated": True,
    }


@router.post("/referral-config/batch")
def set_referral_config_batch(body: dict):
    """批量更新多台设备的引流账号配置。

    Body: {"items": [{"device_id": "...", "telegram": "@x", "whatsapp": "+1"}, ...]}
    """
    import yaml as _yaml

    _items = body.get("items", [])
    if not _items:
        raise HTTPException(400, "items 不能为空")

    _cfg_path = config_file("chat_messages.yaml")
    _data = {}
    if _cfg_path.exists():
        with open(_cfg_path, encoding="utf-8") as _f:
            _data = _yaml.safe_load(_f) or {}
    _data.setdefault("device_referrals", {})

    _errors = []
    _updated = 0
    for _item in _items:
        _did = (_item.get("device_id") or "").strip()
        _tg = (_item.get("telegram") or "").strip()
        _wa = (_item.get("whatsapp") or "").strip()

        if not _did:
            _errors.append({"item": _item, "error": "device_id 不能为空"})
            continue

        # 格式自动修正
        if _tg and not _tg.startswith("@"):
            _tg = "@" + _tg
        if _wa and not _wa.startswith("+"):
            _errors.append({"device_id": _did, "error": f"WhatsApp 号码必须以 + 开头: {_wa}"})
            continue

        _ref = _data["device_referrals"].get(_did, {})
        if _tg:
            _ref["telegram"] = _tg
        if _wa:
            _ref["whatsapp"] = _wa
        _data["device_referrals"][_did] = _ref
        _updated += 1

    with open(_cfg_path, "w", encoding="utf-8") as _f:
        _yaml.dump(_data, _f, allow_unicode=True, default_flow_style=False)

    return {
        "ok": True,
        "updated": _updated,
        "errors": _errors,
        "referrals": _data["device_referrals"],
    }


@router.get("/daily-report")
def tiktok_daily_report():
    """今日运营日报：养号/关注/消息/引流汇总。"""
    from src.host.device_state import get_device_state_store
    import json as _j, time as _t

    ds = get_device_state_store("tiktok")
    # Filter out test/dummy device IDs
    _TEST_PREFIXES = ("TEST_", "BAD", "BLOCKED", "DECAY", "FAIL", "MANUAL",
                      "PROG", "REC", "RISKY", "SLOW", "WARMUP", "__seeds__")
    devices = [d for d in ds.list_devices()
               if not any(d.startswith(p) for p in _TEST_PREFIXES) and len(d) > 10]

    # If coordinator has no real devices, proxy to Worker-03 via cluster
    if not devices:
        try:
            from ..multi_host import get_cluster_coordinator
            import urllib.request as _ur
            coord = get_cluster_coordinator()
            workers = set()
            for h in coord._hosts.values():
                if h.online and h.host_ip:
                    workers.add(f"http://{h.host_ip}:{h.port}")
            for worker_url in workers:
                try:
                    req = _ur.Request(f"{worker_url}/tiktok/daily-report", method="GET")
                    resp = _ur.urlopen(req, timeout=10)
                    data = _j.loads(resp.read().decode())
                    # Ensure revenue key is present (older workers may not include it)
                    if "revenue" not in data:
                        try:
                            from src.leads.store import get_leads_store
                            data["revenue"] = get_leads_store().get_revenue_stats()
                        except Exception:
                            data["revenue"] = {"total_conversions": 0, "total_revenue": 0.0,
                                               "today_conversions": 0, "today_revenue": 0.0}
                    return data
                except Exception:
                    pass
        except Exception:
            pass
    today = _t.strftime("%Y-%m-%d")

    # 设备统计
    device_reports = []
    total = {"watched": 0, "liked": 0, "followed": 0, "dms": 0, "sessions": 0}
    for did in devices:
        s = ds.get_device_summary(did)
        sessions = s.get("sessions_today", 0)
        watched_today = ds.get_int(did, f"daily:{today}:watched")
        liked_today = ds.get_int(did, f"daily:{today}:liked")
        followed_today = ds.get_int(did, f"daily:{today}:followed")
        dms_today = ds.get_int(did, f"daily:{today}:dms")
        device_reports.append({
            "device_id": did,
            "short": did[:8],
            "phase": s.get("phase", ""),
            "day": s.get("day", 0),
            "algo_score": s.get("algorithm_score", 0),
            "sessions_today": sessions,
            "watched_today": watched_today,
            "liked_today": liked_today,
            "followed_today": followed_today,
            "dms_today": dms_today,
            "total_watched": s.get("total_watched", 0),
            "total_followed": s.get("total_followed", 0),
            "total_dms": s.get("total_dms_sent", 0),
        })
        total["watched"] += watched_today
        total["liked"] += liked_today
        total["followed_today"] = total.get("followed_today", 0) + followed_today
        total["dms_today"] = total.get("dms_today", 0) + dms_today
        total["followed"] += s.get("total_followed", 0)
        total["dms"] += s.get("total_dms_sent", 0)
        total["sessions"] += sessions

    # 聊天历史统计
    chat_count = 0
    follow_count = 0
    try:
        ch = data_file("chat_history.json")
        if ch.exists():
            cd = _j.loads(ch.read_text(encoding="utf-8"))
            chat_count = len(cd.get("chatted_users", {}))
        fl = data_file("follow_log.json")
        if fl.exists():
            fd = _j.loads(fl.read_text(encoding="utf-8"))
            follow_count = len(fd.get("followed_positions", {}))
    except Exception:
        pass

    # 今日任务结果统计 (from task store)
    today_task_stats = {"auto_replied": 0, "escalated": 0, "follow_backs": 0}
    try:
        from ..api import task_store as _ts
        import json as _json2
        import datetime as _dt
        # Tasks use UTC timestamps; accept both today and yesterday UTC (covers local day across midnight)
        _utc_today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
        _utc_yesterday = (_dt.datetime.utcnow() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        _valid_dates = {today, _utc_today, _utc_yesterday}
        all_tasks = _ts.list_tasks(limit=500)
        for t in all_tasks:
            created = t.get("created_at", "")
            # Accept if created on any valid date (handles UTC/local mismatch)
            if not any(created.startswith(d) for d in _valid_dates):
                continue
            if t.get("status") != "completed":
                continue
            result_str = t.get("result", "{}")
            try:
                result_obj = _json2.loads(result_str) if isinstance(result_str, str) else (result_str or {})
            except Exception:
                result_obj = {}
            inbox = result_obj.get("inbox_result", {})
            today_task_stats["auto_replied"] += inbox.get("auto_replied", 0)
            today_task_stats["escalated"] += inbox.get("escalated", 0)
            chat = result_obj.get("chat_result", {})
            today_task_stats["follow_backs"] += chat.get("messaged", 0)
            follow = result_obj.get("follow_result", {})
            today_task_stats.setdefault("followed", 0)
            today_task_stats["followed"] += follow.get("followed", 0)
    except Exception:
        pass

    # 营收数据（来自 leads.db）
    revenue = {"total_conversions": 0, "total_revenue": 0.0,
               "today_conversions": 0, "today_revenue": 0.0}
    try:
        from src.leads.store import get_leads_store
        revenue = get_leads_store().get_revenue_stats()
    except Exception:
        pass

    return {
        "date": today,
        "devices": device_reports,
        "total": total,
        "unique_chats": chat_count,
        "unique_follows": follow_count,
        "today": today_task_stats,
        "revenue": revenue,
    }


@router.get("/cron-status")
def tiktok_cron_status():
    """返回关键 cron 任务状态：上次运行时间、下次运行倒计时。"""
    import json as _json, time as _time
    from datetime import datetime as _dt, timezone as _tz
    try:
        _cfg_path = config_file("scheduled_jobs.json")
        jobs = _json.loads(_cfg_path.read_text(encoding="utf-8")) if _cfg_path.exists() else []
    except Exception:
        jobs = []

    def _next_run_secs(cron_expr: str) -> int:
        """估算下次运行距现在多少秒（仅支持 */N 格式）。"""
        try:
            parts = cron_expr.strip().split()
            if len(parts) >= 1 and parts[0].startswith("*/"):
                interval_min = int(parts[0][2:])
                now_min = _dt.now().minute
                next_min = ((now_min // interval_min) + 1) * interval_min
                secs_until = (next_min - now_min) * 60 - _dt.now().second
                return max(0, secs_until)
        except Exception:
            pass
        return -1

    result = []
    key_jobs = {"tiktok_inbox_10m", "daily_report_2350", "tiktok_followup_noon"}
    for job in jobs:
        if job.get("id") not in key_jobs and not job.get("enabled"):
            continue
        _next = _next_run_secs(job.get("cron", ""))
        result.append({
            "id": job.get("id"),
            "name": job.get("name"),
            "cron": job.get("cron"),
            "enabled": job.get("enabled", False),
            "last_run": job.get("last_run", ""),
            "next_run_secs": _next,
        })
    return {"ok": True, "jobs": result}


@router.post("/sync-w03-leads")
def sync_w03_leads(body: dict = {}):
    """将 Worker-03 合格/已回复线索同步到本地 leads.db，供跟进任务使用。

    返回: {ok, total_from_w03, synced(新建), updated(升格状态), skipped}
    """
    import json as _sj, urllib.request as _sur
    from src.leads.store import get_leads_store as _sgls

    _limit = body.get("limit", 200)
    try:
        _resp = _sur.urlopen(
            _sur.Request(f"http://192.168.0.103:8000/tiktok/qualified-leads?limit={_limit}"),
            timeout=10
        )
        _data = _sj.loads(_resp.read())
        _w3_leads = _data.get("leads", []) if isinstance(_data, dict) else []
    except Exception as _e:
        return {"ok": False, "error": f"无法连接 Worker-03: {_e}", "synced": 0, "updated": 0}

    _STATUS_ORDER = {"new": 0, "contacted": 1, "responded": 2, "qualified": 3, "converted": 4, "blacklisted": 5}
    _sls = _sgls()
    _synced = 0
    _updated = 0
    _skipped = 0

    for _lead in _w3_leads:
        _username = (_lead.get("username") or _lead.get("name") or "").strip()
        if not _username:
            _skipped += 1
            continue
        _username_clean = _username.lstrip("@")

        # 推断 device_id 归因
        _did = _lead.get("device_id") or ""
        if not _did:
            for _ri in (_lead.get("recent_interactions") or []):
                _di = _ri.get("device_id") or (_ri.get("metadata") or {}).get("device_id") or ""
                if _di:
                    _did = _di
                    break

        _status = _lead.get("status") or "responded"
        _score = float(_lead.get("score") or 0)
        _w3_lid = _lead.get("id") or _lead.get("lead_id")

        _pitched_at = _lead.get("pitched_at") or _lead.get("last_pitched") or ""

        # 检查本地是否已有此用户
        _existing_lid = _sls.find_by_platform_username("tiktok", _username)
        if _existing_lid:
            _existing = _sls.get_lead(_existing_lid)
            if _existing:
                _cur_status = _existing.get("status", "new")
                # 只升格，不降级
                if _STATUS_ORDER.get(_status, 0) > _STATUS_ORDER.get(_cur_status, 0):
                    _sls.update_lead(_existing_lid, status=_status)
                    _updated += 1
                else:
                    _skipped += 1
                # P2 去重修复：若 W03 已发过话术但本地无 send_dm 记录，补记 stub
                if _pitched_at and not _sls.has_dm_sent(_username_clean, "tiktok"):
                    _sls.add_interaction(_existing_lid, "tiktok", "send_dm",
                        direction="outbound", content="[W03已发话术，stub]",
                        metadata={"w03_pitched_at": _pitched_at, "stub": True, "w03_lead_id": _w3_lid},
                        device_id=_did or "")
            continue

        # 新建线索桩
        _new_lid = _sls.add_lead(
            name=_username_clean,
            source_platform="tiktok",
            tags=["tiktok", "w03_synced"],
        )
        if _new_lid:
            _sls.add_platform_profile(_new_lid, "tiktok", username=_username_clean)
            _sls.update_lead(_new_lid, status=_status, score=_score)
            # 记录同步 interaction（含 device_id 归因，供面板过滤）
            _sync_meta = {"w03_lead_id": _w3_lid, "sync": True}
            if _did:
                _sync_meta["device_id"] = _did
            _sls.add_interaction(_new_lid, "tiktok", "w03_sync",
                direction="outbound", content="从Worker-03同步",
                metadata=_sync_meta, device_id=_did or "")
            # P2 去重修复：若 W03 已发过话术，补记 send_dm stub 供去重检查
            if _pitched_at:
                _sls.add_interaction(_new_lid, "tiktok", "send_dm",
                    direction="outbound", content="[W03已发话术，stub]",
                    metadata={"w03_pitched_at": _pitched_at, "stub": True, "w03_lead_id": _w3_lid},
                    device_id=_did or "")
            _synced += 1
        else:
            _skipped += 1

    import logging as _slog
    _slog.getLogger(__name__).info(
        "[W03同步] total=%d 新建=%d 升格=%d 跳过=%d",
        len(_w3_leads), _synced, _updated, _skipped
    )
    return {
        "ok": True,
        "total_from_w03": len(_w3_leads),
        "synced": _synced,
        "updated": _updated,
        "skipped": _skipped,
    }


@router.get("/devices")
def tiktok_list_devices():
    """List all TikTok device states with summaries."""
    from src.host.device_state import get_device_state_store
    ds = get_device_state_store("tiktok")
    devices = ds.list_devices()
    return [ds.get_device_summary(did) for did in devices]


@router.get("/devices/{device_id}")
def tiktok_device_detail(device_id: str):
    """Get detailed TikTok device state."""
    from src.host.device_state import get_device_state_store
    ds = get_device_state_store("tiktok")
    summary = ds.get_device_summary(device_id)
    summary["all_state"] = ds.get_all(device_id)
    return summary


@router.get("/stats")
def tiktok_global_stats():
    """TikTok global statistics: follows, follow-backs, DMs, CRM pipeline."""
    from src.host.device_state import get_device_state_store
    from src.leads.follow_tracker import LeadsFollowTracker
    from src.leads.store import get_leads_store

    ds = get_device_state_store("tiktok")
    tracker = LeadsFollowTracker()
    store = get_leads_store()

    devices = ds.list_devices()
    device_stats = [ds.get_device_summary(did) for did in devices]

    totals = {
        "total_watched": sum(d["total_watched"] for d in device_stats),
        "total_liked": sum(d["total_liked"] for d in device_stats),
        "total_followed": sum(d["total_followed"] for d in device_stats),
        "total_dms_sent": sum(d["total_dms_sent"] for d in device_stats),
    }

    return {
        "devices": device_stats,
        "totals": totals,
        "follow_stats": tracker.get_stats(),
        "crm_pipeline": store.pipeline_stats(),
    }


@router.post("/devices/{device_id}/init")
def tiktok_init_device(device_id: str):
    """Initialize a TikTok device (set cold_start phase)."""
    from src.host.device_state import get_device_state_store
    ds = get_device_state_store("tiktok")
    ds.init_device(device_id)
    return ds.get_device_summary(device_id)


@router.post("/devices/{device_id}/reset")
def tiktok_reset_device(device_id: str, body: dict):
    """Reset device state fields."""
    from src.host.device_state import get_device_state_store
    ds = get_device_state_store("tiktok")
    for key, value in body.items():
        ds.set(device_id, key, value)
    return ds.get_device_summary(device_id)


@router.get("/devices/{device_id}/accounts")
def tiktok_device_accounts(device_id: str):
    """List all TikTok accounts on a device with per-account stats."""
    from src.host.device_state import get_device_state_store
    ds = get_device_state_store("tiktok")
    return {"device_id": device_id, "accounts": ds.get_account_summaries(device_id)}


@router.post("/devices/{device_id}/accounts/{account}/init")
def tiktok_init_account(device_id: str, account: str):
    """Initialize state for a specific TikTok account on a device."""
    from src.host.device_state import get_device_state_store
    ds = get_device_state_store("tiktok")
    ds.init_account(device_id, account)
    cid = ds.account_device_id(device_id, account)
    return ds.get_device_summary(cid)


@router.get("/devices/{device_id}/schedule")
def tiktok_account_schedule(device_id: str):
    """Return account scheduling status for a device."""
    from src.host.account_scheduler import get_account_scheduler
    sched = get_account_scheduler()
    sched.auto_discover_accounts(device_id)
    return {"device_id": device_id,
            "accounts": sched.get_schedule_status(device_id)}


@router.post("/devices/{device_id}/accounts/{account}/config")
def tiktok_account_config(device_id: str, account: str, body: dict):
    """Configure scheduling parameters for a TikTok account."""
    from src.host.account_scheduler import get_account_scheduler
    sched = get_account_scheduler()
    sched.register_account(
        device_id, account,
        daily_sessions=body.get("daily_sessions", 4),
        daily_minutes=body.get("daily_minutes", 120),
        cooldown_minutes=body.get("cooldown_minutes", 90),
        enabled=body.get("enabled", True),
        priority=body.get("priority", 0),
    )
    return {"status": "configured", "device_id": device_id, "account": account}


@router.get("/schedules")
def tiktok_all_schedules():
    """Return account scheduling status for all devices."""
    from src.host.account_scheduler import get_account_scheduler
    return {"schedules": get_account_scheduler().get_all_schedules()}


@router.get("/profiles")
def tiktok_all_profiles():
    """Return all account behavior profiles."""
    from src.behavior.account_profile import get_profile_manager
    return {"profiles": get_profile_manager().get_all_summaries()}


@router.get("/profiles/{device_id}")
def tiktok_device_profile(device_id: str, account: str = ""):
    """Get profile for a specific device/account."""
    from src.behavior.account_profile import get_profile_manager
    summary = get_profile_manager().get_summary(device_id, account)
    if not summary:
        raise HTTPException(status_code=404, detail="Profile not found")
    return summary


@router.get("/profiles/{device_id}/adaptive-params")
def tiktok_adaptive_params(device_id: str, account: str = ""):
    """Get adaptive warmup parameters based on account profile."""
    from src.behavior.account_profile import get_profile_manager
    params = get_profile_manager().get_adaptive_params(device_id, account)
    if not params:
        return {"status": "no_profile", "message": "Not enough data for adaptive params"}
    return {"status": "ok", "params": params}


@router.get("/seeds/{country}")
def tiktok_best_seeds(country: str, top_n: int = 10):
    """Get highest quality seed accounts for a target country."""
    from src.host.device_state import get_device_state_store
    ds = get_device_state_store("tiktok")
    seeds = ds.get_best_seeds(country, top_n=top_n, min_uses=1, min_hit_rate=0.0)
    return {"country": country, "seeds": seeds, "count": len(seeds)}


@router.get("/readiness")
def tiktok_readiness():
    """设备就绪度评估：每台设备的VPN/阶段/风险/就绪状态。

    集群感知：Coordinator 聚合 Worker 数据。
    """
    from src.host.device_state import get_device_state_store
    from src.device_control.device_manager import get_device_manager
    from src.behavior.vpn_manager import get_vpn_manager
    from src.behavior.vpn_health import get_vpn_health_monitor
    import time as _t

    mgr = get_device_manager(DEFAULT_DEVICES_YAML)
    devices = mgr.get_all_devices()
    ds = get_device_state_store("tiktok")
    vm = get_vpn_manager()
    health_mon = get_vpn_health_monitor()
    health_data = health_mon.get_status() if health_mon else {}

    # 加载配置池
    from src.host.routers.vpn import _load_pool
    pool = _load_pool()
    pool_configs = {c["id"]: c for c in pool.get("configs", [])}
    pool_countries = {c["id"]: c.get("country", "") for c in pool.get("configs", [])}
    # 统计配置池中可用的国家
    available_countries = list(set(c.get("country", "") for c in pool.get("configs", []) if c.get("country")))

    result_devices = []
    for d in devices:
        did = d.get("device_id", "") if isinstance(d, dict) else getattr(d, "device_id", "")
        if not did:
            continue

        is_online = (d.get("status", "") if isinstance(d, dict) else getattr(d, "status", "")) in ("connected", "online", "busy")
        short = did[:8]
        alias = ""
        try:
            from src.host.api import ALIASES
            alias = ALIASES.get(did, "")
        except Exception:
            pass

        # VPN 状态
        vpn_status = vm.status(did) if is_online else None
        vpn_connected = vpn_status.connected if vpn_status else False
        h = health_data.get(did, {})
        vpn_ip = h.get("verified_ip", "")
        vpn_country = health_mon._expected_countries.get(did, "") if health_mon else ""

        # TikTok 阶段
        summary = ds.get_device_summary(did)
        phase = summary.get("phase", "unknown")
        algo_score = summary.get("algorithm_score", 0)
        day = summary.get("day", 0)
        recovery = summary.get("recovery_active", False)
        sessions_today = summary.get("sessions_today", 0)

        # 就绪评估
        blockers = []
        if not is_online:
            blockers.append("设备离线")
        if is_online and not vpn_connected:
            blockers.append("VPN未连接")

        ready = is_online and len(blockers) == 0

        # 推荐养号参数
        if phase == "cold_start":
            recommend_duration = 30
        elif phase == "interest_building":
            recommend_duration = 35
        else:
            recommend_duration = 40

        # 意大利时区检查
        import datetime
        try:
            it_hour = (datetime.datetime.utcnow().hour + 1) % 24  # UTC+1 粗略
            in_golden = 9 <= it_hour <= 22
        except Exception:
            in_golden = True

        result_devices.append({
            "device_id": did,
            "short": short,
            "alias": alias,
            "online": is_online,
            "ready": ready,
            "blockers": blockers,
            "vpn_connected": vpn_connected,
            "vpn_ip": vpn_ip,
            "vpn_country": vpn_country,
            "phase": phase,
            "algo_score": round(algo_score, 3),
            "day": day,
            "recovery": recovery,
            "sessions_today": sessions_today,
            "recommend_duration": recommend_duration,
        })

    # Coordinator 模式：聚合 Worker
    if not result_devices:
        try:
            from src.host.multi_host import get_cluster_coordinator, load_cluster_config
            import urllib.request, json as _json_rd
            cfg_cl = load_cluster_config()
            if cfg_cl.get("role") == "coordinator":
                coord = get_cluster_coordinator()
                if coord:
                    overview = coord.get_overview()
                    for host in overview.get("hosts", []):
                        if not host.get("online"):
                            continue
                        ip = host.get("host_ip", "")
                        port = host.get("port", 8000)
                        try:
                            req = urllib.request.Request(
                                f"http://{ip}:{port}/tiktok/readiness", method="GET")
                            resp = urllib.request.urlopen(req, timeout=10)
                            worker_data = _json_rd.loads(resp.read().decode())
                            result_devices.extend(worker_data.get("devices", []))
                            if not available_countries:
                                available_countries = worker_data.get("available_countries", [])
                            break
                        except Exception:
                            continue
        except Exception:
            pass

    ready_count = sum(1 for d in result_devices if d["ready"])
    online_count = sum(1 for d in result_devices if d["online"])
    vpn_ok = sum(1 for d in result_devices if d["vpn_connected"])
    recovering = sum(1 for d in result_devices if d["recovery"])

    return {
        "devices": result_devices,
        "summary": {
            "total": len(result_devices),
            "online": online_count,
            "ready": ready_count,
            "vpn_connected": vpn_ok,
            "recovering": recovering,
        },
        "available_countries": sorted(available_countries),
    }


@router.post("/launch-campaign")
def tiktok_launch_campaign(body: dict):
    """一键养号编排引擎：选国家 → VPN部署 → 养号 → SSE进度。

    Body:
    {
        "country": "italy",           # 目标国家
        "duration_minutes": 30,       # 养号时长
        "device_ids": [...],          # 目标设备（可选，默认全部就绪设备）
        "auto_vpn": true,             # 自动部署VPN（默认true）
        "verify_geo": false,          # Geo-IP验证（默认false，养号中已有检查）
    }
    """
    import time as _t
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from src.device_control.device_manager import get_device_manager, DeviceStatus

    country = body.get("country", "italy").strip()
    duration = body.get("duration_minutes", 30)
    auto_vpn = body.get("auto_vpn", True)
    device_ids = body.get("device_ids")

    _cp = DEFAULT_DEVICES_YAML
    manager = get_device_manager(_cp)

    if not device_ids:
        device_ids = [d.device_id for d in manager.get_all_devices()
                      if d.status in (DeviceStatus.CONNECTED, DeviceStatus.BUSY)]

    if not device_ids:
        return {"ok": False, "error": "没有在线设备"}

    results = {"vpn_phase": {}, "task_phase": {}}
    vpn_deployed = 0

    # Phase 1: VPN 就绪（如果开启auto_vpn）
    if auto_vpn:
        from src.host.routers.vpn import _load_pool
        from src.behavior.vpn_manager import (
            get_vpn_manager, _import_via_intent, _start_vpn_adb,
            _stop_vpn_adb, check_vpn_status,
        )

        pool = _load_pool()
        # 找到匹配国家的配置
        matching_configs = [c for c in pool.get("configs", [])
                           if c.get("country", "").lower() == country.lower()]
        if not matching_configs:
            # 没有匹配的配置，尝试用当前配置
            vm = get_vpn_manager()
            if vm.current_config:
                pass  # 保持现有VPN，跳过部署
            else:
                return {"ok": False,
                        "error": f"配置池中没有 {country} 的VPN配置，请先添加"}
        else:
            # 用第一个匹配的配置部署
            target_config = matching_configs[0]
            uri = target_config["uri"]

            def _vpn_setup_one(did):
                short = did[:8]
                try:
                    status = check_vpn_status(did)
                    if status.connected:
                        return did, {"ok": True, "short": short, "action": "already_connected"}
                    _stop_vpn_adb(did)
                    _t.sleep(0.5)
                    if not _import_via_intent(did, uri):
                        return did, {"ok": False, "short": short, "error": "VPN导入失败"}
                    _t.sleep(1.5)
                    _start_vpn_adb(did)
                    _t.sleep(2)
                    s = check_vpn_status(did)
                    return did, {"ok": True, "short": short, "connected": s.connected}
                except Exception as e:
                    return did, {"ok": False, "short": short, "error": str(e)[:60]}

            with ThreadPoolExecutor(max_workers=10) as pool_exec:
                futs = {pool_exec.submit(_vpn_setup_one, did): did for did in device_ids}
                for fut in as_completed(futs):
                    did, res = fut.result()
                    results["vpn_phase"][did[:8]] = res
                    if res.get("ok"):
                        vpn_deployed += 1

    # Phase 2: 批量提交养号任务
    from src.host.task_origin import with_origin
    from ..api import task_store, get_worker_pool, run_task
    pool = get_worker_pool()
    tasks_created = 0
    for did in device_ids:
        try:
            task_id = task_store.create_task(
                task_type="tiktok_warmup",
                device_id=did,
                params=with_origin(
                    {
                        "duration_minutes": duration,
                        "target_country": country,
                        "phase": "auto",
                    },
                    "tiktok_onboarding",
                ),
            )
            pool.submit(task_id, did, run_task, task_id, _cp)
            tasks_created += 1
            results["task_phase"][did[:8]] = {"ok": True, "task_id": task_id}
        except Exception as e:
            results["task_phase"][did[:8]] = {"ok": False, "error": str(e)[:60]}

    return {
        "ok": tasks_created > 0,
        "country": country,
        "duration_minutes": duration,
        "total_devices": len(device_ids),
        "vpn_deployed": vpn_deployed,
        "tasks_created": tasks_created,
        "results": results,
    }


@router.get("/funnel")
def tiktok_funnel():
    """TikTok 转化漏斗 — 从养号到转化的各阶段数据。"""
    funnel = {"stages": []}

    # 设备阶段统计
    try:
        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        devs = ds.list_devices()
        phases = {}
        total_watched = 0
        total_followed = 0
        total_dms = 0
        for did in devs:
            s = ds.get_device_summary(did)
            phase = s.get("phase", "unknown")
            phases[phase] = phases.get(phase, 0) + 1
            total_watched += s.get("total_watched", 0)
            total_followed += s.get("total_followed", 0)
            total_dms += s.get("total_dms_sent", 0)
        funnel["device_phases"] = phases
        funnel["total_watched"] = total_watched
        funnel["total_followed"] = total_followed
        funnel["total_dms"] = total_dms
    except Exception:
        funnel["total_watched"] = 0
        funnel["total_followed"] = 0
        funnel["total_dms"] = 0

    # CRM 线索漏斗
    try:
        from src.leads.store import get_leads_store
        store = get_leads_store()
        pipeline = store.pipeline_stats()
        by_status = pipeline.get("by_status", {})
        funnel["leads_total"] = pipeline.get("total_leads", 0)
        funnel["leads_new"] = by_status.get("new", 0)
        funnel["leads_contacted"] = by_status.get("contacted", 0)
        funnel["leads_responded"] = by_status.get("responded", 0)
        funnel["leads_qualified"] = by_status.get("qualified", 0)
        funnel["leads_converted"] = by_status.get("converted", 0)
    except Exception:
        funnel["leads_total"] = 0

    # 回关统计
    try:
        from src.leads.follow_tracker import LeadsFollowTracker
        tracker = LeadsFollowTracker()
        fs = tracker.get_stats()
        funnel["follow_backs"] = fs.get("total_follow_backs", 0)
        funnel["follow_back_rate"] = fs.get("follow_back_rate", 0.0)
    except Exception:
        funnel["follow_backs"] = 0
        funnel["follow_back_rate"] = 0.0

    # P7-C: 若本机无设备数据，从 Worker-03 聚合实际数值
    _NUMERIC_KEYS = ("total_watched", "total_followed", "total_dms", "follow_backs",
                     "leads_total", "leads_new", "leads_contacted", "leads_responded",
                     "leads_qualified", "leads_converted")
    if funnel.get("total_watched", 0) == 0 and funnel.get("total_followed", 0) == 0:
        try:
            import urllib.request as _ufr, json as _jfr
            _req = _ufr.Request("http://192.168.0.103:8000/tiktok/funnel", method="GET")
            _resp = _ufr.urlopen(_req, timeout=5)
            _wf = _jfr.loads(_resp.read().decode())
            for _k in _NUMERIC_KEYS:
                if _wf.get(_k, 0) > 0:
                    funnel[_k] = funnel.get(_k, 0) + _wf[_k]
            if _wf.get("device_phases"):
                funnel["device_phases"] = _wf["device_phases"]
            funnel["_source"] = "worker03"
        except Exception:
            funnel["_source"] = "local_only"

    # 构建漏斗阶段（7段：养号→关注→回关→私信→回复→合格→转化）
    stages = [
        {"name": "刷视频",   "key": "watched",   "value": funnel.get("total_watched", 0),    "color": "#8b5cf6"},
        {"name": "关注用户", "key": "followed",   "value": funnel.get("total_followed", 0),   "color": "#3b82f6"},
        {"name": "被回关",   "key": "followback", "value": funnel.get("follow_backs", 0),     "color": "#06b6d4"},
        {"name": "发私信",   "key": "dms",        "value": funnel.get("total_dms", 0),        "color": "#f59e0b"},
        {"name": "已回复",   "key": "responded",  "value": funnel.get("leads_responded", 0),  "color": "#22c55e"},
        {"name": "合格线索", "key": "qualified",   "value": funnel.get("leads_qualified", 0),  "color": "#f97316"},
        {"name": "已转化",   "key": "converted",   "value": funnel.get("leads_converted", 0),  "color": "#ef4444"},
    ]

    # 阶段间转化率
    for i in range(1, len(stages)):
        prev = stages[i - 1]["value"]
        curr = stages[i]["value"]
        stages[i]["conversion_rate"] = round(curr / prev * 100, 1) if prev > 0 else 0
    stages[0]["conversion_rate"] = 100.0

    funnel["stages"] = stages

    # 对话维度统计（从 conversations.db 获取引流状态）
    try:
        import sqlite3
        conv_db = str(data_file("conversations.db"))
        conn = sqlite3.connect(conv_db, timeout=5)
        total_convs = conn.execute("SELECT COUNT(DISTINCT lead_id) FROM conversations").fetchone()[0]
        # 高轮次对话（>=5条消息，暗示深度互动）
        deep_convs = conn.execute(
            "SELECT COUNT(*) FROM (SELECT lead_id FROM conversations GROUP BY lead_id HAVING COUNT(*)>=5)"
        ).fetchone()[0]
        # 有用户回复的对话
        replied_convs = conn.execute(
            "SELECT COUNT(DISTINCT lead_id) FROM conversations WHERE role='user'"
        ).fetchone()[0]
        conn.close()
        funnel["conversation_stats"] = {
            "total": total_convs,
            "deep": deep_convs,
            "replied": replied_convs,
            "deep_rate": round(deep_convs / total_convs * 100, 1) if total_convs else 0,
            "reply_rate": round(replied_convs / total_convs * 100, 1) if total_convs else 0,
        }
    except Exception:
        funnel["conversation_stats"] = {"total": 0, "deep": 0, "replied": 0}

    # 各设备转化对比
    try:
        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        dev_compare = []
        for did in ds.list_devices():
            s = ds.get_device_summary(did)
            alias = s.get("alias", did[:8])
            w = s.get("total_watched", 0)
            f = s.get("total_followed", 0)
            d = s.get("total_dms_sent", 0)
            dev_compare.append({
                "device_id": did, "alias": alias,
                "watched": w, "followed": f, "dms": d,
                "follow_rate": round(f / w * 100, 1) if w else 0,
                "dm_rate": round(d / f * 100, 1) if f else 0,
            })
        dev_compare.sort(key=lambda x: x["dms"], reverse=True)
        funnel["device_compare"] = dev_compare[:20]
    except Exception:
        funnel["device_compare"] = []

    return funnel


@router.get("/qualified-leads")
def tiktok_qualified_leads(limit: int = 20, min_score: float = 0):
    """热门线索列表（responded + qualified），含 TikTok 用户名和最近互动摘要。
    同时聚合 Worker-03 的线索数据（去重）。"""
    from src.leads.store import get_leads_store
    store = get_leads_store()

    # 查 responded + qualified 两种状态
    all_local: list = []
    for st in ("qualified", "responded"):
        chunk = store.list_leads(
            status=st, platform="tiktok",
            order_by="updated_at DESC", limit=limit,
        )
        all_local.extend(chunk)

    # 按 score 降序去重（同 lead_id 且同规范化用户名只保留一条）
    seen_ids: set = set()
    seen_unames: set = set()
    deduped: list = []
    for lead in sorted(all_local, key=lambda x: float(x.get("score") or 0), reverse=True):
        lid = lead["id"]
        lkey = _normalize_uname(lead.get("name") or "")
        if lid not in seen_ids and (not lkey or lkey not in seen_unames):
            seen_ids.add(lid)
            if lkey:
                seen_unames.add(lkey)
            deduped.append(lead)

    # 从 Worker-03 拉取线索（如果有）
    try:
        import urllib.request as _ur, json as _jj
        _w3_url = "http://192.168.0.103:8000/leads?status=responded&order_by=score+DESC&limit=30"
        _resp = _ur.urlopen(_ur.Request(_w3_url), timeout=4)
        _w3_data = _jj.loads(_resp.read())
        _w3_leads = _w3_data.get("leads", []) if isinstance(_w3_data, dict) else _w3_data
        for wl in _w3_leads:
            # 用规范化用户名去重，防止 @Rajiya Akhter vs @Rajiya_Akhter 重复
            _wkey = _normalize_uname(wl.get("name") or wl.get("username") or "")
            if _wkey and _wkey not in seen_unames:
                seen_unames.add(_wkey)
                wl["_source"] = "worker03"
                deduped.append(wl)
    except Exception:
        pass

    # 按 score 重新排序，取前 limit 条
    deduped.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    deduped = deduped[:limit]

    result = []
    for lead in deduped:
        source = lead.get("_source", "local")
        if source == "worker03":
            # Worker-03 数据已有结构，直接用
            result.append({
                "lead_id": lead.get("id") or lead.get("lead_id"),
                "username": lead.get("platform_username") or lead.get("name", ""),
                "name": lead.get("name", ""),
                "score": round(float(lead.get("score") or 0), 1),
                "status": lead.get("status", "responded"),
                "updated_at": lead.get("updated_at", ""),
                "source": "worker03",
                "recent_interactions": [],
            })
            continue
        profiles = store.get_platform_profiles(lead["id"])
        tk_profile = next((p for p in profiles if p.get("platform") == "tiktok"), {})
        recent = store.get_interactions(lead["id"], platform="tiktok", limit=3)
        # 获取最近一条对方发来的消息
        last_inbound = next(
            (ix for ix in recent if ix.get("direction") == "inbound"),
            None
        )
        result.append({
            "lead_id": lead["id"],
            "username": tk_profile.get("username", "") or lead.get("name", ""),
            "name": lead.get("name", ""),
            "score": round(float(lead.get("score") or 0), 1),
            "status": lead.get("status", ""),
            "updated_at": lead.get("updated_at", ""),
            "source": "local",
            "last_message": (last_inbound.get("content") or "")[:100] if last_inbound else "",
            "recent_interactions": [
                {
                    "action": ix["action"],
                    "direction": ix["direction"],
                    "content": (ix.get("content") or "")[:80],
                    "created_at": ix["created_at"],
                }
                for ix in recent
            ],
        })
    return {"leads": result, "total": len(result)}


@router.get("/daily-report/export")
def tiktok_daily_report_export():
    """生成今日运营战报 HTML（可截图/打印/分享）。"""
    from fastapi.responses import HTMLResponse
    import datetime as _dt
    try:
        report = tiktok_daily_report()
        funnel_data = tiktok_funnel()
    except Exception as exc:
        return HTMLResponse(f"<pre>Error generating report: {exc}</pre>", status_code=500)

    today = report.get("date", str(_dt.date.today()))
    total = report.get("total", {})
    devices = report.get("devices", [])
    stages = funnel_data.get("stages", [])
    max_val = max((s["value"] for s in stages), default=1) or 1

    stage_rows = "".join(
        f'<tr><td style="padding:6px 12px;color:#666;text-align:right;white-space:nowrap">{s["name"]}</td>'
        f'<td style="padding:6px 8px"><div style="height:20px;width:{max(2,round(s["value"]/max_val*100))}%;'
        f'background:{s["color"]};border-radius:4px;min-width:2px"></div></td>'
        f'<td style="padding:6px 8px;font-weight:600;color:#333">{s["value"]}</td></tr>'
        for s in stages
    )
    device_rows = "".join(
        f'<tr><td>{d.get("short", d.get("device_id",""))[:8]}</td><td>{d.get("phase","-")}</td>'
        f'<td>{d.get("watched_today",0)}</td><td>{d.get("liked_today",0)}</td>'
        f'<td>{d.get("followed_today",0)}</td><td>{d.get("dms_today",0)}</td>'
        f'<td>{d.get("algo_score","-")}</td></tr>'
        for d in devices
    )
    fb_rate = 0.0
    tf = total.get("followed", 0) or funnel_data.get("total_followed", 0)
    fb = funnel_data.get("follow_backs", 0)
    if tf > 0:
        fb_rate = round(fb / tf * 100, 1)

    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TikTok 战报 · {today}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"Helvetica Neue",Arial,sans-serif;background:#f0f2f5;padding:20px;color:#1a1a2e}}
.hdr{{background:linear-gradient(135deg,#1a1a2e,#16213e);color:white;border-radius:12px;padding:24px;margin-bottom:20px}}
.hdr h1{{font-size:22px;font-weight:700}}.hdr p{{font-size:13px;opacity:.7;margin-top:4px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:12px;margin-bottom:20px}}
.card{{background:white;border-radius:10px;padding:16px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
.val{{font-size:28px;font-weight:700}}.lbl{{font-size:11px;color:#888;margin-top:4px}}
.sec{{background:white;border-radius:10px;padding:16px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
.sec h2{{font-size:14px;font-weight:600;margin-bottom:12px}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{text-align:left;padding:8px;background:#f8f9fa;color:#666;font-weight:600}}
td{{padding:8px;border-bottom:1px solid #f0f2f5}}
.ft{{text-align:center;margin-top:20px;font-size:11px;color:#aaa}}
@media print{{body{{background:white;padding:0}}.sec,.card{{box-shadow:none;border:1px solid #eee}}}}
</style></head><body>
<div class="hdr"><h1>&#127914; TikTok 运营战报</h1><p>{today} · OpenClaw 自动化系统</p></div>
<div class="grid">
  <div class="card"><div class="val" style="color:#8b5cf6">{total.get("watched",0)}</div><div class="lbl">今日刷视频</div></div>
  <div class="card"><div class="val" style="color:#3b82f6">{total.get("followed_today",total.get("followed",0))}</div><div class="lbl">今日关注</div></div>
  <div class="card"><div class="val" style="color:#06b6d4">{fb}</div><div class="lbl">累计回关 ({fb_rate}%)</div></div>
  <div class="card"><div class="val" style="color:#a78bfa">{total.get("dms_today",total.get("dms",0))}</div><div class="lbl">今日私信</div></div>
  <div class="card"><div class="val" style="color:#f97316">{funnel_data.get("leads_qualified",0)}</div><div class="lbl">合格线索</div></div>
  <div class="card"><div class="val" style="color:#ef4444">{funnel_data.get("leads_converted",0)}</div><div class="lbl">已转化</div></div>
</div>
<div class="sec"><h2>&#128200; 转化漏斗</h2><table><tbody>{stage_rows}</tbody></table></div>
<div class="sec"><h2>&#128241; 设备明细</h2>
<table><thead><tr><th>设备</th><th>阶段</th><th>刷视频</th><th>点赞</th><th>关注</th><th>私信</th><th>算法分</th></tr></thead>
<tbody>{device_rows}</tbody></table></div>
<div class="ft">由 OpenClaw 自动生成 · {today} · 如需手动归因请访问后台</div>
</body></html>"""
    return HTMLResponse(html)


# ── 实时对话监控 + 人工升级队列 ──────────────────────────────────────────────────

import threading as _threading
_escalation_queue: list = []   # {"contact", "device_id", "message", "intent", "ts"}
_escalation_lock = _threading.Lock()


@router.post("/escalation/add")
def add_escalation(body: dict):
    """内部调用: 将对话添加到人工处理队列（由 check_inbox 自动触发）。"""
    with _escalation_lock:
        _escalation_queue.append({
            "contact": body.get("contact", ""),
            "device_id": body.get("device_id", ""),
            "message": body.get("message", "")[:500],
            "intent": body.get("intent", ""),
            "ts": body.get("ts", ""),
        })
        # 只保留最近 50 条
        if len(_escalation_queue) > 50:
            del _escalation_queue[:-50]
    return {"ok": True}


@router.get("/escalation-queue")
def get_escalation_queue():
    """获取待人工处理的对话队列。支持集群聚合。"""
    with _escalation_lock:
        items = list(reversed(_escalation_queue))
    # If local queue empty, try to aggregate from cluster workers
    if not items:
        try:
            import urllib.request as _ur, json as _jj
            from ..multi_host import get_cluster_coordinator
            coord = get_cluster_coordinator()
            all_items = []
            for h in coord._hosts.values():
                if h.online and h.host_ip:
                    try:
                        url = f"http://{h.host_ip}:{h.port}/tiktok/escalation-queue"
                        req = _ur.Request(url, method="GET")
                        resp = _ur.urlopen(req, timeout=5)
                        data = _jj.loads(resp.read().decode())
                        all_items.extend(data.get("items", []))
                    except Exception:
                        pass
            if all_items:
                return {"count": len(all_items), "items": all_items}
        except Exception:
            pass
    return {"count": len(items), "items": items}


@router.delete("/escalation-queue/{idx}")
def dismiss_escalation(idx: int):
    """从队列中移除一条升级记录（已处理）。"""
    with _escalation_lock:
        rev_idx = len(_escalation_queue) - 1 - idx
        if 0 <= rev_idx < len(_escalation_queue):
            _escalation_queue.pop(rev_idx)
    return {"ok": True}


@router.delete("/escalation-queue")
def clear_escalation_queue():
    """清空人工处理队列。"""
    with _escalation_lock:
        _escalation_queue.clear()
    return {"ok": True}


# ─── 热门线索转化话术引擎 ───────────────────────────────────────────────────────

def _normalize_uname(name: str) -> str:
    """规范化 TikTok 用户名，用于去重（空格=下划线，忽略大小写，忽略中点/省略号）。"""
    import re as _re2
    if not name:
        return ""
    n = name.lower().lstrip("@").strip().rstrip(".")
    # 中点·、省略号…、空格、下划线、连字符、点 全部去掉
    n = _re2.sub(r'[\s_.\-·…]+', '', n)
    # 只保留字母数字
    n = _re2.sub(r'[^\w]', '', n)
    return n


def _gen_referral_pitch(name: str, device_id: str, cta_url: str = "") -> str:
    """用设备配置的 TG/WA 联系方式生成意大利语引流话术。
    返回空字符串表示该设备未配置联系方式。"""
    import random as _r
    try:
        import yaml as _yaml
        _cfg_path = config_file("chat_messages.yaml")
        with open(_cfg_path, encoding="utf-8") as _f:
            _cfg = _yaml.safe_load(_f) or {}
        refs = _cfg.get("device_referrals", {}).get(device_id, {})
        telegram = refs.get("telegram", "").strip()
        whatsapp = refs.get("whatsapp", "").strip()
        clean = name.lstrip("@").split("_")[0].capitalize() if name else "amico"
        if telegram and whatsapp:
            templates = [
                f"Ciao {clean}! Ho qualcosa di interessante per te 😊 Scrivimi su Telegram: {telegram} o WhatsApp: {whatsapp}",
                f"Ehi {clean}! Parliamo di più su Telegram: {telegram} oppure WhatsApp: {whatsapp} 🔥",
                f"Ciao {clean}! Ti aspetto su Telegram {telegram} o WA {whatsapp} per un'opportunità speciale!",
            ]
        elif telegram:
            templates = [
                f"Ciao {clean}! Ho qualcosa di speciale per te 😊 Scrivimi su Telegram: {telegram}",
                f"Ehi {clean}! Parliamo su Telegram: {telegram} — ho un'offerta interessante!",
                f"Ciao {clean}! Contattami su Telegram {telegram} quando puoi 🎯",
            ]
        elif whatsapp:
            templates = [
                f"Ciao {clean}! Scrivimi su WhatsApp: {whatsapp} — ho qualcosa per te!",
                f"Ehi {clean}! Ti aspetto su WhatsApp {whatsapp} 😊",
            ]
        else:
            return ""
        msg = _r.choice(templates)
        if cta_url:
            msg += f" {cta_url}"
        return msg
    except Exception:
        return ""


def _gen_pitch(name: str, cta_url: str, last_msg: str, llm=None) -> str:
    """生成个性化转化话术。优先LLM，降级到模板。"""
    cta_part = f" {cta_url}" if cta_url else ""
    if llm:
        try:
            ctx = f" They last said: '{last_msg[:80]}'" if last_msg else ""
            prompt = (f"Write a short TikTok DM follow-up to {name} who showed buying interest.{ctx}"
                      f" Be warm, personal, 1-2 sentences max, include a gentle CTA.{' CTA URL: '+cta_url if cta_url else ''}"
                      f"\nOutput only the message itself.")
            reply = llm.chat([{"role": "user", "content": prompt}]).strip().split('\n')[0].strip()
            if len(reply) >= 20:
                return reply
        except Exception:
            pass
    return (f"Hey {name}! 👋 Really enjoyed our chat. I have something special that might interest you "
            f"— let's connect and I'll share more details!{cta_part}")


@router.post("/pitch-hot-leads")
def pitch_hot_leads(body: dict = None):
    """对已回应线索发送引流话术（TG/WA联系方式），推进到qualified状态。

    body: {
      "min_score": 10,            # 最低评分门槛（批量模式）
      "max_pitch": 5,             # 单次最多处理数量（批量模式）
      "target_username": "",      # 指定用户名（单人模式，优先）
      "lead_id": null,            # 指定 lead_id（单人模式）
      "cta_url": "",              # 附加的跳转链接（可选）
      "dry_run": false            # 预览模式，不真正发送
    }
    """
    body = body or {}
    min_score = body.get("min_score", 10)
    max_pitch = body.get("max_pitch", 5)
    cta_url = (body.get("cta_url") or "").strip()
    dry_run = body.get("dry_run", False)
    target_username = (body.get("target_username") or "").strip().lstrip("@")
    specific_lead_id = body.get("lead_id")

    # 单人模式：直接用指定用户名
    if target_username:
        hot_leads = [{"name": target_username, "id": specific_lead_id, "score": 99}]
    else:
        # 批量模式：获取热门线索（responded状态 + score达标）
        hot_leads = []
        # 先查本地
        try:
            import urllib.request as _ur, json as _jj
            url = f"{local_api_base()}/leads?status=responded&order_by=score+DESC&limit={max_pitch * 3}"
            resp = _ur.urlopen(_ur.Request(url), timeout=5)
            all_leads = _jj.loads(resp.read())
            if isinstance(all_leads, dict):
                all_leads = all_leads.get("leads", all_leads.get("items", []))
            hot_leads = [l for l in all_leads if (l.get("score") or 0) >= min_score][:max_pitch]
        except Exception:
            pass
        # 本地无数据则查 Worker-03
        if not hot_leads:
            try:
                import urllib.request as _ur2, json as _jj2
                url2 = f"http://192.168.0.103:8000/leads?status=responded&order_by=score+DESC&limit={max_pitch * 3}"
                resp2 = _ur2.urlopen(_ur2.Request(url2), timeout=5)
                all_leads2 = _jj2.loads(resp2.read())
                if isinstance(all_leads2, dict):
                    all_leads2 = all_leads2.get("leads", all_leads2.get("items", []))
                hot_leads = [l for l in all_leads2 if (l.get("score") or 0) >= min_score][:max_pitch]
            except Exception as _e:
                if not hot_leads:
                    return {"ok": False, "error": f"获取线索失败: {_e}"}

    # 批量模式：按规范化用户名去重（空格/下划线/大小写差异视为同人）
    if not target_username:
        _dedup_map: dict = {}
        for _l in hot_leads:
            _key = _normalize_uname(_l.get("username") or _l.get("name") or "")
            if not _key:
                continue
            if _key not in _dedup_map or (_l.get("score") or 0) > (_dedup_map[_key].get("score") or 0):
                _dedup_map[_key] = _l
        hot_leads = list(_dedup_map.values())

    if not hot_leads:
        return {"ok": True, "pitched": 0,
                "message": f"暂无 score≥{min_score} 的已回应线索"}

    # 获取 Worker-03 在线设备列表作为发送池
    _sender_devices = []
    try:
        import urllib.request as _ur2, json as _jj2
        resp2 = _ur2.urlopen(_ur2.Request("http://192.168.0.103:8000/devices"), timeout=4)
        devs = _jj2.loads(resp2.read())
        if isinstance(devs, dict):
            devs = devs.get("devices", [])
        _sender_devices = [d["device_id"] for d in devs
                           if d.get("status") in ("connected", "online", "busy", "active")
                           and d.get("device_id")]
    except Exception:
        pass
    if not _sender_devices:
        return {"ok": False, "error": "Worker-03 无在线设备，无法发送话术"}

    # LLM 客户端
    _llm = None
    try:
        from src.ai.llm_client import get_llm_client
        _llm = get_llm_client()
    except Exception:
        pass

    results = []
    # 优先使用有 TG/WA 配置的设备（_gen_referral_pitch 能生成意大利语话术）
    _configured_devices = [d for d in _sender_devices if _gen_referral_pitch("test", d)]
    _preferred_devices = _configured_devices if _configured_devices else _sender_devices
    _dev_idx = 0

    for lead in hot_leads:
        name = (lead.get("username") or lead.get("name") or "").strip()
        lead_id = lead.get("id") or lead.get("lead_id") or specific_lead_id
        score = lead.get("score", 0)
        last_msg = lead.get("last_message") or lead.get("notes") or ""

        # 清理 @ 前缀获取纯用户名
        clean_name = (target_username or name).lstrip("@")
        if not clean_name:
            continue

        # 优先使用有配置的设备，轮询
        device_id = _preferred_devices[_dev_idx % len(_preferred_devices)]
        _dev_idx += 1

        # 生成意大利语引流话术（TG/WA联系方式）
        pitch = _gen_referral_pitch(clean_name, device_id, cta_url)
        # 引流话术为空时降级到 LLM 生成
        if not pitch:
            pitch = _gen_pitch(clean_name, cta_url, last_msg, _llm)

        task_id = None
        if not dry_run:
            try:
                import urllib.request as _ur3, json as _jj3
                # 创建发送任务（自动路由到 Worker-03）
                _payload = _jj3.dumps({
                    "type": "tiktok_send_dm",
                    "device_id": device_id,
                    "params": {"recipient": clean_name, "message": pitch}
                }).encode()
                _req3 = _ur3.Request(f"{local_api_base()}/tasks", data=_payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
                _resp3 = _ur3.urlopen(_req3, timeout=10)
                task_id = _jj3.loads(_resp3.read()).get("task_id", "")

                # 推进线索状态 → qualified
                if lead_id:
                    try:
                        _put = _ur3.Request(
                            f"{local_api_base()}/leads/{lead_id}",
                            data=_jj3.dumps({"status": "qualified"}).encode(),
                            headers={"Content-Type": "application/json"},
                            method="PUT"
                        )
                        _ur3.urlopen(_put, timeout=5)
                    except Exception:
                        pass
            except Exception as _e2:
                results.append({"lead": name, "error": str(_e2)[:80]})
                continue

        results.append({
            "lead": name,
            "lead_id": lead_id,
            "score": score,
            "device": device_id[:8],
            "pitch_preview": pitch[:100],
            "task_id": (task_id or "")[:8] if task_id else None,
        })

    pitched = len([r for r in results if r.get("task_id")])
    return {
        "ok": True,
        "pitched": pitched if not dry_run else 0,
        "preview": dry_run,
        "total_hot": len(hot_leads),
        "items": results,
    }


@router.post("/batch-auto-reply")
def batch_auto_reply(body: dict = None):
    """批量AI回复升级队列中的真实联系人，自动生成私信并排队发送。

    只处理: 有真实 contact 名称、非占位符(user_XXX)、有 device_id 的条目。
    每个 (contact, device_id) 组合只发一次，避免重复。
    """
    body = body or {}
    max_process = body.get("max_process", 10)   # 每次最多处理条数，防止刷屏
    dry_run = body.get("dry_run", False)          # 干运行模式：只返回计划，不真正发送

    with _escalation_lock:
        all_items = list(reversed(_escalation_queue))

    # 若本地队列为空，聚合集群 Worker 数据
    if not all_items:
        try:
            import urllib.request as _ur, json as _jj
            from ..multi_host import get_cluster_coordinator
            coord = get_cluster_coordinator()
            for h in coord._hosts.values():
                if h.online and h.host_ip:
                    try:
                        url = f"http://{h.host_ip}:{h.port}/tiktok/escalation-queue"
                        resp = _ur.urlopen(_ur.Request(url), timeout=5)
                        data = _jj.loads(resp.read().decode())
                        all_items.extend(data.get("items", []))
                    except Exception:
                        pass
        except Exception:
            pass

    # 过滤出真实联系人
    def _is_real(item):
        c = (item.get("contact") or "").strip()
        return bool(c) and not c.startswith("user_") and len(c) > 2

    real_items = [x for x in all_items if _is_real(x) and x.get("device_id")]

    # 按 (contact, device_id) 去重，保留最新一条
    seen = {}
    for item in real_items:
        key = (item["contact"], item["device_id"])
        if key not in seen:
            seen[key] = item
    unique_items = list(seen.values())[:max_process]

    if not unique_items:
        return {"ok": True, "processed": 0, "skipped": len(all_items),
                "message": "没有可处理的真实联系人（均为占位符或缺少设备ID）"}

    # 获取LLM客户端
    try:
        from src.ai.llm_client import get_llm_client
        llm = get_llm_client()
        _llm_ok = True
    except Exception:
        _llm_ok = False

    _fallback_replies = [
        "Hey! Thanks for reaching out 😊 I'd love to chat more about this with you!",
        "Hi there! Great to connect with you. What can I help you with today?",
        "Thanks for your message! Let's talk more, I think we can help each other 🙌",
    ]

    from ..api import task_store, get_worker_pool, run_task, _config_path
    pool = get_worker_pool()
    results = []
    processed_contacts = set()

    for item in unique_items:
        contact = item["contact"]
        device_id = item["device_id"]
        message = item.get("message", "")
        intent = item.get("intent", "positive")
        key = (contact, device_id)

        # 生成回复
        reply = _fallback_replies[0]
        if _llm_ok and message:
            try:
                prompt = f"""你是TikTok私信销售专家。客户发来了消息，请用英语/意大利语（根据消息语言）生成一条自然友好的回复，目标是继续对话并了解需求。

客户消息: {message[:200]}
意向分类: {intent}

要求: 1条回复，30-80字，自然口语化，不含占位符，可直接发送。只输出回复内容本身。"""
                resp = llm.chat([{"role": "user", "content": prompt}])
                reply = resp.strip().split('\n')[0].strip()
                if len(reply) < 5:
                    reply = _fallback_replies[0]
            except Exception:
                reply = _fallback_replies[intent == 'positive' and 0 or 1]

        task_id = None
        if not dry_run:
            try:
                # 通过 HTTP /tasks 创建任务，自动路由到正确的 Worker 节点
                import urllib.request as _ur2, json as _jj2
                _payload = _jj2.dumps({
                    "type": "tiktok_send_dm",
                    "device_id": device_id,
                    "params": {"recipient": contact, "message": reply}
                }).encode()
                _req2 = _ur2.Request(
                    f"{local_api_base()}/tasks",
                    data=_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                _resp2 = _ur2.urlopen(_req2, timeout=10)
                _result2 = _jj2.loads(_resp2.read().decode())
                task_id = _result2.get("task_id", "ok")
                processed_contacts.add(key)
            except Exception as e:
                results.append({"contact": contact, "device_id": device_id[:8],
                                 "error": str(e)[:80], "reply": reply})
                continue

        results.append({
            "contact": contact,
            "device_id": device_id[:8],
            "intent": intent,
            "reply": reply[:100],
            "task_id": (task_id or "")[:8] if task_id else None,
        })

    # 从升级队列移除已处理的条目
    if not dry_run and processed_contacts:
        with _escalation_lock:
            _escalation_queue[:] = [
                x for x in _escalation_queue
                if (x.get("contact"), x.get("device_id")) not in processed_contacts
            ]

    return {
        "ok": True,
        "processed": len([r for r in results if r.get("task_id")]),
        "skipped": len(all_items) - len(results),
        "dry_run": dry_run,
        "items": results,
    }


@router.get("/conversations")
def get_active_conversations(limit: int = 50):
    """获取最近的对话记录（来自 LeadsStore interactions 表）。支持集群透传。"""
    try:
        from src.leads.store import get_leads_store
        store = get_leads_store()
        # Quick check: if no TikTok interactions locally, proxy to cluster worker
        _has_tiktok = any(
            store.get_interactions(l.get("id") or l.get("lead_id", 0), platform="tiktok", limit=1)
            for l in store.list_leads(limit=5)
            if l.get("id") or l.get("lead_id")
        )
        if not _has_tiktok:
            try:
                import urllib.request as _ur, json as _jj
                from ..multi_host import get_cluster_coordinator
                coord = get_cluster_coordinator()
                for h in coord._hosts.values():
                    if h.online and h.host_ip:
                        url = f"http://{h.host_ip}:{h.port}/tiktok/conversations?limit={limit}"
                        req = _ur.Request(url, method="GET")
                        resp = _ur.urlopen(req, timeout=8)
                        return _jj.loads(resp.read().decode())
            except Exception:
                pass
        # 获取所有 leads 然后取最近的 interactions
        all_leads = store.list_leads(limit=200)
        conversations = []
        for lead in all_leads:
            lead_id = lead.get("id") or lead.get("lead_id")
            if not lead_id:
                continue
            try:
                interactions = store.get_interactions(lead_id, platform="tiktok", limit=1)
                if interactions:
                    last = interactions[0]
                    _status = lead.get("status", "")
                    # Map CRM status → FSM state label
                    _fsm_state_map = {
                        "new": "NEW",
                        "contacted": "GREETING",
                        "responded": "QUALIFYING",
                        "negotiating": "NEGOTIATING",
                        "converted": "CONVERTED",
                        "lost": "REJECTED",
                        "dormant": "DORMANT",
                    }
                    _meta = last.get("metadata") or {}
                    if isinstance(_meta, str):
                        try:
                            import json as _jm
                            _meta = _jm.loads(_meta)
                        except Exception:
                            _meta = {}
                    # device_id: try metadata, then lead source_device, then last interaction
                    _device_id = (_meta.get("device_id") or
                                  lead.get("source_device") or
                                  last.get("device_id") or "")
                    conversations.append({
                        "lead_id": lead_id,
                        "contact": lead.get("username") or lead.get("name", ""),
                        "platform": "tiktok",
                        "last_message": last.get("content", "")[:200],
                        "direction": last.get("direction", ""),
                        "ts": last.get("created_at", "") or last.get("timestamp", ""),
                        "status": _status,
                        "fsm_state": _meta.get("conv_state") or _fsm_state_map.get(_status, _status.upper()),
                        "intent": _meta.get("intent") or last.get("intent", ""),
                        "score": lead.get("score", 0),
                        "device_id": _device_id,
                    })
            except Exception:
                continue
        # 按时间倒序
        conversations.sort(key=lambda x: x.get("ts", ""), reverse=True)
        return {"conversations": conversations[:limit], "total": len(conversations)}
    except Exception as e:
        return {"conversations": [], "total": 0, "error": str(e)}


@router.post("/start-daily-campaign")
def start_daily_campaign(body: dict = None):
    """一键启动今日运营计划: 养号 → 关注 → 收件箱 → 引流消息。

    按时间错开各设备任务，避免同时占用所有资源。
    """
    body = body or {}
    country = body.get("country", "italy")
    max_devices = body.get("max_devices", 17)

    from src.device_control.device_manager import get_device_manager
    manager = get_device_manager(DEFAULT_DEVICES_YAML)
    manager.discover_devices()
    device_ids = [d.device_id for d in manager.get_all_devices() if d.is_online]
    device_ids = device_ids[:max_devices]

    # 如果本地无设备，尝试通过集群 Worker 分发
    if not device_ids:
        try:
            from ..multi_host import get_cluster_coordinator
            coord = get_cluster_coordinator()
            cluster_devices = [d for d in coord.get_all_devices()
                               if d.get("status") in ("connected", "online", "busy")]
            if cluster_devices:
                import urllib.request as _ur, json as _json
                workers_used = set()
                cluster_created = []
                for cd in cluster_devices[:max_devices]:
                    worker_url = cd.get("worker_url", "")
                    if not worker_url:
                        # Construct from host_ip + host_port if available
                        _hip = cd.get("host_ip", "") or cd.get("host_ip", "")
                        _hport = cd.get("host_port", 8000)
                        if _hip:
                            worker_url = f"http://{_hip}:{_hport}"
                    if not worker_url:
                        continue
                    if worker_url in workers_used:
                        continue
                    workers_used.add(worker_url)
                    try:
                        payload = _json.dumps({"country": country, "max_devices": max_devices}).encode()
                        req = _ur.Request(f"{worker_url}/tiktok/start-daily-campaign",
                                          data=payload,
                                          headers={"Content-Type": "application/json"},
                                          method="POST")
                        resp = _ur.urlopen(req, timeout=15)
                        result = _json.loads(resp.read().decode())
                        cluster_created.append({"worker": worker_url, "result": result})
                    except Exception as e:
                        cluster_created.append({"worker": worker_url, "error": str(e)[:60]})
                if cluster_created:
                    return {"ok": True, "mode": "cluster", "dispatched_to": cluster_created}
        except Exception:
            pass
        return {"ok": False, "error": "没有在线设备"}

    from src.host.task_origin import with_origin
    from ..api import task_store, get_worker_pool, run_task
    pool = get_worker_pool()
    _cp = DEFAULT_DEVICES_YAML
    created = []
    import time as _time

    # Deduplication: collect already pending/running task types per device
    _existing = task_store.list_tasks(limit=200)
    _active_types: dict = {}  # device_id -> set of task types
    for _t in _existing:
        _st = _t.get("status", "")
        if _st in ("pending", "running"):
            _did = _t.get("device_id", "")
            _active_types.setdefault(_did, set()).add(_t.get("type") or _t.get("task_type", ""))

    for i, did in enumerate(device_ids):
        _busy = _active_types.get(did, set())
        try:
            dev_created = []
            # 1. 养号
            if "tiktok_warmup" not in _busy:
                t1 = task_store.create_task(
                    task_type="tiktok_warmup",
                    device_id=did,
                    params=with_origin(
                        {"duration_minutes": 10, "target_country": country, "phase": "auto"},
                        "tiktok_daily_campaign",
                    ),
                )
                pool.submit(t1, did, run_task, t1, _cp)
                dev_created.append(t1)
            # 2. 关注
            if "tiktok_follow" not in _busy:
                t2 = task_store.create_task(
                    task_type="tiktok_follow",
                    device_id=did,
                    params=with_origin(
                        {"keyword": country, "count": 20, "target_country": country},
                        "tiktok_daily_campaign",
                    ),
                )
                pool.submit(t2, did, run_task, t2, _cp)
                dev_created.append(t2)
            # 3. 收件箱 + 自动回复
            if "tiktok_check_inbox" not in _busy:
                t3 = task_store.create_task(
                    task_type="tiktok_check_inbox",
                    device_id=did,
                    params=with_origin(
                        {"auto_reply": True, "max_conversations": 20},
                        "tiktok_daily_campaign",
                    ),
                )
                pool.submit(t3, did, run_task, t3, _cp)
                dev_created.append(t3)
            # 4. 回关发私信
            if "tiktok_chat" not in _busy:
                t4 = task_store.create_task(
                    task_type="tiktok_chat",
                    device_id=did,
                    params=with_origin({"max_chats": 5}, "tiktok_daily_campaign"),
                )
                pool.submit(t4, did, run_task, t4, _cp)
                dev_created.append(t4)
            created.append({"device_id": did[:8], "tasks": dev_created, "skipped": len(_busy)})
        except Exception as e:
            created.append({"device_id": did[:8], "error": str(e)[:60]})

    try:
        from ..api import _audit
        _audit("daily_campaign", "all", f"country={country} devices={len(device_ids)}")
    except Exception:
        pass

    total_created = sum(len(c.get("tasks", [])) for c in created if "tasks" in c)
    return {
        "ok": True,
        "devices": len(device_ids),
        "total_tasks": total_created,
        "country": country,
        "created": created,
    }


# ── 消息话术模板管理 ──────────────────────────────────────────────────────────────

def _load_chat_config() -> dict:
    import yaml
    cfg = config_file("chat_messages.yaml")
    if cfg.exists():
        with open(cfg, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _save_chat_config(data: dict):
    import yaml
    cfg = config_file("chat_messages.yaml")
    cfg.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False)


@router.get("/messages")
def get_message_templates():
    """获取所有消息话术模板。"""
    data = _load_chat_config()
    return {
        "greeting_messages": data.get("greeting_messages", []),
        "referral_telegram": data.get("referral_telegram", []),
        "referral_whatsapp": data.get("referral_whatsapp", []),
        "messages": data.get("messages", []),
        "device_referrals": data.get("device_referrals", {}),
        "country": data.get("country", "italy"),
    }


@router.put("/messages")
def update_message_templates(body: dict):
    """更新消息话术模板（支持部分更新）。"""
    data = _load_chat_config()
    updatable = ["greeting_messages", "referral_telegram", "referral_whatsapp",
                 "messages", "country"]
    for key in updatable:
        if key in body:
            data[key] = body[key]
    _save_chat_config(data)
    return {"ok": True, "updated": [k for k in updatable if k in body]}


@router.post("/messages/preview")
def preview_message(body: dict):
    """预览消息渲染结果 (替换占位符 {name}/{telegram}/{whatsapp})。"""
    template = body.get("template", "")
    device_id = body.get("device_id", "")
    name = body.get("name", "TestUser")

    data = _load_chat_config()
    refs = data.get("device_referrals", {}).get(device_id, {})
    telegram = refs.get("telegram", "@yourhandle")
    whatsapp = refs.get("whatsapp", "+1234567890")

    rendered = template.replace("{name}", name).replace(
        "{telegram}", telegram).replace("{whatsapp}", whatsapp)
    return {"rendered": rendered, "telegram": telegram, "whatsapp": whatsapp}


@router.get("/messages/ab-stats")
def get_ab_stats():
    """Get A/B test statistics for message variants."""
    try:
        from src.host.ab_stats import get_stats
        import yaml
        chat_path = config_file("chat_messages.yaml")
        variants = []
        if chat_path.exists():
            data = yaml.safe_load(chat_path.read_text(encoding="utf-8"))
            variants = data.get("message_variants", [])
        from src.host.ab_stats import get_stats, get_adaptive_weight
        stats = get_stats()
        result = []
        for v in variants:
            vid = v.get("id", "")
            s = stats.get(vid, {"sent": 0, "replied": 0, "reply_rate": 0.0, "last_sent": ""})
            base_w = float(v.get("weight", 1))
            eff_w = get_adaptive_weight(vid, base_w)
            result.append({
                "id": vid,
                "name": v.get("name", vid),
                "description": v.get("description", ""),
                "weight": base_w,
                "effective_weight": round(eff_w, 2),   # P9-D: 前端进度条用
                "adapted": s.get("sent", 0) >= 30,
                "sent": s["sent"],
                "replied": s["replied"],
                "reply_rate": s["reply_rate"],
                "last_sent": s["last_sent"],
            })
        # Add untracked default variant
        if not any(v["id"] == "default" for v in result):
            s = stats.get("default", {"sent": 0, "replied": 0, "reply_rate": 0.0, "last_sent": ""})
            if s["sent"] > 0:
                result.insert(0, {"id": "default", "name": "默认话术", "description": "原始 messages 列表", "weight": 0, **s})
        return {"variants": result, "total_variants": len(variants)}
    except Exception as e:
        return {"variants": [], "error": str(e)}


@router.get("/warmup-progress")
def tiktok_warmup_progress_proxy():
    """代理到 /platforms/tiktok/warmup-progress（支持集群透传）。"""
    from .platforms import tiktok_warmup_progress
    return tiktok_warmup_progress()


@router.get("/auto-monitor")
def get_auto_monitor():
    """获取自动收件箱监控状态。"""
    try:
        from src.host.task_policy import policy_blocks_auto_tiktok_check_inbox
        if policy_blocks_auto_tiktok_check_inbox():
            return {"enabled": False, "interval_minutes": 10, "blocked_by_policy": True}
    except Exception:
        pass
    from src.host.job_scheduler import load_scheduled_jobs
    jobs = load_scheduled_jobs()
    job = next((j for j in jobs if j.get("id") == "auto_monitor_inbox"), None)
    if job and job.get("enabled"):
        cron = job.get("cron", "*/10 * * * *")
        # parse interval from cron like */10 * * * *
        try:
            interval = int(cron.split()[0].replace("*/", ""))
        except Exception:
            interval = 10
        return {"enabled": True, "interval_minutes": interval, "job": job}
    return {"enabled": False, "interval_minutes": 10}


@router.post("/auto-monitor")
def set_auto_monitor(body: dict):
    """开启/关闭自动收件箱监控。

    Body: {"enabled": true, "interval_minutes": 10}
    """
    from src.host.job_scheduler import load_scheduled_jobs, save_scheduled_jobs
    enabled = bool(body.get("enabled", False))
    try:
        from src.host.task_policy import policy_blocks_auto_tiktok_check_inbox
        if policy_blocks_auto_tiktok_check_inbox() and enabled:
            jobs = load_scheduled_jobs()
            jobs = [j for j in jobs if j.get("id") != "auto_monitor_inbox"]
            save_scheduled_jobs(jobs)
            return {
                "ok": False,
                "enabled": False,
                "interval_minutes": int(body.get("interval_minutes", 10)),
                "error": "策略已禁止自动查收件箱（config/task_execution_policy.yaml disable_auto_tiktok_check_inbox）",
            }
    except Exception:
        pass
    interval = int(body.get("interval_minutes", 10))
    interval = max(1, min(60, interval))

    jobs = load_scheduled_jobs()
    # Remove existing auto-monitor job
    jobs = [j for j in jobs if j.get("id") != "auto_monitor_inbox"]

    if enabled:
        jobs.append({
            "id": "auto_monitor_inbox",
            "name": "TikTok 自动收件箱检查",
            "cron": f"*/{interval} * * * *",
            "action": "tiktok_check_inbox",
            "params": {"auto_reply": True, "max_conversations": 20},
            "enabled": True,
        })

    save_scheduled_jobs(jobs)
    return {"ok": True, "enabled": enabled, "interval_minutes": interval}


@router.get("/messages/stats")
def message_stats():
    """获取话术使用统计（从任务结果中统计）。"""
    from ..api import task_store as _ts
    import json as _json
    tasks = _ts.list_tasks(limit=500)
    total_sent = 0
    total_replied = 0
    for t in tasks:
        if t.get("status") != "completed":
            continue
        r = t.get("result") or {}
        if isinstance(r, str):
            try:
                r = _json.loads(r)
            except Exception:
                continue
        chat = r.get("chat_result", {})
        inbox = r.get("inbox_result", {})
        total_sent += chat.get("messaged", 0)
        total_replied += inbox.get("auto_replied", 0)
    return {"total_sent": total_sent, "total_auto_replied": total_replied}


# ──────────────────────────────────────────────────────────────────────────────
# 账号养号 / 互相关注互动
# ──────────────────────────────────────────────────────────────────────────────

def _get_tiktok_ds():
    from src.host.device_state import get_device_state_store
    return get_device_state_store("tiktok")


def _get_online_devices():
    from src.device_control.device_manager import get_device_manager
    mgr = get_device_manager(DEFAULT_DEVICES_YAML)
    return [d.device_id for d in mgr.get_all_devices() if d.is_online]


@router.get("/account-health")
def get_account_health():
    """获取所有在线设备的账号健康数据（phase/stats/算法分/健康评分）。
    当本地无设备时，自动代理到 Worker-03。"""
    ds = _get_tiktok_ds()
    devices = _get_online_devices()

    # 本地无设备 → 代理到 Worker-03
    if not devices:
        try:
            import urllib.request as _ur, json as _jj
            _resp = _ur.urlopen(
                _ur.Request("http://192.168.0.103:8000/tiktok/account-health"),
                timeout=6
            )
            _data = _jj.loads(_resp.read())
            _data["_source"] = "worker03"
            return _data
        except Exception as _e:
            return {"health": {}, "total": 0, "_error": str(_e)}

    result = {}
    for did in devices:
        summary = ds.get_device_summary(did)
        username = ds.get(did, "tiktok_username") or ""
        # 健康评分公式: 观看数*0.1 + 关注数*2 + DM数*3 + 算法分*30，上限100
        watched = summary.get("total_watched", 0)
        followed = summary.get("total_followed", 0)
        dms = summary.get("total_dms_sent", 0)
        algo = summary.get("algorithm_score", 0.0)
        day = summary.get("day", 0)
        health = min(100, round(
            watched * 0.1 + followed * 2 + dms * 3 + algo * 30 + min(day, 14) * 1
        ))
        result[did] = {
            **summary,
            "username": username,
            "health_score": health,
        }
    return {"health": result, "total": len(devices)}


@router.get("/account-usernames")
def get_account_usernames():
    """获取所有在线设备的 TikTok 用户名（来自 device_state）。"""
    ds = _get_tiktok_ds()
    devices = _get_online_devices()
    result = {}
    for did in devices:
        username = ds.get(did, "tiktok_username") or ""
        result[did] = username
    return {"usernames": result, "total": len(devices)}


@router.put("/account-username")
def set_account_username(body: dict):
    """手动设置设备的 TikTok 用户名。Body: {device_id, username}"""
    device_id = body.get("device_id", "")
    username = body.get("username", "").strip()
    if not device_id or not username:
        raise HTTPException(400, "device_id 和 username 必填")
    if not username.startswith("@"):
        username = "@" + username
    ds = _get_tiktok_ds()
    ds.set(device_id, "tiktok_username", username)
    return {"ok": True, "device_id": device_id, "username": username}


@router.post("/scan-all-usernames")
def scan_all_usernames():
    """为所有在线设备启动 tiktok_scan_username 任务（截图+Vision AI 扫描用户名）。"""
    from src.host.api import task_store, get_worker_pool, run_task
    from src.host.task_origin import with_origin
    cfg = DEFAULT_DEVICES_YAML
    pool = get_worker_pool()
    devices = _get_online_devices()
    created = []
    for did in devices:
        tid = task_store.create_task(
            task_type="tiktok_scan_username",
            device_id=did,
            params=with_origin({}, "tiktok_scan_all"),
        )
        pool.submit(tid, did, run_task, tid, cfg)
        created.append({"task_id": tid, "device_id": did})
    return {"created": len(created), "tasks": created}


@router.post("/cross-follow-all")
def cross_follow_all(body: dict = {}):
    """让所有在线设备互相关注。每台设备关注其他所有设备的账号。"""
    from src.host.api import task_store, get_worker_pool, run_task
    from src.host.task_origin import with_origin
    cfg = DEFAULT_DEVICES_YAML
    pool = get_worker_pool()
    ds = _get_tiktok_ds()
    devices = _get_online_devices()

    # 收集已知用户名
    known = {}
    for did in devices:
        uname = ds.get(did, "tiktok_username") or ""
        if uname:
            known[did] = uname

    if len(known) < 2:
        return {"ok": False, "error": "需要至少2台设备配置了 TikTok 用户名，请先运行「扫描用户名」"}

    created = []
    for did in devices:
        for target_did, target_uname in known.items():
            if target_did == did:
                continue
            tid = task_store.create_task(
                task_type="tiktok_follow_user",
                device_id=did,
                params=with_origin(
                    {"target_username": target_uname},
                    "tiktok_cross_follow",
                ),
            )
            pool.submit(tid, did, run_task, tid, cfg)
            created.append({"task_id": tid, "device_id": did, "target": target_uname})
    return {"created": len(created), "tasks": created}


@router.post("/cross-interact-all")
def cross_interact_all(body: dict = {}):
    """让所有在线设备互相互动（观看视频+点赞，可选评论）。"""
    from src.host.api import task_store, get_worker_pool, run_task
    from src.host.task_origin import with_origin
    cfg = DEFAULT_DEVICES_YAML
    pool = get_worker_pool()
    ds = _get_tiktok_ds()
    devices = _get_online_devices()
    watch_seconds = int(body.get("watch_seconds", 15))
    do_like = bool(body.get("do_like", True))
    do_comment = bool(body.get("do_comment", False))

    # 收集已知用户名
    known = {}
    for did in devices:
        uname = ds.get(did, "tiktok_username") or ""
        if uname:
            known[did] = uname

    if len(known) < 2:
        return {"ok": False, "error": "需要至少2台设备配置了 TikTok 用户名，请先运行「扫描用户名」"}

    import time as _time
    _cooldown_secs = int(body.get("cooldown_hours", 4)) * 3600  # 默认4小时冷却
    created = []
    skipped = []
    for did in devices:
        for target_did, target_uname in known.items():
            if target_did == did:
                continue
            # 去重检查：4小时内已互动则跳过
            _key = f"last_interact_{target_uname.lstrip('@').lower()}"
            _last_ts = ds.get(did, _key)
            if _last_ts:
                try:
                    if _time.time() - int(_last_ts) < _cooldown_secs:
                        skipped.append({"device_id": did, "target": target_uname, "reason": "cooldown"})
                        continue
                except Exception:
                    pass
            tid = task_store.create_task(
                task_type="tiktok_interact_user",
                device_id=did,
                params=with_origin(
                    {
                        "target_username": target_uname,
                        "watch_seconds": watch_seconds,
                        "do_like": do_like,
                        "do_comment": do_comment,
                    },
                    "tiktok_cross_interact",
                ),
            )
            pool.submit(tid, did, run_task, tid, cfg)
            created.append({"task_id": tid, "device_id": did, "target": target_uname})
    return {"created": len(created), "skipped": len(skipped), "tasks": created}


@router.post("/leads/merge-duplicates")
def merge_duplicate_leads(body: dict = {}):
    """扫描并合并跨设备重复的线索（同平台、同用户名 → 保留最优，归并互动记录）。

    逻辑：
      1. 扫描 platform_profiles 表，找同 (platform, normalized_username) 的多条记录
      2. 保留 score 最高（或 created_at 最早）的作为主线索
      3. 将次线索的所有 interactions 重新归属到主线索
      4. 删除次线索（含其 platform_profiles）
    返回: {merged_groups, total_removed, leads_merged}
    """
    import sqlite3 as _sq3, re as _re, json as _sj

    _dry_run = bool(body.get("dry_run", False))  # dry_run=true 只统计不修改
    _platform = body.get("platform", "tiktok")
    _ldb = str(data_file("leads.db"))

    def _norm(s):
        s = _re.sub(r"^@+", "", (s or "").lower().strip())
        return _re.sub(r"\s+", "", s)

    conn = _sq3.connect(_ldb, timeout=10)
    try:
        # 1. 找所有重复组（同 platform + 规范化 username）
        rows = conn.execute(
            "SELECT lead_id, username FROM platform_profiles WHERE platform = ? AND username != ''",
            (_platform,)
        ).fetchall()

        # 分组：norm_username → [lead_id, ...]
        groups: dict = {}
        for lid, uname in rows:
            key = _norm(uname)
            if not key:
                continue
            groups.setdefault(key, []).append(lid)

        dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
        if not dup_groups:
            return {"ok": True, "merged_groups": 0, "total_removed": 0, "leads_merged": []}

        merged_groups = 0
        total_removed = 0
        leads_merged = []

        for uname_key, lid_list in dup_groups.items():
            # 2. 选主线索（score 最高，相同则 id 最小/最老）
            scores = {}
            for lid in lid_list:
                r = conn.execute("SELECT score, created_at FROM leads WHERE id = ?", (lid,)).fetchone()
                scores[lid] = (float(r[0] or 0) if r else 0, -(lid))  # (score, -id) 越大越优先
            primary = max(lid_list, key=lambda x: scores[x])
            secondaries = [x for x in lid_list if x != primary]

            if _dry_run:
                leads_merged.append({
                    "primary": primary, "merged": secondaries, "username": uname_key
                })
                continue

            # 3. 将次线索的 interactions 归属到主线索
            for sec in secondaries:
                conn.execute(
                    "UPDATE interactions SET lead_id = ? WHERE lead_id = ?", (primary, sec)
                )
            # 4. 删除次线索（CASCADE 会同时删除 platform_profiles）
            for sec in secondaries:
                conn.execute("DELETE FROM leads WHERE id = ?", (sec,))

            # 5. 更新主线索 score（重新计算互动分）
            _int_count = conn.execute(
                "SELECT COUNT(*) FROM interactions WHERE lead_id = ?", (primary,)
            ).fetchone()[0]
            _new_score = min(float(_int_count) * 5, 100)  # 简单：每条互动 5 分，上限 100
            conn.execute("UPDATE leads SET score = MAX(score, ?) WHERE id = ?", (_new_score, primary))

            merged_groups += 1
            total_removed += len(secondaries)
            leads_merged.append({
                "primary": primary, "merged": secondaries, "username": uname_key
            })

        if not _dry_run:
            conn.commit()

        _result = {
            "ok": True,
            "dry_run": _dry_run,
            "merged_groups": merged_groups if not _dry_run else len(dup_groups),
            "total_removed": total_removed,
            "leads_merged": leads_merged[:50],
        }

        # 推 SSE 事件，让前端实时感知去重结果
        if not _dry_run and total_removed > 0:
            try:
                from src.host.event_stream import push_event as _pe
                _pe("leads.merged", {
                    "merged_groups": merged_groups,
                    "total_removed": total_removed,
                    "platform": _platform,
                }, "")
            except Exception:
                pass

        return _result
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"合并失败: {e}")
    finally:
        conn.close()


@router.post("/leads/ai-rescore")
def ai_rescore_leads(body: dict = {}):
    """用 GLM-4-flash AI 对 OPTIONAL/未标记意图的线索重新评分，提升转化精度。

    逻辑：
      1. 从 interactions 表查出 intent=OPTIONAL 或空、且有入站消息的记录
      2. 逐条调用 GLM-4-flash，判定 NEEDS_REPLY / NO_REPLY / OPTIONAL
      3. 更新 interactions.intent 字段
      4. 推送 SSE 事件 leads.rescored 通知前端刷新

    参数：limit(默认30)、platform(默认tiktok)、dry_run(默认false)
    """
    import sqlite3 as _sq3

    _limit = min(int(body.get("limit", 30)), 100)
    _dry_run = bool(body.get("dry_run", False))
    _ldb = str(data_file("leads.db"))

    import json as _rj
    conn = _sq3.connect(_ldb, timeout=10)
    try:
        # 查询有入站消息的最近互动（intent 存储在 metadata JSON 中）
        # leads.name 作为展示名，username 从 platform_profiles 获取
        rows = conn.execute(
            """SELECT i.id, i.lead_id, i.content, i.metadata,
                      COALESCE(pp.username, l.name, '') as display_name
               FROM interactions i
               JOIN leads l ON l.id = i.lead_id
               LEFT JOIN platform_profiles pp ON pp.lead_id = i.lead_id AND pp.platform = 'tiktok'
               WHERE i.direction = 'inbound'
                 AND i.content IS NOT NULL AND trim(i.content) != ''
               ORDER BY i.created_at DESC
               LIMIT ?""",
            (_limit * 3,)  # 多查一些，过滤后取 OPTIONAL
        ).fetchall()

        # 过滤：只处理 OPTIONAL 或未标记的
        filtered = []
        for row_id, lead_id, content, meta_str, username in rows:
            try:
                meta = _rj.loads(meta_str or "{}")
            except Exception:
                meta = {}
            old_intent = meta.get("intent", "")
            if old_intent in ("NEEDS_REPLY", "NO_REPLY"):
                continue  # 已有明确结论，跳过
            filtered.append((row_id, lead_id, content, meta, old_intent, username))
            if len(filtered) >= _limit:
                break

        if not filtered:
            return {"ok": True, "rescored": 0, "needs_reply": 0, "no_reply": 0,
                    "skipped": 0, "dry_run": _dry_run}

        from src.ai.llm_client import get_llm_client
        client = get_llm_client()

        _system = (
            "你是意大利市场营销分析AI。判断TikTok用户私信的意图，"
            "只回复以下三个词之一，不要任何解释或标点：\n"
            "NEEDS_REPLY — 用户有兴趣、询问信息、想要联系\n"
            "NO_REPLY — 用户明确拒绝、要求停止或负面态度\n"
            "OPTIONAL — 中性互动，无明确需求"
        )

        rescored = 0
        needs_reply = 0
        no_reply = 0
        skipped = 0
        changed_leads = []

        for row_id, lead_id, content, meta, old_intent, username in filtered:
            try:
                _user_p = (
                    f"用户名：{username or '未知'}\n"
                    f"私信内容：「{(content or '')[:300]}」\n"
                    f"判断意图："
                )
                raw = client.chat_with_system(
                    _system, _user_p, temperature=0.05, max_tokens=20, use_cache=False
                )
                raw = (raw or "").strip().upper()

                if "NEEDS_REPLY" in raw:
                    new_intent = "NEEDS_REPLY"
                    needs_reply += 1
                elif "NO_REPLY" in raw:
                    new_intent = "NO_REPLY"
                    no_reply += 1
                else:
                    new_intent = "OPTIONAL"
                    skipped += 1

                if not _dry_run and new_intent != old_intent:
                    # 更新 metadata.intent 字段（保留其他 metadata）
                    meta["intent"] = new_intent
                    conn.execute(
                        "UPDATE interactions SET metadata = ? WHERE id = ?",
                        (_rj.dumps(meta, ensure_ascii=False), row_id)
                    )
                    if new_intent == "NEEDS_REPLY":
                        changed_leads.append({"lead_id": lead_id, "intent": new_intent,
                                              "username": username or ""})
                rescored += 1
            except Exception:
                skipped += 1

        if not _dry_run:
            conn.commit()

        # 推 SSE 事件通知前端刷新线索面板
        if not _dry_run and (needs_reply > 0 or no_reply > 0):
            try:
                from src.host.event_stream import push_event as _pe
                _pe("leads.rescored", {
                    "rescored": rescored,
                    "needs_reply": needs_reply,
                    "no_reply": no_reply,
                    "changed_leads": changed_leads[:20],
                }, "")
            except Exception:
                pass

        import logging as _log
        _log.getLogger(__name__).info(
            "[AI重评] 处理 %d 条，NEEDS_REPLY=%d NO_REPLY=%d OPTIONAL=%d",
            rescored, needs_reply, no_reply, skipped
        )
        return {
            "ok": True,
            "rescored": rescored,
            "needs_reply": needs_reply,
            "no_reply": no_reply,
            "skipped": skipped,
            "dry_run": _dry_run,
        }
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"AI重评失败: {e}")
    finally:
        conn.close()


def run_ai_rescore_leads_scheduled(body: dict = None) -> dict:
    """
    与 POST /tiktok/leads/ai-rescore 同源逻辑，供 job_scheduler 进程内调用。
    仅跑线索库 AI 重评，不创建按设备任务（避免与 tiktok_* 批量分支冲突）。
    """
    try:
        return ai_rescore_leads(body or {})
    except HTTPException as e:
        return {"ok": False, "error": str(e.detail), "status_code": getattr(e, "status_code", 500)}


# ═══════════════════════════════════════════════════════════════════════════
# ChatBrain API — AI 对话系统
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/chat/conversation/{lead_id}")
async def get_chat_conversation(lead_id: str, limit: int = 50):
    """获取某个线索的完整对话历史"""
    try:
        from src.ai.chat_bridge import get_lead_conversation, get_lead_stats, get_lead_profile
        return {
            "lead_id": lead_id,
            "messages": get_lead_conversation(lead_id, limit),
            "stats": get_lead_stats(lead_id),
            "profile": get_lead_profile(lead_id),
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/chat/active")
async def get_active_conversations(limit: int = 30):
    """获取最近活跃的对话列表（含质量评分和阶段统计）"""
    try:
        from src.ai.conversation_memory import ConversationMemory
        import sqlite3

        mem = ConversationMemory.get_instance()
        leads = mem.list_leads(limit)

        # 从 conversations.db 获取更详细的统计
        db_path = str(data_file("conversations.db"))
        enriched = []
        stage_dist = {}
        total_msgs = 0
        total_user_msgs = 0
        total_bot_msgs = 0
        replied_count = 0

        try:
            conn = sqlite3.connect(db_path, timeout=5)
            for lead in leads:
                lid = lead.get("lead_id", "")
                rows = conn.execute(
                    "SELECT role, content, timestamp FROM conversations "
                    "WHERE lead_id=? ORDER BY timestamp DESC LIMIT 100",
                    (lid,)
                ).fetchall()

                user_msgs = sum(1 for r in rows if r[0] == "user")
                bot_msgs = sum(1 for r in rows if r[0] in ("assistant", "bot"))
                msg_count = len(rows)
                total_msgs += msg_count
                total_user_msgs += user_msgs
                total_bot_msgs += bot_msgs

                # 回复率判断
                has_reply = user_msgs > 0
                if has_reply:
                    replied_count += 1

                # 推断对话阶段
                stage = "icebreak"
                if msg_count >= 8:
                    stage = "referral"
                elif msg_count >= 5:
                    stage = "soft_pitch"
                elif msg_count >= 3:
                    stage = "rapport"
                stage_dist[stage] = stage_dist.get(stage, 0) + 1

                # 对话质量评分 (0-100)
                quality = min(100, int(
                    (min(user_msgs, 5) * 12) +
                    (min(msg_count, 10) * 3) +
                    (20 if has_reply else 0) +
                    (10 if msg_count >= 5 else 0)
                ))

                last_msg_preview = ""
                if rows:
                    last_msg_preview = (rows[0][1] or "")[:50]

                enriched.append({
                    **lead,
                    "user_messages": user_msgs,
                    "bot_messages": bot_msgs,
                    "has_reply": has_reply,
                    "stage": stage,
                    "quality_score": quality,
                    "last_preview": last_msg_preview,
                })
            conn.close()
        except Exception:
            enriched = leads

        # 全局 KPI
        total_convs = len(enriched)
        reply_rate = round(replied_count / total_convs * 100) if total_convs else 0
        avg_quality = round(sum(c.get("quality_score", 0) for c in enriched) / total_convs) if total_convs else 0
        avg_rounds = round(total_msgs / total_convs, 1) if total_convs else 0

        return {
            "conversations": enriched,
            "total": total_convs,
            "kpi": {
                "reply_rate": reply_rate,
                "avg_quality": avg_quality,
                "avg_rounds": avg_rounds,
                "total_messages": total_msgs,
                "user_messages": total_user_msgs,
                "bot_messages": total_bot_msgs,
                "replied_count": replied_count,
            },
            "stage_distribution": stage_dist,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/chat/analyze-profile")
async def analyze_user_profile(data: dict = Body(...)):
    """分析用户画像"""
    try:
        from src.ai.profile_analyzer import ProfileAnalyzer
        analyzer = ProfileAnalyzer.get_instance()
        use_llm = data.get("use_llm", False)
        if use_llm:
            profile = analyzer.analyze_with_llm(
                username=data.get("username", ""),
                bio=data.get("bio", ""),
                follower_count=data.get("follower_count", 0),
                source=data.get("source", "follow"),
            )
        else:
            profile = analyzer.analyze_text(
                username=data.get("username", ""),
                bio=data.get("bio", ""),
                follower_count=data.get("follower_count", 0),
                source=data.get("source", "follow"),
            )
        return {
            "username": profile.username,
            "industry": profile.industry,
            "interests": profile.interests,
            "personality": profile.personality,
            "account_type": profile.account_type,
            "language_style": profile.language_style,
            "icebreaker_topics": profile.icebreaker_topics,
            "referral_angle": profile.referral_angle,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


AB_GREET_EXPERIMENT = "tiktok_greet_opening"
AB_GREET_VARIANTS = ["warm_question", "compliment_light", "casual_short"]
AB_GREET_HINTS = {
    "warm_question": (
        "开场要温暖、自然，带一个容易回答的开放式问题；避免销售感。"
    ),
    "compliment_light": (
        "开场先具体、轻量地称赞对方简介或内容中的某一点，再自然接一句，不要夸张。"
    ),
    "casual_short": (
        "开场要非常短、随意，像朋友随手发的一句，避免正式或模板句。"
    ),
}


def _ensure_greet_ab_experiment():
    from src.host.ab_testing import get_ab_store
    get_ab_store().create(
        AB_GREET_EXPERIMENT, "message", variants=AB_GREET_VARIANTS,
    )


@router.post("/chat/preview-greet")
async def preview_greet_message(data: dict = Body(...)):
    """预览 AI 生成的打招呼消息（不发送，仅生成）；集成 A/B 变体分配。"""
    try:
        username = data.get("username", "")
        bio = data.get("bio", "")
        language = data.get("language", "")
        contact_info = data.get("contact_info", "")
        source = data.get("source", "contact")
        device_id = data.get("device_id", "")

        if not username:
            raise HTTPException(400, "username required")

        _ensure_greet_ab_experiment()
        from src.host.ab_testing import get_ab_store
        ab = get_ab_store()
        variant = ab.assign(
            AB_GREET_EXPERIMENT,
            device_id=device_id or "",
            user_id=username,
        )
        hint = AB_GREET_HINTS.get(variant, AB_GREET_HINTS["warm_question"])

        try:
            from src.ai.chat_brain import ChatBrain, UserProfile
            brain = ChatBrain.get_instance()
            prof = UserProfile(username=username, bio=bio, source=source)
            res = brain.generate_icebreaker(
                username,
                profile=prof,
                platform="tiktok",
                target_language=language or "",
                source=source,
                ab_style_hint=hint,
                ab_variant=variant,
                persist=False,
            )
            if res.message:
                return {
                    "message": res.message,
                    "engine": "chat_brain",
                    "username": username,
                    "variant": variant,
                    "experiment": AB_GREET_EXPERIMENT,
                    "ab_style_hint": hint,
                }
        except Exception:
            pass

        from src.ai.chat_bridge import generate_followback_message
        msg = generate_followback_message(
            username, bio, target_language=language, contact_info=contact_info,
        )
        return {
            "message": msg or "",
            "engine": "chat_bridge",
            "username": username,
            "variant": variant,
            "experiment": AB_GREET_EXPERIMENT,
            "ab_style_hint": hint,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/chat/generate")
async def generate_chat_message(data: dict = Body(...)):
    """手动触发 AI 生成消息；破冰支持用户编辑后的正文；可选记录 A/B sent。"""
    try:
        from src.ai.chat_bridge import generate_inbox_reply, generate_followback_message
        msg_type = data.get("type", "reply")
        username = data.get("username", "")
        message = data.get("message", "")
        bio = data.get("bio", "")
        language = data.get("language", "")
        contact_info = data.get("contact_info", "")
        device_id = data.get("device_id", "")
        variant = data.get("variant", "")
        experiment = data.get("experiment", "")
        record_ab = data.get("record_ab", True)

        custom = (message or "").strip()

        if msg_type == "icebreaker" and custom:
            result = custom
        elif msg_type == "icebreaker":
            result = generate_followback_message(
                username, bio, target_language=language, contact_info=contact_info,
            )
        else:
            result = generate_inbox_reply(
                username, message, bio=bio,
                target_language=language, contact_info=contact_info,
            )

        if record_ab and msg_type == "icebreaker" and variant and experiment:
            try:
                from src.host.ab_testing import get_ab_store
                get_ab_store().record(
                    experiment,
                    variant,
                    "sent",
                    device_id=device_id or "",
                    metadata={
                        "username": username,
                        "custom_text": bool(custom),
                    },
                )
            except Exception:
                pass

        return {
            "message": result,
            "username": username,
            "type": msg_type,
            "variant": variant,
            "experiment": experiment,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/device/{device_id}/find-contact-friends")
async def find_contact_friends_endpoint(device_id: str, data: dict = Body({})):
    """在 TikTok 中查找通讯录好友并自动关注/发消息"""
    try:
        from src.app_automation.contacts_manager import tiktok_find_contact_friends
        from src.app_automation.tiktok import TikTokAutomation
        import asyncio

        max_friends = data.get("max_friends", 20)
        auto_follow = data.get("auto_follow", True)
        auto_message = data.get("auto_message", True)
        language = data.get("language", "")
        contact_info = data.get("contact_info", "")

        tiktok = TikTokAutomation()
        result = await asyncio.to_thread(
            tiktok_find_contact_friends,
            tiktok, device_id,
            max_friends=max_friends,
            auto_follow=auto_follow,
            auto_message=auto_message,
            target_language=language,
            contact_info=contact_info,
        )
        try:
            from ..event_stream import push_event
            push_event("task.completed", {
                "action": "contact_discovery",
                "found": result.get("found", 0),
                "followed": result.get("followed", 0),
            }, device_id=device_id)
        except Exception:
            pass
        return {"ok": True, "device_id": device_id, **result}
    except Exception as e:
        raise HTTPException(500, str(e))


# ════════════════════════════════════════════════════════
# AI 对话策略效果分析
# ════════════════════════════════════════════════════════

@router.get("/chat/strategy-analysis")
async def chat_strategy_analysis():
    """分析各阶段对话策略的实际效果，生成优化建议"""
    import sqlite3

    db_path = str(data_file("conversations.db"))
    result = {"stages": {}, "insights": [], "top_openers": [], "reply_patterns": []}

    try:
        conn = sqlite3.connect(db_path, timeout=5)

        # 获取所有对话的详细统计
        leads = conn.execute(
            "SELECT lead_id, COUNT(*) as total, "
            "SUM(CASE WHEN role='user' THEN 1 ELSE 0 END) as user_msgs, "
            "SUM(CASE WHEN role IN ('assistant','bot') THEN 1 ELSE 0 END) as bot_msgs, "
            "MIN(timestamp) as first_ts, MAX(timestamp) as last_ts "
            "FROM conversations GROUP BY lead_id"
        ).fetchall()

        total_convs = len(leads)
        replied_convs = sum(1 for l in leads if l[2] > 0)
        deep_convs = sum(1 for l in leads if l[1] >= 5)

        # 分析开场白效果（第一条 bot 消息）
        opener_stats = {}
        for lead in leads:
            lid = lead[0]
            first_bot = conn.execute(
                "SELECT content FROM conversations "
                "WHERE lead_id=? AND role IN ('assistant','bot') "
                "ORDER BY timestamp ASC LIMIT 1", (lid,)
            ).fetchone()
            if not first_bot or not first_bot[0]:
                continue
            opener = first_bot[0][:60].strip()
            has_reply = lead[2] > 0

            if opener not in opener_stats:
                opener_stats[opener] = {"sent": 0, "replied": 0, "text": first_bot[0][:100]}
            opener_stats[opener]["sent"] += 1
            if has_reply:
                opener_stats[opener]["replied"] += 1

        # 排序：发送量 >= 2 且回复率最高
        top_openers = []
        for k, v in opener_stats.items():
            if v["sent"] >= 2:
                rate = round(v["replied"] / v["sent"] * 100, 1)
                top_openers.append({"text": v["text"], "sent": v["sent"],
                                    "replied": v["replied"], "rate": rate})
        top_openers.sort(key=lambda x: (-x["rate"], -x["sent"]))
        result["top_openers"] = top_openers[:10]

        # 阶段推断统计
        stage_data = {"icebreak": 0, "rapport": 0, "qualify": 0,
                      "soft_pitch": 0, "referral": 0}
        for lead in leads:
            mc = lead[1]
            if mc >= 8:
                stage_data["referral"] += 1
            elif mc >= 5:
                stage_data["soft_pitch"] += 1
            elif mc >= 3:
                stage_data["rapport"] += 1
            else:
                stage_data["icebreak"] += 1

        result["stages"] = stage_data

        # 用户回复模式分析
        reply_patterns = {}
        user_msgs = conn.execute(
            "SELECT content FROM conversations WHERE role='user' "
            "ORDER BY timestamp DESC LIMIT 500"
        ).fetchall()
        for row in user_msgs:
            txt = (row[0] or "").lower().strip()
            if len(txt) < 2:
                continue
            # 归类回复类型
            if any(w in txt for w in ["telegram", "tg", "whatsapp", "wa", "contact"]):
                cat = "referral_intent"
            elif any(w in txt for w in ["thank", "grazie", "好的", "ok", "sure"]):
                cat = "positive"
            elif any(w in txt for w in ["no", "stop", "spam", "block", "不"]):
                cat = "negative"
            elif "?" in txt or "？" in txt:
                cat = "question"
            else:
                cat = "neutral"
            reply_patterns[cat] = reply_patterns.get(cat, 0) + 1
        result["reply_patterns"] = reply_patterns

        conn.close()

        # 生成优化建议
        insights = []
        reply_rate = round(replied_convs / total_convs * 100, 1) if total_convs else 0

        if reply_rate < 20:
            insights.append({
                "type": "warning", "priority": "high",
                "text": "回复率仅 " + str(reply_rate) + "%，建议：1) 优化开场白更个性化 2) 减少模板化内容 3) 分析目标用户画像后定制话术"
            })
        elif reply_rate < 50:
            insights.append({
                "type": "info", "priority": "medium",
                "text": "回复率 " + str(reply_rate) + "%，中等水平。建议对比效果最好的开场白，提炼共同特征"
            })
        else:
            insights.append({
                "type": "success", "priority": "low",
                "text": "回复率 " + str(reply_rate) + "%，表现优秀！建议保持当前策略并扩大量级"
            })

        deep_rate = round(deep_convs / total_convs * 100, 1) if total_convs else 0
        if deep_rate < 10:
            insights.append({
                "type": "warning", "priority": "high",
                "text": "深度对话率仅 " + str(deep_rate) + "%，大多数对话未超过 5 轮。建议：增加追问和互动点，避免单向推送"
            })

        ref_intent = reply_patterns.get("referral_intent", 0)
        if ref_intent > 0:
            insights.append({
                "type": "success", "priority": "medium",
                "text": "已有 " + str(ref_intent) + " 条回复含 TG/WA 引流意向词，说明引流策略正在生效"
            })

        negative = reply_patterns.get("negative", 0)
        if negative > replied_convs * 0.3 and replied_convs > 5:
            insights.append({
                "type": "warning", "priority": "high",
                "text": "负面回复占比 " + str(round(negative / replied_convs * 100)) + "%，偏高。建议：1) 降低推送频率 2) 优化目标筛选条件"
            })

        result["insights"] = insights
        result["summary"] = {
            "total_conversations": total_convs,
            "replied": replied_convs,
            "reply_rate": reply_rate,
            "deep_conversations": deep_convs,
            "deep_rate": deep_rate,
        }

    except Exception as e:
        result["error"] = str(e)

    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# P3-1: 涨粉实时仪表盘 API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.post("/comment_reply_monitor/enable")
def enable_comment_reply_monitor(cron: str = "0 */3 * * *", max_replies: int = 20):
    """启用/更新评论回复→DM 定时监控（每3小时扫描所有设备）。"""
    import uuid as _uuid, json as _json, datetime as _dt
    from ..database import get_conn
    sid = "comment_reply_monitor_auto"
    created = _dt.datetime.utcnow().isoformat() + "Z"
    params = _json.dumps({"max_replies": max_replies}, ensure_ascii=False)
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT schedule_id FROM schedules WHERE name=?", (sid,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE schedules SET cron_expr=?, params=?, enabled=1 WHERE name=?",
                (cron, params, sid)
            )
            conn.commit()
            return {"status": "updated", "name": sid, "cron": cron}
        else:
            new_id = str(_uuid.uuid4())
            conn.execute(
                "INSERT INTO schedules (schedule_id,name,cron_expr,task_type,device_id,params,enabled,created_at) "
                "VALUES (?,?,?,?,?,?,1,?)",
                (new_id, sid, cron, "tiktok_check_comment_replies", None, params, created)
            )
            conn.commit()
            return {"status": "created", "schedule_id": new_id, "name": sid, "cron": cron}


@router.post("/comment_reply_monitor/disable")
def disable_comment_reply_monitor():
    """关闭评论回复→DM 定时监控。"""
    from ..database import get_conn
    with get_conn() as conn:
        conn.execute(
            "UPDATE schedules SET enabled=0 WHERE name='comment_reply_monitor_auto'"
        )
        conn.commit()
    return {"status": "disabled"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# P3-1: 涨粉实时仪表盘 API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/growth_stats")
def growth_stats(date: str = ""):
    """返回每台设备今日关注/配额/阶段等涨粉统计，供前端仪表盘轮询。"""
    import datetime as _dt
    from ..device_state import get_device_state_store
    from ..database import get_conn

    today = date or _dt.date.today().isoformat()
    ds = get_device_state_store("tiktok")

    devices = []
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT device_id FROM device_states WHERE platform='tiktok' "
                "AND device_id NOT LIKE '__%__'"
            ).fetchall()
            all_ids = [r[0] for r in rows]
    except Exception:
        all_ids = []

    total_followed = 0
    total_quota = 0
    paused_count = 0

    for did in all_ids:
        followed = ds.get_int(did, f"daily:{today}:followed") or 0
        ramp_max = ds.get_follow_ramp_max(did) or 5
        phase = ds.get_phase(did) or "unknown"
        can_follow = ds.get_bool(did, "can_follow")
        total_total = ds.get_int(did, "total_followed") or 0
        sessions_today = ds.get_int(did, f"daily:{today}:sessions") or 0
        # 估算回关粉：使用15%的保守回关率
        est_fans = round(followed * 0.15)
        # 配额使用率
        quota_pct = round(followed / ramp_max * 100) if ramp_max > 0 else 0
        # 是否应自动暂停（超过90%）
        should_pause = quota_pct >= 90 and ramp_max > 0
        if should_pause:
            paused_count += 1

        total_followed += followed
        total_quota += ramp_max

        devices.append({
            "device_id": did,
            "followed_today": followed,
            "quota_max": ramp_max,
            "quota_pct": quota_pct,
            "should_pause": should_pause,
            "phase": phase,
            "can_follow": can_follow,
            "total_followed": total_total,
            "sessions_today": sessions_today,
            "est_fans_today": est_fans,
        })

    # 按关注数降序
    devices.sort(key=lambda x: x["followed_today"], reverse=True)

    return {
        "date": today,
        "devices": devices,
        "summary": {
            "total_devices": len(devices),
            "total_followed_today": total_followed,
            "total_quota": total_quota,
            "paused_count": paused_count,
            "est_total_fans_today": round(total_followed * 0.15),
        },
    }

