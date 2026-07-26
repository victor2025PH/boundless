"""群戏剧本 Schema —— 本包所有模块的数据契约（单一事实源）。

剧本是**纯数据**（YAML）：每一拍（beat）只声明「谁、什么意图、可选产品/物料/软广强度」，
**绝不写死台词**。台词由该角色所绑人设的 LLM 现场生成——这是「同一剧本每次演出都不同」
的根基，也是反模板化封号的第一道设计。

结构：``Playbook``（一场戏） → ``roles``（角色槽） + ``beats``（话题弧线）。
运行期由 ``casting`` 把角色槽映射到「账号 × 人设」（``Casting``/``CastMember``），
``director`` 按 beat 产出 ``BeatDirective`` 喂给生成层，事件流落 ``ShowEvent``。

四角标准戏（``asker`` 抛痛点 / ``advocate`` 种草 / ``bystander`` 附和 / ``skeptic``
质疑）中 **skeptic 是可信度关键角**：全是好评反而假，必须有人泼冷水再被化解。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 常量表 ──────────────────────────────────────────────────────────────────

#: 四角标准戏的角色槽（剧本可只用其中几个，但 beats 引用的槽必须已声明）
STANDARD_SLOTS: Tuple[str, ...] = ("asker", "advocate", "bystander", "skeptic")

#: 产品系（与官网 brand 对齐：growth 获客系 / studio 内容陪伴系 / lingo 翻译系）
VALID_SYSTEMS: Tuple[str, ...] = ("growth", "studio", "lingo")

#: 节拍语速档 → 基础间隔秒（``pacing`` 在此之上叠泊松抖动与打字时长）
PACE_BASE_SECONDS: Dict[str, float] = {
    "chatty": 25.0,   # 你一言我一语，抢着说
    "normal": 60.0,   # 常规群聊节奏
    "slow": 130.0,    # 有人思考/刚看到消息
}

#: 事件类型：真发一条台词 / 发物料 / 真人插话 / 导演让路 / 收尾
EVENT_KINDS: Tuple[str, ...] = ("line", "media", "human", "yield", "terminate")

#: 其中「我们的号真在群里出过声」的那几种。**这条判据只能有一处定义**：它同时决定
#: 发言均衡（:meth:`ShowState.lines_by`）与演出矩阵（``performance`` 的暴露口径），
#: 两处各写一遍必然漂——而漂的后果是看板报的暴露量与实际发出的消息对不上。
#: ``human`` 是真人插话（``speaker_account`` 根本不是我们的号），``yield``/``terminate``
#: 是导演的内部动作，平台侧一个字都看不到，一律不算。
SPOKEN_KINDS: Tuple[str, ...] = ("line", "media")

#: 场次状态机
SHOW_STATUSES: Tuple[str, ...] = (
    "pending", "running", "yielded", "done", "aborted")

#: 所有 directive 默认携带的禁止项——2026-07-25 群聊灰度实测三连击的直接教训
#: （进群自我介绍像私聊开场 / 把群里闲聊当「对你说」/ 答非所问长篇输出）。
DEFAULT_MUST_NOT: Tuple[str, ...] = (
    "不要自我介绍，也不要用私聊式亲昵开场",
    "不要发链接刷屏，不要用广告腔硬推",
    "不要提及或编造你和某个群成员的私聊经历",
    "不要长篇大论，像群友插话一两句为宜",
)

#: 软广强度上限（红区群由 runtime 另行锁死，这里只做 schema 边界）
SOFT_AD_MAX = 10


# ── 数据结构 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Role:
    """角色槽声明。``slot`` 是 beats 的引用键，``desc`` 供选角与人工阅读。"""

    slot: str
    desc: str = ""


@dataclass(frozen=True)
class Beat:
    """一个话题节拍。

    - ``role``：由哪个角色槽发言；
    - ``intent``：**意图**而非台词（"抛痛点：多号管理切到崩溃"）；
    - ``product``：本拍软植入的产品 id（缺省＝纯闲聊拍，不带货）；
    - ``media``：本拍要甩的物料 key（产品卡/截图/K 线图…）；
    - ``soft``：本拍软广强度覆盖（-1＝沿用 playbook 级 ``soft_ad_level``）；
    - ``pace``：语速档，见 :data:`PACE_BASE_SECONDS`。
    """

    id: str
    role: str
    intent: str
    product: str = ""
    media: str = ""
    soft: int = -1
    pace: str = "normal"

    def effective_soft(self, playbook_soft: int) -> int:
        """本拍生效的软广强度（拍级覆盖优先，否则用剧本级）。"""
        return int(self.soft) if self.soft >= 0 else int(playbook_soft)


@dataclass(frozen=True)
class Playbook:
    """一场戏的完整剧本。"""

    id: str
    name: str
    system: str = ""
    products: Tuple[str, ...] = ()
    soft_ad_level: int = 5
    roles: Tuple[Role, ...] = ()
    beats: Tuple[Beat, ...] = ()

    @property
    def slots(self) -> Tuple[str, ...]:
        return tuple(r.slot for r in self.roles)

    def beat_at(self, cursor: int) -> Optional[Beat]:
        """按游标取拍；越界返回 None（＝该收尾了）。"""
        if 0 <= cursor < len(self.beats):
            return self.beats[cursor]
        return None


@dataclass(frozen=True)
class CastMember:
    """选角结果的一项：角色槽 ← 账号 × 人设。"""

    slot: str
    account_id: str
    persona_id: str
    display_name: str = ""
    platform: str = "telegram"

    @property
    def account_key(self) -> str:
        return f"{self.platform}:{self.account_id}"


@dataclass(frozen=True)
class Casting:
    """一场戏的演员表。``unfilled`` 记录没配上的槽（降级演）。

    ``blocked`` 是 ``((槽, 原因码), ...)``——**空槽必须说得出为什么空**。四条护栏
    （号池/指纹、开口预算、号对到线、主推到线）都会让槽空着，可它们要求的处置完全
    相反：号不够要补号，预算压的是护栏在正常工作**不该动**，号对到线要换一批号去铺
    新群，主推到线要把种草角色让给别人。只报「缺角：skeptic」等于把四种处境糊成一种。

    更要命的是它们**看起来一模一样**：戏一天天变冷清，运营找不到原因，最后的动作
    通常是「把护栏关了试试」——护栏于是死在自己的沉默上。所以归因不是锦上添花，
    它是护栏能不能活下来的前提。
    """

    members: Tuple[CastMember, ...] = ()
    unfilled: Tuple[str, ...] = ()
    blocked: Tuple[Tuple[str, str], ...] = ()

    def reason_for(self, slot: str) -> str:
        """这个槽为什么空着（原因码；没记录 → 空串）。"""
        target = str(slot)
        for got, reason in self.blocked:
            if got == target:
                return reason
        return ""

    def by_slot(self, slot: str) -> Optional[CastMember]:
        for m in self.members:
            if m.slot == slot:
                return m
        return None

    def by_account(self, account_id: str) -> Optional[CastMember]:
        for m in self.members:
            if m.account_id == str(account_id):
                return m
        return None

    @property
    def filled_slots(self) -> Tuple[str, ...]:
        return tuple(m.slot for m in self.members)


@dataclass(frozen=True)
class BeatDirective:
    """导演下给生成层的「意图指令」——注意这里**没有台词字段**。

    ``must_not`` 默认携带 :data:`DEFAULT_MUST_NOT`；``reference_last`` 指示本拍
    是否应该顺着上一条接话（承接感），由导演按弧线位置决定。
    """

    beat_id: str
    intent: str
    product: str = ""
    media: str = ""
    soft_level: int = 5
    must_not: Tuple[str, ...] = DEFAULT_MUST_NOT
    reference_last: bool = True
    #: 真人插话让路时，导演把真人原话塞这里要求角色真实回应（而非继续念剧本）
    respond_to_human: str = ""


@dataclass(frozen=True)
class ShowEvent:
    """场次事件流的一条（落库即 ``choreography_events`` 一行）。"""

    seq: int
    ts: float
    speaker_account: str
    role: str
    beat_id: str
    text: str
    kind: str = "line"


@dataclass
class ShowState:
    """一场戏的运行时状态（**可变**：导演推进它，store 持久化它）。"""

    session_id: str
    group_key: str
    playbook: Playbook
    casting: Casting
    beat_cursor: int = 0
    status: str = "pending"
    events: List[ShowEvent] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    platform: str = "telegram"
    #: 本场是否为排练（不真发）——runtime 据此切换发送出口
    dry_run: bool = True
    #: 真人最近一次插话时间（导演据此让路降速）
    last_human_ts: float = 0.0

    # ── 便捷读写 ─────────────────────────────────────────────────────────
    @property
    def current_beat(self) -> Optional[Beat]:
        return self.playbook.beat_at(self.beat_cursor)

    @property
    def next_seq(self) -> int:
        return len(self.events) + 1

    def append_event(self, ev: ShowEvent) -> None:
        self.events.append(ev)

    def last_event(self) -> Optional[ShowEvent]:
        return self.events[-1] if self.events else None

    def lines_by_account(self, account_id: str) -> int:
        """某号本场已发言条数（防单号刷屏 / 轮换均衡度用）。"""
        return sum(
            1 for e in self.events
            if e.speaker_account == str(account_id) and e.kind in SPOKEN_KINDS
        )


# ── 校验与加载 ──────────────────────────────────────────────────────────────


def _int_or_none(value: Any) -> Optional[int]:
    """能当整数用就返回整数，否则 ``None``（供校验器把「不是数字」当一类违规上报）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def validate_playbook(pb: Playbook) -> List[str]:
    """校验剧本，返回问题列表（空＝通过）。

    **硬错**（会让戏演不成）：缺 id / 无 beats / beat 引用了未声明的角色槽 /
    引用了未在 ``products`` 声明的产品 / soft 越界或非数字 / pace 非法。
    **软警告**以 ``warn:`` 前缀返回（不阻断），如缺 skeptic 角（可信度会打折）。

    ``Playbook`` 是不做类型强制的 frozen dataclass，人工构造与未来新增的载入路径都
    可能造出字段类型不对的对象。这类数据**正是校验器该判违规的东西**，不能反过来把
    校验器掀翻——所以数值字段先转换再比较，最外层再兜一道，任何意外一律**给出「不
    通过」结论**。静默放行是这里最坏的失败模式：调用方看到空列表会当成「验过了没问题」。
    """
    problems: List[str] = []
    try:
        if not str(pb.id or "").strip():
            problems.append("playbook.id 不能为空")
        if not pb.beats:
            problems.append("playbook.beats 不能为空")
        if pb.system and pb.system not in VALID_SYSTEMS:
            problems.append(f"未知 system: {pb.system}（应为 {VALID_SYSTEMS}）")
        soft_level = _int_or_none(pb.soft_ad_level)
        if soft_level is None:
            problems.append(
                f"soft_ad_level 不是数字: {pb.soft_ad_level!r}（应为 0..{SOFT_AD_MAX}）")
        elif not (0 <= soft_level <= SOFT_AD_MAX):
            problems.append(
                f"soft_ad_level 越界: {pb.soft_ad_level}（0..{SOFT_AD_MAX}）")

        slots = set(pb.slots)
        if len(slots) != len(pb.roles):
            problems.append("roles 存在重复 slot")
        seen_beat_ids = set()
        for b in pb.beats:
            if not str(b.id or "").strip():
                problems.append("存在 id 为空的 beat")
            elif b.id in seen_beat_ids:
                problems.append(f"beat id 重复: {b.id}")
            else:
                seen_beat_ids.add(b.id)
            if b.role not in slots:
                problems.append(f"beat[{b.id}] 引用了未声明的角色槽: {b.role}")
            if not str(b.intent or "").strip():
                problems.append(f"beat[{b.id}] 缺 intent（意图是导演的唯一输出，不可空）")
            if b.product and pb.products and b.product not in pb.products:
                problems.append(
                    f"beat[{b.id}] 的 product={b.product} 未在 playbook.products 声明")
            beat_soft = _int_or_none(b.soft)
            if beat_soft is None:
                problems.append(f"beat[{b.id}] soft 不是数字: {b.soft!r}")
            elif beat_soft >= 0 and not (0 <= beat_soft <= SOFT_AD_MAX):
                problems.append(f"beat[{b.id}] soft 越界: {b.soft}")
            if b.pace not in PACE_BASE_SECONDS:
                problems.append(
                    f"beat[{b.id}] 未知 pace: {b.pace}（应为 {tuple(PACE_BASE_SECONDS)}）")

        # 软警告：没有质疑角 = 一边倒好评 = 可信度打折（实践中最容易被识破的结构）
        if "skeptic" not in slots:
            problems.append("warn: 缺 skeptic 角——全是好评反而假，建议补一个质疑再化解")
        return problems
    except Exception as exc:  # noqa: BLE001 —— 校验器自己炸也必须给出「不通过」结论
        problems.append(f"剧本校验异常，按不通过处理: {exc!r}")
        return problems


def _coerce_role(raw: Any) -> Role:
    if isinstance(raw, str):
        return Role(slot=raw.strip())
    d = raw or {}
    return Role(slot=str(d.get("slot") or "").strip(),
                desc=str(d.get("desc") or ""))


def _coerce_beat(raw: Any) -> Beat:
    d = raw or {}
    def _int(key: str, default: int) -> int:
        try:
            return int(d.get(key, default))
        except (TypeError, ValueError):
            return default
    return Beat(
        id=str(d.get("id") or "").strip(),
        role=str(d.get("role") or "").strip(),
        intent=str(d.get("intent") or "").strip(),
        product=str(d.get("product") or "").strip(),
        media=str(d.get("media") or "").strip(),
        soft=_int("soft", -1),
        pace=str(d.get("pace") or "normal").strip().lower(),
    )


def _as_items(value: Any, field_name: str) -> Tuple[Any, ...]:
    """集合字段 → 可遍历元组；非可迭代标量（YAML 里写成 ``roles: 5``）回落空。

    运营手写 YAML 把列表写成标量是常见笔误。载入器的职责是**把话说清楚地交给校验器**
    ——回落空集合后 :func:`validate_playbook` 会报「beats 不能为空」，运营看到的是
    「你的剧本哪里写错了」；而在这里抛 TypeError，后台导入接口只会给他一个 500。
    """
    if value is None:
        return ()
    try:
        return tuple(value)
    except TypeError:
        logger.debug("[group_show.playbook] %s 不是集合（%r），按空处理",
                     field_name, value)
        return ()


def playbook_from_dict(data: Dict[str, Any]) -> Playbook:
    """dict → Playbook（容错：缺字段给缺省，类型不对不抛）。"""
    d = data or {}
    try:
        soft = int(d.get("soft_ad_level", 5))
    except (TypeError, ValueError):
        soft = 5
    products = d.get("products") or []
    if isinstance(products, str):
        products = [products]
    roles_raw = _as_items(d.get("roles"), "roles")
    beats_raw = _as_items(d.get("beats"), "beats")
    return Playbook(
        id=str(d.get("id") or "").strip(),
        name=str(d.get("name") or d.get("id") or "").strip(),
        system=str(d.get("system") or "").strip().lower(),
        products=tuple(str(p).strip() for p in products if str(p).strip()),
        soft_ad_level=soft,
        roles=tuple(_coerce_role(r) for r in roles_raw),
        beats=tuple(_coerce_beat(b) for b in beats_raw),
    )


def load_playbook(path: Any) -> Optional[Playbook]:
    """从 YAML 载入单个剧本；文件缺失/解析失败 → None（软失败不阻断）。"""
    try:
        import yaml
        p = Path(path)
        if not p.is_file():
            return None
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return None
        pb = playbook_from_dict(data)
        return pb if pb.id else None
    except Exception:  # noqa: BLE001 —— 剧本坏了不该拖垮调用方
        return None


def load_playbook_dir(dir_path: Any) -> Dict[str, Playbook]:
    """载入目录下所有 ``*.yaml`` 剧本，返回 ``{id: Playbook}``（跳过坏文件）。"""
    out: Dict[str, Playbook] = {}
    try:
        d = Path(dir_path)
        if not d.is_dir():
            return out
        for f in sorted(d.glob("*.yaml")):
            pb = load_playbook(f)
            if pb is not None:
                out[pb.id] = pb
    except Exception:  # noqa: BLE001
        return out
    return out
