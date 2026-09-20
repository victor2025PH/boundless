"""人设长传记检索质量评测（把 56 例 A/B 校准台固化成常驻门禁）。

**为什么要有这个文件**：`personas.bio_retrieval` 那一串参数——`sem_floor`/
`sem_floor_kw0`/`sem_veto`/`neighbor_expand`/`sentence_semantic`/别名表——全是靠
一份真实人设文档上的 56 例人工用例集 A/B 校出来的，而那份台子长期只活在
gitignored 的 `tmp_*.py` 里。结果是：任何人改动 `persona_bio_store` 的分块、
打分或准入逻辑，CI 只会告诉他「单元测试还绿着」，**不会**告诉他整体命中率掉了
几个点。这个模块就是补这个洞。

口径（与 tmp 台子一致，换算得出同一个数）：
  - 逐条跑**真实** ``PersonaBioStore.search_bio`` + ``build_bio_block``；
  - 正例：任一期望原文子串出现在注入块里即命中；
  - 负例：注入块必须为空（文档里没有的事 / 纯寒暄不得注入）；
  - ``accuracy`` = (正例命中 + 负例正确沉默) / 全部用例。

两条轨（与 ``memory_eval`` 同一套设计）：
  - **确定性轨**（默认，CI 常驻）：``deterministic_embed`` 字符 bigram 哈希，
    零依赖可复现。它只捕获字面重叠，语义通道基本是「瞎的」——所以它守的是
    **分块 / 别名扩张 / 邻域扩张 / 关键词准入 / 负例沉默**这条链，*不是*语义
    天花板。``sem_veto`` 在这条轨上多半由 ``_SEM_VETO_MIN_SIGNAL`` 兜底而不激活，
    这是刻意的：宁可门禁守窄一点，也不要一个在 CI 里永远测不准的数字。
  - **真实嵌入轨**（``build_real_embed_fn`` 可用时）：才真正度量 sem_floor /
    sem_veto 这些语义参数。局域网嵌入端点不可达 → 优雅跳过。

语料：``config/eval/persona_bio_corpus.yaml``（合成虚构人设，结构仿真）。真实
人设档案含个人信息不进仓，要用它校准就设 ``AITR_BIO_EVAL_CORPUS`` 指到本地
另一份同结构 yaml（支持 ``doc_path`` 指外部 .docx/.txt）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .memory_eval import build_real_embed_fn, deterministic_embed

EmbedFn = Callable[[str], Optional[List[float]]]

DEFAULT_CORPUS_PATH = "config/eval/persona_bio_corpus.yaml"
CORPUS_ENV = "AITR_BIO_EVAL_CORPUS"

# 确定性轨的准入线。**不是**真实嵌入下的天花板（那一档实测 96.4%），而是
# 「字面通道该够得着的都够得着」的下限；掉下去 = 分块/别名/邻域扩张出了回归。
DEFAULT_ACCURACY_TARGET = 0.80
DEFAULT_MAX_FALSE_RECALL = 2          # 负例误召上限（条）

CATEGORIES = ("syn", "tail", "en", "neg")


@dataclass
class BioCase:
    """一条检索用例。``expect`` 为文档原文子串，任一命中即算。"""

    category: str
    query: str
    expect: List[str] = field(default_factory=list)
    negative: bool = False
    note: str = ""


@dataclass
class BioCorpus:
    persona_id: str
    doc: str
    profile: Dict[str, Any] = field(default_factory=dict)
    cases: List[BioCase] = field(default_factory=list)
    source: str = ""


def _read_doc(spec: Mapping[str, Any], base_dir: str) -> str:
    """``doc``（内联正文）优先；否则 ``doc_path`` 指外部 .docx/.txt（相对语料文件）。"""
    inline = spec.get("doc")
    if isinstance(inline, str) and inline.strip():
        return inline
    rel = str(spec.get("doc_path") or "").strip()
    if not rel:
        raise ValueError("语料缺 doc / doc_path")
    path = rel if os.path.isabs(rel) else os.path.join(base_dir, rel)
    with open(path, "rb") as f:
        raw = f.read()
    if path.lower().endswith(".docx"):
        from src.utils.persona_doc_import import extract_docx_text
        return extract_docx_text(raw)
    return raw.decode("utf-8", "ignore")


def load_bio_corpus(path: Optional[str] = None) -> BioCorpus:
    """加载检索评测语料；``path`` 为空则读 env 覆写，再回落内置默认集。"""
    import yaml

    path = path or os.environ.get(CORPUS_ENV) or DEFAULT_CORPUS_PATH
    with open(path, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f) or {}
    if not isinstance(spec, Mapping):
        raise ValueError(f"语料格式错误（应为 mapping）：{path}")

    cases: List[BioCase] = []
    for row in spec.get("cases") or []:
        if not isinstance(row, Mapping):
            continue
        q = str(row.get("q") or row.get("query") or "").strip()
        if not q:
            continue
        cases.append(BioCase(
            category=str(row.get("cat") or row.get("category") or "tail"),
            query=q,
            expect=[str(x) for x in (row.get("expect") or []) if str(x)],
            negative=bool(row.get("negative")),
            note=str(row.get("note") or ""),
        ))
    profile = spec.get("profile")
    return BioCorpus(
        persona_id=str(spec.get("persona_id") or "bio_eval"),
        doc=_read_doc(spec, os.path.dirname(os.path.abspath(path))),
        profile=dict(profile) if isinstance(profile, Mapping) else {},
        cases=cases,
        source=path,
    )


def corpus_is_grounded(corpus: BioCorpus) -> List[Dict[str, str]]:
    """校验每个期望词确实是文档原文子串；返回臆造项（空 = 全部接地）。

    没有这一步，语料会慢慢腐化成「期望文档里根本没有的东西」——那时候门禁红了
    也不知道是检索坏了还是用例错了。负例不带期望词，天然跳过。
    """
    low = corpus.doc.lower()
    bad: List[Dict[str, str]] = []
    for c in corpus.cases:
        if c.negative:
            continue
        for e in c.expect:
            if e.lower() not in low:
                bad.append({"query": c.query, "expect": e})
    return bad


def _snapshot_globals(pbs: Any) -> Dict[str, Any]:
    return {
        "embed": pbs._EMBED_FN,                  # noqa: SLF001（评测需接管全局钩子）
        "cfg": pbs._RETRIEVAL_CFG_OVERRIDE,      # noqa: SLF001
        "alias": pbs._ALIAS_PROVIDER,            # noqa: SLF001
    }


def _restore_globals(pbs: Any, snap: Mapping[str, Any]) -> None:
    pbs.set_embed_fn(snap["embed"])
    pbs.set_retrieval_cfg(snap["cfg"])
    pbs.set_alias_provider(snap["alias"])
    pbs.clear_query_embedding_cache()


def evaluate_bio_retrieval(
    corpus: Optional[BioCorpus] = None,
    *,
    embed_fn: Optional[EmbedFn] = None,
    settings: Optional[Mapping[str, Any]] = None,
    use_profile_aliases: bool = True,
    accuracy_target: float = DEFAULT_ACCURACY_TARGET,
    max_false_recall: int = DEFAULT_MAX_FALSE_RECALL,
) -> Dict[str, Any]:
    """端到端跑真实检索链，返回逐条命中 + 分类汇总 + passed。

    ``settings`` 缺省 = ``default_retrieval_settings()``（代码内置默认档，
    **刻意不读 config 文件**：评测要在任意机器上复现同一个数字）。传字典即在
    默认档上叠加，用于校准实验或跑生产档位。
    """
    from src.companion import persona_bio_store as pbs
    from src.companion.persona_bio_store import PersonaBioStore

    corpus = corpus if corpus is not None else load_bio_corpus()
    eff = pbs.default_retrieval_settings()
    if settings:
        eff.update(dict(settings))

    snap = _snapshot_globals(pbs)
    try:
        pbs.set_embed_fn(embed_fn)
        pbs.set_retrieval_cfg(eff)
        pbs.set_alias_provider(
            (lambda _pid, _p=corpus.profile: _p)
            if (use_profile_aliases and corpus.profile) else None)
        pbs.clear_query_embedding_cache()

        store = PersonaBioStore(":memory:", embed_fn=embed_fn)
        store.replace_bio_doc(corpus.persona_id, corpus.doc)
        top_k = int(eff.get("top_k", 3))

        results: List[Dict[str, Any]] = []
        for c in corpus.cases:
            hits = store.search_bio(corpus.persona_id, c.query, top_k=top_k)
            block = store.build_bio_block(corpus.persona_id, c.query, hits=hits) or ""
            low = block.lower()
            ok = (not block) if c.negative else any(e.lower() in low for e in c.expect)
            results.append({
                "category": c.category, "query": c.query, "expect": list(c.expect),
                "negative": c.negative, "ok": ok, "block_chars": len(block),
                "block": block, "note": c.note,
            })
    finally:
        _restore_globals(pbs, snap)

    n = len(results)
    pos = [r for r in results if not r["negative"]]
    negs = [r for r in results if r["negative"]]
    correct = sum(1 for r in results if r["ok"])
    false_recall = sum(1 for r in negs if not r["ok"])
    by_cat = {
        c: {"ok": sum(1 for r in results if r["category"] == c and r["ok"]),
            "total": sum(1 for r in results if r["category"] == c)}
        for c in CATEGORIES
        if any(r["category"] == c for r in results)
    }
    accuracy = round(correct / n, 4) if n else 0.0
    pos_recall = (round(sum(1 for r in pos if r["ok"]) / len(pos), 4) if pos else 0.0)
    return {
        "corpus": corpus.source,
        "persona_id": corpus.persona_id,
        "settings": eff,
        "embedding": "real" if embed_fn is not None else "none",
        "results": results,
        "summary": {
            "total": n, "correct": correct, "accuracy": accuracy,
            "positives": len(pos), "pos_recall": pos_recall,
            "negatives": len(negs), "false_recall": false_recall,
            "avg_block_chars": (
                round(sum(r["block_chars"] for r in pos) / len(pos), 1) if pos else 0.0),
            "by_category": by_cat,
        },
        "accuracy_target": accuracy_target,
        "max_false_recall": max_false_recall,
        "passed": accuracy >= accuracy_target and false_recall <= max_false_recall,
    }


def compare_bio_settings(
    variants: Sequence[tuple],
    *,
    corpus: Optional[BioCorpus] = None,
    embed_fn: Optional[EmbedFn] = None,
) -> Dict[str, Any]:
    """A/B 台：``variants`` = ``[(标签, 参数覆盖 dict), ...]``，首项为基线。

    返回每档汇总 + 相对基线的逐条 fixed/broken——「改这个参数到底修好了什么、
    弄坏了什么」，正是当初调 sem_veto / neighbor_expand 时反复要看的那张表。
    """
    corpus = corpus if corpus is not None else load_bio_corpus()
    rows = [{"label": lbl,
             "report": evaluate_bio_retrieval(corpus, embed_fn=embed_fn, settings=cfg)}
            for lbl, cfg in variants]
    base = rows[0]["report"] if rows else None
    for row in rows[1:]:
        fixed, broken = [], []
        for b, r in zip(base["results"], row["report"]["results"]):
            if b["ok"] == r["ok"]:
                continue
            (fixed if r["ok"] else broken).append(
                {"category": b["category"], "query": b["query"]})
        row["fixed"], row["broken"] = fixed, broken
        row["delta_accuracy"] = round(
            row["report"]["summary"]["accuracy"] - base["summary"]["accuracy"], 4)
    return {"variants": rows}


def format_bio_report(report: Dict[str, Any]) -> str:
    m = report["summary"]
    cats = "  ".join(f"{c}={v['ok']}/{v['total']}" for c, v in m["by_category"].items())
    lines = [
        "=== 人设传记检索报告 ===",
        f"语料: {report['corpus']}  嵌入: {report['embedding']}  "
        f"档位: top_k={report['settings'].get('top_k')} "
        f"sem_veto={report['settings'].get('sem_veto')} "
        f"neighbor={report['settings'].get('neighbor_expand')} "
        f"sent_sem={report['settings'].get('sentence_semantic')}",
        f"准确率: {m['accuracy']:.1%} ({m['correct']}/{m['total']})  "
        f"正例召回: {m['pos_recall']:.1%}  负例误召: {m['false_recall']}/{m['negatives']}  "
        f"正例块长: {m['avg_block_chars']:.0f}字",
        f"分类: {cats}",
        f"目标: 准确率≥{report['accuracy_target']:.0%} 且 误召≤{report['max_false_recall']}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    bad = [r for r in report["results"] if not r["ok"]]
    if bad:
        lines.append(f"未通过 {len(bad)} 例:")
        for r in bad[:20]:
            why = "负例误召" if r["negative"] else "未命中"
            tail = f"  期望={r['expect'][:2]}" if r["expect"] else ""
            lines.append(f"  [{r['category']}/{why}] {r['query']}{tail}")
            if r["negative"] and r["block"]:
                lines.append(f"      误注入({r['block_chars']}字): "
                             f"{r['block'][:90].replace(chr(10), ' ').strip()}")
    return "\n".join(lines)


def format_compare_report(cmp: Dict[str, Any]) -> str:
    lines = ["=== 人设传记检索 A/B ===",
             f"{'档位':30} {'准确率':16} {'Δ':>8}  修好/弄坏"]
    for i, row in enumerate(cmp["variants"]):
        m = row["report"]["summary"]
        delta = "" if i == 0 else f"{row['delta_accuracy']:+.1%}"
        diff = "" if i == 0 else f"  +{len(row['fixed'])}/-{len(row['broken'])}"
        lines.append(f"{row['label']:30} {m['accuracy']:6.1%} "
                     f"({m['correct']}/{m['total']})  {delta:>8}{diff}")
    for row in cmp["variants"][1:]:
        if not (row["fixed"] or row["broken"]):
            continue
        lines.append(f"\n-- {row['label']} vs 基线 --")
        for d in row["fixed"]:
            lines.append(f"  ✓修好 [{d['category']}] {d['query']}")
        for d in row["broken"]:
            lines.append(f"  ✗弄坏 [{d['category']}] {d['query']}")
    return "\n".join(lines)


__all__ = [
    "BioCase", "BioCorpus", "CATEGORIES",
    "DEFAULT_CORPUS_PATH", "CORPUS_ENV",
    "DEFAULT_ACCURACY_TARGET", "DEFAULT_MAX_FALSE_RECALL",
    "load_bio_corpus", "corpus_is_grounded", "evaluate_bio_retrieval",
    "compare_bio_settings", "format_bio_report", "format_compare_report",
    "build_real_embed_fn", "deterministic_embed",
]
