"""入站爆发合并（debounce，2026-08-02 P0）。

事故形态（WA 实锤占比 64%：入站爆发 → 多条出站）：客户把一句话拆成几条连发
（「你叫什么名字」7 秒后又来「现在几点了」），每条各自触发一次 auto_draft →
两次独立 LLM 生成互不知情 → 客户收到两条各自作答、开头互相复读的回复。

修法＝把「新入站 → 立即拟稿」改成「新入站 → 等一个静默窗（窗内再来消息就续等、
合并），到点用**合并后的 peer_text** 只拟一次稿」。生成链拿到的是完整的话（历史
里也已有全部爆发消息），一次回答所有内容——从**因**上消灭双生成，比出站守卫
（治**果**）优先命中。

设计：
  - 纯逻辑（merge/窗口计算）与计时器分离，测试注入 timer_factory 手动触发零 sleep；
  - 每会话独立窗口：窗内新消息 → 取消旧 timer、并入文本、重开窗（conv dict 取最新）；
  - ``max_wait_sec`` 硬上限：客户连续不停打字时不无限等（从首条起算，到点强制开火）；
  - ``max_texts`` 上限：并入条数达上限立即开火（防极端刷屏撑大 prompt）；
  - **碎片爆发自适应**（2026-08-12 实测「你/是/个/大/傻/子」一行一字连发被
    max_texts=6 + max_wait=25s 双上限切成两窗）：窗内已并入 ≥3 条超短消息
    （≤2 字）＝对方在一个字一个字打一句话 → 上限自动放宽到
    ``frag_max_texts`` / ``frag_max_wait_sec``（默认 12 条 / 45s），整句话
    合成一窗；正常长度消息不受影响；
  - fire 在 timer 线程执行下游回调——与原路径一致（原本就在 ingest 线程同步调，
    回调内部只做 DB 写 + run_coroutine_threadsafe，线程无关）；异常吞掉并记日志，
    绝不让守卫杀掉计时器线程；
  - **语音条单独合并窗**（2026-09-05 K-3 F，J-2 顺手项）：88MP86 BABY BEAR 09-04
    20:39–22:25 ``guard=fresh post_humanize 延迟窗内客户插话`` 30+ 次——客户连发
    语音条，录下一条要十几秒，8s 静默窗每条都到点、各自触发拟稿再被 fresh-guard
    放弃＝白烧 LLM。窗内**最新一条**是语音（ingest 给的 ``[语音]`` 占位 / conv 带
    ``media_type=voice``）→ 静默窗换用 ``voice_window_sec``（默认 15s）；随后来的
    文本仍按常规窗；硬上限 ``max_wait_sec`` 照旧约束。
  - 配置 ``inbox.auto_draft.inbound_merge``（默认 enabled:false＝零行为变更）。

代价（显式接受）：所有 B 线自动回复多 ``window_sec`` 秒延迟——本部署 autosend
本就有拟人打字延迟（数十秒），几秒静默窗反而更像真人「看完再回」。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_SEC = 8.0
DEFAULT_MAX_WAIT_SEC = 25.0
DEFAULT_MAX_TEXTS = 6
# 语音条静默窗：录一条语音 10–20s 是常态，8s 窗每条都到点。15s 仍在 25s 硬上限内。
DEFAULT_VOICE_WINDOW_SEC = 15.0
# ingest 对无正文语音入站给的占位（与 protocol_bridge.media_placeholder("voice") 同源，
# 这里留字面兜底避免 inbox → integrations 的硬依赖）。
_VOICE_PLACEHOLDER = "[语音]"
# 碎片爆发（一行一字打一句话）自适应上限：见模块 docstring。
DEFAULT_FRAG_MAX_WAIT_SEC = 45.0
DEFAULT_FRAG_MAX_TEXTS = 12
# 「超短碎片」判定：≤2 字（CJK 单字/双字、"ok" 类）且不含空白。
_FRAG_PIECE_MAX_CHARS = 2
# 窗内 ≥3 条超短碎片才认定为碎片模式/拼句（两条短消息「嗯」「好」是正常聊天）。
_FRAG_RUN_MIN = 3


def _is_frag_piece(text: Any) -> bool:
    t = str(text or "").strip()
    return bool(t) and len(t) <= _FRAG_PIECE_MAX_CHARS and not any(
        ch.isspace() for ch in t)


def is_fragment_burst(texts: List[str]) -> bool:
    """窗内消息是否呈「一字一条」碎片形态（≥3 条超短碎片）。纯函数。"""
    return sum(1 for t in texts or [] if _is_frag_piece(t)) >= _FRAG_RUN_MIN


def is_voice_piece(text: Any, conv: Optional[Dict[str, Any]] = None) -> bool:
    """这条入站是不是语音条。纯函数。

    两个信号任一命中：conv 带 ``media_type``/``content_type`` == voice（前向兼容，
    当前 ingest 的 conv_dict 不带）；或正文以 ``[语音]`` 占位开头（ingest 对无正文
    语音入站的既定形态；带转写的语音正文是干净文本，按普通文本走常规窗——那时
    客户已经"说完了"，没有再等的理由）。
    """
    try:
        c = conv or {}
        mt = str(c.get("media_type") or c.get("content_type") or "").strip().lower()
        if mt == "voice":
            return True
    except Exception:
        pass
    try:
        placeholder = _VOICE_PLACEHOLDER
        try:
            from src.integrations.protocol_bridge import media_placeholder
            placeholder = str(media_placeholder("voice") or _VOICE_PLACEHOLDER)
        except Exception:
            pass
        return str(text or "").lstrip().startswith(placeholder)
    except Exception:
        return False


def merge_burst_texts(texts: List[str]) -> str:
    """合并爆发消息为一个 peer_text（保序、去首尾空白、跳过空条）。

    碎片拼句（2026-08-12）：连续 ≥3 条超短碎片（≤2 字）＝对方把一句话拆开
    逐字打（「你/是/个/大/傻/子」），按原顺序**无分隔拼成一句**——换行留着
    只会让 LLM 把 6 个字当 6 条独立消息各自理解（实测只回了最后一条）。
    正常长度的消息仍按换行分条（多条各是完整话，不该被粘）。
    """
    pieces = [t.strip() for t in texts or [] if str(t or "").strip()]
    lines: List[str] = []
    run: List[str] = []

    def _flush_run() -> None:
        if not run:
            return
        if len(run) >= _FRAG_RUN_MIN:
            lines.append("".join(run))
        else:
            lines.extend(run)
        run.clear()

    for p in pieces:
        if _is_frag_piece(p):
            run.append(p)
        else:
            _flush_run()
            lines.append(p)
    _flush_run()
    return "\n".join(lines)


def resolve_merge_cfg(auto_draft_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 ``inbox.auto_draft.inbound_merge`` 配置块（默认关）。"""
    blk = dict((auto_draft_cfg or {}).get("inbound_merge") or {})
    return {
        "enabled": bool(blk.get("enabled", False)),
        "window_sec": float(blk.get("window_sec", DEFAULT_WINDOW_SEC)),
        "voice_window_sec": float(
            blk.get("voice_window_sec", DEFAULT_VOICE_WINDOW_SEC)),
        "max_wait_sec": float(blk.get("max_wait_sec", DEFAULT_MAX_WAIT_SEC)),
        "max_texts": int(blk.get("max_texts", DEFAULT_MAX_TEXTS)),
        "frag_max_wait_sec": float(
            blk.get("frag_max_wait_sec", DEFAULT_FRAG_MAX_WAIT_SEC)),
        "frag_max_texts": int(
            blk.get("frag_max_texts", DEFAULT_FRAG_MAX_TEXTS)),
    }


class InboundMerger:
    """按会话 debounce 入站消息，静默窗到点用合并文本调一次下游回调。

    callback 契约与 ``register_new_inbound_cb`` 一致：``(conv: dict, text: str)``。
    """

    def __init__(
        self,
        callback: Callable[[Dict[str, Any], str], None],
        *,
        window_sec: float = DEFAULT_WINDOW_SEC,
        max_wait_sec: float = DEFAULT_MAX_WAIT_SEC,
        max_texts: int = DEFAULT_MAX_TEXTS,
        frag_max_wait_sec: float = DEFAULT_FRAG_MAX_WAIT_SEC,
        frag_max_texts: int = DEFAULT_FRAG_MAX_TEXTS,
        voice_window_sec: Optional[float] = None,
        timer_factory: Optional[Callable[..., Any]] = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._cb = callback
        self._window = max(0.5, float(window_sec))
        # 语音窗不低于常规窗（配置写小了按常规窗兜底，语音永远不比文本更急）
        _vw = DEFAULT_VOICE_WINDOW_SEC if voice_window_sec is None else float(voice_window_sec)
        self._voice_window = max(self._window, _vw)
        self._max_wait = max(self._window, float(max_wait_sec))
        self._max_texts = max(1, int(max_texts))
        # 碎片模式的放宽上限（不低于常规值——配置写小了按常规值兜底）
        self._frag_max_wait = max(self._max_wait, float(frag_max_wait_sec))
        self._frag_max_texts = max(self._max_texts, int(frag_max_texts))
        self._timer_factory = timer_factory or self._default_timer
        self._now = now_fn
        self._lock = threading.Lock()
        # cid -> {conv, texts, first_ts, timer}
        self._pending: Dict[str, Dict[str, Any]] = {}
        # 观测（进程级）
        self.merged_bursts = 0     # 真正发生合并（≥2 条并作一次）的爆发数
        self.merged_messages = 0   # 被并入的消息总条数（含首条）
        self.fired = 0             # 开火（下游回调被调）次数
        self.frag_bursts = 0       # 碎片模式（一字一条拼句）生效的爆发数
        self.voice_windows = 0     # 语音窗生效次数（最新一条是语音条、按 voice_window 开窗）

    @staticmethod
    def _default_timer(delay: float, fn: Callable[[], None]) -> Any:
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.start()
        return t

    @staticmethod
    def _conv_key(conv: Dict[str, Any]) -> str:
        cid = str(conv.get("conversation_id") or "")
        if cid:
            return cid
        return "%s:%s:%s" % (
            conv.get("platform", ""), conv.get("account_id", ""),
            conv.get("chat_key", ""))

    def push(self, conv: Dict[str, Any], text: str) -> None:
        """收一条新入站。窗内已有 pending → 并入并重开窗；否则起新窗。"""
        cid = self._conv_key(conv)
        fire_now = False
        with self._lock:
            st = self._pending.get(cid)
            now = self._now()
            if st is None:
                st = {"conv": dict(conv), "texts": [str(text or "")],
                      "first_ts": now, "timer": None}
                self._pending[cid] = st
            else:
                st["texts"].append(str(text or ""))
                st["conv"] = dict(conv)  # 最新 conv（含最新 last_message 等元数据）
                _t = st.get("timer")
                if _t is not None:
                    try:
                        _t.cancel()
                    except Exception:
                        pass
            # 碎片模式（一字一条打一句话）→ 条数/等待上限自动放宽，整句合一窗
            _frag = is_fragment_burst(st["texts"])
            _cap_texts = self._frag_max_texts if _frag else self._max_texts
            _cap_wait = self._frag_max_wait if _frag else self._max_wait
            if len(st["texts"]) >= _cap_texts:
                fire_now = True
            else:
                # 最新一条是语音条 → 静默窗换语音窗（录下一条要十几秒）；文本照常规窗
                _win = self._window
                if is_voice_piece(text, conv):
                    _win = self._voice_window
                    self.voice_windows += 1
                # 静默窗 vs 首条起算的硬上限，取先到者
                delay = min(_win,
                            max(0.0, st["first_ts"] + _cap_wait - now))
                if delay <= 0:
                    fire_now = True
                else:
                    st["timer"] = self._timer_factory(
                        delay, lambda c=cid: self._fire(c))
        if fire_now:
            self._fire(cid)

    def _fire(self, cid: str) -> None:
        with self._lock:
            st = self._pending.pop(cid, None)
            if st is None:
                return
            _t = st.get("timer")
            if _t is not None:
                try:
                    _t.cancel()
                except Exception:
                    pass
        texts = st["texts"]
        merged = merge_burst_texts(texts)
        self.fired += 1
        if len(texts) > 1:
            self.merged_bursts += 1
            self.merged_messages += len(texts)
            _frag = is_fragment_burst(texts)
            if _frag:
                self.frag_bursts += 1
            logger.info(
                "[inbound_merge] 爆发合并 %d 条 → 一次拟稿 conv=%s%s",
                len(texts), cid, "（碎片拼句）" if _frag else "")
        try:
            self._cb(st["conv"], merged)
        except Exception:
            logger.warning(
                "[inbound_merge] 下游回调异常 conv=%s", cid, exc_info=True)

    def flush_all(self) -> None:
        """立即开火所有 pending（测试/优雅停机用）。"""
        for cid in list(self._pending.keys()):
            self._fire(cid)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def stats_snapshot(self) -> Dict[str, int]:
        return {
            "merged_bursts": self.merged_bursts,
            "merged_messages": self.merged_messages,
            "fired": self.fired,
            "frag_bursts": self.frag_bursts,
            "voice_windows": self.voice_windows,
            "pending": self.pending_count(),
        }


__all__ = [
    "InboundMerger",
    "merge_burst_texts",
    "is_fragment_burst",
    "is_voice_piece",
    "resolve_merge_cfg",
    "DEFAULT_WINDOW_SEC",
    "DEFAULT_VOICE_WINDOW_SEC",
    "DEFAULT_MAX_WAIT_SEC",
    "DEFAULT_MAX_TEXTS",
    "DEFAULT_FRAG_MAX_WAIT_SEC",
    "DEFAULT_FRAG_MAX_TEXTS",
]
