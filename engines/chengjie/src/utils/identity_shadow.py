"""P2.3 跨平台记忆身份自动打通——影子模式（shadow mode）匹配核心。

同一个真实客户可能同时出现在 telegram / whatsapp / line，但情景记忆按
``platform:uid`` 隔离（见 :mod:`src.utils.cross_platform_identity` 的
``user_identity_map``——既有**手动** link 机制）。本模块做的是**自动发现**：
从 inbox ``conversations`` 表的身份画像（display_name / username / phone）里
找出「疑似同一人」的跨平台会话配对并出报告。

**铁律：只发现、只报告，绝不写关联。** 升级路径永远是人工核对后走既有
``POST /api/identity/link``（或未来另立带审计的自动化，不在本模块职责内）。

三档 confidence tier（宁可漏配不可错配，错认会把两个人的记忆串仓）：

- ``high``   规范化电话号码相等（含护栏后缀比对，处理 +86/86/0086/trunk-0 差异）；
- ``medium`` username/handle 精确相等（≥4 字符、含字母、排除占位词）；
- ``low``    display_name 精确相等（≥2 个 CJK 字或 ≥5 个拉丁字母，常见名/占位名
  进 skip 表）——**low 只进报告，绝不作为任何自动化依据**。

设计对齐仓库惯例：匹配全部纯函数（无 DB/网络，确定性输出）；
``run_shadow_scan`` 是唯一 IO 入口（sqlite **read-only URI** 打开生产库，
零写入、不跑迁移），供 CLI ``scripts/identity_shadow_scan.py`` 与将来的
后台任务共用。feature flag ``contacts.identity_shadow.enabled``（默认关）。
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

TIER_HIGH = "high"
TIER_MEDIUM = "medium"
TIER_LOW = "low"
_TIER_RANK = {TIER_HIGH: 0, TIER_MEDIUM: 1, TIER_LOW: 2}

# ── 电话规范化/比对参数（改动必须同步 tests/test_identity_shadow.py 钉住的语义）──
MIN_PHONE_DIGITS = 8    # 短于 8 位不可能是完整号码（验证码/分机/序号），绝不参与匹配
MAX_PHONE_DIGITS = 15   # E.164 上限；超长视为脏数据
PHONE_SUFFIX_MIN = 9    # 后缀比对时「短号」至少 9 位（国家显著号码位长），防短尾撞号
MAX_CC_PREFIX = 4       # 后缀比对时长短号位差 ≤4（国家码 1-3 位 + 冗余 1），
                        # 防两个不同长号仅因「碰巧同尾」凑对

USERNAME_MIN_LEN = 4

# username 占位词（casefold 后比对）：平台默认名/测试名没有身份信号
_USERNAME_PLACEHOLDERS = frozenset({
    "admin", "administrator", "user", "test", "guest", "none", "null",
    "bot", "official", "support", "service", "system", "unknown", "default",
    "telegram", "whatsapp", "line", "messenger",
})

# display_name skip 表（casefold 后比对）：平台默认名 + 高频常见名/称呼——
# 「两个 Maria」是常态不是信号。运营可按误报情况往这里补词。
_COMMON_NAME_SKIP = frozenset({
    # 平台默认/占位
    "whatsapp user", "telegram user", "line user", "微信用户", "用户",
    "unknown", "guest", "admin", "administrator", "test", "客服", "客户",
    # 高频称呼/泛化名
    "朋友", "先生", "女士", "小姐姐", "小哥哥", "亲爱的",
    "小明", "小红", "小美", "小丽", "阿明", "阿强",
    # 高频拉丁常见名（单名，全球 top 级；带姓的全名不受影响）
    "maria", "jose", "john", "michael", "david", "james", "anna", "mary",
    "muhammad", "mohammed", "ali", "sarah", "peter", "kevin", "angel",
})

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[a-zA-Z]")
# whatsapp 私聊 chat_key 即电话（镜像 protocol_bridge._WA_PHONE_RE 语义）
_WA_PHONE_LIKE_RE = re.compile(r"^\d{8,15}$")


# ── 电话 ────────────────────────────────────────────────────────────────────

def normalize_phone(raw: Any) -> str:
    """电话规范化：非数字全剥（+/空格/连字符/括号），``00`` 国际冠码剥掉。

    返回纯数字串；空/过短(<8)/过长(>15)/全同数字（``0000…`` 占位）一律返回
    ``""``＝**绝不参与匹配**。注意：**不**剥国内 trunk-0（``0927…``），那属于
    比对期变体（:func:`phones_match`）——规范化只做无损归一。
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    if not (MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS):
        return ""
    if len(set(digits)) == 1:
        return ""
    return digits


def _phone_variants(n: str) -> List[str]:
    """号码的有效比对形态：原样 + 去国内 trunk-0（``09270135480``→``9270135480``）。

    去 0 后必须仍 ≥ PHONE_SUFFIX_MIN 位才算变体（防把短号剥得更短硬凑）。
    """
    out = [n]
    if n.startswith("0") and len(n) - 1 >= PHONE_SUFFIX_MIN:
        out.append(n[1:])
    return out


def phones_match(a: str, b: str) -> bool:
    """两个**已规范化**号码是否同号。语义（tests 钉死）：

    1. 精确相等；
    2. 护栏后缀比对：短号 ≥9 位、长号比短号最多长 4 位（国家码量级）、
       且长号**以短号整体结尾**——覆盖「8613812345678 vs 13812345678（缺国家码）」
       与「639270135480 vs 09270135480（trunk-0 国内格式，经变体）」；
       两个都带（不同）国家码的完整号永远不会互为后缀 → 不误配。
    """
    if not a or not b:
        return False
    for va in _phone_variants(a):
        for vb in _phone_variants(b):
            if va == vb:
                return True
            s, l = (va, vb) if len(va) <= len(vb) else (vb, va)
            if (len(s) >= PHONE_SUFFIX_MIN
                    and len(l) - len(s) <= MAX_CC_PREFIX
                    and l.endswith(s)):
                return True
    return False


# ── username / display_name ────────────────────────────────────────────────

def username_key(raw: Any) -> str:
    """username 匹配键：strip + 去 ``@`` + casefold。

    返回 ``""``＝不参与匹配：长度 <4、占位词表命中、或不含任何字母
    （纯数字 handle 大概率是平台内部 id，与 chat_key 语义撞车）。
    """
    u = str(raw or "").strip().lstrip("@").casefold()
    if len(u) < USERNAME_MIN_LEN:
        return ""
    if u in _USERNAME_PLACEHOLDERS:
        return ""
    if not _LATIN_RE.search(u):
        return ""
    return u


def display_name_key(raw: Any) -> str:
    """display_name 匹配键：空白折叠 + casefold。

    返回 ``""``＝不参与匹配：CJK 字符 <2 **且** 拉丁字母 <5（单字名/短名）、
    常见名 skip 表命中、或不含任何文字字符（纯数字名＝id 回落）。
    """
    name = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not name:
        return ""
    key = name.casefold()
    if key in _COMMON_NAME_SKIP:
        return ""
    cjk = len(_CJK_RE.findall(name))
    latin = len(_LATIN_RE.findall(name))
    if cjk >= 2 or latin >= 5:
        return key
    return ""


# ── 候选与配对 ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class IdentityCandidate:
    """一条平台会话的身份画像（匹配输入）。"""
    platform: str
    chat_key: str
    display_name: str = ""
    phone: str = ""
    username: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "platform": self.platform,
            "chat_key": self.chat_key,
            "display_name": self.display_name,
            "phone": self.phone,
            "username": self.username,
        }


@dataclass
class ShadowPair:
    """一对「疑似同一人」的跨平台会话（匹配输出，报告项）。"""
    tier: str
    a: IdentityCandidate
    b: IdentityCandidate
    evidence: List[str] = field(default_factory=list)
    already_linked: bool = False   # 双方在 user_identity_map 里已同 canonical

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier,
            "already_linked": self.already_linked,
            "evidence": list(self.evidence),
            "a": self.a.to_dict(),
            "b": self.b.to_dict(),
        }


def _candidate_phone(c: IdentityCandidate) -> str:
    """候选的可比对号码。whatsapp 私聊 chat_key 本身即电话（phone 列可能没回填）
    → 数字形 chat_key 兜底当 phone。**仅限 whatsapp**：telegram 的数字 chat_key
    是 user id、line 是内部 id，绝不能当电话参与匹配。"""
    n = normalize_phone(c.phone)
    if n:
        return n
    if c.platform == "whatsapp" and _WA_PHONE_LIKE_RE.match(c.chat_key or ""):
        return normalize_phone(c.chat_key)
    return ""


def _candidate_name_key(c: IdentityCandidate) -> str:
    """候选的 display_name 匹配键；名字==chat_key（裸 id 回落名）无信号。"""
    key = display_name_key(c.display_name)
    if key and key == str(c.chat_key or "").strip().casefold():
        return ""
    return key



def _prefer_display_name(a: str, b: str) -> str:
    """合并 display_name：优先非空；号码形占位让位给真人名。"""
    a, b = (a or "").strip(), (b or "").strip()
    if not a:
        return b
    if not b:
        return a
    a_digits = a.isdigit() or (a.startswith("+") and a[1:].isdigit())
    b_digits = b.isdigit() or (b.startswith("+") and b[1:].isdigit())
    if a_digits and not b_digits:
        return b
    return a


def _merge_candidate_signals(
    prev: "IdentityCandidate", nxt: "IdentityCandidate",
) -> "IdentityCandidate":
    """同会话多行画像合并：phone/username 取先非空，display_name 见上。"""
    return IdentityCandidate(
        platform=prev.platform,
        chat_key=prev.chat_key,
        display_name=_prefer_display_name(prev.display_name, nxt.display_name),
        phone=(prev.phone or "").strip() or (nxt.phone or "").strip(),
        username=(prev.username or "").strip() or (nxt.username or "").strip(),
    )


def match_candidates(candidates: Iterable[IdentityCandidate]) -> List[ShadowPair]:
    """纯函数匹配：候选列表 → 跨平台疑似配对列表（确定性输出）。

    - 同平台内部**不**配对（同平台重复账号是另一类问题，不在本职责）；
    - 同一对会话命中多种证据 → 合并为一条，tier 取最高档、evidence 全保留；
    - 输出稳定排序：tier（high→low）→ a/b 的 (platform, chat_key) 字典序。
    """
    uniq: Dict[Tuple[str, str], IdentityCandidate] = {}
    for c in candidates or []:
        plat = str(c.platform or "").strip().lower()
        ck = str(c.chat_key or "").strip()
        if not plat or not ck:
            continue
        k = (plat, ck)
        if k not in uniq:
            uniq[k] = IdentityCandidate(
                platform=plat, chat_key=ck,
                display_name=str(c.display_name or ""),
                phone=str(c.phone or ""), username=str(c.username or ""),
            )
        else:
            # 同 (platform, chat_key) 多行（多账号镜像/通讯录回填先后）→
            # 合并信号而非保先丢后。生产事故：ORDER BY last_ts 让无 phone
            # 的新行盖住有 phone 的旧行 → high 档真配对被漏（639649321471）。
            uniq[k] = _merge_candidate_signals(uniq[k], c)
    items = sorted(uniq.values(), key=lambda x: (x.platform, x.chat_key))

    pairs: Dict[Tuple[str, str, str, str], ShadowPair] = {}

    def _add(x: IdentityCandidate, y: IdentityCandidate, tier: str, ev: str) -> None:
        if x.platform == y.platform:
            return
        if (x.platform, x.chat_key) > (y.platform, y.chat_key):
            x, y = y, x
        key = (x.platform, x.chat_key, y.platform, y.chat_key)
        p = pairs.get(key)
        if p is None:
            pairs[key] = ShadowPair(tier=tier, a=x, b=y, evidence=[ev])
            return
        if _TIER_RANK[tier] < _TIER_RANK[p.tier]:
            p.tier = tier
        if ev not in p.evidence:
            p.evidence.append(ev)

    # high：电话。按后 PHONE_SUFFIX_MIN 位分桶（变体各入桶），桶内逐对严格校验。
    buckets: Dict[str, List[Tuple[IdentityCandidate, str]]] = {}
    for c in items:
        n = _candidate_phone(c)
        if not n:
            continue
        for v in dict.fromkeys(_phone_variants(n)):
            buckets.setdefault(v[-PHONE_SUFFIX_MIN:], []).append((c, n))
    for bkey in sorted(buckets):
        lst, seen = [], set()
        for c, n in buckets[bkey]:
            ck = (c.platform, c.chat_key)
            if ck not in seen:
                seen.add(ck)
                lst.append((c, n))
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                (ca, na), (cb, nb) = lst[i], lst[j]
                if phones_match(na, nb):
                    ev = f"phone:{na}" if na == nb else \
                        "phone:" + "~".join(sorted((na, nb)))
                    _add(ca, cb, TIER_HIGH, ev)

    # medium：username 精确相等
    by_user: Dict[str, List[IdentityCandidate]] = {}
    for c in items:
        k = username_key(c.username)
        if k:
            by_user.setdefault(k, []).append(c)
    for k in sorted(by_user):
        lst = by_user[k]
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                _add(lst[i], lst[j], TIER_MEDIUM, f"username:{k}")

    # low：display_name 精确相等（只进报告，绝不建议自动化）
    by_name: Dict[str, List[IdentityCandidate]] = {}
    for c in items:
        k = _candidate_name_key(c)
        if k:
            by_name.setdefault(k, []).append(c)
    for k in sorted(by_name):
        lst = by_name[k]
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                _add(lst[i], lst[j], TIER_LOW, f"display_name:{k}")

    out = list(pairs.values())
    out.sort(key=lambda p: (_TIER_RANK[p.tier],
                            p.a.platform, p.a.chat_key,
                            p.b.platform, p.b.chat_key))
    return out


def annotate_already_linked(
    pairs: List[ShadowPair],
    links: Mapping[Tuple[str, str], str],
) -> List[ShadowPair]:
    """标注「这对已被手动关联」：双方 peer 在 ``user_identity_map`` 里
    共享同一 canonical_id。

    platform_uid 可能是裸 chat_key，也可能是 ``account:chat_key`` 分桶键
    （与 SkillManager._episodic_storage_key 同口径）——两侧任一形态命中且
    canonical 有交集即视为已关联。
    """
    from src.utils.identity_shadow_actions import peer_canonicals
    for p in pairs:
        cans_a = peer_canonicals(links, p.a.platform, p.a.chat_key)
        cans_b = peer_canonicals(links, p.b.platform, p.b.chat_key)
        p.already_linked = bool(cans_a and cans_b and (cans_a & cans_b))
    return pairs


# ── 会话行 → 候选（含噪音过滤）────────────────────────────────────────────────

def _is_noise_peer(platform: str, account_id: str, chat_key: str) -> bool:
    """平台系统条目（不是人）→ 不进候选。复用 inbox 的单一判定源
    ``is_system_peer``（惰性 import，避免纯模块背上 inbox 依赖），
    另补 whatsapp 广播/频道 jid（is_system_peer 只认 telegram 规则）。"""
    try:
        from src.inbox.store import is_system_peer
        if is_system_peer(platform, account_id, chat_key):
            return True
    except Exception:
        pass
    ck = str(chat_key or "").casefold()
    return ck.endswith("@broadcast") or ck.endswith("@newsletter")


def candidates_from_conversations(
    rows: Iterable[Mapping[str, Any]],
) -> Tuple[List[IdentityCandidate], Dict[str, int]]:
    """inbox ``conversations`` 行 → 匹配候选。

    过滤（计数进 skipped，报告可见）：群/频道（``chat_type`` 非 private）、
    平台系统号、以及三个字段都出不了有效匹配键的「无信号」行。
    """
    out: List[IdentityCandidate] = []
    skipped = {"group_or_channel": 0, "system_peer": 0, "no_signal": 0}
    for r in rows or []:
        platform = str(r.get("platform") or "").strip().lower()
        chat_key = str(r.get("chat_key") or "").strip()
        if not platform or not chat_key:
            continue
        chat_type = str(r.get("chat_type") or "").strip().lower()
        if chat_type not in ("", "private"):
            skipped["group_or_channel"] += 1
            continue
        if _is_noise_peer(platform, str(r.get("account_id") or ""), chat_key):
            skipped["system_peer"] += 1
            continue
        c = IdentityCandidate(
            platform=platform, chat_key=chat_key,
            display_name=str(r.get("display_name") or ""),
            phone=str(r.get("phone") or ""),
            username=str(r.get("username") or ""),
        )
        if not (_candidate_phone(c) or username_key(c.username)
                or _candidate_name_key(c)):
            skipped["no_signal"] += 1
            continue
        out.append(c)
    return out, skipped


def signal_coverage(
    rows: Iterable[Mapping[str, Any]],
) -> Dict[str, Dict[str, int]]:
    """各平台「可匹配信号」覆盖率（P7：让 ops 卡自解释「为什么 0 配对」）。

    口径＝**有效**匹配信号而非裸列非空：phone 走 :func:`_candidate_phone`
    （含 whatsapp 私聊 chat_key 即号码的兜底），username 走 :func:`username_key`
    （规范化后非空才算）。行过滤与 :func:`candidates_from_conversations` 同两道
    闸（仅私聊 + 非平台系统号），保证分母与候选池同族——若那边改口径这里同步。

    返回 ``{platform: {"total": n, "phone": k, "username": m}}``（平台名排序）。
    """
    cov: Dict[str, Dict[str, int]] = {}
    for r in rows or []:
        platform = str(r.get("platform") or "").strip().lower()
        chat_key = str(r.get("chat_key") or "").strip()
        if not platform or not chat_key:
            continue
        chat_type = str(r.get("chat_type") or "").strip().lower()
        if chat_type not in ("", "private"):
            continue
        if _is_noise_peer(platform, str(r.get("account_id") or ""), chat_key):
            continue
        c = IdentityCandidate(
            platform=platform, chat_key=chat_key,
            display_name=str(r.get("display_name") or ""),
            phone=str(r.get("phone") or ""),
            username=str(r.get("username") or ""),
        )
        slot = cov.setdefault(platform, {"total": 0, "phone": 0, "username": 0})
        slot["total"] += 1
        if _candidate_phone(c):
            slot["phone"] += 1
        if username_key(c.username):
            slot["username"] += 1
    return {k: cov[k] for k in sorted(cov)}


# ── IO：只读扫描入口（CLI 与后台任务共用；本次不接线调度器）────────────────────

def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """sqlite **read-only** URI 打开：零写入、不建表不迁移，
    对正在服务的生产库无锁害。库文件不存在直接抛（mode=ro 不会创建）。"""
    conn = sqlite3.connect(
        f"file:{Path(db_path).as_posix()}?mode=ro", uri=True,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    return conn


def _read_conversations_ro(db_path: Path, limit: int) -> List[Dict[str, Any]]:
    conn = _connect_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM conversations ORDER BY last_ts DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _read_identity_links_ro(db_path: Path) -> Dict[Tuple[str, str], str]:
    """读既有手动关联表 ``user_identity_map``（bot.db）。库/表缺失 → 空
    （best-effort：标注失败不影响发现本身）。"""
    try:
        conn = _connect_ro(db_path)
    except Exception:
        return {}
    try:
        rows = conn.execute(
            "SELECT platform, platform_uid, canonical_id FROM user_identity_map",
        ).fetchall()
        return {(str(r[0]), str(r[1])): str(r[2]) for r in rows}
    except Exception:
        return {}
    finally:
        conn.close()


def run_shadow_scan(
    inbox_db: Path,
    identity_db: Optional[Path] = None,
    *,
    limit: int = 5000,
) -> Dict[str, Any]:
    """影子扫描：读真实库 → 匹配 → 报告 dict。**只读，绝不写任何库。**

    返回结构（``scripts/identity_shadow_scan.py --json`` 原样输出）::

        {ok, generated_at, inbox_db, identity_db, scanned_conversations,
         candidates, skipped: {...}, counts: {high, medium, low,
         already_linked}, pairs: [ShadowPair.to_dict()...]}
    """
    inbox_db = Path(inbox_db)
    try:
        rows = _read_conversations_ro(inbox_db, limit)
    except Exception as exc:
        return {"ok": False, "error": f"read_inbox_failed: {exc}",
                "inbox_db": str(inbox_db)}
    cands, skipped = candidates_from_conversations(rows)
    pairs = match_candidates(cands)
    links: Dict[Tuple[str, str], str] = {}
    if identity_db:
        links = _read_identity_links_ro(Path(identity_db))
    annotate_already_linked(pairs, links)
    counts = {
        TIER_HIGH: sum(1 for p in pairs if p.tier == TIER_HIGH),
        TIER_MEDIUM: sum(1 for p in pairs if p.tier == TIER_MEDIUM),
        TIER_LOW: sum(1 for p in pairs if p.tier == TIER_LOW),
        "already_linked": sum(1 for p in pairs if p.already_linked),
    }
    return {
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "inbox_db": str(inbox_db),
        "identity_db": str(identity_db) if identity_db else "",
        "scanned_conversations": len(rows),
        "candidates": len(cands),
        "skipped": skipped,
        "counts": counts,
        "coverage": signal_coverage(rows),
        "pairs": [p.to_dict() for p in pairs],
    }


def format_report(report: Mapping[str, Any]) -> str:
    """人类可读报告（CLI 默认输出）。"""
    if not report.get("ok"):
        return f"[error] 影子扫描失败：{report.get('error', 'unknown')}"
    lines: List[str] = []
    lines.append("=== 跨平台身份影子扫描（只报告，不写库）===")
    sk = report.get("skipped") or {}
    lines.append(
        f"库: {report.get('inbox_db')} | 会话: {report.get('scanned_conversations')}"
        f" | 候选: {report.get('candidates')}"
        f"（跳过 群/频道 {sk.get('group_or_channel', 0)}"
        f" / 系统号 {sk.get('system_peer', 0)}"
        f" / 无信号 {sk.get('no_signal', 0)}）"
    )
    counts = report.get("counts") or {}
    lines.append(
        f"疑似配对: {len(report.get('pairs') or [])}"
        f"（high {counts.get(TIER_HIGH, 0)} / medium {counts.get(TIER_MEDIUM, 0)}"
        f" / low {counts.get(TIER_LOW, 0)}；已手动关联 {counts.get('already_linked', 0)}）"
    )
    cov = report.get("coverage") or {}
    if cov:
        parts = []
        for plat in sorted(cov):
            s = cov[plat] or {}
            parts.append(
                f"{plat} 号码 {s.get('phone', 0)}/{s.get('total', 0)}"
                f" 用户名 {s.get('username', 0)}/{s.get('total', 0)}")
        lines.append("信号覆盖: " + " | ".join(parts))
    for p in report.get("pairs") or []:
        a, b = p.get("a") or {}, p.get("b") or {}
        linked = "已关联" if p.get("already_linked") else "未关联"
        lines.append(
            f"[{p.get('tier'):<6}] {a.get('platform')}:{a.get('chat_key')}"
            f"（{a.get('display_name') or '-'}） <-> "
            f"{b.get('platform')}:{b.get('chat_key')}"
            f"（{b.get('display_name') or '-'}）"
            f" | 证据: {'; '.join(p.get('evidence') or [])} | {linked}"
        )
    lines.append(
        "说明：low 档仅供人工核查线索，绝不作为自动化依据；"
        "确认同一人后走既有手动关联 POST /api/identity/link。"
    )
    return "\n".join(lines)


__all__ = [
    "TIER_HIGH", "TIER_MEDIUM", "TIER_LOW",
    "IdentityCandidate", "ShadowPair",
    "normalize_phone", "phones_match", "username_key", "display_name_key",
    "match_candidates", "annotate_already_linked",
    "candidates_from_conversations", "signal_coverage",
    "run_shadow_scan", "format_report",
]
