"""表情包（贴纸）注册表（SQLite）＋落盘根 + 跨平台发送方案（2026-08-17 主线）。

坐席可发的贴纸素材库：官方包（随产品播种）+ 自建包（上传/收藏入站贴纸）。
二进制落 ``src/web/static/sticker_packs/<pack_id>/``（/static 直服，供面板网格与
消息气泡渲染），元数据落 DB（与 persona_media_store 同型：线程安全单连接 +
WAL + ``:memory:`` 供单测）。

- 一行 = 一张可发送贴纸；``file_path`` 空 + ``line_package_id/line_sticker_id``
  非空 = LINE 官方商店贴纸（纯 ID 映射，无本地文件，仅 LINE 会话可原生发送）。
- 发送编排**不在本模块**（那在 sticker_routes），但「该平台怎么发」的纯函数
  判定 :func:`resolve_send_plan` 放这里——路由与单测同源。
- 落盘根必须 ``Path(__file__)`` 锚定绝对路径（static_asset_paths 铁律：进程
  CWD 是实例数据根，相对路径写出去的文件 /static 永远服务不到）。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 默认库路径（与其它 config/*.db 同处=实例数据根；main/路由可 configure 显式指定）。
DEFAULT_DB_PATH = "config/sticker_packs.db"

PACK_OFFICIAL = "official"
PACK_CUSTOM = "custom"
_VALID_KINDS = (PACK_OFFICIAL, PACK_CUSTOM)

# 「收藏」包的固定 id：入站贴纸一键收藏的缺省去处（首次收藏时自动建）。
COLLECTED_PACK_ID = "collected"

_DDL = """
CREATE TABLE IF NOT EXISTS sticker_packs (
    id          TEXT NOT NULL PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'custom',
    cover_url   TEXT NOT NULL DEFAULT '',
    sort        INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stickers (
    id              TEXT NOT NULL PRIMARY KEY,
    pack_id         TEXT NOT NULL,
    file_path       TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL DEFAULT '',
    emoji_tag       TEXT NOT NULL DEFAULT '',
    keywords        TEXT NOT NULL DEFAULT '[]',
    animated        INTEGER NOT NULL DEFAULT 0,
    bytes           INTEGER NOT NULL DEFAULT 0,
    width           INTEGER NOT NULL DEFAULT 0,
    height          INTEGER NOT NULL DEFAULT 0,
    sha256          TEXT NOT NULL DEFAULT '',
    line_package_id TEXT NOT NULL DEFAULT '',
    line_sticker_id TEXT NOT NULL DEFAULT '',
    sort            INTEGER NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 1,
    hits            INTEGER NOT NULL DEFAULT 0,
    last_used_at    REAL NOT NULL DEFAULT 0,
    created_by      TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_stk_pack   ON stickers(pack_id, enabled, sort);
CREATE INDEX IF NOT EXISTS idx_stk_sha    ON stickers(pack_id, sha256);
CREATE INDEX IF NOT EXISTS idx_stk_recent ON stickers(last_used_at);
"""

_PACK_UPDATABLE = {"title", "cover_url", "sort", "enabled"}
_STK_UPDATABLE = {"emoji_tag", "keywords", "sort", "enabled"}


# ── 落盘根（/static 直服）────────────────────────────────────────────────────

_ROOT_OVERRIDE: Optional[Path] = None
_STATIC_SUBDIR = "sticker_packs"


def configure_sticker_root(path: Optional[Any]) -> None:
    """覆写落盘根（单测指向 tmp；传 None 恢复默认）。"""
    global _ROOT_OVERRIDE
    _ROOT_OVERRIDE = Path(path) if path else None


def sticker_root() -> Path:
    """贴纸文件落盘根：``src/web/static/sticker_packs``（绝对路径，按需创建交调用方）。"""
    if _ROOT_OVERRIDE is not None:
        return _ROOT_OVERRIDE
    return Path(__file__).resolve().parents[1] / "web" / "static" / _STATIC_SUBDIR


def sticker_url(pack_id: str, filename: str) -> str:
    """浏览器可加载的 /static URL（与 :func:`sticker_root` 目录布局一一对应）。"""
    return f"/static/{_STATIC_SUBDIR}/{pack_id}/{filename}"


def safe_pack_dir(pack_id: str) -> str:
    """pack id 收敛为安全目录名（防路径穿越；与 persona_media 的 _safe_pid 同哲学）。"""
    import re
    s = re.sub(r"[^A-Za-z0-9_-]", "_", str(pack_id or ""))[:64]
    return s or "pack"


# ── 跨平台发送方案（纯函数——路由与单测同源）──────────────────────────────────

def sibling_path(file_path: str, ext: str) -> str:
    """规范化产物的兄弟文件路径（``x.webp`` → ``x.png``/``x.gif``）。"""
    root, _ = os.path.splitext(str(file_path or ""))
    return f"{root}{ext}" if root else ""


def resolve_send_plan(
    platform: str, row: Dict[str, Any], *,
    wa_sticker_capable: bool = False,
    has_gif: Optional[bool] = None,
    has_png: Optional[bool] = None,
) -> Dict[str, str]:
    """按平台能力决定「发什么类型、用哪个产物、镜像记什么、回执叫什么」。

    返回 ``{"eff_type", "variant"(webp|png|gif|line), "mirror_type", "sent_as"}``：

    - telegram：静态 → 原生贴纸（``send_sticker`` webp）；动图 → ``send_animation``
      发 GIF（TG 对 animated webp 无视频贴纸语义，GIF 观感最好；无 GIF 产物退
      静态贴纸），镜像统一记 ``sticker``（工作台气泡按贴纸渲染 webp，浏览器对
      animated webp 原生会动）。
    - whatsapp：Node 边车具备贴纸分支（握手探明）→ 原生贴纸 webp（动图原生支持）；
      老边车未升级 → 图片 png 回退（诚实降级，绝不发 document 附件吓客户）。
    - line：本函数只处理**文件**贴纸 → 图片 png 回退（LINE 自建图当普通图；
      官方商店贴纸的纯 ID 原生路径由路由在进本函数**之前**分流）。
    - 其余平台（messenger/zalo/instagram/official…）：图片 png 回退。

    ``has_gif``/``has_png``：产物兄弟文件是否存在；None＝按磁盘探测。
    """
    plat = str(platform or "").lower()
    fp = str(row.get("file_path") or "")
    animated = bool(row.get("animated"))
    if has_gif is None:
        has_gif = bool(fp) and os.path.isfile(sibling_path(fp, ".gif"))
    if has_png is None:
        has_png = bool(fp) and os.path.isfile(sibling_path(fp, ".png"))
    png_variant = "png" if has_png else "webp"

    if plat == "telegram":
        if animated and has_gif:
            return {"eff_type": "animation", "variant": "gif",
                    "mirror_type": "sticker", "sent_as": "sticker"}
        return {"eff_type": "sticker", "variant": "webp",
                "mirror_type": "sticker", "sent_as": "sticker"}
    if plat == "whatsapp" and wa_sticker_capable:
        return {"eff_type": "sticker", "variant": "webp",
                "mirror_type": "sticker", "sent_as": "sticker"}
    return {"eff_type": "image", "variant": png_variant,
            "mirror_type": "image", "sent_as": "image"}


# ── SQLite 注册表 ────────────────────────────────────────────────────────────

class StickerStore:
    """表情包注册表（线程安全 SQLite；模式对齐 persona_media_store）。"""

    def __init__(self, db_path: Any = ":memory:") -> None:
        self._is_mem = str(db_path) == ":memory:"
        if not self._is_mem:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if not self._is_mem:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._conn.commit()

    # ── 行归一 ──────────────────────────────────────────────

    @staticmethod
    def _pack_row(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        d["enabled"] = bool(d.get("enabled"))
        return d

    @staticmethod
    def _stk_row(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        d["enabled"] = bool(d.get("enabled"))
        d["animated"] = bool(d.get("animated"))
        try:
            d["keywords"] = json.loads(d.get("keywords") or "[]")
        except Exception:
            d["keywords"] = []
        return d

    # ── pack CRUD ───────────────────────────────────────────

    def create_pack(
        self, title: str, *, kind: str = PACK_CUSTOM,
        pack_id: str = "", created_by: str = "", sort: int = 0,
    ) -> Dict[str, Any]:
        pid = str(pack_id or uuid.uuid4().hex[:12])
        k = kind if kind in _VALID_KINDS else PACK_CUSTOM
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO sticker_packs"
                " (id, title, kind, sort, enabled, created_by, created_at, updated_at)"
                " VALUES (?,?,?,?,1,?,?,?)",
                (pid, str(title or "").strip()[:80], k, int(sort),
                 str(created_by or ""), now, now))
            self._conn.commit()
        return self.get_pack(pid) or {}

    def get_pack(self, pack_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM sticker_packs WHERE id=?", (str(pack_id),)).fetchone()
        return self._pack_row(r) if r else None

    def list_packs(self, *, include_disabled: bool = False) -> List[Dict[str, Any]]:
        """全部包（含条目数与封面回落=包内首张贴纸 url）。"""
        q = "SELECT * FROM sticker_packs"
        if not include_disabled:
            q += " WHERE enabled=1"
        q += " ORDER BY sort ASC, created_at ASC"
        with self._lock:
            packs = [self._pack_row(r) for r in self._conn.execute(q).fetchall()]
            counts = dict(self._conn.execute(
                "SELECT pack_id, COUNT(*) FROM stickers WHERE enabled=1"
                " GROUP BY pack_id").fetchall())
            covers = dict(self._conn.execute(
                "SELECT pack_id, MIN(sort || '#' || url) FROM stickers"
                " WHERE enabled=1 AND url != '' GROUP BY pack_id").fetchall())
        for p in packs:
            p["count"] = int(counts.get(p["id"], 0))
            if not p.get("cover_url"):
                raw = str(covers.get(p["id"]) or "")
                p["cover_url"] = raw.split("#", 1)[1] if "#" in raw else ""
        return packs

    def update_pack(self, pack_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        sets, vals = [], []
        for k, v in fields.items():
            if k not in _PACK_UPDATABLE:
                continue
            if k in ("sort",):
                v = int(v or 0)
            elif k == "enabled":
                v = 1 if v else 0
            else:
                v = str(v or "")
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return self.get_pack(pack_id)
        vals.extend([time.time(), str(pack_id)])
        with self._lock:
            self._conn.execute(
                f"UPDATE sticker_packs SET {', '.join(sets)}, updated_at=? WHERE id=?",
                vals)
            self._conn.commit()
        return self.get_pack(pack_id)

    def delete_pack(self, pack_id: str) -> List[str]:
        """删包（连带条目行）。返回被删条目的本地文件路径清单（磁盘清理交调用方，
        调用方须做根目录容纳检查后再 unlink）。"""
        pid = str(pack_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT file_path FROM stickers WHERE pack_id=?", (pid,)).fetchall()
            self._conn.execute("DELETE FROM stickers WHERE pack_id=?", (pid,))
            self._conn.execute("DELETE FROM sticker_packs WHERE id=?", (pid,))
            self._conn.commit()
        return [str(r["file_path"]) for r in rows if r["file_path"]]

    # ── sticker CRUD ────────────────────────────────────────

    def add_sticker(
        self, pack_id: str, *, file_path: str = "", url: str = "",
        emoji_tag: str = "", keywords: Optional[List[str]] = None,
        animated: bool = False, bytes_: int = 0, width: int = 0, height: int = 0,
        sha256: str = "", line_package_id: str = "", line_sticker_id: str = "",
        sort: int = 0, created_by: str = "", sticker_id: str = "",
    ) -> Dict[str, Any]:
        sid = str(sticker_id or uuid.uuid4().hex)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO stickers"
                " (id, pack_id, file_path, url, emoji_tag, keywords, animated,"
                "  bytes, width, height, sha256, line_package_id, line_sticker_id,"
                "  sort, enabled, created_by, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                (sid, str(pack_id), str(file_path or ""), str(url or ""),
                 str(emoji_tag or "")[:16],
                 json.dumps([str(x) for x in (keywords or [])], ensure_ascii=False),
                 1 if animated else 0, int(bytes_ or 0), int(width or 0),
                 int(height or 0), str(sha256 or ""),
                 str(line_package_id or ""), str(line_sticker_id or ""),
                 int(sort or 0), str(created_by or ""), now, now))
            self._conn.commit()
        return self.get(sid) or {}

    def get(self, sticker_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM stickers WHERE id=?", (str(sticker_id),)).fetchone()
        return self._stk_row(r) if r else None

    def find_by_sha(self, pack_id: str, sha256: str) -> Optional[Dict[str, Any]]:
        if not sha256:
            return None
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM stickers WHERE pack_id=? AND sha256=? LIMIT 1",
                (str(pack_id), str(sha256))).fetchone()
        return self._stk_row(r) if r else None

    def list_stickers(
        self, pack_id: str, *, enabled_only: bool = True,
    ) -> List[Dict[str, Any]]:
        q = "SELECT * FROM stickers WHERE pack_id=?"
        if enabled_only:
            q += " AND enabled=1"
        q += " ORDER BY sort ASC, created_at ASC"
        with self._lock:
            rows = self._conn.execute(q, (str(pack_id),)).fetchall()
        return [self._stk_row(r) for r in rows]

    def update_sticker(self, sticker_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        sets, vals = [], []
        for k, v in fields.items():
            if k not in _STK_UPDATABLE:
                continue
            if k == "keywords":
                v = json.dumps([str(x) for x in (v or [])], ensure_ascii=False)
            elif k == "sort":
                v = int(v or 0)
            elif k == "enabled":
                v = 1 if v else 0
            else:
                v = str(v or "")
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return self.get(sticker_id)
        vals.extend([time.time(), str(sticker_id)])
        with self._lock:
            self._conn.execute(
                f"UPDATE stickers SET {', '.join(sets)}, updated_at=? WHERE id=?",
                vals)
            self._conn.commit()
        return self.get(sticker_id)

    def delete_sticker(self, sticker_id: str) -> Optional[Dict[str, Any]]:
        """删条目行；返回被删行（磁盘清理交调用方）。"""
        row = self.get(sticker_id)
        if row is None:
            return None
        with self._lock:
            self._conn.execute("DELETE FROM stickers WHERE id=?", (str(sticker_id),))
            self._conn.commit()
        return row

    # ── 使用记账（最近使用/热度）────────────────────────────

    def record_use(self, sticker_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE stickers SET hits=hits+1, last_used_at=? WHERE id=?",
                (time.time(), str(sticker_id)))
            self._conn.commit()

    def recent(self, limit: int = 24) -> List[Dict[str, Any]]:
        """最近使用（实例级共享——小团队坐席共用素材，个人维度留扩展）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM stickers WHERE enabled=1 AND last_used_at>0"
                " ORDER BY last_used_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [self._stk_row(r) for r in rows]

    def counts(self) -> Dict[str, int]:
        with self._lock:
            packs = self._conn.execute(
                "SELECT COUNT(*) FROM sticker_packs WHERE enabled=1").fetchone()[0]
            stickers = self._conn.execute(
                "SELECT COUNT(*) FROM stickers WHERE enabled=1").fetchone()[0]
        return {"packs": int(packs), "stickers": int(stickers)}

    def pack_size(self, pack_id: str) -> int:
        with self._lock:
            n = self._conn.execute(
                "SELECT COUNT(*) FROM stickers WHERE pack_id=?",
                (str(pack_id),)).fetchone()[0]
        return int(n)

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass


# ── 模块级单例 ───────────────────────────────────────────────────────────────

_STORE: Optional[StickerStore] = None
_STORE_LOCK = threading.Lock()


def configure_sticker_store(db_path: Any) -> StickerStore:
    """显式指定库路径（main 启动期 / 单测传 ``:memory:`` 或 tmp 路径）。"""
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            _STORE.close()
        _STORE = StickerStore(db_path)
    return _STORE


def get_sticker_store() -> Optional[StickerStore]:
    """取全局单例（未配置则按默认路径惰性建；失败返回 None——路由按 503 处理）。"""
    global _STORE
    if _STORE is not None:
        return _STORE
    with _STORE_LOCK:
        if _STORE is None:
            try:
                _STORE = StickerStore(DEFAULT_DB_PATH)
            except Exception:
                logger.warning("[sticker_store] 初始化失败 path=%s",
                               DEFAULT_DB_PATH, exc_info=True)
                return None
    return _STORE


def reset_sticker_store() -> None:
    """清全局单例（单测 teardown 用）。"""
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            _STORE.close()
        _STORE = None
