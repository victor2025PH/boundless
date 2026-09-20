"""坐席对语音转写的人工改正 → 追加式 JSONL 台账（ASR P1，2026-09-12）。

每一条改正就是一条 ASR 金标（音频 + 人听出来的逐字稿）：``src/eval/asr_eval`` 直接把它并入
评测集，评测集随运营使用自然长大——这是「换模型要先有尺子」那把尺子的**数据来源**；
其次它是热词/领域词候选（机器转写 vs 人工改正的差异词）。

落盘：数据根 ``config/asr_corrections.jsonl``（与 ``reply_settings_audit.jsonl`` /
``assistant_actions_audit.jsonl`` 同目录同风格：``config_manager.config_path`` 的父目录），
每行 ``{ts, conversation_id, message_id, media_ref, audio_path, audio_sha1, machine_text,
corrected_text, lang, agent, platform}``。只追加不改写；读取时坏行跳过。

本模块零 web 依赖（纯函数 + 文件追加），路由/UI 接线属下一阶段（工作台媒体行「改正」入口）。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

FILENAME = "asr_corrections.jsonl"
_lock = threading.Lock()


def corrections_path(config_manager: Any = None, config_dir: Optional[str] = None) -> Optional[Path]:
    """改正台账路径：显式 ``config_dir`` > ``config_manager.config_path`` 父目录 > None。"""
    if config_dir:
        return Path(config_dir) / FILENAME
    base = getattr(config_manager, "config_path", None) if config_manager is not None else None
    return (Path(base).parent / FILENAME) if base else None


def _sha1_file(path: str) -> str:
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def build_correction(
    *, conversation_id: str, machine_text: str, corrected_text: str,
    message_id: str = "", media_ref: str = "", audio_path: str = "", lang: str = "",
    agent: str = "", platform: str = "", ts: Optional[float] = None,
) -> Dict[str, Any]:
    """构造一条改正记录（纯函数；``audio_sha1`` 在音频可读时计算，供缓存键/去重）。"""
    corrected = str(corrected_text or "").strip()
    if not corrected:
        raise ValueError("corrected_text required")
    return {
        "ts": float(ts if ts is not None else time.time()),
        "conversation_id": str(conversation_id or ""),
        "message_id": str(message_id or ""),
        "media_ref": str(media_ref or ""),
        "audio_path": str(audio_path or ""),
        "audio_sha1": _sha1_file(audio_path) if audio_path and os.path.isfile(audio_path) else "",
        "machine_text": str(machine_text or "").strip()[:2000],
        "corrected_text": corrected[:2000],
        "lang": str(lang or "").strip().lower(),
        "agent": str(agent or "")[:80],
        "platform": str(platform or "")[:32],
    }


def append_correction(path: Any, record: Dict[str, Any]) -> bool:
    """追加一行（父目录不存在则建）；任何 IO 失败返回 False，绝不抛。"""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with _lock:
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except Exception:
        return False


def iter_corrections(path: Any, *, limit: int = 0) -> List[Dict[str, Any]]:
    """读全部改正（坏行跳过）；``limit>0`` 只取最近 N 条（按文件顺序取尾）。文件缺失 → []。"""
    try:
        p = Path(path)
        if not p.is_file():
            return []
        out: List[Dict[str, Any]] = []
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if isinstance(d, dict) and str(d.get("corrected_text") or "").strip():
                    out.append(d)
        return out[-int(limit):] if limit and limit > 0 else out
    except Exception:
        return []


def hotword_candidates(records: Iterable[Dict[str, Any]], *, min_count: int = 2,
                       max_len: int = 12) -> List[Dict[str, Any]]:
    """机器转写 vs 人工改正的**替换/插入片段** → 热词候选（出现 ≥ min_count 次）。

    字符级对齐（difflib）取改正侧的差异片段（2..max_len 字符、只含 CJK/字母数字）：
    「治療的价格 → 智聊的价格」得「智聊」。只是候选清单给运营看，不自动进配置。
    """
    import difflib
    import re
    ok = re.compile(r"^(?:[\u3400-\u9fff]+|[A-Za-z][A-Za-z0-9]+)$")
    counts: Dict[str, int] = {}
    for r in records:
        corr = str(r.get("corrected_text") or "").strip()
        mach = str(r.get("machine_text") or "").strip()
        if not corr or corr == mach:
            continue
        sm = difflib.SequenceMatcher(a=mach, b=corr, autojunk=False)
        seen = set()
        for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
            if tag not in ("replace", "insert"):
                continue
            w = corr[j1:j2].strip()
            if 2 <= len(w) <= max_len and ok.match(w) and w not in seen:
                seen.add(w)
                counts[w] = counts.get(w, 0) + 1
    return [{"word": w, "count": c} for w, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            if c >= max(1, int(min_count))]


__all__ = [
    "FILENAME", "append_correction", "build_correction", "corrections_path",
    "hotword_candidates", "iter_corrections",
]
