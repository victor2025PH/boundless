# -*- coding: utf-8 -*-
"""P-1 C（#259 #254 · 34585H E/F）：生成侧硬禁 + few-shot + 生成侧门禁。

钉住：
  - build_style_hint：禁破折号 / 分号 / 列表、禁无据引用、一两句；无历史多一条「首次接触不假装熟悉」；
  - few-shot 三语各 6 组，示例回复零 AI 标点 / 零引用短语 / ≤2 句；其他语种只给禁令；
  - spoken_style 包启用 → 不给内置示例；配置关 → 空串；
  - history_has_peer_turns：回复链 ≥2 条对方消息才算有共同过去，开场链 ≥1；
  - persona_reply 接线：回复链 extra_hint 消费口一处 + 开场 directive 一处（静态钉）；
  - outbound_style_gate --draft-log：从 [draft] 行算生成侧破折号率，<5% 过、≥5% 退 1、样本不足退 3。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.inbox import draft_style_hint as sh

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def test_hint_has_bans_and_history_switch():
    h = sh.build_style_hint("en", has_history=True, config={})
    for needle in ("No em dash", "no semicolon", "no bullet points", "you mentioned", "One or two short sentences"):
        assert needle in h
    assert "first exchange" not in h
    h0 = sh.build_style_hint("en", has_history=False, config={})
    assert "first exchange" in h0 and "do not recall" in h0


@pytest.mark.parametrize("lang", ["en", "zh", "ja"])
def test_few_shot_examples_are_clean(lang):
    from outbound_style_gate import violations
    from src.inbox.claim_guard import find_claims
    from src.inbox.outbound_humanize import humanize
    pairs = sh._FEW_SHOT[lang]
    assert 5 <= len(pairs) <= 10
    for _peer, reply in pairs:
        assert violations(reply) == [], reply
        assert find_claims(reply, lang) == [], reply
        out, st = humanize(reply, lang, cfg=None)
        assert out == reply and st["punct_fix"] == 0 and st["trimmed"] == 0, reply
    block = sh.few_shot_block(lang)
    assert block.count("\n- ") == len(pairs)
    assert block in sh.build_style_hint(lang, has_history=True, config={})


def test_other_languages_get_bans_only_and_switches():
    h = sh.build_style_hint("th", has_history=True, config={})
    assert "No em dash" in h and "Examples of the register" not in h
    # spoken_style 包启用 → few-shot 交给包
    h2 = sh.build_style_hint("zh", has_history=True, config={"ai": {"spoken_style": {"enabled": True}}})
    assert "No em dash" in h2 and "口吻示例" not in h2
    # few_shot 关 / 整段关
    h3 = sh.build_style_hint("zh", config={"inbox": {"auto_draft": {"style_hint": {"few_shot": False}}}})
    assert "口吻示例" not in h3 and "No em dash" in h3
    assert sh.build_style_hint("zh", config={"inbox": {"auto_draft": {"style_hint": False}}}) == ""
    # 语言缺失按文字系统
    assert "口吻示例" in sh.build_style_hint("", config={}, sample_text="你好呀")
    assert "トーンの例" in sh.build_style_hint("", config={}, sample_text="こんにちは")


def test_history_has_peer_turns():
    only_now = [{"role": "user", "content": "hi"}]
    assert sh.history_has_peer_turns(only_now) is False
    assert sh.history_has_peer_turns(only_now, min_turns=1) is True
    two = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}, {"role": "user", "content": "yo"}]
    assert sh.history_has_peer_turns(two) is True
    assert sh.history_has_peer_turns([], min_turns=1) is False
    assert sh.history_has_peer_turns(None) is False


def test_persona_reply_wired_reply_chain_and_opener():
    src = (ROOT / "src" / "inbox" / "persona_reply.py").read_text(encoding="utf-8")
    assert src.count("build_style_hint(") == 2
    i_hb = src.index("[persona_reply] 交接提醒跳过")
    i_sh = src.index("build_style_hint(")
    i_unified = src.index("extra_hint=_time_hint")
    assert i_hb < i_sh < i_unified
    i_dir = src.index("directive = build_opener_directive(")
    i_sh2 = src.index("build_style_hint(", i_sh + 1)
    i_gen = src.index("user_message=directive")
    assert i_dir < i_sh2 < i_gen
    assert "history_has_peer_turns(history, min_turns=1)" in src


def test_gate_generation_side_from_draft_log(tmp_path: Path, capsys):
    from outbound_style_gate import gen_side_stats, main
    line = ("[2026-09-08 16:00:00] [INFO] src.inbox.outbound_humanize: [draft] conv=whatsapp:a:b stage=enrich "
            "draft=inbox:x origin=auto lang=en len=40 claim={claim} punct_fix={pf} style_fix=0 trimmed=0 dash={d} "
            "fp=abcd1234 preview='hi'")
    ok_lines = [line.format(claim="clean", pf=0, d=0) for _ in range(97)]
    ok_lines += [line.format(claim="rewrite", pf=1, d=1) for _ in range(3)]
    g = gen_side_stats(ok_lines + ["unrelated line"])
    assert g == {"total": 100, "dash": 3, "dash_rate_pct": 3.0, "claim_rewrite": 3}
    p = tmp_path / "app.log"
    p.write_text("\n".join(ok_lines), encoding="utf-8")
    assert main(["--draft-log", str(p)]) == 0
    assert "dash_rate=3.0%" in capsys.readouterr().out
    # ≥5% → 1
    bad = ok_lines + [line.format(claim="clean", pf=2, d=2) for _ in range(5)]
    p.write_text("\n".join(bad), encoding="utf-8")
    assert main(["--draft-log", str(p)]) == 1
    # 样本不足 → 3；文件不存在 → 2
    p.write_text("\n".join(ok_lines[:5]), encoding="utf-8")
    assert main(["--draft-log", str(p)]) == 3
    assert main(["--draft-log", str(tmp_path / "nope.log")]) == 2
