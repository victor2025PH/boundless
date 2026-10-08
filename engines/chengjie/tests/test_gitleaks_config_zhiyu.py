"""仓库 gitleaks 配置的护栏（智语）：按内容豁免 localStorage 键名常量，且不放大到真密钥。"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CFG = ROOT / ".gitleaks.toml"
INBOX = ROOT / "engines" / "chengjie" / "src" / "web" / "templates" / "unified_inbox.html"


def _cfg() -> dict:
    return tomllib.loads(CFG.read_text(encoding="utf-8"))


def _patterns() -> list[re.Pattern]:
    return [re.compile(p) for p in _cfg()["allowlist"]["regexes"]]


def _allowed(s: str) -> bool:
    return any(p.search(s) for p in _patterns())


def test_default_rules_kept():
    cfg = _cfg()
    assert cfg["extend"]["useDefault"] is True
    al = cfg["allowlist"]
    # 只做 secret 值的整串匹配：不按路径/提交/行豁免，不用 stopwords
    assert al.get("regexTarget", "secret") == "secret"
    assert not al.get("paths") and not al.get("commits") and not al.get("stopwords")
    for p in al["regexes"]:
        assert p.startswith("^") and p.endswith("$"), p


def test_inbox_storage_key_constants_allowed():
    text = INBOX.read_text(encoding="utf-8")
    vals = re.findall(r"_KEY\s*=\s*'(ws_[A-Za-z0-9_]+_v\d+)'", text)  # 带版本号的键名（gitleaks 只会命中这类）
    for v in ("ws_kb_suggest_pref_v1", "ws_dpick_lat_v1", "ws_voice_persona_v1", "ws_xlate_prefs_v1"):
        assert v in vals, v
    assert all(_allowed(v) for v in vals), [v for v in vals if not _allowed(v)]


def test_real_looking_secrets_not_allowed():
    for s in (
        "ws_" + "Xk9fQ2LmZp7Rt4Vb8NcW_v1",    # 混大小写
        "ws_" + "a1b2c3d4e5f6_v1",              # 带数字
        "q8Zf3LmN0pR7sT2vW5xY9aB4cD6eG1hJ",
        "sk-" + "abcdefghijklmnopqrstuvwx",
        "ws_dpick_lat_v1 extra",                # 非整串
        "xws_dpick_lat_v1",
        "ws_dpick_lat_v123",
    ):
        assert not _allowed(s), s
