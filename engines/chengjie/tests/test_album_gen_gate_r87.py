# -*- coding: utf-8 -*-
"""R87 #329a（3TCW5P）：「发的图不是相册导入的图」——生成链归属可见 + 有真人相册的人设默认不生成。

真闸回放：十翼 12:12 ``send_media delivered`` 但无「已发注册相册媒体」行（生成链 + 自动定妆入册），
12:24 才是相册 id。运营不知道生成链存在，客户拿到一张相册里没有的脸。

钉四层：
A. ``photo_capability.persona_generate_allowed``：显式 ``capabilities.photo_generate`` 即定；缺省
   有真人相册 → 关、无相册 → 开；``persona_has_real_album`` 不把 auto_generated 生成图算真人相册。
B. ``image_gate.generate_with_gate``：真出图后端 + selfie + 人设不许生成 → ``ok=False error=gen_disabled``
   且**不调 provider.generate**；album 后端 / 物体图 / 允许生成的人设照常。
C. ``image_autosend.stage_image_file``：同闸（B 线物体图也覆盖），记 fallback reason ``gen_disabled``。
D. 相册页：能力条第二行开关 ``pma-cap-gen``（PUT capabilities.photo_generate）、卡片「AI 生成」角标、
   筛选 ``aigen``；词条三语齐平；``capabilities.photo_generate`` 列入 prompt 豁免表。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.companion import photo_capability as pc

_ROOT = Path(__file__).resolve().parents[1]


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def list(self, pid):
        return list(self.rows)


def _real(pid="p1"):
    return _Store([{"id": "a", "media_type": "photo", "tags": ["kind:selfie"]}])


def _gen_only():
    return _Store([{"id": "g", "media_type": "photo", "tags": ["auto_generated", "scene:cafe"]},
                   {"id": "v", "media_type": "video", "tags": []}])


# ── A ────────────────────────────────────────────────────────────────────────

def test_persona_has_real_album_ignores_generated_and_video(monkeypatch):
    import src.companion.persona_media_store as pms
    monkeypatch.setattr(pms, "get_persona_media_store", lambda: _real())
    assert pc.persona_has_real_album("p1") is True
    monkeypatch.setattr(pms, "get_persona_media_store", lambda: _gen_only())
    assert pc.persona_has_real_album("p1") is False
    monkeypatch.setattr(pms, "get_persona_media_store", lambda: None)
    assert pc.persona_has_real_album("p1") is False
    assert pc.persona_has_real_album("") is False


def test_generate_allowed_default_depends_on_real_album():
    p = {"id": "p1", "capabilities": {"photos": True}}
    assert pc.persona_generate_allowed(p, has_real_album=True) is False
    assert pc.persona_generate_allowed(p, has_real_album=False) is True
    assert pc.persona_generate_allowed(None, has_real_album=False) is True
    assert pc.persona_generate_allowed(None, has_real_album=True) is False


def test_generate_allowed_explicit_wins():
    on = {"id": "p1", "capabilities": {"photos": True, "photo_generate": True}}
    off = {"id": "p1", "capabilities": {"photos": True, "photo_generate": False}}
    assert pc.persona_generate_allowed(on, has_real_album=True) is True
    assert pc.persona_generate_allowed(off, has_real_album=False) is False
    # None 视为未设置
    unset = {"id": "p1", "capabilities": {"photo_generate": None}}
    assert pc.persona_generate_allowed(unset, has_real_album=True) is False


def test_generate_allowed_by_id_resolves_persona_and_album(monkeypatch):
    import src.companion.persona_media_store as pms
    from src.utils import persona_manager as pmm

    class _PM:
        def __init__(self, p):
            self.p = p

        def get_persona_by_id(self, pid):
            return self.p

    monkeypatch.setattr(pms, "get_persona_media_store", lambda: _real())
    monkeypatch.setattr(pmm.PersonaManager, "get_instance",
                        classmethod(lambda cls: _PM({"id": "p1", "capabilities": {"photos": True}})))
    assert pc.persona_generate_allowed_by_id("p1") is False
    monkeypatch.setattr(pmm.PersonaManager, "get_instance",
                        classmethod(lambda cls: _PM({"id": "p1", "capabilities": {"photos": True, "photo_generate": True}})))
    assert pc.persona_generate_allowed_by_id("p1") is True
    assert pc.persona_generate_allowed_by_id("") is True


# ── B ────────────────────────────────────────────────────────────────────────

class _Prov:
    def __init__(self, backend):
        self.backend = backend
        self.enabled = True
        self.calls = 0

    async def generate(self, prompt, seed=-1, **kw):
        self.calls += 1
        return SimpleNamespace(ok=True, image_path="x.png", provider=self.backend, extra={})


def test_generate_with_gate_blocks_selfie_for_real_album_persona(monkeypatch):
    from src.ai import image_gate
    monkeypatch.setattr(pc, "persona_has_real_album", lambda pid: True)
    prov = _Prov("command")
    persona = {"id": "p1", "capabilities": {"photos": True}}
    res = asyncio.run(image_gate.generate_with_gate(prov, "p", persona=persona,
                                                    gate_cfg={"enabled": False}))
    assert res.ok is False and res.error == "gen_disabled" and prov.calls == 0
    # 显式开 → 放行（gate 关，直接返回 provider 结果）
    persona["capabilities"]["photo_generate"] = True
    res2 = asyncio.run(image_gate.generate_with_gate(prov, "p", persona=persona,
                                                     gate_cfg={"enabled": False}))
    assert res2.ok is True and prov.calls == 1


def test_generate_with_gate_album_backend_and_object_untouched(monkeypatch):
    from src.ai import image_gate
    monkeypatch.setattr(pc, "persona_has_real_album", lambda pid: True)
    persona = {"id": "p1", "capabilities": {"photos": True}}
    alb = _Prov("album")
    r1 = asyncio.run(image_gate.generate_with_gate(alb, "p", persona=persona, gate_cfg={"enabled": False}))
    assert r1.ok is True and alb.calls == 1
    obj = _Prov("command")
    r2 = asyncio.run(image_gate.generate_with_gate(obj, "p", persona=persona, kind="object",
                                                   gate_cfg={"enabled": False}))
    assert r2.ok is True and obj.calls == 1
    # 没有人设的人像生成（persona=None）不受影响
    r3 = asyncio.run(image_gate.generate_with_gate(_Prov("command"), "p", persona=None,
                                                   gate_cfg={"enabled": False}))
    assert r3.ok is True


# ── C ────────────────────────────────────────────────────────────────────────

def test_stage_image_file_gen_disabled_records_reason(monkeypatch):
    from src.inbox import image_autosend as ia
    from src.ai import companion_selfie as cs
    monkeypatch.setattr(pc, "persona_generate_allowed_by_id", lambda pid: False)
    prov = _Prov("command")
    monkeypatch.setattr(cs, "get_selfie_provider", lambda cfg: prov)
    monkeypatch.setattr(ia, "resolve_image_autosend_cfg", lambda cfg: {"enabled": True, "provider": {"backend": "command"}})
    before = int(ia._METRICS.get("fallback") or 0)
    out = asyncio.run(ia.stage_image_file({}, "telegram", "a", "p1", {"kind": ia.KIND_SELFIE}))
    assert out is None and prov.calls == 0
    assert ia._METRICS["last_reason"] == "gen_disabled" and int(ia._METRICS["fallback"]) == before + 1
    # album 后端不经生成闸
    alb = _Prov("album")
    monkeypatch.setattr(cs, "get_selfie_provider", lambda cfg: alb)
    monkeypatch.setattr(ia, "resolve_image_autosend_cfg", lambda cfg: {"enabled": True, "provider": {"backend": "album"}})
    asyncio.run(ia.stage_image_file({}, "telegram", "a", "p1", {"kind": ia.KIND_SELFIE}))
    assert alb.calls == 1


# ── D ────────────────────────────────────────────────────────────────────────

def test_personas_template_wires_gen_toggle_badge_and_filter():
    html = (_ROOT / "src" / "web" / "templates" / "personas.html").read_text(encoding="utf-8", errors="ignore")
    assert 'id="pma-cap-gen"' in html and "pmaToggleGen" in html
    assert "capabilities: { photo_generate: on }" in html
    assert "photo_generate" in html and "_pmaGenOn()" in html
    assert "indexOf('auto_generated')" in html and "pma_st_aigen" in html
    assert "case 'aigen'" in html and "['aigen', 'pma_f_aigen']" in html
    for k in ("pma_gen_label", "pma_gen_tip", "pma_gen_default_on", "pma_gen_default_off",
              "pma_gen_saved_on", "pma_gen_saved_off", "pma_st_aigen_t", "pma_f_aigen"):
        assert k in html, k


def test_album_gen_pack_three_langs():
    from src.web.i18n_packs import album_gen_r87 as P
    assert set(P.ZH) == set(P.EN) == set(P.ZH_HANT)
    for k, v in P.EN.items():
        assert not re.search(r"[\u4e00-\u9fff]", v), k
    from src.web.i18n_packs import collect_all
    zh, en, extras = collect_all()
    assert zh["pma_st_aigen"] == "AI 生成" and en["pma_f_aigen"] == P.EN["pma_f_aigen"]


def test_photo_generate_is_prompt_exempt():
    from src.utils.persona_manager import PROMPT_EXEMPT_FIELDS
    assert "capabilities.photo_generate" in PROMPT_EXEMPT_FIELDS
