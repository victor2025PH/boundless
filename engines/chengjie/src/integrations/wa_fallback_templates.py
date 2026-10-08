"""WhatsApp Cloud API 24h 窗口回退模板选择（2026-10-08 智语）。

自由文本命中 ``window_expired`` 时，按会话语言从 ``whatsapp_cloud.window_fallback_template``
挑一条已审核模板。纯函数，不发网络。

配置形状（向后兼容）：

- 只填顶层 ``name`` / ``language`` / ``text_param``（旧形状）→ 不看会话语言，行为与以前一致。
- ``by_language.{zh,en,tl}`` 有 name → 会话语言命中该条；Taglish / fil / ceb 归 ``tl``。
  对不上时用 ``default_language``（缺省 ``zh``），再不行才回落顶层旧模板。
- 名字以 ``wa_fb_gamble_`` 开头的模板只允许内部包。``edition="public"``（或运行期
  ``CHATX_FLAVOR`` / ``CHATX_EDITION`` 为 public、clean）时这些名字视为没配置。

模板正文本身在 ``config/presets/packs/*/wa_fallback_templates.yaml``（中性）和
``config/presets/internal/gambling_operator/wa_fallback_templates.yaml``（仅内部包）。
本模块不把正文发给 Meta——发送的是审核通过后的 name + language + ``{{1}}`` 参数。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# 公开包选择器拒绝此前缀。博彩模板必须用它，这样即使配置里手写了名字，公开形态也不会发出去。
INTERNAL_ONLY_NAME_PREFIX = "wa_fb_gamble_"

_LANGS = ("zh", "en", "tl")
_NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
_PLACEHOLDER_RE = re.compile(r"\{\{(\d+)\}\}")
# UTILITY 正文里出现这些，Meta 会改判 MARKETING 或拒审。否定句里写到也算（审核不看语境）。
_PROMO_RE = re.compile(
    r"discount|promo|\bsale\b|buy now|limited time|\boffer\b|\bbonus\b|jackpot|"
    r"\bodds\b|\bbet\b|betting|casino|sabong|free spin|优惠|折扣|促销|红包|"
    r"下注|投注|中奖|赠送|博彩|赌场|百家乐",
    re.I,
)
_URL_RE = re.compile(r"https?://|www\.", re.I)

# 会话语言 → 我们的三档。Meta 模板语言码另见各 yaml 的 language 字段（tl → fil）。
_ZH = frozenset({
    "zh", "zh-cn", "zh-hans", "zh-sg", "zh-tw", "zh-hk", "zh-hant", "cn", "chinese",
})
_EN = frozenset({"en", "en-us", "en-gb", "en-ph", "english"})
_TL = frozenset({
    "tl", "fil", "fil-ph", "tl-ph", "tagalog", "taglish", "ceb", "filipino", "bisaya",
})


def normalize_fallback_lang(raw: Any) -> str:
    """会话 / 配置里的语言码 → ``zh`` | ``en`` | ``tl`` | ``""``。

    Taglish、Filipino（fil）和宿务（ceb）都归 ``tl``：下游只备一份他加禄/菲律宾语模板，
    Meta 语言码用 ``fil``（模板语言表没有单独的 Taglish / Tagalog 码）。
    """
    s = str(raw or "").strip().lower().replace("_", "-")
    if not s or s in {"unknown", "auto", "und"}:
        return ""
    if s in _ZH or s.startswith("zh-"):
        return "zh"
    if s in _EN or s.startswith("en-"):
        return "en"
    if s in _TL or s.startswith("fil-") or s.startswith("tl-"):
        return "tl"
    return ""


def is_internal_only_template_name(name: Any) -> bool:
    return str(name or "").strip().startswith(INTERNAL_ONLY_NAME_PREFIX)


def runtime_edition() -> str:
    """桌面打包形态。``public`` / ``clean`` 视作公开包；未设置则 ``""``（开发/服务进程不拦前缀以外的名字）。"""
    for key in ("CHATX_FLAVOR", "CHATX_EDITION"):
        v = str(os.environ.get(key) or "").strip().lower()
        if v in {"public", "clean"}:
            return "public"
        if v == "internal":
            return "internal"
    return ""


def _edition(edition: str) -> str:
    e = str(edition or "").strip().lower()
    if e in {"public", "clean"}:
        return "public"
    if e == "internal":
        return "internal"
    return ""


def _usable_name(name: Any, edition: str) -> str:
    n = str(name or "").strip()
    if not n:
        return ""
    if _edition(edition) == "public" and is_internal_only_template_name(n):
        return ""
    return n


def _meta_language(spec: Dict[str, Any], lang_key: str) -> str:
    code = str(spec.get("language") or "").strip()
    if code:
        return code
    return {"zh": "zh_CN", "en": "en", "tl": "fil"}.get(lang_key, "en_US")


def _pack_choice(spec: Dict[str, Any], name: str, lang_key: str, source: str) -> Dict[str, Any]:
    max_chars = spec.get("param_max_chars")
    try:
        max_chars = int(max_chars) if max_chars not in (None, "") else None
    except (TypeError, ValueError):
        max_chars = None
    if max_chars is not None and max_chars <= 0:
        max_chars = None
    text_param = spec.get("text_param", True)
    return {
        "name": name,
        "language": _meta_language(spec, lang_key),
        "text_param": bool(text_param) if text_param is not None else True,
        "param_max_chars": max_chars,
        "resolved_lang": lang_key,
        "source": source,
    }


def _empty() -> Dict[str, Any]:
    return {
        "name": "", "language": "", "text_param": True, "param_max_chars": None,
        "resolved_lang": "", "source": "none",
    }


def _by_language(block: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw = block.get("by_language")
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        lang = normalize_fallback_lang(key)
        if lang in _LANGS and lang not in out:
            out[lang] = spec
    return out


def select_window_fallback(
    block: Any,
    conv_lang: Any = "",
    *,
    edition: str = "",
) -> Dict[str, Any]:
    """从配置块挑一条回退模板。``name`` 为空表示不回退。

    ``edition="public"`` 时丢掉 ``wa_fb_gamble_*``。旧形状（没有可用的 by_language）
    忽略 ``conv_lang``。
    """
    if not isinstance(block, dict):
        return _empty()
    edition_n = _edition(edition)
    by = _by_language(block)
    usable = {k: _usable_name(v.get("name"), edition_n) for k, v in by.items()}
    has_by = any(usable.values())
    legacy_name = _usable_name(block.get("name"), edition_n)

    if not has_by:
        if not legacy_name:
            return _empty()
        lang_key = normalize_fallback_lang(block.get("language"))
        return _pack_choice(block, legacy_name, lang_key, "legacy")

    lang = normalize_fallback_lang(conv_lang)
    default = normalize_fallback_lang(block.get("default_language")) or "zh"
    tried = []
    if lang:
        tried.append((lang, "by_language"))
    if default not in (lang,):
        tried.append((default, "default_language"))
    for key, source in tried:
        name = usable.get(key) or ""
        if name:
            return _pack_choice(by[key], name, key, source)
    if legacy_name:
        return _pack_choice(block, legacy_name, normalize_fallback_lang(block.get("language")), "legacy")
    return _empty()


def fallback_configured(block: Any, *, edition: str = "") -> bool:
    """健康面板：有没有一条公开形态下真能用的回退模板。"""
    if not isinstance(block, dict):
        return False
    edition_n = _edition(edition)
    if _usable_name(block.get("name"), edition_n):
        return True
    return any(_usable_name(spec.get("name"), edition_n) for spec in _by_language(block).values())


def configured_languages(block: Any, *, edition: str = "") -> List[str]:
    """已配置且当前形态可用的语言档。只有旧的单模板时返回 ``["legacy"]``。"""
    if not isinstance(block, dict):
        return []
    edition_n = _edition(edition)
    langs = [k for k, spec in _by_language(block).items()
             if _usable_name(spec.get("name"), edition_n)]
    if langs:
        return langs
    if _usable_name(block.get("name"), edition_n):
        return ["legacy"]
    return []


def prepare_text_param(text: str, max_chars: Optional[int] = None) -> str:
    """``{{1}}`` 参数。不设上限时原样返回（旧模板兼容，由发送侧再截到 1024）。

    设了 ``param_max_chars`` 时折掉换行/多空格（Meta 拒参数里的换行）并截断。
    """
    s = str(text or "")
    if not max_chars:
        return s
    s = re.sub(r"\s+", " ", s).strip()
    limit = int(max_chars)
    if len(s) <= limit:
        return s
    cut = s[:limit].rstrip()
    if " " in cut:
        head, _, _ = cut.rpartition(" ")
        if len(head) >= max(8, limit // 2):
            cut = head
    return cut.strip()


def catalog_files(engine_root: Path, *, edition: str = "") -> List[Path]:
    """模板目录。``edition="public"`` 不返回 ``config/presets/internal`` 下的文件（与打包省略同口径）。"""
    root = Path(engine_root)
    files = sorted((root / "config" / "presets" / "packs").glob("*/wa_fallback_templates.yaml"))
    if _edition(edition) != "public":
        files.extend(sorted(
            (root / "config" / "presets" / "internal").glob("*/wa_fallback_templates.yaml")))
    return files


def load_catalog(engine_root: Path, *, edition: str = "") -> List[Dict[str, Any]]:
    import yaml
    out: List[Dict[str, Any]] = []
    for path in catalog_files(engine_root, edition=edition):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            continue
        for item in data.get("templates") or []:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row["_pack"] = str(data.get("pack") or path.parent.name)
            row["_edition"] = str(data.get("edition") or "")
            row["_file"] = str(path)
            row["_schema"] = str(data.get("schema") or "")
            out.append(row)
    return out


def template_problems(item: Dict[str, Any]) -> List[str]:
    """一条目录模板不符合 Meta UTILITY 提交约束时的原因（空列表 = 可提交）。"""
    bad: List[str] = []
    name = str(item.get("name") or "")
    if not _NAME_RE.match(name):
        bad.append(f"name 须为小写字母数字下划线: {name!r}")
    if str(item.get("category") or "") != "UTILITY":
        bad.append("category 须为 UTILITY")
    lang = str(item.get("lang") or "")
    code = str(item.get("language") or "")
    if lang not in _LANGS:
        bad.append(f"lang 须为 zh/en/tl: {lang!r}")
    expect = {"zh": "zh_CN", "en": "en", "tl": "fil"}.get(lang, "")
    if expect and code != expect:
        bad.append(f"language 应为 {expect}，实际 {code!r}")
    edition = str(item.get("_edition") or item.get("edition") or "")
    if edition == "internal_only" and not is_internal_only_template_name(name):
        bad.append("内部包模板名须以 wa_fb_gamble_ 开头")
    if edition == "public" and is_internal_only_template_name(name):
        bad.append("公开包不得使用 wa_fb_gamble_ 前缀")
    body = str(item.get("body") or "")
    if not body or len(body) > 1024:
        bad.append(f"正文长度须在 1..1024，实际 {len(body)}")
    nums = [int(n) for n in _PLACEHOLDER_RE.findall(body)]
    if nums != [1]:
        bad.append(f"正文须恰好一个 {{{{1}}}}，实际 {nums}")
    stripped = body.strip()
    if stripped.startswith("{{") or stripped.endswith("}}"):
        bad.append("变量不能在正文开头或结尾")
    if _PROMO_RE.search(body):
        bad.append("UTILITY 正文含促销/博彩推广用语")
    if _URL_RE.search(body):
        bad.append("正文不要带链接")
    if lang == "zh" and "退订" not in body:
        bad.append("中文正文须含「退订」（STOP 闸整句关键词）")
    if lang in {"en", "tl"} and not re.search(r"\bSTOP\b", body):
        bad.append("英/他加禄正文须含整词 STOP（STOP 闸整句关键词）")
    if item.get("text_param") is not True:
        bad.append("text_param 须为 true（{{1}} 带简短事项）")
    samples = item.get("samples") or {}
    sample = ""
    if isinstance(samples, dict):
        sample = str(samples.get(1) or samples.get("1") or "")
    if not sample.strip():
        bad.append("缺 {{1}} 的提交样例 samples.1")
    return bad


def problems_for(items: Iterable[Dict[str, Any]]) -> List[str]:
    bad: List[str] = []
    for item in items:
        for reason in template_problems(item):
            bad.append(f"{item.get('name')}: {reason}")
    return bad
