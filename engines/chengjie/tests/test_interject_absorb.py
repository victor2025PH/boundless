"""A 线插话吸收门禁（``src/client/interject_absorb.py``，2026-08-02）。

产品语义（Stephanie2「主动等待」）：客户连发短消息 → 出队口合并成一条输入；
生成/humanize 的几秒窗口里客户补话 → 发送前两道关口放弃过期回复（新消息的
处理天然带旧上下文，一条新回复覆盖两问）。

覆盖：cfg 解析默认值/坏形、can_merge 各分支（跨 chat/媒体占位/媒体附件/超龄/
缺时间戳/语音转录文本）、merge_texts、should_abort 容差、入站注册表有界性、
enabled=false 旁路（默认关 + 接线静态门控钉）、两关口与出队合并的接线静态钉。
纯函数零 IO；注册表进程级，每例隔离。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.client import interject_absorb as ia  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_registry():
    ia._reset_registry_for_tests()
    yield
    ia._reset_registry_for_tests()


def _item(chat_id=100, text="你好", enq_ts=1000.0, message=None, **extra):
    """telegram_client._process_message 入队的 msg_data 实际形态（探明现场）。"""
    d = {
        "message": message,
        "user_id": 42,
        "username": "peer",
        "text": text,
        "chat_id": chat_id,
        "image_ocr_text": "",
        "_trigger_path": None,
        "_is_voice_msg": False,
        "_peer_audio_emotion": None,
        "voice_media_ref": "",
        "_enq_ts": enq_ts,
    }
    d.update(extra)
    return d


class _FakeMsg:
    """鸭子型 pyrogram Message：只带媒体属性槽位。"""

    def __init__(self, **attrs):
        for k in ("photo", "document", "video", "video_note",
                  "animation", "sticker", "voice", "audio"):
            setattr(self, k, attrs.get(k))


# ── 配置解析 ────────────────────────────────────────────────────────


def test_parse_cfg_defaults_disabled():
    """新子系统铁律：默认 enabled=False；max_merge=3 / max_age_sec=45；
    生成前安静窗默认 0＝整段关闭。"""
    for cfg in (None, {}, {"telegram": {}}):
        out = ia.parse_interject_cfg(cfg)
        assert out["enabled"] is False
        assert out["max_merge"] == 3 and out["max_age_sec"] == 45.0
        assert out["pregen_quiet_sec"] == 0.0
        assert out["pregen_frag_quiet_sec"] == 0.0
        assert out["pregen_max_wait_sec"] == 20.0


def test_parse_cfg_override_and_bad_shapes():
    cfg = {"telegram": {"interject_absorb": {
        "enabled": True, "max_merge": 5, "max_age_sec": 10}}}
    out = ia.parse_interject_cfg(cfg)
    assert out["enabled"] is True
    assert out["max_merge"] == 5 and out["max_age_sec"] == 10.0
    # 坏形回默认；max_merge 下限 1（0/负数不许把本条也合没）
    bad = {"telegram": {"interject_absorb": "yes"}}
    assert ia.parse_interject_cfg(bad)["enabled"] is False
    weird = {"telegram": {"interject_absorb": {
        "enabled": True, "max_merge": 0, "max_age_sec": "abc"}}}
    out2 = ia.parse_interject_cfg(weird)
    assert out2["max_merge"] == 1 and out2["max_age_sec"] == 45.0


def test_parse_cfg_pregen_quiet_and_frag_default():
    """pregen_quiet_sec 显式配置；frag 档缺省＝普通档 2 倍，可单独覆写。"""
    cfg = {"telegram": {"interject_absorb": {
        "enabled": True, "pregen_quiet_sec": 5}}}
    out = ia.parse_interject_cfg(cfg)
    assert out["pregen_quiet_sec"] == 5.0
    assert out["pregen_frag_quiet_sec"] == 10.0
    cfg2 = {"telegram": {"interject_absorb": {
        "enabled": True, "pregen_quiet_sec": 4, "pregen_frag_quiet_sec": 7,
        "pregen_max_wait_sec": 30}}}
    out2 = ia.parse_interject_cfg(cfg2)
    assert out2["pregen_frag_quiet_sec"] == 7.0
    assert out2["pregen_max_wait_sec"] == 30.0


# ── can_merge_queued 各分支 ─────────────────────────────────────────


def test_can_merge_plain_text_ok():
    it = _item(chat_id=100, text="在吗", enq_ts=1000.0)
    assert ia.can_merge_queued(
        it, chat_id=100, now=1010.0, max_age_sec=45.0) is True


def test_can_merge_cross_chat_false():
    it = _item(chat_id=101, text="在吗", enq_ts=1000.0)
    assert ia.can_merge_queued(
        it, chat_id=100, now=1010.0, max_age_sec=45.0) is False


def test_can_merge_media_placeholder_text_false():
    """[图片内容]/[视频]/[贴纸]/[表情] 等占位文本一律不并（媒体语义不动）。"""
    for t in ("[图片内容] 一只猫", "[视频]", "[表情] 😊", "[贴纸]",
              "[动态表情]", "[文件] a.pdf", "[图片消息 - 解析失败或无文字]",
              "【系统】占位"):
        it = _item(text=t)
        assert ia.can_merge_queued(
            it, chat_id=100, now=1001.0, max_age_sec=45.0) is False, t


def test_can_merge_voice_transcript_ok():
    """A 线语音入队前已转录成 ``[语音转录] xx`` → 属纯文本类，可并。"""
    it = _item(text="[语音转录] 我到楼下了", _is_voice_msg=True,
               message=_FakeMsg(voice=object()))
    assert ia.can_merge_queued(
        it, chat_id=100, now=1001.0, max_age_sec=45.0) is True


def test_can_merge_voice_failure_placeholder_false():
    for t in ("[语音消息 - 转录失败或无内容]", "[语音消息 - 下载失败]",
              "[语音转录] ", "[语音转录]   "):
        it = _item(text=t)
        assert ia.can_merge_queued(
            it, chat_id=100, now=1001.0, max_age_sec=45.0) is False, t


def test_can_merge_media_attachment_false():
    """带媒体附件的消息（哪怕文本是 caption 纯文字）不并。"""
    for attr in ("photo", "document", "video", "video_note",
                 "animation", "sticker"):
        it = _item(text="看这个", message=_FakeMsg(**{attr: object()}))
        assert ia.can_merge_queued(
            it, chat_id=100, now=1001.0, max_age_sec=45.0) is False, attr


def test_can_merge_age_and_missing_enq_ts():
    ok = _item(enq_ts=1000.0)
    assert ia.can_merge_queued(
        ok, chat_id=100, now=1045.0, max_age_sec=45.0) is True
    stale = _item(enq_ts=1000.0)
    assert ia.can_merge_queued(
        stale, chat_id=100, now=1046.0, max_age_sec=45.0) is False
    no_ts = _item()
    no_ts.pop("_enq_ts")
    assert ia.can_merge_queued(
        no_ts, chat_id=100, now=1001.0, max_age_sec=45.0) is False
    # 非 dict / 空文本 → False 且不抛
    assert ia.can_merge_queued(
        None, chat_id=100, now=1.0, max_age_sec=45.0) is False
    assert ia.can_merge_queued(
        _item(text="   "), chat_id=100, now=1001.0, max_age_sec=45.0) is False


# ── merge_texts / should_abort_stale_reply ─────────────────────────


def test_merge_texts_joins_and_strips():
    assert ia.merge_texts(["  在吗  ", "", "帮我看下订单\n", None,
                           "急！"]) == "在吗\n帮我看下订单\n急！"
    assert ia.merge_texts([]) == ""
    assert ia.merge_texts(["单条"]) == "单条"


def test_should_abort_tolerance_boundaries():
    """latest > msg_ts 即过期，浮点容差 0.5s；缺时间戳不中止（宁发不吞）。"""
    assert ia.should_abort_stale_reply(1000.0, 1000.0) is False   # 同刻
    assert ia.should_abort_stale_reply(1000.0, 1000.4) is False   # 容差内
    assert ia.should_abort_stale_reply(1000.0, 1000.6) is True    # 补话了
    assert ia.should_abort_stale_reply(1000.0, 999.0) is False    # 更旧
    assert ia.should_abort_stale_reply(0.0, 2000.0) is False      # 缺基准
    assert ia.should_abort_stale_reply(1000.0, 0.0) is False      # 无记录
    assert ia.should_abort_stale_reply(None, None) is False       # type: ignore


def test_note_inbound_monotonic_lookup():
    assert ia.latest_inbound_ts(555) == 0.0
    ia.note_inbound(555, 100.0)
    ia.note_inbound("555", 200.0)      # int/str 键归一化到同一条
    ia.note_inbound(555, 150.0)        # 只进不退
    assert ia.latest_inbound_ts(555) == 200.0
    assert ia.latest_inbound_ts("555") == 200.0


def test_note_inbound_bounded_trim():
    for i in range(ia._REG_MAX + 40):
        ia.note_inbound(f"c{i}", float(i))
    assert len(ia._LATEST_INBOUND) <= ia._REG_MAX
    # 最新的仍在、最老的被裁
    assert ia.latest_inbound_ts(f"c{ia._REG_MAX + 39}") > 0.0
    assert ia.latest_inbound_ts("c0") == 0.0


# ── enabled=false 旁路 + 接线静态钉 ────────────────────────────────


def _tc_src() -> str:
    root = Path(__file__).parent.parent / "src" / "client"
    return (root / "telegram_client.py").read_text(encoding="utf-8")


def test_disabled_by_default_wiring_gated():
    """关闭时既不合并也不中止：默认 cfg enabled=False，且接线两段都以
    enabled 为第一道门（出队合并 `_ij_cfg["enabled"]`、纯判定 helper
    `_interject_is_stale` 不开即 False 短路——2026-08-04 起判定与副作用
    分离，条间关口复用同一判定）。"""
    assert ia.parse_interject_cfg({})["enabled"] is False
    tc = _tc_src()
    # 出队合并块：enabled 与队列非空同为前提
    assert '_ij_cfg["enabled"] and self.message_queue.qsize()' in tc
    # 纯判定 helper：未开启 → return False（不中止）
    gate_idx = tc.index("def _interject_is_stale")
    gate = tc[gate_idx: gate_idx + 1400]
    assert 'if not _ij_cfg.get("enabled"):' in gate
    assert "return False" in gate
    # 副作用 helper 复用纯判定（单一判定源，两者不得各算一套）
    abort_idx = tc.index("def _interject_stale_abort")
    abort = tc[abort_idx: abort_idx + 900]
    assert "if not _interject_is_stale():" in abort


def test_wiring_two_gates_present():
    """两道关口都在：诚实回落后/humanize 前 + humanize sleep 后/send 前。"""
    tc = _tc_src()
    assert tc.count('_interject_stale_abort("pre_humanize")') == 1
    assert tc.count('_interject_stale_abort("post_humanize")') == 1
    # 顺序钉：helper 定义在 dup_guard 登记之后、pre 关口在 humanize 之前、
    # post 关口在 humanize 之后、发送(sent_text_for_context 赋值)之前
    i_isstale = tc.index("def _interject_is_stale")
    i_def = tc.index("def _interject_stale_abort")
    i_pre = tc.index('_interject_stale_abort("pre_humanize")')
    i_hum = tc.index("await self.run_prereply_humanize(")
    i_post = tc.index('_interject_stale_abort("post_humanize")')
    i_send = tc.index("sent_text_for_context = reply_final")
    assert i_isstale < i_def < i_pre < i_hum < i_post < i_send
    # 中止时撤销 dup_guard 乐观登记（防幽灵条目误拦覆盖两问的新回复）
    gate = tc[i_def: i_pre]
    assert "unregister" in gate and "_dg_reg_token" in gate
    # 群聊旁路（复用现场 _is_group 判定，在纯判定 helper 内）
    pure = tc[i_isstale: i_def]
    assert "if _is_group:" in pure


def test_wiring_between_bubbles_gate_present():
    """分条间第三道关口（2026-08-04）：条间延迟后、发下一条前查插话——
    命中即停发剩余条（已发算数），上下文只记实际发出的部分。"""
    tc = _tc_src()
    i_loop = tc.index("[interject] 分条间对方补话")
    # 关口在分条循环里、用纯判定（无副作用版，不动 dup_guard 登记）
    blk = tc[i_loop - 2600: i_loop + 900]
    assert "_interject_is_stale()" in blk
    assert "_bubbles_interrupted = True" in blk
    # 上下文按实际发出集合记录（记了没发的话，下一轮 AI 会以为自己说过）
    assert '"\\n\\n".join(_sent_chunks)' in tc
    # 分条观测带 partial 标记
    assert 'record_bubble_send(' in tc and "partial=_bubbles_interrupted" in tc


def test_wiring_dequeue_merge_present():
    """出队合并接线：can_merge_queued/merge_texts + 私聊限定 + note_inbound。"""
    tc = _tc_src()
    assert "can_merge_queued" in tc
    assert "merge_texts" in tc
    assert "note_inbound" in tc
    assert "'_enq_ts': time.time()" in tc
    assert "[interject] 出队合并" in tc
    assert "[interject] 生成期间对方补话，放弃本条回复" in tc
    # 群聊旁路：合并前有 looks_like_group_chat 判定
    m_idx = tc.index("[interject] 出队合并")
    blk = tc[m_idx - 3000: m_idx]
    assert "looks_like_group_chat" in blk
    # 合并锚点取最新一条（防过期关口把合并稿误判成旧稿）
    assert "message_data['_enq_ts'] = _ij_next['_enq_ts']" in tc


def test_wiring_voice_path_has_no_gate():
    """语音已发出（_voice_sent=True）分支不设中止关口——发出去的撤不回。"""
    tc = _tc_src()
    v_idx = tc.index("elif reply_final and _voice_sent:")
    tail = tc[v_idx: v_idx + 1800]
    assert "_interject_stale_abort" not in tail


# ── 生成前安静窗 + 未答连发注册表（2026-08-12）──────────────────────


def test_pregen_quiet_for_fragment_vs_sentence():
    """碎片字（≤2 字）用 frag 档；整句用普通档；语音前缀先剥；空文本 0。"""
    cfg = {"pregen_quiet_sec": 4.0, "pregen_frag_quiet_sec": 9.0}
    assert ia.pregen_quiet_for("你", cfg) == 9.0
    assert ia.pregen_quiet_for("说话", cfg) == 9.0          # 2 字仍算碎片
    assert ia.pregen_quiet_for("最近有啥新闻", cfg) == 4.0
    assert ia.pregen_quiet_for("[语音转录] 好", cfg) == 9.0
    assert ia.pregen_quiet_for("", cfg) == 0.0
    assert ia.pregen_quiet_for(None, cfg) == 0.0


def test_pending_texts_note_drain_order():
    """登记→取走：时序保序；晚于 upto 的条目留给下一任务。

    注册表按真实时钟做 TTL 裁剪 → 测试时间戳一律取 time.time() 邻域。
    """
    import time as _t
    now = _t.time()
    ia.note_inbound_text(7, now - 20.0, "你")
    ia.note_inbound_text(7, now - 17.0, "妹")
    ia.note_inbound_text(7, now - 15.0, "的")
    ia.note_inbound_text(7, now - 1.0, "最近有啥新闻")
    got = ia.drain_pending_texts(7, now - 15.0)
    assert [s for _, s in got] == ["你", "妹", "的"]
    # 后到的还在表里
    rest = ia.drain_pending_texts(7, now + 999.0)
    assert [s for _, s in rest] == ["最近有啥新闻"]
    # 再取为空
    assert ia.drain_pending_texts(7, now + 999.0) == []


def test_pending_texts_media_placeholder_not_registered():
    """媒体占位/空文本不入表（合并语义只吃纯文本）。"""
    import time as _t
    now = _t.time()
    ia.note_inbound_text(8, now, "[图片内容]")
    ia.note_inbound_text(8, now + 1.0, "  ")
    assert ia.drain_pending_texts(8, now + 999.0) == []


def test_pending_texts_voice_prefix_stripped():
    import time as _t
    now = _t.time()
    ia.note_inbound_text(9, now, "[语音转录] 在吗")
    got = ia.drain_pending_texts(9, now + 999.0)
    assert [s for _, s in got] == ["在吗"]


def test_requeue_texts_restores_for_next_task():
    """过期中止后 requeue：条目还回、去重、保序，下一任务能重新取走。"""
    import time as _t
    now = _t.time()
    ia.note_inbound_text(10, now - 5.0, "你")
    got = ia.drain_pending_texts(10, now + 999.0)
    assert len(got) == 1
    ia.requeue_texts(10, got)
    ia.requeue_texts(10, got)          # 重复 requeue 不翻倍
    ia.note_inbound_text(10, now, "说话")
    again = ia.drain_pending_texts(10, now + 999.0)
    assert [s for _, s in again] == ["你", "说话"]


def test_merge_pending_entries_fragment_seamless():
    """碎片感知合并：逐字连发无缝拼句；整句间换行（复用 B 线合并器）。"""
    entries = [(1.0, "你"), (2.0, "妹"), (3.0, "的"),
               (4.0, "最近有啥新闻")]
    merged = ia.merge_pending_entries(entries)
    assert "你妹的" in merged
    assert "最近有啥新闻" in merged
    assert "你\n妹" not in merged


def test_pending_texts_bounded_per_chat():
    import time as _t
    now = _t.time()
    for i in range(20):
        ia.note_inbound_text(11, now - 40.0 + i, f"消息{i}")
    got = ia.drain_pending_texts(11, now + 999.0)
    assert len(got) <= ia._PENDING_MAX_PER_CHAT
    assert got[-1][1] == "消息19"      # 丢最旧、留最新


def test_wiring_pregen_quiet_present():
    """接线静态钉：生成前安静窗（让位/合并/异常回退）三件套在 telegram_client。"""
    tc = _tc_src()
    assert "pregen_quiet_for" in tc
    assert "drain_pending_texts" in tc
    assert "merge_pending_entries" in tc
    assert "note_inbound_text" in tc
    assert "[interject] 生成前对方补话，本条让位给最新消息" in tc
    assert "[interject] 生成前合并" in tc
    # 过期中止时把已消费的未答文本还回注册表
    assert "requeue_texts" in tc
    assert "_ij_burst_entries" in tc
