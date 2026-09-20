"""中央凭据池观测（进程级单例，风格对齐 avatar_voice_stats / frontend_error_stats）。

为什么必须有：中央池的失败是**静默降级**——池不可达就回落自带凭据，用户侧
一切照常，日志里也只有一行 warning。没有这组计数就无法回答运营最关心的两个问题：

- 「中央池到底有没有在生效」＝ `by_source.credpool` vs `by_source.config` 的比例；
- 「用户实际享受到哪一档隔离」＝ `by_tier`（付费档才拿得到专属凭据）。

刻意不记的东西：api_hash、粘定键、卡密——观测面绝不承载任何密钥或可反查身份的值。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

#: 回落原因分类（保持有限集合，防脏字符串把 distinct 撑爆）
_FALLBACK_REASONS = (
    "unreachable",   # 传输层不可达（池没起 / 网络断）
    "rejected",      # 池明确拒绝（容量耗尽 / 越权）
    "no_token",      # 未配服务 token
    "no_client",     # 瘦客户端没找到（多半是打包漏带）
    "bad_data",      # 池返回的 api_id 非法
    "pool_down_no_cache",  # 池内号且无凭据缓存 → 拒绝错配启动（该号暂不上线）
    "error",         # 其它异常
)


class CredPoolStats:
    """线程安全的累计计数器（无上限风险：所有 key 都是有限枚举）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_source: Dict[str, int] = {
            "credpool": 0,        # 现场向池索取成功
            "credpool_cache": 0,  # 池不可达，用该号缓存的池凭据（仍是池凭据）
            "config": 0,
            "none": 0,
        }
        self._by_tier: Dict[str, int] = {}
        self._fallbacks: Dict[str, int] = {}
        self._reports = {"ok": 0, "fail": 0}
        self._releases = 0
        self._with_proxy = 0
        self._logins = 0
        self._with_fingerprint = 0
        self._last_error: str = ""

    def record_login(self, fingerprinted: bool) -> None:
        """一次协议登录发起。

        指纹不经过中央池（本地种子派生），所以它的覆盖率只能在这里记——
        否则「三隔离」里就有一件套是黑的，看板只能凭感觉说"应该开了"。
        """
        with self._lock:
            self._logins += 1
            if fingerprinted:
                self._with_fingerprint += 1

    # ── 记录 ──────────────────────────────────────────────────────────

    def record_resolve(
        self, source: str, tier: Optional[str] = None, with_proxy: bool = False
    ) -> None:
        """一次凭据解析的结果（credpool / config / none）。

        `with_proxy` 记录本次是否连独立出口一起拿到——隔离是三件套，
        只统计凭据会把「拿到凭据但没拿到 IP」的半隔离状态藏起来。
        """
        key = source if source in self._by_source else "none"
        with self._lock:
            self._by_source[key] += 1
            # 缓存命中用的也是池凭据（同一组），档位/出口一并照常计入——
            # 否则池抖动一下，看板上的付费档和出口覆盖率就凭空掉一截。
            if key in ("credpool", "credpool_cache"):
                t = (tier or "free").strip().lower() or "free"
                self._by_tier[t] = self._by_tier.get(t, 0) + 1
                if with_proxy:
                    self._with_proxy += 1

    def record_fallback(self, reason: str, detail: str = "") -> None:
        """回落到自带凭据的原因——这是排查「池为什么没生效」的第一现场。"""
        key = reason if reason in _FALLBACK_REASONS else "error"
        with self._lock:
            self._fallbacks[key] = self._fallbacks.get(key, 0) + 1
            if detail:
                self._last_error = str(detail)[:200]

    def record_report(self, ok: bool) -> None:
        with self._lock:
            self._reports["ok" if ok else "fail"] += 1

    def record_release(self) -> None:
        with self._lock:
            self._releases += 1

    # ── 导出 ──────────────────────────────────────────────────────────

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            total = sum(self._by_source.values())
            from_pool = (self._by_source.get("credpool", 0)
                         + self._by_source.get("credpool_cache", 0))
            return {
                "active": total > 0,
                "resolves": total,
                "by_source": dict(self._by_source),
                "by_tier": dict(self._by_tier),
                "fallbacks": dict(self._fallbacks),
                "reports": dict(self._reports),
                "releases": self._releases,
                "with_proxy": self._with_proxy,
                "logins": self._logins,
                "with_fingerprint": self._with_fingerprint,
                # 中央池实际承载比例——这一个数就能回答「池有没有在生效」
                # （含缓存命中：那也是池凭据，只是这一刻池不在线）
                "pool_share": round(from_pool / total, 3) if total else 0.0,
                # 池降级期占比：>0 说明池当时不可达、靠本地缓存顶着（该修池了）
                "cache_share": (round(self._by_source.get("credpool_cache", 0) / total, 3)
                                if total else 0.0),
                # 三隔离完整度：拿到凭据的里面有多少同时拿到了独立出口
                "proxy_share": round(self._with_proxy / from_pool, 3) if from_pool else 0.0,
                # 三隔离第三件套：发起的登录里有多少带了独立设备指纹
                "fp_share": (round(self._with_fingerprint / self._logins, 3)
                             if self._logins else 0.0),
                "last_error": self._last_error,
            }

    def dump_prom(self) -> str:
        d = self.dump()
        lines = [
            "# HELP credpool_resolves_total Credential resolutions by source",
            "# TYPE credpool_resolves_total counter",
        ]
        for source, n in d["by_source"].items():
            lines.append(f'credpool_resolves_total{{source="{source}"}} {n}')
        lines += [
            "# HELP credpool_tier_total Pool allocations by effective member tier",
            "# TYPE credpool_tier_total counter",
        ]
        for tier, n in d["by_tier"].items():
            lines.append(f'credpool_tier_total{{tier="{tier}"}} {n}')
        lines += [
            "# HELP credpool_fallback_total Fallbacks to local credentials by reason",
            "# TYPE credpool_fallback_total counter",
        ]
        for reason, n in d["fallbacks"].items():
            lines.append(f'credpool_fallback_total{{reason="{reason}"}} {n}')
        lines += [
            "# HELP credpool_pool_share Share of resolutions served by the central pool",
            "# TYPE credpool_pool_share gauge",
            f'credpool_pool_share {d["pool_share"]}',
            "# HELP credpool_proxy_share Share of pool allocations that also got a dedicated egress IP",
            "# TYPE credpool_proxy_share gauge",
            f'credpool_proxy_share {d["proxy_share"]}',
            "# HELP credpool_fp_share Share of protocol logins that carried a per-account device fingerprint",
            "# TYPE credpool_fp_share gauge",
            f'credpool_fp_share {d["fp_share"]}',
        ]
        return "\n".join(lines) + "\n"


_stats: Optional[CredPoolStats] = None
_stats_lock = threading.Lock()


def get_credpool_stats() -> CredPoolStats:
    global _stats
    if _stats is None:
        with _stats_lock:
            if _stats is None:
                _stats = CredPoolStats()
    return _stats


# ── 外部看门狗状态（供 ops 卡展示）─────────────────────────────────────────
# 池是**我们**跑的服务，进程健康只有外部看门狗知道。它把状态写成 json，这里读
# 给 ops 卡——否则池挂了只有一个没人看的日志文件知道。
# 路径**由配置给出且默认为空**：引擎要发到客户桌面，那里没有我们的运维目录，
# 绝不能把绝对路径写死进代码。
_WD_CACHE: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": None}


def watchdog_state(path: str) -> Optional[Dict[str, Any]]:
    """读看门狗状态文件（按 mtime 缓存）；没配/读不到 → None，卡片自动不显示。"""
    p = (path or "").strip()
    if not p:
        return None
    try:
        import json
        import os
        import time

        mtime = os.path.getmtime(p)
        if _WD_CACHE["path"] == p and _WD_CACHE["mtime"] == mtime:
            return _WD_CACHE["data"]
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None
        now = time.time()
        last_probe = float(data.get("last_probe_ts") or 0)
        out = {
            "kind": str(data.get("last_kind") or "unknown"),   # ok / down / hang
            "detail": str(data.get("last_detail") or "")[:200],
            "strikes": int(data.get("strikes") or 0),
            "restarts": int(data.get("restarts") or 0),
            # 探针自己多久没跑过——看门狗本身死掉同样是盲区，这个数能暴露它
            "probe_age_min": int((now - last_probe) / 60) if last_probe else -1,
            "degraded_min": (int((now - float(data["degraded_since"])) / 60)
                             if data.get("degraded_since") else 0),
        }
        _WD_CACHE.update({"path": p, "mtime": mtime, "data": out})
        return out
    except Exception:  # noqa: BLE001
        return None
