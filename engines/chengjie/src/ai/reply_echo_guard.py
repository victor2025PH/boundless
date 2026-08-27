"""B51 出稿反复读闸（2026-08-23 实施64 P1-2，`_285` 实录）。

事故：出站草稿与**对方上一条原话**几乎逐字相同（AI 把客户的话当成了要发的
回复）。无论上游哪个环节串了稿（翻译镜像回灌 / LLM 复读 / 历史窗口错位），
出稿前这道闸都必须兜住——「回复不得与对方上一条雷同」是硬不变量。

判定刻意保守（宁可放过，绝不误杀正常回复）：
- 归一化（去空白/标点/emoji + casefold）后，短文本不判（「好」「哈哈」
  「ok」这类正常呼应词雷同是常态）；
- 相似度用 difflib ratio（与 proactive_variety 同族口径）+ 整包含判定
  （回复把对方原话整段包进来且几乎没加自己的话）。

纯函数、零依赖，可单测。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

#: 归一化后短于此长度不判（正常短呼应「好的」「嗯嗯」「why?」雷同是常态）。
MIN_JUDGE_CHARS = 6

#: 归一化相似度阈值：生产复读事故是近逐字（>0.95），0.9 留余量抓「改一两个
#: 标点/语气词」的变体；正常「围绕对方话题展开」的回复远低于此。
ECHO_RATIO = 0.90

_STRIP_RE = re.compile(
    r"[\s，。！？!?,.、；;：:…~～\-—_'\"“”‘’()（）\[\]【】<>《》@#*&^%$/\\|`+=]+")
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\ufe0f]+")


def _norm(text: str) -> str:
    t = str(text or "")
    t = _EMOJI_RE.sub("", t)
    t = _STRIP_RE.sub("", t)
    return t.casefold()


def reply_echoes_inbound(reply: str, inbound: str) -> bool:
    """出稿是否复读了对方上一条（归一化近逐字 / 整段包含）。

    只与**对方上一条**比（调用方传入）；与自己历史回复的复读另有
    ``_push_recent_reply`` 防复读环，语义不混。
    """
    r = _norm(reply)
    i = _norm(inbound)
    if len(i) < MIN_JUDGE_CHARS or len(r) < MIN_JUDGE_CHARS:
        return False
    if r == i:
        return True
    # 整包含：对方原话整段进了回复，且回复没加多少自己的内容
    if len(i) >= 8 and i in r and len(r) <= int(len(i) * 1.3):
        return True
    try:
        ratio = SequenceMatcher(None, r, i).ratio()
    except Exception:
        return False
    return ratio >= ECHO_RATIO


__all__ = ["reply_echoes_inbound", "ECHO_RATIO", "MIN_JUDGE_CHARS"]
