"""方言出货闸门：仅粤语可调用；未出货档不得改声学、不得进 UI。"""
from pathlib import Path

from src.ai.cosy_dialect import (
    COSY3_DIALECT_INSTRUCT,
    SHIPPED_DIALECT_FLAVORS,
    UNSHIPPED_COSY3_INSTRUCT,
    dialect_acoustic_override,
    is_shipped_dialect,
    merge_dialect_acoustic,
    normalize_dialect_flavor,
)
from src.utils.persona_manager import PersonaManager


def test_only_cantonese_is_shipped():
    assert SHIPPED_DIALECT_FLAVORS == frozenset({"cantonese"})
    assert is_shipped_dialect("cantonese")
    assert is_shipped_dialect("Cantonese")
    for f in ("minnan", "taiwan", "chuanyu", "dongbei", "hunan", "beijing", ""):
        assert not is_shipped_dialect(f)
        assert normalize_dialect_flavor(f) == ""
    assert normalize_dialect_flavor("cantonese") == "cantonese"


def test_unshipped_flavors_never_get_acoustic_override():
    """instruct2+普通话稿不是闽南语/四川话——出货闸门必须全挡。"""
    assert COSY3_DIALECT_INSTRUCT == {}
    for f in (*UNSHIPPED_COSY3_INSTRUCT, "cantonese", "beijing", "", "unknown"):
        assert dialect_acoustic_override(f) is None


def test_archived_instruct_strings_still_match_cosyvoice3():
    """未出货归档：官方 instruct 原文备查，改字模型就不认。"""
    assert UNSHIPPED_COSY3_INSTRUCT["dongbei"] == "请用东北话表达。"
    assert UNSHIPPED_COSY3_INSTRUCT["hunan"] == "请用湖南话表达。"
    assert UNSHIPPED_COSY3_INSTRUCT["minnan"] == "请用闽南话表达。"
    assert UNSHIPPED_COSY3_INSTRUCT["taiwan"] == UNSHIPPED_COSY3_INSTRUCT["minnan"]
    assert UNSHIPPED_COSY3_INSTRUCT["chuanyu"] == "请用四川话表达。"


def test_merge_strips_unshipped_flavor_keeps_clone_binding():
    vp = {
        "enabled": True,
        "owner_consent": True,
        "backend": "avatar_clone",
        "reference_audio_path": "/x/ref.wav",
        "dialect_flavor": "chuanyu",
    }
    m = merge_dialect_acoustic(vp)
    assert m["reference_audio_path"] == "/x/ref.wav"
    assert m["owner_consent"] is True
    assert m["backend"] == "avatar_clone"
    assert m.get("clone_instruct") in (None, "")
    assert m["dialect_flavor"] == ""
    assert vp["backend"] == "avatar_clone"
    assert vp["dialect_flavor"] == "chuanyu"  # 入参不被改


def test_merge_keeps_cantonese_flavor():
    vp = {"backend": "avatar_clone", "dialect_flavor": "cantonese"}
    m = merge_dialect_acoustic(vp)
    assert m["dialect_flavor"] == "cantonese"
    assert m["backend"] == "avatar_clone"


def test_merge_noop_without_flavor():
    vp = {"enabled": True, "backend": "avatar_clone"}
    assert merge_dialect_acoustic(vp)["backend"] == "avatar_clone"


def test_normalize_profile_strips_unshipped_dialect():
    out = PersonaManager.normalize_profile_shape({
        "name": "苏婉",
        "voice_profile": {"backend": "avatar_clone", "dialect_flavor": "minnan"},
    })
    assert out["voice_profile"]["dialect_flavor"] == ""
    keep = PersonaManager.normalize_profile_shape({
        "voice_profile": {"dialect_flavor": "cantonese"},
    })
    assert keep["voice_profile"]["dialect_flavor"] == "cantonese"


def test_personas_html_dialect_select_only_cantonese():
    html = (Path(__file__).resolve().parents[1]
            / "src" / "web" / "templates" / "personas.html").read_text(
                encoding="utf-8")
    assert 'option value="cantonese"' in html
    for banned in ("taiwan", "chuanyu", "dongbei", "minnan", "hunan", "beijing"):
        assert f'option value="{banned}"' not in html
