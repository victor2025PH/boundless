import json, urllib.request, urllib.parse, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
H = "http://127.0.0.1:9000"
def get(p, t=30):
    with urllib.request.urlopen(H + p, timeout=t) as r:
        return json.loads(r.read())

d = get("/rvc/devices", 20)
print("== /rvc/devices ==")
print("ok:", d.get("ok"), "| source:", d.get("source"), "| ex_note:", d.get("ex_note"))
ie, oe = d.get("inputs_ex") or {}, d.get("outputs_ex") or {}
print(f"输入: 原始 {ie.get('raw_count')} 条 → 合并 {ie.get('merged_count')} 条 | 推荐: {(ie.get('pick') or {}).get('label')} ({(ie.get('pick') or {}).get('reason','')[:20]})")
print(f"输出: 原始 {oe.get('raw_count')} 条 → 合并 {oe.get('merged_count')} 条 | 推荐: {(oe.get('pick') or {}).get('label')}")
vis_i = [x for x in ie.get("devices", []) if not x.get("hidden")]
vis_o = [x for x in oe.get("devices", []) if not x.get("hidden")]
print("输入默认可见:", len(vis_i), "条")
for x in vis_i: print("   ", x["group"], "|", x["label"], "|", len(x["variants"]), "条合并")
print("输出默认可见:", len(vis_o), "条")
for x in vis_o: print("   ", x["group"], "|", x["label"], "|", len(x["variants"]), "条合并")
dang = [x["label"] for x in ie.get("devices", []) if x.get("danger")]
print("输入危险项(默认折叠):", dang)
json.dump(d, open(r"C:\模仿音色\logs\_p0_devices_snapshot.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
