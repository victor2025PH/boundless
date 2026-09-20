# -*- coding: utf-8 -*-
"""流失预警页能力探测（RH-P1，纯函数核心）。

背景：/relations-health 页面与侧栏菜单**无条件**渲染，但它依赖的
``/api/relations/*`` 一族路由只在 contacts 子系统启用时注册——两套闸门
不同步，contacts 关着时页面拿裸 404 什么都解释不了（2026-08-01 实锤）。

本模块回答页面开局的三个问题（admin.py 的 ``/api/relations/capability``
薄包装本函数，**无论 contacts 是否启用都注册**）：

1. 全量榜（contacts journey/亲密度/付费信号）可用吗？——看路由是否真的注册了
   （请求时刻自省 ``app.routes``，比读 flag 更接近真相：flag 开了没重启、
   或 intimacy 引擎装配失败，路由都不会在）。
2. 不可用时还能用什么？——收件箱侧 ChurnPredictor 轻量榜（``inbox_store``
   在即在），页面据此降级而不是死页。
3. 是「没开」还是「开了没重启」？——flag 与路由注册状态的组合能区分
   ``pending_restart``（overlay 热加载后 flag 已翻真、但 bootstrap 只在
   进程启动时跑），前端引导文案完全不同。

纯函数 + 显式入参：路由集合/config/store 存在性全部由调用方喂进来，
零 app 依赖，门禁 ``tests/test_relations_capability.py`` 直接穷举组合。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

# 路由自省用的锚点：全量榜与「生成话术」闭环各看一个代表性 path。
BOARD_ROUTE = "/api/relations/health-board"
REACTIVATION_ROUTE = "/api/reactivation/{journey_id}/draft-reunion"

MODE_FULL = "full"
MODE_LITE = "lite"
MODE_NONE = "none"


def route_paths_of(app: Any) -> set:
    """从 FastAPI/Starlette app 收集全部已注册 path（请求时刻调用，注册顺序无关）。"""
    out = set()
    for r in getattr(app, "routes", []) or []:
        p = getattr(r, "path", None)
        if p:
            out.add(str(p))
    return out


def build_capability(
    *,
    route_paths: Iterable[str],
    config: Optional[Mapping[str, Any]],
    has_inbox_store: bool,
    contact_count: Optional[int] = None,
) -> Dict[str, Any]:
    """组装能力快照（绝不抛；所有字段前端可直接消费）。

    - ``mode``：full（全量榜可用）/ lite（仅收件箱轻量榜）/ none（两者皆无）。
    - ``pending_restart``：flag 已开但路由未注册＝改了配置还没重启（或装配失败），
      前端据此把引导文案从「去开配置」换成「等待重启生效」。
    - ``cold_start``：全量榜可用但 contacts 还没有数据（刚开闸），前端默认切
      轻量榜 + 出「数据积累中」条，不让运营对着空表怀疑功能坏了。
    """
    paths = set(str(p) for p in (route_paths or []))
    contacts_flag = bool(
        (((config or {}).get("contacts") or {}).get("enabled")))
    board = BOARD_ROUTE in paths
    reactivation = REACTIVATION_ROUTE in paths
    lite = bool(has_inbox_store)
    if board:
        mode = MODE_FULL
    elif lite:
        mode = MODE_LITE
    else:
        mode = MODE_NONE
    cnt = None
    if contact_count is not None:
        try:
            cnt = max(0, int(contact_count))
        except Exception:
            cnt = None
    return {
        "ok": True,
        "mode": mode,
        "contacts_enabled": contacts_flag,
        "board_registered": board,
        "reactivation_registered": reactivation,
        "lite_available": lite,
        "contact_count": cnt,
        "cold_start": bool(board and cnt == 0),
        "pending_restart": bool(contacts_flag and not board),
        "flag_path": "contacts.enabled",
    }
