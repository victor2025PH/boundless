# -*- coding: utf-8 -*-
"""生活素材「聊过即退役」会话级账本（实施55，2026-08-22 老板拍板）。

背景：人设 ``life_arc.beats`` 是预写的生活素材池，此前按日期全局轮换
（``pick_life_beat``，stride 窗内恒定）——同一条素材会在不同日期反复轮到，
对同一个客户可能聊了又聊（「天天只有抹茶店」类实录）。老板拍板：**聊过的
生活素材不要再聊**，聊资以实时新闻/娱乐为主。本模块＝会话级已用账本：

- 记账三入口（宁多勿漏——素材白白退役的代价低，复读的代价高）：
  ① 主动 ``life_share`` 真发成功（``mark_life_share_sent(beat=)``）；
  ② 聊天回复**真提及**了本轮注入的 beat（``beat_mentioned`` 内容重叠判定，
     A/B 两线共同收口在 ``_update_after_reply``）；
- 消费单入口：``pick_life_beat(skip_fn=)`` 逐会话排除已用素材，全用完
  → 不注入生活线（新闻/其他素材自然补位）。

落盘 ``config/life_beat_used.json`` 为 **CWD 相对路径**（本仓 C 类惯例，
与 daily_topics 缓存同口径：生产进程 CWD=实例数据根）；测试经
``set_ledger_path`` 重定向 tmp，绝不写仓库 config/。绝不抛。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from zlib import crc32

DEFAULT_LEDGER_PATH = "config/life_beat_used.json"

# 有界：每会话最多记这么多条素材键；全局最多这么多会话（超限踢最旧会话）。
_MAX_BEATS_PER_CONVO = 128
_MAX_CONVOS = 600

_LOCK = threading.Lock()
_LEDGER_PATH: Optional[str] = None          # None=默认路径（懒解析）
_STATE: Dict[str, Dict[str, float]] = {}    # {convo_key: {beat_key: ts}}
_LOADED = False

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_WORD_RE = re.compile(r"[a-z0-9]{3,}")
_NOISE_RE = re.compile(r"[\s\W_]+")


def set_ledger_path(path: Optional[str]) -> None:
    """注入落盘路径（测试 tmp / 显式实例路径）；换路径重置内存态并重懒加载。"""
    global _LEDGER_PATH, _LOADED
    with _LOCK:
        p = str(path or "").strip() or None
        if p != _LEDGER_PATH:
            _LEDGER_PATH = p
            _STATE.clear()
            _LOADED = False


def _path() -> Path:
    """账本落点：显式注入 > AITR_DATA_DIR/config/（生产实例数据根；测试进程
    conftest 把该 env 指向进程级 tmp → 记账天然隔离，绝不写仓库 config/）
    > CWD 相对默认（无 env 的旧行为，生产 CWD=数据根时与 env 解析同一文件）。"""
    if _LEDGER_PATH:
        return Path(_LEDGER_PATH)
    env = str(os.environ.get("AITR_DATA_DIR") or "").strip()
    if env:
        return Path(env) / "config" / "life_beat_used.json"
    return Path(DEFAULT_LEDGER_PATH)


def _ensure_loaded() -> None:
    """懒加载落盘账本（须已持锁）；坏文件/缺文件＝空账本，绝不抛。"""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(data, dict):
        return
    for ck, beats in data.items():
        if not isinstance(beats, dict):
            continue
        row: Dict[str, float] = {}
        for bk, ts in beats.items():
            try:
                row[str(bk)] = float(ts)
            except (TypeError, ValueError):
                continue
        if row:
            _STATE[str(ck)] = row


def _persist() -> None:
    """全量覆写落盘（须已持锁）；失败静默＝纯进程内（下次记账再试）。"""
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(_STATE, ensure_ascii=False, indent=0),
            encoding="utf-8")
    except Exception:
        pass


def beat_key(beat: Any) -> str:
    """素材文本 → 稳定键（去空白/标点后 crc32 hex）；空文本返回 ""。"""
    norm = _NOISE_RE.sub("", str(beat or "").lower())
    if not norm:
        return ""
    return format(crc32(norm.encode("utf-8", "ignore")), "08x")


def is_beat_used(convo_key: str, beat: Any) -> bool:
    """该会话是否已聊过这条素材。空键/空素材恒 False。"""
    ck = str(convo_key or "")
    bk = beat_key(beat)
    if not ck or not bk:
        return False
    with _LOCK:
        _ensure_loaded()
        return bk in (_STATE.get(ck) or {})


def record_beat_used(convo_key: str, beat: Any, ts: Optional[float] = None) -> None:
    """记账：该会话聊过这条素材（幂等；超界踢最旧）。绝不抛。"""
    ck = str(convo_key or "")
    bk = beat_key(beat)
    if not ck or not bk:
        return
    ts_v = float(ts if ts is not None else time.time())
    with _LOCK:
        _ensure_loaded()
        if ck not in _STATE and len(_STATE) >= _MAX_CONVOS:
            # 踢「最近一次记账最旧」的会话（近似 LRU，防会话数无限涨）
            oldest = sorted(
                _STATE, key=lambda k: max(_STATE[k].values() or [0.0]))
            for old in oldest[: max(1, _MAX_CONVOS // 10)]:
                _STATE.pop(old, None)
        row = _STATE.setdefault(ck, {})
        if bk not in row and len(row) >= _MAX_BEATS_PER_CONVO:
            for old_bk in sorted(row, key=row.get)[: _MAX_BEATS_PER_CONVO // 4]:
                row.pop(old_bk, None)
        row[bk] = ts_v
        _persist()


def skip_fn_for(convo_key: str) -> Callable[[str], bool]:
    """给 ``pick_life_beat(skip_fn=)`` 用的闭包：beat → 该会话是否已聊过。"""
    ck = str(convo_key or "")

    def _skip(beat: str) -> bool:
        return is_beat_used(ck, beat)

    return _skip


def used_count(convo_key: str) -> int:
    """该会话已退役素材数（观测用）。"""
    with _LOCK:
        _ensure_loaded()
        return len(_STATE.get(str(convo_key or "")) or {})


# ── 「真提及」判定（纯函数）────────────────────────────────────────────────────
def _content_tokens(text: str) -> set:
    """内容 token：CJK bigram + 拉丁词（≥3 字符）。与 memory_grounding 同哲学。"""
    s = str(text or "").lower()
    toks: set = set()
    cjk = "".join(_CJK_RE.findall(s))
    for i in range(len(cjk) - 1):
        toks.add(cjk[i:i + 2])
    toks.update(_LATIN_WORD_RE.findall(s))
    return toks


def beat_mentioned(reply: str, beat: str, *, min_ratio: float = 0.20,
                   min_hits: int = 3) -> bool:
    """回复是否**真提及**了这条生活素材（内容 token 重叠判定，纯函数）。

    口径偏灵敏（老板要的是绝不复读；错标＝素材提前退役，代价低）：
    素材 token 命中率 ≥ ``min_ratio`` 且绝对命中 ≥ ``min_hits``——改写型
    转述（「早市抢着一批贼新鲜的羊肉」→「今儿早市抢了批羊肉贼新鲜」）实测
    重叠率落在 0.25-0.35 区间，0.20 地板收得住；min_hits=3 挡「只共享
    今晚/晚上这类通用 bigram」的误标。素材太短（token < min_hits）时要求
    全部命中。
    """
    bt = _content_tokens(beat)
    if not bt:
        return False
    rt = _content_tokens(reply)
    if not rt:
        return False
    hits = len(bt & rt)
    if len(bt) < max(1, int(min_hits)):
        return hits == len(bt)
    return hits >= int(min_hits) and (hits / len(bt)) >= float(min_ratio)


def convo_key_from_context(context: Any) -> str:
    """从 user_context 推会话账本键：conversation_id 优先，回落
    platform:account:chat（与 A 线 freshness convo_key / 主动链 conversation_id
    同一格式——三处必须同键，账本才对得上）。"""
    try:
        ctx = context if isinstance(context, dict) else {}
        cid = str(ctx.get("conversation_id") or "").strip()
        if cid:
            return cid
        plat = str(ctx.get("platform") or "").strip()
        acct = str(ctx.get("account_id") or "").strip()
        chat = str(ctx.get("chat_id") or ctx.get("user_id") or "").strip()
        if plat and chat:
            return f"{plat}:{acct or 'default'}:{chat}"
        return ""
    except Exception:
        return ""


__all__ = [
    "DEFAULT_LEDGER_PATH",
    "beat_key",
    "beat_mentioned",
    "convo_key_from_context",
    "is_beat_used",
    "record_beat_used",
    "set_ledger_path",
    "skip_fn_for",
    "used_count",
]
