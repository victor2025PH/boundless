# -*- coding: utf-8 -*-
"""接入进度（实施99 P1-2，2026-09-08）：落库 / 合并（auto > manual > todo）/ 到期 overdue / 页面勾选 → 状态灯与步骤卡。"""
from __future__ import annotations

import time

import pytest

from src.web import onboarding_progress as op


def test_store_and_merge(tmp_path):
    st = op.OnboardingProgress(str(tmp_path / "p.db"))
    now = 1_800_000_000.0
    st.set("tiktok", 1, state="done", now=now)
    st.set("tiktok", 2, state="submitted", due_ts=now + 5 * 86400, note="已交表", now=now)
    st.set("tiktok", 3, state="blocked", now=now)
    assert st.get("tiktok", 2)["note"] == "已交表" and set(st.all("tiktok")) == {1, 2, 3}
    with pytest.raises(ValueError):
        st.set("tiktok", 1, state="nope")
    merged = op.merge_steps(5, st.all("tiktok"), {4: "doing"}, now)
    assert [s["state"] for s in merged] == ["done", "submitted", "blocked", "doing", "todo"]
    assert merged[3]["auto"] is True and merged[1]["overdue"] is False and merged[1]["due_date"]
    # 到期未完成 → overdue；auto 覆盖手动
    merged = op.merge_steps(5, st.all("tiktok"), {2: "done"}, now + 6 * 86400)
    assert merged[1]["state"] == "done" and merged[1]["auto"] is True
    st.set("tiktok", 2, state="submitted", due_ts=now + 5 * 86400, now=now)
    merged = op.merge_steps(5, st.all("tiktok"), {}, now + 6 * 86400)
    assert merged[1]["overdue"] is True
    st.clear("tiktok", 3)
    assert 3 not in st.all("tiktok")


@pytest.fixture()
def _isolated_progress(tmp_path, monkeypatch):
    op._reset_for_tests()
    store = op.OnboardingProgress(str(tmp_path / "prog.db"))
    monkeypatch.setattr(op, "get_progress", lambda path=None: store)
    yield store
    op._reset_for_tests()


def test_step_marking_flow(auth_client, _isolated_progress):
    ref = {"Referer": "http://testserver/workspace/onboarding/tiktok"}
    st = auth_client.get("/api/onboarding/tiktok/status").json()
    assert st["progress"] == {"done": 0, "total": 5} and len(st["steps"]) == 5 and st["step"] == 1
    # 第 1 步标记完成 → 当前步前移到 2；进度 1/5
    r = auth_client.post("/workspace/onboarding/tiktok/step/1", data={"state": "done"}, headers=ref, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?step=1#obg-step-1")
    st = auth_client.get("/api/onboarding/tiktok/status").json()
    assert st["steps"][0]["state"] == "done" and st["progress"]["done"] == 1 and st["step"] == 2
    # 第 2 步已提交审核，预计 0 天 → 立刻 overdue → 状态灯琥珀 + 提示
    r = auth_client.post("/workspace/onboarding/tiktok/step/2", data={"state": "submitted", "due_days": "0"}, headers=ref,
                         follow_redirects=False)
    assert r.status_code == 303
    time.sleep(0.01)
    st = auth_client.get("/api/onboarding/tiktok/status").json()
    assert st["steps"][1]["state"] == "submitted" and st["steps"][1]["overdue"] is True
    assert st["light"] == "amber" and st["hint"] == "review_overdue" and st["step"] == 2
    html = auth_client.get("/workspace/onboarding/tiktok").text
    assert "预计出结果的日子已过" in html and 'id="obg-step-2"' in html and "st-submitted" in html and "st-done" in html
    assert "已完成 1 / 5" in html and 'data-light="amber"' in html
    # 受阻 → 琥珀 step_blocked；重置 → 回 todo
    r = auth_client.post("/workspace/onboarding/tiktok/step/2", data={"state": "todo"}, headers=ref, follow_redirects=False)
    r = auth_client.post("/workspace/onboarding/tiktok/step/3", data={"state": "blocked"}, headers=ref, follow_redirects=False)
    st = auth_client.get("/api/onboarding/tiktok/status").json()
    assert st["hint"] == "step_blocked" and st["step"] == 3 and st["steps"][1]["state"] == "todo"
    # 自动检测的步骤拒绝手动：先配凭证让第 4 步变 auto(doing)
    r = auth_client.post("/workspace/onboarding/tiktok/credentials", data={"app_id": "a", "secret": "s"}, headers=ref,
                         follow_redirects=False)
    assert r.status_code == 303
    st = auth_client.get("/api/onboarding/tiktok/status").json()
    assert st["steps"][3]["auto"] is True and st["steps"][3]["state"] == "doing"
    r = auth_client.post("/workspace/onboarding/tiktok/step/4", data={"state": "done"}, headers=ref, follow_redirects=False)
    assert r.status_code == 303 and "error=step_auto" in r.headers["location"]
    # 非法
    assert auth_client.post("/workspace/onboarding/tiktok/step/9", data={"state": "done"}, headers=ref).status_code == 404
    assert auth_client.post("/workspace/onboarding/tiktok/step/1", data={"state": "weird"}, headers=ref).status_code == 400
    assert auth_client.post("/workspace/onboarding/nope/step/1", data={"state": "done"}, headers=ref).status_code == 404
    from src.integrations import account_orchestrator as ao
    ao._WORKER_FACTORIES.pop("tiktok:official", None)
