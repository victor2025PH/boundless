# -*- coding: utf-8 -*-
"""静默吞异常 ratchet 门禁（2026-08-28 建账）。

问题：全树 8,168 处 `except Exception`。宽异常本身是**刻意的设计**——「绝不阻塞
主链」是本仓写在 AGENTS 里的原则，媒体/语音/埋点等旁路失败不该拖垮回复。问题不在
catch，在**无声**：其中 2,052 处的 handler 体里既不留痕迹、也不做补偿动作，失败后
什么都没发生。故障于是只能靠用户投诉暴露。

已实锤的两种形态（都在 ai/ai_client.py）：
  - L1203 把 `notify_key_failure(...)` 整段包进 try/except pass —— **连告警调用
    自己挂掉都被吞**，那是观测链的最后一环；
  - L2038 包住 `record_error()` 埋点 —— 这类「尽力而为的埋点包装」是可辩护的。
两者混在同一个数字里，所以本门禁**不强制清理存量**，只做一件事：**不许再涨**。

口径（刻意窄，宁可漏数不可错数）：
  - 只数 `except Exception` / `except BaseException` / 裸 `except`（AST 判定，
    不做文本匹配；`except ValueError` 这类具体异常一律不数——那是精确处理）；
  - handler 体必须**全部**由 pass / continue / break / `return`(无值或 None)
    组成才算「静默」。**兜底赋值不算**：`except: _tod = 默认值` 是显式处理，不是
    吞掉（首版规则没排除它，数字虚高到 4,598，抽查后收窄至此）；
  - 体内出现任何 logger.*/print/raise/record_*/incr* 一律不算（说过话了）；
  - 只扫 `src/`（scripts/tools/tests 是一次性与夹具代码，静默在那里危害小得多）。

台账维护：清理存量后把对应模块数字改小；`test_silent_exception_ledger_not_stale`
会在实际值低于天花板时点名要求收紧，防止台账虚高吞掉倒退空间。
**未登记的模块天花板为 0**——新模块不许带着静默吞异常出生。
"""
from __future__ import annotations

import ast
import pathlib
from collections import defaultdict

_REPO = pathlib.Path(__file__).resolve().parents[1]
SRC_ROOT = _REPO / "src"

# 视为「留下痕迹」的调用名：日志 / 计数 / 告警。命中即不算静默。
_VOCAL_ATTRS = frozenset({
    "debug", "info", "warning", "warn", "error", "exception", "critical",
    "print", "capture_exception",
})

# 每个 src/<模块> 的静默处天花板（**只降不升**）。基线取自 2026-08-28 全树扫描。
_SILENT_CEILINGS: dict[str, int] = {
    "web": 575,
    "integrations": 335,
    "ai": 213,
    "inbox": 202,
    "companion": 190,
    "skills": 129,
    "utils": 124,
    "client": 119,
    "ops": 44,
    "contacts": 41,
    "eval": 21,
    "bootstrap": 11,
    "licensing": 9,
    "_root": 9,
    "trigger": 7,
    "nurture": 6,
    "monitoring": 5,
    "workspace": 4,
    "assistant": 3,
    "voicecall": 3,
    "hooks": 2,
}


def _is_broad(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return True
    names: list[str] = []
    if isinstance(t, ast.Name):
        names = [t.id]
    elif isinstance(t, ast.Tuple):
        names = [e.id for e in t.elts if isinstance(e, ast.Name)]
    return any(n in ("Exception", "BaseException") for n in names)


def _is_inert(body: list[ast.stmt]) -> bool:
    """体内只有纯放弃语句（无补偿动作）。"""
    for node in body:
        if isinstance(node, (ast.Pass, ast.Continue, ast.Break)):
            continue
        if isinstance(node, ast.Return) and (
            node.value is None
            or (isinstance(node.value, ast.Constant) and node.value.value is None)
        ):
            continue
        return False
    return True


def _is_vocal(body: list[ast.stmt]) -> bool:
    for node in body:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Raise):
                return True
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if isinstance(f, ast.Attribute):
                if f.attr in _VOCAL_ATTRS or f.attr.startswith(("record_", "incr")):
                    return True
            elif isinstance(f, ast.Name):
                if f.id in _VOCAL_ATTRS or f.id.startswith("record_") or f.id == "_metric":
                    return True
    return False


_SCAN_CACHE: dict[str, list[str]] | None = None


def _scan() -> dict[str, list[str]]:
    """→ {模块: [ "相对路径:行号", ... ]}（进程内缓存：全树 AST 约 14s，两个用例共用一次）"""
    global _SCAN_CACHE
    if _SCAN_CACHE is not None:
        return _SCAN_CACHE
    found: dict[str, list[str]] = defaultdict(list)
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            # 别人半存盘的文件不该把本门禁变红（共享工作树常态）；
            # 语法本身另有 pre-commit debug-statements / CI collect 守。
            continue
        rel = path.relative_to(SRC_ROOT)
        module = rel.parts[0] if len(rel.parts) > 1 else "_root"
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or not _is_broad(node):
                continue
            if _is_inert(node.body) and not _is_vocal(node.body):
                found[module].append(f"src/{rel.as_posix()}:{node.lineno}")
    _SCAN_CACHE = found
    return found


def test_no_new_silent_exception_handlers():
    """任何模块的静默处数量都不得超过台账天花板。"""
    found = _scan()
    over: list[str] = []
    for module, hits in sorted(found.items()):
        ceiling = _SILENT_CEILINGS.get(module, 0)
        if len(hits) > ceiling:
            # 刻意不猜「哪一处是新增的」：命中按路径排序，新增的不一定在头尾，
            # 给一个不准的指向比不给更糟。下面只给样本，定位交给 git diff。
            samples = "\n      ".join(hits[:5])
            over.append(
                f"  {module}: {len(hits)} > 天花板 {ceiling}（超 {len(hits) - ceiling}）\n"
                f"      本模块命中样本（非新增指向，定位用 `git diff`）:\n      {samples}"
            )
    assert not over, (
        "新增了静默吞异常的 handler（体内只有 pass/return，既不记日志也不兜底）。\n"
        + "\n".join(over)
        + "\n\n改法三选一：\n"
          "  1) 记一条带上下文的 logger.debug/warning（最省事，且让故障可见）；\n"
          "  2) 换成具体异常类型（`except KeyError:`）——精确处理不受本门禁约束；\n"
          "  3) 做真正的兜底赋值/补偿动作（那不算静默）。\n"
        "确因清理存量而下降 → 请同步调小 _SILENT_CEILINGS。"
    )


def test_silent_exception_ledger_not_stale():
    """实际值低于天花板时点名收紧，防台账虚高吞掉倒退空间。"""
    found = _scan()
    slack: list[str] = []
    for module, ceiling in sorted(_SILENT_CEILINGS.items()):
        actual = len(found.get(module, []))
        if actual < ceiling:
            slack.append(f"  {module}: 实际 {actual} < 天花板 {ceiling}（请下调）")
    assert not slack, (
        "静默异常台账虚高——已清理但天花板没跟着降，等于给倒退留了空间：\n"
        + "\n".join(slack)
    )


def test_ratchet_detects_a_planted_violation():
    """探测器自证：本门禁必须真的抓得到新增的静默 handler。

    2026-08-27 教训：当晚把 gitleaks 钩子搬进 CI 时，它对一对真密钥依然 Passed
    （官方入口是 `protect --staged`，CI 里暂存区为空故恒绿）。此后凡上门禁，
    必须附一个「它应该抓到」的样本证明鉴别力。
    """
    src = (
        "def f():\n"
        "    try:\n"
        "        g()\n"
        "    except Exception:\n"
        "        pass\n"
    )
    tree = ast.parse(src)
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert len(handlers) == 1
    assert _is_broad(handlers[0])
    assert _is_inert(handlers[0].body)
    assert not _is_vocal(handlers[0].body)

    # 反面：三种「不算静默」的写法都必须放行
    for ok_src in (
        "try:\n    g()\nexcept Exception:\n    logger.warning('x')\n",   # 说了话
        "try:\n    g()\nexcept Exception:\n    x = 1\n",                  # 兜底赋值
        "try:\n    g()\nexcept ValueError:\n    pass\n",                  # 具体异常
    ):
        hs = [n for n in ast.walk(ast.parse(ok_src)) if isinstance(n, ast.ExceptHandler)]
        h = hs[0]
        silent = _is_broad(h) and _is_inert(h.body) and not _is_vocal(h.body)
        assert not silent, f"误判为静默: {ok_src!r}"
