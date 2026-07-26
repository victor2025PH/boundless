"""多号同台的关联风险分组 —— 回答「这两个号能不能同时出现在同一个群里」。

**为什么不直接用 ``src/integrations/fingerprint.py``**：那个模块造的是**浏览器指纹画像**
（UA / 屏幕 / 时区 / WebGL），解决的是「每个号看起来像不同的浏览器」。而群戏要防的是
**平台把多个号认成一伙**，其主导信号是**网络出口**——两个号 UA 再不一样，从同一个公网 IP
在同一个群里一唱一和，照样一锅端；何况 protocol 模式（pyrogram）压根不走浏览器，UA 指纹
对它完全无关。所以本模块按「出口」而不是「画像」分组，与那个模块语义正交：一个是造指纹，
一个是判关联。

分组规则（保守优先，宁可少上一个号）
------------------------------------
1. 有独立代理 → ``proxy:<id>``      —— 唯一真正意义上的网络隔离
2. device 模式且拿得到设备标识 → ``device:<serial>`` —— 手机走自己的蜂窝网，出口独立
3. 其余一律 → ``host:<tag>``        —— 共享宿主出口，**全部视为同一组**

第 3 条是本模块的立身之本，也是它与旧实现最大的分歧。旧逻辑是「指纹未知 → 拿 account_id
当组名 → 于是各自独立」，**把「未知」当成了「安全」**。真实后果：一台机器上 9 个没配代理的
号会被判成 9 个互不相干的指纹组、全部准许同台，而它们共享同一个公网 IP。这不是理论隐患，
这就是本仓 2026-07 账号注册表的实际状态（``proxy_id`` / ``fingerprint_id`` 九个号全为空串）。

``fingerprint_id`` 为什么不参与分组
-----------------------------------
它在**同一出口之下**只能削弱、不能消除关联：平台看到同一 IP 上多个号在同一个群里高频交替
发言，浏览器画像再不同也救不回来。所以它只作为 ``source`` 记录进体检报告（用于告诉运营
「你配了画像但没配代理，防护是不完整的」），不参与分组决策。

本模块是**纯函数**：无 I/O、无全局状态，账号数据由调用方从注册表捞好后传进来。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# ── 常量 ────────────────────────────────────────────────────────────────────

#: 共享宿主出口的兜底组名前缀。落到这个组的号彼此**不能同台**。
HOST_GROUP_PREFIX = "host"

#: 独立代理组名前缀（真正的网络隔离）。
PROXY_GROUP_PREFIX = "proxy"

#: 物理设备组名前缀（手机走自己的蜂窝网）。
DEVICE_GROUP_PREFIX = "device"

#: 宿主标识缺省值。多机部署时由调用方传入实际机器名区分。
DEFAULT_HOST_TAG = "local"

#: meta 里可能承载设备标识的键，按可信度从高到低探测。
_DEVICE_META_KEYS: Tuple[str, ...] = (
    "device_serial", "device_id", "serial", "udid", "adb_serial",
)

#: 分组来源标签，进体检报告用于解释「这个号为什么被分到这一组」。
SOURCE_PROXY = "proxy"
SOURCE_DEVICE = "device"
SOURCE_HOST = "host"
SOURCE_OVERRIDE = "override"


def _s(value: Any) -> str:
    """任意值 → 干净字符串（``None`` / 异常对象一律成空串，绝不抛）。"""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:  # noqa: BLE001 —— 账号数据来自外部库，__str__ 都可能炸
        return ""


# ── 分组 ────────────────────────────────────────────────────────────────────


def derive_group(
    account: Mapping[str, Any],
    *,
    host_tag: str = DEFAULT_HOST_TAG,
    overrides: Optional[Mapping[str, str]] = None,
) -> Tuple[str, str]:
    """给单个账号定关联组，返回 ``(组名, 来源)``。

    ``overrides`` 是运营手工维护的台账（``{account_id: 组名}``），优先级最高——设备台账
    比程序猜测更可信；键同时接受 ``account_id`` 与 ``platform:account_id`` 两种写法。

    永不抛异常：任何解析不出来的情况都落到宿主组，也就是**最保守**的那一档。
    """
    try:
        account_id = _s(account.get("account_id"))
        platform = _s(account.get("platform")).lower() or "telegram"
        account_key = f"{platform}:{account_id}"

        if overrides:
            manual = _s(overrides.get(account_id)) or _s(overrides.get(account_key))
            if manual:
                return manual, SOURCE_OVERRIDE

        # ① 独立代理＝真隔离。注册表列优先，其次 meta（有些登录流程只写进 meta）。
        meta = account.get("meta")
        meta = meta if isinstance(meta, Mapping) else {}
        proxy = _s(account.get("proxy_id")) or _s(meta.get("proxy_id"))
        if proxy:
            return f"{PROXY_GROUP_PREFIX}:{proxy}", SOURCE_PROXY

        # ② 物理设备走自己的蜂窝网络，出口天然独立于宿主机。
        #    注意必须**同时**满足「device 模式」与「拿得到设备标识」——只有 mode=device
        #    却不知道是哪台设备时，无法断言它和别人不同机，必须落回宿主组。
        if _s(account.get("mode")).lower() == "device":
            for key in _DEVICE_META_KEYS:
                serial = _s(meta.get(key))
                if serial:
                    return f"{DEVICE_GROUP_PREFIX}:{serial}", SOURCE_DEVICE

        # ③ 兜底：共享宿主出口 → 同一组 → 彼此不得同台。
        return f"{HOST_GROUP_PREFIX}:{_s(host_tag) or DEFAULT_HOST_TAG}", SOURCE_HOST
    except Exception:  # noqa: BLE001 —— 分组器自己炸也必须给出最保守的结论
        return f"{HOST_GROUP_PREFIX}:{_s(host_tag) or DEFAULT_HOST_TAG}", SOURCE_HOST


def overrides_from_config(app_config: Any) -> Dict[str, str]:
    """读运营手工关联台账 ``companion.group_show.linkage_overrides`` → ``{账号: 组名}``。

    这是 :func:`derive_group` 的 ``overrides`` 参数在配置层的落点：设备台账比程序猜测
    更可信，但它必须**写出来才算数**——写在 overlay 里可审计、可回退，且体检报告的
    ``by_source`` 会如实标成 ``override``，谁在断言「这两个号互相独立」一目了然。

    ⚠ 它断言的是**网络出口独立**这件事实，不是「我想让它们同台」这个愿望。给两个
    共享出口的号写不同组名＝向选角闸门谎报隔离，风险自负（灰度测试群可接受，
    生产群等于亲手拆掉唯一一道防关联硬闸）。

    永不抛：任何取不出来的形态都按「没有台账」处理（空表）。
    """
    try:
        node = ((app_config or {}).get("companion") or {}).get("group_show") or {}
        raw = node.get("linkage_overrides")
    except Exception:  # noqa: BLE001 —— app_config 可能是任何鸭子
        return {}
    if not isinstance(raw, Mapping):
        return {}
    out: Dict[str, str] = {}
    for acc, group in raw.items():
        key, val = _s(acc), _s(group)
        if key and val:
            out[key] = val
    return out


def derive_fingerprint_groups(
    accounts: Optional[Iterable[Mapping[str, Any]]],
    *,
    host_tag: str = DEFAULT_HOST_TAG,
    overrides: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """批量派生 ``{account_id: 关联组}``，可直接喂给 ``cast_roles(fingerprint_groups=...)``。

    键用裸 ``account_id``（选角侧两种写法都认）。同一 ``account_id`` 重复出现以首条为准。
    """
    out: Dict[str, str] = {}
    for account in (accounts or ()):
        if not isinstance(account, Mapping):
            continue
        account_id = _s(account.get("account_id"))
        if not account_id or account_id in out:
            continue
        group, _source = derive_group(
            account, host_tag=host_tag, overrides=overrides)
        out[account_id] = group
    return out


# ── 体检 ────────────────────────────────────────────────────────────────────


def linkage_readiness(
    accounts: Optional[Iterable[Mapping[str, Any]]],
    *,
    host_tag: str = DEFAULT_HOST_TAG,
    overrides: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """关联风险体检：这批号最多能几个同台，瓶颈在哪，怎么解。

    返回字段
    --------
    ``total``            参与体检的账号数
    ``groups``           ``{组名: [account_id, ...]}``
    ``max_concurrent``   **最多允许同台的号数**＝独立组数（每组只能出一个）
    ``by_source``        ``{来源: 数量}``，看清有多少号是靠宿主兜底进来的
    ``shared_host``      落在宿主兜底组里的 account_id 列表（这些号互相排斥）
    ``problems``         人话问题列表（``warn:`` 前缀＝软警告，其余＝硬伤）
    ``advice``           **可执行的解法**，见 :func:`unblock_advice`

    这份报告存在的意义：把「本机现在根本不具备多号同台条件」这件事**顶到运营面前**，
    而不是让它藏在选角结果的 ``unfilled`` 里被当成「号不够，回头再说」。而光说「演不了」
    是不负责任的，所以 ``advice`` 必须同时给出怎么才能演。
    """
    result: Dict[str, Any] = {
        "total": 0, "groups": {}, "max_concurrent": 0,
        "by_source": {}, "shared_host": [], "problems": [], "advice": [],
    }
    try:
        groups: Dict[str, List[str]] = {}
        by_source: Dict[str, int] = {}
        shared_host: List[str] = []
        seen: set = set()

        for account in (accounts or ()):
            if not isinstance(account, Mapping):
                continue
            account_id = _s(account.get("account_id"))
            if not account_id or account_id in seen:
                continue
            seen.add(account_id)
            group, source = derive_group(
                account, host_tag=host_tag, overrides=overrides)
            groups.setdefault(group, []).append(account_id)
            by_source[source] = by_source.get(source, 0) + 1
            if source == SOURCE_HOST:
                shared_host.append(account_id)

        total = len(seen)
        max_concurrent = len(groups)
        problems: List[str] = []

        if total == 0:
            problems.append("没有任何账号可供体检")
        elif max_concurrent <= 1 and total > 1:
            problems.append(
                f"{total} 个号全部落在同一关联组，最多只能有 1 个号上台——"
                f"这场戏演不起来。根因：没有任何号配了独立代理。")
        if shared_host:
            problems.append(
                f"warn: {len(shared_host)} 个号靠共享宿主出口兜底（互相排斥）: "
                f"{', '.join(shared_host[:8])}"
                f"{' 等' if len(shared_host) > 8 else ''}")

        # 配了浏览器画像却没配代理＝防护不完整，值得单独点名（容易被误以为已经安全了）
        fp_only = [
            _s(a.get("account_id")) for a in (accounts or ())
            if isinstance(a, Mapping)
            and _s(a.get("fingerprint_id"))
            and not _s(a.get("proxy_id"))
        ]
        fp_only = [x for x in fp_only if x]
        if fp_only:
            problems.append(
                f"warn: 这些号配了指纹画像但没配代理，同出口下画像挡不住关联: "
                f"{', '.join(fp_only[:8])}")

        result.update({
            "total": total,
            "groups": groups,
            "max_concurrent": max_concurrent,
            "by_source": by_source,
            "shared_host": shared_host,
            "problems": problems,
            "advice": unblock_advice(
                total=total, max_concurrent=max_concurrent,
                shared_host=shared_host),
        })
        return result
    except Exception as exc:  # noqa: BLE001 —— 体检器自己炸也要给出「不通过」结论
        result["problems"] = [f"关联体检异常，按不通过处理: {exc!r}"]
        return result


#: 一台戏起码要几个号才不像双簧（与 casting.MIN_HEALTHY_CAST 同口径，此处不 import
#: 以保持本模块零依赖）。
_DECENT_CAST = 3


def unblock_advice(
    *,
    total: int,
    max_concurrent: int,
    shared_host: Sequence[str] = (),
) -> List[str]:
    """把「演不了」翻译成「怎么才能演」，按落地成本从低到高排。

    单纯报「没配代理」等于把问题原样丢回给运营。这里给的三条路都是本仓当下真能走的：

    * **真机 + 蜂窝网**排在最前，因为它往往被忽略——兄弟仓 ``mobile-auto0423`` 已有
      Android 真机自动化，每台机走自己的移动网络，出口天然独立，不用额外买任何东西。
      条件是登记 ``mode=device`` 且 meta 里带得上设备序列号（否则本模块认不出来）。
    * **独立代理**是标准解，但要花钱且要维护可用性。
    * **降级为单号真实参与**是「今天就能做」的那条：一个号不演戏、只当个真实群友参与
      讨论。触达效率低得多，但零关联风险，且它产出的真实对话反过来能喂给剧本打磨。
    """
    advice: List[str] = []
    if total <= 0:
        return ["先在账号注册表里登记可用账号"]
    if max_concurrent >= _DECENT_CAST:
        return advice  # 够演了，不必啰嗦

    # 号本身就不够时，先说「补号」——此时谈迁真机/配代理是空话（总共就这么几个号，
    # 迁到哪儿都凑不满一台戏），照着做完了还是演不了。
    if total < _DECENT_CAST:
        advice.append(
            f"先把在线号补到至少 {_DECENT_CAST} 个（当前 {total} 个）——"
            f"号不够时，配代理也凑不满一台戏")

    # 能迁/能配的上限是「现有的号」，不能超过总数（否则会写出「把其中 2 个号迁走」
    # 而总共只有 1 个号这种自相矛盾的建议）。
    need = min(_DECENT_CAST - max_concurrent, max(total - max_concurrent, 0))
    if shared_host and need > 0:
        advice.append(
            f"把其中 {need} 个号迁到真机（mode=device + meta.device_serial），"
            f"手机走自己的蜂窝网，出口天然独立，不用买代理")
        advice.append(
            f"或给其中 {need} 个号各配一个独立代理并写进注册表 proxy_id")
    advice.append(
        "在补齐出口之前，可先降级为「单号真实参与」——一个号不演戏、只作为真实群友参与"
        "讨论，零关联风险，产出的真实对话还能反过来打磨剧本")
    return advice


def format_readiness(report: Mapping[str, Any]) -> str:
    """体检报告 → 人类可读文本（CLI / 日志用）。"""
    lines: List[str] = []
    total = report.get("total", 0)
    max_concurrent = report.get("max_concurrent", 0)
    lines.append(f"关联风险体检：{total} 个号 / {max_concurrent} 个独立关联组")
    lines.append(f"  最多可同台：{max_concurrent} 人")

    by_source = report.get("by_source") or {}
    if by_source:
        detail = "  ".join(f"{k}={v}" for k, v in sorted(by_source.items()))
        lines.append(f"  分组来源：{detail}")

    groups = report.get("groups") or {}
    for group, members in sorted(groups.items()):
        flag = " ⚠ 互斥" if len(members) > 1 else ""
        lines.append(f"    {group}: {', '.join(members)}{flag}")

    for problem in (report.get("problems") or ()):
        prefix = "  ⚠ " if str(problem).startswith("warn:") else "  ✗ "
        lines.append(prefix + str(problem).removeprefix("warn: "))

    advice = report.get("advice") or ()
    if advice:
        lines.append("  怎么解：")
        for item in advice:
            lines.append(f"    → {item}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_HOST_TAG",
    "DEVICE_GROUP_PREFIX",
    "HOST_GROUP_PREFIX",
    "PROXY_GROUP_PREFIX",
    "SOURCE_DEVICE",
    "SOURCE_HOST",
    "SOURCE_OVERRIDE",
    "SOURCE_PROXY",
    "derive_fingerprint_groups",
    "derive_group",
    "format_readiness",
    "linkage_readiness",
    "overrides_from_config",
    "unblock_advice",
]
