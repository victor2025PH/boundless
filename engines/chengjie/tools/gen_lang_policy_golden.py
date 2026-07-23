"""生成 lang_policy 行为金标准（golden matrix）——双栈一致性的单一事实源。

背景：会话语言策略存在两份拷贝（本仓 src/ai/lang_policy.py 为规范版；
boundless/tgkz2026/backend/lang_policy.py 为自包含移植版，内嵌了紧凑语种检测核心）。
两份代码演进时极易行为漂移——"chengjie 修了误判、tgkz 没同步"这类事故只能靠
**行为级**契约测试拦截（文件哈希对不上没有意义，两版实现本就不同）。

本脚本维护一份带**显式期望值**的用例矩阵：
  1. 生成时先用本仓规范实现校验矩阵（期望错了直接报错，金标准不可能悄悄编码回归）；
  2. 校验通过后写出 tests/lang_policy_golden.json 到两个仓库的 tests/ 目录；
  3. 两侧各有一个契约测试（chengjie: test_lang_policy_golden.py /
     tgkz: test_lang_policy_parity.py）加载同一份 JSON 断言行为。

用例选取原则：只收录两侧实现**必须一致**的行为——请求解析/中性词剥离是同源正则
（必须逐字一致）；证据分级与决策只选两侧检测核心共同覆盖的语种（CJK/假名/谚文/
西里尔/泰文/阿拉伯/整句英文），避开拉丁关键词表差异区（es/pt 长尾词）。

用法：python tools/gen_lang_policy_golden.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_CHENGJIE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_CHENGJIE_ROOT))

_TGKZ_BACKEND = _CHENGJIE_ROOT.parent.parent / "tgkz2026" / "backend"

GOLDEN_VERSION = 1

# ── 用例矩阵（期望值显式声明，生成时用规范实现校验） ───────────────────

PARSE_REQUEST_CASES = [
    # 中文书写
    {"text": "我们用日语聊吧", "expect": "ja"},
    {"text": "说日语", "expect": "ja"},
    {"text": "跟我说日语吧", "expect": "ja"},
    {"text": "请用日文回复", "expect": "ja"},
    {"text": "换成英文", "expect": "en"},
    {"text": "换回中文吧", "expect": "zh"},
    {"text": "改用韩语", "expect": "ko"},
    {"text": "切换到西班牙语", "expect": "es"},
    {"text": "日文回复我", "expect": "ja"},
    {"text": "用英语交流", "expect": "en"},
    {"text": "算了还是中文吧", "expect": "zh"},
    {"text": "能不能说日语", "expect": "ja"},
    # 英文书写
    {"text": "please speak japanese", "expect": "ja"},
    {"text": "can you speak Japanese?", "expect": "ja"},
    {"text": "speak in english please", "expect": "en"},
    {"text": "reply in chinese", "expect": "zh"},
    {"text": "talk to me in korean", "expect": "ko"},
    {"text": "switch to english", "expect": "en"},
    {"text": "in japanese please", "expect": "ja"},
    # 日文/韩文书写
    {"text": "日本語で話してください", "expect": "ja"},
    {"text": "日本語でお願いします", "expect": "ja"},
    {"text": "英語で話して", "expect": "en"},
    {"text": "中国語にして", "expect": "zh"},
    {"text": "한국어로 말해줘", "expect": "ko"},
    {"text": "영어로 대답해 주세요", "expect": "en"},
    # 负向请求（看不懂 X → 切到消息自身语言）
    {"text": "sorry I can't understand chinese", "expect": "en"},
    # 排除：否定 / 能力陈述疑问 / 转述 / 普通聊天 / 中性词
    {"text": "别说日语了", "expect": ""},
    {"text": "不要用英文", "expect": ""},
    {"text": "don't speak english to me", "expect": ""},
    {"text": "你会说日语吗？", "expect": ""},
    {"text": "我会说日语", "expect": ""},
    {"text": "日本語が難しいですね", "expect": ""},
    {"text": "今天天气不错", "expect": ""},
    {"text": "whatsapp", "expect": ""},
    {"text": "ok", "expect": ""},
    {"text": "昨天有个客户说要用日语聊，我没理他", "expect": ""},
]

# 剥离后应为空（不构成任何语言证据）
NEUTRAL_STRIP_EMPTY_CASES = [
    "whatsapp", "WhatsApp", "telegram", "ok", "OK!!", "usdt", "ok thx",
    "https://t.me/abc123", "@someone_123", "12345", "666", "👍👍", "ok 👍",
    "whatsapp telegram line", "yes", "thx", "gm",
]

# 证据分级（两侧检测核心共同覆盖区）
CLASSIFY_CASES = [
    {"text": "你在干嘛呢", "lang": "zh", "strength": "strong"},
    {"text": "日本語わかりますか", "lang": "ja", "strength": "strong"},
    {"text": "안녕하세요 반가워요", "lang": "ko", "strength": "strong"},
    {"text": "Привет как дела", "lang": "ru", "strength": "strong"},
    {"text": "What did you eat today my friend?", "lang": "en", "strength": "strong"},
    {"text": "whatsapp", "lang": "", "strength": "none"},
    {"text": "ok 👍", "lang": "", "strength": "none"},
    {"text": "nice", "lang": "en", "strength": "weak"},
]

_ZH_HISTORY = [
    {"role": "user", "content": "你在干嘛呢"},
    {"role": "assistant", "content": "刚下班～你呢"},
    {"role": "user", "content": "我也刚回家"},
]

# 会话决策场景（expect 只断言关心的字段）
RESOLVE_CASES = [
    {
        "name": "incident2_brand_word_sticky",
        "text": "whatsapp", "history": _ZH_HISTORY, "prev": "zh",
        "pref": "", "pref_input": "", "lock": "",
        "expect": {"lang": "zh", "stable": False},
    },
    {
        "name": "incident1_explicit_request",
        "text": "我们用日语聊吧", "history": _ZH_HISTORY, "prev": "zh",
        "pref": "", "pref_input": "", "lock": "",
        "expect": {"lang": "ja", "source": "explicit_request",
                   "request": "ja", "stable": True},
    },
    {
        "name": "incident1_pref_persists_over_zh",
        "text": "好的知道了", "history": _ZH_HISTORY, "prev": "ja",
        "pref": "ja", "pref_input": "zh", "lock": "",
        "expect": {"lang": "ja", "source": "user_pref"},
    },
    {
        "name": "genuine_switch_immediate",
        "text": "What did you eat today my friend?", "history": _ZH_HISTORY,
        "prev": "zh", "pref": "", "pref_input": "", "lock": "",
        "expect": {"lang": "en", "source": "detected", "stable": True},
    },
    {
        "name": "weak_evidence_sticky",
        "text": "nice", "history": _ZH_HISTORY, "prev": "zh",
        "pref": "", "pref_input": "", "lock": "",
        "expect": {"lang": "zh", "source": "sticky", "stable": False},
    },
    {
        "name": "operator_lock_absolute",
        "text": "我们用日语聊吧", "history": _ZH_HISTORY, "prev": "zh",
        "pref": "", "pref_input": "", "lock": "en",
        "expect": {"lang": "en", "source": "operator_lock", "request": "ja"},
    },
    {
        "name": "pref_released_by_third_lang_drift",
        "text": "刚才那句话我看不懂啊",
        "history": _ZH_HISTORY + [
            {"role": "user", "content": "其实我中文也可以的日语太难了"},
        ],
        "prev": "ja", "pref": "ja", "pref_input": "ja", "lock": "",
        "expect": {"lang": "zh", "source": "stable_switch", "stable": True},
    },
    {
        "name": "lock_normalizes_tts_code",
        "text": "hello there my friend how are you", "history": None,
        "prev": "zh-cn", "pref": "", "pref_input": "", "lock": "zh-cn",
        "expect": {"lang": "zh", "source": "operator_lock"},
    },
]


def _build_golden() -> dict:
    return {
        "version": GOLDEN_VERSION,
        "generated_by": "engines/chengjie/tools/gen_lang_policy_golden.py",
        "note": "行为金标准：chengjie 与 tgkz2026 两份 lang_policy 拷贝必须对本矩阵给出一致结果。"
                "修改任何一侧行为时，先改规范版并重新生成本文件，再同步移植版。",
        "parse_request": PARSE_REQUEST_CASES,
        "neutral_strip_empty": NEUTRAL_STRIP_EMPTY_CASES,
        "classify": CLASSIFY_CASES,
        "resolve": RESOLVE_CASES,
    }


def _validate_with_canonical(golden: dict) -> list:
    """用本仓规范实现校验矩阵期望值；返回不一致清单（空 = 全部通过）。"""
    from src.ai.lang_policy import (
        classify_evidence,
        parse_language_request,
        resolve_conversation_language,
        strip_neutral_tokens,
    )

    bad = []
    for c in golden["parse_request"]:
        got = parse_language_request(c["text"])
        if got != c["expect"]:
            bad.append(f"parse_request {c['text']!r}: expect={c['expect']!r} got={got!r}")
    for t in golden["neutral_strip_empty"]:
        got = strip_neutral_tokens(t)
        if got != "":
            bad.append(f"neutral_strip {t!r}: expect empty got={got!r}")
    for c in golden["classify"]:
        lang, strength = classify_evidence(c["text"])
        if lang != c["lang"] or strength != c["strength"]:
            bad.append(
                f"classify {c['text']!r}: expect=({c['lang']},{c['strength']}) got=({lang},{strength})"
            )
    for c in golden["resolve"]:
        d = resolve_conversation_language(
            c["text"], c["history"],
            prev_lang=c["prev"], lang_pref=c["pref"],
            lang_pref_input=c["pref_input"], operator_lock=c["lock"],
        )
        for k, v in c["expect"].items():
            if getattr(d, k) != v:
                bad.append(
                    f"resolve[{c['name']}].{k}: expect={v!r} got={getattr(d, k)!r}"
                )
    return bad


def main() -> int:
    golden = _build_golden()
    problems = _validate_with_canonical(golden)
    if problems:
        print("金标准与规范实现不一致（先修矩阵或修实现，二者必须显式对齐）：")
        for p in problems:
            print("  -", p)
        return 1

    targets = [_CHENGJIE_ROOT / "tests" / "lang_policy_golden.json"]
    if _TGKZ_BACKEND.exists():
        targets.append(_TGKZ_BACKEND / "tests" / "lang_policy_golden.json")
    payload = json.dumps(golden, ensure_ascii=False, indent=2)
    for t in targets:
        t.write_text(payload, encoding="utf-8")
        print(f"golden 已写出: {t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
