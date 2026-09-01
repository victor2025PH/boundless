# -*- coding: utf-8 -*-
"""小智「Windows 操控 runner」HTTP 客户端门禁（实施91 P1-0，2026-08-30）。

守 runner_client 的三条本子系统契约（改前先读）：
1. **目标只能来自白名单**：base_url 由 runner_pairing 换出，token 由 config
   取——client 绝不接受调用方传入裸 URL；不在白名单/被踢/无 token 一律拒发
   并给出可区分的原因（前端好给对话术）。
2. **失败诚实回落**：runner 离线/超时/坏响应一律 {ok:False,error:...}，绝不崩、
   绝不假成功（受控机是外部进程，不可信其一定在）。
3. **token 不外泄**：token 只出现在请求头，probe/inspect 的返回体绝不含它。

纯逻辑 + mock 传输（monkeypatch _http_json），非 Windows 也跑。
"""
from __future__ import annotations

import pytest

from src.assistant import runner_client as rc
from src.assistant import runner_pairing as rp


@pytest.fixture(autouse=True)
def _clean():
    rp._reset_for_test()
    yield
    rp._reset_for_test()


def _cfg(global_token="GLOBAL-T", machines=None):
    pc = {"token": global_token}
    if machines is not None:
        pc["machines"] = machines
    return {"assistant": {"pc_runner": pc}}


_WL = [
    {"id": "zhuji", "base_url": "http://127.0.0.1:18760", "label": "主机"},
    {"id": "kouxing", "base_url": "http://192.168.0.198:18760"},
]


# ── resolve_target：白名单换出 + token 解析 + 可区分拒因 ────────────────────
def test_resolve_uses_whitelist_base_and_global_token():
    rp.load_machines(_WL)
    base, token, reason = rc.resolve_target("zhuji", _cfg())
    assert reason == "ok"
    assert base == "http://127.0.0.1:18760"      # 白名单换出，非调用方传入
    assert token == "GLOBAL-T"


def test_resolve_per_machine_token_overrides_global():
    rp.load_machines(_WL)
    cfg = _cfg(machines=[{"id": "zhuji", "token": "PER-T"}])
    _base, token, reason = rc.resolve_target("zhuji", cfg)
    assert reason == "ok" and token == "PER-T"        # 逐机 token 优先
    # 未逐机配 token 的机器回落全局 token
    _b2, t2, r2 = rc.resolve_target("kouxing", cfg)
    assert r2 == "ok" and t2 == "GLOBAL-T"


def test_resolve_rejects_non_whitelisted_machine():
    rp.load_machines(_WL)
    base, token, reason = rc.resolve_target("ghost", _cfg())
    assert base is None and token == "" and reason == "not_whitelisted"


def test_resolve_distinguishes_revoked_from_absent():
    rp.load_machines(_WL)
    rp.revoke("zhuji")
    base, _token, reason = rc.resolve_target("zhuji", _cfg())
    assert base is None and reason == "revoked"       # 踢下线 vs 未配区分


def test_resolve_no_token_is_refused():
    rp.load_machines(_WL)
    base, _token, reason = rc.resolve_target("zhuji", _cfg(global_token=""))
    assert base is None and reason == "no_token"      # 无 token 拒发（不裸奔）


# ── build_headers：Bearer + actor 消毒截断 ────────────────────────────────
def test_build_headers_bearer_and_actor():
    h = rc.build_headers("SECRET", actor="user@pc:zhuji")
    assert h["Authorization"] == "Bearer SECRET"
    assert h["Content-Type"] == "application/json"
    assert h["X-PC-Actor"] == "user@pc:zhuji"


def test_build_headers_actor_truncated_and_optional():
    h = rc.build_headers("t", actor="x" * 200)
    assert len(h["X-PC-Actor"]) == 60                 # 截断防超长
    assert "X-PC-Actor" not in rc.build_headers("t")  # 不传 actor 则无此头


# ── parse_response：坏结构一律判失败 ──────────────────────────────────────
def test_parse_response_shapes():
    assert rc.parse_response(200, {"ok": True, "x": 1})["ok"] is True
    assert rc.parse_response(200, {"ok": False, "error": "e"})["ok"] is False
    # 非 dict / 缺 ok 字段 → bad_response（不冒充成功）
    assert rc.parse_response(200, "not-a-dict")["error"] == "bad_response"
    assert rc.parse_response(200, {"no_ok": 1})["error"] == "bad_response"
    assert rc.parse_response(500, None)["error"] == "bad_response"


# ── probe / inspect：成功透传 + 离线/超时诚实回落 ─────────────────────────
def test_probe_success_passthrough(monkeypatch):
    rp.load_machines(_WL)
    monkeypatch.setattr(rc, "_http_json",
                        lambda *a, **k: (200, {"ok": True, "version": "v",
                                               "uia_available": True}))
    r = rc.probe("zhuji", _cfg())
    assert r["ok"] is True and r["uia_available"] is True


def test_probe_offline_when_transport_raises(monkeypatch):
    rp.load_machines(_WL)

    def _boom(*a, **k):
        raise ConnectionRefusedError("runner down")

    monkeypatch.setattr(rc, "_http_json", _boom)
    r = rc.probe("zhuji", _cfg())
    assert r["ok"] is False and r["error"] == "offline"   # 不崩、不假成功


def test_probe_bad_machine_short_circuits(monkeypatch):
    # 未配对机器压根不该发 HTTP——传输若被调用即测试失败
    def _must_not_call(*a, **k):  # pragma: no cover - 不应触达
        raise AssertionError("resolve 失败仍发了 HTTP")

    monkeypatch.setattr(rc, "_http_json", _must_not_call)
    r = rc.probe("ghost", _cfg())
    assert r["ok"] is False and r["error"] == "not_whitelisted"


def test_inspect_timeout_falls_back(monkeypatch):
    """超时回落：受控机不响应 → offline，绝不阻塞/崩/假成功（媒体外部进程纪律）。"""
    rp.load_machines(_WL)

    def _timeout(*a, **k):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(rc, "_http_json", _timeout)
    r = rc.inspect("zhuji", "list_windows", {}, _cfg())
    assert r["ok"] is False and r["error"] == "offline"


def test_inspect_success_passthrough(monkeypatch):
    rp.load_machines(_WL)
    captured = {}

    def _ok(method, url, headers, payload, timeout):
        captured["url"] = url
        captured["payload"] = payload
        captured["auth"] = headers.get("Authorization")
        return (200, {"ok": True, "tool": "list_windows",
                      "windows": [{"title": "记事本", "foreground": True}]})

    monkeypatch.setattr(rc, "_http_json", _ok)
    r = rc.inspect("zhuji", "list_windows", {}, _cfg(), actor="u@pc:zhuji")
    assert r["ok"] is True and r["windows"][0]["title"] == "记事本"
    # 白名单换出的 base + /call，载荷携 tool/args，token 只在头里
    assert captured["url"] == "http://127.0.0.1:18760/call"
    assert captured["payload"] == {"tool": "list_windows", "args": {}}
    assert captured["auth"] == "Bearer GLOBAL-T"


def test_inspect_bad_machine_no_http(monkeypatch):
    def _must_not_call(*a, **k):  # pragma: no cover - 不应触达
        raise AssertionError("未配对仍发了 HTTP")

    monkeypatch.setattr(rc, "_http_json", _must_not_call)
    r = rc.inspect("ghost", "list_windows", {}, _cfg())
    assert r["ok"] is False and r["error"] == "not_whitelisted"


def test_token_never_leaks_into_returned_body(monkeypatch):
    """probe/inspect 返回体绝不含 token（token 只活在请求头）。"""
    rp.load_machines(_WL)
    monkeypatch.setattr(rc, "_http_json",
                        lambda *a, **k: (200, {"ok": True, "tool": "x"}))
    for fn in (lambda: rc.probe("zhuji", _cfg()),
               lambda: rc.inspect("zhuji", "list_windows", {}, _cfg())):
        body = fn()
        assert "GLOBAL-T" not in str(body)
        for k in body:
            assert "token" not in str(k).lower()
