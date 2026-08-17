# -*- coding: utf-8 -*-
"""Meta Graph API 版本的单一事实源（纯函数，零 IO）。

Meta 的 Graph API 按季度发版、**每个版本约两年后被移除**（不是弃用警告，是直接
404/错误）。我们有三处调它：Messenger Page 收发、Instagram DM 收发（复用前者的
base）、WhatsApp Cloud 收发，另有告警外发通道会往 Messenger 推消息。

**为什么需要这个模块，而不是各文件各写一个常量**——2026-08-03 实测的真实状态：

    facebook_webhook.py   v25.0   到期 2028-07-29   还行
    whatsapp_cloud.py     v21.0   到期 2027-01-21   剩 171 天
    webhook_notifier.py   v19.0   到期 2026-05-21   **已死 74 天**

三个文件三个版本，其中一个在两个半月前就停用了，而**没有任何东西会告诉我们**：
Graph API 版本过期不会在部署时报错，只在真正发请求那一刻返回错误——而告警通道恰恰
是「平时零流量、出事才发一条」的路径，于是「出事时告警发不出去」这种最坏组合会一直
潜伏到某次真的需要它。版本号写在哪个文件里不是重点，**「没有一个地方能回答『我们钉
的版本还能活多久』」才是缺陷本身**。

所以本模块提供两样东西：钉哪个版本（:data:`PRODUCT_VERSIONS`），以及每个版本官方
公布的死期（:data:`VERSION_EXPIRY`）。有了后者，门禁就能在到期前若干天把 CI 变红，
把「两年后某天线上静默失败」换成「今天有人被点名去 bump 一行」。

**刻意不做成配置项**：本模块修的就是「同一个事实散落在多处、各自漂移」，加一个
config 覆写等于又开一个漂移入口，且运营没有任何理由需要在运行时改 Graph 版本
（改了也无法验证）。需要换版本就改这里的常量，门禁自然覆盖新值。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

from src.utils import external_api_lifecycle as _lifecycle

# ── Meta 官方公布的版本生命周期 ──────────────────────────────────────────────
# 来源：https://developers.facebook.com/docs/graph-api/changelog/versions/
# 值为 None＝官方尚未公布死期（最新版发布初期都是 TBD）。**别自己按「发布+2年」
# 推算填进去**：推算值和公布值长得一模一样，但门禁会拿它当事实用，等于把猜测洗成
# 结论。None 由 :func:`version_status` 显式当「未知」处理。
VERSION_EXPIRY: Dict[str, Optional[date]] = {
    "v13.0": date(2024, 5, 28),
    "v14.0": date(2024, 9, 17),
    "v15.0": date(2024, 11, 20),
    "v16.0": date(2025, 5, 14),
    "v17.0": date(2025, 9, 12),
    "v18.0": date(2026, 1, 26),
    "v19.0": date(2026, 5, 21),
    "v20.0": date(2026, 9, 24),
    "v21.0": date(2027, 1, 21),
    "v22.0": date(2027, 5, 20),
    "v23.0": date(2027, 10, 8),
    "v24.0": date(2028, 2, 18),
    "v25.0": date(2028, 7, 29),
    "v26.0": None,  # 2026-07-29 发布，死期未公布
}

#: 默认钉的版本。
#:
#: 取 v25.0 而非当时最新的 v26.0 是刻意的：v26.0 发布于 2026-07-29（本模块建立前
#: 5 天）且带着破坏性变更（Commerce Order Management 在 v26+ 直接被拦），而 v25.0
#: 已经是 `facebook_webhook` 在生产上真跑着的版本、死期 2028-07-29 还有约两年跑道。
#: 「跟最新」在这里没有收益，只有未验证的风险——版本治理要的是**可控的跑道**，
#: 不是版本号大。
DEFAULT_VERSION = "v25.0"

#: 按产品钉版本。默认全部跟随 :data:`DEFAULT_VERSION`；只有当某个产品确实需要
#: 与其他产品错开时（例如某版本对该产品有破坏性变更，需要滞后迁移）才在此显式
#: 写死。**保持这里尽量空**——每多一个条目就多一条要单独盯死期的线。
PRODUCT_VERSIONS: Dict[str, str] = {}

#: 档位阈值与判定算法收归 :mod:`src.utils.external_api_lifecycle`——Shopify 等其他
#: 外部 API 是同一类问题（钉版本 + 有公布死期），档位算法各写一份必然漂移。
#: 这里保留同名常量只为向后兼容既有引用。
CRITICAL_DAYS = _lifecycle.CRITICAL_DAYS
WARN_DAYS = _lifecycle.WARN_DAYS


def version_for(product: str = "") -> str:
    """取某产品应使用的 Graph API 版本（未单独钉版本即回落默认）。"""
    return PRODUCT_VERSIONS.get(str(product or "").strip().lower(), DEFAULT_VERSION)


def graph_base(product: str = "") -> str:
    """``https://graph.facebook.com/<版本>``，各集成拼端点用的唯一入口。"""
    return f"https://graph.facebook.com/{version_for(product)}"


@dataclass(frozen=True)
class VersionStatus:
    """某个版本此刻的健康状态。"""

    version: str
    expiry: Optional[date]
    #: 剩余天数；已过期为负数；死期未公布为 None
    days_left: Optional[int]
    #: ``ok`` | ``warn``（临近）| ``critical``（迫近）| ``expired`` | ``unknown``
    #: （版本不在官方表里，多半是写错了）| ``unpublished``（官方尚未公布死期）
    level: str

    @property
    def healthy(self) -> bool:
        """能否安心继续用。``unknown`` 算不健康——不在官方表里的版本号八成是笔误，
        当作健康会让门禁对真正的错误闭嘴。"""
        return self.level in ("ok", "unpublished")


def version_status(version: str, today: Optional[date] = None) -> VersionStatus:
    """判定单个版本的健康状态（纯函数，``today`` 可注入以便单测）。"""
    ver = str(version or "").strip().lower()
    exp, days, level = _lifecycle.status_in_table(ver, VERSION_EXPIRY, today)
    return VersionStatus(ver, exp, days, level)


def pinned_versions() -> Dict[str, str]:
    """当前生效的全部版本：``{产品或 "default": 版本}``。门禁与看板的取数入口
    ——两边读同一个函数，才不会出现「门禁绿着而看板红着」。"""
    out: Dict[str, str] = {"default": DEFAULT_VERSION}
    out.update(PRODUCT_VERSIONS)
    return out


def health_report(today: Optional[date] = None) -> Dict[str, VersionStatus]:
    """全部在用版本的健康快照。"""
    return {k: version_status(v, today) for k, v in pinned_versions().items()}


def worst_level(today: Optional[date] = None) -> str:
    """整体取最差档（一处过期就是过期，不因为别处健康而被平均掉）。"""
    order = ["ok", "unpublished", "warn", "critical", "unknown", "expired"]
    levels = [s.level for s in health_report(today).values()]
    return max(levels, key=order.index) if levels else "ok"


def pins() -> List[_lifecycle.ApiPin]:
    """向通用生命周期登记表申报 Meta 侧钉住的版本。

    有 per-product 覆写时逐个申报——覆写的存在意义就是「这个产品跟大部队不同步」，
    合并成一条会正好把那个差异藏起来。
    """
    out: List[_lifecycle.ApiPin] = []
    for product, ver in pinned_versions().items():
        out.append(
            _lifecycle.ApiPin(
                key="meta_graph" if product == "default" else f"meta_graph:{product}",
                label=f"Meta Graph API（{product}）",
                version=ver,
                eol=VERSION_EXPIRY.get(ver),
                failure_mode=_lifecycle.FAIL_HARD,
                owner="src/integrations/meta_graph_version.py",
                doc_url="https://developers.facebook.com/docs/graph-api/changelog",
                remediation=(
                    "把 DEFAULT_VERSION 提到新版本，并按官方 changelog 把新版到期日"
                    "补进 VERSION_EXPIRY（照抄官方公布值，别按发布+2 年自己推算）。"
                ),
            )
        )
    return out
