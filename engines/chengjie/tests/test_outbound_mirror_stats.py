# -*- coding: utf-8 -*-
"""出站媒体归档发布观测门禁（2026-08-02，配套分条语音逐条镜像）。

守的不变量：
1. ``publish_outbound_media`` 每次调用（成/败）都进计数——「静默退回文本占位」
   从此有数可查；
2. dump()/dump_prom() 契约稳定（workspace metrics 与 Prometheus 的消费口径）；
3. 失败不撑爆内存（combo 封顶归 __other__）。
"""

from pathlib import Path

import pytest

from src.integrations import protocol_bridge as PB
from src.integrations.outbound_mirror_stats import (
    OutboundMirrorStats,
    get_outbound_mirror_stats,
)


# ─────────────────── 纯计数语义 ───────────────────

def test_counts_and_nested_dump():
    st = OutboundMirrorStats()
    st.record_publish("telegram", "voice", ok=True)
    st.record_publish("telegram", "voice", ok=False)
    st.record_publish("telegram", "image", ok=True)
    st.record_publish("whatsapp", "voice", ok=True)
    d = st.dump()
    assert d["total"] == 4 and d["fail"] == 1
    assert d["by_platform"]["telegram"] == {"total": 3, "fail": 1}
    assert d["by_kind"]["voice"] == {"total": 3, "fail": 1}
    assert d["by_kind"]["image"] == {"total": 1, "fail": 0}
    assert d["last_fail_ts"] > 0 and d["last_ok_ts"] > 0


def test_dump_prom_contract():
    st = OutboundMirrorStats()
    st.record_publish("telegram", "voice", ok=False)
    prom = st.dump_prom()
    assert 'outbound_media_publish_total{platform="telegram",kind="voice"} 1' in prom
    assert 'outbound_media_publish_fail_total{platform="telegram",kind="voice"} 1' in prom


def test_combo_cap_overflows_to_other():
    st = OutboundMirrorStats()
    for i in range(40):
        st.record_publish(f"plat{i}", "voice", ok=False)
    d = st.dump()
    assert d["total"] == 40 and d["fail"] == 40
    # 超限的进 __other__，不无限增殖 distinct key
    assert "__other__" in d["by_platform"]
    assert len(d["by_platform"]) <= 33


def test_sanitizes_dirty_inputs():
    st = OutboundMirrorStats()
    st.record_publish("", None, ok=True)  # type: ignore[arg-type]
    d = st.dump()
    assert d["by_platform"].get("unknown", {}).get("total") == 1


# ─────────────────── publish_outbound_media 接线 ───────────────────

@pytest.fixture
def media_root(tmp_path, monkeypatch):
    root = tmp_path / "static_media"
    monkeypatch.setattr(PB, "protocol_media_root", lambda: root)
    return root


def test_publish_success_and_failure_are_both_counted(media_root, tmp_path):
    st = get_outbound_mirror_stats()
    base = st.dump()

    ok_src = tmp_path / "clip.ogg"
    ok_src.write_bytes(b"OggS")
    url, mt = PB.publish_outbound_media("telegram", "acct1", str(ok_src))
    assert url and mt == "voice"

    # 文件不存在 → 发布失败也必须计数（这正是要观测的静默降级）
    url2, mt2 = PB.publish_outbound_media("telegram", "acct1",
                                          str(tmp_path / "gone.ogg"))
    assert (url2, mt2) == ("", "")

    d = st.dump()
    assert d["total"] == base["total"] + 2
    assert d["fail"] == base["fail"] + 1
    voice = d["by_kind"].get("voice", {"total": 0, "fail": 0})
    base_voice = base["by_kind"].get("voice", {"total": 0, "fail": 0})
    assert voice["total"] == base_voice["total"] + 2
    assert voice["fail"] == base_voice["fail"] + 1


def test_stats_failure_never_breaks_publish(media_root, tmp_path, monkeypatch):
    """观测是锦上添花：stats 自身炸了也不能影响发布主流程。"""
    import src.integrations.outbound_mirror_stats as OMS

    def _boom():
        raise RuntimeError("stats down")

    monkeypatch.setattr(OMS, "get_outbound_mirror_stats", _boom)
    src = tmp_path / "pic.jpg"
    src.write_bytes(b"\xff\xd8")
    url, mt = PB.publish_outbound_media("telegram", "a", str(src))
    assert url.startswith("/static/protocol_media/telegram/") and mt == "image"
    assert Path(PB.static_media_ref_to_path(url)).is_file()
