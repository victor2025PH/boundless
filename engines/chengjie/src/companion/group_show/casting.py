"""群戏选角 —— 把剧本角色槽映射到「账号 × 人设」，安全约束凌驾于表现力之上。

**为什么这个模块的约束是承重墙**：多号在同一个群里演戏，最致命的失败不是「演得不像」，
而是被平台**关联判定为机器人集群一锅端**。而关联的第一杀手是**设备/网络指纹同源**——
同一指纹组的两个号同时出现在一个群里说话，在平台侧几乎等于明牌自首，一封封一串。
所以 :func:`cast_roles` 宁可让角色槽空着（进 ``unfilled`` 降级演），也绝不把同指纹组的
两个号放上同一张演员表。这条是**硬约束**，任何软优化（性别搭配之类）都不得越过它。

**「指纹未知」一律按「同组」处理**（:data:`SHARED_HOST_GROUP`）。这条容易被写反，所以单独
拎出来讲：把未知的号当成各自独立看似只是宽松一点，实际上是把最典型的一锅端姿势伪装成了
安全配置——一台机器上 N 个没配代理的号会被判成 N 个互不相干的组、全部准许同台，而它们
共享同一个公网 IP。**未知不等于安全**。真实指纹归属请用
:func:`~src.companion.group_show.linkage.derive_fingerprint_groups` 从账号注册表派生。

其余设计取舍：

* **确定性**：同输入必产同输出——选角要能离线 dry-run 复现、要能进回归门禁，一旦掺
  ``random`` 就再也钉不住「这场戏为什么是这几个号」。需要「别每场都让同一个号当主推」
  的轮换感时，用 ``zlib.crc32(剧本id|角色槽|账号)`` 做确定性打散：换一场戏自然换一批人，
  但同一场戏重跑一万次结果一致。
* **降级而非报错**：号不够、候选数据脏，是运营常态而不是异常。填不上的槽进 ``unfilled``
  交给导演决定「少个人也演 / 这场先不演」；本模块**不向调用方抛异常**（本仓一贯风格：
  宁可保守降级也不要把编排链炸掉）。
* **纯函数**：无 I/O、无全局状态。候选池由调用方从账号编排器捞好后传进来，本模块只做
  「给定这批号，怎么分角色最安全」这一件事。

一条值得记住的结构性结论：因为每选中一个号就锁死它整个指纹组，**每次选人恰好消耗一个
可用指纹组**，所以最终能填上的槽数恒等于 ``min(角色槽数, 可用指纹组数)``，与挑人顺序、
与性别软优化**都无关**。这保证了软优化永远不会偷走一个本可填上的槽。
"""
from __future__ import annotations

import logging
import zlib
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from src.companion.group_show.attendance import NORMAL_PAIR_CO
from src.companion.group_show.linkage import DEFAULT_HOST_TAG, HOST_GROUP_PREFIX
from src.companion.group_show.performance import (
    added_co_performance,
    admits_speaker,
)
from src.companion.group_show.playbook import CastMember, Casting, Playbook
from src.companion.group_show.roles import (
    NORMAL_ROLE_CO,
    admits_role,
    role_headroom,
)

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────────────────────────

#: 唯一被认可的健康态。**缺 health 字段一律按不可用处理**——群戏是多号同台的高危动作，
#: 「不知道这号还活着没」与「知道它挂了」在风险上应当同权，宁可少上一个人。
HEALTH_OK = "online"

#: 指纹归属未知时的兜底组。**所有未知的号共用同一个组名**，于是它们彼此互斥、最多上一个。
#: 语义是「不知道这几个号是不是同一个网络出口，那就假定它们是」——理由见模块 docstring
#: 里对「未知 ≠ 安全」的说明。
SHARED_HOST_GROUP = f"{HOST_GROUP_PREFIX}:{DEFAULT_HOST_TAG}"

#: 角色槽填坑优先级（数字越小越先分配到人）。号不够时按这个顺序砍人：
#: advocate 种草角缺位＝这场戏没有落点，等于白演；asker 不抛痛点，advocate 只能硬广；
#: skeptic 撑可信度（一边倒好评最假）；bystander 附和角锦上添花，第一个被砍。
SLOT_PRIORITY: Dict[str, int] = {
    "advocate": 0,
    "asker": 1,
    "skeptic": 2,
    "bystander": 3,
}

#: 剧本自定义槽统一排在标准四角之后，内部按剧本声明顺序（作者写在前面的先填）。
_CUSTOM_SLOT_RANK = 100

#: 少于这个人数的戏会显单薄（两个人一唱一和的「双簧感」很扎眼），出软警告。
MIN_HEALTHY_CAST = 3

#: 性别写法归一（运营手填数据什么都有，别让 "男"/"M" 各算一个性别桶）。
_GENDER_ALIASES: Dict[str, str] = {
    "m": "male", "male": "male", "man": "male", "男": "male", "男性": "male",
    "f": "female", "female": "female", "woman": "female",
    "女": "female", "女性": "female",
}


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _s(value: Any) -> str:
    """任意值 → 干净字符串（``None``/异常对象一律成空串，绝不抛）。"""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:  # noqa: BLE001 —— 候选是外部脏数据，__str__ 都可能炸
        return ""


def _int(value: Any, default: int = 0) -> int:
    """任意值 → int（运营手填/URL 参数什么都可能传进来，绝不抛）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_gender(value: Any) -> str:
    """归一性别标记；无法识别的非空值保留为独立桶（如 "nb"），空值＝未知。"""
    raw = _s(value).lower()
    if not raw:
        return ""
    return _GENDER_ALIASES.get(raw, raw)


def _gender_map(persona_gender: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """``{persona_id: 性别}`` 归一；传了非 Mapping 的垃圾就当没传。"""
    out: Dict[str, str] = {}
    if not isinstance(persona_gender, Mapping):
        return out
    for pid, gender in persona_gender.items():
        key = _s(pid)
        val = _normalize_gender(gender)
        if key and val:
            out[key] = val
    return out


def _override_map(fingerprint_groups: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """外部指纹表归一。键同时接受 ``account_id`` 与 ``platform:account_id``——
    调用方两种写法都很自然，为这个细节让人 debug 一小时不值得。"""
    out: Dict[str, str] = {}
    if not isinstance(fingerprint_groups, Mapping):
        return out
    for acc, group in fingerprint_groups.items():
        key = _s(acc)
        val = _s(group)
        if key and val:
            out[key] = val
    return out


def _declared_slots(playbook: Any) -> Tuple[str, ...]:
    """剧本声明的角色槽（按声明序去重）。剧本里重复声明同一槽是 playbook 层的问题，
    选角这边只当一个槽处理，不因此拒演。"""
    slots: List[str] = []
    for role in (getattr(playbook, "roles", ()) or ()):
        slot = _s(getattr(role, "slot", "")) or _s(role)
        if slot and slot not in slots:
            slots.append(slot)
    return tuple(slots)


def _rotation_key(playbook_id: str, slot: str, account_key: str,
                  seed: str = "") -> int:
    """确定性轮换键：同一账号在不同剧本/角色槽/**场次**下排序位次不同，于是「谁演主推」
    会自然轮转，而**同一场戏重跑结果完全一致**（这正是不用 random 的原因）。

    ``seed`` 通常传群 key。少了它，同一剧本的第一个槽**永远**由同一个号来演——第一个
    槽选人时场上还没人、同台历史一律为 0，排序就完全落到这个键上，而它此前只认
    剧本+槽位，跨群恒定。实测代价（8 个号 / 每场 2 人 / 号对上限 3）：28 个号对里只有
    7 个被用到，安全场次从 84 掉到 21——**四分之三的安全余量被浪费在同一个号身上**，
    而且那个号在每个群里都是主推（内容指纹层面同样刺眼）。
    """
    raw = f"{playbook_id}|{slot}|{seed}|{account_key}".encode("utf-8", "ignore")
    return zlib.crc32(raw)


# ── 候选池 ──────────────────────────────────────────────────────────────────


def _stickiness(prior_slots: Optional[Mapping[str, str]],
                account_id: str, slot: str) -> int:
    """群内角色粘性分（越大越优先）：2＝上次在本群就演这个槽；1＝本群没演过；
    0＝在本群演过**别的**槽（翻脸位，最后才轮到它）。

    角色轮转（``role_headroom`` 轴）是**跨群**语义——防单号在很多群长期当主推；
    可它在**同一个群**里会主动制造翻脸：上一场演过 advocate 的号余量变小，下一场
    恰好被轮去演 skeptic（2026-07-27 双号灰度实录，两场之间立场对调）。群里的
    真人翻两屏聊天记录就能看出「昨天质疑今天安利」。粘性排在轮转**之前**：
    群内粘住、跨群照转，两条轴各管各的。
    """
    if not prior_slots:
        return 1
    prior = _s((prior_slots or {}).get(_s(account_id)))
    if not prior:
        return 1
    return 2 if prior == _s(slot) else 0


def _normalize_candidate(
    raw: Any,
    overrides: Mapping[str, str],
    *,
    allow_shared_host: bool = False,
) -> Optional[Dict[str, str]]:
    """单个候选归一；不合格返回 ``None``（调用方直接丢弃）。

    丢弃条件只有两条，都是「留下来反而更危险」：
    1. 没有 ``account_id``——发不出消息，更要命的是无法做去重与指纹归组；
    2. ``health`` 不是 ``online``——含字段缺失，理由见 :data:`HEALTH_OK`。

    指纹组解析优先级：外部覆盖表 > 候选自带 ``fingerprint_group`` > :data:`SHARED_HOST_GROUP`
    兜底。**兜底把所有指纹未知的号并进同一组**，于是它们最多只能上一个。

    这条兜底方向是本模块最重要的一行代码，值得解释：早期实现拿 ``account_id`` 当兜底组名，
    等于「不知道就算它独立」。看似只是宽松一点，实际后果是——一台机器上 N 个没配代理的号
    会被判成 N 个互不相干的指纹组、**全部准许同台**，而它们共享同一个公网 IP，在同一个群里
    一唱一和。那不是「多空一个槽」的保守偏差，那是把最典型的一锅端姿势当成了安全配置。
    真实数据佐证：本仓 2026-07 账号注册表 9 个号的 ``proxy_id`` / ``fingerprint_id`` 全为空串。

    ``allow_shared_host=True`` 恢复旧的「各自独立」语义，**只允许 dry-run 排练使用**
    （排练一条消息都不发，零关联风险，却会因为全部同组而只剩一个演员，戏根本排不起来）。
    真发路径传 True ＝ 自行解除唯一一道防关联硬闸，不要这么做。
    """
    if not isinstance(raw, Mapping):
        return None
    account_id = _s(raw.get("account_id"))
    if not account_id:
        return None
    if _s(raw.get("health")).lower() != HEALTH_OK:
        return None
    platform = _s(raw.get("platform")).lower() or "telegram"
    account_key = f"{platform}:{account_id}"
    group = (
        _s(overrides.get(account_id))
        or _s(overrides.get(account_key))
        or _s(raw.get("fingerprint_group"))
    )
    if not group:
        group = account_id if allow_shared_host else SHARED_HOST_GROUP
    return {
        "account_id": account_id,
        "platform": platform,
        "persona_id": _s(raw.get("persona_id")),
        "display_name": _s(raw.get("display_name")),
        "account_key": account_key,
        "fingerprint_group": group,
    }


def eligible_candidates(
    candidates: Optional[Sequence[Any]],
    *,
    fingerprint_groups: Optional[Mapping[str, str]] = None,
    allow_shared_host: bool = False,
) -> List[Dict[str, str]]:
    """把原始候选列表洗成「可上台的号」（健康过滤 + 同号去重 + 指纹组归属解析）。

    保持输入顺序返回（同一账号重复登记以**首条**为准），真正的挑选顺序在
    :func:`cast_roles` 里按角色槽逐个算，所以这里不排序。

    ``allow_shared_host`` 语义见 :func:`_normalize_candidate`——**仅 dry-run 可传 True**。

    候选池整体不可用时（编排器返回了标量/错误对象等非序列）返回**空池**而非抛异常：
    本模块对外承诺不抛（见模块 docstring），而看板与预检类调用方通常直接调它、不经
    :func:`cast_roles` 那层兜底，正是最容易踩到的入口。这里刻意丢弃已归一的半个池——
    「来路不明的半张演员表」比「没人上台」危险得多，与 :func:`cast_roles` 的降级同款。
    """
    overrides = _override_map(fingerprint_groups)
    pool: List[Dict[str, str]] = []
    seen: Set[str] = set()
    try:
        for raw in (candidates or ()):
            cand = _normalize_candidate(
                raw, overrides, allow_shared_host=allow_shared_host)
            if cand is None or cand["account_key"] in seen:
                continue
            seen.add(cand["account_key"])
            pool.append(cand)
    except Exception:  # noqa: BLE001 —— 候选池是外部数据，形状什么样都可能
        # 用 warning：这是调用方传错了东西（而非单条脏候选），量小且值得被看见。
        logger.warning(
            "[group_show.casting] 候选池不可遍历，本次按空池降级（type=%s）",
            type(candidates).__name__, exc_info=True)
        return []
    return pool


def slot_fill_order(playbook: Any) -> Tuple[str, ...]:
    """角色槽的填坑顺序：标准四角按 :data:`SLOT_PRIORITY`，自定义槽按声明序排其后。"""
    declared = _declared_slots(playbook)
    index = {slot: i for i, slot in enumerate(declared)}
    return tuple(sorted(
        declared,
        key=lambda s: (SLOT_PRIORITY.get(s, _CUSTOM_SLOT_RANK), index[s]),
    ))


# ── 选角 ────────────────────────────────────────────────────────────────────


def _pick_for_slot(
    pool: Sequence[Dict[str, str]],
    slot: str,
    playbook_id: str,
    used_accounts: Set[str],
    used_groups: Set[str],
    cast_genders: Set[str],
    gender_by_persona: Mapping[str, str],
    co_counts: Optional[Mapping[Tuple[str, str], int]] = None,
    chosen_ids: Optional[Sequence[str]] = None,
    co_limit: int = 0,
    seed: str = "",
    role_counts: Optional[Mapping[Tuple[str, str], int]] = None,
    role_limit: int = 0,
    prior_slots: Optional[Mapping[str, str]] = None,
) -> Optional[Dict[str, str]]:
    """给一个角色槽挑人：先过硬约束，再按「同台历史」排序，最后做性别软优化。

    排序主键是 :func:`~src.companion.group_show.performance.added_co_performance`
    ——这个号跟本场已选的人**在多少个群里同台开过口**。优先挑没同台过的，于是同一批号
    反复演戏时，实际产生的「两个号在同一群都发过言」会被摊薄到不同的号对上。没传台账
    时该项恒为 0，排序完全退化成原来的确定性轮换（既有行为一字不变）。

    ``co_limit`` 把这条从**软排序**升级成**硬闸门**：已经跟场上某人同台到线的号直接
    出局（:func:`~src.companion.group_show.performance.admits_speaker`）。排序只能保证
    「尽量挑好的」，当所有候选都很差时它照样会挑一个上——闸门管的正是这种时候。

    ``role_limit`` 是**第三条轴**的闸门（:func:`~src.companion.group_show.roles.admits_role`）：
    已经在太多群里演过主推的号不再接主推。它与上面两条正交——前两条是**号对**口径
    （「这几个号是不是一伙的」），这条是**单号**口径（「这个号自己是不是个托」）。
    一个号不需要任何同伙就能被认成托，所以号对轴排得再稀疏也拦不住它。掩护角色
    （路人/提问/质疑）恒放行，理由见 ``roles`` 模块 docstring。

    性别软优化只在「已选中的人清一色同一个已知性别」时才启动，并且**只是在已经通过硬
    过滤的候选里换一个挑**——因为每次选人恒定消耗一个指纹组（见模块 docstring 的结论），
    换谁都不会让后面少填一个槽，所以这个优化是零代价的。它排在同台历史之后：性别齐整
    只是「看着假」，同台共现是**会被算法抓的**，孰轻孰重很清楚。
    """
    picked = list(chosen_ids or ())
    available = [
        c for c in pool
        if c["account_key"] not in used_accounts
        and c["fingerprint_group"] not in used_groups
        and admits_speaker(c["account_id"], picked, co_counts, limit=co_limit)
        and admits_role(c["account_id"], slot, role_counts, limit=role_limit)
    ]
    if not available:
        return None
    # 第二主键取「角色余量还剩多少」的**负值**＝余量大的先上：主推轴的容量是线性的
    # （号数 × NORMAL_ROLE_CO），买不到杠杆，只能靠均摊把它用满。同台历史相同时优先
    # 挑没怎么演过主推的号，主推就自然轮着来了；掩护角色的余量恒为满值、该项无影响。
    # 粘性分排在同台历史之后、主推轮转之前：共现是算法抓的（最重），群内翻脸是
    # 真人肉眼看的（次重），轮转均衡是长线卫生（最轻）——见 _stickiness docstring。
    available.sort(key=lambda c: (
        added_co_performance(c["account_id"], picked, co_counts),
        -_stickiness(prior_slots, c["account_id"], slot),
        -role_headroom(c["account_id"], slot, role_counts, limit=role_limit),
        _rotation_key(playbook_id, slot, c["account_key"], seed),
        c["account_key"],
    ))

    # 清一色同性别的演员表一眼假（真群里不可能五个人全是女生在夸同一个产品）
    if len(cast_genders) == 1:
        only = next(iter(cast_genders))
        for cand in available:
            gender = gender_by_persona.get(cand["persona_id"], "")
            if gender and gender != only:
                return cand
    return available[0]


#: 空槽的原因码（前端按码取文案，别把中文写进数据）。
BLOCKED_POOL = "pool"        # 号不够 / 剩下的全被指纹组锁死
BLOCKED_BUDGET = "budget"    # 开口人数已到预算（护栏在正常工作，不该动）
BLOCKED_PAIR = "pair"        # 与场上的人同台已到线
BLOCKED_ROLE = "role"        # 主推角色已在太多群演过


def _blocked_reason(
    pool: Sequence[Dict[str, str]],
    slot: str,
    used_accounts: Set[str],
    used_groups: Set[str],
    co_counts: Optional[Mapping[Tuple[str, str], int]],
    picked: Sequence[str],
    co_limit: int,
    role_counts: Optional[Mapping[Tuple[str, str], int]],
    role_limit: int,
) -> str:
    """这个槽为什么没人上：``pool`` / ``pair`` / ``role``（只在填不上时才调）。

    四种空槽长得一模一样，可处置完全相反——号不够要补号，闸门拦的是护栏在正常工作。
    分不出来的后果是运营看着戏一天天变冷清、找不到原因，最后「把护栏关了试试」。

    判定顺序即因果顺序：先看还有没有人可选（没有 ＝ ``pool``，两条闸门根本没轮到），
    再看是不是全被号对闸门挡掉，最后才归给角色闸门。混合命中时报**号对**——它是号池
    级的结构性问题（得换一批号去铺新群），角色到线只需要把种草角色让给别人，轻得多。
    """
    remaining = [
        c for c in pool
        if c["account_key"] not in used_accounts
        and c["fingerprint_group"] not in used_groups
    ]
    if not remaining:
        return BLOCKED_POOL
    if not any(admits_speaker(c["account_id"], picked, co_counts, limit=co_limit)
               for c in remaining):
        return BLOCKED_PAIR
    return BLOCKED_ROLE


def cast_roles(
    playbook: Playbook,
    candidates: Sequence[Dict[str, Any]],
    *,
    fingerprint_groups: Optional[Mapping[str, str]] = None,
    persona_gender: Optional[Mapping[str, str]] = None,
    allow_shared_host: bool = False,
    co_performance: Optional[Mapping[Tuple[str, str], int]] = None,
    max_speakers: int = 0,
    co_limit: int = NORMAL_PAIR_CO,
    seed: str = "",
    role_counts: Optional[Mapping[Tuple[str, str], int]] = None,
    role_limit: int = NORMAL_ROLE_CO,
    prior_slots: Optional[Mapping[str, str]] = None,
) -> Casting:
    """把剧本角色槽分配给候选账号，返回 :class:`Casting`。

    参数
    ----
    playbook:
        待演剧本，只读 ``id`` 与 ``roles``（beats 与选角无关）。
    candidates:
        候选账号列表，每项形如 ``{"account_id", "platform", "persona_id",
        "display_name", "health", "fingerprint_group"}``；字段缺失容错。
    fingerprint_groups:
        ``{account_id: 指纹组}`` 外部表，**覆盖**候选自带的 ``fingerprint_group``；
        用于「指纹归属由设备台账说了算，不信候选里那份快照」的场景。
    persona_gender:
        ``{persona_id: "male"/"female"}``，只用于性别搭配软优化；缺失＝跳过该优化。
    allow_shared_host:
        指纹未知的号是否按「各自独立」处理。默认 ``False``＝并入同一兜底组（最多上一个）。
        **只有 dry-run 排练才允许传 True**，理由见 :func:`_normalize_candidate`。
        想拿到真实指纹归属，用 :func:`~src.companion.group_show.linkage.derive_fingerprint_groups`
        从账号注册表派生一张表喂给 ``fingerprint_groups``，而不是把这个开关打开。
    co_performance:
        ``{(号A, 号B): 两个号共同开过口的群数}``，由
        :func:`~src.companion.group_show.performance.co_performance` 从演出台账算出。
        传了就**优先挑与已选演员没同台过的号**，让共现摊薄到不同号对上；不传＝退化成
        原来的确定性轮换。这是成员矩阵冻结之后唯一还能动的暴露面（见 ``performance``
        模块 docstring）。
    max_speakers:
        本场最多让几个号开口（``<=0`` ＝不限，按剧本声明的槽数走）。**这是比号池更强的
        杠杆**：共现对次是 ``C(开口人数, 2)``，10 个号每场 3 人开口只能安全覆盖 30 个群，
        降到 2 人就能覆盖 90 个。超出的槽按 :data:`SLOT_PRIORITY` 砍，进 ``unfilled``
        走既有的降级演路径。预算算法见
        :func:`~src.companion.group_show.performance.speaker_budget`。
    co_limit:
        **单个号对**最多允许同台到几个群（默认 :data:`~src.companion.group_show.attendance.NORMAL_PAIR_CO`，
        ``<=0`` ＝关闭）。到线的号对不再一起上台，宁可让槽空着降级演。
        没传 ``co_performance`` 时这条恒不生效——**无台账＝无行为变化**，护栏默认开着
        也不会改动任何既有调用；一旦喂了真台账，保护自动就位，不需要谁记得去打开。
        它与 ``max_speakers`` 是「反应性兜底」与「预测性预防」的关系，见
        :func:`~src.companion.group_show.performance.resolve_speaker_cap`。
    seed:
        轮换扰动（**传群 key**）。不传的话同一剧本的第一个槽跨群恒定由同一个号来演：
        第一个槽选人时场上还没人、同台历史全为 0，排序完全落到确定性轮换键上。实测
        代价是安全场次缩水到四分之一（8 个号 / 每场 2 人：84 → 21），且那个号在每个
        群里都是主推。见 :func:`_rotation_key`。
    role_counts:
        ``{(号, 角色槽): 这个号在几个群演过这个角色}``，由
        :meth:`~src.companion.group_show.store.GroupShowStore.role_ledger` 派生。
        传了就**优先挑没怎么演过主推的号**，并在到线时把它挡在主推之外。
    role_limit:
        单个号最多在几个群演**推销角色**（默认
        :data:`~src.companion.group_show.roles.NORMAL_ROLE_CO`，``<=0`` ＝关闭）。
        与 ``co_limit`` 正交：那条防「这几个号是一伙的」，这条防「这个号自己是个托」。
        同样地，没传 ``role_counts`` 时恒不生效——**无台账＝无行为变化**。
    prior_slots:
        ``{号: 上次在目标群演的槽}``，由
        :meth:`~src.companion.group_show.store.GroupShowStore.prior_slots` 派生。
        传了就**群内粘住角色**：上次演过本槽的号优先、在本群演过别的槽的号最后
        （防「上一场泼冷水、下一场安利」的当群翻脸，2026-07-27 双号灰度实录）。
        跨群轮转不受影响——这本台账是按目标群查的。不传＝无行为变化。

    硬约束（任何情况下都成立）
    --------------------------
    1. 同一指纹组最多出一个号（关联封号第一杀手，见模块 docstring）；
    2. 非 ``online`` 的号不上台；
    3. 一个账号只演一个角色；
    4. 号不够就按 :data:`SLOT_PRIORITY` 保重要角色，其余进 ``unfilled``；
    5. 确定性：同输入同输出，且**与候选列表的先后顺序无关**。

    ``members`` 按剧本**声明序**返回（方便人对着剧本读），``unfilled`` 同理——
    分配顺序是优先级序，但输出顺序是声明序，两者刻意分开。
    """
    declared = _declared_slots(playbook)
    try:
        pool = eligible_candidates(
            candidates, fingerprint_groups=fingerprint_groups,
            allow_shared_host=allow_shared_host)
        playbook_id = _s(getattr(playbook, "id", ""))
        gender_by_persona = _gender_map(persona_gender)

        used_accounts: Set[str] = set()
        used_groups: Set[str] = set()
        cast_genders: Set[str] = set()
        chosen: Dict[str, Dict[str, str]] = {}
        chosen_ids: List[str] = []
        cap = max(0, _int(max_speakers))

        blocked: List[Tuple[str, str]] = []
        for slot in slot_fill_order(playbook):
            if cap and len(chosen) >= cap:
                # 开口人数已到预算：**仍然走完循环**，让剩下的槽如实进 unfilled，
                # 而不是提前 break——导演要能看出「这场是被预算压成几个人的」。
                blocked.append((slot, BLOCKED_BUDGET))
                continue
            cand = _pick_for_slot(
                pool, slot, playbook_id, used_accounts, used_groups,
                cast_genders, gender_by_persona, co_performance, chosen_ids,
                _int(co_limit), _s(seed), role_counts, _int(role_limit),
                prior_slots=prior_slots,
            )
            if cand is None:
                # 号已用尽（或剩下的全被指纹/闸门锁死）——后面的低优先级槽多半也填不上，
                # 但仍然走完循环，让 unfilled 如实反映全部空槽，并逐槽记下原因：
                # 四种空槽长得一样、处置却相反，糊在一起运营只能靠猜。
                blocked.append((slot, _blocked_reason(
                    pool, slot, used_accounts, used_groups, co_performance,
                    chosen_ids, _int(co_limit), role_counts, _int(role_limit))))
                continue
            chosen[slot] = cand
            used_accounts.add(cand["account_key"])
            used_groups.add(cand["fingerprint_group"])
            chosen_ids.append(cand["account_id"])
            gender = gender_by_persona.get(cand["persona_id"], "")
            if gender:
                cast_genders.add(gender)

        members = tuple(
            CastMember(
                slot=slot,
                account_id=chosen[slot]["account_id"],
                persona_id=chosen[slot]["persona_id"],
                display_name=chosen[slot]["display_name"],
                platform=chosen[slot]["platform"],
            )
            for slot in declared if slot in chosen
        )
        unfilled = tuple(slot for slot in declared if slot not in chosen)
        # 按**声明序**输出（与 unfilled 同口径），分配顺序是优先级序、两者刻意分开
        reasons = dict(blocked)
        return Casting(
            members=members, unfilled=unfilled,
            blocked=tuple((slot, reasons[slot]) for slot in unfilled
                          if slot in reasons))
    except Exception:  # noqa: BLE001 —— 选角炸了也不能拖垮编排链
        # 最保守的降级：谁也不上台，全部槽标成空。导演看到空演员表会判定这场不演，
        # 这比「带着半张来路不明的演员表硬开」安全得多。
        return Casting(members=(), unfilled=declared)


# ── 校验 ────────────────────────────────────────────────────────────────────


def validate_casting(
    casting: Casting,
    playbook: Playbook,
    *,
    fingerprint_groups: Optional[Mapping[str, str]] = None,
    persona_gender: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """校验演员表，返回问题列表（空＝通过）。

    与 :func:`~src.companion.group_show.playbook.validate_playbook` 同款约定：
    **硬错**直接返回文案（这场戏不该开），**软警告**以 ``warn:`` 前缀返回（能演但会打折）。

    这是一道**防御性复查**：正常情况下 :func:`cast_roles` 产出的演员表不可能违反硬约束，
    但演员表可能来自持久化状态、人工编辑或旧版本代码，所以开演前再验一次指纹与分身。
    注意 :class:`Casting` 本身不携带指纹信息，因此指纹复查只在传入 ``fingerprint_groups``
    时进行（不传＝跳过，绝不假装检查过）。
    """
    problems: List[str] = []
    try:
        members = tuple(getattr(casting, "members", ()) or ())
        if not members:
            return ["演员表为空——一个可用号都没有，这场戏不能开"]

        declared = set(_declared_slots(playbook))
        filled = [_s(getattr(m, "slot", "")) for m in members]

        # ① 一号不能分饰两角：同一个号在群里自问自答，是比指纹更容易被人眼抓到的破绽
        by_account: Dict[str, List[str]] = {}
        for member in members:
            key = _s(getattr(member, "account_key", "")) or _s(
                getattr(member, "account_id", ""))
            by_account.setdefault(key, []).append(_s(getattr(member, "slot", "")))
        for account, slots in sorted(by_account.items()):
            if len(slots) > 1:
                problems.append(
                    f"账号 {account} 同时占了 {'/'.join(slots)}——一号不能分饰两角")

        # ② 角色槽重复分配（两个号抢同一个槽，导演按槽取人会取到谁全看运气）
        for slot in sorted({s for s in filled if filled.count(s) > 1}):
            problems.append(f"角色槽 {slot} 被重复分配给多个号")

        # ③ 演员表里出现剧本没声明的槽 → 导演永远不会给它下 directive，这号只会干坐着
        if declared:
            for slot in sorted({s for s in filled if s and s not in declared}):
                problems.append(f"演员表出现剧本未声明的角色槽: {slot}")

        # ④ 核心角缺位＝这场戏没有落点，白演还白担风险
        if "advocate" not in filled:
            if "advocate" in declared:
                problems.append("核心角色 advocate 未配上——种草角缺位，这场戏没有落点")
            else:
                problems.append(
                    "核心角色 advocate 未配上：剧本压根没声明该槽，先补剧本再选角")

        # ⑤ 指纹复查（关联封号第一杀手，能查就必须查）
        #
        #    :class:`Casting` 本身不携带指纹信息，所以这一步依赖外部表。没传表时**如实说
        #    「没查」**而不是静默放行——「跳过了一项安全检查」和「通过了一项安全检查」在
        #    调用方眼里长得一模一样，这种沉默正是事故的温床。
        overrides = _override_map(fingerprint_groups)
        if not overrides:
            problems.append(
                "warn: 未传指纹表，本次跳过了同台关联复查（真发前请用 "
                "linkage.derive_fingerprint_groups 派生后再验一次）")
        else:
            seen_group: Dict[str, str] = {}
            for member in members:
                account_id = _s(getattr(member, "account_id", ""))
                account_key = _s(getattr(member, "account_key", ""))
                # 表里查不到的号＝指纹归属未知＝并进兜底组，于是两个未知号会在这里撞出
                # 硬错。这是有意为之：外部表没覆盖到的号，没有任何依据断言它们互相独立。
                group = (
                    _s(overrides.get(account_id))
                    or _s(overrides.get(account_key))
                    or SHARED_HOST_GROUP
                )
                if group in seen_group:
                    problems.append(
                        f"同指纹组 {group} 撞车：{seen_group[group]} 与 {account_id} "
                        f"不能同台（关联封号第一杀手）")
                else:
                    seen_group[group] = account_id

        # ⑥ 以下均为软警告：能演，但戏的质量/可信度打折
        if "skeptic" not in filled:
            problems.append("warn: 缺 skeptic 质疑角——一边倒好评反而假，可信度打折")

        if len(members) < MIN_HEALTHY_CAST:
            problems.append(
                f"warn: 演员只有 {len(members)} 个（<{MIN_HEALTHY_CAST}），"
                f"戏会显单薄，像双簧")

        gender_by_persona = _gender_map(persona_gender)
        known = [
            gender_by_persona.get(_s(getattr(m, "persona_id", "")), "")
            for m in members
        ]
        known = [g for g in known if g]
        if len(known) >= 2 and len(set(known)) == 1:
            problems.append(
                f"warn: 演员清一色 {known[0]}——真群里不会这么齐整，建议混搭性别")

        no_persona = sorted({
            _s(getattr(m, "account_id", "")) for m in members
            if not _s(getattr(m, "persona_id", ""))
        })
        if no_persona:
            problems.append(
                f"warn: 这些号没绑人设，台词会退化成默认口吻: {', '.join(no_persona)}")

        unfilled = tuple(
            _s(s) for s in (getattr(casting, "unfilled", ()) or ()) if _s(s))
        rest = [s for s in unfilled if s != "advocate"]
        if rest:
            problems.append(f"warn: 未配上的角色槽: {', '.join(rest)}（号不够，降级演）")

        return problems
    except Exception as exc:  # noqa: BLE001 —— 校验器自己炸也必须给出「不通过」结论
        problems.append(f"选角校验异常，按不通过处理: {exc!r}")
        return problems


__all__ = [
    "BLOCKED_BUDGET",
    "BLOCKED_PAIR",
    "BLOCKED_POOL",
    "BLOCKED_ROLE",
    "HEALTH_OK",
    "MIN_HEALTHY_CAST",
    "SHARED_HOST_GROUP",
    "SLOT_PRIORITY",
    "cast_roles",
    "eligible_candidates",
    "slot_fill_order",
    "validate_casting",
]
