"""平台账号注册表（M1）。

承载「多登录方式并存」所需的账号持久化：每个账号记住自己的 ``mode``
（protocol / web / device）、绑定的代理与指纹（防关联），供账号池编排器在重启后
用正确的 worker 类型把它拉起。

设计：独立 SQLite（默认 ``config/account_registry.db``），线程安全，幂等 migration
（``executescript(_DDL)`` + ALTER 列表，已存在即忽略），与 ``src/inbox/store.py`` 风格一致。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS platform_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    account_id      TEXT NOT NULL,
    mode            TEXT NOT NULL DEFAULT 'device',
    label           TEXT NOT NULL DEFAULT '',
    proxy_id        TEXT NOT NULL DEFAULT '',
    fingerprint_id  TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    business_line   TEXT NOT NULL DEFAULT '',
    meta_json       TEXT NOT NULL DEFAULT '{}',
    created_at      REAL NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL DEFAULT 0,
    last_online_at  REAL NOT NULL DEFAULT 0,
    UNIQUE(platform, account_id)
);
CREATE INDEX IF NOT EXISTS idx_platform_accounts_plat
    ON platform_accounts(platform, status);
"""

# 预留 ALTER 迁移位（新增列集中于此，已存在即忽略）
_MIGRATIONS: List[str] = [
    # 融合实例 P1：账号业务线标签（translation / companion / ''=未标注）。
    # 消费方：autodraft 账号级档位封顶（translation → review）、proactive 过滤。
    "ALTER TABLE platform_accounts ADD COLUMN business_line TEXT NOT NULL DEFAULT ''",
]

VALID_STATUS = ("pending", "online", "offline", "removed")

# 业务线合法值（'' = 未标注，不封顶不过滤 = 旧行为）
VALID_BUSINESS_LINES = ("", "translation", "companion")


class AccountRegistry:
    """平台账号注册表（线程安全 SQLite 封装）。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, timeout=10
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            for _sql in _MIGRATIONS:
                try:
                    self._conn.execute(_sql)
                except Exception:
                    pass
            self._conn.commit()

    @staticmethod
    def _decode_meta(meta_json: Any) -> Dict[str, Any]:
        """meta_json 文本 → 解密后的 dict（坏 JSON/非 dict → {}）。"""
        try:
            meta = json.loads(meta_json or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        # N3：读出时解密 meta 敏感字段（session_string 等）；旧明文行透传不破
        try:
            from src.integrations.registry_crypto import decrypt_meta
            meta = decrypt_meta(meta)
        except Exception:
            pass
        return meta

    @classmethod
    def _row_to_dict(cls, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["meta"] = cls._decode_meta(d.pop("meta_json", "{}"))
        return d

    @staticmethod
    def _meta_json(meta: Optional[Dict[str, Any]]) -> str:
        """N3：写盘前加密 meta 敏感字段再 json 序列化（best-effort）。"""
        m = meta or {}
        try:
            from src.integrations.registry_crypto import encrypt_meta
            m = encrypt_meta(m)
        except Exception:
            pass
        return json.dumps(m, ensure_ascii=False)

    def upsert(
        self,
        platform: str,
        account_id: str,
        *,
        mode: Optional[str] = None,
        label: Optional[str] = None,
        proxy_id: Optional[str] = None,
        fingerprint_id: Optional[str] = None,
        status: Optional[str] = None,
        business_line: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        merge_meta: bool = False,
    ) -> Dict[str, Any]:
        """新增或更新账号。**仅覆盖显式传入（非 None）的字段**，其余沿用既有值，
        避免「只想改状态」的调用把 mode/label 等清掉。

        ``meta`` 的覆盖语义分两档：
        - ``merge_meta=False``（默认，向后兼容）：**整块替换**——调用方须自行
          read-merge-write，否则会清掉既有键。适用于「读出改完写回」或显式删键。
        - ``merge_meta=True``：**锁内原子浅合并**——只更新传入的键，其余键
          （persona_id / auto_reply / banned / self_* / session_string…）原样保留。
          登录持久化等「只想登记自己那一两个键」的调用**必须**用这档：2026-07-23
          生产事故＝baileys 重登 ``meta={"baileys_login_id"}`` 整块覆盖，把 WA 账号
          的人设绑定抹掉 → 新好友回落默认人设+错语言音色。
        """
        platform = str(platform or "").lower()
        account_id = str(account_id or "")
        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM platform_accounts WHERE platform=? AND account_id=?",
                (platform, account_id),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """INSERT INTO platform_accounts
                       (platform, account_id, mode, label, proxy_id, fingerprint_id,
                        status, business_line, meta_json,
                        created_at, updated_at, last_online_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (platform, account_id,
                     mode or "device", label or "", proxy_id or "",
                     fingerprint_id or "", status or "pending",
                     business_line or "",
                     self._meta_json(meta),
                     now, now, now if status == "online" else 0),
                )
            else:
                cur = dict(existing)
                new_mode = mode if mode is not None else cur["mode"]
                new_label = label if label is not None else cur["label"]
                new_proxy = proxy_id if proxy_id is not None else cur["proxy_id"]
                new_fp = (fingerprint_id if fingerprint_id is not None
                          else cur["fingerprint_id"])
                new_status = status if status is not None else cur["status"]
                new_bl = (business_line if business_line is not None
                          else cur.get("business_line", ""))
                if meta is not None and merge_meta:
                    base = self._decode_meta(cur["meta_json"])
                    base.update(meta)
                    new_meta = self._meta_json(base)
                elif meta is not None:
                    new_meta = self._meta_json(meta)
                else:
                    new_meta = cur["meta_json"]
                last_online = (now if new_status == "online"
                               else cur["last_online_at"])
                self._conn.execute(
                    """UPDATE platform_accounts
                       SET mode=?, label=?, proxy_id=?, fingerprint_id=?, status=?,
                           business_line=?, meta_json=?, updated_at=?, last_online_at=?
                       WHERE platform=? AND account_id=?""",
                    (new_mode, new_label, new_proxy, new_fp, new_status,
                     new_bl, new_meta, now, last_online, platform, account_id),
                )
            self._conn.commit()
        _invalidate_business_line_cache(platform, account_id)
        return self.get(platform, account_id) or {}

    def set_business_line(
        self, platform: str, account_id: str, business_line: str
    ) -> bool:
        """打/改账号业务线标签（'' = 清除）。非法值拒绝返回 False。"""
        bl = str(business_line or "").strip().lower()
        if bl not in VALID_BUSINESS_LINES:
            return False
        with self._lock:
            self._conn.execute(
                """UPDATE platform_accounts SET business_line=?, updated_at=?
                   WHERE platform=? AND account_id=?""",
                (bl, time.time(), str(platform or "").lower(),
                 str(account_id or "")),
            )
            self._conn.commit()
        _invalidate_business_line_cache(platform, account_id)
        return True

    def set_status(self, platform: str, account_id: str, status: str) -> None:
        if status not in VALID_STATUS:
            return
        now = time.time()
        with self._lock:
            self._conn.execute(
                """UPDATE platform_accounts SET status=?, updated_at=?,
                       last_online_at=CASE WHEN ?='online' THEN ? ELSE last_online_at END
                   WHERE platform=? AND account_id=?""",
                (status, now, status, now, str(platform or "").lower(),
                 str(account_id or "")),
            )
            self._conn.commit()

    def get(self, platform: str, account_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM platform_accounts WHERE platform=? AND account_id=?",
                (str(platform or "").lower(), str(account_id or "")),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list(
        self, platform: Optional[str] = None, *, include_removed: bool = False
    ) -> List[Dict[str, Any]]:
        q = "SELECT * FROM platform_accounts WHERE 1=1"
        args: List[Any] = []
        if platform:
            q += " AND platform=?"
            args.append(str(platform).lower())
        if not include_removed:
            q += " AND status != 'removed'"
        q += " ORDER BY platform, created_at"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def remove(self, platform: str, account_id: str) -> None:
        self.set_status(platform, account_id, "removed")
        # P4：账号移除时顺手回收其自身头像文件，防静态目录膨胀（best-effort，无硬依赖）
        try:
            from src.integrations.account_self_profile import cleanup_avatar
            cleanup_avatar(platform, account_id)
        except Exception:
            pass
        # 中央凭据池：把该账号占的容量还回去，否则池侧容量缓慢泄漏、
        # 预测虚报「快耗尽」逼运营多注册凭据（后台线程，不阻塞本调用）
        try:
            from src.integrations.credpool_bridge import release_for_account_bg
            release_for_account_bg(platform, account_id)
        except Exception:
            pass

    def delete_row(self, platform: str, account_id: str) -> bool:
        """硬删注册表行（历史账号治理 P0，2026-08-17）。

        与 ``remove()``（软删＝status 置 removed、行保留）互补：本方法把行从
        ``platform_accounts`` 物理删除，专供「彻底删除历史账号」链路
        （``/api/accounts/{pl}/{aid}/purge-history``）在清完会话数据后收尾——
        否则 removed 残行会在账号面板历史区留下「已移除 · 0 会话」死行。
        状态门禁（仅 offline/removed 可删）由路由层把关，注册表原语保持单一职责。
        返回是否真的删掉了一行；附带回收自身头像 + 归还凭据池容量
        （与 ``remove()`` 同一 best-effort 收尾，重复调用无害）。
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM platform_accounts WHERE platform=? AND account_id=?",
                (str(platform or "").lower(), str(account_id or "")),
            )
            self._conn.commit()
            deleted = bool(cur.rowcount)
        if deleted:
            try:
                from src.integrations.account_self_profile import cleanup_avatar
                cleanup_avatar(platform, account_id)
            except Exception:
                pass
            try:
                from src.integrations.credpool_bridge import release_for_account_bg
                release_for_account_bg(platform, account_id)
            except Exception:
                pass
        return deleted


def parse_persona_ids(meta: Optional[Dict[str, Any]]) -> List[str]:
    """账号 meta → 绑定的人设 id 列表（容忍历史存储形态）。

    registry 里 ``persona_ids`` 实际存在三种形态（真实数据实测）：list、
    ``"['su_wan']"`` 这种 **str(list) 字符串**、缺失只有单数 ``persona_id``。
    """
    m = meta or {}
    out: List[str] = []
    single = str(m.get("persona_id") or "").strip()
    if single:
        out.append(single)
    raw = m.get("persona_ids")
    if isinstance(raw, (list, tuple)):
        out.extend(str(x).strip() for x in raw)
    elif isinstance(raw, str):
        for part in raw.strip().strip("[]").split(","):
            p = part.strip().strip("'\"").strip()
            if p:
                out.append(p)
    seen: set = set()
    uniq = []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


#: 「非活跃」状态：登出(offline) / 已删除(removed)。计「能收发消息」的账号时排除。
#: pending（登录在途 / 待编排器拉起）与 online / desktop 一并算活跃——与聊天页
#: ``_registry_active_map`` / ``_merge_orchestrator_status`` 的「活跃账号」定义同口径。
_INACTIVE_STATUSES = frozenset({"offline", "removed"})


def live_accounts_by_platform(*, exclude_offline: bool = True) -> Dict[str, int]:
    """各平台「当前能收发消息的账号数」——渠道接入向导计数 / 头部就绪口径单一源。

    #126（2026-09-01 skuio）：向导 Telegram 行常显「已接 1 个账号」而机器实登多号。
    与人设「应用到」弹窗（#61/#78）同族——**账号真相只在运行时注册表
    ``platform_accounts``**（桌面 QR / 协议登录都 upsert 到这里；config 里没有）。

    与向导既有 ``_accounts_by_platform`` 的关键差别（本函数是收敛后的单一事实源）：
    - ``removed`` 恒不计（软删账号）；
    - ``exclude_offline``（默认 True）：``offline``（登出/离线）**不计**——这是
      「登录登出实时跟随」：登出一个号，向导「已接 N」与头部「能收发 X/7」立即
      各减一，不再把一个躺着登出的号谎报成「已接」。人设「应用到」弹窗刻意保留
      offline（那里是「给哪个号绑人设」，离线号回来仍要能绑），故两处口径按用途
      各自成立，本函数专供「能不能收发」这一问。

    取数失败一律返回空 dict（调用方回落纯配置口径，绝不让向导因此报错）。
    """
    try:
        out: Dict[str, int] = {}
        for row in get_account_registry().list():  # 默认已排除 removed
            if exclude_offline and str(row.get("status") or "") in _INACTIVE_STATUSES:
                continue
            p = str(row.get("platform") or "").lower()
            if p:
                out[p] = out.get(p, 0) + 1
        return out
    except Exception:
        logger.debug("live_accounts_by_platform 读取失败（调用方回落）", exc_info=True)
        return {}


def persona_binding_refs(profile_id: str) -> List[str]:
    """反查：哪些**未移除**账号显式绑定了该人设 → ``["platform:account_id", …]``。

    删除类操作的核查项（误删实锤 2026-07-28：pid=mizuki 的传记库存被当
    「孤儿」清掉，而账号 8438080491 的 registry meta 明明写着
    ``persona_id: mizuki``——「有没有档案/有没有审计」都不是「在不在用」的
    判据，**绑定表才是**）。fail-open：registry 不可用返回 []——护栏失明时
    不挡正常运维，删除本身仍有 force 语义兜底。
    """
    pid = str(profile_id or "").strip()
    if not pid:
        return []
    try:
        reg = get_account_registry()
        return [
            f"{row.get('platform')}:{row.get('account_id')}"
            for row in reg.list()
            if pid in parse_persona_ids(row.get("meta"))
        ]
    except Exception:
        logger.debug("persona_binding_refs 反查失败（fail-open）", exc_info=True)
        return []


_registry: Optional[AccountRegistry] = None
_registry_lock = threading.Lock()


def default_registry_db_path() -> Path:
    """注册表默认路径。分裂布局落数据根，同树布局仍是 ``config/account_registry.db``。"""
    from src.licensing.data_paths import cwd_or_data_file
    return cwd_or_data_file("account_registry.db")


def get_account_registry(db_path: Optional[Path] = None) -> AccountRegistry:
    """进程内单例。首次调用可指定路径，默认同树 ``config/account_registry.db``。"""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                path = Path(db_path) if db_path else default_registry_db_path()
                _registry = AccountRegistry(path)
    return _registry


# ── 业务线热路查询（autodraft 每条入站 / proactive 每 tick 都会问）─────────────
# 30s TTL 进程内缓存；写路径（upsert/set_business_line）即时失效。
# 单例未初始化（如纯单测 / 无编排器部署）→ 返回 ''，绝不在此隐式建库。
_BL_CACHE: Dict[str, tuple] = {}
_BL_CACHE_TTL = 30.0


def _bl_cache_key(platform: str, account_id: str) -> str:
    return f"{str(platform or '').lower()}:{str(account_id or '')}"


def _invalidate_business_line_cache(platform: str, account_id: str) -> None:
    _BL_CACHE.pop(_bl_cache_key(platform, account_id), None)


def peek_account(platform: str, account_id: str) -> Optional[Dict[str, Any]]:
    """只读现有单例查账号行；单例未初始化/异常 → None，**绝不隐式建库**。

    供 web 读路径（会话头「账号已停用」横幅，P1-198 2026-08-05）取
    status/mode——读路径不该有建库副作用（纯单测/无编排器部署里注册表
    本就不存在；与 cached_business_line 同一铁律）。
    """
    try:
        reg = _registry
        if reg is None:
            return None
        return reg.get(platform, account_id)
    except Exception:
        return None


def cached_business_line(platform: str, account_id: str) -> str:
    """账号业务线标签（'' = 未标注）。带 TTL 缓存，任何异常回落 ''。"""
    key = _bl_cache_key(platform, account_id)
    now = time.time()
    hit = _BL_CACHE.get(key)
    if hit is not None and (now - hit[1]) < _BL_CACHE_TTL:
        return hit[0]
    val = ""
    try:
        reg = _registry  # 只读现有单例，不触发建库
        if reg is not None:
            row = reg.get(platform, account_id)
            val = str((row or {}).get("business_line") or "").strip().lower()
    except Exception:
        val = ""
    _BL_CACHE[key] = (val, now)
    return val
