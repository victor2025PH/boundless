# -*- coding: utf-8 -*-
"""给 ComfyUI_PuLID_Flux_ll 打兼容补丁（在 gpu176 上跑）。

新版 ComfyUI (>=2026-07) 的 Flux.forward 调 forward_orig 时新增关键字参数
timestep_zero_index（Flux Kontext 参考图用，纯文生图恒为 None）。
PuLID 节点 hook 替换的 pulid_forward_orig 是旧签名，没有该参数 → TypeError。
本脚本给签名补上 timestep_zero_index=None（值被忽略，纯文生图行为不变）。幂等。
"""
import sys

PATH = r"D:\ComfyUI\custom_nodes\ComfyUI_PuLID_Flux_ll\PulidFluxHook.py"

OLD = """    guidance: Tensor = None,
    control = None,
    transformer_options={},
    attn_mask: Tensor = None,
) -> Tensor:"""

NEW = """    guidance: Tensor = None,
    control = None,
    timestep_zero_index=None,
    transformer_options={},
    attn_mask: Tensor = None,
) -> Tensor:"""


def main() -> int:
    with open(PATH, encoding="utf-8") as f:
        src = f.read()
    if "timestep_zero_index" in src:
        print("already patched")
        return 0
    if OLD not in src:
        print("ERROR: pattern not found, upstream layout changed", file=sys.stderr)
        return 1
    with open(PATH, "w", encoding="utf-8", newline="") as f:
        f.write(src.replace(OLD, NEW, 1))
    print("patched OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
