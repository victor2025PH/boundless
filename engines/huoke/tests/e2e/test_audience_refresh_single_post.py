# -*- coding: utf-8 -*-
"""
E2E：三条入口各自对 /task-params/reload-audience-presets 仅 1× POST：
  ① TikTok 运维条 ↻
  ② 平台快捷任务弹窗「↻ 刷新」
  ③ 总览「执行方案」弹窗「↻ 人群预设」（_ttFlowRefreshAudiencePresets）

同一会话内连续验证，避免再次 page.goto 与单 worker uvicorn 竞态。

运行前：pip install -r requirements-e2e.txt && python -m playwright install chromium
调试：pytest tests/e2e -m e2e -v --tracing=retain-on-failure（失败时见 test-results/）
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.e2e


def _e2e_prep_tiktok_platform(page, e2e_base_url: str, guest_token: str) -> None:
    """登录态 + 打开 TikTok 平台页；等待 platforms.js 就绪。"""
    page.set_default_timeout(120_000)
    page.add_init_script(
        f"""
        localStorage.setItem('oc_token', {json.dumps(guest_token)});
        localStorage.setItem('oc_user', 'guest');
        localStorage.setItem('oc_role', 'admin');
        """
    )
    page.goto(f"{e2e_base_url}/dashboard", wait_until="domcontentloaded")
    page.evaluate("navigateToPage('plat-tiktok')")
    page.wait_for_function(
        """() => {
          const el = document.querySelector('#page-plat-tiktok');
          return el && el.classList.contains('active') && typeof _tkQuickTaskWithModal === 'function';
        }""",
        timeout=120_000,
    )


def _open_tiktok_follow_modal_with_audience(page) -> None:
    """打开含「人群预设」区块的快捷任务弹窗（tiktok_follow）。"""
    page.evaluate(
        "async () => { await _tkQuickTaskWithModal('tiktok','tiktok_follow'); }"
    )
    page.wait_for_selector("#tk-task-modal", state="visible", timeout=60_000)
    page.wait_for_selector("#tkp-audience_preset", state="visible", timeout=30_000)


def test_reload_audience_presets_three_entrypoints_single_post_each(
    page, e2e_base_url: str, e2e_guest_token: str
):
    """运维条、平台弹窗、总览执行方案弹窗 — 各 1 次 POST reload。"""
    _e2e_prep_tiktok_platform(page, e2e_base_url, e2e_guest_token)
    page.wait_for_selector("#tt-ops-audience-refresh", state="visible")

    posts: list[str] = []

    def _track(req):
        u = req.url
        if req.method == "POST" and "/task-params/reload-audience-presets" in u:
            posts.append(u)

    page.on("request", _track)

    with page.expect_request(
        lambda r: r.method == "POST"
        and "/task-params/reload-audience-presets" in r.url,
        timeout=60_000,
    ):
        page.click("#tt-ops-audience-refresh")
    page.wait_for_timeout(400)
    assert len(posts) == 1, f"[ops bar] expected 1 POST, got {len(posts)}: {posts}"

    _open_tiktok_follow_modal_with_audience(page)

    with page.expect_request(
        lambda r: r.method == "POST"
        and "/task-params/reload-audience-presets" in r.url,
        timeout=60_000,
    ):
        page.locator('#tk-task-modal button[title="重新拉取服务器预设列表"]').click()
    page.wait_for_timeout(400)
    assert len(posts) == 2, f"[plat modal] expected 2 POSTs total, got {len(posts)}: {posts}"

    page.evaluate("() => { var m = document.getElementById('tk-task-modal'); if (m) m.remove(); }")
    page.evaluate("navigateToPage('overview')")
    page.wait_for_function(
        "() => document.querySelector('#page-overview')?.classList.contains('active')",
        timeout=60_000,
    )
    page.evaluate(
        "async () => { await _ttOpenFlowConfig('e2e-smoke'); }"
    )
    page.wait_for_selector("#tt-flow-overlay.open", state="visible", timeout=90_000)
    page.wait_for_selector("button.ttf-head-refresh-audience", state="visible", timeout=30_000)

    with page.expect_request(
        lambda r: r.method == "POST"
        and "/task-params/reload-audience-presets" in r.url,
        timeout=60_000,
    ):
        page.click("button.ttf-head-refresh-audience")
    page.wait_for_timeout(400)
    assert len(posts) == 3, f"[flow modal] expected 3 POSTs total, got {len(posts)}: {posts}"
