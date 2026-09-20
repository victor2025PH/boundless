"""B 线草稿「新入站过期」守卫（fresh guard，2026-08-03）。

事故形态（A 线 interject_absorb 的 B 线等价缺口）：拟稿的 10-20 秒里客户又补了话，
AutosendWorker 仍把按旧输入写的草稿投出去——答非所问；且新消息随后又催生新草稿，
两条回复接连砸过去。

与既有机制的分工（探明于 2026-08-03，勿重复建设）：
  - ``DraftService.auto_generate_draft`` 已有**创建侧**吸收：新入站文本与活跃草稿
    peer_text 不同 → 旧稿 ``cancelled/stale_peer`` + 重拟新稿。本守卫只补它够不到的
    **竞态窗**——新入站已落库、但其 debounce 拟稿回调（inbound_merge 默认 8s 静默窗）
    还没走到 stale_peer 那一步，worker 恰在此窗内把旧稿投了出去。
  - 相同文本的新入站走幂等跳过（**不会**重拟）——此时旧稿是客户唯一会收到的回复，
    取消它 = 客户永远没有回复，故 ``find_superseding_inbound`` 要求文本不同才判过期。
  - 低于 ``inbox.auto_draft.min_text_len`` 的新入站不会触发拟稿回调，同理不拦。

配置 ``inbox.l2_autosend.fresh_guard``（默认关 = 零行为变更）::

    fresh_guard:
      enabled: false     # true 才启用
      grace_sec: 3       # 入站晚于草稿创建超过此秒数才算「拟稿没看见的插话」

纯函数，无 I/O；坏输入一律「不拦」（宁可发旧稿，不可断链）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

DEFAULT_GRACE_SEC = 3.0


def superseded_by_inbound(
    draft_ts: float,
    latest_inbound_ts: float,
    *,
    grace_sec: float = DEFAULT_GRACE_SEC,
) -> bool:
    """会话最新入站是否晚于草稿创建超过 grace（= 草稿按旧输入写成，已过期）。

    坏输入（非数值 / 非正时间戳）一律返回 False——守卫的失败模式必须是「放行」。
    grace 非法回落默认值、负数按 0（任何更晚入站都算过期）。
    """
    try:
        d = float(draft_ts)
        latest = float(latest_inbound_ts)
    except (TypeError, ValueError):
        return False
    if d <= 0 or latest <= 0:
        return False
    try:
        g = float(grace_sec)
    except (TypeError, ValueError):
        g = DEFAULT_GRACE_SEC
    if g < 0:
        g = 0.0
    return latest > d + g


def parse_fresh_guard_cfg(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读 ``inbox.l2_autosend.fresh_guard``，返回归一化配置（全有默认值，永不抛）。

    兼容两种入参形态（风格照 ``reply_split.parse_bubbles_cfg``）：
      - 完整配置树（bootstrap 注入路径）：钻 ``inbox.l2_autosend.fresh_guard``，
        并顺带镜像 ``inbox.auto_draft.min_text_len``（判「新入站会不会触发新拟稿」用，
        worker 自己拿不到全局树——与 dup_guard_cfg 同一注入范式）；
      - ``l2_autosend`` 子块（worker 构造入参自解析回落）：直接取 ``fresh_guard`` 键，
        min_text_len 无从得知按 0（= 任何非空插话都算会触发拟稿，与出货默认一致）。
    """
    cfg = config if isinstance(config, dict) else {}
    inbox = cfg.get("inbox")
    ad: Dict[str, Any] = {}
    if isinstance(inbox, dict):
        l2 = inbox.get("l2_autosend")
        blk = l2.get("fresh_guard") if isinstance(l2, dict) else None
        if isinstance(inbox.get("auto_draft"), dict):
            ad = inbox["auto_draft"]
    else:
        blk = cfg.get("fresh_guard")
    if not isinstance(blk, dict):
        blk = {}

    try:
        grace = float(blk.get("grace_sec", DEFAULT_GRACE_SEC))
    except (TypeError, ValueError):
        grace = DEFAULT_GRACE_SEC
    try:
        min_len = int(ad.get("min_text_len", 0))
    except (TypeError, ValueError):
        min_len = 0
    return {
        "enabled": bool(blk.get("enabled", False)),
        "grace_sec": max(0.0, grace),
        "min_text_len": max(0, min_len),
    }


def find_superseding_inbound(
    rows: Optional[List[Dict[str, Any]]],
    *,
    draft_ts: float,
    peer_text: str,
    grace_sec: float = DEFAULT_GRACE_SEC,
    min_text_len: int = 0,
) -> Optional[Dict[str, Any]]:
    """从会话消息行里找「使草稿过期」的更晚入站，返回最新命中行或 None。

    判过期须同时满足（每一条都对应「取消旧稿后一定有新回复兜着」的现场事实）：
      - ``direction == 'in'`` 且 ``ts`` 晚于草稿创建超过 grace（superseded_by_inbound）；
      - 文本非空且与草稿 ``peer_text`` 不同——相同文本 = auto_generate_draft 幂等跳过
        **不会**产生新草稿，且旧稿本来就在回这句话，取消它只会造成静默；
      - 长度 ≥ ``min_text_len``——不够长的入站不会触发新一轮拟稿（auto_draft 的
        min_text_len 过滤），同样不能取消客户唯一的回复。

    ``rows`` 期望 ``list_recent_messages`` 的 ts 升序输出，但内部不依赖顺序
    （从尾扫并取 ts 最大命中）。坏输入 / 异常形态一律 None（不拦）。
    """
    if not isinstance(rows, list) or not rows:
        return None
    peer = str(peer_text or "").strip()
    best: Optional[Dict[str, Any]] = None
    best_ts = 0.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("direction") or "") != "in":
            continue
        try:
            ts = float(r.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if not superseded_by_inbound(draft_ts, ts, grace_sec=grace_sec):
            continue
        text = str(r.get("text") or "").strip()
        if not text:
            continue  # 纯媒体/空行：不确定会触发新拟稿，保守放行旧稿
        if text == peer:
            continue  # 同文本幂等跳过不会重拟——取消旧稿=客户没有任何回复
        if min_text_len > 0 and len(text) < min_text_len:
            continue  # 不会过 auto_draft 的 min_text_len 闸，不会有新稿
        if ts >= best_ts:
            best, best_ts = r, ts
    return best


def interrupted_by_inbound(
    rows: Optional[List[Dict[str, Any]]],
    *,
    started_ts: float,
    grace_sec: float = 0.5,
) -> Optional[Dict[str, Any]]:
    """从会话消息行里找「投递开始后新到的入站」，返回最新命中行或 None。

    分条发送的**条间中止**判据（B 线版 interject——A 线由 telegram_client 的
    interject_absorb 承担同语义）。与 ``find_superseding_inbound`` 的三条筛选
    **刻意不同**：那边拦的是「整条回复还没发出」，取消须保证有新稿兜底，故
    同文本/过短/纯媒体都放行；这里首条 bubble **已经发出**＝客户已有回复，
    不存在「取消后客户零回复」的断链风险——真人被打断就是停手，不管对方
    发来的是文字、表情还是语音，所以**任何**新入站都算打断。

    ``grace_sec`` 只是时钟抖动垫（默认 0.5s，同进程 ``time.time()`` 采样间的
    次序误差），不是竞态宽限——别把它调大，4-8s 的条间隔经不起 3s 的免检窗。
    坏输入一律 None（不停；守卫的失败模式必须是「照发」）。
    """
    if not isinstance(rows, list) or not rows:
        return None
    try:
        started = float(started_ts)
    except (TypeError, ValueError):
        return None
    if started <= 0:
        return None
    try:
        g = float(grace_sec)
    except (TypeError, ValueError):
        g = 0.5
    if g < 0:
        g = 0.0
    best: Optional[Dict[str, Any]] = None
    best_ts = 0.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("direction") or "") != "in":
            continue
        try:
            ts = float(r.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts <= started + g:
            continue
        if ts >= best_ts:
            best, best_ts = r, ts
    return best


__all__ = [
    "DEFAULT_GRACE_SEC",
    "superseded_by_inbound",
    "parse_fresh_guard_cfg",
    "find_superseding_inbound",
    "interrupted_by_inbound",
]
