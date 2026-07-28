"""相册场景缺口「自动补货」门禁（P2 图文一致性，2026-07-27）。

不变量：
- 配置**默认关**（新子系统约定）；一致性总闸 ``consistency.enabled=false`` 一并关；
- ``qualify_restock_targets`` 只选「反复要不到（unmet ≥ 阈）+ 该人设该场景零备货」，
  共享池有货不补（报告侧已滤）；单人设布局（只有根相册）补根册；
- 计划文件：同 (人设,场景) pending 去重、逐项 done 标记、坏文件容错空计划；
- 策展命名 ``<scene>_<series>_<nn>``：接续现有最大序号，扫不出按 01；
- ``_meta.json`` 登记 merge 保留现有条目；
- 渲染入册端到端：体检软放行（无 vision 配置）下按 count 出图、文件名/元数据
  齐备；album 后端拒补（自我复制无意义）；
- watchdog：每小时节流 + 每日预算 + pending 去重；默认关静默。
"""
from __future__ import annotations

import json

import pytest

from src.companion import media_restock as mr


# ── 配置解析 ────────────────────────────────────────────────────────────────
def test_resolve_restock_cfg_defaults_and_kill():
    off = mr.resolve_restock_cfg({})
    assert off["enabled"] is False          # 默认关
    on = mr.resolve_restock_cfg(
        {"consistency": {"auto_restock": {"enabled": True}}})
    assert on["enabled"] is True
    assert on["min_unmet"] == 2 and on["per_scene"] == 2 and on["max_per_day"] == 6
    # 一致性总闸关 → 一并关
    killed = mr.resolve_restock_cfg(
        {"consistency": {"enabled": False,
                         "auto_restock": {"enabled": True}}})
    assert killed["enabled"] is False


# ── 目标选取 ────────────────────────────────────────────────────────────────
def test_qualify_targets_missing_personas_and_threshold():
    rows = [
        {"scene": "beach", "demand": 6, "unmet": 4,
         "missing_personas": ["lin", "zhao"], "supply": 0},
        {"scene": "gym", "demand": 3, "unmet": 1,      # 未达阈值 → 不补
         "missing_personas": ["lin"], "supply": 0},
        {"scene": "cafe", "demand": 5, "unmet": 3,
         "missing_personas": [], "supply": 4},          # 有货 → 不补
    ]
    supply = {"lin": {"home": 3}, "zhao": {"cafe": 4}}
    got = mr.qualify_restock_targets(rows, supply, min_unmet=2, max_targets=6)
    assert got == [("lin", "beach"), ("zhao", "beach")]
    # max_targets 截断
    assert mr.qualify_restock_targets(rows, supply, min_unmet=2, max_targets=1) \
        == [("lin", "beach")]


def test_qualify_targets_single_persona_layout():
    # 供给只有根目录（无人设分册）：全局零库存场景 → 补根册（"" persona）
    rows = [{"scene": "beach", "demand": 4, "unmet": 3,
             "missing_personas": [], "supply": 0}]
    got = mr.qualify_restock_targets(rows, {"": {"home": 5}}, min_unmet=2)
    assert got == [("", "beach")]
    # 多人设布局下 missing 为空（各家都有）→ 不补
    got2 = mr.qualify_restock_targets(
        rows, {"lin": {"beach": 1}}, min_unmet=2)
    assert got2 == []


def test_qualify_targets_skips_unrenderable_scenes():
    # other/unknown 归并桶没有生图短语 → 不入补货（补出来是无意义图）
    rows = [
        {"scene": "other", "demand": 9, "unmet": 9,
         "missing_personas": ["lin"], "supply": 0},
        {"scene": "unknown", "demand": 5, "unmet": 5,
         "missing_personas": ["lin"], "supply": 0},
        {"scene": "gym", "demand": 3, "unmet": 3,
         "missing_personas": ["lin"], "supply": 0},
    ]
    got = mr.qualify_restock_targets(rows, {"lin": {"home": 2}}, min_unmet=2)
    assert got == [("lin", "gym")]
    assert "beach" in mr.RENDERABLE_SCENES and "other" not in mr.RENDERABLE_SCENES


# ── 计划文件 ────────────────────────────────────────────────────────────────
def test_plan_roundtrip_add_dedup_done(tmp_path):
    p = tmp_path / "plan.json"
    plan = mr.load_plan(p)                    # 无文件 → 空计划
    assert plan == {"items": []}
    assert mr.plan_add(plan, "lin", "beach", 2, now=1_700_000_000.0)
    assert not mr.plan_add(plan, "lin", "beach", 2)    # pending 去重
    assert mr.plan_add(plan, "", "gym", 1)             # 根册项可加
    assert not mr.plan_add(plan, "lin", "", 1)         # 无场景拒收
    assert mr.save_plan(p, plan)
    plan2 = mr.load_plan(p)
    todo = mr.pending_items(plan2)
    assert [(t["persona_id"], t["scene"]) for t in todo] == \
        [("lin", "beach"), ("", "gym")]
    mr.mark_item_done(todo[0], 2, now=1_700_000_100.0)
    assert todo[0]["status"] == "done" and todo[0]["rendered"] == 2
    # done 后同键可再入队（下一轮缺口仍在就再补）
    assert mr.plan_add(plan2, "lin", "beach", 2)
    # 坏文件容错
    p2 = tmp_path / "bad.json"
    p2.write_text("{broken", encoding="utf-8")
    assert mr.load_plan(p2) == {"items": []}


# ── 策展命名 / 元数据 ───────────────────────────────────────────────────────
def test_next_curated_name_numbering(tmp_path):
    d = tmp_path / "lin"
    assert mr.next_curated_name(d, "beach", "white-dress") == \
        "beach_white-dress_01.jpg"            # 目录不存在 → 01
    d.mkdir()
    (d / "beach_white-dress_01.jpg").write_bytes(b"x")
    (d / "beach_white-dress_07.png").write_bytes(b"x")
    (d / "beach_other-series_03.jpg").write_bytes(b"x")
    assert mr.next_curated_name(d, "beach", "white-dress") == \
        "beach_white-dress_08.jpg"            # 接续同系列最大序号
    assert mr.next_curated_name(d, "beach", "white dress", ext=".png") == \
        "beach_white-dress_08.png"            # 系列短语规整成 slug
    assert mr.next_curated_name(d, "gym", "auto") == "gym_auto_01.jpg"


def test_write_album_meta_entry_merge(tmp_path):
    d = tmp_path / "lin"
    d.mkdir()
    (d / "_meta.json").write_text(
        json.dumps({"old.jpg": {"scene": "cafe"}}), encoding="utf-8")
    assert mr.write_album_meta_entry(
        d, "beach_x_01.jpg", scene="Beach", tod="day", series="x")
    data = json.loads((d / "_meta.json").read_text(encoding="utf-8"))
    assert data["old.jpg"] == {"scene": "cafe"}          # 旧条目保留
    assert data["beach_x_01.jpg"] == \
        {"scene": "beach", "tod": "day", "series": "x"}  # 规整小写


def test_scene_render_phrase_day_night():
    p = mr.scene_render_phrase("beach")
    assert "beach" in p and "daylight" in p       # 白天光照显式（凌晨跑也不带夜色）
    n = mr.scene_render_phrase("night_city")
    assert "night" in n and "daylight" not in n
    assert mr.restock_tod_for_scene("night_city") == "night"
    assert mr.restock_tod_for_scene("beach") == "day"
    # 词表外场景类退化为词面短语
    assert "rooftop" in mr.scene_render_phrase("rooftop")


# ── 渲染入册（端到端，假 provider + 体检软放行）────────────────────────────
class _FakeGenProvider:
    backend = "command"

    def __init__(self, src_dir):
        self.src_dir = src_dir
        self.calls = 0

    def reference_image(self, key):
        return ""

    async def generate(self, prompt, *, seed=-1, **kw):
        from src.ai.companion_selfie import SelfieResult
        self.calls += 1
        f = self.src_dir / f"gen_{self.calls}.jpg"
        f.write_bytes(b"\xff\xd8\xff fakejpg" + bytes([self.calls]))
        return SelfieResult(ok=True, image_path=str(f), provider="command")


async def test_render_restock_item_end_to_end(tmp_path):
    album = tmp_path / "albums"
    scfg = {"enabled": True, "provider": {"album_dir": str(album)}}
    prov = _FakeGenProvider(tmp_path)
    item = {"persona_id": "lin", "scene": "beach", "count": 2}
    rv = await mr.render_restock_item(item, scfg, {}, provider=prov)
    assert rv["ok"] and rv["rendered"] == 2 and prov.calls == 2
    files = sorted((album / "lin").glob("beach_*_*.jpg"))
    assert len(files) == 2
    assert files[0].name.endswith("_01.jpg") and files[1].name.endswith("_02.jpg")
    meta = json.loads((album / "lin" / "_meta.json").read_text(encoding="utf-8"))
    for f in files:
        assert meta[f.name]["scene"] == "beach"
        assert meta[f.name]["tod"] == "day"
        assert meta[f.name].get("series")     # 系列=衣着 slug（P2-1 场景联动）


async def test_render_restock_item_threads_gate_expectations(tmp_path, monkeypatch):
    """P2-2 接线：补货渲染带 场景后验 + 目标 tod 代表小时（凌晨跑白天备货，
    期望时段必须是 day 而非「现在几点」）；且禁相册兜底。"""
    import src.ai.image_gate as ig
    seen = {}
    real = ig.generate_with_gate

    async def spy(provider, prompt, **kw):
        seen.update({k: kw.get(k) for k in
                     ("expect_scene", "expect_hour", "allow_album_fallback")})
        return await real(provider, prompt, **kw)

    monkeypatch.setattr(ig, "generate_with_gate", spy)
    album = tmp_path / "albums"
    scfg = {"enabled": True, "provider": {"album_dir": str(album)}}
    rv = await mr.render_restock_item(
        {"persona_id": "lin", "scene": "beach", "count": 1}, scfg, {},
        provider=_FakeGenProvider(tmp_path))
    assert rv["ok"]
    assert seen["expect_scene"] == "beach"
    assert ig.expected_tod(seen["expect_hour"]) == "day"
    assert seen["allow_album_fallback"] is False
    # 夜景场景 → 期望时段 night
    rv2 = await mr.render_restock_item(
        {"persona_id": "lin", "scene": "night_city", "count": 1}, scfg, {},
        provider=_FakeGenProvider(tmp_path))
    assert rv2["ok"] and ig.expected_tod(seen["expect_hour"]) == "night"


async def test_render_restock_item_rejects_album_backend(tmp_path):
    class _AlbumProv:
        backend = "album"

    scfg = {"enabled": True, "provider": {"album_dir": str(tmp_path)}}
    rv = await mr.render_restock_item(
        {"persona_id": "lin", "scene": "beach", "count": 1},
        scfg, {}, provider=_AlbumProv())
    assert not rv["ok"] and "no_generation_backend" in rv["reason"]
    # 无 album_dir → 拒（没地方入册）
    rv2 = await mr.render_restock_item(
        {"persona_id": "lin", "scene": "beach"}, {"provider": {}}, {},
        provider=_FakeGenProvider(tmp_path))
    assert not rv2["ok"] and rv2["reason"] == "no_album_dir"


# ── watchdog 接线：节流 / 预算 / 去重 / 默认关 ─────────────────────────────
def _watchdog(cfg):
    from src.inbox.health_watchdog import HealthWatchdog

    class _CM:
        config = cfg

    class _App:
        class state:
            pass

    return HealthWatchdog(app=_App(), config_manager=_CM())


def test_watchdog_media_restock_plan(tmp_path, monkeypatch):
    plan_path = tmp_path / "plan.json"
    cfg = {"companion": {"selfie": {
        "enabled": True,
        "consistency": {"auto_restock": {
            "enabled": True, "min_unmet": 2, "per_scene": 2,
            "max_per_day": 3, "plan_path": str(plan_path)}}}}}
    wd = _watchdog(cfg)
    monkeypatch.setattr(
        "src.companion.media_gap.collect_scene_supply",
        lambda scfg, **kw: {"lin": {"home": 3}})
    monkeypatch.setattr(
        "src.inbox.image_autosend.metrics_snapshot",
        lambda: {"scene_demand": {"beach": 5}, "scene_unmet": {"beach": 4}})
    t0 = 1_700_000_000.0
    wd._check_media_restock(now=t0)
    plan = mr.load_plan(plan_path)
    todo = mr.pending_items(plan)
    assert [(t["persona_id"], t["scene"]) for t in todo] == [("lin", "beach")]
    assert wd.total_media_restock_planned == 1
    # <1h 节流 → 不跑
    wd._check_media_restock(now=t0 + 600)
    assert wd.total_media_restock_planned == 1
    # ≥1h 再跑：同键 pending 去重 → 不重复入队
    wd._check_media_restock(now=t0 + 3700)
    assert wd.total_media_restock_planned == 1
    assert len(mr.pending_items(mr.load_plan(plan_path))) == 1


def test_watchdog_media_restock_disabled_by_default(tmp_path):
    wd = _watchdog({"companion": {"selfie": {"enabled": True}}})
    wd._check_media_restock(now=1_700_000_000.0)
    assert wd.total_media_restock_planned == 0
    # selfie 未启用同样静默
    wd2 = _watchdog({"companion": {"selfie": {
        "enabled": False,
        "consistency": {"auto_restock": {"enabled": True}}}}})
    wd2._check_media_restock(now=1_700_000_000.0)
    assert wd2.total_media_restock_planned == 0
