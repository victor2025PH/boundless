"""YAML 重复键检测（2026-07-31 一次真实事故后抽出的单一事实源）。

**事故**：往实例 overlay 的 ``platform_login:`` 下新插了一个 ``telegram:`` 块，
没注意到那里**已经有一个**。PyYAML 对重复键**不报错、不警告、静默取最后一个**
→ 原块整段被丢弃（``protocol_enabled`` / ``companion_runtime`` / credpool 令牌 /
设备指纹全没了）→ 重启后编排器不再注册 Telegram worker → 5 个账号离线约 2 分钟。

这类错误的可怕之处是**全程无声**：YAML 解析成功、服务照常启动、日志无任何异常，
只是少了一整块配置。人不可能靠「下次小心」防住，只能靠可执行的检查。

三个消费口共用本模块（避免各写一份判据）：
- ``src/utils/config_check.py``    启动自检 + ``python main.py --check`` 严格闸门
- ``deploy/instances/restart_instance.ps1``  重启前置闸门（经下面那个 CLI）
- ``tools/check_config_duplicates.py``       运维手查 / 计划任务
"""
from __future__ import annotations

from typing import Any, Dict, List


def find_duplicate_key_details(text: str) -> List[Dict[str, Any]]:
    """重复键明细（P0 2026-08-29，「重复键挡全线重启 12 小时」事故后补）。

    每条：``{path, line, first_line, fixable}``（line 从 1 起）。``fixable`` =
    **后一次出现**是与首次出现**字节相同的单行标量条目**（块式映射内）——删掉
    那一行语义零变化（PyYAML 本就取最后一个，两行又完全一样）。块级重复 /
    值不同 / 流式映射 / 带不同注释，一律 ``fixable=False`` 留给人工判断。
    解析失败返回 ``[{"path": "<parse-error>", ...}]``。
    """
    import yaml

    out: List[Dict[str, Any]] = []
    stack: List[str] = []
    lines = text.splitlines()

    def _line(idx: int) -> str:
        return lines[idx].strip() if 0 <= idx < len(lines) else ""

    class _Loader(yaml.SafeLoader):
        pass

    def _mapping(loader: Any, node: Any, deep: bool = False) -> Any:
        first: Dict[str, Any] = {}
        for k_node, v_node in node.value:
            key = str(loader.construct_object(k_node, deep=True))
            if key in first:
                fk, fv = first[key]
                dup_line = k_node.start_mark.line          # 0 起
                fixable = (
                    not node.flow_style
                    and isinstance(v_node, yaml.ScalarNode)
                    and isinstance(fv, yaml.ScalarNode)
                    and v_node.start_mark.line == k_node.start_mark.line
                    and v_node.end_mark.line == k_node.start_mark.line
                    and fv.end_mark.line == fk.start_mark.line
                    and _line(dup_line) != ""
                    and _line(dup_line) == _line(fk.start_mark.line)
                )
                out.append({
                    "path": ".".join(stack + [key]),
                    "line": dup_line + 1,
                    "first_line": fk.start_mark.line + 1,
                    "fixable": bool(fixable),
                })
            else:
                first[key] = (k_node, v_node)
        result: Dict[Any, Any] = {}
        for k_node, v_node in node.value:
            key = loader.construct_object(k_node, deep=True)
            stack.append(str(key))
            try:
                result[key] = loader.construct_object(v_node, deep=True)
            finally:
                stack.pop()
        return result

    _Loader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    try:
        yaml.load(text, _Loader)
    except Exception:
        return [{"path": "<parse-error>", "line": 0,
                 "first_line": 0, "fixable": False}]
    return out


def dedupe_identical_lines(text: str) -> Dict[str, Any]:
    """删掉全部 ``fixable`` 重复行（字节相同的单行标量后现）。

    返回 ``{text, removed, remaining}``：``removed``/``remaining`` 为明细列表；
    没有可修项时 ``text`` 原样返回。**只做零语义变化的手术**——值不同/块级
    重复绝不自作主张（选错边=丢配置，比重复键本身更糟）。
    """
    details = find_duplicate_key_details(text)
    removable = sorted({d["line"] for d in details if d["fixable"]})
    if not removable:
        return {"text": text, "removed": [],
                "remaining": [d for d in details if not d["fixable"]]}
    keep_newline = text.endswith("\n")
    lines = text.splitlines()
    drop = {ln - 1 for ln in removable}
    new_text = "\n".join(
        ln for i, ln in enumerate(lines) if i not in drop)
    if keep_newline and new_text:
        new_text += "\n"
    # 手术后复检：残余重复按新文本行号如实回报（连删多处后行号已位移）
    remaining = [d for d in find_duplicate_key_details(new_text)]
    return {
        "text": new_text,
        "removed": [d for d in details if d["fixable"]],
        "remaining": remaining,
    }


def find_duplicate_keys(text: str) -> List[str]:
    """返回 YAML 文本里所有重复键的路径（``a.b.c`` 形式）；无重复返回 ``[]``。

    纯函数（只吃字符串），便于用合成样本钉住。解析失败返回 ``["<parse-error>"]``
    ——那属于另一类问题，但同样不该静默放过。

    只把**同一层级的同名键**算重复：不同层级同名（``a.x`` 与 ``a.y.x``）、以及
    列表里各元素的同名键都是正常写法，误报会让人不再信这个检查，等于没有。
    """
    import yaml

    dups: List[str] = []
    stack: List[str] = []

    class _Loader(yaml.SafeLoader):
        pass

    def _mapping(loader: Any, node: Any, deep: bool = False) -> Any:
        seen = set()
        for k_node, _ in node.value:
            key = str(loader.construct_object(k_node, deep=True))
            if key in seen:
                dups.append(".".join(stack + [key]))
            seen.add(key)
        # 逐键下探（为拼出完整路径，手工递归而非直接 construct_mapping）
        out: Dict[Any, Any] = {}
        for k_node, v_node in node.value:
            key = loader.construct_object(k_node, deep=True)
            stack.append(str(key))
            try:
                out[key] = loader.construct_object(v_node, deep=True)
            finally:
                stack.pop()
        return out

    _Loader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    try:
        yaml.load(text, _Loader)
    except Exception:
        return ["<parse-error>"]
    return dups


__all__ = [
    "dedupe_identical_lines",
    "find_duplicate_key_details",
    "find_duplicate_keys",
]
