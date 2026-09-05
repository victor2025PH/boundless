# -*- coding: utf-8 -*-
"""入站爆发合并门禁（2026-08-02 P0，防同义双发·治因）。

事故金标（WA 实锤）：客户 7 秒内连发「你叫什么名字」「现在几点了」，两条各自
触发独立拟稿 → 两次 LLM 生成互不知情 → 同义双发。合并后应只拟一次稿、
peer_text 含两条全文。

测试用注入 timer_factory + now_fn，零 sleep 可复现。
"""

from __future__ import annotations

from src.inbox.inbound_debounce import (
    DEFAULT_VOICE_WINDOW_SEC,
    InboundMerger,
    is_fragment_burst,
    is_voice_piece,
    merge_burst_texts,
    resolve_merge_cfg,
)


class FakeTimer:
    """手动触发的假计时器：记录 delay，测试端显式 fire。"""

    def __init__(self, delay, fn):
        self.delay = delay
        self.fn = fn
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.fn()


class Harness:
    def __init__(self, **kw):
        self.calls = []
        self.timers = []
        self.now = 1000.0

        def _timer_factory(delay, fn):
            t = FakeTimer(delay, fn)
            self.timers.append(t)
            return t

        self.merger = InboundMerger(
            lambda conv, text: self.calls.append((conv, text)),
            timer_factory=_timer_factory,
            now_fn=lambda: self.now,
            **kw,
        )

    def last_timer(self):
        return self.timers[-1]


def _conv(cid="conv-1", **extra):
    d = {"conversation_id": cid, "platform": "whatsapp"}
    d.update(extra)
    return d


def test_burst_merges_into_single_fire():
    """事故金标：窗内两条 → 只拟一次稿，合并全文保序。"""
    h = Harness(window_sec=8, max_wait_sec=25)
    h.merger.push(_conv(), "你叫什么名字")
    assert h.calls == []                      # 静默窗内不开火
    h.now += 7
    h.merger.push(_conv(), "现在几点了")
    assert h.timers[0].cancelled              # 旧窗被取消重开
    h.now += 8
    h.last_timer().fire()
    assert len(h.calls) == 1
    assert h.calls[0][1] == "你叫什么名字\n现在几点了"
    snap = h.merger.stats_snapshot()
    assert snap["merged_bursts"] == 1 and snap["merged_messages"] == 2
    assert snap["fired"] == 1 and snap["pending"] == 0


def test_single_message_fires_alone():
    h = Harness()
    h.merger.push(_conv(), "在吗")
    h.last_timer().fire()
    assert h.calls == [(h.calls[0][0], "在吗")]
    assert h.merger.stats_snapshot()["merged_bursts"] == 0


def test_conversations_are_isolated():
    h = Harness()
    h.merger.push(_conv("a"), "A的消息")
    h.merger.push(_conv("b"), "B的消息")
    assert h.merger.pending_count() == 2
    for t in list(h.timers):
        t.fire()
    assert sorted(c[1] for c in h.calls) == ["A的消息", "B的消息"]


def test_max_wait_caps_endless_typing():
    """客户连续打字不停：从首条起算 max_wait 到点强制开火，不无限等。"""
    h = Harness(window_sec=8, max_wait_sec=20)
    h.merger.push(_conv(), "第1条")
    for i in range(2, 5):
        h.now += 7                            # 每 7s 来一条，窗口本会一直续
        h.merger.push(_conv(), f"第{i}条")
    # 首条起已过 21s > max_wait=20 → push 内应立即开火而非再开窗
    assert len(h.calls) == 1
    assert h.calls[0][1].count("\n") == 3     # 4 条合并


def test_max_texts_fires_immediately():
    # 正常长度消息达 max_texts 上限立即开火（碎片形态另有放宽，见下方碎片段）
    h = Harness(max_texts=3)
    h.merger.push(_conv(), "第一条消息")
    h.merger.push(_conv(), "第二条消息")
    assert h.calls == []
    h.merger.push(_conv(), "第三条消息")       # 达上限立即开火
    assert len(h.calls) == 1
    assert h.calls[0][1] == "第一条消息\n第二条消息\n第三条消息"


def test_latest_conv_dict_wins():
    """窗内后续消息带更新的 conv 元数据 → 开火时用最新 conv。"""
    h = Harness()
    h.merger.push(_conv(last_message="旧"), "a")
    h.merger.push(_conv(last_message="新"), "b")
    h.last_timer().fire()
    assert h.calls[0][0]["last_message"] == "新"


def test_callback_exception_swallowed():
    boom = InboundMerger(
        lambda c, t: (_ for _ in ()).throw(RuntimeError("boom")),
        timer_factory=lambda d, f: FakeTimer(d, f))
    boom.push(_conv(), "x")
    boom.flush_all()                          # 不抛、pending 清空
    assert boom.pending_count() == 0
    assert boom.fired == 1


def test_no_conversation_id_falls_back_to_composite_key():
    h = Harness()
    c = {"platform": "whatsapp", "account_id": "acc1", "chat_key": "639xxx"}
    h.merger.push(dict(c), "a")
    h.merger.push(dict(c), "b")
    assert h.merger.pending_count() == 1      # 同复合键并到一个窗


def test_merge_burst_texts_skips_blank():
    assert merge_burst_texts(["a", "  ", "", "b "]) == "a\nb"


def test_resolve_merge_cfg():
    assert resolve_merge_cfg(None)["enabled"] is False
    assert resolve_merge_cfg({})["enabled"] is False
    cfg = resolve_merge_cfg({"inbound_merge": {
        "enabled": True, "window_sec": 5, "max_wait_sec": 30, "max_texts": 4}})
    assert cfg["enabled"] is True
    assert cfg["window_sec"] == 5.0 and cfg["max_wait_sec"] == 30.0
    assert cfg["max_texts"] == 4
    # 碎片上限有默认值且可覆写
    assert cfg["frag_max_wait_sec"] == 45.0 and cfg["frag_max_texts"] == 12
    cfg2 = resolve_merge_cfg({"inbound_merge": {
        "frag_max_wait_sec": 60, "frag_max_texts": 20}})
    assert cfg2["frag_max_wait_sec"] == 60.0 and cfg2["frag_max_texts"] == 20


# ── 碎片拼句（2026-08-12「你/是/个/大/傻/子」实录）──────────────────────────
def test_fragment_burst_detection():
    assert is_fragment_burst(["你", "是", "个"]) is True
    assert is_fragment_burst(["你", "是"]) is False           # 2 条不算
    assert is_fragment_burst(["嗯", "好的呀今天不错", "哦"]) is False  # 仅 2 碎片
    assert is_fragment_burst([]) is False


def test_merge_fragments_join_into_sentence():
    """事故金标：一行一字连发 → 无分隔拼成一句，不留换行。"""
    assert merge_burst_texts(["你", "是", "个", "大", "傻", "子"]) == "你是个大傻子"


def test_merge_mixed_fragments_and_sentences():
    # 整句（>2 字）独立成行，其后碎片段拼句
    assert merge_burst_texts(["刚到家啦", "你", "是", "个", "笨", "蛋"]) == \
        "刚到家啦\n你是个笨蛋"
    # 前导 2 字短条与碎片 run 相邻会被一并拼句（「在吗你是个笨蛋」LLM 理解无损，
    # 误分行让每字被当独立消息的代价更大——刻意取粘连）
    assert merge_burst_texts(["在吗", "你", "是", "个", "笨", "蛋"]) == "在吗你是个笨蛋"
    # 不足 3 条的短消息不粘（「嗯」「好」是正常聊天）
    assert merge_burst_texts(["嗯", "好"]) == "嗯\n好"


def test_fragment_burst_relaxes_max_texts():
    """碎片模式：max_texts=6 不再截断一句 8 个字的话，整句一窗。"""
    h = Harness(max_texts=6, frag_max_texts=12)
    for ch in "你是不是不理我了":                 # 8 条超短碎片
        h.merger.push(_conv(), ch)
    assert h.calls == []                          # 未被 6 条上限切断
    h.last_timer().fire()
    assert len(h.calls) == 1
    assert h.calls[0][1] == "你是不是不理我了"
    assert h.merger.stats_snapshot()["frag_bursts"] == 1


def test_fragment_burst_still_capped_by_frag_limits():
    h = Harness(max_texts=4, frag_max_texts=6)
    for ch in "你是个大傻子":                     # 第 6 条达 frag 上限立即开火
        h.merger.push(_conv(), ch)
    assert len(h.calls) == 1 and h.calls[0][1] == "你是个大傻子"


def test_fragment_burst_relaxes_max_wait():
    """碎片模式下 max_wait 换用 frag_max_wait（慢速逐字打不被硬上限切窗）。"""
    h = Harness(window_sec=8, max_wait_sec=20, frag_max_wait_sec=60)
    h.merger.push(_conv(), "你")
    for i, ch in enumerate("是个大傻"):
        h.now += 6
        h.merger.push(_conv(), ch)               # 首条起 24s > max_wait=20
    assert h.calls == []                          # 碎片模式未强制开火
    h.last_timer().fire()
    assert h.calls[0][1] == "你是个大傻"


# ── 语音条单独合并窗（K-3 F / J-2 顺手项，2026-09-05）───────────────────────
# 88MP86 BABY BEAR 09-04 20:39–22:25 `guard=fresh post_humanize 延迟窗内客户插话`
# 30+ 次：客户连发语音条，8s 窗每条都到点各自拟稿再被 fresh-guard 放弃＝白烧 LLM。

def test_voice_piece_detection():
    assert is_voice_piece("[语音]") is True
    assert is_voice_piece("  [语音] 转写在这") is True         # 预览形态也认
    assert is_voice_piece("在吗", {"media_type": "voice"}) is True   # conv 信号（前向兼容）
    assert is_voice_piece("在吗", {"content_type": "VOICE"}) is True
    assert is_voice_piece("在吗") is False
    assert is_voice_piece("[图片]") is False
    assert is_voice_piece("", None) is False
    assert is_voice_piece(None, {"media_type": ""}) is False


def test_voice_burst_uses_longer_window_still_capped_by_max_wait():
    """语音条 → 静默窗按 voice_window_sec（15s）开，而不是 8s；硬上限照旧约束。"""
    h = Harness(window_sec=8, max_wait_sec=25, voice_window_sec=15)
    h.merger.push(_conv(), "[语音]")
    assert h.last_timer().delay == 15.0           # 语音窗，不是 8
    h.now += 12                                   # 8s 窗早到点了，15s 窗还在 → 第二条并入
    h.merger.push(_conv(), "[语音]")
    assert h.timers[0].cancelled
    # 首条起 12s，硬上限 25s 只剩 13s < 15s 语音窗 → 取先到者
    assert h.last_timer().delay == 13.0
    assert h.calls == []
    h.last_timer().fire()
    assert len(h.calls) == 1
    assert h.calls[0][1] == "[语音]\n[语音]"
    snap = h.merger.stats_snapshot()
    assert snap["merged_bursts"] == 1 and snap["voice_windows"] == 2


def test_text_window_unchanged_and_text_after_voice_uses_text_window():
    """文本窗不变：纯文本仍是 8s；语音之后来文本 → 客户已在打字，回常规窗。"""
    h = Harness(window_sec=8, max_wait_sec=25, voice_window_sec=15)
    h.merger.push(_conv(), "你叫什么名字")
    assert h.last_timer().delay == 8.0
    h.now += 3
    h.merger.push(_conv(), "[语音]")
    assert h.last_timer().delay == 15.0
    h.now += 3
    h.merger.push(_conv(), "刚发的听到了吗")
    assert h.last_timer().delay == 8.0            # 最新一条是文本 → 常规窗
    assert h.merger.stats_snapshot()["voice_windows"] == 1


def test_voice_window_defaults_and_never_below_text_window():
    h = Harness(window_sec=8)
    h.merger.push(_conv(), "[语音]")
    assert h.last_timer().delay == DEFAULT_VOICE_WINDOW_SEC == 15.0
    # 配置写小了（voice 2s < window 8s）→ 按常规窗兜底，语音永远不比文本更急
    h2 = Harness(window_sec=8, voice_window_sec=2)
    h2.merger.push(_conv(), "[语音]")
    assert h2.last_timer().delay == 8.0
    # 旧调用方不传 → 不抛、走默认
    assert resolve_merge_cfg({"inbound_merge": {}})["voice_window_sec"] == 15.0
    assert resolve_merge_cfg({"inbound_merge": {"voice_window_sec": 20}})["voice_window_sec"] == 20.0


def test_autodraft_wiring_passes_voice_window():
    """接线钉：autodraft_helpers 构造 InboundMerger 时把配置里的 voice_window_sec 传进去
    ——否则配置键是摆设。"""
    import inspect

    from src.inbox import autodraft_helpers as AH
    src = inspect.getsource(AH)
    assert 'voice_window_sec=_im_cfg.get("voice_window_sec")' in src
