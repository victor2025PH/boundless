# -*- coding: utf-8 -*-
"""D1 门禁：静态壳 i18n bundle 端点（/api/i18n/bundle）。

不变量：
- prefix 必填且每段 ≥4 字符——禁空/短前缀把整本词典倾倒出去；
- 键数硬上限 800（切片语义）；
- lang 三值收敛（非法回 zh），显式 lang 优先于请求语言；
- 内容与 get_translations 合并视图逐键一致（单一事实源，不是第二份词典）。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.web.routes.i18n_bundle_routes import register_i18n_bundle_routes


def _client():
    app = FastAPI()
    register_i18n_bundle_routes(app, api_auth=lambda request: None)
    return TestClient(app)


def test_prefix_required_and_min_length():
    c = _client()
    assert c.get("/api/i18n/bundle").status_code == 400
    assert c.get("/api/i18n/bundle?prefix=").status_code == 400
    # 全部前缀过短 → 400（防 "a," 这类变相全量）
    assert c.get("/api/i18n/bundle?prefix=a,bc").status_code == 400


def test_goal_bundle_matches_merged_dict():
    from src.web.web_i18n import get_translations
    c = _client()
    r = c.get("/api/i18n/bundle?prefix=inbox.goal.&lang=zh")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["lang"] == "zh"
    keys = d["keys"]
    assert keys, "inbox.goal.* 应有词条（cp-goal 的整个文案面）"
    assert all(k.startswith("inbox.goal.") for k in keys)
    merged = get_translations("zh")
    for k, v in list(keys.items())[:50]:
        assert merged.get(k) == v, f"bundle 与合并视图不一致: {k}"
    # 桌面目标卡的关键键必须在切片里（shim 就绪判据）
    assert "inbox.goal.title" in keys


def test_lang_variants_and_fallback():
    c = _client()
    zh = c.get("/api/i18n/bundle?prefix=inbox.goal.&lang=zh").json()["keys"]
    en = c.get("/api/i18n/bundle?prefix=inbox.goal.&lang=en").json()["keys"]
    assert zh.get("inbox.goal.title") != en.get("inbox.goal.title"), "zh/en 应不同文案"
    bad = c.get("/api/i18n/bundle?prefix=inbox.goal.&lang=xx").json()
    assert bad["lang"] == "zh", "非法 lang 收敛回 zh"


def test_multi_prefix_and_cap():
    c = _client()
    r = c.get("/api/i18n/bundle?prefix=inbox.goal.,inbox.pill.")
    assert r.status_code == 200
    keys = r.json()["keys"]
    assert any(k.startswith("inbox.pill.") for k in keys)
    assert len(keys) <= 800
