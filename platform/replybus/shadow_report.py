# -*- coding: utf-8 -*-
r"""platform/replybus/shadow_report.py — 影子模式对比日志汇总报告（纯 stdlib）。

上游写入端：智控王 `backend/private_message_handler.py` 的 `_shadow_log_task`
（第四阶段新增，S3.5 灰度前置）。AI_BACKEND 仍是 'local'（真实回复不变）时，
额外背景问一次 chengjie『它会怎么回』，把"本地 vs chengjie"逐条写成 JSONL，
供业务方在真正切换 AI_BACKEND=chengjie 前，用真实流量数据判断草稿质量。

本工具是这份日志的**读取/汇总/展示端**，与写入端约定的固定格式（不要单方面改字段名）：

  目录：环境变量 ``TG_REPLYBUS_SHADOW_DIR``，缺省 ``<智控王仓库根>/backend/data/replybus_shadow/``。
  文件：按天分文件 ``shadow-YYYYMMDD.jsonl``（UTC 日期），JSON Lines，一行一条记录。
  字段：``ts / user_id / phone / msg_id / local_reply_preview / local_reply_len /
        chengjie_available / chengjie_action / chengjie_reply_preview /
        chengjie_reply_len / chengjie_persona / chengjie_reason``。

用法：
    python platform/replybus/shadow_report.py                       # 最近 7 天，文本报告
    python platform/replybus/shadow_report.py --days 3 --sample 10
    python platform/replybus/shadow_report.py --format json
    python platform/replybus/shadow_report.py --selftest             # 自造临时数据自测
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

FILE_RE_PREFIX = "shadow-"
FILE_RE_SUFFIX = ".jsonl"

# 与智控王侧 config.py::FeatureConfig 的默认值规则一致的路径推导（不 import 智控王代码，
# 只是字符串拼接一个"合理默认"；真实部署以环境变量为准，这里只是自解释的兜底）。
_DEFAULT_TGKZ_ROOT = r"D:\aixyc2026\tgkz2026"


def _default_shadow_dir() -> str:
    env = os.environ.get("TG_REPLYBUS_SHADOW_DIR", "").strip()
    if env:
        return env
    return str(Path(_DEFAULT_TGKZ_ROOT) / "backend" / "data" / "replybus_shadow")


def _parse_file_date(path: Path) -> Optional[str]:
    name = path.name
    if name.startswith(FILE_RE_PREFIX) and name.endswith(FILE_RE_SUFFIX):
        day = name[len(FILE_RE_PREFIX):-len(FILE_RE_SUFFIX)]
        if len(day) == 8 and day.isdigit():
            return day
    return None


def discover_files(shadow_dir: str, days: int) -> List[Path]:
    d = Path(shadow_dir)
    if not d.is_dir():
        return []
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=days - 1)
    out = []
    for f in sorted(d.glob(f"{FILE_RE_PREFIX}*{FILE_RE_SUFFIX}")):
        day_str = _parse_file_date(f)
        if not day_str:
            continue
        try:
            day = datetime.strptime(day_str, "%Y%m%d").date()
        except ValueError:
            continue
        if cutoff <= day <= today:
            out.append(f)
    return out


def _parse_ts(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_records(files: List[Path]) -> Dict[str, Any]:
    """逐行解析，容忍脏行。返回 {"records": [...], "malformed": n, "files_read": n}。"""
    records: List[Dict[str, Any]] = []
    malformed = 0
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        malformed += 1
                        continue
                    if not isinstance(obj, dict) or "user_id" not in obj:
                        malformed += 1
                        continue
                    records.append(obj)
        except OSError:
            continue
    return {"records": records, "malformed": malformed, "files_read": len(files)}


def _is_true(v: Any) -> bool:
    return v is True or v == "true" or v == 1


def summarize(records: List[Dict[str, Any]], sample_n: int) -> Dict[str, Any]:
    total = len(records)
    if total == 0:
        return {"total": 0}

    collected = [r for r in records if _is_true(r.get("chengjie_available"))]
    not_collected = total - len(collected)
    collection_rate = (len(collected) / total) if total else 0.0

    action_dist = Counter(str(r.get("chengjie_action", "unknown")) for r in collected)

    # 分歧类型 A：本地有回复但 chengjie 判定 silent
    divergence_a = [
        r for r in collected
        if int(r.get("local_reply_len") or 0) > 0 and str(r.get("chengjie_action")) == "silent"
    ]
    # 分歧类型 B：本地没回复但 chengjie 判定该有 draft
    divergence_b = [
        r for r in collected
        if int(r.get("local_reply_len") or 0) == 0 and str(r.get("chengjie_action")) == "draft"
    ]

    def _avg_len(rs: List[Dict[str, Any]], key: str) -> float:
        vals = [int(r.get(key) or 0) for r in rs]
        return (sum(vals) / len(vals)) if vals else 0.0

    local_avg_len_all = _avg_len(records, "local_reply_len")
    chengjie_avg_len_collected = _avg_len(collected, "chengjie_reply_len")

    # 时间范围：优先用记录里的 ts，解析不到就不给范围
    ts_values = [t for t in (_parse_ts(r.get("ts")) for r in records) if t is not None]
    time_range = None
    if ts_values:
        time_range = {"from": min(ts_values).isoformat(), "to": max(ts_values).isoformat()}

    def _sample(rs: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
        # 最近 n 条：按 ts 倒序（ts 解析不到的排最后，不崩）
        def _key(r):
            t = _parse_ts(r.get("ts"))
            return t or datetime.min.replace(tzinfo=timezone.utc)
        return sorted(rs, key=_key, reverse=True)[:n]

    return {
        "total": total,
        "collected": len(collected),
        "not_collected": not_collected,
        "collection_rate": collection_rate,
        "action_distribution": dict(action_dist),
        "divergence_a_local_reply_chengjie_silent": len(divergence_a),
        "divergence_b_local_silent_chengjie_draft": len(divergence_b),
        "local_avg_reply_len_all": local_avg_len_all,
        "chengjie_avg_reply_len_collected": chengjie_avg_len_collected,
        "time_range": time_range,
        "sample_divergence_a": _sample(divergence_a, sample_n),
        "sample_divergence_b": _sample(divergence_b, sample_n),
    }


def render_text(summary: Dict[str, Any], malformed: int, files_read: int) -> str:
    lines: List[str] = []
    lines.append("== platform/replybus 影子模式对比报告 ==")
    lines.append(f"读取文件数: {files_read}  脏行(解析失败): {malformed}")
    if summary.get("total", 0) == 0:
        lines.append("暂无数据（影子模式尚未产生任何对比记录，或目录不存在/为空）。")
        return "\n".join(lines)

    tr = summary.get("time_range")
    if tr:
        lines.append(f"时间范围: {tr['from']} ~ {tr['to']}")
    lines.append(f"总对比条数: {summary['total']}")
    lines.append(
        f"成功采集到 chengjie 决策: {summary['collected']} 条"
        f"（采集率 {summary['collection_rate']*100:.1f}%，"
        f"未采集 {summary['not_collected']} 条——多为 chengjie 未启动/网络不可达，"
        f"不代表 chengjie 判断质量差）"
    )
    lines.append("")
    lines.append("-- 成功采集记录中的 chengjie 决策分布 --")
    dist = summary.get("action_distribution", {})
    total_collected = summary.get("collected", 0) or 1
    for action, count in sorted(dist.items(), key=lambda x: -x[1]):
        lines.append(f"  {action:10s} {count:6d} 条  ({count/total_collected*100:.1f}%)")
    lines.append("")
    lines.append("-- 业务最该关注的两类分歧 --")
    lines.append(f"  A. 本地有回复，但 chengjie 判定 silent（不回）: {summary['divergence_a_local_reply_chengjie_silent']} 条")
    lines.append(f"  B. 本地没回复，但 chengjie 判定该有 draft（会回）: {summary['divergence_b_local_silent_chengjie_draft']} 条")
    lines.append("")
    lines.append("-- 回复长度对比（粗略反映风格差异，过短/过长）--")
    lines.append(f"  本地 AI 平均回复长度（全部记录）: {summary['local_avg_reply_len_all']:.1f} 字")
    lines.append(f"  chengjie 平均回复长度（仅成功采集记录）: {summary['chengjie_avg_reply_len_collected']:.1f} 字")

    def _render_samples(title: str, samples: List[Dict[str, Any]]) -> None:
        lines.append("")
        lines.append(f"-- {title}（最近 {len(samples)} 条抽样）--")
        if not samples:
            lines.append("  （无）")
            return
        for i, r in enumerate(samples, 1):
            lines.append(f"  [{i}] ts={r.get('ts','?')} user_id={r.get('user_id','?')}")
            lines.append(f"      本地: {r.get('local_reply_preview','') or '(空)'}")
            lines.append(f"      chengjie({r.get('chengjie_action','?')}): {r.get('chengjie_reply_preview','') or '(空)'}")

    _render_samples("分歧A抽样：本地回复了，chengjie说不回", summary.get("sample_divergence_a", []))
    _render_samples("分歧B抽样：本地没回复，chengjie想回", summary.get("sample_divergence_b", []))
    return "\n".join(lines)


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default=None, help="影子日志目录（缺省用环境变量/默认路径）")
    ap.add_argument("--days", type=int, default=7, help="回看天数（默认 7，必须 >=1）")
    ap.add_argument("--sample", type=int, default=5, help="每类分歧抽样条数（默认 5，>=0）")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--selftest", action="store_true", help="自造临时数据自测，不读真实目录")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    if args.selftest:
        return _selftest()

    if args.days < 1:
        print("--days 必须 >= 1", file=sys.stderr)
        return 2
    if args.sample < 0:
        print("--sample 必须 >= 0", file=sys.stderr)
        return 2

    shadow_dir = args.dir or _default_shadow_dir()
    files = discover_files(shadow_dir, args.days)
    loaded = load_records(files)
    summary = summarize(loaded["records"], args.sample)

    if args.format == "json":
        print(json.dumps({
            "shadow_dir": shadow_dir, "files_read": loaded["files_read"],
            "malformed_lines": loaded["malformed"], "summary": summary,
        }, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"目录: {shadow_dir}")
        print(render_text(summary, loaded["malformed"], loaded["files_read"]))
    return 0


def _make_record(ts: str, user_id: str, local_reply: str, available: bool,
                 action: str, chengjie_reply: str = "", persona: str = "") -> Dict[str, Any]:
    return {
        "ts": ts, "user_id": user_id, "phone": "+886900000000", "msg_id": "m1",
        "local_reply_preview": local_reply, "local_reply_len": len(local_reply),
        "chengjie_available": available, "chengjie_action": action,
        "chengjie_reply_preview": chengjie_reply, "chengjie_reply_len": len(chengjie_reply),
        "chengjie_persona": persona, "chengjie_reason": "",
    }


def _selftest() -> int:
    import tempfile

    failures: List[str] = []

    def check(desc: str, ok: bool) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            failures.append(desc)

    print("== shadow_report.py 自测 ==")

    print("[1/3] 空目录/不存在目录 -> 不崩，输出'暂无数据'")
    with tempfile.TemporaryDirectory(prefix="shadow_report_empty_") as d:
        files = discover_files(d, 7)
        check("空目录发现 0 个文件", len(files) == 0)
        loaded = load_records(files)
        summary = summarize(loaded["records"], 5)
        check("汇总 total=0", summary.get("total") == 0)
        text = render_text(summary, loaded["malformed"], loaded["files_read"])
        check("文本报告含'暂无数据'", "暂无数据" in text)
    not_exist = os.path.join(tempfile.gettempdir(), "shadow_report_does_not_exist_xyz")
    check("不存在目录发现 0 个文件（不抛异常）", discover_files(not_exist, 7) == [])

    print("[2/3] 完整数据集：混合正常/分歧/未采集/脏行")
    with tempfile.TemporaryDirectory(prefix="shadow_report_full_") as d:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        path = os.path.join(d, f"shadow-{today}.jsonl")
        now = datetime.now(timezone.utc)
        with open(path, "w", encoding="utf-8") as f:
            # 正常采集，一致（本地有回复，chengjie也是draft）
            f.write(json.dumps(_make_record(
                (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "u1", "你好，有什么可以帮你", True, "draft", "您好，请问需要什么服务")) + "\n")
            # 分歧A：本地有回复，chengjie判silent
            f.write(json.dumps(_make_record(
                (now - timedelta(minutes=4)).isoformat().replace("+00:00", "Z"),
                "u2", "这是本地生成的回复内容", True, "silent")) + "\n")
            # 分歧B：本地没回复，chengjie判draft
            f.write(json.dumps(_make_record(
                (now - timedelta(minutes=3)).isoformat().replace("+00:00", "Z"),
                "u3", "", True, "draft", "看起来对方在询问价格，建议回复报价单")) + "\n")
            # 未采集到（chengjie不可用）
            f.write(json.dumps(_make_record(
                (now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
                "u4", "本地照常回复", False, "fallback")) + "\n")
            # 脏行：非法 JSON
            f.write("这不是合法JSON\n")
            # 脏行：合法JSON但缺 user_id
            f.write(json.dumps({"foo": "bar"}) + "\n")

        files = discover_files(d, 7)
        check("发现 1 个文件", len(files) == 1)
        loaded = load_records(files)
        check("有效记录 4 条", len(loaded["records"]) == 4)
        check("脏行计 2 条", loaded["malformed"] == 2)

        summary = summarize(loaded["records"], 5)
        check("total=4", summary["total"] == 4)
        check("collected=3（4条中1条未采集）", summary["collected"] == 3)
        check("collection_rate=0.75", abs(summary["collection_rate"] - 0.75) < 1e-9)
        check("action分布 draft=2 silent=1", summary["action_distribution"] == {"draft": 2, "silent": 1})
        check("分歧A=1（u2）", summary["divergence_a_local_reply_chengjie_silent"] == 1)
        check("分歧B=1（u3）", summary["divergence_b_local_silent_chengjie_draft"] == 1)
        check("时间范围非空", summary["time_range"] is not None)

        text = render_text(summary, loaded["malformed"], loaded["files_read"])
        check("文本报告含采集率", "采集率" in text)
        check("文本报告含分歧A样本(u2)", "u2" in text)
        check("文本报告含分歧B样本(u3)", "u3" in text)

        js = json.dumps({"summary": summary}, ensure_ascii=False, default=str)
        check("JSON 序列化不崩", isinstance(js, str) and len(js) > 0)

    print("[3/3] --days 边界过滤：3 天前的文件应被排除")
    with tempfile.TemporaryDirectory(prefix="shadow_report_daterange_") as d:
        old_day = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y%m%d")
        recent_day = datetime.now(timezone.utc).strftime("%Y%m%d")
        for day in (old_day, recent_day):
            with open(os.path.join(d, f"shadow-{day}.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps(_make_record(
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "ux", "x", True, "silent")) + "\n")
        files_7d = discover_files(d, 7)
        check("--days 7 只发现最近的 1 个文件（排除 10 天前）", len(files_7d) == 1)
        files_30d = discover_files(d, 30)
        check("--days 30 发现全部 2 个文件", len(files_30d) == 2)

    if failures:
        print(f"\n== 结果：{len(failures)} 项失败 ==")
        return 1
    print("\n== 结果：全部通过 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
