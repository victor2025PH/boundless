# -*- coding: utf-8 -*-
"""Q4N7 + SWZL dump inbox XML, grep "Meta AI" rows."""
from __future__ import annotations
import sys, re

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DEVICES = [
    ("Q4N7AM7HMZGU4LZD", "Q4N7"),
    ("SWZLPNYTROMZMJLR", "SWZL"),
]


def dump_one(serial: str, label: str):
    import uiautomator2 as u2
    print(f"\n{'='*60}\n  {label} ({serial})\n{'='*60}")
    try:
        d = u2.connect(serial)
        print(f"  current pkg: {d.app_current()}")
        xml = d.dump_hierarchy()
        out = f"D:/workspace/mobile-auto0423/inbox_{label.lower()}.xml"
        with open(out, "w", encoding="utf-8") as f:
            f.write(xml)
        print(f"  saved -> {out} ({len(xml)} chars)")

        # grep "Meta AI" content-desc 行 + bounds + class
        # 用 multi-attr 抓整 node tag
        pat = re.compile(
            r'<node[^>]*content-desc="([^"]*Meta AI[^"]*)"[^>]*?bounds="(\[[^\]]+\]\[[^\]]+\])"',
            re.IGNORECASE | re.DOTALL)
        hits = pat.findall(xml)
        # 也试反过来 bounds 在前
        pat2 = re.compile(
            r'<node[^>]*bounds="(\[[^\]]+\]\[[^\]]+\])"[^>]*?content-desc="([^"]*Meta AI[^"]*)"',
            re.IGNORECASE | re.DOTALL)
        hits2 = [(b, c) for (b, c) in pat2.findall(xml)]
        # 合并去重
        all_hits = []
        seen = set()
        for c, b in hits:
            key = (c, b)
            if key not in seen:
                all_hits.append((c, b))
                seen.add(key)
        for b, c in hits2:
            key = (c, b)
            if key not in seen:
                all_hits.append((c, b))
                seen.add(key)
        print(f"\n  >>> 含 'Meta AI' 的节点 ({len(all_hits)} 个, 含 bounds):")
        for i, (c, b) in enumerate(all_hits, 1):
            print(f"    [{i}] desc=\"{c[:80]}\" bounds={b}")
        if not all_hits:
            print("    (没有任何 content-desc 含 'Meta AI')")

        # 也找 text="Meta AI" 节点
        pat_t1 = re.compile(
            r'<node[^>]*text="([^"]*Meta AI[^"]*)"[^>]*?bounds="(\[[^\]]+\]\[[^\]]+\])"',
            re.IGNORECASE | re.DOTALL)
        pat_t2 = re.compile(
            r'<node[^>]*bounds="(\[[^\]]+\]\[[^\]]+\])"[^>]*?text="([^"]*Meta AI[^"]*)"',
            re.IGNORECASE | re.DOTALL)
        thits = []
        seen2 = set()
        for t, b in pat_t1.findall(xml):
            if (t, b) not in seen2:
                thits.append((t, b))
                seen2.add((t, b))
        for b, t in pat_t2.findall(xml):
            if (t, b) not in seen2:
                thits.append((t, b))
                seen2.add((t, b))
        print(f"\n  >>> 含 'Meta AI' 的 text 节点 ({len(thits)} 个):")
        for i, (t, b) in enumerate(thits, 1):
            # 算宽高
            m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', b)
            w = int(m.group(3)) - int(m.group(1)) if m else "?"
            h = int(m.group(4)) - int(m.group(2)) if m else "?"
            print(f"    [{i}] text=\"{t[:60]}\" bounds={b} ({w}x{h})")

        # 也 grep 当前页是不是真 inbox
        if "Chats" in xml or "聊天" in xml or "inbox" in xml.lower():
            print("\n  ✓ 看起来在 inbox")
        else:
            print("\n  ✗ 不在 inbox — 可能 splash/restore prompt/login 页")
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")


def main():
    for serial, label in DEVICES:
        dump_one(serial, label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
