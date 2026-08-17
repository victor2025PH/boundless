# -*- coding: utf-8 -*-
"""C3 门禁：工作流模块 feature flag ``inbox.workflows.enabled`` + C1/C2 配套。

flag 语义不变量：
- **默认开**（键缺失 / config_manager 缺失 / 读取异常）——出货默认零行为变化；
- 关闭时链域端点（列链/CRUD/种子/执行列表/漏斗/会话执行/取消/启动）一律 403；
- 关闭只拦「新动作」：语义上在途执行不中断（Runner 不受闸，此处按路由面回归）。
（原「NBA 面不受影响 / execute-action 链分支软拒」不变量已随「AI 下一步」面板
 2026-08-14 整体下线删除——那些端点不复存在。）

C2 goal_id 透传不变量：
- start-chain 带 goal_id → 落 workflow_executions.context_json（漏斗归因地基）；
- 不带 → context 无该键（零行为变化）；超长截 64。

C1 推荐映射静态一致性：
- cp-chain-exec.js 里的 模板→种子链 映射，模板 id 必须真实存在于 goals 模板注册表、
  链 id 必须真实存在于种子包——防两边各自演化后推荐指向幽灵。
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from src.inbox.store import InboxStore
from src.inbox.workflow_starter import STARTER_CHAINS, ensure_starter_chains
from src.web.routes.unified_inbox_workflow_routes import register_workflow_routes

_REPO = Path(__file__).resolve().parents[1]


def _api_auth(request: Request):
    """带类型标注（Depends 场景下裸 lambda 的 request 会被当查询参数 → 422）。"""
    return None


def _client(tmp_path, cfg=None, with_cm=True):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    register_workflow_routes(app, api_auth=_api_auth)
    app.state.inbox_store = InboxStore(tmp_path / "inbox.db")
    if with_cm:
        app.state.config_manager = SimpleNamespace(
            config=cfg if cfg is not None else {})
    return TestClient(app)


_OFF = {"inbox": {"workflows": {"enabled": False}}}

# 链域端点全集（method, path, json body）——flag 关必须全部 403
_CHAIN_ENDPOINTS = [
    ("GET", "/api/workspace/workflow-chains", None),
    ("POST", "/api/workspace/workflow-chains", {"name": "x", "steps": []}),
    ("PUT", "/api/workspace/workflow-chains/c1", {"name": "x"}),
    ("DELETE", "/api/workspace/workflow-chains/c1", None),
    ("POST", "/api/workspace/workflow-chains/seed", {}),
    ("GET", "/api/workspace/chain-executions", None),
    ("GET", "/api/workspace/chain-funnel", None),
    ("GET", "/api/workspace/conv/tg:a:c1/chain-executions", None),
    ("POST", "/api/workspace/chain-executions/e1/cancel", {}),
    ("POST", "/api/workspace/conv/tg:a:c1/start-chain", {"chain_id": "c1"}),
]


# ── flag 默认开（三种缺省形态） ─────────────────────────────────────────────

def test_default_on_without_config_manager(tmp_path):
    c = _client(tmp_path, with_cm=False)
    assert c.get("/api/workspace/workflow-chains").status_code == 200
    assert c.get("/api/workspace/chain-funnel").status_code == 200


def test_default_on_with_empty_config(tmp_path):
    c = _client(tmp_path, cfg={"inbox": {}})
    assert c.get("/api/workspace/workflow-chains").status_code == 200


def test_default_on_with_explicit_true(tmp_path):
    c = _client(tmp_path, cfg={"inbox": {"workflows": {"enabled": True}}})
    r = c.post("/api/workspace/workflow-chains/seed")
    assert r.status_code == 200 and r.json()["ok"] is True


# ── flag 关：链域全 403 ────────────────────────────────────────────────────

def test_off_blocks_all_chain_endpoints(tmp_path):
    c = _client(tmp_path, cfg=_OFF)
    for method, path, body in _CHAIN_ENDPOINTS:
        if body is None:
            r = c.request(method, path)
        else:
            r = c.request(method, path, json=body)
        assert r.status_code == 403, f"{method} {path} 应 403，实际 {r.status_code}"
        assert r.json().get("detail"), f"{method} {path} 403 须带 i18n detail"


# ── C2：start-chain goal_id 透传 ───────────────────────────────────────────

def _start(c, conv, body):
    r = c.post(f"/api/workspace/conv/{conv}/start-chain", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True, d
    return d["exec_id"]


def test_start_chain_persists_goal_id_in_context(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.inbox_store
    ensure_starter_chains(store)
    cid = STARTER_CHAINS[0]["chain_id"]
    eid = _start(c, "tg:a:g1", {"chain_id": cid, "goal_id": "goal-abc"})
    ctx = json.loads(store.get_workflow_execution(eid)["context_json"] or "{}")
    assert ctx.get("goal_id") == "goal-abc"


def test_start_chain_without_goal_id_unchanged(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.inbox_store
    ensure_starter_chains(store)
    eid = _start(c, "tg:a:g2", {"chain_id": STARTER_CHAINS[1]["chain_id"]})
    ctx = json.loads(store.get_workflow_execution(eid)["context_json"] or "{}")
    assert "goal_id" not in ctx


def test_start_chain_goal_id_capped_at_64(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.inbox_store
    ensure_starter_chains(store)
    eid = _start(c, "tg:a:g3",
                 {"chain_id": STARTER_CHAINS[2]["chain_id"], "goal_id": "x" * 200})
    ctx = json.loads(store.get_workflow_execution(eid)["context_json"] or "{}")
    assert ctx.get("goal_id") == "x" * 64


# ── C1：推荐映射静态一致性（JS ↔ 两侧注册表） ─────────────────────────────
# 单一事实源已收敛到 sidebar-chrome.js 的 CopilotShared.GOAL_CHAIN_REC
# （cp-goal / cp-chain-exec 两消费方经 CopilotShared 引用，不再各写本地副本），
# 本门禁跟随解析新源；不变量不变：映射两端必须真实存在 + 唤回配对钉死。

def _js_rec_map():
    src = (_REPO / "shared" / "copilot" / "sidebar-chrome.js").read_text(
        encoding="utf-8")
    m = re.search(r"GOAL_CHAIN_REC\s*=\s*\{(.*?)\}", src, re.S)
    assert m, "sidebar-chrome.js 缺 GOAL_CHAIN_REC 映射（C1 推荐的单一事实源）"
    pairs = re.findall(r"([a-z_]+)\s*:\s*\"([a-z0-9_]+)\"", m.group(1))
    assert pairs, "GOAL_CHAIN_REC 为空"
    return dict(pairs)


def test_rec_map_consumers_reference_shared_source():
    """两消费方必须引用 CopilotShared.GOAL_CHAIN_REC 而非本地副本（防双源回潮）。"""
    for name in ("cp-chain-exec.js", "cp-goal.js"):
        src = (_REPO / "shared" / "copilot" / "components" / name).read_text(
            encoding="utf-8")
        assert "CopilotShared.GOAL_CHAIN_REC" in src, f"{name} 未引用共享映射"
        assert not re.search(r"(GOAL_CHAIN_REC|CHAIN_RECO)\s*=\s*\{\s*\n?\s*[a-z_]+\s*:", src), \
            f"{name} 出现本地映射副本（应引用 CopilotShared.GOAL_CHAIN_REC）"


def test_rec_map_targets_exist_on_both_sides():
    from src.companion.goals.templates import TEMPLATES
    rec = _js_rec_map()
    starter_ids = {c["chain_id"] for c in STARTER_CHAINS}
    for tid, chain_id in rec.items():
        assert tid in TEMPLATES, f"推荐映射引用不存在的目标模板 {tid}"
        assert chain_id in starter_ids, f"推荐映射引用不存在的种子链 {chain_id}"


def test_rec_map_covers_reactivate_pairing():
    """至少钉住最高置信的一对：唤回目标 ↔ 唤回链（方案的原始动机样例）。"""
    rec = _js_rec_map()
    assert rec.get("engagement_reactivate") == "starter_reactivate_3step"


def test_rec_map_js_equals_python():
    """J：前端单源（sidebar-chrome CopilotShared）与后端消费拷贝
    （workflow_starter.GOAL_CHAIN_REC，漏斗跟随率用）必须逐项相等——
    改映射必须两边同改，否则「推荐的」和「统计口径里的推荐」分叉。"""
    from src.inbox.workflow_starter import GOAL_CHAIN_REC
    assert _js_rec_map() == dict(GOAL_CHAIN_REC)


# ── J：漏斗推荐跟随率（route 层，goals 域护栏内解析模板） ────────────────────

def test_chain_funnel_rec_follow_end_to_end(tmp_path, monkeypatch):
    from src.companion.goals import service as goal_svc
    from src.companion.goals.store import GoalStore
    gstore = GoalStore(":memory:")
    monkeypatch.setattr(goal_svc, "get_configured_store", lambda *_a, **_k: gstore)
    g_re = gstore.create_goal(
        conversation_id="tg:a:r1", platform="tg", account_id="a",
        chat_key="r1", template="engagement_reactivate")
    g_cu = gstore.create_goal(
        conversation_id="tg:a:r3", platform="tg", account_id="a",
        chat_key="r3", template="custom")
    c = _client(tmp_path, cfg={"companion": {"goals": {"enabled": True}}})
    store = c.app.state.inbox_store
    ensure_starter_chains(store)
    # 唤回目标：1 次启动推荐链 + 1 次启动别的链 → followed 1 / eligible 2
    store.start_chain_execution(
        "starter_reactivate_3step", "tg:a:r1", {"goal_id": g_re["goal_id"]})
    store.start_chain_execution(
        "starter_icebreak_7d", "tg:a:r1", {"goal_id": g_re["goal_id"]})
    # custom 模板无推荐 → 不进分母（没有推荐可跟就不算「没跟」）
    store.start_chain_execution(
        "starter_icebreak_7d", "tg:a:r3", {"goal_id": g_cu["goal_id"]})
    # 目标已删/幽灵 id → 不进分母
    store.start_chain_execution(
        "starter_icebreak_7d", "tg:a:r4", {"goal_id": "ghost-goal"})
    d = c.get("/api/workspace/chain-funnel").json()
    assert d["rec_follow"] == {"followed": 1, "eligible": 2, "rate": 0.5}
    assert "goal_chain_starts" not in d, "goal_id 明细不得出 API"


def test_chain_funnel_rec_follow_goals_disabled_soft(tmp_path):
    """goals 模块未启用 → 解析不了模板 → 分母 0、rate None，绝不报错。"""
    c = _client(tmp_path, cfg={})
    store = c.app.state.inbox_store
    ensure_starter_chains(store)
    store.start_chain_execution(
        STARTER_CHAINS[0]["chain_id"], "tg:a:x1", {"goal_id": "g-any"})
    d = c.get("/api/workspace/chain-funnel").json()
    assert d["rec_follow"] == {"followed": 0, "eligible": 0, "rate": None}


# ── D1：授权档位分层（licensing feature gate 接线） ─────────────────────────
# 语义：config 关 > license 锁——运营显式关闭报「未启用」，档位不够报「升级」，
# 两种 403 文案分流（坐席该做的事不同）；gate 总开关默认关=全放行（零行为变化）；
# licensing 层异常恒放行（商业开关不是安全闸门，与 feature_gate 内部口径一致）。

def _lic_cfg(plan, gate_on=True):
    return {
        "inbox": {"workflows": {"enabled": True}},
        "licensing": {"feature_gate": {"enabled": gate_on, "plan_override": plan}},
    }


def test_license_registered_as_pro():
    from src.licensing.feature_gate import FEATURE_MIN_PLAN
    assert FEATURE_MIN_PLAN.get("workflows") == "pro"


def test_license_lock_blocks_chain_routes(tmp_path):
    c = _client(tmp_path, cfg=_lic_cfg("basic"))
    r = c.get("/api/workspace/workflow-chains")
    assert r.status_code == 403
    # 与运营关闭分流文案（升级引导 vs 未启用）
    c2 = _client(tmp_path, cfg=_OFF)
    r2 = c2.get("/api/workspace/workflow-chains")
    assert r2.status_code == 403
    assert r.json()["detail"] != r2.json()["detail"], "license 锁与运营关闭必须分流文案"


def test_license_pro_and_above_allowed(tmp_path):
    for plan in ("pro", "flagship"):
        c = _client(tmp_path, cfg=_lic_cfg(plan))
        assert c.get("/api/workspace/workflow-chains").status_code == 200, plan


def test_license_gate_off_allows_all(tmp_path):
    c = _client(tmp_path, cfg=_lic_cfg("community", gate_on=False))
    assert c.get("/api/workspace/workflow-chains").status_code == 200


def test_config_off_wins_over_license(tmp_path):
    """运营显式关闭时报「未启用」而非「升级」——即使档位其实够。"""
    cfg = {
        "inbox": {"workflows": {"enabled": False}},
        "licensing": {"feature_gate": {"enabled": True, "plan_override": "flagship"}},
    }
    c = _client(tmp_path, cfg=cfg)
    r = c.get("/api/workspace/workflow-chains")
    assert r.status_code == 403
    c_off = _client(tmp_path, cfg=_OFF)
    assert r.json()["detail"] == c_off.get(
        "/api/workspace/workflow-chains").json()["detail"]


def test_license_lock_hits_start_chain_and_seed(tmp_path):
    """写路径同受锁（选择器最常用的两个动作），防「读锁写漏」。"""
    c = _client(tmp_path, cfg=_lic_cfg("community"))
    assert c.post("/api/workspace/workflow-chains/seed").status_code == 403
    assert c.post("/api/workspace/conv/tg:a:c1/start-chain",
                  json={"chain_id": "starter_icebreak_7d"}).status_code == 403


def test_membership_matrix_label_bilingual():
    """会员中心矩阵行名 mb_feat_workflows 双语齐备（模板按 family 动态拼键
    ``mb_feat_<name>``，window.T 静态门禁扫不到，只能在这里钉）。"""
    from src.web.web_i18n import get_translations
    for lang in ("zh", "en"):
        assert get_translations(lang).get("mb_feat_workflows"), f"缺 {lang} 词条"


# ── E2：档位试算矩阵（全档扫一遍，锁得住/放得开/文案对） ────────────────────

def test_plan_sweep_workflow_lock_matrix(tmp_path):
    """community/basic 锁、pro/flagship 放。
    单 client 热切 config（feature_gate 每请求现读配置，无缓存假设）。"""
    from src.licensing.feature_gate import PLAN_ORDER, plan_rank
    c = _client(tmp_path, cfg=_lic_cfg("community"))
    for plan in PLAN_ORDER:
        c.app.state.config_manager.config = _lic_cfg(plan)
        expect = 200 if plan_rank(plan) >= plan_rank("pro") else 403
        got = c.get("/api/workspace/workflow-chains").status_code
        assert got == expect, f"{plan}: 期望 {expect} 实际 {got}"


def test_plan_sweep_gate_snapshot_row_coherent():
    """会员中心矩阵的数据源 gate_snapshot：workflows 行 min_plan/allowed/locked
    三处口径必须自洽（矩阵显示与真实拦截不一致=卖的和交付的分叉）。"""
    from src.licensing.feature_gate import PLAN_ORDER, gate_snapshot, plan_rank
    for plan in PLAN_ORDER:
        cfg = {"licensing": {"feature_gate": {
            "enabled": True, "plan_override": plan}}}
        snap = gate_snapshot(cfg)
        row = snap["features"]["workflows"]
        assert row["min_plan"] == "pro"
        allowed = plan_rank(plan) >= plan_rank("pro")
        assert row["allowed"] is allowed, plan
        assert ("workflows" in snap["locked"]) == (not allowed), plan


# ── E1：/workflows 页面级可用性（走 conftest 全量 admin app） ────────────────

def _set_lic(config_manager, plan=None, gate_on=True):
    lic = config_manager.config.setdefault("licensing", {})
    fg = {"enabled": gate_on}
    if plan:
        fg["plan_override"] = plan
    lic["feature_gate"] = fg


def test_workflows_alias_redirects(auth_client):
    """短链别名修 404：组件「管理工作链」/ops 卡深链的都是 /workflows，
    真实页面在 /workspace/workflows——别名一处修复全部历史链接。"""
    r = auth_client.get("/workflows", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/workspace/workflows"


def test_workflows_page_renders_by_default(auth_client):
    assert auth_client.get("/workspace/workflows").status_code == 200


def test_workflows_page_license_locked_redirects_membership(auth_client, config_manager):
    """license 锁 → 升级引导（与 nav-locked 同去处）+ 来源参数，不渲染半残页。"""
    _set_lic(config_manager, plan="basic")
    r = auth_client.get("/workspace/workflows", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/membership?from=workflows"


def test_workflows_page_config_off_404(auth_client, config_manager):
    """运营显式关闭 → 404（模块不存在于此部署；不是升级问题，别去 membership）。"""
    config_manager.config.setdefault("inbox", {})["workflows"] = {"enabled": False}
    assert auth_client.get("/workspace/workflows").status_code == 404


def test_workflows_page_pro_plan_renders(auth_client, config_manager):
    _set_lic(config_manager, plan="pro")
    assert auth_client.get("/workspace/workflows").status_code == 200


# ── E3：锁定页通用守卫（nav 同源的页面族 → /membership 升级引导） ────────────

def test_page_feature_mapping_pure():
    """页面路径 → 功能族：与 NAV_ITEMS 的 feature 标注同源；边界整段匹配。"""
    from src.web.nav_schema import feature_for_page_path as f
    assert f("/knowledge") == "kb"
    assert f("/knowledge/edit") == "kb"            # 子路径随前缀
    assert f("/knowledgebase") is None             # 整段边界，不误伤
    assert f("/personas") == "personas"
    assert f("/care-schedule") == "care"
    assert f("/workspace/channels/line") == "rpa"
    assert f("/workspace/channels/telegram") is None   # 未归族的渠道页不守卫
    assert f("/membership") is None                # 升级页自身永不守卫（无环）
    assert f("/") is None
    assert f("/workspace") is None


def test_locked_page_redirects_to_membership(auth_client, config_manager):
    """basic 档直连 /knowledge（kb=pro）→ 302 升级引导 + 来源参数。"""
    _set_lic(config_manager, plan="basic")
    r = auth_client.get("/knowledge", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/membership?from=kb"
    # 升级页自身照常可达（无重定向环）
    assert auth_client.get("/membership").status_code == 200


def test_locked_page_open_when_gate_off(auth_client, config_manager):
    _set_lic(config_manager, plan="basic", gate_on=False)
    assert auth_client.get("/knowledge").status_code == 200


def test_page_guard_flagship_all_open(auth_client, config_manager):
    _set_lic(config_manager, plan="flagship")
    for p in ("/knowledge", "/personas", "/analytics"):
        r = auth_client.get(p)
        assert r.status_code == 200, f"{p} -> {r.status_code}"


def test_api_guard_unchanged_by_page_branch(auth_client, config_manager):
    """API 族守卫语义不受页面分支影响：仍是 403 JSON（不是 302）。"""
    _set_lic(config_manager, plan="basic")
    r = auth_client.get("/api/kb/anything", follow_redirects=False)
    assert r.status_code == 403
    assert r.json().get("error") == "feature_locked"


# ── E4：升级页来源引导（?from=<族> → 横幅 + 矩阵行高亮） ────────────────────

def test_membership_from_renders_hint_and_highlight(auth_client, config_manager):
    _set_lic(config_manager, plan="basic")
    html = auth_client.get("/membership?from=workflows").text
    assert 'class="mb-from-hint"' in html, "来源横幅未渲染"
    assert 'class="mb-row-hi"' in html, "对应矩阵行未高亮"


def test_membership_from_rejects_unknown_and_absent(auth_client, config_manager):
    """未注册族名不反射不渲染（防垃圾参数）；无参数无横幅（老入口零变化）。"""
    _set_lic(config_manager, plan="basic")
    for url in ("/membership?from=hax0r", "/membership?from=", "/membership"):
        html = auth_client.get(url).text
        assert 'class="mb-from-hint"' not in html, url
        assert 'class="mb-row-hi"' not in html, url


def test_membership_from_hint_key_bilingual():
    from src.web.web_i18n import get_translations
    for lang in ("zh", "en"):
        v = get_translations(lang).get("mb_from_hint") or ""
        assert "{feat}" in v and "{plan}" in v, f"{lang} 词条缺占位符"
        assert get_translations(lang).get("mb_from_cta"), f"缺 {lang} CTA 词条"


def test_membership_from_cta_renders_with_shop_url(auth_client, config_manager):
    """配了 shop_url → 横幅出购买按钮；测试机无 chatx 授权 → 走「按当前授权」
    回落链（basic 档 → plan=autochat-entry），家族安全语义端到端钉住。"""
    _set_lic(config_manager, plan="basic")
    config_manager.config["licensing"]["shop_url"] = "https://site.example/order"
    html = auth_client.get("/membership?from=workflows").text
    assert 'class="mb-from-cta"' in html
    assert "plan=autochat-entry" in html


def test_membership_from_cta_hidden_without_shop_url(auth_client, config_manager):
    """没配 shop_url → 无按钮（横幅照常），绝不渲染空链接。"""
    _set_lic(config_manager, plan="basic")
    html = auth_client.get("/membership?from=workflows").text
    assert 'class="mb-from-hint"' in html
    assert 'class="mb-from-cta"' not in html


def test_nav_locked_link_carries_from(auth_client, config_manager):
    """侧栏锁标链接带来源参数（与页面守卫 302 同语义）。"""
    _set_lic(config_manager, plan="basic")
    html = auth_client.get("/").text
    assert 'class="nav-locked"' in html
    assert 'href="/membership?from=' in html
