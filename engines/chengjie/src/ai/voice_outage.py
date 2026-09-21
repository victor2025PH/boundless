"""语音出站断档台账（进程级单例）。

为什么需要它（2026-08-02 实锤）：zhiliao 语音链断了 5 天——hub 音色档 404 →
tts_pipeline 的 voice_consistency=strict 全部拒发——**零告警**。既有
``health_watchdog._check_avatar_voice`` 的 hang 检测依赖「7852 探针绿 + 失败
streak 新鲜度（默认 20min）」，对「低流量下几天里零星请求全部失败」不敏感：
每次失败都不够新鲜、探针又一直是绿的。

本台账记录**每一次「已决定发语音」的最终成败**（A 线 sender / B 线 autosend /
主动线），供 ``HealthWatchdog._check_voice_outage`` 按滚动窗判
「窗口内尝试 ≥N 次且 0 成功」——流量再低，只要有请求全在失败就会响。

风格对齐 ``avatar_voice_stats``：无新增依赖、线程安全；record 任何异常都吞掉，
绝不阻塞语音主链路；**绝不记录文本原文**，只记 (ts, ok, source, reason)。
``source`` ∈ aline|autosend|manual|wa_rpa|mr_rpa|desktop_bridge（不硬校验，小写归一后原样入账；
新链请走模块级 ``note_voice_attempt`` 一行式入口，别各自复制 try/except）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional


def _ledger_path() -> Optional[Path]:
    """台账落盘路径（2026-08-05 P2：跨重启记忆）。

    ``AITR_DATA_DIR``（conftest 在 import 期指向进程级 tmp → 测试天然隔离）→
    否则 CWD 相对 ``logs/``（服务进程 CWD=实例数据根，落点正确=C 类数据语义；
    从引擎根跑的 CLI 落仓库 logs/ 已 gitignore，无害）。任何异常返回 None=
    退化为纯内存台账，绝不因路径问题影响语音主链。
    """
    try:
        base = str(os.environ.get("AITR_DATA_DIR") or "").strip()
        root = Path(base) if base else Path(".")
        return root / "logs" / "voice_outage_ledger.json"
    except Exception:
        return None


def _prom_lbl(v: Any) -> str:
    """Prometheus 标签值消毒（source 虽是内部字面量，但它经 record 入参进来）。

    只留 ``[a-z0-9_]`` 并截断——反斜杠/引号/换行会直接把 exposition 文本弄成
    不可解析的，抓取端整份 /metrics 一起挂（不是「这一行丢了」）。
    """
    s = "".join(c for c in str(v or "").lower() if c.isalnum() or c == "_")
    return (s or "unknown")[:32]


class VoiceOutageLedger:
    # bounded：防长期运行撑爆内存。
    # 2026-08-22 由 200 提到 600：接进 wa_rpa/mr_rpa 后台账从 3 条链变 5 条，
    # 而这两条是 RPA 轮询链（量级远高于坐席手动），200 条在忙日可能装不满 24h
    # ——那会让「24h 窗 0 成功」的判据**悄悄缩窗**：一条链早上全灭、下午被别的链
    # 的事件挤出 deque，告警就再也不响了。600 条整写仍是 ~40KB JSON（每次记账
    # 一写，微秒级），换掉这个静默缩窗值得。真被挤到不足 24h 时 snapshot 会带
    # ``truncated``（见 outage_snapshot）——宁可说「我只看得到 9h」也不装作 24h。
    _MAXLEN = 600
    _REASON_LEN = 80
    _SCHEMA_V = 1

    __slots__ = ("_lock", "_events", "_last_ok_ts", "_last_fail_ts",
                 "_consecutive_fails", "_path")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # (ts, ok, source, reason)
        self._events: deque = deque(maxlen=self._MAXLEN)
        self._last_ok_ts = 0.0
        self._last_fail_ts = 0.0
        self._consecutive_fails = 0
        self._path = _ledger_path()
        self._load()

    # ── 持久化（P2 2026-08-05：修「重启即失忆」）────────────────────────────
    # 为什么必须落盘：本台账是 watchdog「滚动窗全败告警」与 effective-config
    # 「hub recent_failures 风险预告」的唯一数据源，而本机重启频繁（多 agent
    # 线攒批装载）——断档期间一次重启就把「正在断档」的证据清零，告警和预告
    # 双双哑火，恰好复刻它要防的那类「5 天零告警」事故。快照式整写（≤200 行）
    # 而非 append 日志：体积有界免轮转，load 即完整状态。全程 best-effort。
    def _load(self) -> None:
        p = self._path
        if p is None:
            return
        try:
            if not p.is_file():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("v") != self._SCHEMA_V:
                return
            rows = data.get("events") or []
            for r in rows[-self._MAXLEN:]:
                try:
                    ts, ok, src, rsn = r
                    if not ok and is_capability_skip(str(rsn)):
                        continue
                    self._events.append(
                        (float(ts), bool(ok), str(src), str(rsn)))
                except Exception:
                    continue
            self._last_ok_ts = float(data.get("last_ok_ts") or 0.0)
            self._last_fail_ts = float(data.get("last_fail_ts") or 0.0)
            self._consecutive_fails = int(data.get("consecutive_fails") or 0)
        except Exception:
            # 坏文件/坏形＝当空台账起步；下次成功 record 会原子覆盖掉坏文件
            self._events.clear()
            self._last_ok_ts = 0.0
            self._last_fail_ts = 0.0
            self._consecutive_fails = 0

    def _save_locked(self) -> None:
        """调用方须持有 self._lock。原子写（tmp→replace）防半写坏文件。"""
        p = self._path
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({
                "v": self._SCHEMA_V,
                "events": [list(e) for e in self._events],
                "last_ok_ts": self._last_ok_ts,
                "last_fail_ts": self._last_fail_ts,
                "consecutive_fails": self._consecutive_fails,
            }, ensure_ascii=False)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, p)
        except Exception:
            pass

    def record_voice_attempt(self, ok: bool, source: str,
                             reason: str = "") -> None:
        """记一次「已决定发语音」的最终成败。

        调用约定：只在语音被触发且真正尝试之后记账——trigger 未命中/策略判文字
        的早退**不记**（那不是断档，是正常决策）。``reason`` 用现场已有的错误
        字符串（synth_failed/edge_rejected/deliver_failed…），截断防撑爆。

        R87 P2-2：``clone_lang_unsupported`` / ``clone_lang_garbled`` 是语种能力缺口
        （按设计回落文字），不是链路断档——不入台账，避免日语会话 3 条就把看门狗
        打成「语音出站断档 3/3」。
        """
        try:
            src = str(source or "").strip().lower() or "unknown"
            rsn = str(reason or "").strip()[: self._REASON_LEN]
        except Exception:
            src, rsn = "unknown", ""
        if not ok and is_capability_skip(rsn):
            return
        with self._lock:
            ts = time.time()
            self._events.append((ts, bool(ok), src, rsn))
            if ok:
                self._last_ok_ts = ts
                self._consecutive_fails = 0
            else:
                self._last_fail_ts = ts
                self._consecutive_fails += 1
            # 每次记账即快照（语音尝试是人/AI 发送级频率，几十次/天；
            # ≤200 行 JSON 整写 ≈ 微秒级，换「重启零丢失」）
            self._save_locked()

    def outage_snapshot(self, now: Optional[float] = None, *,
                        window_hours: float = 24.0) -> Dict[str, Any]:
        """滚动窗统计快照（无副作用）。

        键名 ``attempts_24h``/``ok_24h`` 是对外契约名（watchdog/看板按此读），
        实际窗口由 ``window_hours`` 决定（默认 24）；``consecutive_fails`` 是
        进程级连败 streak（成功清零，不受窗口影响）。

        ``truncated``/``effective_window_hours``＝**诚实字段**：事件 deque 有界，
        高流量下最老的事件会被挤掉，此时名义 24h 窗其实只覆盖了几小时。不说的话
        「窗内 0 成功」这个判据会随流量悄悄缩窗（早上全灭的那条链，下午就被别的
        链的事件挤出去、告警自动闭嘴），是与「绿灯说谎」同一类的失真。
        """
        ts = float(now if now is not None else time.time())
        cutoff = ts - max(0.0, float(window_hours)) * 3600.0
        with self._lock:
            rows = [e for e in self._events if e[0] >= cutoff]
            fail_reasons: Dict[str, int] = {}
            by_source: Dict[str, Dict[str, int]] = {}
            ok_n = 0
            for _ts, _ok, _src, _rsn in rows:
                s = by_source.setdefault(
                    _src, {"attempts": 0, "ok": 0, "fail_reasons": {}})
                s["attempts"] += 1
                if _ok:
                    ok_n += 1
                    s["ok"] += 1
                else:
                    key = _rsn or "unknown"
                    fail_reasons[key] = fail_reasons.get(key, 0) + 1
                    # 逐链原因（2026-08-22）：单链断档告警若报全局 fail_reasons，
                    # 会把别的链的原因混进来（「share_skip_」与「hub 合成超时」
                    # 是两种完全不同的活），运维照着修错地方。
                    sr = s["fail_reasons"]
                    sr[key] = int(sr.get(key) or 0) + 1
            # 缩窗自陈：deque 满 && 最老事件仍落在窗内 ⇒ 更老的已被挤掉，
            # 真实覆盖只到最老那条为止。未满/为空一律不声称缩窗。
            truncated = False
            eff_hours = float(window_hours)
            if len(self._events) >= self._MAXLEN and self._events:
                oldest = float(self._events[0][0])
                if oldest >= cutoff:
                    truncated = True
                    eff_hours = max(0.0, (ts - oldest) / 3600.0)
            return {
                "attempts_24h": len(rows),
                "ok_24h": ok_n,
                "window_hours": float(window_hours),
                "effective_window_hours": round(eff_hours, 2),
                "truncated": truncated,
                "last_ok_ts": float(self._last_ok_ts),
                "last_fail_ts": float(self._last_fail_ts),
                "consecutive_fails": int(self._consecutive_fails),
                "fail_reasons": fail_reasons,
                "by_source": by_source,
            }


    def dump_prom(self) -> str:
        """Prometheus 文本行（P2 2026-08-05；聚合口在 drafts_routes /metrics）。

        gauge 语义：滚动 24h 窗快照（非累计 counter——deque 会自然滚出）。

        ⚠ **by_source 现在必须展开**（2026-08-22 反转了首版「价值密度低，不展开」
        的判断）：接进 wa_rpa/mr_rpa 后台账有 5 条链，各走不同 TTS 后端与投递通道，
        「一条全灭、其余正常」是常态形态——此时上面三个汇总 gauge **全是好看的**
        （ok_24h 很大、consecutive_fails=0），任何基于它们的告警规则都不会响，而
        那条链的客户已经完全收不到语音。当晚事故就是这个形态。基数天然有界
        （source 是各链里的字面量，5 个），展开的成本远小于「Prometheus 侧永远
        无法表达单链断档」的代价。
        """
        snap = self.outage_snapshot()
        lines = [
            "# HELP voice_outage_attempts_24h Voice send attempts in rolling 24h window (all chains)",
            "# TYPE voice_outage_attempts_24h gauge",
            f"voice_outage_attempts_24h {int(snap.get('attempts_24h') or 0)}",
            "# HELP voice_outage_ok_24h Successful voice sends in rolling 24h window",
            "# TYPE voice_outage_ok_24h gauge",
            f"voice_outage_ok_24h {int(snap.get('ok_24h') or 0)}",
            "# HELP voice_outage_consecutive_fails Consecutive voice send failures (resets on success)",
            "# TYPE voice_outage_consecutive_fails gauge",
            f"voice_outage_consecutive_fails {int(snap.get('consecutive_fails') or 0)}",
        ]
        by_source = snap.get("by_source") or {}
        if isinstance(by_source, dict) and by_source:
            lines += [
                "# HELP voice_outage_source_attempts_24h Voice send attempts per chain (rolling 24h)",
                "# TYPE voice_outage_source_attempts_24h gauge",
            ]
            for src, st in sorted(by_source.items()):
                if not isinstance(st, dict):
                    continue
                lines.append(
                    f'voice_outage_source_attempts_24h{{source="{_prom_lbl(src)}"}} '
                    f"{int(st.get('attempts') or 0)}")
            lines += [
                "# HELP voice_outage_source_ok_24h Successful voice sends per chain (rolling 24h)",
                "# TYPE voice_outage_source_ok_24h gauge",
            ]
            for src, st in sorted(by_source.items()):
                if not isinstance(st, dict):
                    continue
                lines.append(
                    f'voice_outage_source_ok_24h{{source="{_prom_lbl(src)}"}} '
                    f"{int(st.get('ok') or 0)}")
        return "\n".join(lines) + "\n"


#: 按设计回落文字的「正常业务态」原因（不入断档台账）：
#: - 语种能力缺口（克隆声不支持 / 成品念错该语种，R87）
#: - 桌面桥（2026-09-19 P2）：坐席正在用麦（开会/通话）、语音专属配额到顶——回复本身照发（文字），不是链路断
#: - 桌面桥策略拒（``driver_policy``：非工作时段 / 日上限 / 对方没来过信）：文字同样发不出去，是业务闸门不是语音链
_DESIGNED_SKIP_MARKERS = ("clone_lang_unsupported", "clone_lang_garbled",
                          "mic_busy", "voice_daily_cap", "voice_per_peer_daily_cap",
                          "driver_policy")


def is_capability_skip(reason: str) -> bool:
    """按设计回落文字的原因 ≠ 语音链路断档（详见 ``_DESIGNED_SKIP_MARKERS``）。"""
    r = str(reason or "").lower()
    return any(m in r for m in _DESIGNED_SKIP_MARKERS)


_SINGLETON: Optional[VoiceOutageLedger] = None
_LOCK = threading.Lock()


def get_voice_outage() -> VoiceOutageLedger:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = VoiceOutageLedger()
    return _SINGLETON


def note_voice_attempt(ok: bool, source: str, reason: str = "") -> None:
    """记账的**唯一推荐入口**（永不抛，永不阻塞调用方）。

    为什么不让各链自己 ``get_voice_outage().record_voice_attempt(...)``：那要求每个
    调用点自带 try/except——而它们全在发送热路上，漏一个就是「记账把发消息搞崩」。
    2026-08-22 实测的教训更直接：**WhatsApp / Messenger 两条 RPA 语音链压根没接台账**
    （只有 aline / autosend / manual 三条接了），于是那两条链的语音断档对看门狗、
    ops 卡、Prometheus 全部隐形——当晚 hub 引擎被挤到每发必超时，台账却显示
    24h「24 次尝试 24 次成功」，故障最后是**靠老板的耳朵**发现的。一行式入口就是
    为了让「再加一条语音链」时接台账的成本低到没有借口不接。

    ``source`` 是分桶键（aline/autosend/manual/wa_rpa/mr_rpa…），随 by_source 出看板；
    ``reason`` 只在 ok=False 时有意义，直接传现场错误串（``hub_synth_timing_out:...``
    这类前缀正是运维定位的依据，别自己改写成人话）。
    """
    try:
        get_voice_outage().record_voice_attempt(bool(ok), source, reason)
    except Exception:
        pass


def reset_for_test() -> None:
    """测试隔离：丢弃单例（下次 get 重建**空**台账）。生产代码勿调。

    P2 落盘后必须连持久化文件一起清——否则重建时 ``_load`` 会把上一个测试的
    台账读回来，「下次 get＝空」的既有契约被静默破坏（所有断言 attempts 计数
    的测试都会串味）。模拟「跨重启恢复」的测试请直接构造 ``VoiceOutageLedger()``
    实例，别走本函数。
    """
    global _SINGLETON
    with _LOCK:
        _SINGLETON = None
    try:
        p = _ledger_path()
        if p is not None and p.is_file():
            p.unlink()
    except Exception:
        pass


__all__ = [
    "VoiceOutageLedger", "get_voice_outage", "note_voice_attempt",
    "reset_for_test", "is_capability_skip",
]
