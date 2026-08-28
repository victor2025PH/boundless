# -*- coding: utf-8 -*-
"""回复句数的档位内随机（B130，2026-08-28）。

实录：档位选「适中 2-4 句」，实际**恒定输出 3 句**，坐席与客户都能看出机械感
（skuio 09:44）。根因不在切分链——句数从头到尾只由 prompt 引导，而 prompt 给的是
一个**区间**「2-4 句话」，叠加 bubbles 合同默认 ``max_parts=3``，LLM 就稳稳停在
中间值。区间对模型等于没说，它每次都取同一个数。

修法是把「区间」换成**这一条的具体数字**：每次装配 prompt 时在档位区间内摇一个
数告诉模型「这条写 N 句」。区间语义仍写在前半句（运营在设置页看到的还是 2-4 句），
只是每轮落到不同的点上。

真人还有一个特征是偶尔只回一句（「嗯」「在的」），所以额外给一个小概率让目标掉到
档位下限**再低一句**（下限 1）——只低一句，不做整档坍塌：运营选了「稍详细」却经常
只收到一句话，那是违背设置而不是拟人。
"""
from __future__ import annotations

import random
from typing import Dict, Optional, Tuple

#: 档位 → (下限, 上限) 句数。键含历史别名（concise/brief=short，balanced=moderate，
#: long=detailed），与 ``persona_manager._format_persona_instructions`` 的分支一一对应。
SENTENCE_RANGES: Dict[str, Tuple[int, int]] = {
    "short": (1, 2), "concise": (1, 2), "brief": (1, 2),
    "balanced": (2, 4), "moderate": (2, 4),
    "detailed": (4, 6), "long": (4, 6),
}

#: 「偶发短打」概率：目标掉到下限再低一句（不低于 1）。
#: 0.15 是拟人与「听话」的折中——五六条里出现一条短回复，像人；再高就等于
#: 运营选的档位形同虚设。
SHORT_BURST_PROB = 0.15


def pick_reply_sentence_target(
    reply_length: str, *, rng: Optional[random.Random] = None,
) -> int:
    """在档位区间内摇出**这一条**的目标句数；未知档位返回 0（＝不注入具体数）。

    ``rng`` 供测试注入确定性随机源；生产用模块级全局 ``random``。
    """
    lo_hi = SENTENCE_RANGES.get((reply_length or "").strip().lower())
    if not lo_hi:
        return 0
    lo, hi = lo_hi
    r = rng or random
    if lo > 1 and r.random() < SHORT_BURST_PROB:
        return lo - 1
    return r.randint(lo, hi)


def sentence_target_hint(
    reply_length: str, *, rng: Optional[random.Random] = None,
) -> str:
    """给 prompt 追加的一句话（未知档位/摇不出数 → 空串，调用方原样不加）。"""
    n = pick_reply_sentence_target(reply_length, rng=rng)
    if n <= 0:
        return ""
    if n == 1:
        return "这一条只回一句话就好，不要展开。"
    return f"这一条写 {n} 句，别多也别少。"
