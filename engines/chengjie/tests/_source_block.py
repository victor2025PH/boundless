"""按名字取源码块（接线类静态断言的共享工具）。

为什么不用 `inspect.getsource`：它按**导入时**记下的行号去切当前磁盘文件。
本仓多条线并发施工，另一条线在回归跑到一半改了同一个文件，行号一漂移，
断言就会拿到错位内容——真实发生过：`getsource(TelegramProtocolWorker.start)`
返回了孤零零一行 `try:`，于是接线测试莫名其妙全红，而代码其实好好的。

这里每次现读文件、按缩进找块尾，对行号漂移免疫；块不存在则如实抛错。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def source_block(module: Any, header: str) -> str:
    """返回 ``header``（如 ``"class Foo:"`` / ``"    def bar("``）所在块的正文。

    ``module`` 可以是模块对象，也可以是文件路径（str/Path）——有些模块导入代价
    很大或有环境前置（如 ``src.client.telegram_client`` 会 import pyrogram，而
    pyrogram 的 sync 包装在导入时就要求有事件循环），静态断言没必要为此付代价。

    块尾＝下一条缩进不深于 header 的非空、非注释行。
    """
    if isinstance(module, (str, Path)):
        path, label = Path(module), str(module)
    else:
        path, label = Path(module.__file__), module.__name__
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    start = next((i for i, ln in enumerate(lines) if ln.strip().startswith(header.strip())
                  and ln.startswith(header[:len(ln) - len(ln.lstrip())])), None)
    if start is None:
        start = next((i for i, ln in enumerate(lines) if header.strip() in ln), None)
    if start is None:
        raise AssertionError(f"源码里找不到块：{header!r}（{label}）")

    indent = len(lines[start]) - len(lines[start].lstrip())
    body = [lines[start]]
    for ln in lines[start + 1:]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            body.append(ln)
            continue
        if (len(ln) - len(ln.lstrip())) <= indent:
            break
        body.append(ln)
    return "\n".join(body)
