# -*- coding: utf-8 -*-
"""融合实例 P3 精简版门禁：会员中心页 + API + 顶栏档位徽章 + nav 锁标渲染。

conftest 全量 admin app；档位一律走 plan_override（不依赖宿主机真实 license）。
nav 锁标的纯逻辑在 test_feature_gate.py::test_nav_context_marks_locked_items，
这里补 HTML 渲染面（锁定项渲染为 /membership 跳转 + 徽章出现在顶栏）。
"""


def _set_gate(config_manager, enabled=True, override=None):
    lic = config_manager.config.setdefault("licensing", {})
    fg = {"enabled": enabled}
    if override:
        fg["plan_override"] = override
    lic["feature_gate"] = fg


def test_membership_api_snapshot_shape(auth_client, config_manager):
    _set_gate(config_manager, override="basic")
    r = auth_client.get("/api/admin/membership")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["gate"]["enabled"] is True
    assert d["gate"]["plan"] == "basic"
    assert "companion" in d["gate"]["locked"]
    assert "translation_suite" not in d["gate"]["locked"]
    # 渲染友好矩阵：行×档位格，与 FEATURE_MIN_PLAN 单源一致
    plans = d["matrix"]["plans"]
    assert plans == ["community", "basic", "pro", "flagship"]
    rows = {row["name"]: row for row in d["matrix"]["rows"]}
    assert rows["translation_suite"]["cells"] == [False, True, True, True]
    assert rows["companion"]["cells"] == [False, False, False, True]
    assert rows["companion"]["allowed"] is False
    # 席位/额度块存在（值随环境，形状必须在）
    assert "seats" in d and "limit" in d["seats"]
    assert "quota" in d and "included_chars" in d["quota"]


def test_membership_api_gate_off(auth_client, config_manager):
    _set_gate(config_manager, enabled=False)
    d = auth_client.get("/api/admin/membership").json()
    assert d["gate"]["enabled"] is False
    assert d["gate"]["locked"] == []


def test_membership_page_renders(auth_client, config_manager):
    """注意：_i18n_bootstrap.html 会把整包 i18n 以 JSON（\\u 转义）嵌进每页供
    window.T 用——「裸键不得出现在 html」的断言天然不成立；改断言服务端已把
    键解析成字面 CJK（JSON 里的值是 \\u 转义，字面 CJK 只能来自模板渲染）。"""
    _set_gate(config_manager, override="pro")
    r = auth_client.get("/membership")
    assert r.status_code == 200
    html = r.text
    # 矩阵标题/档位名被服务端解析成字面文本（非 \u 转义 → 来自模板而非 i18n JSON 包）
    assert ("功能矩阵" in html) or ("Feature matrix" in html)
    assert ("专业版" in html) or (">Pro<" in html)
    # 矩阵格子真渲染（✓）且 pro 档存在锁定行标（flagship 专属功能）
    assert "✓" in html
    assert ("未包含" in html) or ("Not included" in html)


def test_topbar_badge_and_nav_lock_render(auth_client, config_manager):
    """gate 开 → 顶栏出档位徽章；被锁 nav 项渲染为 /membership 锁标跳转。

    base.html 的 CSS 永远含 .plan-badge/.nav-locked 选择器文本 → 断言必须
    锚定渲染出的元素标记（class="..."），不能裸搜类名。"""
    _set_gate(config_manager, override="basic")
    html = auth_client.get("/").text
    assert 'class="plan-badge plan-badge-basic"' in html
    # basic 档：知识库(kb=pro) 应以锁标形式指向会员中心
    assert 'href="/membership" class="nav-locked"' in html


def test_topbar_badge_absent_when_gate_off(auth_client, config_manager):
    _set_gate(config_manager, enabled=False)
    html = auth_client.get("/").text
    assert 'class="plan-badge' not in html
    assert 'class="nav-locked"' not in html
