"""主动触达候选卫生 — 自家舰队 peer 排除（P1 2026-08-03）。

实锤（2026-08-03 outreach_log）：主动触达把**自家账号**当客户问候——
``telegram:8041810715:8755679833``（8041810715 账号给自家另一账号 8755679833
发 ritual_morning）、``whatsapp:...:...`` 自己给自己发早安语音。自家账号之间的
会话在收件箱镜像里有意义（能看到彼此对话），但「主动情感触达」对它们零意义、
纯空转，还向平台风控表演自动化行为。

判据两条（跨平台）：
  - ``chat_key == account_id``：自己给自己（Saved Messages 语义）；
  - ``(platform, chat_key)`` 命中自家账号注册表：账号 A 主动去问候账号 B。

纯函数、零 IO（registry 由调用方注入），可单测。与 ``peer_bot_guard`` 的 bot
排除分层：那条管「对面是机器人」，这条管「对面是自己人」，都是候选池卫生。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Set, Tuple

__all__ = [
    "SERVICE_NAME_WORDS",
    "build_own_fleet_index",
    "is_own_fleet_peer",
    "is_service_peer_name",
    "proactive_candidate_ok",
    "build_peer_filter",
]

# 业务/服务号显示名词表（P2 2026-08-04，news_share 群发事故连带发现）：
# 「泷玥🍀全球一手实卡接码&代开会员(转账&文件语音确认）」「丽娜/全能业务
# 小客服」这类号收到了「你在关注霍尔木兹吗」——它们不是 bot（peer_bot_guard
# 不管）、不是自家账号（fleet 不管），但主动情感触达对其零意义、纯烧额度，
# 还向风控表演自动化。词表刻意保守：真人昵称不会含「接码/代充/收款」；
# 误伤代价＝不主动问候（入站回复完全不受影响）。
SERVICE_NAME_WORDS = (
    "接码", "实卡", "代开", "代充", "发卡", "卡商", "自助下单",
    "客服", "业务", "代理", "招商", "换汇", "跑分", "收款", "转账",
    "官方", "中介", "推广", "广告", "引流", "供货", "批发",
)


def is_service_peer_name(display_name: str) -> bool:
    """按显示名识别「业务/服务号」（接码/发卡/官方客服……）。

    纯函数零 IO；空名/无命中 → False（正常客户放行）。与 bot / fleet 排除
    同属候选池卫生的第三层：bot 管「对面是程序」、fleet 管「对面是自己人」、
    这条管「对面是同行/商家」。
    """
    low = str(display_name or "").strip().lower()
    if not low:
        return False
    return any(w in low for w in SERVICE_NAME_WORDS)


def build_own_fleet_index(registry: Any) -> Dict[str, Set[str]]:
    """从账号注册表建 ``{platform: {account_id, ...}}`` 自家账号索引。

    ``registry`` 需有 ``list()`` 返回含 ``platform``/``account_id`` 的行；
    缺失/异常一律回空 dict（调用方据此只保留「自己给自己」那条弱判定，绝不误伤）。
    """
    index: Dict[str, Set[str]] = {}
    try:
        rows = registry.list() if registry is not None else []
    except Exception:
        return {}
    for a in rows or []:
        try:
            plat = str(a.get("platform") or "").strip().lower()
            acct = str(a.get("account_id") or "").strip()
        except Exception:
            continue
        if plat and acct:
            index.setdefault(plat, set()).add(acct)
    return index


def is_own_fleet_peer(
    platform: str,
    account_id: str,
    chat_key: str,
    own_index: Optional[Dict[str, Set[str]]] = None,
) -> bool:
    """该会话的对端是否是自家账号（自己给自己 / 舰队内互发）。

    - ``chat_key == account_id`` → True（自己给自己，不依赖 registry）；
    - ``chat_key`` ∈ ``own_index[platform]`` → True（账号间互发）。
    空 chat_key / 无匹配 → False（正常客户，放行）。
    """
    ck = str(chat_key or "").strip()
    if not ck:
        return False
    if ck == str(account_id or "").strip():
        return True
    plat = str(platform or "").strip().lower()
    bucket = (own_index or {}).get(plat)
    return bool(bucket and ck in bucket)


def proactive_candidate_ok(
    row: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    *,
    own_index: Optional[Dict[str, Set[str]]] = None,
) -> Tuple[bool, str]:
    """主动触达候选统一卫生闸 → ``(是否可发, 拦截原因)``。

    聚合两类「不该主动触达的对端」，所有主动路径（proactive_topic / care /
    reactivation / friend_welcome）共用一个入口，避免各自漏接判定：
      - ``bot``：对面是机器人（复用 peer_bot_guard，守卫未启用时天然放行）；
      - ``own_fleet``：对面是自家账号（自己给自己 / 舰队内互发）。

    ``row`` 需含 ``platform``/``account_id``/``chat_key``（+ bot 判定要的
    ``peer_is_bot``/``chat_type``/``username``）。防御式，任何判定异常都按放行
    （fail-open：候选卫生是体验优化，不是安全红线，绝不因判定抖动漏发关怀）。
    """
    r = row or {}
    try:
        from src.inbox.peer_bot_guard import proactive_exclude_row
        if proactive_exclude_row(r, config or {}):
            return (False, "bot")
    except Exception:
        pass
    try:
        if is_own_fleet_peer(
                str(r.get("platform") or ""),
                str(r.get("account_id") or ""),
                str(r.get("chat_key") or ""),
                own_index):
            return (False, "own_fleet")
    except Exception:
        pass
    return (True, "")


def build_peer_filter(
    inbox_store: Any,
    config: Optional[Dict[str, Any]] = None,
    *,
    registry: Any = None,
) -> Callable[[str, str, str], bool]:
    """造一个 ``(platform, account_id, chat_key) -> 是否该跳过`` 过滤闭包。

    给 care / reactivation 这类「手里只有 chat_key、没有完整会话行」的派发路径
    注入：闭包内用 conv_id 查一次 inbox 会话行，再走 ``proactive_candidate_ok``
    统一判定。``inbox_store`` 缺失 → 恒放行（返回 False，不拦——bot 检测本就是
    增量护栏，宁可漏拦不误伤真实关怀）。自家账号索引在造闭包时建一次
    （账号增删低频，随进程重启刷新即可）。
    """
    own_index = build_own_fleet_index(registry) if registry is not None else {}

    def _should_skip(platform: str, account_id: str, chat_key: str) -> bool:
        if inbox_store is None:
            return False
        try:
            from src.inbox.normalizer import conv_id
            cid = conv_id(str(platform or ""), str(account_id or "default"),
                          str(chat_key or ""))
            row = dict(inbox_store.get_conversation(cid) or {})
            row.setdefault("platform", platform)
            row.setdefault("account_id", account_id)
            row.setdefault("chat_key", chat_key)
            ok, _reason = proactive_candidate_ok(
                row, config, own_index=own_index)
            return not ok
        except Exception:
            return False   # fail-open：查不到/异常不拦

    return _should_skip
