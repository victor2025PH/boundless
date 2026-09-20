"""cp-i18n.js 全量 zh↔en 键齐平门禁（i18n P0 后续，2026-08-20）。

背景：副驾词典靠 ``reg({zh},{en})`` 分块注册，此前只有按功能点名的局部双语
门禁（test_goal_push_preset / test_copilot_panel_manifest 等），「en 段漏键 /
en 值残留中文」要靠人肉发现——2026-08-19 审计时 745/745 齐平纯属各线自觉。
本门禁把它变成契约：

① zh/en 键集全等（en 漏键＝英文界面该词条静默回落中文/键名）；
② en 值零 CJK（英文词典里出现汉字＝翻译时把中文抄进了 en 桶）；
③ 解析条目数下限 + 可疑行清单必须为空——格式漂移时解析器宁可红也不假绿。

解析器为行级状态机（文件为机器一致缩进的单行词条），不执行 JS。
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "shared" / "copilot" / "i18n" / "cp-i18n.js"

# 单行词条："key": "value",   （值内允许转义引号；行尾可挂 // 注释）
_ENTRY = re.compile(
    r'^\s*"((?:[^"\\]|\\.)+)"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,?\s*(?://.*)?$')
# 「看着像词条但没匹配上」的可疑行（多行拼接值/单引号等格式漂移探测器）
_SUSPECT = re.compile(r'^\s*"(?:[^"\\]|\\.)+"\s*:')


def _parse() -> tuple:
    zh: dict = {}
    en: dict = {}
    suspects: list = []
    state = "out"
    for lineno, raw in enumerate(
            _SRC.read_text(encoding="utf-8").splitlines(), start=1):
        s = raw.strip()
        if state == "out":
            if s == "reg(":
                state = "pre_zh"
            continue
        if state == "pre_zh":
            if s.startswith("{"):
                state = "zh"
            continue
        if state == "zh":
            m = _ENTRY.match(raw)
            if m:
                zh[m.group(1)] = m.group(2)
                continue
            if s.startswith("},"):
                state = "pre_en"
            elif _SUSPECT.match(raw):
                suspects.append((lineno, s[:80]))
            continue
        if state == "pre_en":
            if s.startswith("{"):
                state = "en"
            continue
        if state == "en":
            m = _ENTRY.match(raw)
            if m:
                en[m.group(1)] = m.group(2)
                continue
            if s.startswith("}"):
                state = "tail"
            elif _SUSPECT.match(raw):
                suspects.append((lineno, s[:80]))
            continue
        if state == "tail":
            if s.startswith(")"):
                state = "out"
            continue
    return zh, en, suspects


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def test_cp_i18n_zh_en_key_parity():
    zh, en, suspects = _parse()
    assert not suspects, (
        f"cp-i18n.js 出现解析器不认识的词条格式（多行拼接/单引号？），"
        f"先修格式或升级本解析器：{suspects[:5]}")
    # 解析地板：词典 2026-08 已 700+ 键，解析出来远低于此＝解析器失效（防假绿）
    assert len(zh) >= 700, f"zh 段仅解析出 {len(zh)} 键——解析器疑似失效"
    only_zh = sorted(set(zh) - set(en))
    only_en = sorted(set(en) - set(zh))
    assert not only_zh, f"en 段缺 {len(only_zh)} 键（英文界面将回落中文/键名）：{only_zh[:10]}"
    assert not only_en, f"zh 段缺 {len(only_en)} 键（中文界面将回落键名）：{only_en[:10]}"


def test_cp_i18n_en_values_zero_cjk():
    zh, en, _ = _parse()
    bad = {k: v for k, v in en.items() if _has_cjk(v)}
    assert not bad, (
        f"en 词典残留汉字 {len(bad)} 条（英文界面必现中文）："
        f"{dict(list(bad.items())[:5])}")
