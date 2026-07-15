"""人设资产验收 lint（2026-07-15 B3）——新人设上线前的自动体检。

背景（当日实锤的资产债）：四个人设（含男性 Marcus）共用同一个女声参考音；
外貌锚点出图偏年轻触发未成年安全拦截；场景池硬编码时段词导致清晨说"刚下课"。
这些都属于「资产生产管线缺验收」——问题在上线后才被用户当穿帮发现。

检查项（纯函数，文件存在性可注入便于测试）：
  R1 参考音：voice_profile 启用但缺路径/文件不存在 → error
  R2 参考音共用：多人设指向同一参考音 → warn；**跨性别共用** → error
     （男性人设用女声克隆源，一开口就穿帮）
  R3 外貌锚点：配置了自拍场景池但 appearance 为空 → error（狗图事故防线）
  R4 年龄锚点：appearance 里的 "N-year-old" 与 persona.age 不一致 → warn
  R5 场景池时段覆盖：某代表时段（8/13/19/23 点）全池硬冲突 → warn
     （运行时会回退全池 → 时段错配场景仍可能注入）
  R6 台词库：voice_profile 启用但 prerender_lines/<id>.txt 缺失 → info
     （高频短句没备货 → 每句都吃 GPU 合成延迟）
输出统一为 ``{persona, check, severity, detail}`` 列表；severity ∈ error/warn/info。
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, List, Optional

_AGE_RE = re.compile(r"(\d{2})\s*-\s*year\s*-\s*old|(\d{2})-year-old", re.IGNORECASE)


def _issue(persona: str, check: str, severity: str, detail: str) -> Dict[str, str]:
    return {"persona": persona, "check": check, "severity": severity,
            "detail": detail}


def lint_personas(
    profiles: Dict[str, Any],
    *,
    file_exists: Optional[Callable[[str], bool]] = None,
    lines_dir: str = "config/prerender_lines",
) -> List[Dict[str, str]]:
    """对人设档案批量体检，返回问题列表（空=全部通过）。"""
    exists = file_exists or os.path.isfile
    issues: List[Dict[str, str]] = []
    profiles = profiles or {}

    # ── R1/R6 逐人设 + 收集参考音归属（供 R2 共用分析）──────────────────────
    ref_owners: Dict[str, List[str]] = {}   # ref_path -> [persona_id...]
    for pid, prof in profiles.items():
        if not isinstance(prof, dict):
            continue
        vp = prof.get("voice_profile") if isinstance(
            prof.get("voice_profile"), dict) else {}
        if vp.get("enabled"):
            ref = str(vp.get("reference_audio_path") or "").strip()
            if not ref:
                issues.append(_issue(
                    pid, "reference_audio", "error",
                    "voice_profile 启用但未配置 reference_audio_path"))
            else:
                ref_owners.setdefault(ref.replace("\\", "/"), []).append(pid)
                if not exists(ref):
                    issues.append(_issue(
                        pid, "reference_audio", "error",
                        f"参考音文件不存在: {ref}"))
            lines_file = f"{lines_dir}/{pid}.txt"
            if not exists(lines_file):
                issues.append(_issue(
                    pid, "prerender_lines", "info",
                    f"缺台词库 {lines_file}（高频短句无备货，每句都吃合成延迟）"))

        # ── R3/R4 外貌锚点 ──────────────────────────────────────────────────
        scenes = prof.get("selfie_scenes")
        appearance = str(prof.get("appearance") or "").strip()
        if isinstance(scenes, (list, tuple)) and scenes and not appearance:
            issues.append(_issue(
                pid, "appearance", "error",
                "配置了自拍场景池但 appearance 外貌锚点为空（狗图事故防线）"))
        if appearance:
            m = _AGE_RE.search(appearance)
            age_cfg = prof.get("age")
            if m and age_cfg:
                anchor_age = int(m.group(1) or m.group(2))
                try:
                    if abs(anchor_age - int(age_cfg)) > 1:
                        issues.append(_issue(
                            pid, "appearance_age", "warn",
                            f"外貌锚点写 {anchor_age} 岁但 persona.age={age_cfg}"
                            "（出图年龄漂移→易触发安全拦截或穿帮）"))
                except (TypeError, ValueError):
                    pass

        # ── R5 场景池时段覆盖 ────────────────────────────────────────────────
        if isinstance(scenes, (list, tuple)) and scenes:
            try:
                from src.ai.companion_selfie import scene_conflicts_with_hour
                for hour, label in ((8, "上午"), (13, "午后"),
                                    (19, "傍晚"), (23, "深夜")):
                    fitting = [s for s in scenes
                               if not scene_conflicts_with_hour(str(s), hour)]
                    if not fitting:
                        issues.append(_issue(
                            pid, "scene_pool_bucket", "warn",
                            f"{label}(约{hour}点)时段全池硬冲突——运行时将回退"
                            "全池，时段错配场景仍可能注入聊天/生图"))
            except Exception:
                pass

    # ── R2 参考音共用分析 ────────────────────────────────────────────────────
    for ref, owners in ref_owners.items():
        if len(owners) < 2:
            continue
        genders = set()
        for pid in owners:
            g = str((profiles.get(pid) or {}).get("gender") or "").strip().lower()
            if g:
                genders.add(g)
        sev = "error" if len(genders) > 1 else "warn"
        why = ("跨性别共用同一参考音（男性人设用女声克隆源，开口即穿帮）"
               if len(genders) > 1 else
               "多人设共用同一参考音（声音一样，用户串号即穿帮）")
        for pid in owners:
            issues.append(_issue(
                pid, "reference_audio_shared", sev,
                f"{why}: {ref} ← {', '.join(owners)}"))
    return issues


def format_report(issues: List[Dict[str, str]]) -> str:
    """人类可读报告（脚本/CI 输出用）。"""
    if not issues:
        return "人设资产体检：全部通过 ✓"
    order = {"error": 0, "warn": 1, "info": 2}
    rows = sorted(issues, key=lambda x: (order.get(x["severity"], 9), x["persona"]))
    icon = {"error": "✗", "warn": "!", "info": "·"}
    lines = [f"人设资产体检：{sum(1 for i in issues if i['severity']=='error')} error / "
             f"{sum(1 for i in issues if i['severity']=='warn')} warn / "
             f"{sum(1 for i in issues if i['severity']=='info')} info"]
    for r in rows:
        lines.append(
            f" {icon.get(r['severity'], '?')} [{r['severity']}] "
            f"{r['persona']} / {r['check']}: {r['detail']}")
    return "\n".join(lines)


__all__ = ["lint_personas", "format_report"]
