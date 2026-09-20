"""图文一致性 P0 门禁（2026-07-27）：发的图必须和说的话/时间/衣服对得上。

覆盖四层（实录事故 → 不变量）：
- **场景/时段/冷却/连续性纯函数**（``persona_media``）：场景类归一、昼夜冲突、
  select_media 的硬冷却（绝不放宽）/场景硬匹配（仅通用池）/时段软过滤/同系列
  优先；pick_media 从持久账本推导冷却与连续窗。
- **文件系统相册**（``SelfieProvider._pick_from_album`` + ``_meta.json`` sidecar）：
  点名场景挑不到＝如实失败（绝不顶包）；时段过滤 + 全冲突放行须打
  ``tod_softened`` 标；跨人设隔离（多人设布局下根目录不做兜底）。
- **配文诚实**（``_fixed_caption``/``caption_album`` 池/LLM 配文指令）：相册旧照
  绝不配「刚拍」；freshness=old 的指令必须显式禁止时间谎言。
- **承诺场景与质疑观测**（``promised_scene``/``detect_media_complaint``）：
  承诺句点名的场景要能抽出来（兑现硬要求）；收图后质疑有信号可计数。
- **元数据回填纯核心**（``persona_media_meta_backfill``）：VLM 昼夜解析保守、
  manifest 归一、人工值优先、DB 标签幂等。
"""
import json
import random

import pytest

import src.ai.companion_selfie as cs
from src.companion import persona_media as pm
from src.companion import persona_media_meta_backfill as bf
from src.inbox import image_autosend as ia


@pytest.fixture(autouse=True)
def _reset():
    from src.companion.persona_media_store import (
        configure_persona_media_store, reset_persona_media_store)
    cs.reset_selfie_provider()
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    yield
    cs.reset_selfie_provider()
    reset_persona_media_store()


# ── 场景类归一 ──────────────────────────────────────────────────────────────
def test_scene_class_of_en_phrase_zh_word_and_slug():
    assert pm.scene_class_of("walking on the beach at sunset") == "beach"
    assert pm.scene_class_of("发张海边的照片") == "beach"
    assert pm.scene_class_of("outdoor-park") == "park"          # 策展 slug
    assert pm.scene_class_of("mirror-selfie") == "home"         # 居家镜自拍
    assert pm.scene_class_of("cozy home, sitting on couch") == "home"
    assert pm.scene_class_of("图书馆自习") == "library"


def test_scene_class_of_car_word_boundary():
    assert pm.scene_class_of("sitting in my car") == "car"
    assert pm.scene_class_of("车里自拍") == "car"
    # cardigan/carpet 不得误判成 car
    assert pm.scene_class_of("wearing a cardigan on the carpet") == ""


def test_scene_class_of_unknown_returns_empty():
    assert pm.scene_class_of("") == ""
    assert pm.scene_class_of("changing-room") == ""  # 词表外=未知（宁缺勿错）


# ── 昼夜冲突纯函数 ──────────────────────────────────────────────────────────
def test_tod_conflicts_day_photo_late_night():
    assert pm.tod_conflicts_with_hour("day", 23) is True
    assert pm.tod_conflicts_with_hour("day", 2) is True
    assert pm.tod_conflicts_with_hour("day", 12) is False


def test_tod_conflicts_night_photo_daytime():
    assert pm.tod_conflicts_with_hour("night", 10) is True
    assert pm.tod_conflicts_with_hour("night", 23) is False


def test_tod_dusk_window_allows_both():
    for h in (17, 18, 21):
        assert pm.tod_conflicts_with_hour("day", h) is False
        assert pm.tod_conflicts_with_hour("night", h) is False


def test_tod_unknown_or_bad_never_conflicts():
    assert pm.tod_conflicts_with_hour("", 23) is False
    assert pm.tod_conflicts_with_hour("dawn", 23) is False
    assert pm.tod_conflicts_with_hour("day", None) is False
    assert pm.tod_conflicts_with_hour("day", "x") is False


def test_row_tod_and_scene_class_from_tags_and_filename():
    row = {"tags": ["curated", "tod:night", "scene:mirror-selfie"],
           "file_path": "x/whatever.jpg"}
    assert pm.row_tod(row) == "night"
    assert pm.row_scene_class(row) == "home"
    # 无 scene: 标签 → 策展文件名前缀兜底
    row2 = {"tags": [], "file_path": "a/cafe_white-dress_02.jpg"}
    assert pm.row_scene_class(row2) == "cafe"
    assert pm.row_tod(row2) == ""


# ── select_media 一致性过滤 ─────────────────────────────────────────────────
def _row(i, *, triggers=(), tags=(), last=0):
    return {"id": str(i), "media_type": "photo", "enabled": True,
            "triggers": list(triggers), "tags": list(tags), "weight": 1,
            "last_sent_at": last}


def test_select_hard_exclude_never_relaxes():
    rows = [_row(1), _row(2)]
    out = pm.select_media(rows, "看看你", generic_ok=True,
                          hard_exclude_ids={"1", "2"})
    assert out is None  # 冷却窗内全排除 → 拒发（交生成/诚实文字）


def test_select_required_scene_only_from_generic_pool():
    rows = [_row(1, tags=["scene:cafe"]), _row(2, tags=["scene:beach"])]
    out = pm.select_media(rows, "发张海边的", generic_ok=True,
                          required_scene_class="beach")
    assert out and out["id"] == "2"
    # 全池都不匹配 → None（绝不顶包）
    assert pm.select_media([_row(1, tags=["scene:cafe"])], "海边",
                           generic_ok=True, required_scene_class="beach") is None
    # 场景未知的条目不算匹配（宁缺勿错）
    assert pm.select_media([_row(1)], "海边", generic_ok=True,
                           required_scene_class="beach") is None


def test_select_required_scene_keyword_pool_unrestricted():
    # 运营显式绑定触发词的条目优先级最高，不受场景硬匹配限制
    # （例句须带求图动词——「你会跳舞吗」这类信息性疑问句会让关键词池
    #   整体让路（is_info_question 语义），与场景硬匹配正交）
    rows = [_row(1, triggers=["跳舞"], tags=["scene:home"])]
    out = pm.select_media(rows, "跳舞给我看看", generic_ok=False,
                          required_scene_class="beach")
    assert out and out["id"] == "1"
    # 信息性提问（问名词本身）→ 关键词池让路，通用池又没开 → None
    assert pm.select_media(rows, "你会跳舞吗", generic_ok=False,
                           required_scene_class="beach") is None


def test_select_now_hour_soft_filter():
    rows = [_row(1, tags=["tod:day"]), _row(2, tags=["tod:night"])]
    out = pm.select_media(rows, "看看你", generic_ok=True, now_hour=23,
                          rng=random.Random(7))
    assert out and out["id"] == "2"  # 深夜剔掉白天照
    # 全冲突 → 放行原池（软过滤，配文层兜底诚实）
    out2 = pm.select_media([_row(1, tags=["tod:day"])], "看看你",
                           generic_ok=True, now_hour=23)
    assert out2 and out2["id"] == "1"


def test_select_prefer_series_same_outfit_first():
    rows = [_row(1, tags=["series:pink-dress"]),
            _row(2, tags=["series:white-shirt"]),
            _row(3, tags=["series:pink-dress"])]
    out = pm.select_media(rows, "再拍一张", generic_ok=True,
                          prefer_series="pink-dress", exclude_ids={"1"},
                          rng=random.Random(1))
    assert out and out["id"] == "3"  # 同系列未发的那张优先


def test_pick_media_cooldown_and_continuity_from_ledger():
    from src.companion.persona_media_store import get_persona_media_store
    st = get_persona_media_store()
    r1 = st.add("p1", "photo", "f1.jpg", "", tags=["series:pink-dress"])
    r2 = st.add("p1", "photo", "f2.jpg", "", tags=["series:pink-dress"])
    st.add("p1", "photo", "f3.jpg", "", tags=["series:white-shirt"])
    now = 1_700_000_000.0
    # 10 分钟前发过 r1 → 冷却窗（24h）内 r1 硬排除；连续窗（90min）内优先同系列 → r2
    st.record_send("ck", str(r1["id"]), persona_id="p1",
                   series="pink-dress", now=now - 600)
    out = pm.pick_media(st, "p1", "再来一张", generic_ok=True, conv_key="ck",
                        resend_cooldown_hours=24, continuity_minutes=90,
                        now=now, rng=random.Random(3))
    assert out and out["id"] == str(r2["id"])
    # 冷却窗外（26h 前）→ r1 回到候选；连续窗过期 → 无系列偏好
    st2_now = now + 26 * 3600
    out2 = pm.pick_media(st, "p1", "再来一张", generic_ok=True, conv_key="ck",
                         resend_cooldown_hours=24, continuity_minutes=90,
                         now=st2_now, rng=random.Random(3))
    assert out2 is not None


# ── SelfieProvider 相册：场景硬匹配 / 时段 / sidecar / 跨人设隔离 ───────────
def _album(tmp_path, key="lin", files=(), meta=None):
    d = tmp_path / "albums" / key
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).write_bytes(b"\x89PNGdummy")
    if meta is not None:
        (d / "_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return d


def _provider(tmp_path, **over):
    cfg = {"enabled": True, "backend": "album",
           "album_dir": str(tmp_path / "albums")}
    cfg.update(over)
    return cs.SelfieProvider(cfg)


async def test_album_scene_mismatch_fails_honestly(tmp_path):
    _album(tmp_path, "lin", ["cafe_white-dress_01.jpg", "car_white-shirt_01.jpg"])
    prov = _provider(tmp_path)
    res = await prov.generate("", album_key="lin", album_scene="beach")
    assert not res.ok and res.error == "album_scene_mismatch"
    # 点名到有的场景 → 只出该场景
    res2 = await prov.generate("", album_key="lin", album_scene="cafe")
    assert res2.ok and "cafe_" in res2.image_path
    assert res2.extra.get("scene_class") == "cafe"


async def test_album_tod_filter_and_softened_flag(tmp_path):
    meta = {"cafe_white-dress_01.jpg": {"tod": "day"},
            "street_night-selfie_01.jpg": {"tod": "night"}}
    _album(tmp_path, "lin",
           ["cafe_white-dress_01.jpg", "street_night-selfie_01.jpg"], meta)
    prov = _provider(tmp_path)
    res = await prov.generate("", album_key="lin", now_hour=23)
    assert res.ok and "street_night" in res.image_path
    assert res.extra.get("tod_softened") is False
    # 池里只剩冲突照 → 放行但打 softened 标（配文层按旧照口径兜底）
    only_day = _album(tmp_path, "day_only", ["cafe_white-dress_01.jpg"],
                      {"cafe_white-dress_01.jpg": {"tod": "day"}})
    assert only_day.is_dir()
    res2 = await prov.generate("", album_key="day_only", now_hour=23)
    assert res2.ok and res2.extra.get("tod_softened") is True


async def test_album_sidecar_overrides_filename_meta(tmp_path):
    # sidecar 显式 scene 优先于文件名前缀解析
    meta = {"cafe_white-dress_01.jpg": {"scene": "beach", "series": "bikini"}}
    _album(tmp_path, "lin", ["cafe_white-dress_01.jpg"], meta)
    prov = _provider(tmp_path)
    res = await prov.generate("", album_key="lin", album_scene="beach")
    assert res.ok and res.extra.get("series") == "bikini"


async def test_album_prefer_series_continuity(tmp_path):
    _album(tmp_path, "lin", [
        "cafe_white-dress_01.jpg", "cafe_white-dress_02.jpg",
        "car_black-top_01.jpg"])
    prov = _provider(tmp_path)
    res = await prov.generate(
        "", album_key="lin", prefer_series="white-dress",
        exclude_paths={"cafe_white-dress_01.jpg"})
    assert res.ok and "white-dress_02" in res.image_path


async def test_album_root_no_cross_persona_fallback(tmp_path):
    # 多人设分册布局：根目录散图不做跨人设兜底（换脸换人事故通道）
    _album(tmp_path, "other", ["cafe_white-dress_01.jpg"])
    (tmp_path / "albums" / "legacy_root.jpg").write_bytes(b"\x89PNGdummy")
    prov = _provider(tmp_path)
    res = await prov.generate("", album_key="ghost")  # 该人设无分册
    assert not res.ok and res.error == "album_empty"
    # default_album_key＝运营显式共享素材包 → 语义保留（opt-in 不受隔离影响）
    prov2 = _provider(tmp_path, default_album_key="other")
    res2 = await prov2.generate("", album_key="ghost")
    assert res2.ok and "cafe_white-dress_01" in res2.image_path


async def test_album_root_flat_layout_still_serves(tmp_path):
    # 单人设平铺布局（根目录放图、无任何分册）：老行为不变
    root = tmp_path / "albums"
    root.mkdir(parents=True, exist_ok=True)
    (root / "selfie_pink-shirt_01.jpg").write_bytes(b"\x89PNGdummy")
    prov = _provider(tmp_path)
    res = await prov.generate("", album_key="whoever")
    assert res.ok and "selfie_pink-shirt_01" in res.image_path


# ── 配文诚实（fixed 兜底 / caption_album 池 / LLM 指令）────────────────────
def test_fixed_caption_old_never_uses_fresh_claim():
    scfg = {"caption": "刚拍的，给你看～", "caption_album": ""}
    cap = ia._fixed_caption(scfg, ia.KIND_SELFIE, "old", "zh")
    assert cap and "刚拍" not in cap  # 旧照走 caption_album 池，绝不用「刚拍」口径
    assert ia._fixed_caption(scfg, ia.KIND_SELFIE, "fresh", "zh") == "刚拍的，给你看～"
    # 运营显式配 caption_album 优先
    scfg2 = {"caption": "x", "caption_album": "翻到一张旧照"}
    assert ia._fixed_caption(scfg2, ia.KIND_SELFIE, "old", "") == "翻到一张旧照"
    # 视频无旧照池（措辞是"照片"）→ 只认 caption_album 配置
    assert ia._fixed_caption(scfg, "video", "old", "zh") == ""
    # 物体图恒走 contextual_caption
    scfg3 = {"contextual_caption": "拍好啦"}
    assert ia._fixed_caption(scfg3, ia.KIND_OBJECT, "fresh", "") == "拍好啦"


def test_caption_album_pool_is_honest_bilingual():
    banned = ("刚拍", "剛拍", "新鲜出炉", "现在拍", "just took", "just now",
              "just shot", "fresh out")
    for lang in ("zh", "en"):
        pool = cs._STAGE_TEXTS["caption_album"][lang]
        assert pool, f"caption_album {lang} 池不能为空"
        for text in pool:
            low = text.lower()
            for b in banned:
                assert b not in low, f"旧照配文池不得含时间谎言：{text!r}"


def test_caption_instruction_freshness_old_forbids_fresh_claim():
    ins_old = cs.build_photo_caption_instruction(
        "看看你", kind="selfie", freshness="old")
    assert "之前拍的" in ins_old and "禁止" in ins_old
    assert "刚拍的自拍照" not in ins_old
    ins_fresh = cs.build_photo_caption_instruction(
        "看看你", kind="selfie", freshness="fresh")
    assert "刚拍" in ins_fresh


# ── 承诺场景抽取 + 收图质疑信号 ─────────────────────────────────────────────
def test_promised_scene_extracted_from_promise_sentence():
    from src.ai.outbound_promise_guard import promised_scene
    sc = promised_scene("好呀，等我拍张海边的照片发你哦")
    assert sc and "beach" in sc.lower()
    # 非承诺句里的场景词不算承诺内容
    assert promised_scene("我今天去了海边玩") == ""
    # 承诺没点名场景 → 空串（兑现不限场景）
    assert promised_scene("等我拍张照片发你") == ""


def test_detect_media_complaint_kinds_and_limits():
    assert cs.detect_media_complaint("怎么又是这张照片") == "repeat"
    assert cs.detect_media_complaint("这根本不是你吧") == "not_you"
    assert cs.detect_media_complaint("这是网图吧，骗人") == "fake"
    assert cs.detect_media_complaint("same photo again??") == "repeat"
    assert cs.detect_media_complaint("今天天气不错") == ""
    assert cs.detect_media_complaint("") == ""
    assert cs.detect_media_complaint("x" * 300) == ""  # 超长不判（多半在聊别的）


def test_record_media_complaint_counts():
    before = ia.metrics_snapshot().get("complaints", 0)
    ia.record_media_complaint("fake:这是网图吧")
    snap = ia.metrics_snapshot()
    assert snap.get("complaints", 0) == before + 1
    assert "网图" in snap.get("last_complaint", "")


# ── 元数据回填纯核心 ────────────────────────────────────────────────────────
def test_parse_tod_response_strict_and_conservative():
    assert bf.parse_tod_response('{"tod": "day"}') == "day"
    assert bf.parse_tod_response('json里说 {"tod": "night"} 啦') == "night"
    assert bf.parse_tod_response("day") == "day"
    assert bf.parse_tod_response("NIGHT") == "night"
    assert bf.parse_tod_response('{"tod": "unknown"}') == ""
    assert bf.parse_tod_response("室内看不出来，可能是白天也可能是晚上 day night") == ""
    assert bf.parse_tod_response("") == ""
    assert bf.parse_tod_response(None) == ""


def test_manifest_entries_canonicalize_scene():
    mf = {"photos": [
        {"file": "outdoor-park_gray-dress_01.jpg", "scene": "outdoor-park",
         "outfit": "gray-dress"},
        {"file": "mirror-selfie_pink-dress_01.jpg", "scene": "mirror-selfie",
         "outfit": "pink-dress"},
        {"bad": "row"},
    ]}
    ent = bf.manifest_entries(mf)
    assert ent["outdoor-park_gray-dress_01.jpg"]["scene"] == "park"
    assert ent["mirror-selfie_pink-dress_01.jpg"]["scene"] == "home"
    assert bf.manifest_entries(None) == {}
    assert bf.manifest_entries({"photos": "junk"}) == {}


def test_merge_file_meta_existing_wins():
    out = bf.merge_file_meta(
        "cafe_white-dress_01.jpg",
        {"scene": "park", "series": "manifest-series"},
        {"scene": "beach", "tod": "night"})
    assert out["scene"] == "beach" and out["tod"] == "night"
    assert out["series"] == "manifest-series"
    # 全空来源 → 文件名约定兜底
    out2 = bf.merge_file_meta("cafe_white-dress_01.jpg", None, None)
    assert out2 == {"scene": "cafe", "series": "white-dress"}


def test_plan_album_backfill_skips_specials_and_lists_need_tod():
    files = ["cafe_white-dress_01.jpg", "face_ref.jpg", "manifest.json",
             "_meta.json", "video_01.mp4",
             "street_night-selfie_01.jpg"]
    existing = {"street_night-selfie_01.jpg": {"tod": "night"}}
    plan = bf.plan_album_backfill(files, None, existing)
    assert set(plan["need_tod"]) == {"cafe_white-dress_01.jpg"}
    assert "face_ref.jpg" not in plan["meta"]
    assert "video_01.mp4" not in plan["meta"]
    assert plan["meta"]["street_night-selfie_01.jpg"]["tod"] == "night"
    # force_tod：全部照片重分类
    plan2 = bf.plan_album_backfill(files, None, existing, force_tod=True)
    assert set(plan2["need_tod"]) == {
        "cafe_white-dress_01.jpg", "street_night-selfie_01.jpg"}


def test_apply_tod_results_only_day_night():
    meta = {"a.jpg": {"scene": "cafe"}}
    out, n = bf.apply_tod_results(
        meta, {"a.jpg": "day", "b.jpg": "unknown", "c.jpg": "night"})
    assert n == 2
    assert out["a.jpg"]["tod"] == "day" and out["a.jpg"]["scene"] == "cafe"
    assert out["c.jpg"] == {"tod": "night"}
    assert "b.jpg" not in out


def test_tags_with_tod_idempotent_and_db_rows_filter():
    assert bf.tags_with_tod(["curated"], "day") == ["curated", "tod:day"]
    assert bf.tags_with_tod(["curated", "tod:day"], "night") is None  # 已有不覆盖
    assert bf.tags_with_tod(["x"], "unknown") is None
    rows = [
        {"media_type": "photo", "tags": ["scene:cafe"]},
        {"media_type": "photo", "tags": ["tod:day"]},
        {"media_type": "video", "tags": []},
    ]
    todo = bf.db_rows_needing_tod(rows)
    assert len(todo) == 1 and todo[0]["tags"] == ["scene:cafe"]
