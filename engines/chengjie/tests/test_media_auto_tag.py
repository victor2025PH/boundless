"""VLM 自动打标门禁（实施90 P0-3）：prompt 契约 / 保守解析 / 人工值优先合并 /
建议态载荷 / 硬红线 / 单行与批量流水线（假 VLM，零网络零 GPU）。"""
import io
import json
from pathlib import Path

import pytest

pytest.importorskip("PIL")

from PIL import Image

from src.companion.media_auto_tag import (
    TAG_FAILED,
    TAG_SKIPPED,
    TAG_TAGGED,
    backfill_row_assets,
    build_auto_meta,
    build_auto_tag_prompt,
    derive_tags,
    hard_reject,
    parse_auto_tag_response,
    resolve_album_ai_cfg,
    run_batch,
    tag_media_row,
)
from src.companion.persona_media_store import PersonaMediaStore


def _good_json(**over):
    base = {
        "scene": "beach", "tod": "day", "season": "summer",
        "place_country": "JP", "place_confidence": "high",
        "landmark": "Enoshima", "indoor": "outdoor", "people_count": 1,
        "outfit": "white summer dress", "objects": ["sea", "umbrella"],
        "nsfw": False, "explicit": False, "looks_underage": False,
        "quality": "ok", "desc_zh": "海边白裙自拍，阳光很好",
        "triggers_zh": ["海边", "沙滩", "海边", "这个触发词实在是太长了不能要"],
        "sensitivity": 1,
    }
    base.update(over)
    return json.dumps(base, ensure_ascii=False)


def test_resolve_album_ai_cfg_defaults_on_since_d_n3():
    """#238 D-N3（2026-09-07）：上传即自动识别默认开——此前默认关＝skuio 144 张全部 autotag=False。
    运营显式 enabled: false 仍受尊重。"""
    c = resolve_album_ai_cfg({})
    assert c["enabled"] is True and c["auto_on_upload"] is True
    assert c["face_check"] is True
    off = resolve_album_ai_cfg({"companion": {"selfie": {"album_ai": {"enabled": False}}}})
    assert off["enabled"] is False
    c2 = resolve_album_ai_cfg({"companion": {"selfie": {"album_ai": {
        "enabled": True, "auto_on_upload": False, "max_batch": "50",
        "face_check": False}}}})
    assert c2 == {"enabled": True, "auto_on_upload": False, "max_batch": 50,
                  "face_check": False}


def test_prompt_contract():
    p = build_auto_tag_prompt()
    for field in ("scene", "tod", "season", "place_country",
                  "place_confidence", "landmark", "people_count", "nsfw",
                  "looks_underage", "quality", "desc_zh", "triggers_zh",
                  "sensitivity", "STRICT JSON"):
        assert field in p, field
    from src.companion.persona_media import SCENE_CLASSES
    for cls in SCENE_CLASSES:
        assert cls in p


def test_parse_good_and_normalization():
    d = parse_auto_tag_response(_good_json())
    assert d["scene"] == "beach" and d["tod"] == "day"
    assert d["season"] == "summer" and d["country"] == "JP"
    assert d["country_conf"] == "high"
    # 触发词消毒：去重 + 超长丢弃
    assert d["triggers"] == ["海边", "沙滩"]
    assert d["sensitivity"] == 1 and d["quality"] == "ok"


def test_parse_conservative_unknowns():
    d = parse_auto_tag_response(_good_json(
        scene="somewhere weird", tod="unknown", season="unknown",
        place_country="Japan", quality="great", indoor="mixed",
        people_count="many", sensitivity="hot"))
    assert d["scene"] == "" and d["tod"] == "" and d["season"] == ""
    assert d["country"] == ""            # 非 ISO 两位码不收
    assert d["quality"] == "" and d["indoor"] == ""
    assert d["people_count"] == -1 and d["sensitivity"] == 0


def test_parse_salvages_json_in_prose_and_rejects_junk():
    wrapped = "Sure! Here is the JSON:\n" + _good_json() + "\nhope it helps"
    assert parse_auto_tag_response(wrapped)["scene"] == "beach"
    assert parse_auto_tag_response("") is None
    assert parse_auto_tag_response("no json here") is None
    assert parse_auto_tag_response("[1,2,3]") is None


def test_derive_tags_manual_first_and_precedence():
    parsed = parse_auto_tag_response(_good_json())
    # 空白条目：四族全补
    tags = derive_tags(parsed, {}, [])
    assert set(tags) == {"scene:beach", "tod:day", "season:summer", "place:JP"}
    # 运营已有 scene → 绝不覆盖；其余补缺
    tags2 = derive_tags(parsed, {}, ["scene:cafe", "series:white-dress"])
    assert "scene:cafe" in tags2 and "scene:beach" not in tags2
    assert "series:white-dress" in tags2 and "tod:day" in tags2
    # EXIF 国别 > VLM 高置信；EXIF 季节只补画面无证据的缺
    tags3 = derive_tags(parsed, {"country": "CA", "season": "winter"}, [])
    assert "place:CA" in tags3 and "place:JP" not in tags3
    assert "season:summer" in tags3      # 画面证据优先
    parsed_noseason = parse_auto_tag_response(_good_json(season="unknown"))
    tags4 = derive_tags(parsed_noseason, {"season": "winter"}, [])
    assert "season:winter" in tags4
    # 低置信 VLM 国别不落标签
    parsed_low = parse_auto_tag_response(_good_json(place_confidence="low"))
    tags5 = derive_tags(parsed_low, {}, [])
    assert not any(t.startswith("place:") for t in tags5)
    # 全都有了 → None（零写）
    assert derive_tags(parsed, {}, list(tags)) is None


def test_detect_tag_conflicts():
    from src.companion.media_auto_tag import detect_tag_conflicts
    parsed = parse_auto_tag_response(_good_json(
        scene="night_city", tod="night", season="summer",
        place_country="JP", place_confidence="high"))
    # 真分歧：人工 park（原始 slug 也认得）vs AI night_city；tod 昼夜相反
    c = detect_tag_conflicts(parsed, ["scene:outdoor-park", "tod:day"])
    assert c["scene"] == {"manual": "park", "ai": "night_city"}
    assert c["tod"] == {"manual": "day", "ai": "night"}
    # 等价组防误报：镜前自拍 人工 home vs AI bedroom 不算分歧
    parsed2 = parse_auto_tag_response(_good_json(scene="bedroom"))
    assert "scene" not in detect_tag_conflicts(parsed2, ["scene:home"])
    # AI 无结论不算分歧；人工无标注不算分歧
    parsed3 = parse_auto_tag_response(_good_json(
        scene="somewhere weird", tod="unknown", season="unknown"))
    assert detect_tag_conflicts(parsed3, ["scene:park", "tod:day"]) == {}
    assert detect_tag_conflicts(parsed, []) == {}
    # place：只拿 AI 高置信对比
    c2 = detect_tag_conflicts(parsed, ["place:CA"])
    assert c2["place"] == {"manual": "CA", "ai": "JP"}
    parsed_low = parse_auto_tag_response(_good_json(place_confidence="low"))
    assert "place" not in detect_tag_conflicts(parsed_low, ["place:CA"])
    # season 分歧
    c3 = detect_tag_conflicts(parsed, ["season:winter"])
    assert c3["season"] == {"manual": "winter", "ai": "summer"}


def test_tag_media_row_records_conflicts(tmp_path):
    st = PersonaMediaStore(":memory:")
    p = _mk_photo(tmp_path, "cf.jpg")
    row = st.add("lin", "photo", str(p), "/u/cf.jpg",
                 tags=["scene:park", "tod:day"])
    tag_media_row(st, row, {}, describe=lambda c, pth, pr: _good_json(
        scene="night_city", tod="night"))
    got = st.get(row["id"])
    conf = got["auto_meta"]["conflicts"]
    assert conf["scene"]["ai"] == "night_city"
    assert conf["tod"] == {"manual": "day", "ai": "night"}
    # 人工标签原样健在（分歧只是提示）
    assert "scene:park" in got["tags"] and "tod:day" in got["tags"]
    # 无分歧则不写键
    row2 = st.add("lin", "photo", str(p), "/u/cf2.jpg", sha256="z2",
                  tags=["scene:beach"])
    tag_media_row(st, row2, {}, describe=lambda c, pth, pr: _good_json())
    assert "conflicts" not in st.get(row2["id"])["auto_meta"]


def test_build_auto_meta_and_bond_suggestion():
    parsed = parse_auto_tag_response(_good_json(sensitivity=2))
    meta = build_auto_meta(parsed, {"year": 2026, "month": 7, "country": "JP",
                                    "season": "summer"}, model="m", now=123.0)
    assert meta["v"] == 1 and meta["ts"] == 123.0 and meta["model"] == "m"
    assert meta["triggers_suggest"] == ["海边", "沙滩"]
    assert meta["sensitivity"] == 2 and meta["suggest_min_bond"] == 45
    assert meta["exif"]["month"] == 7 and meta["flags"]["nsfw"] is False
    assert meta["desc"].startswith("海边")


def test_hard_reject():
    assert hard_reject({"underage": True, "nsfw": True}) is True
    assert hard_reject({"underage": True, "explicit": True}) is True
    assert hard_reject({"underage": True}) is False    # 单独年龄误判不误杀
    assert hard_reject({"nsfw": True}) is False
    assert hard_reject(None) is False


# ── 单行 / 批量流水线（:memory: store + 真图 tmp 文件 + 假 VLM）─────────────

def _mk_photo(tmp_path, name="a.jpg", size=(600, 400)):
    p = tmp_path / name
    Image.new("RGB", size, (120, 140, 40)).save(p, "JPEG")
    return p


def _store_with_photo(tmp_path, **add_kw):
    st = PersonaMediaStore(":memory:")
    p = _mk_photo(tmp_path)
    row = st.add("lin", "photo", str(p), "/static/persona_albums/lin/a.jpg",
                 sha256="x", **add_kw)
    return st, row


def test_tag_media_row_success(tmp_path):
    st, row = _store_with_photo(tmp_path)
    status = tag_media_row(st, row, {}, describe=lambda c, p, pr: _good_json())
    assert status == TAG_TAGGED
    got = st.get(row["id"])
    assert got["tag_status"] == TAG_TAGGED
    assert "scene:beach" in got["tags"] and "season:summer" in got["tags"]
    assert got["auto_meta"]["desc"] and got["auto_meta"]["suggest_min_bond"] == 20
    assert got["enabled"] is True        # 建议态：不动启停/触发词/门槛
    assert got["triggers"] == [] and got["min_bond_level"] == 0


def test_tag_media_row_vlm_down_and_parse_fail(tmp_path):
    st, row = _store_with_photo(tmp_path)
    assert tag_media_row(st, row, {}, describe=lambda c, p, pr: None) == TAG_FAILED
    assert st.get(row["id"])["tag_status"] == TAG_FAILED
    assert tag_media_row(st, row, {},
                         describe=lambda c, p, pr: "not json") == TAG_FAILED


def test_tag_media_row_video_without_cover_skipped(tmp_path):
    st = PersonaMediaStore(":memory:")
    row = st.add("lin", "video", str(tmp_path / "v.mp4"), "/x/v.mp4")
    assert tag_media_row(st, row, {},
                         describe=lambda c, p, pr: _good_json()) == TAG_SKIPPED
    assert st.get(row["id"])["tag_status"] == TAG_SKIPPED


def test_tag_media_row_hard_redline_disables(tmp_path):
    st, row = _store_with_photo(tmp_path)
    bad = _good_json(looks_underage=True, nsfw=True)
    assert tag_media_row(st, row, {}, describe=lambda c, p, pr: bad) == TAG_TAGGED
    got = st.get(row["id"])
    assert got["enabled"] is False
    assert got["auto_meta"]["flags"]["underage"] is True


def test_exif_hints_survive_tagging(tmp_path):
    st, row = _store_with_photo(tmp_path)
    st.set_auto_tag(row["id"], auto_meta={"exif": {
        "year": 2026, "month": 1, "country": "CA", "season": "winter"}})
    row = st.get(row["id"])
    tag_media_row(st, row, {}, describe=lambda c, p, pr: _good_json(
        season="unknown", place_country="", place_confidence="low"))
    got = st.get(row["id"])
    assert "season:winter" in got["tags"] and "place:CA" in got["tags"]
    assert got["auto_meta"]["exif"]["country"] == "CA"


def test_face_check_prompt_and_parse():
    from src.companion.media_auto_tag import (
        build_face_check_prompt, parse_face_check_response)
    p = build_face_check_prompt()
    assert "same_person" in p and "STRICT JSON" in p
    assert parse_face_check_response('{"same_person": "yes"}') == "yes"
    assert parse_face_check_response('{"same_person": "NO"}') == "no"
    assert parse_face_check_response("blah {\"same_person\":\"unsure\"} ok") == "unsure"
    assert parse_face_check_response("unsure") == "unsure"
    assert parse_face_check_response("maybe the same person, hard to tell "
                                     "given lighting") == ""
    assert parse_face_check_response("") == ""


def test_face_check_writes_advisory_only(tmp_path):
    from src.companion.media_auto_tag import tag_media_row
    ref = _mk_photo(tmp_path, "face_ref.jpg", size=(200, 200))
    st, row = _store_with_photo(tmp_path)
    pair_calls = []

    def pair(cfg, paths, prompt):
        pair_calls.append(tuple(paths))
        return '{"same_person": "no"}'

    status = tag_media_row(st, row, {}, describe=lambda c, p, pr: _good_json(),
                           describe_pair=pair, face_ref=str(ref))
    assert status == TAG_TAGGED
    got = st.get(row["id"])
    assert got["auto_meta"]["face_match"] == "no"
    assert got["enabled"] is True            # 建议态：绝不自动下架
    assert pair_calls and str(ref) in pair_calls[0][0]
    # 多人照（people_count=2）不比对
    st2, row2 = _store_with_photo(tmp_path)
    pair_calls.clear()
    tag_media_row(st2, row2, {}, describe=lambda c, p, pr: _good_json(people_count=2),
                  describe_pair=pair, face_ref=str(ref))
    assert pair_calls == []
    assert "face_match" not in st2.get(row2["id"])["auto_meta"]
    # 无基准照 → 不比对；比对失败 → 不写键、主打标不受影响
    st3, row3 = _store_with_photo(tmp_path)
    tag_media_row(st3, row3, {}, describe=lambda c, p, pr: _good_json(),
                  describe_pair=pair, face_ref="")
    assert "face_match" not in st3.get(row3["id"])["auto_meta"]
    st4, row4 = _store_with_photo(tmp_path)
    assert tag_media_row(st4, row4, {}, describe=lambda c, p, pr: _good_json(),
                         describe_pair=lambda c, p, pr: None,
                         face_ref=str(ref)) == TAG_TAGGED
    assert "face_match" not in st4.get(row4["id"])["auto_meta"]


def test_run_batch_inline_assets_and_tags(tmp_path):
    st = PersonaMediaStore(":memory:")
    p1 = _mk_photo(tmp_path, "one.jpg")
    p2 = _mk_photo(tmp_path, "two.jpg")
    r1 = st.add("lin", "photo", str(p1), "/static/persona_albums/lin/one.jpg")
    r2 = st.add("lin", "photo", str(p2), "/static/persona_albums/lin/two.jpg",
                tags=["scene:cafe"])
    calls = []

    def fake(cfg, path, prompt):
        calls.append(path)
        return _good_json()

    out = run_batch(st, {}, inline=True, describe=fake)
    assert out["ok"] is True and out["queued"] == 2 and out["assets"] == 2
    g1, g2 = st.get(r1["id"]), st.get(r2["id"])
    for g in (g1, g2):
        assert g["tag_status"] == TAG_TAGGED
        assert g["phash"] and len(g["phash"]) == 16
        assert g["thumb_url"].endswith(".thumb.webp")
    assert "scene:cafe" in g2["tags"]            # 人工值健在
    # 幂等：only_missing 再跑零打标
    calls.clear()
    out2 = run_batch(st, {}, inline=True, describe=fake)
    assert out2["queued"] == 0 and calls == []


def test_backfill_row_assets_photo_only(tmp_path):
    st = PersonaMediaStore(":memory:")
    p = _mk_photo(tmp_path, "x.jpg")
    row = st.add("lin", "photo", str(p), "/static/persona_albums/lin/x.jpg")
    out = backfill_row_assets(st, row)
    assert out["thumb"] is True and out["phash"] is True
    assert out["published"] is False        # url 已有＝上传件，无需发布
    got = st.get(row["id"])
    assert got["thumb_url"] == "/static/persona_albums/lin/x.jpg.thumb.webp"
    assert (tmp_path / "x.jpg.thumb.webp").is_file()
    # 已有资产 → 幂等零动作
    out2 = backfill_row_assets(st, got)
    assert not any(out2.values())


def test_backfill_publishes_curated_rows(tmp_path):
    """策展入册（url 空、文件在素材目录）→ 发布进 static + 缩略图 + pHash。"""
    st = PersonaMediaStore(":memory:")
    src_dir = tmp_path / "assets" / "lin"
    src_dir.mkdir(parents=True)
    p = _mk_photo(src_dir, "cafe_white-dress_01.jpg")
    static_root = tmp_path / "static_albums"
    row = st.add("lin", "photo", str(p), "")     # url 空＝裂图形态
    out = backfill_row_assets(st, row, publish_root=str(static_root),
                              url_base="/static/persona_albums")
    assert out["published"] is True and out["thumb"] is True
    assert out["phash"] is True
    got = st.get(row["id"])
    assert got["url"] == "/static/persona_albums/lin/cafe_white-dress_01.jpg"
    assert got["thumb_url"] == got["url"] + ".thumb.webp"
    copy = static_root / "lin" / "cafe_white-dress_01.jpg"
    assert copy.is_file()
    assert (static_root / "lin" / "cafe_white-dress_01.jpg.thumb.webp").is_file()
    assert Path(got["file_path"]) == p           # 原件身份不动（发送链契约）
    # 幂等：二跑零动作
    out2 = backfill_row_assets(st, got, publish_root=str(static_root),
                               url_base="/static/persona_albums")
    assert not any(out2.values())
    # 同名不同内容 → 换名不覆盖
    (src_dir.parent / "lin2").mkdir(parents=True, exist_ok=True)
    p2 = _mk_photo(src_dir.parent / "lin2", "cafe_white-dress_01.jpg",
                   size=(300, 200))
    row2 = st.add("lin", "photo", str(p2), "")
    backfill_row_assets(st, row2, publish_root=str(static_root),
                        url_base="/static/persona_albums")
    got2 = st.get(row2["id"])
    assert got2["url"] and got2["url"] != got["url"]


def test_backfill_video_publish_cover_soft(tmp_path):
    """视频发布：url 回填必成；封面依赖 ffmpeg/真视频——假文件软失败不阻塞。"""
    st = PersonaMediaStore(":memory:")
    v = tmp_path / "dance.mp4"
    v.write_bytes(b"not a real video")
    row = st.add("lin", "video", str(v), "")
    out = backfill_row_assets(st, row, publish_root=str(tmp_path / "sroot"),
                              url_base="/static/persona_albums")
    assert out["published"] is True
    got = st.get(row["id"])
    assert got["url"].endswith("/lin/dance.mp4")
    assert (tmp_path / "sroot" / "lin" / "dance.mp4").is_file()
    # 假视频抽不出封面 → cover False、thumb_url 保持空（软失败）
    if not out["cover"]:
        assert got["thumb_url"] == ""


def test_lan_vision_cfg_privacy_hard_gate():
    """隐私硬线（2026-08-30 生产实锤钉死）：云端点必须被剥掉、3s 快车道超时必须还原。"""
    from src.companion.media_auto_tag import _lan_vision_cfg
    cfg = {
        "provider": "openai_compatible",
        "base_url": "http://192.168.0.176:11434/v1",
        "base_urls": ["http://192.168.0.176:11434/v1",
                      "https://api.siliconflow.cn/v1"],
        "endpoint_timeouts": {"192.168.0.176": 3, "siliconflow": 11},
        "timeout": 20,
        "zhipu_api_key": "sk-cloud",
    }
    out = _lan_vision_cfg(cfg)
    assert out["base_urls"] == ["http://192.168.0.176:11434/v1"]
    assert out["base_url"] == "http://192.168.0.176:11434/v1"
    assert "endpoint_timeouts" not in out
    assert out["timeout"] >= 60
    assert out["zhipu_api_key"] == ""
    # localhost/10.x 属内网；纯云配置 → None（宁可不打标绝不出网）
    assert _lan_vision_cfg({"base_urls": ["http://localhost:11434/v1"]}) is not None
    assert _lan_vision_cfg({"base_urls": ["http://10.1.2.3:11434/v1"]}) is not None
    assert _lan_vision_cfg({"base_urls": ["https://api.siliconflow.cn/v1"]}) is None
    assert _lan_vision_cfg({"base_urls": ["https://vision.example.com/v1"]}) is None
    assert _lan_vision_cfg({}) is None
