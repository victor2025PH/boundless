# -*- coding: utf-8 -*-
"""代运营多客户工作区 · 第一段（只加字段与默认工作区，不改变任何现有行为）。

设计：D:\\handoff\\zhiliao_dev\\DESIGN_agency_workspace.md（2026-10-08 蛋博士批准）。
在收件箱库既有 P3 骨架（``workspaces`` 表、``conversation_meta.workspace_id``）上扩展，不另起一套。

第一段约定：
- 各库给归属表加 ``workspace_id TEXT NOT NULL DEFAULT 'default'``（幂等 ALTER，旧行即 default）；
- 读接口对外输出**不带**新列（:func:`strip_workspace`），保证迁移前后接口输出逐字一致；
  第三段打开隔离时再有意识地暴露；
- :func:`current_workspace` 永远返回 ``default``（隔离开关第三段才有）。

注意命名：``src/workspace/`` 是坐席工作台（UI / 分配），与这里的「客户工作区」无关。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

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


def ensure_default_workspace_sql() -> str:
    """收件箱库 ``workspaces`` 表里的默认工作区行（INSERT OR IGNORE，可重复执行）。"""
    return ("INSERT OR IGNORE INTO workspaces (workspace_id, display_name, config_json, "
            "created_at, updated_at, kind, status) VALUES ('default', '', '{}', 0, 0, "
            "'default', 'active')")


__all__ = ["DEFAULT_WORKSPACE_ID", "WORKSPACE_COLUMN", "WORKSPACE_COLUMN_DDL", "add_column_sql",
           "strip_workspace", "strip_all", "current_workspace", "ensure_default_workspace_sql"]
