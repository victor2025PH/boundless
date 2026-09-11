# -*- coding: utf-8 -*-
"""副驾 overlay 覆盖门禁（2026-08-28，「改了词典忘重跑生成器」这条债的机制化）。

背景
----
``shared/copilot/i18n/cp-i18n.js`` 是 zh/en 单一事实源；其他语种靠
``cp-i18n-ext.<lang>.js`` overlay 覆盖（``scripts/i18n_desktop_ext.py`` 生成，
装载协议＝紧跟 cp-i18n.js 之后经 ``CopilotShared.regExt`` 后装）。

已有门禁 ``test_cp_i18n_parity`` 只查 cp-i18n.js **内部** zh↔en 齐平，
**完全不看 overlay**。于是有一条长期静默的债：往 cp-i18n.js 加了键、没重跑
生成器 → 繁中坐席那几个词条静默回落简中，没有任何红灯。2026-08-28 实锤：
并行线给 cp-image 加了键未重跑，本线加折叠/截断词条时才顺手清掉。

本门禁守两条硬不变量 + 一张显式债表：

① **zh_hant 必须 100% 覆盖**（``test_zh_hant_overlay_covers_every_key``）。
   它走 OpenCC 离线转换（``scripts/i18n_hant.py``，s2twp + 术语钉），任何时候
   都能重跑、不依赖 GPU/网络 → 「100% 覆盖」是可执行的契约而非愿望。
   修法就一条命令：``python -m scripts.i18n_desktop_ext generate --langs zh_hant``

② **任何 overlay 都不许有 cp-i18n.js 已经没有的键**（``test_no_stale_keys``）。
   键改名/删除后 overlay 里的化石会把**旧文案**继续喂给该语种坐席——比缺键更
   坏（缺键回落还是对的内容，化石是错的内容）。当前四个语种均为 0。

③ **机翻语种（vi/th/id）的覆盖缺口必须显式登记**（``_MT_OVERLAY_DEBT``）。
   它们走 ollama_mt（``scripts/i18n_mt.py``，需要局域网 GPU 端点），CI/日常
   施工不该被一个可能不在线的依赖卡住，所以**不做覆盖率硬门禁**；但也不能像
   此前那样彻底不可见——2026-08-28 实测 th/id overlay 是 **0 键空壳**、vi 只有
   62/974，而生成器 08-27 04:16 跑出 0 键**还报了成功**（静默降级）。三个语种
   的坐席一直在吃 en/zh 回落，代码里零处提及。债表把它摆到明面，并由
   ``test_mt_debt_registry_not_stale`` 防其过期（谁把某语种真做起来了，就得来
   删掉对应条目——否则债表本身变成谎言）。
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.test_cp_i18n_parity import _parse

_REPO = Path(__file__).resolve().parents[1]
_I18N_DIR = _REPO / "shared" / "copilot" / "i18n"

# overlay 单行词条（生成器输出恒为机器一致缩进的单行）
_EXT_ENTRY = re.compile(r'^\s*"((?:[^"\\]|\\.)+)"\s*:')

# 机翻语种的已知覆盖债（2026-08-28 实测）：lang → 该语种缺多少键。
# 语义＝「这个语种的 overlay 没做起来/只做了一部分，坐席在吃 en/zh 回落」。
# 谁把它做起来（覆盖率 ≥ _MT_DONE_RATIO）就来删掉对应条目。
# 2026-09-12（1.0.83 语言切换批）：HY-MT 重跑 vi/th/id 三语 overlay 到 94–98%（cp/shell/SHELL_STR
# 三套齐），债表清空。再有语种掉回 <90% 会由 test_mt_debt_registry_not_stale 的 unlisted_gaps 抓回。
_MT_OVERLAY_DEBT: dict = {}
# 覆盖率达到这个比例即视为「做起来了」→ 债表条目必须删除
_MT_DONE_RATIO = 0.9


def _overlay_keys(lang: str) -> set:
    p = _I18N_DIR / f"cp-i18n-ext.{lang}.js"
    if not p.is_file():
        return set()
    out = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        m = _EXT_ENTRY.match(line)
        if m and m.group(1).startswith("cp."):
            out.add(m.group(1))
    return out


def _overlay_langs() -> list:
    return sorted(p.name.split(".")[1] for p in _I18N_DIR.glob("cp-i18n-ext.*.js"))


def test_zh_hant_overlay_covers_every_key():
    """繁中 overlay 必须覆盖 cp-i18n.js 全部键（离线可重跑，故为硬契约）。"""
    zh, _en, _susp = _parse()
    assert len(zh) >= 700, f"zh 段仅解析出 {len(zh)} 键——解析器疑似失效（防假绿）"
    keys = _overlay_keys("zh_hant")
    missing = sorted(set(zh) - keys)
    assert not missing, (
        f"繁中 overlay 缺 {len(missing)} 键（这些词条对繁中坐席静默回落简中）："
        f"{missing[:8]}\n"
        f"修法：python -m scripts.i18n_desktop_ext generate --langs zh_hant"
        f"（改完记得 bump 两个宿主的 cp-i18n-ext ?v= + scripts/bump_ui_build.py）"
    )


def test_no_stale_keys_in_any_overlay():
    """任何 overlay 都不许留 cp-i18n.js 已删/已改名的键（化石＝喂错文案）。"""
    zh, _en, _susp = _parse()
    # 地板：overlay 解析器若因格式漂移失效 → 键集为空 → stale 恒为空＝假绿。
    # 拿满覆盖的 zh_hant 当探针，坏了先红（覆盖门禁那边同时也会红）。
    assert len(_overlay_keys("zh_hant")) >= 700, (
        "zh_hant overlay 解析出的键过少——overlay 解析器疑似失效（防假绿）")
    bad = {}
    for lang in _overlay_langs():
        stale = sorted(_overlay_keys(lang) - set(zh))
        if stale:
            bad[lang] = stale
    assert not bad, (
        f"overlay 残留 cp-i18n.js 里已不存在的键（该语种坐席会看到旧文案，"
        f"比缺键更坏）：{ {k: v[:5] for k, v in bad.items()} }\n"
        f"修法：重跑对应语种生成器（生成器按源键集重建，会自动清掉化石）"
    )


def test_mt_debt_registry_not_stale():
    """债表必须与实况一致：登记的仍未做起来，未登记的必须已做起来。"""
    zh, _en, _susp = _parse()
    total = len(zh)
    wrongly_listed = []
    unlisted_gaps = []
    for lang in _overlay_langs():
        if lang == "zh_hant":
            continue                      # 由上面的硬门禁单独守
        ratio = len(_overlay_keys(lang)) / max(1, total)
        if lang in _MT_OVERLAY_DEBT and ratio >= _MT_DONE_RATIO:
            wrongly_listed.append(f"{lang}（已达 {ratio:.0%}）")
        if lang not in _MT_OVERLAY_DEBT and ratio < _MT_DONE_RATIO:
            unlisted_gaps.append(f"{lang}（仅 {ratio:.0%}）")
    assert not wrongly_listed, (
        f"这些语种已做起来，请从 _MT_OVERLAY_DEBT 删掉对应条目："
        f"{wrongly_listed}（债表过期＝它本身变成谎言）")
    assert not unlisted_gaps, (
        f"这些语种 overlay 覆盖不足却没登记进 _MT_OVERLAY_DEBT："
        f"{unlisted_gaps}——要么重跑生成器补齐，要么显式登记为已知债"
        f"（该语种坐席在吃 en/zh 回落，不该零处提及）")
