"""相册健康体检门禁（实施90 阶段二）：纯函数聚合 + 判词全路径 + 近重复族并查。"""
import time

from src.companion.album_health import build_album_health

_NOW = 1_800_000_000.0
_PH_A = "00000000000000ff"
_PH_B = "00000000000000fe"     # 距 A 1 bit
_PH_C = "ffffffffffff0000"


def _row(rid, pid="lin", **over):
    base = {
        "id": rid, "persona_id": pid, "media_type": "photo", "enabled": True,
        "triggers": [], "tags": [], "auto_meta": {}, "phash": "",
        "thumb_url": "/t.webp", "hits": 1, "last_sent_at": _NOW - 100,
        "created_at": _NOW - 30 * 86400, "url": f"/u/{rid}.jpg",
        "file_path": f"/f/{rid}.jpg", "weight": 1, "min_bond_level": 0,
    }
    base.update(over)
    return base


def test_empty_and_grouping():
    rep = build_album_health([], now=_NOW)
    assert rep["personas"] == {}
    rep2 = build_album_health([_row("a"), _row("b", pid="mei")], now=_NOW)
    assert set(rep2["personas"]) == {"lin", "mei"}
    assert rep2["personas"]["lin"]["total"] == 1


def test_healthy_album_no_verdict():
    rows = [_row("a", tag_status="tagged", phash=_PH_A,
                 tags=["scene:cafe"]),
            _row("b", tag_status="tagged", phash=_PH_C,
                 tags=["scene:beach"])]
    p = build_album_health(rows, now=_NOW)["personas"]["lin"]
    assert p["verdict"] == []
    assert p["scene_supply"] == {"cafe": 1, "beach": 1}
    assert p["phash_cover"] == "2/2"


def test_untagged_thumbs_and_zero_hit_verdicts():
    rows = [
        _row("a", tag_status="", thumb_url="", hits=0, last_sent_at=0,
             created_at=_NOW - 20 * 86400),
        _row("b", tag_status="failed", hits=0, last_sent_at=0,
             created_at=_NOW - 3 * 86400),
        _row("c", tag_status="tagged", hits=0, last_sent_at=0,
             created_at=_NOW - 20 * 86400),
    ]
    p = build_album_health(rows, now=_NOW)["personas"]["lin"]
    assert p["tag_status"]["untagged"] == 1 and p["tag_status"]["failed"] == 1
    assert p["thumbs_missing"] == 1
    assert p["zero_hit_enabled"] == 3 and p["stale_never_sent"] == 2
    v = " | ".join(p["verdict"])
    assert "先跑「AI 补标」" in v and "缺缩略图 1" in v
    assert "零命中 3/3" in v and "从未发出 2" in v


def test_neardup_flags_and_demand_verdicts():
    rows = [
        _row("a", tag_status="tagged", phash=_PH_A),
        _row("b", tag_status="tagged", phash=_PH_B),
        _row("c", tag_status="tagged", phash=_PH_C,
             auto_meta={"flags": {"nsfw": True}, "quality": "blurry"}),
        _row("d", tag_status="tagged",
             auto_meta={"face_match": "no",
                        "conflicts": {"scene": {"manual": "park",
                                                "ai": "night_city"}}}),
    ]
    demand = {"street": {"demand": 6, "unmet": 5},
              "cafe": {"demand": 2, "unmet": 0}}
    p = build_album_health(rows, demand, now=_NOW)["personas"]["lin"]
    assert p["neardup_groups"] == [["a", "b"]]
    assert p["flags"] == {"sensitive": 1, "lowq": 1, "face_mismatch": 1,
                          "conflicts": 1}
    assert p["missing_demand"] == [{"scene": "street", "unmet": 5}]
    v = " | ".join(p["verdict"])
    assert "近重复 1 组共 2 张" in v
    assert "敏感素材 1" in v and "疑似非本人 1" in v and "标注分歧 1" in v
    assert "客户在要「street」" in v
    # 有备货的场景不算缺货
    rows2 = rows + [_row("e", tag_status="tagged", tags=["scene:street"])]
    p2 = build_album_health(rows2, demand, now=_NOW)["personas"]["lin"]
    assert p2["missing_demand"] == []


def test_disabled_rows_do_not_count_supply_or_zero_hit():
    rows = [_row("a", enabled=False, hits=0, tags=["scene:beach"],
                 tag_status="tagged")]
    p = build_album_health(rows, now=_NOW)["personas"]["lin"]
    assert p["enabled"] == 0
    assert p["scene_supply"] == {} and p["zero_hit_enabled"] == 0
