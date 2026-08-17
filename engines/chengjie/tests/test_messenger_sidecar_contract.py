# -*- coding: utf-8 -*-
"""Messenger 网页接入「跨语言契约」门禁（2026-07-30，桌面 turnkey 路线 A 配合项）。

**为什么需要它**：Messenger 没有干净协议库，人设个人号只能走 messenger.com 网页版
——Python provider 用 f-string 拼 sidecar 路由，Node ``services/messenger-web/server.js``
用 Express 注册路由，**两侧没有任何编译期保护**。sidecar 改一个路由名（或 Python 侧
改错拼接），另一侧就 404 静默失败，「Messenger 接入走不通」原样重现，且没有红、没有
异常——只是登录/重连悄悄不工作。这条链路正被密集重构（桌面 turnkey），正是最该上
同步保险的时候。

**口径（刻意窄）**：只钉**明确属于 Messenger、且不在发送热区**的接入核心契约——
网页登录三路由（``messenger_web_login.py``）+ 断线重连（``ops_overview_routes.py``）。
发送层（``/accounts/:id/send`` 等）平台归属需另考且在活跃重构区，**不纳入**。

提取/归一化核心见 ``tests/_sidecar_contract.py``（与 WhatsApp 门禁同构共用）。

覆盖的不变量：
  1. provider 拼的每个 sidecar 路径都在 server.js 注册；
  2. 断线重连路由 ``/accounts/:id/relogin`` 在 server.js 注册（ops 卡 / 账号 rail /
     workspace_channels 三处 UI 的「重新登录」都指向它，断了三处一起失效）；
  3. provider 实际拼的路径都被契约清单覆盖（防 provider 新增调用而门禁没跟上）；
  4. server.js 路由提取器至少提出合理数量的路由（防格式变化 → 提取到 0 → 全假绿）。
"""

from __future__ import annotations

import re

from tests import _sidecar_contract as sc

SERVER_JS = sc.REPO / "services" / "messenger-web" / "server.js"
PROVIDER_PY = sc.REPO / "src" / "integrations" / "messenger_web_login.py"
OPS_ROUTES_PY = sc.REPO / "src" / "web" / "routes" / "ops_overview_routes.py"

# Messenger 接入核心契约（归一化路径 → 存在理由）。新增 provider→sidecar 调用时
# 显式补这里（测试 3 会强制你补，测试 1 会校验它真存在）。
_CONTRACT = {
    "/login/start": "网页登录发起（connect 弹窗 + provider start）",
    "/login/:P/status": "登录轮询（connect 弹窗每 2.5s poll）",
    "/login/:P/relay-step": "表单中继只读探针（交互登录：登录页此刻该渲染哪一步原生表单）",
    "/login/:P/relay-submit": "表单中继写入端（交互登录：把原生表单字段值填回登录页）",
    "/login/:P/cancel": "取消登录（关窗 / 换码前拆旧会话）",
    "/accounts/:P/relogin": "断线重连（ops 卡 / 账号 rail / workspace_channels 三处入口）",
}


def test_server_route_extractor_sane():
    """提取器健全：至少提出 login+relogin 那几条，否则后续断言全假绿。"""
    routes = sc.express_routes(SERVER_JS)
    assert len(routes) >= 8, f"server.js 路由提取异常，只得到 {len(routes)} 条：{sorted(routes)}"
    assert "/health" in routes, "连 /health 都没提到，提取正则坏了"


def test_contract_routes_exist_in_sidecar():
    """契约清单里每条路由都必须在 server.js 注册（漂移即红）。"""
    routes = sc.express_routes(SERVER_JS)
    missing = [(p, why) for p, why in _CONTRACT.items() if p not in routes]
    assert not missing, (
        "Messenger 接入契约路由在 sidecar (server.js) 缺失——Python 侧会 404 静默失败：\n  "
        + "\n  ".join(f"{p}  （{why}）" for p, why in missing)
        + f"\n  server.js 现有路由：{sorted(routes)}"
    )


def test_provider_paths_covered_by_contract():
    """provider 实际拼的每个 sidecar 路径都得在契约清单里（防新增调用漏钉）。"""
    prov = sc.provider_paths(PROVIDER_PY)
    assert prov, "未从 messenger_web_login.py 提取到任何 {base}/... 路径（提取正则坏了？）"
    uncovered = sorted(prov - set(_CONTRACT))
    assert not uncovered, (
        "messenger_web_login.py 新增了 sidecar 调用但未登记进本门禁 _CONTRACT——"
        "补进去以恢复契约守护：\n  " + "\n  ".join(uncovered)
    )


def test_provider_paths_all_registered():
    """双保险：provider 拼的路径直接对照 server.js（不经契约清单中转）。"""
    routes = sc.express_routes(SERVER_JS)
    prov = sc.provider_paths(PROVIDER_PY)
    missing = sorted(prov - routes)
    assert not missing, (
        "messenger_web_login.py 拼的 sidecar 路径在 server.js 找不到：\n  "
        + "\n  ".join(missing) + f"\n  server.js 现有：{sorted(routes)}"
    )


def test_relogin_call_site_present():
    """ops_overview_routes.py 里 relogin 调用点仍在（UI 三处入口的唯一后端出口）。

    Messenger relogin 调用点在**稳定的** ops 路由文件，故可直接钉调用点；WhatsApp 的
    reconnect 调用点在编排/发送热区（account_orchestrator.py），那边只钉 sidecar 侧
    存在性——见 test_whatsapp_sidecar_contract.py 的说明。"""
    txt = OPS_ROUTES_PY.read_text(encoding="utf-8")
    assert re.search(r"/accounts/\{[^}]+\}/relogin", txt), (
        "ops_overview_routes.py 不再拼 /accounts/{id}/relogin——"
        "「重新登录」三处 UI 入口会全部失效"
    )
