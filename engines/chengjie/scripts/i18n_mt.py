# -*- coding: utf-8 -*-
"""UI 语言包批量机翻管线（i18n MT P1，2026-08-27）。

背景：扩展 UI 语言（i18n_packs.EXTRA_LANGS，vi/th/id…）机制是「en 底 + override」，
但 override 全靠人手写——vi 覆盖 3.4%、th/id 各 0.8%，剩余 96%+ 对扩展语坐席
显示英文。本管线把「缺键 → 机翻草稿 → 占位符/质量校验 → 写入词包」流水线化，
新语言从「几十人天」降到「1 天机翻 + 数人天母语校对热路径」。

三步用法（每步幂等，可分批重跑）::

    # ① 盘点：各扩展语缺什么（--tier hot 只看密封工作台页在用的键）
    python -m scripts.i18n_mt inventory [--tier hot]

    # ② 机翻：OpenAI 兼容端点批量翻缺键 → 草稿 JSON（不碰词包；失败键记录原因）
    python -m scripts.i18n_mt translate --lang vi --tier hot \
        --api-base https://api.deepseek.com/v1 --model deepseek-v4-flash \
        [--api-key ... | 环境变量 I18N_MT_API_KEY / DEEPSEEK_API_KEY] [--limit 200]
    # （外部产出的译文也可走 --engine file --from-json path 只做校验/归一）

    # ③ 写包：草稿合入 i18n_packs/<lang>_auto.py（再次校验 + 排除人工包已覆盖键）
    python -m scripts.i18n_mt write --lang vi --draft tmp/i18n_mt/vi_draft_*.json

自检（零网络，CI 门禁 tests/test_i18n_mt_pipeline.py 调它）::

    python -m scripts.i18n_mt selftest

设计红线：
- **草稿不直写词包**：translate 只产 JSON；write 单独跑且重复校验——engine 输出
  永远当不可信输入对待。
- **<lang>_auto.py 是工具专属文件**：母语复核后的键请「转正」挪进人工词包
  （vi_<域>.py 等），write 会自动排除人工包已覆盖键、regen 不会复活；直接改
  auto 文件的编辑会在下次 write --mode replace 时丢失。
- 校验口径与既有门禁同源：占位符与 ZH 逐键一致（test_i18n_extra_langs 同款）、
  目标语零 CJK（en 密封门禁同哲学）、HTML 标签守恒、防 LLM 跑飞长度比。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_PACKS_DIR = _ROOT / "src" / "web" / "i18n_packs"
_DRAFT_DIR = _ROOT / "tmp" / "i18n_mt"

_PH_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff]")
_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*[^>]*>|&[a-z]+;")

# 语言显示名（prompt 用）；translation_service.LANG_NAMES 有则优先。
_FALLBACK_LANG_NAMES = {
    "vi": "Vietnamese", "th": "Thai", "id": "Indonesian", "ja": "Japanese",
    "ko": "Korean", "ru": "Russian", "es": "Spanish", "pt": "Portuguese",
    "ms": "Malay", "ar": "Arabic", "fr": "French", "de": "German",
    "tr": "Turkish", "hi": "Hindi", "zh_hant": "Traditional Chinese",
}

# 确定性转换语种（非翻译）：走各自专用生成器，本管线拒绝——校验器的 CJK 泄漏
# 检查对它们是反的（译文本该是 CJK）。
_CONVERSION_LANGS = {"zh_hant": "scripts/i18n_hant.py"}


def _lang_name(code: str) -> str:
    try:
        from src.ai.translation_service import LANG_NAMES
        return LANG_NAMES.get(code, _FALLBACK_LANG_NAMES.get(code, code))
    except Exception:
        return _FALLBACK_LANG_NAMES.get(code, code)


# ── 词表读取 ──────────────────────────────────────────────────────────────────

def merged_views():
    """(zh, en, extras) —— 与运行时同机制（web_i18n 合并视图 + collect_all extras）。"""
    from src.web.i18n_packs import collect_all
    from src.web.web_i18n import get_translations
    zh = get_translations("zh")
    en = get_translations("en")
    _pzh, _pen, extras = collect_all()
    return zh, en, extras


def hot_keys() -> set:
    """热路径键集 = 密封工作台页实际用到的键（i18n_scan 同源，含外壳/收件箱/看板）。"""
    from scripts.i18n_scan import scan_workspace_i18n
    return set(scan_workspace_i18n()["used_keys"])


def missing_keys(lang: str, tier: str = "all") -> dict:
    """{key: {"zh":…, "en":…}} —— 该语言尚无 override 的键（tier=hot 时仅热路径）。"""
    zh, en, extras = merged_views()
    have = set(extras.get(lang, {}))
    keys = set(zh) - have
    if tier == "hot":
        keys &= hot_keys()
    return {k: {"zh": zh[k], "en": en.get(k, "")} for k in sorted(keys)}


# ── 校验（translate 与 write 双侧同一口径）───────────────────────────────────

def validate_entry(key: str, zh: str, tr: str, en: str = "") -> str:
    """合格返回 ''，否则返回拒绝原因（草稿里留痕，人工可查）。"""
    t = str(tr or "").strip()
    if not t:
        return "empty"
    if t == str(zh).strip():
        return "untranslated_zh_passthrough"
    if _CJK_RE.search(t):
        return "cjk_leak"
    if set(_PH_RE.findall(t)) != set(_PH_RE.findall(zh or "")):
        return "placeholder_mismatch"
    if t.count("{") != t.count("}"):
        return "brace_imbalance"
    if sorted(_TAG_RE.findall(t)) != sorted(_TAG_RE.findall(zh or "")):
        return "html_tag_mismatch"
    limit = max(len(zh or ""), len(en or "")) * 4 + 24
    if len(t) > limit:
        return "too_long"
    return ""


# ── 机翻引擎 ──────────────────────────────────────────────────────────────────

def _llm(api_base: str, api_key: str, model: str, prompt: str,
         timeout: float = 120.0) -> str:
    body = {"model": model, "max_tokens": 8000, "temperature": 0.2,
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        api_base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return str(data["choices"][0]["message"]["content"] or "")


def _build_prompt(lang: str, chunk: dict, glossary: dict | None = None) -> str:
    name = _lang_name(lang)
    gl = ""
    if glossary:
        pairs = "；".join(f"{k}→{v}" for k, v in glossary.items())
        gl = f"术语表（必须遵守）：{pairs}。\n"
    src = json.dumps(chunk, ensure_ascii=False)
    return (
        f"你是软件本地化译员。把下面 JSON 每个键的界面词条翻译成 {name}。\n"
        "语境：SaaS 坐席工作台（客服/销售聊天软件）的界面文案——按钮、标签、提示。\n"
        "规则：\n"
        "1. 每个键给出 zh（原文）与 en（参考译）；以 zh 语义为准，en 用来消歧短词。\n"
        "2. {大括号占位符} 原样保留：名字不译、不增不减。\n"
        "3. HTML 标签与 &实体; 原样保留。\n"
        "4. 措辞简短克制（界面空间有限），不加解释、不加引号、不加句号（原文有则保留）。\n"
        "5. 产品名 ChatX、平台名（Telegram/WhatsApp/LINE/Messenger）不译。\n"
        f"{gl}"
        "只输出 JSON：{\"键\": \"译文\"}，键不变，不要 markdown 代码块。\n\n"
        f"{src}"
    )


def translate_batch_llm(api_base: str, api_key: str, model: str, lang: str,
                        chunk: dict, glossary: dict | None = None) -> dict:
    out = _llm(api_base, api_key, model, _build_prompt(lang, chunk, glossary))
    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        raise ValueError(f"LLM 未返回 JSON: {out[:160]}")
    parsed = json.loads(m.group(0))
    return {str(k): str(v) for k, v in parsed.items()}


# ── ollama_mt（HY-MT 专用翻译模型，本地 LAN 节点）──────────────────────────────
# 与 src/ai/translation_engines.OllamaMTEngine 同约定：原生 /api/chat（尊重请求级
# keep_alive）+「把下面的文本翻译成{语种中文名}，不要额外解释。」单条提示词。
# MT 模型不吃格式指令 → {占位符}/HTML 标签走**掩码**硬保护（⟦i⟧ 哨兵，译后还原）。
_LANG_ZH_NAMES = {
    "vi": "越南语", "th": "泰语", "id": "印度尼西亚语", "ja": "日语", "ko": "韩语",
    "ru": "俄语", "es": "西班牙语", "pt": "葡萄牙语", "ms": "马来语", "ar": "阿拉伯语",
    "fr": "法语", "de": "德语", "tr": "土耳其语", "hi": "印地语", "en": "英语",
}
_MASK_RE = re.compile(
    r"\{[a-zA-Z_][a-zA-Z0-9_]*\}|</?[a-zA-Z][a-zA-Z0-9]*[^>]*>|&[a-z]+;")


def _mask(text: str):
    toks: list = []

    def rep(m):
        toks.append(m.group(0))
        return f"\u27e6{len(toks) - 1}\u27e7"
    return _MASK_RE.sub(rep, text), toks


def _unmask(text: str, toks: list) -> str:
    for i, t in enumerate(toks):
        text = text.replace(f"\u27e6{i}\u27e7", t)
    return text


def translate_one_ollama_mt(api_base: str, model: str, lang: str, text: str,
                            timeout: float = 30.0, keep_alive: str = "24h") -> str:
    masked, toks = _mask(text)
    name = _LANG_ZH_NAMES.get(lang, _lang_name(lang))
    prompt = f"把下面的文本翻译成{name}，不要额外解释。\n\n{masked}"
    body = {"model": model, "stream": False, "keep_alive": keep_alive,
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        api_base.rstrip("/") + "/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    out = str((data.get("message") or {}).get("content") or "").strip()
    return _unmask(out, toks)


# ── 草稿产出（translate）─────────────────────────────────────────────────────

def run_translate(lang: str, tier: str, engine: str, *, api_base: str = "",
                  api_key: str = "", model: str = "", from_json: str = "",
                  limit: int = 0, batch: int = 40, glossary_path: str = "",
                  out_dir: Path | None = None, todo: dict | None = None,
                  llm_fn=None, sleep_s: float = 0.05, resume: str = "",
                  flush_every: int = 200, max_error_streak: int = 20) -> Path:
    """产出草稿 JSON（items=通过校验的译文；rejected=拒绝原因）。返回草稿路径。

    崩溃韧性（2026-08-27 实测跑批进程静默死亡、整段成果丢失后加）：
    - **增量落盘**：每 ``flush_every`` 键覆写一次草稿文件（原子换名），进程死了
      最多丢一个窗口的量；
    - **断点续跑**：``--resume <草稿>`` 载入已完成键，只翻剩余（rejected 键也
      跳过——它们是校验拒绝不是没翻，二次补翻请另跑不带 resume 的窗口）；
    - **连败熔断**：引擎连续失败 ``max_error_streak`` 次即中止（现场已落盘），
      防止对着挂掉的节点空烧几小时。
    """
    if lang in _CONVERSION_LANGS:
        raise SystemExit(
            f"{lang} 是确定性转换语种，请用 {_CONVERSION_LANGS[lang]}（非机翻）")
    todo = todo if todo is not None else missing_keys(lang, tier)
    if limit and len(todo) > limit:
        todo = dict(list(todo.items())[:limit])
    glossary = None
    if glossary_path:
        glossary = json.loads(Path(glossary_path).read_text(encoding="utf-8"))

    items: dict = {}
    rejected: dict = {}
    if resume:
        prev = json.loads(Path(resume).read_text(encoding="utf-8"))
        assert prev.get("lang") == lang, f"resume 草稿语言 {prev.get('lang')} ≠ {lang}"
        items.update(prev.get("items") or {})
        rejected.update(prev.get("rejected") or {})
        done = set(items) | set(rejected)
        todo = {k: v for k, v in todo.items() if k not in done}
        print(f"[i18n_mt] resume: 已有 {len(items)} 通过/{len(rejected)} 拒绝，"
              f"续翻 {len(todo)}")
    if engine == "file":
        raw = json.loads(Path(from_json).read_text(encoding="utf-8"))
        raw = raw.get("items", raw)  # 兼容裸 map 或草稿再加工
        for k, tr in raw.items():
            if k not in todo:
                rejected[k] = "not_missing_or_unknown_key"
                continue
            why = validate_entry(k, todo[k]["zh"], tr, todo[k]["en"])
            (items if not why else rejected)[k] = tr if not why else why
    elif engine == "llm":
        if not api_key:
            raise SystemExit("--api-key 缺失（或设环境变量 I18N_MT_API_KEY / DEEPSEEK_API_KEY）")
        call = llm_fn or (lambda c: translate_batch_llm(
            api_base, api_key, model, lang, c, glossary))
        keys = list(todo)
        for i in range(0, len(keys), batch):
            chunk = {k: todo[k] for k in keys[i:i + batch]}
            try:
                trs = call(chunk)
            except Exception as e:  # noqa: BLE001 —— 单批失败不放倒整跑，键留给重跑
                print(f"[i18n_mt] {lang} 批次 {i//batch + 1} 失败: {e}")
                continue
            for k in chunk:
                tr = trs.get(k)
                if tr is None:
                    rejected[k] = "missing_in_llm_reply"
                    continue
                why = validate_entry(k, chunk[k]["zh"], tr, chunk[k]["en"])
                (items if not why else rejected)[k] = tr.strip() if not why else why
            print(f"[i18n_mt] {lang} 批次 {i//batch + 1}/{(len(keys)+batch-1)//batch}"
                  f" 通过 {len(items)} / 拒绝 {len(rejected)}")
    elif engine == "ollama_mt":
        # 单流 + 节流：与生产翻译共用节点，绝不并发挤占；keep_alive 常暖零冷载。
        out_dir = out_dir or _DRAFT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        draft = out_dir / f"{lang}_draft_{time.strftime('%Y%m%d_%H%M%S')}.json"

        def _flush():
            tmp = draft.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "lang": lang, "tier": tier, "engine": engine, "model": model,
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "items": items, "rejected": rejected,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(draft)

        keys = list(todo)
        t0 = time.time()
        streak = 0
        for n, k in enumerate(keys, 1):
            try:
                tr = translate_one_ollama_mt(api_base, model, lang, todo[k]["zh"])
                streak = 0
            except Exception as e:  # noqa: BLE001
                rejected[k] = f"engine_error: {e}"
                streak += 1
                if streak >= max_error_streak:
                    _flush()
                    print(f"[i18n_mt] {lang} 引擎连败 {streak} 次，熔断中止"
                          f"（现场已落盘 {draft}，修复后 --resume 续跑）")
                    return draft
                continue
            why = validate_entry(k, todo[k]["zh"], tr, todo[k]["en"])
            (items if not why else rejected)[k] = tr if not why else why
            if n % 50 == 0 or n == len(keys):
                rate = n / max(time.time() - t0, 0.001)
                print(f"[i18n_mt] {lang} {n}/{len(keys)} 通过 {len(items)}"
                      f" 拒绝 {len(rejected)} ({rate:.1f}/s)")
            if n % flush_every == 0:
                _flush()
            if sleep_s:
                time.sleep(sleep_s)
        _flush()
        print(f"[i18n_mt] 草稿已写 {draft}（通过 {len(items)}，拒绝 {len(rejected)}）")
        return draft
    else:
        raise SystemExit(f"未知 engine: {engine}")

    out_dir = out_dir or _DRAFT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    draft = out_dir / f"{lang}_draft_{time.strftime('%Y%m%d_%H%M%S')}.json"
    draft.write_text(json.dumps({
        "lang": lang, "tier": tier, "engine": engine, "model": model,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "items": items, "rejected": rejected,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[i18n_mt] 草稿已写 {draft}（通过 {len(items)}，拒绝 {len(rejected)}）")
    return draft


# ── 写包（write）──────────────────────────────────────────────────────────────

_AUTO_HEADER = '''# -*- coding: utf-8 -*-
"""{lang_name} ({lang}) 机翻长尾词条 —— scripts/i18n_mt.py 自动生成，勿手改。

生成: {created} · engine={engine} model={model} · {n} 键
定位: 「en 底 + override」的**机翻底稿层**——比英文回落更贴近坐席母语，但未经
母语复核。母语复核通过的键请「转正」挪进人工词包（{lang}_<域>.py），本文件由
工具 regen（write --mode replace）时会自动排除人工包已覆盖键、不会复活旧稿。
门禁: tests/test_i18n_extra_langs.py（子集/占位符/非空，全扩展语言通用）。
"""

{var} = {{
'''


def _load_auto_existing(lang: str, packs_dir: Path) -> dict:
    p = packs_dir / f"{lang}_auto.py"
    if not p.is_file():
        return {}
    ns: dict = {}
    exec(compile(p.read_text(encoding="utf-8-sig"), str(p), "exec"), ns)  # noqa: S102
    return dict(ns.get(lang.upper(), {}) or {})


def _covered_by_human_packs(lang: str, packs_dir: Path) -> set:
    """该语言在**人工词包**（非 <lang>_auto.py）里已覆盖的键。"""
    from src.web.i18n_packs import collect_all
    _zh, _en, extras = collect_all(force_reload=True)
    all_ov = set(extras.get(lang, {}))
    return all_ov - set(_load_auto_existing(lang, packs_dir))


def run_write(lang: str, draft_path: Path, *, mode: str = "merge",
              packs_dir: Path | None = None, dry_run: bool = False,
              zh_view: dict | None = None) -> dict:
    """草稿合入 <lang>_auto.py。返回统计 dict（写入/排除/拒绝数）。"""
    packs_dir = packs_dir or _PACKS_DIR
    d = json.loads(Path(draft_path).read_text(encoding="utf-8"))
    assert d.get("lang") == lang, f"草稿语言 {d.get('lang')} ≠ --lang {lang}"
    en_view: dict = {}
    if zh_view is None:
        zh_view, en_view, _ex = merged_views()

    human = _covered_by_human_packs(lang, packs_dir) if packs_dir == _PACKS_DIR else set()
    base = _load_auto_existing(lang, packs_dir) if mode == "merge" else {}
    stats = {"in_draft": len(d.get("items", {})), "written": 0,
             "skip_human": 0, "skip_orphan": 0, "rejected": 0}
    for k, tr in sorted(d.get("items", {}).items()):
        if k not in zh_view:
            stats["skip_orphan"] += 1
            continue
        if k in human:
            stats["skip_human"] += 1
            continue
        # 复检口径必须与 translate 侧一致（都带 en 参考的长度上限）——2026-08-27
        # 实测：不带 en 时上限按 zh 长度算，越南语天然比中文长，82 个合法键被误拒。
        why = validate_entry(k, zh_view[k], tr, en_view.get(k, ""))
        if why:
            stats["rejected"] += 1
            continue
        base[k] = str(tr).strip()
    stats["written"] = len(base)

    out = packs_dir / f"{lang}_auto.py"
    if dry_run:
        print(f"[i18n_mt] dry-run: {out} 将含 {stats['written']} 键 "
              f"(人工包排除 {stats['skip_human']}, orphan {stats['skip_orphan']}, "
              f"复检拒绝 {stats['rejected']})")
        return stats

    lines = [_AUTO_HEADER.format(
        lang_name=_lang_name(lang), lang=lang, created=d.get("created", ""),
        engine=d.get("engine", ""), model=d.get("model", ""),
        n=stats["written"], var=lang.upper())]
    prev_group = None
    for k in sorted(base):
        group = k.split(".", 1)[0]
        if group != prev_group:
            lines.append(f"    # ── {group} ──\n")
            prev_group = group
        lines.append(f"    {k!r}: {base[k]!r},\n")
    lines.append("}\n")
    out.write_text("".join(lines), encoding="utf-8")
    print(f"[i18n_mt] 已写 {out}: {stats['written']} 键 "
          f"(人工包排除 {stats['skip_human']}, orphan {stats['skip_orphan']}, "
          f"复检拒绝 {stats['rejected']})")
    return stats


# ── 转正（promote）：机翻键复核通过后挪进人工词包 ─────────────────────────────

_REVIEWED_HEADER = '''# -*- coding: utf-8 -*-
"""{lang} 人工复核词条（scripts/i18n_mt.py promote 从机翻包转正；可直接手改）。

来源：<lang>_auto.py 机翻底稿 → 母语复核 → 本文件。regen/write 会自动排除
本文件已覆盖键（不复活旧机翻稿）。热更新即时生效。
"""

{var} = {{
'''


def _rewrite_pack(path: Path, lang: str, data: dict, header: str) -> None:
    lines = [header]
    prev = None
    for k in sorted(data):
        g = k.split(".", 1)[0]
        if g != prev:
            lines.append(f"    # ── {g} ──\n")
            prev = g
        lines.append(f"    {k!r}: {data[k]!r},\n")
    lines.append("}\n")
    path.write_text("".join(lines), encoding="utf-8")


def run_promote(lang: str, *, keys: list | None = None, prefix: str = "",
                to: str = "", packs_dir: Path | None = None) -> dict:
    """把 <lang>_auto.py 里复核过的键挪进人工词包 <to>.py（默认 <lang>_reviewed）。

    顺序契约：**先从 auto 移除、后写入人工包**——中间窗口该键短暂回落底语言
    （无害），反序则是同键双包定义 → collect fail-fast（热重载会保旧字典，但
    没必要制造告警）。
    """
    packs_dir = packs_dir or _PACKS_DIR
    to = to or f"{lang}_reviewed"
    auto_path = packs_dir / f"{lang}_auto.py"
    auto = _load_auto_existing(lang, packs_dir)
    if not auto:
        raise SystemExit(f"{auto_path} 不存在或为空")
    picked = {k for k in (keys or []) if k in auto}
    if prefix:
        picked |= {k for k in auto if k.startswith(prefix)}
    if not picked:
        raise SystemExit("未选中任何键（--keys / --prefix）")

    moved = {k: auto.pop(k) for k in sorted(picked)}
    var = lang.upper()
    _rewrite_pack(auto_path, lang, auto, _AUTO_HEADER.format(
        lang_name=_lang_name(lang), lang=lang,
        created=time.strftime("%Y-%m-%d %H:%M:%S"),
        engine="promote-rewrite", model="", n=len(auto), var=var))

    to_path = packs_dir / f"{to}.py"
    existing: dict = {}
    if to_path.is_file():
        ns: dict = {}
        exec(compile(to_path.read_text(encoding="utf-8-sig"), str(to_path),
                     "exec"), ns)  # noqa: S102
        existing = dict(ns.get(var, {}) or {})
    existing.update(moved)
    _rewrite_pack(to_path, lang, existing,
                  _REVIEWED_HEADER.format(lang=lang, var=var))
    print(f"[i18n_mt] 转正 {len(moved)} 键: {auto_path.name} → {to_path.name}"
          f"（auto 余 {len(auto)}，人工包共 {len(existing)}）")
    return {"moved": len(moved), "auto_left": len(auto), "reviewed": len(existing)}


# ── 盘点（inventory）──────────────────────────────────────────────────────────

def run_inventory(tier: str = "all") -> dict:
    from src.web.i18n_packs import EXTRA_LANGS
    zh, _en, extras = merged_views()
    hot = hot_keys()
    rep = {"zh_total": len(zh), "hot_total": len(hot), "langs": {}}
    for lg in EXTRA_LANGS:
        ov = set(extras.get(lg, {}))
        miss_all = set(zh) - ov
        miss_hot = miss_all & hot
        rep["langs"][lg] = {
            "override": len(ov),
            "hot_covered": len(ov & hot),
            "missing_all": len(miss_all),
            "missing_hot": len(miss_hot),
            "missing_zh_chars": sum(len(zh[k]) for k in
                                    (miss_hot if tier == "hot" else miss_all)),
        }
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return rep


# ── 自检（零网络；pytest 门禁调用）───────────────────────────────────────────

def run_selftest() -> int:
    import tempfile
    zh, en, _ex = merged_views()
    lang = "vi"
    todo_full = missing_keys(lang, "hot")
    todo = dict(list(todo_full.items())[:24])
    assert todo, "selftest: 无缺键样本（vi 已全覆盖？请换语种）"

    # 假引擎：拿 en 参考译加语言前缀——占位符/标签天然守恒，零 CJK
    def fake_llm(chunk: dict) -> dict:
        return {k: f"[{lang}] " + v["en"] for k, v in chunk.items()}

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        draft = run_translate(lang, "hot", "llm", api_key="selftest",
                              out_dir=tdp, todo=todo, llm_fn=fake_llm)
        d = json.loads(draft.read_text(encoding="utf-8"))
        assert len(d["items"]) == len(todo), f"selftest: 通过数 {len(d['items'])} ≠ {len(todo)}"
        stats = run_write(lang, draft, packs_dir=tdp, zh_view=zh)
        assert stats["written"] == len(todo)
        ns: dict = {}
        exec(compile((tdp / f"{lang}_auto.py").read_text(encoding="utf-8-sig"),
                     "auto", "exec"), ns)  # noqa: S102
        assert set(ns[lang.upper()]) == set(todo)
        # 校验器负样本
        k0 = next(iter(todo))
        assert validate_entry(k0, "保存 {n}", "Save") == "placeholder_mismatch"
        assert validate_entry(k0, "保存", "保存") == "untranslated_zh_passthrough"
        assert validate_entry(k0, "保存", "Lưu 中") == "cjk_leak"
        assert validate_entry(k0, "保存", "") == "empty"
        assert validate_entry(k0, "<b>存</b>", "Save") == "html_tag_mismatch"
        assert validate_entry(k0, "保存 {n}", "Lưu {n}") == ""
    print("[i18n_mt] selftest OK")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_inv = sub.add_parser("inventory", help="各扩展语缺键盘点")
    p_inv.add_argument("--tier", choices=("all", "hot"), default="all")

    p_tr = sub.add_parser("translate", help="机翻缺键 → 草稿 JSON（不碰词包）")
    p_tr.add_argument("--lang", required=True)
    p_tr.add_argument("--tier", choices=("all", "hot"), default="hot")
    p_tr.add_argument("--engine", choices=("llm", "ollama_mt", "file"), default="llm")
    p_tr.add_argument("--api-base", default="https://api.deepseek.com/v1")
    p_tr.add_argument("--api-key", default=os.environ.get(
        "I18N_MT_API_KEY", os.environ.get("DEEPSEEK_API_KEY", "")))
    p_tr.add_argument("--model", default="deepseek-v4-flash")
    p_tr.add_argument("--from-json", default="", help="engine=file 时的外部译文 JSON")
    p_tr.add_argument("--limit", type=int, default=0)
    p_tr.add_argument("--batch", type=int, default=40)
    p_tr.add_argument("--glossary", default="", help="术语表 JSON {原文:译法}")
    p_tr.add_argument("--sleep", type=float, default=0.05,
                      help="ollama_mt 单条间隔秒（与生产共用节点的礼让节流）")
    p_tr.add_argument("--resume", default="", help="断点续跑：上次的草稿 JSON 路径")

    p_wr = sub.add_parser("write", help="草稿合入 i18n_packs/<lang>_auto.py")
    p_wr.add_argument("--lang", required=True)
    p_wr.add_argument("--draft", required=True)
    p_wr.add_argument("--mode", choices=("merge", "replace"), default="merge")
    p_wr.add_argument("--dry-run", action="store_true")

    p_pm = sub.add_parser("promote", help="复核通过的机翻键 → 人工词包（regen 不复活）")
    p_pm.add_argument("--lang", required=True)
    p_pm.add_argument("--keys", default="", help="逗号分隔键名")
    p_pm.add_argument("--prefix", default="", help="按键前缀批量选中（如 inbox.xl.）")
    p_pm.add_argument("--to", default="", help="目标人工包名（默认 <lang>_reviewed）")

    sub.add_parser("selftest", help="零网络全链路自检")

    a = ap.parse_args()
    if a.cmd == "inventory":
        run_inventory(a.tier)
        return 0
    if a.cmd == "translate":
        run_translate(a.lang, a.tier, a.engine, api_base=a.api_base,
                      api_key=a.api_key, model=a.model, from_json=a.from_json,
                      limit=a.limit, batch=a.batch, glossary_path=a.glossary,
                      sleep_s=a.sleep, resume=a.resume)
        return 0
    if a.cmd == "write":
        run_write(a.lang, Path(a.draft), mode=a.mode, dry_run=a.dry_run)
        return 0
    if a.cmd == "promote":
        run_promote(a.lang,
                    keys=[k.strip() for k in a.keys.split(",") if k.strip()],
                    prefix=a.prefix, to=a.to)
        return 0
    if a.cmd == "selftest":
        return run_selftest()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
