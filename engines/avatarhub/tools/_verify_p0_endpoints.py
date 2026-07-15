import json, urllib.request, urllib.parse, sys, io, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
H = "http://127.0.0.1:9000"
def get(p, t=60):
    with urllib.request.urlopen(H + p, timeout=t) as r:
        return json.loads(r.read())

print("═══ 1) /rvc/devices 结构化 ═══")
d = get("/rvc/devices", 20)
ie, oe = d.get("inputs_ex") or {}, d.get("outputs_ex") or {}
vis_i = [x for x in ie.get("devices", []) if not x.get("hidden")]
vis_o = [x for x in oe.get("devices", []) if not x.get("hidden")]
print(f"ok={d.get('ok')} source={d.get('source')} | 输入 91→常用{len(vis_i)} | 输出 60→常用{len(vis_o)}")
print("输入常用:", [x["label"] for x in vis_i])
print("输出常用:", [x["label"] for x in vis_o])
print("输入推荐:", (ie.get("pick") or {}).get("label"), "|", (ie.get("pick") or {}).get("reason"))
print("输出推荐:", (oe.get("pick") or {}).get("label"), "|", (oe.get("pick") or {}).get("reason"))
droid = next((x for x in ie.get("devices",[]) if "DroidCam" in x["label"]), None)
cable = next((x for x in oe.get("devices",[]) if x["group"]=="live"), None)
json.dump(d, open(r"C:\模仿音色\logs\_p0_devices_snapshot.json","w",encoding="utf-8"), ensure_ascii=False, indent=1)

print()
print("═══ 2) /api/audio/mic_test 默认麦(1.5s) ═══")
t0=time.time()
m = get("/api/audio/mic_test?secs=1.5&wav=1", 30)
print(f"ok={m.get('ok')} 用时{time.time()-t0:.1f}s device={m.get('device')} idx={m.get('resolved_index')}")
print(f"底噪={m.get('floor_dbfs')}dB 人声={m.get('speech_dbfs')}dB 峰值={m.get('peak_dbfs')}dB SNR={m.get('snr_db')}dB")
print(f"结论[{m.get('level')}]: {m.get('verdict')} | wav回放={len(m.get('wav_b64') or '')//1024}KB")

print()
print("═══ 3) /api/audio/mic_test 指定 DroidCam(设备名解析) ═══")
if droid:
    m2 = get("/api/audio/mic_test?secs=1&wav=0&device=" + urllib.parse.quote(droid["value"]), 30)
    print(f"传入: {droid['value']!r}")
    print(f"ok={m2.get('ok')} 解析到 sd 索引={m2.get('resolved_index')} 峰值={m2.get('peak_dbfs')}dB")
    print(f"结论: {m2.get('verdict','')} {m2.get('detail','')}")

print()
print("═══ 4) /api/audio/output_test → CABLE(回环自证,人耳无感) ═══")
if cable:
    o = get("/api/audio/output_test?device=" + urllib.parse.quote(cable["value"]), 30)
    pr = o.get("probe") or {}
    print(f"传入: {cable['value']!r}")
    print(f"ok={o.get('ok')} sr={o.get('samplerate')} probe: heard={pr.get('heard')} peak={pr.get('peak_dbfs')}dB idx={pr.get('device_index')} err={pr.get('err')}")
    print("hint:", o.get("hint"))

print()
print("═══ 5) /api/env_check 人话化 ═══")
e = get("/api/env_check", 30)
for c in e.get("checks", []):
    fx = c.get("fix") or {}
    print(f"  {'✅' if c['ok'] else '❌'} {c.get('cn')}  [{c['item']}]  why={c.get('why','')[:22]}  fix={fx.get('type','')}/{fx.get('label','')}")

print()
print("═══ 6) /api/device/checkup?device=…(修 bug 验证) ═══")
dev = droid["value"] if droid else ""
ck = get("/api/device/checkup?mic_secs=1&src=&device=" + urllib.parse.quote(dev), 60)
mic_item = next((i for i in ck.get("items", []) if i["key"]=="mic"), {})
print(f"总分={ck.get('score')} {ck.get('grade')} | 麦项 detail: {mic_item.get('detail')}")
print("→ detail 应显示传入的设备名而非系统默认:", "PASS" if (dev.split(" (")[0] in mic_item.get("detail","")) else "CHECK")
