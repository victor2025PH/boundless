"""向导漏斗口径 —— 和 :mod:`outcome` 分开，不改原有冷热判定。

成功不是「群里有人说话」，也不是「我们先私聊了谁」。

成功 = 开演之后，**这个群成员名单里的人**自己先给固定向导号发来私聊，
并且做了这一项功能。私聊里多说一句不算做了。没有点名功能的事件时只记添加。
帮手号收到的私聊、向导先发出去的冷私聊，都不算。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from src.companion.group_show.playbook import GUIDE_SLOT

#: 开始测试的唯一事件名。客户随口聊到「试试」不算。
#: 私聊正文以 ``guide_trial:`` 开头（或单独成行）才算，后面是这一项功能的 id。
TRIAL_EVENT = "guide_trial"


def configured_guide_account(app_config: Optional[Mapping[str, Any]]) -> str:
    """``companion.group_show.guide_account``。没配就是空串，调用方不得拿别的号顶上。"""
    try:
        node = ((app_config or {}).get("companion") or {}).get("group_show") or {}
        return str(node.get("guide_account") or "").strip()
    except Exception:  # noqa: BLE001 —— 配置形状脏时按「没配向导」处理
        return ""


def playbook_declares_guide(playbook: Any) -> bool:
    """剧本声明了向导槽。没声明的旧剧本走原路径，行为不变。"""
    try:
        roles = getattr(playbook, "roles", ()) or ()
    except Exception:  # noqa: BLE001
        return False
    for role in roles:
        slot = str(getattr(role, "slot", "") or "").strip().lower()
        if slot == GUIDE_SLOT:
            return True
    return False


def _is_inbound(row: Mapping[str, Any]) -> bool:
    if row.get("inbound") is True:
        return True
    direction = str(row.get("direction") or "").strip().lower()
    return direction in ("in", "inbound")


def trial_marker(feature: str) -> str:
    """记下「做了这一项功能」的那一行。功能 id 为空就没有事件，不能拿来充数。"""
    feat = _clean_feature(feature)
    if not feat:
        return ""
    return f"{TRIAL_EVENT}:{feat}"


def _clean_feature(feature: Any) -> str:
    feat = str(feature or "").strip()
    if not feat or feat.lower() == "one":
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", feat):
        return ""
    return feat


def single_feature(playbook: Any) -> str:
    """剧本只声明了一项产品时，那就是这场要试的功能。多项或没有，返回空串。"""
    try:
        products = getattr(playbook, "products", ()) or ()
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(products, str):
        products = (products,)
    clean = []
    for raw in products:
        feat = _clean_feature(raw)
        if feat and feat not in clean:
            clean.append(feat)
    return clean[0] if len(clean) == 1 else ""


def features_by_playbook(books: Any) -> Dict[str, str]:
    """``{playbook_id: 唯一功能}``。没有唯一功能的剧本不出现。"""
    out: Dict[str, str] = {}
    try:
        pairs = books.items() if isinstance(books, Mapping) else ()
    except Exception:  # noqa: BLE001
        return out
    for pid, pb in pairs:
        feat = single_feature(pb)
        key = str(pid or "").strip()
        if key and feat:
            out[key] = feat
    return out


def configured_guide_feature(app_config: Optional[Mapping[str, Any]]) -> str:
    """``companion.group_show.guide_feature``。没配就是空串，改由剧本上的那一项决定。"""
    try:
        node = ((app_config or {}).get("companion") or {}).get("group_show") or {}
        return _clean_feature(node.get("guide_feature"))
    except Exception:  # noqa: BLE001
        return ""


def _trial_feature(row: Mapping[str, Any]) -> str:
    """这行点名的功能 id。点不出来就是空串，私聊正文本身不算。"""
    named = _clean_feature(row.get("feature"))
    if named:
        return named
    text = str(row.get("text") or "").replace("\r\n", "\n")
    prefix = TRIAL_EVENT + ":"
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith(prefix):
            return _clean_feature(line[len(prefix):])
    return ""


def _counts_as_feature(row: Mapping[str, Any], feature: str) -> bool:
    """算不算「做了这一项」。第二句闲聊不算。点了别的功能也不算。"""
    got = _trial_feature(row)
    if got:
        return (not feature) or got == feature
    kind = str(row.get("event") or row.get("kind") or "").strip().lower()
    bare = (row.get("tested") is True or row.get("test_started") is True
            or kind == TRIAL_EVENT)
    # 没写功能 id 的旧标记，只在这场没有钉死功能时才认。
    return bool(bare) and not feature


def fallback_peers(
    member_ids: Optional[Iterable[Any]],
    spoke_ids: Optional[Iterable[Any]],
    added_ids: Optional[Iterable[Any]],
    *,
    window_complete: bool,
) -> Tuple[str, ...]:
    """窗口走完后，在群里说过话、仍没自己来加向导的成员。

    只产出名单。本函数不发私聊；窗口没走完也不把人放进名单，免得窗口还开着就去催。
    """
    if not window_complete:
        return ()

    def _ids(raw: Optional[Iterable[Any]]) -> set:
        out = set()
        for item in raw or ():
            got = str(item or "").strip()
            if got:
                out.add(got)
        return out

    return tuple(sorted((_ids(member_ids) & _ids(spoke_ids)) - _ids(added_ids)))


def _member_id(row: Mapping[str, Any]) -> str:
    return str(row.get("user_id") or row.get("id") or "").strip()


def attribute_inbound(
    members: Optional[Iterable[Mapping[str, Any]]],
    dms: Optional[Iterable[Mapping[str, Any]]],
    *,
    guide_account_id: str,
    after: float,
    group_key: str = "",
    feature: str = "",
) -> Dict[str, Tuple[str, ...]]:
    """把「谁加了向导 / 谁开始测试」从成员名单和私聊记录里算出来。

    纯函数：不读库。``members`` / ``dms`` 都是普通字典。

    一条私聊算进 ``added``，必须同时满足：

    - ``user_id`` 在本群成员里（路过的人、别的群的人不算）；
    - 对方账号是向导，不是帮手；
    - 这条线程里**第一句是对方先说的**，而且时间晚于 ``after``（开演时刻）。

    ``tested`` 是 ``added`` 的子集：对方做了 ``feature`` 点名的那一项
    （:func:`trial_marker` 或行上的 ``feature``）。``feature`` 为空时，认任意一个
    写明了的功能 id。私聊里多说一句不算。
    """
    guide = str(guide_account_id or "").strip()
    want_feature = _clean_feature(feature)
    want_group = str(group_key or "").strip()
    member_ids = set()
    for raw in members or ():
        if not isinstance(raw, Mapping):
            continue
        if want_group:
            got = str(raw.get("group_key") or raw.get("group_id") or "").strip()
            if got != want_group:
                continue
        uid = _member_id(raw)
        if uid:
            member_ids.add(uid)

    empty: Dict[str, Tuple[str, ...]] = {"added": (), "tested": ()}
    if not guide or not member_ids:
        return empty

    by_user: Dict[str, list] = {}
    for raw in dms or ():
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("account_id") or "").strip() != guide:
            continue
        uid = str(raw.get("user_id") or "").strip()
        if uid not in member_ids:
            continue
        by_user.setdefault(uid, []).append(raw)

    try:
        after_ts = float(after)
    except (TypeError, ValueError):
        return empty

    added = []
    tested = []
    for uid, rows in by_user.items():
        ordered = sorted(rows, key=lambda r: _ts(r))
        first = ordered[0]
        if not _is_inbound(first) or _ts(first) <= after_ts:
            continue
        added.append(uid)
        if any(_is_inbound(r) and _ts(r) > after_ts
               and _counts_as_feature(r, want_feature) for r in ordered):
            tested.append(uid)
    return {"added": tuple(sorted(added)), "tested": tuple(sorted(tested))}


def speakers_answered_by_guide(
    inbound: Optional[Iterable[Mapping[str, Any]]],
    guide_lines: Optional[Iterable[Mapping[str, Any]]],
) -> Tuple[str, ...]:
    """群里发过言、并且向导在这句之后开口接过的人。

    向导先说、对方后说，不算接过这句。没有向导的群内发言时，一个人都不算。
    """
    times: list = []
    for raw in guide_lines or ():
        if not isinstance(raw, Mapping):
            continue
        ts = _ts(raw)
        if ts > 0:
            times.append(ts)
    answered = set()
    for raw in inbound or ():
        if not isinstance(raw, Mapping):
            continue
        sid = str(raw.get("sender_id") or "").strip()
        ts = _ts(raw)
        if sid and ts > 0 and any(g > ts for g in times):
            answered.add(sid)
    return tuple(sorted(answered))


def _ts(row: Mapping[str, Any]) -> float:
    try:
        return float(row.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0
