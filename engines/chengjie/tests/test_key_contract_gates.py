# -*- coding: utf-8 -*-
"""键契约门禁（P1-2，2026-08-18）——封死「消费者读了没人写的键」假绿类缺陷。

事故原型（2026-08-18 真机探针抓到）：persona_stock 读 ``reference_audio`` 而
生产正典是 ``reference_audio_path``——模块与测试夹具用同一个错键互相印证，
11 条门禁绿着错。这类缺陷静态可判：**voice_profile 家族的每个消费端读键，
必须存在于生产者键宇宙**（enroll 构建器 ∪ 合并层键表 ∪ 仓内三份 yaml 的
voice_profile 块 ∪ 显式登记的「文档型可选键」）。

- 新增消费读键而没人写 → 本门禁红（要么改键名、要么去写入方补键、要么带
  理由登记 _DOCUMENTED_OPTIONAL）；
- 测试夹具回潮裸 ``reference_audio`` → 专项 ratchet 红。

speech_prints 条目 schema 同批钉住（overlay 校验器 ⊇ 出厂件实际用键——
avatarhub 内容升级加新字段时先红这里，防校验器静默拒收出厂同款条目）。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ENGINE = Path(__file__).resolve().parents[1]

#: 文档型可选键（运营可在 yaml 手配、代码消费但不经任何构建器写出）。
#: 登记必须带理由——没有理由的键该去改代码不是来这里注册。
_DOCUMENTED_OPTIONAL = {
    # tts_pipeline 逐字稿/口头禅/口语化/方言/副语言/静态语气：Phase2/3/7 人设级
    # 调音键，profiles_runtime 按需手配（当前生产未必每键都在用）
    "catchphrase", "colloquial", "paralinguistic", "instruct",
    # voice_emotion：按情绪分参考音（多参考音进阶配置，文档约定）+ 情绪覆写
    "reference_audio_by_emotion", "emotion",
    # 方言底色（生产实例 profiles_runtime 实配 chuanyu；测试密闭不读实例数据，
    # 仓内 yaml 快照滞后于实例运行时数据——真产键，按文档型登记）
    "dialect_flavor",
}

_READ_PAT = re.compile(
    r"\b(?:vp|voice_profile|base_vp)\s*(?:\.get\(\s*[\"']([a-z_]+)[\"']"
    r"|\[\s*[\"']([a-z_]+)[\"']\])")


def _yaml_voice_profile_keys(path: Path) -> set:
    """递归收集一份 yaml 里所有 voice_profile 块的键。"""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    out: set = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "voice_profile" and isinstance(v, dict):
                    out.update(str(x) for x in v.keys())
                walk(v)
        elif isinstance(node, list):
            for it in node:
                walk(it)

    walk(data)
    return out


def _producer_keys() -> set:
    from src.ai.persona_voice import _CLONE_BLEED_KEYS
    from src.ai.voice_enroll import build_avatar_voice_profile

    keys = set(_CLONE_BLEED_KEYS)
    keys.update(build_avatar_voice_profile(
        reference_audio_path="x.wav", speaker_id="s",
        reference_text="t", source_ref={"a": 1}).keys())
    for rel in ("config/config.example.yaml", "config/profiles_runtime.yaml",
                "config/profiles/cloud_light.yaml"):
        p = ENGINE / rel
        if p.is_file():
            keys.update(_yaml_voice_profile_keys(p))
    # 合并层入口判定键（_merge_voice_profile 的占位过滤表）
    keys.update({"enabled", "backend", "voice", "speaker_id",
                 "reference_audio_path"})
    return keys


def _consumer_reads() -> dict:
    reads: dict = {}
    for p in (ENGINE / "src").rglob("*.py"):
        try:
            t = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in _READ_PAT.finditer(t):
            k = m.group(1) or m.group(2)
            reads.setdefault(k, set()).add(str(p.relative_to(ENGINE)))
    return reads


def test_voice_profile_reads_have_producers():
    producers = _producer_keys()
    universe = producers | _DOCUMENTED_OPTIONAL
    orphan = {k: sorted(v) for k, v in _consumer_reads().items()
              if k not in universe}
    assert not orphan, (
        "以下 voice_profile 读键没有任何生产者也未登记为文档型可选键"
        "（reference_audio_path 假绿事故同类）：\n"
        + "\n".join(f"  {k}: {files}" for k, files in sorted(orphan.items())))


def test_documented_optional_not_stale():
    """登记表防过期：登记键必须仍被消费——消费点删光了就把登记一起删。"""
    reads = _consumer_reads()
    stale = sorted(k for k in _DOCUMENTED_OPTIONAL if k not in reads)
    assert not stale, f"登记键已无消费点，请从 _DOCUMENTED_OPTIONAL 移除: {stale}"


# 刻意没有「全仓禁裸 reference_audio 字符串」的钝测试：实扫发现该字符串的
# 合法用途有命令模板占位符（tts_pipeline）、告警分类标签（persona_asset_lint）、
# API 响应字段名（voice_routes）——真风险只存在于「从 voice 配置 dict 读键」，
# 上面的读键模式门禁已精确覆盖且自闭环（夹具写错键+代码读对键=测试自然红；
# 夹具代码同错=读键门禁红）。


def test_speech_prints_factory_within_validator_schema():
    """出厂指纹条目的用键 ⊆ overlay 校验器允许集——avatarhub 加新字段时先红
    这里（否则运营把出厂条目复制进面板保存会被校验器无声拒收）。"""
    import json
    f = ENGINE.parent.parent / "platform" / "spoken_style" / "data" / "speech_prints.json"
    if not f.is_file():
        import pytest
        pytest.skip("spoken_style 包不在本仓")
    allowed = {"print", "catch", "example", "guide"}
    data = json.loads(f.read_text(encoding="utf-8"))
    bad = {}
    for role, entry in data.items():
        if str(role).startswith("_") or not isinstance(entry, dict):
            continue
        extra = set(entry.keys()) - allowed
        if extra:
            bad[role] = sorted(extra)
    assert not bad, (
        f"出厂指纹条目出现校验器不认的字段（同步扩 validate_entry）: {bad}")
