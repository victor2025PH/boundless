# -*- coding: utf-8 -*-
"""validate_personas.py — 人设总线导出文件轻量校验器（仅标准库）。

校验对象：PERSONA_BUS.md §3 格式（version=1）。三类检查：
  1. 结构：顶层信封 + persona 必填字段/类型 + 四槽位齐全同构；
  2. 值域：fingerprint 必须是 64 位小写十六进制或 null；present=false 时
     fingerprint/ref 必须为 null；present=true 时 ref 必须为非空字符串；
  3. 无本体泄漏启发式：personas JSON 序列化后——
     - 不含 >2000 连续 base64 字母数字长串（内嵌资产本体的典型形态）；
     - 不含 *_b64 字段名 / data:image|audio;base64 URI / \\u0000 二进制标记。

用法：python tools/persona_bus/validate_personas.py <file.json> [more.json ...]
退出码：0=全部通过；1=任一文件不通过或读取失败。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:  # GBK 控制台防中文炸 print
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SLOT_KEYS = ("face", "voice", "prompt", "knowledge")
SOURCE_SYSTEMS = {"avatarhub", "chengjie", "huoke"}
_FP_RE = re.compile(r"^[0-9a-f]{64}$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

# 泄漏启发式（对 personas 数组的 JSON 序列化文本整体扫描）
_B64_RUN_RE = re.compile(r"[A-Za-z0-9+/=]{2000,}")     # >2000 连续字母数字
_B64_KEY_RE = re.compile(r'"[A-Za-z0-9_]*_b64"')       # face_b64/voice_b64 等字段名
_DATA_URI_RE = re.compile(r"data:(?:image|audio|video)/[^\"]{0,64};base64,")
_NUL_RE = re.compile(r"\\u0000")                        # JSON 转义形式的 NUL（二进制标记）


def _err(errors: list, where: str, msg: str) -> None:
    errors.append(f"{where}: {msg}")


def check_slot(sl, where: str, errors: list) -> None:
    if not isinstance(sl, dict):
        _err(errors, where, "槽位必须是对象")
        return
    for key in ("present", "fingerprint", "ref", "version"):
        if key not in sl:
            _err(errors, where, f"缺必填键 {key}")
    present = sl.get("present")
    if not isinstance(present, bool):
        _err(errors, where, "present 必须是 bool")
        return
    fp, ref, ver = sl.get("fingerprint"), sl.get("ref"), sl.get("version")
    if fp is not None and (not isinstance(fp, str) or not _FP_RE.fullmatch(fp)):
        _err(errors, where, "fingerprint 必须是 64 位小写 sha256 hex 或 null")
    if ver is not None and not isinstance(ver, str):
        _err(errors, where, "version 必须是字符串或 null")
    if present:
        if not isinstance(ref, str) or not ref.strip():
            _err(errors, where, "present=true 时 ref 必须为非空字符串")
    else:
        if fp is not None:
            _err(errors, where, "present=false 时 fingerprint 必须为 null")
        if ref is not None:
            _err(errors, where, "present=false 时 ref 必须为 null")


def check_persona(p, idx: int, errors: list, seen_keys: set) -> None:
    where = f"personas[{idx}]"
    if not isinstance(p, dict):
        _err(errors, where, "必须是对象")
        return
    for key in ("source_key", "display_name", "customer_name", "slots",
                "tags", "created_at", "raw"):
        if key not in p:
            _err(errors, where, f"缺必填键 {key}")
    sk = p.get("source_key")
    if not isinstance(sk, str) or not sk.strip():
        _err(errors, where, "source_key 必须为非空字符串")
    elif sk in seen_keys:
        _err(errors, where, f"source_key 重复：{sk}")
    else:
        seen_keys.add(sk)
    if not isinstance(p.get("display_name"), str) or not p.get("display_name"):
        _err(errors, where, "display_name 必须为非空字符串")
    cn = p.get("customer_name")
    if cn is not None and not isinstance(cn, str):
        _err(errors, where, "customer_name 必须是字符串或 null")
    slots = p.get("slots")
    if not isinstance(slots, dict):
        _err(errors, where, "slots 必须是对象")
    else:
        for k in SLOT_KEYS:
            if k not in slots:
                _err(errors, where, f"slots 缺槽位 {k}")
            else:
                check_slot(slots[k], f"{where}.slots.{k}", errors)
    tags = p.get("tags")
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        _err(errors, where, "tags 必须是字符串数组")
    ca = p.get("created_at")
    if ca is not None and (not isinstance(ca, str) or not _ISO_RE.match(ca)):
        _err(errors, where, "created_at 必须是 ISO8601 字符串或 null")
    if not isinstance(p.get("raw"), dict):
        _err(errors, where, "raw 必须是对象")


def check_leak(personas, errors: list) -> None:
    text = json.dumps(personas, ensure_ascii=False)
    m = _B64_RUN_RE.search(text)
    if m:
        _err(errors, "泄漏启发式",
             f"含 {len(m.group(0))} 字符连续 base64 长串（资产本体疑似入文），"
             f"片段头 {m.group(0)[:24]}…")
    m = _B64_KEY_RE.search(text)
    if m:
        _err(errors, "泄漏启发式", f"含内嵌资产字段名 {m.group(0)}（*_b64 禁止导出）")
    m = _DATA_URI_RE.search(text)
    if m:
        _err(errors, "泄漏启发式", f"含 data URI 资产头 {m.group(0)[:48]}")
    if _NUL_RE.search(text):
        _err(errors, "泄漏启发式", "含 \\u0000 二进制标记")


def validate_file(path: Path) -> "tuple[bool, int, list]":
    """→ (通过?, persona 条数, 错误列表)。"""
    errors: list = []
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return False, 0, [f"文件读取/解析失败：{e}"]
    if not isinstance(doc, dict):
        return False, 0, ["顶层必须是 JSON 对象"]
    if doc.get("version") != 1:
        _err(errors, "顶层", f"version 必须为 1（实际 {doc.get('version')!r}）")
    ss = doc.get("source_system")
    if not isinstance(ss, str) or not ss:
        _err(errors, "顶层", "source_system 必须为非空字符串")
    elif ss not in SOURCE_SYSTEMS:
        _err(errors, "顶层", f"source_system 未登记：{ss}（合法值 {sorted(SOURCE_SYSTEMS)}）")
    ea = doc.get("exported_at")
    if not isinstance(ea, str) or not _ISO_RE.match(ea):
        _err(errors, "顶层", "exported_at 必须是 ISO8601 字符串")
    personas = doc.get("personas")
    if not isinstance(personas, list):
        _err(errors, "顶层", "personas 必须是数组")
        return False, 0, errors
    seen: set = set()
    for i, p in enumerate(personas):
        check_persona(p, i, errors, seen)
    check_leak(personas, errors)
    return not errors, len(personas), errors


def main(argv: list) -> int:
    if not argv:
        print("用法: python validate_personas.py <personas.json> [more.json ...]",
              file=sys.stderr)
        return 1
    all_ok = True
    for arg in argv:
        path = Path(arg)
        ok, n, errors = validate_file(path)
        if ok:
            print(f"[validate_personas] OK   {path}（{n} 条 persona）")
        else:
            all_ok = False
            print(f"[validate_personas] FAIL {path}（{n} 条 persona，"
                  f"{len(errors)} 个问题）")
            for e in errors[:50]:
                print(f"  - {e}", file=sys.stderr)
            if len(errors) > 50:
                print(f"  …等共 {len(errors)} 个问题", file=sys.stderr)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
