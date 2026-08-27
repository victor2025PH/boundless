# -*- coding: utf-8 -*-
"""用户报障闭环门禁（实施73 P0，2026-08-27）。

小智的三大定位之一是**报障通道**：用户点报障 → 截图 → 提交 → 客服/开发收到 →
修复 → 引导更新。这条链上有两类缺陷**运行时完全静默**，只能靠静态门禁兜住：

1. **发布了但没人订阅**：`assistant_routes` 每次报障成功都
   `publish("assistant_report_alert")`，别名 `levels=None` 不做等级过滤——
   所以「收不到」百分之百是订阅侧的洞。而这两个别名在 `_TECHNICAL_ALERTS`
   而非 business 目录，2026-08-27 之前**不在关注别名集里**：自检 CLI、ops
   「🔔 告警链路」卡、健康灯三处都会对「报障进虚空」判 healthy。
   实测 zhiliao：tg-ops 通道启用、订阅 25 个运维别名，唯独这两个不在。

2. **状态机双源漂移**：前端 `stLabel` 自带一张状态→文案表，后端
   `bug_intake.VALID_STATUSES` 是真相。漂移的后果不是报错，是用户在「我的
   工单」里看到英文原文 `in_progress`——修复前实况就是前端有后端不存在的
   `collecting`/`wontfix`，而后端真实存在的 `in_progress`/`closed` 没映射。

另钉提交后的**预期管理文案**：只给工单号不说「接下来会发生什么」，用户要么
反复提交要么放弃——这是报障通道最典型的信任流失点。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BALL = ROOT / "shared" / "assistant" / "assistant-ball.js"
ASSISTANT_ROUTES = ROOT / "src" / "web" / "routes" / "assistant_routes.py"


def _ball() -> str:
    return BALL.read_text(encoding="utf-8")


# ── 1. 报障别名必须在关注集里（否则「进虚空」不可见）────────────────────────

def test_report_aliases_are_high_value():
    """两个用户报障别名必须进高价值关注集。

    它们不在 business 受众目录（属 technical），所以**只能**靠
    HIGH_VALUE_ALIASES 进入 `default_focus_aliases()`；掉出去 = 自检重新失明。
    """
    from src.integrations.alert_link_audit import (
        HIGH_VALUE_ALIASES,
        default_focus_aliases,
    )

    focus = default_focus_aliases()
    for alias in ("assistant_report", "bug_intake"):
        assert alias in HIGH_VALUE_ALIASES, (
            f"{alias} 掉出 HIGH_VALUE_ALIASES——用户报障没人订阅时自检会说 healthy"
        )
        assert alias in focus, f"{alias} 未进关注别名集"


def test_report_aliases_are_registered_events():
    """别名必须真实存在于 notifier 的事件表，否则订阅了也匹配不到。"""
    from src.inbox.webhook_notifier import _EVENT_ALIASES

    for alias, ev in (("assistant_report", "assistant_report_alert"),
                      ("bug_intake", "bug_intake_alert")):
        assert alias in _EVENT_ALIASES, f"{alias} 不在 _EVENT_ALIASES"
        assert ev in (_EVENT_ALIASES[alias].get("types") or set()), (
            f"{alias} 未映射到事件 {ev}"
        )


def test_assistant_report_publish_is_unconditional():
    """报障发布不得被加上等级/条件闸门。

    TG 群那条链（`bug_intake_alert`）刻意只发 P0/P1 防轰炸，但 web 报障是
    **用户主动提交**的单条事件，加 severity 闸=多数工单静默丢失。
    """
    src = ASSISTANT_ROUTES.read_text(encoding="utf-8")
    assert '"assistant_report_alert"' in src, "assistant_report_alert 发布点消失"
    # 发布点不得被 severity/等级判断包住（那是 TG 群链刻意的防轰炸设计，
    # 搬到 web 链上＝用户主动提交的单条事件被静默丢弃）
    assert not re.search(r'if[^\n]*severity[^\n]*:\s*\n\s*.*publish\(\s*\n?\s*'
                         r'"assistant_report_alert"', src), (
        "assistant_report_alert 发布被 severity 条件包住"
    )
    from src.inbox.webhook_notifier import _EVENT_ALIASES
    assert _EVENT_ALIASES["assistant_report"].get("levels") is None, (
        "assistant_report 别名被加了 levels 过滤——web 报障会按等级静默丢弃"
    )


# ── 2. 状态机单源（前端表必须与后端 VALID_STATUSES 逐字一致）────────────────

def test_status_labels_match_backend_states():
    """`stLabel` 的键集 == `bug_intake.VALID_STATUSES`（不多不少）。

    多（如曾有的 collecting/wontfix）= 死代码假装支持；
    少（如曾漏的 in_progress/closed）= 用户看到英文原文状态。
    """
    from src.ops.bug_intake import VALID_STATUSES

    src = _ball()
    m = re.search(r"function stLabel\(st\)\s*\{\s*var map = \{(.*?)\};",
                  src, flags=re.S)
    assert m, "assistant-ball.js 找不到 stLabel 的状态映射表"
    keys = set(re.findall(r"'?([a-z_]+)'?\s*:", m.group(1)))
    assert keys == set(VALID_STATUSES), (
        f"前端状态表 {sorted(keys)} != 后端 VALID_STATUSES "
        f"{sorted(VALID_STATUSES)}——多余键是死代码，缺失键让用户看到英文原文"
    )


def test_status_labels_have_zh_en_entries():
    """每个状态的 i18n 键必须 zh/en 各一份（裸键回退对用户是乱码）。"""
    from src.ops.bug_intake import VALID_STATUSES

    src = _ball()
    for st in VALID_STATUSES:
        key = "st_" + st
        n = len(re.findall(rf"\b{key}\s*:", src))
        assert n >= 2, f"状态文案 {key} 定义 {n} 次 < 2（zh/en 各需一份）"


# ── 3. 提交后的预期管理 ──────────────────────────────────────────────────────

def test_submit_success_sets_expectations():
    """成功回执必须回答「接下来会发生什么」，不能只给一个工单号。"""
    src = _ball()
    assert re.search(r"\brp_next\s*:", src), "缺 rp_next 预期管理文案"
    assert len(re.findall(r"\brp_next\s*:", src)) >= 2, "rp_next 缺 zh/en 之一"
    assert "t('rp_next')" in src, "rp_next 未被渲染到提交成功回执里"


# ── 4. 答不上来必须有出路（知识缺口 → 报障线索）────────────────────────────

def test_zero_hit_always_offers_report_entry():
    """检索零命中时必须给报障入口，不能只丢一句「不知道」。

    2026-08-27 实况：零命中分支的注释写着「诚实说不知道 + 报障入口」，但
    `report_hint` 只由问题里的 bug 关键词（报错/闪退/crash…）触发——用户问
    「XX 页怎么用」而语料没货时，回答是一句死路。而「答不上来」正是最该转成
    工单线索的时刻：它同时是用户的出路和我们补 how-to 语料的照单。
    """
    src = ASSISTANT_ROUTES.read_text(encoding="utf-8")
    assert 'bool(report_hint or not strong)' in src, (
        "零命中未强制给报障入口——知识缺口会变成用户的死路，"
        "而且这条缺口也不会变成任何可追踪的线索"
    )


# ── 5. 环境块要带定位必需字段 ────────────────────────────────────────────────

def test_env_block_carries_version_and_machine():
    """报障环境块必须带**应用版本**与**机器码**。

    UA 里只有 Electron/Chrome 版本，没有本产品版本号 → 「这个 bug 在哪个版本
    上」全靠猜；机器码是坐席机唯一标识，客服按它找机器。两者服务端直接可取
    （与 /api/support/info 同源），刻意不信任客户端上报值。
    """
    src = ASSISTANT_ROUTES.read_text(encoding="utf-8")
    assert "from src.utils.app_identity import app_version" in src, (
        "报障环境块未采集应用版本"
    )
    assert "from src.utils.diag_upload import machine_code" in src, (
        "报障环境块未采集机器码"
    )
    assert "应用版本" in src and "机器码" in src, "环境块缺版本/机器码标签"
