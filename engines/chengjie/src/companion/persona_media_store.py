"""每人设「相册/媒体」注册表（SQLite，图片+视频）。

给「每个人设一个相册、后台上传图/视频、按关键词触发调用」建持久层：人设身份仍在
``PersonaManager`` 的 YAML profile，**媒体**（二进制落 /static、高频命中计数、触发词元数据）
落 DB（与 inbox/audit store 同型）。一行 = 一个可发送的媒体条目。

设计（对齐 ``translation_trend_store`` 的线程安全 SQLite + 模块级单例范式）：
- 线程安全（单连接 + Lock，``check_same_thread=False``，WAL）；``:memory:`` 供单测。
- CRUD + ``record_hit``（命中计数/轮播避重用）+ ``find_by_sha``（上传去重）。
- JSON 列（triggers/tags/caption_i18n）在读出时反序列化为 Python 对象。
- **匹配/挑选逻辑不在本模块**（那是纯函数，见 ``persona_media.py``）——本模块只管存取。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MEDIA_PHOTO = "photo"
MEDIA_VIDEO = "video"
_VALID_MEDIA = (MEDIA_PHOTO, MEDIA_VIDEO)

# 默认库路径（与其它 config/*.db 同处）；main.py 启动期可用 configure_* 显式指定。
DEFAULT_DB_PATH = "config/persona_media.db"

_DDL = """
CREATE TABLE IF NOT EXISTS persona_media (
    id             TEXT NOT NULL PRIMARY KEY,
    persona_id     TEXT NOT NULL,
    media_type     TEXT NOT NULL DEFAULT 'photo',
    file_path      TEXT NOT NULL DEFAULT '',
    url            TEXT NOT NULL DEFAULT '',
    thumb_url      TEXT NOT NULL DEFAULT '',
    triggers       TEXT NOT NULL DEFAULT '[]',
    caption        TEXT NOT NULL DEFAULT '',
    caption_i18n   TEXT NOT NULL DEFAULT '{}',
    tags           TEXT NOT NULL DEFAULT '[]',
    weight         INTEGER NOT NULL DEFAULT 1,
    enabled        INTEGER NOT NULL DEFAULT 1,
    tier           TEXT NOT NULL DEFAULT '',
    min_bond_level INTEGER NOT NULL DEFAULT 0,
    bytes          INTEGER NOT NULL DEFAULT 0,
    width          INTEGER NOT NULL DEFAULT 0,
    height         INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    sha256         TEXT NOT NULL DEFAULT '',
    hits           INTEGER NOT NULL DEFAULT 0,
    last_sent_at   REAL NOT NULL DEFAULT 0,
    created_by     TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL DEFAULT 0,
    updated_at     REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pmedia_persona ON persona_media(persona_id, enabled);
CREATE INDEX IF NOT EXISTS idx_pmedia_sha ON persona_media(persona_id, sha256);
-- 2026-07-22 防复读记忆账本：每会话已发媒体（含系列标签）持久记录。
-- 「同一张/同一系列（同套服装连拍）再发给同一个人」= 像 AI 复读机的穿帮信号；
-- 进程内 _LAST_SENT 只避上一条且重启即失 → 落库成为跨重启的"发过什么"记忆。
-- file_key（2026-07-28）：同一张图的**跨链身份**。注册相册链按 DB uuid 记账、
-- 文件系统相册链按文件名记账 → 两套命名空间互不相认，24h 重发冷却形同虚设
-- （实录：06:25:14 发 uuid=364afa02…、06:25:27 又发同一个 cafe_white-dress_01.jpg，
-- 而该系列明明还有 _02.._04 没发过）。两条链都补记文件名，冷却才真的拦得住。
CREATE TABLE IF NOT EXISTS persona_media_sends (
    conv_key    TEXT NOT NULL,
    media_id    TEXT NOT NULL,
    persona_id  TEXT NOT NULL DEFAULT '',
    series      TEXT NOT NULL DEFAULT '',
    sent_at     REAL NOT NULL DEFAULT 0,
    file_key    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (conv_key, media_id)
);
CREATE INDEX IF NOT EXISTS idx_pmsends_conv ON persona_media_sends(conv_key, sent_at);
"""

# update() 允许热改的元数据字段（file_path/url/sha 等身份字段不可改——换文件请删了重传）。
_UPDATABLE = {
    "media_type", "triggers", "caption", "caption_i18n", "tags", "weight",
    "enabled", "tier", "min_bond_level", "thumb_url", "duration_ms",
    "width", "height",
}
_JSON_COLS = {"triggers", "tags", "caption_i18n"}


def _dumps(v: Any, default: str) -> str:
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return default


class PersonaMediaStore:
    """每人设媒体注册表（线程安全 SQLite）。"""

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
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """存量库补列/补表（幂等；调用方已持锁）。失败不抛——建表已成，缺列走降级路径。"""
        for table, col, decl in (
            ("persona_media_sends", "file_key", "TEXT NOT NULL DEFAULT ''"),
        ):
            try:
                have = {r[1] for r in self._conn.execute(
                    "PRAGMA table_info(%s)" % table)}
                if col not in have:
                    self._conn.execute(
                        "ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
            except Exception:
                logger.debug("[persona_media] 迁移 %s.%s 跳过", table, col,
                             exc_info=True)
        # P3 2026-08-22：场景需求日账本（旧库升级路径；新库走 _DDL 一并建）。
        # scene_demand/unmet 原是进程计数、重启即清零——本机日均 8+ 次重启下
        # chips 热度排序/补货报告的「需求侧」形同摆设，落库才有跨重启记忆。
        try:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS scene_demand_daily ("
                " day TEXT NOT NULL, scene TEXT NOT NULL,"
                " demand INTEGER NOT NULL DEFAULT 0,"
                " unmet INTEGER NOT NULL DEFAULT 0,"
                " PRIMARY KEY (day, scene))")
        except Exception:
            logger.debug("[persona_media] scene_demand_daily 建表跳过", exc_info=True)

    # ── 场景需求日账本（P3 2026-08-22：需求侧跨重启记忆）────────────────────
    def record_scene_demand(self, scene_class: str, *, unmet: bool,
                            now: Optional[float] = None) -> None:
        """按日 upsert 场景需求（demand +1；unmet 时 unmet +1）。绝不抛。"""
        sc = str(scene_class or "").strip().lower()
        if not sc:
            return
        day = time.strftime("%Y-%m-%d",
                            time.localtime(now if now is not None else time.time()))
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO scene_demand_daily (day, scene, demand, unmet)"
                    " VALUES (?, ?, 1, ?)"
                    " ON CONFLICT(day, scene) DO UPDATE SET"
                    " demand = demand + 1, unmet = unmet + excluded.unmet",
                    (day, sc, 1 if unmet else 0))
                self._conn.commit()
        except Exception:
            logger.debug("[persona_media] record_scene_demand 失败（已忽略）",
                         exc_info=True)

    def scene_demand_window(self, days: int = 14, *,
                            now: Optional[float] = None) -> Dict[str, Dict[str, int]]:
        """近 N 天需求汇总 ``{scene: {demand, unmet}}``；失败返回 {}。"""
        ts = now if now is not None else time.time()
        since = time.strftime("%Y-%m-%d", time.localtime(ts - max(1, int(days)) * 86400))
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT scene, SUM(demand), SUM(unmet) FROM scene_demand_daily"
                    " WHERE day >= ? GROUP BY scene", (since,)).fetchall()
            return {str(r[0]): {"demand": int(r[1] or 0), "unmet": int(r[2] or 0)}
                    for r in rows}
        except Exception:
            logger.debug("[persona_media] scene_demand_window 失败（已忽略）",
                         exc_info=True)
            return {}

    @staticmethod
    def _row_to_dict(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        for col in _JSON_COLS:
            raw = d.get(col)
            try:
                d[col] = json.loads(raw) if isinstance(raw, str) and raw else (
                    {} if col == "caption_i18n" else [])
            except Exception:
                d[col] = {} if col == "caption_i18n" else []
        d["enabled"] = bool(d.get("enabled"))
        return d

    def add(
        self, persona_id: str, media_type: str, file_path: str, url: str, *,
        thumb_url: str = "", triggers: Optional[List[str]] = None,
        caption: str = "", caption_i18n: Optional[Dict[str, str]] = None,
        tags: Optional[List[str]] = None, weight: int = 1, enabled: bool = True,
        tier: str = "", min_bond_level: int = 0, bytes_: int = 0,
        width: int = 0, height: int = 0, duration_ms: int = 0, sha256: str = "",
        created_by: str = "", now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """新增一个媒体条目，返回落库后的行（dict）。"""
        mt = str(media_type or "").strip().lower()
        if mt not in _VALID_MEDIA:
            mt = MEDIA_PHOTO
        mid = uuid.uuid4().hex
        ts = float(now if now is not None else time.time())
        row = (
            mid, str(persona_id or ""), mt, str(file_path or ""), str(url or ""),
            str(thumb_url or ""), _dumps(list(triggers or []), "[]"),
            str(caption or ""), _dumps(dict(caption_i18n or {}), "{}"),
            _dumps(list(tags or []), "[]"), int(weight or 1),
            1 if enabled else 0, str(tier or ""), int(min_bond_level or 0),
            int(bytes_ or 0), int(width or 0), int(height or 0),
            int(duration_ms or 0), str(sha256 or ""), 0, 0.0,
            str(created_by or ""), ts, ts,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO persona_media (id, persona_id, media_type, file_path, "
                "url, thumb_url, triggers, caption, caption_i18n, tags, weight, "
                "enabled, tier, min_bond_level, bytes, width, height, duration_ms, "
                "sha256, hits, last_sent_at, created_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            self._conn.commit()
        got = self.get(mid)
        return got or {}

    def get(self, media_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM persona_media WHERE id = ?", (str(media_id or ""),)
            ).fetchone()
        return self._row_to_dict(r) if r else None

    def list(
        self, persona_id: Optional[str] = None, *,
        enabled_only: bool = False, media_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """列出媒体条目（按 created_at 升序）。persona_id=None 列全部。"""
        where, args = [], []
        if persona_id is not None:
            where.append("persona_id = ?")
            args.append(str(persona_id))
        if enabled_only:
            where.append("enabled = 1")
        if media_type:
            where.append("media_type = ?")
            args.append(str(media_type).strip().lower())
        sql = "SELECT * FROM persona_media"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at ASC, id ASC"
        with self._lock:
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def find_by_sha(self, persona_id: str, sha256: str) -> Optional[Dict[str, Any]]:
        """按 (persona_id, sha256) 查已存在条目（上传去重）。"""
        if not sha256:
            return None
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM persona_media WHERE persona_id = ? AND sha256 = ? LIMIT 1",
                (str(persona_id or ""), str(sha256)),
            ).fetchone()
        return self._row_to_dict(r) if r else None

    def update(self, media_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """热改元数据（仅 ``_UPDATABLE`` 白名单字段）；返回更新后的行。"""
        sets, args = [], []
        for k, v in fields.items():
            if k not in _UPDATABLE:
                continue
            if k in _JSON_COLS:
                v = _dumps(v, "{}" if k == "caption_i18n" else "[]")
            elif k == "enabled":
                v = 1 if v else 0
            elif k in ("weight", "min_bond_level", "duration_ms", "width", "height"):
                v = int(v or 0)
            sets.append(f"{k} = ?")
            args.append(v)
        if not sets:
            return self.get(media_id)
        sets.append("updated_at = ?")
        args.append(time.time())
        args.append(str(media_id or ""))
        with self._lock:
            self._conn.execute(
                f"UPDATE persona_media SET {', '.join(sets)} WHERE id = ?", tuple(args))
            self._conn.commit()
        return self.get(media_id)

    def delete(self, media_id: str) -> Optional[Dict[str, Any]]:
        """删除条目，返回被删的行（供调用方顺手删磁盘文件）。"""
        row = self.get(media_id)
        if row is None:
            return None
        with self._lock:
            self._conn.execute(
                "DELETE FROM persona_media WHERE id = ?", (str(media_id),))
            self._conn.commit()
        return row

    def rewrite_file_path_prefix(self, old_prefix: str, new_prefix: str) -> int:
        """#67-① 存量迁移配套：把 ``file_path`` 的目录前缀整体改写，返回行数。

        发送链按 DB 绝对路径取文件——相册根迁数据根后不改写＝发送仍指旧位置
        （桌面更新后旧位置蒸发 → pyrogram「Failed to decode」）。仅前缀精确
        匹配的行被改；幂等（重复调用第二次 0 行）。
        """
        old = str(old_prefix or "").strip()
        new = str(new_prefix or "").strip()
        if not old or not new or old == new:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "UPDATE persona_media SET file_path = ? || substr(file_path, ?)"
                " WHERE substr(file_path, 1, ?) = ?",
                (new, len(old) + 1, len(old), old))
            self._conn.commit()
        return int(cur.rowcount or 0)

    # ── AI 打标机器写点（实施90；与运营 update() 白名单分离，防误改）──────────

    def set_auto_tag(
        self, media_id: str, *,
        phash: Optional[str] = None,
        auto_meta: Optional[Dict[str, Any]] = None,
        tag_status: Optional[str] = None,
        tags: Optional[List[str]] = None,
        thumb_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """打标/补齐流水线的专用部分更新（None＝该字段不动）；返回更新后的行。"""
        sets: List[str] = []
        args: List[Any] = []
        if phash is not None:
            sets.append("phash = ?")
            args.append(str(phash))
        if auto_meta is not None:
            sets.append("auto_meta = ?")
            args.append(_dumps(dict(auto_meta), "{}"))
        if tag_status is not None:
            sets.append("tag_status = ?")
            args.append(str(tag_status))
        if tags is not None:
            sets.append("tags = ?")
            args.append(_dumps(list(tags), "[]"))
        if thumb_url is not None:
            sets.append("thumb_url = ?")
            args.append(str(thumb_url))
        if not sets:
            return self.get(media_id)
        sets.append("updated_at = ?")
        args.append(time.time())
        args.append(str(media_id or ""))
        try:
            with self._lock:
                self._conn.execute(
                    f"UPDATE persona_media SET {', '.join(sets)} WHERE id = ?",
                    tuple(args))
                self._conn.commit()
        except Exception:
            logger.debug("[persona_media] set_auto_tag 失败（已忽略）",
                         exc_info=True)
            return None
        return self.get(media_id)

    def phashes(self, persona_id: str) -> Dict[str, str]:
        """该人设全部**非空**感知指纹 ``{id: phash}``（上传近重复比对用）。"""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, phash FROM persona_media "
                    "WHERE persona_id = ? AND phash != ''",
                    (str(persona_id or ""),)).fetchall()
            return {str(r["id"]): str(r["phash"]) for r in rows}
        except Exception:
            logger.debug("[persona_media] phashes 读取失败（已忽略）",
                         exc_info=True)
            return {}

    def record_hit(self, media_id: str, now: Optional[float] = None) -> None:
        """命中一次（hits+1、last_sent_at=now），供轮播避重 + 内容分析。绝不抛。"""
        ts = float(now if now is not None else time.time())
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE persona_media SET hits = hits + 1, last_sent_at = ? "
                    "WHERE id = ?", (ts, str(media_id or "")))
                self._conn.commit()
        except Exception:
            logger.debug("[persona_media] record_hit 失败（已忽略）", exc_info=True)

    # ── 每会话已发媒体账本（2026-07-22 防复读记忆）────────────────────────

    def record_send(
        self, conv_key: str, media_id: str, *,
        persona_id: str = "", series: str = "",
        file_key: str = "",
        now: Optional[float] = None,
    ) -> None:
        """记「这条媒体发给过这个会话」（幂等 upsert）。绝不抛。

        ``file_key``＝该媒体的文件名（``Path(file_path).name``，大小写原样——
        文件系统相册链按 ``Path(f).name`` 精确比对）。两条链都填它，冷却/排除面
        才能认出「注册相册的 uuid」与「相册文件」是同一张图。
        """
        if not conv_key or not media_id:
            return
        ts = float(now if now is not None else time.time())
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO persona_media_sends"
                    "(conv_key, media_id, persona_id, series, sent_at, file_key) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(conv_key, media_id) DO UPDATE SET sent_at = ?, "
                    "file_key = CASE WHEN excluded.file_key != '' "
                    "THEN excluded.file_key ELSE file_key END",
                    (str(conv_key), str(media_id), str(persona_id or ""),
                     str(series or ""), ts, str(file_key or ""), ts))
                self._conn.commit()
        except Exception:
            logger.debug("[persona_media] record_send 失败（已忽略）", exc_info=True)

    def sent_history(
        self, conv_key: str, *, max_age_days: float = 0,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """该会话收过的媒体：``{"ids": set, "series": set, "file_keys": set,
        "items": [{id,series,ts,file_key}]}``。查失败返回空集合。

        ``max_age_days`` > 0 时只看最近 N 天（时间衰减，2026-07-22）：太久之前
        发过的图重新可用——真人也会隔几个月重发怀旧照，永久排除反而把相册
        提前耗尽逼进"翻旧照"模式。0=不衰减（全历史排除）。

        ``file_keys``（2026-07-28）＝跨链身份面：两条相册链各按自己的 id 记账，
        排除面必须并上文件名才认得出「同一张图刚发过」。
        """
        out: Dict[str, Any] = {
            "ids": set(), "series": set(), "file_keys": set(), "items": []}
        if not conv_key:
            return out
        try:
            sql = ("SELECT media_id, series, sent_at, file_key "
                   "FROM persona_media_sends WHERE conv_key = ?")
            args: list = [str(conv_key)]
            if max_age_days and max_age_days > 0:
                ts = float(now if now is not None else time.time())
                sql += " AND sent_at >= ?"
                args.append(ts - float(max_age_days) * 86400.0)
            with self._lock:
                rows = self._conn.execute(sql, tuple(args)).fetchall()
            for r in rows:
                out["ids"].add(str(r["media_id"]))
                s = str(r["series"] or "")
                if s:
                    out["series"].add(s)
                try:
                    fk = str(r["file_key"] or "")
                except (IndexError, KeyError):
                    fk = ""      # 迁移前的旧行
                if fk:
                    out["file_keys"].add(fk)
                # items：带时间戳的明细（P0 一致性——重发冷却/服装连续性判断用）
                out["items"].append({
                    "id": str(r["media_id"]), "series": s,
                    "ts": float(r["sent_at"] or 0), "file_key": fk})
        except Exception:
            logger.debug("[persona_media] sent_history 失败（已忽略）", exc_info=True)
        return out

    def send_ledger_stats(self) -> Dict[str, Any]:
        """相册投放观测（自检卡 runtime 用）：库存/投放次数/覆盖会话/已消耗唯一图。

        - ``media_total``/``media_enabled``：库存条目
        - ``total_sends``：账本累计投放（同图同会话只记一次）
        - ``convs_covered``：收到过相册媒体的会话数
        - ``unique_media_sent``：被发出过的唯一条目数（消耗率=unique/enabled）
        """
        out = {"media_total": 0, "media_enabled": 0, "total_sends": 0,
               "convs_covered": 0, "unique_media_sent": 0}
        try:
            with self._lock:
                r1 = self._conn.execute(
                    "SELECT COUNT(*) c, COALESCE(SUM(enabled),0) e "
                    "FROM persona_media").fetchone()
                r2 = self._conn.execute(
                    "SELECT COUNT(*) c, COUNT(DISTINCT conv_key) k, "
                    "COUNT(DISTINCT media_id) m FROM persona_media_sends"
                ).fetchone()
            out["media_total"] = int(r1["c"] or 0)
            out["media_enabled"] = int(r1["e"] or 0)
            out["total_sends"] = int(r2["c"] or 0)
            out["convs_covered"] = int(r2["k"] or 0)
            out["unique_media_sent"] = int(r2["m"] or 0)
        except Exception:
            logger.debug("[persona_media] send_ledger_stats 失败", exc_info=True)
        return out

    def stats(self, persona_id: Optional[str] = None) -> Dict[str, Any]:
        """条目计数（总/按类型/启用数）。"""
        where, args = [], []
        if persona_id is not None:
            where.append("persona_id = ?")
            args.append(str(persona_id))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT media_type, COUNT(*) c, SUM(enabled) e FROM persona_media"
                f"{clause} GROUP BY media_type", tuple(args)).fetchall()
        out = {"total": 0, "enabled": 0, "photo": 0, "video": 0}
        for r in rows:
            mt = str(r["media_type"] or "")
            c = int(r["c"] or 0)
            out["total"] += c
            out["enabled"] += int(r["e"] or 0)
            if mt in out:
                out[mt] = c
        return out

    def analytics(
        self, persona_id: Optional[str] = None, *, top_n: int = 8,
    ) -> Dict[str, Any]:
        """观测聚合：计数 + 命中总数 + 命中最高的 Top-N 条目（供 ops 看板/metrics）。

        persona_id=None → 跨全部人设聚合（metrics 端点用）；否则限定单人设（人设编辑器可用）。
        """
        out: Dict[str, Any] = dict(self.stats(persona_id))
        where, args = [], []
        if persona_id is not None:
            where.append("persona_id = ?")
            args.append(str(persona_id))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            trow = self._conn.execute(
                f"SELECT COALESCE(SUM(hits),0) th FROM persona_media{clause}",
                tuple(args)).fetchone()
            top_rows = self._conn.execute(
                f"SELECT * FROM persona_media{clause} "
                f"{'AND' if where else 'WHERE'} hits > 0 "
                f"ORDER BY hits DESC, last_sent_at DESC LIMIT ?",
                tuple(args) + (int(max(0, top_n)),)).fetchall()
        out["total_hits"] = int(trow["th"] or 0) if trow else 0
        out["top"] = [
            {
                "id": d.get("id"), "persona_id": d.get("persona_id"),
                "media_type": d.get("media_type"), "hits": d.get("hits") or 0,
                "caption": d.get("caption") or "", "triggers": d.get("triggers") or [],
            }
            for d in (self._row_to_dict(r) for r in top_rows)
        ]
        return out


# ── 模块级单例（懒建；main.py 可用 configure_* 显式指定库路径）───────────────
_STORE: Optional[PersonaMediaStore] = None
_DB_PATH: str = DEFAULT_DB_PATH
_CFG_LOCK = threading.Lock()


def configure_persona_media_store(db_path: Any = DEFAULT_DB_PATH) -> Optional[PersonaMediaStore]:
    """启动期装配（幂等）。指定库路径并建库。"""
    global _STORE, _DB_PATH
    with _CFG_LOCK:
        _DB_PATH = str(db_path)
        if _STORE is None:
            try:
                _STORE = PersonaMediaStore(_DB_PATH)
            except Exception:
                logger.warning("[persona_media] 建库失败", exc_info=True)
                _STORE = None
        return _STORE


def get_persona_media_store() -> Optional[PersonaMediaStore]:
    """取 store 单例（未配置则按默认路径懒建）。建库失败返回 None（调用方需容错）。"""
    global _STORE
    if _STORE is None:
        with _CFG_LOCK:
            if _STORE is None:
                try:
                    _STORE = PersonaMediaStore(_DB_PATH)
                except Exception:
                    logger.warning("[persona_media] 懒建库失败", exc_info=True)
                    _STORE = None
    return _STORE


def peek_persona_media_store() -> Optional[PersonaMediaStore]:
    """取**已存在**的单例（绝不懒建）。

    给低价值 best-effort 写点用（如场景需求账本）：生产进程里单例早被媒体链
    懒建过=写得进；测试进程没人建过=天然零磁盘写入（不会像 get_* 那样把
    ``config/persona_media.db`` 懒建到测试 CWD——「测试写仓库 config/」教训）。
    """
    return _STORE


def reset_persona_media_store() -> None:
    """测试钩子：清空单例。"""
    global _STORE
    with _CFG_LOCK:
        _STORE = None


__all__ = [
    "MEDIA_PHOTO", "MEDIA_VIDEO", "DEFAULT_DB_PATH", "PersonaMediaStore",
    "configure_persona_media_store", "get_persona_media_store",
    "peek_persona_media_store", "reset_persona_media_store",
]
