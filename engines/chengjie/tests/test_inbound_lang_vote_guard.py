"""入站语言「证据不足不投票」护栏（2026-08-20「OK 翻语言」实锤）门禁。

事故链：中文群用户回「OK」→ detect=en → conversations upsert 非 unknown 即覆写
→ 会话语言翻 en → 坐席中文被 409 拦 + 出站自动翻译开始反向译英。
护栏＝极短纯 ASCII / 常见客套词返回 unknown（upsert 对 unknown 不覆写）。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.inbox.normalizer import detect_inbound_language  # noqa: E402


def test_trivial_latin_returns_unknown():
    for t in ("OK", "ok", "Okay!", "kk", "yes", "No", "thx", "Hi~", "haha",
              "ty", "good", "done."):
        assert detect_inbound_language(t) == "unknown", t


def test_short_letters_below_threshold_unknown():
    assert detect_inbound_language("go") == "unknown"
    assert detect_inbound_language("a b") == "unknown"


def test_real_sentences_still_vote():
    # 真英文句子照常投票（依赖真 detect_language；只断言不是 unknown）
    assert detect_inbound_language("could you send me the photo please") != "unknown"
    # 中文照常
    assert detect_inbound_language("你好，请问怎么下载安装") != "unknown"


def test_empty_and_numbers():
    assert detect_inbound_language("") in ("unknown", "")
    assert detect_inbound_language("12345") == "unknown"


def test_normalizer_uses_guard():
    src = (_ROOT / "src" / "inbox" / "normalizer.py").read_text(encoding="utf-8")
    assert "lang = detect_inbound_language(raw)" in src, (
        "归一化器退回了裸 detect_language——「OK」又会翻会话语言")
