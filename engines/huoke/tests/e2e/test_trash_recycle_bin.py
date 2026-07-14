# -*- coding: utf-8 -*-
"""
E2E：回收站「永久删除」与数据一致性。

- HTTP：创建 pending 任务（run_on_host=false）→ 取消 → 软删，回收站 +1。
- 浏览器：先 `GET /health`（同源 + token 注入，避免先开 `/dashboard` 时全量脚本/WebSocket 与单 worker 竞态导致 `erase-batch` 长时间无响应）；再用 Playwright `page.request.post` 调 `POST /tasks/erase-batch`。数据校验后再 **可选** 打开 `/dashboard` 做轻量冒烟（见代码）。
- HTTP：再查 `GET /tasks/count?trash_only=true` 应恢复为删除前数量。

说明：回收站角标依赖 `loadTasks` + `_refreshTrashCountBadge`，在自动化环境下易受脚本缓存与计时影响；
本条用例覆盖「详情 API + 浏览器侧永久删除」主路径，角标由单元/接口与手动回归覆盖。

需：pip install -r requirements-e2e.txt && python -m playwright install chromium
"""
from __future__ import annotations

import json
import time
import urllib.request

import pytest

pytestmark = pytest.mark.e2e


def _http_json(method: str, url: str, body: dict | None = None) -> dict:
    data = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw.strip() else {}


def _seed_trash_item(base: str) -> str:
    r = _http_json(
        "POST",
        base + "/tasks",
        {
            "type": "telegram_send_message",
            "params": {"username": "@e2e_trash", "message": "e2e"},
            "run_on_host": False,
        },
    )
    tid = r["task_id"]
    _http_json("POST", base + f"/tasks/{tid}/cancel", {})
    _http_json("DELETE", base + f"/tasks/{tid}", None)
    return tid


def _trash_count(base: str) -> int:
    return int(_http_json("GET", base + "/tasks/count?trash_only=true", None).get("count", -1))


def _prep(page, token: str, api_origin: str) -> None:
    page.set_default_timeout(120_000)
    origin = api_origin.rstrip("/")
    page.add_init_script(
        f"""
        localStorage.setItem('oc_api_origin', {json.dumps(origin)});
        localStorage.setItem('oc_token', {json.dumps(token)});
        localStorage.setItem('oc_user', 'guest');
        localStorage.setItem('oc_role', 'admin');
        """
    )


def test_erase_task_from_browser_restores_trash_count(
    page, e2e_base_url: str, e2e_guest_token: str
):
    before = _trash_count(e2e_base_url)
    tid = _seed_trash_item(e2e_base_url)
    mid = _trash_count(e2e_base_url)
    assert mid == before + 1

    _prep(page, e2e_guest_token, e2e_base_url)
    # 仅轻量打开同源页，避免 /dashboard 拉 WebSocket/全量脚本时占满单 worker 导致后续 API 挂起。
    page.goto(f"{e2e_base_url}/health", wait_until="domcontentloaded")

    resp = page.request.post(
        e2e_base_url + "/tasks/erase-batch",
        data=json.dumps({"task_ids": [tid]}),
        headers={"Content-Type": "application/json"},
        timeout=60_000,
    )
    assert resp.status == 200
    assert (resp.json() or {}).get("erased") == 1

    deadline = time.time() + 30
    while time.time() < deadline:
        if _trash_count(e2e_base_url) == before:
            break
        time.sleep(0.25)
    else:
        pytest.fail(
            f"trash count expected {before}, got {_trash_count(e2e_base_url)}"
        )

    # 数据路径已验证；再轻量打开仪表盘，确认 SPA 可加载（不在此之前打开，以免与 erase 并发占满 worker）。
    page.goto(f"{e2e_base_url}/dashboard", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector("#page-title", timeout=30_000)
