"""营销目标编排层（marketing goals）。

「编排层而非新引擎」：目标 = 声明式意图（模板 + 参数 + 截止），推进复用既有
四套引擎——关系事实源（contacts/intimacy）、变现权益（monetization）、
回复生成（skill_manager 注入 ``_goal_block``）、主动触达（proactive_topic 桥）。

模块分工：
- ``templates``     模板注册表（纯数据：弧线/意图池/力度曲线）
- ``store``         SQLite 持久层（goals / goal_actions / goal_events）
- ``signals``       信号采集（settle-on-read；读现有事实源，绝不新建状态）
- ``planner``       每日拍规划（纯函数：情绪 hold / 沉默熔断 / 退避陪伴 / 意图轮换）
- ``ledger``        结算器（纯函数：完成/过期/里程碑/进度，单调不回退）
- ``context_block`` ``_goal_block`` 构建（≤3 行 + 恒在纪律行）
- ``service``       编排单入口（refresh_goal / goal_view / build_block_for_chat）
- ``stats``         进程级观测（/api/workspace/metrics.goals + Prometheus goals_*）
- ``bridge``        P1 主动触达桥（默认关；auto 档目标搭 proactive_topic 顺风车）

默认关（``companion.goals.enabled: false``）；开启后也只有**手动建了目标的会话**
才有任何行为变化。
"""

from src.companion.goals.templates import (  # noqa: F401
    AUTONOMY_LEVELS,
    GOAL_STATUSES,
    PUSH_LEVELS,
    get_template,
    list_templates,
)
