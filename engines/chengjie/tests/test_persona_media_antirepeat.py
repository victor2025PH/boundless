"""相册防复读记忆（P3, 2026-07-22）+ 生图相册兜底（P4）单测。

背景：真人不会给同一个人重复发同一张照片，也很少连发同套衣服的连拍——
重复=「像 AI」的穿帮信号。persona_media_sends 账本（每会话已发，持久）+
select_media 分层排除（同图>同系列>翻旧照）实现"发过的记住、系列不重样"。
"""

from __future__ import annotations

import random

import pytest

from src.companion.persona_media import pick_media, select_media, series_of
from src.companion.persona_media_store import PersonaMediaStore


def _mk_rows():
    """3 个系列 5 张图（同 curator 产出结构：tags 带 series:xxx）。"""
    return [
        {"id": "a1", "enabled": True, "media_type": "photo", "weight": 1,
         "triggers": [], "tags": ["curated", "scene:cafe", "series:white-dress"],
         "min_bond_level": 0, "last_sent_at": 100},
        {"id": "a2", "enabled": True, "media_type": "photo", "weight": 1,
         "triggers": [], "tags": ["curated", "scene:street", "series:white-dress"],
         "min_bond_level": 0, "last_sent_at": 200},
        {"id": "b1", "enabled": True, "media_type": "photo", "weight": 1,
         "triggers": [], "tags": ["curated", "scene:bedroom", "series:pink-hoodie"],
         "min_bond_level": 0, "last_sent_at": 300},
        {"id": "b2", "enabled": True, "media_type": "photo", "weight": 1,
         "triggers": [], "tags": ["curated", "scene:car", "series:pink-hoodie"],
         "min_bond_level": 0, "last_sent_at": 50},
        {"id": "c1", "enabled": True, "media_type": "photo", "weight": 1,
         "triggers": [], "tags": ["curated", "scene:park", "series:black-top"],
         "min_bond_level": 0, "last_sent_at": 400},
    ]


def test_series_of():
    assert series_of({"tags": ["curated", "series:white-dress"]}) == "white-dress"
    assert series_of({"tags": ["curated"]}) == ""
    assert series_of(None) == ""


def test_layer1_excludes_sent_ids_and_series():
    """层1：已发的 id 与其系列都被排除——只剩全新系列的图。"""
    rng = random.Random(7)
    for _ in range(20):
        pick = select_media(
            _mk_rows(), "", generic_ok=True, rng=rng,
            exclude_ids={"a1"}, exclude_series={"white-dress"})
        assert pick["id"] in ("b1", "b2", "c1"), \
            "white-dress 系列（a1/a2）必须整组排除"


def test_layer2_relaxes_series_when_all_series_seen():
    """层2：所有系列都发过但还有没发过的图 → 允许旧系列新图（好过重发同一张）。"""
    rng = random.Random(7)
    for _ in range(20):
        pick = select_media(
            _mk_rows(), "", generic_ok=True, rng=rng,
            exclude_ids={"a1", "b1", "c1"},
            exclude_series={"white-dress", "pink-hoodie", "black-top"})
        assert pick["id"] in ("a2", "b2"), "应挑没发过的图（即使系列旧）"


def test_layer3_oldest_resend_when_exhausted():
    """层3：全部发过 → 挑 last_sent_at 最老的（翻旧照），绝不拒发。"""
    all_ids = {"a1", "a2", "b1", "b2", "c1"}
    pick = select_media(
        _mk_rows(), "", generic_ok=True,
        exclude_ids=all_ids,
        exclude_series={"white-dress", "pink-hoodie", "black-top"})
    assert pick["id"] == "b2", "last_sent_at=50 最老的应被翻出来"


def test_store_send_ledger_roundtrip(tmp_path):
    """record_send / sent_history 持久账本读写（含幂等 upsert）。"""
    st = PersonaMediaStore(str(tmp_path / "pm.db"))
    st.record_send("wa:1:100", "m1", persona_id="p", series="white-dress")
    st.record_send("wa:1:100", "m2", persona_id="p", series="")
    st.record_send("wa:1:100", "m1", persona_id="p", series="white-dress")  # 幂等
    st.record_send("wa:2:200", "m9", persona_id="p", series="black-top")
    h = st.sent_history("wa:1:100")
    assert h["ids"] == {"m1", "m2"}
    assert h["series"] == {"white-dress"}
    assert st.sent_history("wa:2:200")["ids"] == {"m9"}
    assert st.sent_history("nobody")["ids"] == set()


def test_sent_history_time_decay(tmp_path):
    """W2：90 天时间衰减——旧记录过期后图重新可用；0=永久排除。"""
    st = PersonaMediaStore(str(tmp_path / "pm.db"))
    now = 1_800_000_000.0
    st.record_send("c1", "old", persona_id="p", series="x",
                   now=now - 100 * 86400)   # 100 天前
    st.record_send("c1", "new", persona_id="p", series="y",
                   now=now - 5 * 86400)     # 5 天前
    h90 = st.sent_history("c1", max_age_days=90, now=now)
    assert h90["ids"] == {"new"}, "100 天前的记录应过期"
    assert h90["series"] == {"y"}
    h_all = st.sent_history("c1", max_age_days=0, now=now)
    assert h_all["ids"] == {"old", "new"}, "0=永久排除（全历史）"


def test_send_gate_peer_exempt():
    """W1：exempt_peers 白名单——测试号免限额（子串匹配），生产客户不受影响。"""
    from src.skills.companion_send_gate import peer_exempt
    cfg = {"companion_send_gate": {
        "enabled": True, "exempt_peers": ["639273815533", "85263115820"]}}
    assert peer_exempt(cfg, "639273815533")
    assert peer_exempt(cfg, "639273815533@s.whatsapp.net")  # 带后缀也命中
    assert peer_exempt(cfg, "+639273815533")                # 带国家码前缀
    assert not peer_exempt(cfg, "8613800138000")            # 生产客户不豁免
    assert not peer_exempt(cfg, "")
    assert not peer_exempt({}, "639273815533")              # 无配置不豁免


def test_send_ledger_stats(tmp_path):
    """T3 观测：库存/投放/覆盖/唯一消耗聚合。"""
    st = PersonaMediaStore(str(tmp_path / "pm.db"))
    st.add("p", "photo", "/a.jpg", "", triggers=[], tags=["series:x"])
    st.add("p", "photo", "/b.jpg", "", triggers=[], tags=["series:y"])
    st.record_send("c1", "m1", persona_id="p", series="x")
    st.record_send("c1", "m2", persona_id="p", series="y")
    st.record_send("c2", "m1", persona_id="p", series="x")
    s = st.send_ledger_stats()
    assert s["media_total"] == 2 and s["media_enabled"] == 2
    assert s["total_sends"] == 3
    assert s["convs_covered"] == 2
    assert s["unique_media_sent"] == 2


def test_pick_media_uses_conv_history(tmp_path):
    """pick_media(conv_key=...)：同会话不再挑已发的图/系列。"""
    st = PersonaMediaStore(str(tmp_path / "pm.db"))
    r1 = st.add("p", "photo", "/a1.jpg", "", triggers=[],
                tags=["series:white-dress"])
    st.add("p", "photo", "/b1.jpg", "", triggers=[],
           tags=["series:pink-hoodie"])
    st.record_send("conv1", str(r1["id"]), persona_id="p", series="white-dress")
    for _ in range(10):
        pick = pick_media(st, "p", "", generic_ok=True, conv_key="conv1")
        assert pick is not None
        assert "pink-hoodie" in (pick.get("tags") or [""])[0] or \
            series_of(pick) == "pink-hoodie"


@pytest.mark.asyncio
async def test_selfie_album_fallback_on_command_failure(tmp_path):
    """P4：command 生图失败 → album_fallback 自动回落相册真图。"""
    from src.ai.companion_selfie import SelfieProvider

    album = tmp_path / "albums" / "p1"
    album.mkdir(parents=True)
    (album / "cafe_white-dress_01.jpg").write_bytes(b"fakejpg")

    provider = SelfieProvider({
        "enabled": True, "backend": "command",
        "album_dir": str(tmp_path / "albums"),
        "out_dir": str(tmp_path / "out"),
        # 必失败的命令（不存在的可执行文件）
        "command_args": ["definitely_not_a_real_binary_xyz", "{prompt}", "{out}"],
        "command_timeout_sec": 5,
    })
    res = await provider.generate("test prompt", album_key="p1")
    assert res.ok, f"应回落相册成功 (err={res.error})"
    assert res.provider == "album"
    assert res.extra.get("fallback_from") == "command"
    assert res.extra.get("primary_error")


def test_album_series_of_path():
    from src.ai.companion_selfie import album_series_of_path
    assert album_series_of_path("cafe_white-dress_01.jpg") == "white-dress"
    assert album_series_of_path(
        r"D:\albums\p\mirror-selfie_pink-dress_03.jpg") == "pink-dress"
    assert album_series_of_path("auto_selfie_1753000000_ab12cd.png") == ""
    assert album_series_of_path("face_ref.jpg") == ""
    assert album_series_of_path("") == ""


@pytest.mark.asyncio
async def test_album_pick_excludes_face_ref_and_sent(tmp_path):
    """相册直选：face_ref 永不投放；已发文件+同系列被分层排除。"""
    from src.ai.companion_selfie import SelfieProvider

    album = tmp_path / "albums" / "p1"
    album.mkdir(parents=True)
    for n in ("face_ref.jpg", "cafe_white-dress_01.jpg",
              "street_white-dress_02.jpg", "bedroom_pink-hoodie_01.jpg"):
        (album / n).write_bytes(b"x")
    provider = SelfieProvider({
        "enabled": True, "backend": "album",
        "album_dir": str(tmp_path / "albums"),
    })
    # 排除 white-dress 系列的一张 → 层1 应只剩 pink-hoodie
    for _ in range(12):
        res = await provider.generate(
            "", album_key="p1",
            exclude_paths={"cafe_white-dress_01.jpg"})
        assert res.ok
        name = res.image_path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        assert name == "bedroom_pink-hoodie_01.jpg", \
            f"face_ref 与 white-dress 系列都该被排除，实际挑了 {name}"


@pytest.mark.asyncio
async def test_album_default_key_fallback(tmp_path):
    """default_album_key：会话人设分册无图 → 回落默认分册（单素材多人设）。"""
    from src.ai.companion_selfie import SelfieProvider

    default_album = tmp_path / "albums" / "lin_xiaoyu"
    default_album.mkdir(parents=True)
    (default_album / "cafe_white-dress_01.jpg").write_bytes(b"x")
    provider = SelfieProvider({
        "enabled": True, "backend": "album",
        "album_dir": str(tmp_path / "albums"),
        "default_album_key": "lin_xiaoyu",
    })
    # 请求的人设 lin_jiaxin 没有分册 → 应命中 lin_xiaoyu 的图
    res = await provider.generate("", album_key="lin_jiaxin")
    assert res.ok, f"应回落默认分册 (err={res.error})"
    assert "cafe_white-dress_01.jpg" in res.image_path


@pytest.mark.asyncio
async def test_gate_skips_album_fallback_result(tmp_path):
    """image_gate：command 失败回落的相册真图免体检（策展图无需 VLM 复检）。"""
    from src.ai.companion_selfie import SelfieProvider
    from src.ai.image_gate import generate_with_gate

    album = tmp_path / "albums" / "p1"
    album.mkdir(parents=True)
    (album / "street_black-top_01.jpg").write_bytes(b"x")
    provider = SelfieProvider({
        "enabled": True, "backend": "command",
        "album_dir": str(tmp_path / "albums"),
        "out_dir": str(tmp_path / "out"),
        "command_args": ["definitely_not_a_real_binary_xyz", "{prompt}", "{out}"],
        "command_timeout_sec": 5,
    })
    called = {"n": 0}

    async def _fake_check(*a, **kw):
        called["n"] += 1
        return False, "should_not_be_called"

    from unittest.mock import patch as _patch
    with _patch("src.ai.image_gate.check_image", side_effect=_fake_check):
        res = await generate_with_gate(
            provider, "test", gate_cfg={"enabled": True}, album_key="p1")
    assert res.ok and res.provider == "album"
    assert called["n"] == 0, "album 兜底结果不得进 VLM 体检"


def test_selfie_provider_rebuilds_on_cfg_change(tmp_path):
    """配置单例审计第 3 处：provider 配置变更后单例重建（热重载生效）。"""
    from src.ai.companion_selfie import get_selfie_provider, reset_selfie_provider
    reset_selfie_provider()
    try:
        p1 = get_selfie_provider({"enabled": True, "backend": "album"})
        p2 = get_selfie_provider({"enabled": True, "backend": "album"})
        assert p1 is p2, "同配置应复用单例"
        p3 = get_selfie_provider({"enabled": True, "backend": "command"})
        assert p3 is not p1, "配置变更应重建"
        assert p3.backend == "command"
    finally:
        reset_selfie_provider()


@pytest.mark.asyncio
async def test_selfie_album_fallback_disabled(tmp_path):
    """album_fallback: false → 保持原失败语义（零行为变更开关）。"""
    from src.ai.companion_selfie import SelfieProvider

    provider = SelfieProvider({
        "enabled": True, "backend": "command",
        "album_dir": str(tmp_path / "albums"),
        "out_dir": str(tmp_path / "out"),
        "album_fallback": False,
        "command_args": ["definitely_not_a_real_binary_xyz", "{prompt}", "{out}"],
        "command_timeout_sec": 5,
    })
    res = await provider.generate("test prompt", album_key="p1")
    assert not res.ok
