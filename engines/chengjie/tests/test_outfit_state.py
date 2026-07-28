"""「今日衣着」状态门禁（P1 图文一致性，2026-07-27）。

不变量：
- 系列 slug ↔ 衣着短语互转只认**服装词**（night-selfie/auto-* 场景系列绝不当
  衣服穿；token 级匹配防 rooftop→top 误命中）；
- 「今天穿什么」按 persona+日期确定性取值（跨请求/跨 A、B 链同一天恒定，
  明天自动换，不同 persona 各异）；
- 连续窗内刚发过的衣着系列（照片事实）**覆盖**每日默认——「再拍一张」不换装；
- 衣橱池优先级 persona 配置 → config 池 → 相册衣橱 → 内置兜底，首个非空层胜出；
- 相册衣橱扫描尊重跨人设隔离（多人设布局下根目录不兜底）；
- 总闸 ``consistency.enabled=false`` / ``consistency.outfit=false`` 一关全关
  （返回空衣着=生图 prompt 不注入=旧行为）。
"""
import datetime as dt

import pytest

from src.companion import outfit_state as ost


@pytest.fixture(autouse=True)
def _reset():
    from src.companion.persona_media_store import (
        configure_persona_media_store, reset_persona_media_store)
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    ost.invalidate_wardrobe_cache()
    yield
    reset_persona_media_store()
    ost.invalidate_wardrobe_cache()


# ── 系列 ↔ 衣着短语互转 ─────────────────────────────────────────────────────
def test_series_outfit_phrase_garment_only():
    assert ost.series_outfit_phrase("white-dress") == "white dress"
    assert ost.series_outfit_phrase("pink_hoodie") == "pink hoodie"
    assert ost.series_outfit_phrase("beige-knit-sweater") == "beige knit sweater"
    # 场景系列 / 非策展名不是衣服
    assert ost.series_outfit_phrase("night-selfie") == ""
    assert ost.series_outfit_phrase("auto-beach") == ""
    assert ost.series_outfit_phrase("") == ""


def test_series_outfit_phrase_token_boundary():
    # rooftop 含子串 "top" 但 token 不是服装词 → 不误判
    assert ost.series_outfit_phrase("rooftop") == ""
    assert ost.series_outfit_phrase("crop-top") == "crop top"


def test_outfit_slug_round_trip():
    assert ost.outfit_slug("white dress") == "white-dress"
    assert ost.outfit_slug("") == ""
    for phrase in ("white dress", "beige knit sweater", "denim jacket"):
        assert ost.series_outfit_phrase(ost.outfit_slug(phrase)) == phrase


# ── 衣橱池优先级 ────────────────────────────────────────────────────────────
def test_outfit_pool_priority_first_nonempty_wins():
    persona = {"outfits": ["red qipao", "  ", "white dress"]}
    assert ost.outfit_pool(persona, ["cfg outfit"], ["album outfit"]) == \
        ["red qipao", "white dress"]
    assert ost.outfit_pool({}, ["cfg outfit"], ["album outfit"]) == ["cfg outfit"]
    assert ost.outfit_pool({}, None, ["album outfit"]) == ["album outfit"]
    pool = ost.outfit_pool({}, None, None)
    assert pool and all(isinstance(x, str) and x for x in pool)  # 内置兜底非空


# ── 今日确定性取值 ──────────────────────────────────────────────────────────
def test_resolve_today_outfit_deterministic_and_daily():
    pool = ["a", "b", "c", "d", "e"]
    d1 = dt.datetime(2026, 7, 27, 9, 0)
    d1b = dt.datetime(2026, 7, 27, 23, 0)   # 同日不同时刻
    d2 = dt.datetime(2026, 7, 28, 9, 0)
    assert ost.resolve_today_outfit("lin", pool, now=d1) == \
        ost.resolve_today_outfit("lin", pool, now=d1b)
    assert ost.resolve_today_outfit("lin", pool, now=d1) in pool
    # 跨多天必有变化（不会天天同一件）
    days = {ost.resolve_today_outfit(
        "lin", pool, now=d1 + dt.timedelta(days=i)) for i in range(10)}
    assert len(days) > 1
    # 不同 persona 在足够多天里分布不同
    diff = [ost.resolve_today_outfit("lin", pool, now=d1 + dt.timedelta(days=i))
            != ost.resolve_today_outfit("zhao", pool, now=d1 + dt.timedelta(days=i))
            for i in range(10)]
    assert any(diff)
    assert ost.resolve_today_outfit("lin", [], now=d1) == ""
    assert ost.resolve_today_outfit("lin", None, now=d2) == ""


# ── 配置解析 ────────────────────────────────────────────────────────────────
def test_resolve_outfit_cfg_defaults_and_kill_switches():
    assert ost.resolve_outfit_cfg({})["enabled"] is True          # 默认开
    assert ost.resolve_outfit_cfg(None)["enabled"] is True
    # 一致性总闸关 → 一并关
    off = ost.resolve_outfit_cfg({"consistency": {"enabled": False}})
    assert off["enabled"] is False
    # outfit 布尔关
    off2 = ost.resolve_outfit_cfg({"consistency": {"outfit": False}})
    assert off2["enabled"] is False
    # dict 形式带池
    c = ost.resolve_outfit_cfg(
        {"consistency": {"outfit": {"enabled": True, "pool": ["red dress"]}}})
    assert c["enabled"] is True and list(c["pool"]) == ["red dress"]


# ── current_outfit 单一入口 ─────────────────────────────────────────────────
def test_current_outfit_continuity_beats_daily():
    persona = {"id": "lin", "outfits": ["white dress", "gray hoodie"]}
    out = ost.current_outfit(persona, {}, persona_key="lin",
                             recent_series="pink-dress")
    assert out == {"outfit": "pink dress", "source": "continuity"}
    # 连续系列不是衣着（场景系列）→ 回落今日衣着
    out2 = ost.current_outfit(persona, {}, persona_key="lin",
                              recent_series="night-selfie")
    assert out2["source"] == "daily" and out2["outfit"] in persona["outfits"]


def test_current_outfit_disabled_returns_empty():
    persona = {"id": "lin", "outfits": ["white dress"]}
    out = ost.current_outfit(
        persona, {"consistency": {"outfit": False}}, persona_key="lin")
    assert out == {"outfit": "", "source": "disabled"}
    # 关掉时连续性也不注入
    out2 = ost.current_outfit(
        persona, {"consistency": {"enabled": False}},
        persona_key="lin", recent_series="pink-dress")
    assert out2["outfit"] == ""


def test_current_outfit_same_day_stable_across_chains():
    """A/B 两链同参调用（同 persona_key 同日）必须拿到同一件衣服。"""
    persona = {"id": "lin", "outfits": ["white dress", "gray hoodie", "denim jacket"]}
    now = dt.datetime(2026, 7, 27, 10, 0)
    a = ost.current_outfit(persona, {}, persona_key="lin", now=now)
    b = ost.current_outfit(persona, {}, persona_key="lin",
                           now=now.replace(hour=22))
    assert a == b and a["source"] == "daily"


# ── 相册衣橱（FS + DB）─────────────────────────────────────────────────────
def test_wardrobe_for_collects_garment_series_only(tmp_path):
    d = tmp_path / "albums" / "lin"
    d.mkdir(parents=True)
    for f in ("cafe_white-dress_01.jpg", "street_night-selfie_01.jpg",
              "home_gray-hoodie_02.png"):
        (d / f).write_bytes(b"\x89PNGdummy")
    scfg = {"provider": {"album_dir": str(tmp_path / "albums")}}
    ward = ost.wardrobe_for("lin", scfg)
    assert "white dress" in ward and "gray hoodie" in ward
    assert all("selfie" not in w for w in ward)  # 场景系列不进衣橱


def test_wardrobe_for_registry_rows(tmp_path):
    from src.companion.persona_media_store import get_persona_media_store
    st = get_persona_media_store()
    st.add("lin", "photo", "f1.jpg", "", tags=["series:pink-dress"])
    st.add("lin", "photo", "f2.jpg", "", tags=["series:night-selfie"])
    ost.invalidate_wardrobe_cache()
    ward = ost.wardrobe_for("lin", {"provider": {"album_dir": str(tmp_path)}})
    assert ward == ["pink dress"]


def test_wardrobe_isolation_multi_persona_root_not_borrowed(tmp_path):
    root = tmp_path / "albums"
    other = root / "other"
    other.mkdir(parents=True)
    (other / "cafe_red-dress_01.jpg").write_bytes(b"\x89PNGdummy")
    (root / "home_blue-shirt_01.jpg").write_bytes(b"\x89PNGdummy")
    scfg = {"provider": {"album_dir": str(root)}}
    # ghost 人设无分册 + 多人设布局 → 根目录素材不外借
    assert ost.wardrobe_for("ghost", scfg) == []
    ost.invalidate_wardrobe_cache()
    # 单人设平铺布局（无分册）→ 根目录照常入橱
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "home_blue-shirt_01.jpg").write_bytes(b"\x89PNGdummy")
    ward = ost.wardrobe_for("solo", {"provider": {"album_dir": str(flat)}})
    assert ward == ["blue shirt"]


def test_outfit_chat_line():
    assert ost.outfit_chat_line("") == ""
    line = ost.outfit_chat_line("white dress")
    assert "white dress" in line and "穿" in line


# ── P2 场景×衣着联动 + 季节 ─────────────────────────────────────────────────
_SUMMER = dt.datetime(2026, 7, 27, 10, 0)
_WINTER = dt.datetime(2026, 1, 15, 10, 0)
_SPRING = dt.datetime(2026, 4, 10, 10, 0)


def test_outfit_categories_tokens():
    assert "swim" in ost.outfit_categories("white bikini")
    assert "sleep" in ost.outfit_categories("silk pajamas")
    assert "sport" in ost.outfit_categories("black leggings and sports bra")
    assert "work" in ost.outfit_categories("navy blazer")
    assert "dressy" in ost.outfit_categories("white dress")
    cats = ost.outfit_categories("denim jacket over a white tee")
    assert "outer" in cats
    assert ost.outfit_categories("") == frozenset()
    assert ost.outfit_categories("something plain") == frozenset()


def test_season_of_months_and_hemisphere():
    assert ost.season_of(_SUMMER) == "summer"
    assert ost.season_of(_WINTER) == "winter"
    assert ost.season_of(_SPRING) == "spring"
    assert ost.season_of(dt.datetime(2026, 10, 1)) == "autumn"
    # 南半球倒扣：7 月=冬
    assert ost.season_of(_SUMMER, hemisphere="south") == "winter"


def test_outfit_scene_conflict_rules():
    # 泳装进咖啡馆/健身房穿睡衣/办公室穿泳装 → 高置信违和
    assert ost.outfit_scene_conflict("white swimsuit", "cafe", now=_SUMMER)
    assert ost.outfit_scene_conflict("silk pajamas", "gym", now=_SUMMER)
    assert ost.outfit_scene_conflict("white bikini", "office", now=_SUMMER)
    # 海边不该裹毛衣（outer 排除）
    assert ost.outfit_scene_conflict("beige knit sweater", "beach", now=_SUMMER)
    # 便装进咖啡馆 / 未知场景类 → 不违和
    assert not ost.outfit_scene_conflict("white t-shirt and jeans", "cafe", now=_SPRING)
    assert not ost.outfit_scene_conflict("white dress", "", now=_SPRING)
    # 睡衣在家/卧室合理
    assert not ost.outfit_scene_conflict("silk pajamas", "bedroom", now=_SPRING)


def test_outfit_season_conflict():
    # 冬天泳装（任何场景）/冬天户外短袖 → 违和；室内吊带不管
    assert ost.outfit_scene_conflict("white swimsuit", "beach", now=_WINTER)
    assert ost.outfit_scene_conflict("tank top and shorts", "street", now=_WINTER)
    assert not ost.outfit_scene_conflict("tank top and shorts", "home", now=_WINTER)
    # 夏天重外套 → 违和；季节开关关掉 → 放行
    assert ost.outfit_scene_conflict("long wool coat", "street", now=_SUMMER)
    assert not ost.outfit_scene_conflict(
        "long wool coat", "street", now=_SUMMER, season_on=False)
    # 春秋不加季节约束
    assert not ost.outfit_scene_conflict("beige knit sweater", "street", now=_SPRING)


def test_adapt_outfit_prefer_tier_switches():
    pool = ["white t-shirt and jeans", "black leggings and sportswear",
            "white dress"]
    # gym 有运动装 → 换（换装是真实行为）
    o, tag = ost.adapt_outfit_to_scene(
        "white t-shirt and jeans", pool, "gym", "lin", now=_SPRING)
    assert tag == "switched" and "sport" in ost.outfit_categories(o)
    # beach：日常 hoodie（outer 违和）→ 换裙装/轻装
    o2, tag2 = ost.adapt_outfit_to_scene(
        "light gray hoodie", pool, "beach", "lin", now=_SUMMER)
    assert tag2 == "switched" and o2 == "white dress"
    # 已是优先类 → 不动（同场景重复调用稳定）
    o3, tag3 = ost.adapt_outfit_to_scene(o, pool, "gym", "lin", now=_SPRING)
    assert (o3, tag3) == (o, "")


def test_adapt_outfit_keep_and_veto():
    # 无优先货可换但当前不违和 → 保持（一天一套是主旋律）
    o, tag = ost.adapt_outfit_to_scene(
        "white t-shirt and jeans", ["white t-shirt and jeans"], "cafe",
        "lin", now=_SPRING)
    assert (o, tag) == ("white t-shirt and jeans", "")
    # 违和且全池违和 → 放弃注入（生图模型按场景自由穿，比硬注入诚实）
    o2, tag2 = ost.adapt_outfit_to_scene(
        "white swimsuit", ["white swimsuit"], "office", "lin", now=_SUMMER)
    assert (o2, tag2) == ("", "vetoed")
    # 空衣着不适配
    assert ost.adapt_outfit_to_scene("", ["a"], "gym", "lin") == ("", "")


def test_adapt_outfit_deterministic_same_day():
    pool = ["black leggings and sportswear", "gray tracksuit",
            "white t-shirt and jeans"]
    picks = {ost.adapt_outfit_to_scene(
        "white dress", pool, "gym", "lin", now=_SPRING)[0] for _ in range(5)}
    assert len(picks) == 1   # 同 key 同日同场景恒定


def test_current_outfit_scene_adaptation_integration():
    persona = {"id": "lin",
               "outfits": ["light gray hoodie", "white dress"]}
    # 海边场景：今日若为 hoodie（outer 违和）→ 场景适配换 white dress
    out = ost.current_outfit(
        persona, {}, persona_key="lin",
        scene="walking on the beach at sunset", now=_SUMMER)
    assert out["outfit"] == "white dress"
    # 连续性衣着与场景冲突同样被适配（照片事实也不能泳装进办公室）
    out2 = ost.current_outfit(
        persona, {}, persona_key="lin", recent_series="white-bikini",
        scene="at the office", now=_SUMMER)
    assert out2["outfit"] != "white bikini"
    # 场景不冲突时连续性照旧优先
    out3 = ost.current_outfit(
        persona, {}, persona_key="lin", recent_series="pink-dress",
        scene="sitting in a cozy cafe", now=_SPRING)
    assert out3 == {"outfit": "pink dress", "source": "continuity"}
    # 不传场景 = 只有季节约束（行为兼容 P1）
    out4 = ost.current_outfit(persona, {}, persona_key="lin", now=_SPRING)
    assert out4["source"] in ("daily", "scene") and out4["outfit"]


def test_resolve_outfit_cfg_season_keys():
    c = ost.resolve_outfit_cfg({})
    assert c["season"] is True and c["hemisphere"] == "north"
    c2 = ost.resolve_outfit_cfg(
        {"consistency": {"outfit": {"season": False, "hemisphere": "south"}}})
    assert c2["season"] is False and c2["hemisphere"] == "south"
