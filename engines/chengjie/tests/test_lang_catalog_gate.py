"""Q-21 A（#290，2026-09-12）：语言目录单点门禁——五处消费方 ⊆ ``src/i18n/lang_catalog``，且都含粤语。

ZDPNMT / R33MZW / N8G3JZ 三单 1.0.82 复验仍报「下拉没有粤语」：回复工坊 10 语、cp-xlate-tools 15 语、
顶栏 34 语、人设语言 4 语各写一份短表。本门禁钉：

1. 目录本体：恰 34 码、含 ``yue`` / ``zh-tw``、中 / 英 / 繁显示名齐、能力位齐、
   ``translation_engines.HYMT_TARGET_LANGS`` 与目录同一集合（后端白名单不再手写）；
2. ``unified_inbox.html`` ``_XL_CATALOG``（顶栏译文 + 对话翻译 收→/发→/我的语言 四个 select 的唯一灌入源）
   码序与目录逐项一致；
3. ``cp-draft.js``（回复工坊「跟随人设/账户」下拉）与 ``cp-xlate-tools.js``（翻译工具目标语）：运行时只经
   ``/api/lang-catalog`` 取表（静态钉调用点），内置短表只准作兜底且 ⊆ 目录；双树（shared / desktop）逐字节一致；
4. ``personas.html`` ``#p-language``：静态兜底 option ⊆ 目录 + 挂了 ``/api/lang-catalog`` 加载器；
5. ``/api/lang-catalog`` 响应形状 + ``cap`` 过滤 + ``ui_lang`` 显示名。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.i18n import lang_catalog as lc

_ROOT = Path(__file__).resolve().parents[1]
_HTML = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_PERSONAS = _ROOT / "src" / "web" / "templates" / "personas.html"
_CP_DRAFT = _ROOT / "shared" / "copilot" / "components" / "cp-draft.js"
_CP_XLATE = _ROOT / "shared" / "copilot" / "components" / "cp-xlate-tools.js"
_CP_CLIENT = _ROOT / "shared" / "copilot" / "client" / "copilot-client.js"
_DESKTOP = _ROOT / "desktop" / "renderer" / "shared" / "copilot"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── 1. 目录本体 ─────────────────────────────────────────────────────────────
def test_catalog_is_34_codes_with_yue_and_zh_tw():
    assert len(lc.LANG_CATALOG) == 34 and len(lc.CODES) == 34
    assert {"yue", "zh-tw", "zh", "en"} <= lc.CODES
    for e in lc.LANG_CATALOG:
        assert e.code == e.code.strip().lower()
        assert e.zh and e.en and e.hant and e.endonym, e.code
        assert isinstance(e.draft, bool) and isinstance(e.translate, bool) and isinstance(e.tts_clone, bool)
    assert lc.ordered_codes()[:3] == ["zh", "zh-tw", "yue"]


def test_catalog_names_and_normalize():
    assert lc.display_name("yue", "zh") == "粤语"
    assert lc.display_name("yue", "en") == "Cantonese"
    assert lc.display_name("yue", "zh-hant") == "粵語"
    assert lc.display_name("xx", "zh") == "xx"          # 目录外回落原码
    assert lc.normalize("zh-HK") == "zh-tw" and lc.normalize("zh_Hant") == "zh-tw"
    assert lc.normalize("zh-yue") == "yue" and lc.normalize("Cantonese") == "yue"
    assert lc.normalize("en-US") == "en" and lc.normalize("fil") == "tl"
    assert lc.has("yue") and not lc.has("klingon")


def test_capability_bits_and_filters():
    assert set(lc.codes_with("translate")) == lc.CODES
    assert set(lc.codes_with("draft")) == lc.CODES
    tts = set(lc.codes_with("tts_clone"))
    assert {"zh", "en"} <= tts and tts < lc.CODES
    assert "yue" not in tts     # 粤语 TTS 归 Q-22，本批只是展示位


def test_backend_whitelist_reads_catalog():
    from src.ai.translation_engines import HYMT_TARGET_LANGS
    from src.web.routes import unified_inbox_translate_routes as r
    assert set(HYMT_TARGET_LANGS) == lc.CODES
    assert r._AGENT_LANG_ALLOWED == set(lc.CODES)
    src = _read(_ROOT / "src" / "ai" / "translation_engines.py")
    assert "from src.i18n.lang_catalog import CODES" in src
    # 旧手写 34 码字面量必须已删（否则又是第二份表）
    assert '"zh", "zh-tw", "yue", "en", "ja", "ko", "fr", "es", "it"' not in src


# ── 2. unified_inbox.html：_XL_CATALOG 与目录逐项同序 ─────────────────────────
def _xl_catalog_codes() -> list:
    html = _read(_HTML)
    m = re.search(r"const _XL_CATALOG=\[(.*?)\];", html, re.S)
    assert m, "unified_inbox.html 缺 _XL_CATALOG 字面量"
    return re.findall(r"\{code:'([a-z-]+)'", m.group(1))


def test_unified_inbox_xl_catalog_matches_catalog_order():
    codes = _xl_catalog_codes()
    assert codes == lc.ordered_codes(), "顶栏译文 / 对话翻译四个 select 的目录与 lang_catalog 不同序/不同集"
    assert "yue" in codes and "zh-tw" in codes
    html = _read(_HTML)
    # 四个 select 只由目录灌入
    assert "for(const id of ['xl-top-lang','xlate-in','xlate-out','xl-agent-lang'])" in html
    assert "for(const it of _XL_CATALOG)" in html


# ── 3. copilot 双树组件：运行时只读 /api/lang-catalog，兜底表 ⊆ 目录 ───────────
def _js_fallback_codes(src: str, var: str) -> list:
    m = re.search(var + r"\s*=\s*\[(.*?)\];", src, re.S)
    assert m, f"缺 {var}"
    return re.findall(r'"([a-z-]*)"', m.group(1))


def test_cp_draft_reads_catalog_endpoint_and_fallback_subset():
    src = _read(_CP_DRAFT)
    assert "langCatalog" in src and "/api/lang-catalog" in src
    assert "_loadLangCatalog(" in src and "caps.draft === false" in src
    codes = [c for c in _js_fallback_codes(src, "const LANGS_FALLBACK") if c]
    # 兜底表形状 [code, key]，取偶数位的 code
    fb = re.findall(r'\["([a-z-]*)",\s*"cp\.lang\.[a-z_]+"\]', src)
    fb_codes = {c for c in fb if c}
    assert fb_codes and fb_codes <= lc.CODES, fb_codes - lc.CODES
    assert not codes or set(c for c in codes if c and not c.startswith("cp.")) <= lc.CODES


def test_cp_xlate_tools_reads_catalog_endpoint_and_fallback_subset():
    src = _read(_CP_XLATE)
    assert "langCatalog" in src and "/api/lang-catalog" in src
    assert "_ensureCatalog()" in src and "caps.translate === false" in src
    fb = set(_js_fallback_codes(src, "const LANGS_FALLBACK"))
    assert fb and fb <= lc.CODES, fb - lc.CODES


def test_copilot_client_exposes_lang_catalog_on_both_adapters():
    src = _read(_CP_CLIENT)
    assert src.count("async langCatalog(") == 2, "Web / Desktop 两个适配器都要有 langCatalog()"
    assert "/api/lang-catalog" in src


@pytest.mark.parametrize("rel", [
    "components/cp-draft.js", "components/cp-xlate-tools.js", "client/copilot-client.js", "app.html",
])
def test_copilot_dual_tree_in_sync(rel):
    a = (_ROOT / "shared" / "copilot" / rel).read_bytes()
    b = (_DESKTOP / rel).read_bytes()
    assert a == b, f"双树不同步：shared/copilot/{rel} vs desktop/renderer/shared/copilot/{rel}"


def test_copilot_app_html_bumped_for_q21():
    html = _read(_ROOT / "shared" / "copilot" / "app.html")
    for f in ("copilot-client.js", "cp-draft.js", "cp-xlate-tools.js"):
        m = re.search(re.escape(f) + r"\?v=([0-9a-z]+)", html)
        assert m and m.group(1) >= "20260912", f"{f} 的 ?v= 未随 Q-21 递增"


# ── 4. personas.html：#p-language 兜底 option ⊆ 目录 + 挂了目录加载器 ───────────
def test_personas_language_select_reads_catalog():
    html = _read(_PERSONAS)
    m = re.search(r'<select id="p-language"[^>]*>(.*?)</select>', html, re.S)
    assert m
    opts = re.findall(r'<option value="([^"]*)"', m.group(1))
    assert opts[0] == ""                                  # 跟随对方语言恒为首项
    assert {o for o in opts if o} <= lc.CODES
    assert "_dpLoadLangCatalog" in html and "/api/lang-catalog" in html


# ── 5. 端点 ────────────────────────────────────────────────────────────────
def test_catalog_payload_shape_and_filters():
    p = lc.catalog_payload("en")
    assert p["ok"] and p["version"] == lc.CATALOG_VERSION and p["ui_lang"] == "en"
    codes = [l["code"] for l in p["langs"]]
    assert codes == lc.ordered_codes()
    yue = next(l for l in p["langs"] if l["code"] == "yue")
    assert yue["name"] == "Cantonese" and yue["zh"] == "粤语" and yue["hant"] == "粵語"
    assert yue["caps"] == {"draft": True, "translate": True, "tts_clone": False}
    p2 = lc.catalog_payload("zh", only=lc.codes_with("tts_clone"))
    assert {l["code"] for l in p2["langs"]} == set(lc.codes_with("tts_clone"))
    json.dumps(p)   # 可序列化


def test_api_lang_catalog_route(auth_client):
    r = auth_client.get("/api/lang-catalog?ui_lang=zh-hant")
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert d["ok"] and len(d["langs"]) == 34
    assert next(l for l in d["langs"] if l["code"] == "yue")["name"] == "粵語"
    r2 = auth_client.get("/api/lang-catalog?cap=tts_clone")
    assert r2.status_code == 200 and "yue" not in {l["code"] for l in r2.json()["langs"]}
    r3 = auth_client.get("/api/unified-inbox/lang-plan")
    assert r3.status_code == 400          # 缺 conversation_id
    r4 = auth_client.get("/api/unified-inbox/lang-plan?conversation_id=wa:acc:nobody")
    assert r4.status_code == 200 and r4.json()["plan"] is None
