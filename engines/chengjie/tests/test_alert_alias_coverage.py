# -*- coding: utf-8 -*-
"""告警别名覆盖契约门禁（2026-07-30）：源码里每个 ``publish("*_alert")`` 都必须有
**具名订阅别名**可订阅,否则运营配 webhook 收不到它 = 告警报进虚空。

**为什么需要它**：``webhook_notifier._EVENT_ALIASES`` 把订阅别名映到 EventBus
event_type,运营在 ``notify_webhooks.json`` 订阅别名。若某 watchdog ``publish`` 了一个
``_alert`` 却没配具名别名,运营无论订阅哪个具名别名都收不到它——只有 ``"all"`` 能兜底,
而 AGENTS.md 明确建议「先只订阅高价值具名别名」。这类漏配**没有编译期保护、失败还
静默**:告警照常 publish、日志照常记,只是永远到不了人。

``test_alert_delivery_e2e`` 的 ``_EMITTED_ALERTS`` 是**手工登记表**(注释明说「新增发布
点务必同步维护」),2026-07-30 实测已漂移——9 个真实 ``publish`` 的告警(draft_quality/
ai_quality/realtime_voice/human_deliver/avatar_voice/colloquial_llm/orchestrator_worker/
memory_key_drift/voice_burst)没进表。手工维护不可靠,故本门禁改为**自动扫描真实源码
发布点**,杜绝「加了告警忘配别名」静默漏投。

**纯静态、零副作用**:只读源码文本做正则提取,``_EVENT_ALIASES`` 是纯数据字典
(``test_alert_delivery_e2e`` 已 import 验证过 import 安全)。

覆盖的不变量：
  1. 源码每个 ``publish("X_alert")`` 的 X_alert 都在某个具名别名(非 all)的 types 里;
  2. 提取器至少提出合理数量的发布点(防正则坏了 → 提取到 0 → 假绿);
  3. 判定纯函数 ``uncovered`` 的探测器有效性自证(合成无别名告警必被挑出)。
"""

from __future__ import annotations

import pathlib
import re

from src.inbox.webhook_notifier import _EVENT_ALIASES

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
# EventBus 告警发布：get_event_bus().publish("x_alert", ...) / bus.publish('x_alert', ...)
_PUBLISH_RE = re.compile(r"""publish\(\s*["']([a-z][a-z0-9_]*_alert)["']""")
# 消费方 / 别名定义处,不是发布点
_SKIP_FILES = {"webhook_notifier.py"}


def _emitted_alerts() -> set:
    out: set = set()
    for p in _SRC.rglob("*.py"):
        if p.name in _SKIP_FILES:
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        out |= set(_PUBLISH_RE.findall(txt))
    return out


def _named_alert_types() -> set:
    """所有具名别名(排除 all)映射到的 event_type 并集。"""
    named: set = set()
    for alias, rule in _EVENT_ALIASES.items():
        if alias == "all":
            continue
        types = rule.get("types")
        if types:
            named |= set(types)
    return named


def uncovered(emitted: set, named: set) -> set:
    """源码告警里没有具名别名的那些（纯函数,供门禁 + 探测器自证共用）。"""
    return {e for e in emitted if e not in named}


def test_alert_publish_extractor_sane():
    em = _emitted_alerts()
    assert len(em) >= 15, f"告警发布点提取异常,只得 {len(em)} 个:{sorted(em)}"
    assert "host_alert" in em and "health_alert" in em, \
        "连 host_alert/health_alert 都没提到,提取正则坏了"


def test_every_alert_has_named_subscribe_alias():
    em = _emitted_alerts()
    named = _named_alert_types()
    miss = sorted(uncovered(em, named))
    assert not miss, (
        "以下告警在源码 publish 但没有具名订阅别名——运营配 webhook 收不到(报进虚空):"
        "\n  " + "\n  ".join(miss)
        + "\n请在 src/inbox/webhook_notifier.py 的 _EVENT_ALIASES 补具名别名"
          "(不能只靠 'all' 兜底)。"
    )


def test_uncovered_detector_self_proof():
    """探测器有效性:合成一个无别名告警,必须被挑出(防判定逻辑假绿)。"""
    named = {"host_alert", "health_alert"}
    assert uncovered({"host_alert", "ghost_alert"}, named) == {"ghost_alert"}
    assert uncovered({"host_alert"}, named) == set()
