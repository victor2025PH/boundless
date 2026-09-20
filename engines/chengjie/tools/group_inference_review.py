# -*- coding: utf-8 -*-
"""群判定复核 CLI（只读）——「边车把私聊看成群了吗」用数字回答。

背景（2026-08-20 群/频道 P1）：群/私聊之分在 WA(`@g.us`) / LINE(群 id) /
TG(负数 chat_id) / Zalo(边车群注册表) 是**地址自描述的硬事实**，但 messenger 与
instagram 是网页边车，只能靠 DOM 启发式猜（同线程 ≥2 发言人 / 线程行叠 ≥2 张
头像，见 services/instagram-web/ig_threads.js）。启发式必然有误判，且两个方向
后果不对称：

- **私聊被看成群**（假阳）：档位降 review、群护栏上身，最坏路径是
  ``skip_group_chats`` 让这个真客户**一条草稿都没有**——静默丢客且看板无痕。
  正因如此 ``group_draft_skip`` 默认不对弱证据群走静默出口（见
  src/inbox/automation_mode.py），但「不静默」只是止血，误判本身仍要能被看见。
- **群被看成私聊**（假阴）：只是回到旧行为，AI 可能在群里自动说话——这是
  ``resolve_automation_mode`` 群闸想拦的那件事，漏判即绕过。

本工具用**独立于判定链的第二信号**交叉复核：入站消息的 ``sender_name``。
真群的入站发言人会有多个（或至少带名字）；1:1 私聊没有发言人名。三个桶：

- **假阳嫌疑**＝标了群、有发言人名、但只有一个人说过话（私聊被看成群）；
- **假阴嫌疑**＝标了私聊却有 ≥2 个发言人名（群被看成私聊，群闸被绕过）；
- **无发言人**＝标了群但一个发言人名都没落库——这是**落库链**的缺陷而非判定
  错误（本工具对这些会话判不了，群气泡也渲染不出发言人）。首版把它混进假阳
  清单，结果 2026-08-20 首跑 telegram 12 条全是缺口，真信号被淹没。

**嫌疑不是判决**（真群可能只有一个活跃成员），所以输出的是清单 + 样本量，
给人去点开会话看，而不是自动改数据。

只读（sqlite ro URI，绝不建库/绝不写回），多实例机按 scripts/_data_root 契约
逐根审读。

用法::

    python tools/group_inference_review.py                    # 全部活跃实例，近 30 天
    python tools/group_inference_review.py --days 7
    python tools/group_inference_review.py --data-root D:/chengjie-instances/zhiliao/data
    python tools/group_inference_review.py --json             # 机器可读
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402

#: 群判据靠网页 DOM 启发式的平台（与 src/inbox/automation_mode.py 同一名单；
#: 那边是运行时行为、这边是复核口径，任一处收紧要同步另一处）。
WEAK_PLATFORMS = ("messenger", "instagram")
#: 少于这么多条入站就不下嫌疑判词——两条消息的群本来就可能只有一个人说话。
DEFAULT_MIN_INBOUND = 3
#: 清单每平台至多列这么多条（给人去点，不是给人读日志）。
LIST_CAP = 12

_GROUP_TYPES = ("group", "supergroup", "channel", "megagroup")


def _is_group_type(chat_type: str) -> bool:
    return str(chat_type or "").strip().lower() in _GROUP_TYPES


def summarize_group_inference(
    rows: Sequence[Dict[str, Any]],
    min_inbound: int = DEFAULT_MIN_INBOUND,
) -> Dict[str, Any]:
    """按平台聚合群/私聊计数 + 两类嫌疑清单。纯函数（门禁钉住）。

    每行需要：``platform`` / ``conversation_id`` / ``chat_type`` /
    ``display_name`` / ``inbound_n`` / ``distinct_senders`` / ``named_senders``。
    """
    plats: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        plat = str(r.get("platform") or "").strip().lower()
        if not plat:
            continue
        p = plats.setdefault(plat, {
            "platform": plat,
            "weak_evidence": plat in WEAK_PLATFORMS,
            "group_convs": 0, "private_convs": 0,
            "suspect_false_group": [], "suspect_missed_group": [],
            "gap_no_sender": [],
            "graded": 0,
        })
        inbound = int(r.get("inbound_n") or 0)
        distinct = int(r.get("distinct_senders") or 0)
        named = int(r.get("named_senders") or 0)
        item = {
            "conversation_id": str(r.get("conversation_id") or ""),
            "display_name": str(r.get("display_name") or ""),
            "chat_type": str(r.get("chat_type") or ""),
            "inbound_n": inbound, "distinct_senders": distinct,
            "named_senders": named,
        }
        if _is_group_type(item["chat_type"]):
            p["group_convs"] += 1
            if inbound >= min_inbound:
                p["graded"] += 1
                if named == 0:
                    # 第二信号整个缺席＝**判不了**，不是「判错了」。混进假阳清单会
                    # 把一条落库缺口伪装成一堆判定事故，真信号（single_speaker）
                    # 被淹没——2026-08-20 首跑实测 telegram 12 条全是这种。
                    item["why"] = "no_sender_name"
                    p["gap_no_sender"].append(item)
                elif distinct <= 1:
                    item["why"] = "single_speaker"
                    p["suspect_false_group"].append(item)
        else:
            p["private_convs"] += 1
            if inbound >= min_inbound:
                p["graded"] += 1
                if distinct >= 2:
                    item["why"] = "multi_speaker"
                    p["suspect_missed_group"].append(item)

    for p in plats.values():
        for key in ("suspect_false_group", "suspect_missed_group", "gap_no_sender"):
            p[key].sort(key=lambda x: (-x["inbound_n"], x["conversation_id"]))
            p[key + "_n"] = len(p[key])
            p[key] = p[key][:LIST_CAP]
        p["verdicts"] = _verdicts(p, min_inbound)
    return {"platforms": [plats[k] for k in sorted(plats)], "min_inbound": min_inbound}


def _verdicts(p: Dict[str, Any], min_inbound: int) -> List[str]:
    out: List[str] = []
    fp, fn = p["suspect_false_group_n"], p["suspect_missed_group_n"]
    gap = p["gap_no_sender_n"]
    if p["graded"] == 0:
        out.append("样本不足（无 ≥%d 条入站的会话）——判词留白，不是没问题" % min_inbound)
        return out
    if gap:
        # 缺口本身是**另一条链**的缺陷（发言人没落库）：群气泡渲染不出发言人、
        # 本工具对这些会话也判不了。对弱证据平台额外危险（判不了＝假阳藏在里面）。
        out.append("⚠ %d 个群会话**一个发言人名都没落库** = 复核盲区（群气泡也渲染"
                   "不出发言人）。修落库链（TG 走 protocol_bridge 的 sender_name 透传，"
                   "边车走 ingest payload）%s"
                   % (gap, "；弱证据平台的假阳可能就藏在这批里" if p["weak_evidence"] else ""))
    if fp:
        if p["weak_evidence"]:
            out.append("⚠ %d 个群会话有发言人名但**只有一个人说过话** = 假阳嫌疑"
                       "（私聊被看成群）。逐个点开确认；确认误判就说明该平台的 DOM "
                       "判据要收紧" % fp)
        else:
            out.append("ℹ %d 个群会话只有一个发言人——本平台群地址自描述，"
                       "大概率是「真群但只有一个人说话」，不是判定问题" % fp)
    elif p["weak_evidence"] and not gap:
        out.append("✅ 无「标了群却只有一个发言人」的会话（启发式暂未见假阳）")
    if fn:
        out.append("⚠ %d 个私聊会话出现 ≥2 个发言人名 = 假阴嫌疑（群被看成私聊 → "
                   "群闸被绕过，AI 可能在群里自动说话）" % fn)
    else:
        out.append("✅ 无「标了私聊却多人发言」的会话（暂未见假阴）")
    return out


def collect_conversations(
    db_path: Path, days: int
) -> Optional[List[Dict[str, Any]]]:
    """读 inbox.db 近 N 天有入站的会话 + 发言人统计；库缺/坏 → None（绝不建库）。"""
    p = Path(db_path)
    if not p.is_file():
        return None
    cutoff = time.time() - max(1, days) * 86400.0
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
            if "sender_name" not in cols:
                # 老库没这列 → 第二信号不存在，如实回空而不是硬报「全是嫌疑」
                return []
            sql = (
                "SELECT c.platform AS platform, c.conversation_id AS conversation_id, "
                "  c.chat_type AS chat_type, c.display_name AS display_name, "
                "  COUNT(*) AS inbound_n, "
                "  COUNT(DISTINCT NULLIF(m.sender_name, '')) AS distinct_senders, "
                "  SUM(CASE WHEN m.sender_name != '' THEN 1 ELSE 0 END) AS named_senders "
                "FROM messages m JOIN conversations c "
                "  ON c.conversation_id = m.conversation_id "
                "WHERE m.direction = 'in' AND m.ts >= ? "
                "GROUP BY c.conversation_id"
            )
            return [dict(r) for r in con.execute(sql, (cutoff,)).fetchall()]
        finally:
            con.close()
    except Exception:
        return None


def _render(root: Path, summary: Optional[Dict[str, Any]]) -> None:
    print(f"\n=== {root} ===")
    if summary is None:
        print("  （无 inbox.db 或读取失败，跳过）")
        return
    if not summary["platforms"]:
        print("  窗口内无入站会话")
        return
    print(f"  {'平台':<12}{'群':>6}{'私聊':>8}{'假阳嫌疑':>10}{'假阴嫌疑':>10}"
          f"{'无发言人':>10}  判据")
    for p in summary["platforms"]:
        print(f"  {p['platform']:<12}{p['group_convs']:>6}{p['private_convs']:>8}"
              f"{p['suspect_false_group_n']:>10}{p['suspect_missed_group_n']:>10}"
              f"{p['gap_no_sender_n']:>10}"
              f"  {'DOM 启发式' if p['weak_evidence'] else '地址自描述'}")
    for p in summary["platforms"]:
        lines = [v for v in p["verdicts"] if v[:1] in ("⚠", "ℹ")]
        if not lines and not p["weak_evidence"]:
            continue
        print(f"\n  [{p['platform']}]")
        for v in p["verdicts"]:
            print("    " + v)
        for key, label in (("suspect_false_group", "假阳嫌疑"),
                           ("suspect_missed_group", "假阴嫌疑"),
                           ("gap_no_sender", "无发言人")):
            for it in p[key]:
                print(f"      {label} {it['conversation_id']} "
                      f"「{it['display_name']}」 in={it['inbound_n']} "
                      f"发言人={it['distinct_senders']} why={it['why']}")


def _db_candidates(root: Path) -> List[Path]:
    """inbox.db 在数据根下的可能位置（实测生产落 config/ 下；容错根下）。"""
    return [root / "config" / "inbox.db", root / "inbox.db"]


def _force_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，判词里的 ✅/⚠ 会让整个工具崩在 print 上。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(description="群判定复核（只读，交叉验 sender_name）")
    ap.add_argument("--data-root", default="", help="指定实例数据根（默认自动发现全部活跃实例）")
    ap.add_argument("--db", default="", help="直接指定 inbox.db 路径（跳过数据根解析）")
    ap.add_argument("--days", type=int, default=30, help="回看天数（默认 30）")
    ap.add_argument("--min-inbound", type=int, default=DEFAULT_MIN_INBOUND,
                    help=f"下嫌疑判词的最小入站条数（默认 {DEFAULT_MIN_INBOUND}）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    args = ap.parse_args(argv)

    if args.db:
        dbs = [Path(args.db)]
    else:
        dbs = []
        for root in resolve_data_roots(args.data_root):
            dbs.append(next((c for c in _db_candidates(Path(root)) if c.is_file()),
                            _db_candidates(Path(root))[0]))
    out: Dict[str, Any] = {}
    for db in dbs:
        rows = collect_conversations(db, args.days)
        summary = (None if rows is None
                   else summarize_group_inference(rows, args.min_inbound))
        out[str(db)] = summary
        if not args.json:
            _render(db, summary)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
