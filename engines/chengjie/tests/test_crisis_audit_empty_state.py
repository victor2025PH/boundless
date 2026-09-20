# -*- coding: utf-8 -*-
"""客户安全预警页（原危机审计）空态门禁。

2026-08-18 P1：生产常态是 0 危机事件，裸空表看着像页面坏了 → 「安全体检」好消息空态。
2026-09-05 #185（J-8 A，P1 安全）：**「功能没开」与「没有事件」曾长一个样**——留痕
（crisis_audit）出厂关，页面照样 0 条 + 「这是好消息」，让人以为被保护着。契约改为三档：

1. ``audit_off``：路由回 state=audit_off → 红色横幅（ca_js020/021）+ 一键开启（ca_js022，
   仅 master/admin 可点）；正文**绝不**出现 ca_js010「好消息」；
2. ``audit_on_empty``：留痕开着且近 N 天 0 条 → 才是好消息（ca_js025 带 {n} + ca_js012）；
3. ``has_events``：正常表格；仅未处理/带客户筛选的空 → 普通「无匹配」+「查看已处理历史」；
4. 旧后端无 state 字段 → 「留痕状态未知」（ca_js028），既不亮红也不说好消息；
5. 术语人话化：页面不再出现 R4/R6/R8 与配置键 ``companion.wellbeing.crisis_audit``；
   等级/类别/标记全部走人话词条；
6. 全部新键 zh/en 双语齐备（pack crisis_audit_page）。
"""
from __future__ import annotations

import re
from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"


def _src() -> str:
    return (_TPL / "crisis_audit.html").read_text(encoding="utf-8")


def _js(src: str) -> str:
    m = re.search(r"<script>(.*)</script>", src, re.S)
    assert m, "模板缺 <script> 块"
    return m.group(1)


def test_three_empty_states_are_distinct_branches():
    js = _js(_src())
    assert "state === 'audit_off'" in js, "空态必须有 audit_off 分支"
    assert "state === 'audit_on_empty'" in js, "空态必须有 audit_on_empty 分支"
    assert "state === 'unknown'" in js, "旧后端无 state 字段要走未知分支"
    for marker in ('data-empty="audit_off"', 'data-empty="audit_on_empty"',
                   'data-empty="unknown"', 'data-empty="no_match"'):
        assert marker in js, f"空态缺可测标记 {marker}"


def test_audit_off_never_says_good_news():
    """audit_off 分支体内不得消费「好消息」键 ca_js010；红条必须消费 ca_js020/021/022。"""
    js = _js(_src())
    # 取 audit_off 分支到下一个 else 之间的正文
    seg = js.split("state === 'audit_off'", 1)[1].split("} else if", 1)[0]
    assert "ca_js010" not in seg
    assert "ca_js020" in seg and "ca_js021" in seg
    # 横幅渲染函数：red banner + 一键开启按钮 → caEnableAudit（innerHTML 拼出的 onclick）
    assert "function caRenderBanner" in js
    banner = js.split("function caRenderBanner", 1)[1].split("async function caEnableAudit", 1)[0]
    assert 'data-state="audit_off"' in banner
    # 红条按开关判（留痕关着但有历史 → 表格照常 + 红条仍在），不只看空态档
    assert "d.audit_on === false" in banner
    assert "ca_js020" in banner and "ca_js021" in banner and "ca_js022" in banner
    assert "caEnableAudit(true)" in banner
    assert "ca_js010" not in banner


def test_audit_on_empty_uses_window_days_placeholder():
    js = _js(_src())
    seg = js.split("state === 'audit_on_empty'", 1)[1].split("} else if", 1)[0]
    assert "window.Tf('ca_js025'" in seg and "window_days" in seg
    assert "ca_js012" in seg
    # 旧「好消息」标题键仍双语存在（历史兼容），但不再是无条件默认分支
    assert "ca_js010" not in seg


def test_enable_button_hits_enable_endpoint_and_reloads():
    js = _js(_src())
    assert "async function caEnableAudit" in js, "一键开启必须是顶层函数（全局可达）"
    seg = js.split("async function caEnableAudit", 1)[1].split("async function caLoad", 1)[0]
    assert "/api/crisis-events/enable" in seg
    assert "method: 'POST'" in seg
    assert "caLoad()" in seg, "开启成功后必须重载列表让红条消失"
    assert "ca_js023" in seg and "ca_js024" in seg


def test_show_all_button_is_global_function():
    src = _src()
    assert "function caShowAll()" in src, "caShowAll 必须是顶层函数声明"
    assert "caShowAll()" in src and "ca_js011" in src
    assert "ca-only-unhandled" in src


def test_jargon_and_config_keys_gone_from_page():
    src = _src()
    assert "companion.wellbeing.crisis_audit" not in src, "配置键不得出现在页面上"
    assert "R4→R6→R8" not in src and "R4" not in re.sub(r"<!--.*?-->", "", src, flags=re.S)
    assert "ca_s003" not in src, "「安全链留痕 · 需开启」黑话已下线"
    assert "ca_s001" not in src and "ca_s002" not in src, "页名改走 ca_s020"
    assert "ca_s020" in src and "ca_s021" in src
    # 搜索标签人话化 + 图例三态
    assert "ca_s022" in src and "ca_s023" in src
    for k in ("ca_s024", "ca_s025", "ca_s026", "ca_s027", "ca_s028", "ca_s029", "ca_s030"):
        assert k in src, f"图例缺 {k}"
    # JS 层：等级/类别人话映射
    js = _js(src)
    assert "ca_js030" in js and "ca_js031" in js, "等级徽标人话（严重/偏高）"
    assert "self_harm: 'ca_js032'" in js and "despair: 'ca_js033'" in js
    assert "audio_distress: 'ca_js034'" in js


def test_keys_bilingual_and_no_good_news_in_off_banner():
    from src.web.i18n_packs.crisis_audit_page import EN, ZH

    keys = [
        "ca_js010", "ca_js011", "ca_js012",
        "ca_s020", "ca_s021", "ca_s022", "ca_s023", "ca_s024", "ca_s025", "ca_s026",
        "ca_s027", "ca_s028", "ca_s029", "ca_s030", "ca_s031", "ca_s032", "ca_s033",
        "ca_js020", "ca_js021", "ca_js022", "ca_js023", "ca_js024", "ca_js025",
        "ca_js026", "ca_js027", "ca_js028", "ca_js030", "ca_js031", "ca_js032",
        "ca_js033", "ca_js034",
    ]
    for key in keys:
        assert ZH.get(key) and EN.get(key), f"{key} 缺双语"
    # 好消息语气只留给 audit_on 档；audit_off 横幅文案里不许出现
    assert "好消息" in ZH["ca_js010"] and "good news" in EN["ca_js010"].lower()
    for k in ("ca_js020", "ca_js021", "ca_js022"):
        assert "好消息" not in ZH[k] and "good news" not in EN[k].lower()
    # 红条必须把「仍在识别」与「不记录、不通知」两层意思都讲出来
    assert "仍在生效" in ZH["ca_js021"] and "不会通知" in ZH["ca_js021"]
    assert "{n}" in ZH["ca_js025"] and "{n}" in EN["ca_js025"]
    # 页名 = 人话
    assert ZH["ca_s020"] == "客户安全预警"
    assert EN["ca_s020"] == "Customer Safety Alerts"


def test_nav_and_help_terms_renamed():
    from src.web.help_terms import HELP_TERMS
    from src.web.nav_schema import NAV_ITEMS
    from src.web.web_i18n import get_translations

    assert NAV_ITEMS["crisis_audit"]["label_zh"] == "客户安全预警"
    assert get_translations("zh")["crisis_audit"] == "客户安全预警"
    assert get_translations("en")["crisis_audit"] == "Customer Safety Alerts"
    ht = HELP_TERMS["nav_crisis_audit"]
    assert ht["zh"] == "客户安全预警" and ht["en"] == "Customer Safety Alerts"
    assert "留痕已开启" in ht["usage"]


def test_page_role_gate_excludes_viewer():
    """高敏页只给主管/合规：viewer 不再在矩阵里（隐私靠角色门，不靠关留痕）。"""
    from src.utils.web_user_store import (
        PAGE_PERMISSIONS, ROLE_ADMIN, ROLE_MASTER, ROLE_SUPERVISOR, ROLE_VIEWER,
    )

    allowed = PAGE_PERMISSIONS["crisis_audit"]
    assert ROLE_VIEWER not in allowed
    assert {ROLE_MASTER, ROLE_ADMIN, ROLE_SUPERVISOR} <= allowed
