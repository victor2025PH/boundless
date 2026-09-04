"""#145③ 人设产品目录隔离：注入层按人设过滤 + 出站守卫拦他人设产品名。"""

from __future__ import annotations

from src.companion.goals import offers as offers_mod
from src.companion.goals import site_catalog as sc
from src.utils.persona_guard import find_violations, sanitize

_CATALOG = {
    "site": {"base_url": "https://example.test", "order_path": "/order"},
    "products": [
        {
            "id": "prod_a",
            "name_zh": "专属货A",
            "name_en": "ExclusiveA",
            "personas": ["persona_a"],
            "pains": ["痛点a", "pain-a"],
            "pitch_zh": "A 的货",
            "url": "https://example.test/a",
        },
        {
            "id": "prod_b",
            "name_zh": "专属货B",
            "name_en": "ExclusiveB",
            "personas": ["persona_b"],
            "pains": ["痛点b", "pain-b"],
            "pitch_zh": "B 的货",
            "url": "https://example.test/b",
        },
        {
            "id": "prod_shared",
            "name_zh": "智聊 ChatX",
            "name_en": "ChatX",
            "pains": ["痛点a", "痛点b", "客服"],
            "pitch_zh": "共享货",
            "url": "https://example.test/shared",
        },
    ],
    "offers": [
        {
            "id": "off_b",
            "enabled": True,
            "authorized_by": "ops",
            "valid_until": "2099-01-01",
            "label_zh": "B专属折扣",
            "url": "https://example.test/off-b",
            "product_id": "prod_b",
            "for_churn": ["太贵"],
            "personas": ["persona_b"],
        },
    ],
}


def _ids(rows):
    return [str(p.get("id") or "") for p in rows]


def test_pick_products_persona_a_excludes_b():
    picks = sc.pick_products(
        _CATALOG, {"need": {"v": "痛点b"}}, persona_id="persona_a", limit=4)
    ids = _ids(picks)
    assert "prod_b" not in ids
    names = " ".join(
        f"{p.get('name_zh','')} {p.get('name_en','')}" for p in picks)
    assert "专属货B" not in names
    assert "ExclusiveB" not in names


def test_shared_product_visible_to_all_personas():
    for pid in ("persona_a", "persona_b", "someone_else"):
        picks = sc.pick_products(_CATALOG, {}, persona_id=pid, limit=4)
        assert "prod_shared" in _ids(picks)
        names = " ".join(str(p.get("name_zh") or "") for p in picks)
        assert "智聊 ChatX" in names


def test_bound_product_hidden_when_persona_id_empty():
    picks = sc.pick_products(_CATALOG, {}, persona_id="", limit=4)
    ids = _ids(picks)
    assert "prod_a" not in ids
    assert "prod_b" not in ids
    assert "prod_shared" in ids


def test_fields_persona_id_fallback():
    picks = sc.pick_products(
        _CATALOG, {"_persona_id": "persona_a"}, limit=4)
    assert "prod_a" in _ids(picks)
    assert "prod_b" not in _ids(picks)


def test_catalog_block_zero_foreign_product():
    prods = sc.pick_products(_CATALOG, {}, persona_id="persona_a", limit=4)
    block = sc.build_catalog_block(
        prods, push_level="soft", site=_CATALOG["site"],
        persona_id="persona_a")
    assert block
    assert "专属货B" not in block
    assert "ExclusiveB" not in block
    assert "prod_b" not in block


def test_pick_offer_foreign_persona_skipped():
    assert offers_mod.pick_offer(
        _CATALOG, churn_reason="太贵", persona_id="persona_a") is None
    hit = offers_mod.pick_offer(
        _CATALOG, churn_reason="太贵", persona_id="persona_b")
    assert hit and hit["id"] == "off_b"


def test_sanitize_strips_foreign_product_name():
    persona = {"id": "persona_a", "speaking": {"forbidden_phrases": []},
               "identity": {"deny_ai": False}}
    txt = "对了，专属货B最近在搞活动，我觉得一般般。今天天气不错。"
    cleaned, hits = sanitize(
        txt, persona, foreign_products=["专属货B", "ExclusiveB"])
    assert hits
    assert "专属货B" not in cleaned
    assert "今天天气不错" in cleaned


def test_sanitize_own_and_shared_names_not_flagged():
    persona = {"id": "persona_a"}
    txt = "我们店里客服用的是智聊 ChatX，专属货A 也还行。"
    cleaned, hits = sanitize(
        txt, persona, foreign_products=["专属货B", "ExclusiveB"])
    assert hits == []
    assert cleaned == txt


def test_sanitize_foreign_only_falls_to_neutral_not_original():
    persona = {"id": "persona_a"}
    txt = "专属货B。"
    cleaned, hits = sanitize(
        txt, persona, foreign_products=["专属货B"])
    assert hits
    assert "专属货B" not in cleaned
    assert cleaned == "那个我没怎么接触过，咱们聊点别的吧"


def test_find_violations_gold_foreign_name():
    persona = {"id": "persona_a"}
    hits = find_violations(
        "ExclusiveB is overpriced honestly.",
        persona, foreign_products=["ExclusiveB"])
    assert hits
    hits_ok = find_violations(
        "ChatX handles the inbox.",
        persona, foreign_products=["ExclusiveB"])
    assert hits_ok == []


def test_visible_to_persona_contract():
    shared = {"id": "x"}
    bound = {"id": "y", "personas": ["persona_a"]}
    assert offers_mod.visible_to_persona(shared, "") is True
    assert offers_mod.visible_to_persona(shared, "persona_b") is True
    assert offers_mod.visible_to_persona(bound, "") is False
    assert offers_mod.visible_to_persona(bound, "persona_b") is False
    assert offers_mod.visible_to_persona(bound, "persona_a") is True
    assert offers_mod.bound_persona_ids(shared) == ()
    assert "persona_a" in offers_mod.bound_persona_ids(bound)
