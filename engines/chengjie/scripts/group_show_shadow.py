# -*- coding: utf-8 -*-
"""群脉 CrowdX 影子模式 CLI —— 挂在真实群上跑导演循环，一条真消息都不发。

    # 挂到一个真实群上观察 30 分钟（占位台词，秒出、不烧 LLM）
    python -m scripts.group_show_shadow --group -1001234567 -p growth_matrixx

    # 用真 LLM 生成真台词，看「这个群里真发会说出什么」
    python -m scripts.group_show_shadow --group -1001234567 -p growth_matrixx \\
        --minutes 60 --poll 10 --llm

    # 指定收件箱库与落库路径（双实例部署下各实例库不同）
    python -m scripts.group_show_shadow --group tg:-100123 -p lingo_lingox \\
        --inbox-db config/inbox.db --db config/group_show.db

**它和排练的区别**：排练是全虚拟的（虚拟时钟 + 剧本里写死的真人插话）；影子模式
读的是**这个群真实的入站消息**，按**真实时间**跑，于是让路窗、节奏档、真人接管
这些参数第一次有了真实环境下的读数。**它和真发的区别**：没有区别，除了最后一步
不发——本文件与 :mod:`src.companion.group_show.shadow` 都不 import 任何发送出口。

输出里被抑制的决策带 ``[抑制:yield]`` 之类的标记：**沉默和发言同等重要**，
「这一小时导演 12 次想开口、11 次因为让路而闭嘴」是影子模式最该交付的信息。

开演前会把五条轴的读数一次性打全（这也是「预演真发」的全部意义——真发链路上拦你的
是哪一条，这里就该先拦一次）：

1. **出口关联**（``linkage``）——同群里的号是不是从同一个 IP 出网；
2. **出席重合**（``attendance``）——这几个号是不是到处一起出现；
3. **开口共现**（``performance``）——号对最近同台多密，一场最多允许几张嘴；
4. **角色集中度**（``roles``）——同一个号是不是在太多群里都当那个推销的；
5. **跨群间隔**（``schedule``）——谁刚在别的群冒过头，真发时会被顶后多久。

前四条会**真的改变选角**（闸门挂在 ``cast_roles`` 上）；第五条只标注不改演出：影子
模式的职责是如实预演，替真发链路擅自少发一条，反而会让「这场戏长什么样」失真。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.companion.group_show.casting import (  # noqa: E402
    cast_roles,
    validate_casting,
)
from src.companion.group_show.linkage import (  # noqa: E402
    derive_fingerprint_groups,
    format_readiness,
    linkage_readiness,
)
from src.companion.group_show.playbook import (  # noqa: E402
    load_playbook_dir,
    validate_playbook,
)
from src.companion.group_show.runtime import (  # noqa: E402
    llm_generator,
    stub_generator,
)
from src.companion.group_show.shadow import (  # noqa: E402
    ShadowRunner,
    format_decision,
    format_report,
)

PLAYBOOK_DIR = _ROOT / "config" / "playbooks"
DEFAULT_INBOX_DB = _ROOT / "config" / "inbox.db"

#: 每次轮询回看多少条最近消息。10 秒一轮的群不可能塞进 200 条，留足冗余即可；
#: 真正防重复的是游标，不是这个窗口。
_TAIL_WINDOW = 200

#: 已喂过的 message_id 记忆上限（配合 ts 游标做同秒并列消息的去重）
_SEEN_CAP = 2000


# ── 演员表 ──────────────────────────────────────────────────────────────────


def _fake_actors(n: int) -> List[Dict[str, Any]]:
    """占位演员：各自独立指纹组（只为看编排结构，不代表真发时的号）。"""
    names = ["阿哲", "小满", "老陈", "Kiki", "阿May", "大鹏", "囡囡", "阿泰"]
    return [
        {"account_id": f"demo{i + 1}", "platform": "telegram",
         "persona_id": f"persona_{i + 1}",
         "display_name": names[i % len(names)],
         "health": "online", "fingerprint_group": f"fp{i + 1}"}
        for i in range(max(1, n))
    ]


def _open_store(db_path: Any) -> Any:
    """打开场次库；``--db`` 没给或建不起来 → None（影子模式照跑，只是不落档也不体检）。

    **一个进程只开这一个**：出席体检 / 演出台账 / 开口预算 / 落档四处都用它。各自
    `GroupShowStore(path)` 会各开一条连接、各跑一遍建表迁移，Windows 上还会把文件
    锁住（本地冒烟已被咬到删不掉库）。
    """
    if not db_path:
        return None
    try:
        from src.companion.group_show.store import GroupShowStore
        st = GroupShowStore(db_path)
        return st if getattr(st, "available", False) else None
    except Exception as exc:  # noqa: BLE001 —— 落档是旁路能力，开不了就不开
        print(f"[warn] 场次库打不开（{exc}）", file=sys.stderr)
        return None


def _cast_attendance_problems(casting: Any, store: Any, group_key: str
                              ) -> List[str]:
    """本场演员在台账里的同框记录 → 问题清单（台账缺失就静默跳过）。

    刻意只出 ``warn:``：出席重合是**历史累积**的结果，此刻拦下这场戏也消除不了它，
    硬拦只会让运营绕过工具。要的是把事实摆到开演前，让他换个搭档。
    """
    if store is None:
        return []
    try:
        from src.companion.group_show.attendance import cast_entanglement
        report = cast_entanglement(
            getattr(casting, "members", ()) or (),
            store.memberships(), group_key=group_key)
        return [p if p.startswith("warn:") else f"warn: {p}"
                for p in (report.get("problems") or ())]
    except Exception as exc:  # noqa: BLE001 —— 体检挂了不该拦住整场戏
        print(f"[warn] 出席体检跳过（{exc}）", file=sys.stderr)
        return []


def _show_context(store: Any, inbox: Any, pool: Sequence[Any], *,
                  group_key: str = "", platform: str = "telegram") -> Any:
    """一次读账，拿到这场戏的全部风险读数与闸门参数（见 ``group_show.ledgers``）。

    读账与拼参**一行都不在这里写**。影子模式的全部价值建立在「它跟真发走同一套判断」
    之上，各自实现一遍是最容易失守的地方：少传一个 kwarg，预览里四个号演得热热闹闹、
    真发只有两个能上，而这个偏差没有任何征兆。
    """
    from src.companion.group_show.ledgers import read_show_context
    return read_show_context(store, inbox, pool=pool, group_key=group_key,
                             platform=platform)


def _axis_notes(ctx: Any, cap: Dict[str, Any]) -> List[str]:
    """把各条轴的读数翻成开演前的几行人话（含**台账缺口**这条元信息）。

    开口人数是这里唯一还能当场改的旋钮，也是杠杆最大的：共现对次对开口人数是**二次**的
    （n 张嘴一场贡献 n(n-1)/2 对），少让一个号开口比多养五个号管用，而且立刻生效。
    超预算默认**真的把选角压回去**（不是打一行 warn 就照演），要突破得显式
    ``--over-budget`` 并留痕——工具默认走安全档、越界要人主动按。
    """
    notes: List[str] = []
    if getattr(ctx, "degraded", ()):
        notes.append(
            f"warn: 台账少读了「{'/'.join(ctx.degraded)}」——下面的风险读数不完整；"
            f"「没有共现」和「读不到数据」在这里长得一模一样，别当绿灯看")
    notes += [f"warn: {line}" for line in (getattr(ctx, "advice", ()) or ())[:3]]
    budget = getattr(ctx, "budget", {}) or {}
    if cap["source"] == "capped":
        notes.append(
            f"warn: {cap['reason']}（号池 {budget.get('pool')} 个号 / "
            f"已铺 {budget.get('groups')} 个群）；确要突破加 --over-budget")
    elif cap["source"] == "override":
        # 越界留痕：这一行既进 stderr 也进问题清单，事后翻日志能查到是谁按的
        notes.append(f"warn: {cap['reason']}——已按运营显式指令放行")
    elif cap["source"] == "auto":
        notes.append(f"note: {cap['reason']}（--max-speakers 可显式指定）")
    notes += _capacity_notes(ctx)
    return notes


def _capacity_notes(ctx: Any) -> List[str]:
    """还能铺多少群——**三条轴取最紧的那条**。

    单独一行的理由：上面那句「按预算自动限到 1 人开口」在 solo 演法下伴随的是预算段
    如实报出的「无上限」，而那只是发言轴。号对轴归零之后瓶颈已经换成主推集中度
    （10 个号还是只有 30 个群）。运营记得住的就一个数，最显眼的偏偏最乐观——所以真上限
    必须跟预算摆在同一屏，否则这条 CLI 就成了那个把人送进去的乐观读数。
    """
    try:
        capacity = getattr(ctx, "capacity", {}) or {}
        if not capacity or capacity.get("max_groups") is None:
            return []
        level = "warn" if not capacity.get("fits", True) else "note"
        return [f"{level}: 按台账实测的演法（每场 {capacity.get('speakers')} 个号开口、"
                f"{round(float(capacity.get('advocate_ratio') or 0) * 100)}% 的场次带货），"
                f"这批号最多铺 {capacity['max_groups']} 个群，"
                f"瓶颈在「{capacity.get('binding')}」轴——现已铺 {capacity.get('groups')} 个"]
    except Exception:  # noqa: BLE001 —— 容量只是规划参考，算不出来就不说
        return []


def _waiting_notes(watch: Any, accounts: Sequence[Any]) -> List[str]:
    """开演前点名：这批演员里谁还在跨群冷却窗里，还要等多久。"""
    return [f"warn: {row['account']} 刚在别的群开过口，真发要再等 "
            f"{row['wait_sec'] // 60} 分 {row['wait_sec'] % 60} 秒才轮得到它在本群说话"
            for row in watch.waiting(accounts)]


def CrossGroupWatch(store: Any, inbox: Any, **kwargs: Any) -> Any:
    """跨群间隔探针的长跑版 —— 实现已上移到 ``ledgers.ContextWatch``。

    影子模式与真发链路要问的是同一个问题（「这个号刚刚是不是在别的群说过话」），只是
    **拿到答案后的反应相反**：真发会按它推迟发送，影子只是如实报数（少演一条会让「这场
    戏长什么样」这个读数失真，而那正是运营挂影子要看的东西）。判定归一处、反应各自定，
    才不会出现「影子说会被顶 3 分钟、真发其实没顶」这种最伤信任的错位。

    这层薄壳只为保住脚本内的旧调用姿势（位置参数），不含任何判定逻辑。
    """
    from src.companion.group_show.ledgers import ContextWatch
    return ContextWatch(store, inbox, **kwargs)


#: 空槽原因 → 该怎么办。两条闸门的**处方相反**，说错就把人支反了：号对余量挡下来时
#: 补号没用（问题是这批老号最近同台太密，得换人或降开口人数）；角色集中度挡下来时补号
#: **正是**对的，也可以让老号这场改演路人。``pool``/``budget`` 不在表里——前者「号不够」
#: 已经写在缺角提示里，后者由 :func:`_axis_notes` 负责解释，重复说只会稀释真正的信号。
_GATE_ADVICE = {
    "pair": "剩下的候选跟场上的人最近同台太多，已被**号对余量**闸门挡下；"
            "补号没用，换一批人或降开口人数才有用",
    "role": "剩下的号最近在太多群里演过同一个**推销角色**，被角色集中度闸门挡下；"
            "让它们这场改演路人，或者补新号进来",
}


def _gate_note(casting: Any) -> List[str]:
    """空槽到底是**号不够**还是被闸门挡下的——说错运营就会去补号。

    归因直接读 ``casting.blocked``（选角循环里当场记的），**不再从外部重新推断**。
    外部推断只能给一个笼统结论，而一场戏完全可能主推位被角色闸门挡、路人位被号对
    闸门挡——两个处方相反的问题被合并成一句话，无论说哪句都有一半是错的。判断只留
    在产生它的那一处，CLI 与导播台才不会给出互相矛盾的解释。
    """
    try:
        by_reason: Dict[str, List[str]] = {}
        for slot, reason in (getattr(casting, "blocked", ()) or ()):
            if str(reason) in _GATE_ADVICE:
                by_reason.setdefault(str(reason), []).append(str(slot))
        return [f"note: {'/'.join(slots)} 空着不是因为号不够——{_GATE_ADVICE[reason]}"
                for reason, slots in sorted(by_reason.items())]
    except Exception:  # noqa: BLE001 —— 解释性提示，算不出来就不说
        return []


def _real_actors(
    registry_path: Path,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """从账号注册表读真实在线号，返回 ``(候选, 注册表原始行)``。

    两份都要：候选喂选角，原始行喂 :func:`linkage.linkage_readiness`——体检要看
    ``proxy_id`` / ``mode`` / ``meta`` 这些候选结构里没有的字段。

    指纹归属**不在这里猜**，交给 ``linkage`` 统一派生（出口＞设备＞宿主兜底）。
    影子模式虽然零发送，但它的意义正是「预演真发」，所以选角口径必须与真发完全一致：
    否则影子里四个号演得热热闹闹，真发时只有一个号能上，等于白演一场。
    """
    try:
        from src.integrations.account_registry import AccountRegistry
        reg = AccountRegistry(registry_path)
        rows = reg.list("telegram")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 读账号注册表失败（{exc}），回落占位演员", file=sys.stderr)
        return [], []
    out: List[Dict[str, Any]] = []
    online_rows: List[Dict[str, Any]] = []
    for r in rows:
        if str(r.get("status") or "") != "online":
            continue
        online_rows.append(r)
        meta = r.get("meta") or {}
        out.append({
            "account_id": str(r.get("account_id") or ""),
            "platform": "telegram",
            "persona_id": str(meta.get("persona_id")
                              or r.get("persona_id") or ""),
            "display_name": str(r.get("display_name")
                                or r.get("label") or r.get("account_id") or ""),
            "health": "online",
        })
    return out, online_rows


# ── 收件箱尾随（游标：只喂新消息，绝不重复喂） ───────────────────────────────


class InboxTail:
    """按会话尾随收件箱的**新入站**消息。

    游标由两件东西共同构成，缺一不可：

    1. ``_cursor_ts``——只看 ``ts >= 游标`` 的消息，把窗口收窄到「上次读到之后」；
    2. ``_seen``（message_id 集合）——同一秒可能落好几条消息，纯 ts 游标要么漏喂
       （用 ``>``）要么重复喂（用 ``>=``）。``message_id`` 是收件箱的确定性主键，
       用它做最终判据，ts 只负责把候选集缩小。

    首轮 :meth:`prime` **只记不喂**：把群里已有的历史全部标记成「见过」。影子模式
    要观察的是「从现在起这个群发生了什么」，把几千条历史一次性灌进导演循环只会
    让它对着昨天的话题演戏。
    """

    def __init__(self, store: Any, conversation_id: str) -> None:
        self._store = store
        self.conversation_id = str(conversation_id)
        self._seen: Set[str] = set()
        self._order: List[str] = []
        self._cursor_ts = 0.0

    def _recent(self) -> List[Dict[str, Any]]:
        try:
            return list(self._store.list_recent_messages(
                self.conversation_id, limit=_TAIL_WINDOW) or [])
        except Exception as exc:  # noqa: BLE001 —— 读库抖动不该中断观察
            print(f"[warn] 读收件箱失败（{exc}）", file=sys.stderr)
            return []

    def _remember(self, msg_id: str, ts: float) -> None:
        if msg_id in self._seen:
            return
        self._seen.add(msg_id)
        self._order.append(msg_id)
        if len(self._order) > _SEEN_CAP:
            self._seen.discard(self._order.pop(0))
        self._cursor_ts = max(self._cursor_ts, ts)

    def prime(self) -> int:
        """把已有历史标成已读，返回历史条数（**不产出任何待喂消息**）。"""
        rows = self._recent()
        for r in rows:
            self._remember(str(r.get("message_id") or ""),
                           float(r.get("ts") or 0.0))
        return len(rows)

    def poll(self) -> List[Dict[str, Any]]:
        """取「上次之后新到的入站消息」，按时间升序。"""
        fresh: List[Dict[str, Any]] = []
        for r in self._recent():
            if str(r.get("direction") or "in").lower() != "in":
                continue
            mid = str(r.get("message_id") or "")
            ts = float(r.get("ts") or 0.0)
            if not mid or mid in self._seen or ts < self._cursor_ts:
                continue
            self._remember(mid, ts)
            fresh.append(r)
        fresh.sort(key=lambda m: float(m.get("ts") or 0.0))
        return fresh


def _open_inbox(path: Path) -> Any:
    try:
        from src.inbox.store import InboxStore
        return InboxStore(path)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 打开收件箱库失败（{path}）：{exc}", file=sys.stderr)
        return None


def _resolve_conversation(
    store: Any, group: str, platform: str,
) -> Optional[Dict[str, Any]]:
    """``--group`` 既可传 chat_key 也可传 conversation_id（运营手里两种都有）。"""
    key = str(group or "").strip()
    if not key:
        return None
    try:
        convs = store.list_conversations(limit=500, platform=platform or "")
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 列会话失败：{exc}", file=sys.stderr)
        return None
    for c in convs:
        ck = str(c.get("chat_key") or "")
        if key in (ck, str(c.get("conversation_id") or "")) \
                or ck.endswith(f":{key}"):
            return c
    return None


# ── 台词生成器 ──────────────────────────────────────────────────────────────


async def _build_llm_generator():
    """尽力构造真 LLM 生成器；构造不出来返回 None（调用方回落占位）。

    ``ConfigManager.load`` 是 **async**——漏 await 会让整场戏 0 条台词却显示
    「正常收场」（排练首跑踩过），这里照抄排练 CLI 的正确写法。
    """
    try:
        from src.ai.ai_client import AIClient
        from src.utils.config_manager import ConfigManager
        cfg_mgr = ConfigManager(str(_ROOT / "config" / "config.yaml"))
        await cfg_mgr.load()
        client = AIClient(cfg_mgr)
        await client.initialize()
        return llm_generator(client)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 无法构造 LLM 客户端（{exc}），回落占位台词", file=sys.stderr)
        return None


# ── 主循环 ──────────────────────────────────────────────────────────────────


def _print_decision(decision: Any, watch: Any) -> int:
    """打一条决策，并标注真发时它会不会被跨群间隔顶后；返回顶后秒数。

    影子模式**不因此改变演出**（如实预演优先于自作主张），但必须把「这条真发要晚七
    分钟才出得去」说出来：不说的话，运营在影子里看到的节奏跟真发的节奏根本是两回事，
    而节奏正是他挂影子模式要看的东西。
    """
    line = format_decision(decision)
    wait = 0
    if watch is not None and not getattr(decision, "suppressed", ""):
        wait = watch.delay_for(getattr(decision, "account_id", ""),
                               float(getattr(decision, "at", 0.0) or time.time()))
        if wait > 0:
            line += f"　⏳ 真发会被跨群间隔顶后 {wait // 60} 分 {wait % 60} 秒"
    print(line, flush=True)
    return wait


def _print_human(msg: Dict[str, Any], name: str) -> None:
    stamp = time.strftime("%H:%M:%S", time.localtime(float(msg.get("ts") or 0)))
    text = str(msg.get("text") or msg.get("original_text") or "").strip()
    print(f"[{stamp}] 👤 {name}：{text}", flush=True)


def _print_plan_groups(args: argparse.Namespace) -> int:
    """「我想铺 N 个群，该怎么演」——不接群、不读账的纯测算档。

    做成不需要 ``--group`` 的独立子命令：这个问题运营是在**加群之前**问的，那时既没有
    台账也没有场次。要求他先跑起来才能问「跑起来之前该怎么规划」是本末倒置，而人对付
    这种阻力的办法通常是干脆不问、凭感觉铺。
    """
    from src.companion.group_show.capacity import plan_capacity, recommend_mix

    groups = max(1, int(args.plan_groups))
    pool = max(1, int(args.actors))
    seats = max(0, int(args.plan_seats))
    mix = recommend_mix(pool=pool, groups=groups, seats=seats)
    print(f"目标：{pool} 个号铺 {groups} 个群"
          + (f"（每群进 {seats} 个号）" if seats > 1 else "（每群只进 1 个号）"))
    if mix["fits"]:
        pct = round(mix["advocate_ratio"] * 100)
        print(f"  ✓ 可行 —— 每场 {mix['speakers']} 个号开口，"
              + ("每场都可以带货" if pct >= 100 else f"且只有 {pct}% 的场次带货"))
    else:
        print(f"  ✖ 铺不动 —— 即使压到每场 1 个号开口、带货密度降到底，"
              f"号池也至少要 {mix['pool_needed']} 个")
    plan = plan_capacity(pool=pool, groups=groups, seats=seats,
                         speakers=mix["speakers"],
                         advocate_ratio=mix["advocate_ratio"])
    for axis in plan["axes"]:
        cap = "不设限" if axis["unlimited"] else f"{axis['max_groups']} 个群"
        print(f"    {'✓' if axis['fits'] else '✖'} {axis['axis']:<7} 最多 {cap}"
              f"   ← {axis['lever']}")
    for line in plan["advice"]:
        print(f"  · {line}")
    return 0


async def _run(args: argparse.Namespace) -> int:
    if int(getattr(args, "plan_groups", 0) or 0) > 0:
        return _print_plan_groups(args)
    books = load_playbook_dir(PLAYBOOK_DIR)
    if args.list or not args.playbook:
        if not books:
            print(f"剧本目录为空: {PLAYBOOK_DIR}")
            return 1
        print(f"可用剧本（{PLAYBOOK_DIR}）：")
        for pid, pb in sorted(books.items()):
            hard = [w for w in validate_playbook(pb) if not w.startswith("warn:")]
            print(f"  {'✖' if hard else '✓'} {pid:<20} {pb.name}  "
                  f"[{pb.system}] {len(pb.beats)} 拍")
        return 0

    pb = books.get(args.playbook)
    if pb is None:
        print(f"没有这个剧本: {args.playbook}（--list 看可用的）")
        return 1
    if not str(args.group or "").strip():
        print("必须指定 --group（群的 chat_key 或 conversation_id）")
        return 1

    # ① 收件箱：找到这个群的会话
    inbox = _open_inbox(Path(args.inbox_db))
    if inbox is None:
        return 1
    conv = _resolve_conversation(inbox, args.group, args.platform)
    if conv is None:
        print(f"[error] 收件箱里找不到这个群：{args.group}"
              f"（库 {args.inbox_db}，platform={args.platform or '任意'}）")
        return 1
    conv_id = str(conv.get("conversation_id") or "")
    group_label = str(conv.get("display_name") or conv.get("chat_key") or conv_id)

    # ② 演员表
    actors: List[Dict[str, Any]] = []
    registry_rows: List[Dict[str, Any]] = []
    if args.real:
        actors, registry_rows = _real_actors(Path(args.registry))
        if not actors:
            print("[warn] 没有在线的真实账号，回落占位演员", file=sys.stderr)
    if not actors:
        actors = _fake_actors(args.actors)

    # 指纹体检：影子模式虽不发消息，选角口径也必须与真发一致，
    # 否则「影子里四个号很热闹、真发只有一个能上」＝白演一场。
    fingerprint_groups: Optional[Dict[str, str]] = None
    if registry_rows:
        print(format_readiness(
            linkage_readiness(registry_rows, host_tag=args.host_tag)))
        print()
        fingerprint_groups = derive_fingerprint_groups(
            registry_rows, host_tag=args.host_tag)

    # 一条连接走完体检 / 台账 / 预算 / 落档四件事（见 :func:`_open_store`）。
    store = _open_store(args.db)

    # 台账**只读一次**，三条动态轴（共现 / 角色 / 时间）共用同一份口径与同一个窗口。
    # 读两遍除了多几次全表扫描没有任何好处，还会让两张卡在窗口边界上给出互相矛盾的读数。
    group_key = str(conv.get("chat_key") or args.group or "")
    platform = str(conv.get("platform") or args.platform or "telegram")
    ctx = _show_context(store, inbox, [a.get("account_id") for a in actors],
                        group_key=group_key, platform=platform)
    requested = max(0, int(getattr(args, "max_speakers", 0) or 0))
    allow_over = bool(getattr(args, "over_budget", False))
    cap = ctx.speaker_cap(requested=requested, allow_over=allow_over)

    # 闸门参数由 ctx 统一拼（真发链路将来用的是同一个方法）：优先挑「没跟已选演员一起
    # 开过口」的号把共现摊薄、按预算压住开口人数、挡住角色已经饱和的号。
    # 与下面的出席体检互补——那个报既成事实，这个改这一场的人选。
    casting = cast_roles(
        pb, actors, fingerprint_groups=fingerprint_groups,
        **ctx.cast_kwargs(requested=requested, allow_over=allow_over,
                          seed=group_key))
    problems = validate_playbook(pb) + validate_casting(
        casting, pb, fingerprint_groups=fingerprint_groups)
    # 出席体检（第二条轴）：出口体检管「同群同出口」，这条管「这几个号是不是到处
    # 一起出现」。排班表是规划工具，开演时没人强制看它——不在这里查，就会出现
    # 「矩阵建议 A/B 分开，实际却让 A/B 在第 40 个群里又一起演一台」。
    problems += _cast_attendance_problems(casting, store, group_key)
    # 开口预算 / 角色集中度这两条闸门在选角**之前**就已经落进 cast_kwargs 了，这里只报账
    # （连带台账缺口——少读一本的后果长得跟「一切安全」一模一样，必须说破）。
    problems += _axis_notes(ctx, cap)
    # 跨群间隔只标注不改演出：开演前先点名，谁刚在别的群冒过头、真发时会被顶后。
    watch = CrossGroupWatch(store, inbox, group_key=group_key, platform=platform)
    problems += _waiting_notes(watch, [m.account_id for m in casting.members])
    problems += _gate_note(casting)
    hard = [p for p in problems
            if not p.startswith("warn:") and not p.startswith("note:")]
    for p in problems:
        prefix = "⚠ " if p.startswith("warn:") else (
            "· " if p.startswith("note:") else "✖ ")
        print(prefix + p, file=sys.stderr)
    if hard or not casting.members:
        print("[error] 剧本或演员表不过关，影子模式不开跑（先修上面的硬错）")
        return 1

    generate = await _build_llm_generator() if args.llm else None
    if generate is None:
        generate = stub_generator()

    runner = ShadowRunner(
        pb, casting,
        group_key=str(conv.get("chat_key") or args.group),
        platform=str(conv.get("platform") or args.platform or "telegram"),
        config=_director_config(args),
        generate=generate, store=store)

    cast_desc = "  ".join(
        f"{m.slot}={m.display_name or m.account_id}" for m in casting.members)
    print(f"═══ 影子模式：{pb.id} @ {group_label} ═══")
    print(f"演员表：{cast_desc}")
    print(f"会话：{conv_id}　观察 {args.minutes} 分钟　轮询 {args.poll}s　"
          f"**不发任何消息**")

    # ③ 尾随游标：先把历史标成已读，只观察从现在起发生的事
    tail = InboxTail(inbox, conv_id)
    print(f"（已跳过 {tail.prime()} 条历史消息，从此刻起开始观察）\n", flush=True)

    deadline = time.time() + max(1.0, float(args.minutes)) * 60.0
    poll = max(1.0, float(args.poll))
    delayed = 0
    while time.time() < deadline and not runner.terminated:
        for msg in tail.poll():
            name = (str(msg.get("sender_name") or "").strip()
                    or str(msg.get("sender_id") or "").strip() or "群友")
            _print_human(msg, name)
            decision = await runner.on_inbound(
                sender_id=str(msg.get("sender_id") or name),
                sender_name=name,
                text=str(msg.get("text") or msg.get("original_text") or ""),
                ts=float(msg.get("ts") or time.time()))
            if decision is not None:
                delayed += 1 if _print_decision(decision, watch) else 0
            if runner.terminated:
                break
        if not runner.terminated:
            decision = await runner.tick()
            if decision is not None:
                delayed += 1 if _print_decision(decision, watch) else 0
        if runner.terminated:
            break
        await asyncio.sleep(poll)

    report = runner.report()
    if delayed:
        # 真发链路上这些条会被顶后。放进 report 而不只是打一行：--json 的消费方
        # （回归脚本 / 后续的批量对比）拿不到 stdout 里的中文提示。
        report["cross_group_delayed"] = delayed
    print()
    if args.json:
        print(json.dumps({
            **report,
            "decisions_detail": [{
                "at": d.at, "who": d.display_name, "role": d.role,
                "beat": d.beat_id, "soft": d.soft_level, "media": d.media,
                "reason": d.reason, "suppressed": d.suppressed, "text": d.text,
            } for d in runner.decisions],
        }, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
        if delayed:
            print(f"⏳ 其中 {delayed} 条真发时会被跨群间隔顶后"
                  f"（同一个号刚在别的群冒过头）")
    return 0


def _director_config(args: argparse.Namespace) -> Dict[str, Any]:
    """只把 CLI 显式给的导演参数放进去，其余走 :class:`DirectorConfig` 缺省。"""
    cfg: Dict[str, Any] = {}
    if args.yield_seconds is not None:
        cfg["human_yield_seconds"] = float(args.yield_seconds)
    if args.max_lines is not None:
        cfg["max_lines"] = int(args.max_lines)
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(
        description="群脉 CrowdX 影子模式（接真实群，零发送）")
    ap.add_argument("--group", default="", help="群的 chat_key 或 conversation_id")
    ap.add_argument("-p", "--playbook", default="", help="剧本 id")
    ap.add_argument("--list", action="store_true", help="列出可用剧本")
    ap.add_argument("--minutes", type=float, default=30, help="观察多少分钟")
    ap.add_argument("--poll", type=float, default=10, help="轮询间隔秒")
    ap.add_argument("--inbox-db", dest="inbox_db", default=str(DEFAULT_INBOX_DB),
                    help="收件箱库路径")
    ap.add_argument("--db", default="", help="群戏落库路径（留空=纯内存）")
    ap.add_argument("--platform", default="", help="按平台收窄会话查找")
    ap.add_argument("--llm", action="store_true", help="用真 LLM 生成台词")
    ap.add_argument("--real", action="store_true", help="用注册表里的真实在线号当演员")
    ap.add_argument("--registry", default=str(_ROOT / "config" / "account_registry.db"),
                    help="账号注册表路径（配 --real 用）")
    ap.add_argument("--actors", type=int, default=4, help="占位演员数")
    ap.add_argument("--max-speakers", dest="max_speakers", type=int, default=0,
                    help="本场最多几个号开口（0＝按预算自动限；共现对次对这个数是二次的，"
                         "10 个号每场 3 人开口只够铺 30 个群，降到 2 人能铺 90 个）")
    ap.add_argument("--over-budget", dest="over_budget", action="store_true",
                    help="允许 --max-speakers 超出预算（默认自动压回预算内；"
                         "越界会在日志里留一行审计）")
    ap.add_argument("--host-tag", dest="host_tag", default="",
                    help="本机标识（同一台机器出网的号会被归进同一关联组）")
    ap.add_argument("--yield-seconds", dest="yield_seconds", type=float,
                    default=None, help="覆盖让路窗秒数")
    ap.add_argument("--max-lines", dest="max_lines", type=int, default=None,
                    help="覆盖单场发言条数上限")
    ap.add_argument("--plan-groups", dest="plan_groups", type=int, default=0,
                    help="纯测算：想铺这么多群该怎么演（不接群、不读账，"
                         "号池取 --actors、每群进几个号取 --plan-seats）")
    ap.add_argument("--plan-seats", dest="plan_seats", type=int, default=0,
                    help="配 --plan-groups：每个群进几个号（>1 才会纳入成员共现轴；"
                         "留空＝每群只进一个号，即真 solo）")
    ap.add_argument("--json", action="store_true", help="收尾输出 JSON")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 —— 老终端没这个方法，不影响主流程
        pass
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\n[中断] 影子模式已停止（本来也没发过任何消息）")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
