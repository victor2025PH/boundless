"""出站近重复守卫（2026-07-31，198 实锤三连发的投递侧防线）。

事故：坐席在回复工坊「生成草稿 → 填入并发送」连点三次，同一条客户消息在 43 秒内
收到三条同义改写（『Haha, that's true. …』×3）；一分钟后又把客户**已经回答过**的
问题原样再发一遍。生成层的防复读（anti-repeat 窗口比对 + 换角度重生）管不住这类
「每次重生都被发出」的人为/流程重复——需要**投递前**对「最近刚发过什么」做最后一道
核对。

设计（纯函数 + 进程级计数，风格对齐 ``reply_split``）：
  - ``near_duplicate_of_recent(text, recent_rows)``：与窗口内最近**出站**消息比对，
    返回命中详情或 None。两档判定（阈值按 198 实录金标校准）：
      * ``dup``     —— 归一化后相等 / SequenceMatcher ≥ 0.90 / 旧消息整体被包含
                       （对应「同一句原样再发」）；
      * ``similar`` —— 共同前缀 ≥ 12（归一化字符，实录三连发共同开头
                       『hahathatstrue』=13）**或** 相似度 ≥ 0.78
                       （对应「换角度重生的同义改写」——尾部各异，靠开头/高重合抓）
                       **或** 内容词重合 ≥ 0.55 且相似度 ≥ 0.50（2026-08-02 增，见下）。
  - **只做建议，不做决定**：路由把命中转成 409 + 前端确认框（坐席仍可强发），
    绝不静默吞掉人的明确意图；过短文本（归一化 < 12 字符）永不判——「好的」「嗯嗯」
    重复发送是正常聊天。
  - 判定基于**发出去的文本**（翻译后），与库里最近出站行同一口径。

2026-08-02 扩展（客户连发多条 → 两次独立 LLM 生成互不知情 → 同义双发，WA 实锤
入站爆发→多条出站占比 64%）：
  - **内容词重合度** ``token_containment``（拉丁词 + CJK bigram 的
    |A∩B| / min(|A|,|B|)）补第三个 similar 判据——同义改写「开头各异、内容词高度
    重合」（实锤 linda 对：char 0.571 / contain 0.583，旧两档全 miss）。阈值按本机
    30 天生产语料校准：6 对实锤重复 contain ∈ [0.47, 1.0]，659 对正常相邻出站
    p95=0.34；取 contain ≥ 0.55 且 char ≥ 0.50，放过「客户没回追问一次称呼」
    （0.667/0.488）这类合法跟进。媒体占位行（``[图片]``/``[语音]`` 开头）只参与
    dup 档——相册配文风格天然雷同（0.667/0.645），similar 档会误伤。
  - **进程级出站登记表** ``outbound_registry``：DB 镜像有写入时差 + A/B 双链各自
    投递，仅查库会漏「在途/刚发」的那条。发送方在守卫放行后**乐观登记**（失败可
    unregister），守卫把登记表与 DB 行合并比对——两条在途投递互相可见，A 线发的
    B 线也能看见。
  - **自动链拦截配置** ``resolve_guard_cfg``（``inbox.outbound_dup_guard``，默认
    enabled:false 守新子系统约定）：人工路由保持「建议+确认」；自动链（autosend
    deliver / A 线直发）命中即静默跳过（不算投递错误、不喂熔断），分来源计数可观测。
    已知接受的罕见误拦：客户显式要求「再说一遍」的复读（0.667/0.637）——代价是
    沉默一轮可恢复，比同义双发暴露机器人便宜。
"""

from __future__ import annotations

import re
import threading
import time
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

# 归一化：去空白 + 常见标点/emoji 噪声，小写化——「换个标点/emoji」不算新内容
_STRIP_RE = re.compile(
    r"[\s,.!?;:~·、，。！？；：…\"'“”‘’()（）\[\]【】\-—_*]+"
)
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")

DEFAULT_WINDOW_SEC = 180.0
DEFAULT_MIN_LEN = 12          # 归一化后短于此永不判（正常聊天口头禅）
DEFAULT_DUP_RATIO = 0.90
DEFAULT_SIMILAR_RATIO = 0.78
DEFAULT_SIMILAR_PREFIX = 12   # 共同前缀（归一化字符）达到即认「同义开头」
DEFAULT_CONTAIN_MIN = 24      # 旧消息整体被新文本包含时，旧消息至少要这么长
# 内容词重合判据（2026-08-02，阈值按 30 天生产语料校准，见模块 docstring）
DEFAULT_TOKEN_CONTAIN = 0.55  # |A∩B|/min(|A|,|B|) 达到此值
DEFAULT_TOKEN_RATIO = 0.50    # 且 SequenceMatcher 达到此值 → similar

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_TOKEN_RE = re.compile(r"[a-z0-9à-ỹ']{2,}")  # 含越南语等带调拉丁字母


def normalize_for_dup(text: str) -> str:
    """比对用归一化：去空白/标点/emoji + 小写。"""
    s = _EMOJI_RE.sub("", str(text or ""))
    s = _STRIP_RE.sub("", s)
    return s.lower()


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def content_tokens(text: str) -> set:
    """内容 token 集：拉丁词（≥2 字符，含带调字母）+ CJK 相邻字 bigram，小写。

    基于**原文**（保留空格分词）而非 normalize_for_dup 产物——归一化会抹掉词界。
    """
    s = str(text or "").lower()
    words = set(_LATIN_TOKEN_RE.findall(s))
    cjk_chars = _CJK_RE.findall(s)
    bigrams = set(a + b for a, b in zip(cjk_chars, cjk_chars[1:]))
    return words | bigrams


def token_containment(a: str, b: str) -> float:
    """内容词重合度 = |A∩B| / min(|A|,|B|)（任一方为空 → 0，不判）。"""
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _is_media_placeholder(raw_text: str) -> bool:
    """出站镜像里的媒体占位行（「[图片] 配文」「[语音]×2」等）。"""
    return str(raw_text or "").lstrip().startswith(("[", "【"))


def near_duplicate_of_recent(
    text: str,
    recent_rows: Optional[List[Dict[str, Any]]],
    *,
    now: Optional[float] = None,
    window_sec: float = DEFAULT_WINDOW_SEC,
    min_len: int = DEFAULT_MIN_LEN,
    dup_ratio: float = DEFAULT_DUP_RATIO,
    similar_ratio: float = DEFAULT_SIMILAR_RATIO,
    similar_prefix: int = DEFAULT_SIMILAR_PREFIX,
    token_contain: float = DEFAULT_TOKEN_CONTAIN,
    token_ratio: float = DEFAULT_TOKEN_RATIO,
) -> Optional[Dict[str, Any]]:
    """待发文本是否与窗口内最近出站消息近重复。

    recent_rows：store.list_recent_messages 产物（含 direction/text/ts）。
    命中返回 ``{level, similarity, age_sec, matched_text}``，否则 None。
    防御式：任何字段缺失/异常按「不命中」处理（守卫绝不阻断正常发送）。
    """
    cand = normalize_for_dup(text)
    if len(cand) < int(min_len):
        return None
    now = float(now if now is not None else time.time())
    best: Optional[Dict[str, Any]] = None
    for row in reversed(list(recent_rows or [])):
        try:
            if not isinstance(row, dict):
                continue
            if str(row.get("direction") or "") != "out":
                continue
            ts = float(row.get("ts") or 0.0)
            if ts <= 0 or now - ts > float(window_sec):
                continue
            prev_raw = str(row.get("text") or "")
            prev = normalize_for_dup(prev_raw)
            if len(prev) < int(min_len):
                continue
            ratio = SequenceMatcher(None, cand, prev).ratio()
            level = ""
            if cand == prev or ratio >= float(dup_ratio):
                level = "dup"
            elif len(prev) >= DEFAULT_CONTAIN_MIN and prev in cand:
                # 旧消息整体被新文本包含：把刚问过的问题拼在新话里原样再问
                level = "dup"
            elif _is_media_placeholder(prev_raw) or _is_media_placeholder(text):
                # 媒体占位行只参与 dup 档：相册配文风格天然雷同，similar 档会误伤
                pass
            elif (_common_prefix_len(cand, prev) >= int(similar_prefix)
                    or ratio >= float(similar_ratio)
                    or (token_containment(text, prev_raw) >= float(token_contain)
                        and ratio >= float(token_ratio))):
                level = "similar"
            if not level:
                continue
            hit = {
                "level": level,
                "similarity": round(ratio, 3),
                "age_sec": max(0.0, round(now - ts, 1)),
                "matched_text": prev_raw[:120],
            }
            # dup 强于 similar；同级取相似度更高者
            if (best is None
                    or (hit["level"] == "dup" and best["level"] != "dup")
                    or (hit["level"] == best["level"]
                        and hit["similarity"] > best["similarity"])):
                best = hit
        except Exception:
            continue
    return best


# ── 进程级出站登记表（2026-08-02）────────────────────────────────────────────
# 为什么需要：守卫查库比对「最近发过什么」，但 ① DB 出站镜像有写入时差、② 两条
# 投递可能同时在途（humanize 延迟窗内）、③ A 线直发与 B 线 autosend 各自落库口径
# 不一——发送方在守卫放行后**乐观登记**待发文本，守卫把登记表并入比对集，让
# 「在途的那条」对后来者立即可见。发送失败可 unregister（防登记幽灵拦住重试）。
class RecentOutboundRegistry:
    """conversation_id → 最近待发/已发文本（进程内，TTL 剪枝，线程安全）。"""

    def __init__(self, *, ttl_sec: float = 600.0, max_per_conv: int = 6,
                 max_convs: int = 512) -> None:
        self._ttl = float(ttl_sec)
        self._max_per_conv = int(max_per_conv)
        self._max_convs = int(max_convs)
        self._lock = threading.Lock()
        self._data: Dict[str, List[Dict[str, Any]]] = {}
        self._seq = 0

    def register(self, conv_id: str, text: str,
                 *, now: Optional[float] = None) -> int:
        """登记一条待发文本，返回 token（供发送失败时 unregister）。"""
        conv_id = str(conv_id or "")
        if not conv_id or not str(text or "").strip():
            return 0
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._seq += 1
            token = self._seq
            rows = self._data.setdefault(conv_id, [])
            rows.append({"direction": "out", "ts": ts,
                         "text": str(text), "_token": token})
            if len(rows) > self._max_per_conv:
                del rows[: len(rows) - self._max_per_conv]
            # 全局条目上限：最老会话整体剪掉（防长期运行撑爆）
            if len(self._data) > self._max_convs:
                oldest = min(
                    self._data,
                    key=lambda c: self._data[c][-1]["ts"] if self._data[c] else 0)
                self._data.pop(oldest, None)
            return token

    def unregister(self, conv_id: str, token: int) -> None:
        with self._lock:
            rows = self._data.get(str(conv_id or ""))
            if rows:
                self._data[str(conv_id)] = [
                    r for r in rows if r.get("_token") != token]

    def recent_rows(self, conv_id: str,
                    *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """取该会话未过期登记行（形状与 store.list_recent_messages 行兼容）。"""
        ts_now = float(now if now is not None else time.time())
        with self._lock:
            rows = self._data.get(str(conv_id or "")) or []
            live = [r for r in rows if ts_now - r["ts"] <= self._ttl]
            if len(live) != len(rows):
                if live:
                    self._data[str(conv_id)] = live
                else:
                    self._data.pop(str(conv_id or ""), None)
            return [dict(r) for r in live]


outbound_registry = RecentOutboundRegistry()


def resolve_guard_cfg(root_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析自动链守卫配置 ``inbox.outbound_dup_guard``（默认关＝零行为变更）。

    人工发送路由的「建议+确认」守卫**不受本配置控制**（一直在）；本配置只管
    自动链（autosend deliver / A 线直发）要不要「命中即静默跳过」。
    """
    try:
        blk = dict(((root_config or {}).get("inbox") or {})
                   .get("outbound_dup_guard") or {})
    except Exception:
        blk = {}
    return {
        "enabled": bool(blk.get("enabled", False)),
        "window_sec": float(blk.get("window_sec", DEFAULT_WINDOW_SEC)),
        # block_similar=false → 自动链只拦 dup 档（近逐字），similar 档仅计数放行
        "block_similar": bool(blk.get("block_similar", True)),
    }


# ── 观测（进程级计数，autosend-status / metrics 可挂）────────────────────────
_STATS_LOCK = threading.Lock()
_STATS: Dict[str, int] = {"checked": 0, "hit_dup": 0, "hit_similar": 0, "forced": 0}
_BLOCKED_BY_SOURCE: Dict[str, int] = {}


def record_dup_check(level: str = "", *, forced: bool = False,
                     source: str = "", blocked: bool = False) -> None:
    """记一次守卫判定：level ∈ ''(未命中)|dup|similar；forced=坐席确认后强发；
    source/blocked=自动链来源（autosend|a_line）命中即拦截时记分来源计数。"""
    with _STATS_LOCK:
        _STATS["checked"] += 1
        if level == "dup":
            _STATS["hit_dup"] += 1
        elif level == "similar":
            _STATS["hit_similar"] += 1
        if forced:
            _STATS["forced"] += 1
        if blocked and source:
            _BLOCKED_BY_SOURCE[source] = _BLOCKED_BY_SOURCE.get(source, 0) + 1


def dup_guard_metrics_snapshot() -> Dict[str, Any]:
    with _STATS_LOCK:
        snap: Dict[str, Any] = dict(_STATS)
        snap["blocked_by_source"] = dict(_BLOCKED_BY_SOURCE)
        return snap


__all__ = [
    "near_duplicate_of_recent",
    "normalize_for_dup",
    "content_tokens",
    "token_containment",
    "record_dup_check",
    "dup_guard_metrics_snapshot",
    "RecentOutboundRegistry",
    "outbound_registry",
    "resolve_guard_cfg",
]
