"""群脉 CrowdX「导演控制台」路由（剧本库 / 逐拍详情 / 一键排练 / 历史场次）。

端点：
- ``GET  /api/group-show/playbooks``        —— 剧本库 + 逐本校验结果（硬错/警告分开）
- ``GET  /api/group-show/playbooks/{pid}``  —— 单本剧本的逐拍表
- ``POST /api/group-show/rehearse``         —— 跑一场排练，返回 ``RehearsalResult`` 的 JSON
- ``GET  /api/group-show/linkage``          —— 关联风险体检（同群能上几个号，网络出口轴）
- ``POST /api/group-show/attendance``       —— 出席矩阵排班（跨群成员重合轴）
- ``POST /api/group-show/attendance/joined``—— 记一笔「已加入」（出席台账，唯一的写端点）
- ``GET  /api/group-show/exposure``         —— 成员/演出/角色三读数 + 每场开口人数预算
- ``POST /api/group-show/schedule``         —— 开演排期 + 节奏体检（时间轴）
- ``GET  /api/group-show/sessions``         —— 最近场次（``GroupShowStore.recent_sessions``）
- ``POST /api/group-show/live``             —— 真发预检 / 后台开演（双锁 + 同群互斥）
- ``GET  /group-show``                      —— 导播台页面（同源会话鉴权）

本模块是 :mod:`src.companion.group_show` 的外壳：排练 / 排班 / 读数走 dry 路径
（``runtime.rehearse`` 物理上不 import 编排器）；**唯一的真发送出口**是
``POST /api/group-show/live``（``check_only=false``），它把出口交给
:func:`~src.companion.group_show.live.perform`，由调用方注入的编排器投递——本文件
仍然不 ``import`` 发送实现细节以外的 worker。

风险读数同理：所有台账（发言 / 角色 / 冷却 / 预算）一律经
:mod:`src.companion.group_show.ledgers` 取，**本文件不自己读库**。影子 CLI 与将来的
真发链路走的是同一份实现——控制台再实现一遍的代价不是重复代码，是口径漂移：卡片写
「预算 2 人」而排练演 4 人，两个数都自称权威，运营只能猜哪个是真的。

台词生成器**默认占位**（``stub_generator``，秒出、可复现、CI 可跑）；``use_llm``
才构造真 LLM 生成器。后者的构造姿势照抄 ``scripts/group_show_rehearse.py``——
``ConfigManager.load()`` 是 **async 必须 await**、``AIClient`` 要
``await initialize()``，这两步漏一步都会让整场戏静默演成空场（2026-07-25 踩过）。
真 LLM 一场戏可能跑几分钟，故服务端加 :data:`LLM_TIMEOUT_SEC` 硬超时，前端另有
loading 态与超时提示。

子系统缺失（模块未部署）时全部端点返回 503 而非 500——导播台是旁路能力，
不该因为它把后台整页打挂。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

#: 真 LLM 排练的服务端硬超时（秒）——一场 10 拍的戏按单拍十几秒算也就几分钟；
#: 超过即判生成侧异常，宁可显式 504 也不让请求悬死（前端 abort 后无人收尸）。
LLM_TIMEOUT_SEC = 600.0

#: 占位演员数上限（选角只为看编排结构，几十个号没有意义且会拖慢页面）。
MAX_ACTORS = 12

#: 占位演员的显示名（与 CLI ``_fake_actors`` 同一套，便于两边结果对照）。
_DEMO_NAMES = ("阿哲", "小满", "老陈", "Kiki", "阿May", "大鹏", "囡囡", "阿泰")

#: 单次排班的群数上限。排班本身是 O(群数 × 号池 × 席位) 的贪心，几千个群会把请求拖死；
#: 而运营一次能手工加完的群本来也就几十个，超出部分留给下一批。
MAX_PLAN_GROUPS = 500

#: 跨群角色轮换用的槽位序（与标准四角同名，按席位数截断）。
_ROTATION_SLOTS = ("advocate", "skeptic", "bystander", "asker",
                   "extra1", "extra2", "extra3", "extra4",
                   "extra5", "extra6", "extra7", "extra8")

#: 同群真发互斥表 ``{group_key: (session_id, claimed_at)}``。进程内即可——双实例各管各的群，
#: 跨进程互斥会把「另一台机器上的影子观察」也误判成冲突。
#:
#: 带 ``claimed_at``：后台任务若异常退出没跑到 ``finally`` 放锁，靠 TTL 自愈，
#: 否则这个群会永久卡死（运营只能重启进程）。TTL 略长于 ``MAX_LIVE_SECONDS``。
_live_inflight: Dict[str, Tuple[str, float]] = {}
LIVE_INFLIGHT_TTL_SEC = float(45 * 60 + 5 * 60)  # 50min ≈ 硬顶 45min + 缓冲


def _inflight_sid(group_key: str, *, now: Optional[float] = None) -> Optional[str]:
    """读同群占位者；过期条目顺手清掉。"""
    key = str(group_key or "")
    held = _live_inflight.get(key)
    if not held:
        return None
    sid, at = held[0], float(held[1])
    t = float(now if now is not None else time.time())
    if t - at >= LIVE_INFLIGHT_TTL_SEC:
        _live_inflight.pop(key, None)
        return None
    return str(sid)


def _claim_live_group(group_key: str, session_id: str) -> Optional[str]:
    """抢占同群真发位。已被占 → 返回占位者的 session_id；否则写入后返 ``None``。"""
    key = str(group_key or "")
    if not key:
        return "invalid"
    now = time.time()
    held_sid = _inflight_sid(key, now=now)
    if held_sid and held_sid != session_id:
        return held_sid
    _live_inflight[key] = (session_id, now)
    return None


def _release_live_group(group_key: str, session_id: str) -> None:
    key = str(group_key or "")
    held = _live_inflight.get(key)
    if held and held[0] == session_id:
        _live_inflight.pop(key, None)


def _live_actors(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """注册表在线行 → 真发选角候选（指纹交给 ``derive_fingerprint_groups``）。"""
    out: List[Dict[str, Any]] = []
    for r in rows or ():
        aid = str(r.get("account_id") or "")
        if not aid:
            continue
        meta = r.get("meta") or {}
        out.append({
            "account_id": aid,
            "platform": str(r.get("platform") or "telegram"),
            "persona_id": str(meta.get("persona_id")
                              or r.get("persona_id") or ""),
            "display_name": str(r.get("display_name")
                                or r.get("label") or aid),
            "health": "online",
        })
    return out


# ── 子系统装载（缺失即 503，绝不 500） ──────────────────────────────────────


def _load_playbook_api():
    """返回 (load_playbook_dir, validate_playbook)；子系统缺失 → None。"""
    try:
        from src.companion.group_show.playbook import (
            load_playbook_dir,
            validate_playbook,
        )
        return load_playbook_dir, validate_playbook
    except Exception:  # noqa: BLE001 —— 模块缺失/导入期异常都按「不可用」处理
        logger.debug("[group_show] playbook 模块不可用", exc_info=True)
        return None


def _load_runtime_api():
    """返回 (rehearse, stub_generator, llm_generator)；子系统缺失 → None。"""
    try:
        from src.companion.group_show.runtime import (
            llm_generator,
            rehearse,
            stub_generator,
        )
        return rehearse, stub_generator, llm_generator
    except Exception:  # noqa: BLE001
        logger.debug("[group_show] runtime 模块不可用", exc_info=True)
        return None


def _online_accounts(config_manager: Any) -> List[Dict[str, Any]]:
    """账号注册表里的在线号原始行（读不到 → 空列表，页面显示空态）。

    要**原始行**而不是选角候选：体检看的是 ``proxy_id`` / ``mode`` / ``meta``，
    这些字段在洗成候选时会被丢掉。
    """
    try:
        from src.integrations.account_registry import AccountRegistry
        d = _config_dir(config_manager)
        repo_default = (Path(__file__).resolve().parents[3]
                        / "config" / "account_registry.db")
        path = (d / "account_registry.db") if d is not None else repo_default
        if not path.is_file():
            path = repo_default
        # 构造器会顺手建库建目录——只读页面不该有这种副作用，库不存在就直接空态。
        if not path.is_file():
            return []
        reg = AccountRegistry(path)
        return [r for r in (reg.list("telegram") or [])
                if str(r.get("status") or "") == "online"]
    except Exception:  # noqa: BLE001 —— 注册表读不到不该让导播台白屏
        logger.debug("[group_show] 账号注册表不可用", exc_info=True)
        return []


# ── 路径与落库 ──────────────────────────────────────────────────────────────


def _config_dir(config_manager: Any) -> Optional[Path]:
    """实例的 config 目录（双实例部署下各自独立）；拿不到 → None。"""
    try:
        cfg_path = getattr(config_manager, "config_path", None)
        return Path(cfg_path).parent if cfg_path else None
    except Exception:  # noqa: BLE001
        return None


def playbook_dir(config_manager: Any) -> Path:
    """剧本目录：优先实例 ``config/playbooks``，回落仓库根同名目录。

    双实例部署下两台各演各的剧本；实例目录还没建剧本时回落仓库自带的样例，
    避免新实例首开导播台空空如也。
    """
    repo_default = Path(__file__).resolve().parents[3] / "config" / "playbooks"
    d = _config_dir(config_manager)
    if d is not None:
        candidate = d / "playbooks"
        if candidate.is_dir():
            return candidate
    return repo_default


def _recorded_memberships(config_manager: Any) -> Dict[str, List[str]]:
    """出席台账 ``{群: [号]}``；库不可用 → 空表（排班照跑，只是不知道存量）。"""
    st = _store(config_manager)
    if st is None:
        return {}
    try:
        return {k: list(v) for k, v in (st.memberships() or {}).items()}
    except Exception:  # noqa: BLE001 —— 台账读不到不该让排班整个失败
        logger.debug("[group_show] 出席台账不可用（按空账处理）", exc_info=True)
        return {}


def _joins_done_today(config_manager: Any) -> Dict[str, int]:
    """``{号: 今天已加群数}``——排期表的第 1 天要按它预扣额度。

    没有这一步，「做完今天 → 点已加入 → 重排」会把限速刷回满格：同一个号当天被派第二轮。
    """
    st = _store(config_manager)
    if st is None:
        return {}
    try:
        return dict(st.joins_since(time.time() - 86400.0) or {})
    except Exception:  # noqa: BLE001 —— 读不到就按「今天还没加过」，不拦排班
        logger.debug("[group_show] 今日加群计数不可用", exc_info=True)
        return {}


def _store(config_manager: Any):
    """场次库（随实例 config 目录走）；不可用 → None（排练照跑，只是不留档）。"""
    try:
        from src.companion.group_show.store import (
            DEFAULT_DB_PATH,
            configure_group_show_store,
        )
        d = _config_dir(config_manager)
        path = (d / "group_show.db") if d is not None else DEFAULT_DB_PATH
        st = configure_group_show_store(path)   # 同路径幂等；换路径会真换库
        return st if getattr(st, "available", False) else None
    except Exception:  # noqa: BLE001
        logger.debug("[group_show] 场次库不可用（排练不落档）", exc_info=True)
        return None


def _inbox_store(request: Any):
    """收件箱库（日常群内出向消息的来源）；拿不到 → None。

    场次库只记编排出来的戏，但日常自动回复 / 坐席手发 / 主动触达同样是这些号在这些
    群里开口，平台一视同仁地数。只读场次库 ⇒ 一场戏没演过就报「安全」，而号可能早已
    在几十个群里互相同框——风险卡最不该犯的**假安全**。
    """
    state = getattr(getattr(request, "app", None), "state", None)
    return getattr(state, "inbox_store", None)


def _show_context(request: Any, config_manager: Any, *, window_days: int = 0,
                  group_key: str = "", pool: Optional[List[str]] = None):
    """本页所有风险读数的**唯一**来源（与影子 CLI、真发链路同一份实现）。

    这里刻意不做任何加工：读账口径（窗口、来源并集、软失败姿势）全在
    :mod:`src.companion.group_show.ledgers`。控制台自己再实现一遍的代价不是重复代码，
    是**口径漂移**——卡片上写「预算 2 人」而排练演 4 人，两个数都自称权威，运营只能
    猜哪个是真的。

    子系统缺失 → ``None``（调用方按空态渲染，不 500）。
    """
    try:
        from src.companion.group_show.ledgers import read_show_context
    except Exception:  # noqa: BLE001 —— 子系统没部署不该让整页打挂
        logger.debug("[group_show] ledgers 模块不可用", exc_info=True)
        return None
    accounts = pool if pool is not None else [
        a for a in (str(r.get("account_id") or "")
                    for r in _online_accounts(config_manager)) if a]
    try:
        return read_show_context(
            _store(config_manager), _inbox_store(request),
            pool=accounts, group_key=group_key, window_days=window_days)
    except Exception:  # noqa: BLE001 —— 读账失败退化成空态，别连累整页
        logger.debug("[group_show] 风险读数不可用（按空态处理）", exc_info=True)
        return None


def _day_start(raw: Any) -> Tuple[float, bool]:
    """``(排期这一天的本地零点, 是不是服务器猜的)``。

    正解是前端把浏览器本地零点传上来——运营人在目标时区，服务器不在。猜出来的值照样
    能用（总比整个功能不能用强），但必须**如实标注**：不标的话，跨境部署里「深夜没排
    演出」这个读数是错的，而它看起来完全正常，没人会去怀疑。
    """
    try:
        ts = float(raw)
        if ts > 0:
            return ts, False
    except (TypeError, ValueError):
        pass
    lt = time.localtime()
    return (time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0,
                         lt.tm_wday, lt.tm_yday, -1)), True)


def _hours_pair(raw: Any, default: Tuple[int, int]) -> Tuple[int, int]:
    """``[起, 止)`` 活动时段；形状不对就用默认（排期器自己也会兜，这里只是早一步）。"""
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            return int(raw[0]) % 24, int(raw[1]) % 24
        except (TypeError, ValueError):
            pass
    return default


def _cooldowns(request: Any, config_manager: Any,
               degraded: List[str]) -> List[Dict[str, Any]]:
    """此刻还在跨群冷却窗里的号 + 还要等多久。

    刻意**只**读冷却那一本账（而不是整份 :func:`_show_context`）：``degraded`` 会原样
    渲染成「冷却名单可能不全」这句话，把预算/角色轴的失败也混进来就成了误报——那句话
    的可信度是它唯一的价值。

    ``group_key=""``：这张表横跨所有群，没有「本群」可排除——问的是「谁刚在**某个**群
    冒过头」，任何一个群都算数。
    """
    try:
        from src.companion.group_show.ledgers import ShowContext, read_last_spoke
    except Exception:  # noqa: BLE001
        return []
    try:
        last = read_last_spoke(_store(config_manager), _inbox_store(request),
                               degraded=degraded)
        return list(ShowContext(last_spoke=last).waiting(sorted(last)))[:12]
    except Exception:  # noqa: BLE001 —— 冷却读数是旁路能力，挂了别拦排期
        logger.debug("[group_show] 冷却名单不可用（按空表处理）", exc_info=True)
        return []


def _resolve_cap(ctx: Any, *, requested: int, allow_over: bool) -> Dict[str, Any]:
    """预算 → 本场开口人数上限（读数缺失时退化成「按运营要的来」）。"""
    if ctx is not None:
        try:
            return ctx.speaker_cap(requested=requested, allow_over=allow_over)
        except Exception:  # noqa: BLE001 —— 算不出来就别拦人，与护栏上线前一致
            logger.debug("[group_show] 开口预算不可用（本场不限）", exc_info=True)
    return {"effective": int(requested or 0), "requested": int(requested or 0),
            "budget": 0, "capped": False, "source": "none", "reason": ""}


# ── 序列化（纯函数，便于单测） ───────────────────────────────────────────────


def split_problems(problems: Any) -> Tuple[List[str], List[str]]:
    """``validate_playbook`` 的结果 → (硬错, 警告)。``warn:`` 前缀＝不阻断的软警告。"""
    hard: List[str] = []
    warn: List[str] = []
    for p in problems or []:
        text = str(p or "")
        if text.startswith("warn:"):
            warn.append(text[5:].strip())
        elif text:
            hard.append(text)
    return hard, warn


def playbook_summary(pb: Any, problems: Any) -> Dict[str, Any]:
    """剧本库一行（列表视图用，不含逐拍明细）。"""
    hard, warn = split_problems(problems)
    return {
        "id": str(getattr(pb, "id", "")),
        "name": str(getattr(pb, "name", "")),
        "system": str(getattr(pb, "system", "")),
        "products": list(getattr(pb, "products", ()) or ()),
        "soft_ad_level": int(getattr(pb, "soft_ad_level", 0) or 0),
        "beat_count": len(getattr(pb, "beats", ()) or ()),
        "roles": [str(r.slot) for r in (getattr(pb, "roles", ()) or ())],
        "errors": hard,
        "warnings": warn,
        "valid": not hard,
    }


def playbook_detail(pb: Any, problems: Any) -> Dict[str, Any]:
    """剧本逐拍表（详情视图用）。``soft`` 已折算成本拍生效值，前端不必再算。"""
    out = playbook_summary(pb, problems)
    pb_soft = int(getattr(pb, "soft_ad_level", 0) or 0)
    out["roles_detail"] = [
        {"slot": str(r.slot), "desc": str(getattr(r, "desc", "") or "")}
        for r in (getattr(pb, "roles", ()) or ())
    ]
    out["beats"] = [{
        "id": str(b.id),
        "role": str(b.role),
        "intent": str(b.intent),
        "product": str(getattr(b, "product", "") or ""),
        "media": str(getattr(b, "media", "") or ""),
        "soft": int(b.effective_soft(pb_soft)),
        "soft_override": int(getattr(b, "soft", -1)) >= 0,
        "pace": str(getattr(b, "pace", "") or "normal"),
    } for b in (getattr(pb, "beats", ()) or ())]
    return out


def rehearsal_payload(result: Any, generator: str) -> Dict[str, Any]:
    """``RehearsalResult`` → JSON（字段名对齐 CLI 的 ``--json``，两边可互相比对）。"""
    casting = getattr(result, "casting", None)
    return {
        "ok": True,
        "generator": generator,
        "session_id": str(getattr(result, "session_id", "")),
        "playbook_id": str(getattr(result, "playbook_id", "")),
        "group_key": str(getattr(result, "group_key", "")),
        "terminate_reason": str(getattr(result, "terminate_reason", "")),
        "line_count": int(getattr(result, "line_count", 0)),
        "duration_seconds": float(getattr(result, "duration_seconds", 0.0)),
        "skipped": int(getattr(result, "skipped", 0)),
        "dry_run": bool(getattr(result, "dry_run", True)),
        "warnings": [str(w) for w in (getattr(result, "warnings", None) or [])],
        "naturalness": dict(getattr(result, "naturalness", None) or {}),
        "casting": {
            "members": [{
                "slot": m.slot, "account_id": m.account_id,
                "persona_id": m.persona_id,
                "display_name": m.display_name or m.account_id,
                "platform": m.platform,
            } for m in (getattr(casting, "members", ()) or ())],
            "unfilled": list(getattr(casting, "unfilled", ()) or ()),
            # 空槽必须说得出为什么空：号不够要补号，预算压的是护栏在正常工作**不该动**，
            # 号对/主推到线要换号或让角色。四种处境糊成「缺角」两个字，运营看着戏一天天
            # 变冷清又找不到原因，最后的动作通常是「把护栏关了试试」。
            "blocked": {str(s): str(r)
                        for s, r in (getattr(casting, "blocked", ()) or ())},
        },
        "lines": [{
            "seq": int(ln.seq),
            "at_seconds": float(ln.at_seconds),
            "account_id": str(ln.account_id),
            "display_name": str(ln.display_name or ln.account_id),
            "persona_id": str(ln.persona_id or ""),
            "role": str(ln.role or ""),
            "beat_id": str(ln.beat_id or ""),
            "soft_level": int(getattr(ln, "soft_level", 0) or 0),
            "media": str(getattr(ln, "media", "") or ""),
            "kind": str(ln.kind or "line"),
            "text": str(ln.text or ""),
        } for ln in (getattr(result, "lines", None) or [])],
    }


# ── 入参解析（纯函数） ──────────────────────────────────────────────────────


def demo_actors(n: Any) -> List[Dict[str, Any]]:
    """占位演员：各自独立指纹组，保证都能同台（只为看编排结构）。"""
    count = 4
    try:
        count = int(n)
    except (TypeError, ValueError):
        count = 4
    count = max(1, min(count, MAX_ACTORS))
    return [{
        "account_id": f"demo{i + 1}",
        "platform": "telegram",
        "persona_id": f"persona_{i + 1}",
        "display_name": _DEMO_NAMES[i % len(_DEMO_NAMES)],
        "health": "online",
        "fingerprint_group": f"fp{i + 1}",
    } for i in range(count)]


def _clamp_int(raw: Any, *, default: int, lo: int, hi: int) -> int:
    """入参取整并夹到区间；解析不了用默认值（表单来的都是字符串，别 400 打回去）。"""
    try:
        return max(lo, min(int(raw), hi))
    except (TypeError, ValueError):
        return default


def group_list(body: Any, *, cap: int = MAX_PLAN_GROUPS) -> List[str]:
    """从请求体取群清单，支持两种模式。

    * ``groups``：真实群清单（字符串数组，或换行分隔的一整段文本——运营多半是从别处
      复制粘贴过来的，逼他们转成 JSON 数组没有意义）；
    * ``group_count``：还没有群 id 时的**测算模式**，用 ``#1..#N`` 占位排一张形状表，
      让运营先看清「这个规模下号池够不够」再去加群。占位符刻意用语言中性的 ``#n``。

    两者都给时以真实清单为准。超过 ``cap`` 截断（排班是 O(群×号×席位) 的贪心）。
    """
    body = body if isinstance(body, dict) else {}
    raw = body.get("groups")
    if isinstance(raw, str):
        raw = raw.replace(",", "\n").splitlines()
    if isinstance(raw, (list, tuple)):
        out: List[str] = []
        seen: set = set()
        for item in raw:
            gid = (str(item.get("group_id") or item.get("chat_key") or "").strip()
                   if isinstance(item, dict) else str(item or "").strip())
            if gid and gid not in seen:
                seen.add(gid)
                out.append(gid)
            if len(out) >= cap:
                break
        if out:
            return out
    count = _clamp_int(body.get("group_count"), default=0, lo=0, hi=cap)
    return [f"#{i + 1}" for i in range(count)]


def parse_human(raw: Any) -> List[Tuple[int, str, str]]:
    """真人插话入参 → ``[(第几条我方发言之后, 谁, 说了什么)]``。

    两种写法都吃：CLI 风格字符串 ``"2:老王:这靠谱吗"``，或结构化
    ``{"after":2,"who":"老王","text":"这靠谱吗"}``。**坏条目静默跳过**——
    排练是探索性操作，不该因为一行手滑就整场 400。
    """
    out: List[Tuple[int, str, str]] = []
    if isinstance(raw, str):
        raw = [ln for ln in raw.splitlines()]
    for item in (raw or []):
        try:
            if isinstance(item, dict):
                after = int(item.get("after", 0))
                who = str(item.get("who") or "群友")
                text = str(item.get("text") or "")
            else:
                line = str(item or "").strip()
                if not line:
                    continue
                parts = line.split(":", 2)
                if len(parts) != 3:
                    continue
                after, who, text = int(parts[0]), parts[1].strip(), parts[2]
            if text.strip():
                out.append((max(0, after), who or "群友", text))
        except (TypeError, ValueError):
            continue
    return out


# ── 注册 ────────────────────────────────────────────────────────────────────


def register_group_show_routes(app, ctx) -> None:
    api_auth = ctx.api_auth
    api_write = ctx.api_write
    page_auth = ctx.page_auth
    templates = ctx.templates
    config_manager = ctx.config_manager
    audit_store = getattr(ctx, "audit_store", None)

    def _books(request: Request):
        """载入剧本库 + 校验器；子系统缺失 → 503。"""
        api = _load_playbook_api()
        if api is None:
            raise HTTPException(503, tr(request, "err.gs.unavailable"))
        load_dir, validate = api
        return load_dir(playbook_dir(config_manager)), validate

    @app.get("/api/group-show/playbooks")
    async def api_group_show_playbooks(request: Request):
        """剧本库：每本一行 + 校验结果（硬错标红、warn 标黄由前端渲染）。"""
        api_auth(request)
        books, validate = _books(request)
        items = [playbook_summary(pb, validate(pb))
                 for _pid, pb in sorted(books.items())]
        return {
            "ok": True,
            "dir": str(playbook_dir(config_manager)),
            "count": len(items),
            "playbooks": items,
        }

    @app.get("/api/group-show/playbooks/{pid}")
    async def api_group_show_playbook_detail(pid: str, request: Request):
        """单本剧本的逐拍表（拍号 / 角色 / 意图 / 产品 / 软广 / 语速）。"""
        api_auth(request)
        books, validate = _books(request)
        pb = books.get(str(pid or ""))
        if pb is None:
            raise HTTPException(
                404, tr(request, "err.gs.playbook_not_found", pid=str(pid or "")))
        return {"ok": True, "playbook": playbook_detail(pb, validate(pb))}

    @app.post("/api/group-show/rehearse")
    async def api_group_show_rehearse(request: Request,
                                      _=Depends(api_write("manage_ops"))):
        """离线演一场（dry-run，绝不发真消息）。

        body: ``{playbook_id, actors, seed, human:[...], use_llm, group_key}``。
        默认占位台词秒出；``use_llm=true`` 才走真 LLM（慢，带服务端硬超时）。
        """
        books, _validate = _books(request)   # 校验结果随 result.warnings 回，不重复算
        runtime_api = _load_runtime_api()
        if runtime_api is None:
            raise HTTPException(503, tr(request, "err.gs.unavailable"))
        rehearse, stub_generator, llm_generator = runtime_api

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 —— 空 body / 非 JSON 一律按空处理
            body = {}
        body = body if isinstance(body, dict) else {}

        pid = str(body.get("playbook_id") or "").strip()
        if not pid:
            raise HTTPException(400, tr(request, "err.gs.playbook_required"))
        pb = books.get(pid)
        if pb is None:
            raise HTTPException(
                404, tr(request, "err.gs.playbook_not_found", pid=pid))

        try:
            seed = int(body.get("seed", 7))
        except (TypeError, ValueError):
            seed = 7
        use_llm = bool(body.get("use_llm"))
        group_key = str(body.get("group_key") or "rehearsal").strip() or "rehearsal"

        generate = None
        generator = "stub"
        if use_llm:
            generate = await _build_llm_generator(config_manager)
            generator = "llm" if generate is not None else "stub"
        if generate is None:
            generate = stub_generator()

        # max_speakers 让运营在排练里先看「只让 N 个号开口是什么效果」——这是演出矩阵
        # 最强的那个杠杆（共现对次对开口人数是二次的），真发前应该先在这里过一眼。
        # 没指定就按预算自动限：排练的用处是**预览真发的样子**，如果它演 4 人而真发只
        # 允许 2 人，这个预览就是在骗人。想看不受限的效果传 over_budget（会如实标注）。
        # 刻意**不**传 co_performance：本端点用的是占位号（``demo_actors``），真实台账
        # 的 account_id 跟它们对不上，传了是空转，还会让人以为轮换已经在起作用。
        cap = _resolve_cap(
            _show_context(request, config_manager),
            requested=_clamp_int(body.get("max_speakers"),
                                 default=0, lo=0, hi=MAX_ACTORS),
            allow_over=bool(body.get("over_budget")))
        coro = rehearse(
            pb, demo_actors(body.get("actors", 4)),
            group_key=group_key, seed=seed, generate=generate,
            human_script=parse_human(body.get("human")),
            max_speakers=int(cap["effective"]),
            store=_store(config_manager),
        )
        try:
            if generator == "llm":
                result = await asyncio.wait_for(coro, timeout=LLM_TIMEOUT_SEC)
            else:
                result = await coro
        except asyncio.TimeoutError:
            raise HTTPException(504, tr(request, "err.gs.rehearse_timeout"))
        except Exception as ex:  # noqa: BLE001 —— 如实回给运营，别吞成白屏
            logger.warning("[group_show] 排练失败 playbook=%s: %s", pid, ex)
            raise HTTPException(
                500, tr(request, "err.gs.rehearse_failed", err=str(ex)))

        if audit_store is not None:
            try:
                actor = "api"
                try:
                    actor = request.session.get("username", "api")
                except Exception:  # noqa: BLE001 —— 无 SessionMiddleware（内部调用）
                    pass
                # 越界必须留痕：排练本身不发消息，但「运营按过这个开关」是后面
                # 真发超预算时唯一能追溯到的线索。
                over = " over_budget=1" if cap["source"] == "override" else ""
                audit_store.log(actor, "group_show_rehearse", f"playbook:{pid}", "",
                                f"generator={generator} lines={result.line_count} "
                                f"speakers<={cap['effective']}{over}")
            except Exception:  # noqa: BLE001
                logger.debug("[group_show] 排练审计写入失败（已忽略）", exc_info=True)

        payload = rehearsal_payload(result, generator)
        payload["speaker_cap"] = cap
        return payload

    @app.get("/api/group-show/linkage")
    async def api_group_show_linkage(request: Request):
        """关联风险体检：这批真号最多能几个同台、瓶颈在哪、怎么解。

        导播台用占位演员排练，热热闹闹四个号——但真发时同出口的号只能上一个。
        这个接口存在的唯一目的，就是不让运营看完一场漂亮排练就以为「可以真发了」。
        """
        api_auth(request)
        try:
            from src.companion.group_show.linkage import (
                linkage_readiness, overrides_from_config,
            )
        except Exception:  # noqa: BLE001
            raise HTTPException(503, tr(request, "err.gs.unavailable"))
        try:
            rows = _online_accounts(config_manager)
            # 运营关联台账（linkage_overrides）与真发同口径——体检说「最多同台 N 人」
            # 必须和选角实际放行的数字一致，否则看板与闸门各说各话。
            report = linkage_readiness(
                rows, overrides=overrides_from_config(
                    getattr(config_manager, "config", None) or {}))
        except Exception:  # noqa: BLE001 —— 体检是辅助信息，挂了不该连累整页排练
            logger.debug("[group_show] 关联体检失败（按空态返回）", exc_info=True)
            report = {"total": 0, "max_concurrent": 0, "by_source": {},
                      "shared_host": [], "problems": [], "advice": []}
        report["ok"] = True
        # groups 里是 account_id 明细，页面只用得上「几组」和瓶颈名单，不外泄全表
        report.pop("groups", None)
        return report

    @app.post("/api/group-show/attendance")
    async def api_group_show_attendance(request: Request):
        """出席矩阵：这批号该分别混进哪些群，以及现在的号池够铺多少个群。

        与 ``/linkage`` 是**两条正交的轴**：那边算「同一个群里能同时上几个号」（网络
        出口），这边算「跨群的成员重合」。多号多群模式下后者才是头号可检测特征——
        同一批号共享同一批群，平台一次成员表 join 就能算出来，且群主肉眼可查。

        用 POST 只是因为群清单可能很长要走 body；本端点**纯计算无副作用**（不落库、
        不发消息、不改注册表），故按只读鉴权，让没有 manage_ops 的运营也能先算账。

        排班会自动读**出席台账**（运营点过「已加入」的记录）：台账里属于本次群清单的
        算存量只排增量，不在清单里的算历史——历史群贡献的共同出席照进度量，否则每批
        新群都显示「安全」，累积起来早就超标了。``existing`` 显式传入时覆盖台账。

        body: ``{groups:[...] | group_count:int, seats:int, existing:{群:[号]}}``
        """
        api_auth(request)
        try:
            from src.companion.group_show.attendance import (
                format_plan, max_safe_groups, plan_attendance,
                recommend_pool_size, rotate_roles, schedule_joins, split_ledger,
            )
            from src.companion.group_show.linkage import derive_fingerprint_groups
        except Exception:  # noqa: BLE001
            raise HTTPException(503, tr(request, "err.gs.unavailable"))

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 —— 空 body / 非 JSON 一律按空处理
            body = {}
        body = body if isinstance(body, dict) else {}

        seats = _clamp_int(body.get("seats"), default=3, lo=1, hi=12)
        rows = _online_accounts(config_manager)
        pool = [str(r.get("account_id") or "") for r in rows]
        pool = [a for a in pool if a]

        groups = group_list(body, cap=MAX_PLAN_GROUPS)
        capacity = {
            "pool_size": len(pool),
            "seats": seats,
            "max_safe_groups": max_safe_groups(pool=len(pool), seats=seats),
            "recommend_pool_size": (
                recommend_pool_size(groups=len(groups), seats=seats)
                if groups else 0),
        }
        if not groups:
            return {"ok": True, "capacity": capacity, "plan": None}

        existing = body.get("existing")
        ledger = _recorded_memberships(config_manager)
        if isinstance(existing, dict):
            ledger.update({str(k): list(v or []) for k, v in existing.items()})
        known, past = split_ledger(ledger, groups)
        plan = plan_attendance(
            pool, groups, seats=seats, existing=known, history=past,
            fingerprint_groups=derive_fingerprint_groups(rows),
        )
        roles = rotate_roles(plan.assignments, _ROTATION_SLOTS[:seats])
        # 排期不是锦上添花：静态表最自然的执行方式是「今天全加完」，而同群同日涌入
        # 三个陌生人比共同出席更刺眼。日程把这条时间轴上的坑堵掉。
        schedule = schedule_joins(
            plan, per_account_per_day=_clamp_int(
                body.get("joins_per_day"), default=2, lo=1, hi=10),
            done_today=_joins_done_today(config_manager))
        return {
            "ok": True,
            "capacity": capacity,
            "plan": {
                "seats": plan.seats,
                "assignments": {k: list(v) for k, v in plan.assignments.items()},
                "joins": {k: list(v) for k, v in plan.joins.items()},
                "already": {k: list(v) for k, v in plan.already.items()},
                "total_joins": plan.total_joins,
                "metrics": plan.metrics,
                "problems": plan.problems,
                "advice": plan.advice,
                "roles": roles,
                "schedule": schedule,
                "text": format_plan(plan),
            },
        }

    @app.post("/api/group-show/attendance/joined")
    async def api_group_show_attendance_joined(
            request: Request, _=Depends(api_write("manage_ops"))):
        """记一笔「这个号已经加进这个群了」（或撤销）。

        这是排班表从「一张纸」变成「能跨天执行的清单」的那一步：没有台账，运营第二天
        打开控制台还是同一张 Day 1，而且历史群的共同出席永远算不进风险度量。

        **只动我们自己的账本**——不发消息、不调平台接口、不会真的加群或退群。撤销
        (``done=false``) 删的也只是账本行，群里的成员关系不受影响。

        body: ``{group: str, account: str, done: bool}``
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        body = body if isinstance(body, dict) else {}
        group = str(body.get("group") or "").strip()
        account = str(body.get("account") or "").strip()
        if not group or not account:
            raise HTTPException(400, tr(request, "err.ws.field_required",
                                        field="group/account"))
        st = _store(config_manager)
        if st is None:
            raise HTTPException(503, tr(request, "err.gs.unavailable"))
        done = body.get("done")
        if done is False:
            st.forget_membership(group, account)
        else:
            st.record_membership(group, account, source="manual")
        return {"ok": True, "group": group, "account": account,
                "done": done is not False}

    @app.get("/api/group-show/exposure")
    async def api_group_show_exposure(request: Request, window_days: int = 0):
        """成员共现 vs 演出共现双读数 + 每场开口人数预算。

        与 ``/attendance`` 的分工：那边排「该加哪些群」（成员面，**加完就冻结**），
        这边读「已经在哪些群开过口」（演出面，**每场都能重排**）。成员面排坏了不等于
        完蛋——演出面才是平台实时看得到的东西，也是那之后唯一还能动的自由度。

        最该被看见的产出是 ``budget``：共现对次对每场开口人数是**二次**的，10 个号
        每场 3 人开口只能安全覆盖 30 个群，降到 2 人就能覆盖 90 个。少让一个号开口
        比多养五个号还管用，且立刻生效。

        演出面取**两个来源的并集**：本子系统的场次库 + 收件箱里所有群内出向消息
        （日常自动回复 / 坐席手发 / 主动触达）。只读场次库会得到「一场戏没演过 ⇒
        安全」的假绿灯，而这些号很可能早就靠日常回复在几十个群里互相同框了——平台
        数的是消息，不区分是不是编排的。

        ``window_days>0`` 只看近 N 天的发言（共现要能随时间淡出，否则跑几个月每一对
        都饱和、这个数就没有分辨力了）；缺省 0 ＝全部历史，前端缺省请求 30 天。

        纯读、无副作用，故按只读鉴权。
        """
        api_auth(request)
        try:
            from src.companion.group_show.ledgers import ALL_HISTORY
        except Exception:  # noqa: BLE001
            raise HTTPException(503, tr(request, "err.gs.unavailable"))

        days = _clamp_int(window_days, default=0, lo=0, hi=3650)
        if _store(config_manager) is None:
            return {"ok": True, "available": False, "window_days": days,
                    "report": None}
        ctx = _show_context(request, config_manager,
                            window_days=days if days else ALL_HISTORY)
        if ctx is None:
            return {"ok": True, "available": False, "window_days": days,
                    "report": None}
        return {
            "ok": True, "available": True, "window_days": days,
            "report": ctx.exposure, "roles": ctx.roles,
            # 三条轴各自的「能铺多少群」差好几倍，而运营只会记住一个数——而且最显眼的
            # 那个偏偏最乐观（``budget`` 对单号开口如实报「无上限」，可那只是发言轴）。
            # 这里给的是**取最紧那条轴**之后的真实上限 + 卡在哪。
            # 注意与 ``/attendance`` 响应里同名字段不是一回事：那边算的是「要加这些群
            # 得备多少号」（成员面选型），这边算的是「按现在这套演法还能铺多少群」。
            "capacity": ctx.capacity,
            # 顶部那盏灯取**最差**的一轴：三条轴里有一条已经露馅，另外两条再干净也救
            # 不回来——平台是按最可疑的那个特征下手的，不是按平均分。
            "verdict": ctx.verdict,
            # 少读一本账 ＝ 共现表变空 ＝ 一片绿。不说出来没人会怀疑它。
            "degraded": list(ctx.degraded),
        }

    @app.post("/api/group-show/schedule")
    async def api_group_show_schedule(request: Request):
        """开演排期：这批群今天各自几点演，以及这张表有多像机器排的。

        与 ``/attendance`` 里那份 ``schedule`` 是两条**不同**的时间轴：那边排的是
        **加群**（一次性动作，约束是「同一个群每天只进一个号」），这边排的是**开演**
        （每天都发生，约束是时段与节奏）。加群摊得再开，开演全挤在同一个小时里照样
        一眼假。

        ``day_start_ts`` 必须由前端给（＝浏览器本地零点）。服务器时区跟群成员所在时区
        没有任何关系，用服务器时区判「深夜」会在跨境场景下把凌晨三点算成下午三点——
        那正是这条轴要防的事故本身。缺参时**才**退回服务器本地零点，并如实标注。

        同号跨群间隔（``admits_at_time``）在排期阶段还判不了——选角在开演时才跑。这里
        只回一份 ``cooldowns``：现在处于冷却窗里的号 + 最早什么时候能上，让运营看得见
        「为什么这场会被往后挪」。真正的闸门挂在真发链路上。

        body: ``{groups:[...] | group_count:int, per_day, active_hours:[起,止],
        day_start_ts, seed}``。纯计算无副作用，按只读鉴权。
        """
        api_auth(request)
        try:
            from src.companion.group_show.schedule import (
                DEFAULT_ACTIVE_HOURS, plan_show_times, schedule_advice,
                schedule_metrics,
            )
        except Exception:  # noqa: BLE001
            raise HTTPException(503, tr(request, "err.gs.unavailable"))

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        body = body if isinstance(body, dict) else {}

        groups = group_list(body, cap=MAX_PLAN_GROUPS)
        if not groups:
            groups = sorted(_recorded_memberships(config_manager))[:MAX_PLAN_GROUPS]
        day_start, guessed = _day_start(body.get("day_start_ts"))
        active = _hours_pair(body.get("active_hours"), DEFAULT_ACTIVE_HOURS)

        plan = plan_show_times(
            groups, day_start_ts=day_start,
            per_day=_clamp_int(body.get("per_day"), default=1, lo=0, hi=6),
            active_hours=active, seed=str(body.get("seed") or ""))
        metrics = schedule_metrics(plan)
        degraded: List[str] = []
        cooldowns = _cooldowns(request, config_manager, degraded)
        return {
            "ok": True, "groups": len(groups), "day_start_ts": day_start,
            "day_start_guessed": guessed, "active_hours": list(active),
            "plan": plan, "metrics": metrics,
            "advice": schedule_advice(metrics),
            "verdict": str(metrics.get("verdict") or "safe"),
            "cooldowns": cooldowns,
            # 台账读挂时「冷却窗空空如也」跟「大家都能上」长得一模一样。少一个来源
            # 必须显式说出来，否则这块绿灯是假的、而且没人会去怀疑它。
            "cooldowns_degraded": degraded,
        }

    @app.get("/api/group-show/sessions")
    async def api_group_show_sessions(request: Request, limit: int = 20,
                                      group_key: str = ""):
        """最近排练/演出场次。库不可用 → 空列表（页面显示空态，不报错）。"""
        api_auth(request)
        st = _store(config_manager)
        if st is None:
            return {"ok": True, "available": False, "sessions": []}
        lim = max(1, min(int(limit or 20), 200))
        rows = st.recent_sessions(group_key=str(group_key or ""), limit=lim)
        return {
            "ok": True,
            "available": True,
            "count": len(rows),
            # casting 是对象（续演用），JSON 只回 cast 字典视图。
            "sessions": [{k: v for k, v in r.items() if k != "casting"}
                         for r in rows],
        }

    @app.post("/api/group-show/live")
    async def api_group_show_live(request: Request,
                                  _=Depends(api_write("manage_ops"))):
        """真发一场群戏（或只做开演前体检）。

        body::

            {
              "playbook_id": "solo_matrixx",   # 单号请用 solo_*
              "group_key": "-100…",
              "platform": "telegram",
              "account_ids": ["…"],            # 可选；缺省=全部在线号
              "confirm_live": true,            # 参数侧那把锁（真发必传）
              "check_only": true,              # 只体检、一条都不发
              "max_speakers": 1,
              "seed": 0
            }

        **为什么真发走后台任务**：一场戏最长 45 分钟，挂在 HTTP 请求上会被反向代理
        掐掉，而且坐席一点按钮就转圈半小时是假 UX。开演前闸门同步返回；真发只回
        ``session_id``，进度看 ``/api/group-show/sessions``。

        **同群互斥**：同一个 ``group_key`` 同时只允许一场真发——两条戏叠在同一群里
        比任何共现指标都更假。
        """
        try:
            from src.companion.group_show.live import (
                is_solo_playbook, live_enabled, plan_live, perform,
            )
            from src.companion.group_show.linkage import (
                derive_fingerprint_groups, overrides_from_config,
            )
            from src.companion.group_show.ledgers import ContextWatch
        except Exception:  # noqa: BLE001
            raise HTTPException(503, tr(request, "err.gs.unavailable"))

        books, _validate = _books(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        body = body if isinstance(body, dict) else {}

        pid = str(body.get("playbook_id") or "").strip()
        if not pid:
            raise HTTPException(400, tr(request, "err.gs.playbook_required"))
        pb = books.get(pid)
        if pb is None:
            raise HTTPException(
                404, tr(request, "err.gs.playbook_not_found", pid=pid))

        group_key = str(body.get("group_key") or "").strip()
        if not group_key:
            raise HTTPException(400, tr(request, "err.gs.group_required"))
        platform = str(body.get("platform") or "telegram").strip().lower() or "telegram"
        check_only = bool(body.get("check_only"))
        confirm = bool(body.get("confirm_live"))
        app_cfg = getattr(config_manager, "config", None) or {}

        rows = _online_accounts(config_manager)
        want = {str(a) for a in (body.get("account_ids") or []) if str(a or "")}
        if want:
            rows = [r for r in rows if str(r.get("account_id") or "") in want]
        actors = _live_actors(rows)
        # 指纹归属＝注册表派生 + 运营关联台账覆盖（linkage_overrides，优先级最高）。
        # 台账是 derive_group 设计好的正门：多机部署/真机蜂窝号靠它声明出口独立，
        # 而不是把 allow_shared_host 那道后门打开。
        fps = (derive_fingerprint_groups(
            rows, overrides=overrides_from_config(app_cfg)) if rows else {})

        ctx = _show_context(request, config_manager, group_key=group_key,
                            pool=[a["account_id"] for a in actors])
        cast_kw = (ctx.cast_kwargs(
            requested=_clamp_int(body.get("max_speakers"),
                                 default=0, lo=0, hi=MAX_ACTORS),
            allow_over=bool(body.get("over_budget")),
            seed=group_key) if ctx is not None else {})

        # 预检：合成武装只为把选角/指纹跑完；静默窗/其它 live.* 仍取真实配置，
        # 否则 overlay 里 quiet_hours:[0,0] 会被合成 dict 抹掉，深夜体检永远假红。
        real_live = {}
        try:
            real_live = dict(
                (((app_cfg.get("companion") or {}).get("group_show") or {})
                 .get("live") or {}))
        except Exception:  # noqa: BLE001
            real_live = {}
        diag_live = {**real_live, "enabled": True}
        diag_cfg = {"companion": {"group_show": {"live": diag_live}}}
        plan = plan_live(
            pb, actors, group_key=group_key, platform=platform,
            confirm_live=True, app_config=diag_cfg, cast_kwargs=cast_kw,
            fingerprint_groups=fps, require_outlet=False)
        casting_view = {}
        if plan.casting is not None:
            casting_view = {
                "filled": {m.slot: m.account_id for m in plan.casting.members},
                "unfilled": list(plan.casting.unfilled),
                "blocked": {str(s): str(r)
                            for s, r in (plan.casting.blocked or ())},
            }

        if check_only:
            # quiet_hours 原样回显：灰度夜测时「文件是 [0,0] 但 reason=quiet_hours」
            # 靠这个一眼分清「配置没进进程」vs「真在禁演窗」。
            try:
                from src.companion.group_show.live import live_quiet_hours
                qh = live_quiet_hours(diag_cfg)
            except Exception:  # noqa: BLE001
                qh = None
            return {
                "ok": True, "check_only": True,
                "playbook_id": pid, "group_key": group_key,
                "solo": bool(is_solo_playbook(pb) or plan.solo),
                "config_enabled": live_enabled(app_cfg),
                "confirm_live": confirm,
                "armed": bool(confirm and live_enabled(app_cfg)),
                "plan_ok": plan.ok, "reason": plan.reason,
                "in_quiet_hours": bool(getattr(plan, "in_quiet_hours", False)),
                "quiet_hours": list(qh) if qh is not None else None,
                "warnings": list(plan.warnings),
                "actors": len(actors), "casting": casting_view,
                "inflight": _inflight_sid(group_key),
            }

        if not confirm:
            raise HTTPException(400, tr(request, "err.gs.live_confirm_required"))
        if not live_enabled(app_cfg):
            raise HTTPException(403, tr(request, "err.gs.live_config_off"))
        if not plan.ok:
            raise HTTPException(409, tr(request, "err.gs.live_preflight_failed",
                                        reason=plan.reason or "unknown"))

        sid = f"live_{int(time.time())}_{group_key[-6:]}"
        held = _claim_live_group(group_key, sid)
        if held:
            raise HTTPException(409, tr(request, "err.gs.live_inflight",
                                        sid=held))

        try:
            from src.integrations.account_orchestrator import (
                get_orchestrator_if_running,
            )
            orch = get_orchestrator_if_running()
        except Exception:  # noqa: BLE001
            orch = None
        if orch is None:
            _release_live_group(group_key, sid)
            raise HTTPException(503, tr(request, "err.gs.live_no_orchestrator"))

        generate = await _build_persona_generator(config_manager)
        if generate is None:
            _release_live_group(group_key, sid)
            raise HTTPException(503, tr(request, "err.gs.live_no_generator"))

        watch = ContextWatch(_store(config_manager), _inbox_store(request),
                             group_key=group_key, platform=platform)
        seed = _clamp_int(body.get("seed"), default=0, lo=0, hi=10**9)

        async def _run() -> None:
            try:
                await perform(
                    pb, actors, group_key=group_key, platform=platform,
                    orchestrator=orch, generate=generate,
                    confirm_live=True, app_config=app_cfg,
                    store=_store(config_manager), watch=watch,
                    cast_kwargs=cast_kw, fingerprint_groups=fps,
                    seed=seed, session_id=sid)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 后台任务崩了也要放锁
                logger.exception("[group_show] 真发后台异常 group=%s sid=%s",
                                 group_key, sid)
            finally:
                _release_live_group(group_key, sid)

        asyncio.create_task(_run())
        if audit_store is not None:
            try:
                actor = "api"
                try:
                    actor = request.session.get("username", "api")
                except Exception:  # noqa: BLE001
                    pass
                audit_store.log(
                    actor, "group_show_live", f"playbook:{pid}", group_key,
                    f"session={sid} actors={len(actors)}")
            except Exception:  # noqa: BLE001
                logger.debug("[group_show] 真发审计写入失败（已忽略）", exc_info=True)

        return {
            "ok": True, "started": True, "session_id": sid,
            "playbook_id": pid, "group_key": group_key,
            "actors": len(actors), "casting": casting_view,
            "solo": bool(is_solo_playbook(pb)),
        }

    @app.get("/group-show", response_class=HTMLResponse)
    async def group_show_page(request: Request, _=Depends(page_auth)):
        return templates.TemplateResponse(
            request, "group_show.html",
            {"request": request, "active": "group_show"},
        )


async def _build_llm_generator(config_manager: Any):
    """尽力构造真 LLM 台词生成器；构造不出来返回 None（调用方回落占位）。

    姿势照抄 ``scripts/group_show_rehearse.py::_build_llm_generator``：
    ``ConfigManager.load()`` 是 **async**、``AIClient.__init__`` 只给安全默认
    （真正读配置在 ``initialize()``）——两步都不能省，漏了会让 AIClient 拿着
    半成品配置把整场戏生成成空。web 侧已有装载好的 ConfigManager，优先复用它
    （省一次磁盘读，也天然避开忘 await 的坑）。
    """
    try:
        from src.ai.ai_client import AIClient
        from src.companion.group_show.runtime import llm_generator

        cfg_mgr = config_manager
        if not hasattr(cfg_mgr, "get_ai_config"):
            from src.utils.config_manager import ConfigManager
            cfg_path = getattr(config_manager, "config_path", None)
            cfg_mgr = (ConfigManager(str(cfg_path)) if cfg_path
                       else ConfigManager())
            await cfg_mgr.load()        # ⚠ async——忘了 await 全场生成失败
        client = AIClient(cfg_mgr)
        await client.initialize()
        return llm_generator(client)
    except Exception:  # noqa: BLE001 —— 构造不出来就回落占位，别让排练跑不成
        logger.warning("[group_show] 无法构造 LLM 客户端，回落占位台词", exc_info=True)
        return None


async def _build_persona_generator(config_manager: Any):
    """真发专用：人设发声链 + persona_guard。构造失败 → ``None``（调用方拒演）。

    刻意**不**回落 ``stub_generator`` / ``llm_generator``——前者会把占位串发进真群，
    后者没有人设味道也没有自曝守卫。真发宁可不演，也不发错声。
    """
    try:
        from src.ai.ai_client import AIClient
        from src.companion.group_show.voice import persona_generator

        cfg_mgr = config_manager
        if not hasattr(cfg_mgr, "get_ai_config"):
            from src.utils.config_manager import ConfigManager
            cfg_path = getattr(config_manager, "config_path", None)
            cfg_mgr = (ConfigManager(str(cfg_path)) if cfg_path
                       else ConfigManager())
            await cfg_mgr.load()
        client = AIClient(cfg_mgr)
        await client.initialize()
        persona_mgr = None
        try:
            from src.utils.persona_manager import PersonaManager
            persona_mgr = PersonaManager.get_instance()
        except Exception:  # noqa: BLE001 —— 没人设管理器也能演，只是味道淡一点
            logger.debug("[group_show] PersonaManager 不可用（人设发声降级）",
                         exc_info=True)
        return persona_generator(client, persona_manager=persona_mgr)
    except Exception:  # noqa: BLE001
        logger.warning("[group_show] 无法构造人设发声链", exc_info=True)
        return None
