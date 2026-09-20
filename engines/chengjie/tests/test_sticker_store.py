"""贴纸注册表 + 跨平台发送方案门禁（sticker_store）。"""
from src.inbox.sticker_store import (
    COLLECTED_PACK_ID,
    PACK_CUSTOM,
    PACK_OFFICIAL,
    StickerStore,
    resolve_send_plan,
    safe_pack_dir,
    sibling_path,
    sticker_url,
)


def _store() -> StickerStore:
    return StickerStore(":memory:")


def test_pack_crud_and_counts():
    st = _store()
    p = st.create_pack("测试包", kind=PACK_CUSTOM, created_by="t")
    assert p["id"] and p["title"] == "测试包" and p["enabled"] is True
    st.add_sticker(p["id"], file_path="/x/a.webp", url="/static/sticker_packs/x/a.webp")
    packs = st.list_packs()
    assert len(packs) == 1 and packs[0]["count"] == 1
    assert packs[0]["cover_url"].endswith("a.webp"), "无显式封面时回落首张贴纸"
    st.update_pack(p["id"], title="改名", enabled=False)
    assert st.get_pack(p["id"])["title"] == "改名"
    assert st.list_packs() == []  # enabled=False 不在默认清单
    assert len(st.list_packs(include_disabled=True)) == 1


def test_pack_create_idempotent_by_id():
    st = _store()
    st.create_pack("官方", kind=PACK_OFFICIAL, pack_id="off1")
    st.create_pack("官方重复", kind=PACK_OFFICIAL, pack_id="off1")
    assert st.get_pack("off1")["title"] == "官方", "同 id 重复创建必须幂等（保首次）"


def test_delete_pack_returns_file_paths():
    st = _store()
    p = st.create_pack("x")
    st.add_sticker(p["id"], file_path="/tmp/a.webp")
    st.add_sticker(p["id"], file_path="/tmp/b.webp")
    st.add_sticker(p["id"], file_path="")  # LINE 纯 ID 条目无文件
    paths = st.delete_pack(p["id"])
    assert sorted(paths) == ["/tmp/a.webp", "/tmp/b.webp"]
    assert st.get_pack(p["id"]) is None
    assert st.pack_size(p["id"]) == 0


def test_sticker_sha_dedup_and_usage():
    st = _store()
    p = st.create_pack("x")
    r = st.add_sticker(p["id"], file_path="/f.webp", sha256="abc")
    assert st.find_by_sha(p["id"], "abc")["id"] == r["id"]
    assert st.find_by_sha(p["id"], "zzz") is None
    assert st.recent() == []
    st.record_use(r["id"])
    rec = st.recent()
    assert len(rec) == 1 and rec[0]["id"] == r["id"] and rec[0]["hits"] == 1


def test_line_id_sticker_row():
    st = _store()
    p = st.create_pack("line 官方", kind=PACK_OFFICIAL, pack_id="lp")
    r = st.add_sticker(
        "lp", sticker_id="lp-52002734",
        url="https://stickershop.line-scdn.net/stickershop/v1/sticker/52002734/android/sticker.png",
        line_package_id="11537", line_sticker_id="52002734")
    assert r["line_package_id"] == "11537" and not r["file_path"]
    # 幂等：同 sticker_id 再加不覆盖
    st.add_sticker("lp", sticker_id="lp-52002734", line_package_id="99999")
    assert st.get("lp-52002734")["line_package_id"] == "11537"


def test_safe_pack_dir_and_url():
    assert safe_pack_dir("../evil") == "___evil"  # ../ 三字符逐一替换、无路径语义
    assert safe_pack_dir("") == "pack"
    assert sticker_url("p1", "a.webp") == "/static/sticker_packs/p1/a.webp"
    assert sibling_path("/x/y/a.webp", ".png") == "/x/y/a.png"
    assert sibling_path("", ".png") == ""


def test_collected_pack_constant():
    assert COLLECTED_PACK_ID == "collected"


# ── 跨平台发送方案（纯函数矩阵）────────────────────────────────────────────

def _row(animated=False, fp="/x/a.webp"):
    return {"file_path": fp, "animated": animated}


def test_plan_telegram_static_native_sticker():
    p = resolve_send_plan("telegram", _row(), has_gif=False, has_png=True)
    assert p == {"eff_type": "sticker", "variant": "webp",
                 "mirror_type": "sticker", "sent_as": "sticker"}


def test_plan_telegram_animated_prefers_gif_animation():
    p = resolve_send_plan("telegram", _row(animated=True),
                          has_gif=True, has_png=True)
    assert p["eff_type"] == "animation" and p["variant"] == "gif"
    assert p["mirror_type"] == "sticker" and p["sent_as"] == "sticker"


def test_plan_telegram_animated_without_gif_falls_to_static_sticker():
    p = resolve_send_plan("telegram", _row(animated=True),
                          has_gif=False, has_png=True)
    assert p["eff_type"] == "sticker" and p["variant"] == "webp"


def test_plan_whatsapp_native_when_sidecar_capable():
    p = resolve_send_plan("whatsapp", _row(), wa_sticker_capable=True,
                          has_gif=False, has_png=True)
    assert p["eff_type"] == "sticker" and p["sent_as"] == "sticker"


def test_plan_whatsapp_falls_to_image_when_sidecar_old():
    p = resolve_send_plan("whatsapp", _row(), wa_sticker_capable=False,
                          has_gif=False, has_png=True)
    assert p == {"eff_type": "image", "variant": "png",
                 "mirror_type": "image", "sent_as": "image"}


def test_plan_line_and_messenger_image_fallback():
    for plat in ("line", "messenger", "zalo", "instagram"):
        p = resolve_send_plan(plat, _row(), has_gif=False, has_png=True)
        assert p["eff_type"] == "image" and p["sent_as"] == "image", plat


def test_plan_png_sibling_missing_falls_back_to_webp_variant():
    p = resolve_send_plan("messenger", _row(), has_gif=False, has_png=False)
    assert p["variant"] == "webp"
