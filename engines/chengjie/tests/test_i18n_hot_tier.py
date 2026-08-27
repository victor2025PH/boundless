# -*- coding: utf-8 -*-
"""扩展语「热路径覆盖」棘轮门禁（P1 2026-08-27）。

热路径 = 密封工作台页实际用到的键（scripts.i18n_scan 单一事实源，与 coverage
门禁同源）。扩展语的价值排序应跟坐席使用频率走——导航/收件箱/看板这些天天摸的
键先母语化，长尾管理页允许英文回落。

棘轮语义：**只许涨不许降**。地板值低于当前实际值（留模板演化余量）；
覆盖上去后请把地板抬到新水位。若因模板下线键导致假红，重新核算后调整地板并
在提交说明里写明原因。
"""

from scripts.i18n_scan import scan_workspace_i18n
from src.web.i18n_packs import EXTRA_LANGS, collect_all

# lang -> 热路径 override 键数地板（涨了就抬：2026-08-27 机翻跑批后实测
# vi=2867、th=2838、id=2867（各留 ~60 键模板演化余量）；
# zh_hant 由 i18n_hant 全量转换生成，实测≈全热路径）
_FLOORS = {"vi": 2800, "th": 2780, "id": 2800, "zh_hant": 2900}


def test_extra_langs_hot_tier_ratchet():
    hot = set(scan_workspace_i18n()["used_keys"])
    assert len(hot) > 1000, "热路径键集异常缩水（i18n_scan 抽取失效？）"
    _zh, _en, extras = collect_all()
    for lg in EXTRA_LANGS:
        floor = _FLOORS.get(lg, 1)
        covered = len(set(extras.get(lg, {})) & hot)
        assert covered >= floor, (
            f"{lg} 热路径覆盖跌破棘轮地板: {covered} < {floor}"
            f"（勿删热路径词条；如模板下线键导致，请核算后调整 _FLOORS 并说明）")


def test_floors_have_entry_for_every_extra_lang():
    """新增扩展语必须同时定地板——防「表加了语言、热路径无人管」的空转。"""
    missing = [lg for lg in EXTRA_LANGS if lg not in _FLOORS]
    assert not missing, f"EXTRA_LANGS 新语言未定热路径地板: {missing}"
