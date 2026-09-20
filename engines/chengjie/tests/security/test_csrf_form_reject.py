"""S3 回归门禁：CSRF 对非 JSON（表单/multipart）写请求校验失败也拒绝。

历史缺陷：csrf_middleware 仅对 application/json 写请求返回 403，其余（表单编码）
在 token / 同源校验失败后仍放行，导致基于 session 的表单端点可被 CSRF。
同时验证：未认证引导入口 /login /logout /setup 仍豁免（不误伤登录/首装流程）。
"""
from starlette.testclient import TestClient


def test_form_write_without_csrf_is_rejected(client):
    """未带 Bearer / CSRF token / 同源 Origin 的表单写 → 403（关闭旧放行旁路）。"""
    r = client.post(
        "/users/create",
        data={"username": "evil", "password": "x", "role": "master"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 403, f"表单写在无 CSRF 时应 403，得到 {r.status_code}"


def test_multipart_write_without_csrf_is_rejected(client):
    """multipart 表单写同样应被拦。"""
    r = client.post("/templates/update", files={"f": ("a.txt", b"x")})
    assert r.status_code == 403, f"multipart 写在无 CSRF 时应 403，得到 {r.status_code}"


def test_login_endpoint_exempt(client):
    """/login 表单 POST（未认证引导入口）不应被 CSRF 挡（否则击穿登录 fixture/流程）。"""
    r = client.post("/login", data={"auth_token": "wrong"}, follow_redirects=False)
    assert r.status_code != 403, "/login 应豁免 CSRF（得到 403，将击穿登录）"


def test_json_write_without_csrf_still_rejected(client):
    """JSON 写在无 CSRF/Bearer 时仍应 403（既有行为不回退）。"""
    r = client.post("/api/change-password", json={"old_password": "a", "new_password": "b"})
    assert r.status_code == 403, f"JSON 写无 CSRF 应保持 403，得到 {r.status_code}"


def test_order_hook_webhook_exempt_from_csrf(client):
    """外部订单回流 webhook 必须穿透 CSRF 中间件到达路由本体。

    真机事故（2026-07-27）：路由级 token 鉴权设计正确，但全站 CSRF 中间件
    先一步 403 掉一切无 Bearer/无 cookie 的外部 POST——隔离路由测试全绿、
    生产 webhook 全灭。此测试用**完整 admin app**（带中间件）钉死豁免口。
    功能未开时路由自答 403（detail=功能未启用），与 CSRF 的 detail 可区分。
    """
    r = client.post("/api/goals/order-hook", json={"ref": "x"})
    detail = ""
    try:
        detail = str((r.json() or {}).get("detail") or "")
    except Exception:
        pass
    assert detail != "CSRF token missing or invalid", \
        "order-hook 被 CSRF 中间件拦截，webhook 无法到达路由（豁免口回退）"
