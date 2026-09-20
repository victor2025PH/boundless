"""Phase 6 拟人化(营业时段/延迟) + 接管摘标 单测。"""

from __future__ import annotations

import pytest

from src.integrations import protocol_autoreply as pa


@pytest.fixture(autouse=True)
def _clear_state():
    pa._last_reply.clear()
    pa._last_sent.clear()
    yield
    pa._last_reply.clear()
    pa._last_sent.clear()


# ── 营业时段 ──────────────────────────────────────────────────────────────

def test_business_hours_disabled_is_always_on():
    assert pa.within_business_hours({}, now=0) is True
    assert pa.within_business_hours(
        {"protocol_autoreply": {"hours": {"enabled": False}}}, now=0) is True


def test_business_hours_day_window():
    cfg = {"protocol_autoreply": {"hours": {
        "enabled": True, "start": "09:00", "end": "23:00", "tz_offset": 0}}}
    assert pa.within_business_hours(cfg, now=12 * 3600) is True   # 12:00
    assert pa.within_business_hours(cfg, now=2 * 3600) is False   # 02:00


def test_business_hours_overnight_window():
    cfg = {"protocol_autoreply": {"hours": {
        "enabled": True, "start": "22:00", "end": "06:00", "tz_offset": 0}}}
    assert pa.within_business_hours(cfg, now=2 * 3600) is True    # 02:00 在跨夜窗
    assert pa.within_business_hours(cfg, now=12 * 3600) is False  # 12:00 不在


def test_business_hours_tz_offset():
    cfg = {"protocol_autoreply": {"hours": {
        "enabled": True, "start": "09:00", "end": "23:00", "tz_offset": 8}}}
    # UTC 02:00 + 8h = 本地 10:00 → 在窗内
    assert pa.within_business_hours(cfg, now=2 * 3600) is True


# ── 拟人化延迟 ────────────────────────────────────────────────────────────

def test_pick_delay_none():
    assert pa.pick_delay({}) == 0.0
    assert pa.pick_delay({"protocol_autoreply": {"delay": {"max_sec": 0}}}) == 0.0


def test_pick_delay_fixed():
    cfg = {"protocol_autoreply": {"delay": {"min_sec": 2, "max_sec": 2}}}
    assert pa.pick_delay(cfg) == 2.0


def test_pick_delay_range():
    cfg = {"protocol_autoreply": {"delay": {"min_sec": 1, "max_sec": 4}}}
    for _ in range(20):
        d = pa.pick_delay(cfg)
        assert 1.0 <= d <= 4.0


# ── 单一节奏源收口·第三链（2026-08-07）─────────────────────────────────────
# 协议 7×24 直发链此前只读 protocol_autoreply.delay（从没人配 → 秒回），
# 而设置页滑杆写 inbox.l2_autosend.deliver_delay → 「滑杆不生效」根因。

_SLIDER = {"inbox": {"l2_autosend": {"deliver_delay": {
    "min_sec": 8, "max_sec": 20, "adaptive": True}}}}


def test_pacing_block_follows_slider_when_own_absent():
    # 未配 protocol_autoreply.delay → 跟随滑杆键（本次修复的核心行为）
    block = pa.resolve_send_pacing_block(_SLIDER, {})
    assert block == {"min_sec": 8, "max_sec": 20, "adaptive": True}


def test_pacing_block_follows_slider_when_own_zeroed():
    # 显式 0/0（「没意见」而非「要秒回」）也跟随——不能按「块存在」判定
    block = pa.resolve_send_pacing_block(
        _SLIDER, {"delay": {"min_sec": 0, "max_sec": 0}})
    assert block == {"min_sec": 8, "max_sec": 20, "adaptive": True}


def test_pacing_block_own_wins_when_configured():
    block = pa.resolve_send_pacing_block(
        _SLIDER, {"delay": {"min_sec": 2, "max_sec": 5}})
    assert block == {"min_sec": 2, "max_sec": 5}


def test_pacing_block_follow_false_keeps_instant():
    # 想保住本链秒回的逃生阀：follow:false → 用 own（空节奏＝0 延迟），不套兜底默认
    block = pa.resolve_send_pacing_block(
        _SLIDER, {"delay": {"follow": False}})
    assert block == {"follow": False}


def test_pacing_block_falls_back_to_default_when_nothing_configured():
    # 存量节点收口：own 与 slider 都没配（无 deliver_delay）→ 兜底拟人默认，绝不裸秒回。
    block = pa.resolve_send_pacing_block({}, {})
    assert block == pa.DEFAULT_PROTOCOL_PACING
    assert float(block["max_sec"]) >= 3   # 非秒回

    # slider 显式 0/0（「没意见」）同样兜底，不当成「要秒回」
    block2 = pa.resolve_send_pacing_block(
        {"inbox": {"l2_autosend": {"deliver_delay": {"min_sec": 0, "max_sec": 0}}}}, {})
    assert block2 == pa.DEFAULT_PROTOCOL_PACING


def test_effective_protocol_pacing_source_labels():
    # runtime 与 banner 共用本函数——四种来源标注锁定（banner 据此显示不说谎）
    assert pa.effective_protocol_pacing({"min_sec": 2, "max_sec": 5}, _SLIDER["inbox"]["l2_autosend"]["deliver_delay"])[1] == "own"
    assert pa.effective_protocol_pacing({}, {"min_sec": 8, "max_sec": 20})[1] == "slider"
    assert pa.effective_protocol_pacing({}, {})[1] == "default"
    assert pa.effective_protocol_pacing({"follow": False}, {"min_sec": 8, "max_sec": 20})[1] == "instant"


@pytest.mark.asyncio
async def test_run_autoreply_uses_default_when_unconfigured():
    """存量节点端到端：protocol_autoreply 开、无任何节奏配置 → 兜底延迟真的生效（非秒回）。"""
    slept = []

    async def _send(**kw):
        pass

    async def _sleep(d):
        slept.append(d)

    cfg = {"protocol_autoreply": {"enabled": True}}   # 无 delay、无 deliver_delay
    res = await pa.run_autoreply(
        _payload(), registry=_Reg(), cfg=cfg, generate=_gen, send=_send,
        risk_fn=lambda t: "low", now=None, sleep=_sleep)   # now=None → 走真实 elapsed 扣减
    assert res["sent"] is True
    # 兜底 8-20s adaptive；短回复"你好"估时后仍应 >0（非秒回），且 <=20
    assert slept and 0 < sum(slept) <= 20


def test_humanize_flags_default_both_on():
    assert pa.humanize_flags({}, "telegram") == (True, True)


def test_humanize_flags_global_off():
    cfg = {"inbox": {"l2_autosend": {
        "mark_read_before_reply": False, "typing_indicator": False}}}
    assert pa.humanize_flags(cfg, "telegram") == (False, False)


def test_humanize_flags_platform_override():
    cfg = {"inbox": {"l2_autosend": {
        "typing_indicator": True,
        "platform_humanize": {"telegram": {"typing": False}}}}}
    assert pa.humanize_flags(cfg, "telegram") == (True, False)
    # 覆写只作用于该平台，其余平台仍跟随全局
    assert pa.humanize_flags(cfg, "whatsapp") == (True, True)


@pytest.mark.asyncio
async def test_run_autoreply_follows_slider_and_runs_humanize_in_order():
    """协议链集成：无 own.delay 时跟随滑杆延迟，且已读→打字→发送有序。"""
    events = []

    async def _send(**kw):
        events.append(("send", kw.get("text")))

    async def _sleep(d):
        events.append(("sleep", round(float(d), 2)))

    async def _mark_read(**kw):
        events.append(("read", kw.get("chat_key")))

    async def _typing(**kw):
        events.append(("type", kw.get("action")))

    cfg = {"protocol_autoreply": {"enabled": True},
           "inbox": {"l2_autosend": {"deliver_delay": {
               "min_sec": 3, "max_sec": 3, "adaptive": False}}}}
    res = await pa.run_autoreply(
        _payload(), registry=_Reg(), cfg=cfg, generate=_gen, send=_send,
        risk_fn=lambda t: "low", now=1000, sleep=_sleep,
        mark_read=_mark_read, typing=_typing)
    assert res["sent"] is True
    kinds = [e[0] for e in events]
    assert kinds[0] == "read"                 # 先看
    assert "type" in kinds                    # 挂过「正在输入」
    assert kinds[-1] == "send"                # 最后才发
    assert kinds.index("read") < kinds.index("type") < kinds.index("send")
    # 总等待≈3s（滑杆值）——证明协议链确实吃到了 deliver_delay
    slept = sum(e[1] for e in events if e[0] == "sleep")
    assert 2.5 <= slept <= 3.5


@pytest.mark.asyncio
async def test_run_autoreply_follow_false_stays_instant():
    """follow:false 逃生阀：协议链保持秒回（不吃滑杆）。"""
    slept = []

    async def _send(**kw):
        pass

    async def _sleep(d):
        slept.append(d)

    cfg = {"protocol_autoreply": {"enabled": True,
                                  "delay": {"follow": False}},
           "inbox": {"l2_autosend": {"deliver_delay": {
               "min_sec": 30, "max_sec": 60}}}}
    res = await pa.run_autoreply(
        _payload(), registry=_Reg(), cfg=cfg, generate=_gen, send=_send,
        risk_fn=lambda t: "low", now=1000, sleep=_sleep)
    assert res["sent"] is True
    assert slept == []   # follow:false → 空节奏 → 0 延迟


# ── P2 连发间隔地板（min_gap_sec，2026-08-12）────────────────────────────

_GAP_CFG = {"protocol_autoreply": {"enabled": True},
            "inbox": {"l2_autosend": {"deliver_delay": {
                "min_sec": 1, "max_sec": 1, "adaptive": False,
                "min_gap_sec": 10}}}}


async def _run_gap(cfg, text, now, slept):
    async def _send(**kw):
        pass

    async def _sleep(d):
        slept.append(float(d))

    return await pa.run_autoreply(
        _payload(text), registry=_Reg(), cfg=cfg, generate=_gen, send=_send,
        risk_fn=lambda t: "low", now=now, sleep=_sleep)


@pytest.mark.asyncio
async def test_gap_floor_first_send_not_padded():
    """首条没有「上一条发出」参照 → 不垫，等基础延迟即可。"""
    slept = []
    res = await _run_gap(_GAP_CFG, "在吗", 1000.0, slept)
    assert res["sent"] is True
    assert 0.5 <= sum(slept) <= 1.5   # 只有基础 1s，无地板垫付


@pytest.mark.asyncio
async def test_gap_floor_pads_second_send_to_same_chat():
    """同会话第二条距上条发出 6s（< min_gap_sec=10）→ 补等到 ~10s（±15% 抖动）。"""
    slept1, slept2 = [], []
    r1 = await _run_gap(_GAP_CFG, "在吗", 1000.0, slept1)
    assert r1["sent"] is True
    # now=1006：过 5s AUTO_COOLDOWN，距上条 send_ts(1000) 6s < 10s 地板
    r2 = await _run_gap(_GAP_CFG, "还在吗", 1006.0, slept2)
    assert r2["sent"] is True
    # required = 10×[0.85,1.15] − 6 ∈ [2.5, 5.5]，必大于基础 1s
    assert 2.5 <= sum(slept2) <= 5.5


@pytest.mark.asyncio
async def test_gap_floor_far_apart_not_padded():
    """距上条发出已超过间隔 → 地板不介入，仍是基础延迟。"""
    slept1, slept2 = [], []
    await _run_gap(_GAP_CFG, "在吗", 1000.0, slept1)
    slept2.clear()
    r2 = await _run_gap(_GAP_CFG, "还在吗", 1030.0, slept2)   # 30s 后
    assert r2["sent"] is True
    assert 0.5 <= sum(slept2) <= 1.5


@pytest.mark.asyncio
async def test_gap_floor_off_by_default():
    """未配 min_gap_sec（默认 0）→ 连发行为与旧版一致（只有基础延迟）。"""
    cfg = {"protocol_autoreply": {"enabled": True},
           "inbox": {"l2_autosend": {"deliver_delay": {
               "min_sec": 1, "max_sec": 1, "adaptive": False}}}}
    slept1, slept2 = [], []
    await _run_gap(cfg, "在吗", 1000.0, slept1)
    r2 = await _run_gap(cfg, "还在吗", 1006.0, slept2)
    assert r2["sent"] is True
    assert 0.5 <= sum(slept2) <= 1.5


# ── 接管摘标 ──────────────────────────────────────────────────────────────

class _Store:
    def __init__(self, tags):
        self._t = {"c1": list(tags)}

    def get_conv_tags(self, cid):
        return self._t.get(cid, [])

    def set_conv_tags(self, cid, tags):
        self._t[cid] = list(tags)


def test_clear_needs_human_removes_tag():
    s = _Store([pa.HANDOFF_TAG, "vip"])
    assert pa.clear_needs_human(s, "c1") is True
    assert s.get_conv_tags("c1") == ["vip"]


def test_clear_needs_human_noop_when_absent():
    s = _Store(["vip"])
    assert pa.clear_needs_human(s, "c1") is False
    assert s.get_conv_tags("c1") == ["vip"]


def test_clear_needs_human_none_store():
    assert pa.clear_needs_human(None, "c1") is False


# ── 与 run_autoreply 集成 ─────────────────────────────────────────────────

class _Reg:
    def get(self, p, a):
        return {"meta": {"auto_reply": True}}


def _payload(text="在吗"):
    return {"platform": "telegram", "account_id": "tg1",
            "chat_key": "1", "text": text, "direction": "in"}


async def _gen(**kw):
    return "你好"


@pytest.mark.asyncio
async def test_run_autoreply_off_hours_skips():
    sent = []

    async def _send(**kw):
        sent.append(kw)

    cfg = {"protocol_autoreply": {"enabled": True, "hours": {
        "enabled": True, "start": "09:00", "end": "23:00", "tz_offset": 0}}}
    res = await pa.run_autoreply(
        _payload(), registry=_Reg(), cfg=cfg, generate=_gen, send=_send,
        risk_fn=lambda t: "low", now=2 * 3600)  # 02:00 → 营业外
    assert res["reason"] == "off_hours"
    assert sent == []


@pytest.mark.asyncio
async def test_run_autoreply_applies_delay_before_send():
    sent = []
    slept = []

    async def _send(**kw):
        sent.append(kw)

    async def _sleep(d):
        slept.append(d)

    cfg = {"protocol_autoreply": {"enabled": True,
                                  "delay": {"min_sec": 2, "max_sec": 2}}}
    res = await pa.run_autoreply(
        _payload(), registry=_Reg(), cfg=cfg, generate=_gen, send=_send,
        risk_fn=lambda t: "low", now=1000, sleep=_sleep)
    assert res["sent"] is True
    assert slept == [2.0]      # 发送前等了拟人延迟
    assert len(sent) == 1
