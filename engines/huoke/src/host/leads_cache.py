# -*- coding: utf-8 -*-
"""P9-A: Worker-03 CRM 数据的 Stale-While-Revalidate 缓存。

设计原理:
  - 所有读请求命中内存缓存（μs 级响应），消除 7s 网络延迟
  - 后台线程每 30s 刷新一次 Worker-03 数据
  - 启动时异步预热（不阻塞 API 启动）
  - stale TTL 90s：即使后台刷新失败，仍提供最多 90s 的旧数据

Usage:
    from src.host.leads_cache import W03Cache, get_w03_cache
    cache = get_w03_cache()
    leads = cache.get_leads()   # 立即返回（缓存命中）或 None（冷启动）
    stats = cache.get_stats()
"""
import json
import logging
import threading
import time
import urllib.request
from typing import Any, Optional

log = logging.getLogger(__name__)

_W03_BASE = "http://192.168.0.103:8000"
_REFRESH_INTERVAL = 30   # 后台刷新间隔（秒）

# P9-A: 只有 Coordinator 才需要代理缓存；Worker 节点直接读本地 DB
def _is_worker_node() -> bool:
    """通过集群配置判断当前节点是否为 worker（非 coordinator）。"""
    try:
        from src.host.multi_host import load_cluster_config
        role = load_cluster_config().get("role", "standalone")
        return role == "worker"
    except Exception:
        return False

_IS_WORKER03 = _is_worker_node()
_STALE_TTL = 90          # 最长使用旧缓存时间（秒）
_FETCH_TIMEOUT = 12      # 单次 HTTP 请求超时

# (endpoint_key, path, fetch_timeout)
_ENDPOINTS = [
    ("leads_all",  "/leads?limit=200&order_by=score+DESC",     12),
    ("stats",      "/leads/stats",                              15),
    ("funnel",     "/funnel?platform=tiktok&days=30",           12),
]


class W03Cache:
    """单例 Worker-03 数据缓存，带后台刷新线程。"""

    def __init__(self):
        self._cache: dict[str, dict] = {}  # key -> {"ts": float, "data": Any}
        self._lock = threading.Lock()
        self._refreshing: set[str] = set()
        self._started = False
        # 2026-05-05 H.2-followup: 停 _refresh_loop 用 stop_event + thread ref
        self._stop_event = threading.Event()
        self._refresh_thread: Optional[threading.Thread] = None

    # ── 内部工具 ─────────────────────────────────────────────────────

    def _fetch(self, path: str, timeout: int = _FETCH_TIMEOUT) -> Optional[Any]:
        try:
            req = urllib.request.Request(f"{_W03_BASE}{path}",
                                         headers={"Connection": "close"})
            resp = urllib.request.urlopen(req, timeout=timeout)
            try:
                return json.loads(resp.read().decode())
            finally:
                resp.close()
        except Exception as e:
            log.debug("[W03Cache] fetch %s failed: %s", path, e)
            return None

    def _store(self, key: str, data: Any):
        with self._lock:
            self._cache[key] = {"ts": time.time(), "data": data}
        log.debug("[W03Cache] stored %s (items=%s)", key,
                  len(data) if isinstance(data, list) else "dict")

    def _get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._cache.get(key)
        if not entry:
            return None
        if time.time() - entry["ts"] < _STALE_TTL:
            return entry["data"]
        return None  # 超过 stale TTL，视为缓存失效

    def _bg_refresh(self, key: str, path: str, timeout: int):
        """非阻塞后台刷新单个 key（防重入）。"""
        if key in self._refreshing:
            return
        self._refreshing.add(key)

        def _run():
            try:
                data = self._fetch(path, timeout)
                if data is not None:
                    self._store(key, data)
            finally:
                self._refreshing.discard(key)

        threading.Thread(target=_run, daemon=True, name=f"w03-refresh-{key}").start()

    def _refresh_loop(self):
        """后台线程：每隔 _REFRESH_INTERVAL 秒全量刷新所有端点。"""
        # 2026-05-05 H.2-followup: while True + time.sleep → stop_event 可控
        while not self._stop_event.is_set():
            self._stop_event.wait(_REFRESH_INTERVAL)
            if self._stop_event.is_set():
                break
            for key, path, timeout in _ENDPOINTS:
                try:
                    data = self._fetch(path, timeout)
                    if data is not None:
                        self._store(key, data)
                except Exception:
                    pass

    # ── 公开 API ──────────────────────────────────────────────────────

    def start(self, warm: bool = True):
        """启动后台刷新线程（幂等，多次调用安全）。在 Worker-03 上为空操作。"""
        if _IS_WORKER03:
            log.debug("[W03Cache] 跳过缓存启动（本机为 Worker-03，不自我代理）")
            return
        if self._started:
            return
        self._started = True

        if warm:
            def _warm():
                time.sleep(2)  # 等待 API 启动完成
                for key, path, timeout in _ENDPOINTS:
                    data = self._fetch(path, timeout)
                    if data is not None:
                        self._store(key, data)
                log.info("[W03Cache] 预热完成: %s", list(self._cache.keys()))
            threading.Thread(target=_warm, daemon=True, name="w03-cache-warm").start()

        # 2026-05-05 H.2-followup: 保 thread ref 以便 stop join
        self._stop_event.clear()
        t = threading.Thread(target=self._refresh_loop, daemon=True,
                             name="w03-cache-bg")
        t.start()
        self._refresh_thread = t
        log.info("[W03Cache] 后台刷新线程已启动（间隔 %ds）", _REFRESH_INTERVAL)

    def stop(self, timeout_sec: float = 5.0) -> bool:
        """优雅停止后台刷新线程 (Stage H.2-followup).

        设 _stop_event 让 _refresh_loop 下次 wait 立即唤醒退出. join thread.
        Returns True 如果在 timeout 内退出.
        """
        self._stop_event.set()
        t = self._refresh_thread
        self._refresh_thread = None
        self._started = False
        if t is None:
            return True
        t.join(timeout=timeout_sec)
        return not t.is_alive()

    def get_leads(self) -> Optional[list]:
        """返回 Worker-03 全量线索列表（缓存）。None = 尚未预热。"""
        return self._get("leads_all")

    def get_stats(self) -> Optional[dict]:
        """返回 Worker-03 pipeline 统计（缓存）。"""
        return self._get("stats")

    def get_funnel(self) -> Optional[dict]:
        """返回 Worker-03 CRM 漏斗数据（缓存）。"""
        return self._get("funnel")

    def invalidate(self, key: Optional[str] = None):
        """手动使缓存失效（可指定 key 或全部）。"""
        with self._lock:
            if key:
                self._cache.pop(key, None)
            else:
                self._cache.clear()

    def info(self) -> dict:
        """返回各缓存项的年龄和大小（用于监控和调试）。"""
        with self._lock:
            return {
                k: {
                    "age_s": round(time.time() - v["ts"], 1),
                    "items": len(v["data"]) if isinstance(v["data"], list) else 1,
                    "stale": (time.time() - v["ts"]) > _STALE_TTL,
                }
                for k, v in self._cache.items()
            }

    def filter_leads(self, leads: list, *, status: str = "", platform: str = "",
                     min_score: float = 0, search: str = "",
                     order_by: str = "score DESC",
                     limit: int = 50, offset: int = 0) -> list:
        """在内存中对 Worker-03 线索应用与 list_leads() 相同的过滤条件。
        避免每次都向 Worker-03 发起带参数的 HTTP 请求。
        """
        result = leads[:]
        if status:
            result = [l for l in result if l.get("status") == status]
        if platform:
            # Worker-03 leads 的 source_platform 字段
            result = [l for l in result if l.get("source_platform") == platform]
        if min_score > 0:
            result = [l for l in result if float(l.get("score") or 0) >= min_score]
        if search:
            s = search.lower()
            result = [l for l in result
                      if s in (l.get("name") or "").lower()
                      or s in (l.get("email") or "").lower()
                      or s in (l.get("company") or "").lower()]
        # 排序
        reverse = "DESC" in order_by.upper()
        sort_key = "score" if "score" in order_by else (
            "created_at" if "created_at" in order_by else
            "updated_at" if "updated_at" in order_by else "name"
        )
        try:
            result.sort(key=lambda x: x.get(sort_key) or "", reverse=reverse)
        except Exception:
            pass
        return result[offset: offset + limit]


# ── 单例 ──────────────────────────────────────────────────────────────
_INSTANCE: Optional[W03Cache] = None


def get_w03_cache() -> W03Cache:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = W03Cache()
    return _INSTANCE


def stop_w03_cache(timeout_sec: float = 5.0) -> bool:
    """模块级 wrapper for api.py lifespan shutdown (Stage H.2-followup).

    没启动 instance 时返 True (no-op). 与 start 对称.
    """
    if _INSTANCE is None:
        return True
    return _INSTANCE.stop(timeout_sec=timeout_sec)
