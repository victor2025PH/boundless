"""lang_policy 行为金标准契约测试（chengjie 规范版侧）。

金标准 tests/lang_policy_golden.json 由 tools/gen_lang_policy_golden.py 生成
（生成时已用本仓实现校验），同一份 JSON 也部署在 tgkz2026/backend/tests/ 由其
test_lang_policy_parity.py 校验——两侧任何一边行为漂移，对应 CI 立即红灯。

改行为的正确姿势：改 src/ai/lang_policy.py → 更新生成脚本里的期望矩阵 →
重新运行 tools/gen_lang_policy_golden.py → 同步 tgkz 移植版直到其 parity 测试转绿。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ai.lang_policy import (
    classify_evidence,
    parse_language_request,
    resolve_conversation_language,
    strip_neutral_tokens,
)

_GOLDEN_PATH = Path(__file__).parent / "lang_policy_golden.json"


def _load_golden() -> dict:
    assert _GOLDEN_PATH.exists(), (
        "缺少 tests/lang_policy_golden.json —— 运行 "
        "python tools/gen_lang_policy_golden.py 生成"
    )
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


_G = _load_golden()


@pytest.mark.parametrize(
    "case", _G["parse_request"], ids=lambda c: f"parse:{c['text'][:24]}"
)
def test_golden_parse_request(case):
    assert parse_language_request(case["text"]) == case["expect"]


@pytest.mark.parametrize(
    "text", _G["neutral_strip_empty"], ids=lambda t: f"neutral:{t[:20]}"
)
def test_golden_neutral_strip_empty(text):
    assert strip_neutral_tokens(text) == ""


@pytest.mark.parametrize(
    "case", _G["classify"], ids=lambda c: f"classify:{c['text'][:20]}"
)
def test_golden_classify(case):
    lang, strength = classify_evidence(case["text"])
    assert (lang, strength) == (case["lang"], case["strength"])


@pytest.mark.parametrize("case", _G["resolve"], ids=lambda c: c["name"])
def test_golden_resolve(case):
    d = resolve_conversation_language(
        case["text"],
        case["history"],
        prev_lang=case["prev"],
        lang_pref=case["pref"],
        lang_pref_input=case["pref_input"],
        operator_lock=case["lock"],
    )
    for k, v in case["expect"].items():
        assert getattr(d, k) == v, f"{case['name']}.{k}: expect={v!r} got={getattr(d, k)!r}"
