# -*- coding: utf-8 -*-
"""帮助 & 指令参考（L-4 C，#198，D-L6，2026-09-06）门禁。

WBFYSR 实录：帮助页＝TG 时代斜杠命令表（「话术管理(4)」误导）+「客服培训演示」点开一屏
{"detail": …} JSON（docs/training 未随包）。钉住：
1. client 形态整页收起（横幅 + l4-locked）；partner / internal 原页；
2. 「话术管理」→「机器人斜杠命令（旧）」+ 适用范围一句话；
3. 培训演示入口按文件存在性渲染；/training 缺文件 → 人话 HTML 错误页（404），不吐 JSON。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
TPL_DIR = REPO / "src" / "web" / "templates"


def test_help_template_wiring():
    tpl = (TPL_DIR / "help.html").read_text(encoding="utf-8")
    assert '{% include "_client_tier_upgrading.html" %}' in tpl
    assert '<div id="help-root" class="{% if ui_client_hide %}l4-locked{% endif %}">' in tpl
    assert "{% if training_available %}" in tpl, "培训演示入口须做存在性检测"
    assert "sec.scope_key" in tpl, "章节适用范围未渲染"


def test_section_renamed_with_scope():
    from src.web.help_commands import get_help_sections
    from src.web.i18n_packs.help_page import EN, ZH
    sec = get_help_sections()[0]
    assert sec["key"] == "scripts" and sec["scope_key"] == "hp_sec_scripts_scope"
    assert ZH["hp_sec_scripts"] == "机器人斜杠命令（旧）"
    assert "话术管理" not in ZH["hp_sec_scripts"]
    assert "Telegram" in ZH["hp_sec_scripts_scope"] and "Telegram" in EN["hp_sec_scripts_scope"]
    for k in ("hp_sec_scripts", "hp_sec_scripts_scope", "pe_title", "pe_body", "pe_back"):
        assert ZH.get(k) and EN.get(k), f"{k} 缺双语"


def _render_ctx(**over):
    """给 _page_error.html（extends base.html）渲染所需的最小公共上下文（admin._enrich_context 的
    简化版：导航单源 + 词典 + 角色）。"""
    from src.web.nav_schema import get_nav_context
    from src.web.web_i18n import get_translations
    ctx = dict(get_nav_context(None))
    ctx.update(i18n=get_translations("zh"), ui_mode="full", user_role="master",
               active="help", page_perms={}, domain_web_pages=[], help_terms={},
               ui_vis={}, ws_multi_seat=False, plan_badge=None, ui_client_hide=False)
    ctx.update(over)
    return ctx


def _app(monkeypatch, tmp_path, slides_exist: bool):
    from src.web.routes import page_routes as pr
    slides = tmp_path / "docs" / "training" / "slides.html"
    if slides_exist:
        slides.parent.mkdir(parents=True)
        slides.write_text("<html><body>slides</body></html>", encoding="utf-8")
    monkeypatch.setattr(pr, "_TRAINING_SLIDES_PATH", slides)

    real = Jinja2Templates(directory=str(TPL_DIR))

    class _Enriched:
        def TemplateResponse(self, request, name, ctx, **kw):
            full = _render_ctx()
            full.update(ctx or {})
            return real.TemplateResponse(request, name, full, **kw)

    app = FastAPI()

    async def page_auth():
        return True

    pr.register_page_routes(app, SimpleNamespace(templates=_Enriched(), page_auth=page_auth,
                                                 event_tracker=None, log_buffer=None))
    return TestClient(app)


def test_training_missing_renders_human_error_page_not_json(monkeypatch, tmp_path):
    c = _app(monkeypatch, tmp_path, slides_exist=False)
    r = c.get("/training")
    assert r.status_code == 404
    ctype = r.headers.get("content-type", "")
    assert "text/html" in ctype, f"应是 HTML 错误页而不是 JSON：{ctype}"
    assert '"detail"' not in r.text
    assert "培训演示" in r.text or "training" in r.text.lower()
    assert 'href="/help"' in r.text


def test_training_present_serves_slides(monkeypatch, tmp_path):
    c = _app(monkeypatch, tmp_path, slides_exist=True)
    r = c.get("/training")
    assert r.status_code == 200 and "slides" in r.text


def test_help_page_passes_training_availability(monkeypatch, tmp_path):
    from src.web.routes import page_routes as pr
    captured = {}

    class _T:
        def TemplateResponse(self, request, name, ctx, **kw):
            captured.update(ctx)
            from fastapi.responses import HTMLResponse
            return HTMLResponse("ok")

    slides = tmp_path / "slides.html"
    monkeypatch.setattr(pr, "_TRAINING_SLIDES_PATH", slides)
    app = FastAPI()

    async def page_auth():
        return True

    pr.register_page_routes(app, SimpleNamespace(templates=_T(), page_auth=page_auth,
                                                 event_tracker=None, log_buffer=None))
    c = TestClient(app)
    assert c.get("/help").status_code == 200
    assert captured["training_available"] is False
    slides.write_text("x", encoding="utf-8")
    c.get("/help")
    assert captured["training_available"] is True
