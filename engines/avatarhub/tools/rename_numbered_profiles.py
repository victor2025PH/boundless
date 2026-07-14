# -*- coding: utf-8 -*-
"""VN2 角色库去编号迁移（2026-07-09，一次性）：把角色库里"编号/字母款"旧名迁到人设名。

安全设计：
  · 走运行中 Hub 的 PATCH /profiles/{name} 改名链路——DB 单事务、使用台账跟随、
    OG 封面缓存失效、profile_renamed 广播全部复用现成代码，脚本零私自碰库；
  · 只动"旧自动名"（编号款/清晰X声N款/男声风格款）——用户手工起过的名字一律不碰
    （有 voicepack_spk 但名字不匹配旧款式 → 视为用户已自定义，跳过并报告）；
  · 溯源双保险：spk 继续存 voicepack_spk 字段，description 刷成
    "真人音色库 {spk} · {名字的特征}"，编号从显示名退居档案位；
  · 改名映射落 logs\\rename_map_20260709.json（old→new），反向执行即回滚；
  · 若被改名的正是当前出镜角色，改后 POST activate?auto=true 刷新
    active_profile.txt（改名端点只更内存，不重写落盘文件——重启会退回首个角色）。

用法：.venv_launcher\\Scripts\\python.exe tools\\rename_numbered_profiles.py [--dry]
"""
import io
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HUB = "http://127.0.0.1:9000"

# 旧自动名款式（只有匹配这些的才迁移；全款式均以编号/字母/风格词收尾，无人设含义）
LEGACY_PATTERNS = [
    re.compile(r"^(低音|中音|明亮)?(沉稳|自然|活泼)?(男|女)声\s*\d{4}$"),   # 低音沉稳男声 0139
    re.compile(r"^清晰(男|女)声[A-Z]$"),                                    # 清晰男声A
    re.compile(r"^(男|女)声(磁性|利落|活泼|均衡|低音炮|低沉版|浑厚|乡音)$"),  # 男声磁性…
]

# 无 spk 档案的内嵌声（方案 §2.3 对照表钉死）：旧名 → (新名, 新描述)
EMBEDDED_MAP = {
    "男声低音炮": ("铜钟低音", "低音·内嵌克隆声（无档案编号）：低频量感如铜钟，厚而不闷，适合品牌口播"),
    "男声均衡":   ("温白开", "中音·内嵌克隆声（无档案编号）：各维居中、百搭不腻，日常对话首选"),
}


def api(path, method="GET", body=None):
    req = urllib.request.Request(HUB + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=data, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def is_legacy(name: str) -> bool:
    return any(p.match(name) for p in LEGACY_PATTERNS)


def main():
    dry = "--dry" in sys.argv
    vp = ROOT / "voice_pack_aishell3"
    featured = {r["spk"]: r for r in json.loads((vp / "featured.json").read_text(encoding="utf-8"))}
    names = json.loads((vp / "names.json").read_text(encoding="utf-8"))

    d = api("/profiles")
    profiles = d["profiles"]
    active = d.get("active") or ""
    taken = {p["name"] for p in profiles}

    plan, skipped = [], []
    for p in profiles:
        old = p["name"]
        spk = p.get("voicepack_spk") or ""
        if spk:
            ft, nm = featured.get(spk), names.get(spk) or {}
            new = (ft or {}).get("title") or nm.get("title") or ""
            note = (nm.get("note") or (ft or {}).get("tagline") or "").strip()
            desc = f"真人音色库 {spk} · {note}" if note else f"真人音色库 {spk}"
            if not new or new == old:
                continue
            if not is_legacy(old):
                skipped.append((old, f"名字非旧自动名款式（疑似用户自定义），不动；命名表建议「{new}」"))
                continue
        elif old in EMBEDDED_MAP:
            new, desc = EMBEDDED_MAP[old]
        else:
            continue
        if new in taken:
            skipped.append((old, f"目标名「{new}」已被占用，跳过"))
            continue
        taken.add(new)
        plan.append({"old": old, "new": new, "desc": desc, "spk": spk,
                     "was_active": old == active})

    print(f"角色 {len(profiles)} 个 · 计划改名 {len(plan)} 个 · 跳过 {len(skipped)} 个")
    for it in plan:
        print(f"  {it['old']:<14} → {it['new']:<8} ({it['spk'] or '内嵌声'})"
              + ("  [出镜中]" if it["was_active"] else ""))
    for old, why in skipped:
        print(f"  [跳过] {old}: {why}")
    if dry:
        print("(--dry 未执行)")
        return
    if not plan:
        print("没有需要迁移的角色。")
        return

    done, failed = [], []
    for it in plan:
        try:
            r = api(f"/profiles/{urllib.request.quote(it['old'])}", "PATCH",
                    {"new_name": it["new"]})
            if not r.get("renamed"):
                raise RuntimeError(f"改名未生效: {r}")
            api(f"/profiles/{urllib.request.quote(it['new'])}", "PATCH",
                {"description": it["desc"]})
            if it["was_active"]:   # 改名端点只更内存态；auto=true 重激活刷新落盘文件且不计使用台账
                api(f"/profiles/{urllib.request.quote(it['new'])}/activate?auto=true", "POST")
            done.append(it)
            print(f"  ✔ {it['old']} → {it['new']}")
        except Exception as e:
            failed.append((it, str(e)))
            print(f"  ✘ {it['old']} → {it['new']}: {e}")

    log = ROOT / "logs" / "rename_map_20260709.json"
    log.parent.mkdir(exist_ok=True)
    log.write_text(json.dumps({
        "ts": time.time(), "hub": HUB,
        "renamed": [{"old": i["old"], "new": i["new"], "spk": i["spk"]} for i in done],
        "failed": [{"old": i["old"], "new": i["new"], "err": e} for i, e in failed],
        "skipped": [{"name": n, "why": w} for n, w in skipped],
        "rollback": "反向执行 renamed 里的 new→old 即回滚（同样走 PATCH new_name）",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"改名 {len(done)} 成功 / {len(failed)} 失败；映射已落 {log}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    main()
