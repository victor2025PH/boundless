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
    "external_fleet_peer_match",
    "is_own_fleet_peer",
    "is_service_peer_name",
    "own_fleet_candidates",
    "own_fleet_dismissed_from_config",
    "own_fleet_extra_from_config",
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


def own_fleet_extra_from_config(
    config: Optional[Dict[str, Any]],
) -> list:
    """外机自有号显式登记表（实施72 P2，``companion.own_fleet.extra``）。

    条目形如 ``{platform, account_id, name}``——**另一台机器**运营的自有账号
    （本机注册表里没有/已归档，但对端确实是自动化自有号）。实锤：Calixa Lopez
    账密迁至别机后，本机 Wisley 与它同线程，两边都自动化＝跨机 AI 自聊回路。
    缺失/结构坏 → 空表（零行为变更）。
    """
    try:
        node = (((config or {}).get("companion") or {}).get("own_fleet")
                or {}).get("extra")
        if not isinstance(node, list):
            return []
        out = []
        for e in node:
            if not isinstance(e, dict):
                continue
            plat = str(e.get("platform") or "").strip().lower()
            acct = str(e.get("account_id") or "").strip()
            name = str(e.get("name") or "").strip()
            if plat and (acct or name):
                out.append({"platform": plat, "account_id": acct, "name": name})
        return out
    except Exception:
        return []


def external_fleet_peer_match(
    platform: str, chat_key: str, peer_name: str,
    config: Optional[Dict[str, Any]],
) -> str:
    """会话对端是否命中「外机自有号」登记表 → 命中返回标识串（否则 ""）。

    匹配两式（同平台前提下任一命中）：``chat_key == 登记 account_id``（TG 类
    chat_key=对端用户 id 的平台）；``display_name 归一等于登记 name``（messenger
    e2ee 线程 chat_key=线程 id ≠ 用户 id，只能靠名字——同名真客户被误命中的代价
    是「多一次人审」，软失败可接受且 why_no_reply 可见）。纯函数零 IO。
    """
    plat = str(platform or "").strip().lower()
    ck = str(chat_key or "").strip()
    pname = " ".join(str(peer_name or "").split()).casefold()
    if not plat or (not ck and not pname):
        return ""
    for e in own_fleet_extra_from_config(config):
        if e["platform"] != plat:
            continue
        if ck and e["account_id"] and ck == e["account_id"]:
            return f"extra:{e['account_id']}"
        ename = " ".join(str(e.get("name") or "").split()).casefold()
        if pname and ename and pname == ename:
            return f"extra:{e.get('account_id') or e.get('name')}"
    return ""


def own_fleet_dismissed_from_config(
    config: Optional[Dict[str, Any]],
) -> Set[str]:
    """「疑似自有号」已裁决登记表（``companion.own_fleet.dismissed``）。

    运营看过候选提示后裁决「这个归档号不是外机自有号（测试/废号）」→ 登记
    ``"platform:account_id"``（或 ``{platform, account_id, reason}`` dict 自带
    存档理由），命中它的对端**不再出候选提示**——已裁决的已知状态不该永久
    亮黄冒充「待人处理」（与门禁 allowlist 附因登记同一纪律）。
    与 ``extra``（登记＝封 review）语义相反：dismissed 零行为变更，只消音；
    删条目即恢复提示。首批：2026-08-27 老板裁决三条全为测试/废号。
    缺失/结构坏 → 空集（零行为变更）。
    """
    out: Set[str] = set()
    try:
        node = (((config or {}).get("companion") or {}).get("own_fleet")
                or {}).get("dismissed")
        if not isinstance(node, list):
            return out
        for e in node:
            if isinstance(e, str):
                key = e.strip().lower()
                if ":" in key:
                    out.add(key)
            elif isinstance(e, dict):
                plat = str(e.get("platform") or "").strip().lower()
                acct = str(e.get("account_id") or "").strip().lower()
                if plat and acct:
                    out.add(f"{plat}:{acct}")
        return out
    except Exception:
        return set()


def own_fleet_candidates(
    registry_rows: Optional[list],
    conversations: Optional[list],
    config: Optional[Dict[str, Any]] = None,
    *,
    max_items: int = 20,
) -> list:
    """「疑似自有号未登记」候选检测（实施72 下一阶段 P1，纯函数零 IO）。

    针对的洞：跨机自有号互聊（本机 Wisley ↔ 别机 Calixa）当前只靠人工登记
    ``companion.own_fleet.extra``——漏登记＝两台机器的 AI 隔着平台互聊零提示。
    而「账密迁去别机」在本机注册表里的痕迹恰是 **status=removed 的归档行**
    （Calixa 原型），``build_own_fleet_index`` 只收现役账号，正好看不见它们。

    判定（同平台）：**私聊**对端 ``chat_key == 某 removed 账号的 account_id``
    （id 命中）或 对端显示名归一 == 其 ``meta.self_name``（名字命中，messenger
    e2ee 线程 chat_key=线程 id 只能靠名字），且未被 ``own_fleet.extra`` 覆盖
    （``external_fleet_peer_match``）→ 出候选行。

    刻意只匹 removed 行：对端命中**现役**账号＝同库对子，是「老板拿自己账号
    扮客户测全自动」的常态工作流（compute_mode_caps ⑥ 也刻意不封它）；把它
    列成「未登记」会诱导运营错把本机现役号登进 extra → 白吃 review 封顶。
    只读提示不封顶；``registry_rows`` 必须来自 ``list(include_removed=True)``。
    已裁决的归档号（``own_fleet.dismissed``）不再提示——按**账号**消音而非按
    线程：运营裁决的是「这个号是测试/废号」，它将来换线程出现同样不提示。
    """
    convs = conversations or []
    if not convs:
        return []
    dismissed = own_fleet_dismissed_from_config(config)
    by_id: Dict[Tuple[str, str], Dict[str, Any]] = {}
    by_name: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in registry_rows or []:
        try:
            if str(row.get("status") or "").strip().lower() != "removed":
                continue
            plat = str(row.get("platform") or "").strip().lower()
            acct = str(row.get("account_id") or "").strip()
            if not plat or not acct:
                continue
            if f"{plat}:{acct.lower()}" in dismissed:
                continue
            name = " ".join(str(
                ((row.get("meta") or {}).get("self_name")) or "").split()
            ).casefold()
            info = {"account_id": acct, "self_name":
                    str(((row.get("meta") or {}).get("self_name")) or "")}
            by_id[(plat, acct)] = info
            if name:
                by_name[(plat, name)] = info
        except Exception:
            continue
    if not by_id and not by_name:
        return []
    out: list = []
    seen: Set[Tuple[str, str, str]] = set()
    for c in convs:
        try:
            if str(c.get("chat_type") or "private") not in ("private", ""):
                continue
            plat = str(c.get("platform") or "").strip().lower()
            owner = str(c.get("account_id") or "").strip()
            ck = str(c.get("chat_key") or "").strip()
            pname = str(c.get("display_name") or "").strip()
            if not plat or not ck or ck in ("me", owner):
                continue
            hit = by_id.get((plat, ck))
            match = "id"
            if hit is None:
                norm = " ".join(pname.split()).casefold()
                hit = by_name.get((plat, norm)) if norm else None
                match = "name"
            # 归档账号自己遗留的会话目录（owner==命中账号）不是「对端像自有号」
            if hit is None or hit["account_id"] == owner:
                continue
            if external_fleet_peer_match(plat, ck, pname, config):
                continue  # 已登记 own_fleet.extra ＝ 封顶层已接管，无需提示
            key = (plat, owner, ck)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "platform": plat,
                "account_id": owner,
                "chat_key": ck,
                "peer_name": pname,
                "matched_account": hit["account_id"],
                "matched_name": hit["self_name"],
                "match": match,
                "last_ts": float(c.get("last_ts") or 0.0),
            })
        except Exception:
            continue
    out.sort(key=lambda x: -x["last_ts"])
    return out[: max(1, int(max_items))]


def build_own_fleet_index(
    registry: Any, extra: Optional[list] = None,
) -> Dict[str, Set[str]]:
    """从账号注册表建 ``{platform: {account_id, ...}}`` 自家账号索引。

    ``registry`` 需有 ``list()`` 返回含 ``platform``/``account_id`` 的行；
    缺失/异常一律回空 dict（调用方据此只保留「自己给自己」那条弱判定，绝不误伤）。
    ``extra``（实施72 P2）＝``own_fleet_extra_from_config`` 的外机自有号条目，
    并入索引后主动触达对它们同样跳过（registry 异常时 extra 仍生效）。
    """
    index: Dict[str, Set[str]] = {}
    try:
        rows = registry.list() if registry is not None else []
    except Exception:
        rows = []
    for a in rows or []:
        try:
            plat = str(a.get("platform") or "").strip().lower()
            acct = str(a.get("account_id") or "").strip()
        except Exception:
            continue
        if plat and acct:
            index.setdefault(plat, set()).add(acct)
    for e in extra or []:
        try:
            plat = str(e.get("platform") or "").strip().lower()
            acct = str(e.get("account_id") or "").strip()
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
    # P-2 E / F（#259 #252 · D-P4）：客户要求过停联（账号级名单，随重装 / 迁移包持久）
    # 与自聊会话（peer == 自己）**永不**主动触达——关怀 / 唤醒 / opener / 目标同一入口。
    # 名单取进程默认实例（首次 get_blocklist(store) 已登记路径）；未初始化 → 空名单放行。
    try:
        from src.inbox.normalizer import is_self_chat
        if is_self_chat(str(r.get("platform") or ""), str(r.get("account_id") or ""),
                        str(r.get("chat_key") or "")):
            return (False, "self_chat")
    except Exception:
        pass
    try:
        from src.inbox.account_blocklist import get_blocklist
        if get_blocklist().is_blocked(str(r.get("platform") or ""),
                                      str(r.get("account_id") or ""),
                                      str(r.get("chat_key") or "")):
            return (False, "stop_contact")
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
    own_index = build_own_fleet_index(
        registry, extra=own_fleet_extra_from_config(config),
    ) if registry is not None else {}

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
