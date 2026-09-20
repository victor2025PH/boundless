# -*- coding: utf-8 -*-
"""唱歌协同 hint 契约（实施58 P1「唱不了别假唱」——文本假唱的源头预防）。

护住三件事：
1. 三态语义：非要歌=""；会真唱=FULFILL（草稿只是失败兜底）；发不出=REFUSAL
   （判定镜像 autosend_song 闸序：enabled→备货→频控→语言/避重选曲）。
2. 保守边界：A 线 can_deliver=False / persona 缺席 / 判定异常 → 一律 REFUSAL
   ——宁多一道「别承诺」，不放 LLM 裸奔。
3. 接线静态钉：skill_manager 两处注入 + 三处清残留；ai_client 消费块在场
   ——挪走任何一处先红（heat-zone 文件被并行线重构时的哨兵）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.companion import song_stock
from src.companion.song_stock import (
    _HINT_FULFILL,
    _HINT_REFUSAL,
    MIN_STOCK_BYTES,
    SongSendLedger,
    song_coherence_hint,
)

PID = "p_test"
TID = "origin_moon"


def _supply_cfg(tmp_path: Path, *, lang: str = "zh", stock: bool = True,
                enabled: bool = True, **extra) -> dict:
    tdir = tmp_path / "templates"
    tdir.mkdir(exist_ok=True)
    (tdir / "manifest.json").write_text(json.dumps({"templates": [{
        "id": TID, "title": "月光", "file": "m.wav",
        "lyrics": "月亮爬上了窗台", "lang": lang, "source": "origin",
        "enabled": True}]}, ensure_ascii=False), encoding="utf-8")
    sroot = tmp_path / "stock"
    if stock:
        sd = sroot / PID / "songs"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / f"{TID}.ogg").write_bytes(b"OggS" + b"\x00" * MIN_STOCK_BYTES)
    scfg = {"enabled": enabled, "templates_dir": str(tdir),
            "stock_dir": str(sroot)}
    scfg.update(extra)
    return {"companion": {"singing": scfg}}


@pytest.fixture()
def fresh_ledger(tmp_path, monkeypatch):
    led = SongSendLedger(path=tmp_path / "ledger.json")
    monkeypatch.setattr(song_stock, "get_song_ledger", lambda: led)
    return led


def _hint(cfg, text="给我唱首歌吧", **kw):
    kw.setdefault("persona_id", PID)
    kw.setdefault("conv_id", "tg:1:2")
    kw.setdefault("can_deliver", True)
    return song_coherence_hint(cfg, peer_text=text, **kw)


def test_not_a_request_is_silent(tmp_path, fresh_ledger):
    cfg = _supply_cfg(tmp_path)
    assert _hint(cfg, text="今天吃了火锅超开心") == ""
    assert _hint(cfg, text="") == ""


def test_negation_is_silent(tmp_path, fresh_ledger):
    cfg = _supply_cfg(tmp_path)
    assert _hint(cfg, text="别唱了别唱了") == ""


def test_a_line_without_deliver_always_refusal(tmp_path, fresh_ledger):
    cfg = _supply_cfg(tmp_path)
    assert _hint(cfg, can_deliver=False) == _HINT_REFUSAL


def test_full_supply_gives_fulfill(tmp_path, fresh_ledger):
    cfg = _supply_cfg(tmp_path)
    assert _hint(cfg) == _HINT_FULFILL


def test_disabled_gives_refusal(tmp_path, fresh_ledger):
    cfg = _supply_cfg(tmp_path, enabled=False)
    assert _hint(cfg) == _HINT_REFUSAL


def test_no_stock_gives_refusal(tmp_path, fresh_ledger):
    cfg = _supply_cfg(tmp_path, stock=False)
    assert _hint(cfg) == _HINT_REFUSAL


def test_missing_persona_conservative_refusal(tmp_path, fresh_ledger):
    cfg = _supply_cfg(tmp_path)
    assert _hint(cfg, persona_id="") == _HINT_REFUSAL


def test_daily_cap_gives_refusal(tmp_path, fresh_ledger):
    cfg = _supply_cfg(tmp_path, daily_cap=2, cooldown_hours=0)
    now = time.time()
    fresh_ledger.record("tg:1:2", TID, now=now - 3600)
    fresh_ledger.record("tg:1:2", "other", now=now - 1800)
    assert _hint(cfg) == _HINT_REFUSAL


def test_cooldown_gives_refusal(tmp_path, fresh_ledger):
    cfg = _supply_cfg(tmp_path, daily_cap=10, cooldown_hours=6)
    fresh_ledger.record("tg:1:2", "other", now=time.time() - 600)
    assert _hint(cfg) == _HINT_REFUSAL


def test_lang_mismatch_refusal_and_fallback_fulfill(tmp_path, fresh_ledger):
    cfg = _supply_cfg(tmp_path)  # zh 曲库
    assert _hint(cfg, text="can you sing me a song") == _HINT_REFUSAL
    cfg2 = _supply_cfg(tmp_path, allow_lang_fallback=True)
    assert _hint(cfg2, text="can you sing me a song") == _HINT_FULFILL


def test_sticky_demand_via_recent_texts(tmp_path, fresh_ledger):
    """实施66 P0-2 实录金标：上文「给我唱首歌吧」+ 本条「不行，必须唱」
    → 粘性补判 demand → FULFILL；无粘性证据同句必须静默（事故当晚就是
    这句逃逸词表 → hint 消失 → LLM 文字假唱）。"""
    cfg = _supply_cfg(tmp_path)
    assert _hint(cfg, text="不行，必须唱",
                 recent_texts=["给我唱首歌吧"]) == _HINT_FULFILL
    assert _hint(cfg, text="不行，必须唱") == ""


def test_loose_urge_gets_refusal(tmp_path, fresh_ledger):
    """泛化催促（无「唱」字）＝loose：本轮不会真唱 → 恒 REFUSAL 禁假唱。"""
    cfg = _supply_cfg(tmp_path)
    assert _hint(cfg, text="快点呀",
                 recent_texts=["给我唱首歌吧"]) == _HINT_REFUSAL


def test_pressure_exempts_cooldown_in_hint(tmp_path, fresh_ledger):
    """hint 与 autosend_song 的冷却豁免同判（漂移＝FULFILL/真发不一致）。"""
    cfg = _supply_cfg(tmp_path, daily_cap=10, cooldown_hours=6)
    fresh_ledger.record("tg:1:2", "other", now=time.time() - 600)
    assert _hint(cfg, text="不行，必须唱",
                 recent_texts=["给我唱首歌吧"],
                 demand_pressure=2) == _HINT_FULFILL
    assert _hint(cfg, demand_pressure=1) == _HINT_REFUSAL


def test_repeat_window_exhaustion_refusal(tmp_path, fresh_ledger):
    cfg = _supply_cfg(tmp_path, daily_cap=10, cooldown_hours=0)
    # 唯一模板 7 天窗内唱过 → 避重排空 → 宁可不唱 → REFUSAL
    fresh_ledger.record("tg:1:2", TID, now=time.time() - 86400)
    assert _hint(cfg) == _HINT_REFUSAL


def test_exception_path_conservative_refusal(tmp_path, fresh_ledger,
                                             monkeypatch):
    cfg = _supply_cfg(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(song_stock, "load_song_manifest", _boom)
    assert _hint(cfg) == _HINT_REFUSAL


def test_framing_caption_semantics():
    """止损话术（P3-1）：默认开、可关、同会话同日恒定、跨会话有变体。"""
    from src.companion.song_stock import _FRAMING_LINES, framing_caption
    now = time.time()
    c1 = framing_caption({"framing_line": True}, conv_id="a", now=now)
    assert c1 in _FRAMING_LINES
    assert framing_caption({"framing_line": True}, conv_id="a", now=now) == c1
    assert framing_caption({"framing_line": False}, conv_id="a") == ""
    assert framing_caption({}, conv_id="a", now=now) == c1     # 缺省=开
    variants = {framing_caption({}, conv_id=f"c{i}", now=now)
                for i in range(12)}
    assert len(variants) > 1                                   # 变体池在轮换


# ── 接线静态钉（heat-zone 并行重构的哨兵） ───────────────────────────────────

_SM = Path(__file__).resolve().parent.parent / "src" / "skills" / "skill_manager.py"
_AC = Path(__file__).resolve().parent.parent / "src" / "ai" / "ai_client.py"


def test_skill_manager_wiring_pinned():
    src = _SM.read_text(encoding="utf-8", errors="replace")
    # 注入点：A 线 Stage S（发不出时 REFUSAL）+ B 线草稿（can_deliver=True）
    # ——实施66 起 A 线已接真唱短路，不再有 can_deliver=False 的旁观注入。
    assert src.count('user_context["_song_coherence_hint"] =') >= 2, \
        "A/B 两处注入缺席"
    assert "_handle_song_request" in src, "A 线唱歌短路（Stage S）缺席"
    assert "can_deliver=True" in src, "B 线草稿 hint 缺席"
    # 残留清理：A 线轮首 + A 线回复后 + 草稿 finally ≥3 处 pop
    assert src.count('pop("_song_coherence_hint"') >= 3, "hint 残留清理被挪走"


def test_ai_client_consumer_pinned():
    src = _AC.read_text(encoding="utf-8", errors="replace")
    assert '_song_coherence_hint' in src, "prompt 消费块缺席"
    assert "唱歌协同——重要" in src, "prompt 段头缺席（hint 设了没人读）"
