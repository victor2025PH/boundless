# -*- coding: utf-8 -*-
"""P1-1 speech_prints 数据区 overlay 门禁（2026-08-18）。

钉住：合并语义（overlay 同键胜出/元键保留）、物化保鲜（双源指纹，手改任一源
即重建，新鲜不重写）、条目校验（print 必填/上限/未知键拒）、save 原子写+立即
重物化、**包端到端**（重定向后 colloquial_rewrite.prompt_block 真吃到 overlay
条目，且出厂角色不丢）、包升级重命名探测（_PRINTS_PATH 符号在位）、桥接两处
接线静态钉、备货聚合认账 overlay。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.ai.speech_prints_overlay import (
    ensure_runtime_file,
    install_redirect,
    merged_view,
    overlay_path,
    runtime_path,
    save_entry,
    validate_entry,
)

ENGINE = Path(__file__).resolve().parents[1]


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config" / "config.yaml"))
    yield tmp_path / "config"


def _mk_factory(tmp_path) -> Path:
    f = tmp_path / "factory.json"
    f.write_text(json.dumps({
        "_说明": "出厂说明",
        "出厂角色": {"print": "出厂画像", "catch": ["老词"]},
        "被覆盖角色": {"print": "出厂版画像"},
    }, ensure_ascii=False), encoding="utf-8")
    return f


# ── 合并与物化 ───────────────────────────────────────────────────────────────

def test_merged_view_overlay_wins(tmp_path):
    f = _mk_factory(tmp_path)
    o = tmp_path / "overlay.json"
    o.write_text(json.dumps({
        "被覆盖角色": {"print": "实例版画像"},
        "新角色": {"print": "新画像"},
    }, ensure_ascii=False), encoding="utf-8")
    m = merged_view(factory=f, overlay=o)
    assert m["被覆盖角色"]["print"] == "实例版画像", "overlay 同键必须胜出"
    assert m["出厂角色"]["print"] == "出厂画像" and "新角色" in m
    assert "_说明" in m, "出厂元键保留"


def test_runtime_freshness_and_rebuild(tmp_path):
    f = _mk_factory(tmp_path)
    o = tmp_path / "overlay.json"
    r = tmp_path / "runtime.json"
    p1 = ensure_runtime_file(factory=f, overlay=o, runtime=r)
    assert p1 == r and r.is_file()
    sig1 = r.stat().st_mtime_ns
    assert ensure_runtime_file(factory=f, overlay=o, runtime=r) == r
    assert r.stat().st_mtime_ns == sig1, "双源未变不得重写（热路成本）"
    time.sleep(0.02)
    o.write_text(json.dumps({"新角色": {"print": "手改后画像"}},
                            ensure_ascii=False), encoding="utf-8")
    ensure_runtime_file(factory=f, overlay=o, runtime=r)
    data = json.loads(r.read_text(encoding="utf-8"))
    assert data["新角色"]["print"] == "手改后画像", "手改 overlay 必须触发重建"


def test_runtime_none_when_no_sources(tmp_path):
    assert ensure_runtime_file(factory=tmp_path / "nope1.json",
                               overlay=tmp_path / "nope2.json",
                               runtime=tmp_path / "rt.json") is None


# ── 校验与保存 ───────────────────────────────────────────────────────────────

def test_validate_entry_rules():
    ok = validate_entry({"print": "画像", "catch": ["a", " b "],
                         "example": "范文", "guide": "指引"})
    assert ok["catch"] == ["a", "b"]
    with pytest.raises(ValueError):
        validate_entry({"catch": ["x"]})                    # 缺 print
    with pytest.raises(ValueError):
        validate_entry({"print": "x", "bogus": 1})          # 未知键
    with pytest.raises(ValueError):
        validate_entry({"print": "x" * 601})                # 超限
    with pytest.raises(ValueError):
        validate_entry({"print": "x", "catch": "不是列表"})


def test_save_entry_writes_overlay_and_runtime(iso):
    clean = save_entry("小测", {"print": "测试画像", "catch": ["嗯嗯"]})
    assert clean["print"] == "测试画像"
    data = json.loads(overlay_path().read_text(encoding="utf-8"))
    assert data["小测"]["print"] == "测试画像"
    rt = json.loads(runtime_path().read_text(encoding="utf-8"))
    assert rt["小测"]["print"] == "测试画像", "save 后 runtime 必须已重物化"
    for bad in ("", "_偷元键", "x" * 81):
        with pytest.raises(ValueError):
            save_entry(bad, {"print": "p"})


# ── 包端到端（正金标：包内消费真吃到 overlay）────────────────────────────────

@pytest.fixture
def pkg(iso):
    from src.ai.spoken_style_bridge import _find_platform_dir
    pdir = _find_platform_dir()
    if pdir is None:
        pytest.skip("spoken_style 包不在本仓")
    import sys
    if str(pdir) not in sys.path:
        sys.path.insert(0, str(pdir))
    from spoken_style import colloquial_rewrite as cr
    orig_path = cr._PRINTS_PATH
    orig_cache = dict(cr._prints_cache)
    yield cr
    cr._PRINTS_PATH = orig_path
    cr._prints_cache.clear()
    cr._prints_cache.update({"mtime": -1.0, "data": {}})
    cr._prints_cache.update({k: v for k, v in orig_cache.items()
                             if k in ("mtime", "data")} or
                            {"mtime": -1.0, "data": {}})


def test_package_consumes_overlay_after_redirect(pkg):
    cr = pkg
    save_entry("覆盖端到端角色", {"print": "端到端画像词组XYZQ"})
    assert install_redirect() is True
    assert cr._PRINTS_PATH == runtime_path()
    cr._prints_cache.update({"mtime": -1.0})          # 逼一次重读
    block = cr.prompt_block("覆盖端到端角色")
    assert "端到端画像词组XYZQ" in block, "包内 L1 必须吃到 overlay 条目"
    # 出厂角色不丢（合并语义）：取出厂件里任一真实角色验证
    factory_keys = [k for k in json.loads(
        (ENGINE.parent.parent / "platform" / "spoken_style" / "data"
         / "speech_prints.json").read_text(encoding="utf-8")).keys()
        if not k.startswith("_")]
    assert factory_keys and cr.prompt_block(factory_keys[0]), \
        "重定向后出厂角色的指纹段必须照常产出"


def test_package_rename_detector():
    """avatarhub 升级若重命名 _PRINTS_PATH——重定向会软失效，此门禁先红点名。"""
    src = (ENGINE.parent.parent / "platform" / "spoken_style"
           / "colloquial_rewrite.py").read_text(encoding="utf-8")
    assert "_PRINTS_PATH" in src and "def _prints()" in src


# ── 接线静态钉 + 备货认账 ────────────────────────────────────────────────────

def test_bridge_wiring_pins():
    src = (ENGINE / "src" / "ai" / "spoken_style_bridge.py").read_text(
        encoding="utf-8")
    i_import = src.index("import spoken_style")
    i_redirect = src.index("install_redirect")
    assert i_import < i_redirect, "重定向必须在包装载成功之后"
    i_l1 = src.index("def system_block")
    i_fresh = src.index("ensure_runtime_file")
    assert i_fresh > i_l1, "L1 热路必须带 overlay 保鲜检查"


def test_persona_stock_counts_overlay(iso):
    from src.companion.persona_stock import load_speech_print_keys
    if load_speech_print_keys() is None:
        pytest.skip("出厂件不在本仓")
    save_entry("备货认账角色", {"print": "画像"})
    keys = load_speech_print_keys()
    assert keys is not None and "备货认账角色" in keys, \
        "备货就绪判定必须认 overlay 条目"


# ── 保存端点（路由级：校验/口称名键/备货行翻✓）──────────────────────────────

@pytest.fixture
def sp_client(iso, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import src.web.routes.persona_media_routes as pmr
    from src.companion.persona_media_store import (
        configure_persona_media_store, reset_persona_media_store)
    from src.utils.persona_manager import PersonaManager

    monkeypatch.setattr(pmr, "_ALBUM_ROOT", tmp_path / "albums")
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    pm = PersonaManager.get_instance()
    pm.upsert_profile("splin", {"name": "Sp Lin", "personality": "温柔",
                                "role": "companion"})

    class _CfgMgr:
        config = {}
        config_path = str(tmp_path / "config" / "config.yaml")

    app = FastAPI()
    pmr.register_persona_media_routes(
        app, auth_dep=lambda: True, config_manager=_CfgMgr())
    yield TestClient(app)
    reset_persona_media_store()
    pm.delete_profile("splin")


def test_save_endpoint_roundtrip(sp_client):
    r = sp_client.post("/api/personas/splin/speech-print",
                       json={"entry": {"print": "路由级画像", "catch": ["嗯"]}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["spoken_name"] == "Sp Lin"
    data = json.loads(overlay_path().read_text(encoding="utf-8"))
    assert data["Sp Lin"]["print"] == "路由级画像", "键必须=口称名逐字"
    # 备货行随存随认（出厂件在仓才可判）
    r2 = sp_client.get("/api/personas/splin/stock-readiness")
    sp = (r2.json().get("items") or {}).get("speech_print") or {}
    if sp.get("applicable"):
        assert sp.get("ready") is True, "保存后备货指纹行必须翻✓"


def test_save_endpoint_validation(sp_client):
    assert sp_client.post("/api/personas/splin/speech-print",
                          json={"entry": {"catch": ["x"]}}).status_code == 400
    assert sp_client.post("/api/personas/nope/speech-print",
                          json={"entry": {"print": "p"}}).status_code == 404
