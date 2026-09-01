"""相册健康报告（实施90 阶段二）：备货体检单一口径——「该补什么、该清什么」。

供给侧真相全在 persona_media 注册表行里（打标覆盖 / 零命中 / 低质 / 敏感 /
近重复族 / 标注分歧 / 缩略图缺口），需求侧在 ``scene_demand_daily`` 日账本
（客户点名场景 demand/unmet，跨重启持久）。本模块**纯函数**聚合（rows/demand
由调用方注入——CLI 走只读连接，绝不碰活体库写事务），产出逐人设体检 + 判词。

判词哲学＝proactive_review 同族：给「下一步人工动作」（补标/并系列/补货/复核），
不自动改任何东西。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from src.companion.image_phash import NEAR_DUP_MAX_HAMMING, hamming_hex

# 「上架很久却从未发出」的时间线（秒）：早于此仍零发送 → 死库存嫌疑
_STALE_NEVER_SENT_SEC = 14 * 86400


def _neardup_groups(photos: Sequence[Dict[str, Any]],
                    max_dist: int) -> List[List[str]]:
    """带指纹照片的近重复族（并查集，n=百级两两比对微秒级）。返回 size≥2 的组。"""
    items = [(str(r.get("id")), str(r.get("phash") or ""))
             for r in photos if str(r.get("phash") or "")]
    parent = {i: i for i, _ in items}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if hamming_hex(items[i][1], items[j][1]) <= max_dist:
                ra, rb = find(items[i][0]), find(items[j][0])
                if ra != rb:
                    parent[ra] = rb
    groups: Dict[str, List[str]] = {}
    for mid, _ in items:
        groups.setdefault(find(mid), []).append(mid)
    return [sorted(g) for g in groups.values() if len(g) >= 2]


def _row_scene(row: Dict[str, Any]) -> str:
    try:
        from src.companion.persona_media import row_scene_class
        return row_scene_class(row)
    except Exception:
        return ""


def build_album_health(
    rows: Sequence[Dict[str, Any]],
    demand: Optional[Dict[str, Dict[str, int]]] = None,
    *, now: Optional[float] = None,
    neardup_max: int = NEAR_DUP_MAX_HAMMING,
) -> Dict[str, Any]:
    """注册表行 + 场景需求账本 → 逐人设健康体检（纯函数）。

    ``demand``＝``{scene: {"demand": n, "unmet": n}}``（store.scene_demand_window
    口径，全局共享——需求账本不分人设，缺货判定按「该人设该场景零备货」）。
    """
    ts = float(now if now is not None else time.time())
    dm = demand if isinstance(demand, dict) else {}
    by_pid: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows or []:
        if isinstance(r, dict):
            by_pid.setdefault(str(r.get("persona_id") or "?"), []).append(r)
    personas: Dict[str, Any] = {}
    for pid, rs in sorted(by_pid.items()):
        photos = [r for r in rs
                  if str(r.get("media_type") or "photo") == "photo"]
        enabled = [r for r in rs if r.get("enabled", True)]
        st = {"tagged": 0, "pending": 0, "failed": 0, "skipped": 0,
              "untagged": 0}
        flags = {"sensitive": 0, "lowq": 0, "face_mismatch": 0, "conflicts": 0}
        supply: Dict[str, int] = {}
        zero_hit = 0
        stale_never_sent = 0
        thumbs_missing = 0
        phash_have = 0
        for r in rs:
            s = str(r.get("tag_status") or "")
            st[s if s in st else "untagged"] += 1
            am = r.get("auto_meta") if isinstance(r.get("auto_meta"), dict) else {}
            fl = am.get("flags") if isinstance(am.get("flags"), dict) else {}
            if fl.get("nsfw") or fl.get("explicit") or fl.get("underage"):
                flags["sensitive"] += 1
            if str(am.get("quality") or "") in ("blurry", "dark"):
                flags["lowq"] += 1
            if str(am.get("face_match") or "") == "no":
                flags["face_mismatch"] += 1
            if isinstance(am.get("conflicts"), dict) and am["conflicts"]:
                flags["conflicts"] += 1
            if r.get("enabled", True):
                sc = _row_scene(r)
                if sc:
                    supply[sc] = supply.get(sc, 0) + 1
                if not int(r.get("hits") or 0):
                    zero_hit += 1
                    try:
                        created = float(r.get("created_at") or 0)
                    except (TypeError, ValueError):
                        created = 0.0
                    if (not float(r.get("last_sent_at") or 0)
                            and created and ts - created > _STALE_NEVER_SENT_SEC):
                        stale_never_sent += 1
        for r in photos:
            if not str(r.get("thumb_url") or ""):
                thumbs_missing += 1
            if str(r.get("phash") or ""):
                phash_have += 1
        dup_groups = _neardup_groups(photos, neardup_max)
        missing_demand = []
        for scene, dd in sorted(dm.items(), key=lambda kv: -int(
                (kv[1] or {}).get("unmet") or 0)):
            unmet = int((dd or {}).get("unmet") or 0)
            if unmet > 0 and not supply.get(scene):
                missing_demand.append({"scene": scene, "unmet": unmet})
        n_en = len(enabled)
        verdict: List[str] = []
        if st["untagged"] or st["failed"]:
            verdict.append(
                f"未打标 {st['untagged']} 张、失败 {st['failed']} 张——先跑「AI 补标」")
        if thumbs_missing:
            verdict.append(f"缺缩略图 {thumbs_missing} 张（补标批会一并补齐）")
        if n_en and zero_hit >= max(3, int(n_en * 0.3)):
            verdict.append(
                f"零命中 {zero_hit}/{n_en}——触发词太窄或素材同质化，"
                f"其中上架超两周从未发出 {stale_never_sent} 张")
        if dup_groups:
            n_dup = sum(len(g) for g in dup_groups)
            verdict.append(
                f"近重复 {len(dup_groups)} 组共 {n_dup} 张——建议归入同一系列"
                "或下架冗余（strict 档下同族只算一张）")
        if flags["sensitive"]:
            verdict.append(
                f"敏感素材 {flags['sensitive']} 张——复核关系门槛是否 ≥ 建议值")
        if flags["face_mismatch"]:
            verdict.append(
                f"疑似非本人 {flags['face_mismatch']} 张——人工确认（换脸级穿帮风险）")
        if flags["conflicts"]:
            verdict.append(
                f"标注分歧 {flags['conflicts']} 张——人工裁决人工/AI 谁对")
        for md in missing_demand[:3]:
            verdict.append(
                f"客户在要「{md['scene']}」（近窗 unmet {md['unmet']} 次）"
                "而该人设零备货——照单补货")
        personas[pid] = {
            "total": len(rs), "enabled": n_en,
            "photo": len(photos), "video": len(rs) - len(photos),
            "tag_status": st, "flags": flags,
            "thumbs_missing": thumbs_missing,
            "phash_cover": f"{phash_have}/{len(photos)}",
            "zero_hit_enabled": zero_hit,
            "stale_never_sent": stale_never_sent,
            "neardup_groups": dup_groups,
            "scene_supply": dict(sorted(supply.items(), key=lambda kv: -kv[1])),
            "missing_demand": missing_demand,
            "verdict": verdict,
        }
    return {"generated_at": ts, "personas": personas}


__all__ = ["build_album_health"]
