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


__all__ = ["find_duplicate_keys"]
