# -*- coding: utf-8 -*-
"""P2-RA: 依据 _rvc_probe_report.json 的量化分桶给 9 个编号男声落显示别名 → rvc_alias_map.json。
只加显示名不动 .pth 文件名（改文件名会断 rvc_model 绑定链/预设键/引擎路径解析三处）。
别名与 names.json / featured.json / 现有角色名三方查重；note 写量化依据（质心/亮度桶），
边界款（borderline_with）在 note 里点名，人工只需试听这几对。"""
import io
import json
import sys
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
BASE = Path(r"C:\模仿音色")
report = json.loads((BASE / "tools" / "_rvc_probe_report.json").read_text(encoding="utf-8"))["rows"]

# 人工终审的别名（依据报告 rank_dark_to_bright 1→9）：暗厚=木/铜/绒 意象，中性=生活意象，
# 明亮=金属/光 意象——和声音库「意象词+身份词」一个语感，但都是 3-4 字短词，混排不撞车。
ALIAS = {
    "weights/CN_男声-03号.pth": ("沉香木",   "全库最暗（质心870Hz），胸腔共鸣厚——旁白/成熟稳重人设"),
    "weights/CN_男声-01号.pth": ("青铜钟",   "同档最暗带钟鸣泛音（质心897Hz）——低沉但不闷"),
    "weights/CN_男声-06号.pth": ("天鹅绒",   "暗而顺滑（质心1155Hz、高频最收）——深夜电台质感"),
    "weights/CN_男声-09号.pth": ("贴麦主播", "中性偏亮、2-4k存在感全场最高——人声贴耳，带货吃香"),
    "weights/CN_男声-10号.pth": ("白衬衫",   "全场最均衡（质心1530Hz居中）——干净通勤感，万金油"),
    "weights/CN_男声-08号.pth": ("磨砂气嗓", "中亮带气声颗粒（5-8k空气感全场最高）——松弛口播"),
    "weights/CN_男声-02号.pth": ("鎏金亮嗓", "亮而饱满（频谱最平、中频有劲）——热场/促销有冲劲"),
    "weights/CN_男声-04号.pth": ("钢弦亮嗓", "第二亮（质心1866Hz）金属弦感——清晰穿透，嘈杂场景不糊"),
    "weights/CN_男声-07号.pth": ("银哨清亮", "全库最亮（质心2019Hz）偏薄清冷——年轻利落人设"),
}

# ── 三方查重：声音库 names.json + featured.json + 现有角色名 ──
taken = set()
nj = json.loads((BASE / "voice_pack_aishell3" / "names.json").read_text(encoding="utf-8"))
taken |= {v["title"] for v in nj.values()}
fj = json.loads((BASE / "voice_pack_aishell3" / "featured.json").read_text(encoding="utf-8"))
taken |= {v.get("title", "") for v in (fj.values() if isinstance(fj, dict) else fj)}
try:
    d = json.loads(urllib.request.urlopen("http://127.0.0.1:9000/profiles", timeout=8).read())
    taken |= {p["name"] for p in d["profiles"]}
except Exception as e:
    print(f"(角色名在线查重跳过: {e})")

out = {}
for rel_id, (alias, note) in ALIAS.items():
    assert rel_id in report, f"报告缺少 {rel_id}（先跑 rvc_autobucket.py）"
    assert alias not in taken, f"别名撞车: {alias}"
    assert alias not in {v[0] for k, v in ALIAS.items() if k != rel_id}, f"别名内部重复: {alias}"
    r = report[rel_id]
    bl = [Path(b).stem.replace("CN_男声-", "") for b in r.get("borderline_with", [])]
    out[rel_id] = {"alias": alias, "note": note, "bucket": r["bucket"],
                   "centroid_hz": r["centroid_hz"],
                   **({"audition_vs": bl} if bl else {})}

dst = BASE / "rvc_alias_map.json"
dst.write_text(json.dumps({"comment": "RVC 变声模型显示别名（不改 .pth 文件名防断链）。"
                                      "alias=下拉/资产列表显示名；audition_vs=频谱边界款，人工试听裁决对象",
                           "aliases": out}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"{len(out)} 条别名 → {dst}")
for k, v in out.items():
    print(f"  {Path(k).stem.replace('CN_男声-','')}: {v['alias']:<6} [{v['bucket']}] {v['note'][:30]}")
