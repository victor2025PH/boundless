"""相册场景供需缺口报告门禁（P1 图文一致性，2026-07-27）。

不变量：
- 需求侧计数（``record_scene_request``）按归一场景类累计 demand/unmet，
  认不出的场景归 "other"（不丢数）；
- 投诉分维计数（``record_media_complaint(kind=,persona_id=)``）按类型/人设
  分布，与总数同步；
- 供给扫描合并 注册相册(DB) + 文件系统相册（meta sidecar 的 scene 标注优先，
  回落策展文件名前缀），按 persona 分桶，根目录平铺记 "" 共享池；
- 纯函数报告：按 未兑现↓→需求↓ 排序、共享池有货不算缺、缺货人设=有库存但该
  场景 0 张的人设、top_n 截断、空输入零崩溃。
"""
import pytest

from src.companion import media_gap as mg
from src.inbox import image_autosend as ia


@pytest.fixture(autouse=True)
def _reset():
    from src.companion.persona_media_store import (
        configure_persona_media_store, reset_persona_media_store)
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    mg.invalidate_supply_cache()
    yield
    reset_persona_media_store()
    mg.invalidate_supply_cache()


# ── 需求侧计数 ──────────────────────────────────────────────────────────────
def test_record_scene_request_demand_and_unmet():
    before = ia.metrics_snapshot()
    d0 = int((before.get("scene_demand") or {}).get("beach", 0) or 0)
    u0 = int((before.get("scene_unmet") or {}).get("beach", 0) or 0)
    ia.record_scene_request("walking on the beach", unmet=False)
    ia.record_scene_request("海边", unmet=True)
    snap = ia.metrics_snapshot()
    assert snap["scene_demand"]["beach"] == d0 + 2
    assert snap["scene_unmet"]["beach"] == u0 + 1


def test_record_scene_request_unknown_bucketed_as_other():
    before = int((ia.metrics_snapshot().get("scene_demand") or {})
                 .get("other", 0) or 0)
    ia.record_scene_request("somewhere-weird", unmet=False)
    snap = ia.metrics_snapshot()
    assert snap["scene_demand"]["other"] == before + 1


def test_record_media_complaint_by_kind_and_persona():
    before = ia.metrics_snapshot()
    t0 = int(before.get("complaints", 0) or 0)
    k0 = int((before.get("complaints_by_kind") or {}).get("fake", 0) or 0)
    p0 = int((before.get("complaints_by_persona") or {}).get("lin", 0) or 0)
    ia.record_media_complaint("fake:这是网图吧", kind="fake", persona_id="lin")
    ia.record_media_complaint("repeat:怎么又是这张")   # kind 从前缀解析
    snap = ia.metrics_snapshot()
    assert snap["complaints"] == t0 + 2
    assert snap["complaints_by_kind"]["fake"] == k0 + 1
    assert snap["complaints_by_kind"]["repeat"] >= 1
    assert snap["complaints_by_persona"]["lin"] == p0 + 1


# ── 供给扫描 ────────────────────────────────────────────────────────────────
def test_collect_scene_supply_fs_meta_priority_and_root(tmp_path):
    root = tmp_path / "albums"
    lin = root / "lin"
    lin.mkdir(parents=True)
    (lin / "cafe_white-dress_01.jpg").write_bytes(b"\x89PNGdummy")
    (lin / "beach_bikini_01.jpg").write_bytes(b"\x89PNGdummy")
    # sidecar 把文件名前缀 cafe 显式改判 gym → meta 优先
    (lin / "_meta.json").write_text(
        '{"cafe_white-dress_01.jpg": {"scene": "gym"}}', encoding="utf-8")
    (root / "street_gray-hoodie_01.jpg").write_bytes(b"\x89PNGdummy")
    (root / "IMG_random.jpg").write_bytes(b"\x89PNGdummy")   # 非策展名
    supply = mg.collect_scene_supply(
        {"provider": {"album_dir": str(root)}}, force=True)
    assert supply["lin"]["gym"] == 1 and supply["lin"]["beach"] == 1
    assert "cafe" not in supply["lin"]
    assert supply[""]["street"] == 1     # 根目录平铺 → 共享池
    assert supply[""]["unknown"] == 1    # 认不出的按 unknown 盘点（不丢数）


def test_collect_scene_supply_registry_enabled_only(tmp_path):
    from src.companion.persona_media_store import get_persona_media_store
    st = get_persona_media_store()
    st.add("lin", "photo", "a.jpg", "", tags=["scene:beach"])
    r = st.add("lin", "photo", "b.jpg", "", tags=["scene:beach"])
    st.update(r["id"], enabled=False)
    st.add("lin", "photo", "c.jpg", "", tags=[])   # 场景未知
    supply = mg.collect_scene_supply(
        {"provider": {"album_dir": str(tmp_path / "none")}}, force=True)
    assert supply["lin"]["beach"] == 1          # 停用条目不算库存
    assert supply["lin"]["unknown"] == 1


# ── 纯函数报告 ──────────────────────────────────────────────────────────────
def test_scene_gap_report_sorting_missing_and_rate():
    supply = {"lin": {"cafe": 3}, "zhao": {"beach": 2, "cafe": 1}, "": {}}
    demand = {"beach": 5, "cafe": 2, "gym": 4}
    unmet = {"beach": 1, "gym": 4}
    rep = mg.scene_gap_report(supply, demand, unmet)
    scenes = [r["scene"] for r in rep["rows"]]
    assert scenes == ["gym", "beach", "cafe"]    # 未兑现↓ → 需求↓
    gym = rep["rows"][0]
    assert gym["supply"] == 0 and gym["unmet_rate"] == 1.0
    assert gym["missing_personas"] == ["lin", "zhao"]
    beach = rep["rows"][1]
    assert beach["supply"] == 2 and beach["missing_personas"] == ["lin"]
    cafe = rep["rows"][2]
    assert cafe["unmet"] == 0 and cafe["missing_personas"] == []
    assert rep["supply_totals"]["lin"] == 3
    assert rep["active"] is True


def test_scene_gap_report_shared_pool_covers_missing():
    supply = {"lin": {"cafe": 1}, "": {"beach": 4}}
    rep = mg.scene_gap_report(supply, {"beach": 3}, {"beach": 1})
    row = rep["rows"][0]
    # 共享池有货 → 不点名缺货人设
    assert row["supply"] == 4 and row["missing_personas"] == []


def test_scene_gap_report_top_n_and_empty_inputs():
    demand = {f"s{i}": 1 for i in range(20)}
    rep = mg.scene_gap_report({}, demand, {}, top_n=5)
    assert len(rep["rows"]) == 5
    empty = mg.scene_gap_report(None, None, None)
    assert empty["rows"] == [] and empty["active"] is False
    # demand=0 且 unmet=0 的场景不出行
    rep2 = mg.scene_gap_report({}, {"beach": 0}, {"beach": 0})
    assert rep2["rows"] == []
