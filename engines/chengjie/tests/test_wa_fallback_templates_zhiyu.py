# -*- coding: utf-8 -*-
"""WhatsApp 24h 窗口回退模板：按会话语言选择、旧单模板兼容、STOP 闸、公开包不含博彩（智语）。"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

ENGINE = Path(__file__).resolve().parents[1]
BUILD = ENGINE / "desktop" / "build"
if str(BUILD) not in sys.path:
    sys.path.insert(0, str(BUILD))

import edition_gate  # noqa: E402

from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore
from src.integrations import protocol_bridge
from src.integrations import whatsapp_cloud as wac
from src.integrations.shared import official_stop_gate
from src.integrations.wa_fallback_templates import (
    INTERNAL_ONLY_NAME_PREFIX,
    catalog_files,
    configured_languages,
    fallback_configured,
    load_catalog,
    normalize_fallback_lang,
    prepare_text_param,
    problems_for,
    select_window_fallback,
)

PNID = "1098765432"
TOKEN = "unit-wa-fallback-token"
USER = "8613800138000"
DOC = ENGINE / "docs" / "wa_cloud_window_fallback_templates.md"
GAMBLE_NAMES = (
    "wa_fb_gamble_member_zh",
    "wa_fb_gamble_member_en",
    "wa_fb_gamble_member_fil",
    "wa_fb_gamble_pause_zh",
    "wa_fb_gamble_pause_en",
    "wa_fb_gamble_pause_fil",
)


def _spec(name: str, language: str, *, max_chars: int = 80) -> dict:
    return {"name": name, "language": language, "text_param": True, "param_max_chars": max_chars}


def _neutral_block(**extra) -> dict:
    block = {
        "name": "",
        "language": "zh_CN",
        "text_param": True,
        "default_language": "zh",
        "by_language": {
            "zh": _spec("cbp_svc_followup_zh", "zh_CN"),
            "en": _spec("cbp_svc_followup_en", "en"),
            "tl": _spec("cbp_svc_followup_fil", "fil"),
        },
    }
    block.update(extra)
    return block


# ── 语言归一 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    ("zh", "zh"),
    ("zh-CN", "zh"),
    ("zh_TW", "zh"),
    ("zh-HK", "zh"),
    ("zh-Hant", "zh"),
    ("en", "en"),
    ("en-US", "en"),
    ("en-PH", "en"),
    ("tl", "tl"),
    ("fil", "tl"),
    ("fil-PH", "tl"),
    ("Tagalog", "tl"),
    ("Taglish", "tl"),
    ("ceb", "tl"),
    ("Bisaya", "tl"),
    ("", ""),
    ("auto", ""),
    ("unknown", ""),
    ("ja", ""),
])
def test_normalize_fallback_lang(raw, expect):
    assert normalize_fallback_lang(raw) == expect


def test_select_matches_conversation_language():
    block = _neutral_block()
    zh = select_window_fallback(block, "zh-CN")
    en = select_window_fallback(block, "en-PH")
    tl = select_window_fallback(block, "taglish")
    assert (zh["name"], zh["language"], zh["source"]) == ("cbp_svc_followup_zh", "zh_CN", "by_language")
    assert (en["name"], en["language"], en["resolved_lang"]) == ("cbp_svc_followup_en", "en", "en")
    assert (tl["name"], tl["language"], tl["resolved_lang"]) == ("cbp_svc_followup_fil", "fil", "tl")
    assert select_window_fallback(block, "fil")["name"] == "cbp_svc_followup_fil"
    assert select_window_fallback(block, "ceb")["name"] == "cbp_svc_followup_fil"


def test_unknown_language_uses_configurable_default_then_legacy():
    block = _neutral_block()
    picked = select_window_fallback(block, "ja")
    assert picked["name"] == "cbp_svc_followup_zh" and picked["source"] == "default_language"
    block["default_language"] = "en"
    assert select_window_fallback(block, "")["name"] == "cbp_svc_followup_en"
    # 缺默认档时才用顶层旧模板，不拿另一种语言冒充
    thin = {
        "name": "reengage_v1",
        "language": "en_US",
        "text_param": True,
        "default_language": "zh",
        "by_language": {"en": _spec("cbp_svc_followup_en", "en")},
    }
    legacy = select_window_fallback(thin, "ja")
    assert legacy["name"] == "reengage_v1" and legacy["source"] == "legacy"
    assert legacy["language"] == "en_US"


def test_legacy_single_template_ignores_conversation_language():
    block = {"name": "reengage_v1", "language": "en_US", "text_param": True}
    picked = select_window_fallback(block, "tl")
    assert picked["name"] == "reengage_v1"
    assert picked["language"] == "en_US"
    assert picked["source"] == "legacy"
    assert picked["param_max_chars"] is None
    assert prepare_text_param("your order shipped\nline 2", max_chars=picked["param_max_chars"]) == (
        "your order shipped\nline 2"
    )


def test_param_max_chars_collapses_whitespace_and_truncates():
    text = "订单\n物流   " + ("甲" * 90)
    out = prepare_text_param(text, max_chars=80)
    assert "\n" not in out and "  " not in out
    assert len(out) <= 80 and out.startswith("订单 物流")


def test_public_edition_drops_gamble_bucket_without_substituting_another_language():
    block = _neutral_block()
    block["by_language"]["zh"] = _spec("wa_fb_gamble_member_zh", "zh_CN")
    dropped = select_window_fallback(block, "zh", edition="public")
    assert dropped["name"] == "" and dropped["source"] == "none"
    kept = select_window_fallback(block, "en", edition="public")
    assert kept["name"] == "cbp_svc_followup_en"
    internal = select_window_fallback(block, "zh", edition="internal")
    assert internal["name"] == "wa_fb_gamble_member_zh"
    assert fallback_configured(block, edition="public") is True
    only = {"by_language": {"zh": _spec("wa_fb_gamble_member_zh", "zh_CN")}}
    assert fallback_configured(only, edition="public") is False
    assert configured_languages(only, edition="public") == []
    assert fallback_configured(only, edition="internal") is True
    assert configured_languages(block, edition="") == ["zh", "en", "tl"]


# ── 目录：Meta 约束 + 公开包看不到内部模板 ──────────────────────────────────

def test_catalog_templates_pass_utility_rules_and_doc_lists_them():
    items = load_catalog(ENGINE)
    assert items, "模板目录为空"
    assert problems_for(items) == []
    names = [it["name"] for it in items]
    assert len(names) == len(set(names))
    public = load_catalog(ENGINE, edition="public")
    public_names = {it["name"] for it in public}
    assert public_names
    assert not any(n.startswith(INTERNAL_ONLY_NAME_PREFIX) for n in public_names)
    assert set(GAMBLE_NAMES).isdisjoint(public_names)
    assert set(GAMBLE_NAMES) <= set(names)
    public_files = [p.as_posix() for p in catalog_files(ENGINE, edition="public")]
    assert public_files and all("/presets/internal/" not in p for p in public_files)
    all_files = catalog_files(ENGINE)
    assert any("gambling_operator" in p.as_posix() for p in all_files)
    doc = DOC.read_text(encoding="utf-8")
    for item in items:
        assert item["name"] in doc
        assert item["body"] in doc
        assert item["category"] == "UTILITY"
        assert item["_schema"] == "zhiliao.wa_fallback_templates.v1"


def test_pack_yaml_points_at_the_catalog_file():
    roots = [
        ENGINE / "config" / "presets" / "packs" / "agency",
        ENGINE / "config" / "presets" / "packs" / "cross_border_private",
        ENGINE / "config" / "presets" / "packs" / "own_business",
        ENGINE / "config" / "presets" / "internal" / "gambling_operator",
    ]
    for root in roots:
        pack = yaml.safe_load((root / "pack.yaml").read_text(encoding="utf-8"))
        rel = (pack.get("files") or {}).get("wa_fallback_templates")
        assert rel == "wa_fallback_templates.yaml"
        data = yaml.safe_load((root / rel).read_text(encoding="utf-8"))
        assert data["schema"] == "zhiliao.wa_fallback_templates.v1"
        assert data["edition"] == pack["edition"]


def test_gamble_names_are_not_in_public_sources():
    """公开包会打进 config.example.yaml 与 src；完整博彩模板名只能出现在 internal 与提交文档。"""
    needles = list(GAMBLE_NAMES)
    roots = [
        ENGINE / "src",
        ENGINE / "config" / "config.example.yaml",
        ENGINE / "config" / "presets" / "packs",
    ]
    hits = []
    for root in roots:
        files = [root] if root.is_file() else root.rglob("*")
        for path in files:
            if not path.is_file() or path.suffix not in {".py", ".yaml", ".yml", ".md", ".json", ".js"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for name in needles:
                if name in text:
                    hits.append(f"{path.relative_to(ENGINE)}:{name}")
    assert hits == []
    example = (ENGINE / "config" / "config.example.yaml").read_text(encoding="utf-8")
    assert "cbp_svc_followup_zh" in example  # 注释里的中性示例
    internal = (ENGINE / "config" / "presets" / "internal" / "gambling_operator"
                / "wa_fallback_templates.yaml").read_text(encoding="utf-8")
    assert all(name in internal for name in GAMBLE_NAMES)


def test_edition_split_still_omits_internal_presets():
    policy = edition_gate.load_policy(ENGINE / "desktop" / "build" / "editions.json")
    assert edition_gate.check_policy(policy) == []
    omitted = set(policy["editions"]["public"]["omitted"])
    seed = set(policy["internal_only"]["seed_paths"])
    assert "config/presets/internal" in omitted
    assert "config/presets/internal" in seed
    gamble = ENGINE / "config" / "presets" / "internal" / "gambling_operator" / "wa_fallback_templates.yaml"
    rel = gamble.relative_to(ENGINE).as_posix()
    assert rel.startswith("config/presets/internal/")


# ── 发送路径：语言、旧模板、STOP、公开形态 ──────────────────────────────────

class _FakeGraph:
    def __init__(self):
        self.requests = []
        self.script = []
        self.lock = threading.Lock()
        graph = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def _reply(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}")
                with graph.lock:
                    graph.requests.append(data)
                    nxt = graph.script.pop(0) if graph.script else None
                if nxt:
                    return self._reply(*nxt)
                self._reply(200, {"messages": [{"id": "wamid.T"}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v21.0"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def graph(tmp_path, monkeypatch):
    monkeypatch.delenv("CHATX_FLAVOR", raising=False)
    monkeypatch.delenv("CHATX_EDITION", raising=False)
    monkeypatch.delenv("WA_CLOUD_GRAPH_BASE", raising=False)
    g = _FakeGraph()
    official_stop_gate.reset_for_tests()
    wac.reset_stats_for_tests()
    store = InboxStore(tmp_path / "inbox.db")
    protocol_bridge.register_inbox_store_getter(lambda: store)
    wac.configure_runtime({
        "graph_base": g.base,
        "window_fallback_template": _neutral_block(),
    })
    yield g, store
    g.close()
    protocol_bridge.register_inbox_store_getter(None)
    wac.configure_runtime({})
    wac.reset_stats_for_tests()
    official_stop_gate.reset_for_tests()


def _expire(graph):
    graph.script.append((400, {"error": {"code": 131047, "message": "Re-engagement message"}}))


def test_send_uses_fil_template_for_tl_and_for_stored_taglish(graph):
    g, store = graph
    _expire(g)
    out = asyncio.run(wac.wa_send_text(
        USER, "order\nstatus please", PNID, TOKEN, lang="tl"))
    assert out["ok"] is True and out["window_fallback"] is True
    assert out["fallback_lang"] == "tl" and out["fallback_source"] == "by_language"
    tpl = g.requests[-1]["template"]
    assert tpl["name"] == "cbp_svc_followup_fil"
    assert tpl["language"] == {"code": "fil"}
    assert tpl["components"][0]["parameters"][0]["text"] == "order status please"
    assert g.requests[-2]["type"] == "text"

    store.upsert_conversation(InboxConversation(
        conversation_id=f"whatsapp:{PNID}:wa:user:{USER}",
        platform="whatsapp", account_id=PNID, chat_key=f"wa:user:{USER}",
        language="taglish",
    ))
    _expire(g)
    out2 = asyncio.run(wac.wa_send_text(USER, "sige po", PNID, TOKEN))
    assert out2["fallback_lang"] == "tl"
    assert g.requests[-1]["template"]["name"] == "cbp_svc_followup_fil"


def test_send_legacy_template_keeps_full_param(graph):
    g, _store = graph
    wac.configure_runtime({
        "graph_base": g.base,
        "window_fallback_template": {"name": "reengage_v1", "language": "en_US", "text_param": True},
    })
    _expire(g)
    out = asyncio.run(wac.wa_send_text(USER, "your order shipped", PNID, TOKEN, lang="tl"))
    assert out["ok"] is True and out["fallback_source"] == "legacy"
    tpl = g.requests[-1]["template"]
    assert tpl["name"] == "reengage_v1"
    assert tpl["language"] == {"code": "en_US"}
    assert tpl["components"][0]["parameters"][0]["text"] == "your order shipped"


def test_stop_gate_blocks_text_and_template_before_any_post(graph):
    g, _store = graph
    official_stop_gate.apply_stop("whatsapp", PNID, f"wa:user:{USER}", hits=["stop"])
    _expire(g)
    text = asyncio.run(wac.wa_send_text(USER, "still there", PNID, TOKEN, lang="en"))
    tpl = asyncio.run(wac.wa_send_template(USER, "cbp_svc_followup_en", "en", PNID, TOKEN))
    assert text["ok"] is False and text["blocked"] == "stop_contact"
    assert tpl["ok"] is False and tpl["blocked"] == "stop_contact"
    assert g.requests == []


def test_public_flavor_does_not_send_gamble_template(graph, monkeypatch):
    g, _store = graph
    monkeypatch.setenv("CHATX_FLAVOR", "public")
    wac.configure_runtime({
        "graph_base": g.base,
        "window_fallback_template": {
            "default_language": "zh",
            "by_language": {
                "zh": _spec("wa_fb_gamble_member_zh", "zh_CN"),
                "en": _spec("wa_fb_gamble_member_en", "en"),
                "tl": _spec("wa_fb_gamble_member_fil", "fil"),
            },
        },
    })
    _expire(g)
    out = asyncio.run(wac.wa_send_text(USER, "账户协助", PNID, TOKEN, lang="zh"))
    assert out["ok"] is False
    assert out.get("window_fallback") is not True
    assert len(g.requests) == 1 and g.requests[0]["type"] == "text"
    health = wac.wa_cloud_health({"whatsapp_cloud": {
        "window_fallback_template": {
            "by_language": {"zh": _spec("wa_fb_gamble_member_zh", "zh_CN")},
        },
    }})
    assert health["window_fallback_template"] is False
    assert health["window_fallback_languages"] == []


def test_health_reports_configured_languages(monkeypatch):
    monkeypatch.delenv("CHATX_FLAVOR", raising=False)
    monkeypatch.delenv("CHATX_EDITION", raising=False)
    on = wac.wa_cloud_health({"whatsapp_cloud": {"window_fallback_template": _neutral_block()}})
    assert on["window_fallback_template"] is True
    assert on["window_fallback_languages"] == ["zh", "en", "tl"]
    off = wac.wa_cloud_health({"whatsapp_cloud": {"window_fallback_template": {"name": "", "by_language": {}}}})
    assert off["window_fallback_template"] is False
    assert off["window_fallback_languages"] == []
    legacy = wac.wa_cloud_health({"whatsapp_cloud": {"window_fallback_template": {
        "name": "reengage_v1", "language": "en_US",
    }}})
    assert legacy["window_fallback_template"] is True
    assert legacy["window_fallback_languages"] == ["legacy"]
