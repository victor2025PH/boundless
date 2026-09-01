"""感知哈希（pHash，64bit）纯函数——相册近重复检测的确定性指纹层（实施90）。

分层去重语义：sha256（字节级重传，store 已有）→ **pHash**（重编码/缩放/轻裁剪
后仍是"同一张图"）→ 嵌入层**刻意不做**（相册量级每人设百张，pHash 两两比对
微秒级已够；嵌入是"同主体不同照片"语义，用在这会把合法连拍误合并）。

实现＝经典 DCT pHash（与 imagehash.phash 同构，零新依赖）：灰度 32×32 →
2D DCT-II（numpy 矩阵乘）→ 取左上 8×8 低频 → 按中位数二值化 → 64bit（16 hex）。
汉明距经验带：同图重编码 0~6、近重复 ≤ :data:`NEAR_DUP_MAX_HAMMING`、
无关图 ~26-38。numpy/PIL 缺失或图片解码失败一律返回 ""＝无指纹
（所有消费方对空指纹跳过判定，软降级绝不阻塞上传/挑图）。
"""
from __future__ import annotations

import io
import logging
from typing import Any, Dict, Iterable, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# 近重复判定阈值（64bit 汉明距）。8 偏保守：重编码/缩放稳落带内，
# 构图相似但确实不同的两张一般 >16（业界经验 8-10 起步，收紧防误合并）。
NEAR_DUP_MAX_HAMMING = 8

_HASH_HEX_LEN = 16  # 64 bit


def _dct_matrix(n: int):
    import numpy as np
    k = np.arange(n).reshape(-1, 1).astype(np.float64)
    i = np.arange(n).reshape(1, -1).astype(np.float64)
    return np.cos(np.pi / n * (i + 0.5) * k)


def phash_bytes(data: Any) -> str:
    """图片字节 → 64bit pHash 的 16 位 hex；解码失败/依赖缺失 → ""。"""
    if not data:
        return ""
    try:
        import numpy as np
        from PIL import Image
        with Image.open(io.BytesIO(bytes(data))) as im:
            # 动图（gif/webp 多帧）取首帧；转灰度缩 32×32（LANCZOS 抗混叠）。
            im.seek(0)
            g = im.convert("L").resize((32, 32), Image.LANCZOS)
            a = np.asarray(g, dtype=np.float64)
        m = _dct_matrix(32)
        d = m @ a @ m.T
        low = d[:8, :8].flatten()
        med = float(np.median(low))
        bits = 0
        for v in low:
            bits = (bits << 1) | (1 if v > med else 0)
        return f"{bits:016x}"
    except Exception:
        logger.debug("[image_phash] 指纹计算失败（已忽略）", exc_info=True)
        return ""


def phash_file(path: Any) -> str:
    """文件路径版 ``phash_bytes``；读不到返回 ""。"""
    try:
        with open(str(path), "rb") as f:
            return phash_bytes(f.read())
    except Exception:
        return ""


def hamming_hex(a: Any, b: Any) -> int:
    """两个 16 位 hex 指纹的汉明距；任一无效 → 999（=永不判近重复）。"""
    sa, sb = str(a or "").strip().lower(), str(b or "").strip().lower()
    if len(sa) != _HASH_HEX_LEN or len(sb) != _HASH_HEX_LEN:
        return 999
    try:
        return bin(int(sa, 16) ^ int(sb, 16)).count("1")
    except ValueError:
        return 999


def is_near_dup(a: Any, b: Any, max_dist: int = NEAR_DUP_MAX_HAMMING) -> bool:
    return hamming_hex(a, b) <= int(max_dist)


def nearest(phash: Any, candidates: Dict[str, str]) -> Tuple[str, int]:
    """在 ``{id: phash}`` 里找最近邻；无有效候选返回 ("", 999)。"""
    best_id, best_d = "", 999
    for cid, h in (candidates or {}).items():
        d = hamming_hex(phash, h)
        if d < best_d:
            best_id, best_d = str(cid), d
    return best_id, best_d


def expand_ids_by_phash(
    rows: Iterable[Optional[Dict[str, Any]]],
    seed_ids: Iterable[Any],
    max_dist: int = NEAR_DUP_MAX_HAMMING,
) -> Set[str]:
    """把 id 集合按 pHash 近邻扩成「近重复族」（防复读排除面用，纯函数）。

    ``rows``＝候选条目（含 ``id``/``phash``）；``seed_ids``＝已发过的条目 id。
    返回 seed ∪ {与任一 seed 条目指纹距离 ≤ max_dist 的条目 id}。
    seed 条目无指纹（旧数据/计算失败）时只按原 id 排除——软降级不误伤。
    单遍扩散（不做传递闭包）：A≈B、B≈C 但 A 远 C 时不牵连 C，防链式误合并。
    """
    out: Set[str] = {str(x) for x in (seed_ids or ())}
    if not out:
        return out
    row_list = [r for r in (rows or []) if isinstance(r, dict)]
    seed_hashes = [
        str(r.get("phash") or "") for r in row_list
        if str(r.get("id")) in out and str(r.get("phash") or "")
    ]
    if not seed_hashes:
        return out
    for r in row_list:
        rid = str(r.get("id"))
        if rid in out:
            continue
        h = str(r.get("phash") or "")
        if not h:
            continue
        for sh in seed_hashes:
            if hamming_hex(h, sh) <= int(max_dist):
                out.add(rid)
                break
    return out


__all__ = [
    "NEAR_DUP_MAX_HAMMING", "phash_bytes", "phash_file", "hamming_hex",
    "is_near_dup", "nearest", "expand_ids_by_phash",
]
