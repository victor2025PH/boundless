# -*- coding: utf-8 -*-
"""已撤销旧设定（boundaries.retired_facts）prompt 注入门禁（2026-08-03）。

这是「删除不干净」事故链里压住**会话历史自强化**与**客户假记忆钩子**
（「你上次视频还给我看了你新养的猫」——生产实录）的 prompt 层防线：
档案删干净后，历史窗口里的旧轮次仍会诱导 LLM 复读，只有显式否定指令能赢过
模仿倾向。full 与 compact 两种格式都必须注入（compact 是生产主用格式，
且历史越长越可能走 compact——恰是防线最该在场的场景）。
"""
from src.utils.persona_manager import PersonaManager


def _pm():
    return PersonaManager()


def test_full_format_injects_block():
    out = _pm()._format_persona_instructions({
        "name": "小雨", "role": "大学生",
        "boundaries": {"retired_facts": ["养猫（你没有养猫，旧设定已删）"]},
    })
    assert "已作废的旧设定" in out
    assert "养猫（你没有养猫，旧设定已删）" in out
    # 语义要点：只禁认领、不禁话题（硬回避客户聊猫反而机器人味）
    assert "别顺着承认" in out or "绝不把这些当成你的现状" in out


def test_compact_format_injects_block():
    out = _pm()._format_persona_compact({
        "name": "小雨", "role": "大学生",
        "boundaries": {"retired_facts": ["养猫（旧设定已删）"]},
    })
    assert "已作废的旧设定" in out
    assert "养猫（旧设定已删）" in out


def test_absent_when_empty_or_blank():
    pm = _pm()
    base = {"name": "小雨", "role": "大学生"}
    assert "已作废的旧设定" not in pm._format_persona_instructions(dict(base))
    assert "已作废的旧设定" not in pm._format_persona_compact(dict(base))
    for empty in ([], ["  "], "", None):
        p = dict(base, boundaries={"retired_facts": empty})
        assert "已作废的旧设定" not in pm._format_persona_instructions(p)
        assert "已作废的旧设定" not in pm._format_persona_compact(p)


def test_string_form_tolerated():
    """运营手填单条字符串（而非列表）也要生效——宽进严出。"""
    p = {"name": "小雨", "role": "x",
         "boundaries": {"retired_facts": "单条字符串设定"}}
    assert "单条字符串设定" in _pm()._format_persona_instructions(p)
    assert "单条字符串设定" in _pm()._format_persona_compact(p)


def test_other_boundaries_unaffected():
    """retired_facts 与 topics_to_avoid 并存互不挤占。"""
    out = _pm()._format_persona_instructions({
        "name": "小雨", "role": "x",
        "boundaries": {
            "topics_to_avoid": ["政治"],
            "retired_facts": ["养猫"],
        },
    })
    assert "避免讨论以下话题：政治" in out
    assert "已作废的旧设定" in out
