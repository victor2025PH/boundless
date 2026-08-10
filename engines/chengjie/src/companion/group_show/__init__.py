"""群脉 CrowdX —— 多号群戏编排（内部代号 ``group_show``）。

**定位**：一个「导演」调度多个 AI 账号，在同一个群里按剧本演一场看起来完全自然的
群聊，把产品软性种进去。它不是「多个号发广告」——那一眼假、当场被举报封号。

**与 1:1 陪伴链路的关系**：共底座、不串味。每个号发言仍走各自人设 + 各自独立记忆窗，
导演只下「意图指令」（intent directive）不写死台词；台词由该号人设 LLM 现场生成，
所以同一剧本每次演出台词都不同——从根上消灭「模板化重复」这个头号封号特征。

**四条铁律**（贯穿本包所有模块）：

1. **共底座不串味**：``ecp.py`` 的自我视角投影保证每个号只看得到「群里公开发生的事」，
   绝不注入别的号的 1:1 私聊记忆（账号隔离墙不破）。
2. **发送唯一出口**：所有发言必经 ``AccountOrchestrator.send``（自带 ``send_blocked``
   护栏＝Kill-Switch/金丝雀/限额/License），绝不旁路自己发。
3. **确定性可测**：选角 / 节奏 / 视角投影 / 自然度全部纯函数，可离线 dry-run 打分。
4. **合规分级**：沙箱与自建群＝绿区；有真实用户流量的群＝红区**硬禁演**（只观察）。

**两条驱动、一套决策**（``runtime.py`` 排练 / ``live.py`` 真发）。排练跑完整导演循环与真实
台词生成、但**物理上没有出口**（模块内不 import 任何发送件，门禁扫源码钉死）；真发是另一条
驱动，出口靠调用方注入。刻意不做成「同一个循环加个 ``dry_run`` 开关」——那种形状里，一次
参数默认值写错就是几十个真人群里的广告，而它在代码审查中看起来只是个布尔。

代价是两条循环会**漂**：排练里四个号演得热热闹闹、真发只有两个能上，预览是假的而它看起来
完全正常。所以有一条门禁按**同种子对等性**钉住——同一剧本 + 同一号池 + 同一时钟，两条驱动
必须产出逐字相同的（发言人, 台词, 时刻）序列。要么一起改，要么门禁红。

真发独有的四件事（排练里没有对应物，因为它们的代价只在真群里存在）：**双锁**（调用点显式
``confirm_live=True`` ∧ 部署级配置开关，缺一不发）；**改不动的硬顶**（条数/时长/迭代次数写死
在模块里，配置调不动——它兜的正是「配置写错」这个场景）；**拒绝降级**——排练缺人时导演会拿
别人顶上（预览效果要完整），真发缺人则整场拒演：一个号连着扮四个人自问自答，是比不演出戏更
坏的结果；**禁演时段**——排期器排表时早已绕开安静时段，但导播台上「现在演一场」前面没有任何
一张表，凌晨三点一群人围着夸同一个产品不需要任何统计就能看出来。

禁演只在**开演那一刻**判，刻意不做中途硬切：22:50 开的戏聊到 23:10 完全是真人样子，而
「每场都在 23:00:00 整齐收声」本身就是个更刺眼的机器指纹。窗口配 ``[0, 0]`` ＝ 解除（让
「关闸门」和「配窗口」共用一个配置项——两个开关一定会出现「窗配了但没打开」这种状态，而它
看起来跟生效了一模一样）。

拒演信息必须带**归因**。四条护栏都会让角色空着，可处置完全相反：号不够要补号，预算压的是
护栏在正常工作（不该动），号对到线要换一批号去铺，主推到线要把种草角色让给别人。只报
「缺角 advocate」时运营最自然的动作是去买号——而那在后三种原因下是反的。

**开演前置条件（铁律 4 的量化版）**：真发前有**两条正交的关联轴**，都要过。

*轴一：同群内的网络出口*（``linkage.py``）。一场戏能上几个号，上限是
``linkage_readiness()`` 算出的 ``max_concurrent``（＝独立网络出口数），**不是**在线号数。
同一出口下的多个号在同一群里一唱一和，是关联封号最典型的姿势。真发路径必须先跑体检、按
``derive_fingerprint_groups()`` 派生的表选角，且**不得**使用
``cast_roles(allow_shared_host=True)``——那个开关只为离线预览而存在。

*轴二：跨群的成员重合*（``attendance.py``，多号多群模式下的**头号**特征）。同一批号同时
是同一批群的成员，平台一次成员表比对就能算出来，群主肉眼也可查，而且真人身上不会出现。
它比轴一强得多：出口可以买代理解决，「这 10 个号共享 100 个群」买什么都解决不了。正解是
``plan_attendance()`` 排稀疏矩阵（每群只派号池的一个子集）+ ``rotate_roles()`` 跨群轮换
角色 + ``schedule_joins()`` 摊开加群时间轴（**同一个群每天只进一个号**）。

轴二有个必须遵守的用法：排班要喂**出席台账**（``GroupShowStore.memberships()`` →
``split_ledger()`` → ``existing`` / ``history``）。加群是跨天的手工动作，分批铺是常态；
只算当前这批 → 每批都显示「安全」，而这些号身上累积的共同出席早已超标。历史群不进排班表，
但它们贡献的号对计数照进度量——报的是**总共**共享几个群，不是这一批共享几个。

*轴三：跨群的**发言**重合*（``performance.py``）。轴二管「谁在群里」，轴三管「谁开过口」。
两者形状相同、算法共用，但性质相反：成员关系一旦加群就**冻结**（集体退群本身更可疑），
而「这一场让谁开口」**每场都能重排**。平台侧地位也不同——成员表要专门拉出来比对才暴露，
消息却每条都在实时流过反垃圾管道。所以轴二排坏了不等于完蛋，轴三是那之后唯一还能动的面。

轴三给出了本子系统**最强的那个杠杆**：共现对次是 ``群数 × C(每场开口人数, 2)``，对开口
人数是二次的。10 个号每场 3 人开口只能安全覆盖 30 个群，**降到 2 人就能覆盖 90 个**——
少让一个号开口比多养五个号还管用，且立刻生效。用法：``performance_metrics()`` 读演出面、
``exposure_report()`` 出成员/演出双读数与可执行建议、``speaker_budget()`` 算每场开口人数
上限，再把 ``co_performance()`` 与 ``max_speakers`` 喂给 ``cast_roles()`` 落到选角上。

轴三的护栏分两层，**缺一不可**：``resolve_speaker_cap()`` 把预算落成本场人数上限（预测性——
在号对还没顶线之前就别让它顶）；``admits_speaker()`` 按号对余量做准入闸门（反应性——认真账，
兜住「预算是拿陈旧台账算的」）。闸门默认开着（``cast_roles(co_limit=NORMAL_PAIR_CO)``），
但**没喂台账时恒不生效**，所以「默认安全」不以改动既有行为为代价。

「最坏一对共享 8 个群」这个数本身没法执行——运营既不知道是哪两个号，也不知道该动哪几个群。
``hot_pairs()`` 把它下钻成「哪两个号 · 缠在哪几个群 · 闸门还剩几个群余量 · 现在是否已被拦」，
并**把闸门的当前判定与证据放在同一行**：否则会出现最伤信任的那种错位——卡片红着、运营手动
躲着这两个号，其实闸门早就在拦了（白躲）；或者卡片红着他以为被拦了，实际台账是空的、闸门
根本没生效。

轴三的台账必须取**两个来源的并集**：``GroupShowStore.performance_ledger()``（编排出来的戏，
**排练不算、真人插话不算**）+ ``InboxStore.group_speech_ledger()``（收件箱里所有群内出向
消息＝日常自动回复 / 坐席手发 / 主动触达）。只读前者 ⇒「一场戏没演过就报安全」，而号很可能
早就靠日常回复在几十个群里同框了——平台数的是消息，不区分是不是编排的。**假安全**是风险
读数最不该犯的错。

*轴四：单号的角色集中度*（``roles.py``）。前三条全是**号对**口径——它们回答「这几个号是不是
一伙的」。但把一个号认成托根本不需要先找出它的同伙：它在 40 个群里都是那个「我用了三个月真
的有效」的人，翻一下历史发言就完了。这条与号对无关，所以矩阵排得再稀疏也拦不住。关键分寸是
**只管推销角色**（``SELLING_SLOTS``）：路人/提问/质疑是掩护，铺再多群都是真人行为，把它们
一起纳管会先耗尽掩护角色、逼运营关掉整条护栏。这条轴**买不到杠杆**——每场种草恰好一个主推，
容量是 ``号数 × NORMAL_ROLE_CO``，线性，只能靠加号或让部分场次干脆不排主推。

*轴五：时间节奏*（``schedule.py``）。静态特征都洗干净之后，节奏是最后一条还会出卖人的轴：
同号前后脚在两个群冒头、一批群挤在同一个小时、精确每 30 分钟一场、每天同一时刻。第四条**默认
会发生**（纯函数同输入同输出），所以 ``plan_show_times()`` 把当天零点拌进抖动种子——跨天自动
换表、同一天仍可复现。运行期的同号跨群间隔由 ``admits_at_time()`` / ``next_allowed_ts()`` 兜，
且它的台账同样要取**两个来源的并集**（``GroupShowStore.last_spoke_at()`` +
``InboxStore.group_last_spoke_at()``），理由与轴三一字不差。

**五条轴只允许有一处读账**（``ledgers.py``）。上面每条轴都要「读台账 → 拼 ``cast_roles``
入参」，而消费方有三个：影子 CLI、导播台、真发链路。三份实现只要有一处窗口不同、软失败
姿势不同、或少传一个 kwarg，就会出现最难发现的那种事故——**排练里四个号演得热热闹闹，
真发只有两个能上**，预览是假的而它看起来完全正常。所以任何消费方都只调
``read_show_context()``，拿到 ``ShowContext`` 后用 ``cast_kwargs()`` 拼参、
``speaker_cap()`` 定人数、``waiting()`` 查冷却，**一条业务判断都不自己复制**。
``ShowContext.degraded`` 非空 ＝ 某本账没读到，此时读数偏「安全」，必须原样透给用户。

**「能铺多少群」只允许有一个答案**（``capacity.py``）。上面四条风险轴各自都能推出一个容量
数字，而它们**差好几倍**——运营问的却是一个问题，他会记住哪个数取决于先看到哪张卡，而最显眼
的偏偏最乐观。最要命的一处：每场只让一个号开口时轴三如实报「无上限」（零号对，这在它自己那条
轴上没错），可轴四此时早已接管成瓶颈——10 个号还是只有 30 个群。照着「无上限」去铺，等于让每
个号在 10 个群里当主推，也就是最容易被人肉识别的那个特征。所以对外的容量答案一律经
``plan_capacity()`` **取最紧的那条轴**，并点名 ``binding`` 是哪条、``lever`` 该拧哪个旋钮。

演法参数要用 ``observed_mix()`` 从台账**反推**，不要读配置：护栏放行、人工补刀、日常自动回复
都会把实际开口人数顶上去，按配置算出来的是一份「按计划我们很安全」的报告，而平台数的是真发生
的消息。轴四的第二个旋钮是**带货密度**——不是每场戏都得种草，容量随之变成
``号数 × NORMAL_ROLE_CO / 带货占比``；「10 个号铺 100 个群」的确切答案就是它给的：
每场 1 个号开口 + 只有 30% 的场次带货。

另有一条**不在本模块建模**的轴：单号发言吞吐（一个号一天演得完这么多场吗）。``plan_capacity()``
只把 ``groups_per_account`` 当**事实**报出来、不判吉凶——它约束的是节奏，归 ``schedule.py``；
在这里编一个阈值只会多一盏没有依据的灯。
"""
from __future__ import annotations

from src.companion.group_show.attendance import (  # noqa: F401
    AttendancePlan,
    attendance_metrics,
    cast_entanglement,
    max_safe_groups,
    plan_attendance,
    recommend_pool_size,
    rotate_roles,
    schedule_joins,
    shared_groups,
    split_ledger,
)
from src.companion.group_show.capacity import (  # noqa: F401
    observed_mix,
    plan_capacity,
    pool_needed,
    recommend_mix,
    role_capacity,
)
from src.companion.group_show.ledgers import (  # noqa: F401
    ALL_HISTORY,
    ContextWatch,
    ShowContext,
    merge_speech,
    read_last_spoke,
    read_role_counts,
    read_show_context,
    read_show_outcomes,
    read_speech,
    speaking_groups,
)
from src.companion.group_show.outcome import (  # noqa: F401
    DEFAULT_WINDOW_HOURS,
    MIN_SHOWS_FOR_PLAYBOOK,
    ShowOutcome,
    build_outcome,
    by_playbook,
    clamp_window_hours,
    count_conversions,
    is_real_show,
    outcome_verdict,
    rollup,
    summarize_inbound,
    window_bounds,
)
from src.companion.group_show.samples import (  # noqa: F401
    SAMPLE_LIMIT,
    build_sample_entry,
    is_stale,
    load_samples,
    pick_sample_lines,
    playbook_fingerprint,
    samples_for,
    samples_path,
    save_sample,
)
from src.companion.group_show.live import (  # noqa: F401
    MAX_LIVE_LINES,
    MAX_LIVE_SECONDS,
    LiveLine,
    LivePlan,
    LiveResult,
    format_live,
    is_solo_playbook,
    live_enabled,
    live_quiet_hours,
    orchestrator_sender,
    perform,
    plan_live,
)
from src.companion.group_show.linkage import (  # noqa: F401
    derive_fingerprint_groups,
    derive_group,
    linkage_readiness,
)
from src.companion.group_show.performance import (  # noqa: F401
    added_co_performance,
    admits_speaker,
    co_performance,
    exposure_report,
    hot_pairs,
    pair_headroom,
    performance_metrics,
    rank_speakers,
    resolve_speaker_cap,
    speaker_budget,
    worst_verdict,
)
from src.companion.group_show.roles import (  # noqa: F401
    NORMAL_ROLE_CO,
    SELLING_SLOTS,
    admits_role,
    is_selling_slot,
    role_headroom,
    role_metrics,
    role_report,
)
from src.companion.group_show.schedule import (  # noqa: F401
    MIN_CROSS_GROUP_GAP_SEC,
    QUIET_HOURS,
    admits_at_time,
    in_quiet_hours,
    next_allowed_ts,
    plan_show_times,
    schedule_advice,
    schedule_metrics,
)
from src.companion.group_show.playbook import (  # noqa: F401
    Beat,
    BeatDirective,
    CastMember,
    Casting,
    Playbook,
    Role,
    ShowEvent,
    ShowState,
    load_playbook,
    load_playbook_dir,
    validate_playbook,
)

__all__ = [
    "ALL_HISTORY",
    "DEFAULT_WINDOW_HOURS",
    "MAX_LIVE_LINES",
    "MAX_LIVE_SECONDS",
    "MIN_CROSS_GROUP_GAP_SEC",
    "MIN_SHOWS_FOR_PLAYBOOK",
    "NORMAL_ROLE_CO",
    "QUIET_HOURS",
    "SAMPLE_LIMIT",
    "SELLING_SLOTS",
    "AttendancePlan",
    "Beat",
    "BeatDirective",
    "CastMember",
    "Casting",
    "ContextWatch",
    "LiveLine",
    "LivePlan",
    "LiveResult",
    "Playbook",
    "Role",
    "ShowContext",
    "ShowEvent",
    "ShowOutcome",
    "ShowState",
    "added_co_performance",
    "admits_at_time",
    "admits_role",
    "admits_speaker",
    "attendance_metrics",
    "build_outcome",
    "build_sample_entry",
    "by_playbook",
    "cast_entanglement",
    "clamp_window_hours",
    "co_performance",
    "count_conversions",
    "derive_fingerprint_groups",
    "derive_group",
    "exposure_report",
    "format_live",
    "hot_pairs",
    "in_quiet_hours",
    "is_real_show",
    "is_selling_slot",
    "is_solo_playbook",
    "is_stale",
    "linkage_readiness",
    "live_enabled",
    "live_quiet_hours",
    "load_playbook",
    "load_playbook_dir",
    "load_samples",
    "max_safe_groups",
    "merge_speech",
    "next_allowed_ts",
    "observed_mix",
    "orchestrator_sender",
    "outcome_verdict",
    "pair_headroom",
    "perform",
    "performance_metrics",
    "pick_sample_lines",
    "plan_attendance",
    "playbook_fingerprint",
    "plan_capacity",
    "plan_live",
    "plan_show_times",
    "pool_needed",
    "rank_speakers",
    "read_last_spoke",
    "read_role_counts",
    "read_show_context",
    "read_show_outcomes",
    "read_speech",
    "recommend_mix",
    "recommend_pool_size",
    "resolve_speaker_cap",
    "role_capacity",
    "role_headroom",
    "role_metrics",
    "role_report",
    "rollup",
    "rotate_roles",
    "samples_for",
    "samples_path",
    "save_sample",
    "schedule_advice",
    "schedule_joins",
    "schedule_metrics",
    "shared_groups",
    "speaker_budget",
    "speaking_groups",
    "split_ledger",
    "summarize_inbound",
    "validate_playbook",
    "window_bounds",
    "worst_verdict",
]
