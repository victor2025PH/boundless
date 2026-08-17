# -*- coding: utf-8 -*-
"""Messenger 官方渠道「保存即探针」门禁（2026-08-10 P2）。

守的断层：向导保存 Page Token 后，token 抄错/被吊销要等第一次真实出站失败才
暴露；page_id 靠用户去 Meta 后台抄，先存一半再补会造成账号行 id（"official"）
与 webhook 入站镜像（page_id）分裂。保存链路接 Graph ``/me`` 探针后：

- token 真伪当场有结论（probe 随保存响应返回，含 Graph 原始报错）；
- page_id 自动回填（token 自己会说自己是谁），账号行与入站镜像天然同 id；
- 探针失败**绝不阻塞保存**（离线环境照样能把凭证存进去）。

测试模式对齐 test_official_channel_onboarding：直测路由模块 helper +
接线静态断言，不搭全量 HTTP fixture。
"""
from __future__ import annotations

import inspect
from typing import Any, Dict

from src.web.routes import unified_inbox_setup_routes as sr


class _FakeCM:
    """最小 config_manager 假件：save_channel_credentials 模拟真实现的
    「写 overlay + 深合并进进程 config」副作用（真语义由 config_manager 自测）。"""

    def __init__(self, cfg: Dict[str, Any]):
        self.config = cfg
        self.saved: list = []

    def save_channel_credentials(self, channel: str, values: Dict[str, Any]):
        self.saved.append((str(channel), dict(values)))
        block = self.config.setdefault("facebook_messenger", {})
        block.update(values)
        return True, "ok", []


async def test_probe_skips_non_messenger_and_missing_token(monkeypatch):
    called = []

    async def _fake_probe(token, **kw):
        called.append(token)
        return {"ok": True, "page_id": "1", "name": "x", "picture": ""}

    monkeypatch.setattr(
        "src.integrations.facebook_webhook.fb_probe_page", _fake_probe)
    # 非 messenger 渠道：不探
    assert await sr._maybe_probe_messenger_page("zalo", _FakeCM({})) is None
    # messenger 但 token 未填：不探（离线/半配置不制造噪声）
    cm = _FakeCM({"facebook_messenger": {"enabled": True}})
    assert await sr._maybe_probe_messenger_page("messenger", cm) is None
    assert called == []


async def test_probe_autofills_page_id_and_returns_identity(monkeypatch):
    async def _fake_probe(token, **kw):
        assert token == "tok-live"
        return {"ok": True, "page_id": "17891234", "name": "Boundless Page",
                "picture": "https://p.example/x.jpg"}

    monkeypatch.setattr(
        "src.integrations.facebook_webhook.fb_probe_page", _fake_probe)
    cm = _FakeCM({"facebook_messenger": {
        "enabled": True, "page_access_token": "tok-live"}})
    probe = await sr._maybe_probe_messenger_page("messenger", cm)
    assert probe and probe["ok"] is True
    assert probe["name"] == "Boundless Page"
    # page_id 自动回填走同一条 save 路（overlay/掩码语义由真实现保证）
    assert cm.saved == [("messenger", {"page_id": "17891234"})]
    assert cm.config["facebook_messenger"]["page_id"] == "17891234"


async def test_probe_keeps_existing_page_id(monkeypatch):
    async def _fake_probe(token, **kw):
        return {"ok": True, "page_id": "999", "name": "n", "picture": ""}

    monkeypatch.setattr(
        "src.integrations.facebook_webhook.fb_probe_page", _fake_probe)
    cm = _FakeCM({"facebook_messenger": {
        "enabled": True, "page_access_token": "t", "page_id": "111"}})
    probe = await sr._maybe_probe_messenger_page("messenger", cm)
    assert probe["ok"] is True
    assert cm.saved == []          # 运营显式填过的 page_id 优先，不覆写
    assert cm.config["facebook_messenger"]["page_id"] == "111"


async def test_probe_failure_reported_not_raised(monkeypatch):
    async def _fake_probe(token, **kw):
        return {"ok": False, "error": "Invalid OAuth access token",
                "error_kind": "auth"}

    monkeypatch.setattr(
        "src.integrations.facebook_webhook.fb_probe_page", _fake_probe)
    cm = _FakeCM({"facebook_messenger": {
        "enabled": True, "page_access_token": "bad"}})
    probe = await sr._maybe_probe_messenger_page("messenger", cm)
    assert probe == {"ok": False, "error": "Invalid OAuth access token",
                     "error_kind": "auth"}
    assert cm.saved == []          # 失败不回填、不落任何写


async def test_probe_infra_exception_returns_none(monkeypatch):
    """探针基建自身炸了（import/网络层意外）→ None（保存响应不出 probe 字段），
    保存主流程零影响。"""

    async def _boom(token, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "src.integrations.facebook_webhook.fb_probe_page", _boom)
    cm = _FakeCM({"facebook_messenger": {
        "enabled": True, "page_access_token": "t"}})
    assert await sr._maybe_probe_messenger_page("messenger", cm) is None


def test_save_route_wires_probe_before_provision():
    """接线静态断言：探针必须在 _provision_official_account **之前**跑（回填的
    page_id 决定账号行 id，与 webhook 入站镜像同口径），结论随 out["probe"] 返回。"""
    src = inspect.getsource(sr)
    probe_at = src.index("page_probe = await _maybe_probe_messenger_page(")
    provision_at = src.index("official_account = _provision_official_account(")
    assert probe_at < provision_at
    assert 'out["probe"] = page_probe' in src
