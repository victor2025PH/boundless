"""代理池（M4）。

「一号一代理」是多账号防关联的核心之一。本模块提供一个**用户自填**的代理池：
代理资源来源留空（不内置任何代理），由运营在「账号管理 → 新增账号 → 代理配置」里
逐条录入（或导入），登录新账号时绑定一条代理，持久化到账号注册表的 ``proxy_id``。

存储：独立 SQLite（默认 ``config/proxy_pool.db``），线程安全，与 ``account_registry`` 同风格。

── 防封增强（2026-08，对齐竞品「零信任」防关联叙事）────────────────────────────
在原「录入 + TCP 可达」基础上补齐真正影响封号率的四件事，全部**向后兼容**（旧
字段/签名不变，新字段带默认值、新参数用关键字）：

1. **代理类型分类**（``kind``：datacenter/residential/isp/mobile）——只有住宅/移动
   IP 才真正防关联，数据中心 IP 极易被平台识别关联。
2. **出口 Geo**（``country``/``region``/``exit_ip``）——IP 归属地要与账号注册地/时区
   匹配，否则触发风控。由真握手验活顺带探测。
3. **真·握手验活**（``test`` 升级）——旧实现只做 TCP connect（只证明端口开着）。现在
   通过代理**真发一个请求**到 echo-ip 服务，一次往返同时确认「代理协议真能用」+ 拿到
   出口 IP/国家。探测器**可注入**（``probe_fn``，测试零网络）；默认实现对 http/https
   代理用 aiohttp，对 socks 若无 ``aiohttp_socks`` 则**优雅回落** TCP（不引入硬依赖、
   绝不劣于现状）。
4. **健康状态机 + 黑名单轮换**（``fail_count``/``cooldown_until``）——连续失败达阈值
   自动进冷却（黑名单），``pick_available`` 跳过占用中/冷却中的代理。

「一号一 IP」由 ``assign(..., exclusive=True)`` 落实：绑定新代理时释放该账号占用的
其他代理，保证一个账号只占一条出口。
"""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

_DDL = """
CREATE TABLE IF NOT EXISTS proxies (
    proxy_id        TEXT PRIMARY KEY,
    label           TEXT NOT NULL DEFAULT '',
    scheme          TEXT NOT NULL DEFAULT 'socks5',
    host            TEXT NOT NULL DEFAULT '',
    port            INTEGER NOT NULL DEFAULT 0,
    username        TEXT NOT NULL DEFAULT '',
    password        TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'unknown',
    assigned_account TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL DEFAULT 0,
    last_checked_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_proxies_status ON proxies(status);
"""

# 幂等 migration：对存量 db 缺列即补（ALTER TABLE ADD COLUMN），带默认值不破坏旧行。
# 与 account_registry 同风格——独立 db 自己 migrate。
_MIGRATIONS: List[tuple[str, str]] = [
    ("kind", "ALTER TABLE proxies ADD COLUMN kind TEXT NOT NULL DEFAULT 'unknown'"),
    ("country", "ALTER TABLE proxies ADD COLUMN country TEXT NOT NULL DEFAULT ''"),
    ("region", "ALTER TABLE proxies ADD COLUMN region TEXT NOT NULL DEFAULT ''"),
    ("exit_ip", "ALTER TABLE proxies ADD COLUMN exit_ip TEXT NOT NULL DEFAULT ''"),
    ("fail_count", "ALTER TABLE proxies ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0"),
    ("cooldown_until", "ALTER TABLE proxies ADD COLUMN cooldown_until REAL NOT NULL DEFAULT 0"),
    ("latency_ms", "ALTER TABLE proxies ADD COLUMN latency_ms REAL NOT NULL DEFAULT 0"),
]

VALID_SCHEMES = ("http", "https", "socks4", "socks5")
# unknown = 未标注（保守不判为住宅）；只有 residential/mobile 才真正防关联。
PROXY_KINDS = ("unknown", "datacenter", "residential", "isp", "mobile")

# 健康状态机默认阈值（可经 ProxyPool 构造参数覆盖）
DEFAULT_FAIL_THRESHOLD = 3        # 连续失败达此值 → 进冷却（黑名单）
DEFAULT_COOLDOWN_SEC = 1800.0     # 冷却时长 30min
# 默认验活 echo 服务：通过代理请求它，返回体即代理出口的 IP + 国家（免费、无需 key）。
DEFAULT_PROBE_URL = "http://ip-api.com/json"


def _proxy_url(scheme: str, host: str, port: int, username: str, password: str) -> str:
    auth = ""
    if username:
        auth = username + (f":{password}" if password else "") + "@"
    return f"{scheme}://{auth}{host}:{port}"


def _normalize_country(code: str) -> str:
    """归一国家标识为大写去空格；空串保持空串。仅做轻量归一，不做别名映射。"""
    return str(code or "").strip().upper()


def geo_mismatch(proxy_country: str, expected_country: str) -> bool:
    """代理出口国是否与期望国不一致。

    **信息不足不判**（任一为空 → 返回 False）：宁可漏报不误报——把「未探测到国家」
    当成 mismatch 会在正常场景刷告警。仅当两者都已知且不同才算 mismatch。
    """
    a = _normalize_country(proxy_country)
    b = _normalize_country(expected_country)
    if not a or not b:
        return False
    return a != b


def parse_import_lines(
    text: str, *, default_scheme: str = "socks5",
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把供应商导出的代理清单文本解析成条目（一键代理 P3 批量入库的纯核心）。

    供应商后台导出的主流三种行格式全收：

    - ``host:port``
    - ``host:port:user:pass``（Proxy-Seller / ZooProxy 等的默认导出）
    - ``scheme://user:pass@host:port`` 与 ``scheme://host:port``（URL 形）

    空行与 ``#`` 注释行跳过；批内按 (host, port, username) 去重（同一份导出常
    重复粘贴）。返回 ``(entries, bad)``——坏行**逐行带行号与原因**如实返回而不是
    整批拒绝：粘 50 行错 2 行，运营需要知道是哪 2 行，而不是重来一遍。
    """
    entries: List[Dict[str, Any]] = []
    bad: List[Dict[str, Any]] = []
    seen: set = set()
    scheme_default = str(default_scheme or "socks5").lower()
    if scheme_default not in VALID_SCHEMES:
        scheme_default = "socks5"

    for no, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        scheme, user, pw = scheme_default, "", ""
        rest = line
        if "://" in line:
            head, rest = line.split("://", 1)
            scheme = head.strip().lower()
            if scheme not in VALID_SCHEMES:
                bad.append({"line": no, "text": raw, "reason": "bad_scheme"})
                continue
            if "@" in rest:
                cred, rest = rest.rsplit("@", 1)
                user, _, pw = cred.partition(":")
        parts = rest.split(":")
        if len(parts) == 2:
            host, port_s = parts
        elif len(parts) == 4 and not user:
            host, port_s, user, pw = parts
        else:
            bad.append({"line": no, "text": raw, "reason": "bad_format"})
            continue
        host = host.strip()
        try:
            port = int(port_s.strip())
        except ValueError:
            bad.append({"line": no, "text": raw, "reason": "bad_port"})
            continue
        if not host or not (0 < port < 65536):
            bad.append({"line": no, "text": raw, "reason": "bad_host_port"})
            continue
        key = (host, port, user)
        if key in seen:
            continue  # 批内重复：静默跳过（同一条粘两遍不是错误）
        seen.add(key)
        entries.append({"scheme": scheme, "host": host, "port": port,
                        "username": user.strip(), "password": pw})
    return entries, bad


@dataclass
class ProbeResult:
    """一次验活探测的结果。

    - ``ok``：代理是否真的可用（协议握手 + 请求成功）。
    - ``exit_ip`` / ``country``：探测到的出口 IP 与国家（可能为空——TCP 回落拿不到）。
    - ``latency_ms``：往返耗时。
    - ``error``：失败原因（截断），供排障。
    """

    ok: bool
    exit_ip: str = ""
    country: str = ""
    region: str = ""
    latency_ms: float = 0.0
    error: str = ""


ProbeFn = Callable[[Dict[str, Any]], Awaitable[Optional[ProbeResult]]]


async def default_proxy_probe(
    proxy: Dict[str, Any], *, url: str = DEFAULT_PROBE_URL, timeout: float = 8.0
) -> Optional[ProbeResult]:
    """默认验活探测：通过代理真发一个 HTTP 请求，拿出口 IP + 国家。

    返回语义（三态，调用方据此决定是否回落 TCP）：
    - ``ProbeResult(ok=True, ...)``  代理真能用，附出口 IP/国家。
    - ``ProbeResult(ok=False, error)``  探测器工作了但代理不通/坏。
    - ``None``  **探测器本身不可用**（缺 aiohttp / socks 缺 aiohttp_socks）→ 让调用方
      回落到 TCP 可达探测，绝不把「我们没装库」误报成「代理坏了」。
    """
    scheme = str(proxy.get("scheme") or "socks5").lower()
    host = str(proxy.get("host") or "")
    port = int(proxy.get("port") or 0)
    if not host or not port:
        return ProbeResult(ok=False, error="missing host/port")
    username = str(proxy.get("username") or "")
    password = str(proxy.get("password") or "")
    proxy_url = _proxy_url(scheme, host, port, username, password)

    try:
        import aiohttp  # noqa: F401
    except Exception:
        return None  # 没有 aiohttp → 回落 TCP

    started = time.monotonic()
    try:
        if scheme in ("http", "https"):
            import aiohttp

            timeout_cfg = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
                async with session.get(url, proxy=proxy_url) as resp:
                    data = await resp.json(content_type=None)
        else:  # socks4 / socks5 需要 aiohttp_socks，缺库则回落
            try:
                from aiohttp_socks import ProxyConnector  # type: ignore
            except Exception:
                return None
            import aiohttp

            connector = ProxyConnector.from_url(proxy_url)
            timeout_cfg = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(
                connector=connector, timeout=timeout_cfg
            ) as session:
                async with session.get(url) as resp:
                    data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001  代理不通/超时/坏 → 明确判坏（非回落）
        return ProbeResult(ok=False, error=str(exc)[:200])

    latency_ms = (time.monotonic() - started) * 1000.0
    data = data or {}
    # ip-api.com 契约：{"status":"success","query":"1.2.3.4","countryCode":"US","regionName":"California"}
    exit_ip = str(data.get("query") or data.get("ip") or data.get("origin") or "")
    country = _normalize_country(
        data.get("countryCode") or data.get("country_code") or data.get("country") or ""
    )
    region = str(data.get("regionName") or data.get("region") or "")
    return ProbeResult(
        ok=True, exit_ip=exit_ip, country=country, region=region, latency_ms=latency_ms
    )


class ProxyPool:
    """用户自填的代理池（线程安全 SQLite 封装）。"""

    def __init__(
        self,
        db_path: Path,
        *,
        fail_threshold: int = DEFAULT_FAIL_THRESHOLD,
        cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
        probe_url: str = DEFAULT_PROBE_URL,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, timeout=10
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._fail_threshold = max(1, int(fail_threshold))
        self._cooldown_sec = max(0.0, float(cooldown_sec))
        self._probe_url = probe_url or DEFAULT_PROBE_URL
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """幂等补列（存量 db 缺哪列补哪列）。调用方须已持有 ``self._lock``。"""
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(proxies)").fetchall()
        }
        for col, ddl in _MIGRATIONS:
            if col not in cols:
                self._conn.execute(ddl)

    @staticmethod
    def _row(row: sqlite3.Row, *, mask: bool = True, now: Optional[float] = None) -> Dict[str, Any]:
        d = dict(row)
        d["url"] = _proxy_url(
            d["scheme"], d["host"], d["port"], d["username"], d["password"])
        if mask:
            d["password"] = "******" if d.get("password") else ""
            if d.get("username"):
                d["url"] = _proxy_url(
                    d["scheme"], d["host"], d["port"], d["username"], "******")
        # 派生健康视图（供 UI / 挑选逻辑）——不落库，读时计算。
        ts = time.time() if now is None else now
        cooldown_until = float(d.get("cooldown_until") or 0)
        d["in_cooldown"] = cooldown_until > ts
        d["cooldown_remaining"] = max(0, int(cooldown_until - ts)) if cooldown_until > ts else 0
        d["assigned"] = bool(d.get("assigned_account"))
        # 健康 = 状态 ok 且不在冷却；住宅/移动 IP 才算「防关联合格」出口。
        d["healthy"] = (d.get("status") == "ok") and not d["in_cooldown"]
        d["is_residential"] = str(d.get("kind") or "") in ("residential", "mobile")
        return d

    def add(
        self,
        *,
        scheme: str = "socks5",
        host: str = "",
        port: int = 0,
        username: str = "",
        password: str = "",
        label: str = "",
        kind: str = "unknown",
        country: str = "",
        region: str = "",
    ) -> Dict[str, Any]:
        scheme = str(scheme or "socks5").lower()
        if scheme not in VALID_SCHEMES:
            raise ValueError(f"不支持的代理协议: {scheme}")
        if not host or not port:
            raise ValueError("host 与 port 必填")
        kind = str(kind or "unknown").lower()
        if kind not in PROXY_KINDS:
            raise ValueError(f"不支持的代理类型: {kind}")
        pid = "px_" + secrets.token_hex(5)
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO proxies
                   (proxy_id, label, scheme, host, port, username, password,
                    status, assigned_account, created_at, updated_at, last_checked_at,
                    kind, country, region, exit_ip, fail_count, cooldown_until, latency_ms)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, label, scheme, host, int(port), username, password,
                 "unknown", "", now, now, 0,
                 kind, _normalize_country(country), region, "", 0, 0, 0),
            )
            self._conn.commit()
        return self.get(pid, mask=True) or {}

    def get(self, proxy_id: str, *, mask: bool = True) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM proxies WHERE proxy_id=?", (proxy_id,)
            ).fetchone()
        return self._row(row, mask=mask) if row else None

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM proxies ORDER BY created_at"
            ).fetchall()
        return [self._row(r, mask=True) for r in rows]

    def remove(self, proxy_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM proxies WHERE proxy_id=?", (proxy_id,))
            self._conn.commit()

    def assign(self, proxy_id: str, account_key: str, *, exclusive: bool = True) -> None:
        """把代理绑定到账号。

        ``exclusive=True``（默认，落实「一号一 IP」）：先释放该账号占用的**其他**代理，
        保证一个账号只占一条出口——账号换绑/重登时旧出口自动回收，不会悄悄囤占多条。
        """
        now = time.time()
        with self._lock:
            if exclusive and account_key:
                self._conn.execute(
                    "UPDATE proxies SET assigned_account='', updated_at=? "
                    "WHERE assigned_account=? AND proxy_id<>?",
                    (now, account_key, proxy_id),
                )
            self._conn.execute(
                "UPDATE proxies SET assigned_account=?, updated_at=? WHERE proxy_id=?",
                (account_key, now, proxy_id),
            )
            self._conn.commit()

    def unassign(self, proxy_id: str) -> None:
        """解绑单条代理（不影响其他）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE proxies SET assigned_account='', updated_at=? WHERE proxy_id=?",
                (time.time(), proxy_id),
            )
            self._conn.commit()

    def release_account(self, account_key: str) -> None:
        """释放某账号占用的所有代理（账号删除/封禁下线时调用）。"""
        if not account_key:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE proxies SET assigned_account='', updated_at=? WHERE assigned_account=?",
                (time.time(), account_key),
            )
            self._conn.commit()

    def set_status(self, proxy_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE proxies SET status=?, last_checked_at=?, updated_at=? WHERE proxy_id=?",
                (status, time.time(), time.time(), proxy_id),
            )
            self._conn.commit()

    def set_geo(self, proxy_id: str, *, country: str = "", region: str = "", exit_ip: str = "") -> None:
        """写入出口 Geo（供运营手工标注或验活回填）。空值不覆盖已有值。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT country, region, exit_ip FROM proxies WHERE proxy_id=?", (proxy_id,)
            ).fetchone()
            if not row:
                return
            self._conn.execute(
                "UPDATE proxies SET country=?, region=?, exit_ip=?, updated_at=? WHERE proxy_id=?",
                (
                    _normalize_country(country) or row["country"],
                    region or row["region"],
                    exit_ip or row["exit_ip"],
                    time.time(),
                    proxy_id,
                ),
            )
            self._conn.commit()

    def clear_cooldown(self, proxy_id: str) -> None:
        """手工把代理移出黑名单（清冷却 + 失败计数），状态回 unknown 待重测。"""
        with self._lock:
            self._conn.execute(
                "UPDATE proxies SET cooldown_until=0, fail_count=0, "
                "status='unknown', updated_at=? WHERE proxy_id=?",
                (time.time(), proxy_id),
            )
            self._conn.commit()

    def record_probe(
        self,
        proxy_id: str,
        result: ProbeResult,
        *,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """把一次探测结果写进健康状态机（连续失败达阈值进冷却/黑名单）。

        - ok：清零 fail_count、清冷却、状态 ok、回填 geo/延迟。
        - fail：fail_count+1；达阈值 → status='cooldown' + cooldown_until=now+冷却；
          未达阈值 → status='fail'。
        返回更新后的 masked 条目。
        """
        ts = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT fail_count FROM proxies WHERE proxy_id=?", (proxy_id,)
            ).fetchone()
            if not row:
                return {}
            if result.ok:
                self._conn.execute(
                    "UPDATE proxies SET status='ok', fail_count=0, cooldown_until=0, "
                    "exit_ip=CASE WHEN ?<>'' THEN ? ELSE exit_ip END, "
                    "country=CASE WHEN ?<>'' THEN ? ELSE country END, "
                    "region=CASE WHEN ?<>'' THEN ? ELSE region END, "
                    "latency_ms=?, last_checked_at=?, updated_at=? WHERE proxy_id=?",
                    (
                        result.exit_ip, result.exit_ip,
                        result.country, result.country,
                        result.region, result.region,
                        float(result.latency_ms), ts, ts, proxy_id,
                    ),
                )
            else:
                fail_count = int(row["fail_count"] or 0) + 1
                if fail_count >= self._fail_threshold and self._cooldown_sec > 0:
                    status = "cooldown"
                    cooldown_until = ts + self._cooldown_sec
                else:
                    status = "fail"
                    cooldown_until = 0
                self._conn.execute(
                    "UPDATE proxies SET status=?, fail_count=?, cooldown_until=?, "
                    "last_checked_at=?, updated_at=? WHERE proxy_id=?",
                    (status, fail_count, cooldown_until, ts, ts, proxy_id),
                )
            self._conn.commit()
        return self.get(proxy_id, mask=True) or {}

    async def test(
        self,
        proxy_id: str,
        timeout: float = 8.0,
        *,
        probe_fn: Optional[ProbeFn] = None,
    ) -> bool:
        """验活：真握手探测（可注入 ``probe_fn``），失败/无探测器回落 TCP 可达。

        结果一律经 ``record_probe`` 进健康状态机（含黑名单冷却）。
        """
        entry = self.get(proxy_id, mask=False)
        if not entry:
            return False

        result: Optional[ProbeResult] = None
        probe = probe_fn or self._default_probe
        try:
            result = await probe(entry)
        except Exception as exc:  # noqa: BLE001  探测器抛错也当「探测器不可用」→ 回落
            result = None
            _ = exc

        if result is None:
            # 探测器不可用 → 回落 TCP 可达（只证明端口开着，拿不到 geo）。
            tcp_ok = await self._tcp_reachable(entry["host"], int(entry["port"]), timeout)
            result = ProbeResult(ok=tcp_ok, error="" if tcp_ok else "tcp unreachable")

        self.record_probe(proxy_id, result)
        return bool(result.ok)

    async def _default_probe(self, proxy: Dict[str, Any]) -> Optional[ProbeResult]:
        return await default_proxy_probe(proxy, url=self._probe_url)

    @staticmethod
    async def _tcp_reachable(host: str, port: int, timeout: float) -> bool:
        try:
            fut = asyncio.open_connection(host, int(port))
            reader, writer = await asyncio.wait_for(fut, timeout=timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except Exception:
            return False

    def pick_available(
        self,
        *,
        kind: Optional[str] = None,
        country: Optional[str] = None,
        residential_only: bool = False,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """挑一条**可用**代理：未被占用、不在冷却、优先 status='ok'。

        可按 ``kind`` / ``country`` 过滤，``residential_only`` 只挑住宅/移动 IP（真防关联）。
        用于「自动分配一号一 IP」——登录若未显式指定代理时兜底挑一条干净出口。
        """
        ts = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM proxies WHERE assigned_account='' AND cooldown_until<=? "
                # 优先 ok → 最近验活过（新鲜）→ 最早添加（确定性兜底，避免同状态下顺序不定）
                "ORDER BY (status='ok') DESC, last_checked_at DESC, created_at ASC",
                (ts,),
            ).fetchall()
        for r in rows:
            entry = self._row(r, mask=True, now=ts)
            if entry.get("status") == "fail":
                continue
            if kind and entry.get("kind") != str(kind).lower():
                continue
            if country and _normalize_country(entry.get("country") or "") != _normalize_country(country):
                continue
            if residential_only and not entry.get("is_residential"):
                continue
            return entry
        return None

    def health_summary(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """看板用聚合：总数/健康/冷却/占用/住宅占比。零依赖，读时计算。"""
        items = self.list()
        ts = time.time() if now is None else now
        total = len(items)
        healthy = sum(1 for x in items if x.get("healthy"))
        in_cooldown = sum(1 for x in items if (float(x.get("cooldown_until") or 0) > ts))
        assigned = sum(1 for x in items if x.get("assigned"))
        residential = sum(1 for x in items if x.get("is_residential"))
        return {
            "total": total,
            "healthy": healthy,
            "in_cooldown": in_cooldown,
            "assigned": assigned,
            "residential": residential,
            "residential_pct": round(residential / total * 100, 1) if total else 0.0,
        }


_pool: Optional[ProxyPool] = None
_pool_lock = threading.Lock()


def get_proxy_pool(db_path: Optional[Path] = None) -> ProxyPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                path = Path(db_path) if db_path else Path("config/proxy_pool.db")
                _pool = ProxyPool(path)
    return _pool
