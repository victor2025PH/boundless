# -*- coding: utf-8 -*-
"""N-2 B（#239，P2）：克隆音色登记后反馈缺失（2026-09-07）。

事实（R8ECFS，skuio，1.0.76）：STEVEN 语音 tab 登记 STEVEN.ogg，后端 13:50–13:51 `backend=avatar_clone
voice=STEVEN` 四次合成、`[voice/tts-test] 有声终审 speech=voiced`——**后端全部成功**；界面：① toast
「克隆音色已登记并挂载到本人设: avatar_clone」约 1s 消失且含内部名；② 登记后「试听」按钮消失（预置声
选择器含唯一的 ▶ 试听键，三态切 clone 即隐藏）、四段试听音频无处播放；③「音色体检：暂无体检数据」未回填；
④ 底部仍「有未保存的修改」（<cp-voice> 登记表单在抽屉里，其 input 事件冒泡把抽屉标脏），登记与保存关系不明。

钉住（本条做登记反馈；③ 体检数据回填归 N-4，这里只留占位 + 数据钩子）：
- 常驻行：登记就绪 → `psn_vc_bound`「已登记 {file}」+ `#pe-vc-preview` ▶ 试听 + 解绑同排显示；
- `peVcPreview`：`/api/voice/tts-test` 带 persona_id（走本人设克隆档），`<audio controls>` 常驻在卡上；
  每次试听写 `window.__PSN_VOICE_AUDITION[pid]` + 派发 `persona-voice-audition`（N-4 钩子）；
- toast：`_peVcToast` ≥ 8s、可关闭（×）、内嵌 ▶、文案用录音文件名不用 backend 内部名、明说「登记已写入人设并生效，无需再点保存」；
- 抽屉脏标记忽略来自 `#pe-vc-card` 的 input/change；
- 顺带：`_presetPreview` 两处中文 fallback 清掉（`test_sealed_pages_no_cjk_in_own_scripts[personas.html]` 转绿）。
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates" / "personas.html"


def _html() -> str:
    return _TPL.read_text(encoding="utf-8")


def _seg(html: str, start: str, end: str) -> str:
    i = html.index(start)
    j = html.index(end, i)
    return html[i:j]


def test_persistent_enrolled_row_has_play_and_unbind():
    html = _html()
    card = _seg(html, 'id="pe-vc-card"', "<cp-voice mode=\"enroll\"")
    assert 'id="pe-vc-status"' in card and 'id="pe-vc-preview"' in card and 'id="pe-vc-unbind"' in card
    assert 'onclick="peVcPreview()"' in card and 'id="pe-vc-player"' in card
    assert "psn_vc_persist_note" in card, "登记即生效要在卡上明示"
    sync = _seg(html, "async function peVcSync(", "window.__PSN_VOICE_AUDITION = ")
    ready = sync[sync.index("row.is_clone && row.ready"):sync.index("} else if (row && row.is_clone)")]
    assert "psn_vc_bound" in ready and "pv.style.display = ''" in ready and "ub.style.display = ''" in ready
    assert "_peVcFile =" in ready


def test_preview_uses_persona_clone_profile_and_feeds_voice_check_hook():
    html = _html()
    pv = _seg(html, "async function peVcPreview(", "function _peVcToast(")
    assert "'/api/voice/tts-test'" in pv and "persona_id: pid" in pv, "试听必须按本人设克隆档解析"
    assert "voice_cfg_override" not in pv, "克隆试听不得覆盖成预置声"
    assert "<audio controls" in pv and "pl.style.display = ''" in pv
    assert "window.__PSN_VOICE_AUDITION[pid] = rec" in pv and "'persona-voice-audition'" in pv
    assert "voice_meta: d.voice_meta" in pv
    assert "psn_voice_preview_fail" in pv
    # 体检行占位 + 钩子名（交 N-4）
    assert 'id="pe-vq-audition" data-hook="persona-voice-audition"' in html
    # 不在本条渲染「已核有声 / 疑似无声」口径（N-4 边界）
    assert "疑似无声" not in pv and "speech ===" not in pv


def test_enrolled_toast_is_long_closable_and_names_recording_not_backend():
    html = _html()
    toast = _seg(html, "function _peVcToast(", "if (typeof _fillDrawerFields === 'function')")
    assert "dur || 8000" in toast and "×" in toast and "psn_vc_toast_close" in toast
    assert 'onclick="peVcPreview()"' in toast, "toast 内嵌 ▶ 试听"
    after = _seg(html, "async function peVcAfterEnrolled(", "document.addEventListener('cp-voice-enrolled'")
    assert "_peVcToast(" in after and "psn_vc_enrolled_ok" in after and "9000" in after
    assert "psn_vc_enrolled_synced" not in after, "旧 toast 含内部 backend 名，不再使用"
    assert "{backend:" not in after
    from src.web.i18n_packs.persona_studio import EN, ZH
    for k in ("psn_vc_enrolled_ok", "psn_vc_persist_note", "psn_vc_audition_of", "psn_vc_toast_close", "psn_vc_bound"):
        assert ZH.get(k) and EN.get(k), k
        assert set(re.findall(r"\{(\w+)\}", ZH[k])) == set(re.findall(r"\{(\w+)\}", EN[k])), k
    assert "avatar_clone" not in ZH["psn_vc_enrolled_ok"] and "无需再点" in ZH["psn_vc_enrolled_ok"]
    assert ZH["psn_vc_bound"].startswith("已登记 ")


def test_drawer_dirty_ignores_enroll_widget_events():
    html = _html()
    fn = _seg(html, "function _markDrawerDirty(e) {", "function _clearDrawerDirty()")
    assert "closest('#pe-vc-card')) return;" in fn
    # 守卫必须在标脏之前
    assert fn.index("closest('#pe-vc-card')") < fn.index("classList.add('show')")


def test_preset_preview_no_cjk_fallback_left():
    html = _html()
    pp = _seg(html, "async function _presetPreview(", "function _vmRefName(")
    assert "window.Tf('psn_voice_preview_text', {name: nm})" in pp
    assert "window.Tf('psn_voice_preview_fail'" in pp
    assert not re.search(r"window\.T\('psn_voice_preview_(text|fail)',\s*'", pp), "中文 fallback 又回来了"
