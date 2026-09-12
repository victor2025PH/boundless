# -*- coding: utf-8 -*-
"""投递幂等钉（B41「同稿双投」修复，2026-08-22）。

事故背景
========
2026-08-21 内测实录（impl49 B41）：全自动真发打通当晚，同一组回复**字节级相同
×2** 发给真实客户。根因面：inbox 常规草稿按会话固定 ``source_id`` upsert——第二次
生成会把**已投递**的行原地复活（status→pending、sent_at→0），若两次生成产出同文
（同 prompt 短窗双触发），worker 会把同一份内容再投一遍；此外人工通过链与自动链
之间也缺一道「同稿只许出门一次」的最后闸。

``DeliverOnceRegistry``＝**每 worker 实例**的投递权登记表（纯内存、零 DB 依赖）：

- ``claim(conversation_id, draft_id, text)``：投递前申请。同 (会话,草稿) 在途 →
  拒 ``in_flight``；同 (会话,草稿) 已成功投递过**同指纹文本**（TTL 窗内）→ 拒
  ``dup_text``。拿到返回空串。
- ``release(..., delivered=True/False)``：投递结束释放在途位；成功才登记已投
  指纹（transient 失败释放后重试可再 claim）。

Q-30 E（#319，2026-09-13）第二把键 **(会话, 归一化文本)**：UG6KJG 实录——切档瞬间
``resume by=mode_select`` 放行让位改期载荷的同一 tick，同会话另一条新稿也过闸，两稿
``draft_id`` 不同、文本一字不差，(会话,草稿) 键各自成立 → 双发。现在同会话**同文**
在途 → ``in_flight``；同会话同文 :data:`DELIVERED_TEXT_TTL_SEC`（180s，与出站近重复守卫
同窗）内已出门 → ``dup_text``，不看 draft_id。归一化后短于 :data:`TEXT_KEY_MIN_LEN` 的
口头禅（「好的」「嗯嗯」）不上文本键——与 ``outbound_dup_guard.DEFAULT_MIN_LEN`` 同一
校准（30 天生产语料），短句连答两次是正常聊天不是双发。

作用域是**实例级**而非模块级（刻意）：生产部署里任一时刻只有一个持投递能力的
worker 实例（enabled=true 时人工/自动两条链共用同一实例；enabled=false 时只有
deliver_only 兜底实例），实例级已覆盖全部真实竞态面；「第二个自动循环」由
``AutosendWorker.run`` 的 ``_RUNNING_SVC_KEYS`` 防线独立兜底。模块级单例会让
测试替身跨用例串味（同名 conv/draft 是测试常态），精度换不来额外保护。

刻意边界：
- **不同文本的复活行放行**——客户又说了话、真的重新生成了一代内容，再投递是
  正确行为；本闸只杀「同稿」。
- **真失败重试可重发**（B41 语义）——重试项只在上一次投递**失败**（release 未登记
  指纹）后存在，重发同文本是 recoverable 的既定语义。Q-30 E 起 worker 不再按
  ``_attempt>0`` 整体跳过 claim：真失败＝无指纹＝claim 自然放行，语义一字不变；
  而让位改期回队的 ``_yield_defer`` 载荷不是重试，照常过闸（此前被「重试项」
  口径误豁免）。
- 跨重启的防线由 DB ``sent_at`` 标记（worker 成功后经 ``InboxStore.
  mark_draft_sent`` 补写）与出站近重复守卫（读 DB 消息镜像）分担。
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Dict, Tuple

# 已投指纹的记忆窗：窗内同稿同文再来一律拒（复活行 revive 场景）；
# 窗外视为新生命周期（同一会话隔天真的可能说同一句话）。
DELIVERED_TTL_SEC = 24 * 3600.0
# Q-30 E：同会话同文键的记忆窗，与出站近重复守卫 180s 同刻度。
DELIVERED_TEXT_TTL_SEC = 180.0
# 口头禅不上文本键（与 outbound_dup_guard.DEFAULT_MIN_LEN 同校准）。
TEXT_KEY_MIN_LEN = 12
# 在途位僵尸兜底：投递协程异常到连 finally 都没跑（进程被杀边缘），
# 超时自动失效，绝不把会话永久闸死。正常路径远短于此。
INFLIGHT_STALE_SEC = 30 * 60.0
_MAX_ENTRIES = 4096


def text_fp(text: str) -> str:
    """文本指纹：空白折叠后 sha1 前 16 位（只认「同一份字节内容」——本闸只杀
    同稿，语义近重复归 outbound_dup_guard）。"""
    norm = " ".join(str(text or "").split())
    return hashlib.sha1(norm.encode("utf-8", "ignore")).hexdigest()[:16]


def _norm_text(text: str) -> str:
    return " ".join(str(text or "").split())


class DeliverOnceRegistry:
    """单 worker 实例的「同稿只出门一次」登记表（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: Dict[str, float] = {}              # key -> claim ts
        self._delivered: Dict[str, Tuple[float, str]] = {}  # key -> (ts, fp)
        self._inflight_text: Dict[str, float] = {}          # text_key -> ts
        self._delivered_text: Dict[str, float] = {}         # text_key -> ts
        self._stats = {
            "claims": 0, "blocked_in_flight": 0, "blocked_dup_text": 0}

    @staticmethod
    def _key(conversation_id: str, draft_id: str) -> str:
        return f"{conversation_id or '-'}|{draft_id or '-'}"

    @staticmethod
    def _text_key(conversation_id: str, text: str) -> str:
        """(会话, 归一化文本) 键。短于 :data:`TEXT_KEY_MIN_LEN` → 空＝不上这把锁。"""
        if len(_norm_text(text)) < TEXT_KEY_MIN_LEN:
            return ""
        return f"{conversation_id or '-'}|#t:{text_fp(text)}"

    def _gc(self, now: float) -> None:
        """锁内调用：清过期在途位/已投指纹，防表无界增长。"""
        stale = now - INFLIGHT_STALE_SEC
        for k in [k for k, ts in self._inflight.items() if ts < stale]:
            self._inflight.pop(k, None)
        for k in [k for k, ts in self._inflight_text.items() if ts < stale]:
            self._inflight_text.pop(k, None)
        tcut = now - DELIVERED_TEXT_TTL_SEC
        for k in [k for k, ts in self._delivered_text.items() if ts < tcut]:
            self._delivered_text.pop(k, None)
        if len(self._delivered) > _MAX_ENTRIES:
            cutoff = now - DELIVERED_TTL_SEC
            for k in [k for k, (ts, _) in self._delivered.items()
                      if ts < cutoff]:
                self._delivered.pop(k, None)
            # 仍超限（短时间海量会话）→ 按时间序丢最旧的一半，保上界
            if len(self._delivered) > _MAX_ENTRIES:
                for k, _ in sorted(self._delivered.items(),
                                   key=lambda kv: kv[1][0])[
                        : len(self._delivered) // 2]:
                    self._delivered.pop(k, None)

    def has_delivered(self, conversation_id: str, draft_id: str,
                      text: str = "", *, now: float | None = None) -> bool:
        """上一轮是否已登记成功指纹（真失败重试判据：未登记才算真失败）。"""
        did = str(draft_id or "")
        if not did:
            return False
        ts = time.time() if now is None else float(now)
        key = self._key(str(conversation_id or ""), did)
        tkey = self._text_key(str(conversation_id or ""), text) if text else ""
        fp = text_fp(text) if text else ""
        with self._lock:
            rec = self._delivered.get(key)
            if rec and ts - rec[0] < DELIVERED_TTL_SEC:
                if not fp or rec[1] == fp:
                    return True
            if tkey:
                dts = self._delivered_text.get(tkey)
                if dts is not None and ts - dts < DELIVERED_TEXT_TTL_SEC:
                    return True
        return False

    def claim(self, conversation_id: str, draft_id: str, text: str, *,
              now: float | None = None) -> str:
        """申请该草稿本代内容的投递权。

        返回空串＝拿到（调用方**必须**在结束时 ``release``）；非空＝拒因：
        ``in_flight``（另一条链正在投同一稿 / 同会话同文）/ ``dup_text``
        （同稿同文已投，或同会话同文 180s 内已出门）。draft_id 为空（历史载荷
        / 测试替身）→ 直接放行不登记——闸门宁可漏过不可把无 id 的正常投递闸死。
        """
        did = str(draft_id or "")
        if not did:
            return ""
        ts = time.time() if now is None else float(now)
        conv = str(conversation_id or "")
        key = self._key(conv, did)
        tkey = self._text_key(conv, text)
        fp = text_fp(text)
        with self._lock:
            self._gc(ts)
            self._stats["claims"] += 1
            if key in self._inflight or (tkey and tkey in self._inflight_text):
                self._stats["blocked_in_flight"] += 1
                return "in_flight"
            rec = self._delivered.get(key)
            if rec and rec[1] == fp and ts - rec[0] < DELIVERED_TTL_SEC:
                self._stats["blocked_dup_text"] += 1
                return "dup_text"
            if tkey:
                dts = self._delivered_text.get(tkey)
                if dts is not None and ts - dts < DELIVERED_TEXT_TTL_SEC:
                    self._stats["blocked_dup_text"] += 1
                    return "dup_text"
            self._inflight[key] = ts
            if tkey:
                self._inflight_text[tkey] = ts
        return ""

    def release(self, conversation_id: str, draft_id: str, *,
                delivered: bool, text: str = "",
                now: float | None = None) -> None:
        """释放在途位；``delivered=True`` 时登记已投指纹（同稿同文 / 同会话同文）。"""
        did = str(draft_id or "")
        if not did:
            return
        ts = time.time() if now is None else float(now)
        conv = str(conversation_id or "")
        key = self._key(conv, did)
        tkey = self._text_key(conv, text) if text else ""
        with self._lock:
            self._inflight.pop(key, None)
            if tkey:
                self._inflight_text.pop(tkey, None)
            if delivered:
                self._delivered[key] = (ts, text_fp(text) if text else "")
                if tkey:
                    self._delivered_text[tkey] = ts

    def stats_snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._stats)


__all__ = [
    "DELIVERED_TTL_SEC", "DELIVERED_TEXT_TTL_SEC", "INFLIGHT_STALE_SEC",
    "TEXT_KEY_MIN_LEN", "DeliverOnceRegistry", "text_fp",
]

