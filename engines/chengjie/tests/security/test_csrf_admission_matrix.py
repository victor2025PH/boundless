# -*- coding: utf-8 -*-
"""CSRF 准入矩阵门禁（2026-07-31「人设切换失败」事故沉淀）。

事故链：S3 收紧 CSRF 后，写请求放行=三张通行证之一（X-CSRF-Token 配对 /
Bearer / 同源 Origin·Referer）——但通行证发放藏在 workspace_base 的页面级
fetch 补丁里，共享组件宿主（iframe App 等）一张都不带；实测 Chromium 同源
POST 不发 Origin，整条写通道只挂在 Referer 一根线上。中间件拒绝时零日志
零计数，前端把一切失败折叠成「请重试」。

本门禁把**准入矩阵五种姿势**钉成回归网（安全语义不回退），并钉住修复面：
- 403 响应必须带机器可读 ``code:"csrf"``（前端分型/自愈重试的依据）；
- 拒绝必须计入 csrf_stats（形态分类 = 诊断结论）；
- 遥测 beacon 端点豁免（sendBeacon 无法带头，否则观测通道先于业务瞎掉）；
- 安全方法响应会补种 csrf_token cookie（客户端自愈的前提）。
"""

from src.web.csrf_stats import classify_reject, get_csrf_reject_stats

_WRITE = "/api/persona/bind"
_PAYLOAD = {"scope": "conversation", "conversation_id": "telegram:a:b", "profile_id": "x"}


def _is_csrf_reject(r) -> bool:
    if r.status_code != 403:
        return False
    try:
        return (r.json() or {}).get("code") == "csrf"
    except Exception:
        return False


# ── 准入矩阵：五种请求姿势 ─────────────────────────────────────────────

def test_bare_write_rejected_with_machine_code(client):
    """裸 POST（无任何通行证）→ 403 且带 code=csrf（前端分型依据，勿删字段）。"""
    r = client.post(_WRITE, json=_PAYLOAD)
    assert r.status_code == 403
    body = r.json()
    assert body.get("code") == "csrf", f"403 须带 code=csrf，得到 {body}"
    assert body.get("detail") == "CSRF token missing or invalid"


def test_same_origin_origin_header_passes(client):
    """带同源 Origin → 过 CSRF 闸（后续 401/404 都行，但绝不能是 CSRF 403）。"""
    r = client.post(_WRITE, json=_PAYLOAD, headers={"Origin": "http://testserver"})
    assert not _is_csrf_reject(r), "同源 Origin 应通过 CSRF 闸"


def test_same_origin_referer_passes(client):
    r = client.post(_WRITE, json=_PAYLOAD,
                    headers={"Referer": "http://testserver/workspace"})
    assert not _is_csrf_reject(r), "同源 Referer 应通过 CSRF 闸"


def test_csrf_cookie_header_pair_passes(client):
    client.cookies.set("csrf_token", "tok-abc")
    r = client.post(_WRITE, json=_PAYLOAD, headers={"X-CSRF-Token": "tok-abc"})
    assert not _is_csrf_reject(r), "cookie 与头配对应通过 CSRF 闸"


def test_bearer_exempt(client):
    """Bearer 请求豁免 CSRF（桌面壳 IPC 链路）；后续鉴权 401 属正常。"""
    r = client.post(_WRITE, json=_PAYLOAD, headers={"Authorization": "Bearer whatever"})
    assert not _is_csrf_reject(r), "Bearer 请求不应被 CSRF 闸拦"


def test_cross_origin_rejected(client):
    """跨源 Origin（反代改写 Host / 恶意站点）→ 必拒（安全语义不回退）。"""
    r = client.post(_WRITE, json=_PAYLOAD,
                    headers={"Origin": "http://evil.example"})
    assert _is_csrf_reject(r), "跨源 Origin 必须被 CSRF 闸拦下"


def test_pair_mismatch_with_same_origin_referer_still_passes(client):
    """头与 cookie 不配对但 Referer 同源 → 仍应放行（配对失败落到同源回落，
    不比「没带头」更糟——否则页面补丁读到陈旧 cookie 反而把好请求打死）。"""
    client.cookies.set("csrf_token", "tok-abc")
    r = client.post(_WRITE, json=_PAYLOAD,
                    headers={"X-CSRF-Token": "stale-tok",
                             "Referer": "http://testserver/workspace"})
    assert not _is_csrf_reject(r)


# ── 遥测豁免：beacon 无法自定义头，观测通道必须先于业务活着 ──────────────

def test_telemetry_beacon_paths_exempt(client):
    for p in ("/api/telemetry/frontend-error", "/api/telemetry/ui-event"):
        r = client.post(p, json={"page": "/x", "fn": "f", "type": "Error"})
        assert not _is_csrf_reject(r), f"{p} 应豁免 CSRF（sendBeacon 带不了头）"


# ── 拒绝必留痕：计数 + 形态分类 ───────────────────────────────────────

def test_rejects_are_counted_with_kind(client):
    stats = get_csrf_reject_stats()
    stats.reset()
    client.post(_WRITE, json=_PAYLOAD)                                    # bare
    client.post(_WRITE, json=_PAYLOAD, headers={"Origin": "http://evil"})  # origin_mismatch
    client.cookies.set("csrf_token", "tok-abc")
    client.post(_WRITE, json=_PAYLOAD)                                    # cookie_no_header（事故形态）
    client.cookies.delete("csrf_token")
    d = stats.dump()
    assert d["total"] == 3
    assert d["by_kind"].get("bare") == 1
    assert d["by_kind"].get("origin_mismatch") == 1
    assert d["by_kind"].get("cookie_no_header") == 1
    assert "/api/persona/bind" in d["by_path"]
    prom = stats.dump_prom()
    assert "csrf_rejects_total 3" in prom
    assert 'csrf_rejects_by_kind_total{kind="cookie_no_header"} 1' in prom


def test_classify_reject_pure():
    assert classify_reject(had_cookie=True, had_header=True) == "pair_mismatch"
    assert classify_reject(had_cookie=True, had_header=False) == "cookie_no_header"
    assert classify_reject(had_cookie=False, had_header=False,
                           origin="http://x") == "origin_mismatch"
    assert classify_reject(had_cookie=False, had_header=False,
                           referer="http://x/") == "referer_mismatch"
    assert classify_reject(had_cookie=False, had_header=False) == "bare"


def test_reject_path_digits_masked(client):
    """path 数字段掩码：/api/xx/12345 与 /54321 归并一键（控基数防撑爆）。"""
    stats = get_csrf_reject_stats()
    stats.reset()
    client.post("/api/anything/12345/act", json={})
    client.post("/api/anything/98765/act", json={})
    d = stats.dump()
    assert d["by_path"].get("/api/anything/<n>/act") == 2


# ── 客户端自愈的前提：安全方法响应补种 csrf_token cookie ─────────────────

def test_safe_method_seeds_csrf_cookie(client):
    client.cookies.delete("csrf_token")
    r = client.get("/login")
    setc = r.headers.get("set-cookie", "")
    assert "csrf_token=" in setc, (
        "GET 响应须补种 csrf_token（copilot-client 缺 cookie 自愈依赖此行为）")


# ── 放行侧留痕（P2 收口决策数据面）：每张通行证的准入都计数 ────────────────

def test_admits_counted_by_ticket(client):
    stats = get_csrf_reject_stats()
    stats.reset()
    client.post(_WRITE, json=_PAYLOAD,
                headers={"Origin": "http://testserver"})                  # origin
    client.post(_WRITE, json=_PAYLOAD,
                headers={"Referer": "http://testserver/workspace"})       # referer
    client.cookies.set("csrf_token", "tok-adm")
    client.post(_WRITE, json=_PAYLOAD, headers={"X-CSRF-Token": "tok-adm"})  # pair
    client.cookies.delete("csrf_token")
    client.post(_WRITE, json=_PAYLOAD,
                headers={"Authorization": "Bearer whatever"})             # bearer
    d = stats.dump()
    assert d["admitted_by"] == {
        "csrf_pair": 1, "bearer": 1, "origin": 1, "referer": 1}
    prom = stats.dump_prom()
    assert 'csrf_admits_total{ticket="referer"} 1' in prom


# ── 浏览器体检探针（P2）：echo 回显通行证 / summary 契约 ──────────────────

def test_echo_goes_through_full_csrf_chain(client):
    """echo 的价值＝真实过闸：无凭证裸打必须被 CSRF 拦（绝不豁免）。"""
    r = client.post("/api/preflight/echo", json={})
    assert _is_csrf_reject(r), "echo 不得豁免 CSRF——否则探针测不出写通道问题"


def test_echo_reports_admission_ticket(auth_client):
    # auth_client 默认带 Bearer → ticket=bearer
    r = auth_client.post("/api/preflight/echo", json={})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["ticket"] == "bearer"
    assert d["had"]["bearer"] is True
    # 压掉 Bearer、改走 cookie 配对 → ticket=csrf_pair（体检页浏览器态的正路）
    auth_client.cookies.set("csrf_token", "tok-echo")
    r2 = auth_client.post("/api/preflight/echo", json={},
                          headers={"Authorization": "", "X-CSRF-Token": "tok-echo"})
    assert r2.status_code == 200 and r2.json()["ticket"] == "csrf_pair"
    # 只剩同源 Referer 回落 → ticket=referer（体检页应给 warn 的形态）
    r3 = auth_client.post("/api/preflight/echo", json={},
                          headers={"Authorization": "",
                                   "Referer": "http://testserver/workspace"})
    auth_client.cookies.delete("csrf_token")
    assert r3.status_code == 200 and r3.json()["ticket"] == "referer"


def test_preflight_summary_contract(auth_client):
    r = auth_client.get("/api/preflight/summary")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert float(d["server_ts"]) > 0
    assert isinstance(d["build"], str) and d["build"], "构建戳应读自 ui-build.txt 首行"
