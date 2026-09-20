# -*- coding: utf-8 -*-
"""副驾 App 壳静态词条 + 出图错误码 i18n 契约门禁（2026-08-22）。

实锤背景：8-22 早晨坐席壳里「AI 生成图片」卡标题显示裸键 ``cp.app.h_image``——
键本身在 cp-i18n.js 里**有**，但 app.html 引字典的 ``?v=`` 停在 20260821a、
webview 用的是缓存旧字典（缺 8-22 新增键 → ``t()`` 回落裸键名）。缓存问题靠
bump 双戳修，**漏键问题**从此由本门禁静态钉死：

① app.html 所有 ``data-cp-i18n``/``-ph``/``-title`` 引用键必须存在于 zh+en 词典
   （缺任一语言＝对应语言界面裸键）；
② 出图失败错误码契约：路由 ``KNOWN_ERROR_CODES`` 每个码（除 unknown 走通用
   回落）必须有 ``cp.image.errc_<code>``/``errs_<code>`` 双语词条——后端新增码
   忘配词条时这里先红，坐席不再看见生错误码当标题。
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_cp_i18n_parity import _parse

_REPO = Path(__file__).resolve().parents[1]
_APP = _REPO / "shared" / "copilot" / "app.html"

_ATTR = re.compile(r'data-cp-i18n(?:-ph|-title)?="([^"]+)"')

# 前端自造码（不经后端）：网络层失败。与后端契约表并集参与词条校验。
_FRONTEND_ONLY_CODES = ("network",)


def _dicts():
    zh, en, suspects = _parse()
    assert not suspects, f"cp-i18n.js 词条格式漂移：{suspects[:5]}"
    return zh, en


def test_app_html_static_keys_exist_bilingual():
    zh, en = _dicts()
    keys = set(_ATTR.findall(_APP.read_text(encoding="utf-8")))
    assert keys, "app.html 未扫出任何 data-cp-i18n 键——解析器疑似失效"
    miss_zh = sorted(k for k in keys if k not in zh)
    miss_en = sorted(k for k in keys if k not in en)
    assert not miss_zh, f"app.html 引用键缺 zh 词条（中文界面裸键）：{miss_zh}"
    assert not miss_en, f"app.html 引用键缺 en 词条（英文界面裸键）：{miss_en}"


def test_image_error_code_i18n_contract():
    from src.web.routes.image_gen_routes import KNOWN_ERROR_CODES

    zh, en = _dicts()
    need = [c for c in KNOWN_ERROR_CODES if c != "unknown"]
    need += list(_FRONTEND_ONLY_CODES)
    missing = []
    for code in need:
        for k in (f"cp.image.errc_{code}", f"cp.image.errs_{code}"):
            if k not in zh or k not in en:
                missing.append(k)
    # 通用回落词条（unknown / 未配码时的标题与建议）必须在
    for k in ("cp.image.err_ttl_generic", "cp.image.errs_unknown"):
        if k not in zh or k not in en:
            missing.append(k)
    assert not missing, (
        "出图错误码缺 i18n 词条（坐席将看到生错误码/裸键当标题）："
        f"{sorted(set(missing))}")


def test_image_error_suggestions_do_not_leak_internal_hosts():
    """建议文案是给坐席看的人话：不得夹内网 IP/端口（内部拓扑不外泄，
    技术细节属「技术详情」折叠区）。"""
    zh, en = _dicts()
    ip = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|:\d{4,5}\b")
    bad = [k for k, v in list(zh.items()) + list(en.items())
           if k.startswith("cp.image.err") and ip.search(v)]
    assert not bad, f"错误文案泄漏内网地址：{sorted(set(bad))}"


# 「显存」不是坐席的词汇（2026-08-28 老板点名：「这个信息不应该让用户看到」）。
# 旧 cp.image.vram_low_warn 把内部实现（显存/腾挪/算力）直接摊给坐席，且在白标
# 与演示场合等于自曝「这套系统跑在一台显存吃紧的共享卡上」。坐席只需要结果语言
# （要多等多久）；显存数字仍在 /api/image/config 里给运维/ops 卡用。
_INFRA_TERMS_ZH = ("显存", "视讯记忆体", "腾挪", "算力", "GPU", "VRAM")
_INFRA_TERMS_EN = ("vram", "gpu")

# 白名单：**只有**这两类允许出现内部实现词，且逐键登记（2026-08-28 全量盘点
# 971 键得出，当时命中 10 处、软化 2 处后剩 8 处）。
#   - 结构化失败卡（2026-08-22 错误面收口的产物）：出现时机是**真的失败了**，
#     坐席下一步就是把原因转给运维——那时点名「显存不足」是有用信息不是噪音。
#   - LAN GPU 主机掉线告警：受众本就是运维。
# 常态文案（预警条/提示/按钮/存货说明）没有豁免：它们在「一切正常」时也挂着，
# 坐席无从处置，只会变成看不懂的噪音 + 白标/演示露馅（老板 2026-08-28 点名）。
_JARGON_ALLOW = {
    "cp.image.errc_vram_insufficient",
    "cp.image.errs_vram_insufficient",
    "cp.image.errs_no_output",
    "cp.alert.lan_gpu",
}

# 「模型 / 冷启动 / 预热」这一类**运行机制**词（2026-08-28 老板第二轮点名：
# 「不要给用户说什么冷启动、模型，用户不了解也不关心」）。与上面的硬件词分开
# 管：硬件词（显存/GPU）在失败卡里还有转述给运维的价值，机制词连那点价值都
# 没有——坐席对「模型在不在显存里」既不理解也无从处置，只需要知道「要等多久」。
# 因此这一组的豁免面更窄：只放过 cp.alert.*（受众本就是运维）与「模型文件缺失」
# 这类**必须点名才能报修**的失败卡。
_MECHANISM_TERMS = ("冷启动", "冷啟動", "加载模型", "載入模型", "预热", "預熱", "模型")
_MECHANISM_ALLOW = {
    "cp.alert.image_models",          # ops 告警：出图模型失踪
    "cp.alert.colloquial_llm",        # ops 告警：口语化模型连续失败
    "cp.image.errc_model_missing",    # 失败卡：不点名「模型文件缺失」运维无从下手
    "cp.image.errs_model_missing",
}


def test_copilot_copy_has_no_mechanism_jargon():
    """常态文案不得出现「模型/冷启动/预热」这类运行机制词。"""
    zh, _en = _dicts()
    bad = []
    for k, v in zh.items():
        if not k.startswith("cp.") or k in _MECHANISM_ALLOW:
            continue
        hit = [t for t in _MECHANISM_TERMS if t in str(v)]
        if hit:
            bad.append(f"{k} → {hit}  «{str(v)[:52]}»")
    assert not bad, (
        "右栏文案出现运行机制词（坐席不理解也无从处置）——只说「要等多久」「暂时"
        "用不了」，机制细节留给日志与 ops：\n  " + "\n  ".join(sorted(bad)))


def test_mechanism_allowlist_not_stale():
    zh, _en = _dicts()
    stale = [f"{k}：已不含机制词，请从 _MECHANISM_ALLOW 除名"
             for k in sorted(_MECHANISM_ALLOW)
             if k in zh and not any(t in str(zh[k]) for t in _MECHANISM_TERMS)]
    assert not stale, "\n".join(stale)


def test_copilot_copy_has_no_infra_jargon():
    """扫**整个右栏词典**（不只出图面板）：新面板/新文案一律不许再漏内部词。"""
    zh, en = _dicts()
    bad = []
    for k, v in zh.items():
        if not k.startswith("cp.") or k in _JARGON_ALLOW:
            continue
        hit = [t for t in _INFRA_TERMS_ZH if t.lower() in str(v).lower()]
        if hit:
            bad.append(f"{k} → {hit}  «{str(v)[:48]}»")
    for k, v in en.items():
        if not k.startswith("cp.") or k in _JARGON_ALLOW:
            continue
        hit = [t for t in _INFRA_TERMS_EN if t in str(v).lower()]
        if hit:
            bad.append(f"{k} → {hit}  «{str(v)[:48]}»")
    assert not bad, (
        "右栏文案出现内部实现词（坐席无从处置 + 白标/演示露馅）——改成结果语言"
        "（「要多等多久」「暂时用不了」），显存/队列等诊断信息留给 ops；确属"
        "「真失败后转给运维」的诊断文案才登记进 _JARGON_ALLOW：\n  "
        + "\n  ".join(sorted(bad)))


def test_jargon_allowlist_not_stale():
    """白名单条目被清理后必须除名（防它变成永久豁免）。"""
    zh, en = _dicts()
    stale = []
    for k in sorted(_JARGON_ALLOW):
        vz, ve = str(zh.get(k, "")), str(en.get(k, "") or "")
        hit = ([t for t in _INFRA_TERMS_ZH if t.lower() in vz.lower()]
               + [t for t in _INFRA_TERMS_EN if t in ve.lower()])
        if k not in zh and k not in en:
            stale.append(f"{k}：词条已删除，请从 _JARGON_ALLOW 除名")
        elif not hit:
            stale.append(f"{k}：已不含内部词，请从 _JARGON_ALLOW 除名")
    assert not stale, "\n".join(stale)


def test_image_busy_copy_is_outcome_language():
    """忙闲提示必须存在且双语齐备（替代已下线的 vram_low_warn）。"""
    zh, en = _dicts()
    for k in ("cp.image.queue_wait", "cp.image.warmup_wait"):
        assert k in zh and k in en, f"缺忙闲提示词条 {k}"
    assert "cp.image.vram_low_warn" not in zh and "cp.image.vram_low_warn" not in en, (
        "vram_low_warn 已于 2026-08-28 下线（把「显存被常驻模型占满」说成「出图卡"
        "较忙」＝永久误报）——不要复活它")


def test_infra_jargon_ratchet_detects_violation():
    """探测器自证：把已下线的旧文案塞回常态键，门禁必须点名。"""
    zh, _ = _dicts()
    poisoned = dict(zh)
    poisoned["cp.image.hint"] = "出图卡当前较忙（空闲显存 0.1G），要先腾挪显存"
    bad = [k for k, v in poisoned.items()
           if k.startswith("cp.") and k not in _JARGON_ALLOW
           and any(t.lower() in str(v).lower() for t in _INFRA_TERMS_ZH)]
    assert bad == ["cp.image.hint"], f"旧文案未被检出，门禁失效：{bad}"
