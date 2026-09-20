# -*- coding: utf-8 -*-
"""Q-38（#272 追加 HPS7C3 / E2GXEP）：画像 name 禁写人设自称 + 摸底进度卡确认钮可见。

E2GXEP：WhatsApp Mizuki/12084403608 × Jeo/917983336714，客户说「Hi Mizuki」被写成
称呼=Mizuki（AI 推断）。Q-19 短语过、Q-25 因入站含 Mizuki 放行、Q-20 只管出站。
硬闸 = reserved_self_names（build_self_name_allowlist ∪ peer_calls_you）→ FactGate
own_name / vocative。UI：``.gl-slotchk`` 不再 overflow:hidden 裁确认钮。
"""
from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path

import pytest

from src.companion import fact_gate as fg
from src.companion.goals import profile_fill as pf
from src.companion.goals import profile_slots as ps
from src.companion.goals.profile_slots import cell_view
from src.companion.goals.store import get_goal_store, reset_goal_store
from src.utils.persona_guard import reserved_self_names

_ROOT = Path(__file__).resolve().parents[1]
PLAT, ACCT, CK = "whatsapp", "12084403608", "917983336714"
CONV = f"{PLAT}:{ACCT}:{CK}"
MIZUKI = {"name": "Mizuki"}


@pytest.fixture(autouse=True)
def _reset():
    pf.reset_stall()
    pf.bind_app(None)
    reset_goal_store()
    yield
    pf.reset_stall()
    pf.bind_app(None)
    reset_goal_store()


def _st():
    return get_goal_store(":memory:")


def _fields(st, ck=CK):
    return dict((st.get_customer_profile(PLAT, ck) or {}).get("fields") or {})


def _apply_name(st, value, evidence, inbound, reserved, *, ck=CK, cid=CONV, source="ai_inferred"):
    return pf.apply(
        st, PLAT, ck, {"name": {"value": value, "evidence": evidence}},
        source=source, conversation_id=cid, recent_inbound=list(inbound),
        reserved_self_names=reserved)


# ── 五例 ──────────────────────────────────────────────────────────────────────

def test_hi_mizuki_persona_not_written(caplog):
    """① 人设 Mizuki + 入站「Hi Mizuki」→ name 不写，reason own_name|vocative。"""
    caplog.set_level(logging.INFO)
    reserved = reserved_self_names(MIZUKI)
    assert "mizuki" in reserved
    st = _st()
    inbound = ["Hi Mizuki"]
    ok, why = fg.check("Mizuki", slot_or_kind="name", evidence="Hi Mizuki",
                       inbound_texts=inbound, reserved_self_names=reserved)
    assert ok is False and why in ("own_name", "vocative")
    ap = _apply_name(st, "Mizuki", "Hi Mizuki", inbound, reserved)
    assert "name" not in ap["written"]
    assert ap["skipped"].get("name") in ("own_name", "vocative")
    assert not cell_view(_fields(st).get("name"))[0]
    blob = " ".join(r.message for r in caplog.records)
    assert "reason=own_name" in blob or "reason=vocative" in blob
    got = dict(ps.capture_from_text("我叫Mizuki", reserved_self_names=reserved))
    assert "name" not in got
    assert dict(ps.capture_from_text("我叫Mizuki")).get("name") == "Mizuki"


def test_self_intro_jeo_writes():
    """② 「My name is Jeo / 我叫 Jeo」→ name=Jeo 过（人设仍是 Mizuki）。"""
    reserved = reserved_self_names(MIZUKI)
    st = _st()
    ap = _apply_name(st, "Jeo", "My name is Jeo", ["My name is Jeo"], reserved)
    assert "name" in ap["written"]
    assert cell_view(_fields(st).get("name"))[0] == "Jeo"
    st2 = _st()
    ap2 = _apply_name(st2, "Jeo", "我叫 Jeo", ["我叫 Jeo"], reserved, ck="2", cid=f"{PLAT}:{ACCT}:2")
    assert "name" in ap2["written"]
    assert dict(ps.capture_from_text("我叫Jeo", reserved_self_names=reserved)).get("name") == "Jeo"


def test_peer_calls_you_alicia_not_written():
    """③ peer_calls_you=Alicia + 「hey Alicia」→ 不写 Alicia。"""
    reserved = reserved_self_names({}, peer_calls_you="Alicia")
    assert "alicia" in reserved
    st = _st()
    inbound = ["hey Alicia"]
    ap = _apply_name(st, "Alicia", "hey Alicia", inbound, reserved)
    assert "name" not in ap["written"]
    assert ap["skipped"].get("name") in ("own_name", "vocative")
    assert not cell_view(_fields(st).get("name"))[0]


def test_no_persona_ordinary_name_ok():
    """④ 无 persona → 空 reserved，不误伤普通名。非 name 槽零新判据。"""
    reserved = reserved_self_names(None)
    assert reserved == set()
    st = _st()
    inbound = ["My name is Michael"]
    ap = _apply_name(st, "Michael", "My name is Michael", inbound, reserved)
    assert "name" in ap["written"]
    assert cell_view(_fields(st).get("name"))[0] == "Michael"
    # 非 name 槽：带 reserved 也不新开刀
    ok, why = fg.check("nurse", slot_or_kind="occupation", evidence="I am a nurse",
                       inbound_texts=["I am a nurse"], reserved_self_names=reserved_self_names(MIZUKI))
    assert (ok, why) == (True, "")


def test_e2gxep_replay_whatsapp_mizuki_jeo(caplog):
    """⑤ E2GXEP 回放：whatsapp:12084403608:917983336714，人设 Mizuki；confirmed 不动。"""
    caplog.set_level(logging.INFO)
    reserved = reserved_self_names(MIZUKI)
    st = _st()
    inbound = ["Hi Mizuki"]
    ap = _apply_name(st, "Mizuki", "Hi Mizuki", inbound, reserved)
    assert CONV.startswith("whatsapp:12084403608:917983336714")
    assert "name" not in ap["written"]
    assert ap["skipped"].get("name") in ("own_name", "vocative")
    # 坐席确认 / 手录一字不动
    ap_ok = pf.apply(st, PLAT, CK, {"name": "Jeo"}, source="confirmed", conversation_id=CONV)
    assert "name" in ap_ok["written"]
    v, src, stt = cell_view(_fields(st).get("name"))
    assert (v, src, stt) == ("Jeo", "confirmed", "confirmed")
    ap_ai = _apply_name(st, "Mizuki", "Hi Mizuki", inbound, reserved)
    assert "name" not in ap_ai["written"]
    assert cell_view(_fields(st).get("name"))[0] == "Jeo"


# ── UI：确认钮可见 + 行滚动 + 探问钉外 ───────────────────────────────────────────

def test_cp_goal_slot_confirm_not_clipped():
    js_path = _ROOT / "shared" / "copilot" / "components" / "cp-goal.js"
    mirror = _ROOT / "desktop" / "renderer" / "shared" / "copilot" / "components" / "cp-goal.js"
    assert js_path.read_bytes() == mirror.read_bytes(), "双树必须字节一致"
    js = js_path.read_text(encoding="utf-8")
    assert re.search(r"\.gl-slotchk\s*\{[^}]*overflow\s*:\s*hidden", js) is None, \
        ".gl-slotchk 不得 overflow:hidden（会裁确认钮）"
    assert re.search(r"\.gl-slots-row\s*\{[^}]*max-height", js), ".gl-slots-row 须限高滚动"
    assert ".gl-slot-val" in js
    confirm_css = js[js.index(".gl-slot-confirm {"):js.index(".gl-slot-confirm:hover")]
    assert "flex-shrink:0" in confirm_css
    render = js[js.index("_renderSlotsProgress(g) {"):js.index("const pendingTxt = pendingN")]
    assert "gl-slot-val" in render and 'data-act="slot_confirm"' in render
    assert "</div>${captureWarn}${probeUi}</div>" in js, "探问钮须在 gl-slots-row 外"


# ── 清洗 --own-name 默认 dry-run ───────────────────────────────────────────────

def _load_purge():
    p = _ROOT / "tools" / "profile_purge_unanchored.py"
    spec = importlib.util.spec_from_file_location("profile_purge_unanchored_q38", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_purge_own_name_dry_run_and_confirmed_kept(tmp_path):
    mod = _load_purge()
    src = (_ROOT / "tools" / "profile_purge_unanchored.py").read_text(encoding="utf-8")
    assert "--own-name" in src
    st = get_goal_store(str(tmp_path / "q38.db"))
    st.upsert_profile_cells(PLAT, CK, {
        "name": pf.make_cell("Mizuki", "ai_inferred", evidence="Hi Mizuki"),
        "occupation": pf.make_cell("nurse", "ai_inferred", evidence="I am a nurse"),
    })
    st.upsert_profile_cells(PLAT, "other", {
        "name": pf.make_cell("Mizuki", "confirmed"),
    })
    reserved = reserved_self_names(MIZUKI)
    rows = mod.scan_fields(PLAT, CK, dict((st.get_customer_profile(PLAT, CK) or {}).get("fields") or {}),
                           own_name_reserved=reserved, own_name_only=True)
    assert len(rows) == 1 and rows[0]["slot"] == "name"
    assert rows[0]["reason"] in ("own_name", "vocative")
    conf = mod.scan_fields(PLAT, "other", dict((st.get_customer_profile(PLAT, "other") or {}).get("fields") or {}),
                           own_name_reserved=reserved, own_name_only=True)
    assert conf == [], "confirmed 一字不动"
    n = mod.purge(st, rows, apply=False)
    assert n == 0
    assert cell_view(_fields(st).get("name"))[0] == "Mizuki"
    n = mod.purge(st, rows, apply=True)
    assert n == 1
    assert not cell_view(_fields(st).get("name"))[0]
    assert cell_view(dict((st.get_customer_profile(PLAT, "other") or {}).get("fields") or {}).get("name"))[0] == "Mizuki"
