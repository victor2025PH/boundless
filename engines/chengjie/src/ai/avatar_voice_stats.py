"""AvatarHub 语音用量观测（进程级单例）。

用途：证明 avatar_clone 在生产**真的在跑、走了哪条通道、可用性如何**——
合成成败/延迟、情绪标签 vs 动态 instruct 通道分布、预渲染命中（零 GPU 发送）、
GPU 串行队列深度峰值、STT 成败。供 ops-overview「AvatarHub 语音」卡与 Prometheus。

风格对齐 ``speech_emotion_stats``：无新增依赖；``dump()`` 供 /api/workspace/metrics，
``dump_prom()`` 供 Prometheus。**绝不记录文本原文**，只记标签与计数。
best-effort：record 任何异常都吞掉，绝不阻塞语音主链路。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class AvatarVoiceStats:
    # 缺口台词收集上限（distinct 文本数，防刷量撑爆；文本截断 40 字）
    _MISS_TEXT_CAP = 50
    _MISS_TEXT_LEN = 40

    __slots__ = (
        "_lock", "_synth_total", "_synth_ok", "_synth_fail", "_latency_ms_sum",
        "_by_channel", "_by_emotion", "_colloquial", "_colloquial_llm",
        "_colloquial_gen", "_paralinguistic",
        "_prerender_hits", "_prerender_miss",
        "_miss_texts", "_miss_personas", "_queue_depth",
        "_queue_peak", "_queue_wait_ms_sum", "_queue_wait_n",
        "_stt_total", "_stt_ok", "_started_at", "_last_ts",
        "_synth_fail_streak", "_last_synth_ok_ts", "_last_synth_fail_ts",
        "_truncation_rejects",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._synth_total = 0
        self._synth_ok = 0
        self._synth_fail = 0
        # 半死检测口径（2026-07-14 事故：health 200 但合成全超时 2h20m 无人知）：
        # 连续失败 streak + 最近成功/失败时刻，供 HealthWatchdog 判「探测绿但合成挂死」
        self._synth_fail_streak = 0
        self._last_synth_ok_ts = 0.0
        self._last_synth_fail_ts = 0.0
        self._latency_ms_sum = 0        # 仅成功合成累计（算均值）
        self._by_channel: Dict[str, int] = {}   # emotion / instruct（成功合成的通道分布）
        self._by_emotion: Dict[str, int] = {}   # emotion 通道的标签分布
        # 活人感增强命中（成功合成中占比）：口语化改写（文本层）+ 副语言标记（声音层）
        self._colloquial = 0            # 送引擎前做过口语化改写的合成条数（含规则+LLM）
        self._colloquial_llm = 0        # 其中 LLM 档改写的条数
        self._colloquial_gen = 0        # 生成层口语版直送（Phase G，零 TTS 前改写）
        self._paralinguistic = 0        # 注入过 [sigh]/[breath]/[laughter] 的合成条数
        self._prerender_hits = 0        # 预渲染命中（零 GPU 发送）
        self._prerender_miss = 0        # 预渲染可备货缺口（短句未命中 → 值得进台词库）
        # 缺口台词 Top-N（AI 出站短句，非用户隐私内容；capped 防撑爆）：
        # 运营据此决定往 config/prerender_lines/<persona>.txt 加什么
        self._miss_texts: Dict[str, int] = {}
        # 缺口的人设归属：text -> {persona_id: count}（每文本 ≤8 人设）
        self._miss_personas: Dict[str, Dict[str, int]] = {}
        self._queue_depth = 0           # 当前 GPU 串行等待+执行中的请求数
        self._queue_peak = 0            # 峰值（观测并发压力）
        self._queue_wait_ms_sum = 0     # 排队等待累计（分段观测：等待 vs 合成）
        self._queue_wait_n = 0
        self._stt_total = 0
        self._stt_ok = 0
        # 质量闸门拒发数（2026-07-15「乱码语音」防线：截断/坏音被拦下的次数）
        self._truncation_rejects = 0
        self._started_at = time.time()
        self._last_ts = 0.0

    # ── 合成 ─────────────────────────────────────────────────────────────────
    def record_synth(self, *, ok: bool, latency_ms: int = 0,
                     channel: str = "", emotion: str = "",
                     colloquial: bool = False, colloquial_llm: bool = False,
                     colloquial_generated: bool = False,
                     paralinguistic: bool = False) -> None:
        """记一次 avatar_clone 合成。channel ∈ {emotion, instruct}。

        ``colloquial``/``colloquial_llm``/``colloquial_generated``/``paralinguistic``：
        本条送引擎前是否做过口语化改写（规则或 LLM）/ LLM 档口语化 /
        生成层口语版直送（Phase G）/ 副语言标记注入。
        """
        try:
            ch = str(channel or "").strip().lower()
            emo = str(emotion or "").strip().lower()
        except Exception:
            ch, emo = "", ""
        with self._lock:
            self._synth_total += 1
            self._last_ts = time.time()
            if ok:
                self._synth_ok += 1
                self._synth_fail_streak = 0
                self._last_synth_ok_ts = self._last_ts
                if latency_ms > 0:
                    self._latency_ms_sum += int(latency_ms)
                if ch:
                    self._by_channel[ch] = self._by_channel.get(ch, 0) + 1
                if ch == "emotion" and emo:
                    self._by_emotion[emo] = self._by_emotion.get(emo, 0) + 1
                if colloquial:
                    self._colloquial += 1
                if colloquial_llm:
                    self._colloquial_llm += 1
                if colloquial_generated:
                    self._colloquial_gen += 1
                if paralinguistic:
                    self._paralinguistic += 1
            else:
                self._synth_fail += 1
                self._synth_fail_streak += 1
                self._last_synth_fail_ts = self._last_ts

    def hang_signal(self) -> Dict[str, Any]:
        """「半死」判定信号（无副作用快照）：健康探测之外的第二证据源。

        HealthWatchdog 用它识别 2026-07-14 型事故——7852 /health 一直 200 但
        /v1/tts/clone 全部超时（health-only 探测永远绿）。只出原始信号，
        阈值判断留在 watchdog（可配置、可测试）。
        """
        with self._lock:
            return {
                "fail_streak": int(self._synth_fail_streak),
                "last_ok_ts": float(self._last_synth_ok_ts),
                "last_fail_ts": float(self._last_synth_fail_ts),
            }

    def record_prerender_hit(self) -> None:
        with self._lock:
            self._prerender_hits += 1
            self._last_ts = time.time()

    def record_prerender_miss(self, text: str = "", persona_id: str = "") -> None:
        """短句可备货缺口：查了预渲染但没命中（值得进台词库的候选）。

        只记 AI 出站短句（调用方已按长度过滤），截断 + 上限——distinct 满后仅累计
        已知条目，不再收新键（防撑爆）。``persona_id`` 归属（每文本 ≤8 个人设）供
        「入 <persona> 库」与自动入库的目标决策。
        """
        with self._lock:
            self._prerender_miss += 1
            self._last_ts = time.time()
            t = str(text or "").strip()[: self._MISS_TEXT_LEN]
            if not t:
                return
            if t in self._miss_texts:
                self._miss_texts[t] += 1
            elif len(self._miss_texts) < self._MISS_TEXT_CAP:
                self._miss_texts[t] = 1
            else:
                return
            pid = str(persona_id or "").strip()
            if pid:
                per = self._miss_personas.setdefault(t, {})
                if pid in per or len(per) < 8:
                    per[pid] = per.get(pid, 0) + 1

    # ── GPU 队列水位（enter/exit 由串行锁调用方包裹）────────────────────────
    def queue_enter(self) -> None:
        with self._lock:
            self._queue_depth += 1
            self._queue_peak = max(self._queue_peak, self._queue_depth)

    def queue_exit(self) -> None:
        with self._lock:
            self._queue_depth = max(0, self._queue_depth - 1)

    def record_queue_wait(self, wait_ms: int) -> None:
        """记一次进 GPU 锁前的排队等待时长（容量规划口径）。"""
        with self._lock:
            self._queue_wait_ms_sum += max(0, int(wait_ms))
            self._queue_wait_n += 1

    def record_truncation_reject(self) -> None:
        """质量闸门拦下一条截断/坏音（未发出）。持续 >0 增长 = TTS 后端在出坏音。"""
        with self._lock:
            self._truncation_rejects += 1
            self._last_ts = time.time()

    # ── STT ──────────────────────────────────────────────────────────────────
    def record_stt(self, *, ok: bool) -> None:
        with self._lock:
            self._stt_total += 1
            self._last_ts = time.time()
            if ok:
                self._stt_ok += 1

    # ── 导出 ─────────────────────────────────────────────────────────────────
    def dump(self) -> Dict[str, Any]:
        with self._lock:
            total = self._synth_total
            ok = self._synth_ok
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "synth_total": int(total),
                "synth_ok": int(ok),
                "synth_fail": int(self._synth_fail),
                "synth_fail_streak": int(self._synth_fail_streak),
                "last_synth_ok_ts": float(self._last_synth_ok_ts),
                "fail_rate": round(self._synth_fail / total, 4) if total else 0,
                "avg_latency_ms": int(self._latency_ms_sum / ok) if ok else 0,
                "by_channel": dict(sorted(self._by_channel.items())),
                "by_emotion": dict(sorted(self._by_emotion.items())),
                "colloquial": int(self._colloquial),
                "colloquial_llm": int(self._colloquial_llm),
                "colloquial_gen": int(self._colloquial_gen),
                "paralinguistic": int(self._paralinguistic),
                # 活人感增强占比（口语化命中 / 成功合成）——校准 lead_prob 的数据口径
                "colloquial_rate": round(self._colloquial / ok, 4) if ok else 0,
                "colloquial_llm_rate": round(self._colloquial_llm / ok, 4) if ok else 0,
                "colloquial_gen_rate": round(self._colloquial_gen / ok, 4) if ok else 0,
                "paralinguistic_rate": round(self._paralinguistic / ok, 4) if ok else 0,
                "prerender_hits": int(self._prerender_hits),
                "prerender_miss": int(self._prerender_miss),
                # 备货覆盖率 = 命中 / (命中+短句缺口)；无样本 → None（前端显 —）
                "prerender_coverage": (
                    round(self._prerender_hits
                          / (self._prerender_hits + self._prerender_miss), 4)
                    if (self._prerender_hits + self._prerender_miss) else None),
                "top_misses": [
                    {"text": t, "n": n,
                     "personas": dict(self._miss_personas.get(t) or {})}
                    for t, n in sorted(
                        self._miss_texts.items(), key=lambda kv: -kv[1])[:8]],
                "queue_depth": int(self._queue_depth),
                "queue_peak": int(self._queue_peak),
                "avg_queue_wait_ms": (
                    int(self._queue_wait_ms_sum / self._queue_wait_n)
                    if self._queue_wait_n else 0),
                "stt_total": int(self._stt_total),
                "stt_ok": int(self._stt_ok),
                "truncation_rejects": int(self._truncation_rejects),
            }

    def dump_prom(self) -> str:
        lines = [
            "# HELP avatar_voice_synth_total avatar_clone synth attempts",
            "# TYPE avatar_voice_synth_total counter",
            "# HELP avatar_voice_synth_ok_total avatar_clone synth successes",
            "# TYPE avatar_voice_synth_ok_total counter",
            "# HELP avatar_voice_prerender_hits_total prerendered voice reuses (zero GPU)",
            "# TYPE avatar_voice_prerender_hits_total counter",
            "# HELP avatar_voice_prerender_miss_total short-text prerender misses (stock gap)",
            "# TYPE avatar_voice_prerender_miss_total counter",
            "# HELP avatar_voice_queue_peak GPU serial queue peak depth",
            "# TYPE avatar_voice_queue_peak gauge",
            "# HELP avatar_voice_stt_total AvatarHub STT attempts",
            "# TYPE avatar_voice_stt_total counter",
            "# HELP avatar_voice_truncation_rejects_total truncated/garbage audio blocked by quality gate",
            "# TYPE avatar_voice_truncation_rejects_total counter",
            "# HELP avatar_voice_by_channel_total successful synth by channel",
            "# TYPE avatar_voice_by_channel_total counter",
            "# HELP avatar_voice_colloquial_total synth with colloquial rewrite (liveliness, text layer)",
            "# TYPE avatar_voice_colloquial_total counter",
            "# HELP avatar_voice_colloquial_llm_total synth with LLM colloquial rewrite",
            "# TYPE avatar_voice_colloquial_llm_total counter",
            "# HELP avatar_voice_colloquial_gen_total synth fed by generation-layer spoken variant",
            "# TYPE avatar_voice_colloquial_gen_total counter",
            "# HELP avatar_voice_paralinguistic_total synth with paralinguistic marks (liveliness, audio layer)",
            "# TYPE avatar_voice_paralinguistic_total counter",
        ]
        with self._lock:
            lines.append(f"avatar_voice_synth_total {self._synth_total}")
            lines.append(f"avatar_voice_synth_ok_total {self._synth_ok}")
            lines.append(f"avatar_voice_colloquial_total {self._colloquial}")
            lines.append(f"avatar_voice_colloquial_llm_total {self._colloquial_llm}")
            lines.append(f"avatar_voice_colloquial_gen_total {self._colloquial_gen}")
            lines.append(f"avatar_voice_paralinguistic_total {self._paralinguistic}")
            lines.append(f"avatar_voice_prerender_hits_total {self._prerender_hits}")
            lines.append(f"avatar_voice_prerender_miss_total {self._prerender_miss}")
            lines.append(f"avatar_voice_queue_peak {self._queue_peak}")
            lines.append(f"avatar_voice_stt_total {self._stt_total}")
            lines.append(
                f"avatar_voice_truncation_rejects_total {self._truncation_rejects}")
            for ch, n in sorted(self._by_channel.items()):
                lines.append(
                    f'avatar_voice_by_channel_total{{channel="{_esc(ch)}"}} {int(n)}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._synth_total = 0
            self._synth_ok = 0
            self._synth_fail = 0
            self._synth_fail_streak = 0
            self._last_synth_ok_ts = 0.0
            self._last_synth_fail_ts = 0.0
            self._latency_ms_sum = 0
            self._by_channel.clear()
            self._by_emotion.clear()
            self._colloquial = 0
            self._colloquial_llm = 0
            self._colloquial_gen = 0
            self._paralinguistic = 0
            self._prerender_hits = 0
            self._prerender_miss = 0
            self._miss_texts.clear()
            self._miss_personas.clear()
            self._queue_depth = 0
            self._queue_peak = 0
            self._queue_wait_ms_sum = 0
            self._queue_wait_n = 0
            self._stt_total = 0
            self._stt_ok = 0
            self._truncation_rejects = 0
            self._last_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[AvatarVoiceStats] = None
_LOCK = threading.Lock()


def get_avatar_voice_stats() -> AvatarVoiceStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = AvatarVoiceStats()
    return _SINGLETON


__all__ = ["AvatarVoiceStats", "get_avatar_voice_stats"]
