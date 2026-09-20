# -*- coding: utf-8 -*-
"""告警渠道面板「目标达成推送」一键预置接线门禁（2026-08-18）。

背景：goal_complete/goal_miss 别名与多渠道 notifier（TG/WA Cloud/飞书/企微/钉钉）
后端早已齐备，但运营要自己翻别名文档手配渠道——「成交时推送老板的 IM」这个
最高价值场景没有一步到位的入口。修法＝cp-accounts 告警渠道区顶部预置卡：
选平台加一行、预勾 goal_complete+goal_miss、复用既有行编辑/测试/保存机制
（刻意不造第二套渠道表单）。

钉住的都是「静默失效」高发位：
1. 预置按钮 data-act 已在 _onAction 派发表接线（漏接=按钮点了没反应）；
2. 预置行事件集＝goal_complete+goal_miss（错集=接了渠道却订错事件）;
3. cp-i18n 新词条 zh+en 双语齐（形状判据同 test_alert_audience_catalog：
   同键出现次数==2；只补一种语言=面板显示裸键名）；
4. 双宿主 cp-i18n 版本戳一致（unified_inbox 与 copilot app 各引一份，
   漂移=一边坐席拿旧词条看到裸键）。
双树镜像逐字节一致由 test_copilot_shared_sync 守，此处不重复。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CP_ACCOUNTS = REPO / "shared" / "copilot" / "components" / "cp-accounts.js"
CP_I18N = REPO / "shared" / "copilot" / "i18n" / "cp-i18n.js"
APP_HTML = REPO / "shared" / "copilot" / "app.html"
INBOX_TPL = REPO / "src" / "web" / "templates" / "unified_inbox.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_preset_action_wired_and_rendered():
    src = _read(CP_ACCOUNTS)
    # 派发表接线（IIFE 组件内 data-act 靠 _onAction 分发，漏了=死按钮）
    assert re.search(r'act === "wh-goal-preset"\)\s*return this\._whGoalPreset', src), \
        "_onAction 缺 wh-goal-preset 派发——预置按钮点了没反应"
    assert 'data-act="wh-goal-preset"' in src, "预置按钮没渲染"
    # 预置块必须进 _renderWebhooks 的输出（只定义不调用=面板上根本看不见）
    m = re.search(r"async _renderWebhooks\(\)\s*\{(.*?)\n    \}", src, re.S)
    assert m and "_whGoalPresetHtml()" in m.group(1), \
        "_renderWebhooks 没拼入 _whGoalPresetHtml()"


def test_preset_row_subscribes_goal_aliases():
    """预置行事件集＝goal_complete + goal_miss（与后端别名逐字对齐）。"""
    src = _read(CP_ACCOUNTS)
    m = re.search(r"_whGoalPreset\(el\)\s*\{(.*?)\n    \}", src, re.S)
    assert m, "缺 _whGoalPreset 方法"
    body = m.group(1)
    assert '"goal_complete"' in body and '"goal_miss"' in body, \
        "预置行必须预勾 goal_complete+goal_miss"
    # 别名真实存在于 notifier 注册表（防前端拼错字符串静默订阅不存在的别名）
    from src.inbox.webhook_notifier import _EVENT_ALIASES
    assert "goal_complete" in _EVENT_ALIASES and "goal_miss" in _EVENT_ALIASES
    # 行名唯一化（重名追加序号）——绝不覆盖运营已有渠道行
    assert "goal-push-" in body


def test_preset_platforms_cover_wa_and_cn_bots():
    """预置至少覆盖 Telegram/WhatsApp/飞书/企微/钉钉 五渠道（老板在哪都够到）。"""
    src = _read(CP_ACCOUNTS)
    m = re.search(r"_whGoalPresetHtml\(\)\s*\{(.*?)\n    \}", src, re.S)
    assert m, "缺 _whGoalPresetHtml 方法"
    body = m.group(1)
    for fmt in ("telegram", "whatsapp", "feishu", "wecom", "dingtalk"):
        assert f'"{fmt}"' in body, f"预置缺 {fmt} 渠道入口"


def test_preset_i18n_keys_bilingual_in_cp_i18n():
    """cp-i18n 形状＝zh/en 两个 dict，同键恰好出现 2 次；<2=漏语言，0=裸键名。"""
    src = _read(CP_I18N)
    needed = [
        "cp.acct.wh_goal_title", "cp.acct.wh_goal_desc",
        "cp.acct.wh_goal_added", "cp.acct.wh_goal_wa_hint",
        "cp.acct.wh_ch_whatsapp",
    ]
    problems = []
    for k in needed:
        n = src.count('"%s"' % k)
        if n < 2:
            problems.append("%s（出现 %d 次）" % (k, n))
    assert not problems, (
        "cp-i18n 词条缺失或只补了一种语言（面板会显示裸键名）：%s" % problems)


def test_cp_i18n_version_stamp_consistent_across_hosts():
    """cp-i18n.js 双宿主（unified_inbox + copilot app.html）?v= 必须同值——
    漂移＝一边坐席拿缓存旧词条，新键全部显示成裸键名。"""
    pat = re.compile(r'/copilot/i18n/cp-i18n\.js\?v=([A-Za-z0-9]+)')
    inbox_v = pat.search(_read(INBOX_TPL))
    app_v = pat.search(_read(APP_HTML))
    assert inbox_v and app_v, "宿主丢了 cp-i18n.js 的 ?v= 缓存戳"
    assert inbox_v.group(1) == app_v.group(1), (
        f"双宿主 cp-i18n 版本戳漂移：unified_inbox={inbox_v.group(1)} "
        f"app.html={app_v.group(1)}")
