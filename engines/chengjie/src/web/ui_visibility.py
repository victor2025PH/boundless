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

部署形态（flavor，2026-08-20 实施49 P1-6；2026-09-06 L-4 A 加代理商层）：布尔键
之外还有一个**形态维度**——同一份代码既跑内部服务器（117 双实例，运维/开发天天
用），也跑客户桌面包（skuio 那类最终用户）。客户形态下「实时日志 / 开发者工具」
这类运维页出现在侧栏只会制造「这是给我用的吗」的困惑（B6 原话：不该对用户开放），
而内部部署必须原样保留。故：

- ``resolve_ui_flavor(config)`` → ``"client"`` | ``"partner"`` | ``"internal"``
  （三层：用户版 ⊂ 代理商版 ⊂ 研发版；D-L2 / D-L7 2026-09-06）；
- 判定序＝显式配置 ``ui_visibility.flavor``（三值之一，运维/发版脚本可写死）→
  桌面模式（``AITR_DESKTOP_MODE`` env 或 ``app.desktop_mode``，与
  ``env_probe._is_desktop_mode`` 同口径）→ 否则 internal（服务器部署零变化）；
- 消费方＝nav_schema（client 形态剔 ``CLIENT_HIDDEN_ITEM_IDS`` 运维/研发面 +
  ``PARTNER_ONLY_ITEM_IDS`` 代理商面）、admin.py ``_enrich_context``（模板全局
  ``ui_flavor`` / ``ui_client_flavor`` / ``ui_developer_mode`` / ``ui_client_hide``）。
  **藏而不废**：``/logs`` ``/developer`` ``/help`` 等 URL 与 API 一律不封
  （开发者页本就有密码闸），内部人员在客户机上仍可直接敲地址进去。

开发者模式（developer_mode，2026-09-06 L-4 A / D-L2）：内部人在**客户机**上需要
看被 client 形态藏掉的项（排障 / 演示 / 给代理商配白标），不该靠改配置文件。
故：

- 载体＝**session 键** ``developer_mode``（不是 overlay 键——落盘会让客户机
  永久变成研发面；session 随退出登录一起消失＝「退出自动关」）；
- 前提＝同一 session 里 ``dev_unlocked`` 为真（``/developer`` 密码闸）；
  ``POST /developer/logout`` 或密码闸失效时本键即作废，不必单独清；
- 写入口单一＝``POST /api/developer/developer-mode``（ui_visibility_routes，
  须登录 + dev 解锁）；
- 消费方＝nav_schema ``get_nav_context(cfg, developer_mode=True)``（被藏项回归
  并带 ``tier`` 注解 → 侧栏「研发 / 代理商」角标）+ 模板全局
  ``ui_developer_mode`` / ``ui_client_hide``（L-2 人设页三处「开发者模式可见」
  读 ``ui_client_hide``：真＝client 形态且未开开发者模式＝该藏的都藏）。

坐席规模（seat_mode，2026-08-20 实施49 P1-6 / 反馈 B13）：第三个维度——**数据**
驱动而非配置驱动。「认领/释放」是多坐席协作原语（认领＝这个客户我跟，防两个人
同时回同一个客户）；单人部署里它没有任何对手方，只是每行都杵着的一枚按钮 + 一个
永远等于「全部」的「我的」筛选（B13 原话：功能导向不明，建议非必须则删）。故：

- ``resolve_seat_mode(config, users)`` → ``"multi"`` | ``"single"``；
- 判定序＝显式配置 ``ui_visibility.seat_mode``（single/multi，运维可写死）→
  **可进工作台的启用账号数**（``PAGE_PERMISSIONS["workspace"]`` 的角色集＝
  master/admin/supervisor/agent；viewer 只读不接管故不算坐席）≥2 → multi；
- 回落 **multi＝显示**：藏错方向的代价不对称——真团队被藏掉认领会退回「两个人
  同时回同一个客户」，而单人多看一枚按钮只是噪音。读不到用户表/异常一律 multi。
- 消费方＝收件箱模板（``ws_multi_seat``：行内快捷认领键 / 「我的」筛选 / 会话卡
  认领按钮 / 认领轮询与自动续租全部随之熄灭）。**藏而不废**：
  ``/api/workspace/claim*`` 一律不封，加个第二账号刷新即恢复。

契约：
- 配置键 ``ui_visibility.{manual_console,group_extract,matrix_nav,group_show,
  team_collab,ai_settings}``，缺省 **False=隐藏**（新子系统默认关约定；开启是
  运营/开发决策，走 /developer 页写 overlay）。
- ``ui_visibility.flavor`` / ``ui_visibility.seat_mode`` 是**字符串**键，刻意
  不进 ``UI_VISIBILITY_KEYS`` 布尔家族（``resolve_ui_visibility`` 只搬已知布尔
  键，不会把它们透传进 ``/api/desktop/ui-flags`` 的 flags 里）。
- ``resolve_ui_visibility`` 纯函数：任何异常/坏类型回落缺省（fail-hidden——
  这些是内部功能，读不到配置时藏起来比露出来安全）。
- 消费方：admin.py ``_enrich_context`` 注入模板全局 ``ui_vis``；
  nav_schema ``get_nav_context(ui_visibility=)``；desktop 壳经
  ``GET /api/desktop/ui-flags``（developer_api_routes）。
"""

from __future__ import annotations

import os
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


# ── 部署形态 ─────────────────────────────────────────────────────────────────
FLAVOR_CLIENT = "client"
FLAVOR_PARTNER = "partner"
FLAVOR_INTERNAL = "internal"
FLAVOR_KEY = "flavor"
# 三层（低 → 高）：用户版 / 代理商版 / 研发版。上层看得见下层的一切。
FLAVOR_TIERS = (FLAVOR_CLIENT, FLAVOR_PARTNER, FLAVOR_INTERNAL)

# 开发者模式：session 键名（写入口 ui_visibility_routes；读 resolve_developer_mode）
DEVELOPER_MODE_KEY = "developer_mode"
DEV_UNLOCKED_KEY = "dev_unlocked"

_TRUTHY = ("1", "true", "yes", "on")


def _desktop_mode(config: Any) -> bool:
    """桌面/自包含部署判定。与 ``env_probe._is_desktop_mode`` 同口径（那边是
    启动期唯一事实源；这里 lazy import，导入失败时按同样两个信号自判，绝不
    因为一个探测函数把导航渲染带崩）。"""
    try:
        from src.bootstrap.env_probe import _is_desktop_mode
        return bool(_is_desktop_mode(config))
    except Exception:
        pass
    try:
        if str(os.environ.get("AITR_DESKTOP_MODE") or "").strip().lower() in _TRUTHY:
            return True
        app_cfg = (config or {}).get("app") if isinstance(config, dict) else None
        return bool(isinstance(app_cfg, dict) and app_cfg.get("desktop_mode", False))
    except Exception:
        return False


def resolve_ui_flavor(config: Any = None) -> str:
    """部署形态 → ``client`` | ``partner`` | ``internal``。纯判定，异常一律回落 internal。

    回落方向刻意与布尔键相反（那边 fail-hidden，这里 fail-**internal**＝不隐藏）：
    读不到配置时把运维页藏掉会让内部机器突然缺入口，而多显示一个入口对客户只是
    噪音——两害相权，宁可少藏。``partner`` 只能显式配置（桌面信号只推出 client）。
    """
    try:
        section = (config or {}).get(CONFIG_SECTION) if isinstance(config, dict) else None
        if isinstance(section, dict):
            raw = str(section.get(FLAVOR_KEY) or "").strip().lower()
            if raw in FLAVOR_TIERS:
                return raw
        return FLAVOR_CLIENT if _desktop_mode(config) else FLAVOR_INTERNAL
    except Exception:
        return FLAVOR_INTERNAL


def is_client_flavor(config: Any = None) -> bool:
    return resolve_ui_flavor(config) == FLAVOR_CLIENT


def resolve_developer_mode(session: Any = None) -> bool:
    """开发者模式是否生效：session 里 ``developer_mode`` 与 ``dev_unlocked`` 同真。

    只读 session、不读配置——这是「人」的临时状态而非部署属性；任何异常回落
    False（关着比开着安全：开发者模式会把研发面放回客户机侧栏）。
    """
    try:
        if session is None:
            return False
        return bool(session.get(DEVELOPER_MODE_KEY, False)) and \
            bool(session.get(DEV_UNLOCKED_KEY, False))
    except Exception:
        return False


def client_hide_active(config: Any = None, developer_mode: bool = False) -> bool:
    """「用户版隐藏」是否生效＝client 形态 **且** 未开开发者模式。

    模板全局 ``ui_client_hide`` 与 nav_schema 剔除判定共用这一个口径，
    保证侧栏藏了的页，页内入口 / 卡片同样藏；开发者模式一开两面同时回来。
    """
    return is_client_flavor(config) and not bool(developer_mode)


# ── 坐席规模（认领/释放这类协作原语的开关）──────────────────────────────────
SEAT_MODE_KEY = "seat_mode"
SEAT_MULTI = "multi"
SEAT_SINGLE = "single"

# 回落坐席角色集：与 web_user_store.PAGE_PERMISSIONS["workspace"] 同口径
# （那边是唯一事实源，import 失败才用这份拷贝——导航渲染不能被一次导入异常带崩）。
_SEAT_ROLES_FALLBACK = frozenset({"master", "admin", "supervisor", "agent"})


def _seat_roles() -> frozenset:
    try:
        from src.utils.web_user_store import PAGE_PERMISSIONS
        roles = PAGE_PERMISSIONS.get("workspace")
        if roles:
            return frozenset(roles)
    except Exception:
        pass
    return _SEAT_ROLES_FALLBACK


def count_seat_accounts(users: Any) -> int:
    """能进工作台的**启用**账号数。纯函数；坏行跳过而非整体放弃。

    ``users`` ＝ ``WebUserStore.list_users()`` 那样的 dict 序列。判据故意只看
    「角色能不能进工作台」+「账号是否启用」——viewer 只读不接管（认领对它无意义），
    停用账号更不是坐席。
    """
    roles = _seat_roles()
    n = 0
    try:
        for u in (users or []):
            if not isinstance(u, dict):
                continue
            if str(u.get("role") or "").strip().lower() not in roles:
                continue
            enabled = u.get("enabled", 1)
            # 旧库/异常值一律按启用算（宁可多算一个坐席＝多显示，不误藏协作功能）
            if enabled in (0, "0", False):
                continue
            n += 1
    except Exception:
        return 0
    return n


def resolve_seat_mode(config: Any = None, users: Any = None) -> str:
    """坐席规模 → ``multi`` | ``single``。异常/取不到用户表一律 multi（不误藏）。"""
    try:
        section = (config or {}).get(CONFIG_SECTION) if isinstance(config, dict) else None
        if isinstance(section, dict):
            raw = str(section.get(SEAT_MODE_KEY) or "").strip().lower()
            if raw in (SEAT_MULTI, SEAT_SINGLE):
                return raw
        if users is None:
            return SEAT_MULTI
        return SEAT_SINGLE if count_seat_accounts(users) < 2 else SEAT_MULTI
    except Exception:
        return SEAT_MULTI


def is_multi_seat(config: Any = None, users: Any = None) -> bool:
    return resolve_seat_mode(config, users) != SEAT_SINGLE
