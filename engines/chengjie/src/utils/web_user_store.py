"""Web 管理面板用户存储 — SQLite + PBKDF2 密码哈希"""

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

ROLE_MASTER = "master"
ROLE_ADMIN = "admin"
ROLE_SUPERVISOR = "supervisor"
ROLE_VIEWER = "viewer"
ROLE_AGENT = "agent"

ROLE_LABELS = {
    ROLE_MASTER: "主帐号（全部权限）",
    ROLE_ADMIN: "管理员（编辑权限）",
    ROLE_SUPERVISOR: "主管（坐席+团队看板）",
    ROLE_VIEWER: "只读观察员",
    ROLE_AGENT: "坐席（仅聊天工作台）",
}

# ── UI 模式 ─────────────────────────────────────────────────
UI_MODE_SIMPLE = "simple"
UI_MODE_FULL = "full"

UI_MODE_LABELS = {
    UI_MODE_SIMPLE: "简洁模式",
    UI_MODE_FULL:   "完整模式",
}

# 简洁模式可见页清单的唯一事实源在 src/web/nav_schema.py（SIMPLE_CORE /
# SIMPLE_MORE）。此处曾有一份按 page_key 的重复清单（SIMPLE_MODE_*_PAGES），
# 模板早已不消费且内容过时（含废键 "ch"、缺 workspace 等），2026-08-03 删除
# ——双源清单是静默漂移的温床。
ROLE_DEFAULT_UI_MODE = {
    ROLE_MASTER: UI_MODE_SIMPLE,
    ROLE_ADMIN:  UI_MODE_SIMPLE,
    ROLE_SUPERVISOR: UI_MODE_SIMPLE,
    ROLE_VIEWER: UI_MODE_SIMPLE,
    ROLE_AGENT:  UI_MODE_SIMPLE,
}


def resolve_ui_mode(cookie_val: str, role: str) -> str:
    """Determine effective ui_mode from cookie preference + role default."""
    if cookie_val in (UI_MODE_SIMPLE, UI_MODE_FULL):
        return cookie_val
    return ROLE_DEFAULT_UI_MODE.get(role, UI_MODE_SIMPLE)


# ── 页面与写入权限 ──────────────────────────────────────────
PAGE_PERMISSIONS = {
    "dash":       {ROLE_MASTER, ROLE_ADMIN, ROLE_SUPERVISOR, ROLE_VIEWER},
    "tpl":        {ROLE_MASTER, ROLE_ADMIN},
    "ch":         {ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER},
    "strategies": {ROLE_MASTER, ROLE_ADMIN},
    "audit":      {ROLE_MASTER, ROLE_ADMIN, ROLE_SUPERVISOR, ROLE_VIEWER},
    "diff":       {ROLE_MASTER, ROLE_ADMIN},
    "logs":       {ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER},
    "analytics":  {ROLE_MASTER, ROLE_ADMIN, ROLE_SUPERVISOR, ROLE_VIEWER},
    "help":       {ROLE_MASTER, ROLE_ADMIN, ROLE_SUPERVISOR, ROLE_VIEWER},
    # 用户管理：admin 也可管（层级细分交由 assignable_roles/can_manage_target）
    "users":      {ROLE_MASTER, ROLE_ADMIN},
    "settings":   {ROLE_MASTER},
    "import":     {ROLE_MASTER},
    "export":     {ROLE_MASTER},
    "cases":      {ROLE_MASTER, ROLE_ADMIN, ROLE_SUPERVISOR, ROLE_VIEWER},
    "episodic":   {ROLE_MASTER, ROLE_ADMIN, ROLE_SUPERVISOR, ROLE_VIEWER},
    # 客户安全预警（原危机审计）：高敏内容（客户原话摘要）——只给主管/合规看，viewer 不放
    # （2026-09-05 #185，隐私顾虑靠角色门 + 摘要 ≤120 字，而不是靠关掉留痕）
    "crisis_audit": {ROLE_MASTER, ROLE_ADMIN, ROLE_SUPERVISOR},
    "care":       {ROLE_MASTER, ROLE_ADMIN, ROLE_SUPERVISOR, ROLE_VIEWER},
    "monetization": {ROLE_MASTER, ROLE_ADMIN},   # 营收/变现数据：仅主帐号+管理员
    "line_rpa":   {ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER},
    "personas":   {ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER},
    # 坐席工作台（统一收件箱）：master/admin/supervisor/agent 可用（viewer 只读不接管）
    "workspace":  {ROLE_MASTER, ROLE_ADMIN, ROLE_SUPERVISOR, ROLE_AGENT},
}

_KNOWN_ROLES = {ROLE_MASTER, ROLE_ADMIN, ROLE_SUPERVISOR, ROLE_VIEWER, ROLE_AGENT}
_DOMAIN_PAGE_KEYS: Set[str] = set()   # 由域清单注册的键（可被后续注册覆写，核心键不可）


def register_domain_page_permissions(pages: Any) -> Dict[str, Set[str]]:
    """把域清单 ``web.pages[].roles`` 注册进 PAGE_PERMISSIONS（域包声明自己页面的可见角色）。

    规则：核心表已有的键不动（核心口径优先，域不能放宽核心页）；未声明 roles 或全是未知
    角色名的页不注册（保持「无条目 → 仅 master」的兜底）；master 恒在允许集内。
    返回本次实际注册的 {page_key: roles}。
    """
    registered: Dict[str, Set[str]] = {}
    for page in (pages or []):
        if not isinstance(page, dict):
            continue
        key = str(page.get("key") or "").strip()
        roles_raw = page.get("roles")
        if not key or not isinstance(roles_raw, (list, tuple, set)):
            continue
        if key in PAGE_PERMISSIONS and key not in _DOMAIN_PAGE_KEYS:
            continue
        roles = {str(r).strip().lower() for r in roles_raw} & _KNOWN_ROLES
        if not roles:
            continue
        roles.add(ROLE_MASTER)
        PAGE_PERMISSIONS[key] = roles
        _DOMAIN_PAGE_KEYS.add(key)
        registered[key] = roles
    return registered


WRITE_PERMISSIONS = {
    "edit_template":  {ROLE_MASTER, ROLE_ADMIN},
    "edit_channel":   {ROLE_MASTER, ROLE_ADMIN},
    "edit_strategy":  {ROLE_MASTER, ROLE_ADMIN},
    "episodic_memory": {ROLE_MASTER, ROLE_ADMIN},
    "manage_users":   {ROLE_MASTER, ROLE_ADMIN},
    "manage_settings":{ROLE_MASTER},
    "import_export":  {ROLE_MASTER},
    "edit_persona":   {ROLE_MASTER, ROLE_ADMIN},
    "manage_ops":     {ROLE_MASTER, ROLE_ADMIN},  # E2：确认/指派运维事件
}


# ── L3 按人权限覆写（能力级，正交于 PAGE/WRITE 的页面级角色权限）────────────
# 能力权限注册表（L3 按人覆写的全集）：v1 只收录有真实执法点的四项
# （执法点在 translate/voice/send 路由，另一条线接线）；加新键必须同时有执法点，
# 否则矩阵变装饰品——这是本表的存在纪律。
# 值语义：domain=编辑器分组；zh=能力名（仅注释/后台参考，前端文案走 i18n pack）；
# roles=角色默认允许集（不在集内的角色默认禁止，可被 allow 覆写拉回）。
# dict 定义序即 API/编辑器展示序，勿按字母重排。
PERM_REGISTRY: Dict[str, Dict[str, Any]] = {
    "chat.send_text":  {"domain": "chat", "zh": "发送文字",
                        "roles": {ROLE_MASTER, ROLE_ADMIN, ROLE_SUPERVISOR, ROLE_AGENT}},
    "chat.send_media": {"domain": "chat", "zh": "发送图片/媒体",
                        "roles": {ROLE_MASTER, ROLE_ADMIN, ROLE_SUPERVISOR, ROLE_AGENT}},
    "chat.send_voice": {"domain": "chat", "zh": "语音合成与发送",
                        "roles": {ROLE_MASTER, ROLE_ADMIN, ROLE_SUPERVISOR, ROLE_AGENT}},
    "ai.translate":    {"domain": "ai", "zh": "手动翻译",
                        "roles": {ROLE_MASTER, ROLE_ADMIN, ROLE_SUPERVISOR, ROLE_AGENT}},
}


def default_perm_allowed(role: str, perm: str) -> bool:
    """角色默认判定。未注册 perm → True（fail-open：执法点面对未知/新键绝不误拦）。"""
    entry = PERM_REGISTRY.get(str(perm or ""))
    if entry is None:
        return True
    return str(role or "") in entry["roles"]


def parse_perms(perms_json: Any) -> Dict[str, Set[str]]:
    """解析 web_users.perms_json → ``{"allow": set, "deny": set}``。

    空串/None/坏 JSON/非法结构 → 双空（纯继承角色默认）。绝不抛。
    不在此处过滤未注册键——resolve 侧对未注册键本就 fail-open，留着原样
    可让「注册表回滚后残留的旧覆写」在观测里可见而非静默蒸发。
    """
    empty: Dict[str, Set[str]] = {"allow": set(), "deny": set()}
    raw = perms_json
    if not raw:
        return empty
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if not isinstance(data, dict):
            return {"allow": set(), "deny": set()}
        out: Dict[str, Set[str]] = {"allow": set(), "deny": set()}
        for bucket in ("allow", "deny"):
            v = data.get(bucket)
            if isinstance(v, list):
                out[bucket] = {str(x) for x in v if isinstance(x, str) and x}
        return out
    except Exception:
        return {"allow": set(), "deny": set()}


def resolve_user_perm(store: Any, username: str, role: str, perm: str) -> bool:
    """按人能力权限单点判定（执法点唯一入口，绝不抛）。

    顺序：master 恒 True（防自锁）→ perm 未注册 → True（fail-open）→
    username 空 / store 缺 / 行缺失 / 读取异常 → 角色默认 →
    deny 优先 > allow > 角色默认。
    """
    r = str(role or "")
    p = str(perm or "")
    if r == ROLE_MASTER:
        return True
    if p not in PERM_REGISTRY:
        return True
    u = str(username or "").strip()
    if not u or store is None:
        return default_perm_allowed(r, p)
    try:
        row = store.get_user(u)
    except Exception:
        row = None
    if not isinstance(row, dict):
        return default_perm_allowed(r, p)
    overrides = parse_perms(row.get("perms_json"))
    if p in overrides["deny"]:
        return False
    if p in overrides["allow"]:
        return True
    return default_perm_allowed(r, p)


# ── 角色分层（用户管理的「谁能管谁 / 谁能发什么角色」单一事实源）─────────────
def assignable_roles(actor_role: str) -> List[str]:
    """操作者可分配的角色清单（层级语义：master > admin > 其余）。

    - master：可发 admin/supervisor/agent/viewer（唯独不能再造 master）；
    - admin：只可发 supervisor/agent/viewer（不得造平级 admin，防横向扩权）；
    - 其它角色：不具用户管理能力，返回空表。
    """
    if actor_role == ROLE_MASTER:
        return [ROLE_ADMIN, ROLE_SUPERVISOR, ROLE_AGENT, ROLE_VIEWER]
    if actor_role == ROLE_ADMIN:
        return [ROLE_SUPERVISOR, ROLE_AGENT, ROLE_VIEWER]
    return []


def can_manage_target(actor_role: str, target_role: str) -> bool:
    """操作者能否管理目标账号（角色变更/禁用/删除/设额度共用一个判定）。

    - master 行任何人不可管（含 master 自己——密码自改走 change-password 不经此）；
    - master 可管其余任何角色；admin 只可管 supervisor/agent/viewer（不得动平级）。
    """
    if target_role == ROLE_MASTER:
        return False
    if actor_role == ROLE_MASTER:
        return True
    if actor_role == ROLE_ADMIN:
        return target_role in (ROLE_SUPERVISOR, ROLE_AGENT, ROLE_VIEWER)
    return False


def _hash_pw(password: str, salt: bytes = None) -> tuple:
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return salt, dk


class WebUserStore:
    _DDL = """
    CREATE TABLE IF NOT EXISTS web_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        pw_salt BLOB NOT NULL,
        pw_hash BLOB NOT NULL,
        role TEXT NOT NULL DEFAULT 'viewer',
        display_name TEXT NOT NULL DEFAULT '',
        lang TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        last_login TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        monthly_char_quota INTEGER NOT NULL DEFAULT 0,
        quota_alert_pct INTEGER NOT NULL DEFAULT 80,
        perms_json TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS web_sessions (
        jti        TEXT PRIMARY KEY,
        username   TEXT NOT NULL,
        role       TEXT NOT NULL DEFAULT '',
        ip         TEXT DEFAULT '',
        user_agent TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        last_seen  TEXT NOT NULL,
        revoked    INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_ws_user ON web_sessions(username);
    """

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.executescript(self._DDL)
        # 迁移：为既有库补 lang 列（语言跟人走；CREATE IF NOT EXISTS 不会给旧表补列）
        try:
            self._conn.execute(
                "ALTER TABLE web_users ADD COLUMN lang TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass  # 列已存在（新库由 _DDL 建好 / 旧库已迁过）
        # 迁移：坐席月度字符额度两列（0=不限；alert_pct=预警阈值百分比）
        try:
            self._conn.execute(
                "ALTER TABLE web_users ADD COLUMN monthly_char_quota INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # 列已存在
        try:
            self._conn.execute(
                "ALTER TABLE web_users ADD COLUMN quota_alert_pct INTEGER NOT NULL DEFAULT 80"
            )
        except sqlite3.OperationalError:
            pass  # 列已存在
        # 迁移：L3 按人权限覆写（''=纯继承角色默认；JSON {"allow":[...],"deny":[...]}）
        try:
            self._conn.execute(
                "ALTER TABLE web_users ADD COLUMN perms_json TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass  # 列已存在
        # 迁移：目标达成等业务事件的坐席 Telegram 通知号（P2 2026-08-18；
        # ''=未绑定=只推管理员渠道。纯 chat_id 非密钥，负数=群）
        try:
            self._conn.execute(
                "ALTER TABLE web_users ADD COLUMN notify_tg_chat_id TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass  # 列已存在
        self._conn.commit()

    # ── Session 管理 ──────────────────────────────────────────
    # #186（2026-09-05）僵尸会话：桌面壳每次启动走 auth_token 直登 → 每次 INSERT 一行、
    # 旧行永不 revoke（壳从不 /logout），用户管理页「活跃会话」一天长一条。两道回收：
    #   ① 同设备换新（create_session(replace_same_device=True)）：同 (username, ip, ua)
    #      的旧活跃行随新登录作废，只留最近一条——只在令牌直登路径开（浏览器多机同 NAT
    #      同 UA 的账号密码登录不敢一刀切）；
    #   ② 空闲过期：last_seen 超过 SESSION_IDLE_DAYS 的会话 touch 拒绝、列表不显、懒标
    #      revoked；超过 SESSION_PRUNE_DAYS 的行在登录时物理清理。
    SESSION_IDLE_DAYS = 7
    SESSION_PRUNE_DAYS = 30

    @staticmethod
    def _cutoff(days: float) -> str:
        """与 created_at/last_seen 同格式（本地时间字串，可直接字典序比较）。"""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))

    def _expire_idle_locked(self) -> int:
        """空闲过期懒标记（调用方持锁）。返回本次标记条数。"""
        cur = self._conn.execute(
            "UPDATE web_sessions SET revoked=1 WHERE revoked=0 AND last_seen < ?",
            (self._cutoff(self.SESSION_IDLE_DAYS),),
        )
        return int(cur.rowcount or 0)

    def create_session(self, username: str, role: str, ip: str = "",
                       user_agent: str = "", *, replace_same_device: bool = False) -> str:
        """创建新 session 记录，返回 jti（唯一 session 标识符）。

        ``replace_same_device=True``：同 (username, ip, user_agent) 的其余活跃会话
        随本次登录作废（同一台设备重复启动只保留最近一条）。
        """
        import uuid as _uuid
        jti = _uuid.uuid4().hex
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        ua = (user_agent or "")[:200]
        with self._lock:
            self._conn.execute(
                "INSERT INTO web_sessions(jti,username,role,ip,user_agent,created_at,last_seen)"
                " VALUES(?,?,?,?,?,?,?)",
                (jti, username, role, ip[:64], ua, now, now),
            )
            if replace_same_device:
                self._conn.execute(
                    "UPDATE web_sessions SET revoked=1 WHERE revoked=0 AND jti<>? "
                    "AND username=? AND ip=? AND user_agent=?",
                    (jti, username, ip[:64], ua),
                )
            try:
                self._expire_idle_locked()
                self._conn.execute(
                    "DELETE FROM web_sessions WHERE last_seen < ?",
                    (self._cutoff(self.SESSION_PRUNE_DAYS),),
                )
            except sqlite3.OperationalError:
                pass  # 回收失败不阻断登录
            self._conn.commit()
        return jti

    def mark_login(self, username: str, *, fallback_role: str = "") -> bool:
        """记 last_login（令牌直登不走 verify()，此前主帐号永远「登录：从未」）。

        ``username`` 不存在且给了 ``fallback_role`` → 记到该角色的账号上（令牌直登的
        用户名是常量 "admin"，主帐号实际用户名可能不是它）。返回是否更新了行。
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE web_users SET last_login=? WHERE username=?", (now, username))
            n = int(cur.rowcount or 0)
            if not n and fallback_role:
                cur = self._conn.execute(
                    "UPDATE web_users SET last_login=? WHERE role=?", (now, fallback_role))
                n = int(cur.rowcount or 0)
            self._conn.commit()
        return n > 0

    def touch_session(self, jti: str) -> bool:
        """更新 session 最后活跃时间，返回该 session 是否有效（空闲超 SESSION_IDLE_DAYS 视为失效）"""
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT revoked, last_seen FROM web_sessions WHERE jti=?", (jti,)
                ).fetchone()
                if not row or row["revoked"]:
                    return False
                if str(row["last_seen"] or "") and str(row["last_seen"]) < self._cutoff(self.SESSION_IDLE_DAYS):
                    self._conn.execute(
                        "UPDATE web_sessions SET revoked=1 WHERE jti=?", (jti,))
                    self._conn.commit()
                    return False
                self._conn.execute(
                    "UPDATE web_sessions SET last_seen=? WHERE jti=?",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), jti),
                )
                self._conn.commit()
            except (sqlite3.OperationalError, sqlite3.InterfaceError):
                try:
                    self._reconnect()
                    return True
                except Exception:
                    return True
        return True

    def _reconnect(self):
        """Re-open the SQLite connection after an InterfaceError."""
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def revoke_session(self, jti: str):
        """撤销指定 session（强制下线）"""
        with self._lock:
            self._conn.execute(
                "UPDATE web_sessions SET revoked=1 WHERE jti=?", (jti,)
            )
            self._conn.commit()

    def revoke_all_sessions(self, username: str = None):
        """撤销所有 session 或指定用户的所有 session"""
        with self._lock:
            if username:
                self._conn.execute(
                    "UPDATE web_sessions SET revoked=1 WHERE username=?", (username,)
                )
            else:
                self._conn.execute("UPDATE web_sessions SET revoked=1")
            self._conn.commit()

    def list_sessions(self, include_revoked: bool = False) -> List[Dict]:
        """列出所有活跃 session（按最后活跃时间倒序；空闲过期的先懒标 revoked 再列）"""
        sql = (
            "SELECT jti,username,role,ip,user_agent,created_at,last_seen,revoked "
            "FROM web_sessions "
            + ("" if include_revoked else "WHERE revoked=0 ")
            + "ORDER BY last_seen DESC LIMIT 100"
        )
        with self._lock:
            try:
                if self._expire_idle_locked():
                    self._conn.commit()
            except sqlite3.OperationalError:
                pass
            rows = self._conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def last_session_login_map(self) -> Dict[str, str]:
        """{username: 最近一次会话 created_at}（含已作废行）——给用户卡「登录」栏兜底：
        令牌直登历史上不记 last_login，主帐号明明天天在用却显示「从未」（#186）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT username, MAX(created_at) AS ts FROM web_sessions GROUP BY username"
            ).fetchall()
        return {str(r["username"]): str(r["ts"] or "") for r in rows if r["ts"]}

    def cleanup_old_sessions(self, days: int = 30):
        """清理超过 N 天未活跃的 session"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM web_sessions WHERE last_seen < datetime('now', ?)",
                (f"-{days} days",),
            )
            self._conn.commit()

    def _ensure_master(self, username: str, password: str):
        """确保至少存在一个主帐号；若已存在则跳过"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM web_users WHERE role=?", (ROLE_MASTER,)
            ).fetchone()
        if row:
            return
        self.create_user(username, password, ROLE_MASTER, display_name="管理员")

    def create_user(self, username: str, password: str, role: str = ROLE_VIEWER,
                    display_name: str = "") -> Optional[Dict]:
        if role not in ROLE_LABELS:
            return None
        salt, hashed = _hash_pw(password)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO web_users (username, pw_salt, pw_hash, role, display_name, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (username, salt, hashed, role, display_name or username,
                     time.strftime("%Y-%m-%d %H:%M:%S"))
                )
                self._conn.commit()
            return self.get_user(username)
        except sqlite3.IntegrityError:
            return None

    def verify(self, username: str, password: str) -> Optional[Dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM web_users WHERE username=? AND enabled=1", (username,)
            ).fetchone()
            if not row:
                return None
            salt = row["pw_salt"]
            expected = row["pw_hash"]
            _, actual = _hash_pw(password, salt)
            if not hmac.compare_digest(actual, expected):
                return None
            self._conn.execute(
                "UPDATE web_users SET last_login=? WHERE id=?",
                (time.strftime("%Y-%m-%d %H:%M:%S"), row["id"])
            )
            self._conn.commit()
            return dict(row)

    def get_user(self, username: str) -> Optional[Dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM web_users WHERE username=?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, role, display_name, enabled, "
                "monthly_char_quota, quota_alert_pct, perms_json, notify_tg_chat_id "
                "FROM web_users WHERE id=?",
                (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> List[Dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, username, role, display_name, created_at, last_login, enabled, "
                "monthly_char_quota, quota_alert_pct, perms_json, notify_tg_chat_id "
                "FROM web_users ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_user(self, user_id: int, role: str = None, enabled: bool = None,
                    password: str = None, display_name: str = None,
                    monthly_char_quota=None, quota_alert_pct=None,
                    notify_tg_chat_id=None) -> bool:
        sets, params = [], []
        if role and role in ROLE_LABELS:
            sets.append("role=?"); params.append(role)
        if enabled is not None:
            sets.append("enabled=?"); params.append(1 if enabled else 0)
        if password:
            salt, hashed = _hash_pw(password)
            sets.append("pw_salt=?"); params.append(salt)
            sets.append("pw_hash=?"); params.append(hashed)
        if display_name is not None:
            sets.append("display_name=?"); params.append(display_name)
        if monthly_char_quota is not None:
            try:
                _q = int(monthly_char_quota)
            except (TypeError, ValueError):
                _q = 0
            sets.append("monthly_char_quota=?"); params.append(max(0, _q))  # 负值视为 0=不限
        if quota_alert_pct is not None:
            try:
                _p = int(quota_alert_pct)
            except (TypeError, ValueError):
                _p = 80
            sets.append("quota_alert_pct=?"); params.append(min(100, max(50, _p)))  # 夹 [50,100]
        if notify_tg_chat_id is not None:
            # 纯 chat_id 白名单：可选负号 + 1..20 位数字；空串=解绑。其余一律忽略
            # （静默存脏值会让「绑定了却收不到」无从排查——宁可不写）。
            _c = str(notify_tg_chat_id).strip()
            if _c == "" or (_c.lstrip("-").isdigit()
                            and len(_c.lstrip("-")) <= 20
                            and _c.count("-") <= (1 if _c.startswith("-") else 0)):
                sets.append("notify_tg_chat_id=?"); params.append(_c)
        if not sets:
            return False
        params.append(user_id)
        with self._lock:
            self._conn.execute(f"UPDATE web_users SET {','.join(sets)} WHERE id=?", params)
            self._conn.commit()
        return True

    def set_user_perms(self, user_id: int, allow: list, deny: list) -> bool:
        """写 L3 按人权限覆写（整单校验，False=拒绝且零写入）。

        - 键必须 ∈ PERM_REGISTRY（未注册键拒绝——写进去也不会被执法，存了只会
          让编辑器出幽灵行）；
        - allow ∩ deny 非空 → **直接拒绝**（不做「deny 留、allow 剔」的静默修正：
          保存成功但落库内容 ≠ 前端所见，比一次 400 更伤信任；前端三档 select
          天然不会产出冲突，冲突只能来自脏调用方，就该被顶回去）；
        - 双空 → 存 ''（回归纯继承角色默认，编辑器「全部继承」即清除覆写）。
        """
        try:
            a = [str(x) for x in (allow or []) if isinstance(x, str) and x]
            d = [str(x) for x in (deny or []) if isinstance(x, str) and x]
        except TypeError:
            return False
        a = list(dict.fromkeys(a))  # 去重保序（展示序=注册表序由读侧保证）
        d = list(dict.fromkeys(d))
        for k in a + d:
            if k not in PERM_REGISTRY:
                return False
        if set(a) & set(d):
            return False
        payload = ""
        if a or d:
            payload = json.dumps({"allow": a, "deny": d},
                                 ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            cur = self._conn.execute(
                "UPDATE web_users SET perms_json=? WHERE id=?", (payload, user_id)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def set_lang(self, username: str, lang: str) -> bool:
        """持久化坐席 UI 语言偏好（语言跟人走）。仅接受 UI_LANGS 白名单；
        空串=清除偏好（回到「跟随系统」：登录不再回填 cookie，中间件按
        Accept-Language 推断）。其它值一律拒绝。"""
        try:
            from src.web.i18n_packs import UI_LANGS  # 单一事实源（xlate P3）；惰性导入防环
        except Exception:
            UI_LANGS = ("zh", "en", "vi", "th", "id", "zh_hant")  # 兜底与表同步
        if lang != "" and lang not in UI_LANGS:
            return False
        with self._lock:
            self._conn.execute(
                "UPDATE web_users SET lang=? WHERE username=?", (lang, username)
            )
            self._conn.commit()
        return True

    def delete_user(self, user_id: int) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT role FROM web_users WHERE id=?", (user_id,)).fetchone()
            if not row or row["role"] == ROLE_MASTER:
                return False
            self._conn.execute("DELETE FROM web_users WHERE id=?", (user_id,))
            self._conn.commit()
        return True

    def can_access_page(self, role: str, page_key: str) -> bool:
        allowed = PAGE_PERMISSIONS.get(page_key)
        if allowed is None:
            return role == ROLE_MASTER
        return role in allowed

    def can_write(self, role: str, permission: str) -> bool:
        allowed = WRITE_PERMISSIONS.get(permission)
        if allowed is None:
            return role == ROLE_MASTER
        return role in allowed

    def user_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) c FROM web_users").fetchone()
        return row["c"] if row else 0
