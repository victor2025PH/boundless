# -*- coding: utf-8 -*-
"""夹具时间炸弹门禁（2026-08-12）：硬编码日历日期不得写进 *trend* 数据夹具。

事故形态（本仓已实爆两次，均为「红了没人认领」的无主红）：
- duel_bench（2026-08-11 爆）：``test_build_status_with_artifacts`` 夹具往
  ``duel_trend.jsonl`` 写死 ``"ts": "2026-07-27T…"``，而消费方
  ``read_trend(days=14)`` 以 **now 为基准**截窗——日历走到 08-11 夹具整体
  出窗，``points==0`` 自爆。与任何人的改动都无关，所以连续两天被各线
  归为「other owners」无人修。
- inbox filter P4（2026-08-11 险爆，其线自修）：观察窗判定依赖
  「数据首见时间」，同样是「夹具日期 × now 窗」组合，教训记为
  「time-bomb-free anchored now」。

不变量（刻意窄——全库 7 个含硬日期 ts 的测试文件里只有 trend 家族是
「必被 now 窗消费」的，其余（audit 迁移/lead envelope/meta 直读/记忆
recency 权重）无窗无害，宽口径会 6 处全误报）：

    测试代码把含硬编码日历日期（``20XX-XX-XX``+时刻）的内容
    写进路径/语句含 ``trend`` 的数据文件 —— 禁止；夹具日期一律
    锚 ``datetime.now()`` 相对值（见 test_duel_bench_status 修复样板）。

扫描器：``tokenize`` 剥注释（否则修复注释里的「2026-07-27…trend」考古
说明会自误报）、字符串掩码算括号深度（数据 JSON 里的括号不得干扰语句
边界）；命中块＝``.write_text(``/``.write_bytes(`` 调用行首至括号闭合。
``_ALLOWLIST`` 登记「确认无窗且不宜改」的例外（附原因），当前空表；
``test_allowlist_not_stale`` 防过期；``test_scanner_catches_known_bomb``
用 duel 事故原始片段自证探测器有效（篡改扫描器即红）。
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
_SELF = Path(__file__).name

# 日历日期 + 时刻（"2026-07-28T02:10:00" / "2026-01-01 10:00:00"）。
# 只带日期不带时刻的字面量（生日金标、命理锚点）不在口径内。
_DATE_RE = re.compile(r"20\d\d-\d\d-\d\d[T ]\d\d:")
_WRITE_RE = re.compile(r"\.write_(?:text|bytes)\s*\(")
_TREND_RE = re.compile(r"trend", re.IGNORECASE)


def _strip_comments(src: str) -> str:
    """注释 → 等长空白（tokenize 精确口径；字符串/代码原样保留）。"""
    out = list(src)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                lines = src.splitlines(keepends=True)
                base = sum(len(ln) for ln in lines[: tok.start[0] - 1])
                for i in range(base + tok.start[1], base + tok.end[1]):
                    if out[i] not in "\r\n":
                        out[i] = " "
    except (tokenize.TokenError, IndentationError):
        return src  # 语法异常的文件交给别的门禁，这里不误判
    return "".join(out)


def _mask_strings(src: str) -> str:
    """字符串字面量内容 → 等长空白（只用于括号深度，判定仍看原文）。"""
    out = list(src)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.STRING:
                lines = src.splitlines(keepends=True)
                base = sum(len(ln) for ln in lines[: tok.start[0] - 1])
                for i in range(base + tok.start[1], base + tok.end[1]):
                    if out[i] not in "\r\n":
                        out[i] = " "
    except (tokenize.TokenError, IndentationError):
        return src
    return "".join(out)


def scan_source(src: str) -> list[tuple[int, str]]:
    """返回 [(行号, 片段)] ＝「写 trend 数据文件且含硬日历日期」的语句块。"""
    text = _strip_comments(src)
    masked = _mask_strings(text)
    hits: list[tuple[int, str]] = []
    for m in _WRITE_RE.finditer(text):
        # 语句块：调用所在行行首 → 参数括号平衡闭合（深度在字符串掩码上算）
        start = text.rfind("\n", 0, m.start()) + 1
        open_pos = text.index("(", m.end() - 1)
        depth, end = 0, len(text)
        for i in range(open_pos, len(text)):
            ch = masked[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        block = text[start:end]
        if _TREND_RE.search(block) and _DATE_RE.search(block):
            lineno = text[: m.start()].count("\n") + 1
            hits.append((lineno, " ".join(block.split())[:160]))
    return hits


# (相对文件名, 片段特征子串) -> 保留原因。仅收「确认消费方无 now 窗且
# 不宜改锚 now」的例外——能改的一律改（duel/semantic 两处已锚 now）。
_ALLOWLIST: dict[tuple[str, str], str] = {}


def test_no_hardcoded_dates_into_trend_fixtures():
    offenders = []
    for p in sorted(TESTS_ROOT.glob("*.py")):
        if p.name == _SELF:
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, snippet in scan_source(src):
            allowed = any(k[0] == p.name and k[1] in snippet
                          for k in _ALLOWLIST)
            if not allowed:
                offenders.append(f"{p.name}:{lineno}  {snippet}")
    assert not offenders, (
        "测试夹具把硬编码日历日期写进了 trend 数据文件——消费方按 now 截窗，"
        "日历走到即自爆（duel_bench 2026-08-11 实锤）。夹具日期请锚 "
        "datetime.now() 相对值（样板见 test_duel_bench_status."
        "test_build_status_with_artifacts）：\n  " + "\n  ".join(offenders))


def test_scanner_catches_known_bomb():
    """探测器有效性自证：duel 事故原始形态必命中；锚 now 修复形态必不命中。"""
    bomb = (
        "def test_x(tmp_path):\n"
        "    (duel / \"duel_trend.jsonl\").write_text(\n"
        "        '{\"ts\":\"2026-07-27T02:10:00\",\"defects_per_100_turns\":10}\\n'\n"
        "        '{\"ts\":\"2026-07-28T02:10:00\",\"defects_per_100_turns\":5}\\n',\n"
        "        encoding=\"utf-8\")\n"
    )
    assert scan_source(bomb), "扫描器必须抓住 duel 事故原始形态"

    fixed = (
        "def test_x(tmp_path):\n"
        "    d0 = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%dT02:10:00')\n"
        "    (duel / \"duel_trend.jsonl\").write_text(\n"
        "        json.dumps({\"ts\": d0, \"defects_per_100_turns\": 10}) + \"\\n\",\n"
        "        encoding=\"utf-8\")\n"
    )
    assert not scan_source(fixed), "锚 now 的修复形态不得误报"

    # 非 trend 数据文件里的硬日期（audit 迁移/lead envelope 类）：口径外不误伤
    benign = (
        "def test_y(tmp_path):\n"
        "    (tmp_path / \"audit_log.jsonl\").write_text(\n"
        "        '{\"ts\": \"2026-01-01 10:00:00\", \"user\": \"admin\"}\\n',\n"
        "        encoding=\"utf-8\")\n"
    )
    assert not scan_source(benign), "无窗消费的非 trend 夹具不得误报"

    # 注释里的考古说明（修复注释常含原始日期与 trend 字样）不得误报
    commented = (
        "def test_z(tmp_path):\n"
        "    # 原硬编码 2026-07-27T02:10:00 写进 duel_trend.jsonl 会出窗\n"
        "    (duel / \"duel_trend.jsonl\").write_text(\n"
        "        json.dumps({\"ts\": d0}) + \"\\n\", encoding=\"utf-8\")\n"
    )
    assert not scan_source(commented), "注释里的日期/trend 字样不得误报"


def test_allowlist_not_stale():
    """白名单条目必须仍然命中扫描器（否则=已修复的死条目，删掉防口径虚化）。"""
    if not _ALLOWLIST:
        return
    for (fname, feature), reason in _ALLOWLIST.items():
        p = TESTS_ROOT / fname
        assert p.is_file(), f"白名单指向不存在的文件 {fname}（{reason}）"
        hits = scan_source(p.read_text(encoding="utf-8"))
        assert any(feature in s for _, s in hits), (
            f"白名单条目已不再命中（该处大概已修复）——删掉这行：{fname} / "
            f"{feature}（{reason}）")
