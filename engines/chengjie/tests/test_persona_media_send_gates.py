"""实施90 挑图新闸门禁：strict 绝不重发 / pHash 近重复族排除 / 季节门 / 地点门 /
resolve_consistency_cfg 新键；重点＝默认参数下旧行为一个字不变。"""
import random

from src.companion.persona_media import pick_media, select_media
from src.companion.persona_media_store import PersonaMediaStore
from src.inbox.image_autosend import resolve_consistency_cfg


def _row(rid, *, triggers=None, tags=None, phash="", last_sent_at=0.0,
         weight=1):
    return {
        "id": rid, "media_type": "photo", "enabled": True,
        "triggers": triggers or [], "tags": tags or [], "phash": phash,
        "weight": weight, "min_bond_level": 0, "last_sent_at": last_sent_at,
        "url": f"/u/{rid}.jpg", "file_path": f"/f/{rid}.jpg",
    }


_REQ = "来张照片"     # 泛化要图：非信息性提问、非邀约


def test_default_behavior_unchanged_layer3_still_resends():
    rows = [_row("a", last_sent_at=10), _row("b", last_sent_at=5)]
    got = select_media(rows, _REQ, generic_ok=True,
                       exclude_ids={"a", "b"}, rng=random.Random(1))
    assert got is not None and got["id"] == "b"   # 翻旧照＝最久没发


def test_strict_no_resend_layer3_refuses():
    rows = [_row("a", last_sent_at=10), _row("b", last_sent_at=5)]
    got = select_media(rows, _REQ, generic_ok=True,
                       exclude_ids={"a", "b"}, no_resend=True)
    assert got is None
    # 只发过一部分 → strict 不影响正常挑新
    got2 = select_media(rows, _REQ, generic_ok=True,
                        exclude_ids={"a"}, no_resend=True,
                        rng=random.Random(1))
    assert got2 is not None and got2["id"] == "b"


_PH_A = "00000000000000ff"
_PH_B = "00000000000000fe"     # 距 A 1 bit＝近重复
_PH_C = "ffffffffffff0000"     # 距 A 很远


def test_phash_family_soft_exclusion():
    rows = [_row("a", phash=_PH_A), _row("b", phash=_PH_B),
            _row("c", phash=_PH_C)]
    got = select_media(rows, _REQ, generic_ok=True, exclude_ids={"a"},
                       rng=random.Random(2))
    assert got is not None and got["id"] == "c"   # b 被族排除


def test_phash_family_hard_cooldown():
    rows = [_row("a", phash=_PH_A), _row("b", phash=_PH_B),
            _row("c", phash=_PH_C)]
    got = select_media(rows, _REQ, generic_ok=True, hard_exclude_ids={"a"},
                       rng=random.Random(3))
    assert got is not None and got["id"] == "c"
    # 全族都在冷却 → None（硬排除任何层不放宽）
    got2 = select_media(rows[:2], _REQ, generic_ok=True,
                        hard_exclude_ids={"a"})
    assert got2 is None


def test_phash_family_strict_exhausted_refuses():
    rows = [_row("a", phash=_PH_A), _row("b", phash=_PH_B)]
    assert select_media(rows, _REQ, generic_ok=True, exclude_ids={"a"},
                        no_resend=True) is None
    # cooldown 档同局面仍出图（旧语义：翻旧照不拒发）
    assert select_media(rows, _REQ, generic_ok=True,
                        exclude_ids={"a"}) is not None


def test_season_gate_generic_pool_only():
    rows = [_row("s", tags=["season:summer"]), _row("n")]
    got = select_media(rows, _REQ, generic_ok=True, now_season="winter",
                       rng=random.Random(4))
    assert got is not None and got["id"] == "n"
    # 全是对立季 → None（交生成/诚实文字）
    assert select_media([_row("s", tags=["season:summer"])], _REQ,
                        generic_ok=True, now_season="winter") is None
    # 同季/过渡季不拦
    got2 = select_media([_row("s", tags=["season:spring"])], _REQ,
                        generic_ok=True, now_season="winter")
    assert got2 is not None
    # 关键词池豁免：运营给冬天配了"海边"触发词就照发（显式意图优先）
    kw = _row("kw", triggers=["海边"], tags=["season:summer"])
    got3 = select_media([kw], "想看海边", generic_ok=False,
                        now_season="winter")
    assert got3 is not None and got3["id"] == "kw"


def test_place_gate_generic_pool_only():
    rows = [_row("jp", tags=["place:JP"]), _row("ca", tags=["place:CA"]),
            _row("u")]
    for seed in range(5):
        got = select_media(rows, _REQ, generic_ok=True, home_country="CA",
                           rng=random.Random(seed))
        assert got is not None and got["id"] in ("ca", "u")
    assert select_media([_row("jp", tags=["place:JP"])], _REQ,
                        generic_ok=True, home_country="CA") is None
    # 关键词池豁免：旅行旧照配了触发词就走运营意图
    kw = _row("jp2", triggers=["东京"], tags=["place:JP"])
    got2 = select_media([kw], "想看东京", home_country="CA")
    assert got2 is not None and got2["id"] == "jp2"


def test_pick_media_wiring_strict_and_family(tmp_path):
    st = PersonaMediaStore(":memory:")
    a = st.add("lin", "photo", "/f/a.jpg", "/u/a.jpg", phash=_PH_A)
    b = st.add("lin", "photo", "/f/b.jpg", "/u/b.jpg", phash=_PH_B)
    st.record_send("conv1", a["id"], persona_id="lin")
    # strict：a 发过 + b 是 a 的近重复族 → 整册拒发
    got = pick_media(st, "lin", _REQ, generic_ok=True, conv_key="conv1",
                     no_resend=True)
    assert got is None
    # cooldown 档（默认）：翻旧照仍出图
    got2 = pick_media(st, "lin", _REQ, generic_ok=True, conv_key="conv1")
    assert got2 is not None
    # 加一张真不同的 → strict 也能出（挑的是新图）
    c = st.add("lin", "photo", "/f/c.jpg", "/u/c.jpg", phash=_PH_C)
    got3 = pick_media(st, "lin", _REQ, generic_ok=True, conv_key="conv1",
                      no_resend=True)
    assert got3 is not None and got3["id"] == c["id"]


def test_pick_media_season_place_params(tmp_path):
    st = PersonaMediaStore(":memory:")
    st.add("lin", "photo", "/f/s.jpg", "/u/s.jpg", tags=["season:summer"])
    ok = st.add("lin", "photo", "/f/n.jpg", "/u/n.jpg", tags=["place:CA"])
    got = pick_media(st, "lin", _REQ, generic_ok=True,
                     now_season="winter", home_country="CA",
                     rng=random.Random(1))
    assert got is not None and got["id"] == ok["id"]


def test_select_media_trace_fills_gates():
    from src.companion.persona_media import select_media
    # 季节拒发：refused=season + cut 计数
    tr = {}
    assert select_media([_row("s", tags=["season:summer"])], _REQ,
                        generic_ok=True, now_season="winter",
                        trace=tr) is None
    assert tr["pool"] == "generic" and tr["start"] == 1
    assert tr["refused"] == "season" and tr["cut"]["season"] == 1
    # 家族扩散 + 冷却剔除：a 在冷却、b 是 a 的近重复 → 都被剔，c 出图
    tr2 = {}
    rows = [_row("a", phash=_PH_A), _row("b", phash=_PH_B),
            _row("c", phash=_PH_C)]
    got = select_media(rows, _REQ, generic_ok=True, hard_exclude_ids={"a"},
                       trace=tr2, rng=random.Random(1))
    assert got is not None and got["id"] == "c"
    assert tr2["cut"]["family"] == 1 and tr2["cut"]["cooldown"] == 2
    assert "refused" not in tr2
    # strict 耗尽：refused=strict_no_resend
    tr3 = {}
    assert select_media(rows[:2], _REQ, generic_ok=True,
                        exclude_ids={"a"}, no_resend=True, trace=tr3) is None
    assert tr3["refused"] == "strict_no_resend"
    # 不传 trace＝零行为变化（原有断言路径全绿即证）
    assert select_media(rows, _REQ, generic_ok=True,
                        rng=random.Random(2)) is not None


def test_pick_media_feeds_gate_stats():
    from src.companion import album_gate_stats as ags
    ags.reset_for_tests()
    st = PersonaMediaStore(":memory:")
    a = st.add("lin", "photo", "/f/a.jpg", "/u/a.jpg", phash=_PH_A)
    st.record_send("c1", a["id"], persona_id="lin")
    assert pick_media(st, "lin", _REQ, generic_ok=True, conv_key="c1",
                      no_resend=True) is None
    snap = ags.snapshot()
    assert snap["refused"] == 1
    assert snap["refused_by"]["strict_no_resend"] == 1
    st.add("lin", "photo", "/f/c.jpg", "/u/c.jpg", phash=_PH_C)
    assert pick_media(st, "lin", _REQ, generic_ok=True,
                      conv_key="c1") is not None
    snap2 = ags.snapshot()
    assert snap2["picks"] == 1 and snap2["active"] is True
    ags.reset_for_tests()
    assert ags.snapshot()["active"] is False
    # 坏结构安全忽略
    ags.record(None, False)
    ags.record({"refused": "weird_reason", "cut": {"scene": "x"}}, False)
    s3 = ags.snapshot()
    assert s3["refused"] == 2 and s3["refused_by"]["no_pool"] == 2


def test_explain_match_dry_run_blocked_by():
    from src.companion.persona_media import explain_match
    rows = [
        _row("day", tags=["tod:day"]),
        _row("sum", tags=["season:summer"]),
        _row("jp", tags=["place:JP"]),
        _row("clean"),
    ]
    out = explain_match(rows, _REQ, generic_ok=True, now_hour=23,
                        now_season="winter", home_country="CA")
    assert out["pool"] == "generic"
    assert out["conv_gates_included"] is False
    by_id = {c["id"]: c for c in out["candidates"]}
    assert by_id["day"]["blocked_by"] == ["tod"]
    assert by_id["sum"]["blocked_by"] == ["season"]
    assert by_id["jp"]["blocked_by"] == ["place"]
    assert by_id["clean"]["blocked_by"] == []
    assert by_id["sum"]["tags"] == ["season:summer"]
    # 关键词池：季节/地点豁免，只提示时段
    kw = [_row("kw", triggers=["海边"], tags=["season:summer", "tod:day"])]
    out2 = explain_match(kw, "想看海边", now_hour=23,
                         now_season="winter", home_country="CA")
    assert out2["pool"] == "keyword"
    assert out2["candidates"][0]["blocked_by"] == ["tod"]
    # 不传上下文 → 全空（旧行为语义不变）
    out3 = explain_match(rows, _REQ, generic_ok=True)
    assert all(c["blocked_by"] == [] for c in out3["candidates"])


def test_resolve_consistency_cfg_new_keys():
    d = resolve_consistency_cfg({})
    assert d["no_resend"] is False
    assert d["season_gate"] is True and d["place_gate"] is True
    d2 = resolve_consistency_cfg({"consistency": {"resend_policy": "strict"}})
    assert d2["no_resend"] is True
    d3 = resolve_consistency_cfg({"consistency": {
        "enabled": False, "resend_policy": "strict"}})
    assert d3["no_resend"] is False
    assert d3["season_gate"] is False and d3["place_gate"] is False
    d4 = resolve_consistency_cfg({"consistency": {
        "season_gate": False, "place_gate": False}})
    assert d4["season_gate"] is False and d4["place_gate"] is False
