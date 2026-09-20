import pytest

from src.ai.translation_service import TranslationService, detect_language


def test_detect_language_common_scripts():
    assert detect_language("你好，今天怎么样") == "zh"
    assert detect_language("こんにちは、元気？") == "ja"
    assert detect_language("안녕하세요") == "ko"
    assert detect_language("مرحبا كيف حالك") == "ar"
    assert detect_language("Привет как дела") == "ru"
    assert detect_language("hola, gracias") == "es"
    assert detect_language("hello friend") == "en"


def test_detect_language_southeast_asian_and_more():
    # 跨境客服高频客户语种（此前会落 en/unknown，现确定性识别）
    assert detect_language("สวัสดีครับ อยากสอบถามราคา") == "th"   # 泰语
    assert detect_language("Xin chào, tôi muốn mua sản phẩm này") == "vi"  # 越南语
    assert detect_language("ជំរាបសួរ តើតម្លៃប៉ុន្មាន") == "km"      # 高棉语
    assert detect_language("Γειά σου τι κάνεις") == "el"           # 希腊语
    assert detect_language("שלום מה שלומך") == "he"               # 希伯来语
    assert detect_language("Halo, saya mau tanya harga") == "id"   # 印尼语（关键词）
    assert detect_language("Salamat, magkano po ito") == "tl"      # 菲律宾语
    assert detect_language("") == "unknown"


def test_detect_language_thai_baht_symbol_not_misdetected():
    # 跨境电商 THB 报价：泰铢符号 ฿ 不应让纯英文消息被判成泰语
    assert detect_language("Price: 100฿ only, free shipping") == "en"


@pytest.mark.asyncio
async def test_translation_service_identity_and_cache():
    svc = TranslationService(default_target_lang="zh")
    same = await svc.translate("你好", target_lang="zh")
    assert same.ok is True
    assert same.provider == "identity"
    assert same.translated_text == "你好"

    first = await svc.translate("hello friend", target_lang="zh")
    assert first.ok is False
    assert first.error == "provider_unavailable"
    second = await svc.translate("hello friend", target_lang="zh")
    assert second.cached is True


@pytest.mark.asyncio
async def test_translation_service_uses_ai_client():
    class FakeAI:
        async def chat(self, prompt, context=None):
            assert "Translate" in prompt
            return "你好朋友"

    svc = TranslationService(ai_client=FakeAI())
    rv = await svc.translate("hello friend", target_lang="zh")
    assert rv.ok is True
    assert rv.provider == "ai"
    assert rv.translated_text == "你好朋友"


# ── 中文变体（繁体/粤语）目标语（2026-08-29）──────────────────────────────────

def test_normalize_lang_zh_variants_first_class():
    """zh-tw 不再折叠成 zh（否则简→繁在 identity 短路里恒原样返回）；
    zh-hant/zh-hk 归一到 zh-tw；简体折叠语义不变。"""
    from src.ai.translation_service import LANG_NAMES, normalize_lang
    assert normalize_lang("zh-TW") == "zh-tw"
    assert normalize_lang("zh_Hant") == "zh-tw"
    assert normalize_lang("zh-HK") == "zh-tw"
    assert normalize_lang("zh-CN") == "zh"
    assert normalize_lang("yue") == "yue"
    # AI 线 prompt 显名（缺了会把裸码写进 prompt）
    assert LANG_NAMES["zh-tw"] == "Traditional Chinese"
    assert LANG_NAMES["yue"] == "Cantonese"


@pytest.mark.asyncio
async def test_translate_zh_to_traditional_and_cantonese_not_identity():
    """简体→繁体/粤语必须真走引擎（修复前 zh-tw 被 normalize 折叠 → 恒 identity）。"""
    class FakeAI:
        def __init__(self, out):
            self._out = out

        async def chat(self, prompt, context=None):
            return self._out

    svc_tw = TranslationService(ai_client=FakeAI("謝謝你的幫忙"))
    r_tw = await svc_tw.translate("谢谢你的帮忙", target_lang="zh-TW")
    assert r_tw.ok is True and r_tw.provider == "ai"
    assert r_tw.translated_text == "謝謝你的幫忙"
    assert r_tw.target_lang == "zh-tw"

    svc_yue = TranslationService(ai_client=FakeAI("唔該晒你幫手"))
    r_yue = await svc_yue.translate("谢谢你的帮忙", target_lang="yue")
    assert r_yue.ok is True and r_yue.provider == "ai"
    assert r_yue.translated_text == "唔該晒你幫手"


# ── #176（2026-09-05 skuio 实录）：记忆/缓存命中路径也要过「引擎拒绝话术」守卫 ────
# 「Exactly」→「好的，请发送需要翻译的内容。」：0831 之前被 LLM 引擎写进翻译记忆的
# meta 拒绝话术，守卫只挂引擎新译分支 → 每次命中记忆都原样吐出、永不重验。

_BAD_SRC = "Exactly"
_BAD_OUT = "好的，请发送需要翻译的内容。"


class _CountingAI:
    def __init__(self, answer: str):
        self.answer = answer
        self.calls = 0

    async def chat(self, prompt, context=None):
        self.calls += 1
        return self.answer


def _mem_store():
    from src.ai.translation_memory import TranslationMemoryStore
    return TranslationMemoryStore(":memory:")


def _seed(store, svc, src, out, *, source="en", target="zh"):
    key = svc._cache_key(src, source, target, "chat", engine="")
    store.put(key, source_text=src, translated_text=out, source_lang=source,
              target_lang=target, style="chat", engine="ai", glossary_ver="")
    return key


def test_refusal_detector_hits_incident_pair():
    from src.ai.translation_confidence import looks_like_engine_refusal
    assert looks_like_engine_refusal(_BAD_SRC, _BAD_OUT) is True
    assert looks_like_engine_refusal(_BAD_SRC, "没错") is False


@pytest.mark.asyncio
async def test_memory_hit_with_refusal_is_purged_and_retranslated():
    store = _mem_store()
    ai = _CountingAI("没错")
    svc = TranslationService(ai_client=ai, memory_store=store)
    key = _seed(store, svc, _BAD_SRC, _BAD_OUT)
    before = int(TranslationService._refusal_purged.get("memory", 0))
    rv = await svc.translate(_BAD_SRC, target_lang="zh")
    # 坏记忆没被原样吐出：走引擎重译拿到真译文，且不是缓存命中
    assert rv.ok is True and rv.translated_text == "没错" and rv.cached is False
    assert ai.calls == 1
    # 坏条目已被新译覆盖（删旧 + 新译成功入库）
    row = store.get(key)
    assert row is not None and row["translated_text"] == "没错"
    assert int(TranslationService._refusal_purged.get("memory", 0)) == before + 1
    # 再来一次：L1 命中正常译文，不再打引擎
    rv2 = await svc.translate(_BAD_SRC, target_lang="zh")
    assert rv2.cached is True and rv2.translated_text == "没错" and ai.calls == 1


@pytest.mark.asyncio
async def test_memory_hit_normal_entry_untouched():
    store = _mem_store()
    ai = _CountingAI("不该被调用")
    svc = TranslationService(ai_client=ai, memory_store=store)
    key = _seed(store, svc, "hello friend", "你好朋友")
    rv = await svc.translate("hello friend", target_lang="zh")
    assert rv.ok is True and rv.cached is True and rv.translated_text == "你好朋友"
    assert ai.calls == 0
    assert store.get(key)["translated_text"] == "你好朋友"


@pytest.mark.asyncio
async def test_memory_hit_refusal_retranslate_failure_does_not_reseed_memory():
    """重译仍是拒绝话术 → 只进短负缓存（ok=False），绝不再写回记忆。"""
    store = _mem_store()
    ai = _CountingAI(_BAD_OUT)
    svc = TranslationService(ai_client=ai, memory_store=store)
    key = _seed(store, svc, _BAD_SRC, _BAD_OUT)
    rv = await svc.translate(_BAD_SRC, target_lang="zh")
    assert rv.ok is False and rv.error == "engine_refusal"
    assert store.get(key) is None          # 旧坏行已删、新坏译文没入库


def test_l1_cache_hit_with_refusal_is_evicted():
    from src.ai.translation_service import TranslationResult
    svc = TranslationService(default_target_lang="zh")
    key = svc._cache_key(_BAD_SRC, "en", "zh", "chat", engine="")
    svc._cache_put(key, TranslationResult(_BAD_SRC, _BAD_OUT, "en", "zh", True, provider="ai"))
    assert svc._cache_get(key) is None
    assert key not in svc._cache
    # 正常条目照常命中；失败态负缓存（ok=False 的 refusal 结果）也照常保留
    good_key = svc._cache_key("hi", "en", "zh", "chat", engine="")
    svc._cache_put(good_key, TranslationResult("hi", "嗨", "en", "zh", True, provider="ai"))
    assert svc._cache_get(good_key).translated_text == "嗨"
    neg_key = svc._cache_key("1", "en", "zh", "chat", engine="")
    svc._cache_put(neg_key, TranslationResult("1", "1", "en", "zh", False, provider="ai",
                                              error="engine_refusal"))
    assert svc._cache_get(neg_key).error == "engine_refusal"


def test_memory_store_delete_and_iter():
    store = _mem_store()
    store.put("k1", source_text="a", translated_text="b", source_lang="en", target_lang="zh")
    store.put("k2", source_text="c", translated_text="d", source_lang="en", target_lang="zh")
    assert len(store.iter_rows()) == 2
    assert store.delete("k1") is True and store.delete("k1") is False
    assert store.delete("") is False
    assert [r["cache_key"] for r in store.iter_rows()] == ["k2"]
    assert store.delete_many(["k2", "nope"]) == 1
    assert store.stats()["entries"] == 0


def test_xlate_memory_purge_cli_dry_run_then_apply(tmp_path, capsys):
    """清洗 CLI：dry-run 只列不删（只读打开）；--apply 只删坏行、好行保留。"""
    from src.ai.translation_memory import TranslationMemoryStore
    import tools.xlate_memory_purge as purge

    db = tmp_path / "translation_memory.db"
    st = TranslationMemoryStore(db)
    svc = TranslationService(default_target_lang="zh")
    bad_key = _seed(st, svc, _BAD_SRC, _BAD_OUT)
    good_key = _seed(st, svc, "hello friend", "你好朋友")
    st.close()

    assert purge.run(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "命中 1 条" in out
    st2 = TranslationMemoryStore(db)
    assert st2.get(bad_key) is not None          # dry-run 没删
    st2.close()

    assert purge.run(["--db", str(db), "--apply", "--json"]) == 0
    rep = __import__("json").loads(capsys.readouterr().out)
    assert rep["total_bad"] == 1 and rep["total_deleted"] == 1
    st3 = TranslationMemoryStore(db)
    assert st3.get(bad_key) is None and st3.get(good_key) is not None
    st3.close()
    # 库不存在 → 跳过不报错
    assert purge.run(["--db", str(tmp_path / "nope.db")]) == 0
    assert "库不存在" in capsys.readouterr().out

