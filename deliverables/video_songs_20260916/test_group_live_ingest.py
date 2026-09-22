#!/usr/bin/env python3
"""群舞台可见性测试：三条互不相同的询价 → 先等实时入站 → 失败再 from_latest。

  python test_group_live_ingest.py

退出码 0 = 三条都进了卖家工作台且文案互不相同。
不清 Telegram 云端（群禁远端删）；只在报告里列出工作台最新几条，方便对照「旧 USB-C 重复」vs「新样本」。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from persona_stage import (  # noqa: E402
    customer_send, pull_group_latest, stage, thread_messages, wait_inbound,
)

SAMPLES = [
    ("mention-id", "Pak @htqp456, harga charger GaN 20W berapa? 500 pcs ke Surabaya."),
    ("plain-en", "Need nylon webbing 25mm, 3km to Bandung next month. Who can quote?"),
    ("mention-en", "@htqp456 sample LED strip 5m, 10 rolls to Bali first, can you?"),
]


def _uniq(msgs: list[dict]) -> int:
    texts = [(m.get("text") or "").strip() for m in msgs if (m.get("text") or "").strip()]
    return len(set(texts))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    st = stage("TG_GROUP")
    before = thread_messages(st, 20)
    print(f"== 补拉前工作台 {len(before)} 条，互异 {_uniq(before)}")
    for m in before[-8:]:
        print(f"  {(m.get('direction') or '?'):3} | {(m.get('text') or '')[:90]}")

    results: list[tuple[str, str, str]] = []
    # 每轮带时间戳后缀：群云端不能远端清，重复跑不该在群里堆一模一样的三句
    run_stamp = time.strftime("%H%M")
    for tag, text in SAMPLES:
        text = f"{text} (#{run_stamp})"
        t0 = time.time()
        d = customer_send(st, text, tag=f"sample-{tag}")
        print(f"\n-- 发出 [{tag}] ok={d.get('ok')} delivered={d.get('delivered')}")
        print(f"   {text}")
        live = wait_inbound(st, text, 8.0, after_ts=t0)
        via = "live"
        if not live:
            pull = pull_group_latest(st)
            print(f"   8s 未实时入站 → pull {pull}")
            live = wait_inbound(st, text, 12.0, after_ts=t0)
            via = f"pull:{pull.get('via')}"
        print(f"   landed={live} via={via}")
        results.append((tag, "OK" if live else "MISS", via))

    after = thread_messages(st, 20)
    print(f"\n== 补拉后工作台 {len(after)} 条，互异 {_uniq(after)}")
    for m in after[-10:]:
        print(f"  {(m.get('direction') or '?'):3} | {(m.get('text') or '')[:90]}")

    misses = [r for r in results if r[1] != "OK"]
    distinct_ok = _uniq(after) >= 3
    print("\n== 样本")
    for tag, stt, via in results:
        print(f"  {tag}: {stt} ({via})")
    print("RESULT", "FAIL" if misses or not distinct_ok else "PASS",
          f"miss={len(misses)} distinct={_uniq(after)}")
    return 1 if misses or not distinct_ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
