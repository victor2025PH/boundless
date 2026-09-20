# -*- coding: utf-8 -*-
"""告警链路自检 CLI（2026-08-01）——「告警最后一公里」是否真的接通。

回答三个运营/运维一直看不到的问题：

1. 现在有几个**启用**的外发通道？（0 = 告警只进日志和铃铛，出事没人知道）
2. 那些最该外发的别名（草稿积压 / 人工投递断链 / 主机告警 …）**逐个**有没有人订阅？
   （配了通道 ≠ 收得到——只订阅 report 的通道对 draft_backlog 毫无覆盖）
3. 配置文件放对根了吗？（服务进程 CWD 是实例数据根；引擎根那份服务读不到 = 诱饵）

**零凭据、只读**：不发消息、不改任何文件。复用 ``scripts/_data_root`` 的数据根契约逐根
解析，所以它看到的就是**服务进程实际会读的那份配置**（不会因 CWD 相对路径病而误报「已配置」）。

用法::

    python tools/alert_link_selfcheck.py            # 自动发现活跃实例，逐根自检
    python tools/alert_link_selfcheck.py --data-root D:\\chengjie-instances\\zhiliao\\data
    python tools/alert_link_selfcheck.py --json      # 机读输出（供巡检/CI 消费）

退出码：全部数据根都「已接通且关注别名零遗漏」→ 0；否则 → 1（可挂外部巡检）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from scripts._data_root import ENGINE_ROOT as DR_ENGINE_ROOT  # noqa: E402
from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402
from src.integrations.alert_link_audit import (  # noqa: E402
    HIGH_VALUE_ALIASES,
    WEBHOOKS_REL,
    audit_alert_link,
    default_focus_aliases,
    detect_orphan_config,
)
from src.integrations.notify_webhooks_store import sanitize_list  # noqa: E402


def _effective_webhooks_for_root(root: Path) -> List[Dict[str, Any]]:
    """该数据根下**服务进程实际生效**的 webhook 列表：overlay 优先，否则 config.yaml。"""
    ov = root / WEBHOOKS_REL
    if ov.is_file():
        try:
            raw = json.loads(ov.read_text(encoding="utf-8"))
            return sanitize_list(raw if isinstance(raw, list) else raw.get("webhooks"))
        except Exception:
            return []
    cfg = load_merged_config(root)
    base = ((cfg or {}).get("notify") or {}).get("webhooks") or []
    return sanitize_list(base)


def _audit_root(root: Path) -> Dict[str, Any]:
    hooks = _effective_webhooks_for_root(root)
    audit = audit_alert_link(hooks)
    orphan = detect_orphan_config(DR_ENGINE_ROOT, root)
    audit["data_root"] = str(root)
    audit["orphan"] = orphan
    return audit


_HIGH_VALUE_LABELS = {
    "draft_backlog": "草稿积压",
    "human_deliver": "人工投递断链",
    "host_alert": "主机告警",
    "assistant_report": "用户报障（小智面板）",
    "bug_intake": "用户报障（报障群）",
}


def _help_connect() -> str:
    """接通指引。别名清单**从 HIGH_VALUE_ALIASES 派生**，不再硬编码「三项」
    ——2026-08-27 补两个报障别名时，这段文案就是靠硬编码悄悄过期的典型。"""
    items = " · ".join(HIGH_VALUE_ALIASES)
    names = " / ".join(
        _HIGH_VALUE_LABELS.get(a, a) for a in HIGH_VALUE_ALIASES)
    return (
        "接通步骤（无需重启，运营角色）：\n"
        "  1) 后台「告警渠道」面板 → 新增渠道：format=telegram、token=<Bot token>、\n"
        "     target=<chat_id>（群/频道/个人均可）。\n"
        f"  2) 订阅先勾高价值 {len(HIGH_VALUE_ALIASES)} 项：{names}\n"
        f"     （别名 {items}），再逐步放开。\n"
        "  3) 面板「发送测试」确认送达 → 保存即热更生效（写入数据根，避开引擎根诱饵）。\n"
        "  API：GET/POST /api/accounts/auto-reply/webhooks（+ /test）。"
    )


def _render(audit: Dict[str, Any]) -> str:
    lines: List[str] = []
    root = audit["data_root"]
    lines.append(f"── 数据根: {root}")
    ch = audit["channels_enabled"]
    total = audit["channels_total"]
    dis = audit["channels_disabled"]
    if ch == 0:
        lines.append(f"  [告警出口] 启用通道 0 个"
                     + (f"（另有 {dis} 个已禁用）" if dis else "")
                     + " —— 所有告警只进日志/铃铛，无外发。")
    else:
        fmts = "、".join(f"{k}×{v}" for k, v in sorted(audit["formats"].items()))
        lines.append(f"  [告警出口] 启用通道 {ch} 个（{fmts}）"
                     + (f"，另 {dis} 个已禁用" if dis else ""))

    cov = audit["covered_count"]
    fc = audit["focus_count"]
    uncovered = audit["uncovered"]
    lines.append(f"  [别名覆盖] 关注别名 {cov}/{fc} 已有通道订阅"
                 + ("。" if not uncovered else f"；未覆盖：{'、'.join(uncovered)}"))
    if ch > 0 and not uncovered:
        # 只有真配了通道时才逐条展示归属，避免 0 通道时刷屏
        for a, names in audit["per_alias"].items():
            lines.append(f"      · {a} ← {'、'.join(names)}")

    orphan = audit.get("orphan")
    if orphan:
        lines.append(f"  [诱饵文件] 引擎根存在 {orphan['engine_file']}"
                     f"（{orphan['engine_bytes']} 字节），但服务读的数据根没有 "
                     f"{orphan['data_file']} —— 编辑引擎根那份对服务无效，"
                     "请走面板保存或写到数据根。")

    if audit["healthy"]:
        lines.append("  [结论] 已接通，关注别名零遗漏。")
    else:
        lines.append("  [结论] 未接通 / 有遗漏。")
        lines.append("  " + _help_connect().replace("\n", "\n  "))
    return "\n".join(lines)


def main(argv: List[str] | None = None) -> int:
    # 输出含中文；Windows 控制台默认 GBK 会把 UTF-8 字节显示成乱码。强制 stdout
    # 为 UTF-8（重定向到文件/CI 稳定可读）；交互式 GBK 控制台仍需 `chcp 65001`。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="告警链路自检（只读、零凭据）")
    ap.add_argument("--data-root", default="", help="显式指定实例数据根（默认自动发现）")
    ap.add_argument("--json", action="store_true", help="机读 JSON 输出")
    args = ap.parse_args(argv)

    roots = resolve_data_roots(args.data_root)
    audits = [_audit_root(r) for r in roots]

    if args.json:
        print(json.dumps({"roots": audits, "focus": default_focus_aliases()},
                         ensure_ascii=False, indent=2))
    else:
        print("告警链路自检（只读；判据=启用通道 + 高价值别名逐个被订阅）\n")
        for a in audits:
            print(_render(a))
            print("")

    all_ok = all(a["healthy"] for a in audits)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
