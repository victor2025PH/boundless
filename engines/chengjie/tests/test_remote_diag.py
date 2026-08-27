# -*- coding: utf-8 -*-
"""远程诊断拉取（check_remote_diag）门禁。

背景（2026-08-21 值守）：内测用户不会走「设置→一键上传→回读短码」，B14 的
诊断码催了一上午。本功能=客服在官网登记取包请求，客户端轮询看到后自动打包
上传（与手点同一实现同一打码），用户零操作。钉住的行为：
- 无请求 → 不上传；有请求 → 上传 + ack 回执带短码；
- 同一 request_id 进程内只执行一次（防服务端销记失败导致的重复上传）；
- 上传失败 → request_id 不吞（下轮可重试）；
- refresh_once 每轮顺带轻询（掉了它整个功能就只剩启动后那一发）。
"""
import pytest

import src.ai.hosted_gateway as hg


class _CM:
    config = {"licensing": {"hosted_ai": {"enabled": True}}}
    config_path = ""


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(hg, "_diag_handled", set())
    monkeypatch.setattr(
        "src.licensing.machine_bridge.machine_fingerprint", lambda: "AAAA-BBBB")
    yield


def _fetch_factory(pending, calls):
    def _f(url, method="GET", body=None, bearer=""):
        calls.append((method, url, body))
        if method == "GET" and "/api/diag-request" in url:
            if pending:
                return {"ok": True, "requested": True, "request_id": "dr-1"}
            return {"ok": True, "requested": False}
        return {"ok": True}
    return _f


def test_no_pending_request_no_upload(monkeypatch):
    calls = []
    uploaded = []

    async def _fake_upload(cm, note=""):
        uploaded.append(note)
        return {"ok": True, "code": "ABC234"}

    monkeypatch.setattr("src.utils.diag_upload.build_and_upload", _fake_upload)
    assert hg.check_remote_diag(_CM(), fetch=_fetch_factory(False, calls)) is False
    assert not uploaded


def test_pending_request_uploads_and_acks(monkeypatch):
    calls = []
    uploaded = []

    async def _fake_upload(cm, note=""):
        uploaded.append(note)
        return {"ok": True, "code": "ABC234"}

    monkeypatch.setattr("src.utils.diag_upload.build_and_upload", _fake_upload)
    assert hg.check_remote_diag(_CM(), fetch=_fetch_factory(True, calls)) is True
    assert uploaded == ["remote-request:dr-1"]
    acks = [c for c in calls if c[0] == "POST"]
    assert len(acks) == 1
    assert acks[0][2] == {"action": "ack", "fp": "AAAA-BBBB",
                          "request_id": "dr-1", "code": "ABC234"}
    # 服务端销记失败（下轮仍回 pending dr-1）→ 进程内去重，不再二次上传
    assert hg.check_remote_diag(_CM(), fetch=_fetch_factory(True, calls)) is False
    assert len(uploaded) == 1


def test_upload_failure_is_retryable(monkeypatch):
    calls = []
    attempts = []

    async def _fail_upload(cm, note=""):
        attempts.append(note)
        return {"ok": False, "error": "upstream_unreachable"}

    monkeypatch.setattr("src.utils.diag_upload.build_and_upload", _fail_upload)
    assert hg.check_remote_diag(_CM(), fetch=_fetch_factory(True, calls)) is False
    assert hg.check_remote_diag(_CM(), fetch=_fetch_factory(True, calls)) is False
    assert len(attempts) == 2, "上传失败必须可重试（request_id 不得被吞）"


def test_not_hosted_short_circuits():
    class _Plain:
        config = {"licensing": {"hosted_ai": {"enabled": False}}}
        config_path = ""

    def _boom(url, method="GET", body=None, bearer=""):
        raise AssertionError("非托管形态不得发诊断轮询")

    assert hg.check_remote_diag(_Plain(), fetch=_boom) is False


def test_refresh_once_polls_remote_diag(monkeypatch):
    """refresh_once 必须每轮顺带轻询（掉了它=功能只剩启动首查一发）。"""
    hit = []
    monkeypatch.setattr(hg, "check_remote_diag", lambda cm: hit.append(1))
    for name in ("ensure_hosted_ai", "ensure_hosted_telegram",
                 "ensure_hosted_vision", "ensure_hosted_voice",
                 "ensure_hosted_asr"):
        monkeypatch.setattr(hg, name, lambda cm: True)
    hg.refresh_once(_CM())
    assert hit == [1]


# ── 实施51：额度轮询捎带 diag_requested 位（远程拉取时延 1h → ~1min）────────────


class _TrialCM:
    config = {"licensing": {"hosted_ai": {"enabled": True}},
              "ai": {"_hosted_trial": True}}
    config_path = ""


def _quota_env(monkeypatch):
    """quota_probe 的最小环境：有令牌、缓存清零、绑定码读取免磁盘。"""
    monkeypatch.setattr(hg, "_quota_cache", {"ts": 0.0, "data": None})
    monkeypatch.setattr(hg, "_state_path", lambda cm: "x")
    monkeypatch.setattr(hg, "_load_cached", lambda p: {"token": "cx.t"})
    monkeypatch.setattr(
        "src.licensing.trial_claim_client.load_state", lambda cfg: {})


def test_quota_diag_flag_kicks_remote_diag(monkeypatch):
    _quota_env(monkeypatch)
    kicked = []
    monkeypatch.setattr(hg, "kick_remote_diag_async",
                        lambda cm: kicked.append(1) or True)
    out = hg.quota_probe(_TrialCM(), fetch=lambda u, m, b: {
        "ok": True, "used": 1, "budget": 10, "remaining": 9,
        "diag_requested": True})
    assert out["enabled"] is True and kicked == [1]


def test_quota_without_flag_never_kicks(monkeypatch):
    _quota_env(monkeypatch)
    kicked = []
    monkeypatch.setattr(hg, "kick_remote_diag_async",
                        lambda cm: kicked.append(1) or True)
    out = hg.quota_probe(_TrialCM(), fetch=lambda u, m, b: {
        "ok": True, "used": 1, "budget": 10, "remaining": 9})
    assert out["enabled"] is True and kicked == []


def test_kick_is_nonblocking_and_deduped(monkeypatch):
    """在途闸：上传没结束时的后续 tick 不再起线程；结束后可再触发。"""
    import threading as _t
    gate = _t.Event()
    ran = []

    def _slow_check(cm):
        ran.append(1)
        gate.wait(timeout=5)

    monkeypatch.setattr(hg, "check_remote_diag", _slow_check)
    assert hg.kick_remote_diag_async(_CM()) is True   # 起了线程
    assert hg.kick_remote_diag_async(_CM()) is False  # 在途 → 拒绝叠加
    gate.set()
    # 等锁释放（后台线程 finally release）
    for _ in range(100):
        if hg._diag_kick_lock.acquire(blocking=False):
            hg._diag_kick_lock.release()
            break
        import time as _time
        _time.sleep(0.02)
    assert hg.kick_remote_diag_async(_CM()) is True   # 释放后可再触发
    gate.set()
    assert len(ran) == 2
    # 收尾：等第二发线程退出，别把锁留在持有态污染后续用例
    for _ in range(100):
        if hg._diag_kick_lock.acquire(blocking=False):
            hg._diag_kick_lock.release()
            break
        import time as _time
        _time.sleep(0.02)
