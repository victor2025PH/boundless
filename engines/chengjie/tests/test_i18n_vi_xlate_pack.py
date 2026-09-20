# -*- coding: utf-8 -*-
"""vi 对话翻译域词包门禁（xlate P2，2026-08-16）。

三层不变量：
1. **全部 vi pack 的 VI 键 ⊆ ZH 合并视图**——``i18n_packs/__init__`` docstring 声明
   「键须已存在于 ZH/EN」但此前无门禁：orphan/typo 的 VI 键会静默沉在合并视图里
   永远不被消费。本条对所有 vi pack 生效（不止本域）。
2. 本域（vi_inbox_xlate）关键键在 ``get_translations('vi')`` 解析为越南语覆盖值
   （证明合并链没把覆盖丢在回落英文之下）。
3. VI 值非空 + 格式化占位符与 ZH 完全一致（``{lang}``/``{cust}`` 等 —— 前端 ``Tf``
   按 ZH 口径喂参，vi 多出/缺失占位符 = 运行时格式化事故）。
"""

import re

from src.web.i18n_packs import collect_packs
from src.web.i18n_packs import vi_inbox_xlate
from src.web.web_i18n import get_translations

_PH_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _placeholders(s: str) -> set:
    return set(_PH_RE.findall(s or ""))


def test_all_vi_pack_keys_exist_in_zh_merged():
    """VI 键 ⊆ ZH 合并视图（orphan 键 = typo 或键已被回收，必须点名）。"""
    _pzh, _pen, pvi = collect_packs()
    zh_all = get_translations("zh")
    orphans = sorted(k for k in pvi if k not in zh_all)
    assert not orphans, f"VI 键在 ZH 合并视图不存在（typo/已回收？）: {orphans[:20]}"


def test_xlate_domain_keys_resolve_to_vietnamese():
    """本域关键键在 vi 视图 == VI 覆盖值（非回落英文）。"""
    vi_view = get_translations("vi")
    spot = [
        "inbox.xl.pop_hd", "inbox.xl.quick_on", "inbox.xl.quick_on_done",
        "inbox.xl.recv", "inbox.xl.send_lbl", "inbox.xl.suggest",
        "inbox.xl.sendmode", "inbox.xl.mylang", "inbox.dlm.tx_hd",
        "inbox.dlm.add_lbl", "inbox.langwarn.fix", "dash.lang.vi",
        "inbox.rt.xl_status_off",
    ]
    for key in spot:
        assert key in vi_inbox_xlate.VI, f"关键键不在本域 VI 包: {key}"
        assert vi_view.get(key) == vi_inbox_xlate.VI[key], (
            f"vi 合并视图未取到 VI 覆盖值: {key} -> {vi_view.get(key)!r}"
        )


def test_vi_values_nonempty_and_placeholders_match_zh():
    """VI 值非空；占位符集合与 ZH 逐键一致（格式化契约）。"""
    zh_all = get_translations("zh")
    bad_empty = [k for k, v in vi_inbox_xlate.VI.items() if not str(v or "").strip()]
    assert not bad_empty, f"VI 空值: {bad_empty[:10]}"
    mismatched = []
    for k, v in vi_inbox_xlate.VI.items():
        zh_v = zh_all.get(k)
        if zh_v is None:
            continue  # 已由 orphan 门禁点名
        if _placeholders(v) != _placeholders(zh_v):
            mismatched.append((k, sorted(_placeholders(v)), sorted(_placeholders(zh_v))))
    assert not mismatched, f"VI 占位符与 ZH 不一致: {mismatched[:10]}"


def test_vi_merged_view_still_falls_back_to_english():
    """未覆盖键回落英文（部分包机制的另一半：不许出现裸键名）。

    2026-08-27 机翻跑批后 vi 覆盖 ~97%——固定探针键已被真译文覆盖，改为
    动态找一个仍未覆盖的键（全覆盖时跳过回落探针，与 test_i18n_extra_langs
    同口径）；机制本身另有 _merge_views 单元验证兜底。
    """
    from src.web.i18n_packs import collect_all
    vi_view = get_translations("vi")
    en_view = get_translations("en")
    zh_view = get_translations("zh")
    _z, _e, extras = collect_all()
    ov = extras.get("vi", {})
    probe = next((k for k in zh_view if k not in ov), None)
    if probe is None:
        return  # 全覆盖：无回落面可测
    assert vi_view.get(probe) == en_view.get(probe)
    assert vi_view.get(probe) != probe
