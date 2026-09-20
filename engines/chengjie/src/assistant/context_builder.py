# -*- coding: utf-8 -*-
"""assistant 上下文构建（纯函数）：(page, role, lang) → prompt 上下文块。

RBAC 红线：agent 角色只注入工作台系（/workspace*）页面信息，管理页
说明一概不注入——上下文构建器是权限的第一道闸，不靠 LLM 自觉。
页面反查走 nav_schema.NAV_ITEMS（单一数据源），说明走 help_terms。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PageInfo:
    key: str = ""
    title: str = ""
    desc: str = ""
    path: str = ""


def _norm_path(page: str) -> str:
    p = str(page or "").split("?", 1)[0].split("#", 1)[0].strip()
    if not p.startswith("/"):
        p = "/" + p if p else ""
    # 去尾斜杠（保留根 "/"）
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    return p[:120]


def agent_page_visible(path: str) -> bool:
    """agent 角色可见页面判定：工作台系 + 帮助页。与 admin.py 页面白名单同向
    （这里是「上下文注入」口径，宁窄勿宽——漏注入无害，多注入=越权泄漏）。"""
    p = _norm_path(path)
    return p.startswith("/workspace") or p in ("/help", "/login")


def resolve_page(page: str, *, lang: str = "zh") -> PageInfo:
    """按路径反查 nav_schema 页面项 + help_terms 说明。查不到返回空 PageInfo
    （调用方按「未知页面」处理，不注入）。"""
    p = _norm_path(page)
    if not p:
        return PageInfo()
    try:
        from src.web.nav_schema import NAV_ITEMS
    except Exception:
        return PageInfo(path=p)
    best_id = ""
    best_len = 0
    for item_id, item in NAV_ITEMS.items():
        ipath = str(item.get("path") or "")
        if not ipath:
            continue
        if p == ipath or p.startswith(ipath + "/"):
            if len(ipath) > best_len:
                best_id, best_len = item_id, len(ipath)
    if not best_id:
        # 工作台首页族（/workspace 下没有独立 nav 项的子页）归 workspace 项
        if p.startswith("/workspace"):
            for item_id, item in NAV_ITEMS.items():
                if str(item.get("path") or "") == "/workspace":
                    best_id = item_id
                    break
        if not best_id:
            return PageInfo(path=p)
    item = NAV_ITEMS[best_id]
    title = str(item.get("label_zh") or best_id)
    desc = ""
    help_key = str(item.get("help") or "")
    if help_key:
        try:
            from src.web.help_terms import HELP_TERMS

            term = HELP_TERMS.get(help_key) or {}
            if lang == "en":
                title = str(term.get("en") or title)
                desc = str(term.get("desc_en") or term.get("desc") or "")
            else:
                title = str(term.get("zh") or title)
                desc = str(term.get("desc") or "")
        except Exception:
            desc = ""
    return PageInfo(key=best_id, title=title, desc=desc[:300], path=p)


def build_context_block(
    *,
    page: str,
    role: str,
    lang: str = "zh",
    ui_build: str = "",
) -> str:
    """组 prompt 上下文段。agent 在管理页（理论上不该发生）或未知页 → 只给
    角色行，绝不泄漏管理页说明。"""
    r = str(role or "").strip().lower() or "unknown"
    lines: list[str] = []
    if lang == "en":
        lines.append(f"User role: {r}")
    else:
        lines.append(f"用户角色：{r}")
    p = _norm_path(page)
    inject_page = bool(p)
    if r == "agent" and not agent_page_visible(p):
        inject_page = False
    if inject_page:
        info = resolve_page(p, lang=lang)
        if info.title:
            if lang == "en":
                lines.append(f'User is on page "{info.title}" ({info.path})')
            else:
                lines.append(f"用户正在「{info.title}」页（{info.path}）")
            if info.desc:
                lines.append(
                    f"Page purpose: {info.desc}" if lang == "en"
                    else f"该页用途：{info.desc}"
                )
    if ui_build:
        lines.append(f"UI build: {str(ui_build)[:40]}")
    return "\n".join(lines)


__all__ = ["PageInfo", "agent_page_visible", "resolve_page", "build_context_block"]
