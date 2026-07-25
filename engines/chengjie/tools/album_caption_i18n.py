# -*- coding: utf-8 -*-
"""相册配文多语化：中文配文 → LLM 批量翻译（en/yue）→ caption_i18n 入库。

背景：策展配文是简体中文人设口吻；英语客户收图时配文语言不匹配穿帮。
media_caption(row, lang) 已按 caption_i18n[lang] 取——缺的只是数据，本工具补齐。

用法：
    python tools/album_caption_i18n.py --db <config>/persona_media.db \
        --persona lin_xiaoyu [--langs en,yue] [--api-base ... --api-key ... --model ...]
幂等：已有目标语种翻译的条目跳过（重跑安全）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.companion.persona_media_store import PersonaMediaStore  # noqa: E402

_LANG_STYLE = {
    "en": "自然口语英文（像年轻女生发 ins 配文，可带 1 个 emoji）",
    "yue": "地道口语粤语书面（香港人 WhatsApp 风格，用嘅/啦/呀等语气词）",
}


def _llm(api_base: str, api_key: str, model: str, prompt: str,
         timeout: float = 90.0) -> str:
    # max_tokens 要给足：deepseek 推理模型的思考也计入 completion 预算
    # （实测思考吃 200-500），批量 JSON 翻译输出本身又长——2000 会被截断。
    body = {"model": model, "max_tokens": 6000, "temperature": 0.4,
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        api_base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return str(data["choices"][0]["message"]["content"] or "")


def translate_batch(api_base: str, api_key: str, model: str,
                    captions: dict, lang: str) -> dict:
    """{id: 中文配文} → {id: 译文}。一次 LLM 调用批量翻译（JSON 进出）。"""
    style = _LANG_STYLE.get(lang, lang)
    src = json.dumps(captions, ensure_ascii=False, indent=0)
    prompt = (
        f"把下面 JSON 里的每条中文照片配文翻译成{style}。\n"
        "保持原意与俏皮语气，每条不超过 25 个词/字。\n"
        "只输出 JSON（key 不变，value 换成译文），不要 markdown 代码块。\n\n"
        f"{src}"
    )
    out = _llm(api_base, api_key, model, prompt)
    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        raise ValueError(f"LLM 未返回 JSON: {out[:120]}")
    parsed = json.loads(m.group(0))
    return {str(k): str(v).strip() for k, v in parsed.items() if str(v).strip()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--persona", required=True)
    ap.add_argument("--langs", default="en,yue")
    ap.add_argument("--api-base", default="https://api.deepseek.com/v1")
    # 密钥绝不硬编码（repo_doctor 门禁扫描）：CLI 传参或环境变量 DEEPSEEK_API_KEY
    ap.add_argument("--api-key", default=os.environ.get("DEEPSEEK_API_KEY", ""))
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--batch", type=int, default=15)
    args = ap.parse_args()
    if not args.api_key:
        ap.error("--api-key 缺失（或设环境变量 DEEPSEEK_API_KEY）")

    store = PersonaMediaStore(args.db)
    rows = store.list(args.persona, enabled_only=False)
    langs = [x.strip() for x in args.langs.split(",") if x.strip()]

    for lang in langs:
        todo = {}
        for r in rows:
            cap = str(r.get("caption") or "").strip()
            i18n = r.get("caption_i18n") or {}
            if cap and not str(i18n.get(lang) or "").strip():
                todo[str(r["id"])] = cap
        if not todo:
            print(f"[caption_i18n] {lang}: 全部已有翻译，跳过")
            continue
        print(f"[caption_i18n] {lang}: 待翻译 {len(todo)} 条")
        items = list(todo.items())
        done = 0
        for i in range(0, len(items), args.batch):
            chunk = dict(items[i:i + args.batch])
            try:
                trs = translate_batch(
                    args.api_base, args.api_key, args.model, chunk, lang)
            except Exception as e:  # noqa: BLE001
                print(f"[caption_i18n] {lang} 批次失败: {e}")
                continue
            for mid, tr in trs.items():
                row = next((r for r in rows if str(r["id"]) == mid), None)
                if row is None:
                    continue
                i18n = dict(row.get("caption_i18n") or {})
                i18n[lang] = tr[:80]
                store.update(mid, caption_i18n=i18n)
                row["caption_i18n"] = i18n
                done += 1
        print(f"[caption_i18n] {lang}: 完成 {done}/{len(todo)}")

    # 抽样展示
    sample = [r for r in store.list(args.persona) if r.get("caption")][:3]
    for r in sample:
        print("  -", r.get("caption"), "|", (r.get("caption_i18n") or {}).get("en"),
              "|", (r.get("caption_i18n") or {}).get("yue"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
