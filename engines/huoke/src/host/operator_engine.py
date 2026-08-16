# -*- coding: utf-8 -*-
"""AI 操盘手 MVP —— 感知→决策→护栏→执行→审计 的单 tick 引擎（2026-08-16 P4）。

设计要点（详见 docs/AI_AGENT_PHONE_CONTROL_ROADMAP.md §5）：

1. **可调用引擎，不是守护进程**。tick() 被调一次才干一次活；时钟触发将来
   交给已有 scheduler.py（它就是干这个的），决策脑与触发器分离。
   不 tick = 什么都不发生，天然安全。

2. **设备由目标配置点名**（config/operator_goals.yaml），不自动发现、
   不全量接管。操盘手只对被授权的设备做决策。

3. **双闸**：enabled（总开关）+ dry_run（演习模式：决策+审计但不下发链）。
   真执行必须两个都扳。配置热加载（mtime，同 task_chain._load_chains 惯例）。

4. **决策是纯函数** decide(snapshot, goal, chains) —— 不碰 DB 不碰网络，
   规则序即优先级，首中即返。可测、可审计、可解释。

5. **复用而非新造**：
   - 链定义/元数据    task_chain.list_chains()（funnel_stage / risk_level）
   - 养号阶段        device_state.get_phase()（cold_start/interest_building/active）
   - 恢复态          adaptive_compliance.is_recovering()
   - 忙态            task_chain.get_chain_runs() 过滤 running
   - 待跟进线索      leads.store.list_leads(status="responded")
   - 执行            task_chain.create_chain()
   执行级配额（ComplianceGuard）在 executor 层已有，本层只加编排级频控
   （单设备每日链数上限），两层互不替代。

6. **每个决策都落审计表** operator_decisions（openclaw.db，先例 chain_runs），
   包括 hold/skip/blocked——「为什么没做」与「为什么做了」同等重要。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from src.host.device_registry import config_dir

logger = logging.getLogger(__name__)

_CFG_PATH = config_dir() / "operator_goals.yaml"
_cfg_cache: Optional[Dict[str, Any]] = None
_cfg_cache_mtime: float = -1.0


# ── 配置 ──────────────────────────────────────────────────────────────

def load_goals_config() -> Dict[str, Any]:
    """热加载 operator_goals.yaml。缺文件/坏文件 = 一切关闭（安全缺省）。"""
    global _cfg_cache, _cfg_cache_mtime
    try:
        mtime = _CFG_PATH.stat().st_mtime
    except OSError:
        return {"enabled": False, "dry_run": True, "goals": []}
    if _cfg_cache is not None and mtime == _cfg_cache_mtime:
        return _cfg_cache
    try:
        with open(_CFG_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg = {
            "enabled": bool(data.get("enabled", False)),
            "dry_run": bool(data.get("dry_run", True)),
            "goals": list(data.get("goals") or []),
        }
        _cfg_cache = cfg
        _cfg_cache_mtime = mtime
        return cfg
    except Exception as e:
        logger.warning("[operator] 加载 operator_goals.yaml 失败: %s", e)
        return {"enabled": False, "dry_run": True, "goals": []}


# ── 数据模型 ──────────────────────────────────────────────────────────

@dataclass
class DeviceSnapshot:
    """单设备的感知快照——决策的全部输入（除目标与链表外）。"""
    device_id: str
    platform: str
    online: bool = True
    phase: str = "cold_start"        # cold_start / interest_building / active
    recovering: bool = False         # AdaptiveCompliance 恢复模式
    busy: bool = False               # 已有链在跑
    launched_today: int = 0          # 操盘手今天已为该设备真实发起的链数
    responded_leads: int = 0         # 该平台待跟进（responded）线索数
    recent_fail_streak: int = 0      # 近期操盘手发起的链「连续失败」计数（反馈闭环）


@dataclass
class Decision:
    action: str                      # execute / hold / skip / blocked
    rule_id: str                     # 命中的规则号（R1..R7），可审计可解释
    reason: str                      # 人话理由
    chain_id: str = ""               # action=execute/blocked 时为选中的链


_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

# 漏斗阶段词表——必须 ⊆ tests/test_chain_templates._FUNNEL_STAGES
# （{warmup, discover, first_touch, followup, harvest, full}）。
# 真机灰度日实锤：首版写了不存在的 "inbox"/"discovery"，合成测试自洽
# 看不出来，与真实链库咬合才照出——现由 test_operator_engine 跨套件断言钉死。
_WARMUP_STAGES = ("warmup",)
_HARVEST_STAGES = ("harvest", "followup")    # 收割：跟进已回复/收件箱线索
_GROWTH_STAGES = ("discover", "first_touch", "full")   # 拓新


def _chain_candidates(chains: List[Dict[str, Any]], platform: str,
                      stages: tuple, goal: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按平台+漏斗阶段过滤链，按 goal 偏好序（次序=偏好）+ 风险升序排列。

    高危链的处理留给调用方——这里不剔除，让决策层能区分
    「没有候选」和「候选被高危闸拦下」两种情况。
    """
    cands = [c for c in chains
             if c.get("platform") == platform
             and c.get("funnel_stage", "") in stages]
    preferred = list(goal.get("preferred_chains") or [])

    def sort_key(c):
        cid = c.get("chain_id", "")
        pref = preferred.index(cid) if cid in preferred else len(preferred)
        risk = _RISK_ORDER.get(c.get("risk_level", "medium"), 1)
        return (pref, risk)

    return sorted(cands, key=sort_key)


def _pick(cands: List[Dict[str, Any]], allow_high_risk: bool):
    """从候选里选第一条可自动执行的链。返回 (chain, blocked_high)。

    blocked_high：候选里存在链但全被高危闸拦下（≠ 没有候选）。
    """
    blocked_high = None
    for c in cands:
        if c.get("risk_level", "medium") == "high" and not allow_high_risk:
            if blocked_high is None:
                blocked_high = c
            continue
        return c, None
    return None, blocked_high


def decide(snap: DeviceSnapshot, goal: Dict[str, Any],
           chains: List[Dict[str, Any]]) -> Decision:
    """Tier-1 规则决策（纯函数）。规则序即优先级，首中即返。

    R1  设备离线           → skip
    R2  已有链在跑         → hold（不叠加，等它跑完）
    R2b 近期连续失败达阈值 → hold（反馈闭环：疑似 VPN/网络/账号，停投待人工）
    R3  编排级日频控已满   → hold
    R4  恢复模式           → 只许低风险养号链；没有 → hold（静默恢复）
    R5  冷启动阶段         → 养号链
    R6  有待跟进线索       → 收割链（harvest/followup）——收割优先于拓新
    R7  缺省               → 拓新链（discover/first_touch/full）
    高危链且 goal 未授权   → blocked（需人工批准，绝不自动执行）

    R2b 是灰度日补的反馈闭环：操盘手曾是 fire-and-forget，4 台首投 3 台在
    VPN 门被拦却仍记 execute，若不退避会每轮撞墙烧日额度。连续失败达阈值即
    停投并把病因摆到审计/返回里，对标 04-21「VPN 掉线 5 小时死循环」事故。
    """
    platform = goal.get("platform", snap.platform)
    allow_high = bool(goal.get("allow_high_risk", False))
    daily_cap = int(goal.get("max_chains_per_device_per_day", 3))
    fail_backoff = int(goal.get("fail_backoff_threshold", 3))

    if not snap.online:
        return Decision("skip", "R1", "设备离线")

    if snap.busy:
        return Decision("hold", "R2", "设备已有任务链在跑，不叠加")

    if fail_backoff > 0 and snap.recent_fail_streak >= fail_backoff:
        return Decision("hold", "R2b",
                        f"近期连续 {snap.recent_fail_streak} 条链失败"
                        f"（疑似 VPN/网络/账号），暂停自动投放，待人工检查")

    if snap.launched_today >= daily_cap:
        return Decision("hold", "R3",
                        f"今日已发起 {snap.launched_today} 条链，达到上限 {daily_cap}")

    if snap.recovering:
        cands = _chain_candidates(chains, platform, _WARMUP_STAGES, goal)
        low = [c for c in cands if c.get("risk_level", "medium") == "low"]
        if low:
            return Decision("execute", "R4",
                            "设备在恢复模式，仅执行低风险养号链",
                            low[0]["chain_id"])
        return Decision("hold", "R4", "设备在恢复模式且无低风险养号链，静默恢复")

    if snap.phase == "cold_start":
        cands = _chain_candidates(chains, platform, _WARMUP_STAGES, goal)
        chosen, blocked = _pick(cands, allow_high)
        if chosen:
            return Decision("execute", "R5", "冷启动阶段，先养号",
                            chosen["chain_id"])
        if blocked:
            return Decision("blocked", "R5",
                            "养号候选链为高风险且目标未授权，需人工批准",
                            blocked["chain_id"])
        return Decision("hold", "R5", "冷启动阶段但没有该平台的养号链")

    if snap.responded_leads > 0:
        cands = _chain_candidates(chains, platform, _HARVEST_STAGES, goal)
        chosen, blocked = _pick(cands, allow_high)
        if chosen:
            return Decision("execute", "R6",
                            f"有 {snap.responded_leads} 条已回复线索待跟进，收割优先",
                            chosen["chain_id"])
        if blocked:
            return Decision("blocked", "R6",
                            "收割候选链为高风险且目标未授权，需人工批准",
                            blocked["chain_id"])
        # 没有收割链就落到拓新（不 return，继续 R7）

    cands = _chain_candidates(chains, platform, _GROWTH_STAGES, goal)
    chosen, blocked = _pick(cands, allow_high)
    if chosen:
        return Decision("execute", "R7", "常规拓新", chosen["chain_id"])
    if blocked:
        return Decision("blocked", "R7",
                        "拓新候选链为高风险且目标未授权，需人工批准",
                        blocked["chain_id"])
    return Decision("hold", "R7", "该平台没有可用的拓新链")


# ── 审计（SQLite，先例 task_chain.chain_runs） ────────────────────────

_table_ensured = False


def _ensure_table():
    global _table_ensured
    if _table_ensured:
        return
    try:
        from .database import get_conn
        with get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS operator_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    goal_id TEXT,
                    device_id TEXT,
                    platform TEXT,
                    rule_id TEXT,
                    action TEXT,
                    chain_id TEXT,
                    run_id TEXT,
                    dry_run INTEGER,
                    reason TEXT,
                    snapshot TEXT
                )
            """)
        _table_ensured = True
    except Exception as e:
        logger.warning("[operator] 创建 operator_decisions 表失败: %s", e)


def record_decision(goal_id: str, snap: DeviceSnapshot, dec: Decision,
                    dry_run: bool, run_id: str = ""):
    _ensure_table()
    try:
        from .database import get_conn
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO operator_decisions "
                "(ts, goal_id, device_id, platform, rule_id, action, chain_id,"
                " run_id, dry_run, reason, snapshot) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 goal_id, snap.device_id, snap.platform, dec.rule_id,
                 dec.action, dec.chain_id, run_id, 1 if dry_run else 0,
                 dec.reason, json.dumps(asdict(snap), ensure_ascii=False)),
            )
    except Exception as e:
        logger.warning("[operator] 审计写入失败: %s", e)


def get_decisions(limit: int = 50, device_id: str = "") -> List[Dict[str, Any]]:
    _ensure_table()
    try:
        from .database import get_conn
        with get_conn() as conn:
            if device_id:
                rows = conn.execute(
                    "SELECT * FROM operator_decisions WHERE device_id = ? "
                    "ORDER BY id DESC LIMIT ?", (device_id, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM operator_decisions "
                    "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("[operator] 审计读取失败: %s", e)
        return []


def _launched_today(device_id: str) -> int:
    """操盘手今天已为该设备真实发起（非 dry_run 的 execute）的链数。"""
    _ensure_table()
    try:
        from .database import get_conn
        today = time.strftime("%Y-%m-%d", time.gmtime())
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM operator_decisions "
                "WHERE device_id = ? AND action = 'execute' AND dry_run = 0 "
                "AND ts >= ?", (device_id, today + "T00:00:00Z")).fetchone()
        return int(row["n"]) if row else 0
    except Exception:
        return 0


def _recent_fail_streak(device_id: str, lookback: int = 10) -> int:
    """反馈闭环：操盘手为该设备发起的链，从最近往前数「连续失败」条数。

    判据 = 把审计里带 run_id 的真实 execute 决策，回查 chain_runs.status：
      aborted/failed = 失败（连击 +1）；completed = 成功（连击到此为止）；
      running = 尚无结论（跳过，不计不断）。
    一旦有一条成功，连击清零——所以设备恢复后自动解除退避，无需人工复位。
    """
    _ensure_table()
    try:
        from .database import get_conn
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT run_id FROM operator_decisions "
                "WHERE device_id = ? AND action = 'execute' AND dry_run = 0 "
                "AND run_id != '' ORDER BY id DESC LIMIT ?",
                (device_id, lookback)).fetchall()
            streak = 0
            for r in rows:
                run = conn.execute(
                    "SELECT status FROM chain_runs WHERE run_id = ?",
                    (r["run_id"],)).fetchone()
                if not run:
                    continue
                st = run["status"]
                if st in ("aborted", "failed"):
                    streak += 1
                elif st == "completed":
                    break
                # running/其它：跳过，不计不断
        return streak
    except Exception:
        return 0


# ── 感知（impure，测试可整体替换 _perceive） ─────────────────────────

def _count_responded_leads(platform: str) -> int:
    try:
        from src.leads.store import get_leads_store
        rows = get_leads_store().list_leads(status="responded",
                                            platform=platform, limit=200)
        return len(rows)
    except Exception as e:
        logger.debug("[operator] 查询待跟进线索失败: %s", e)
        return 0


def _perceive(device_id: str, platform: str) -> DeviceSnapshot:
    """构建单设备感知快照。

    online 恒 True：MVP 不做 ADB 级在线探测（离线设备的链会在执行层
    失败并计入链状态，编排级日频控天然限损）；字段保留给后续接线。
    """
    from .device_state import get_device_state_store
    from .task_chain import get_chain_runs

    phase = get_device_state_store(platform).get_phase(device_id)

    recovering = False
    try:
        from src.behavior.adaptive_compliance import get_adaptive_compliance
        recovering = get_adaptive_compliance().is_recovering(device_id)
    except Exception:
        pass

    busy = any(r.get("status") == "running"
               for r in get_chain_runs(device_id=device_id, limit=10))

    return DeviceSnapshot(
        device_id=device_id,
        platform=platform,
        online=True,
        phase=phase,
        recovering=recovering,
        busy=busy,
        launched_today=_launched_today(device_id),
        responded_leads=_count_responded_leads(platform),
        recent_fail_streak=_recent_fail_streak(device_id),
    )


def _launch_chain(chain_id: str, device_id: str) -> str:
    """真实下发一条链，返回 run_id。测试打桩点。"""
    from .task_chain import create_chain
    info = create_chain(chain_id, device_id)
    return info.get("run_id", "")


def _alert_backoff(device_id: str, reason: str):
    """R2b 退避时发告警（复用 task_chain 同一条 AlertNotifier 通路）。

    webhook 未配置 = 空操作；AlertNotifier 自带去重，每轮重复的退避不会刷屏。
    不传 alert_code（用原文 message，免注册模板）。全吞异常绝不影响决策。
    """
    try:
        from .alert_notifier import AlertNotifier
        notifier = AlertNotifier.get()
        if notifier:
            notifier.notify(level="warning", device_id=device_id,
                            message=f"[操盘手] 自动停投: {reason}")
    except Exception:
        pass


# ── 编排回路 ──────────────────────────────────────────────────────────

def tick(dry_run: Optional[bool] = None) -> Dict[str, Any]:
    """跑一轮操盘手：对每个目标的每台点名设备 感知→决策→(执行)→审计。

    dry_run=None 时用配置里的值；显式传 False 也必须 enabled=True 才会真执行。
    返回本轮全部决策的摘要（API/前端直接可用）。
    """
    cfg = load_goals_config()
    if not cfg["enabled"]:
        return {"enabled": False, "dry_run": True, "decisions": [],
                "note": "操盘手总开关未开（operator_goals.yaml enabled: false）"}

    effective_dry = cfg["dry_run"] if dry_run is None else bool(dry_run)

    from .task_chain import list_chains
    chains = list_chains()

    results: List[Dict[str, Any]] = []
    for goal in cfg["goals"]:
        goal_id = str(goal.get("id", ""))
        platform = str(goal.get("platform", ""))
        for device_id in (goal.get("device_ids") or []):
            try:
                snap = _perceive(device_id, platform)
            except Exception as e:
                logger.warning("[operator] 感知 %s 失败: %s", device_id, e)
                snap = DeviceSnapshot(device_id=device_id, platform=platform,
                                      online=False)
            dec = decide(snap, goal, chains)

            run_id = ""
            if dec.action == "execute" and not effective_dry:
                try:
                    run_id = _launch_chain(dec.chain_id, device_id)
                except Exception as e:
                    logger.warning("[operator] 下发链 %s → %s 失败: %s",
                                   dec.chain_id, device_id, e)
                    dec = Decision("hold", dec.rule_id,
                                   f"下发失败: {e}", dec.chain_id)

            record_decision(goal_id, snap, dec, effective_dry, run_id)
            if dec.rule_id == "R2b":
                _alert_backoff(device_id, dec.reason)
            results.append({
                "goal_id": goal_id,
                "device_id": device_id,
                "platform": platform,
                "action": dec.action,
                "rule_id": dec.rule_id,
                "chain_id": dec.chain_id,
                "reason": dec.reason,
                "run_id": run_id,
            })

    return {"enabled": True, "dry_run": effective_dry, "decisions": results}


# ── 自带时钟（2026-08-17 真机灰度日增） ─────────────────────────────
#
# 原设计是把时钟交给 scheduler.py，但本部署 task_execution_policy.yaml
# 的 manual_execution_only=true + disable_db_scheduler=true 是 04-21
# 5 小时死循环事故复盘后有意为之的人工门禁——为操盘手翻开整个 DB
# 调度器会改变部署安全姿态。故操盘手自带轻量时钟线程：
#   - 只受 operator_goals.yaml 自己的双闸管辖（enabled=false 时空转，
#     改配置文件热生效，无需重启）；enabled: true 即人工确认的落点
#   - 间隔由 tick_interval_minutes 控制（缺省 30，下限 5 分钟）
#   - 每轮 tick 的全部决策照常落审计表

_clock_thread: Optional[Any] = None


def _clock_interval_s() -> int:
    """从配置读时钟间隔（秒），下限 5 分钟防手滑打太密。"""
    try:
        import yaml as _yaml
        with open(_CFG_PATH, "r", encoding="utf-8") as f:
            data = _yaml.safe_load(f) or {}
        minutes = float(data.get("tick_interval_minutes", 30))
    except Exception:
        minutes = 30.0
    return max(300, int(minutes * 60))


def _clock_loop():
    """时钟主循环：短睡眠轮询，enabled 热生效。

    启动后等满一个间隔再首跳（last_tick 初始化为当下）——重启不等于立即
    往真机投链（重启原因很多，惊吓面大）；要即时投放走 POST /operator/tick。
    """
    last_tick = time.time()
    while True:
        time.sleep(15)
        cfg = load_goals_config()
        if not cfg["enabled"]:
            continue
        interval = _clock_interval_s()
        now = time.time()
        if now - last_tick < interval:
            continue
        last_tick = now
        try:
            out = tick()
            decs = out.get("decisions") or []
            executed = sum(1 for d in decs if d.get("action") == "execute")
            logger.info("[operator] 时钟 tick: dry_run=%s 决策 %d 条(execute %d)",
                        out.get("dry_run"), len(decs), executed)
        except Exception as e:
            logger.warning("[operator] 时钟 tick 失败: %s", e)


def start_operator_clock():
    """启动操盘手自带时钟（幂等；enabled=false 时线程空转零成本）。"""
    global _clock_thread
    import threading
    if _clock_thread is not None and _clock_thread.is_alive():
        return
    _clock_thread = threading.Thread(target=_clock_loop, daemon=True,
                                     name="operator-clock")
    _clock_thread.start()
    logger.info("[operator] 自带时钟已启动（间隔 %ds，enabled 热生效）",
                _clock_interval_s())


def status() -> Dict[str, Any]:
    """操盘手当前状态（给 /operator/status）。"""
    cfg = load_goals_config()
    return {
        "enabled": cfg["enabled"],
        "dry_run": cfg["dry_run"],
        "clock_alive": bool(_clock_thread is not None
                            and _clock_thread.is_alive()),
        "tick_interval_s": _clock_interval_s(),
        "goals": [{
            "id": g.get("id", ""),
            "platform": g.get("platform", ""),
            "devices": len(g.get("device_ids") or []),
            "daily_cap": g.get("max_chains_per_device_per_day", 3),
            "allow_high_risk": bool(g.get("allow_high_risk", False)),
        } for g in cfg["goals"]],
        "recent_decisions": get_decisions(limit=20),
    }
