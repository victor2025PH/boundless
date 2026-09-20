# -*- coding: utf-8 -*-
"""搁置可见化门禁（P0 2026-08-14，坐席实测「点搁置似乎没任何改变，然后又跳回原来状态」）。

事故机制＝三层叠加：① 成功反馈只有 2.5s 一闪的「已搁置 ✓」（_flash），之后 UI 与
搁置前逐像素相同——坐席无从知道操作生效了；② 搁置语义（移出待接管/超时提醒队列）
与坐席期待（停 AI/会话消失）错位，且无任何文案解释；③ client 缺方法/缺会话 id 时
按钮静默 return 装死，失败一律「操作失败」不给原因。

本文件钉四层修复的关键不变量：
1. 后端契约：GET /api/unified-inbox/automation 捎带 snooze_until（未搁置/已到点=0；
   搁置中=未来 epoch 秒）——cp-conv-ops 持久状态行的读回半边；
2. 哨兵镜像：前端 SNOOZE_FOREVER_TS 必须与 store.SNOOZE_FOREVER_TS 同值；
3. client 契约：unsnoozeConversation / listSnoozed 打对端点;
4. 组件静态不变量（双树同查）：持久状态行/取消搁置/语义微文案/死路收口（禁用+
   tooltip 替代静默 return）/具体错误透传/cpops_* 埋点全桶；旧 _flash 一闪模式不得回潮。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from src.inbox.store import SNOOZE_FOREVER_TS, InboxStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes

_ROOT = Path(__file__).resolve().parents[1]
_TREES = (
    _ROOT / "shared" / "copilot",
    _ROOT / "desktop" / "renderer" / "shared" / "copilot",
)


def _read(rel: str):
    """双树同名文件都读（内容一致由 test_copilot_shared_sync 钉，这里各自断言
    保证「红了知道是哪棵树没同步」）。"""
    out = []
    for tree in _TREES:
        p = tree / rel
        assert p.is_file(), f"缺 {p}"
        out.append((p, p.read_text(encoding="utf-8")))
    return out


# ── 1. 后端契约：automation GET 捎带 snooze_until ───────────────────────


class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page rendering is not used in API tests")


def _client(tmp_path):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    register_unified_inbox_routes(
        app, page_auth=lambda request: True, api_auth=lambda request: True,
        templates=_Templates())
    store = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = store
    app.state.config_manager = SimpleNamespace(
        config={}, set_overlay_flag=lambda path, value: (True, ""))
    return TestClient(app), store


def _get_automation(c, chat_key: str):
    r = c.get("/api/unified-inbox/automation?platform=telegram"
              f"&account_id=a&chat_key={chat_key}")
    assert r.status_code == 200
    return r.json()


def test_automation_get_snooze_until_zero_when_not_snoozed(tmp_path):
    c, store = _client(tmp_path)
    body = _get_automation(c, "500")
    assert body.get("snooze_until") == 0    # 键必须在（前端 feat 探测靠它），值 0
    store.close()


def test_automation_get_snooze_until_carries_future_ts(tmp_path):
    c, store = _client(tmp_path)
    until = time.time() + 3600
    assert store.set_snooze("telegram:a:501", until) is True
    body = _get_automation(c, "501")
    assert abs(float(body["snooze_until"]) - until) < 2.0
    store.close()


def test_automation_get_snooze_until_zero_after_expiry(tmp_path):
    """已到点（snooze_until 过期）必须回 0——否则前端会渲染「已搁置至 <过去时刻>」。"""
    c, store = _client(tmp_path)
    cid = "telegram:a:502"
    store.set_snooze(cid, time.time() + 3600)
    # 直接把库里的时刻改成过去（set_snooze 对过去值会走 clear，绕过它）
    store.patch_conv_meta(cid, {"snooze_until": time.time() - 60})
    body = _get_automation(c, "502")
    assert body["snooze_until"] == 0
    store.close()


def test_automation_get_snooze_until_forever_sentinel(tmp_path):
    c, store = _client(tmp_path)
    store.set_snooze("telegram:a:503", float("inf"))   # 钉到哨兵
    body = _get_automation(c, "503")
    assert float(body["snooze_until"]) == SNOOZE_FOREVER_TS
    store.close()


def test_automation_get_survives_store_absence(tmp_path):
    """无 inbox_store（异常态）→ 软失败按 0，绝不 500。"""
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    register_unified_inbox_routes(
        app, page_auth=lambda request: True, api_auth=lambda request: True,
        templates=_Templates())
    app.state.config_manager = SimpleNamespace(
        config={}, set_overlay_flag=lambda path, value: (True, ""))
    c = TestClient(app)
    r = c.get("/api/unified-inbox/automation?platform=telegram"
              "&account_id=a&chat_key=504")
    assert r.status_code == 200
    assert r.json().get("snooze_until") == 0


# ── 2. 哨兵镜像 ─────────────────────────────────────────────────────────


def test_frontend_forever_sentinel_mirrors_store():
    for p, src in _read("components/cp-conv-ops.js"):
        m = re.search(r"SNOOZE_FOREVER_TS\s*=\s*(\d+)", src)
        assert m, f"{p} 缺 SNOOZE_FOREVER_TS 前端镜像"
        assert float(m.group(1)) == SNOOZE_FOREVER_TS, (
            f"{p} 哨兵值漂移：前端 {m.group(1)} vs store {SNOOZE_FOREVER_TS}")


# ── 3. client 契约 ──────────────────────────────────────────────────────


def test_client_unsnooze_and_list_snoozed_endpoints():
    for p, src in _read("client/copilot-client.js"):
        assert "unsnoozeConversation" in src, f"{p} 缺 unsnoozeConversation"
        assert "/unsnooze" in src, f"{p} unsnooze 端点路径缺失"
        assert "listSnoozed" in src, f"{p} 缺 listSnoozed"
        assert "/api/workspace/snoozed" in src, f"{p} snoozed 清单端点缺失"


# ── 4. 组件静态不变量（双树）────────────────────────────────────────────


def test_component_persistent_snooze_state():
    for p, src in _read("components/cp-conv-ops.js"):
        # 持久状态行 + 取消搁置 + 微文案（i18n 键为契约）
        for key in ("cp.convops.snoozed_until", "cp.convops.snoozed_forever",
                    "cp.convops.unsnooze", "cp.convops.snooze_hint",
                    "cp.convops.na_t"):
            assert key in src, f"{p} 缺 i18n 键消费 {key}"
        # 读回双路径：automation 搭便车字段 + 旧后端 listSnoozed 回落
        assert "snooze_until" in src, f"{p} 未消费 automation.snooze_until"
        assert "listSnoozed" in src, f"{p} 缺旧后端回落读回"
        # 操作成功用 POST 响应就地重渲（零竞态），不再一闪而过
        assert "_patchAndRender" in src, f"{p} 缺就地重渲"
        # 匹配调用/定义形态 `_flash(`（头注释「_flash 已删」的裸词提及不算回潮）
        assert "_flash(" not in src, (
            f"{p} 旧 _flash 一闪模式回潮——持久状态行才是本修复的核心")


def test_component_dead_end_and_error_surfacing():
    for p, src in _read("components/cp-conv-ops.js"):
        # 死路收口：禁用 + tooltip（而非静默 return 装死）
        assert "disabled title=" in src, f"{p} 缺「禁用+tooltip」死路收口"
        # 具体错误透传：失败行显示服务端 i18n 后的 detail
        assert "_showErr" in src, f"{p} 缺具体错误展示"
        assert "op-err" in src, f"{p} 缺 .op-err 错误行"


def test_component_beacons_cover_all_actions():
    """五个动作 + 失败分桶都有埋点——没有埋点的入口无法参与两周后的用量裁决。"""
    need = ("cpops_pause", "cpops_unsnooze", "cpops_archive",
            "cpops_open_inbox", "cpops_fail_pause", "cpops_fail_snooze",
            "cpops_fail_unsnooze", "cpops_fail_archive")
    for p, src in _read("components/cp-conv-ops.js"):
        for b in need:
            assert b in src, f"{p} 缺埋点 {b}"
        # snooze60/240 经 "cpops_" + act 动态拼出
        assert '"cpops_" + act' in src, f"{p} 缺 snooze 分桶埋点"


def test_i18n_keys_bilingual():
    keys = ("cp.convops.snoozed_until", "cp.convops.snoozed_forever",
            "cp.convops.unsnooze", "cp.convops.snooze_hint", "cp.convops.na_t")
    for p, src in _read("i18n/cp-i18n.js"):
        for k in keys:
            hits = src.count(f'"{k}"')
            assert hits >= 2, f"{p} 键 {k} 未双语齐备（出现 {hits} 次，需 zh+en）"
