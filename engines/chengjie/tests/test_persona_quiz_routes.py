# -*- coding: utf-8 -*-
"""考题路由级门禁（此前只有纯函数/store 测试，路由裸奔过一个真 bug）。

实锤（2026-07-28 生产验收）：`personas.quiz.llm_judge: true` 配了但 web 链
`run_quiz` 漏传 ``judge_fn`` → 裁判从未生效，「酒店运营总监助理」类长期望词
假阴性没人救。本文件钉「配置 → judge_fn 注入」的传递性，两个方向都钉死。
"""
import sys
import time
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_HDRS = {"Authorization": "Bearer test-token"}
_JSON_HDRS = {**_HDRS, "Content-Type": "application/json"}

_PROFILE = {
    "id": "p1", "name": "测试人设", "enabled": True,
    "role": "咖啡店主", "age": 30, "gender": "female",
    "tastes": {"likes": ["红茶", "爬山"]},
}


async def _build_app(tmp_path, llm_judge: bool):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "personas": {
            "profiles": [dict(_PROFILE)],
            "quiz": {"enabled": True, "llm_judge": bool(llm_judge)},
            "doc_import": {"enabled": True},
            "bio_retrieval": {"enabled": True, "semantic": False},
        },
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
    await cm.load()          # 本测试全程在事件循环内（asyncio 测试），直接 await
    PersonaManager.reset()
    from src.web.admin import create_app
    return create_app(cm)


@pytest.fixture(autouse=True)
def _quiet_persona_manager():
    yield
    from src.utils.persona_manager import PersonaManager
    PersonaManager.reset()


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _run_and_capture(tmp_path, monkeypatch, llm_judge: bool):
    """跑一次 quiz 任务，捕获 run_quiz 实际收到的 chat_fn/judge_fn。"""
    from src.utils import persona_quiz as pq
    from src.utils import persona_quiz_store as pqs

    captured = {}

    def _fake_run_quiz(persona, chat_fn, n=10, on_stage=None, judge_fn=None):
        captured["chat_fn"] = chat_fn
        captured["judge_fn"] = judge_fn
        return {"total": 3, "passed": 3, "score": 100, "items": [],
                "persona_name": "测试人设", "n": 3}

    monkeypatch.setattr(pq, "run_quiz", _fake_run_quiz)
    monkeypatch.setattr(pqs, "save_report", lambda *a, **k: None)

    app = await _build_app(tmp_path, llm_judge)

    def _fake_chat(system, user, timeout):
        return "答"

    app.state.persona_doc_chat_fn = _fake_chat

    async with _client(app) as c:
        r = await c.post("/api/personas/p1/quiz", headers=_JSON_HDRS, json={})
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        for _ in range(80):
            j = (await c.get(f"/api/personas/p1/quiz/jobs/{job_id}",
                             headers=_HDRS)).json()["job"]
            if j["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert j["status"] == "done", j
    captured["injected_chat"] = _fake_chat
    return captured


@pytest.mark.asyncio
async def test_llm_judge_on_passes_chat_fn_as_judge(tmp_path, monkeypatch):
    """flag 开 → judge_fn 就是同一个 chat_fn（nightly 同口径），不是 None。"""
    cap = await _run_and_capture(tmp_path, monkeypatch, llm_judge=True)
    assert cap["chat_fn"] is cap["injected_chat"]
    assert cap["judge_fn"] is cap["injected_chat"]


@pytest.mark.asyncio
async def test_llm_judge_off_keeps_judge_none(tmp_path, monkeypatch):
    """flag 关（缺省）→ judge_fn 必须是 None——行为与裁判上线前逐字相同。"""
    cap = await _run_and_capture(tmp_path, monkeypatch, llm_judge=False)
    assert cap["chat_fn"] is cap["injected_chat"]
    assert cap["judge_fn"] is None


# ── M10：finalize 上线流水线（建档同步 + 传记/考题后台任务）─────────────────

async def _await_job(c, job_id):
    for _ in range(120):
        j = (await c.get(f"/api/personas/p1/quiz/jobs/{job_id}",
                         headers=_HDRS)).json()["job"]
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.05)
    raise AssertionError("job 未在时限内完成")


def _fake_quiz(score=90, boom=False):
    def _run(persona, chat_fn, n=10, on_stage=None, judge_fn=None):
        if boom:
            raise RuntimeError("quiz exploded")
        _run.seen = {"persona": persona, "judge_fn": judge_fn, "n": n}
        return {"total": 10, "passed": 9, "score": score, "judged": 1,
                "items": [], "persona_name": "x", "n": 10}
    return _run


@pytest.mark.asyncio
async def test_finalize_full_pipeline(tmp_path, monkeypatch):
    """persona 全给：建档 + 传记入库 + 考题验收一条任务跑完，分段如实回报。"""
    from src.companion import persona_bio_store as pbs
    from src.utils import persona_quiz as pq
    from src.utils import persona_quiz_store as pqs
    from src.utils.persona_manager import PersonaManager

    fake = _fake_quiz(score=90)
    monkeypatch.setattr(pq, "run_quiz", fake)
    monkeypatch.setattr(pqs, "save_report", lambda *a, **k: 1)
    pbs.configure_persona_bio_store(str(tmp_path / "fin.db"), embed_fn=None)
    try:
        app = await _build_app(tmp_path, llm_judge=True)
        app.state.persona_doc_chat_fn = lambda s, u, t: "答"
        async with _client(app) as c:
            r = await c.post("/api/personas/import-doc/finalize",
                             headers=_JSON_HDRS, json={
                                 "profile_id": "np",
                                 "persona": {"name": "新人", "role": "画家",
                                             "age": 25},
                                 "bio_text": "她在巴黎学画，后来搬去里昂。" * 30,
                             })
            assert r.status_code == 200, r.text
            job = await _await_job(c, r.json()["job_id"])
            assert job["status"] == "done"
            res = job["result"]
            assert res["profile_id"] == "np"
            assert res["bio"]["chunks"] > 0
            assert res["quiz"] == {"score": 90, "passed": 9, "total": 10,
                                   "judged": 1}
        assert PersonaManager.get_instance().get_persona_by_id("np")
        assert fake.seen["judge_fn"] is app.state.persona_doc_chat_fn  # 裁判同口径
        assert fake.seen["persona"].get("name") == "新人"   # 考的是落库后的档案
    finally:
        pbs.reset_persona_bio_store()


@pytest.mark.asyncio
async def test_finalize_without_persona_requires_existing(tmp_path, monkeypatch):
    """persona 可省（向导保存链已建档的时序）；档案不存在则 404 防瞎考。"""
    from src.companion import persona_bio_store as pbs
    from src.utils import persona_quiz as pq
    from src.utils import persona_quiz_store as pqs

    monkeypatch.setattr(pq, "run_quiz", _fake_quiz())
    monkeypatch.setattr(pqs, "save_report", lambda *a, **k: 1)
    pbs.configure_persona_bio_store(str(tmp_path / "fin2.db"), embed_fn=None)
    try:
        app = await _build_app(tmp_path, llm_judge=False)
        app.state.persona_doc_chat_fn = lambda s, u, t: "答"
        async with _client(app) as c:
            r = await c.post("/api/personas/import-doc/finalize",
                             headers=_JSON_HDRS,
                             json={"profile_id": "ghost", "bio_text": "x" * 200})
            assert r.status_code == 404
            r = await c.post("/api/personas/import-doc/finalize",
                             headers=_JSON_HDRS,
                             json={"profile_id": "p1",
                                   "bio_text": "咖啡店的故事。" * 40})
            assert r.status_code == 200
            job = await _await_job(c, r.json()["job_id"])
            assert job["status"] == "done"
            assert job["result"]["bio"]["chunks"] > 0
    finally:
        pbs.reset_persona_bio_store()


@pytest.mark.asyncio
async def test_finalize_quiz_error_does_not_mask_bio(tmp_path, monkeypatch):
    """尾部失败不掩盖头部成功：quiz 炸了 → job 仍 done，bio 结果在，quiz_error 如实。"""
    from src.companion import persona_bio_store as pbs
    from src.utils import persona_quiz as pq

    monkeypatch.setattr(pq, "run_quiz", _fake_quiz(boom=True))
    pbs.configure_persona_bio_store(str(tmp_path / "fin3.db"), embed_fn=None)
    try:
        app = await _build_app(tmp_path, llm_judge=False)
        app.state.persona_doc_chat_fn = lambda s, u, t: "答"
        async with _client(app) as c:
            r = await c.post("/api/personas/import-doc/finalize",
                             headers=_JSON_HDRS,
                             json={"profile_id": "p1",
                                   "bio_text": "故事正文。" * 60})
            job = await _await_job(c, r.json()["job_id"])
            assert job["status"] == "done"
            assert job["result"]["bio"]["chunks"] > 0
            assert "quiz exploded" in job["result"]["quiz_error"]
            assert job["result"]["quiz"] is None
    finally:
        pbs.reset_persona_bio_store()


@pytest.mark.asyncio
async def test_finalize_quiz_flag_off_skips_acceptance(tmp_path, monkeypatch):
    """quiz flag 关 → 验收段整体跳过（quiz: null），不算失败也不 503。"""
    from src.companion import persona_bio_store as pbs

    pbs.configure_persona_bio_store(str(tmp_path / "fin4.db"), embed_fn=None)
    try:
        app = await _build_app(tmp_path, llm_judge=False)
        # 热改 live config：关 quiz（chat_fn 不注入——跳过段不该需要 AI）
        cm = app.state.config_manager
        cm.config["personas"]["quiz"]["enabled"] = False
        async with _client(app) as c:
            r = await c.post("/api/personas/import-doc/finalize",
                             headers=_JSON_HDRS,
                             json={"profile_id": "p1",
                                   "bio_text": "只入库不验收。" * 40})
            assert r.status_code == 200, r.text
            job = await _await_job(c, r.json()["job_id"])
            assert job["status"] == "done"
            assert job["result"]["quiz"] is None
            assert "quiz_error" not in job["result"]
            assert job["result"]["bio"]["chunks"] > 0
    finally:
        pbs.reset_persona_bio_store()
