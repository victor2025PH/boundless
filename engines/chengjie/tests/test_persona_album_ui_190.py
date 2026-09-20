"""#190 人设编辑器相册 tab 体验回归钉（J-7 B，2026-09-05）。

skuio 实录四点（K5XHJ2）：① 「试触发」长在 325 张卡底下；② 每张卡一整套表单；③ 触发词全空
没人引导；④ 卡内「保存」与抽屉「保存」两套。另：实施90 的相册 UI 词条（AI 建议触发词 chips /
状态角标 / 搜索筛选 / 试触发被拦门）09-01 进了 i18n 包，模板侧却从未落地——`auto_meta.
triggers_suggest` 一直在库里没人消费。本测试钉住模板形状（静态）+ 词条双语，不跑浏览器。
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates" / "personas.html"


def _src() -> str:
    return _TPL.read_text(encoding="utf-8")


def test_test_trigger_bar_is_sticky_and_above_grid():
    s = _src()
    # 吸顶容器里放试触发输入；网格容器在它之后（DOM 顺序＝body.innerHTML 拼接顺序）
    render = s[s.index("function _pmaRender("):s.index("function _pmaRenderGrid(")]
    assert 'class="pma-sticky"' in render
    assert render.index('id="pma-test-in"') < render.index('id="pma-grid-wrap"')
    assert "capbar + faceref + uploader + test + summary" in render, "试触发块必须排在网格之前"
    assert re.search(r"\.pma-sticky\{position:sticky;top:", s)
    # 命中即定位：候选可点 + 首个命中自动滚到并高亮
    assert "_pmaLocate(cands[0].id" in s
    assert "classList.add('pma-hit')" in s
    # 被拦门（实施90 blocked_by）用既有词条讲清
    assert "pma_test_blocked" in s and "pma_gate_tod" in s


def test_card_is_compact_with_advanced_details():
    s = _src()
    card = s[s.index("function _pmaCard(raw)"):s.index("function _pmaCollect(")]
    # 默认只显缩略图 + 触发词；配文/多语配文/权重/门槛收进 <details class="pma-adv">
    adv_at = card.index('<details class="pma-adv">')
    for cls in ("pma-cap\"", "pma-cap-i18n", "pma-wt", "pma-bd"):
        assert card.index(cls) > adv_at, f"{cls} 必须在「高级」折叠区内"
    assert card.index("pma-trg") < adv_at, "触发词输入必须在折叠区之外"
    # 卡内不再有「保存」按钮（统一到底部保存条）
    assert "pma_save_btn" not in card
    assert "pmaSaveItem" not in s, "逐卡保存函数应已移除"
    # 缩略图优先 thumb_url（325 张全上原图首屏拖死）
    assert "raw.thumb_url || raw.url" in card


def test_ai_suggested_triggers_consumed_and_adoptable():
    s = _src()
    assert "meta.triggers_suggest" in s, "auto_meta.triggers_suggest 必须被 UI 消费"
    assert "_pmaAdoptTrigger(" in s and "pma_adopt_all" in s and "pma_sug_trg" in s
    assert "pmaRetagAll" in s and "/media/retag-all" in s, "整册补标入口（建议词由此而来）"
    assert "/retag'" in s and "pmaRetagOne" in s
    # 打标进行中轮询刷新（pending 消失即停）
    assert "function _pmaMaybePollAi" in s


def test_upload_guides_trigger_filling():
    s = _src()
    up = s[s.index("async function pmaUpload("):s.index("async function pmaDeleteItem(")]
    assert "_pmaSetFilter('notrg')" in up
    assert "pma_need_trg_toast" in up
    assert ".pma-trg" in up and "focus()" in up


def test_unified_save_bar_and_dirty_tracking():
    s = _src()
    assert "var _pmaEdits = {}" in s
    assert "function _pmaOnEdit(card)" in s
    assert 'oninput="_pmaOnEdit(this)" onchange="_pmaOnEdit(this)"' in s
    assert "async function pmaSaveAll()" in s and "function pmaDiscardAll()" in s
    assert re.search(r"\.pma-savebar\{position:sticky;bottom:", s)
    # 翻页/筛选重渲不丢输入：显示值＝库值 + 未保存改动
    assert "function _pmaItemView(it)" in s and "_pmaEdits[String(it.id)]" in s
    # 切人设作废未保存改动要点破，不静默
    assert "pma_unsaved_dropped" in s


def test_paging_and_filters():
    s = _src()
    assert "var _PMA_PAGE = 60" in s
    assert "_pmaShowMore" in s and "pma_more" in s
    assert "pma_search_ph" in s and "pma_f_notrg" in s and "pma_count_shown" in s


def test_new_keys_bilingual():
    from src.web.i18n_packs.persona_apply_modal import EN, ZH
    for k in ("pma_f_notrg", "pma_notrg_n", "pma_more", "pma_adv", "pma_need_trg_ph",
              "pma_need_trg_toast", "pma_unsaved_n", "pma_save_all", "pma_saved_n",
              "pma_discard_all", "pma_discard_confirm", "pma_unsaved_dropped"):
        assert k in ZH and k in EN, k
    for k in ("pma_more", "pma_save_all", "pma_saved_n", "pma_discard_confirm",
              "pma_unsaved_n", "pma_unsaved_dropped", "pma_notrg_n", "pma_need_trg_toast"):
        assert "{n}" in ZH[k] and "{n}" in EN[k], k
