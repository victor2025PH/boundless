"""方言能力出货闸门（纯函数）。

只放行**有标准生成链**的语种。2026-08-30 老板拍板：没有独立语言模型、只能
拿普通话稿 + CosyVoice3 instruct 假装口音的档，不得出现在人设选项，也不得
走合成 / 口语化。

当前唯一出货档＝**粤语**：人设 prompt 写粤文 + CosyVoice3 ``<|yue|>`` zero_shot
克隆（``lang_voice_route``），7852 挂了才回落 Edge ``zh-HK``。粤语不走
``inference_instruct2``（instruct2 会丢参考音语音 token）。

闽南 / 台湾 / 川渝 / 东北 / 湖南 / 北京：无专模、instruct+普通话 ≠ 该语言，
一律视为未出货。官方 instruct 原文留在 ``UNSHIPPED_COSY3_INSTRUCT`` 备查，
``dialect_acoustic_override`` 永不消费。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

SHIPPED_DIALECT_FLAVORS = frozenset({"cantonese"})

# CosyVoice3 common.py instruct_list 原文。未出货——改字模型也不认，仅作归档。
UNSHIPPED_COSY3_INSTRUCT: Dict[str, str] = {
    "chuanyu": "请用四川话表达。",
    "dongbei": "请用东北话表达。",
    "hunan": "请用湖南话表达。",
    "minnan": "请用闽南话表达。",
    "taiwan": "请用闽南话表达。",
}

# 兼容旧测试 / 旧 import；出货表为空（instruct2 路径已关）。
COSY3_DIALECT_INSTRUCT: Dict[str, str] = {}

DEFAULT_DIALECT_NODE = "http://127.0.0.1:7852"


def is_shipped_dialect(flavor: str) -> bool:
    return str(flavor or "").strip().lower() in SHIPPED_DIALECT_FLAVORS


def normalize_dialect_flavor(flavor: str) -> str:
    """未出货档（含空/未知）→ 空串。粤语原样返回。"""
    f = str(flavor or "").strip().lower()
    return f if f in SHIPPED_DIALECT_FLAVORS else ""


def dialect_acoustic_override(
    flavor: str,
    *,
    node: str = DEFAULT_DIALECT_NODE,
) -> Optional[Dict[str, Any]]:
    """未出货方言不得改声学。粤语由 ``lang_voice_route`` 处理，这里也是 None。"""
    _ = (flavor, node)
    return None


def merge_dialect_acoustic(vp: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """清洗 dialect_flavor；不叠 instruct2。新 dict，不改入参。"""
    base = dict(vp) if isinstance(vp, dict) else {}
    if "dialect_flavor" in base:
        base["dialect_flavor"] = normalize_dialect_flavor(
            str(base.get("dialect_flavor") or ""))
    ov = dialect_acoustic_override(str(base.get("dialect_flavor") or ""))
    if ov:
        base.update(ov)
    return base
