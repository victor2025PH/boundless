# -*- coding: utf-8 -*-
"""审计写入点静态扫描共享核心（2026-08-18 自 test_audit_action_labels.py 抽出）。

消费方：
- ``test_audit_action_labels.py``：每个字面 action 必须有 aud_act_* 双语词条；
- ``test_audit_display.py``：写入点 target/new_val 静态段里的 kv 键（``pack=`` /
  ``packs+``）必须有 aud_tgt_* 双语词条——P2 的 ``parse_object_tokens`` 只在
  展示层拆词，键没登记＝徽章回落英文原 key，这里在 CI 期点名。

核心语义（与 test_audit_action_labels 的 AST 化一脉相承）：
- 「审计包装」＝把某形参转发进 ``*.log()`` 的 action 槽位的函数（形参名叫
  action，或 receiver 名含 audit——防 ``logger.log(level, msg)`` 误认）；
- 包装定义处同时记下「哪些形参落进 .log 的 target/old_val/new_val 槽」，
  调用点按位置/关键字双通道解析实参 → 提取字面量与 f-string 的静态段。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# ── ① 直调字面量（正则）──────────────────────────────────────────────
# receiver 形如 audit / audit_store / ctx.audit_store / self._audit 等；
# 第一参允许一层括号嵌套（session.get("username", "web_admin") 这类）。
_LOG_CALL = re.compile(
    r"""(?:\baudit\w*|_audit)\s*\.log\(\s*
        (?:[^,()'"]|\([^()]*\)|'[^']*'|"[^"]*")+?
        ,\s*['"]([a-z0-9_]+)['"]""",
    re.VERBOSE,
)
_ACTION_FMT = re.compile(r"[a-z0-9_]+$")

# AuditStore.log 契约槽位：log(user_id, action, target='', old_val='', new_val='')
_LOG_VALUE_SLOTS = {2: "target", 3: "old_val", 4: "new_val"}

_TREES = None


def _trees():
    """全库 (path, text, ast) 一次解析进程级缓存（多个门禁文件共用）。"""
    global _TREES
    if _TREES is None:
        out = []
        for p in sorted(SRC.rglob("*.py")):
            text = p.read_text(encoding="utf-8", errors="ignore")
            try:
                out.append((p, text, ast.parse(text)))
            except SyntaxError:
                continue
        _TREES = out
    return _TREES


# ── ② 审计包装的 AST 识别 ────────────────────────────────────────────

def _log_action_forward(call: ast.Call):
    """若 ``*.log(...)`` 调用的 action 槽位（位置 1 或 ``action=`` 关键字）
    收的是一个裸变量名，返回 (变量名, receiver 源码)；否则 None。"""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "log"):
        return None
    cand = None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Name):
        cand = call.args[1].id
    for kw in call.keywords:
        if kw.arg == "action" and isinstance(kw.value, ast.Name):
            cand = kw.value.id
    if not cand:
        return None
    try:
        recv = ast.unparse(call.func.value)
    except Exception:
        recv = ""
    return cand, recv


def _log_value_param_slots(call: ast.Call, params) -> dict:
    """``*.log(...)`` 里落进 target/old_val/new_val 槽位的**本函数形参** →
    ``{形参名: 槽名}``（位置实参按槽位序，关键字按名）。"""
    out = {}
    for i, a in enumerate(call.args):
        if i in _LOG_VALUE_SLOTS and isinstance(a, ast.Name) and a.id in params:
            out[a.id] = _LOG_VALUE_SLOTS[i]
    for kw in call.keywords:
        if kw.arg in ("target", "old_val", "new_val") \
                and isinstance(kw.value, ast.Name) and kw.value.id in params:
            out[kw.value.id] = kw.arg
    return out


def _wrapper_spec(fn):
    """函数把某形参转发进 *.log() 的 action 槽位 →
    (action 形参位置, 是否方法, 形参名列表, {值形参名: 槽名})。

    额外要求「形参名就叫 action」或「receiver 名含 audit」——防止
    ``logger.log(level, msg)`` 这类日志包装被误认成审计包装。
    """
    params = [a.arg for a in fn.args.args]
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        fwd = _log_action_forward(n)
        if not fwd:
            continue
        cand, recv = fwd
        if cand in params and (cand == "action" or "audit" in recv.lower()):
            is_method = bool(params) and params[0] in ("self", "cls")
            return (params.index(cand), is_method, params,
                    _log_value_param_slots(n, set(params)))
    return None


def _wrapper_actions_from(tree):
    """单文件：包装定义 → 其调用点提取。返回 (字面 action 集, 动态调用点列表)。

    动态调用点＝action 实参不是字面量：f-string 记静态前缀（对台账），
    裸变量/表达式记空前缀（一律要求登记，宁严勿漏）。
    """
    specs = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spec = _wrapper_spec(node)
            if spec:
                specs[node.name] = spec
    lits, dyns = set(), []
    if not specs:
        return lits, dyns
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        if isinstance(n.func, ast.Name):
            name, attr_call = n.func.id, False
        elif isinstance(n.func, ast.Attribute):
            name, attr_call = n.func.attr, True
        else:
            continue
        if name not in specs:
            continue
        pos, is_method, _params, _vals = specs[name]
        # 绑定方法调用（self.x(...)）时实参里没有 self，槽位左移一格
        idx = pos - 1 if (is_method and attr_call) else pos
        if idx < 0 or idx >= len(n.args):
            continue
        a = n.args[idx]
        if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                and _ACTION_FMT.match(a.value):
            lits.add(a.value)
        elif isinstance(a, ast.JoinedStr):
            prefix = ""
            if a.values and isinstance(a.values[0], ast.Constant):
                prefix = str(a.values[0].value)
            dyns.append((name, prefix))
        elif not isinstance(a, ast.Constant):
            dyns.append((name, ""))
    return lits, dyns


def _collect_wrapper_actions():
    """全库包装调用点：(action → {文件}, [(文件, 包装名, 静态前缀)])。"""
    lits, dyns = {}, []
    for p, _text, tree in _trees():
        rel = str(p.relative_to(SRC.parent))
        file_lits, file_dyns = _wrapper_actions_from(tree)
        for a in file_lits:
            lits.setdefault(a, set()).add(rel)
        dyns.extend((rel, name, prefix) for name, prefix in file_dyns)
    return lits, dyns


def _collect_literal_actions():
    """① 正则直调 + ② AST 包装调用点，合并为 {action: {文件}}。"""
    found = {}
    for p, text, _tree in _trees():
        rel = str(p.relative_to(SRC.parent))
        for m in _LOG_CALL.finditer(text):
            found.setdefault(m.group(1), set()).add(rel)
    wrapper_lits, _ = _collect_wrapper_actions()
    for a, files in wrapper_lits.items():
        found.setdefault(a, set()).update(files)
    return found


# ── ③ 写入点 target/old_val/new_val 实参的静态文本段 ────────────────

def _static_segments(node):
    """字符串实参的静态文本段：Constant → [全文]；f-string → [各 Constant 段]；
    其余（裸变量/表达式）→ []（动态内容静态扫不到，属预期盲区）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)]
    return []


def _action_hint(node) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values \
            and isinstance(node.values[0], ast.Constant):
        return str(node.values[0].value)
    return ""


def collect_audit_value_segments(slots=("target", "old_val", "new_val")):
    """全库审计写入点值槽实参的静态文本段：[(rel文件, action提示, 槽名, 段)]。

    覆盖两类写入点：① 包装调用点（槽位映射取自包装定义，位置/关键字实参
    双通道解析）；② audit receiver 直调 ``*.log(...)``。
    """
    want = set(slots)
    out = []
    for p, _text, tree in _trees():
        rel = str(p.relative_to(SRC.parent))
        specs = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                spec = _wrapper_spec(node)
                if spec:
                    specs[node.name] = spec
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            # ② 直调 *.log(...)（receiver 名含 audit 才算审计账本）
            if isinstance(n.func, ast.Attribute) and n.func.attr == "log":
                try:
                    recv = ast.unparse(n.func.value)
                except Exception:
                    recv = ""
                if "audit" not in recv.lower():
                    continue
                hint = _action_hint(n.args[1]) if len(n.args) >= 2 else ""
                for kw in n.keywords:
                    if kw.arg == "action":
                        hint = _action_hint(kw.value)
                for i, a in enumerate(n.args):
                    slot = _LOG_VALUE_SLOTS.get(i)
                    if slot in want:
                        for seg in _static_segments(a):
                            out.append((rel, hint, slot, seg))
                for kw in n.keywords:
                    if kw.arg in want:
                        for seg in _static_segments(kw.value):
                            out.append((rel, hint, kw.arg, seg))
                continue
            # ① 包装调用点
            if isinstance(n.func, ast.Name):
                name, attr_call = n.func.id, False
            elif isinstance(n.func, ast.Attribute):
                name, attr_call = n.func.attr, True
            else:
                continue
            spec = specs.get(name)
            if not spec:
                continue
            pos, is_method, params, vals = spec
            off = 1 if (is_method and attr_call) else 0
            aidx = pos - off
            hint = (_action_hint(n.args[aidx])
                    if 0 <= aidx < len(n.args) else "")
            for i, a in enumerate(n.args):
                pidx = i + off
                if 0 <= pidx < len(params):
                    slot = vals.get(params[pidx])
                    if slot in want:
                        for seg in _static_segments(a):
                            out.append((rel, hint, slot, seg))
            for kw in n.keywords:
                slot = vals.get(kw.arg or "")
                if slot in want:
                    for seg in _static_segments(kw.value):
                        out.append((rel, hint, slot, seg))
    return out
