"""账号级「工作时间」闸门（配置 ``inbox.work_schedule``）——非工作时间不自动回复。

背景与定位（2026-08-04 P0）：入站自动回复此前**零时间感知**——A 线直发 /
B 线 autosend / 协议号直发三条出口 7×24 秒回，既是拟人破绽（凌晨秒回=一眼假）
也是平台风控特征。本模块把「这个账号现在上不上班」收成单一纯函数事实源，
三条出口同一判定；主动触达/仪式同样消费（休息中的账号不该主动搭讪）。

与既有时间语义的关系（仓里已有五套时钟，别混）：
- ``src/utils/work_schedule.py``：**人工客服团队**排班（human_escalation @ 值班
  客服），schema=mon..sun 区间表 + 按日例外——服务的是「人」的排班；本模块服务
  「AI 替身账号」的作息，消费方/失败语义都不同（那边缺配置=不在班，这边缺配置
  =**必须放行**），刻意不复用其 schema。多段排班/按日例外留给 P2。
- ``companion.proactive_topic.quiet_*``：主动触达安静时段（防骚扰收窄）；
- ``user_clock``：客户时区（对方几点）；``persona_location``：人设时区。
  本班表按**账号时区**评估——工作时间是运营/人设侧的作息，不是客户的。

安全语义（每条有测试钉住，见 tests/test_work_hours_gate.py）：
- **fail-open**：配置缺失/解析失败/时区非法/内部异常 → 一律视为「在班」。
  拦截才是有风险的动作，闸门自身故障绝不能把全部自动回复闸死；
- **危机豁免**（``crisis_bypass`` 默认开）：入站文本命中 severe/elevated 危机
  信号（``wellbeing_guard.detect_crisis``，纯函数）→ 穿透闸门照常回复。
  半夜的求助不能被班表静默——这是安全红线不是可选项；
- **人工通道永不受闸**：本模块只被自动链消费（A 线直发 / B 线 autosend 自动
  循环 / 协议号 run_autoreply / auto_draft 拟稿节流）；手动发送端点与人工通过
  投递（``deliver_human_approved``）不经过本判定；
- **边界确定性抖动**（``edge_jitter_min``）：23:00:00 准时消失与凌晨秒回同样
  穿帮。上/下班边界按 crc32(账号#班次日期#边) 当日恒定偏移 ±N 分钟（同日重算
  不抖振、明日自动换），与 checkin_angle / outfit_state 同族确定性模式。
  抖动按**班次开始日**取键——跨午夜班次的收班边界跟随开班日，不会在午夜换键
  抖振。

配置（``config.yaml::inbox.work_schedule``，新子系统约定默认 enabled:false）::

    inbox:
      work_schedule:
        enabled: false
        timezone: ""            # 班表时区（IANA 名，如 Asia/Shanghai；空=服务器本地钟）
        default:                # 全局默认班表
          workdays: [1,2,3,4,5,6,7]   # ISO 周几（1=周一..7=周日）；空/缺省=每天
          start: "09:00"
          end: "23:00"          # start>end = 跨午夜班（如 14:00-02:00）
                                # start==end = 全天在班（等于该账号不闸）
        edge_jitter_min: 20     # 边界抖动幅度（分钟，0=关）
        crisis_bypass: true     # severe/elevated 危机消息穿透闸门
        off_hours:
          generate_drafts: true # 休息期是否仍拟稿（人可介入 + 危机可见）
          catch_up: true        # 复班后补觉投递被扣住的稿
          catch_up_regenerate_hours: 2  # 稿龄超此值 → 重生成再发（防内容穿帮）
        accounts:               # 账号覆写（键 = "platform:account_id"）
          "telegram:default": { start: "10:00", end: "02:00" }
          # 账号亦可 enabled:false = 该账号豁免班表（全天候）

跨午夜语义：班次归属**开始日**——workdays 含周五、班表 22:00-06:00 时，周六
凌晨 04:00 仍在班（那是周五的班次）；周六本身不在 workdays 也不影响这条尾巴。
"""

from __future__ import annotations

import logging
import time
from binascii import crc32
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

logger = logging.getLogger(__name__)

# should_hold_auto_reply 的扣留原因（协议号 HANDOFF_REASONS 已有同名 off_hours 语义）
HOLD_REASON_OFF_HOURS = "off_hours"

_DAY_MIN = 24 * 60
# 抖动幅度硬上限（分钟）：再大就不是「拟人浮动」而是班表失真
_JITTER_CAP = 180


# ── 配置解析 ────────────────────────────────────────────────


def work_schedule_cfg(config: Any) -> Dict[str, Any]:
    """从完整配置树挖 ``inbox.work_schedule`` 子树（缺失/形状不对 → {}）。"""
    try:
        inbox = config.get("inbox") if isinstance(config, Mapping) else None
        ws = inbox.get("work_schedule") if isinstance(inbox, Mapping) else None
        return dict(ws) if isinstance(ws, Mapping) else {}
    except Exception:
        return {}


def _parse_hhmm(raw: Any) -> Optional[int]:
    """'HH:MM' → 自 0 点起的分钟数；解析失败 → None（上层按 fail-open 处理）。"""
    try:
        s = str(raw or "").strip()
        hh, mm = s.split(":", 1)
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (ValueError, TypeError):
        pass
    return None


def _parse_workdays(raw: Any) -> frozenset:
    """ISO 周几列表（1=周一..7=周日）；空/非法 → 全周（宁可多在班不可静默）。"""
    days = set()
    if isinstance(raw, (list, tuple, set, frozenset)):
        for v in raw:
            try:
                d = int(v)
            except (TypeError, ValueError):
                continue
            if 1 <= d <= 7:
                days.add(d)
    return frozenset(days) if days else frozenset(range(1, 8))


def _edge_jitter_min(ws: Mapping) -> int:
    try:
        j = int(float(ws.get("edge_jitter_min", 20)))
    except (TypeError, ValueError):
        j = 20
    return max(0, min(_JITTER_CAP, j))


def resolve_entry(ws: Any, platform: str, account_id: str) -> Dict[str, Any]:
    """账号覆写 ⊕ 全局默认 → 归一化班表条目。

    ``gated=False`` 的三种来源（都表示「该账号不受闸」）：账号显式
    ``enabled:false``（豁免=全天候）、start/end 缺失或非法（fail-open）、
    ``start == end``（显式全天窗）。
    """
    ws = ws if isinstance(ws, Mapping) else {}
    default = ws.get("default") if isinstance(ws.get("default"), Mapping) else {}
    accounts = ws.get("accounts") if isinstance(ws.get("accounts"), Mapping) else {}
    key = f"{str(platform or '').strip().lower()}:{str(account_id or 'default').strip()}"
    merged: Dict[str, Any] = dict(default)
    source = "default"
    ov = accounts.get(key)
    if isinstance(ov, Mapping):
        source = "account"
        merged.update(ov)
    gated = bool(merged.get("enabled", True))
    start_min = _parse_hhmm(merged.get("start"))
    end_min = _parse_hhmm(merged.get("end"))
    if start_min is None or end_min is None or start_min == end_min:
        gated = False
    tz_name = str(merged.get("timezone") or ws.get("timezone") or "").strip()
    return {
        "key": key,
        "source": source,
        "gated": gated,
        "workdays": _parse_workdays(merged.get("workdays")),
        "start_min": int(start_min or 0),
        "end_min": int(end_min or 0),
        "tz_name": tz_name,
    }


def off_hours_cfg(ws: Any) -> Dict[str, Any]:
    """``off_hours`` 行为块（休息期拟稿 / 复班补觉）归一化读取。"""
    node = ws.get("off_hours") if isinstance(ws, Mapping) else None
    oh = node if isinstance(node, Mapping) else {}
    try:
        regen_h = float(oh.get("catch_up_regenerate_hours", 2) or 0)
    except (TypeError, ValueError):
        regen_h = 2.0
    return {
        "generate_drafts": bool(oh.get("generate_drafts", True)),
        "catch_up": bool(oh.get("catch_up", True)),
        "catch_up_regenerate_hours": max(0.0, regen_h),
    }


# ── 时钟与班次区间 ──────────────────────────────────────────


def _resolve_tz(tz_name: str):
    """IANA 时区名 → tzinfo；空/非法/无 zoneinfo → None（=服务器本地钟）。"""
    if tz_name and ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            logger.debug("[work-schedule] 无效时区 %r，回落服务器本地钟", tz_name)
    return None


def _local_wall(ts: float, tz) -> datetime:
    """epoch → 班表时区的 naive 墙钟时间（全部比较在墙钟空间做）。"""
    if tz is not None:
        return datetime.fromtimestamp(ts, tz).replace(tzinfo=None)
    return datetime.fromtimestamp(ts)


def _wall_to_ts(dt: datetime, tz) -> float:
    """naive 墙钟时间 → epoch（DST fold 取系统默认侧，误差可接受）。"""
    try:
        if tz is not None:
            return dt.replace(tzinfo=tz).timestamp()
        return dt.timestamp()
    except Exception:
        return 0.0


def _jitter(key: str, day_iso: str, edge: str, amp: int) -> int:
    """确定性边界抖动（分钟，[-amp, +amp]）：同账号同班次日恒定，明日自动换。"""
    if amp <= 0:
        return 0
    h = crc32(f"{key}#{day_iso}#{edge}".encode("utf-8"))
    return (h % (2 * amp + 1)) - amp


def _shift_interval(
    entry: Mapping, day, amp: int,
) -> Tuple[datetime, datetime]:
    """开始日为 ``day`` 的班次区间（naive 墙钟）。

    跨午夜（start>end）时 end 属次日；抖动按开始日取键（收班边界不随午夜换键）。
    抖动后开班不许早于当日 00:00（负溢出交给前一日班次语义，砍掉防重叠歧义）；
    收班早于开班的退化配置夹成 1 分钟最小班次。
    """
    day_iso = day.isoformat()
    start_total = entry["start_min"] + _jitter(entry["key"], day_iso, "start", amp)
    end_total = entry["end_min"] + _jitter(entry["key"], day_iso, "end", amp)
    if entry["end_min"] <= entry["start_min"]:
        end_total += _DAY_MIN  # 跨午夜班
    start_total = max(0, start_total)
    if end_total <= start_total:
        end_total = start_total + 1
    base = datetime(day.year, day.month, day.day)
    return (base + timedelta(minutes=start_total),
            base + timedelta(minutes=end_total))


def _containing_interval(
    entry: Mapping, local: datetime, amp: int,
) -> Optional[Tuple[datetime, datetime]]:
    """当前墙钟时刻所在的班次区间（无则 None）。

    只需检查「今天开始的班」与「昨天开始的跨午夜班尾巴」两个候选——单班次
    时长 < 24h + 抖动，开始日再早的班次不可能覆盖到现在。
    """
    for day in (local.date(), (local - timedelta(days=1)).date()):
        if day.isoweekday() not in entry["workdays"]:
            continue
        s, e = _shift_interval(entry, day, amp)
        if s <= local < e:
            return (s, e)
    return None


def _merged_intervals(
    entry: Mapping, local: datetime, amp: int,
    days_back: int = 2, days_fwd: int = 9,
) -> List[List[datetime]]:
    """近窗班次区间（已排序合并重叠），供「下一次边界」推导。"""
    raw: List[Tuple[datetime, datetime]] = []
    d0 = local.date()
    for off in range(-days_back, days_fwd + 1):
        day = d0 + timedelta(days=off)
        if day.isoweekday() in entry["workdays"]:
            raw.append(_shift_interval(entry, day, amp))
    raw.sort()
    merged: List[List[datetime]] = []
    for s, e in raw:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


# ── 对外判定 ────────────────────────────────────────────────


def in_work_hours(
    ws: Any, platform: str, account_id: str, now_ts: Optional[float] = None,
) -> bool:
    """账号此刻是否「在班」。总闸关 / 账号豁免 / 任何异常 → True（fail-open）。"""
    try:
        ws = ws if isinstance(ws, Mapping) else {}
        if not ws.get("enabled"):
            return True
        entry = resolve_entry(ws, platform, account_id)
        if not entry["gated"]:
            return True
        ts = float(now_ts if now_ts is not None else time.time())
        local = _local_wall(ts, _resolve_tz(entry["tz_name"]))
        return _containing_interval(entry, local, _edge_jitter_min(ws)) is not None
    except Exception:
        logger.debug("[work-schedule] 在班判定异常（fail-open 放行）", exc_info=True)
        return True


def should_hold_auto_reply(
    ws: Any, platform: str, account_id: str,
    peer_text: str = "", now_ts: Optional[float] = None,
) -> str:
    """自动回复链的统一闸门判定：返回 ""=放行，"off_hours"=扣留。

    三条自动出口（A 线直发 / B 线 autosend / 协议号 run_autoreply）都调这一个
    函数，保证口径一致。危机豁免在**这里**做而不是各出口自判——豁免语义漂移
    比闸门本身更危险。人工路径（手动发送 / 人工通过投递）不要调本函数。
    """
    try:
        ws = ws if isinstance(ws, Mapping) else {}
        if not ws.get("enabled"):
            return ""
        if in_work_hours(ws, platform, account_id, now_ts=now_ts):
            return ""
        if bool(ws.get("crisis_bypass", True)) and str(peer_text or "").strip():
            try:
                from src.utils.wellbeing_guard import detect_crisis
                level = str(
                    (detect_crisis(str(peer_text)) or {}).get("level") or "none")
                if level in ("severe", "elevated"):
                    return ""
            except Exception:
                # 危机判定自身故障 → 宁可放行（安全优先于班表）
                logger.debug(
                    "[work-schedule] 危机判定异常（按危机豁免放行）", exc_info=True)
                return ""
        return HOLD_REASON_OFF_HOURS
    except Exception:
        logger.debug("[work-schedule] 闸门判定异常（fail-open 放行）", exc_info=True)
        return ""


def schedule_state(
    ws: Any, platform: str, account_id: str, now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """班表快照（生效值解释卡 / autosend-status / watchdog 共用口径）。

    关键字段：``in_hours``（此刻在班否）、``next_change_ts`` + ``next_change_kind``
    （open=下次上班 / close=下次下班）、``shift_started_ts``（当前班次开始时刻，
    watchdog 复班宽限与补觉判定用）。全程 fail-open：异常时 ``in_hours=True``。
    """
    out: Dict[str, Any] = {
        "enabled": False, "gated": False, "in_hours": True,
        "source": "default", "window": "", "workdays": [],
        "timezone": "", "edge_jitter_min": 0, "now_local": "",
        "next_change_ts": 0.0, "next_change_kind": "",
        "shift_started_ts": 0.0, "crisis_bypass": True,
    }
    try:
        ws = ws if isinstance(ws, Mapping) else {}
        out["enabled"] = bool(ws.get("enabled"))
        out["crisis_bypass"] = bool(ws.get("crisis_bypass", True))
        entry = resolve_entry(ws, platform, account_id)
        out["source"] = entry["source"]
        out["gated"] = bool(out["enabled"] and entry["gated"])
        out["workdays"] = sorted(entry["workdays"])
        out["timezone"] = entry["tz_name"] or "local"
        out["edge_jitter_min"] = _edge_jitter_min(ws)
        ts = float(now_ts if now_ts is not None else time.time())
        tz = _resolve_tz(entry["tz_name"])
        local = _local_wall(ts, tz)
        out["now_local"] = local.strftime("%H:%M")
        if not out["gated"]:
            return out
        out["window"] = "%02d:%02d-%02d:%02d" % (
            entry["start_min"] // 60, entry["start_min"] % 60,
            entry["end_min"] // 60, entry["end_min"] % 60)
        amp = out["edge_jitter_min"]
        inside = _containing_interval(entry, local, amp)
        out["in_hours"] = inside is not None
        if inside is not None:
            out["shift_started_ts"] = _wall_to_ts(inside[0], tz)
            out["next_change_ts"] = _wall_to_ts(inside[1], tz)
            out["next_change_kind"] = "close"
        else:
            merged = _merged_intervals(entry, local, amp)
            nxt = next((s for s, _e in merged if s > local), None)
            if nxt is not None:
                out["next_change_ts"] = _wall_to_ts(nxt, tz)
                out["next_change_kind"] = "open"
        return out
    except Exception:
        logger.debug("[work-schedule] 班表快照异常（fail-open）", exc_info=True)
        return out


__all__ = [
    "HOLD_REASON_OFF_HOURS",
    "work_schedule_cfg", "resolve_entry", "off_hours_cfg",
    "in_work_hours", "should_hold_auto_reply", "schedule_state",
]
