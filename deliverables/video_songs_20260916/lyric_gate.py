#!/usr/bin/env python3
"""营销歌词词闸（方案 §三 红线 + §五 产线；纯本地，零网络）。

  python lyric_gate.py                # 跑 registry.json 全部条目
  python lyric_gate.py lyrics/V1_zh.txt [--private]

闸：
  ① 格律：中文行 6~9 单位（CJK 字=1、拉丁词=1）为 PASS，5/10~11 WARN，<5 或 >11 FAIL
  ② 防编造/收益承诺禁词；③ iGaming 词表；④ 公域恋爱向禁词（客户关系养成）
  ⑤ 数字串白名单；⑥ 须有 [chorus]；公域副歌宜含智聊/ChatX
退出码：任一 FAIL → 1。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

BANNED_CLAIMS = [
    "月入", "日入", "躺赚", "稳赚", "暴富", "翻倍", "保证成交", "三天出单", "出单", "一定赚",
    "必赚", "零风险", "无风险", "百分百", "100%", "秒杀同行", "碾压",
]
BANNED_IGAMING = [
    "稳赢", "必中", "包赔", "内幕", "返水", "代充", "洗码", "赔率", "下注", "投注", "押注",
    "赢钱", "翻本", "上分", "首存优惠", "存就送",
]
BANNED_ROMANCE = [
    "恋爱养成", "谈恋爱", "女友", "男友", "暧昧", "陪聊赚钱", "恋爱脑",
]
REAL_NAMES = ["七里香", "周杰伦", "邓紫棋", "Taylor", "月亮代表我的心"]
ALLOW_TOKENS = {"bd2026", "chatx", "kyc", "vip", "line", "whatsapp", "app", "key", "ai", "hp"}

CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
DIGITS = re.compile(r"\d+")
TAG = re.compile(r"^\[(verse|chorus|outro|bridge|intro)\]$", re.I)


def units_zh(line: str) -> int:
    return len(CJK.findall(line)) + len(LATIN_WORD.findall(line))


def gate_file(path: Path, *, lang: str, audience: str, kind: str = "song") -> tuple[list[str], list[str]]:
    fails: list[str] = []
    warns: list[str] = []
    text = path.read_text(encoding="utf-8")
    lines = [l.rstrip() for l in text.splitlines()]
    body = [l for l in lines if l.strip() and not TAG.match(l.strip())]
    if not any(TAG.match(l.strip()) and "chorus" in l.lower() for l in lines):
        fails.append("缺 [chorus] 段")
    joined = "\n".join(body)
    for w in BANNED_CLAIMS:
        if w in joined:
            fails.append(f"收益/编造禁词「{w}」")
    for w in BANNED_IGAMING:
        if w in joined:
            fails.append(f"iGaming 禁词「{w}」")
    if audience == "public":
        for w in BANNED_ROMANCE:
            if w in joined:
                fails.append(f"公域恋爱向禁词「{w}」（请改客户关系养成话术）")
    for w in REAL_NAMES:
        if w.lower() in joined.lower():
            fails.append(f"真实歌名/艺人「{w}」")
    for l in body:
        for tok in DIGITS.findall(l):
            ok = any(tok in t and t in l.lower() for t in ALLOW_TOKENS)
            if not ok:
                fails.append(f"裸数字「{tok}」：{l}")
    zh_like = lang.startswith("zh")
    for l in body:
        if zh_like:
            n = units_zh(l)
            if n < 5 or n > 11:
                fails.append(f"格律 {n} 单位（FAIL 带 <5/>11）：{l}")
            elif n < 6 or n > 9:
                warns.append(f"格律 {n} 单位（建议 6~9）：{l}")
        else:
            n = len(l.split())
            if n < 3 or n > 9:
                fails.append(f"词数 {n}（建议 3~9）：{l}")
    if audience == "public":
        chorus_txt, in_ch = [], False
        for l in lines:
            s = l.strip()
            if TAG.match(s):
                in_ch = "chorus" in s.lower()
                continue
            if in_ch and s:
                chorus_txt.append(s)
        ch = "\n".join(chorus_txt)
        if not ("智聊" in ch or "chatx" in ch.lower()):
            warns.append("公域条目副歌未含品牌词（智聊/ChatX）")
    return fails, warns


def main(argv: list[str]) -> int:
    targets: list[tuple] = []
    if len(argv) > 1 and not argv[1].startswith("--"):
        aud = "private" if "--private" in argv else "public"
        p = Path(argv[1])
        lang = "id" if "_id" in p.stem else ("en" if "_en" in p.stem else "zh")
        targets.append((p if p.is_absolute() else ROOT / p, lang, aud))
    else:
        reg = json.loads((ROOT / "registry.json").read_text(encoding="utf-8"))
        for it in reg["items"]:
            if it.get("lyrics"):
                targets.append((ROOT / it["lyrics"], it["lang"], it["audience"], it.get("kind", "song")))
    bad = 0
    for tgt in targets:
        path, lang, aud = tgt[0], tgt[1], tgt[2]
        kind = tgt[3] if len(tgt) > 3 else "song"
        if not path.exists():
            print(f"[FAIL] {path.name}: 文件不存在")
            bad += 1
            continue
        fails, warns = gate_file(path, lang=lang, audience=aud, kind=kind)
        tag = "FAIL" if fails else "PASS"
        print(f"[{tag}] {path.name} ({lang}, {aud})")
        for f in fails:
            print(f"    x {f}")
        for w in warns:
            print(f"    ~ {w}")
        bad += 1 if fails else 0
    print(f"\n{len(targets)} 件，FAIL {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
