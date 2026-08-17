# -*- coding: utf-8 -*-
"""外部 API 版本生命周期登记表（纯函数，零 IO）。

**这个模块修的是一类缺陷，不是一个 bug。**

2026-08-03 实测发现两个同类实例，都属于「我们钉了一个外部 API 版本，供应商公布了
它的死期，而我们没有任何东西在跟踪那个日期」：

    Meta Graph API   告警通道钉 v19.0     官方 2026-05-21 移除   已死 74 天
    Shopify Admin    连接器钉 2024-01     官方 2025-01-16 到期   已死 564 天

这类缺陷有三个共同点，正是本模块要处理的：
1. **不在部署期报错**——版本过期只在真正发请求那一刻才有反应；
2. **两年尺度**——出问题时最初写代码的人早就不记得有这回事；
3. **没有单一位置能回答「我们钉的版本还能活多久」**。

而它们的**失败模式并不相同**，所以修复方式也不同（见 :attr:`ApiPin.failure_mode`）：
Meta 过期是硬 4xx（吵，但只在调用时吵）；Shopify 过期**根本不报错**，静默
fall-forward 到最老的受支持版本——破坏性变更被悄悄应用，日志里什么都看不到。
后者更阴险：不会有任何信号提示你「你以为在用 2024-01，其实在用别的」。

★ 为什么是登记表而不是「各家各写一个常量」★
共性其实只有三样：钉的版本、公布的死期、把时间变成 CI 红灯。版本格式（``v25.0``
vs ``2026-07``）、失败模式、修复话术全都不同——那些当**数据字段**处理，而不是长成
代码分支。所以本模块很薄：一个 dataclass、一套档位算法、一张登记表。

★ 登记表自己也会漏登记，那就等于把原 bug 搬到上一层 ★
所以配套 :data:`LITERAL_RULES`：每个条目带一条「这类版本号长什么样」的正则和
「只允许出现在哪个文件」的白名单，门禁据此全库扫描——**新加一个钉死的外部版本却
忘了登记，会当场变红**。登记表因此是「构造上完整」的，而不是靠人记得维护。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

#: 距到期少于这么多天＝必须动手（门禁硬失败）。
#: 60 天的依据：升一个外部 API 版本要读 changelog、评估破坏性变更、改代码、跑回归，
#: 两个月是「从容处理」与「过度提前」之间的分界。
CRITICAL_DAYS = 60

#: 进入提醒区间（尚不失败，但应排期）。
WARN_DAYS = 180

#: 失败模式——决定过期后会发生什么，进而决定修复的紧迫性与话术。
FAIL_HARD = "hard_error"            # 过期即报错（Meta：4xx）
FAIL_SILENT = "silent_fallforward"  # 过期不报错，静默换版本（Shopify）
FAIL_UNKNOWN = "unknown"            # 供应商未公布行为


@dataclass(frozen=True)
class ApiPin:
    """我们钉住的一个外部 API 版本。"""

    key: str            # 稳定标识，如 "meta_graph"
    label: str          # 人读名字
    version: str        # 当前钉的版本
    eol: Optional[date] # 公布的到期日；None 的含义见 eol_unpublished
    failure_mode: str
    owner: str          # 该版本的单一事实源模块（出事时去哪儿改）
    doc_url: str
    remediation: str    # 门禁变红时给人看的下一步

    #: ``eol is None`` 有两种截然不同的原因，**混起来会让门禁对笔误放行**：
    #: True  ＝供应商压根不公布死期（Discord），这是事实陈述，健康；
    #: False ＝我们在自家到期表里**没查到**这个版本——要么写错了，要么是早已退役的
    #:         老版本（``2024-01`` 就是后者）。判为 ``unknown``（不健康），门禁必红。
    eol_unpublished: bool = False


@dataclass(frozen=True)
class PinStatus:
    """某个 pin 此刻的健康状态。"""

    pin: ApiPin
    days_left: Optional[int]
    #: ``ok`` | ``warn`` | ``critical`` | ``expired`` | ``unpublished``
    level: str

    @property
    def healthy(self) -> bool:
        return self.level in ("ok", "unpublished")


def status_for(
    eol: Optional[date], today: Optional[date] = None,
) -> Tuple[Optional[int], str]:
    """由到期日算 ``(剩余天数, 档位)``。各家共用同一套档位算法。

    到期**当天**算 ``critical`` 而非 ``expired``（不多算一天，边界向宽）。
    """
    if eol is None:
        return None, "unpublished"
    days = (eol - (today or date.today())).days
    if days < 0:
        return days, "expired"
    if days < CRITICAL_DAYS:
        return days, "critical"
    if days < WARN_DAYS:
        return days, "warn"
    return days, "ok"


def status_in_table(
    version: str,
    table: Dict[str, Optional[date]],
    today: Optional[date] = None,
) -> Tuple[Optional[date], Optional[int], str]:
    """在某家的官方到期表里查版本，返回 ``(到期日, 剩余天数, 档位)``。

    ★ ``unknown`` 与 ``unpublished`` 必须分开，混为一谈会漏掉真事故 ★
    ``unpublished`` ＝在表里、供应商还没公布死期（真的健康）；
    ``unknown`` ＝**不在表里**，要么是笔误，要么是早已退役的老版本（运维配置里
    塞进来的 ``2024-01`` 就是后者）。若把两者都当「没有死期＝健康」，一个退役到
    18 个月的版本会被门禁直接放行——这正是本模块要抓的那种静默。
    """
    ver = str(version or "").strip().lower()
    if ver not in table:
        return None, None, "unknown"
    eol = table[ver]
    days, level = status_for(eol, today)
    return eol, days, level


@dataclass(frozen=True)
class LiteralRule:
    """「这类版本号长什么样 + 只许出现在哪」——完整性门禁的判据。"""

    key: str
    #: 匹配**真实字面量**的正则（``v\\d+`` 这种正则源码不该被它命中）
    pattern: str
    #: 允许出现该字面量的文件（相对 src/），一般就是那家的 SSOT
    owner_files: Tuple[str, ...]
    hint: str


#: 每类外部版本号的字面量特征。新增一家外部 API 时，**登记表和这张表要一起加**，
#: 否则完整性门禁会因为「扫到了没登记的版本号」而变红——这正是设计意图。
LITERAL_RULES: Tuple[LiteralRule, ...] = (
    LiteralRule(
        key="meta_graph",
        pattern=r"graph\.facebook\.com/v\d+\.\d+",
        owner_files=("integrations/meta_graph_version.py",),
        hint="改用 meta_graph_version.graph_base(<product>)",
    ),
    LiteralRule(
        key="meta_graph",
        pattern=r"""GRAPH_API_VERSION\s*=\s*["']v\d+\.\d+["']""",
        owner_files=("integrations/meta_graph_version.py",),
        hint="改用 meta_graph_version.version_for(<product>)",
    ),
    LiteralRule(
        key="shopify_admin",
        pattern=r"/admin/api/20\d{2}-\d{2}",
        owner_files=("ecommerce_tools/shopify_connector.py",),
        hint="改用 shopify_connector.DEFAULT_API_VERSION",
    ),
    LiteralRule(
        key="discord",
        pattern=r"discord\.com/api/v\d+",
        owner_files=("integrations/discord_bot_login.py",),
        hint="改用 discord_bot_login.API_BASE",
    ),
)


def collect_pins() -> List[ApiPin]:
    """汇总当前生效的全部 pin。

    **刻意不用装饰器/import 期自动注册**：那种写法的登记结果取决于「谁 import 了
    谁」，某个模块碰巧没被导入时它的 pin 就静默缺席——而门禁看到的是一张「看起来
    很干净」的表。那恰好是本模块要消灭的静默失败。这里用**显式罗列**，缺谁一眼可见。

    函数内 import 是为了避开循环依赖：各家 SSOT 需要 :func:`status_for`，若本模块
    在顶层反向 import 它们就成环。只有这一处，且只在被调用时发生。
    """
    from src.ecommerce_tools import shopify_connector
    from src.integrations import discord_bot_login, meta_graph_version

    pins: List[ApiPin] = []
    pins.extend(meta_graph_version.pins())
    pins.extend(shopify_connector.pins())
    pins.extend(discord_bot_login.pins())
    return pins


def health_report(today: Optional[date] = None) -> Dict[str, PinStatus]:
    """全部在用外部版本的健康快照：``{key: PinStatus}``。

    门禁与（将来的）看板读同一个函数，才不会出现「门禁绿着而看板红着」。
    """
    out: Dict[str, PinStatus] = {}
    for pin in collect_pins():
        if pin.eol is None and not pin.eol_unpublished:
            # 查不到死期 ≠ 没有死期。见 ApiPin.eol_unpublished。
            out[pin.key] = PinStatus(pin, None, "unknown")
            continue
        days, level = status_for(pin.eol, today)
        out[pin.key] = PinStatus(pin, days, level)
    return out


def worst_level(today: Optional[date] = None) -> str:
    """整体取最差档（一处过期就是过期，不被健康的条目平均掉）。"""
    order = ["ok", "unpublished", "warn", "critical", "unknown", "expired"]
    levels = [s.level for s in health_report(today).values()]
    return max(levels, key=order.index) if levels else "ok"
