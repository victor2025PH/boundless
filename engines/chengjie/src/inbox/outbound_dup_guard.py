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

2026-09-02 #144（skuio 工单，WA 英文/西语两会话实锤）——**similar 档只比对本轮**：
  - 事故：客户发新消息 → AI 回复与 119s/88s 前**上一轮**的回复开头/内容词相近
    （sim 0.55/0.59）→ similar 档拦下 → 重写链没救回 → 静默收尾，两会话此后零回复。
    similar 档的设计语义是「同一 burst 两次独立生成的同义双发」；跨了客户新入站的
    相近回复是**两轮各答一次**，不是双发——客户连问两句相近的话本来就该得到两条相近
    的回答。``near_duplicate_of_recent(..., similar_since_ts=)``：早于「本稿所答的
    最新入站」的出站行不参与 similar 判定；dup 档（≥0.90 原样复读）**不变**，跨轮
    逐字复读仍拦（客户两分钟后再问同一句、AI 一字不差再答＝真复读）。
  - 拦下后**绝不静默收尾**：重写链每个结局（attempted/rescued/still_dup/unchanged/
    unusable/error）打 WARNING；重写失败且客户有新入站在等 → 调用方给会话打
    「需人工」（reason=dup_guard_blocked）进待处理清单——沉默必须有人看见。
  - 观测：``near_duplicate_report`` 把「跨轮相近被放行」单独回报，worker 拆
    同轮拦截 / 跨轮拦截（仅 dup 档）/ 跨轮放行三计数。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

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


def latest_inbound_ts(
    rows: Optional[List[Dict[str, Any]]], *, before_ts: Optional[float] = None,
) -> float:
    """会话行里最近一条**入站**的 ts（``before_ts`` 给定时只看不晚于它的入站）。

    「本稿所答的最新入站」＝草稿创建前最后一条客户消息（#144）：创建后才到的
    入站属于下一轮（fresh_guard 管），不能把 since 边界推到它那里——否则夹在
    两条入站之间的同轮出站会被漏比对。无入站 / 坏输入 → 0.0（调用方按「不筑
    since 边界」处理＝旧行为）。纯函数绝不抛。
    """
    best = 0.0
    for row in list(rows or []):
        try:
            if not isinstance(row, dict) or str(row.get("direction") or "") != "in":
                continue
            ts = float(row.get("ts") or 0.0)
            if ts <= 0:
                continue
            if before_ts is not None and ts > float(before_ts):
                continue
            if ts > best:
                best = ts
        except Exception:
            continue
    return best


def peer_awaiting_reply(rows: Optional[List[Dict[str, Any]]]) -> bool:
    """客户是否有新入站在等回复＝会话最新一条（按 ts）是入站。

    dup 拦截静默收尾前的判据（#144）：为 True 的会话拦下＝客户零回复，必须进
    「需人工」清单让人看见；为 False（最新是出站，客户已有回复）只记日志。
    无行 / 坏输入 → False（不打标——打标是有人工成本的动作，宁漏勿滥）。
    """
    latest_dir, latest_ts = "", -1.0
    for row in list(rows or []):
        try:
            if not isinstance(row, dict):
                continue
            d = str(row.get("direction") or "")
            if d not in ("in", "out"):
                continue
            if d == "out" and str(row.get("status") or "") in ("failed", "resent"):
                continue  # 失败留痕不算「已回复」
            ts = float(row.get("ts") or 0.0)
            if ts >= latest_ts:
                latest_dir, latest_ts = d, ts
        except Exception:
            continue
    return latest_dir == "in"


def near_duplicate_report(
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
    similar_since_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """近重复判定全报告：``{"hit": 命中或 None, "cross_round_similar": 被放行的跨轮相近或 None}``。

    ``similar_since_ts``（#144）：similar 档只比对 ts ≥ 该值的出站——早于它的出站
    是上一轮客户消息的回复，与本稿相近属「两轮各答一次」而非双发，记进
    ``cross_round_similar``（可观测「若无此边界本会被误拦」）但**不命中**。
    dup 档不受 since 影响（跨轮原样复读仍拦）。None/0 → 不筑边界（旧行为）。
    """
    out: Dict[str, Any] = {"hit": None, "cross_round_similar": None}
    cand = normalize_for_dup(text)
    if len(cand) < int(min_len):
        return out
    now = float(now if now is not None else time.time())
    try:
        since = float(similar_since_ts or 0.0)
    except (TypeError, ValueError):
        since = 0.0
    best: Optional[Dict[str, Any]] = None
    cross: Optional[Dict[str, Any]] = None
    for row in reversed(list(recent_rows or [])):
        try:
            if not isinstance(row, dict):
                continue
            if str(row.get("direction") or "") != "out":
                continue
            # B63③：投递失败留痕（status=failed/resent）不参与近重复比对——
            # 「一键重发」的文本必然与它自己的留痕逐字相同，不豁免就永远 409。
            if str(row.get("status") or "") in ("failed", "resent"):
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
                "matched_ts": ts,
                # 同轮＝匹配到的出站晚于本稿所答的最新入站（since 未筑时按同轮）
                "same_round": (since <= 0) or (ts >= since),
            }
            if level == "similar" and since > 0 and ts < since:
                # #144：上一轮的回复——两轮各答一次不是双发，放行但留痕
                if cross is None or hit["similarity"] > cross["similarity"]:
                    cross = hit
                continue
            # dup 强于 similar；同级取相似度更高者
            if (best is None
                    or (hit["level"] == "dup" and best["level"] != "dup")
                    or (hit["level"] == best["level"]
                        and hit["similarity"] > best["similarity"])):
                best = hit
        except Exception:
            continue
    out["hit"] = best
    out["cross_round_similar"] = cross
    return out


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
    similar_since_ts: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """待发文本是否与窗口内最近出站消息近重复。

    recent_rows：store.list_recent_messages 产物（含 direction/text/ts）。
    命中返回 ``{level, similarity, age_sec, matched_text, matched_ts, same_round}``，
    否则 None。``similar_since_ts`` 语义见 :func:`near_duplicate_report`。
    防御式：任何字段缺失/异常按「不命中」处理（守卫绝不阻断正常发送）。
    """
    return near_duplicate_report(
        text, recent_rows, now=now, window_sec=window_sec, min_len=min_len,
        dup_ratio=dup_ratio, similar_ratio=similar_ratio,
        similar_prefix=similar_prefix, token_contain=token_contain,
        token_ratio=token_ratio, similar_since_ts=similar_since_ts,
    )["hit"]


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
        # impl85 阶段3（工单#45 taihua009 02:11 实录）：拦下之后自动「换个说法」
        # 重生一条再核对一次，拦得对但不该空过这一轮。默认开——它只在守卫已
        # enabled 且真的拦截时才多一次本地 LLM 改写，是对「静默空轮」的纯改进；
        # rewrite_retry:false 为紧急停用开关。
        "rewrite_retry": bool(blk.get("rewrite_retry", True)),
    }


# ── 拦截后「换个说法」重试（impl85 阶段3，2026-08-29）─────────────────────────
# 事故：客户 23 秒连发三条 → 合并重新生成的回复与 89 秒前刚发的三句相似度 98%
# → 防复读拦得对，但拦下后没有任何补救，这一轮就空过去了（客户干等）。
# 修法：命中拦截 → 用「已发内容」当负样本让 LLM 换说法重写一条 → **重写稿再过
# 一次同一守卫**——仍命中就维持静默跳过（绝不硬发），重写链任何异常也维持跳过
# （守卫语义只可能更严不可能更松）。

def build_dup_rewrite_prompt(matched_text: str, *, attempt: int = 1) -> str:
    """重写指令（system prompt）：负样本=刚发过的内容；保语言/保意图/换表达。

    ``attempt=2``（R87 #331）：第一次重写仍雷同后的**换角度**指令——换开头、换句式、
    先接对方的话再说自己的，绝不复用上一稿的任何短语。
    """
    matched = str(matched_text or "").strip()[:280]
    base = (
        "你是聊天回复改写器。给你的这句话和刚刚已经发出去的内容几乎一样，"
        "不能原样再发。请换一个说法/角度重写它：保持原意和语气，"
        "**用与原文完全相同的语言**，长度接近原文，绝不解释、绝不加引号、"
        "只输出重写后的那句话。\n"
    )
    if int(attempt or 1) >= 2:
        base += (
            "这已经是第二次改写，上一次改写仍然太像。这次必须**彻底换角度**：换开头、换句式、"
            "先回应对方刚说的话再说自己的，不得复用刚发过内容里的任何短语或口头语。\n"
        )
    return base + f"刚发过的内容（禁止雷同）：{matched}"


def attach_rewrite_fn(cfg: Optional[Dict[str, Any]], ai_client: Any) -> Optional[Dict[str, Any]]:
    """装配层：把「换说法」重写闭包挂进守卫配置 dict（键 ``rewrite_fn``）。

    ai_client 缺席 / 无 ``rewrite_local``（本地小模型改写原语）→ 原样返回，
    自动链维持「命中即静默跳过」旧行为。返回**拷贝**（不改调用方传入的 dict）。
    """
    if not isinstance(cfg, dict) or ai_client is None:
        return cfg
    rewrite_local = getattr(ai_client, "rewrite_local", None)
    if rewrite_local is None:
        return cfg

    async def _rewrite(text: str, matched: str) -> Optional[str]:
        return await rewrite_local(build_dup_rewrite_prompt(matched), str(text))

    async def _rewrite_alt(text: str, matched: str) -> Optional[str]:
        # R87 #331：第二次「换角度」重写——指令更硬、温度更高，仍不过才转人工
        try:
            return await rewrite_local(build_dup_rewrite_prompt(matched, attempt=2), str(text),
                                       temperature=0.95)
        except TypeError:   # 旧签名无 temperature
            return await rewrite_local(build_dup_rewrite_prompt(matched, attempt=2), str(text))

    out = dict(cfg)
    out["rewrite_fn"] = _rewrite
    out["rewrite_fn_alt"] = _rewrite_alt
    return out


async def attempt_dup_rewrite(
    *,
    text: str,
    hit: Optional[Dict[str, Any]],
    rows: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    rewrite_fn: Any,
    source: str = "",
    now: Optional[float] = None,
    similar_since_ts: Optional[float] = None,
    conv_id: str = "",
) -> Optional[str]:
    """dup-guard 命中后的重写重试。返回「重写且再核通过」的文本；None=维持跳过。

    ``rewrite_fn``: ``async (text, matched_text) -> Optional[str]``（由调用链注入，
    A 线用 client.ai_client、B 线经 dup_guard_cfg 携带的闭包）。绝不抛。
    ``similar_since_ts`` 与首检同值（#144）：重写稿再核不得比首检更严——否则
    上一轮的回复会把换过说法的重写稿再拦一次。``conv_id`` 只用于结局日志。
    每个结局都打 WARNING（#144「拦下后绝不静默收尾」）——``rewrite_fn`` 缺席 /
    开关关也算一种结局（``skipped``），让「为什么没救回」在日志里有答案。
    """
    if not (cfg or {}).get("rewrite_retry", True):
        _record_rewrite("skipped", source, conv_id=conv_id,
                        detail="rewrite_retry=false")
        return None
    if rewrite_fn is None or not str(text or "").strip():
        _record_rewrite("skipped", source, conv_id=conv_id,
                        detail="no rewrite_fn" if rewrite_fn is None else "empty text")
        return None
    _record_rewrite("attempted", source, conv_id=conv_id)
    matched = str((hit or {}).get("matched_text") or "")
    try:
        new_text = await rewrite_fn(str(text), matched)
    except Exception as exc:
        _record_rewrite("error", source, conv_id=conv_id,
                        detail=f"{type(exc).__name__}: {exc}"[:160])
        return None
    new_text = str(new_text or "").strip().strip('"“”「」').strip()
    if not new_text or len(new_text) > max(200, len(str(text)) * 3):
        _record_rewrite("unusable", source, conv_id=conv_id,
                        detail=f"len={len(new_text)}")
        return None
    if normalize_for_dup(new_text) == normalize_for_dup(text):
        _record_rewrite("unchanged", source, conv_id=conv_id)
        return None
    try:
        hit2 = near_duplicate_of_recent(
            new_text, rows,
            window_sec=float((cfg or {}).get("window_sec", DEFAULT_WINDOW_SEC)),
            now=now, similar_since_ts=similar_since_ts)
    except Exception as exc:
        _record_rewrite("error", source, conv_id=conv_id,
                        detail=f"recheck {type(exc).__name__}"[:160])
        return None
    lvl2 = (hit2 or {}).get("level", "")
    blocked2 = bool(hit2) and (
        lvl2 == "dup" or bool((cfg or {}).get("block_similar", True)))
    if blocked2:
        _record_rewrite(
            "still_dup", source, conv_id=conv_id,
            detail="level=%s sim=%.2f" % (
                lvl2, float((hit2 or {}).get("similarity") or 0.0)))
        return None
    _record_rewrite("rescued", source, conv_id=conv_id,
                    detail="len %d→%d" % (len(str(text)), len(new_text)))
    return new_text


# ── 观测（进程级计数，autosend-status / metrics 可挂）────────────────────────
_STATS_LOCK = threading.Lock()
_STATS: Dict[str, int] = {"checked": 0, "hit_dup": 0, "hit_similar": 0, "forced": 0,
                          # #144：similar 档因「早于本轮最新入站」被放行的次数
                          "released_cross_round": 0}
_BLOCKED_BY_SOURCE: Dict[str, int] = {}
# 拦截后重写重试漏斗（impl85 阶段3）：attempted → rescued（换说法后放行）/
# still_dup（重写仍雷同，维持跳过）/ unchanged / unusable / error / skipped
_REWRITE_STATS: Dict[str, int] = {}


def _record_rewrite(outcome: str, source: str = "", *, conv_id: str = "",
                    detail: str = "") -> None:
    """记重写漏斗一格 + 打 WARNING（#144：拦截后的每个结局都必须在日志可见）。"""
    key = str(outcome or "unknown")
    with _STATS_LOCK:
        _REWRITE_STATS[key] = _REWRITE_STATS.get(key, 0) + 1
        if source:
            sk = f"{key}:{source}"
            _REWRITE_STATS[sk] = _REWRITE_STATS.get(sk, 0) + 1
    try:
        logger.warning(
            "[dup_guard] rewrite outcome=%s source=%s conv=%s%s",
            key, source or "-", conv_id or "-",
            (" " + str(detail)) if detail else "")
    except Exception:
        pass


def record_dup_check(level: str = "", *, forced: bool = False,
                     source: str = "", blocked: bool = False,
                     released_cross_round: bool = False) -> None:
    """记一次守卫判定：level ∈ ''(未命中)|dup|similar；forced=坐席确认后强发；
    source/blocked=自动链来源（autosend|a_line）命中即拦截时记分来源计数；
    released_cross_round=本次有跨轮相近出站被 since 边界放行（#144 观测）。"""
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
        if released_cross_round:
            _STATS["released_cross_round"] = _STATS.get("released_cross_round", 0) + 1


def dup_guard_metrics_snapshot() -> Dict[str, Any]:
    with _STATS_LOCK:
        snap: Dict[str, Any] = dict(_STATS)
        snap["blocked_by_source"] = dict(_BLOCKED_BY_SOURCE)
        snap["rewrite"] = dict(_REWRITE_STATS)
        return snap


__all__ = [
    "near_duplicate_of_recent",
    "near_duplicate_report",
    "latest_inbound_ts",
    "peer_awaiting_reply",
    "normalize_for_dup",
    "content_tokens",
    "token_containment",
    "record_dup_check",
    "dup_guard_metrics_snapshot",
    "RecentOutboundRegistry",
    "outbound_registry",
    "resolve_guard_cfg",
    "build_dup_rewrite_prompt",
    "attempt_dup_rewrite",
]
