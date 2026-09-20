"""小智回复语言解析：跟随智聊 UI 语言（ui_lang），默认中文、可切换、全语言。"""
from src.web.routes.assistant_routes import _reply_lang_and_name


class _State:
    def __init__(self, ui):
        self.ui_lang = ui


class _Req:
    def __init__(self, ui):
        self.state = _State(ui)


def test_default_zh_when_unset():
    assert _reply_lang_and_name(_Req(""), {})[0] == "zh"
    assert _reply_lang_and_name(_Req(None), {})[0] == "zh"
    assert _reply_lang_and_name(_Req("zh"), {})[0] == "zh"


def test_follows_ui_lang_all_languages():
    for code in ("en", "ja", "ko", "th", "vi", "id", "es", "de", "fr", "ru", "ar"):
        c, n = _reply_lang_and_name(_Req(code), {})
        assert c == code, code
        assert n, f"{code} 应有语言名（否则不追加硬指令）"


def test_region_suffix_stripped():
    assert _reply_lang_and_name(_Req("en-US"), {})[0] == "en"
    assert _reply_lang_and_name(_Req("pt_BR"), {})[0] == "pt"
    # Q-21：zh-tw / yue 是 _LANG_NAMES 一等码（LANGUAGE RULE 否则短路）；
    # 未入表的中文变体（zh-hk）仍归简体 zh。
    assert _reply_lang_and_name(_Req("zh-TW"), {})[0] == "zh-tw"
    assert _reply_lang_and_name(_Req("zh-hk"), {})[0] == "zh"


def test_body_lang_fallback_when_no_ui_lang():
    assert _reply_lang_and_name(_Req(""), {"lang": "ja"})[0] == "ja"


def test_ui_lang_wins_over_body_lang():
    assert _reply_lang_and_name(_Req("ko"), {"lang": "en"})[0] == "ko"


def test_unknown_falls_back_zh():
    assert _reply_lang_and_name(_Req("xx"), {})[0] == "zh"
    assert _reply_lang_and_name(_Req("klingon"), {})[0] == "zh"
