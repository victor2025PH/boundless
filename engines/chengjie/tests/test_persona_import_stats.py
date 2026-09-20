# -*- coding: utf-8 -*-
"""人设导入/考题观测漏斗（G 线）门禁——全部离线，fake chat_fn，绝不真调 LLM。

覆盖三层：
1. 模块 —— ``PersonaImportStats`` 计数/均值/未知事件忽略/线程安全冒烟/
   dump 平铺形状/dump_prom 形状/reset。
2. 端到端 —— 仿 tests/test_persona_doc_import.py 建 app + flag 注入 +
   ``app.state.persona_doc_chat_fn`` 假函数 → parse（JSON+docx）+ extract →
   轮询 jobs 到 done → 旁路观察线程落账 → ``snapshot()`` 计数与均值可见；
   坏 LLM 输出 → extract_error 同样落账。
3. metrics 合流 —— ``register_metrics_route``（与 test_frontend_error_stats
   同款装配）→ ``/api/workspace/metrics`` JSON 段 + ``?format=prometheus`` 文本。
"""

import asyncio
import json
import sys
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils import persona_doc_import as pdi
from src.utils import persona_import_stats as pis
from src.utils.persona_import_stats import PersonaImportStats


@pytest.fixture(autouse=True)
def _clean_state():
    pis.reset_for_test()
    pdi.reset_jobs()
    yield
    pis.reset_for_test()
    pdi.reset_jobs()


# ── 1. 模块：计数 / 均值 / 忽略 / 线程安全 / dump 形状 / reset ────────────────

def test_counts_and_active_flag():
    s = PersonaImportStats()
    assert s.snapshot()["active"] is False
    s.record("parse")
    s.record("parse")
    s.record("bio_stash")
    snap = s.snapshot()
    assert snap["active"] is True
    assert snap["counts"]["parse"] == 2
    assert snap["counts"]["bio_stash"] == 1
    assert snap["counts"]["extract_done"] == 0
    assert snap["extract_avg_ms"] is None
    assert snap["extract_avg_completeness"] is None
    assert snap["quiz_avg_score"] is None
    assert snap["quiz_last_score"] is None


def test_extract_done_means():
    s = PersonaImportStats()
    s.record("extract_done", duration_ms=1000.0, completeness=80)
    s.record("extract_done", duration_ms=2000.0, completeness=60)
    s.record("extract_done")                     # 无附加值：只计数不进均值
    snap = s.snapshot()
    assert snap["counts"]["extract_done"] == 3
    assert snap["extract_avg_ms"] == pytest.approx(1500.0)
    assert snap["extract_avg_completeness"] == pytest.approx(70.0)


def test_quiz_score_mean_and_last():
    s = PersonaImportStats()
    s.record("quiz_done", score=100)
    s.record("quiz_done", score=50)
    snap = s.snapshot()
    assert snap["counts"]["quiz_done"] == 2
    assert snap["quiz_avg_score"] == pytest.approx(75.0)
    assert snap["quiz_last_score"] == pytest.approx(50.0)
    # 非数值/bool 附加值按缺席处理：计数照加、均值与 last 不动
    s.record("quiz_done", score="oops")
    s.record("quiz_done", score=True)
    snap = s.snapshot()
    assert snap["counts"]["quiz_done"] == 4
    assert snap["quiz_avg_score"] == pytest.approx(75.0)
    assert snap["quiz_last_score"] == pytest.approx(50.0)


def test_unknown_events_silently_ignored():
    s = PersonaImportStats()
    s.record("nonsense_event")
    s.record("")
    s.record(None)
    snap = s.snapshot()
    assert snap["active"] is False
    assert sum(snap["counts"].values()) == 0
    assert "nonsense_event" not in snap["counts"]    # 不吞脏键进 counts


def test_thread_safety_smoke_two_threads_50_each():
    s = PersonaImportStats()

    def worker():
        for _ in range(50):
            s.record("parse")
            s.record("extract_done", duration_ms=10.0, completeness=50)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = s.snapshot()
    assert snap["counts"]["parse"] == 100
    assert snap["counts"]["extract_done"] == 100
    assert snap["extract_avg_ms"] == pytest.approx(10.0)
    assert snap["extract_avg_completeness"] == pytest.approx(50.0)


def test_dump_flat_shape():
    s = PersonaImportStats()
    s.record("parse")
    s.record("quiz_done", score=88)
    s.record("quiz_saved")
    d = s.dump()
    assert d["active"] is True
    for ev in ("parse", "parse_docx", "extract_start", "extract_done",
               "extract_error", "bio_stash", "quiz_run", "quiz_done",
               "quiz_saved"):
        assert ev in d                               # 平铺键齐全
    assert d["parse"] == 1
    assert d["quiz_done"] == 1
    assert d["quiz_saved"] == 1
    assert d["quiz_avg_score"] == pytest.approx(88.0)
    assert d["quiz_last_score"] == pytest.approx(88.0)
    assert d["extract_avg_ms"] is None
    assert "counts" not in d                         # dump 平铺、snapshot 分组


def test_quiz_saved_event_counted():
    s = PersonaImportStats()
    s.record("quiz_saved")
    s.record("quiz_saved")
    snap = s.snapshot()
    assert snap["counts"]["quiz_saved"] == 2
    assert snap["active"] is True
    assert pis.dump()["quiz_saved"] == 0             # 单例未记；实例独立
    pis.record_import_event("quiz_saved")
    assert pis.dump()["quiz_saved"] == 1


def test_dump_prom_shape():
    s = PersonaImportStats()
    s.record("parse")
    s.record("extract_done", duration_ms=1234.5, completeness=77)
    txt = s.dump_prom()
    assert "# TYPE persona_import_events_total counter" in txt
    assert 'persona_import_events_total{event="parse"} 1' in txt
    assert 'persona_import_events_total{event="extract_done"} 1' in txt
    assert 'persona_import_events_total{event="quiz_run"} 0' in txt
    assert "persona_import_extract_avg_ms 1234.5" in txt
    assert "persona_import_extract_avg_completeness 77.0" in txt
    assert "persona_import_quiz_avg_score" not in txt    # 无样本不出 series
    assert txt.endswith("\n")


# ── 1b. 长传记检索观测转发（bio_retrieval）──────────────────────────────────

_FAKE_BIO = {"queries": 10, "hits": 7, "empty": 3, "embed_fail": 2,
             "hit_rate": 0.7, "avg_hits": 1.4}


def _patch_bio(monkeypatch, fn):
    """钉死 persona_bio_store.retrieval_stats_snapshot（dump 侧局部 import 会取到）。"""
    from src.companion import persona_bio_store as pbs
    monkeypatch.setattr(pbs, "retrieval_stats_snapshot", fn)


def test_dump_includes_bio_retrieval(monkeypatch):
    _patch_bio(monkeypatch, lambda: dict(_FAKE_BIO))
    s = PersonaImportStats()
    s.record("bio_stash")
    bio = s.dump()["bio_retrieval"]
    assert bio == _FAKE_BIO
    assert bio is not _FAKE_BIO                      # 拷贝，外部改不动上游字典


def test_dump_bio_retrieval_empty_when_snapshot_raises(monkeypatch):
    """观测转发绝不能把 metrics 路由带崩——上游炸了只是这段读数缺失。"""
    def _boom():
        raise RuntimeError("bio store exploded")

    _patch_bio(monkeypatch, _boom)
    d = PersonaImportStats().dump()                  # 不抛
    assert d["bio_retrieval"] == {}
    assert d["active"] is False                      # 其余字段照常


def test_dump_bio_retrieval_empty_when_snapshot_not_a_dict(monkeypatch):
    _patch_bio(monkeypatch, lambda: "not a dict")
    assert PersonaImportStats().dump()["bio_retrieval"] == {}


def test_dump_prom_includes_bio_retrieval_counters(monkeypatch):
    _patch_bio(monkeypatch, lambda: dict(_FAKE_BIO))
    lines = PersonaImportStats().dump_prom().splitlines()
    for name, val in (("queries", 10), ("hits", 7), ("empty", 3),
                      ("embed_fail", 2)):
        metric = f"persona_bio_retrieval_{name}_total"
        assert any(ln.startswith(f"# HELP {metric} ") for ln in lines)
        assert f"# TYPE {metric} counter" in lines
        assert f"{metric} {val}" in lines             # 整数值行，无小数点


def test_dump_prom_skips_bio_retrieval_when_unavailable(monkeypatch):
    def _boom():
        raise RuntimeError("nope")

    _patch_bio(monkeypatch, _boom)
    txt = PersonaImportStats().dump_prom()
    assert "persona_bio_retrieval" not in txt         # 无读数不出假 0 series
    assert 'persona_import_events_total{event="parse"} 0' in txt


def test_singleton_and_reset_for_test():
    pis.record_import_event("parse")
    pis.record_import_event("quiz_done", score=42)
    snap = pis.snapshot()
    assert snap["counts"]["parse"] == 1
    assert pis.dump()["quiz_last_score"] == pytest.approx(42.0)
    pis.reset_for_test()
    snap = pis.snapshot()
    assert snap["active"] is False
    assert snap["counts"]["parse"] == 0
    assert snap["quiz_last_score"] is None


# ── 2. 端到端：parse + extract → 旁路观察线程落账 ────────────────────────────

_HDRS = {"Authorization": "Bearer test-token"}
_JSON_HDRS = {**_HDRS, "Content-Type": "application/json"}

_IDENTITY_JSON = json.dumps({
    "name": "美月",
    "names": {"english": "Mizuki Sato"},
    "role": "在西班牙巴塞罗那的酒店运营总监助理，32岁",
    "age": 32,
    "personality": {"traits": ["温柔", "细心"], "style": "轻声细语"},
}, ensure_ascii=False)

_BIO_JSON = json.dumps({
    "background": "美月出生于大阪，现居巴塞罗那。",
    "context": {"hobbies": ["瑜伽"],
                "specific_memories": ["父亲是建筑师，2019 年去世"]},
    "tastes": {"likes": ["抹茶"]},
}, ensure_ascii=False)


def _two_stage_fake():
    counter = {"n": 0}

    def chat_fn(system, user, timeout):
        counter["n"] += 1
        return _IDENTITY_JSON if counter["n"] == 1 else _BIO_JSON

    return chat_fn


def _min_docx_bytes(paragraphs) -> bytes:
    body = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>' + body + "</w:body></w:document>"
    )
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _build_app(tmp_path):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "personas": {"profiles": [], "doc_import": {"enabled": True}},
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(
        yaml.dump({"greeting": ["hi"]}), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump({"channels": {}}), encoding="utf-8")

    from src.utils.config_manager import ConfigManager
    from src.utils.persona_manager import PersonaManager

    cm = ConfigManager(str(tmp_path / "config.yaml"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cm.load())
    finally:
        loop.close()

    PersonaManager.reset()
    from src.web.admin import create_app
    return create_app(cm)


@pytest.fixture
def app_on(tmp_path):
    yield _build_app(tmp_path)
    from src.utils.persona_manager import PersonaManager
    PersonaManager.reset()


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _poll_job(c, job_id):
    job = None
    for _ in range(300):
        r = await c.get(f"/api/personas/import-doc/jobs/{job_id}",
                        headers=_HDRS)
        assert r.status_code == 200
        job = r.json()["job"]
        if job["status"] in ("done", "error"):
            break
        await asyncio.sleep(0.02)
    return job


def _wait_stat(event: str, timeout: float = 8.0):
    """终态由旁路观察线程落账（轮询步长 0.15s）→ 等它写进快照。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = pis.snapshot()
        if snap["counts"][event] >= 1:
            return snap
        time.sleep(0.05)
    return pis.snapshot()


@pytest.mark.asyncio
async def test_e2e_parse_extract_funnel_recorded(app_on):
    app_on.state.persona_doc_chat_fn = _two_stage_fake()   # 可测缝：注入假 LLM
    async with _client(app_on) as c:
        # JSON 文本解析 → parse
        r = await c.post("/api/personas/import-doc/parse",
                         headers=_JSON_HDRS, json={"text": "第一段\n第二段"})
        assert r.status_code == 200

        # docx 解析 → parse + parse_docx
        r = await c.post(
            "/api/personas/import-doc/parse", headers=_HDRS,
            files={"file": ("persona.docx", _min_docx_bytes(["她叫美月。"]),
                            "application/vnd.openxmlformats-officedocument"
                            ".wordprocessingml.document")})
        assert r.status_code == 200

        # 解析失败（空文本 400）不计数
        r = await c.post("/api/personas/import-doc/parse",
                         headers=_JSON_HDRS, json={"text": "   "})
        assert r.status_code == 400

        # 抽取提交 → extract_start；轮询到 done
        r = await c.post("/api/personas/import-doc/extract",
                         headers=_JSON_HDRS, json={"text": "人设文档全文……"})
        assert r.status_code == 200
        job = await _poll_job(c, r.json()["job_id"])
        assert job["status"] == "done"

    snap = _wait_stat("extract_done")
    assert snap["active"] is True
    assert snap["counts"]["parse"] == 2
    assert snap["counts"]["parse_docx"] == 1
    assert snap["counts"]["extract_start"] == 1
    assert snap["counts"]["extract_done"] == 1
    assert snap["counts"]["extract_error"] == 0
    assert snap["extract_avg_ms"] is not None and snap["extract_avg_ms"] > 0
    assert (snap["extract_avg_completeness"] is not None
            and snap["extract_avg_completeness"] >= 0)


@pytest.mark.asyncio
async def test_e2e_extract_error_recorded(app_on):
    app_on.state.persona_doc_chat_fn = \
        lambda system, user, timeout: "garbage forever"    # 两次都坏 → 任务 error
    async with _client(app_on) as c:
        r = await c.post("/api/personas/import-doc/extract",
                         headers=_JSON_HDRS, json={"text": "人设文档全文……"})
        assert r.status_code == 200
        job = await _poll_job(c, r.json()["job_id"])
        assert job["status"] == "error"

    snap = _wait_stat("extract_error")
    assert snap["counts"]["extract_start"] == 1
    assert snap["counts"]["extract_error"] == 1
    assert snap["counts"]["extract_done"] == 0
    assert snap["extract_avg_ms"] is None                  # 失败不进耗时均值


# ── 3. metrics 合流（与 test_frontend_error_stats 同款装配）──────────────────

def _make_metrics_app():
    from src.web.routes.drafts_routes import register_metrics_route

    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": "admin", "user_id": "u1"}
        return await call_next(req)

    def api_auth(r: Request):
        return True

    register_metrics_route(app, api_auth=api_auth)
    return TestClient(app, raise_server_exceptions=True)


def test_metrics_json_includes_persona_import():
    pis.record_import_event("parse")
    pis.record_import_event("quiz_run")
    pis.record_import_event("quiz_done", score=90)
    c = _make_metrics_app()
    m = c.get("/api/workspace/metrics").json()
    seg = m.get("persona_import")
    assert seg is not None
    assert seg["active"] is True
    assert seg["parse"] == 1
    assert seg["quiz_run"] == 1
    assert seg["quiz_done"] == 1
    assert seg["quiz_avg_score"] == pytest.approx(90.0)


def test_metrics_prometheus_includes_persona_import():
    pis.record_import_event("extract_done", duration_ms=500.0, completeness=66)
    c = _make_metrics_app()
    r = c.get("/api/workspace/metrics?format=prometheus")
    assert r.status_code == 200
    assert 'persona_import_events_total{event="extract_done"} 1' in r.text
    assert "persona_import_extract_avg_ms 500.0" in r.text
    assert "persona_import_extract_avg_completeness 66.0" in r.text


def test_metrics_carry_bio_retrieval_both_formats(monkeypatch):
    """检索读数经同一对出口合流：JSON 段 + Prometheus 计数（路由侧零改动）。"""
    _patch_bio(monkeypatch, lambda: dict(_FAKE_BIO))
    c = _make_metrics_app()
    seg = c.get("/api/workspace/metrics").json()["persona_import"]
    assert seg["bio_retrieval"]["queries"] == 10
    assert seg["bio_retrieval"]["hit_rate"] == pytest.approx(0.7)
    txt = c.get("/api/workspace/metrics?format=prometheus").text
    assert "persona_bio_retrieval_queries_total 10" in txt
    assert "persona_bio_retrieval_embed_fail_total 2" in txt
