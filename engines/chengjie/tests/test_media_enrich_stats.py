"""入站媒体识别「没看懂」计数门禁（2026-07-31）。

存在理由：识别失败是**静默降级**——拿不到描述就回落 `[图片]`/`[语音]` 占位，AI 照常
回复，日志里只有 debug。于是「开关没开」「后端没接上」「识别失败」三种完全不同的处境
在运营眼里长得一模一样。归因错了，人就会去修错的东西（实锤：识图黄灯其实是供给缺席，
而语音那盏假绿背后是安装包压根没带本地 ASR）。
"""

from __future__ import annotations

import asyncio

from src.inbox.media_enrich import _miss_reason, enrich_inbound_media_text
from src.inbox.media_enrich_stats import get_media_enrich_stats


# ── 归因：三种同形不同因的处境必须分开 ────────────────────────────────────

def test_miss_reason_disabled_vs_no_backend_vs_failed(monkeypatch):
    monkeypatch.setattr(
        "src.companion.media_capability._module_installed", lambda _n: False)
    # 开关没开＝运营信号（客户在发图而你还没开识图），不是故障
    assert _miss_reason("image", {}) == "disabled"
    assert _miss_reason("voice", {}) == "disabled"
    # 开了但后端没接上＝该修，且多半能自助修
    assert _miss_reason("image", {"vision": {"enabled": True}}) == "no_backend"
    assert _miss_reason(
        "voice", {"voice_recognition": {"enabled": True,
                                        "provider": "faster_whisper"}}) == "no_backend"
    # 后端在但没识别出来＝看日志（网关不通 / 模型出错 / 令牌过期）
    assert _miss_reason("image", {
        "vision": {"enabled": True, "provider": "zhipu", "api_key": "k"}}) == "failed"


# ── 计数：进 media_runtime_signals，让自检卡答得出「真流量漏了多少」 ────────

def test_unresolved_media_counted(tmp_path):
    st = get_media_enrich_stats()
    st.reset()
    text, desc = asyncio.run(enrich_inbound_media_text(
        media_type="image", media_ref="https://example.com/x.jpg", caption=""))
    assert text == "[图片]" and desc == ""     # 软降级语义不变
    d = st.dump()
    assert d["missed"] == 1 and d["by_reason"].get("unresolved") == 1
    assert d["by_kind"].get("image") == 1


def test_runtime_signals_carry_enrich_when_traffic(monkeypatch):
    from src.companion.media_capability import media_runtime_signals

    st = get_media_enrich_stats()
    st.reset()
    assert "enrich" not in media_runtime_signals()   # 零流量不渲染
    st.record_miss("voice", "no_backend")
    st.record_understood("image")
    sig = media_runtime_signals()
    assert sig["enrich"]["missed"] == 1 and sig["enrich"]["understood"] == 1
    assert sig["enrich"]["miss_rate"] == 0.5


def test_stats_key_cap_and_never_raises():
    """分桶有上限（防未来新类型撑爆），且计数绝不能反过来影响识别主链。"""
    st = get_media_enrich_stats()
    st.reset()
    for i in range(80):
        st.record_miss(f"kind{i}", f"reason{i}")
    d = st.dump()
    assert d["missed"] == 80
    assert len(d["by_reason"]) <= 32 and len(d["by_kind"]) <= 32
