# -*- coding: utf-8 -*-
"""封号账号 → 新账号「回连认领」（账号资产保全 P1，2026-08-19）。

用户需求第③块「把账号的联系人转移到新账号」的**消费侧**：老客户在新账号上
出现（主动加回 / 被加回）时，把 TA 认领回原客户关系——身份（CPI link）与
情景记忆（``link_and_merge_memory`` 整簇合流）一起走，新号的 AI 立刻记得这段
关系，坐席也能看到「这是老客户」。

与 P0（封号徽标 / 自动快照 / 迁移导出）的边界：P0 管「数据不丢 + 拿得走」，
本模块管「回来的人认得出 + 记忆接得上」。**刻意不依赖 P0 的导出包格式**——
匹配底料直接读 inbox ``conversations``（生产真相单源），P0 落地与否都能工作；
`old_account_id` 由调用方显式给（P0 的封号状态管道落地后可作为默认来源）。

匹配信号分层（宁缺勿滥，与记忆接地同哲学）：

- ``exact_peer``（1.0，可自动批量）：同平台同 chat_key。Telegram chat_key=
  全局用户数字 id、WhatsApp=手机 JID，对同一客户跨我方账号恒定 → 确定同一人。
- ``username``（0.9，可自动批量）：TG @handle 全局唯一（可释放重注册，
  封号→回连的时间窗内极强）。
- ``phone``（0.85）：数字归一后尾 10 位比对（国家码书写差异）。
- ``display_name``（0.5，仅提示绝不自动认领）：昵称随时可改、极易撞名。

排除面：群会话（chat_type 非 private）、bot（peer_is_bot）、自会话
（chat_key='me'）、空句柄。认领动作幂等（CPI canonical 已同 → already_linked
短路，不重复搬记忆）；台账 :class:`ClaimLedger` 落可写数据区（config_dir()，
测试经 AITR_DATA_DIR 自动隔离）。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.contacts.memory_merge_bridge import conv_id_to_memory_pair

logger = logging.getLogger(__name__)

# 置信度层级（matched_on → confidence）；auto-claim 只吃 >= AUTO_CLAIM_MIN 的层
CONFIDENCE = {
    "exact_peer": 1.0,
    "username": 0.9,
    "phone": 0.85,
    "display_name": 0.5,
}
AUTO_CLAIM_MIN = 0.9

_MAX_SCAN_ROWS = 2000     # 单账号会话扫描上限（分页拉取的硬顶）
_PAGE = 500               # store.list_conversations 单页上限
_LEDGER_CAP = 5000        # 台账行数上限（超出裁最旧）
_MIN_USERNAME_LEN = 3
_MIN_PHONE_DIGITS = 8
_PHONE_TAIL = 10          # 电话比对键：尾 10 位（吸收国家码书写差异）

# ── 归一化（纯函数） ─────────────────────────────────────────────────────────

def normalize_username(s: Any) -> str:
    """@handle 归一：去 @ / 去空白 / casefold；过短（<3）视为无效返回空。"""
    v = str(s or "").strip().lstrip("@").strip()
    v = v.casefold()
    if len(v) < _MIN_USERNAME_LEN:
        return ""
    return v


def normalize_phone(s: Any) -> str:
    """电话比对键：仅保留数字，不足 8 位视为无效；取尾 10 位吸收国家码差异。"""
    digits = re.sub(r"\D+", "", str(s or ""))
    if len(digits) < _MIN_PHONE_DIGITS:
        return ""
    return digits[-_PHONE_TAIL:]


def normalize_name(s: Any) -> str:
    """昵称归一：casefold + 连续空白折叠；单字符视为无效（撞名噪声）。"""
    v = re.sub(r"\s+", " ", str(s or "").strip()).casefold()
    if len(v) < 2:
        return ""
    return v


def is_claimable_row(row: Dict[str, Any]) -> bool:
    """可参与匹配的会话行：私聊、非 bot、有 chat_key、非自会话。"""
    if not isinstance(row, dict):
        return False
    if str(row.get("chat_type") or "private") != "private":
        return False
    if int(row.get("peer_is_bot") or 0):
        return False
    ck = str(row.get("chat_key") or "").strip()
    if not ck or ck == "me":
        return False
    return True


# ── 库存 / 覆盖率（加回清单读数） ────────────────────────────────────────────

def fetch_account_conversations(
    inbox_store: Any, platform: str, account_id: str,
    *, max_rows: int = _MAX_SCAN_ROWS,
) -> List[Dict[str, Any]]:
    """分页拉某账号全部会话（last_ts 降序），client 侧过滤可认领行。

    刻意不传 ``chat_type=`` 给 store（该形参是并行线在途改动，不做耦合）；
    过滤在本地做。store 异常返回已拉到的部分（fail-soft）。
    """
    out: List[Dict[str, Any]] = []
    before: Optional[float] = None
    while len(out) < max_rows:
        try:
            page = inbox_store.list_conversations(
                platform=platform, account_id=account_id,
                limit=_PAGE, before_ts=before) or []
        except Exception:
            logger.debug("reconnect: list_conversations failed", exc_info=True)
            break
        if not page:
            break
        for r in page:
            if is_claimable_row(r):
                out.append(r)
                if len(out) >= max_rows:
                    break
        try:
            before = float(page[-1].get("last_ts") or 0.0)
        except Exception:
            break
        if len(page) < _PAGE or not before:
            break
    return out


def handle_coverage(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """「可加回句柄覆盖率」读数（方案验收指标）：有 username / phone 的占比。"""
    total = len(rows)
    with_u = sum(1 for r in rows if normalize_username(r.get("username")))
    with_p = sum(1 for r in rows if normalize_phone(r.get("phone")))
    with_any = sum(
        1 for r in rows
        if normalize_username(r.get("username")) or normalize_phone(r.get("phone")))
    return {
        "total": total,
        "with_username": with_u,
        "with_phone": with_p,
        "with_any_handle": with_any,
        "coverage": round(with_any / total, 3) if total else 0.0,
    }


# ── 匹配 ─────────────────────────────────────────────────────────────────────

def build_match_index(old_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """旧账号会话 → 四路匹配索引（chat_key / username / phone / name）。

    同键冲突（两个旧会话同昵称）保留**最近活跃**那个——匹配宁可少不可错，
    弱信号本就只做提示。
    """
    idx: Dict[str, Dict[str, Dict[str, Any]]] = {
        "chat_key": {}, "username": {}, "phone": {}, "name": {},
    }

    def _put(bucket: str, key: str, row: Dict[str, Any]) -> None:
        if not key:
            return
        cur = idx[bucket].get(key)
        if cur is None or float(row.get("last_ts") or 0) > float(cur.get("last_ts") or 0):
            idx[bucket][key] = row

    for r in old_rows:
        _put("chat_key", str(r.get("chat_key") or "").strip(), r)
        _put("username", normalize_username(r.get("username")), r)
        _put("phone", normalize_phone(r.get("phone")), r)
        _put("name", normalize_name(r.get("display_name")), r)
    return idx


def _pair_linked(cpi: Any, old_cid: str, new_cid: str) -> Optional[bool]:
    """两会话记忆键是否已同 canonical；cpi 缺席 / 键推不出 → None（未知）。"""
    if cpi is None:
        return None
    op = conv_id_to_memory_pair(old_cid)
    np_ = conv_id_to_memory_pair(new_cid)
    if op is None or np_ is None:
        return None
    try:
        return str(cpi.resolve(*op)) == str(cpi.resolve(*np_))
    except Exception:
        return None


def match_candidates(
    new_rows: List[Dict[str, Any]],
    old_index: Dict[str, Dict[str, Dict[str, Any]]],
    *, cpi: Any = None,
) -> List[Dict[str, Any]]:
    """新账号会话 × 旧账号索引 → 认领候选（每个新会话取最高置信的一个旧会话）。

    同一 (new, old) 对多信号命中时 ``matched_on`` 列出全部命中信号、
    confidence 取最高层。输出按 confidence 降序、旧会话活跃时间降序。
    """
    out: List[Dict[str, Any]] = []
    for nr in new_rows:
        if not is_claimable_row(nr):
            continue
        hits: Dict[str, List[str]] = {}   # old_cid -> [signals]
        rows: Dict[str, Dict[str, Any]] = {}

        def _hit(signal: str, old_row: Optional[Dict[str, Any]]) -> None:
            if not old_row:
                return
            ocid = str(old_row.get("conversation_id") or "")
            if not ocid:
                return
            hits.setdefault(ocid, []).append(signal)
            rows[ocid] = old_row

        _hit("exact_peer", old_index["chat_key"].get(str(nr.get("chat_key") or "").strip()))
        _hit("username", old_index["username"].get(normalize_username(nr.get("username"))))
        _hit("phone", old_index["phone"].get(normalize_phone(nr.get("phone"))))
        _hit("display_name", old_index["name"].get(normalize_name(nr.get("display_name"))))

        if not hits:
            continue
        # 每个新会话只出最高置信的一个候选（多个旧会话命中时取最强信号者）
        best_cid, best_signals, best_conf = "", [], -1.0
        for ocid, signals in hits.items():
            conf = max(CONFIDENCE.get(s, 0.0) for s in signals)
            if conf > best_conf:
                best_cid, best_signals, best_conf = ocid, signals, conf
        old_row = rows[best_cid]
        new_cid = str(nr.get("conversation_id") or "")
        out.append({
            "new_conversation_id": new_cid,
            "old_conversation_id": best_cid,
            "matched_on": sorted(
                best_signals, key=lambda s: -CONFIDENCE.get(s, 0.0)),
            "confidence": best_conf,
            "already_linked": _pair_linked(cpi, best_cid, new_cid),
            "new_peer": {
                "chat_key": nr.get("chat_key"),
                "display_name": nr.get("display_name"),
                "username": nr.get("username"),
            },
            "old_peer": {
                "chat_key": old_row.get("chat_key"),
                "display_name": old_row.get("display_name"),
                "username": old_row.get("username"),
                "last_ts": old_row.get("last_ts"),
            },
        })
    out.sort(key=lambda c: (
        -float(c.get("confidence") or 0),
        -float((c.get("old_peer") or {}).get("last_ts") or 0)))
    return out


# ── 认领台账 ─────────────────────────────────────────────────────────────────

def default_ledger_path() -> Path:
    """台账落可写数据区（config_dir 单一事实源；测试经 AITR_DATA_DIR 隔离）。"""
    from src.licensing.data_paths import config_dir
    return Path(config_dir()) / "reconnect_claims.json"


class ClaimLedger:
    """回连认领台账（JSON，行=一次认领动作；幂等判重 + 审计留痕两用）。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else default_ledger_path()
        self._lock = threading.Lock()
        self._rows: List[Dict[str, Any]] = []
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._rows = [r for r in data if isinstance(r, dict)]
        except Exception:
            logger.warning("reconnect ledger load failed: %s", self._path,
                           exc_info=True)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._rows, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception:
            logger.warning("reconnect ledger save failed: %s", self._path,
                           exc_info=True)

    def record(self, row: Dict[str, Any]) -> None:
        with self._lock:
            self._ensure_loaded()
            self._rows.append(dict(row, ts=float(row.get("ts") or time.time())))
            if len(self._rows) > _LEDGER_CAP:
                self._rows = self._rows[-_LEDGER_CAP:]
            self._save()

    def has(self, old_cid: str, new_cid: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            return any(
                r.get("old_conversation_id") == old_cid
                and r.get("new_conversation_id") == new_cid
                and r.get("ok") for r in self._rows)

    def all(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            return list(self._rows[-max(1, int(limit)):])[::-1]


_ledger_singleton: Optional[ClaimLedger] = None
_ledger_lock = threading.Lock()


def get_ledger() -> ClaimLedger:
    """进程级台账单例（路径按当次 default_ledger_path 解析）。"""
    global _ledger_singleton
    with _ledger_lock:
        if _ledger_singleton is None or _ledger_singleton._path != default_ledger_path():
            _ledger_singleton = ClaimLedger()
        return _ledger_singleton


# ── 认领执行 ─────────────────────────────────────────────────────────────────

def claim(
    *,
    inbox_store: Any,
    cpi: Any,
    episodic_store: Any,
    old_conversation_id: str,
    new_conversation_id: str,
    contacts_store: Any = None,
    gateway: Any = None,
    operator: str = "",
    matched_on: str = "manual",
    ledger: Optional[ClaimLedger] = None,
    link_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """执行一次回连认领：CPI link + 记忆整簇合流（核心），contacts 合并（可选层）。

    - 幂等：两键已同 canonical → ``already_linked=True`` 直接成功，不重复搬行。
    - contacts 层 best-effort：老会话有 Contact 档案且新会话有 CI 时经
      ``gateway.manual_merge_identity`` 并档；任何缺件只跳过该层，绝不影响
      记忆合流（``contacts="skipped:<原因>"`` 如实回报）。
    - ``link_fn`` 仅测试注入；生产走 ``link_and_merge_memory``。
    """
    old_cid = str(old_conversation_id or "").strip()
    new_cid = str(new_conversation_id or "").strip()
    res: Dict[str, Any] = {
        "ok": False, "already_linked": False, "rows_merged": 0,
        "contacts": "", "error": "",
        "old_conversation_id": old_cid, "new_conversation_id": new_cid,
    }
    if not old_cid or not new_cid or old_cid == new_cid:
        res["error"] = "bad_pair"
        return res
    old_pair = conv_id_to_memory_pair(old_cid)
    new_pair = conv_id_to_memory_pair(new_cid)
    if old_pair is None or new_pair is None:
        res["error"] = "bad_conversation_id"
        return res
    if cpi is None:
        res["error"] = "no_cpi"
        return res

    linked = _pair_linked(cpi, old_cid, new_cid)
    if linked:
        res.update(ok=True, already_linked=True)
    else:
        if link_fn is None:
            from src.utils.cross_platform_identity import link_and_merge_memory
            link_fn = link_and_merge_memory
        try:
            # 锚=旧会话（历史所在地）：新键并入旧 canonical，未来写入同池
            merged = link_fn(
                cpi, episodic_store,
                old_pair[0], old_pair[1], new_pair[0], new_pair[1]) or {}
            res["ok"] = True
            res["already_linked"] = bool(merged.get("already_linked"))
            res["rows_merged"] = int(merged.get("memory_rows_merged") or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reconnect claim link failed %s <- %s",
                           old_cid, new_cid, exc_info=True)
            res["error"] = f"link_failed:{type(exc).__name__}"
            return res

    # contacts 可选层（并档失败不推翻记忆合流的成功）
    res["contacts"] = _merge_contacts_layer(
        contacts_store=contacts_store, gateway=gateway,
        old_cid=old_cid, new_cid=new_cid, operator=operator)

    (ledger or get_ledger()).record({
        "old_conversation_id": old_cid,
        "new_conversation_id": new_cid,
        "matched_on": matched_on,
        "operator": operator or "admin",
        "ok": res["ok"],
        "already_linked": res["already_linked"],
        "rows_merged": res["rows_merged"],
        "contacts": res["contacts"],
    })
    if res["ok"] and not res["already_linked"]:
        logger.info(
            "[asset] 回连认领 %s <- %s matched_on=%s rows=%d contacts=%s op=%s",
            old_cid, new_cid, matched_on, res["rows_merged"],
            res["contacts"] or "-", operator or "admin")
    return res


def _split_cid(cid: str) -> Tuple[str, str, str]:
    parts = str(cid or "").split(":", 2)
    if len(parts) < 3:
        return "", "", ""
    return parts[0], parts[1] or "default", parts[2]


def _merge_contacts_layer(
    *, contacts_store: Any, gateway: Any,
    old_cid: str, new_cid: str, operator: str,
) -> str:
    """contacts 并档（可选层）：老 CI 的 Contact 收编新 CI。返回结果短语。"""
    if contacts_store is None or gateway is None:
        return "skipped:contacts_disabled"
    try:
        op, oa, ok_ = _split_cid(old_cid)
        np_, na, nk = _split_cid(new_cid)
        old_ci = contacts_store.get_ci_by_external(op, oa, ok_)
        new_ci = contacts_store.get_ci_by_external(np_, na, nk)
        if old_ci is None or not getattr(old_ci, "contact_id", ""):
            return "skipped:no_old_contact"
        if new_ci is None:
            return "skipped:no_new_ci"
        if getattr(new_ci, "contact_id", "") == old_ci.contact_id:
            return "already_merged"
        gateway.manual_merge_identity(
            ci_id=new_ci.channel_identity_id,
            target_contact_id=old_ci.contact_id,
            operator=operator or "reconnect_claim")
        return "merged"
    except Exception as exc:  # noqa: BLE001
        logger.debug("reconnect contacts layer failed", exc_info=True)
        return f"skipped:{type(exc).__name__}"


__all__ = [
    "AUTO_CLAIM_MIN",
    "CONFIDENCE",
    "ClaimLedger",
    "build_match_index",
    "claim",
    "default_ledger_path",
    "fetch_account_conversations",
    "get_ledger",
    "handle_coverage",
    "is_claimable_row",
    "match_candidates",
    "normalize_name",
    "normalize_phone",
    "normalize_username",
]
