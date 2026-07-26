# -*- coding: utf-8 -*-
"""开演排期 —— 静态特征都洗干净之后，**节奏**是最后一条还会出卖人的轴。

与 :mod:`~src.companion.group_show.pacing` 的边界（先读这段，别重复实现）
------------------------------------------------------------------------
``pacing`` 管**一场戏内部**：这一拍距上一拍隔几秒（读上一条 + 打这一条 + 泊松抖动）。
本模块管**场与场之间**：这 100 个群，今天各自几点开演。两者相差三个数量级（秒 vs 小时），
更要紧的是**风险模型完全不同**——场内节奏的破绽只有那个群里的人看得到，跨群排期的破绽
要把一批群的时间戳拉到一起做聚合才看得出来，而后者恰恰是平台侧顺手就会做的事。所以
``pacing`` 追求「像一个人在打字」，本模块追求「像一百个互不相识的人各过各的日子」。

与 :func:`~src.companion.group_show.attendance.schedule_joins` 的边界
--------------------------------------------------------------------
那边排的是**加群**（一次性动作，约束是「同一个群每天只进一个号」），本模块排的是**开演**
（每天都发生，约束是时段与节奏）。两条时间轴独立：加群排得再散，开演全挤在同一个小时里
照样一眼假。

四条真实可检测的时间特征（本模块逐条对付）
------------------------------------------
1. **同号跨群前后脚开口**——同一个号在 A 群说完 8 秒后在 B 群冒头，真人不会这么赶。
   排期阶段还不知道选角（选角在开演时才跑），所以这条不由 :func:`plan_show_times` 保证，
   而是留给运行期的 :func:`admits_at_time` / :func:`next_allowed_ts` 兜。
2. **扎堆与等距**——一批群挤在同一个小时里，或者精确每 30 分钟一场。后者比前者更致命：
   **周期性做一次自相关就出来了**，而扎堆还得先定义「多挤算挤」。
3. **深夜开演**——人设该睡觉的时候开演，既不像真人、又拿不到互动（两头亏）。
4. **每天同一时刻**——今天 14:07、明天 14:07、后天 14:07。这条最隐蔽也最好抓，而且它是
   **默认会发生**的：排期函数是纯函数，同样的输入天然给同样的表。本模块的对策是把
   ``day_start_ts`` 拌进抖动种子（见 :func:`plan_show_times`），让「同一天可复现、
   跨天自动换一张表」两件事同时成立，而不是指望调用方每天记得换 ``seed``。

设计约束
--------
* **纯函数、无 I/O**：绝不在函数体内调 ``time.time()``——那样既没法测也没法回放，而排期
  的全部价值就在于「排出来的表能先看、能排练、能复现」。「现在几点」由调用方传进来。
* **确定性抖动走 crc32**：不用 ``random``（全局状态、不可复现），更不用内置 ``hash()``
  （对 str 每进程加盐，换个进程换一张表——运营照着表执行到一半刷新页面就作废了）。
* **不抛异常**：排期是编排链路上的一环，炸了不能拦住整场演出；脏输入一律软降级成形状
  完整的空结果。

时区：本模块**不做任何时区换算**
--------------------------------
``day_start_ts`` ＝「排期这一天的**本地**零点」对应的 epoch 秒，之后全是纯算术。刻意不碰
``time.localtime``：服务器时区跟群成员所在时区没有任何关系，用服务器时区去判「深夜」会在
跨境场景下把凌晨三点算成下午三点——那正是本模块要防的事故本身。
"""
from __future__ import annotations

import logging
import zlib
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.companion.group_show.attendance import (
    VERDICT_DANGER,
    VERDICT_SAFE,
    VERDICT_WARN,
)

logger = logging.getLogger(__name__)

# ── 时段 ────────────────────────────────────────────────────────────────────

#: 安静时段 [起, 止)，24 小时制。与 companion.proactive_topic 的口径对齐：深夜开演
#: 既不像真人、又拿不到互动。
QUIET_HOURS: Tuple[int, int] = (23, 8)

#: 默认活动时段 [起, 止)。9-22 是「醒着且在刷手机」的保守区间；再宽就得靠人设作息背书，
#: 而人设作息是每个号各自的事，不该由排期器一刀切。
DEFAULT_ACTIVE_HOURS: Tuple[int, int] = (9, 22)

#: 同一个号在**两个不同群**里开口的最小间隔（秒）。真人不会前后脚在两个群发言。
MIN_CROSS_GROUP_GAP_SEC: int = 600

#: 同一个群当天两场戏之间的最小间隔（秒）。90 分钟＝「一小时内演两场很假」这条底线
#: 再留半小时余量：群成员对「刚才不是聊过一轮吗」的记忆窗大约就是这个量级。
SAME_GROUP_MIN_GAP_SEC: int = 5400

#: 同一个群一天最多几场。超过这个数，无论怎么排都躲不开「这个群天天有人组团聊天」。
MAX_SHOWS_PER_GROUP_PER_DAY: int = 6

# ── 抖动 ────────────────────────────────────────────────────────────────────

#: 每个时间槽在自己那一格里的抖动幅度（占槽宽的比例）。
#:
#: 取 0.9 而不是 1.0：满幅抖动时相邻两场的间隔可以逼近 0（前一场抖到格尾、后一场抖到
#: 格头），100 个群塞进 13 小时的场景下会排出「同一秒开两场」。留 10% 死区换来一个恒正
#: 的间隔下界，代价只是把间隔的变异系数从 0.408 压到 0.367——都远离「等距」。
#:
#: 注意这个下界只保到「不撞同一秒」：避整点的微调（见 :func:`_dodge_round_minute`）之后，
#: 相邻两场可能只差十几秒。那**不是**风险——两场戏在两个不同的群、由不同的号演，同一个号
#: 的跨群间隔由 :func:`admits_at_time` 单独把关。
SLOT_JITTER_SHARE: float = 0.9

#: 「整点/半点」的分钟数。开在整点是机器排期最容易被看出来的一条（人不会掐着表开口）。
ROUND_MINUTES: Tuple[int, ...] = (0, 30)

#: 撞上整点/半点时往后推的秒数区间。上界刻意 < 3600-1800，保证推完仍落在同一个小时里，
#: 不会把一场戏推出允许时段（否则「避整点」这个小修饰会捅出「演在安静时段」的大篓子）。
ROUND_REPEL_MIN_SEC: int = 70
ROUND_REPEL_MAX_SEC: int = 200

#: 同群最小间隔的修复窗：排到某一格时，从候选队头往后最多看这么多个群找一个不违反间隔的。
#: 不做全量搜索是因为它会退化成「按上次开演时间排序」＝ 把同群间隔钉成常数，
#: 反而制造出周期性——修一条特征、造一条更好抓的特征，是排期器最容易犯的错。
SAME_GROUP_LOOKAHEAD: int = 16

# ── 判词刻度（保守经验值，不是实测阈值；平台真实判据不可知） ──────────────────

#: 间隔被认定为「同一个间隔」的相对容差：与中位数相差 ±10% 以内算同一档。
#: 用相对容差而不是固定秒数，指标才对「每 30 分钟一场」和「每 8 分钟一场」同样敏感。
REGULARITY_TOL: float = 0.1

REGULARITY_WARN: float = 0.5
REGULARITY_DANGER: float = 0.8

#: 最挤那个小时占全天场次的比例。
BURST_WARN: float = 0.25
BURST_DANGER: float = 0.5

#: 单个小时的绝对场次上限。比例指标在总量大时会失灵（100 场里 10 场只占 10%，可那一个
#: 小时里每 6 分钟就开一场戏），所以绝对量要单独看一眼。
HOT_HOUR_SHOWS: int = 6

#: 落在整点/半点的场次占比上限。纯随机分钟数的期望约 3.3%（2/60），20% 已经是明显对齐。
ROUND_MINUTE_WARN: float = 0.2

#: 安静时段命中占比达到它 → 不是手滑排错一场，是整张表的时段配错了。
QUIET_DANGER_RATIO: float = 0.25

#: 跨天实况里「同一个钟点」的占比（周期性）。
DAILY_REPEAT_WARN: float = 0.4
DAILY_REPEAT_DANGER: float = 0.7

#: 样本太少时这些比例指标是平凡真：1 场戏的「最挤小时占比」恒为 100%，2 个间隔「都跟
#: 中位数一样」也恒为真。低于这个场次一律不下结论（同 attendance.CLIQUE_MIN_ACCOUNTS 的
#: 思路：平凡真不含信息，报出来只会训练运营忽略告警）。
MIN_SHOWS_FOR_STATS: int = 4

#: 周期性判据要更保守：跨天数据本来就少，8 场以下「碰巧两场同一个钟点」太容易发生。
MIN_SHOWS_FOR_PERIODICITY: int = 8


# ── 脏输入清洗 ──────────────────────────────────────────────────────────────


def _s(value: Any) -> str:
    """任意值 → 干净字符串（``None`` / ``__str__`` 会炸的对象一律成空串，绝不抛）。"""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:  # noqa: BLE001 —— 外部数据的 __str__ 都可能炸
        return ""


def _i(value: Any, default: int = 0) -> int:
    try:
        got = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return got


def _f(value: Any, default: float = 0.0) -> float:
    """任意值 → 有限浮点。NaN / inf 与非数一律回落 ``default``。

    时间戳算术里放进一个 NaN，后果是整张表静默变成 NaN 而不是抛错——排期表看起来还在，
    每一行都是空的，比直接崩难查得多。
    """
    try:
        got = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if got != got or got in (float("inf"), float("-inf")):
        return default
    return got


def _ts(value: Any) -> Optional[float]:
    """时间戳解析：解不出来返回 ``None``（区别于「解出来是 0」）。

    ``0`` 是合法时间戳（1970 年，早期数据里真的有），拿它当「没有」会把这些行判成远古，
    :mod:`~src.companion.group_show.store` 的窗口过滤踩过同一个坑。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        got = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if got != got or got in (float("inf"), float("-inf")):
        return None
    return got


def _tiebreak(*parts: str) -> int:
    """确定性伪随机源。

    刻意不用内置 ``hash()``——它对 str 每进程加盐，换个进程就换一张排期表；也不用
    ``random``——那是进程级全局状态，两个并行的排期会互相偷对方的随机数。crc32 对同一
    份输入在任何机器任何进程上都是同一个数，排练、回放、页面刷新才对得上。
    """
    return zlib.crc32("\x1f".join(parts).encode("utf-8", "replace"))


def _jitter01(*parts: str) -> float:
    """确定性 ``[0, 1)`` 抖动。

    **crc32 单独用在这里是不够的**，这是实测出来的坑：crc32 在 GF(2) 上是线性的，而抖动的
    输入恰恰是 ``"...slot0"``/``"...slot1"`` 这种只差末位的序列——单看分布它很均匀（十分位
    直方图挑不出毛病），但**相邻两个值的相关系数高达 0.57**。撒点时相邻抖动同向移动，
    间隔的方差就被吃掉一半：实测间隔变异系数只有 0.24，而独立抖动应当给到 0.37。也就是说
    「看起来随机、实际偏等距」——正是本模块要消灭的那个特征，却由自己的随机源制造出来。

    对策是在 crc32 之后接一段**乘法雪崩**（lowbias32 finalizer）。乘法在 GF(2) 上非线性，
    一步就把线性结构打散；仍然是纯整数运算、跨进程跨机器同值，确定性一点不丢。
    """
    x = (_tiebreak(*parts) + 0x9E3779B9) & 0xFFFFFFFF
    x ^= x >> 16
    x = (x * 0x21F0AAAD) & 0xFFFFFFFF
    x ^= x >> 15
    x = (x * 0xD35A2D97) & 0xFFFFFFFF
    x ^= x >> 15
    return x / 4294967296.0


def _as_list(raw: Any) -> List[Any]:
    """把外部传来的「一批东西」洗成 list，形状不对时返回空。

    单个字符串按**一个元素**处理：裸字符串可遍历，直接 ``iter`` 会把 ``"g1"`` 拆成
    ``g``/``1`` 两个「群」，排出来的表看着完全正常，实际在给两个不存在的群排戏。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, Mapping):
        return [raw]
    try:
        return list(raw)
    except TypeError:
        return []


def _group_id(raw: Any) -> str:
    """从「裸 key / 群行」里取群标识；注册表与 inbox 两种命名都认。

    id 值只认标量：``{群: [成员]}`` 台账里如果碰巧有个群叫 ``key``，不加这道限制就会把
    整张台账当成「一行 id 是 ``['a','b']`` 的群记录」，剩下的群全部凭空消失。
    """
    if isinstance(raw, Mapping):
        for key in ("group_id", "chat_key", "group_key", "id", "key"):
            got = raw.get(key)
            if isinstance(got, (str, int)) and not isinstance(got, bool):
                text = _s(got)
                if text:
                    return text
        return ""
    return _s(raw)


def _group_keys(raw: Any) -> List[str]:
    """群清单 → 去空去重的 id 列表（保持给定顺序）。

    ``Mapping`` 有两种合理含义：**一行群记录**（带 ``group_id`` / ``chat_key``）或
    **``{群: 成员}`` 台账**（把 ``plan_attendance().assignments`` 直接喂进来是最顺手的
    用法）。按「有没有 id 键」区分两者——猜错任何一边都会静默排出一张空表，而空表在页面上
    跟「今天不用演」长得一模一样，是最难被发现的那类破口。
    """
    out: List[str] = []
    seen: set = set()
    rows: List[Any]
    if isinstance(raw, Mapping):
        rows = [raw] if _group_id(raw) else list(raw.keys())
    else:
        rows = _as_list(raw)
    for item in rows:
        gid = _group_id(item)
        if gid and gid not in seen:
            seen.add(gid)
            out.append(gid)
    return out


def _median(values: Sequence[float]) -> float:
    """中位数（空序列 → 0.0）。用中位数而不是均值：一两个离群间隔（比如跨过一段禁演
    时段）会把均值拉走，而「大多数间隔长什么样」才是规律性要问的问题。"""
    items = sorted(values)
    n = len(items)
    if not n:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(items[mid])
    return (float(items[mid - 1]) + float(items[mid])) / 2.0


# ── 时段窗口 ────────────────────────────────────────────────────────────────


def _window(raw: Any, default: Tuple[int, int]) -> Tuple[int, int]:
    """``(起, 止)`` 小时窗解析；形状不对回落 ``default``（配置手写，迟早会写成字符串）。"""
    try:
        if isinstance(raw, Mapping) or isinstance(raw, str):
            return default
        seq = list(raw or ())
    except TypeError:
        return default
    if len(seq) != 2:
        return default
    return (_i(seq[0], default[0]) % 24, _i(seq[1], default[1]) % 24)


def _hour_set(window: Tuple[int, int]) -> set:
    """``[起, 止)`` 的小时集合；``起 > 止`` ＝跨午夜（23..8），``起 == 止`` ＝空集。

    跨午夜的语义与 ``companion_proactive._in_quiet_hours`` 一致——两处判「深夜」的口径
    必须同源，否则主动触达觉得该睡了、群戏觉得还能演，那条护栏等于漏了一半。
    """
    start, end = window
    if start == end:
        return set()
    if start < end:
        return set(range(start, end))
    return set(range(start, 24)) | set(range(0, end))


def in_quiet_hours(hour: Any, *, quiet_hours: Any = QUIET_HOURS) -> bool:
    """这个钟点在不在禁演时段里。

    排期器（:func:`plan_show_times`）在**排表时**就绕开了安静时段，但真发是即时动作——
    运营在导播台上点「现在演一场」时没有任何一张表挡在中间。这个谓词就是给那条路用的。

    ``起 == 止``（如 ``(0, 0)``）＝ 空窗 ＝ **不禁**：让「关掉这条闸门」和「配一个窗」
    走同一个配置项，省掉一个单独的 on/off 开关——两个开关一定会出现「窗配了但没打开」
    这种状态，而它看起来跟「窗生效了」一模一样。

    ``hour`` 解析不出来（``None``／脏值）→ **放行**。这里刻意不做保守处理：钟点算不出来
    多半是调用方没传，此时禁演等于把整条真发链路默默锁死，而运营对付「怎么都发不出去」
    的唯一手段就是把闸门关掉——那才是真的没有闸门。
    """
    try:
        h = int(hour)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return False
    if isinstance(hour, bool):
        return False
    return (h % 24) in _hour_set(_window(quiet_hours, QUIET_HOURS))


def _ordered_hours(active: Tuple[int, int], quiet: Tuple[int, int]) -> List[int]:
    """可开演的小时，按「从活动时段起点起、绕一圈」排好序。

    返回的是**相对当天零点的小时偏移**，跨午夜的部分会 ``>= 24``（如夜场 22..8 得到
    ``[22, 23, 32]``）。不取模是刻意的：排期要按真实时间先后串起来，取了模会让凌晨那几场
    排到傍晚那几场前面，同群最小间隔立刻算错。

    ``active`` 与 ``quiet`` 冲突时 **quiet 赢**——安静时段是硬的：活动时段是运营的偏好，
    安静时段是「人设这个点该睡觉」的事实，让偏好覆盖事实就等于取消了这条护栏。
    """
    allowed = _hour_set(active)
    if not allowed:
        # 起 == 止 语义太含糊（「全天」还是「不演」？）。回落默认活动时段而不是放开全天：
        # 猜成「全天」会让配置手滑直接把戏排进凌晨三点。
        allowed = _hour_set(DEFAULT_ACTIVE_HOURS)
    allowed = allowed - _hour_set(quiet)
    if not allowed:
        return []
    start = active[0] % 24
    return [start + k for k in range(24) if (start + k) % 24 in allowed]


# ── 排期 ────────────────────────────────────────────────────────────────────


def _slot_offsets(count: int, capacity_hours: int, salt: str) -> List[float]:
    """在「压缩时间轴」上撒 ``count`` 个点（秒，``[0, capacity)`` 内严格递增）。

    做法是**分层抖动**：把可用时长等分成 ``count`` 格，每场在自己那一格里随机落点。
    不用均匀随机（会扎堆，正是特征 2 的一半），也不用等距+小抖动（间隔的方差太小，
    自相关照样看得出周期），更不用泊松过程——泊松的长尾意味着必然出现「十分钟内五场」的
    簇，而我们的场次分布在**跨群聚合**视角下被看的就是这个簇。

    分层抖动的间隔分布是三角形的：均值 ＝ 格宽，变异系数 ≈ ``SLOT_JITTER_SHARE/√6``
    ≈ 0.37。既不是 0（等距），也不至于大到堆簇。
    """
    capacity = float(capacity_hours) * 3600.0
    if count <= 0 or capacity <= 0:
        return []
    step = capacity / float(count)
    span = step * SLOT_JITTER_SHARE
    return [i * step + _jitter01(salt, "slot", str(i)) * span for i in range(count)]


def _place(offset: float, hours: Sequence[int], day_start: float) -> Tuple[float, int]:
    """压缩时间轴上的一点 → ``(真实时间戳, 当天第几点)``。

    可开演的小时不一定连续（活动时段被安静时段掏掉一块是常态），所以撒点先在「把可用
    小时首尾相接」的压缩轴上做，再逐格映射回真实时间。反过来先在真实轴上撒点再剔除落在
    禁演区的，会让紧挨禁演区的两侧凭空变密——那是自己给自己造了个扎堆。
    """
    idx = int(offset // 3600.0)
    if idx >= len(hours):
        idx = len(hours) - 1
        rest = 3599.0
    else:
        rest = offset - idx * 3600.0
    hour_offset = hours[idx]
    return day_start + hour_offset * 3600.0 + rest, hour_offset % 24


def _dodge_round_minute(at: float, day_start: float, allowed: set, salt: str) -> float:
    """撞上整点/半点就往后挪一点。

    分层抖动本身已经让分钟数近似均匀，撞整点的概率只有 3% 左右；但「整点开演」是这条轴上
    最刺眼的一个特征，3% 也不该留——尤其是**每天恒定的那一场**如果正好落在整点，跨天看
    就是「每天 15:00:00 准时开演」。挪的幅度上界保证不出这个小时，绝不会把戏挪进禁演时段。
    """
    minute = int((at // 60.0) % 60)
    if minute not in ROUND_MINUTES:
        return at
    span = float(ROUND_REPEL_MAX_SEC - ROUND_REPEL_MIN_SEC)
    shifted = at + ROUND_REPEL_MIN_SEC + _jitter01(salt, "dodge", str(int(at))) * span
    hour = int((shifted - day_start) // 3600.0) % 24
    return shifted if hour in allowed else at


def plan_show_times(
    groups: Any,
    *,
    day_start_ts: Any,
    per_day: Any = 1,
    active_hours: Any = DEFAULT_ACTIVE_HOURS,
    seed: Any = "",
    quiet_hours: Any = QUIET_HOURS,
) -> List[Dict[str, Any]]:
    """给每个群排开演时刻，返回按时间升序的 ``[{"group","at","hour","minute","nth"}, ...]``。

    Args:
        groups: 群清单。裸 key / 群行 / ``{群: 成员}`` 台账都认（见 :func:`_group_keys`）。
        day_start_ts: 排期这一天**本地零点**的 epoch 秒。本模块不做时区换算，也不读系统
            时钟——「今天从几点开始」只有调用方知道（见模块 docstring）。
        per_day: 每个群当天演几场。``<= 0`` ⇒ 一场都不排（返回 ``[]``）。这里刻意**不**
            像 ``schedule_joins`` 那样把 0 钳成 1：那边 0 会转死循环，钳位是防御；这边
            0 是一个说得通的意思（今天不演），偷偷改成 1 等于替运营做了「开演」的决定。
        active_hours: ``[起, 止)`` 活动时段，跨午夜写成 ``(22, 3)``。
        seed: 换一个种子出另一张等价优度的表。
        quiet_hours: ``[起, 止)`` 安静时段，与 ``active_hours`` 冲突时**它赢**。

    为什么种子里要拌 ``day_start_ts``
    ---------------------------------
    纯函数天然有个反效果：同样的群清单、同样的 seed，明天排出来的还是同一张表——于是
    「每天 14:07 开演」这条最好抓的周期性特征会**默认发生**，而且运营完全无感（表看起来
    很随机）。把当天零点拌进抖动种子，跨天自动换一张表，同一天重跑仍然逐行一致（排练、
    续演、页面刷新都靠这条）。这比「请调用方记得每天换 seed」可靠得多——那种约定迟早忘。

    排不下的时候
    ------------
    100 个群塞进 13 小时是**真实处境**，不是异常。这里如实排满（平均每 468 秒一场），
    密度问题交给 :func:`schedule_metrics` 报出来，由人决定摊到两天还是放宽时段。排期器
    自作主张砍掉排不下的群，会让运营以为那些群今天不用演。
    """
    try:
        keys = _group_keys(groups)
        rounds = _i(per_day, 1)
        if not keys or rounds <= 0:
            return []
        rounds = min(rounds, MAX_SHOWS_PER_GROUP_PER_DAY)

        day_start = _f(day_start_ts, 0.0)
        active = _window(active_hours, DEFAULT_ACTIVE_HOURS)
        quiet = _window(quiet_hours, QUIET_HOURS)
        hours = _ordered_hours(active, quiet)
        if not hours:
            # 活动时段被安静时段整个吃掉。宁可今天不演也不在深夜演——安静时段是硬的，
            # 这里放宽等于把上面那句话作废。
            return []
        allowed = {h % 24 for h in hours}

        salt = "\x1f".join((_s(seed), str(int(day_start))))
        total = len(keys) * rounds
        offsets = _slot_offsets(total, len(hours), salt)

        slots: List[float] = []
        for offset in offsets:
            at, _hour = _place(offset, hours, day_start)
            slots.append(float(int(_dodge_round_minute(at, day_start, allowed, salt))))

        # 每一轮吃掉连续的一段时间槽 ⇒ 同一个群的两场天然隔了大半个「轮长」。轮内顺序由
        # crc32 重新洗牌（每轮一套），所以同群相邻两场的间隔在轮长上下真实浮动，而不是
        # 被钉成常数——钉成常数就等于把「每天两场」变成「每 6.5 小时一场」的周期信号。
        items: List[Dict[str, Any]] = []
        last_at: Dict[str, float] = {}
        crowded = 0
        for r in range(rounds):
            queue = sorted(keys, key=lambda g: _tiebreak(salt, "round", str(r), g))
            for at in slots[r * len(keys):(r + 1) * len(keys)]:
                pick = 0
                for k in range(min(len(queue), SAME_GROUP_LOOKAHEAD)):
                    prev = last_at.get(queue[k])
                    if prev is None or (at - prev) >= SAME_GROUP_MIN_GAP_SEC:
                        pick = k
                        break
                else:
                    # 队头这一小撮全都离上一场太近：宁可如实排下去也不空转，
                    # 违规数会体现在 schedule_metrics 的同群最小间隔里。
                    crowded += 1
                group = queue.pop(pick)
                last_at[group] = at
                items.append({
                    "group": group,
                    "at": at,
                    "hour": int((at - day_start) // 3600.0) % 24,
                    "minute": int((at // 60.0) % 60),
                    "nth": r + 1,
                })

        if crowded:
            logger.debug(
                "[group_show.schedule] %d 场没能满足同群最小间隔（可用时段太窄）", crowded)
        items.sort(key=lambda row: (row["at"], row["group"]))
        return items
    except Exception:  # noqa: BLE001 —— 排期炸了给空表，绝不交出半张
        logger.debug("[group_show.schedule] plan_show_times 失败（已忽略）",
                     exc_info=True)
        return []


# ── 度量 ────────────────────────────────────────────────────────────────────


def _empty_metrics() -> Dict[str, Any]:
    return {
        "count": 0, "span_hours": 0.0, "hours_used": 0, "shows_per_hour": 0.0,
        "burstiness": 0.0, "peak_hour": None, "peak_hour_count": 0,
        "peak_groups": [], "by_hour": {},
        "regularity": 0.0, "median_gap_sec": 0.0, "min_gap_sec": 0.0,
        "round_minute_ratio": 0.0, "whole_minute_ratio": 0.0,
        "quiet_violations": 0, "daily_repeat": 0.0,
        "verdict": VERDICT_SAFE, "advice": [],
    }


def _rows_of(plan: Any) -> List[Tuple[float, Optional[int], str]]:
    """排期表 → ``[(时间戳, 当天第几点或 None, 群)]``，按时间升序；脏行丢弃不连累其余。

    ``hour`` 缺失时**不猜**（留 ``None``、只退出安静时段那一项判定）：从时间戳反推小时
    要假定一个时区，而本模块唯一的立场就是「时区只有调用方知道」。猜错的代价是把下午的
    戏报成深夜违规，一个假告警比少一个指标更贵。
    """
    rows: List[Tuple[float, Optional[int], str]] = []
    for item in _as_list(plan):
        if not isinstance(item, Mapping):
            continue
        at = _ts(item.get("at"))
        if at is None:
            continue
        raw_hour = item.get("hour")
        hour: Optional[int] = None
        if raw_hour is not None and not isinstance(raw_hour, bool):
            got = _i(raw_hour, -1)
            hour = got % 24 if 0 <= got <= 23 else None
        rows.append((at, hour, _group_id(item.get("group"))))
    rows.sort(key=lambda row: (row[0], row[2]))
    return rows


def _verdict_for(
    *, count: int, regularity: float, burstiness: float, peak: int,
    round_ratio: float, quiet_ratio: float, quiet_hits: int, repeat: float,
) -> str:
    """各条特征各自都能升级判词——它们是**独立**的暴露面，不该互相稀释成一个平均分。"""
    if count < MIN_SHOWS_FOR_STATS:
        # 样本不足时只有安静时段这条判得动（它按单场判，不是比例）
        return VERDICT_WARN if quiet_hits else VERDICT_SAFE
    if (regularity >= REGULARITY_DANGER or burstiness >= BURST_DANGER
            or quiet_ratio >= QUIET_DANGER_RATIO or repeat >= DAILY_REPEAT_DANGER):
        return VERDICT_DANGER
    if (regularity >= REGULARITY_WARN or burstiness >= BURST_WARN
            or peak > HOT_HOUR_SHOWS or round_ratio >= ROUND_MINUTE_WARN
            or quiet_hits or repeat >= DAILY_REPEAT_WARN):
        return VERDICT_WARN
    return VERDICT_SAFE


def schedule_metrics(
    plan: Any,
    *,
    quiet_hours: Any = QUIET_HOURS,
) -> Dict[str, Any]:
    """这张排期表有多像机器排的。``plan`` ＝ :func:`plan_show_times` 的返回值（或同形状的实况）。

    Args:
        plan: ``[{"group","at","hour"}, ...]``。实况里的 ``hour`` 请一并带上，缺了就退出
            安静时段那一项判定（理由见 :func:`_rows_of`）。
        quiet_hours: 判「深夜开演」用的窗口。**签名上多出来的这一个关键字参数**是为了让
            实况能按自己那套作息判——写死 :data:`QUIET_HOURS` 的话，夜场剧本每次体检都会
            被报一堆假违规，报几次之后这张卡就没人看了。默认值与排期端一致，
            ``schedule_metrics(plan)`` 的调用形式不受影响。

    Returns:
        ``count`` 场次／``span_hours`` 首末跨度／``hours_used`` 用掉几个不同的钟头／
        ``shows_per_hour`` 密度／``burstiness`` **最挤那个小时**占全天的比例／
        ``peak_hour``+``peak_hour_count``+``peak_groups`` 挤在哪／``by_hour`` 逐时直方图／
        ``regularity`` 间隔规律性 0..1（1 ＝ 完全等距）／``median_gap_sec``／``min_gap_sec``／
        ``round_minute_ratio`` 整点半点占比／``whole_minute_ratio`` 秒数为 0 的占比／
        ``quiet_violations`` 深夜场次／``daily_repeat`` 跨天周期性／``verdict``／``advice``。

    为什么 burstiness 取最坏那个小时而不是平均
    ------------------------------------------
    平均值必然等于 ``1/可用小时数``，它只反映时段配置、跟排得好不好毫无关系；而平台聚合
    时间戳时抓的就是尖峰——99 场摊得很匀、剩下 9 场挤在 14 点，这张表就是废的。同一条
    「盯最坏那个」的原则贯穿本子系统（见 ``attendance`` 的 min-max 目标）。

    ``regularity`` 为什么用「贴着中位数的间隔占比」
    ----------------------------------------------
    ＝ 与间隔中位数相差 ±:data:`REGULARITY_TOL` 以内的间隔占全部间隔的比例。完全等距
    ⇒ 1.0，本模块的分层抖动 ⇒ 0.2 上下。没用变异系数是因为 CV 是**绝对**离散度：同一份
    抖动力度，30 分钟一场的表和 8 分钟一场的表会得到完全不同的分数，阈值没法定；相对
    容差则对时间尺度免疫。也没用 FFT/自相关——那需要长序列，而一天的排期只有几十个点。
    """
    out = _empty_metrics()
    try:
        rows = _rows_of(plan)
        count = len(rows)
        out["count"] = count
        if not count:
            return out

        times = [row[0] for row in rows]
        out["span_hours"] = (times[-1] - times[0]) / 3600.0

        # 扎堆按**绝对钟头**分桶（真实的「同一个小时里几场」），而不是按当天第几点——
        # 后者会把今天 14 点和明天 14 点算成一堆，那是周期性，由 daily_repeat 单独管。
        buckets: Dict[int, List[Tuple[Optional[int], str]]] = {}
        for at, hour, group in rows:
            buckets.setdefault(int(at // 3600.0), []).append((hour, group))
        hours_used = len(buckets)
        out["hours_used"] = hours_used
        out["shows_per_hour"] = count / hours_used if hours_used else 0.0

        peak_key = max(buckets, key=lambda k: (len(buckets[k]), -k))
        peak_rows = buckets[peak_key]
        out["peak_hour_count"] = len(peak_rows)
        out["peak_hour"] = next((h for h, _g in peak_rows if h is not None), None)
        out["peak_groups"] = sorted({g for _h, g in peak_rows if g})[:8]
        if count >= MIN_SHOWS_FOR_STATS:
            out["burstiness"] = len(peak_rows) / count

        by_hour: Dict[int, int] = {}
        for _at, hour, _g in rows:
            if hour is not None:
                by_hour[hour] = by_hour.get(hour, 0) + 1
        out["by_hour"] = dict(sorted(by_hour.items()))

        gaps = [times[i + 1] - times[i] for i in range(count - 1)]
        if gaps:
            out["min_gap_sec"] = min(gaps)
            median = _median(gaps)
            out["median_gap_sec"] = median
            if len(gaps) >= MIN_SHOWS_FOR_STATS - 1:
                if median <= 0:
                    out["regularity"] = 1.0   # 全部挤在同一秒：退化的「完全等距」
                else:
                    tol = median * REGULARITY_TOL
                    hits = sum(1 for g in gaps if abs(g - median) <= tol)
                    out["regularity"] = hits / len(gaps)

        out["round_minute_ratio"] = sum(
            1 for at in times if int((at // 60.0) % 60) in ROUND_MINUTES) / count
        out["whole_minute_ratio"] = sum(
            1 for at in times if int(at) % 60 == 0) / count

        quiet = _hour_set(_window(quiet_hours, QUIET_HOURS))
        quiet_hits = sum(1 for _at, h, _g in rows if h is not None and h in quiet)
        out["quiet_violations"] = quiet_hits

        # 跨天才谈得上「每天同一时刻」：同一天之内每个钟头只出现一次，这个数会退化成
        # burstiness 的复读。
        if out["span_hours"] > 24.0 and count >= MIN_SHOWS_FOR_PERIODICITY and by_hour:
            out["daily_repeat"] = max(by_hour.values()) / count

        out["verdict"] = _verdict_for(
            count=count, regularity=out["regularity"],
            burstiness=out["burstiness"], peak=out["peak_hour_count"],
            round_ratio=out["round_minute_ratio"],
            quiet_ratio=(quiet_hits / count), quiet_hits=quiet_hits,
            repeat=out["daily_repeat"],
        )
        out["advice"] = schedule_advice(out)
        return out
    except Exception:  # noqa: BLE001 —— 体检炸了给形状完整的空报告，不能白屏
        logger.debug("[group_show.schedule] schedule_metrics 失败（已忽略）",
                     exc_info=True)
        return out


def schedule_advice(metrics: Mapping[str, Any]) -> List[str]:
    """把读数翻译成运营真能执行的动作。顺序即紧要程度。

    排序依据是**被抓到的难易**而不是数字大小：周期性（等距／每天同一时刻）做一次自相关
    就出来，扎堆得先定义「多挤算挤」，深夜开演还得人去看，分钟对齐则要凑够样本才显著。
    """
    advice: List[str] = []
    try:
        count = _i(metrics.get("count"))
        if count <= 0:
            return advice

        regularity = _f(metrics.get("regularity"))
        if regularity >= REGULARITY_WARN:
            median = _f(metrics.get("median_gap_sec"))
            advice.append(
                f"{regularity:.0%} 的相邻间隔都贴着 {median / 60:.0f} 分钟——等距是自相关"
                f"一算就出来的特征，换个 seed 重排，或把 active_hours 放宽让抖动有地方抖")

        repeat = _f(metrics.get("daily_repeat"))
        if repeat >= DAILY_REPEAT_WARN:
            advice.append(
                f"{repeat:.0%} 的场次落在同一个钟点上——「每天这个点准时开演」比扎堆更好抓，"
                f"确认排期种子里带了当天日期（plan_show_times 默认已拌入 day_start_ts）")

        burst = _f(metrics.get("burstiness"))
        peak_count = _i(metrics.get("peak_hour_count"))
        peak_hour = metrics.get("peak_hour")
        peak_groups = [str(g) for g in (metrics.get("peak_groups") or [])]
        if peak_hour is not None and (burst >= BURST_WARN or peak_count > HOT_HOUR_SHOWS):
            who = "/".join(peak_groups[:3]) or "那个小时里的几个群"
            advice.append(
                f"把 {who} 从 {peak_hour} 点挪开，那个小时挤了 {peak_count} 场"
                f"（全天 {count} 场，占 {burst:.0%}）")

        quiet_hits = _i(metrics.get("quiet_violations"))
        if quiet_hits:
            advice.append(
                f"{quiet_hits} 场排进了安静时段——人设该睡觉的时候开演，既不像真人又拿不到"
                f"互动；检查 active_hours 与 quiet_hours 是不是配反了")

        hours_used = _i(metrics.get("hours_used"))
        density = _f(metrics.get("shows_per_hour"))
        if hours_used and density > HOT_HOUR_SHOWS:
            days = (count + hours_used * HOT_HOUR_SHOWS - 1) // max(
                hours_used * HOT_HOUR_SHOWS, 1)
            advice.append(
                f"{hours_used} 个可用小时要塞 {count} 场（每小时 {density:.1f} 场）——"
                f"时段本身就不够用，摊到 {max(days, 2)} 天或放宽 active_hours，"
                f"光换 seed 解决不了")

        round_ratio = _f(metrics.get("round_minute_ratio"))
        if round_ratio >= ROUND_MINUTE_WARN:
            advice.append(
                f"{round_ratio:.0%} 的场次开在整点/半点——人不会掐着表开口，"
                f"排期时刻要抖到秒")
        whole = _f(metrics.get("whole_minute_ratio"))
        if whole >= ROUND_MINUTE_WARN:
            advice.append(
                f"{whole:.0%} 的场次秒数是 0——时刻只抖到分钟，秒位一律取整同样是机器痕迹")

        if not advice:
            advice.append(
                f"{count} 场分布在 {hours_used} 个钟头里，规律性 {regularity:.0%}、"
                f"最挤的小时占 {burst:.0%}，都在自然区间内")
        return advice
    except Exception:  # noqa: BLE001 —— 建议生成炸了不该让整份报告消失
        logger.debug("[group_show.schedule] schedule_advice 失败（已忽略）",
                     exc_info=True)
        return advice


# ── 运行期：同一个号的跨群间隔 ──────────────────────────────────────────────
#
# 这条为什么不在 plan_show_times 里做：排期发生在选角**之前**（选角要看开演当时的在线
# 状态、号对余量、指纹分组，那些排期时都还不知道）。硬要在排期阶段绑定账号，就得把选角
# 器整个前移，而前移之后排出来的表在真开演时早已过期。所以拆成两段——排期定「几点」，
# 开演前再用下面两个函数校「这个号此刻能不能开口」。


def _last_spoke(account: str, last_spoke_ts: Any) -> Optional[float]:
    """查这个号上次开口的时间戳；查不到返回 ``None`` ＝ 放行。

    **缺记录一律放行**，不做「没记录就当刚说过」的保守处理：这张表在冷启动、重启、
    换库之后必然是空的，保守解释会让整批号在第一天全部开不了口，而运营对付「全都不发言」
    的唯一手段就是把护栏关掉——那才是真的没有护栏。
    """
    if not account or not isinstance(last_spoke_ts, Mapping):
        return None
    try:
        return _ts(last_spoke_ts.get(account))
    except Exception:  # noqa: BLE001 —— 自定义 Mapping 的 __getitem__ 也可能炸
        return None


def admits_at_time(
    account: Any,
    when_ts: Any,
    last_spoke_ts: Any,
    *,
    min_gap: Any = MIN_CROSS_GROUP_GAP_SEC,
) -> bool:
    """这个号此刻能不能在这个群开口——上次在**别的群**开口是不是太近了。

    Args:
        account: 账号 id。
        when_ts: 打算开口的时刻。
        last_spoke_ts: ``{account_id: 上次开口时间戳}``；**缺记录 ＝ 放行**。
        min_gap: 最小间隔秒数；``<= 0`` ＝ 关闭这条护栏。

    间隔是**有向**的（``when - last``，不取绝对值），这是踩过一次才定下来的：调用方的典型
    用法是按时间顺序扫一遍排期表，边扫边把 ``last_spoke_ts`` 当游标往前推。取绝对值的话，
    某个号被 :func:`next_allowed_ts` 顶到 9:20 之后，表里 9:04 那一场会因为「跟 9:20 也隔了
    16 分钟」而被放行——于是这个号的发言顺序被排成了 9:20、9:04，游标再也推不动，护栏静默
    失效。有向语义下「记录在未来」（时钟漂移，或同一批表里已排定的更晚一场）一律拦下并顶到
    ``last + min_gap``，既保住顺序又偏保守。

    恰好等于 ``min_gap`` 放行：阈值是「至少隔这么久」，边界含在内。差一秒来回拉扯没有
    任何风险意义，却会让排期器在边界上反复顺延。
    """
    try:
        gap = _f(min_gap, float(MIN_CROSS_GROUP_GAP_SEC))
        if gap <= 0:
            return True
        prev = _last_spoke(_s(account), last_spoke_ts)
        if prev is None:
            return True
        return (_f(when_ts) - prev) >= gap
    except Exception:  # noqa: BLE001 —— 判不出来一律放行：护栏不该反过来卡死演出
        logger.debug("[group_show.schedule] admits_at_time 失败（已忽略）",
                     exc_info=True)
        return True


def next_allowed_ts(
    account: Any,
    when_ts: Any,
    last_spoke_ts: Any,
    *,
    min_gap: Any = MIN_CROSS_GROUP_GAP_SEC,
) -> float:
    """被间隔挡下时，最早什么时候能上。

    放行时**原样返回 ``when_ts``**，绝不凭空往后推——调用方拿它当「这场几点开」直接用，
    无条件加个间隔会让整张排期表每过一道护栏就整体后移，几轮下来全被挤出活动时段。

    存在的意义是让调用方能把这场往后**挪**而不是干脆不演：跨群间隔是节奏问题，
    不是「这个号有问题」，取消演出是过度反应（还会让这个群当天彻底没动静）。

    返回值恒 ``>= when_ts``（有向判据的直接推论，见 :func:`admits_at_time`），所以
    「扫排期表 → 顶后 → 更新游标」这个循环是单调的，不会把同一个号的发言顺序排乱。
    """
    at = _f(when_ts)
    try:
        gap = _f(min_gap, float(MIN_CROSS_GROUP_GAP_SEC))
        if gap <= 0:
            return at
        prev = _last_spoke(_s(account), last_spoke_ts)
        if prev is None or (at - prev) >= gap:
            return at
        return prev + gap
    except Exception:  # noqa: BLE001 —— 算不出来就按「现在就能上」，与护栏上线前一致
        logger.debug("[group_show.schedule] next_allowed_ts 失败（已忽略）",
                     exc_info=True)
        return at


__all__ = [
    "BURST_DANGER",
    "BURST_WARN",
    "DAILY_REPEAT_DANGER",
    "DAILY_REPEAT_WARN",
    "DEFAULT_ACTIVE_HOURS",
    "HOT_HOUR_SHOWS",
    "MAX_SHOWS_PER_GROUP_PER_DAY",
    "MIN_CROSS_GROUP_GAP_SEC",
    "MIN_SHOWS_FOR_PERIODICITY",
    "MIN_SHOWS_FOR_STATS",
    "QUIET_DANGER_RATIO",
    "QUIET_HOURS",
    "REGULARITY_DANGER",
    "REGULARITY_TOL",
    "REGULARITY_WARN",
    "ROUND_MINUTES",
    "ROUND_MINUTE_WARN",
    "SAME_GROUP_MIN_GAP_SEC",
    "SLOT_JITTER_SHARE",
    "admits_at_time",
    "in_quiet_hours",
    "next_allowed_ts",
    "plan_show_times",
    "schedule_advice",
    "schedule_metrics",
]
