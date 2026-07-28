"""出图自检闸门（``src/ai/image_gate.py``）门禁：解析/判定纯函数 + 带重试生成接线。

背景：2026-07-13「狗图事故」——prompt 修复只能降概率，本闸门是发出前的最后防线。
"""
from __future__ import annotations

import pytest

import src.ai.image_gate as ig
from src.ai.companion_selfie import SelfieResult


@pytest.fixture(autouse=True)
def _reset_metrics():
    ig.reset_metrics()
    yield
    ig.reset_metrics()


# ── parse_gate_response ─────────────────────────────────────────────────

def test_parse_clean_and_fenced_json():
    assert ig.parse_gate_response('{"people_count": 1}') == {"people_count": 1}
    fenced = "Sure! ```json\n{\"people_count\": 2, \"nsfw\": false}\n```"
    assert ig.parse_gate_response(fenced)["people_count"] == 2


def test_parse_garbage_returns_none():
    assert ig.parse_gate_response("") is None
    assert ig.parse_gate_response("a lovely dog photo") is None
    assert ig.parse_gate_response("{broken json") is None


# ── gate_verdict ────────────────────────────────────────────────────────

def _p(**kw):
    base = {"people_count": 1, "gender": "female", "apparent_age": 22,
            "is_animal_subject": False, "visible_text_or_watermark": False,
            "nsfw": False}
    base.update(kw)
    return base


def test_verdict_pass_normal_selfie():
    ok, reason = ig.gate_verdict(_p(), expect_gender="female", expect_age=22)
    assert ok and reason == "ok"


def test_verdict_rejects_dog_photo():
    """狗图事故回归：主体是动物 → 必拒。"""
    ok, reason = ig.gate_verdict(
        _p(people_count=0, is_animal_subject=True), expect_gender="female")
    assert not ok and reason == "animal_subject"


def test_verdict_rejects_no_person_and_crowd():
    assert ig.gate_verdict(_p(people_count=0))[1] == "no_person"
    assert ig.gate_verdict(_p(people_count=3))[1] == "multiple_people"


def test_verdict_gender_and_age():
    ok, reason = ig.gate_verdict(_p(gender="male"), expect_gender="female")
    assert not ok and reason.startswith("gender_mismatch")
    # VLM 拿不准性别（unknown）→ 不误伤
    assert ig.gate_verdict(_p(gender="unknown"), expect_gender="female")[0]
    # 22 岁人设生成 70 岁 → 拒；无期望年龄不校验
    ok2, r2 = ig.gate_verdict(_p(apparent_age=70), expect_age=22)
    assert not ok2 and r2.startswith("age_mismatch")
    assert ig.gate_verdict(_p(apparent_age=70), expect_age=0)[0]


def test_verdict_watermark_nsfw_and_softpass():
    # 水印默认只记录不拦（真机实测 VLM 会把场景店招误判成水印）；strict 才拦
    ok_wm, r_wm = ig.gate_verdict(_p(visible_text_or_watermark=True))
    assert ok_wm and r_wm == "watermark_ignored"
    ok_strict, r_strict = ig.gate_verdict(
        _p(visible_text_or_watermark=True), strict_watermark=True)
    assert not ok_strict and r_strict == "text_watermark"
    assert ig.gate_verdict(_p(nsfw=True))[1] == "nsfw"
    ok, reason = ig.gate_verdict(None)  # 解析失败 → 软放行
    assert ok and reason == "parse_fail"


# ── 尺度分级（2026-07-14 suggestive 档）──────────────────────────────────

def test_verdict_sfw_rating_blocks_all_nsfw():
    # 默认 sfw：任何 nsfw（性感/露肤/泳装）即拒——历史行为不变
    ok, r = ig.gate_verdict(_p(nsfw=True), content_rating="sfw")
    assert not ok and r == "nsfw"
    # 未知档位保守按 sfw 处理
    assert ig.gate_verdict(_p(nsfw=True), content_rating="weird")[1] == "nsfw"


def test_verdict_suggestive_passes_sensual_but_blocks_explicit():
    # suggestive：性感(nsfw=True 但非露骨)放行
    ok, r = ig.gate_verdict(
        _p(nsfw=True, explicit=False), expect_gender="female", expect_age=22,
        content_rating="suggestive")
    assert ok and r == "ok"
    # 露骨(explicit)仍硬拒
    ok2, r2 = ig.gate_verdict(
        _p(nsfw=True, explicit=True), content_rating="suggestive")
    assert not ok2 and r2 == "explicit"


def test_verdict_underage_is_hard_blocked_any_rating():
    # 未成年 + 性化信号 → 必拒（红线，最优先，无视档位）
    ok, r = ig.gate_verdict(
        _p(looks_underage=True, nsfw=True), content_rating="suggestive")
    assert not ok and r == "underage"
    # 未成年 + suggestive 档（即便 VLM 没标 nsfw）也拒
    ok2, r2 = ig.gate_verdict(
        _p(looks_underage=True, nsfw=False, explicit=False),
        content_rating="suggestive")
    assert not ok2 and r2 == "underage"
    # sfw 档下未成年但无性化信号 → 不因未成年拦（正常合规照，年龄由 age_mismatch 管）
    assert ig.gate_verdict(
        _p(looks_underage=True, nsfw=False), content_rating="sfw")[1] != "underage"


def test_resolve_gate_cfg_inherits_content_rating():
    # 从父级 selfie.content_rating 继承
    cfg = ig.resolve_gate_cfg({"content_rating": "suggestive"})
    assert cfg["content_rating"] == "suggestive"
    # vision_gate 段可显式覆盖
    cfg2 = ig.resolve_gate_cfg(
        {"content_rating": "suggestive", "vision_gate": {"content_rating": "sfw"}})
    assert cfg2["content_rating"] == "sfw"
    # 缺省 sfw
    assert ig.resolve_gate_cfg({})["content_rating"] == "sfw"


def test_gate_prompt_carries_explicit_and_underage_fields():
    p = ig.build_gate_prompt()
    assert "explicit" in p and "looks_underage" in p


def test_persona_expectations():
    g, age = ig.persona_expectations({"gender": "female", "age": 22})
    assert g == "female" and age == 22
    g2, age2 = ig.persona_expectations({"tags": ["男性"], "age": "58"})
    assert g2 == "male" and age2 == 58
    assert ig.persona_expectations("林小雨") == ("", 0)


def test_resolve_gate_cfg_defaults():
    cfg = ig.resolve_gate_cfg({})
    assert cfg["enabled"] is True and cfg["retries"] == 1
    assert ig.resolve_gate_cfg({"vision_gate": {"enabled": False}})["enabled"] is False


# ── generate_with_gate（接线：假 provider + 假体检）──────────────────────

class _FakeProvider:
    backend = "command"

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def generate(self, prompt, *, seed=-1, **kw):
        self.calls.append(seed)
        return self.results.pop(0)


def _ok_result(path="a.png"):
    return SelfieResult(ok=True, image_path=path, provider="command")


async def test_gate_pass_first_try(monkeypatch):
    prov = _FakeProvider([_ok_result()])

    async def fake_check(path, persona, cfg, **kw):
        return True, "ok"

    monkeypatch.setattr(ig, "check_image", fake_check)
    res = await ig.generate_with_gate(
        prov, "p", persona={}, root_config={}, gate_cfg={"enabled": True}, seed=100)
    assert res.ok and prov.calls == [100]


async def test_gate_retry_with_new_seed_then_pass(monkeypatch):
    prov = _FakeProvider([_ok_result("bad.png"), _ok_result("good.png")])
    verdicts = iter([(False, "animal_subject"), (True, "ok")])

    async def fake_check(path, persona, cfg, **kw):
        return next(verdicts)

    monkeypatch.setattr(ig, "check_image", fake_check)
    res = await ig.generate_with_gate(
        prov, "p", persona={}, root_config={},
        gate_cfg={"enabled": True, "retries": 1}, seed=100)
    assert res.ok and res.image_path == "good.png"
    assert prov.calls == [100, (100 + 7919) % (2 ** 31)]  # 换种子重试（确定性）


async def test_gate_all_reject_returns_failure(monkeypatch):
    prov = _FakeProvider([_ok_result(), _ok_result()])

    async def fake_check(path, persona, cfg, **kw):
        return False, "gender_mismatch(male)"

    monkeypatch.setattr(ig, "check_image", fake_check)
    res = await ig.generate_with_gate(
        prov, "p", persona={}, root_config={},
        gate_cfg={"enabled": True, "retries": 1}, seed=5)
    assert not res.ok and res.error.startswith("vision_gate:")
    assert len(prov.calls) == 2


async def test_gate_threads_content_rating_to_check(monkeypatch):
    prov = _FakeProvider([_ok_result()])
    seen = {}

    async def fake_check(path, persona, cfg, **kw):
        seen.update(kw)
        return True, "ok"

    monkeypatch.setattr(ig, "check_image", fake_check)
    res = await ig.generate_with_gate(
        prov, "p", persona={}, root_config={},
        gate_cfg={"enabled": True, "content_rating": "suggestive"}, seed=1)
    assert res.ok
    assert seen.get("content_rating") == "suggestive"


async def test_gate_disabled_is_passthrough(monkeypatch):
    prov = _FakeProvider([_ok_result()])

    async def boom(*a, **k):  # 闸门关不该体检
        raise AssertionError("check_image should not be called")

    monkeypatch.setattr(ig, "check_image", boom)
    res = await ig.generate_with_gate(
        prov, "p", gate_cfg={"enabled": False}, seed=1)
    assert res.ok and prov.calls == [1]


async def test_gate_provider_failure_passthrough(monkeypatch):
    prov = _FakeProvider([SelfieResult(ok=False, error="boom")])

    async def boom(*a, **k):
        raise AssertionError("no check on failed generation")

    monkeypatch.setattr(ig, "check_image", boom)
    res = await ig.generate_with_gate(prov, "p", gate_cfg={"enabled": True})
    assert not res.ok and res.error == "boom"


async def test_check_image_soft_pass_without_vision_cfg():
    ok, reason = await ig.check_image("x.png", {}, {})
    assert ok and reason == "no_vision_cfg"
    assert ig.metrics_snapshot()["soft_pass"] == 1


# ── 物体图闸门（Phase17：要蛋糕别发面条）────────────────────────────────

def test_object_gate_prompt_carries_subject():
    p = ig.build_object_gate_prompt("a bowl of noodles")
    assert "a bowl of noodles" in p and "subject_match" in p


def test_object_verdict_subject_match():
    ok, r = ig.object_gate_verdict(
        {"main_subject": "ramen", "subject_match": True, "nsfw": False},
        subject="a bowl of noodles")
    assert ok and r == "ok"
    ok2, r2 = ig.object_gate_verdict(
        {"main_subject": "a cat", "subject_match": False, "nsfw": False},
        subject="a bowl of noodles")
    assert not ok2 and r2.startswith("subject_mismatch")
    # 无期望主体 → 不做匹配（只查 NSFW）
    assert ig.object_gate_verdict(
        {"main_subject": "a cat", "subject_match": False}, subject="")[0]
    assert ig.object_gate_verdict({"nsfw": True}, subject="")[1] == "nsfw"
    # 水印与人像口径一致：只记录不拦；解析失败软放行
    assert ig.object_gate_verdict(
        {"subject_match": True, "visible_text_or_watermark": True},
        subject="cake")[1] == "watermark_ignored"
    assert ig.object_gate_verdict(None)[1] == "parse_fail"


async def test_generate_with_gate_object_kind(monkeypatch):
    prov = _FakeProvider([_ok_result("obj1.png"), _ok_result("obj2.png")])
    seen = {}
    verdicts = iter([(False, "subject_mismatch(a cat)"), (True, "ok")])

    async def fake_check(path, persona, cfg, **kw):
        seen.update(kw)
        return next(verdicts)

    monkeypatch.setattr(ig, "check_image", fake_check)
    res = await ig.generate_with_gate(
        prov, "a photo of noodles", root_config={},
        gate_cfg={"enabled": True, "retries": 1}, seed=-1,
        kind="object", subject="a bowl of noodles")
    assert res.ok and res.image_path == "obj2.png"
    assert seen.get("kind") == "object"
    assert seen.get("subject") == "a bowl of noodles"


# ── P2 一致性后验（场景/时段——同一次体检顺带，零额外 VLM 往返）──────────

def test_gate_prompt_scene_tod_fields_opt_in():
    """不开一致性字段时 prompt 与旧版语义一致（零漂移）；开了才带新字段。"""
    base = ig.build_gate_prompt()
    assert '"scene"' not in base and '"time_of_day"' not in base
    p = ig.build_gate_prompt(scene_check=True, tod_check=True)
    assert '"scene"' in p and '"time_of_day"' in p
    assert "beach" in p and "gym" in p    # 分类词表进指令
    p2 = ig.build_gate_prompt(scene_check=True)
    assert '"scene"' in p2 and '"time_of_day"' not in p2


def test_scenes_equivalent_groups():
    assert ig.scenes_equivalent("home", "bedroom")     # 室内家居互认
    assert ig.scenes_equivalent("cafe", "restaurant")  # 餐饮互认
    assert ig.scenes_equivalent("street", "night_city")
    assert not ig.scenes_equivalent("beach", "gym")
    assert ig.scenes_equivalent("", "beach")           # 空=不比较


def test_expected_tod_buckets():
    assert ig.expected_tod(10) == "day"
    assert ig.expected_tod(23) == "night"
    assert ig.expected_tod(3) == "night"
    assert ig.expected_tod(18) == ""      # 黄昏光线两可 → 不校验
    assert ig.expected_tod(6) == ""       # 清晨两可
    assert ig.expected_tod(None) == ""
    assert ig.expected_tod("x") == ""


def test_verdict_scene_mismatch_and_equivalence():
    # 点名海边生成出健身房 → 拒（VLM 明确判了词表内另一类）
    ok, r = ig.gate_verdict(_p(scene="gym"), expect_scene_class="beach")
    assert not ok and r == "scene_mismatch(gym)"
    # 等价组互认（VLM 把卧室说成 home 不算错）
    assert ig.gate_verdict(_p(scene="home"), expect_scene_class="bedroom")[0]
    # VLM 没把握（other/词表外/没答）→ 放行不误伤
    assert ig.gate_verdict(_p(scene="other"), expect_scene_class="beach")[0]
    assert ig.gate_verdict(_p(scene="spaceship"), expect_scene_class="beach")[0]
    assert ig.gate_verdict(_p(), expect_scene_class="beach")[0]
    # 无期望场景 → 不校验
    assert ig.gate_verdict(_p(scene="gym"), expect_scene_class="")[0]


def test_verdict_tod_mismatch():
    # 深夜要图生成出白天照 → 拒（凌晨两点正午烈日照的最后防线）
    ok, r = ig.gate_verdict(_p(time_of_day="day"), expect_tod="night")
    assert not ok and r == "tod_mismatch(day)"
    # unclear（室内光线两可）/一致/无期望 → 放行
    assert ig.gate_verdict(_p(time_of_day="unclear"), expect_tod="night")[0]
    assert ig.gate_verdict(_p(time_of_day="night"), expect_tod="night")[0]
    assert ig.gate_verdict(_p(time_of_day="day"), expect_tod="")[0]


def test_resolve_gate_cfg_scene_tod_defaults():
    cfg = ig.resolve_gate_cfg({})
    assert cfg["scene_check"] is True and cfg["tod_check"] is True
    off = ig.resolve_gate_cfg(
        {"vision_gate": {"scene_check": False, "tod_check": False}})
    assert off["scene_check"] is False and off["tod_check"] is False


async def test_gate_threads_scene_expectations(monkeypatch):
    """generate_with_gate：场景短语→场景类、小时→期望时段，透传给体检。"""
    prov = _FakeProvider([_ok_result()])
    seen = {}

    async def fake_check(path, persona, cfg, **kw):
        seen.update(kw)
        return True, "ok"

    monkeypatch.setattr(ig, "check_image", fake_check)
    res = await ig.generate_with_gate(
        prov, "p", persona={}, root_config={},
        gate_cfg={"enabled": True}, seed=1,
        expect_scene="walking on the beach at sunset", expect_hour=23)
    assert res.ok
    assert seen.get("expect_scene_class") == "beach"
    assert seen.get("expect_tod") == "night"


async def test_gate_scene_tod_checks_can_be_disabled(monkeypatch):
    prov = _FakeProvider([_ok_result()])
    seen = {}

    async def fake_check(path, persona, cfg, **kw):
        seen.update(kw)
        return True, "ok"

    monkeypatch.setattr(ig, "check_image", fake_check)
    await ig.generate_with_gate(
        prov, "p", persona={}, root_config={},
        gate_cfg={"enabled": True, "scene_check": False, "tod_check": False},
        seed=1, expect_scene="beach", expect_hour=23)
    assert seen.get("expect_scene_class") == ""
    assert seen.get("expect_tod") == ""


async def test_check_image_scene_postcheck_e2e(monkeypatch):
    """端到端：期望海边、假 VLM 判 gym → 拒；prompt 确实带了 scene 字段。"""
    captured = {}

    class _FakeVC:
        def __init__(self, cfg):
            pass

        def initialize(self):
            return True

        async def describe_image(self, path, prompt=""):
            captured["prompt"] = prompt
            return ('{"people_count":1,"gender":"female","apparent_age":22,'
                    '"is_animal_subject":false,"visible_text_or_watermark":false,'
                    '"nsfw":false,"explicit":false,"looks_underage":false,'
                    '"scene":"gym","time_of_day":"day"}')

    import src.vision_client as vcm
    monkeypatch.setattr(vcm, "VisionClient", _FakeVC)
    ok, reason = await ig.check_image(
        "x.png", {"gender": "female", "age": 22}, {"vision": {"provider": "x"}},
        expect_scene_class="beach", expect_tod="night")
    assert '"scene"' in captured["prompt"]
    assert not ok and reason == "scene_mismatch(gym)"
