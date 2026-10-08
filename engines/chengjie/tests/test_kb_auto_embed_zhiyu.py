# -*- coding: utf-8 -*-
"""知识库导入 / 启用即向量化（2026-10-08 智语）。

173 实测：启用 22 条只有 1 条有向量——21 条是 10-02 批量导入的，导入接口不走「保存即向量化」。
钉住：
  - 批量导入（JSON/YAML、CSV/XLSX）真导入后、单条启用、批量启用 → 后台补齐「启用且无向量」；
  - 失败自动退避重试（30s/2min/10min），用完记 failed，健康页可见、可手动重试；
  - 健康页（maintenance-advice / health / health-stats）带向量覆盖率，启用条目 <100% 亮黄灯。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.utils import kb_embed_queue as q
from src.utils.kb_store import KnowledgeBaseStore

ENGINE = Path(__file__).resolve().parents[1]


@pytest.fixture()
def store(tmp_path):
    return KnowledgeBaseStore(tmp_path / "kb.db")


def _add(store, title, enabled=1):
    return store.add_entry({"title": title, "triggers": [title], "category": "常规咨询",
                            "example_reply_zh": title + " 回复", "enabled": enabled})


class _Embed:
    """前 fail_calls 次调用整批失败（返回 []），之后正常。"""

    def __init__(self, fail_calls=0):
        self.fail_calls = fail_calls
        self.calls = 0

    async def __call__(self, texts):
        self.calls += 1
        if self.calls <= self.fail_calls:
            return []
        return [[0.1, 0.2, 0.3] for _ in texts]


def _run(queue, reason="import"):
    async def main():
        assert queue.request(reason) is True
        await queue._task
    asyncio.run(main())


# ── 队列 ──────────────────────────────────────────────────────────────


def test_queue_embeds_all_pending_enabled_entries(store):
    ids = [_add(store, f"条目{i}") for i in range(3)]
    off = _add(store, "停用条目", enabled=0)
    sleeps = []
    qq = q.KbEmbedQueue(store, _Embed(), sleep=lambda d: _sleep_noop(sleeps, d))
    _run(qq)
    cov = store.embedding_coverage()
    assert cov["total"] == 3 and cov["done"] == 3
    assert not store.get_entry(off).get("embedding")       # 停用条目不算、不跑
    st = qq.state()
    assert st["status"] == "ok" and st["last_done"] == 3 and st["last_failed"] == 0
    assert st["last_reason"] == "import" and not st["running"] and sleeps == []
    h = q.health(store, qq, enabled=True)
    assert h["light"] == "green" and h["pct"] == 100.0 and h["pending"] == 0
    assert all(store.get_entry(i).get("embedding") for i in ids)


async def _sleep_noop(rec, d):
    rec.append(d)


def test_queue_retries_with_backoff_then_succeeds(store):
    _add(store, "注册")
    _add(store, "提现")
    sleeps = []
    # 首轮：批失败 + 2 条逐条重试失败 = 3 次；第二轮成功
    emb = _Embed(fail_calls=3)
    qq = q.KbEmbedQueue(store, emb, sleep=lambda d: _sleep_noop(sleeps, d))
    _run(qq)
    assert sleeps == [30.0]
    st = qq.state()
    assert st["status"] == "ok" and st["attempts"] == 2 and st["last_failed"] == 0
    assert store.embedding_coverage()["done"] == 2


def test_queue_gives_up_after_retries_visible_and_manual_retry(store):
    eid = _add(store, "退款")
    sleeps = []
    emb = _Embed(fail_calls=10_000)
    qq = q.KbEmbedQueue(store, emb, sleep=lambda d: _sleep_noop(sleeps, d))
    _run(qq)
    assert sleeps == list(q.RETRY_DELAYS)
    st = qq.state()
    assert st["status"] == "failed" and st["attempts"] == len(q.RETRY_DELAYS) + 1
    assert st["failed_ids"] == [eid] and st["last_error"] == "embedding_failed"
    h = q.health(store, qq, enabled=True)
    assert h["light"] == "yellow" and h["reason"] == "embedding_failed" and h["pending"] == 1
    # 状态落库：新队列实例（模拟重启）照样看得到
    assert q.health(store, None)["job"]["status"] == "failed"
    # 端点恢复后手动重试 → 绿
    emb.fail_calls = 0
    async def main():
        assert qq.retry_now() is True
        await qq._task
    asyncio.run(main())
    h2 = q.health(store, qq, enabled=True)
    assert h2["light"] == "green" and h2["job"]["status"] == "ok"
    assert h2["job"]["last_reason"] == "manual_retry"


def test_queue_coalesces_requests_while_running(store):
    _add(store, "a")
    gate = {"n": 0}

    async def emb(texts):
        gate["n"] += 1
        await asyncio.sleep(0)
        return [[1.0] for _ in texts]

    qq = q.KbEmbedQueue(store, emb)

    async def main():
        assert qq.request("import") is True
        assert qq.request("enable") is True       # 合并：跑完再来一轮
        assert qq.request("batch_enable") is True
        await qq._task
    asyncio.run(main())
    assert qq.state()["last_reason"] == "batch_enable"
    assert gate["n"] == 1                        # 第二轮没有待向量化条目 → 不打端点


def test_queue_no_loop_or_unconfigured_does_not_schedule(store):
    _add(store, "a")
    qq = q.KbEmbedQueue(store, _Embed())
    assert qq.request("import") is False          # 同步上下文没有事件循环
    q2 = q.KbEmbedQueue(store, _Embed(), enabled_fn=lambda: False)

    async def main():
        return q2.request("import")
    assert asyncio.run(main()) is False
    h = q.health(store, q2, enabled=False)
    assert h["light"] == "yellow" and h["reason"] == "embedding_unconfigured"
    assert h["job"]["status"] == "unconfigured"


def test_health_empty_kb_is_green(store):
    h = q.health(store, None, enabled=True)
    assert h["enabled_total"] == 0 and h["light"] == "green" and h["pct"] == 100.0


def test_state_has_no_entry_text(store):
    _add(store, "绝密话术标题")
    qq = q.KbEmbedQueue(store, _Embed(fail_calls=10_000), retry_delays=())
    _run(qq)
    raw = store.get_meta(q.STATE_KEY)
    assert raw and "绝密话术标题" not in raw


# ── 路由 ──────────────────────────────────────────────────────────────


def _app(store, tmp_path, auto_embed=True):
    from fastapi import FastAPI
    from starlette.middleware.sessions import SessionMiddleware

    from src.web.routes.kb_routes import register_kb_routes

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    conf = {"knowledge_base": {"auto_embed": auto_embed}}
    cfg = SimpleNamespace(config_path=str(tmp_path / "config.yaml"), config=conf,
                          get=lambda key, default=None: default)
    ctx = SimpleNamespace(kb_store=store, config_manager=cfg, audit_store=None,
                          api_auth=lambda request: None, require_auth=lambda request: None,
                          fire_webhook=lambda *a, **k: None)
    register_kb_routes(app, ctx)
    return app


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_route_import_triggers_background_embedding_and_health(store, tmp_path):
    from fastapi.testclient import TestClient
    app = _app(store, tmp_path)
    qq = app.state.kb_embed_queue
    qq.embed_fn = _Embed()
    data = {"entries": [{"title": f"导入{i}", "triggers": [f"导入{i}"], "category": "常规咨询",
                         "example_reply_zh": "好的", "enabled": 1} for i in range(3)]}
    with TestClient(app) as c:
        r = c.post("/api/kb/import", json={"data": data, "mode": "skip"}).json()
        assert r["added"] == 3 and r["embed_scheduled"] is True
        assert _wait(lambda: qq.state().get("status") == "ok")
        adv = c.get("/api/kb/maintenance-advice").json()
        e = adv["embedding"]
        assert e["enabled_total"] >= 3 and e["light"] == "green" and e["pct"] == 100.0
        assert c.get("/api/kb/health").json()["embedding"]["light"] == "green"
        assert c.get("/api/kb/health-stats").json()["embedding"]["light"] == "green"
        assert c.get("/api/kb/embed-health").json()["pending"] == 0


def test_route_import_dry_run_and_csv_scheduling(store, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    app = _app(store, tmp_path)
    reasons = []
    monkeypatch.setattr(app.state.kb_embed_queue, "request", lambda r: reasons.append(r) or True)
    data = {"entries": [{"title": "试导", "triggers": ["试导"], "enabled": 1}]}
    with TestClient(app) as c:
        r = c.post("/api/kb/import", json={"data": data, "dry_run": True}).json()
        assert "embed_scheduled" not in r and reasons == []
        csv_text = "title,triggers,example_reply_zh\n表格导入,表格,好的\n"
        r2 = c.post("/api/kb/import-csv", json={"csv": csv_text}).json()
        assert r2["added"] == 1 and r2["embed_scheduled"] is True
    assert reasons == ["import_csv"]


def test_route_enable_single_and_batch_schedule(store, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    a = _add(store, "单条", enabled=0)
    b = _add(store, "批量", enabled=0)
    app = _app(store, tmp_path)
    reasons = []
    monkeypatch.setattr(app.state.kb_embed_queue, "request", lambda r: reasons.append(r) or True)
    with TestClient(app) as c:
        assert c.put(f"/api/kb/entries/{a}", json={"enabled": 0}).status_code == 200
        assert reasons == []                                  # 停用不触发
        assert c.put(f"/api/kb/entries/{a}", json={"enabled": 1}).status_code == 200
        assert reasons == ["enable"]
        c.post("/api/kb/entries/batch-update", json={"ids": [b], "enabled": 0})
        assert reasons == ["enable"]
        c.post("/api/kb/entries/batch-update", json={"ids": [b], "enabled": 1})
        assert reasons == ["enable", "batch_enable"]


def test_route_failure_yellow_then_retry_endpoint(store, tmp_path):
    from fastapi.testclient import TestClient
    _add(store, "失败条目")
    app = _app(store, tmp_path)
    qq = app.state.kb_embed_queue
    emb = _Embed(fail_calls=10_000)
    qq.embed_fn = emb
    qq.retry_delays = ()
    with TestClient(app) as c:
        c.post("/api/kb/embed-retry")
        assert _wait(lambda: qq.state().get("status") == "failed")
        e = c.get("/api/kb/maintenance-advice").json()["embedding"]
        assert e["light"] == "yellow" and e["reason"] == "embedding_failed"
        emb.fail_calls = 0
        r = c.post("/api/kb/embed-retry").json()
        assert r["ok"] is True
        assert _wait(lambda: qq.state().get("status") == "ok")
        assert c.get("/api/kb/embed-health").json()["light"] == "green"


def test_route_unconfigured_does_not_schedule_and_is_yellow(store, tmp_path):
    from fastapi.testclient import TestClient
    _add(store, "无端点")
    app = _app(store, tmp_path, auto_embed=False)
    with TestClient(app) as c:
        data = {"entries": [{"title": "导入无端点", "triggers": ["x"], "enabled": 1}]}
        r = c.post("/api/kb/import", json={"data": data}).json()
        assert r["embed_scheduled"] is False
        e = c.get("/api/kb/embed-health").json()
        assert e["light"] == "yellow" and e["reason"] == "embedding_unconfigured"
        assert c.post("/api/kb/embed-retry").json()["ok"] is False


# ── 页面 ──────────────────────────────────────────────────────────────


def test_knowledge_page_shows_coverage_light_and_retry():
    html = (ENGINE / "src/web/templates/knowledge.html").read_text(encoding="utf-8")
    for needle in ('id="kb-embed-chip"', "function _embedHealthHtml", "function embedRetry",
                   "/api/kb/embed-retry", "_applyEmbedChip(d.embedding)", "🟡"):
        assert needle in html, needle
    from src.web.i18n_packs import knowledge_page as kp
    from src.web.i18n_packs import zh_hant_auto as hant
    for k in ("kb_emb_title", "kb_emb_ok", "kb_emb_pending", "kb_emb_failed", "kb_emb_retrying",
              "kb_emb_running", "kb_emb_unconfigured", "kb_emb_retry", "kb_emb_queued",
              "kb_emb_last_fail"):
        assert kp.ZH.get(k) and kp.EN.get(k) and hant.ZH_HANT.get(k), k
        assert f"window.T('{k}')" in html or f"'{k}'" in html, k
