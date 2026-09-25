"""LLM 口语化改写门禁 — src/ai/voice_colloquial_llm.py。

守住 A 档（本地 LLM 深度口语化）的关键不变量：
  - 缓存命中不重复调 LLM（对 TTS 缓存友好 + 省调用）
  - 失败/超时/校验不过 → None（调用方回落规则档，绝不阻塞语音）
  - 端点熔断：连续失败进冷却期，冷却期直接回落（不让挂掉的端点每条拖满超时）
  - 输出消毒：剥前缀/引号/元话语段；长度/语言校验
  - 短句 / 非中文 no-op（与规则档同口径）
"""
from __future__ import annotations

import pytest

from src.ai.voice_colloquial_llm import (
    build_colloquial_prompt,
    is_interrogative_dominant,
    llm_colloquialize,
    reset_state,
    sanitize_llm_output,
)


async def _identity_embed(texts):
    """恒等向量（余弦 1.0）：语义守卫（2026-08-10 起 fail-closed）在单测里
    默认「可校验且通过」——没有 embed 的客户端改写会被整体拒用。"""
    return [[1.0, 0.0] for _ in texts]


class _FakeAI:
    """假 AIClient：按 responses（定值/列表/callable）返回 rewrite_local 结果。"""

    def __init__(self, responses):
        self.calls = 0
        self._responses = responses

    async def rewrite_local(self, system, user, *, timeout_sec=8.0, **kw):
        self.calls += 1
        r = self._responses
        if callable(r):
            return r(user)
        if isinstance(r, list):
            return r[min(self.calls - 1, len(r) - 1)]
        return r

    async def embed(self, texts):
        return await _identity_embed(texts)


# ── 纯函数：prompt ───────────────────────────────────────────────────────────
def test_build_prompt_lead_toggle_and_tone():
    p_lead = build_colloquial_prompt("warm", lead=True)
    p_nolead = build_colloquial_prompt("warm", lead=False)
    assert "开头" in p_lead                       # 允许句首口语连接
    assert "不要用语气词或开场白开头" in p_nolead   # 非首条：直接说
    assert "温暖亲切" in p_lead                    # 情绪语气注入
    assert "保持原意" in p_lead                    # 信息保真硬要求


def test_build_prompt_style():
    p = build_colloquial_prompt("playful", lead=True, style="撒娇黏人")
    assert "撒娇黏人" in p


def test_build_prompt_persona_catchphrase_in_style():
    p = build_colloquial_prompt(
        "warm", lead=True,
        style="声线底色：撒娇；标志性口头禅（可自然用于句首）：哇、啊对对对")
    assert "哇" in p and "啊对对对" in p


# ── 纯函数：sanitize ─────────────────────────────────────────────────────────
def test_sanitize_strips_prefix_and_quotes():
    assert sanitize_llm_output("口语版：其实我今天挺好的啦",
                               "我今天状态不错还行呢") == "其实我今天挺好的啦"
    assert sanitize_llm_output("「其实我今天挺好的」",
                               "我今天状态不错还行呢") == "其实我今天挺好的"


def test_sanitize_meta_truncation():
    out = sanitize_llm_output("其实我今天挺好的\n\n解释：把书面语改成了口语",
                              "我今天状态不错啊啊")
    assert out == "其实我今天挺好的"


def test_sanitize_length_guard():
    orig = "我今天状态不错啊"  # 8 字
    assert sanitize_llm_output("嗯", orig) is None          # 过短
    assert sanitize_llm_output("其实" * 20, orig) is None    # 过长（发挥过度/夹带）


def test_sanitize_rejects_content_eating_shrink():
    """2026-07-27 实锤回归钉：讲故事回复被口语化腰斩（67→33，比率 0.49）竟能过
    旧 0.3 下限 → 客户听到半截故事（「怎么说话说一半呀」）。改写砍 >40% 必拒。"""
    story = ("从前有只小猫特别爱喝抹茶拿铁，每天下午都蹲在咖啡馆窗边，"
             "盯着拉花师傅做小熊图案，有一天它终于忍不住伸爪子偷喝了一口")  # 60+ 字
    half = "从前有只小猫爱喝抹茶，有一天它偷喝了一口"  # ~原文一半
    assert sanitize_llm_output(half, story) is None
    # 正常口语化（轻度压缩 ~0.8）仍应放行
    ok = ("从前有只小猫超爱抹茶拿铁，天天下午蹲咖啡馆窗边看拉花师傅画小熊，"
          "有天忍不住伸爪子偷喝了一口")
    assert sanitize_llm_output(ok, story) == ok


def test_sanitize_rejects_non_chinese_output():
    # 原文 12 字、输出 17 字（长度守卫放行）→ 只可能被语言守卫拒，精确测语言分支
    assert sanitize_llm_output("i feel pretty good today",
                               "我今天状态还不错真的挺好") is None


def test_sanitize_empty():
    assert sanitize_llm_output("", "我今天状态还不错") is None
    assert sanitize_llm_output(None, "我今天状态还不错") is None


# ── async：llm_colloquialize ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_llm_success_and_cache():
    reset_state()
    fake = _FakeAI("其实我今天过得还不错啦")
    src = "我今天其实过得挺不错的但是有点累"
    out1 = await llm_colloquialize(src, ai_client=fake, emotion="warm")
    assert out1 == "其实我今天过得还不错啦"
    assert fake.calls == 1
    out2 = await llm_colloquialize(src, ai_client=fake, emotion="warm")
    assert out2 == out1
    assert fake.calls == 1          # 缓存命中：没再调 LLM
    reset_state()


class _DualAI:
    """假 AIClient：分别记录 rewrite_local / rewrite_cloud 的调用与返回。"""

    def __init__(self, local=None, cloud=None):
        self.local_calls = 0
        self.cloud_calls = 0
        self._local = local
        self._cloud = cloud

    async def rewrite_local(self, system, user, *, timeout_sec=8.0, **kw):
        self.local_calls += 1
        return self._local

    async def rewrite_cloud(self, system, user, *, timeout_sec=12.0, **kw):
        self.cloud_calls += 1
        return self._cloud

    async def embed(self, texts):
        return await _identity_embed(texts)


_SRC = "我今天其实过得挺不错的但是有点累"


@pytest.mark.asyncio
async def test_provider_cloud_uses_cloud_only():
    """provider=cloud：只打云端，本地节点缺模型时也不再白等一次本地往返。"""
    reset_state()
    fake = _DualAI(local="本地不该被调用的改写内容", cloud="其实我今天过得还不错啦")
    out = await llm_colloquialize(_SRC, ai_client=fake, provider="cloud")
    assert out == "其实我今天过得还不错啦"
    assert (fake.cloud_calls, fake.local_calls) == (1, 0)
    reset_state()


@pytest.mark.asyncio
async def test_provider_auto_falls_back_to_local():
    """provider=auto：云端失败（超时/未配）→ 回落本地，不直接掉规则档。"""
    reset_state()
    fake = _DualAI(local="其实我今天过得还不错啦", cloud=None)
    out = await llm_colloquialize(_SRC, ai_client=fake, provider="auto")
    assert out == "其实我今天过得还不错啦"
    assert (fake.cloud_calls, fake.local_calls) == (1, 1)
    reset_state()


@pytest.mark.asyncio
async def test_provider_default_is_local():
    """缺省仍是 local（向后兼容：未配 provider 的部署行为不变）。"""
    reset_state()
    fake = _DualAI(local="其实我今天过得还不错啦", cloud="云端不该被调用")
    out = await llm_colloquialize(_SRC, ai_client=fake)
    assert out == "其实我今天过得还不错啦"
    assert (fake.cloud_calls, fake.local_calls) == (0, 1)
    reset_state()


@pytest.mark.asyncio
async def test_provider_cloud_missing_method_degrades():
    """老客户端没有 rewrite_cloud → 返回 None 回落规则档，绝不抛。"""
    reset_state()
    fake = _FakeAI("本地改写内容不该被用到")
    assert await llm_colloquialize(_SRC, ai_client=fake, provider="cloud") is None
    assert fake.calls == 0
    reset_state()


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_none():
    reset_state()
    fake = _FakeAI(None)
    out = await llm_colloquialize("我今天其实过得挺不错的但是有点累", ai_client=fake)
    assert out is None
    reset_state()


@pytest.mark.asyncio
async def test_llm_bad_output_rejected():
    reset_state()
    fake = _FakeAI("this is a totally english rewrite that is wrong")
    out = await llm_colloquialize("我今天其实过得挺不错的但是有点累", ai_client=fake)
    assert out is None              # 串语言 → 校验不过 → 回落
    reset_state()


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_failures():
    reset_state()
    fake = _FakeAI(None)            # 一直失败
    for i in range(3):
        await llm_colloquialize("这是一句足够长的测试句子内容" + str(i) * 2,
                                ai_client=fake)
    assert fake.calls == 3
    # 冷却期：新文本也直接回落，不再调 LLM
    out = await llm_colloquialize("完全不同的另一句够长的内容啊啊啊", ai_client=fake)
    assert out is None
    assert fake.calls == 3          # 熔断生效，没调 LLM
    reset_state()


@pytest.mark.asyncio
async def test_short_and_non_chinese_noop():
    reset_state()
    fake = _FakeAI("不该被调用")
    assert await llm_colloquialize("好的呀", ai_client=fake) is None      # 短句
    # 英文已是口语化目标语（_is_english_dominant），不再算 no-op。
    # 日文等非中非英仍不调 LLM。
    assert await llm_colloquialize(
        "これはかなり長い日本語の文章ですよ今ここで話しています",
        ai_client=fake) is None
    assert fake.calls == 0          # 短句/非中非英根本不调 LLM
    reset_state()


@pytest.mark.asyncio
async def test_lead_false_reflected_in_prompt(monkeypatch):
    """colloquial_lead=False 通过 prompt 传给 LLM（缓存键也区分 lead）。"""
    reset_state()
    seen = {}

    class _Spy:
        calls = 0

        async def rewrite_local(self, system, user, *, timeout_sec=8.0, **kw):
            _Spy.calls += 1
            seen["system"] = system
            return "其实这句被改写得挺自然的呢"

        async def embed(self, texts):
            return await _identity_embed(texts)

    src = "我今天其实过得挺不错的但是有点累啊"
    await llm_colloquialize(src, ai_client=_Spy(), emotion="warm", lead=False)
    assert "不要用语气词或开场白开头" in seen["system"]
    reset_state()


# ── 端点列表 / local_first / 温度 / 落盘缓存（2026-08-01）────────────────────
from src.ai.voice_colloquial_llm import (  # noqa: E402
    get_last_provider,
    health_signal,
    parse_llm_endpoints,
)

_EPS = [
    {"base_url": "http://192.168.0.198:11434", "model": "qwen3:8b"},
    {"base_url": "http://192.168.0.140:11434", "model": "qwen3.5:9b"},
]


def test_parse_llm_endpoints_validation():
    eps = parse_llm_endpoints([
        {"base_url": "http://192.168.0.198:11434/", "model": "qwen3:8b",
         "timeout_sec": 4, "num_ctx": 4096},
        {"base_url": "", "model": "x"},              # 缺 base → 剔
        {"base_url": "not-a-url", "model": "x"},      # 无 scheme → 剔
        {"base_url": "http://h:1", "model": ""},      # 缺 model → 剔
        "garbage",                                     # 非 dict → 剔
    ])
    assert len(eps) == 1
    assert eps[0]["base_url"] == "http://192.168.0.198:11434"   # 尾斜杠已剥
    assert eps[0]["timeout_sec"] == 4.0 and eps[0]["num_ctx"] == 4096
    assert parse_llm_endpoints(None) == ()
    assert parse_llm_endpoints("not-a-list") == ()


def test_prompt_contains_antianswer_rule_and_fewshot():
    """「转述而非回答」硬规则 + few-shot 必须在两档 prompt 里都在场——
    这是对生产实锤失败（改写变回答/人设改名「小六」）的源头矫正。"""
    for intensity in ("natural", "vivid"):
        p = build_colloquial_prompt("warm", lead=True, intensity=intensity)
        assert "绝不能回答" in p, intensity
        assert "同一个提问" in p, intensity
        assert "原样保留" in p, intensity
        assert "我叫林佳欣" in p, intensity      # few-shot 示例在场
        assert "口语版：" in p, intensity
    # 老断言口径不回归：保真硬要求仍在
    assert "保持原意" in build_colloquial_prompt("warm", lead=True)


@pytest.mark.asyncio
async def test_endpoints_used_before_fallback_client(monkeypatch):
    """配了 llm_endpoints：local 档走端点直连，不再碰 client.rewrite_local。"""
    reset_state()
    calls = []

    async def _fake_ep(ep, system, user, *, temperature, max_tokens=240):
        calls.append(ep["base_url"])
        return "其实我今天过得还不错啦"

    monkeypatch.setattr(
        "src.ai.voice_colloquial_llm._rewrite_via_endpoint", _fake_ep)
    fake = _DualAI(local="不该被调用", cloud="也不该被调用")
    out = await llm_colloquialize(
        _SRC, ai_client=fake, llm_endpoints=_EPS)
    assert out == "其实我今天过得还不错啦"
    assert calls == ["http://192.168.0.198:11434"]      # 首端点命中即止
    assert (fake.local_calls, fake.cloud_calls) == (0, 0)
    assert get_last_provider() == "http://192.168.0.198:11434|qwen3:8b"
    reset_state()


@pytest.mark.asyncio
async def test_endpoint_failover_and_cooldown(monkeypatch):
    """首端点异常 → 冷却 + 自动走第二端点；下一条直接跳过冷却中的首端点。"""
    reset_state()
    calls = []

    async def _fake_ep(ep, system, user, *, temperature, max_tokens=240):
        calls.append(ep["base_url"])
        if "198" in ep["base_url"]:
            raise RuntimeError("connect timeout")
        return "其实我今天过得还不错啦"

    monkeypatch.setattr(
        "src.ai.voice_colloquial_llm._rewrite_via_endpoint", _fake_ep)
    dummy = _DualAI(local="不该被调用", cloud="也不该被调用")
    out1 = await llm_colloquialize(_SRC, ai_client=dummy, llm_endpoints=_EPS)
    assert out1 == "其实我今天过得还不错啦"
    assert calls == ["http://192.168.0.198:11434", "http://192.168.0.140:11434"]
    sig = health_signal()
    assert "http://192.168.0.198:11434|qwen3:8b" in sig["endpoints_cooling"]
    # 第二条（新文本）：198 在冷却中 → 只打 140
    calls.clear()
    out2 = await llm_colloquialize(
        "完全不同的另一句够长的中文内容呀", ai_client=dummy, llm_endpoints=_EPS)
    assert out2 == "其实我今天过得还不错啦"
    assert calls == ["http://192.168.0.140:11434"]
    assert (dummy.local_calls, dummy.cloud_calls) == (0, 0)
    reset_state()


@pytest.mark.asyncio
async def test_local_first_falls_back_to_cloud(monkeypatch):
    """provider=local_first：全部 LAN 端点失败 → 落云端，不直接掉规则档。"""
    reset_state()

    async def _fake_ep(ep, system, user, *, temperature, max_tokens=240):
        raise RuntimeError("all endpoints down")

    monkeypatch.setattr(
        "src.ai.voice_colloquial_llm._rewrite_via_endpoint", _fake_ep)
    fake = _DualAI(local="不该被调用", cloud="其实我今天过得还不错啦")
    out = await llm_colloquialize(
        _SRC, ai_client=fake, provider="local_first", llm_endpoints=_EPS)
    assert out == "其实我今天过得还不错啦"
    assert (fake.local_calls, fake.cloud_calls) == (0, 1)
    assert get_last_provider() == "cloud"
    reset_state()


@pytest.mark.asyncio
async def test_local_first_without_endpoints_uses_fallback_then_cloud():
    """local_first 未配端点：先 ai.fallback（rewrite_local）再云端——老部署可平滑切档。"""
    reset_state()
    fake = _DualAI(local=None, cloud="其实我今天过得还不错啦")
    out = await llm_colloquialize(_SRC, ai_client=fake, provider="local_first")
    assert out == "其实我今天过得还不错啦"
    assert (fake.local_calls, fake.cloud_calls) == (1, 1)
    reset_state()


@pytest.mark.asyncio
async def test_temperature_threaded_and_in_cache_key():
    """温度透传给改写调用，且进缓存键（不同温度不串缓存）。"""
    reset_state()
    seen = {}

    class _Spy:
        def __init__(self):
            self.calls = 0

        async def rewrite_local(self, system, user, *, timeout_sec=8.0, **kw):
            self.calls += 1
            seen["temperature"] = kw.get("temperature")
            return "其实我今天过得还不错啦"

        async def embed(self, texts):
            return await _identity_embed(texts)

    spy = _Spy()
    await llm_colloquialize(_SRC, ai_client=spy, temperature=0.35)
    assert seen["temperature"] == 0.35
    # 同文本换温度 → 不该命中同一缓存键 → 再调一次 LLM
    await llm_colloquialize(_SRC, ai_client=spy, temperature=0.85)
    assert spy.calls == 2
    reset_state()


@pytest.mark.asyncio
async def test_disk_cache_survives_memory_reset():
    """落盘缓存跨「重启」（清内存层）复用；失败结果绝不落盘。"""
    reset_state()
    src = "这是一句专门用于落盘缓存测试的中文长句子"
    fake = _FakeAI("其实呀这是落盘缓存测试的口语版啦")
    out1 = await llm_colloquialize(src, ai_client=fake)
    assert out1 and fake.calls == 1
    # 模拟进程重启：清内存/熔断，但保留磁盘
    reset_state(disk=False)
    dead = _FakeAI(None)                       # LLM 全挂也无所谓
    out2 = await llm_colloquialize(src, ai_client=dead)
    assert out2 == out1
    assert dead.calls == 0                     # 全程零 LLM 调用
    assert health_signal()["disk_cache_hits"] >= 1
    # 失败不落盘：失败文本清内存后仍会重试（不被磁盘固化）
    fail_src = "另一句注定改写失败的落盘语义测试句子"
    await llm_colloquialize(fail_src, ai_client=dead)
    reset_state(disk=False)
    probe = _FakeAI("其实这句后来又能改出来了呢")
    out3 = await llm_colloquialize(fail_src, ai_client=probe)
    assert out3 == "其实这句后来又能改出来了呢"
    assert probe.calls == 1
    reset_state()


# ── 问句主导跳过 + fail-closed（2026-08-10 实录事故矫正）─────────────────────
def test_is_interrogative_dominant_cases():
    # 实录句：被小模型改写成「对它的回答」后念出 → 必须被判问句主导
    assert is_interrogative_dominant("你晚上吃饭了吗，晚上有什么安排")
    assert is_interrogative_dominant("你今天过得怎么样？")
    assert is_interrogative_dominant("吃了吗？睡得好吗？")
    assert is_interrogative_dominant("诶，你晚上吃饭了吗")   # 语气短句不投票
    # 混合文本（陈述+问句）不判 dominant——改写价值在陈述段，语义守卫兜底；
    # 第一条即 prompt few-shot 的规范改写对象（陈述+结尾问句），绝不可误跳
    assert not is_interrogative_dominant(
        "我刚才在健身房锻炼完之后顺便去了趟超市买了点水果和牛奶，你今天过得怎么样？")
    assert not is_interrogative_dominant("我明白，你很健忘啊")
    assert not is_interrogative_dominant("我刚到家，正准备做饭呢。")
    assert not is_interrogative_dominant("")


@pytest.mark.asyncio
async def test_interrogative_skips_llm_tier():
    """问句主导 → 根本不调 LLM（答话式翻车的源头规避），回落规则档。"""
    reset_state()
    fake = _FakeAI("不该被调用")
    assert await llm_colloquialize(
        "你晚上吃饭了吗，晚上有什么安排", ai_client=fake) is None
    assert fake.calls == 0
    reset_state()


@pytest.mark.asyncio
async def test_cantonese_text_skips_llm_tier():
    """粤文语音稿 → 不调 LLM（2026-08-30 粤语人设）：粤文本身就是口语书写形，
    改写 prompt/消毒器/语义地板都分不出粤/普——同义改写会把粤文磨回普通话书写，
    再由 zh-HK 音色念出=失地道感。skip 即保真；普通话长陈述句不受影响。"""
    reset_state()
    fake = _FakeAI("不该被调用")
    assert await llm_colloquialize(
        "我哋今日去咗旺角饮茶，啲点心真係几好食，你得闲都嚟试下啦",
        ai_client=fake) is None
    assert fake.calls == 0
    reset_state()


@pytest.mark.asyncio
async def test_unverifiable_rewrite_rejected_without_cache_poison():
    """无 embed（语义校验不成立）→ 拒用 LLM 稿（fail-closed）；且**不投毒
    缓存、不推熔断**——嵌入恢复后同句立刻可用，端点健康信号不被冤枉。"""
    reset_state()

    class _NoEmbed:
        def __init__(self):
            self.calls = 0

        async def rewrite_local(self, system, user, *, timeout_sec=8.0, **kw):
            self.calls += 1
            return "其实我今天过得还不错啦"

    ne = _NoEmbed()
    assert await llm_colloquialize(_SRC, ai_client=ne) is None
    assert ne.calls == 1
    assert health_signal()["fail_streak"] == 0          # 不喂熔断
    ok = _FakeAI("其实我今天过得还不错啦")
    assert await llm_colloquialize(_SRC, ai_client=ok) == "其实我今天过得还不错啦"
    assert ok.calls == 1                                # 缓存未被空串投毒
    reset_state()


# ── #92（0830 晚餐自述矛盾）：餐食自述守卫 ───────────────────────────────────

def test_food_terms_extraction_92():
    from src.ai.voice_colloquial_llm import food_terms
    got = food_terms("刚吃完晚饭，晚上就随便炒个饭对付")
    assert "晚饭" in got
    assert food_terms("今天好累呀") == set()
    # 复合后缀：X饭/X面（贪婪前缀无妨，守卫按子串豁免判新增）
    assert any("鸡蛋饭" in t for t in food_terms("你泡面配点青菜鸡蛋饭没"))


def test_introduced_food_rejected_92():
    """事故金标：改写引入原文没有的菜名饭名 → sanitize 拒（回落原文照念）。"""
    from src.ai.voice_colloquial_llm import sanitize_llm_output
    original = "我刚吃完晚饭，你吃了吗？"
    bad = "刚吃完晚饭，你泡面配点青菜鸡蛋饭没？我这人懒，晚上就随便炒个饭对付"
    assert sanitize_llm_output(bad, original) is None


def test_food_preserving_rewrite_passes_92():
    """合法口语化（不加新餐食词）照常通过。"""
    from src.ai.voice_colloquial_llm import sanitize_llm_output
    original = "我刚吃完晚饭，感觉非常满足，你吃了吗？"
    good = "我刚吃完晚饭呀，特别满足，你吃了没？"
    assert sanitize_llm_output(good, original) == good


def test_food_decomposition_exempt_92():
    """原文复合词被拆说（青菜鸡蛋面→青菜、鸡蛋）不算新增。"""
    from src.ai.voice_colloquial_llm import introduced_food_terms
    assert introduced_food_terms(
        "我晚上做了青菜鸡蛋面", "晚上给自己整了碗面，放了青菜和鸡蛋") == []


def test_introduced_food_detected_92():
    from src.ai.voice_colloquial_llm import introduced_food_terms
    got = introduced_food_terms("我刚吃完晚饭", "刚吃完晚饭，配了个蛋炒饭")
    assert "蛋炒饭" in got


def test_meal_redline_in_prompts_92():
    """三个改写提示词都带自述行为/饮食红线 + 版本号已 bump（旧缓存作废）。"""
    from src.ai.voice_colloquial_llm import (
        _PROMPT_VERSION, _SCRIPT_PROMPT_VERSION, build_colloquial_prompt,
        build_speech_script_prompt)
    assert _PROMPT_VERSION >= 4
    assert _SCRIPT_PROMPT_VERSION >= 5
    for p in (build_colloquial_prompt("neutral", True, "", intensity="natural"),
              build_colloquial_prompt("neutral", True, "", intensity="vivid"),
              build_speech_script_prompt("neutral")):
        assert "菜名饭名" in p, p[:120]
