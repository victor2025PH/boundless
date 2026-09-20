# -*- coding: utf-8 -*-
"""实施75：全站弹出提醒统一（右下 toast + 消息中心）——静态契约门禁。

背景（2026-08-27 老板投诉实录）：顶部「服务刚完成重启…连环重启已自动抑制」
蓝条常驻且无关闭入口——当天 0:00-12:09 重启 14 次，每次点亮 10 分钟通栏横幅。
拍板目标形态：瞬态提示走右下 toast，常驻状态走右下「系统状态胶囊」+ 消息中心
留痕，顶部只留导航；新状态一律走 AITRNotify 统一总线。

本门禁钉住：
1) notify-bus 基建存在且导出契约完整（notify / ongoing.set / ongoing.clear）；
2) workspace_base 挂载了 notify-bus（带 ?v= 缓存戳）；
3) 顶部两条已退役横幅（ws-restartcool / ws-aidegrade）不得回归；
4) quiet_poll 功能信号（_setRestartCoolFlag → __wsRestartCool）在视觉迁移后原样保留，
   且必须在 cooldown 判定**之前**无条件调用（旧结构「div 不在就整段 return」会在
   横幅退役后把收件箱轮询降速信号一起弄丢——这是本次迁移最大的回归风险点）;
5) 新词条 pack 双语齐平且客户可见文案零运维黑话；
6) RPA 页 toast 委托右下 showToast（位置统一的第一批收编）；
7) 消息中心认识 sys_status 类型（合并去重 + 图标表 + 摘要行）。
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
WSB = REPO / "src" / "web" / "templates" / "workspace_base.html"
BUS = REPO / "src" / "web" / "static" / "workspace" / "notify-bus.js"
RPA_SHARED = REPO / "src" / "web" / "templates" / "_rpa_shared_scripts.html"
BASE = REPO / "src" / "web" / "templates" / "base.html"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def test_notify_bus_exists_with_contract():
    src = _read(BUS)
    assert "window.AITRNotify" in src
    assert "notify: notify" in src
    assert "set: function(id, o)" in src
    assert "clear: function(id)" in src
    # 胶囊容器 + 点击开通知中心 + a11y
    assert "ws-status-capsule" in src
    assert "ws-notif-btn" in src
    assert "aria-live" in src
    # CRMW 缺席的自带最小渲染（非工作台壳环境不哑火）
    assert "aitr-ntf-fallback" in src


def test_workspace_base_includes_bus_with_cache_stamp():
    src = _read(WSB)
    assert re.search(r"notify-bus\.js\?v=\d{8}", src), "notify-bus 挂载必须带 ?v= 缓存戳"


def test_top_banners_retired_and_capsule_wired():
    src = _read(WSB)
    assert 'id="ws-restartcool"' not in src, "重启冷却蓝条不得回归顶部（实施75）"
    assert 'id="ws-aidegrade"' not in src, "AI 降级横幅不得回归顶部（实施75）"
    # 不得再把这两个状态推回 wsBanner 仲裁器
    assert "wsBanner.set('ws-restartcool', true" not in src
    assert "wsBanner.set('ws-aidegrade', true" not in src
    # 新渲染面齐备
    assert "AITRNotify.ongoing.set('restartcool'" in src
    assert "AITRNotify.ongoing.set('aidegrade'" in src
    assert "AITRNotify.ongoing.clear('restartcool')" in src
    assert "AITRNotify.ongoing.clear('aidegrade')" in src


def test_quiet_poll_signal_survives_visual_migration():
    src = _read(WSB)
    fn = re.search(r"function _renderRestartCool\(ir\)\{(.*?)\n  \}", src, re.S)
    assert fn, "_renderRestartCool 必须存在"
    body = fn.group(1)
    # 信号必须在任何 return 之前无条件先设（视觉与信号解耦的核心不变量）
    first_stmt = body.strip().splitlines()[0].strip()
    assert first_stmt.startswith("_setRestartCoolFlag(ir)"), (
        "_setRestartCoolFlag 必须是 _renderRestartCool 第一条语句——"
        "横幅退役后 quiet_poll（收件箱轮询降速）不能跟着丢")
    assert "window.__wsRestartCool" in src


def test_episode_marker_dedups_restart_storms():
    src = _read(WSB)
    assert "function _ntfEpisode(" in src
    assert "aitr.ntf.ep." in src
    # 冷却 toast 只在世代开始时发一次
    assert re.search(r"ep === 'start'", src)


def test_i18n_pack_bilingual_and_no_ops_jargon():
    from src.web.i18n_packs import notify_center

    zh, en = notify_center.ZH, notify_center.EN
    assert set(zh) == set(en), "notify_center pack zh/en 键必须一一对应"
    required = {
        "ntf.type_sys", "ntf.capsule_title", "ntf.rc_toast",
        "ntf.rc_capsule", "ntf.rc_notif", "ntf.ai_capsule", "ntf.ai_recovered",
    }
    assert required <= set(zh)
    # 客户可见面禁运维黑话（实施75 §3.1 文案纪律）
    jargon = ("连环重启", "冷却保护", "熔断", "兜底", "抑制")
    for key, val in zh.items():
        for w in jargon:
            assert w not in val, f"{key} 含运维黑话「{w}」"
    # 占位符 zh/en 同构
    for key in required:
        assert set(re.findall(r"\{(\w+)\}", zh[key])) == set(re.findall(r"\{(\w+)\}", en[key])), key


def test_workspace_base_no_longer_renders_jargon_keys():
    src = _read(WSB)
    assert "ws.restartcool.hint" not in src, "「连环重启已自动抑制」黑话行已退役，不得回归"
    assert "ws.restartcool.text" not in src, "旧蓝条文案键已由 ntf.rc_* 取代"


def test_rpa_toast_delegates_bottom_right():
    src = _read(RPA_SHARED)
    m = re.search(r"rpa\.toast = function\(msg, kind\)\{(.*?)\n\};", src, re.S)
    assert m, "rpa.toast 必须存在"
    assert "window.showToast" in m.group(1), "RPA toast 必须优先委托 base 壳右下 showToast"
    # 兜底实现保留（独立片段场景）
    assert "_toastBox" in m.group(1)
    # warn 档委托依赖 base.html 的 .t-warn 样式
    assert ".t-warn{" in _read(BASE)


def test_batch2_top_banners_retired():
    """P1-batch2（2026-08-27）：除 chandown（生命周期线专批合流）与 conn-banner
    （收件箱列表内上下文条，非顶栏）外，全部顶部横幅退役且不得回归。"""
    src = _read(WSB)
    for bid in ("ws-expiry", "ws-delivblock", "ws-quotalow", "uqw-bar",
                "ws-uibuild", "ws-aiguide", "ws-aitrial"):
        assert ('id="%s"' % bid) not in src, f"{bid} 横幅不得回归顶部（实施75 batch2）"
    for oid in ("expiry", "delivblock", "quotalow", "uqw", "uibuild"):
        assert ("AITRNotify.ongoing.set('%s'" % oid) in src, f"{oid} 胶囊接线丢失"
    al = _read(REPO / "src" / "web" / "templates" / "_alertlink_connect.html")
    assert 'id="ws-alertlink"' not in al
    assert "AITRNotify.ongoing.set('alertlink'" in al


def test_batch2_bus_v2_contract():
    """总线 v2：动作按钮 / 置顶 / DOM 级同键去重 / dismiss / ongoing 快照 + 变更事件。"""
    src = _read(BUS)
    assert "actions" in src and "sticky" in src and "domKey" in src
    assert "dismiss: dismiss" in src
    assert "snapshot: function()" in src
    assert "aitr:ntf-ongoing" in src, "胶囊变更事件被移除——消息中心「进行中」分组会失联"


def test_batch2_base_shell_mounts_bus():
    """经典后台壳也挂总线（会话过期/能力闸/alertlink 在该壳同样要右下提示）。"""
    src = _read(BASE)
    assert re.search(r"notify-bus\.js\?v=\d{8}", src), "base.html 未挂载 notify-bus"
    # 管理壳陈旧页提醒同步迁移（adm-uibuild 顶栏条不得回归）
    assert 'id="adm-uibuild"' not in src


def test_batch2_center_ongoing_group_wired():
    """消息中心「进行中」置顶分组＝胶囊快照单源；胶囊变化面板实时重绘。"""
    src = _read(WSB)
    assert "ntf.ongoing_hdr" in src
    assert "AITRNotify.ongoing.snapshot" in src
    assert "addEventListener('aitr:ntf-ongoing'" in src
    from src.web.i18n_packs import notify_center
    assert "ntf.ongoing_hdr" in notify_center.ZH and "ntf.ongoing_hdr" in notify_center.EN


def test_batch2_session_expired_and_cap_denied_delegate():
    """_api_fetch 的会话过期/能力闸提示优先走总线（保留无总线回落）。"""
    src = _read(REPO / "src" / "web" / "templates" / "_api_fetch.html")
    assert "AITRNotify.ongoing.set('session'" in src
    assert "sticky: true" in src, "会话过期必须置顶（不可自动消失）"
    assert "AITRNotify.dismiss('session')" in src, "会话恢复必须撤下置顶卡片"
    assert "ws.cap.denied_msg" in src


def test_batch2_action_semantics_preserved():
    """迁移不丢出路：拦截卡片保留 待发队列/修复设置(角色闸)/报客服 三条；
    到期保留续费深链；额度保留充值/查看；引导保留 去配置/先不管。"""
    src = _read(WSB)
    assert "ws.delivblock.queue_link" in src
    assert "__wsDelivCanFix" in src and "ws.delivblock.fix_link" in src
    assert "sup.cta.banner" in src and "openSupportPanel" in src
    assert "ws.expiry.renew" in src
    assert "ws.quotawall.recharge" in src and "armWatch" in src
    assert "qw_low_shown" in src and "qw_low_buy" in src
    assert "ws_aiguide_dismissed" in src and "ws_aitrial_dismissed" in src


def test_batch3_chandown_migrated():
    """batch3：顶栏最后一条业务横幅（通道离线）迁右下——生命周期细节由
    test_chandown_lifecycle 钉住，这里钉「迁移完成 + 签名级去重 + 等待卡词条」。"""
    src = _read(WSB)
    assert 'id="ws-chandown"' not in src
    assert "AITRNotify.ongoing.set('chandown'" in src
    assert "dedupKey: 'chandown:' + sig" in src, "签名级去重被移除（同一异常集合会反复弹卡）"
    assert "domKey: 'chandown'" in src, "同键卡片原地更新被移除（会叠罗汉）"
    from src.web.i18n_packs import notify_center
    assert "ntf.chan_relogin_wait" in notify_center.ZH
    assert "ntf.chan_relogin_wait" in notify_center.EN


def test_top_banner_system_frozen():
    """禁新增顶部横幅（实施75 终局 ratchet）：全站 wsBanner.set(id, true) 只允许
    白名单两处——conn-banner（收件箱列表内上下文条，非顶栏 chrome）与
    ws-sessionexpired（统一总线缺席时的回落路径）。新状态一律走 AITRNotify；
    本门禁红了＝有人往顶栏塞新横幅（形态回潮）。"""
    allowed = {"conn-banner", "ws-sessionexpired"}
    web = REPO / "src" / "web"
    offenders = []
    for p in list(web.rglob("*.html")) + list(web.rglob("*.js")):
        src = p.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"wsBanner\.set\(\s*['\"]([\w-]+)['\"]\s*,\s*true", src):
            if m.group(1) not in allowed:
                offenders.append(f"{p.relative_to(web)}: {m.group(1)}")
    assert not offenders, (
        "新增顶部横幅（一律改走 AITRNotify 右下 toast/胶囊/消息中心）：\n  "
        + "\n  ".join(offenders))


def test_batch4_bus_alert_confirm_contract():
    """batch4：原生弹窗的统一替身——AITRNotify.alert（右下 toast；管理壳优先走
    showToast 保住报障 CTA 的 .toast.err 挂钩；全渲染面缺席回落原生绝不吞反馈）
    与 AITRNotify.confirm（Promise 中央卡片，Esc/外点=取消）。"""
    src = _read(BUS)
    assert "alert: uxAlert" in src and "confirm: uxConfirm" in src
    assert "window.showToast" in src, "管理壳 showToast 优先（报障 CTA 挂钩）被移除"
    assert "window.alert(msg)" in src, "alert 的原生回落被移除（渲染面全挂会吞反馈）"
    assert "window.confirm(" in src, "confirm 的原生回落被移除"
    assert "alertdialog" in src, "confirm 卡片 a11y role 被移除"


def test_batch4_alert_reclaimed_in_top_files():
    """batch4 第一批收编：rpa_overview / ops_overview 两大户 44 处 alert 清零
    （全部改走 AITRNotify.alert；成功/告警类带显式档位）。"""
    bare = re.compile(r"(?<![.\w$])alert\s*\(")
    for name in ("rpa_overview.html", "ops_overview.html"):
        src = _read(REPO / "src" / "web" / "templates" / name)
        assert not bare.search(src), f"{name} 出现裸 alert(（收编后不得回潮）"
        assert "AITRNotify.alert(" in src


def test_batch4_sys_status_server_persistence_wired():
    """batch4：sys_status 服务端留痕——总线 beacon + 路由按 id 合并。
    行为契约见 test_sys_status_notif_route.py；这里钉接线不断。"""
    src = _read(BUS)
    assert "/api/workspace/notifications/sys-status" in src, "总线留痕 beacon 被移除"
    route = _read(REPO / "src" / "web" / "routes" / "unified_inbox_batch_notif_routes.py")
    assert "notifications/sys-status" in route
    assert "sys_status" in route


# ── 原生弹窗 ratchet（实施75 P2 收编地基：总量只减不增）─────────────────────
# 2026-08-27 batch4 首批收编：rpa_overview(24) + ops_overview(20) 共 44 处
# alert → AITRNotify.alert，天花板 191→147 锁死战果。
## 收编台账：2026-08-27 191→147（并行线收 rpa_overview/ops_overview）→72
## （batch4 第二梯队：contact360/ai_studio/_channel_body_messenger/draft_review/
##   settings/monetization/agent_perf 共 75 处，成功类分档 'ok'/汇总 'info'）
_NATIVE_DIALOG_CEILINGS = {"alert": 72, "confirm": 135, "prompt": 24}
_NATIVE_DIALOG_RE = re.compile(r"(?<![.\w$])(alert|confirm|prompt)\s*\(")


def _native_dialog_counts():
    web = REPO / "src" / "web"
    totals = {"alert": 0, "confirm": 0, "prompt": 0}
    per: dict = {}
    for p in list(web.rglob("*.html")) + list(web.rglob("*.js")):
        src = p.read_text(encoding="utf-8", errors="ignore")
        c = {"alert": 0, "confirm": 0, "prompt": 0}
        for m in _NATIVE_DIALOG_RE.finditer(src):
            c[m.group(1)] += 1
        n = sum(c.values())
        if n:
            per[str(p.relative_to(web)).replace("\\", "/")] = c
            for k in totals:
                totals[k] += c[k]
    return totals, per


def test_native_dialog_ratchet():
    """阻塞式系统弹窗（alert/confirm/prompt）绕过 i18n/主题/统一形态：总量只减
    不增，新交互一律走 AITRNotify.notify / _appConfirm / _wsConfirm。"""
    totals, per = _native_dialog_counts()
    over = {k: (totals[k], v) for k, v in _NATIVE_DIALOG_CEILINGS.items()
            if totals[k] > v}
    if over:
        tops = sorted(per.items(), key=lambda kv: -sum(kv[1].values()))[:8]
        detail = "\n  ".join(f"{f}: {c}" for f, c in tops)
        raise AssertionError(
            f"原生弹窗新增（超天花板）{over}；当前大户 Top8：\n  {detail}")


def test_native_dialog_ledger_not_stale():
    totals, _ = _native_dialog_counts()
    stale = {k: (totals[k], v) for k, v in _NATIVE_DIALOG_CEILINGS.items()
             if totals[k] < v}
    assert not stale, f"收编有成果未收台账（天花板高于实际，请下调锁死战果）：{stale}"


def test_notif_center_understands_sys_status():
    src = _read(WSB)
    assert "sys_status:1" in src.replace(" ", "").replace("\n", "") or "sys_status: 1" in src, (
        "sys_status 必须进 _COALESCE_TYPES（同 id 合并 + 不计未读角标）")
    assert re.search(r"sys_status:\s*\{\s*iconName", src), "sys_status 必须有 _TYPE_META 图标表条目"
    assert "n.type==='sys_status'" in src, "_renderItem 必须有 sys_status 摘要行分支"
