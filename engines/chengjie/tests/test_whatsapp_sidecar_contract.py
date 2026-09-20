# -*- coding: utf-8 -*-
"""WhatsApp Baileys 接入「跨语言契约」门禁（2026-07-30，与 Messenger 门禁对称）。

**为什么需要它**：WhatsApp 没有官方多账号协议库，走社区 Baileys（Node）。与
Messenger 完全同构——``whatsapp_baileys_login.py`` 用 f-string 拼 sidecar 路由，
``services/whatsapp-baileys/server.js`` 用 Express 注册，**两侧无编译期保护**。改一侧
路由名，另一侧 404 静默失败，登录/重连悄悄不工作，没有红、没有异常。

**口径（与 Messenger 门禁一致，刻意窄）**：只钉接入核心——协议扫码登录三路由
（``whatsapp_baileys_login.py``）+ 断线自愈重连（sidecar ``/accounts/:id/reconnect``）。
发送/会话管理路由族（``/send`` /``/react`` /``/resync`` 等）不纳入：它们是发送/运维层，
且部分调用点在活跃重构区。

**reconnect 只钉 sidecar 侧存在性，不钉 Python 调用点**：与 Messenger 的 relogin
不同——relogin 调用点在稳定的 ``ops_overview_routes.py``（那边直接钉调用点），而 WA
reconnect 的调用点在 ``account_orchestrator.py``（编排/发送**热区**，退避重启时触发
自愈）。门禁**刻意不读对方活跃/发送热区文件**做提取，避免与其重构耦合；reconnect
的价值在「sidecar 保证提供这个自愈出口」，故只钉 server.js 侧。

提取/归一化核心见 ``tests/_sidecar_contract.py``（与 Messenger 门禁同构共用）。纯静态、
零副作用。

覆盖的不变量：
  1. provider 拼的每个 sidecar 路径都在 server.js 注册；
  2. 断线自愈路由 ``/accounts/:id/reconnect`` 在 server.js 注册（编排器自愈唯一出口）；
  3. provider 实际拼的路径都被契约清单覆盖（防 provider 新增调用而门禁没跟上）；
  4. server.js 路由提取器至少提出合理数量的路由（防格式变化 → 提取到 0 → 全假绿）。
"""

from __future__ import annotations

from tests import _sidecar_contract as sc

SERVER_JS = sc.REPO / "services" / "whatsapp-baileys" / "server.js"
PROVIDER_PY = sc.REPO / "src" / "integrations" / "whatsapp_baileys_login.py"

# WhatsApp 接入核心契约（归一化路径 → 存在理由）。
_CONTRACT = {
    "/login/start": "协议扫码登录发起（connect 弹窗 + provider start）",
    "/login/:P/status": "登录轮询（connect 弹窗每 2.5s poll）",
    "/login/:P/cancel": "取消登录（关窗 / 换码前拆旧会话）",
    "/accounts/:P/reconnect": "断线自愈重连（account_orchestrator 退避重启触发；sidecar 侧唯一出口）",
}


def test_server_route_extractor_sane():
    """提取器健全：至少提出 login+reconnect 那几条，否则后续断言全假绿。"""
    routes = sc.express_routes(SERVER_JS)
    assert len(routes) >= 8, f"server.js 路由提取异常，只得到 {len(routes)} 条：{sorted(routes)}"
    assert "/health" in routes, "连 /health 都没提到，提取正则坏了"


def test_contract_routes_exist_in_sidecar():
    """契约清单里每条路由都必须在 server.js 注册（漂移即红）。"""
    routes = sc.express_routes(SERVER_JS)
    missing = [(p, why) for p, why in _CONTRACT.items() if p not in routes]
    assert not missing, (
        "WhatsApp 接入契约路由在 sidecar (server.js) 缺失——Python 侧会 404 静默失败：\n  "
        + "\n  ".join(f"{p}  （{why}）" for p, why in missing)
        + f"\n  server.js 现有路由：{sorted(routes)}"
    )


def test_provider_paths_covered_by_contract():
    """provider 实际拼的每个 sidecar 路径都得在契约清单里（防新增调用漏钉）。"""
    prov = sc.provider_paths(PROVIDER_PY)
    assert prov, "未从 whatsapp_baileys_login.py 提取到任何 {base}/... 路径（提取正则坏了？）"
    uncovered = sorted(prov - set(_CONTRACT))
    assert not uncovered, (
        "whatsapp_baileys_login.py 新增了 sidecar 调用但未登记进本门禁 _CONTRACT——"
        "补进去以恢复契约守护：\n  " + "\n  ".join(uncovered)
    )


def test_provider_paths_all_registered():
    """双保险：provider 拼的路径直接对照 server.js（不经契约清单中转）。"""
    routes = sc.express_routes(SERVER_JS)
    prov = sc.provider_paths(PROVIDER_PY)
    missing = sorted(prov - routes)
    assert not missing, (
        "whatsapp_baileys_login.py 拼的 sidecar 路径在 server.js 找不到：\n  "
        + "\n  ".join(missing) + f"\n  server.js 现有：{sorted(routes)}"
    )
