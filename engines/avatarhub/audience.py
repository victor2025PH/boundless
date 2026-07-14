"""观众提问队列（自托管创作者场景）。

设计要点：
- 与对话主链路【解耦】：本模块只负责"收集 + 排序 + 择优"，不直接驱动数字人开口。
  回答 = 调用方(主播控制台/自动应答)拿到问题文本后走现有 /api/converse，带 priority=0；
  主播自己说话 priority 高→自动插到观众问题前面（复用已实现的优先级准入）。
- 安全：观众输入不直接发声→杜绝"垃圾/不当内容让数字人乱说"。深度内容安全交由对话管线
  的输入安全闸门在【回答时】兜底；本层只做轻量入口校验(长度/空白/控制字符/限流/去重)。
- 纯内存、有界：问题是易逝的；队列、限流表、去重表都设上限，绝不无界增长。
- 线程安全：用一把轻锁护住状态（端点虽是 async，但加锁成本极低且对未来多线程/多请求稳妥）。
"""
from __future__ import annotations
import time
import threading
from collections import deque


def _clean_text(s: str, max_len: int) -> str:
    """去控制字符、压缩空白、截断。"""
    if not s:
        return ""
    s = "".join(ch for ch in s if ch == "\n" or ch >= " ")
    s = " ".join(s.split())
    return s[:max_len].strip()


class AudienceQuestion:
    __slots__ = ("id", "name", "text", "ts", "status", "likes", "likers")
    def __init__(self, qid: int, name: str, text: str):
        self.id = qid
        self.name = name
        self.text = text
        self.ts = time.time()
        self.status = "pending"      # pending | answered | dismissed
        self.likes = 0
        self.likers: set = set()     # 点赞过的 IP（每 IP 每题仅一次，防刷）

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "text": self.text,
                "ts": int(self.ts), "status": self.status, "likes": self.likes}


class AudienceQueue:
    def __init__(self, max_q: int = 200, per_ip_sec: float = 3.0,
                 max_len: int = 200, dedup_sec: float = 60.0, max_name: int = 24,
                 hot_halflife: float = 0.0, drop_policy: str = "reject"):
        self.max_q = max(1, int(max_q))
        self.per_ip_sec = max(0.0, float(per_ip_sec))
        self.max_len = max(1, int(max_len))
        self.max_name = max(1, int(max_name))
        self.dedup_sec = max(0.0, float(dedup_sec))
        # 人气热度半衰期(s)：>0 时赞的权重随问题变老指数衰减，老问题不会永久霸榜，
        # 新问题攒少量赞即可冒头。0=关闭(纯赞数排序，向后兼容)。
        self.hot_halflife = max(0.0, float(hot_halflife))
        # 队满策略：'reject'=拒收新问题(默认，向后兼容)；'evict_stale'=挤掉一条最冷最旧的
        # 待答问题给新问题让位（高负载下保持队列“新鲜”，避免被 5 分钟前的旧积压冻住，
        # 同时新热点仍能进场攒赞冒头）。被挤的记为 evicted（区别于主播主动 dismiss）。
        self.drop_policy = drop_policy if drop_policy in ("reject", "evict_stale") else "reject"
        self._q: "deque[AudienceQuestion]" = deque()
        self._by_id: dict = {}
        self._next_id = 1                 # 自增问题 id（可导出/回填，重启后不与旧 id 冲突）
        self._ip_last: dict = {}          # ip -> last submit ts（限流）
        self._recent: dict = {}           # text -> ts（去重）
        self._submit_ts: "deque[float]" = deque()   # 近期提交时刻（算提问速率）
        self._like_ts: "deque[float]" = deque()     # 近期点赞时刻（算点赞速率）
        self._lock = threading.Lock()
        self._stats = {"submitted": 0, "answered": 0, "dismissed": 0,
                       "rejected_rate": 0, "rejected_dup": 0, "rejected_full": 0,
                       "rejected_invalid": 0, "evicted": 0}

    # ── 内部：过期清理（限流/去重表有界）──────────────────────────
    def _gc(self, now: float):
        if self.per_ip_sec > 0:
            for ip in [k for k, t in self._ip_last.items() if now - t > max(self.per_ip_sec, 30)]:
                self._ip_last.pop(ip, None)
        if self.dedup_sec > 0:
            for tx in [k for k, t in self._recent.items() if now - t > self.dedup_sec]:
                self._recent.pop(tx, None)
        for dq in (self._submit_ts, self._like_ts):    # 速率窗仅留近 5 分钟，绝不无界
            while dq and now - dq[0] > 300:
                dq.popleft()

    def submit(self, text: str, name: str = "", ip: str = "") -> dict:
        now = time.time()
        with self._lock:
            self._gc(now)
            text = _clean_text(text, self.max_len)
            name = _clean_text(name, self.max_name) or "观众"
            if not text:
                self._stats["rejected_invalid"] += 1
                return {"ok": False, "reason": "问题为空"}
            if self.per_ip_sec > 0 and ip:
                last = self._ip_last.get(ip, 0.0)
                if now - last < self.per_ip_sec:
                    self._stats["rejected_rate"] += 1
                    wait = round(self.per_ip_sec - (now - last), 1)
                    return {"ok": False, "reason": f"提问太频繁，请 {wait}s 后再试"}
            if self.dedup_sec > 0 and text in self._recent:
                self._stats["rejected_dup"] += 1
                return {"ok": False, "reason": "刚问过相同问题了"}
            pending = sum(1 for q in self._q if q.status == "pending")
            if pending >= self.max_q:
                if self.drop_policy == "evict_stale" and self._evict_coldest(now):
                    pass   # 已挤出一条最冷最旧的，给新问题腾位
                else:
                    self._stats["rejected_full"] += 1
                    return {"ok": False, "reason": "提问太多，主播正在赶答，请稍后"}
            q = AudienceQuestion(self._next_id, name, text)
            self._next_id += 1
            self._q.append(q)
            self._by_id[q.id] = q
            if ip:
                self._ip_last[ip] = now
            if self.dedup_sec > 0:
                self._recent[text] = now
            self._stats["submitted"] += 1
            self._submit_ts.append(now)
            pos = sum(1 for x in self._q if x.status == "pending")  # 自己即队尾
            return {"ok": True, "id": q.id, "position": pos, "pending": pos}

    def _evict_coldest(self, now: float) -> bool:
        """队满时挤掉一条「最冷最旧」的待答问题（人气分最低，平分取最早）给新问题让位。
        用与 next_pending 同一套人气分→被挤的必是当前最不值得答的，新热点不受影响。
        调用方已持锁。返回是否成功挤出。"""
        victim = None
        victim_key = None
        for q in self._q:
            if q.status != "pending":
                continue
            key = (self._score(q, now), -q.ts)   # 分低、旧者优先被挤（取 min）
            if victim is None or key < victim_key:
                victim, victim_key = q, key
        if victim is None:
            return False
        victim.status = "evicted"
        self._stats["evicted"] += 1
        self._compact()
        return True

    def _score(self, q, now: float) -> float:
        """人气分：默认=赞数；开启半衰期后赞随问题变老指数衰减(防老问题霸榜)。"""
        if self.hot_halflife > 0 and q.likes:
            return q.likes * (0.5 ** ((now - q.ts) / self.hot_halflife))
        return float(q.likes)

    def list(self, status: str = "pending", limit: int = 50, order: str = "fifo") -> list:
        """order: 'fifo'=入列序；'likes'/'hot'=人气优先(分高者前，平分早者前；含热度衰减)。"""
        with self._lock:
            items = [q for q in self._q if (status is None or q.status == status)]
            if order in ("likes", "hot"):
                now = time.time()
                items = sorted(items, key=lambda q: (-self._score(q, now), q.ts))
            return [q.to_dict() for q in items[:max(1, int(limit))]]

    def like(self, qid: int, ip: str = "") -> dict | None:
        """给某未答问题点赞：每 IP 每题仅计一次（防刷）。返回 {ok, likes} 或 None(不存在/已处理)。"""
        with self._lock:
            q = self._by_id.get(int(qid))
            if q is None or q.status != "pending":
                return None
            if ip and ip in q.likers:
                return {"ok": True, "likes": q.likes, "dup": True}
            q.likes += 1
            if ip:
                q.likers.add(ip)
            self._like_ts.append(time.time())
            return {"ok": True, "likes": q.likes}

    def next_pending(self) -> dict | None:
        """取「最值得答」的未答问题：人气分高者优先(含热度衰减)，平分取最早。供自动应答用。
        无任何点赞时退化为纯 FIFO（向后兼容）。不改状态。"""
        with self._lock:
            now = time.time()
            best = None
            best_key = None
            for q in self._q:
                if q.status != "pending":
                    continue
                key = (self._score(q, now), -q.ts)   # 分高、早者优先(取 max)
                if best is None or key > best_key:
                    best, best_key = q, key
            return best.to_dict() if best else None

    def answer(self, qid: int) -> dict | None:
        """标记为已答，返回该问题（调用方据 text 走 converse, priority=0）。"""
        with self._lock:
            q = self._by_id.get(int(qid))
            if q is None or q.status != "pending":
                return None
            q.status = "answered"
            self._stats["answered"] += 1
            self._compact()
            return q.to_dict()

    def dismiss(self, qid: int) -> bool:
        with self._lock:
            q = self._by_id.get(int(qid))
            if q is None or q.status != "pending":
                return False
            q.status = "dismissed"
            self._stats["dismissed"] += 1
            self._compact()
            return True

    def clear(self) -> int:
        with self._lock:
            n = sum(1 for q in self._q if q.status == "pending")
            self._q.clear()
            self._by_id.clear()
            return n

    def _compact(self):
        """限制 _q/_by_id 内存：仅保留最近 2*max_q 条（含已答/忽略，便于回看），其余丢弃。"""
        cap = self.max_q * 2
        while len(self._q) > cap:
            old = self._q.popleft()
            self._by_id.pop(old.id, None)

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for q in self._q if q.status == "pending")

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            self._gc(now)
            pend = [q for q in self._q if q.status == "pending"]
            return {"pending": len(pend),
                    "total_held": len(self._q),
                    "max_q": self.max_q,
                    "top_likes": max((q.likes for q in pend), default=0),
                    "qpm": sum(1 for t in self._submit_ts if now - t <= 60),   # 近 1 分钟提问数
                    "likes_pm": sum(1 for t in self._like_ts if now - t <= 60),  # 近 1 分钟点赞数
                    "stats": dict(self._stats)}

    # ── 持久化：整体导出 / 回填（纯数据，无 I/O；落盘由调用方负责）───────────
    # 只导「问题清单 + id 计数 + 统计」。限流表/去重表/点赞IP 均为易逝表，刻意不导：
    # 重启后自然重建即可，且不落 IP（隐私 + 快照更小）。用于「直播中途重启不丢现场问答」。
    def export_state(self) -> dict:
        with self._lock:
            return {
                "next_id": self._next_id,
                "questions": [
                    {"id": q.id, "name": q.name, "text": q.text, "ts": q.ts,
                     "status": q.status, "likes": q.likes}
                    for q in self._q
                ],
                "stats": dict(self._stats),
            }

    def restore_state(self, data: dict) -> int:
        """从 export_state 的快照重建问题队列（仅应在启动/空队列时调用）。返回恢复条数。
        易逝表不恢复：重启后每 IP 可对同一问题再赞一次，属可接受的软性防刷。"""
        if not isinstance(data, dict):
            return 0
        questions = data.get("questions") or []
        with self._lock:
            self._q.clear()
            self._by_id.clear()
            restored = 0
            max_id = 0
            for d in questions:
                try:
                    qid = int(d["id"])
                    q = AudienceQuestion(qid, str(d.get("name") or "观众"),
                                         str(d.get("text") or ""))
                    q.ts = float(d.get("ts") or time.time())
                    q.status = str(d.get("status") or "pending")
                    q.likes = int(d.get("likes") or 0)
                    self._q.append(q)
                    self._by_id[qid] = q
                    max_id = max(max_id, qid)
                    restored += 1
                except Exception:
                    continue
            try:
                self._next_id = max(int(data.get("next_id") or 1), max_id + 1)
            except Exception:
                self._next_id = max_id + 1
            st = data.get("stats")
            if isinstance(st, dict):
                for k in list(self._stats.keys()):
                    if k in st:
                        try:
                            self._stats[k] = int(st[k])
                        except Exception:
                            pass
            self._compact()
            return restored
