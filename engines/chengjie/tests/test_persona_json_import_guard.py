# -*- coding: utf-8 -*-
"""L-2 C（#203，P1）：人设编辑器「从 JSON 导入字段」回滚 + 加闸（2026-09-06）。

事故（WYGNJ2 / 5EFS2A，skuio 1.0.74）：基础 tab 顶部「粘贴 PROFILE JSON」+「解析并填充」
一键整体覆盖 name/role/personality/tags 与保存基底，无预览、无确认、无撤销、无校验；
误粘 + 误点 + 保存 = 正在服务客户的人设被整体替换。

钉住：
- client 形态（ui_client_hide）不渲染导入块；高级字段 JSON 面板标 data-client-hidden；
- 内部形态：导入块是折叠 <details>（无 open），按钮是「解析并预览」而非「解析并填充」；
- 纯函数核心（模板里 [ji-core] 段，node 直跑）：非法 JSON / 非对象 / 未知顶层键直接拒绝；
  预览按字段出旧值→新值；填充只并入勾选字段；撤销快照恢复旧值；
- GET /api/personas/schema-keys 给闸用的合法顶层键（含 capabilities/life_arc，不含 _mrpa_source）；
- 高级字段面板不再直出 voice_profile / capabilities（语音 tab / 相册 tab 表单接管）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from jinja2 import ChainableUndefined, Environment, FileSystemLoader

_TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
_TPL = _TPL_DIR / "personas.html"


def _render(**ctx) -> str:
    env = Environment(loader=FileSystemLoader(str(_TPL_DIR)), undefined=ChainableUndefined, autoescape=False)
    env.globals["url_for"] = lambda *a, **k: "#"
    env.filters["tojson"] = lambda v, **k: json.dumps(None if isinstance(v, ChainableUndefined) else v, ensure_ascii=False)
    env.policies["json.dumps_function"] = lambda v, **k: json.dumps(
        None if isinstance(v, ChainableUndefined) else v, ensure_ascii=False)
    base = {"i18n": {}, "request": None, "user_role": "", "ui_lang": "zh"}
    base.update(ctx)
    return env.get_template("personas.html").render(**base)


# ── 1. 模板：client 形态不渲染；内部形态折叠 + 预览按钮 ─────────────────────────

def test_client_flavor_hides_json_import_and_marks_adv_panel():
    html = _render(ui_client_hide=True)
    assert 'id="pe-json-import"' not in html
    assert 'id="pe-json-import-wrap"' not in html
    assert 'onclick="_importFromJson()"' not in html
    assert 'data-client-hidden="1"' in html
    # 开发者手填区（A 项）同样隐藏
    assert re.search(r'id="pe-vp-dev"\s+style="display:none"', html)


def test_internal_flavor_renders_collapsed_preview_flow():
    html = _render(ui_client_hide=False)
    m = re.search(r'<details class="json-import-wrap" id="pe-json-import-wrap"[^>]*>', html)
    assert m, "内部形态应渲染导入块"
    assert " open" not in m.group(0), "导入块必须折叠常闭（WYGNJ2：1.0.74 默认展开）"
    btn = re.search(r'<button[^>]*onclick="_importFromJson\(\)"[^>]*>(?P<label>[^<]*)</button>', html)
    assert btn, "导入按钮在场"
    assert "预览" in btn.group("label") and "填充" not in btn.group("label"), btn.group("label")
    assert 'id="pe-ji-preview"' in html
    assert 'data-client-hidden="1"' not in html
    # 折叠态可见的三角标记（原先 CSS 把 marker 藏掉，标题像常驻区块）
    assert ".json-import-wrap summary::before{content:'▸ '" in html


# ── 2. 纯函数核心：node 直跑模板里的 [ji-core] 段 ─────────────────────────────

_CORE_RE = re.compile(r"// \[ji-core-begin\](?P<body>.*?)// \[ji-core-end\]", re.S)

_NODE_HARNESS = r"""
%(core)s
const assert = require('assert');
const known = ['name','role','personality','tags','speaking','background','life_arc','capabilities','voice_profile','id'];
// 非法 JSON / 非对象 / 空 / 未知字段 → 拒绝
assert.strictEqual(_jiParse('{bad', known).error, 'invalid_json');
assert.strictEqual(_jiParse('[1,2]', known).error, 'not_object');
assert.strictEqual(_jiParse('{}', known).error, 'empty');
const unk = _jiParse('{"name":"x","totally_unknown":1,"foo":2}', known);
assert.strictEqual(unk.error, 'unknown_fields');
assert.deepStrictEqual(unk.unknown, ['totally_unknown','foo']);
// {persona:{...}} 外壳可解
const ok = _jiParse('{"persona":{"name":"新名","tags":["vip"]}}', known);
assert.strictEqual(ok.ok, true);
assert.deepStrictEqual(ok.persona, {name:'新名', tags:['vip']});
// 预览：逐字段旧值→新值，无变化标记
const loaded = {name:'旧名', role:'陪伴', tags:['vip'], background:'很长的背景', voice_profile:{voice_mode:'clone'}};
const rows = _jiDiff(loaded, {name:'新名', tags:['vip'], life_arc:{stage:'x'}, id:'ignored'});
assert.deepStrictEqual(rows.map(r => r.key), ['name','tags','life_arc']);
assert.strictEqual(rows[0].old, '旧名'); assert.strictEqual(rows[0]['new'], '新名'); assert.strictEqual(rows[0].changed, true);
assert.strictEqual(rows[1].changed, false);
assert.strictEqual(rows[2].old, undefined); assert.strictEqual(rows[2].changed, true);
// 填充只并入勾选字段；未勾选与未提及字段原样；不改入参
const merged = _jiApply(loaded, {name:'新名', role:'新角色', life_arc:{stage:'x'}, id:'ignored'}, ['name','life_arc']);
assert.strictEqual(merged.name, '新名');
assert.strictEqual(merged.role, '陪伴', '未勾选字段不得被覆盖');
assert.strictEqual(merged.background, '很长的背景', '未提及字段原样保留');
assert.deepStrictEqual(merged.voice_profile, {voice_mode:'clone'});
assert.deepStrictEqual(merged.life_arc, {stage:'x'});
assert.strictEqual(merged.id, undefined, 'id 不经导入并入');
assert.strictEqual(loaded.name, '旧名', '入参不可被就地修改');
// 撤销＝恢复快照
const snapshot = JSON.parse(JSON.stringify(loaded));
const after = _jiApply(loaded, {name:'新名'}, ['name']);
assert.notStrictEqual(after.name, snapshot.name);
assert.deepStrictEqual(JSON.parse(JSON.stringify(snapshot)), loaded);
console.log('JI_CORE_OK');
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不在 PATH")
def test_ji_core_pure_functions_under_node():
    src = _TPL.read_text(encoding="utf-8")
    m = _CORE_RE.search(src)
    assert m, "模板缺 [ji-core-begin]/[ji-core-end] 标记"
    core = m.group("body")
    assert "document." not in core and "window." not in core, "核心段必须是纯函数（可 node 直跑）"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
        tf.write(_NODE_HARNESS % {"core": core})
        path = tf.name
    try:
        r = subprocess.run(["node", path], capture_output=True, timeout=60,
                           encoding="utf-8", errors="replace")
    finally:
        Path(path).unlink(missing_ok=True)
    assert r.returncode == 0, (r.stderr or "") + (r.stdout or "")
    assert "JI_CORE_OK" in (r.stdout or "")


def test_fill_path_requires_preview_and_confirm():
    """_importFromJson 只产预览；真正并入在 _jiConfirm 且经 confirm()；撤销函数在场。"""
    src = _TPL.read_text(encoding="utf-8")
    body = src[src.index("async function _importFromJson()"):src.index("function _jiCancel()")]
    assert "_fillDrawerFields(" not in body, "预览阶段不得直接填表单"
    assert "_loadedProfile =" not in body, "预览阶段不得改保存基底"
    confirm_body = src[src.index("function _jiConfirm()"):src.index("function _jiShowUndoToast(")]
    assert "if (!confirm(" in confirm_body
    assert "_jiUndo = {profile:" in confirm_body
    assert "_jiApply(_loadedProfile, _jiPending.persona, picks)" in confirm_body
    assert "function _jiUndoApply()" in src and "30000" in src


def test_adv_panel_no_longer_dumps_voice_profile_or_capabilities():
    src = _TPL.read_text(encoding="utf-8")
    top = re.search(r"var _ADV_MANAGED_TOP = \[(.*?)\];", src, re.S).group(1)
    assert "'voice_profile'" in top and "'capabilities'" in top
    sub = re.search(r"var _ADV_MANAGED_SUB = \{(.*?)\n\};", src, re.S).group(1)
    assert "voice_profile:" not in sub, "voice_profile 整棵子树归语音 tab，不再按子键露出"
    assert "data-client-hidden') === '1'" in src


# ── 3. 后端：合法顶层键 ───────────────────────────────────────────────────────

def test_known_persona_top_keys_contract():
    from src.utils.persona_manager import known_persona_top_keys
    keys = known_persona_top_keys()
    for k in ("name", "role", "personality", "speaking", "identity", "voice_profile",
              "capabilities", "life_arc", "boundaries", "context", "tastes", "names",
              "tags", "quirks", "language", "background", "appearance", "selfie_scenes"):
        assert k in keys, k
    assert "_mrpa_source" not in keys
    assert keys == sorted(set(keys))


@pytest.fixture
def app_on(tmp_path):
    import asyncio

    import yaml

    from src.utils.persona_manager import PersonaManager

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
    PersonaManager.reset()
    from src.web.admin import create_app
    yield create_app(cm)
    PersonaManager.reset()


@pytest.mark.asyncio
async def test_schema_keys_endpoint(app_on):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app_on), base_url="http://test") as c:
        r = await c.get("/api/personas/schema-keys", headers={"Authorization": "Bearer test-token"})
        assert r.status_code == 200
        keys = r.json()["keys"]
        assert "capabilities" in keys and "voice_profile" in keys and "_mrpa_source" not in keys
