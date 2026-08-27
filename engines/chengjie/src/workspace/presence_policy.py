"""坐席接待状态（presence）→ 行为语义的单一事实源（2026-08-19）。

此前「忙碌该不该派单」散落在 assignment 的配置默认值里、「该不该占席位」写死在
presence 路由的 if 里、「名单显不显示」在内存/SQLite 两个分支各有一套——四处各自
演绎，改一处其他处不知道。本模块把状态语义钉成纯函数，派单/席位/名单统一消费；
门禁 ``tests/test_presence_policy.py`` 钉住矩阵与消费方接线。

行为矩阵（UI 文案见 i18n ``base.presence.*``；「离开」= 值 ``offline`` 的显示语义）：

============  ========  ==========  ==========================
状态           自动派单   计授权席位   备注
============  ========  ==========  ==========================
online 在线    ✅         ✅          正常接待
busy   忙碌    ❌*        ✅          * auto_assign.online_only=False 时可放宽为 ✅；
                                     人在满功能使用软件 ⇒ 必须计席（2026-08-19 前
                                     只有 online 计席，busy 是授权绕过缝隙）
offline 离开   ❌         ❌          手选暂离；心跳仍在（last_seen 刷新）故名单如实
                                     显示为「离开」，与断线（心跳超时不入名单）区分
============  ========  ==========  ==========================

断线（>presence_stale_sec 无心跳）不是一个 status 值：它表现为「从名单消失」，
SLA 再分配（sla_watcher._check_reassign）按 last_seen_at 判定，与本矩阵正交。
"""
from __future__ import annotations

from typing import Any

VALID_STATUS = {"online", "busy", "offline"}


def normalize_status(status: Any) -> str:
    """任意输入归一为合法状态；空/非法一律按 offline（保守：不派单不计席）。"""
    st = str(status or "").strip().lower()
    return st if st in VALID_STATUS else "offline"


def accepts_new_assignments(status: Any, *, allow_busy: bool = False) -> bool:
    """该状态的坐席是否参与新会话自动派单/自动认领。

    ``allow_busy`` 对应 ``workspace.auto_assign.online_only=False``（放宽档）；
    「离开」在任何档位都不参与。
    """
    st = normalize_status(status)
    if st == "online":
        return True
    return st == "busy" and bool(allow_busy)


def counts_license_seat(status: Any) -> bool:
    """该状态是否占用授权席位。

    在线/忙碌都是「人在用软件」⇒ 都计席；「离开」让出席位（暂离者不挤占同事上线）。
    """
    return normalize_status(status) in {"online", "busy"}


def roster_visible(status: Any) -> bool:
    """心跳窗口内的坐席是否进「坐席名单」。

    三态一律如实显示（含手选「离开」——显示为离开比凭空消失更可信；
    真正消失的唯一途径是心跳超时=断线）。内存回落分支此前剔除 offline、
    SQLite 分支不剔，两个分支行为分叉——统一为本函数口径。
    """
    return normalize_status(status) in VALID_STATUS
