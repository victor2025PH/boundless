"""真发演练脚本的安全护栏门禁（tools/live_multiwin_drill.py，2026-07-29）。

演练脚本本身会**真发消息**，它的护栏和判定算式必须像产品代码一样被守住：

  1. ``ensure_safe_target``——唯一防「复跑时把演练文案发给真客户」的硬闸门。
     真发脚本最大的风险不是逻辑错，而是有人顺手把 --chat-key 指到真人会话。
  2. ``expected_dedup_delta``——首次实施时我把「三次提交 → reserved+3」算错（实际 +2：
     仅首见占坑，重复命中记 duplicate 不新增 reserved）。算式错会把正确产品判成失败。
  3. ``count_tagged``——首次实施时按裸子串统计，把上一轮 Saved Messages 里的同类
     演练消息也数进来，误判成「双发」。判定必须按本轮 TAG 限定。

本文件是纯函数门禁（不联网、不发消息），随全量回归常驻。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.live_multiwin_drill import (  # noqa: E402
    G_DUPES,
    G_RESERVED,
    SAVED_MESSAGES,
    UnsafeTarget,
    count_tagged,
    ensure_safe_target,
    expected_dedup_delta,
    make_tag,
)


# ── 1. 目标安全闸门 ────────────────────────────────────────────


def test_saved_messages_is_the_only_default_target():
    assert ensure_safe_target("me") == SAVED_MESSAGES
    assert ensure_safe_target("ME") == SAVED_MESSAGES        # 大小写不敏感
    assert ensure_safe_target("  me  ") == SAVED_MESSAGES    # 容忍空白


@pytest.mark.parametrize("target", ["8244899900", "5433982810", "peer1", "-100123"])
def test_real_peer_refused_without_explicit_optin(target):
    """真人会话必须显式 --allow-peer——否则演练文案会真的发给客户。"""
    with pytest.raises(UnsafeTarget):
        ensure_safe_target(target)
    # 显式 opt-in 后放行（运维知情选择），原值透传不被改写
    assert ensure_safe_target(target, allow_peer=True) == target


def test_empty_target_refused_even_with_optin():
    for allow in (False, True):
        with pytest.raises(UnsafeTarget):
            ensure_safe_target("", allow_peer=allow)
        with pytest.raises(UnsafeTarget):
            ensure_safe_target("   ", allow_peer=allow)


# ── 2. 计数器增量算式（首次实施踩过的坑）────────────────────────


def test_dedup_delta_reserved_counts_first_see_only():
    """三次提交（首发 / 同 id 重复 / 换 id）→ reserved+2、duplicates+1。

    重复命中**不**新增 reserved 是 SendDedup.reserve 的精确语义；若哪天改成
    「每次提交都记 reserved」，本例立刻红，提醒同步改演练判定。
    """
    d = expected_dedup_delta(n_first=1, n_repeat=1, n_new_id=1)
    assert d[G_RESERVED] == 2
    assert d[G_DUPES] == 1


def test_dedup_delta_scales_and_handles_zero():
    assert expected_dedup_delta(0, 0, 0) == {G_RESERVED: 0, G_DUPES: 0}
    # 只重复不首见：reserved 不动（窗口内老 id 重放）
    assert expected_dedup_delta(0, 3, 0)[G_RESERVED] == 0
    assert expected_dedup_delta(0, 3, 0)[G_DUPES] == 3
    # 多批次线性叠加
    assert expected_dedup_delta(2, 5, 3) == {G_RESERVED: 5, G_DUPES: 5}


# ── 3. TAG 限定统计（首次实施踩过的坑）──────────────────────────


def test_count_tagged_isolates_current_run():
    """往轮残留（不同 TAG）绝不能被数进本轮判定。"""
    texts = [
        "[验收QA103730] 人工通过真投递·双窗互斥",   # 上一轮残留
        "[验收QA103815] 人工通过真投递·双窗互斥",   # 本轮
        "客户的正常消息，无关内容",
    ]
    assert count_tagged(texts, "QA103815", "人工通过真投递") == 1
    assert count_tagged(texts, "QA103730", "人工通过真投递") == 1
    # 不按 TAG 限定就会数成 2 —— 这正是首次实施的误判来源
    assert sum(1 for t in texts if "人工通过真投递" in t) == 2


def test_count_tagged_marker_narrows_within_same_run():
    texts = [
        "[验收QA110000] 幂等·第一发（应送达）",
        "[验收QA110000] 幂等·换id（应送达）",
        "[验收QA110000] 人工通过真投递·双窗互斥",
    ]
    assert count_tagged(texts, "QA110000") == 3                  # 本轮全部
    assert count_tagged(texts, "QA110000", "幂等") == 2           # 只数 E1
    assert count_tagged(texts, "QA110000", "第二发") == 0         # 被拦的不该在


def test_count_tagged_empty_tag_never_matches_everything():
    """空 TAG 必须返回 0——绝不能退化成「全都算本轮」而把往轮残留计入。"""
    assert count_tagged(["[验收QA1] x", "y"], "") == 0
    assert count_tagged([], "QA1") == 0


def test_make_tag_shape_is_assertion_friendly():
    tag = make_tag(1785000000.0)
    assert tag.startswith("QA") and len(tag) == 8 and tag[2:].isdigit()
    # 同一时刻恒定（同轮内多次取值不漂移，判定才稳）
    assert make_tag(1785000000.0) == tag
