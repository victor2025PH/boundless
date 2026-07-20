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
