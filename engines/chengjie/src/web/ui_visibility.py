# -*- coding: utf-8 -*-
"""内部功能界面显隐单一事实源（2026-08-14）。

七个「内部/运维向」界面默认对坐席隐藏，开发者工具页（/developer，密码闸）
按键独立开启；藏而不废——只藏入口与渲染，URL/API 不封（与真机矩阵
simple=True 的既有哲学一致）：

- manual_console  人工操作台：桌面壳内嵌官方网页版账号页签条（cx-shell-strip）
                  + 收件箱「打开官方网页版/原生页」入口 + 桌面壳竖栏。
- group_extract   群成员提取：收件箱链接卡 / ops-overview 卡 / 副驾
                  cp-tg-members 组件 / App 模式宿主 shared/copilot/app.html
                  的链接卡（静态 hidden 打底 + ui-flags 揭示，2026-08-15 补漏
                  ——首批只接了 collab，这张卡漏了导致桌面副驾常显）。
                  后端 /api/tg-members* 仍由 group_members.enabled 独立守卫，
                  两层正交。
- matrix_nav      真机矩阵导航组：侧栏「真机矩阵」组 + 命令面板矩阵项 +
                  矩阵上下文导航（页面本身可直达，仅藏导航面）。
- group_show      群脉导播台：侧栏「真机矩阵」组内的导播台项 + 命令面板项
                  （2026-08-16 补：matrix_nav 关时该项曾留守组内，导致
                  「真机矩阵」组标题带着导播台一直显示；与 matrix_nav 同关时
                  整组消失。页面 /group-show 仍可直达；真发另有
                  companion.group_show.live.enabled 双闸，两层正交）。
- team_collab     团队协作副驾卡：web 收件箱右栏 + 桌面副驾 app.html 的
                  data-cp-card="collab"（保 DOM 加 hidden——panel-manifest
                  装配门禁与接线依赖 DOM 存在，先例＝analysis 卡 hiddenBoth）。
- ai_settings     「AI 与转接设置」页两张卡：AI 提示词 & 行为配置（全局
                  ``ai.system_prompt``／AI 名称／回复风格——只影响**未绑人设**
                  的会话，如 Telegram 主线与官网聊天；三端 RPA 与收件箱走
                  persona 体系不受其管）+ 人工客服转接（``human_escalation``，
                  本机实测 enabled:false 从未启用）。两者都是「配一次就不动」
                  的底层项，摆在坐席日常页面只增噪音（2026-08-16 老板实录）。
                  藏而不废：``/settings`` URL 与 ``/api/settings/save`` 全部照旧，
                  开发者工具开该键即恢复；未开时页面出指路空态。
                  **同时藏侧栏「人工转接」项**（simple 模式 core 项，指向
                  ``/settings#escalation``——卡片藏了入口还在＝点进去空页）。
- cockpit         驾驶舱：工作台顶栏「驾驶舱」入口（``/workspace/cockpit``）。
                  该页的主动作是「一键接管/交还」——接管要跳原生页，而原生页
                  整条链已随 ``manual_console`` 关闭，于是页面价值只剩半截；
                  队列本身的四个信号（需人工 / 客户在等 / 草稿待审 / 接管超时）
                  在收件箱筛选与草稿队列里各有既有入口，不因藏本页而失联
                  （2026-08-16 老板决定：人工那套既然藏了，这个跟着藏）。
                  藏而不废：``/workspace/cockpit`` 与 ``/api/cockpit/overview``
                  照旧可达，开发者工具开该键即恢复顶栏入口。用量读数
                  ``tools/cockpit_usage_report.py`` 的观测窗因此中止——重新开启
                  后 ck_* 埋点自然续上，别拿藏起来这段的零点击当「没人用」的证据。

契约：
- 配置键 ``ui_visibility.{manual_console,group_extract,matrix_nav,group_show,
  team_collab,ai_settings}``，缺省 **False=隐藏**（新子系统默认关约定；开启是
  运营/开发决策，走 /developer 页写 overlay）。
- ``resolve_ui_visibility`` 纯函数：任何异常/坏类型回落缺省（fail-hidden——
  这些是内部功能，读不到配置时藏起来比露出来安全）。
- 消费方：admin.py ``_enrich_context`` 注入模板全局 ``ui_vis``；
  nav_schema ``get_nav_context(ui_visibility=)``；desktop 壳经
  ``GET /api/desktop/ui-flags``（developer_api_routes）。
"""

from __future__ import annotations

from typing import Any, Dict

# 键名 → 中文说明（开发者页与审计日志共用；顺序即 UI 渲染顺序）
UI_VISIBILITY_KEYS = (
    ("manual_console", "人工操作台（桌面壳内嵌官方网页版页签条 + 网页入口）"),
    ("group_extract", "群成员提取（收件箱卡 / 运营总览卡 / 副驾组件）"),
    ("matrix_nav", "真机矩阵导航（侧栏组 / 命令面板 / 上下文导航）"),
    ("group_show", "群脉导播台（侧栏「真机矩阵」组内项 + 命令面板）"),
    ("team_collab", "团队协作（副驾协作注解卡，web + 桌面双宿主）"),
    ("ai_settings", "AI 与转接设置（AI 提示词 & 行为配置卡 / 人工客服转接卡 / 侧栏「人工转接」项）"),
    ("cockpit", "驾驶舱（工作台顶栏「驾驶舱」入口 /workspace/cockpit）"),
)

DEFAULTS: Dict[str, bool] = {k: False for k, _ in UI_VISIBILITY_KEYS}

CONFIG_SECTION = "ui_visibility"


def resolve_ui_visibility(config: Any) -> Dict[str, bool]:
    """config(dict|None) → 全键布尔字典。纯函数；异常一律回落缺省（全隐藏）。"""
    out = dict(DEFAULTS)
    try:
        section = (config or {}).get(CONFIG_SECTION) if isinstance(config, dict) else None
        if isinstance(section, dict):
            for key in out:
                if key in section:
                    out[key] = bool(section[key])
    except Exception:
        return dict(DEFAULTS)
    return out


def is_visible(config: Any, key: str) -> bool:
    """单键便捷判定（路由/模板辅助用）。未知键按隐藏处理。"""
    return bool(resolve_ui_visibility(config).get(key, False))
