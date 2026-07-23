# -*- coding: utf-8 -*-
"""C0-3b 档位闸门 API 中间件端到端门禁（融合实例 P1）。

用 conftest 全量 admin app：中间件先于路由/鉴权 —— 档位未解锁的功能族
未登录也直接 403 feature_locked；解锁后同一路径回到常规链路（401/404 等，
绝不是 feature_locked）。矩阵全部走 plan_override（不依赖宿主机真实 license）。
"""


def _set_gate(config_manager, enabled=True, override=None):
    lic = config_manager.config.setdefault("licensing", {})
    fg = {"enabled": enabled}
    if override:
        fg["plan_override"] = override
    lic["feature_gate"] = fg


def _is_locked(resp):
    if resp.status_code != 403:
        return False
    try:
        return resp.json().get("error") == "feature_locked"
    except Exception:
        return False


# 功能族代表路径（路径存在与否无所谓：中间件在路由匹配之前拦截）
_FAMILY_PATHS = {
    "kb": "/api/kb/entries",
    "personas": "/api/personas/p1/media",
    "care": "/api/care/anything",
    "companion": "/api/companion/capabilities",
    "voice_clone": "/api/voice/avatar-status",
    "rpa": "/api/line-rpa/status",
    "monetization": "/api/monetize/grant",
}


def test_gate_off_locks_nothing(client, config_manager):
    _set_gate(config_manager, enabled=False)
    for path in _FAMILY_PATHS.values():
        assert not _is_locked(client.get(path)), path


def test_basic_locks_pro_and_flagship_families(client, config_manager):
    _set_gate(config_manager, override="basic")
    for feat, path in _FAMILY_PATHS.items():
        r = client.get(path)
        assert _is_locked(r), (feat, path, r.status_code)
        body = r.json()
        assert body["feature"] == feat
        assert body["detail"]  # i18n 文案非空（err.lic.feature_locked）
    # 非守卫面（工作台/草稿）不受影响
    assert not _is_locked(client.get("/api/workspace/me"))
    assert not _is_locked(client.get("/api/drafts/pending"))


def test_pro_unlocks_pro_families_keeps_flagship_locked(client, config_manager):
    _set_gate(config_manager, override="pro")
    for feat in ("kb", "personas", "care"):
        r = client.get(_FAMILY_PATHS[feat])
        assert not _is_locked(r), (feat, r.status_code)
    for feat in ("companion", "voice_clone", "rpa", "monetization"):
        r = client.get(_FAMILY_PATHS[feat])
        assert _is_locked(r), (feat, r.status_code)


def test_flagship_locks_nothing(client, config_manager):
    _set_gate(config_manager, override="flagship")
    for feat, path in _FAMILY_PATHS.items():
        r = client.get(path)
        assert not _is_locked(r), (feat, path, r.status_code)


def test_locked_error_is_localized(client, config_manager):
    _set_gate(config_manager, override="community")
    zh = client.get("/api/kb/entries?lang=zh").json()["detail"]
    en = client.get("/api/kb/entries?lang=en").json()["detail"]
    assert zh != "err.lic.feature_locked" and en != "err.lic.feature_locked"
    assert zh != en  # zh/en 双语齐备且随请求语言切换
