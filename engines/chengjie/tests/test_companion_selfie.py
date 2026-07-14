"""Stage A：陪伴形象照引擎（意图/提示词/准入决策/provider 骨架）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.ai.companion_selfie import (
    SELFIE_FEATURE,
    SelfieProvider,
    build_selfie_prompt,
    decide_selfie,
    detect_selfie_request,
    get_selfie_provider,
    reset_selfie_provider,
    resolve_persona_lora,
    resolve_variety_salt,
    selfie_variety,
    stable_selfie_seed,
)


# ── 意图识别 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("t", [
    "给我看看你长什么样",
    "发张自拍呗",
    "想看你的照片",
    "来张照片吧",
    "what do you look like?",
    "send me a pic of you",
    "show me your face",
])
def test_detect_positive(t):
    assert detect_selfie_request(t) is True


@pytest.mark.parametrize("t", [
    "",
    "今天天气真好",
    "我给你看我的照片",   # 用户说自己的照片，不是要 AI 的
    "我们聊聊吧",
    "x" * 250,            # 超长叙述不命中
])
def test_detect_negative(t):
    assert detect_selfie_request(t) is False


@pytest.mark.parametrize("t", [
    "那你發個照片給我看看啊",     # 繁体「发个照片」（简体 marker 匹配不到）
    "想看看妳長什麼樣",
    "傳張自拍來",                 # 含「自拍」
    "拍個照片給我看",
])
def test_detect_positive_traditional(t):
    assert detect_selfie_request(t) is True


@pytest.mark.parametrize("t", [
    "你煮的肯定很好吃,可以拍個照片給我看一下嗎?",  # 要「你煮的」食物图 → 属上下文要图，非人设自拍
    "你做的蛋糕拍张照给我看看",
    "你买的裙子拍张照片",
])
def test_detect_object_photo_not_selfie(t):
    assert detect_selfie_request(t) is False


# ── 提示词构造 ──────────────────────────────────────────────────────────

def test_build_prompt_uses_persona_appearance_and_sfw():
    p = {"name": "小柔", "appearance": "long black hair, soft smile, white dress"}
    out = build_selfie_prompt(p)
    assert "long black hair" in out
    assert "safe-for-work" in out  # 强制 SFW 安全约束


def test_build_prompt_fallback_to_default_then_generic():
    # persona 无外貌 + 给 default_appearance → 用 default
    out = build_selfie_prompt({"name": "A"}, default_appearance="freckled redhead")
    assert "freckled redhead" in out
    # 完全空 → 中性兜底（不抛、有内容）
    out2 = build_selfie_prompt(None)
    assert "Portrait selfie" in out2 and "safe-for-work" in out2


def test_build_prompt_scene_and_style():
    out = build_selfie_prompt("a woman", scene_hint="by the window", style="warm tone")
    assert "by the window" in out and "warm tone" in out


# ── 尺度分级（2026-07-14 suggestive 档）──────────────────────────────────

def test_content_rating_sfw_is_default_and_unchanged():
    from src.ai.companion_selfie import normalize_content_rating
    # 默认档 = sfw：仍带原 SFW 硬约束，不带成年锚（历史 prompt 逐字不变）
    out = build_selfie_prompt("a woman")
    assert "fully clothed, tasteful, safe-for-work, no nudity" in out
    assert "adult, mature" not in out
    # 显式 sfw 与默认等价
    assert build_selfie_prompt("a woman", content_rating="sfw") == out
    # 归一化：空 + sfw=True → sfw；空 + sfw=False → none（历史无约束边界）
    assert normalize_content_rating("", sfw=True) == "sfw"
    assert normalize_content_rating("", sfw=False) == "none"


def test_content_rating_suggestive_adds_sensual_and_adult_anchor():
    out = build_selfie_prompt("a woman", content_rating="suggestive")
    assert "alluring" in out and "not pornographic" in out
    assert "no full nudity" in out          # 硬保留不露点措辞
    assert "adult, mature" in out           # 成年锚（未成年双保险）
    assert "safe-for-work" not in out       # 不再是全遮盖档
    assert "Portrait selfie" in out         # 主体锚不变


def test_content_rating_explicit_is_downgraded_to_suggestive():
    from src.ai.companion_selfie import normalize_content_rating
    # 露骨诉求一律降级到不露骨（硬护栏，产品不给 explicit 口子）
    assert normalize_content_rating("explicit") == "suggestive"
    assert normalize_content_rating("nsfw") == "suggestive"
    out = build_selfie_prompt("a woman", content_rating="explicit")
    assert "not pornographic" in out and "no full nudity" in out
    # 未知值 → 保守回落 sfw
    assert normalize_content_rating("weird") == "sfw"


# ── 2026-07-13「狗图事故」回归：无 appearance 人设的兜底必须是明确的人类主体 ──────

def test_build_prompt_no_companion_word_and_no_cjk_name():
    """旧兜底 "a warm, friendly companion named 林小雨" → FLUX 画成陪伴犬。
    新兜底须：不含 companion、不把中文名塞进 prompt、按 gender/age 给出人类主体。"""
    p = {"name": "林小雨", "age": 22, "gender": "female"}
    out = build_selfie_prompt(p)
    assert "companion" not in out
    assert not any("\u4e00" <= c <= "\u9fff" for c in out)  # 无 CJK 噪声
    assert "22-year-old East Asian woman" in out
    assert "solo, one person" in out  # 反跑偏硬约束


def test_build_prompt_gender_from_tags_and_age():
    out = build_selfie_prompt({"name": "赵老师", "age": 58, "tags": ["男性"]})
    assert "58-year-old East Asian man" in out


def test_build_prompt_string_persona_name_guard():
    # skill_manager 拿不到 persona dict 时回传的**纯中文名**不得当外貌描述
    out = build_selfie_prompt("林小雨")
    assert "林小雨" not in out and "Portrait selfie" in out
    # 真正的描述串（英文/含分隔符）仍然生效
    assert "a woman" in build_selfie_prompt("a woman")


def test_stable_selfie_seed_deterministic():
    from src.ai.companion_selfie import stable_selfie_seed
    a = stable_selfie_seed("lin_xiaoyu")
    assert a == stable_selfie_seed("lin_xiaoyu")  # 确定性
    assert 0 <= a < 2 ** 31
    assert stable_selfie_seed("chen_meiling") != a  # 不同人设不同种子
    assert stable_selfie_seed("") == -1


def test_stable_selfie_seed_salt_backward_compat_and_variety():
    # salt=0（默认）与不传 salt 完全一致 → 向后兼容，旧调用/旧测试口径不变。
    assert stable_selfie_seed("lin_xiaoyu", 0) == stable_selfie_seed("lin_xiaoyu")
    # 非 0 salt → 同人设不同底噪（构图各异），且仍在合法区间、仍确定性。
    s1 = stable_selfie_seed("lin_xiaoyu", 12345)
    assert s1 != stable_selfie_seed("lin_xiaoyu")
    assert s1 == stable_selfie_seed("lin_xiaoyu", 12345)  # 同 salt 确定性
    assert 0 <= s1 < 2 ** 31
    assert stable_selfie_seed("lin_xiaoyu", 999) != s1  # 不同 salt 不同种子
    assert stable_selfie_seed("", 999) == -1            # 空键仍随机


# ── 出图多样性（治头位置/表情千篇一律）─────────────────────────────────────

def test_selfie_variety_deterministic_and_complete():
    v = selfie_variety(0)
    assert set(v) == {"framing", "head", "gaze", "expr", "realism"}
    assert v == selfie_variety(0)                 # 同 salt 确定性（缓存友好）
    # 各池独立轮换：多数 salt 下至少有一维与 salt=0 不同（不锁步）。
    assert any(selfie_variety(i) != v for i in range(1, 8))
    # 非数字 salt 安全归一（不抛）。
    assert selfie_variety("bad") == selfie_variety(0)


def test_build_selfie_prompt_variety_injects_pose_and_keeps_safety():
    # 无 variety_salt（默认）→ 旧行为：正面看镜头。
    legacy = build_selfie_prompt("a woman")
    assert "looking at the camera" in legacy
    assert "solo, one person, looking at the camera" in legacy
    # 有 variety_salt → 注入取景/姿态/表情/写实，且保留单人锚 + SFW 硬约束。
    v = selfie_variety(42)
    out = build_selfie_prompt("a woman", variety_salt=42)
    assert "solo, one person" in out              # 单人锚保留（防跑偏）
    assert v["framing"] in out and v["expr"] in out and v["realism"] in out
    assert "safe-for-work" in out                 # SFW 硬约束不丢
    assert "Portrait selfie" in out               # 前缀不变
    # 不同 salt → prompt 不同（多样性生效）。
    assert build_selfie_prompt("a woman", variety_salt=1) != \
        build_selfie_prompt("a woman", variety_salt=2)


def test_resolve_variety_salt_gate():
    assert resolve_variety_salt({}) is None                         # 无 variety 块=关
    assert resolve_variety_salt({"variety": {"enabled": False}}) is None
    assert resolve_variety_salt(None) is None                       # 软失败
    salt = resolve_variety_salt({"variety": {"enabled": True}})
    assert isinstance(salt, int) and 0 <= salt <= 2 ** 30           # 开=随机 int


# ── 角色 LoRA 部署（per-persona spec + trigger 前置）──────────────────────

def test_resolve_persona_lora_priority():
    # persona dict 三字段优先（registry={} 保证 hermetic，不读真实注册表文件）
    s = resolve_persona_lora(
        {"lora_file": "a.safetensors", "lora_trigger": "linxy", "lora_weight": 0.8},
        {}, registry={})
    assert s == {"file": "a.safetensors", "weight": 0.8, "trigger": "linxy"}
    # persona 无 → 回落全局 scfg.lora
    s2 = resolve_persona_lora(
        {"name": "x"}, {"lora": {"file": "g.safetensors", "trigger": "g", "weight": 0.7}},
        registry={})
    assert s2 == {"file": "g.safetensors", "weight": 0.7, "trigger": "g"}
    # persona 覆盖全局 file，未给的 trigger 回落全局（字段级合并）
    s3 = resolve_persona_lora(
        {"lora_file": "p.safetensors"}, {"lora": {"file": "g.safetensors", "trigger": "g"}},
        registry={})
    assert s3["file"] == "p.safetensors" and s3["trigger"] == "g" and s3["weight"] == 1.0
    # 无任何配置 → 空 file（不挂 LoRA，回落 PuLID）
    assert resolve_persona_lora("str_persona", {}, registry={})["file"] == ""
    assert resolve_persona_lora({"name": "x"}, {}, registry={})["file"] == ""
    assert resolve_persona_lora(None, None, registry={})["file"] == ""


def test_load_lora_registry(tmp_path):
    from src.ai.companion_selfie import load_lora_registry
    assert load_lora_registry(str(tmp_path / "nope.json")) == {}     # 不存在→{}
    p = tmp_path / "reg.json"
    p.write_text('{"lin": {"file": "a.safetensors", "trigger": "linxy", "weight": 0.9}}',
                 encoding="utf-8")
    assert load_lora_registry(str(p))["lin"]["file"] == "a.safetensors"
    p.write_text("not json{", encoding="utf-8")                       # 损坏→{}（缓存按 mtime 刷新）
    import time as _t
    _t.sleep(0.01)
    import os as _os
    _os.utime(str(p), None)
    assert load_lora_registry(str(p)) == {}


def test_resolve_persona_lora_registry_precedence():
    # 注册表层：在全局 scfg.lora 之上、persona 字段之下（字段级合并）
    scfg = {"lora": {"file": "global.safetensors", "trigger": "g", "weight": 0.5}}
    reg = {"lin": {"file": "reg.safetensors", "trigger": "regtrig", "weight": 0.8}}
    s = resolve_persona_lora({"id": "lin"}, scfg, registry=reg)
    assert s == {"file": "reg.safetensors", "weight": 0.8, "trigger": "regtrig"}
    # persona 字段盖过注册表 file，未给的 trigger 仍回落注册表
    s2 = resolve_persona_lora({"id": "lin", "lora_file": "p.safetensors"}, scfg, registry=reg)
    assert s2["file"] == "p.safetensors" and s2["trigger"] == "regtrig"
    # 注册表无该 pid → 回落全局
    assert resolve_persona_lora({"id": "other"}, scfg, registry=reg)["file"] == "global.safetensors"
    assert resolve_persona_lora({"id": "lin"}, scfg, registry={})["file"] == "global.safetensors"


def test_build_selfie_prompt_lora_trigger_leads():
    p = build_selfie_prompt("a woman", lora_trigger="linxy")
    assert p.startswith("linxy, Portrait selfie photo of")   # 触发词领衔（激活身份）
    assert build_selfie_prompt("a woman").startswith("Portrait selfie photo of")  # 空=旧行为
    # trigger + variety 共存，SFW 约束不丢
    pv = build_selfie_prompt("a woman", lora_trigger="linxy", variety_salt=3)
    assert pv.startswith("linxy, ") and "safe-for-work" in pv and "solo, one person" in pv


async def test_command_provider_lora_placeholders_and_noface_routing(monkeypatch, tmp_path):
    """command 后端：{lora}/{lora_weight} 占位按调用填充；无 base_image 且配了
    command_args_noface → 走 noface 命令（GPU 分流）。"""
    import subprocess

    calls: list = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        out = cmd[cmd.index("--out") + 1]
        Path(out).write_bytes(b"\x89PNGok")

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    prov = SelfieProvider({
        "enabled": True, "backend": "command", "out_dir": str(tmp_path / "o"),
        "command_args": ["python", "gen.py", "--prompt", "{prompt}", "--out", "{out}",
                         "--face-ref", "{base}", "--lora", "{lora}",
                         "--lora-weight", "{lora_weight}"],
        "command_args_noface": ["python", "gen.py", "--url", "http://noface",
                                "--prompt", "{prompt}", "--out", "{out}"],
    })
    # 有 base → 主命令 + lora 占位填充
    r = await prov.generate("p", base_image=str(tmp_path / "face.png"),
                            lora="linxy.safetensors", lora_weight=0.9)
    assert r.ok
    cmd = calls[-1]
    assert "linxy.safetensors" in cmd and "0.9" in cmd
    assert "--url" not in cmd                       # 主命令（非 noface）
    assert str(tmp_path / "face.png") in cmd        # {base} 填充
    # 无 base → noface 命令（分流）
    calls.clear()
    r2 = await prov.generate("p", base_image="", lora="x", lora_weight=1.0)
    assert r2.ok and "http://noface" in calls[-1]


# ── 场景轮换（Phase2）───────────────────────────────────────────────────

def test_pick_scene_hint_rotation_and_priority():
    import datetime
    from src.ai.companion_selfie import pick_scene_hint
    pool = ["scene-a", "scene-b", "scene-c"]
    t = datetime.datetime(2026, 7, 13, 20, 0)
    # 同一时刻确定性稳定
    s1 = pick_scene_hint({}, fallback_scenes=pool, now=t)
    assert s1 == pick_scene_hint({}, fallback_scenes=pool, now=t) and s1 in pool
    # salt（相册张数）错开场景；跨时段/跨天会轮换
    s2 = pick_scene_hint({}, fallback_scenes=pool, now=t, salt=1)
    assert s2 != s1
    # persona 自带 selfie_scenes 优先于回落池
    p = {"selfie_scenes": ["dorm room at night"]}
    assert pick_scene_hint(p, fallback_scenes=pool, now=t) == "dorm room at night"
    # 无池 → 固定 default
    assert pick_scene_hint({}, default_scene="fixed", now=t) == "fixed"


# ── 场景反选（Phase18：话题贴合选景）────────────────────────────────────

def test_scene_choice_instruction_and_parse():
    from src.ai.companion_selfie import (
        build_scene_choice_instruction, parse_scene_choice, scene_pool,
    )
    scenes = ["dorm desk with study notes", "night market", "gym"]
    ins = build_scene_choice_instruction(
        "上次你说在备考N1，问问进展", ["在准备日语能力考"], scenes)
    assert "备考N1" in ins and "1. dorm desk" in ins and "只输出一个数字" in ins
    # 解析：正常/带废话/0=无贴合/越界/垃圾
    assert parse_scene_choice("1", 3) == 1
    assert parse_scene_choice("选 2 号最贴", 3) == 2
    assert parse_scene_choice("0", 3) == 0
    assert parse_scene_choice("7", 3) == -1
    assert parse_scene_choice("嗯……", 3) == -1
    # scene_pool：persona 优先 → 回落 config → 空
    assert scene_pool({"selfie_scenes": ["a"]}, ["b"]) == ["a"]
    assert scene_pool({}, ["b"]) == ["b"]
    assert scene_pool("林小雨", None) == []


# ── 文图协同配文指令（Phase2）───────────────────────────────────────────

def test_build_photo_caption_instruction():
    from src.ai.companion_selfie import build_photo_caption_instruction
    ins = build_photo_caption_instruction(
        "给我你的近照", kind="selfie", persona_name="林小雨")
    assert "林小雨" in ins and "给我你的近照" in ins
    assert "已经发出去" in ins  # 关键：LLM 须知道图已发出
    assert "等我去拍" in ins    # 禁止拖延措辞的显式约束
    ins2 = build_photo_caption_instruction("看看你煮的面", kind="object", subject="面")
    assert "「面」" in ins2


def test_generate_command_formats_seed(monkeypatch, tmp_path):
    import subprocess as sp

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("src.ai.companion_selfie.subprocess.run", fake_run)
    prov = SelfieProvider({
        "enabled": True, "backend": "command",
        "command_args": ["gen", "--prompt", "{prompt}", "--out", "{out}",
                         "--seed", "{seed}"],
    })
    prov._generate_command("a woman", tmp_path / "o.png", "", 12345)
    assert "12345" in captured["cmd"]
    prov._generate_command("a woman", tmp_path / "o.png", "")  # 缺省 -1=随机
    assert "-1" in captured["cmd"]


# ── 准入决策 ────────────────────────────────────────────────────────────

def test_decide_too_soon_when_bond_low():
    d = decide_selfie(entitlement=None, gate_enabled=True, free_used=0,
                      free_daily=1, bond_level=1, min_bond_level=2)
    assert d["action"] == "too_soon"


def test_decide_gate_off_always_allow_unlimited():
    # gate 关 → feature_allowed 恒 True → 不限、不消耗免费额度
    d = decide_selfie(entitlement=None, gate_enabled=False, free_used=99,
                      free_daily=1, bond_level=5, min_bond_level=2)
    assert d["action"] == "allow"
    assert d["used_free"] is False


def test_decide_owns_album_allow_unlimited():
    ent = {"grants": [], "unlocked": [SELFIE_FEATURE]}
    d = decide_selfie(entitlement=ent, gate_enabled=True, free_used=99,
                      free_daily=1, bond_level=5, min_bond_level=2)
    assert d["action"] == "allow"
    assert d["used_free"] is False


def test_decide_free_quota_then_locked():
    # gate 开 + 未拥有：额度内 allow(used_free) → 用尽 locked
    base = dict(entitlement={"grants": [], "unlocked": []}, gate_enabled=True,
                free_daily=1, bond_level=5, min_bond_level=2)
    d0 = decide_selfie(free_used=0, **base)
    assert d0["action"] == "allow" and d0["used_free"] is True
    d1 = decide_selfie(free_used=1, **base)
    assert d1["action"] == "locked"


# ── provider 骨架 ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_provider_disabled_returns_error():
    p = SelfieProvider({"enabled": False})
    res = await p.generate("a prompt")
    assert res.ok is False
    assert res.error == "provider_disabled"


@pytest.mark.asyncio
async def test_provider_enabled_unknown_backend_soft_fails():
    p = SelfieProvider({"enabled": True, "backend": "disabled"})
    res = await p.generate("a prompt")
    assert res.ok is False  # backend disabled → 仍软失败，不抛


@pytest.mark.asyncio
async def test_provider_empty_prompt():
    p = SelfieProvider({"enabled": True, "backend": "openai"})
    res = await p.generate("   ")
    assert res.ok is False and res.error == "empty_prompt"


@pytest.mark.asyncio
async def test_provider_command_backend_generates(tmp_path):
    # 用一个最小命令模拟出图：写一个非空 png 文件到 {out}
    import sys
    script = tmp_path / "fake_gen.py"
    script.write_text(
        "import sys\n"
        "out=sys.argv[1]\n"
        "open(out,'wb').write(b'\\x89PNG fake image bytes')\n",
        encoding="utf-8")
    p = SelfieProvider({
        "enabled": True, "backend": "command",
        "out_dir": str(tmp_path / "out"),
        "command_args": [sys.executable, str(script), "{out}"],
    })
    res = await p.generate("portrait of a woman, safe-for-work")
    assert res.ok is True
    assert res.image_path.endswith(".png")


def test_singleton_reuse_and_reset():
    reset_selfie_provider()
    a = get_selfie_provider({"enabled": True})
    b = get_selfie_provider()
    assert a is b
    reset_selfie_provider()
    c = get_selfie_provider({})
    assert c is not a


# ── album 后端（预制相册随机挑发，做「人设照片」最一致/零 API 费） ────────────

@pytest.mark.asyncio
async def test_album_backend_picks_existing_image(tmp_path):
    d = tmp_path / "album"
    d.mkdir()
    img = d / "a.jpg"
    img.write_bytes(b"\x89PNG x")
    p = SelfieProvider({"enabled": True, "backend": "album", "album_dir": str(d)})
    res = await p.generate("ignored prompt for album")
    assert res.ok is True
    assert res.image_path == str(img)
    assert res.provider == "album"
    assert res.extra.get("album_size") == 1


@pytest.mark.asyncio
async def test_album_backend_empty_dir_soft_fails(tmp_path):
    p = SelfieProvider({"enabled": True, "backend": "album",
                        "album_dir": str(tmp_path / "nope")})
    res = await p.generate("x")
    assert res.ok is False
    assert res.error == "album_empty"  # 空相册 → 软失败（调用方退回文字），不抛


@pytest.mark.asyncio
async def test_album_backend_avoids_repeat_when_possible(tmp_path):
    d = tmp_path / "album"
    d.mkdir()
    (d / "a.jpg").write_bytes(b"a")
    (d / "b.png").write_bytes(b"b")
    p = SelfieProvider({"enabled": True, "backend": "album", "album_dir": str(d)})
    first = (await p.generate("x")).image_path
    # 传 avoid_path=first → 两张图时必挑另一张（连发不重复）
    second = (await p.generate("x", avoid_path=first)).image_path
    assert second != first


@pytest.mark.asyncio
async def test_album_backend_persona_subdir_preferred(tmp_path):
    base = tmp_path / "album"
    base.mkdir()
    (base / "root.jpg").write_bytes(b"r")
    sub = base / "xiaorou"
    sub.mkdir()
    (sub / "s.jpg").write_bytes(b"s")
    p = SelfieProvider({"enabled": True, "backend": "album", "album_dir": str(base)})
    res = await p.generate("x", album_key="xiaorou")
    assert res.image_path == str(sub / "s.jpg")  # 命中分册子目录，不用根目录


@pytest.mark.asyncio
async def test_album_backend_ignores_non_images(tmp_path):
    d = tmp_path / "album"
    d.mkdir()
    (d / "notes.txt").write_text("not an image", encoding="utf-8")
    p = SelfieProvider({"enabled": True, "backend": "album", "album_dir": str(d)})
    res = await p.generate("x")
    assert res.ok is False and res.error == "album_empty"  # .txt 不算图片


@pytest.mark.asyncio
async def test_album_key_blocks_path_traversal(tmp_path):
    base = tmp_path / "album"
    base.mkdir()
    (base / "safe.jpg").write_bytes(b"s")
    p = SelfieProvider({"enabled": True, "backend": "album", "album_dir": str(base)})
    # 恶意 album_key 里的路径分隔符/.. 被清洗 → 回落根目录，绝不逃出 album_dir
    res = await p.generate("x", album_key="../../etc")
    assert res.ok is True
    assert res.image_path == str(base / "safe.jpg")


# ── openai images 后端（注入假 client，无网络） ──────────────────────────

import base64 as _b64  # noqa: E402


class _FakeItem:
    def __init__(self, b64_json=None, url=None):
        self.b64_json = b64_json
        self.url = url


class _FakeResp:
    def __init__(self, items):
        self.data = items


class _FakeClient:
    """模拟 openai client：记录请求参数、返回预置 data。"""
    def __init__(self, items):
        self._items = items
        self.last_kwargs = None

        class _Images:
            def __init__(self, outer):
                self._outer = outer

            def generate(self, **kwargs):
                self._outer.last_kwargs = kwargs
                return _FakeResp(self._outer._items)

        self.images = _Images(self)


def test_openai_generate_bytes_from_b64():
    p = SelfieProvider({"enabled": True, "backend": "openai",
                        "api_key": "k", "model": "gpt-image-1"})
    raw = b"\x89PNG real-ish bytes"
    client = _FakeClient([_FakeItem(b64_json=_b64.b64encode(raw).decode())])
    out = p._openai_generate_bytes(client, "a prompt")
    assert out == raw
    # gpt-image-1 不应传 response_format（传了真实 API 会报错）
    assert "response_format" not in client.last_kwargs
    assert client.last_kwargs["model"] == "gpt-image-1"


def test_openai_dalle_sets_b64_response_format():
    p = SelfieProvider({"enabled": True, "backend": "openai",
                        "api_key": "k", "model": "dall-e-3"})
    client = _FakeClient([_FakeItem(b64_json=_b64.b64encode(b"x").decode())])
    p._openai_generate_bytes(client, "a prompt")
    assert client.last_kwargs["response_format"] == "b64_json"


def test_openai_quality_passthrough():
    p = SelfieProvider({"enabled": True, "backend": "openai",
                        "api_key": "k", "model": "gpt-image-1", "quality": "high"})
    client = _FakeClient([_FakeItem(b64_json=_b64.b64encode(b"x").decode())])
    p._openai_generate_bytes(client, "a prompt")
    assert client.last_kwargs["quality"] == "high"


def test_openai_url_fallback(monkeypatch):
    p = SelfieProvider({"enabled": True, "backend": "openai",
                        "api_key": "k", "model": "dall-e-3"})
    client = _FakeClient([_FakeItem(b64_json=None, url="http://img/x.png")])
    monkeypatch.setattr(p, "_download_image", lambda url: b"downloaded-bytes")
    out = p._openai_generate_bytes(client, "a prompt")
    assert out == b"downloaded-bytes"


def test_openai_no_b64_or_url_raises():
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": "k"})
    client = _FakeClient([_FakeItem(b64_json=None, url=None)])
    with pytest.raises(RuntimeError):
        p._openai_generate_bytes(client, "a prompt")


def test_openai_empty_data_raises():
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": "k"})
    client = _FakeClient([])
    with pytest.raises(RuntimeError):
        p._openai_generate_bytes(client, "a prompt")


def test_openai_missing_key_raises():
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": ""})
    with pytest.raises(RuntimeError):
        p._make_openai_client()


@pytest.mark.asyncio
async def test_openai_generate_end_to_end_writes_image(tmp_path, monkeypatch):
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": "k",
                        "model": "gpt-image-1", "out_dir": str(tmp_path / "out")})
    raw = b"\x89PNG end-to-end"
    client = _FakeClient([_FakeItem(b64_json=_b64.b64encode(raw).decode())])
    monkeypatch.setattr(p, "_make_openai_client", lambda: client)
    res = await p.generate("portrait, safe-for-work")
    assert res.ok is True
    assert res.image_path.endswith(".png")
    assert res.provider == "openai"
    from pathlib import Path as _P
    assert _P(res.image_path).read_bytes() == raw


@pytest.mark.asyncio
async def test_openai_generate_times_out_with_explicit_override(monkeypatch, tmp_path):
    import time as _t
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": "k",
                        "out_dir": str(tmp_path / "out")})

    class _SlowImages:
        def generate(self, **kwargs):
            _t.sleep(2.0)
            return _FakeResp([_FakeItem(b64_json="")])

    client = type("C", (), {"images": _SlowImages()})()
    monkeypatch.setattr(p, "_make_openai_client", lambda: client)
    res = await p.generate("a prompt", timeout_sec=0.2)
    assert res.ok is False
    assert "selfie_timeout" in res.error


@pytest.mark.asyncio
async def test_openai_generate_soft_fails_on_client_error(monkeypatch, tmp_path):
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": "k",
                        "out_dir": str(tmp_path / "out")})

    def _boom():
        raise RuntimeError("api down")

    monkeypatch.setattr(p, "_make_openai_client", _boom)
    res = await p.generate("a prompt")
    assert res.ok is False  # 绝不抛，软失败退回
    assert "api down" in res.error


# ── 基础图 img2img（openai images.edit / command {base}，锁人设一致性） ──────

class _FakeClientEdit:
    """同时带 images.generate 与 images.edit 的假 client（记录走了哪条）。"""
    def __init__(self, items):
        self._items = items
        self.edit_called = False
        self.generate_called = False
        self.last_kwargs = None
        outer = self

        class _Images:
            def generate(self, **kwargs):
                outer.generate_called = True
                outer.last_kwargs = kwargs
                return _FakeResp(outer._items)

            def edit(self, **kwargs):
                outer.edit_called = True
                outer.last_kwargs = kwargs
                return _FakeResp(outer._items)

        self.images = _Images()


@pytest.mark.asyncio
async def test_generate_img2img_uses_edit_endpoint(tmp_path, monkeypatch):
    base = tmp_path / "ref.png"
    base.write_bytes(b"\x89PNG ref-face")
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": "k",
                        "model": "gpt-image-1", "out_dir": str(tmp_path / "out")})
    raw = b"\x89PNG edited"
    client = _FakeClientEdit([_FakeItem(b64_json=_b64.b64encode(raw).decode())])
    monkeypatch.setattr(p, "_make_openai_client", lambda: client)
    res = await p.generate("same woman, now cooking", base_image=str(base))
    assert res.ok is True
    assert client.edit_called is True and client.generate_called is False  # 有基础图→走 edit
    assert res.extra.get("img2img") is True
    from pathlib import Path as _P
    assert _P(res.image_path).read_bytes() == raw


@pytest.mark.asyncio
async def test_generate_text2img_when_no_base(tmp_path, monkeypatch):
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": "k",
                        "model": "gpt-image-1", "out_dir": str(tmp_path / "out")})
    client = _FakeClientEdit([_FakeItem(b64_json=_b64.b64encode(b"x").decode())])
    monkeypatch.setattr(p, "_make_openai_client", lambda: client)
    res = await p.generate("a portrait")  # 无基础图 → text2img
    assert res.ok is True
    assert client.generate_called is True and client.edit_called is False
    assert res.extra.get("img2img") is False


@pytest.mark.asyncio
async def test_generate_ignores_missing_base_file(tmp_path, monkeypatch):
    p = SelfieProvider({"enabled": True, "backend": "openai", "api_key": "k",
                        "model": "gpt-image-1", "out_dir": str(tmp_path / "out")})
    client = _FakeClientEdit([_FakeItem(b64_json=_b64.b64encode(b"x").decode())])
    monkeypatch.setattr(p, "_make_openai_client", lambda: client)
    res = await p.generate("a portrait", base_image=str(tmp_path / "nope.png"))
    assert res.ok is True
    # 基础图路径不存在 → 回退 text2img（不因坏参数崩）
    assert client.generate_called is True and client.edit_called is False


def test_reference_image_from_album(tmp_path):
    d = tmp_path / "album"
    d.mkdir()
    (d / "ref.jpg").write_bytes(b"r")
    p = SelfieProvider({"enabled": True, "backend": "openai", "album_dir": str(d)})
    assert p.reference_image() == str(d / "ref.jpg")
    p2 = SelfieProvider({"enabled": True, "backend": "openai",
                         "album_dir": str(tmp_path / "empty")})
    assert p2.reference_image() == ""  # 无相册 → 空（skill 层据此回退 text2img）


@pytest.mark.asyncio
async def test_command_backend_receives_base_placeholder(tmp_path):
    import sys
    # 脚本把 {base} 写进输出，断言基础图路径被正确透传给本地推理脚本
    script = tmp_path / "fake_img2img.py"
    script.write_text(
        "import sys\n"
        "out, base = sys.argv[1], sys.argv[2]\n"
        "open(out,'wb').write(b'PNG:'+base.encode())\n",
        encoding="utf-8")
    base = tmp_path / "ref.png"
    base.write_bytes(b"ref")
    p = SelfieProvider({
        "enabled": True, "backend": "command", "out_dir": str(tmp_path / "out"),
        "command_args": [sys.executable, str(script), "{out}", "{base}"],
    })
    res = await p.generate("a prompt", base_image=str(base))
    assert res.ok is True
    from pathlib import Path as _P
    assert str(base) in _P(res.image_path).read_bytes().decode()
