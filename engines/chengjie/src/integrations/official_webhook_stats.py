"""官方渠道 webhook 回调可达性台账（2026-08-07，IG/Zalo 面板点亮后的下一层断层防线）。

**为什么要有这个模块**：官方 API 渠道（Instagram / Zalo / Messenger 官方 /
WhatsApp Cloud）是**被动 webhook 入站**——Meta/Zalo 的回调打不进来时本系统零报错
（对面收 4xx/超时，重试几次就放弃），坐席只会觉得「这渠道没客人」。与「凭证配了、
状态绿了、但客户消息永远进不来」这个静默断层同构的事故已经发生过一次（IG/Zalo
后端完工却没人发现面板不亮）。本模块把「回调到达」升格为可观测事实：

- 四个 webhook 家族在 **HTTP 入口层**记录（形状不认识的事件也算「到达」——可达性
  问的是对面能不能打到我们，不是业务能不能解析）：GET 验证握手成功/失败、POST
  事件被接受/验签失败/坏 JSON。
- 台账落盘 data 区 JSON（跨重启保住「历史上到达过」的事实——verify 握手是配置期
  一次性动作，进程级计数器一重启就会把健康渠道误判成从未接通）；写入 5s 节流，
  最差丢最近几秒的计数，「首次到达」里程碑立即落盘。
- ``collect_status()`` 出每渠道判词供路由 + ops 卡消费；**只对「从未到达/验签失败」
  下硬结论**，「多久没新事件」只展示不判红——低流量渠道安静≠故障，误报毁信任
  （与 platform_capabilities.reconcile 的 no_traffic 哲学同源）。

记录函数契约：**绝不抛异常**（webhook 收发主流程优先，观测挂了不能影响业务）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: 平台键（与 official_api_worker.OFFICIAL_PLATFORMS / Kill-Switch 作用域同名）→
#: config 顶层块名。LINE official 刻意不在内：其入站走 okline 协议，无 webhook。
PLATFORM_CONFIG_BLOCKS: Dict[str, str] = {
    "instagram": "instagram",
    "zalo": "zalo",
    "messenger": "facebook_messenger",
    "whatsapp": "whatsapp_cloud",
}

#: 有 GET 验证握手的平台（Meta 系）；Zalo 无握手，只看 POST 事件。
HAS_GET_VERIFY: Dict[str, bool] = {
    "instagram": True, "messenger": True, "whatsapp": True, "zalo": False,
}

#: 各 webhook 注册函数把挂载路径写进 app.state 的属性名（挂载真相 > 配置推断：
#: 凭证是启动后才填的场景，config 看着齐全、路由其实没挂——等重启）。
_APP_STATE_ATTRS: Dict[str, str] = {
    "instagram": "ig_webhook_path",
    "zalo": "zalo_webhook_path",
    "messenger": "fb_webhook_path",
    "whatsapp": "wa_cloud_webhook_path",
}

#: 常驻挂载平台（2026-08-10 起 messenger 的 register 不再按凭证 early-return）：
#: 路由永远在，凭证只门控请求级「接不接受」。app_state=None 的配置推断随之改为
#: enabled 即视为已挂载——否则「刚保存一半凭证」会被误判 not_mounted（该判词建议
#: 等重启），而那正是热挂载已消灭的状态；凭证缺失的真相由 creds_ok=False 与
#: auth_failing（入站被拒会记 no_app_secret 账）两个信号表达。
_HOT_MOUNTED = frozenset({"messenger"})

_EMPTY_ROW: Dict[str, Any] = {
    "first_verify_ts": 0.0, "last_verify_ts": 0.0, "verify_fail_total": 0,
    "last_verify_fail_ts": 0.0,
    "first_event_ts": 0.0, "last_event_ts": 0.0, "events_total": 0,
    "error_total": 0, "last_error_ts": 0.0, "last_error_kind": "",
}

_PERSIST_MIN_INTERVAL_SEC = 5.0

_lock = threading.Lock()
_state: Dict[str, Dict[str, Any]] = {}
_loaded = False
_last_persist = 0.0
_path_override: Optional[Path] = None


def _state_path() -> Path:
    if _path_override is not None:
        return _path_override
    try:
        from src.licensing.data_paths import data_file
        return Path(data_file("official_webhook_state.json"))
    except Exception:  # noqa: BLE001 - 兜底仓内（与 data_paths 自身回落一致）
        return Path("config/official_webhook_state.json")


def _load_locked() -> None:
    global _loaded, _state
    if _loaded:
        return
    _loaded = True
    try:
        p = _state_path()
        if p.is_file():
            raw = json.loads(p.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, dict):
                        row = dict(_EMPTY_ROW)
                        row.update({kk: v[kk] for kk in _EMPTY_ROW if kk in v})
                        _state[str(k)] = row
    except Exception:  # noqa: BLE001
        logger.debug("[official_webhook_stats] 台账读取失败（按空台账继续）",
                     exc_info=True)


def _persist_locked(force: bool = False) -> None:
    global _last_persist
    now = time.time()
    if not force and (now - _last_persist) < _PERSIST_MIN_INTERVAL_SEC:
        return
    _last_persist = now
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_state, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001
        logger.debug("[official_webhook_stats] 台账落盘失败（已忽略）", exc_info=True)


def _row(platform: str) -> Dict[str, Any]:
    key = str(platform or "").strip().lower()
    row = _state.get(key)
    if row is None:
        row = dict(_EMPTY_ROW)
        _state[key] = row
    return row


def record_verify(platform: str, ok: bool = True) -> None:
    """GET 验证握手（Meta 系）。失败也记——**失败的握手同样证明公网可达**，
    只是 verify_token 配错了（两种故障的处置完全不同，必须分开数）。"""
    try:
        with _lock:
            _load_locked()
            row = _row(platform)
            now = time.time()
            first = False
            if ok:
                if not row["first_verify_ts"]:
                    row["first_verify_ts"] = now
                    first = True
                row["last_verify_ts"] = now
            else:
                row["verify_fail_total"] = int(row["verify_fail_total"]) + 1
                if not row["last_verify_fail_ts"]:
                    first = True
                row["last_verify_fail_ts"] = now
            _persist_locked(force=first)
    except Exception:  # noqa: BLE001
        logger.debug("[official_webhook_stats] record_verify 失败（已忽略）",
                     exc_info=True)


def record_event(platform: str, n: int = 1) -> None:
    """一次被接受的 POST 事件投递（验签通过 + JSON 可解析；按投递计，不按消息条数）。"""
    try:
        with _lock:
            _load_locked()
            row = _row(platform)
            now = time.time()
            first = not row["first_event_ts"]
            if first:
                row["first_event_ts"] = now
            row["last_event_ts"] = now
            row["events_total"] = int(row["events_total"]) + max(1, int(n))
            _persist_locked(force=first)
    except Exception:  # noqa: BLE001
        logger.debug("[official_webhook_stats] record_event 失败（已忽略）",
                     exc_info=True)


def record_error(platform: str, kind: str) -> None:
    """到达了但没被接受（bad_signature / bad_json）。同样是可达性证据。"""
    try:
        with _lock:
            _load_locked()
            row = _row(platform)
            first = not row["last_error_ts"]
            row["error_total"] = int(row["error_total"]) + 1
            row["last_error_ts"] = time.time()
            row["last_error_kind"] = str(kind or "")[:40]
            _persist_locked(force=first)
    except Exception:  # noqa: BLE001
        logger.debug("[official_webhook_stats] record_error 失败（已忽略）",
                     exc_info=True)


def snapshot() -> Dict[str, Dict[str, Any]]:
    """台账只读快照（拷贝）。"""
    try:
        with _lock:
            _load_locked()
            return {k: dict(v) for k, v in _state.items()}
    except Exception:  # noqa: BLE001
        return {}


def reset_for_tests(path: Optional[Path] = None) -> None:
    """测试专用：清内存态并可覆写落盘路径（生产代码勿调）。"""
    global _state, _loaded, _last_persist, _path_override
    with _lock:
        _state = {}
        _loaded = False
        _last_persist = 0.0
        _path_override = path


def inferred_mounted(platform: str, *, enabled: bool, creds: bool) -> bool:
    """app_state 不可得（CLI / 测试 / 进程外）时的挂载推断**单一事实源**。

    普通平台＝enabled 且凭证齐（register_* 才会挂路由）；热挂载平台
    （``_HOT_MOUNTED``）＝enabled 即视为已挂（路由常驻，凭证只门控接受）。
    ``collect_status`` 回落与 ``tools/official_webhook_selfcheck.py`` 共用本函数
    ——2026-08-10 曾因 CLI 各写一份推断而与模块口径漂移，勿再复制该表达式。
    """
    return bool(enabled) and (
        bool(creds) or str(platform or "").lower() in _HOT_MOUNTED)


def creds_ok(platform: str, config: Dict[str, Any]) -> bool:
    """凭证是否齐到「入站链路能真正放行」的程度。

    判据逐平台与各 register_*（IG/Zalo/WA）的 early-return 条件**逐字对齐**（这里
    松了/紧了都会让 not_mounted 判词失真）。messenger 例外（2026-08-10 热挂载）：
    路由常驻、不再按凭证 early-return，同一组字段改为门控请求级「接不接受」
    （GET 比 verify_token / POST 验 app_secret / 出站用 page_access_token），
    字段集不变。"""
    block = (config or {}).get(PLATFORM_CONFIG_BLOCKS.get(platform, ""), {}) or {}
    g = lambda k: str(block.get(k) or "").strip()  # noqa: E731
    if platform == "instagram":
        return bool(g("page_access_token") and g("app_secret") and g("verify_token"))
    if platform == "zalo":
        return bool(g("access_token"))
    if platform == "messenger":
        return bool(g("page_access_token") and g("app_secret") and g("verify_token"))
    if platform == "whatsapp":
        return bool(g("phone_number_id") and g("access_token")
                    and g("app_secret") and g("verify_token"))
    return False


def classify(*, enabled: bool, creds: bool, mounted: bool,
             has_verify: bool, row: Dict[str, Any]) -> str:
    """单渠道判词（纯函数）。阶梯从「有流量」往「没配置」降级：

    - ``live``            事件到达过（可达性已证明，剩下的看业务）
    - ``handshake_only``  握手成功但零事件（正常冷启动，或开发者后台订阅字段没勾）
    - ``auth_failing``    有人打进来但**没有一次成功**（verify_token/app_secret 配错
                          ——公网可达，凭证错，处置动作与「不可达」完全不同）
    - ``never_reached``   路由挂着、外面从没打进来过（公网 URL/隧道/后台回调没配）
    - ``not_mounted``     渠道启用但路由没挂（凭证缺 / 启动后才填凭证等重启；
                          热挂载平台〔messenger〕不再出现此态）
    - ``disabled``        渠道未启用
    """
    row = row or {}
    if not enabled:
        return "disabled"
    if not mounted:
        return "not_mounted"
    if int(row.get("events_total") or 0) > 0:
        return "live"
    if has_verify and float(row.get("last_verify_ts") or 0) > 0:
        return "handshake_only"
    if (int(row.get("verify_fail_total") or 0) > 0
            or int(row.get("error_total") or 0) > 0):
        return "auth_failing"
    return "never_reached"


def collect_status(config: Dict[str, Any],
                   app_state: Any = None) -> Dict[str, Any]:
    """全渠道可达性总览（路由 / ops 卡 / 将来看门狗的同一口径入口）。

    ``app_state`` 传 FastAPI ``app.state``（读各 webhook 注册时写入的挂载路径＝
    **挂载真相**）；传 None（CLI/测试）时回落「enabled+creds」的配置推断。
    """
    cfg = config or {}
    snap = snapshot()
    platforms: List[Dict[str, Any]] = []
    now = time.time()
    any_enabled = False
    for platform, block_key in PLATFORM_CONFIG_BLOCKS.items():
        block = cfg.get(block_key) or {}
        enabled = bool(block.get("enabled"))
        credok = creds_ok(platform, cfg)
        attr = _APP_STATE_ATTRS[platform]
        if app_state is not None:
            mounted = bool(str(getattr(app_state, attr, "") or "").strip())
        else:
            mounted = inferred_mounted(platform, enabled=enabled, creds=credok)
        row = snap.get(platform) or dict(_EMPTY_ROW)
        verdict = classify(enabled=enabled, creds=credok, mounted=mounted,
                           has_verify=HAS_GET_VERIFY[platform], row=row)
        if enabled:
            any_enabled = True
        item = {
            "platform": platform,
            "enabled": enabled,
            "creds_ok": credok,
            "mounted": mounted,
            "has_verify": HAS_GET_VERIFY[platform],
            "verdict": verdict,
            "webhook_path": str(block.get("webhook_path") or "").strip(),
            "events_total": int(row.get("events_total") or 0),
            "error_total": int(row.get("error_total") or 0),
            "verify_fail_total": int(row.get("verify_fail_total") or 0),
            "last_event_age_sec": (
                int(now - float(row["last_event_ts"]))
                if float(row.get("last_event_ts") or 0) > 0 else -1),
            "last_verify_age_sec": (
                int(now - float(row["last_verify_ts"]))
                if float(row.get("last_verify_ts") or 0) > 0 else -1),
            "last_error_kind": str(row.get("last_error_kind") or ""),
        }
        platforms.append(item)
    has_history = any(
        int((snap.get(p) or {}).get("events_total") or 0) > 0
        or float((snap.get(p) or {}).get("last_verify_ts") or 0) > 0
        for p in PLATFORM_CONFIG_BLOCKS)
    return {
        "ok": True,
        # active=False → ops 卡整卡隐藏（站内「零流量不占版面」惯例）：
        # 没有任何官方渠道启用、台账也无历史 ＝ 这台部署根本不用官方通道。
        "active": bool(any_enabled or has_history),
        "platforms": platforms,
    }


__all__ = [
    "PLATFORM_CONFIG_BLOCKS", "HAS_GET_VERIFY",
    "record_verify", "record_event", "record_error",
    "snapshot", "reset_for_tests", "creds_ok", "classify", "collect_status",
    "inferred_mounted",
]
