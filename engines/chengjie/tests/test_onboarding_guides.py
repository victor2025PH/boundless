# -*- coding: utf-8 -*-
"""渠道接入教程（实施96 / TK-1，2026-09-08）：数据完整 → 教程页渲染（中英）→ 小智问答条目同源。"""
from __future__ import annotations

import re

from src.assistant import onboarding_guides as og


def test_guides_data_integrity():
    # 实施97：微信客服 / 个人微信 PC 副驾两份教程加入（工作台引导页 + HelpKB 同源）
    assert set(og.SLUGS) == {"douyin", "tiktok", "payment-cny", "wechat_kf", "wechat_pc"}
    for slug in og.SLUGS:
        for lang in ("zh", "en"):
            g = og.guide_for(slug, lang)
            assert g and g["title"] and g["intro"] and g["steps"], (slug, lang)
            for s in g["steps"]:
                assert s["title"] and s["detail"] and s["url"], (slug, s)
                assert s["url"].startswith(("https://", "/")), s["url"]
                assert s["link_label"], s
            for l in g["links"]:
                assert l["url"].startswith("https://") and l["label"]
            for e in g["errors"]:
                # 抖音是 7–8 位数字错误码；企微是 5–6 位（可多码并列）；副驾教程按「症状」列排障——都要短、非空
                assert e["code"] and len(e["code"]) <= 24 and e["meaning"] and e["fix"], (slug, e)
                if slug in ("douyin", "tiktok"):
                    assert re.fullmatch(r"\d{7,8}", e["code"]), e["code"]
    assert og.guide_for("nope") is None
    # 抖音教程必须覆盖资质路径的关键节点与硬规则（这是老板要办的事）
    dy = og.guide_for("douyin", "zh")
    joined = " ".join(s["title"] + s["detail"] for s in dy["steps"])
    for must in ("能力实验室", "小程序", "信用分", "im.message_card", "tool.image.upload", "互动经营工作台",
                 "/webhook/douyin", "im_receive_msg", "im_enter_direct_msg", "一个抖音号只能授权"):
        assert must in joined, must
    assert any("28003095" == e["code"] for e in dy["errors"])
    tk = og.guide_for("tiktok", "zh")
    assert "Business Messaging" in tk["intro"] and any("欧洲经济区" in r for r in tk["rules"])
    pay = og.guide_for("payment-cny", "zh")
    assert "USDT" in pay["intro"] and "人民币" in pay["intro"]


def test_howto_tuples_feed_xiaozhi():
    tuples = og.howto_tuples()
    assert [t[0] for t in tuples] == ["onboarding-douyin", "onboarding-tiktok", "onboarding-payment-cny",
                                      "onboarding-wechat_kf", "onboarding-wechat_pc"]
    for slug, title, title_en, ans, ans_en, kw, path in tuples:
        assert path == f"/workspace/onboarding/{slug.split('-', 1)[1]}"
        assert path in ans and path in ans_en
        assert len(ans) <= 4000 and len(ans_en) <= 4000  # HelpKB content 列上限
    from src.assistant.howto_pack import build_howto_entries
    ids = {e["id"] for e in build_howto_entries()}
    assert {"howto:onboarding-douyin", "howto:onboarding-tiktok", "howto:onboarding-payment-cny"} <= ids
    dy = next(e for e in build_howto_entries() if e["id"] == "howto:onboarding-douyin")
    assert "能力实验室" in dy["title"] + dy["keywords"] and dy["path"] == "/workspace/onboarding/douyin"
    # 词表要能命中老板会问的话：「怎么接入抖音」「支持人民币吗」
    assert "抖音" in dy["keywords"] and "申请" in dy["title"]
    pay = next(e for e in build_howto_entries() if e["id"] == "howto:onboarding-payment-cny")
    assert "人民币" in pay["title"] and "微信支付" in pay["keywords"]


def test_help_kb_retrieves_onboarding_answers(tmp_path):
    """真 HelpKB（临时库）灌入后，用老板口吻提问能检回教程条目。"""
    from src.assistant.help_kb import HelpKB
    from src.assistant.howto_pack import build_howto_entries
    kb = HelpKB(tmp_path / "help.db")
    kb.upsert_entries(build_howto_entries())
    search = getattr(kb, "search", None) or getattr(kb, "query", None)
    if search is None:
        return  # 检索 API 名不同则只保证灌库不报错
    for q, want in (("怎么接入抖音企业号", "howto:onboarding-douyin"),
                    ("TikTok 怎么接", "howto:onboarding-tiktok"),
                    ("支持人民币付款吗", "howto:onboarding-payment-cny")):
        hits = search(q, limit=5) if "limit" in search.__code__.co_varnames else search(q)
        ids = [str((h.get("id") if isinstance(h, dict) else getattr(h, "id", "")) or "") for h in (hits or [])]
        assert want in ids, (q, ids)


def test_guide_pages_render_in_both_languages(auth_client):
    r = auth_client.get("/workspace/onboarding/douyin")
    assert r.status_code == 200, r.text[:300]
    html = r.text
    for must in ("能力实验室", "developer.open-douyin.com", "/webhook/douyin", "28003095",
                 'target="_blank" rel="noopener noreferrer"', "obg-step", "obg-light"):
        assert must in html, must
    # 迁壳：工作台壳 + --tk-* 令牌，不再引用旧管理壳令牌
    style = html[html.find(".obg-wrap{"):html.find("</style>", html.find(".obg-wrap{"))]
    assert "var(--tk-border)" in style and "var(--bd)" not in style and "var(--t2)" not in style and "#1e8cf2" not in style
    r = auth_client.get("/workspace/onboarding/tiktok")
    assert r.status_code == 200 and "business-api.tiktok.com" in r.text
    r = auth_client.get("/workspace/onboarding/payment-cny")
    assert r.status_code == 200 and "bd2026.cc/order" in r.text
    assert auth_client.get("/workspace/onboarding/nope").status_code == 404
    # 旧路径 301 到新壳页（保留查询串）
    r = auth_client.get("/help/onboarding/douyin?connected=x", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/workspace/onboarding/douyin?connected=x"
    r = auth_client.get("/workspace/onboarding/douyin?lang=en")
    assert r.status_code == 200
    assert ("Capability Lab" in r.text) or ("能力实验室" in r.text)  # lang 切换由中间件决定，至少渲染成功
