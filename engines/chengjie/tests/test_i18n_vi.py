"""越南语 (vi) i18n 基础设施与英文回落门禁。"""


def test_get_translations_vi_override_and_en_fallback():
    from src.web.web_i18n import get_translations, t

    vi = get_translations("vi")
    en = get_translations("en")

    # VI pack 已翻译的高频键
    assert vi.get("inbox.filter.all") == "Tất cả"
    assert vi.get("ws.cmdk.placeholder") == "Nhập lệnh, trang hoặc hội thoại…"
    assert vi.get("lang_toggle") == "中文"

    # 未进 VI pack 的键回落英文（非中文、非键名）
    missing_key = "__vi_fallback_probe_key_not_in_any_pack__"
    assert missing_key not in vi or vi.get(missing_key) == en.get(missing_key)
    # 选一个真实存在但 VI pack 未覆盖的键
    probe = "inbox.voice.audition_sample_text"
    assert probe in en
    assert probe not in vi or vi[probe] == en[probe]
    assert vi[probe] == en[probe]
    assert t(probe, "vi") == en[probe]


def test_collect_packs_returns_vi_tuple():
    from src.web.i18n_packs import collect_packs

    pzh, pen, pvi = collect_packs()
    assert isinstance(pvi, dict)
    assert pvi.get("inbox.filter.all") == "Tất cả"


def test_set_lang_accepts_vi(tmp_path):
    from src.utils.web_user_store import WebUserStore, ROLE_AGENT

    store = WebUserStore(tmp_path / "u.db")
    store.create_user("amy", "secret123", ROLE_AGENT)
    assert store.set_lang("amy", "vi") is True
    assert store.get_user("amy")["lang"] == "vi"
    assert store.set_lang("amy", "fr") is False
    assert store.get_user("amy")["lang"] == "vi"
