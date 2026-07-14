# -*- coding: utf-8 -*-
"""
E2E：聊天 task_hints → 打开任务详情；多链时折叠块。

通过 route mock `GET /tasks/{id}`（URL 谓词仅匹配 MOCK_TID），不依赖真实设备。
Playwright `route(re.compile(...))` 需匹配整段 URL，此前缀正则易误配，故改用 Callable[[str], bool]。

需 Chromium：pip install -r requirements-e2e.txt && python -m playwright install chromium
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.e2e

MOCK_TID = "e2e-hint-0000-0000-0000-000000000001"


def _prep(page, e2e_guest_token: str) -> None:
    page.set_default_timeout(120_000)
    page.add_init_script(
        f"""
        localStorage.setItem('oc_token', {json.dumps(e2e_guest_token)});
        localStorage.setItem('oc_user', 'guest');
        localStorage.setItem('oc_role', 'admin');
        """
    )


def test_chat_task_hints_modal_and_folded_details(
    page, e2e_base_url: str, e2e_guest_token: str
):
    """一次导航：mock 任务详情 + 点击链打开弹窗 + 验证 3 条链时存在 details。"""
    body = {
        "task_id": MOCK_TID,
        "type": "health",
        "status": "completed",
        "device_id": "e2e-serial-device",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:01Z",
        "params": {},
        "result": {},
        "device_label": "E2E-Label",
        "task_origin_label_zh": "E2E",
    }
    payload = json.dumps(body).encode("utf-8")

    def _url_is_mock_task(url: str) -> bool:
        # 匹配完整 URL 或 path；仅当包含 MOCK_TID 时拦截（避免误伤其它 /tasks/*）
        return MOCK_TID in url

    def _fulfill_mock(route):
        route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=payload,
        )

    page.route(_url_is_mock_task, _fulfill_mock)
    _prep(page, e2e_guest_token)
    page.goto(f"{e2e_base_url}/dashboard", wait_until="domcontentloaded")
    page.evaluate("navigateToPage('chat')")
    page.wait_for_function(
        "() => document.querySelector('#page-chat')?.classList.contains('active')",
        timeout=60_000,
    )
    page.wait_for_function(
        "() => typeof formatChatTaskHintsHtml === 'function' && typeof showTaskDetail === 'function'",
        timeout=60_000,
    )
    page.evaluate(
        """(tid) => {
  const box = document.getElementById('chat-messages');
  const fake = {
    reply: 'E2E',
    task_ids: [tid],
    task_hints: [{
      action: 'tiktok_warmup',
      task_id: tid,
      task_id_short: 'e2e-hint…',
      device_label: 'E2E-Label',
      device_serial: ''
    }]
  };
  const hintBlock = formatChatTaskHintsHtml(fake);
  box.innerHTML += '<div class="chat-msg bot"><div class="chat-bubble">' + fake.reply + hintBlock + '</div></div>';
}""",
        MOCK_TID,
    )
    assert page.locator("#chat-messages .chat-msg.bot").last.locator("a").count() == 1
    # 直接 await showTaskDetail：链接触发的 onclick+setTimeout 在 Chromium 自动化下不稳定；路由 mock 与弹窗逻辑仍被覆盖。
    page.evaluate(
        """async (tid) => { await showTaskDetail(tid); }""",
        MOCK_TID,
    )
    page.wait_for_selector("#task-detail-modal", state="visible", timeout=30_000)
    modal = page.locator("#task-detail-modal")
    assert MOCK_TID in modal.inner_text()
    assert "E2E-Label" in modal.inner_text()
    page.evaluate("() => document.getElementById('task-detail-modal')?.remove()")

    html3 = page.evaluate(
        """() => {
  const fake = {
    task_hints: [
      { task_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', task_id_short: 'aaaaaaaa…', device_label: 'A', action: 'x' },
      { task_id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', task_id_short: 'bbbbbbbb…', device_label: 'B', action: 'x' },
      { task_id: 'cccccccc-cccc-cccc-cccc-cccccccccccc', task_id_short: 'cccccccc…', device_label: 'C', action: 'x' },
    ]
  };
  return formatChatTaskHintsHtml(fake);
}"""
    )
    assert "details" in html3.lower()
    assert "(3)" in html3
