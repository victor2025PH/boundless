"""S6 回归门禁：变现 webhook 必须配 secret；/api/setup/test-ai 已有用户时需登录。"""


def test_monetize_webhook_rejects_when_no_secret(auth_client):
    """未配置 monetization.webhook_secret 时，webhook 直接拒绝（不再默认接受记账）。"""
    r = auth_client.post(
        "/api/monetize/webhook",
        json={"contact_key": "c1", "kind": "subscribe", "item_id": "x", "ref": "r1"},
    )
    # 端点存在时应返回未配置；若变现模块整体关闭返回 404 也可接受（都不等于「成功记账」）。
    if r.status_code == 200:
        body = r.json()
        assert body.get("ok") is False
        assert body.get("reason") == "webhook_secret_not_configured", body
    else:
        assert r.status_code in (401, 403, 404), r.status_code


def test_setup_test_ai_requires_login_when_users_exist(client, config_dir):
    """已存在用户时，未登录访问 /api/setup/test-ai → 401（防外部代刷 LLM）。

    注：conftest 的 create_app 已用非空 auth_token 播种 master → user_count>0。
    这里用未认证的 `client`（无 session、无 Bearer）。
    """
    r = client.post(
        "/api/setup/test-ai",
        json={"api_key": "sk-test", "base_url": "https://example.com", "model": "x"},
    )
    # 未登录 → 被鉴权(401) 或 CSRF(403) 拦下；关键是不得进入真实外呼（非 200）。
    assert r.status_code in (401, 403), f"未登录 test-ai 应被拦，得到 {r.status_code}"
