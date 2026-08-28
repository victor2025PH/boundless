# -*- coding: utf-8 -*-
"""协议侧车窄令牌作用域门禁（2026-08-28 P1-6 第一步）。

背景：四个 Node 侧车（whatsapp-baileys / messenger-web / instagram-web /
zalo-personal）此前在 start.ps1 里读的是 **admin auth_token**——`_api_auth` 里
`Bearer <auth_token>` 匹配即 return，等于给协议进程发了管理员钥匙；而它们真正
需要的只有 `/api/internal/*`（11 个端点，实际打的是 protocol/ingest 与
session-status）。任何能读到侧车环境变量/启动脚本的人，拿到的都是全站 API 权限。

本批只做**引擎侧**：新增可选 `web_admin.worker_token`，命中它只放行
`/api/internal/*`，越界一律 403。留空＝与改动前逐字节等价，因此可以随任意重启
装载而零风险。

⚠ 切换顺序不可颠倒（本文件也是那份运行手册的可执行版本）：
    1. 配 worker_token → **重启引擎**（此后引擎同时接受 admin 与 worker 两把）；
    2. 确认 1 生效后，再改 services/*/start.ps1 下发新令牌 → 重启四个侧车；
    3. （可选收尾）确认侧车全切后，再考虑收窄 admin 令牌对 /api/internal/* 的放行。
   反过来做（先切侧车）会让入站消息在引擎认识新令牌之前全部 401 —— 四个平台
   的客户消息直接停止进入。

判据说明：内部端点都是 POST 且要求特定 body，鉴权通过后会落到 422/400。故本文件
一律断言**状态码类别**（401=没认证 / 403=认证了但越权 / 其余=鉴权已放行），
不依赖业务返回，避免与他线改 body 契约时脆断。
"""
from __future__ import annotations

import asyncio

import pytest
import yaml
from fastapi.testclient import TestClient

from src.utils.config_manager import ConfigManager
from src.web.admin import create_app

ADMIN_TOKEN = "test-token-123"
WORKER_TOKEN = "wk-" + "b7f3a91c4e2d8065" * 2   # 35 chars，过长度闸

INTERNAL_PATH = "/api/internal/protocol/session-status"
ADMIN_PATH = "/api/admin/alert-link-status"


def _client(tmp_path, **web_admin_extra) -> TestClient:
    cfg = {
        "telegram": {"api_id": "111", "api_hash": "abc", "phone_number": "+1"},
        "ai": {"api_key": "test"},
        "skills": {"enabled": []},
        "web_admin": {
            "secret_key": "test-secret-very-long-key-for-testing",
            "auth_token": ADMIN_TOKEN,
            "session_max_age": 3600,
            **web_admin_extra,
        },
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    cm = ConfigManager(str(p))
    asyncio.run(cm.load())
    app = create_app(cm, audit_store=None, boot_ts=0, telegram_client=None,
                     event_tracker=None, log_buffer=None)
    return TestClient(app)


def _post_internal(c: TestClient, token: str):
    return c.post(INTERNAL_PATH, headers={"Authorization": f"Bearer {token}"}, json={})


def _get_admin(c: TestClient, token: str):
    return c.get(ADMIN_PATH, headers={"Authorization": f"Bearer {token}"})


# ── 1. 未配置：必须与改动前逐字节等价 ──────────────────────────────────
class TestNotConfigured:
    def test_admin_token_still_works_everywhere(self, tmp_path):
        c = _client(tmp_path)
        assert _post_internal(c, ADMIN_TOKEN).status_code not in (401, 403)
        assert _get_admin(c, ADMIN_TOKEN).status_code not in (401, 403)

    def test_unknown_token_rejected_everywhere(self, tmp_path):
        c = _client(tmp_path)
        assert _post_internal(c, WORKER_TOKEN).status_code == 401
        assert _get_admin(c, WORKER_TOKEN).status_code == 401


# ── 2. 已配置：这组断言就是「隔离」本身 ────────────────────────────────
class TestConfigured:
    def test_worker_token_allowed_on_internal(self, tmp_path):
        c = _client(tmp_path, worker_token=WORKER_TOKEN)
        assert _post_internal(c, WORKER_TOKEN).status_code not in (401, 403)

    def test_worker_token_forbidden_outside_internal(self, tmp_path):
        """核心不变量：作用域被改坏（比如无条件 return）时本条先红。"""
        c = _client(tmp_path, worker_token=WORKER_TOKEN)
        r = _get_admin(c, WORKER_TOKEN)
        assert r.status_code == 403, (
            f"worker 令牌越界访问管理端点应 403，实得 {r.status_code}")

    def test_admin_token_unaffected(self, tmp_path):
        """向后兼容：配了 worker_token 之后 admin 令牌照样全通。"""
        c = _client(tmp_path, worker_token=WORKER_TOKEN)
        assert _post_internal(c, ADMIN_TOKEN).status_code not in (401, 403)
        assert _get_admin(c, ADMIN_TOKEN).status_code not in (401, 403)

    def test_unknown_token_still_401(self, tmp_path):
        c = _client(tmp_path, worker_token=WORKER_TOKEN)
        r = _post_internal(c, "some-other-token-that-is-long-enough-xx")
        assert r.status_code == 401


# ── 3. 三道自检：坏配置一律降级为「未配置」，绝不带病放行 ──────────────
class TestBadConfigDowngrades:
    def test_same_as_admin_token_gives_no_isolation_but_no_403(self, tmp_path):
        """与 admin 同值＝零隔离；此时应回落成「就是 admin 令牌」而不是造出 403。"""
        c = _client(tmp_path, worker_token=ADMIN_TOKEN)
        assert _get_admin(c, ADMIN_TOKEN).status_code not in (401, 403)

    @pytest.mark.parametrize("bad", [
        "short-token",                       # < 24
        "CHANGE_ME_worker_token_placeholder",  # 占位符
        "YOUR_WORKER_TOKEN_GOES_HERE_XXXX",    # 占位符
        "   ",                                # 空白
    ])
    def test_bad_worker_token_is_not_accepted(self, tmp_path, bad):
        c = _client(tmp_path, worker_token=bad)
        assert _post_internal(c, bad.strip() or "x").status_code == 401

    def test_bad_worker_token_does_not_break_admin(self, tmp_path):
        """坏配置不得连累主令牌（降级要安静且无副作用）。"""
        c = _client(tmp_path, worker_token="CHANGE_ME")
        assert _post_internal(c, ADMIN_TOKEN).status_code not in (401, 403)
        assert _get_admin(c, ADMIN_TOKEN).status_code not in (401, 403)


# ── 4. 覆盖面自证：worker 令牌放行的就是侧车真正需要的那一组 ────────────
def test_scope_covers_every_internal_endpoint(tmp_path):
    """11 个 /api/internal/* 端点必须全部在 worker 作用域内。

    如果将来有人把内部端点挪到别的前缀，侧车会在切换后静默 403 —— 本条把
    「作用域前缀」与「实际注册的内部端点集合」钉在一起。
    """
    c = _client(tmp_path, worker_token=WORKER_TOKEN)
    paths = sorted({
        r.path for r in c.app.routes
        if getattr(r, "path", "").startswith("/api/internal/")
    })
    assert paths, "一个 /api/internal/* 端点都没注册？先查路由装配"
    for p in paths:
        r = c.post(p, headers={"Authorization": f"Bearer {WORKER_TOKEN}"}, json={})
        assert r.status_code != 403, f"{p} 落在 worker 作用域外"
        assert r.status_code != 401, f"{p} 未接受 worker 令牌"
