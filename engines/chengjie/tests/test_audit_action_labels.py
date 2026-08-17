# -*- coding: utf-8 -*-
"""审计动作 → 人话词条 契约门禁（2026-08-05）。

背景：aud_act_* 词典（i18n_packs/audit_actions.py）靠"新增动作时顺手补双语"
的自觉维护——2026-08 初新增的 episodic_*/identity_* 7 个动作全部漏登记，
首页「最近操作」与 /audit 直接把英文枚举漏给运营。本门禁把自觉变成契约：

**全库静态扫描 audit*.log(...) / _audit_identity(...) 的字面 action 名，
每个都必须在 pack 的 ZH 与 EN 两侧有 aud_act_<action> 词条。**

刻意不做反向检查（pack 有键但扫不到调用点 ≠ 死键）：wrapper 内部
``audit.log(actor, action, ...)`` 传变量的动作提取不到，反向断言会误报。
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# receiver 形如 audit / audit_store / ctx.audit_store / self._audit 等；
# 第一参允许一层括号嵌套（session.get("username", "web_admin") 这类），
# 防止嵌套调用里的字面量被误当 action 捕获。
_LOG_CALL = re.compile(
    r"""(?:\baudit\w*|_audit)\s*\.log\(\s*
        (?:[^,()'"]|\([^()]*\)|'[^']*'|"[^"]*")+?
        ,\s*['"]([a-z0-9_]+)['"]""",
    re.VERBOSE,
)
# episodic_identity_routes 的薄包装（第二参即 action 字面量）
_WRAPPER_CALL = re.compile(r"""_audit_identity\(\s*request\s*,\s*['"]([a-z0-9_]+)['"]""")


def _collect_literal_actions():
    found = {}
    for p in SRC.rglob("*.py"):
        text = p.read_text(encoding="utf-8", errors="ignore")
        for m in list(_LOG_CALL.finditer(text)) + list(_WRAPPER_CALL.finditer(text)):
            found.setdefault(m.group(1), set()).add(
                str(p.relative_to(SRC.parent)))
    return found


def test_every_literal_audit_action_has_bilingual_label():
    from src.web.i18n_packs import audit_actions

    found = _collect_literal_actions()
    # 提取器自证：全库现有 100+ 个字面动作，抽不到说明正则坏了（门禁失效比红更糟）
    assert len(found) >= 80, f"扫描仅得 {len(found)} 个动作，提取正则疑似失效"

    missing = {}
    for action, files in sorted(found.items()):
        key = f"aud_act_{action}"
        lack = [side for side, d in (("ZH", audit_actions.ZH), ("EN", audit_actions.EN))
                if key not in d]
        if lack:
            missing[action] = {"lack": lack, "files": sorted(files)}
    assert not missing, (
        "以下审计动作缺人话词条（i18n_packs/audit_actions.py 补 aud_act_<action>，"
        f"zh+en 双语）：{missing}"
    )


def test_extractor_skips_nested_call_arguments():
    """防回归自证：session.get('username', 'web_admin') 里的字面量不得被当 action。"""
    sample = (
        'audit_store.log(request.session.get("username", "web_admin"),\n'
        '                "update_template", target, old, new)\n'
    )
    got = _LOG_CALL.findall(sample)
    assert got == ["update_template"], got


# 动态拼接的 action（f"import_config_{mode}" 等）字面量扫描天然抓不到，
# 人工登记在此台账；条目要求：① 词典必须有双语标签（防止将来改枚举后
# 词条漂移成裸键）② 拼接调用点必须还存在（防台账过期变摆设）。
_KNOWN_DYNAMIC_ACTIONS = {
    "import_config_overwrite": 'f"import_config_{mode}"',
    "import_config_merge": 'f"import_config_{mode}"',
}


def test_known_dynamic_actions_labeled_and_still_dynamic():
    from src.web.i18n_packs import audit_actions

    lack = [a for a in _KNOWN_DYNAMIC_ACTIONS
            if f"aud_act_{a}" not in audit_actions.ZH
            or f"aud_act_{a}" not in audit_actions.EN]
    assert not lack, f"动态动作缺双语词条: {lack}"

    all_src = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore") for p in SRC.rglob("*.py"))
    stale = [frag for frag in set(_KNOWN_DYNAMIC_ACTIONS.values())
             if frag.strip('f"').split("{")[0] not in all_src]
    assert not stale, f"台账里的动态拼接调用点已不存在，请更新 _KNOWN_DYNAMIC_ACTIONS: {stale}"
