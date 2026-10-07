"""P0-5（2026-10-08）：禁词守卫按词边界——他加禄/Taglish/Bisaya 零误删 + 诚实披露口径。

事故：人设禁词 ``AI`` 按子串命中 kailangan / kaibigan / kumain / mainit，``bot`` 命中
abot，``human`` 命中 humanap；退化路径再在词中间挖字母（Kailangan→Klangan）。
"""

import pytest

from src.utils.persona_guard import (
    collect_forbidden,
    find_violations,
    match_forbidden_phrases,
    matches_human_claim,
    sanitize,
)

# 线上人设禁词表并集（故事主持 / 玩家朋友 / 推广 / 直播客服实际在用的词条形态）
_LIVE_FORBIDDEN = [
    "AI", "artificial intelligence", "bot", "robot", "chatbot", "language model",
    "real person", "totoong tao", "human", "tao ako", "WhatsApp", "BOUNDLESS",
    "website", "price", "promo", "invest", "loan", "utang online", "sure win",
    "guaranteed", "siguradong panalo", "customer service", "support team",
    "kikita ka", "easy money", "bonus", "coins", "deposit", "withdraw",
    "free credits", "guaranteed wins", "guaranteed profit", "gcash", "http",
    "www.", ".club", ".com",
]


def _persona(phrases=None):
    return {"speaking": {"forbidden_phrases": list(phrases or [])},
            "identity": {"deny_ai": False, "claim_human": False}}


# ── 日常语料：≥200 句，含高危词中子串（kailangan/kaibigan/kumain/mainit/maingay/
# paikot/abot/sabotahe/humanap/humanda，price/promo/website 在词中）────────────────
TL_CORPUS = [
    # Tagalog
    "Kumain ka na ba?",
    "Kumain na ako kanina, adobo yung ulam.",
    "Kailangan ko pa mag-deliver ng tatlong order bago umuwi.",
    "KAILANGAN mo bang tulong diyan?",
    "Haha same, kaibigan ko rin yun dati.",
    "Mga kaibigan ko nasa Batangas ngayon.",
    "Abot ko pa yung last trip pauwi.",
    "Hindi ko na abot yung jeep kanina.",
    "Mainit dito sa QC grabe.",
    "Sobrang init, mainit pa yung kape ko.",
    "Maingay yung kapitbahay, may videoke na naman.",
    "Paikot-ikot lang ako sa mall kanina.",
    "Paikot yung ruta ng bus, ang tagal.",
    "Parang sabotahe yung traffic sa EDSA ngayon haha.",
    "Humanap ka ng masarap na kainan diyan.",
    "Humanda ka, uulan daw mamaya.",
    "Humanga ako sa diskarte mo.",
    "Mabait yung amo ko ngayon, maaga kami pinauwi.",
    "Maaari ba kitang tawagan mamaya?",
    "Ang sakit ng ulo ko, kailangan ko ng pahinga.",
    "Sige, kwentuhan tayo mamaya.",
    "Ingat ka sa biyahe ha.",
    "Nakauwi ka na ba?",
    "Uy, kumusta ang araw mo?",
    "Grabe yung ulan kanina, baha sa España.",
    "Salamat sa pakikinig, kaibigan.",
    "Nagluto si nanay ng sinigang, ang sarap.",
    "Pagod na ako pero okay lang.",
    "Bukas ulit, maaga pa shift ko.",
    "Ano ulam niyo ngayong gabi?",
    "Nanood ako ng basketball kagabi, panalo yung Ginebra.",
    "Kailangan pa naming bumili ng bigas.",
    "Kaibigan mo ba si Jun?",
    "Ang init talaga, nakaka-tamad lumabas.",
    "Bumili ako ng bote ng tubig sa tindahan.",
    "Pumunta ako sa botika para sa gamot ni lola.",
    "Bumoto ka ba nung eleksyon?",
    "Maraming botante sa barangay namin.",
    "Nasira yung bota ko sa baha.",
    "Ang daming tao, ako na lang pipila.",
    "Ilang taon ka na ulit?",
    "Ang tagal na nating hindi nagkita.",
    "Mamaya na lang tayo mag-usap, nasa byahe pa ako.",
    "Kumain muna tayo bago umalis.",
    "Kailangan ba talaga yun?",
    "Masarap yung laing ni tita.",
    "Bumili ako ng kaing ng mangga sa palengke.",
    "Nasa baitang ka na ba ng hagdan?",
    "Sakaling umulan, dalhin mo payong mo.",
    "Daig pa niya yung artista sa ganda.",
    "Wala pang aircon sa kwarto ko, mainit.",
    "Nasa airport na si kuya, pauwi na galing Dubai.",
    "Gusto ko pumunta sa Taiwan balang araw.",
    "Kain tayo sa kainan sa kanto.",
    "Maikli lang yung pila kanina.",
    "Maingat ka sa daan ha.",
    "Nagpaalam na ako sa kanila.",
    "Hindi pa ako nakakapag-almusal.",
    "Saan ka na ngayon?",
    "Masaya ako para sayo.",
    "Mahal ang bilihin ngayon sa palengke.",
    "Huwag kang mag-alala, anak.",
    "Ipagdasal natin yan.",
    "Salamat sa Diyos, ligtas sila.",
    "May sitio fiesta sa amin sa Sabado.",
    "Nag-text si ate, pauwi na raw siya.",
    "Ang ganda ng paglubog ng araw kanina.",
    "Antok na antok na ako.",
    "Hay naku, sira na naman yung jeep.",
    "Puno yung tren kanina, siksikan.",
    "Kailangan kong magpa-load mamaya.",
    "Ang kulit ng pamangkin ko.",
    "Mainit pa yung pandesal, bili ka na.",
    "Ang ingay sa labas, may nag-aaway.",
    "Paikutin mo lang yung susi, bubukas yan.",
    "Inabot ako ng gabi sa trabaho.",
    "Naabot ko rin yung quota ko ngayong buwan.",
    "Abutan mo nga ako ng tubig.",
    "Humanap na ako ng bagong boarding house.",
    "Humahanap pa rin ako ng trabaho.",
    "Naghahanda na kami para sa Pasko.",
    "Mga kaibigan, salamat sa suporta.",
    "Kaibigang tunay ka talaga.",
    "Kumakain ka ba ng balut?",
    "Kakakain ko lang, busog pa ako.",
    "Hindi ako mahilig sa maanghang.",
    "Okay lang yan, bawi tayo bukas.",
    "Medyo maulan sa Baguio ngayon.",
    "Nakakamiss yung luto ni lola.",
    "Bakit ka gising pa?",
    "Matulog ka na, may pasok ka pa.",
    "Nag-overtime ako kahapon.",
    "Natapos ko na rin yung report.",
    "Kailangan kong umuwi ng probinsya next week.",
    "Saan ka nagtatrabaho ngayon?",
    "Alam mo ba yung bagong kainan sa Maginhawa?",
    "Ang sarap ng halo-halo pag mainit.",
    "Maingay pero masaya yung handaan.",
    "Grabe, ang bilis ng panahon.",
    # Taglish
    "Same, super busy ako this week.",
    "Grabe yung traffic, parang forever na.",
    "Sige, see you later!",
    "Ang pricey naman ng milk tea dun.",
    "Priceless yung reaction ng tatay ko.",
    "Overpriced daw yung bagong resto sa BGC.",
    "Na-promote si ate sa work niya, proud ako.",
    "May promotion daw sa company nila.",
    "Promoter si Carlo sa mall dati.",
    "Busy siya sa pagwewebsite niya, freelance.",
    "Nag-email na ako sa HR kanina.",
    "Wait lang, may tumatawag.",
    "Again? Late na naman yung delivery.",
    "Sabi niya he said yes daw, kinilig ako.",
    "Pumunta kami sa Thailand last year.",
    "Kailangan ko ng new chair, masakit na likod ko.",
    "No pain no gain, sabi nga nila.",
    "Ang gaining ng timbang ko, puro kain kasi.",
    "Na-check mo na ba yung email ko?",
    "Mag-deliver pa ako sa Makati after lunch.",
    "Nag-shopping kami sa Divisoria.",
    "Mag-text ka lang pag nakauwi ka na.",
    "Late na ako sa shift, bye muna.",
    "Ang cute ng pusa mo, awww.",
    "Medyo tired na ako pero kaya pa.",
    "Na-stress ako sa deadline kanina.",
    "Let's go, kain tayo ng samgyup.",
    "Ang galing mo talaga mag-drawing.",
    "Super init today, parang oven.",
    "Ang humid ng weather, sticky lahat.",
    "Nag-robotics class yung anak ko, ang saya niya.",
    "Ang kulit ng humanities prof ko dati.",
    "Ang inhumane ng schedule ng shift namin haha.",
    "Nagpa-reserve na ako sa resto for Friday.",
    "Kailangan ko i-renew yung passport ko.",
    "Nakakaiyak yung K-drama kagabi.",
    "Ang ganda ng playlist mo, share mo naman.",
    "Mag-jogging tayo bukas ng umaga.",
    "Nakatulog ako sa jeep, lumampas tuloy ako.",
    "Ang sarap mag-chill dito sa probinsya.",
    "Yung boss ko, super bait today.",
    "Bago yung phone ko, regalo ni kuya.",
    "Mag-overtime ulit ako bukas, sayang extra.",
    "Libre ko next time, promise.",
    "Okay naman yung interview, sana makapasa.",
    "Nag-volunteer ako sa feeding program.",
    "Ang daming assignments ng kapatid ko.",
    "Mag-grocery muna ako bago umuwi.",
    "Sige na, tulog na tayo.",
    "Ang galing ng performance nila sa fiesta.",
    "Excited na ako sa long weekend.",
    "Uwian na, sa wakas!",
    "Ang sarap ng tulog ko kagabi.",
    "Naubos na yung load ko, wifi muna.",
    "Nagbake ako ng ube cheese pandesal.",
    "Nakaka-proud yung kapatid ko, graduate na.",
    "Medyo maingay sa call center floor pero sanay na.",
    "Night shift ulit, kape is life.",
    "Nanood kami ng sine, ang ganda ng story.",
    "Ang hirap mag-commute pag rush hour.",
    "Si Lola nag-video call, miss na raw niya kami.",
    "Paikot yung GPS, naligaw tuloy ako.",
    "Diskarte lang, kaya mo yan.",
    "Kaya mo yan, laban lang.",
    "Hindi ko alam kung kakayanin ko, pero try ko.",
    "Masaya yung reunion dinner namin.",
    "Dumating na yung balikbayan box galing Dubai.",
    "Mag-ingat ka sa scam texts ha.",
    "Ang ganda ng sunset sa Manila Bay.",
    "Sabay tayo mag-lunch bukas?",
    "Kakatapos lang ng meeting namin.",
    "Ang tagal ng sahod, ubos na budget.",
    "Sweldo na bukas, sa wakas.",
    "First time ko mag-solo travel, kinakabahan ako.",
    "Ang Hawaii shirt ni tito, ang kulay.",
    "Maaga pa naman, kape muna.",
    "Ang dami kong nakain, busog na busog.",
    # Bisaya / Cebuano
    "Kaon na ta, gutom na kaayo ko.",
    "Nikaon na ka?",
    "Init kaayo karon sa Cebu.",
    "Mainit pa ang kape, inom na.",
    "Asa ka padulong?",
    "Unsa imong ganahan nga sud-an?",
    "Salamat kaayo, higala.",
    "Daghan kaayo tawo sa Colon karon.",
    "Kinahanglan ko mopauli una.",
    "Maayong buntag! Kumusta ka?",
    "Ayaw kabalaka, okay ra ta.",
    "Lami kaayo ang lechon diri.",
    "Kapoy na kaayo ko, tulog na ta.",
    "Asa man ka nagtrabaho karon?",
    "Ugma na lang ta mag-istorya.",
    "Ingat sa biyahe, ha.",
    "Nindot kaayo ang panahon karon.",
    "Gimingaw na ko sa akong mama.",
    "Unsa diay imong plano karong semana?",
    "Ganahan ko mokaon og puso ug barbecue.",
    "Kusog kaayo ang ulan ganina.",
    "Hinay-hinay lang sa dalan.",
    "Pila na imong edad karon?",
    "Naa koy klase ugma sayo.",
    "Mobalik ko diri sunod semana.",
    "Paikot-ikot mi sa SM ganina.",
    "Abot na ang bus, dali!",
    "Naabot nako ang last trip.",
    "Mabaw ra ang dagat diri.",
    "Pag-amping kanunay, higala.",
    "Busog na kaayo ko, salamat.",
    "Lingaw kaayo ang fiesta sa among baryo.",
    "Gikapoy ko sa trabaho ganina.",
    "Daghang salamat sa imong pagpaminaw.",
    "Ayaw kalimti imong payong.",
]

assert len(TL_CORPUS) >= 200, len(TL_CORPUS)


@pytest.mark.parametrize("honest", [False, True])
def test_tl_corpus_zero_false_positive(honest):
    p = _persona(_LIVE_FORBIDDEN)
    bad = []
    for s in TL_CORPUS:
        hits = find_violations(s, p, honest_identity=honest, foreign_products=[])
        cleaned, hits2 = sanitize(s, p, honest_identity=honest, foreign_products=[])
        if hits or hits2 or cleaned != s:
            bad.append((s, hits, cleaned))
    assert bad == [], bad[:10]


def test_tl_corpus_whole_text_untouched():
    """整段喂（模型一次回多句）也逐字节不变。"""
    p = _persona(_LIVE_FORBIDDEN)
    blob = " ".join(TL_CORPUS[:60])
    assert sanitize(blob, p, foreign_products=[]) == (blob, [])


# ── 原事故复现：旧实现会被挖坏的句子 ────────────────────────────────────────────

@pytest.mark.parametrize("txt", [
    "Kumain ka na ba? Sige, kwentuhan tayo mamaya.",
    "Kailangan ko pa mag-deliver bago mag-alas sais.",
    "Haha same, kaibigan ko rin yun.",
    "Abot ko pa yung last trip pauwi.",
    "Mainit dito sa QC grabe.",
])
def test_incident_sentences_not_mutilated(txt):
    p = _persona(["AI", "bot", "human", "real person", "tao ako"])
    assert sanitize(txt, p, foreign_products=[]) == (txt, [])


# ── 真违规仍然命中（词边界不等于放水）──────────────────────────────────────────

@pytest.mark.parametrize("txt,phrase", [
    ("Ako ay AI lang naman.", "AI"),
    ("Nag-AI ka ba sa assignment mo?", "AI"),          # 连字符前缀
    ("Yung AI'ng sinabi mo kanina?", "AI"),            # 撇号
    ("我是AI的朋友", "AI"),                              # CJK 紧邻
    ("Ang daming bots sa comments.", "bot"),           # 复数
    ("Bot's reply was weird.", "bot"),                 # 所有格
    ("Check mo https://example.test/x", "http"),       # URL 前缀
    ("Punta ka sa www.example.test", "www."),
    ("Ang promos ngayon grabe.", "promo"),
    ("May withdrawal ba ngayon?", "withdraw"),         # ≥4 字母屈折
    ("Real-person talaga yan.", "real person"),        # 连字符变体
    ("Yung websiteng sinabi mo.", "website"),          # 他加禄连接词 -ng
    ("Pwede ba sa GCash?", "gcash"),
])
def test_real_mentions_still_caught(txt, phrase):
    assert phrase in match_forbidden_phrases(txt, [phrase])


def test_cjk_phrase_still_substring_and_whitespace_insensitive():
    assert match_forbidden_phrases("亲，有什么  可以帮您的吗", ["有什么可以帮您的"])
    assert match_forbidden_phrases("这是专属货B吧", ["专属货B"])


def test_explicit_wildcards():
    assert match_forbidden_phrases("deposito mo na", ["depo*"])
    assert not match_forbidden_phrases("deposito mo na", ["deposit"])
    assert match_forbidden_phrases("chatbots everywhere", ["*bot"])
    assert not match_forbidden_phrases("abot ko pa", ["bot"])


def test_degraded_inline_strip_only_whole_words():
    """整段一句全违规 → 退化抹词：只删整词，绝不在词中间挖字母。"""
    p = _persona(["AI"])
    cleaned, hits = sanitize("AI kailangan mainit kaibigan", p, foreign_products=[])
    assert hits == ["AI"]
    assert cleaned == "kailangan mainit kaibigan"
    p2 = _persona(["http"])
    cleaned2, _ = sanitize("Tingnan mo https://example.test ha", p2, foreign_products=[])
    assert cleaned2 == "Tingnan mo ha"


# ── 诚实披露（honest_identity=True）：如实说是 AI 保留，冒充真人照剥 ─────────────

_HONEST_PERSONA = _persona([
    "AI", "artificial intelligence", "bot", "robot", "chatbot", "language model",
    "real person", "totoong tao", "human", "tao ako", "customer service",
])


@pytest.mark.parametrize("txt", [
    "Honest lang: AI assistant ako ng team na tumutulong dito. Pero sige, kwentuhan pa rin tayo!",
    "Oo, AI ako — assistant lang ng team. Ikaw, kumusta ka?",
    "Hindi ako totoong tao, AI assistant ako. Pero nandito ako para makinig.",
    "Hindi ako human, AI assistant ako ng team.",
    "To be honest, I'm an AI assistant, not a real person.",
    "Tinuod lang, AI assistant ko sa team. Kumusta imong adlaw?",
    "老实说我是AI助手，不过可以继续聊。",
])
def test_honest_disclosure_preserved(txt):
    cleaned, hits = sanitize(txt, _HONEST_PERSONA, honest_identity=True,
                             foreign_products=[])
    assert (cleaned, hits) == (txt, [])


def test_honest_mode_drops_bare_identity_terms_only():
    fb = collect_forbidden(_HONEST_PERSONA, honest_identity=True)
    for bare in ("AI", "bot", "human", "real person", "totoong tao", "robot"):
        assert bare not in fb["phrases"]
    assert "tao ako" in fb["phrases"]            # 声明式短语保留
    assert "customer service" in fb["phrases"]   # 非身份类保留
    assert fb["human_claim"] is True
    assert collect_forbidden(_HONEST_PERSONA)["human_claim"] is False


@pytest.mark.parametrize("txt,keep", [
    ("Totoong tao ako, promise. Kumain ka na?", "Kumain ka na?"),
    ("Tao ako, hindi bot haha. Ingat ka sa biyahe.", "Ingat ka sa biyahe."),
    ("I'm a real person, don't worry! Ano ulam mo?", "Ano ulam mo?"),
    ("I'm not a bot lol. How was work?", "How was work?"),
    ("Hindi ako bot, swear. Musta ka?", "Musta ka?"),
    ("Real person ako, legit. Saan ka ngayon?", "Saan ka ngayon?"),
    ("Tawo ko, dili ko bot. Unsa imong plano karon?", "Unsa imong plano karon?"),
    ("我是真人啦。你吃饭了吗？", "你吃饭了吗？"),
    ("我不是机器人。今天累不累？", "今天累不累？"),
    ("मैं असली इंसान हूँ। आप कैसे हैं?", "आप कैसे हैं?"),
])
def test_honest_mode_strips_human_impersonation(txt, keep):
    cleaned, hits = sanitize(txt, _HONEST_PERSONA, honest_identity=True,
                             foreign_products=[])
    assert hits
    assert keep in cleaned
    assert matches_human_claim(cleaned) == []


def test_honest_mode_human_claim_without_any_config():
    """冒充真人声明不依赖人设禁词：空禁词表也剥。"""
    cleaned, hits = sanitize("Totoong tao ako. Kain na tayo!", _persona([]),
                             honest_identity=True, foreign_products=[])
    assert hits and "Totoong tao ako" not in cleaned and "Kain na tayo" in cleaned


@pytest.mark.parametrize("txt", ["Tao ako haha", "I'm a real human being!", "我是真人"])
def test_honest_mode_all_impersonation_falls_back_to_disclosure(txt):
    cleaned, hits = sanitize(txt, _HONEST_PERSONA, honest_identity=True,
                             foreign_products=[])
    assert hits
    assert cleaned != txt and cleaned.strip()
    assert "AI" in cleaned
    assert matches_human_claim(cleaned) == []


@pytest.mark.parametrize("txt", [
    "Hindi ako totoong tao.",
    "I'm not a real person, I'm an AI assistant.",
    "I'm not human, pero nandito ako.",
    "Ikaw ba, totoong tao ka ba?",
    "You think I'm a real person? Haha.",
    "Ang daming tao, ako na lang pipila.",
    "Dili ko tawo, AI assistant ko.",
])
def test_human_claim_negations_and_near_misses(txt):
    assert matches_human_claim(txt) == []


def test_default_mode_unchanged_for_identity_words():
    """honest_identity=False（回避档）：单个 AI 照旧按句剥，只是不再挖词中字母。"""
    p = _persona(["AI"])
    cleaned, hits = sanitize("AI ako. Kumain ka na?", p, foreign_products=[])
    assert hits == ["AI"] and cleaned == "Kumain ka na?"
    assert matches_human_claim("Tao ako") and find_violations(
        "Tao ako", _persona([]), foreign_products=[]) == []


# ── 仓库预设：诚实档默认开 + 身份口径片段（P0-5 任务 2）──────────────────────────
from pathlib import Path as _Path  # noqa: E402

import yaml as _yaml  # noqa: E402

_CFG = _Path(__file__).resolve().parent.parent / "config"
_SNIPPET = _CFG / "presets" / "snippets" / "persona_identity_honest.yaml"


def _snippet():
    return _yaml.safe_load(_SNIPPET.read_text(encoding="utf-8"))


@pytest.mark.parametrize("preset", sorted(p.stem for p in (_CFG / "presets").glob("*.yaml")))
def test_presets_default_honest_identity_true(preset):
    data = _yaml.safe_load((_CFG / "presets" / f"{preset}.yaml").read_text(encoding="utf-8"))
    assert data["compliance"]["disclosure"]["honest_identity"] is True


def test_snippet_honest_and_no_bare_identity_terms():
    from src.utils.persona_guard import _is_bare_identity_term
    d = _snippet()
    assert d["compliance"]["disclosure"]["honest_identity"] is True
    for name, phrases in d["forbidden_phrase_sets"].items():
        for ph in phrases:
            assert not _is_bare_identity_term(ph), (name, ph)
    for ph in d["example_persona"]["speaking"]["forbidden_phrases"]:
        assert not _is_bare_identity_term(ph), ph


@pytest.mark.parametrize("lang", ["en", "tl", "ceb", "zh", "hi"])
def test_snippet_identity_claims_caught_by_builtin(lang):
    for ph in _snippet()["forbidden_phrase_sets"][f"identity_claims_{lang}"]:
        assert matches_human_claim(ph), ph


@pytest.mark.parametrize("honest", [False, True])
def test_snippet_example_persona_zero_false_positive(honest):
    persona = _snippet()["example_persona"]
    bad = [s for s in TL_CORPUS
           if sanitize(s, persona, honest_identity=honest, foreign_products=[]) != (s, [])]
    assert bad == []


def test_snippet_example_persona_disclosure_kept_claim_stripped():
    persona = _snippet()["example_persona"]
    ok = "Honest lang: AI assistant ako ng team. Kwentuhan pa rin tayo!"
    assert sanitize(ok, persona, honest_identity=True, foreign_products=[]) == (ok, [])
    cleaned, hits = sanitize("Real person ako, legit. Kain na tayo!", persona,
                             honest_identity=True, foreign_products=[])
    assert hits and cleaned == "Kain na tayo!"
