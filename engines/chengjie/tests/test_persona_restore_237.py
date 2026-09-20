# -*- coding: utf-8 -*-
"""N-2 A（#237，P1）：人设备份恢复「请求失败 _esc is not defined」+ 恢复回执 + 导出格式核（2026-09-07）。

事故（RN2SKR / MJGHKQ，skuio，1.0.76）：09-07 13:14 卸载重装后 ``人设加载完成: config=0 canonical=0
runtime=0``；点「从备份恢复…」选卸载前 13:01 导出的 ``chatx-personas-20260907-1301.json`` → 页面
「请求失败 _esc is not defined」。真因：personas.html 备份恢复预览渲染直接调 ``_esc(...)``——那是
``_exportFilteredCSV()`` **体内**的 CSV 转义函数，全局作用域够不着 → ReferenceError 被 catch 吞成
「请求失败」；dry_run 请求其实已发出，预览永不出现、确认无从点起。L-2 C 的「解析并预览」同批同病。

钉住：
- 空库（runtime=0）拿 1.0.76 导出信封恢复必须能走：预览 add=N / overwrite=0 → 确认 → 全部回来、内容一致；
- 回执字段：``imported`` / ``add`` / ``overwrite`` / ``invalid`` / ``persisted``（前端拼「已恢复 N 个人设
  ：新增 A · 覆盖 B，含相册引用 M 条」）；
- 重名预览：同 ID → overwrite_ids；**同名不同 ID**（手工重建的 Mizuki vs 备份里的）→ ``name_clashes``
  顾问字段，前端列出让用户自己选；
- 坏文件人话：不是 JSON / 缺 profiles 列表 / 后端 400 都有人话键，页面内部错误另有归因文案 + console.error；
- 导出格式契约：信封 ``format/version/count/profiles/album_refs`` 与导入端一致，``album_refs[*].file``
  只是文件名（不含机器路径），整份信封贴回 import 直接可用；
- 前端恢复块只用页面全局 ``_escHtml``，``_esc`` 越界引用由 test_template_undefined_identifiers 的作用域
  探测器长期看守。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from src.utils.persona_manager import PersonaManager

_HDRS = {"Authorization": "Bearer test-token", "Content-Type": "application/json"}
_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates" / "personas.html"

_BACKUP_PROFILES = [
    {"id": "Mizuki", "name": "Mizuki", "role": "日语陪伴", "gender": "female", "tags": ["jp"],
     "voice_profile": {"voice_mode": "preset", "backend": "edge_tts", "voice": "ja-JP-NanamiNeural"}},
    {"id": "STEVEN", "name": "STEVEN", "role": "陪伴", "gender": "male", "background": "海外做生意"},
    {"id": "aiko", "name": "Aiko", "role": "陪伴"},
]


@pytest.fixture
def app_env(tmp_path):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"}, "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "personas": {"profiles": []},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(yaml.dump({"greeting": ["hi"]}), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(yaml.dump({"channels": {}}), encoding="utf-8")
    from src.utils.config_manager import ConfigManager
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cm.load())
    finally:
        loop.close()
    from src.companion.persona_media_store import configure_persona_media_store, reset_persona_media_store
    reset_persona_media_store()
    configure_persona_media_store(tmp_path / "persona_media.db")   # 不把 persona_media.db 懒建到仓库 config/
    PersonaManager.reset()
    from src.web.admin import create_app
    yield create_app(cm), tmp_path
    PersonaManager.reset()
    reset_persona_media_store()


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _seed(profiles):
    pm = PersonaManager.get_instance()
    for p in profiles:
        pm.upsert_profile(p["id"], dict(p), _track_history=False)
    return pm


def _snapshot(pm, pid):
    return json.dumps(pm.get_persona_by_id(pid), sort_keys=True, ensure_ascii=False)


@pytest.mark.asyncio
async def test_empty_library_restores_from_1076_envelope_with_receipt(app_env):
    """卸载重装（runtime=0）后拿 13:01 那种导出信封恢复：预览 → 确认 → 全部回来 + 回执字段齐。"""
    app, _ = app_env
    pm = _seed(_BACKUP_PROFILES)
    before = {p["id"]: _snapshot(pm, p["id"]) for p in _BACKUP_PROFILES}
    async with _client(app) as c:
        r = await c.get("/api/personas/profiles/export", headers=_HDRS)
        assert r.status_code == 200
        envelope = r.json()
        assert envelope["format"] == "chatx-personas-backup" and envelope["version"] == 1
        assert envelope["count"] == 3 and isinstance(envelope["album_refs"], dict)

        # 卸载重装：人设池归零（与 RN2SKR 「config=0 canonical=0 runtime=0」同态）
        PersonaManager.reset()
        pm = PersonaManager.get_instance()
        assert pm.list_profile_ids() == []

        # 整份信封贴回（前端 pbPickRestore 读 parsed.profiles；这里模拟其 dry_run 请求）
        r = await c.post("/api/personas/profiles/import?dry_run=1", headers=_HDRS,
                         json={"profiles": envelope["profiles"], "mode": "merge", "dry_run": True})
        assert r.status_code == 200
        pv = r.json()
        assert pv["ok"] and pv["dry_run"] is True
        assert (pv["add"], pv["overwrite"]) == (3, 0) and pv["overwrite_ids"] == []
        assert sorted(pv["add_ids"]) == ["Mizuki", "STEVEN", "aiko"]
        assert pv["names"]["aiko"] == "Aiko" and pv["invalid"] == [] and pv["name_clashes"] == []
        assert pm.list_profile_ids() == [], "dry_run 不得写库"

        r = await c.post("/api/personas/profiles/import", headers=_HDRS,
                         json={"profiles": envelope["profiles"], "mode": "merge"})
        assert r.status_code == 200
        d = r.json()
        # 回执字段：前端 _pbReceipt 用 imported / add / overwrite / invalid
        assert d["ok"] and d["imported"] == 3 and (d["add"], d["overwrite"]) == (3, 0)
        assert d["invalid"] == [] and d["persisted"] is True and not d["persist_warning"]
        assert sorted(pm.list_profile_ids()) == ["Mizuki", "STEVEN", "aiko"]
        for pid, snap in before.items():
            assert _snapshot(pm, pid) == snap, f"{pid} 恢复后内容不一致"
        assert pm.get_persona_by_id("Mizuki")["voice_profile"]["voice"] == "ja-JP-NanamiNeural"


@pytest.mark.asyncio
async def test_rename_preview_same_id_overwrites_same_name_other_id_advises(app_env):
    """重名预览：手工重建的 STEVEN（同 ID）→ 覆盖；手工重建的 Mizuki 用了别的 ID → name_clashes 顾问，不覆盖。"""
    app, _ = app_env
    # 用户重建：STEVEN 沿用原 ID；Mizuki 手工新建时 ID 变成 mizuki2
    _seed([{"id": "STEVEN", "name": "STEVEN", "role": "重建的"},
           {"id": "mizuki2", "name": " mizuki ", "role": "重建的"}])
    async with _client(app) as c:
        r = await c.post("/api/personas/profiles/import?dry_run=1", headers=_HDRS,
                         json={"profiles": _BACKUP_PROFILES, "mode": "merge"})
        assert r.status_code == 200
        pv = r.json()
        assert (pv["add"], pv["overwrite"]) == (2, 1)
        assert pv["overwrite_ids"] == ["STEVEN"] and sorted(pv["add_ids"]) == ["Mizuki", "aiko"]
        # 同名判定：去空白 + 忽略大小写；同 ID 覆盖项不算重名；aiko 无同名
        assert pv["name_clashes"] == [{"id": "Mizuki", "name": "Mizuki", "existing_id": "mizuki2"}]
        pm = PersonaManager.get_instance()
        assert pm.get_persona_by_id("STEVEN")["role"] == "重建的", "dry_run 不得写库"

        # 确认恢复：STEVEN 被备份版覆盖；mizuki2 与 Mizuki 并存（用户自己删多余的那个）
        r = await c.post("/api/personas/profiles/import", headers=_HDRS,
                         json={"profiles": _BACKUP_PROFILES, "mode": "merge"})
        d = r.json()
        assert d["ok"] and (d["add"], d["overwrite"]) == (2, 1) and d["imported"] == 3
        assert pm.get_persona_by_id("STEVEN")["role"] == "陪伴"
        assert pm.get_persona_by_id("mizuki2") is not None and pm.get_persona_by_id("Mizuki") is not None


@pytest.mark.asyncio
async def test_bad_file_reports_in_plain_words(app_env):
    """坏文件：后端 400 有 detail；前端三种坏法各有人话键，且页面内部错误单独归因 + console.error。"""
    app, _ = app_env
    async with _client(app) as c:
        r = await c.post("/api/personas/profiles/import?dry_run=1", headers=_HDRS,
                         json={"profiles": {"not": "a list"}})
        assert r.status_code == 400 and "profiles" in r.json()["detail"]
        r = await c.post("/api/personas/profiles/import?dry_run=1", headers=_HDRS,
                         json={"profiles": [], "mode": "nuke"})
        assert r.status_code == 400
    from src.web.i18n_packs.persona_studio import EN, ZH
    for key in ("psn_restore_bad_json", "psn_restore_bad_shape", "psn_restore_fail_preview",
                "psn_restore_fail_apply", "psn_restore_internal_err", "psn_restore_name_clash",
                "psn_restore_receipt", "psn_restore_receipt_album", "psn_restore_receipt_skipped"):
        assert ZH.get(key) and EN.get(key), f"{key} 缺 zh/en"
        assert set(re.findall(r"\{(\w+)\}", ZH[key])) == set(re.findall(r"\{(\w+)\}", EN[key])), key
    assert "备份文件没有被改动" in ZH["psn_restore_fail_preview"] and "备份文件没有被改动" in ZH["psn_restore_fail_apply"]
    assert "不是备份文件的问题" in ZH["psn_restore_internal_err"]

    html = _TPL.read_text(encoding="utf-8")
    fail = html[html.index("function _pbFail("):html.index("function _pbAlbumRefCount(")]
    assert "console.error(" in fail and "_feBeacon(" in fail, "失败必须落 renderer.log + 归因遥测"
    assert "instanceof ReferenceError" in fail and "psn_restore_internal_err" in fail
    pick = html[html.index("function pbPickRestore("):html.index("function pbCancelRestore(")]
    assert "psn_restore_bad_json" in pick and "psn_restore_bad_shape" in pick and "_pbFail('preview'" in pick
    confirm = html[html.index("async function pbConfirmRestore("):html.index("window.pbExportOne = pbExportOne")]
    assert "_pbFail('restore'" in confirm and "_pbReceipt(" in confirm
    assert "pbCancelRestore();" in confirm.split("catch")[0], "成功才清预览；失败保留预览可重试"


@pytest.mark.asyncio
async def test_export_envelope_contract_matches_import_and_has_no_machine_paths(app_env):
    """导出格式核：信封键齐、album_refs 只带文件名（换机可用）、整份信封贴回 import 直接可用。"""
    app, tmp_path = app_env
    _seed(_BACKUP_PROFILES)
    from src.companion.persona_media_store import get_persona_media_store
    st = get_persona_media_store()
    assert st is not None
    photo = tmp_path / "persona_albums" / "Mizuki" / "photo" / "a0934fac.jpg"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"\xff\xd8\xff")
    st.add("Mizuki", "photo", str(photo), "/media/persona/Mizuki/a0934fac.jpg", triggers=["咖啡"], caption="喝咖啡")
    async with _client(app) as c:
        r = await c.get("/api/personas/profiles/export", headers=_HDRS)
        env = r.json()
        assert set(env) >= {"format", "version", "exported_at", "count", "profiles", "album_refs"}
        refs = env["album_refs"]["Mizuki"]
        assert len(refs) == 1 and refs[0]["file"] == "a0934fac.jpg" and refs[0]["caption"] == "喝咖啡"
        assert "\\" not in refs[0]["file"] and "/" not in refs[0]["file"], "相册引用不得带机器路径"
        assert "_mrpa_source" not in json.dumps(env)
        # 整份信封（含 format/version/album_refs 顶层键）贴回 import：只读 profiles 键
        PersonaManager.reset()
        r = await c.post("/api/personas/profiles/import?dry_run=1", headers=_HDRS, json=env)
        assert r.status_code == 200 and r.json()["add"] == 3

    # 前端 pbPickRestore 接受「数组」或「信封.profiles」两种形态，与 _pbDownload 落盘的信封一致
    html = _TPL.read_text(encoding="utf-8")
    pick = html[html.index("function pbPickRestore("):html.index("function pbCancelRestore(")]
    assert "Array.isArray(parsed.profiles)" in pick and "_pbAlbumRefCount(parsed)" in pick
    assert "'chatx-personas-' + _pbStamp() + '.json'" in html


def test_restore_block_uses_page_global_escape_only():
    """#237 直接钉：备份恢复块 / JSON 导入预览块只用 _escHtml；_esc 只在 _exportFilteredCSV 体内出现。"""
    html = _TPL.read_text(encoding="utf-8")
    restore = html[html.index("function _pbFail("):html.index("window.pbExportOne = pbExportOne")]
    ji = html[html.index("async function _importFromJson("):html.index("function _jiUndoApply(")]
    for name, seg in (("restore", restore), ("json-import", ji)):
        assert re.search(r"(?<![\w$.])_esc\(", seg) is None, f"{name} 块又引用了 CSV 内部的 _esc（#237 复发）"
        assert "_escHtml(" in seg, f"{name} 块应使用页面全局 _escHtml"
    csv = html[html.index("function _exportFilteredCSV("):html.index("function _exportFilteredCSV(") + 2500]
    assert "function _esc(v)" in csv, "_esc 仍应是 CSV 导出的私有 helper（改名请同步本钉）"
    assert "function _escHtml(s)" in html
