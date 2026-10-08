"""四套垂直行业模板包（2026-10-08 智语 · P1-8）。

跨境私域 / 代运营 / 自有业务进公开预设（config/presets/<name>.yaml + packs/<name>/）；
博彩运营商只进内部包（config/presets/internal/gambling_operator/），带年龄核实与余额红线。
每套含：人设、知识库种子、STOP 确认文案、待人工 SOP。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from src.utils.config_init import list_presets, load_preset

ENGINE = Path(__file__).resolve().parents[1]
PRESETS = ENGINE / "config" / "presets"
PUBLIC = ["cross_border_private", "agency", "own_business"]
INTERNAL = PRESETS / "internal" / "gambling_operator"
ALL_DIRS = [PRESETS / "packs" / n for n in PUBLIC] + [INTERNAL]
LANGS = ("zh", "en", "tl")

GAMBLING_WORDS = re.compile(
    r"casino|betting|\bbet\b|sabong|e-?sabong|jackpot|slot|odds|赔率|下注|投注|博彩|赌场|百家乐|PAGCOR", re.I)
RETENTION = re.compile(
    r"sure\?|are you sure|sigurado|确定吗|真的要|we'?ll miss|sayang|再考虑|reconsider|discount|优惠|promo", re.I)
BARE_IDENTITY = {"ai", "bot", "robot", "chatbot", "human", "机器人", "人工智能", "真人", "real person"}


def _y(p: Path):
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _pack(d: Path):
    return _y(d / "pack.yaml")


@pytest.mark.parametrize("d", ALL_DIRS, ids=lambda d: d.name)
def test_pack_has_four_parts(d):
    meta = _pack(d)
    assert meta["schema"] == "zhiliao.vertical_pack.v1" and meta["id"] == d.name
    for key in ("persona", "kb_seed", "stop_confirm", "handoff_sop"):
        assert (d / meta["files"][key]).is_file(), (d.name, key)
    assert set(LANGS) <= set(meta["langs"])
    assert meta["compliance"]["disclosure"]["honest_identity"] is True


@pytest.mark.parametrize("d", ALL_DIRS, ids=lambda d: d.name)
def test_persona_is_honest_and_guard_friendly(d):
    p = _y(d / "persona.yaml")["persona"]
    ident = p["identity"]
    assert ident["deny_ai"] is False and ident["claim_human"] is False and ident["public_ai"] is True
    assert "AI" in ident["deny_ai_reply"]
    fp = p["speaking"]["forbidden_phrases"]
    # 诚实档下单个身份名词会误删披露句（也会挖掉 kailangan 里的 ai）——只许写声明式整句
    assert not {str(x).strip().lower() for x in fp} & BARE_IDENTITY
    assert any("真人" in x for x in fp) and any("tao ako" in x for x in fp)
    assert p["speaking"]["language_follow"] is True


@pytest.mark.parametrize("d", ALL_DIRS, ids=lambda d: d.name)
def test_kb_seed_imports_and_is_retrievable(d, tmp_path):
    from src.utils.kb_store import KnowledgeBaseStore
    seed = _y(d / "kb_seed.yaml")
    entries = seed["entries"]
    assert len(entries) >= 6
    kb = KnowledgeBaseStore(tmp_path / "kb.db")
    ids = []
    for i, ent in enumerate(entries):
        data = dict(ent, id=f"{d.name[:6]}{i:02d}", enabled=1)
        ids.append(kb.add_entry(data))
        assert ent["title"] and ent["triggers"] and ent["example_reply_zh"]
        langs_hit = {"en" if re.search(r"[a-z]", " ".join(ent["triggers"]), re.I) else "",
                     "zh" if re.search(r"[\u4e00-\u9fff]", " ".join(ent["triggers"])) else ""}
        assert {"en", "zh"} <= langs_hit, (d.name, ent["title"])
    assert kb.stats()["enabled_entries"] == len(entries)
    for i, ent in enumerate(entries):
        res = kb.search(ent["triggers"][0], top_k=3)
        assert ids[i] in [x["id"] for x in res["entries"]], (d.name, ent["title"])


@pytest.mark.parametrize("d", ALL_DIRS, ids=lambda d: d.name)
def test_stop_confirm_copy_is_confirm_only(d):
    s = _y(d / "stop_confirm.yaml")
    assert s["schema"] == "zhiliao.stop_confirm.v1"
    for lang in ("en", "tl", "zh"):
        for variant in ("persona", "brand"):
            t = s["templates"][lang][variant]
            assert t and len(t) <= 90, (d.name, lang, variant)
            assert "?" not in t and "？" not in t
            assert "http" not in t and "www." not in t
            assert not RETENTION.search(t), (d.name, lang, t)
            assert not re.search(r"[\U0001F300-\U0001FAFF]", t)


@pytest.mark.parametrize("d", ALL_DIRS, ids=lambda d: d.name)
def test_handoff_sop_complete(d):
    s = _y(d / "handoff_sop.yaml")
    assert s["schema"] == "zhiliao.handoff_sop.v1"
    trig = s["triggers"]
    for k in ("explicit_human", "complaint_refund", "legal_threat", "privacy_payment"):
        assert trig[k], (d.name, k)
    assert s["sla"]["first_response_min"] > 0
    assert len(s["steps"]) >= 5
    for lang in LANGS:
        assert s["holding_reply"][lang] and s["after_hours_reply"][lang]


@pytest.mark.parametrize("name", PUBLIC)
def test_public_presets_listed_and_point_to_pack(name):
    assert name in list_presets()
    data = load_preset(name)
    assert data["compliance"]["disclosure"]["honest_identity"] is True
    assert _pack(PRESETS / "packs" / name)["preset"] == name
    assert _pack(PRESETS / "packs" / name)["edition"] == "public"


@pytest.mark.parametrize("name", PUBLIC)
def test_public_packs_carry_no_gambling_content(name):
    d = PRESETS / "packs" / name
    for f in ("kb_seed.yaml", "stop_confirm.yaml", "handoff_sop.yaml"):
        txt = (d / f).read_text(encoding="utf-8")
        assert not GAMBLING_WORDS.search(txt), (name, f, GAMBLING_WORDS.search(txt).group(0))
    preset_txt = (PRESETS / f"{name}.yaml").read_text(encoding="utf-8")
    assert not GAMBLING_WORDS.search(preset_txt)


def test_agency_pack_isolates_brands():
    meta = _pack(PRESETS / "packs" / "agency")
    assert meta["isolation"]["one_persona_per_brand"] is True and meta["isolation"]["kb_scope"] == "per_brand"
    fp = _y(PRESETS / "packs" / "agency" / "persona.yaml")["persona"]["speaking"]["forbidden_phrases"]
    assert "代运营" in fp and "other clients" in fp


# ── 博彩运营商：只进内部包 ────────────────────────────────────────────────


def test_gambling_pack_is_internal_only():
    meta = _pack(INTERNAL)
    assert meta["edition"] == "internal_only" and meta["preset"] is None
    assert {"age_verification", "balance_red_line", "responsible_gaming", "stop_gate"} <= set(meta["requires"])
    names = list_presets()
    assert "gambling_operator" not in names and not any("gambl" in n or "casino" in n for n in names)
    # 顶层预设里没有任何东西指向 internal/
    for n in names:
        assert "internal/" not in (PRESETS / f"{n}.yaml").read_text(encoding="utf-8")


def test_desktop_build_does_not_ship_presets():
    """桌面安装包的数据清单不带 config/presets（内部包内容不会随公开安装包外发）。"""
    src = (ENGINE / "desktop" / "build" / "build_backend.py").read_text(encoding="utf-8")
    assert '"presets"' not in src and "config/presets" not in src
    pol = ENGINE / "desktop" / "build" / "editions.json"
    if pol.exists():  # 智安的公开 / 内部包白名单落地后：公开包不得包含内部模板目录
        import json
        public = json.loads(pol.read_text(encoding="utf-8"))["editions"]["public"]
        assert "config/presets/internal" not in json.dumps(public)


def test_gambling_age_verification():
    c = _y(INTERNAL / "compliance.yaml")["age_verification"]
    assert c["required"] is True and c["min_age"] >= 18 and c["self_report_is_not_kyc"] is True
    for lang in LANGS:
        assert "{min_age}" in c["ask_copy"][lang] and "{min_age}" in c["age_block_copy"][lang]
    assert any("17" in s for s in c["underage_signals"]) and "minor" in c["underage_signals"]
    assert any("转人工" in s for s in c["on_underage_or_refuse"])


def test_gambling_balance_red_line():
    b = _y(INTERNAL / "compliance.yaml")["balance_red_line"]
    never = " ".join(b["ai_never"])
    for must in ("余额", "出款", "密码", "验证码"):
        assert must in never
    for lang in LANGS:
        assert b["detect_keywords"][lang]
        assert b["cashier_handoff_copy"][lang]
        assert not re.search(r"\d", b["cashier_handoff_copy"][lang]), "转收银话术里不能出现任何数字"
    assert "balance" in b["detect_keywords"]["en"] and "余额" in b["detect_keywords"]["zh"]
    sop = _y(INTERNAL / "handoff_sop.yaml")
    assert "balance_red_line" in sop["triggers"] and sop["sla"]["first_response_min"] <= 5
    kb = _y(INTERNAL / "kb_seed.yaml")["entries"]
    money = [e for e in kb if "收银" in e["title"]][0]
    assert not re.search(r"\d", money["example_reply_zh"])


def test_gambling_persona_blocks_win_and_chase_language():
    p = _y(INTERNAL / "persona.yaml")["persona"]
    fp = {x.lower() for x in p["speaking"]["forbidden_phrases"]}
    for w in ("稳赢", "包赢", "翻本", "sure win", "guaranteed win", "chase your losses", "siguradong panalo"):
        assert w in fp
    assert "投注建议" in p["boundaries"]["topics_to_avoid"]
    rg = _y(INTERNAL / "compliance.yaml")["responsible_gaming"]
    assert rg["signals"] and all(rg["rg_copy"][l] for l in LANGS)
    assert _y(INTERNAL / "compliance.yaml")["marketing"]["proactive_outreach"] is False


def test_pack_sample_replies_survive_persona_guard():
    """各包 KB 示例回复过本包人设的 persona_guard 不被误删（诚实档）。"""
    from src.utils.persona_guard import sanitize
    for d in ALL_DIRS:
        p = _y(d / "persona.yaml")["persona"]
        for ent in _y(d / "kb_seed.yaml")["entries"]:
            r = ent["example_reply_zh"]
            out, hits = sanitize(r, p, honest_identity=True)
            assert out == r and not hits, (d.name, r, hits)
