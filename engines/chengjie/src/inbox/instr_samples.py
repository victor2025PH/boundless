# -*- coding: utf-8 -*-
"""坐席指令 → 拟稿产出 留样 ring（P23 质量抽检面）。

「模型有没有照坐席指令做」是语义判断，没法确定性断言成门禁；能做的是把
(指令, 产出) 成对留样，让周审用人耳抽检 20 条替代拍脑袋。存储契约：

- 落 ``logs/instr_samples.jsonl``——**CWD 相对 = 实例数据根**（C 类数据落点，
  与 outreach/log 家族同哲学：服务进程 CWD 即数据根；测试经 path 参数注入
  tmp_path，绝不写仓库）。
- 只存坐席指令(≤200)与 AI 产出(≤240)——**刻意不存客户原文**（样本用途是
  「指令遵循」抽检，不需要入站内容；隐私面最小化）。
- 尺寸 ring：超 ``_MAX_BYTES`` 时保尾 ``_KEEP`` 行重写，永不无界膨胀。
- 写读全 best-effort：record 失败返 False 不抛（绝不影响拟稿主链），
  read 失败返空表。

读出口：``read_instr_samples(limit)`` → ``GET /api/goals/instr-samples``
（周审 CLI ``scripts/growth_review.py --samples N`` 经该路由消费）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_MAX_BYTES = 512 * 1024   # 超过即触发保尾重写
_KEEP = 300               # 重写时保留的尾行数

_LOCK = threading.Lock()


def _default_path() -> Path:
    return Path("logs") / "instr_samples.jsonl"


def record_instr_sample(
    *,
    instruction: str,
    reply: str,
    conv: str = "",
    source: str = "",
    mode: str = "",
    persona: str = "",
    path: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """追加一条样本；任何失败静默返 False（主链零影响）。"""
    inst = str(instruction or "").strip()[:200]
    rep = str(reply or "").strip()[:240]
    if not inst or not rep:
        return False
    row = {
        "ts": float(now if now is not None else time.time()),
        "conv": str(conv or "")[:96],
        "source": str(source or "")[:16],
        "mode": str(mode or "")[:16],
        "persona": str(persona or "")[:48],
        "instruction": inst,
        "reply": rep,
    }
    p = Path(path) if path is not None else _default_path()
    try:
        with _LOCK:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            try:
                if p.stat().st_size > _MAX_BYTES:
                    lines = p.read_text(encoding="utf-8").splitlines()
                    tail = [ln for ln in lines if ln.strip()][-_KEEP:]
                    p.write_text("\n".join(tail) + "\n", encoding="utf-8")
            except Exception:
                pass
        return True
    except Exception:
        return False


def read_instr_samples(
    limit: int = 20, *, path: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """尾窗样本（新→旧）。坏行跳过；任何失败返空表。"""
    n = max(1, min(int(limit or 20), 200))
    p = Path(path) if path is not None else _default_path()
    try:
        if not p.exists():
            return []
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for ln in reversed(lines):
        if len(out) >= n:
            break
        ln = ln.strip()
        if not ln:
            continue
        try:
            row = json.loads(ln)
        except Exception:
            continue
        if isinstance(row, dict) and row.get("instruction"):
            out.append(row)
    return out


__all__ = ["record_instr_sample", "read_instr_samples"]
