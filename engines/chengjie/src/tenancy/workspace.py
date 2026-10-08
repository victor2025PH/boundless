# -*- coding: utf-8 -*-
"""代运营多客户工作区 · 第一段（只加字段与默认工作区，不改变任何现有行为）。

设计：D:\\handoff\\zhiliao_dev\\DESIGN_agency_workspace.md（2026-10-08 蛋博士批准）。
在收件箱库既有 P3 骨架（``workspaces`` 表、``conversation_meta.workspace_id``）上扩展，不另起一套。

第一段约定：
- 各库给归属表加 ``workspace_id TEXT NOT NULL DEFAULT 'default'``（幂等 ALTER，旧行即 default）；
- 读接口对外输出**不带**新列（:func:`strip_workspace`），保证迁移前后接口输出逐字一致；
  第三段打开隔离时再有意识地暴露；
- :func:`current_workspace` 永远返回 ``default``（隔离开关第三段才有）。

第二段（写入打标，见文件后半）：写入时按账号 / 当前工作区打 ``workspace_id``，读取仍不过滤。

注意命名：``src/workspace/`` 是坐席工作台（UI / 分配），与这里的「客户工作区」无关。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterable, Iterator, Optional

DEFAULT_WORKSPACE_ID = "default"
WORKSPACE_COLUMN = "workspace_id"
#: 迁移用列定义（各库统一口径）
WORKSPACE_COLUMN_DDL = "TEXT NOT NULL DEFAULT 'default'"


def add_column_sql(table: str, column: str = WORKSPACE_COLUMN) -> str:
    return f"ALTER TABLE {table} ADD COLUMN {column} {WORKSPACE_COLUMN_DDL}"


def strip_workspace(d: Optional[Dict[str, Any]], *columns: str) -> Optional[Dict[str, Any]]:
    """第一段：把新加的归属列从对外 dict 里拿掉（原地修改并返回）。None 原样返回。"""
    if d is None:
        return None
    for c in (columns or (WORKSPACE_COLUMN,)):
        d.pop(c, None)
    return d


def strip_all(rows: Iterable[Dict[str, Any]], *columns: str) -> list:
    return [strip_workspace(r, *columns) for r in rows]


def current_workspace(request: Any = None) -> str:
    """当前请求所在工作区。第一段桩：永远 ``default``（等于今天的行为）。"""
    return DEFAULT_WORKSPACE_ID


# ── 第二段：写入打标（只写标签，不隔离；读取行为不变）──────────────────────────
#
# 规则（蛋博士 2026-10-08）：会话跟着账号走；agent_sends / outreach_log / kb_entries 跟着对应的
# 账号或当前工作区；都没有就写 default。账号归属的唯一来源是 ``platform_accounts.workspace_id``。
# 应急开关：环境变量 ``CHENGJIE_WS_TAGGING=0`` → 一律写 default（等于第一段行为）。

#: 写入上下文里的「当前工作区」（路由 / 后台任务可用 :func:`bind_workspace` 绑定；未绑定＝空）
_WRITE_WS: ContextVar[str] = ContextVar("chengjie_write_workspace", default="")
_MAX_WS_LEN = 64


def normalize_workspace_id(value: Any) -> str:
    """去空白、截断；空串表示「没有」。"""
    return str(value or "").strip()[:_MAX_WS_LEN]


def tagging_enabled() -> bool:
    return str(os.environ.get("CHENGJIE_WS_TAGGING", "1")).strip().lower() not in (
        "0", "false", "off", "no")


@contextmanager
def bind_workspace(workspace_id: Any) -> Iterator[str]:
    """在一段写入代码里绑定当前工作区（contextvar，线程 / 协程安全，退出即还原）。"""
    token = _WRITE_WS.set(normalize_workspace_id(workspace_id))
    try:
        yield _WRITE_WS.get()
    finally:
        _WRITE_WS.reset(token)


def current_write_workspace() -> str:
    """写入时的当前工作区：已绑定的值，否则 default。"""
    return _WRITE_WS.get() or DEFAULT_WORKSPACE_ID


def account_workspace(platform: Any, account_id: Any) -> str:
    """账号所属工作区（注册表 TTL 缓存；单例未初始化 / 未登记 / 出错 → default，绝不建库）。"""
    if not platform or not account_id:
        return DEFAULT_WORKSPACE_ID
    try:
        from src.integrations.account_registry import cached_account_workspace
        return normalize_workspace_id(cached_account_workspace(platform, account_id)) \
            or DEFAULT_WORKSPACE_ID
    except Exception:
        return DEFAULT_WORKSPACE_ID


def resolve_write_workspace(*, explicit: Any = None, platform: Any = "", account_id: Any = "",
                            fallback: Any = "") -> str:
    """写入打标的统一口径：显式值 → 账号归属 → ``fallback``（如会话已有标签）→ 当前工作区 → default。

    显式值（非空）原样采用，包括 ``default``；账号归属 / fallback 里的 default 视同「没有」，继续往后找。
    关掉开关时恒为 default。
    """
    if not tagging_enabled():
        return DEFAULT_WORKSPACE_ID
    ws = normalize_workspace_id(explicit)
    if ws:
        return ws
    for cand in (account_workspace(platform, account_id), fallback):
        ws = normalize_workspace_id(cand)
        if ws and ws != DEFAULT_WORKSPACE_ID:
            return ws
    return current_write_workspace()


def ensure_default_workspace_sql() -> str:
    """收件箱库 ``workspaces`` 表里的默认工作区行（INSERT OR IGNORE，可重复执行）。"""
    return ("INSERT OR IGNORE INTO workspaces (workspace_id, display_name, config_json, "
            "created_at, updated_at, kind, status) VALUES ('default', '', '{}', 0, 0, "
            "'default', 'active')")


__all__ = ["DEFAULT_WORKSPACE_ID", "WORKSPACE_COLUMN", "WORKSPACE_COLUMN_DDL", "add_column_sql",
           "strip_workspace", "strip_all", "current_workspace", "ensure_default_workspace_sql",
           "normalize_workspace_id", "tagging_enabled", "bind_workspace", "current_write_workspace",
           "account_workspace", "resolve_write_workspace"]
