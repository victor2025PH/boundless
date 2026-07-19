# -*- coding: utf-8 -*-
"""能力宣称 lint（Phase 12-E）：capability_matrix.json ↔ 仓库真实代码 的一致性门禁。

背景：官网/使用说明/销售一页纸各写一份能力描述，产品演进后互相打架、售前过度承诺
（「无痕/看不出/32FPS 一刀切」）是差评与交付纠纷的源头。本工具把「文案与能力对齐」
从人肉核对变成机器门禁：

  1) 证据核验：每条 claim 的 evidence(file+needle) 必须真实存在——宣称的能力没有
     代码背书 = 红灯（新卖点先登记证据再写文案）。
  2) 措辞禁区：营销面（使用说明/销售一页纸/首页功能注册表）不得出现 banned_phrases
     （无痕/无审查/自动成交…——官网对齐方案 §2 的机器化版本）。
  3) lab 条目审计：status=lab 的能力，claim 文本必须自带「离线」或「实验室」字样，
     防止实验室功能被写成实时能力。
  4) gated-model 令牌扫描：无审查模型令牌 (gemma4-uncensored) 只许出现在注释行
     或 *.example* 覆盖文件里；出现在任何「激活」配置面（非 example、非注释）= 红灯。

用法：python tools/claims_lint.py            退出码 0=通过 / 1=违规 / 2=自身故障
      from tools 导入 check() 供 test_phase12 门禁调用。
"""
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
MATRIX = BASE / "capability_matrix.json"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _surface_text(surf: dict) -> tuple:
    """取营销面文本：整文件，或指定 block（如 avatar_hub.py 的 FEATURE_REGISTRY 列表）。
    返回 (label, text)；文件缺失返回 (label, None)。"""
    f = BASE / surf["file"]
    label = surf["file"] + (f"::{surf['block']}" if surf.get("block") else "")
    if not f.exists():
        return label, None
    text = _read(f)
    if surf.get("block") == "FEATURE_REGISTRY":
        m = re.search(r"FEATURE_REGISTRY\s*=\s*\[(.*?)\n\]", text, re.S)
        text = m.group(1) if m else ""
    return label, text


# ── 4) gated-model 令牌扫描（compliance）────────────────────────────
# 无审查模型 (gemma4-uncensored) 是 gated-only：默认禁用，仅准入客户经
# HUOKE_ALLOW_UNCENSORED=1 + 单独 overlay 启用。因此该令牌只允许出现在
#   ① 注释行（解释 gated-only 政策），或 ② 文件名含 ".example" 的覆盖示例。
# 出现在任何「激活」配置面（非 example、非注释的 yaml/json）= 红灯。
# 扫描面：本引擎根目录顶层配置 + 兄弟引擎 huoke 的 config/（该模型路由配置所在地）。
# 扩展点：其他引擎的 config 目录加进 _gated_token_scan_paths() 的 dirs 即可。
_GATED_MODEL_TOKEN = "gemma4-uncensored"
_CONFIG_SUFFIXES = (".yaml", ".yml", ".json")


def _gated_token_scan_paths():
    dirs = [BASE, BASE.parent / "huoke" / "config"]
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if (p.is_file() and p.suffix.lower() in _CONFIG_SUFFIXES
                    and ".example" not in p.name.lower()):
                yield p


def check_gated_model_tokens() -> tuple:
    """激活配置面禁止裸露无审查模型令牌（注释行豁免）。返回 (violations, oks)。"""
    violations, oks = [], []
    scanned = 0
    for p in _gated_token_scan_paths():
        scanned += 1
        text = _read(p)
        if _GATED_MODEL_TOKEN not in text:
            continue
        naked = []
        for i, ln in enumerate(text.splitlines(), 1):
            if _GATED_MODEL_TOKEN not in ln:
                continue
            # yaml 注释豁免：令牌之前已出现 "#" 视为注释语境（json 无注释，必中）
            if "#" not in ln.split(_GATED_MODEL_TOKEN, 1)[0]:
                naked.append(i)
        if naked:
            violations.append(
                f"激活配置 {p.relative_to(BASE.parent)} 出现未门禁的无审查模型令牌"
                f"「{_GATED_MODEL_TOKEN}」(行 {naked}) —— 该模型仅限 gated overlay"
                f" + HUOKE_ALLOW_UNCENSORED=1 启用")
    if not violations:
        oks.append(f"gated-model 令牌扫描: {scanned} 个激活配置面无裸露「{_GATED_MODEL_TOKEN}」")
    return violations, oks


def check() -> tuple:
    """返回 (violations, oks)：violations 非空即门禁红灯。"""
    cm = json.loads(MATRIX.read_text(encoding="utf-8-sig"))
    violations, oks = [], []

    # 1) 证据核验
    for c in cm.get("claims", []):
        cid, status = c.get("id", "?"), c.get("status", "full")
        if status == "external":
            if c.get("evidence"):
                violations.append(f"{cid}: external 条目不应携带引擎证据（独立产品线勿混述）")
            else:
                oks.append(f"{cid}: external（引擎不背书,仅登记口径）")
            continue
        evs = c.get("evidence") or []
        if not evs:
            violations.append(f"{cid}: 无任何代码证据——宣称能力必须有 evidence")
            continue
        bad = []
        for ev in evs:
            f = BASE / ev["file"]
            if not f.exists():
                bad.append(f"{ev['file']} 不存在")
            elif ev["needle"].lower() not in _read(f).lower():
                bad.append(f"{ev['file']} 中找不到「{ev['needle']}」")
        if bad:
            violations.append(f"{cid}: 证据失效 → {'; '.join(bad)}")
        else:
            oks.append(f"{cid}: {len(evs)} 条证据在位")
        # 3) lab 条目措辞审计
        if status == "lab" and not any(w in c.get("claim", "") for w in ("离线", "实验室")):
            violations.append(f"{cid}: lab 条目 claim 未标注「离线/实验室」，会被读成实时能力")

    # 2) 营销面措辞禁区（负面清单语境豁免：出现在「不承诺/红线/禁止」等否定句里的禁用词
    #    是护栏文案而非承诺——销售一页纸的「不承诺的事」清单必须能点名这些词）
    _NEG = re.compile(r"不承诺|勿写|不得|禁止|红线|❌|避免|不做|不能|另页|独立产品线|物理分页|勿与|删除/降级")
    banned = cm.get("banned_phrases") or []
    for surf in cm.get("marketing_surfaces", []):
        label, text = _surface_text(surf)
        if text is None:
            violations.append(f"营销面缺失: {label}")
            continue
        bad_hits, guard_hits = [], []
        for p in banned:
            if p not in text:
                continue
            lines = [(i + 1, ln) for i, ln in enumerate(text.splitlines()) if p in ln]
            naked = [i for i, ln in lines if not _NEG.search(ln)]
            if naked:
                bad_hits.append(f"「{p}」(行 {naked})")
            else:
                guard_hits.append(p)
        if bad_hits:
            violations.append(f"营销面 {label} 出现肯定语境禁用措辞: {'; '.join(bad_hits)}")
        else:
            extra = f"（护栏语境提及: {guard_hits}）" if guard_hits else ""
            oks.append(f"营销面 {label} 无过度承诺措辞{extra}")

    # 4) gated-model 令牌扫描（激活配置面不得裸露无审查模型令牌）
    v_tok, ok_tok = check_gated_model_tokens()
    violations.extend(v_tok)
    oks.extend(ok_tok)
    return violations, oks


def main() -> int:
    try:
        violations, oks = check()
    except Exception as e:
        print(f"claims_lint 自身故障: {e}")
        return 2
    for line in oks:
        print(f"  一致: {line}")
    for line in violations:
        print(f"  违规: {line}")
    if violations:
        print(f"结论: 违规 {len(violations)} 处（文案与能力不对齐,先修再发）")
        return 1
    print(f"结论: 对齐（{len(oks)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
