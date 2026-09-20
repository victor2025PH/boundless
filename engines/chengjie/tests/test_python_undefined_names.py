# -*- coding: utf-8 -*-
"""Python **未定义名**（pyflakes F821）棘轮门禁。

## 为什么有这条（2026-08-27 实锤事故）

``src/inbox/health_watchdog.py`` 的 ``_check_true_probes`` 里写了 ``Path(...)``，
而该模块**没有顶层 pathlib 导入**（同文件另两处同类函数都是函数内局部导入）。
这是**语法合法**的代码，于是三层防线同时失效：

  · ``gate_sweep`` step 0（``py_compile``）扫不出来——它只证明「能解析」；
  · ``NameError`` 被 ``_tick`` 外层 except 以 **DEBUG 级**吞掉；
  · 唯一信号是「``[true_probe] 轮次完成``」那行**不再出现**——需要人主动去数的负面信号。

四域真活探针从重启起整段不跑，潜伏了一整个重启周期。同一个 ``_tick`` 里还有约 20 个
``_check_*``，任何一个漏个局部 import 都会以**完全相同**的方式静默死掉。模板侧早有
内联 JS 语法门禁（一个语法错误 brick 整个收件箱），Python 侧此前没有对应物。

## 范围（刻意很窄，别扩）

只管未定义名（F821）。不引入行宽/import 排序/未使用变量等全套 lint——那会翻出上千条
既有告警、波及所有线，属另一个立项；**且假阳性会让门禁被所有人无视**。
同族的 F822（``__all__`` 死名）/F823（先用后赋）实测基线均为 0，要纳管应另立项单独跑
基线，不在这里顺手扩。

覆盖 ``src/`` ``tools/`` ``scripts/``。**tests/ 刻意不纳管**：测试里的未定义名跑一次就
大声失败，而本门禁存在的理由恰恰是「静默死在生产路径上」。（实测 tests/ 10 处/6 文件，
想纳管 ``python tools/audit_undefined_names.py --dirs tests`` 即可，成本 ~15s。）

## 棘轮语义

既有违规登记进 ``_ALLOWED``（**逐条附严重度与原因**），天花板只降不升，只禁新增：
  · 新文件出现未定义名 → 红（表里没有该文件）
  · 已登记文件出现**新的**未定义名 → 红（名字不在该文件的集合里）
  · 修好了 → ``test_allowlist_not_stale`` 点名要求回收条目，门禁强度自动恢复

站点级豁免走 ``# noqa: F821``（本仓已在用，见 ``tests/test_cockpit.py`` 的 ``_noop_auth``）
——豁免就写在出事那行旁边，review diff 时看得见，比远处的台账更好。

扫描/分类的**单一事实源**＝``tools/audit_undefined_names.py``（别在这里另写一套）。
自查：``python tools/audit_undefined_names.py``（``--strict`` 有命中即退出码 1）。
"""
import sys
import textwrap
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.audit_undefined_names import (  # noqa: E402
    DEFAULT_DIRS,
    ENGINE_ROOT,
    pyflakes_version,
    scan,
    scan_file,
)

pytestmark = pytest.mark.skipif(
    pyflakes_version() is None,
    reason="pyflakes 未安装（pip install pyflakes）——工具缺失一律 SKIP，不污染回归信号",
)

# 严重度标签：两类命中的后果完全不同，台账必须分开，别当成同一回事。
_REAL = "REAL"          # 运行到该行必抛 NameError；本仓高发形态＝被外层 except 吞掉 → 功能静默死亡
_ANNOT = "annotation"   # 只在不会被求值的注解位（future annotations / 字符串注解 /
                        # 函数内局部变量注解——后者 Python 从不求值）。不会崩，但仍是死名字。

#: 棘轮天平（只降不升）。格式：路径 → (名字集合, 严重度, 原因)。
#:
#: 2026-08-27 立项基线：src/ 23 处 / 12 文件，**tools/ 与 scripts/ 均为 0**
#: （后两者没有条目 = 任何新增立刻红）。基线里 6 个文件是 REAL（运行到就 NameError，
#: 且全被外层 except 吞掉 → 功能静默死亡），全部是本门禁**首跑当天捞出的既有线上缺陷**。
#:
#: 2026-08-27 同日收口：6 个 REAL 已全部修复并回收条目 → **23 处 / 12 文件 降到
#: 13 处 / 6 文件**，剩下的全是注解位死名字（不会崩）。已修清单留档于本注释，别再翻历史：
#:   translation_service(logger) / whatsapp_rpa.service(json) / whatsapp_rpa_routes(json)
#:   / tenant_lifecycle(logging)  —— 补顶层 import
#:   skill_manager(sid)           —— L3559 编码损坏（PUA + 吃掉换行，把
#:                                   `sid = s["strategy_id"]` 折进乱码注释）→ 重建该行
#:   messenger_rpa_routes(request)—— 嵌套 helper 误取 `request.app`，改用闭包里本就有的 `app`
_ALLOWED: Dict[str, Tuple[FrozenSet[str], str, str]] = {
    # ---- annotation：不会崩（注解位不求值），但仍是写了个不存在的名字 -----------
    "src/ai/tts_pipeline.py": (
        frozenset({"List"}), _ANNOT,
        "L546 返回注解 -> List[str]；模块有 from __future__ import annotations（注解即字符串，不求值）。",
    ),
    "src/integrations/messenger_rpa/runner.py": (
        frozenset({"deque", "OrderedDict"}), _ANNOT,
        "L514/536/553：属性赋值上的字符串注解（Dict[str, \"deque[str]\"] 等）；"
        "属性目标的注解连存都不存，更不求值。",
    ),
    "src/integrations/platform_login.py": (
        frozenset({"List"}), _ANNOT,
        "L606/640/675 返回注解 -> List[...]；模块有 future annotations。",
    ),
    "src/integrations/whatsapp_rpa/runner.py": (
        frozenset({"MsgGroup"}), _ANNOT,
        "L2431 形参字符串注解 grp: \"MsgGroup\"。",
    ),
    "src/web/routes/companion_capability_routes.py": (
        frozenset({"Optional"}), _ANNOT,
        "L264 返回注解 -> Optional[bool]；模块有 future annotations。",
    ),
    "src/web/routes/drafts_routes.py": (
        frozenset({"Any", "Dict"}), _ANNOT,
        "L1286/1393 函数内局部变量注解（_osnap: Dict[str, Any] = ...）；"
        "Python 从不求值函数局部变量注解。",
    ),
}


@lru_cache(maxsize=1)
def _scan():
    """全量扫描一次，同 worker 内各条测试共用（xdist 下每 worker 各扫一次）。"""
    return scan(DEFAULT_DIRS, ENGINE_ROOT)


def _fmt(entries) -> str:
    return "\n".join(f"    {p}: {', '.join(sorted(n))}" for p, n in sorted(entries.items()))


# ─────────────────────────── 棘轮本体 ───────────────────────────

def test_no_new_undefined_names():
    """新增未定义名一律红。既有的登记在 _ALLOWED，天花板只降不升。"""
    actual = _scan().by_file()
    new = {}
    for path, names in actual.items():
        allowed = _ALLOWED.get(path, (frozenset(),))[0]
        extra = set(names) - set(allowed)
        if extra:
            new[path] = extra
    assert not new, (
        "发现**新增的未定义名**（语法合法，py_compile 扫不出；运行到就 NameError，"
        "本仓高发形态是被外层 except 吞掉 → 功能静默死亡，正是 2026-08-27 真活探针事故的机制）：\n"
        + _fmt(new)
        + "\n\n修法：补顶层 import，或按本仓惯例在函数内局部导入"
          "（如 `from pathlib import Path as _TPPath`）；"
          "\n确属注解位/刻意为之 → 该行加 `# noqa: F821` 并写清理由，或登记进 _ALLOWED。"
          "\n自查：python tools/audit_undefined_names.py"
    )


def test_tools_and_scripts_stay_clean():
    """tools/ 与 scripts/ 的基线是 **0**（2026-08-27 实测）——这条把「零」显式钉住，
    失败消息直接说清「这两个目录不该有任何一处」，而不是让人去 _ALLOWED 里对表。"""
    dirty = {p: n for p, n in _scan().by_file().items()
             if p.startswith(("tools/", "scripts/"))}
    assert not dirty, (
        "tools/ 与 scripts/ 的未定义名基线是 0，现在不是了：\n" + _fmt(dirty)
        + "\n这两个目录多为一次性/低频执行的脚本，未定义名往往要等真跑那天才炸。"
    )


def test_allowlist_not_stale():
    """_ALLOWED 里的名字须仍在报；已修好的要回收条目，否则门禁强度被悄悄稀释。"""
    actual = _scan().by_file()
    stale = {}
    for path, (names, _sev, _why) in _ALLOWED.items():
        if not (ENGINE_ROOT / path).exists():
            stale[path] = set(names) | {"<文件已不存在>"}
            continue
        gone = set(names) - set(actual.get(path, set()))
        if gone:
            stale[path] = gone
    assert not stale, (
        "以下条目已不再命中（很可能已被修好），请从 _ALLOWED 移除以恢复门禁强度：\n"
        + _fmt(stale)
    )


def test_allowlist_entries_are_documented():
    """每条登记必须带严重度 + 说清「为什么现在还留着」——没有理由的白名单会永久化。"""
    bad = [p for p, (names, sev, why) in _ALLOWED.items()
           if sev not in (_REAL, _ANNOT) or len(why.strip()) < 30 or not names]
    assert not bad, f"_ALLOWED 条目缺严重度/理由过短/名字为空：{bad}"


# ──────────────────── 自证：门禁真能抓到今天那个 bug ────────────────────
#
# 不做这段等于没做门禁。参考本仓先例：tests/test_bazi_chart_eval.py（篡改金标必 FAIL）、
# tests/test_no_fallback_discipline.py::test_asr_expect_tokens_calibrated_against_known_bad_sample。

# 2026-08-27 事故代码的**真实形态**：模块顶层只导 asyncio/logging/time/typing，
# 没有 pathlib；方法体里直接 Path(...)。
_INCIDENT_BAD = '''\
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional


class HealthWatchdog:
    def _check_true_probes(self, *, now: Optional[float] = None) -> None:
        _tp_dir = Path(str(getattr(self._config_manager, "config_path", "")
                           or "config/config.yaml")).parent
        return _tp_dir
'''

# 修复后的形态（= 本仓合法写法，也是同文件另两处同类函数一直在用的写法）：函数内局部导入，
# 且带别名。误报它就等于逼着所有人改代码 —— 那样门禁会被无视。
_INCIDENT_FIXED = _INCIDENT_BAD.replace(
    "        _tp_dir = Path(",
    "        from pathlib import Path as _TPPath  # 本模块无顶层 pathlib\n"
    "        _tp_dir = _TPPath(",
)


def _names_in(tmp_path: Path, source: str, fname: str = "m.py"):
    f = tmp_path / fname
    f.write_text(textwrap.dedent(source), encoding="utf-8")
    findings, unparseable = scan_file(f, tmp_path)
    assert unparseable is None, f"样本应可解析，实际: {unparseable}"
    return sorted(x.name for x in findings)


def test_catches_the_2026_08_27_incident(tmp_path):
    """事故复刻：函数里用了 Path，模块无 pathlib 导入 → 必须报出来。"""
    assert _names_in(tmp_path, _INCIDENT_BAD) == ["Path"]


def test_function_local_import_is_not_flagged(tmp_path):
    """修复形态（函数内 `from pathlib import Path as _TPPath`）→ 必须零命中。

    这是本仓的合法写法（health_watchdog 同文件另两处同类函数就是这么写的）。
    误报它会逼所有人改代码，而**被无视的门禁等于没有门禁**。
    """
    assert _names_in(tmp_path, _INCIDENT_FIXED) == []


def test_plain_function_local_import_is_not_flagged(tmp_path):
    """不带别名的函数内局部导入同样不许报（_check_identity_shadow 的写法）。"""
    src = '''\
    import time

    def f(cfg_path):
        from pathlib import Path
        return Path(cfg_path).parent
    '''
    assert _names_in(tmp_path, src) == []


def test_legit_scoping_idioms_are_not_flagged(tmp_path):
    """作用域地雷区回归钉：comprehension / walrus / global / nonlocal / 类体 /
    嵌套闭包 / try-except 条件导入 / TYPE_CHECKING / import * ——全都不许误报。

    这批正是「手写 AST 检查器」最容易翻车的地方；有它们兜底，将来若换实现能立刻发现退化。
    """
    src = '''\
    from __future__ import annotations

    from typing import TYPE_CHECKING

    from os.path import *          # noqa: F403 - 星号导入：pyflakes 会放弃未定义名判定

    if TYPE_CHECKING:
        from decimal import Decimal

    try:
        import ujson as _json
    except ImportError:
        import json as _json

    _G = 1

    def uses_type_checking_only(x: "Decimal") -> "Decimal":
        return x

    def comprehensions(rows):
        squares = [r * r for r in rows]
        lookup = {k: v for k, v in rows}
        gen = (y for y in squares)
        return squares, lookup, list(gen), {s for s in squares}

    def walrus(rows):
        if (n := len(rows)) > 3:
            return n
        return 0

    def globals_and_nonlocals():
        global _G
        _G = 2
        total = 0

        def inner():
            nonlocal total
            total += 1
            return total

        inner()
        return total, _json.dumps({}), basename("a/b")

    class K:
        FIELD = 3
        DERIVED = FIELD + 1

        def m(self):
            return self.DERIVED
    '''
    assert _names_in(tmp_path, src) == []


def test_noqa_suppresses_at_the_site(tmp_path):
    """站点级豁免：`# noqa: F821` 与裸 `# noqa` 都抑制；无关码（F401）不抑制。"""
    assert _names_in(tmp_path, "def f():\n    return Ghost()  # noqa: F821\n") == []
    assert _names_in(tmp_path, "def f():\n    return Ghost()  # noqa\n") == []
    assert _names_in(tmp_path, "def f():\n    return Ghost()  # noqa: F401\n") == ["Ghost"]


def test_unparseable_file_never_counts_as_a_finding(tmp_path):
    """共享工作树上 sibling 半存盘是常态（gate_sweep step 0 因此刻意只 warn）。
    把「此刻正在存盘」渲染成红灯只会制造瞬态红——语法坏的文件必须只报不算。"""
    f = tmp_path / "half_saved.py"
    f.write_text("def broken(:\n    Ghost()\n", encoding="utf-8")
    findings, unparseable = scan_file(f, tmp_path)
    assert findings == []
    assert unparseable, "语法错误应被如实记录到 unparseable，而不是静默丢弃"


def test_annotation_only_forms_are_still_reported(tmp_path):
    """注解位的死名字不会崩，但仍要报（台账按严重度区分，不是不报）——
    否则「注解里写个不存在的类型」会永久无人知晓。"""
    src = '''\
    from __future__ import annotations

    def f(x: "Nope") -> "AlsoNope":
        return x
    '''
    assert _names_in(tmp_path, src) == ["AlsoNope", "Nope"]


def test_scanner_reaches_real_tree_and_reports_relative_posix_paths():
    """扫描器自证没有空跑：真扫到了上千个文件，且路径键是相对 posix（台账跨平台稳定）。"""
    res = _scan()
    assert res.files_scanned > 800, f"只扫到 {res.files_scanned} 个文件，疑似目录/过滤写错"
    for p in res.by_file():
        assert not p.startswith(("/", "\\")) and ":" not in p and "\\" not in p, p


def test_catches_the_incident_in_the_real_file(tmp_path):
    """比合成样本更硬的一条：拿**事故本尊** ``src/inbox/health_watchdog.py`` 当底本。

    该文件 6000+ 行、满是嵌套作用域/装饰器/async，正是「手写检查器会翻车、合成小样本
    证明不了」的真实规模。做法：抄一份到 tmp（**绝不动真文件**），删掉其中一条函数内
    局部 pathlib 导入 —— 这正是 2026-08-27 的事故形态 —— 断言门禁把那个名字报出来；
    同时断言**原样的文件零命中**（否则就是把已修好的代码又误报成 bug）。
    """
    import re

    real = ENGINE_ROOT / "src" / "inbox" / "health_watchdog.py"
    if not real.exists():
        pytest.skip("health_watchdog.py 不在（重构过？）——这条自证依赖事故本尊做底本")
    text = real.read_text(encoding="utf-8")

    # 函数内局部 pathlib 导入（缩进开头）= 本仓合法写法，也是当日的修复形态
    local_imports = [ln for ln in text.splitlines()
                     if re.match(r"[ \t]+from pathlib import Path\b", ln)]
    if not local_imports:
        pytest.skip("该文件已无函数内局部 pathlib 导入（改顶层导入了？）——事故形态不复存在")

    clean = tmp_path / "hw_asis.py"
    clean.write_text(text, encoding="utf-8")
    findings, unparseable = scan_file(clean, tmp_path)
    assert unparseable is None, f"事故本尊应可解析: {unparseable}"
    assert findings == [], (
        "事故本尊**当前**应零命中（当日已修）。现在报了，说明要么真的又漏了导入，"
        f"要么扫描器误报了本仓合法写法：{[str(f) for f in findings]}"
    )

    # 抽掉那条局部导入 → 该名字在函数体里就没有任何绑定了 = 事故当日的状态
    broken = tmp_path / "hw_broken.py"
    broken.write_text(text.replace(local_imports[0] + "\n", "", 1), encoding="utf-8")
    names = {f.name for f in scan_file(broken, tmp_path)[0]}
    # 先剥行尾注释再取绑定名：`from pathlib import Path as _TPPath  # 本模块无顶层…` → _TPPath
    code = local_imports[0].split("#", 1)[0].strip()
    bound_name = code.split()[-1]
    assert bound_name in names, (
        f"删掉唯一的绑定 `{code}` 后，门禁竟然没报 {bound_name!r} —— "
        f"这就是 2026-08-27 事故的形态，抓不到它这条门禁就没有意义。实际报出: {sorted(names)}"
    )
