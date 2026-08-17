# -*- coding: utf-8 -*-
"""出站近重复守卫 · autosend 接线门禁（2026-08-02 P0）。

锁定：
  - 守卫开启时，同会话 3 分钟内的同义改写第二条被静默跳过（不送、不计投递错误）；
  - 命中拦截走 total_dup_blocked，不喂熔断（_consecutive_errors 不涨）；
  - 重试项 (_attempt>0) 免检——重发同文本是 recoverable 的既定语义；
  - 投递失败撤销在途登记（防登记幽灵把后续重试拦住）；
  - 守卫关闭（默认）= 零行为变更；
  - A 线（telegram_client）与 bootstrap 注入的静态接线不被回退。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from src.inbox.autosend_worker import AutosendWorker
from src.inbox.outbound_dup_guard import outbound_registry

_ENGINE_ROOT = Path(__file__).resolve().parents[1]

FIRST = ("But hey, you're chatting with me now, "
         "so it's not totally alone, right? 😊")
PARAPHRASE = "But hey, you're not really alone right now, are you? I'm here."


def _conv_id() -> str:
    # outbound_registry 是模块级单例，会话 ID 唯一化防跨测试串味
    return f"test-dup-{uuid.uuid4().hex[:12]}"


class _FakeSvc:
    """最小草稿服务：queue 里塞什么，list_drafts 就吐什么（吐完即空）。"""

    def __init__(self):
        self.queue = []
        self.resolved = []
        self._store = None

    def list_drafts(self, status="pending", limit=200):
        batch, self.queue = self.queue, []
        return batch

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append((draft_id, action))
        return {"ok": True}


def _draft(conv, text, draft_id="d1"):
    return {
        "draft_id": draft_id, "autopilot_level": "L2",
        "final_text": text, "platform": "whatsapp",
        "account_id": "a1", "chat_key": "c1", "conversation_id": conv,
    }


def _worker(svc, sent, *, enabled=True, fail=False, **cfg_extra):
    async def _send_cb(platform, account_id, chat_key, text):
        if fail:
            raise RuntimeError("boom")
        sent.append(text)
        return {"ok": True}

    async def _sleep(d):
        return None

    guard = {"enabled": enabled, "window_sec": 180.0, "block_similar": True}
    guard.update(cfg_extra)
    return AutosendWorker(
        draft_service=svc, send_callback=_send_cb, sleep=_sleep,
        dup_guard_cfg=guard,
    )


@pytest.mark.asyncio
async def test_second_paraphrase_blocked():
    """事故金标：第一条正常发出并登记，第二条同义改写被拦。"""
    conv = _conv_id()
    svc = _FakeSvc()
    sent = []
    w = _worker(svc, sent)
    svc.queue = [_draft(conv, FIRST, "d1")]
    await w._tick()
    assert sent == [FIRST]
    svc.queue = [_draft(conv, PARAPHRASE, "d2")]
    await w._tick()
    assert sent == [FIRST]                       # 第二条没发出去
    assert w.total_dup_blocked == 1
    assert w.total_deliver_errors == 0           # 拦截不算投递错误
    assert w._consecutive_errors == 0            # 不喂熔断
    assert w.status_snapshot()["total_dup_blocked"] == 1
    assert w.status_snapshot()["dup_guard_enabled"] is True


@pytest.mark.asyncio
async def test_distinct_replies_pass():
    conv = _conv_id()
    svc = _FakeSvc()
    sent = []
    w = _worker(svc, sent)
    svc.queue = [_draft(conv, FIRST, "d1")]
    await w._tick()
    svc.queue = [_draft(conv, "明天早上九点方便给你打电话吗？", "d2")]
    await w._tick()
    assert len(sent) == 2 and w.total_dup_blocked == 0


@pytest.mark.asyncio
async def test_guard_disabled_is_noop():
    conv = _conv_id()
    svc = _FakeSvc()
    sent = []
    w = _worker(svc, sent, enabled=False)
    svc.queue = [_draft(conv, FIRST, "d1")]
    await w._tick()
    svc.queue = [_draft(conv, FIRST, "d2")]      # 逐字重复也放行（守卫关）
    await w._tick()
    assert len(sent) == 2 and w.total_dup_blocked == 0
    # 关闭时也不产生在途登记
    assert outbound_registry.recent_rows(conv) == []


@pytest.mark.asyncio
async def test_retry_items_skip_guard():
    """重试项 _attempt>0 免检：重发同文本是 recoverable 的既定语义。"""
    conv = _conv_id()
    svc = _FakeSvc()
    sent = []
    w = _worker(svc, sent)
    w._recoverable = True
    svc.queue = [_draft(conv, FIRST, "d1")]
    await w._tick()
    assert sent == [FIRST]
    # 模拟一条到期重试：同文本、_attempt=1 → 不过守卫直接发
    # （投递项与草稿行形状不同：_process_batch 产出的 to_deliver 用 "text" 键）
    item = _draft(conv, FIRST, "d1")
    item["text"] = FIRST
    item["_attempt"] = 1
    w._retry_queue.append({"item": item, "next_ts": 0})
    await w._tick()
    assert sent == [FIRST, FIRST]
    assert w.total_dup_blocked == 0


@pytest.mark.asyncio
async def test_send_failure_unregisters():
    """投递失败撤销在途登记：不留幽灵行拦住后续重发。"""
    conv = _conv_id()
    svc = _FakeSvc()
    w = _worker(svc, [], fail=True)
    svc.queue = [_draft(conv, FIRST, "d1")]
    await w._tick()
    assert w.total_deliver_errors == 1
    assert outbound_registry.recent_rows(conv) == []


@pytest.mark.asyncio
async def test_store_rows_also_feed_guard():
    """DB 出站镜像同样参与比对（跨进程/重启后仍有依据）。"""
    conv = _conv_id()
    svc = _FakeSvc()

    class _Store:
        def list_recent_messages(self, cid, limit=8):
            import time
            return [{"direction": "out", "text": FIRST,
                     "ts": time.time() - 20}]

    svc._store = _Store()
    sent = []
    w = _worker(svc, sent)
    svc.queue = [_draft(conv, PARAPHRASE, "d1")]
    await w._tick()
    assert sent == [] and w.total_dup_blocked == 1


def test_static_wiring_not_reverted():
    """A 线 + bootstrap 的接线以源码为证（防后续重构悄悄拆线）。"""
    tg = (_ENGINE_ROOT / "src" / "client" / "telegram_client.py").read_text(
        encoding="utf-8")
    assert "outbound_dup_guard" in tg and "near_duplicate_of_recent" in tg
    boot = (_ENGINE_ROOT / "src" / "bootstrap" / "web_app.py").read_text(
        encoding="utf-8")
    assert "dup_guard_cfg" in boot and "resolve_guard_cfg" in boot
    helpers = (_ENGINE_ROOT / "src" / "inbox" / "autodraft_helpers.py"
               ).read_text(encoding="utf-8")
    assert "InboundMerger" in helpers and "resolve_merge_cfg" in helpers
