"""托管代理库存批量导入 CLI——供应商导出 txt → 实例代理池（一键代理 P3）。

Web 端已有同能力（接入弹窗 → 代理配置 → 批量导入），本 CLI 服务两个场景：
① 运营直接在服务器上拿到大文件（几百条分批）；② 无坐席会话时的初始灌库。

默认 **dry-run 只解析不落库**（与 persona_media_backfill 同纪律），``--apply``
才写。写入走 ``ProxyPool``（WAL + busy_timeout，与在线服务并发安全）；数据根
按 ``scripts/_data_root`` 契约解析——**别**在引擎根裸跑然后写错库（CWD 陷阱，
见 CLAUDE.md「CWD 相对路径」节）。

用法::

    python tools/proxy_stock_import.py list.txt --country JP --kind isp [--apply]
    python tools/proxy_stock_import.py list.txt --data-root D:\\chengjie-instances\\zhiliao\\data --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402
from src.integrations.proxy_pool import (  # noqa: E402
    PROXY_KINDS, ProxyPool, parse_import_lines,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", help="供应商导出的清单文件（每行一条）")
    ap.add_argument("--country", default="", help="本批地区 ISO 码（如 JP，强烈建议）")
    ap.add_argument("--kind", default="isp", choices=list(PROXY_KINDS))
    ap.add_argument("--scheme", default="socks5")
    ap.add_argument("--label", default="", help="批次备注")
    ap.add_argument("--data-root", default="", help="实例数据根（缺省自动发现第一个）")
    ap.add_argument("--apply", action="store_true", help="真写库（缺省只解析预览）")
    args = ap.parse_args()

    text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    entries, bad = parse_import_lines(text, default_scheme=args.scheme)
    print(f"解析：{len(entries)} 条可入库，{len(bad)} 条坏行")
    for b in bad[:10]:
        print(f"  坏行 #{b['line']}: {b['text'][:60]!r} ({b['reason']})")
    if not entries:
        return 1

    roots = resolve_data_roots(args.data_root)
    if not roots:
        print("找不到实例数据根（用 --data-root 指定）")
        return 1
    root = Path(roots[0])
    db = root / "config" / "proxy_pool.db"
    print(f"目标库：{db}")
    if not args.apply:
        print("dry-run：未写库。确认无误后加 --apply。")
        return 0

    pool = ProxyPool(db)
    existing = {(p.get("host"), int(p.get("port") or 0), p.get("username") or "")
                for p in pool.list()}
    added = dup = 0
    for e in entries:
        if (e["host"], e["port"], e["username"]) in existing:
            dup += 1
            continue
        pool.add(scheme=e["scheme"], host=e["host"], port=e["port"],
                 username=e["username"], password=e["password"],
                 label=args.label, kind=args.kind,
                 country=args.country.strip().upper())
        added += 1
    print(f"入库 {added} 条，重复跳过 {dup} 条。")
    print("提示：一键卡的地区存量 ~30s 内随下次 status 拉取刷新；建议在弹窗里对首条做一次连通性测试。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
