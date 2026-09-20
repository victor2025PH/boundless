# -*- coding: utf-8 -*-
"""副驾 iframe App「陈旧前端」闭环门禁（2026-07-31 人设切换事故收尾）。

事故尾巴：CSRF 写通道当天修好后，坐席仍看到旧版「切换失败，请重试」——那个
窗口跑的是修复前的 JS。根因有两个覆盖缺口，本门禁各钉一条：

1. **iframe App 不在刷新提醒覆盖面里**：workspace_base 的 ui-build 横幅只覆盖
   工作台模板页；桌面壳右栏默认宿主是 iframe 加载的 ``/copilot/app.html``，
   不轮询 ui-build 就会无限期跑旧 JS。app.html（两棵树）必须内置陈旧探针。
2. **/copilot 静态挂载无 Cache-Control**：iframe 入口 app.html 没有 ``?v=`` 戳，
   Starlette 默认响应可被 Chromium 启发式缓存复用数小时——「刷新了还是旧的」。
   挂载必须走 ``RevalidateStaticFiles``（no-cache=每次回源 ETag 校验，304 极廉价）。
"""
from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]

_TREES = (
    REPO / "shared" / "copilot",
    REPO / "desktop" / "renderer" / "shared" / "copilot",
)


def _both(rel: str):
    for base in _TREES:
        p = base / rel
        yield p, p.read_text(encoding="utf-8")


# ── ① app.html 内置陈旧探针 ─────────────────────────────────────────────────

def test_app_html_has_stale_watch_in_both_trees():
    for p, html in _both("app.html"):
        assert 'id="cp-stale"' in html, f"{p} 缺陈旧提醒条 #cp-stale"
        assert "/static/workspace/ui-build.txt" in html, f"{p} 未轮询 ui-build.txt"
        assert "location.reload()" in html, f"{p} 刷新按钮未接 reload"
        assert "visibilitychange" in html, f"{p} 缺回前台复查（长挂 iframe 的主命中路径）"


def test_app_html_stale_banner_hidden_by_default():
    """横幅默认必须不可见：初版曾用 hidden 属性 + 内联 display:flex 相互抵消
    （内联样式压过 UA 的 [hidden] 规则 → 横幅常显）。钉住「display:none 起始 +
    JS 显式切 flex」的实现形态。"""
    for p, html in _both("app.html"):
        i = html.find('id="cp-stale"')
        assert i >= 0, p
        head = html[i:i + 400]
        assert "display:none" in head, f"{p} 陈旧条初始未隐藏（会常显）"
        assert 'style.display = "flex"' in html, f"{p} 探针未显式展示陈旧条"


def test_stale_watch_i18n_keys_bilingual_in_both_trees():
    keys = ("cp.app.stale_text", "cp.app.stale_btn", "cp.app.stale_dismiss")
    for p, js in _both("i18n/cp-i18n.js"):
        for k in keys:
            # reg(zh, en) 双字典各出现一次 → 每键至少 2 次
            assert js.count(f'"{k}"') >= 2, f"{p} 缺双语词条 {k}"


# ── ② /copilot 强制回源校验 ────────────────────────────────────────────────

def test_copilot_mount_uses_revalidate_static():
    src = (REPO / "src" / "web" / "admin.py").read_text(encoding="utf-8")
    assert "class RevalidateStaticFiles(StaticFiles)" in src
    i = src.find('"/copilot"')
    assert i >= 0, "admin.py 未挂载 /copilot（结构变了？更新本门禁）"
    seg = src[i:i + 300]
    assert "RevalidateStaticFiles(" in seg, (
        "/copilot 挂载没走 RevalidateStaticFiles——iframe 入口 app.html 无 ?v= 戳，"
        "默认静态响应会被启发式缓存钉住旧版")


def test_revalidate_static_sends_no_cache(tmp_path):
    """行为验证：200 与 304（ETag 回源）都必须带 Cache-Control: no-cache。"""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from src.web.admin import RevalidateStaticFiles

    (tmp_path / "app.html").write_text("<html>x</html>", encoding="utf-8")
    app = Starlette()
    app.mount("/copilot", RevalidateStaticFiles(directory=str(tmp_path)))
    client = TestClient(app)

    r = client.get("/copilot/app.html")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"
    etag = r.headers.get("etag")
    assert etag, "静态响应缺 ETag（no-cache 依赖它做廉价回源）"

    r2 = client.get("/copilot/app.html", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.headers.get("cache-control") == "no-cache"
