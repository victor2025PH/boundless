# -*- coding: utf-8 -*-
"""告警链路「最后一公里」接通部件接线门禁（2026-08-05）。

背景：出厂 0 通道 → 所有 EventBus 告警报进虚空（L3 草稿烂 167h / 半死态拖 4 天的
共同根因）。修法＝共享 partial ``_alertlink_connect.html``：工作台外壳横幅（admin
角色 + wsBanner 仲裁）+ 一键接通弹窗（测试先行：真发测试消息成功才允许保存），
零新后端——复用 alert-link-status / webhooks / webhooks/test 三条现成路由。

本文件钉住的不变量（都是「静默失效」型缺陷的高发位）：
1. partial 存在且暴露 ``window.wsAlertlinkOpen``（ops 卡按钮的全局入口）；
2. 兜底订阅集与 ``alert_link_audit.HIGH_VALUE_ALIASES`` 同集（双源漂移=接通了
   却覆盖不到高价值别名，自检永远黄灯）；正常路径消费状态接口的 focus_aliases；
3. workspace_base 以横幅模式引入、ops_overview 以纯弹窗模式引入 + CTA 按钮存在；
4. 权限门：partial 整体包在 admin 角色 Jinja 闸内（坐席不渲染，后端本就拒写）；
5. 测试先行纪律：保存按钮初始 disabled，JS 里存在 need_test 闸。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PARTIAL = REPO / "src" / "web" / "templates" / "_alertlink_connect.html"
WS_BASE = REPO / "src" / "web" / "templates" / "workspace_base.html"
OPS = REPO / "src" / "web" / "templates" / "ops_overview.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_partial_exists_and_exposes_global_entry():
    src = _read(PARTIAL)
    assert 'window.wsAlertlinkOpen' in src, "ops 卡按钮依赖的全局入口丢了"
    # 实施75 batch2：未接通提醒退役顶部横幅，迁右下胶囊+卡片（AITRNotify）
    assert 'id="ws-alertlink"' not in src, "告警链路横幅不得回归顶部（实施75）"
    assert "AITRNotify.ongoing.set('alertlink'" in src
    assert 'id="al-connect-mask"' in src
    # 复用现成后端，绝不新造路由
    assert "/api/admin/alert-link-status" in src
    assert "/api/accounts/auto-reply/webhooks/test" in src
    assert "/api/accounts/auto-reply/webhooks" in src


def test_fallback_events_pin_high_value_aliases():
    """兜底订阅集必须与 audit 的高价值别名同集——单一事实源防漂移。"""
    from src.integrations.alert_link_audit import HIGH_VALUE_ALIASES
    src = _read(PARTIAL)
    m = re.search(r"FALLBACK_EVENTS\s*=\s*\[([^\]]*)\]", src)
    assert m, "partial 里找不到 FALLBACK_EVENTS"
    fallback = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    assert fallback == set(HIGH_VALUE_ALIASES), (
        f"兜底订阅集 {fallback} ≠ HIGH_VALUE_ALIASES {set(HIGH_VALUE_ALIASES)}")
    # 正常路径吃状态接口回传的 focus_aliases（与自检覆盖判据同源）
    assert "focus_aliases" in src


def test_workspace_base_renders_banner_mode():
    src = _read(WS_BASE)
    assert '{% include "_alertlink_connect.html" %}' in src
    # 横幅模式旗标必须在 include 之前置位
    idx_flag = src.find("set alertlink_banner = true")
    idx_inc = src.find('{% include "_alertlink_connect.html" %}')
    assert 0 < idx_flag < idx_inc, "workspace_base 需先置 alertlink_banner 再 include"


def test_ops_overview_renders_modal_and_cta_button():
    src = _read(OPS)
    assert '{% include "_alertlink_connect.html" %}' in src
    assert "wsAlertlinkOpen()" in src, "ops 卡 no_channel CTA 按钮丢了"
    assert "ov2_al_connect_btn" in src
    # 按钮必须有权限回落：无 manage_ops / 部件未装载时退回纯文案
    assert "CAN_MANAGE_OPS" in src


def test_partial_is_admin_gated():
    src = _read(PARTIAL)
    head = src[:src.find("<div")]
    assert "user_role" in head and "can_manage_ops" in head, (
        "partial 必须整体包在 admin 角色闸内（坐席看到接通入口只会 403）")


def test_save_requires_successful_test_first():
    """测试先行纪律：保存钮初始 disabled；JS 有 need_test 闸（未测过拦下）。"""
    src = _read(PARTIAL)
    save_tag = re.search(r'<button[^>]*id="al-ct-save"[^>]*>', src)
    assert save_tag and "disabled" in save_tag.group(0)
    assert "al.ct.need_test" in src
    assert re.search(r"if\s*\(\s*!tested\s*\)", src), "保存路径丢了 tested 闸"


def test_i18n_keys_bilingual():
    from src.web.i18n_packs.alert_link_ops import EN, ZH
    needed = [
        "ov2_al_connect_btn", "ws.alertlink.text", "ws.alertlink.connect",
        "ws.alertlink.later", "al.ct.title", "al.ct.desc", "al.ct.how",
        "al.ct.token_ph", "al.ct.chat_ph", "al.ct.test", "al.ct.testing",
        "al.ct.test_ok", "al.ct.test_fail", "al.ct.save", "al.ct.saving",
        "al.ct.save_ok", "al.ct.save_fail", "al.ct.need_both",
        "al.ct.need_test", "al.ct.no_perm", "al.ct.close",
    ]
    for k in needed:
        assert k in ZH, f"ZH 缺 {k}"
        assert k in EN, f"EN 缺 {k}"


# ── 目标达成推送三灯（2026-08-18：扫描器/渠道订阅/本人绑定进接通弹窗）────────


def test_goal_push_section_wiring():
    """弹窗内目标推送状态区的接线不变量：

    1. 区块存在且默认隐藏（数据到达才显示——goals 关/旧后端整区不出现）；
    2. 状态读 /api/goals/notify-status，扫描器开关写 /api/goals/notify-settings
       （两条都是复用后端，绝不新造路由）；
    3. 非 200（403=goals 总闸关 / 404=旧后端无路由）→ 隐藏而非报错（特性探测）；
    4. openModal 必须触发状态加载（否则弹窗常显空区）。"""
    src = _read(PARTIAL)
    assert 'id="al-ct-goal"' in src
    goal_div = re.search(r'<div id="al-ct-goal"[^>]*>', src)
    assert goal_div and "display:none" in goal_div.group(0), "目标区必须默认隐藏"
    assert "/api/goals/notify-status" in src
    assert "/api/goals/notify-settings" in src
    # 特性探测：非 200 一律走隐藏分支
    assert re.search(r"if\s*\(\s*!r\.ok\s*\)\s*\{\s*goalBox\.style\.display='none'", src), \
        "notify-status 非 200 必须整区隐藏（goals 关/旧后端不裸奔）"
    # openModal 内必须拉取目标推送状态
    m = re.search(r"function openModal\(\)\{(.*?)\}", src, re.S)
    assert m and "_goalTick()" in m.group(1), "openModal 丢了 _goalTick()（弹窗空区）"
    # 渠道名来自服务端配置（用户可控字符串）→ 渲染前必须过转义
    assert re.search(r"function _esc\(", src), "目标区 JS 丢了转义 helper"


def test_goal_push_i18n_keys_bilingual():
    from src.web.i18n_packs.alert_link_ops import EN, ZH
    needed = [
        "al.ct.goal_title", "al.ct.goal_hint", "al.ct.goal_scan_on",
        "al.ct.goal_scan_off", "al.ct.goal_scan_btn", "al.ct.goal_scan_saving",
        "al.ct.goal_scan_ok", "al.ct.goal_scan_fail", "al.ct.goal_cover_on",
        "al.ct.goal_cover_off", "al.ct.goal_self_on", "al.ct.goal_self_off",
        "al.ct.goal_opt_title", "al.ct.goal_opt_agent", "al.ct.goal_opt_agent_t",
        "al.ct.goal_opt_miss", "al.ct.goal_opt_miss_t",
        "al.ct.goal_opt_profile", "al.ct.goal_opt_profile_t",
        "al.ct.goal_opt_fail",
    ]
    for k in needed:
        assert k in ZH, f"ZH 缺 {k}"
        assert k in EN, f"EN 缺 {k}"


def test_goal_push_option_toggles_match_server_whitelist():
    """推送选项开关与服务端白名单的防漂移钉（二批 2026-08-18）：

    1. 三开关（push_agent/miss_digest/include_profile）都渲染且经
       data-goal-flag 提交到 /api/goals/notify-settings；
    2. 开关键必须 ⊆ goal_routes._NOTIFY_FLAG_PATHS 白名单（UI 发了服务端
       不认的键=静默不生效，比没有开关更糟）；
    3. 写失败必须回滚勾选态（UI 绝不谎报已保存）。"""
    src = _read(PARTIAL)
    ui_keys = set(re.findall(r'data-goal-flag="([a-z_]+)"', src)) | set(
        re.findall(r"_gOpt\('([a-z_]+)'", src))
    assert {"push_agent", "miss_digest", "include_profile"} <= ui_keys, \
        f"推送选项开关缺键：{ui_keys}"
    # 服务端白名单是 register_goal_routes 函数局部量——从源码正则提取防漂移
    routes_src = (REPO / "src" / "web" / "routes" / "goal_routes.py").read_text(
        encoding="utf-8")
    m = re.search(r"_NOTIFY_FLAG_PATHS\s*=\s*\{(.*?)\}", routes_src, re.S)
    assert m, "goal_routes 丢了 _NOTIFY_FLAG_PATHS 白名单"
    server_keys = set(re.findall(r'"([a-z_]+)":\s*"companion\.', m.group(1)))
    unknown = ui_keys - server_keys
    assert not unknown, (
        f"弹窗开关提交了服务端白名单外的键（会被静默忽略）：{unknown}；"
        f"白名单={server_keys}")
    # 失败回滚：勾选态还原语句必须在（谎报已保存=运营以为改了其实没改）
    assert "cb.checked=!want" in src, "_goalSetFlag 丢了失败回滚勾选态"
